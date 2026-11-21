"""Основа aiflow: версия и пути плагина, состояния задачи, настройки по умолчанию, выбор моделей,
время, запуск процессов, JSON-файлы, проверка живых процессов, шаблоны путей.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

# Оболочка, из которой запустили aiflow (Git Bash), оставляет в окружении PWD своего каталога. Дочерние
# программы (opencode) верят PWD больше, чем настоящему рабочему каталогу: ревьюер, запущенный службой в
# копии задачи, открывал репозиторий, из которого когда-то подняли службу, и не находил там PR.
for _stale in ("PWD", "OLDPWD"):
    os.environ.pop(_stale, None)

HERE = Path(__file__).resolve().parent
ENTRY = HERE / "aiflow.py"  # вход CLI: его запускают служба, автозапуск и фоновое ревью
PLUGIN_ROOT = HERE.parent
TEMPLATES = PLUGIN_ROOT / "templates"


def _plugin_version() -> str:
    try:
        return json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


VERSION = _plugin_version()

STAGES = ["new", "planning", "plan_review", "in_progress", "ready", "in_review", "merged", "done"]
# шаг доски для каждого состояния задачи; done — последний шаг «Готово»: на нём стоит всякая законченная задача
STATION = {"new": 0, "planning": 1, "plan_check": 1, "discussing": 1, "answered": 1, "plan_review": 2, "approved": 2,
           "in_progress": 3, "ready": 4, "in_review": 5, "merged": 6, "done": 7}
HISTORY_KEEP = 60  # столько последних переходов хранит задача (защита от роста файла, если стадии зациклятся)


def visited_stations(t: dict) -> set[int]:
    """Шаги доски, через которые задача прошла. Хранятся в самой задаче (stations) и от длины истории не
    зависят. У задачи из прежней версии их нет: тогда считаются по истории, а если её начало уже обрезано,
    всё до самого дальнего шага считается пройденным — пропуски там не узнать."""
    if isinstance(t.get("stations"), list):
        return {0, *(int(i) for i in t["stations"])}
    history = t.get("history") or []
    visited = {0}
    for h in history:
        for key in ("from", "to"):
            if h.get(key) in STATION and h[key] != "done":
                visited.add(STATION[h[key]])
    if len(history) >= HISTORY_KEEP:
        visited |= set(range(max(visited) + 1))
    return visited
# approved — план одобрен (или повтор с готовым PR), задача ждёт слот исполнителя
SIDE_STATES = ["needs_human", "cancelled", "discussing", "answered", "approved"]
TALK_KINDS = ("discuss", "ask")  # разговор в сессии планировщика: PR только по договорённости
IMPLEMENTING = {"planning", "in_progress"}
TERMINAL = {"merged", "done", "cancelled"}
FINISHED = {"merged", "done"}  # задача выполнена: зависимые могут стартовать
COMPLEXITIES = ("trivial", "simple", "complex")  # оценивает планировщик; trivial вручную — сразу исполнитель
WORKER_MD = Path(__file__).resolve().parent.parent / "agents" / "worker.md"
# Части инструкций планировщика, которые он читает по надобности, а не носит в контексте каждой сессии.
PLANNER_PLAN_MD = PLUGIN_ROOT / "prompts" / "planner-plan.md"  # сложная задача: draft PR, шаблон плана, UI, доработка
PLANNER_TALK_MD = PLUGIN_ROOT / "prompts" / "planner-talk.md"  # вопрос о коде и обсуждение идеи
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
MARKERS = ("<!-- aiflow", "<!-- ai-review", "<!-- human")
HUMAN_MARKER = "<!-- human -->"
AMEND_MARKER = "<!-- aiflow:amend -->"
BOT_LOGINS = {"github-actions", "dependabot", "codecov", "vercel", "netlify", "sonarcloud", "coderabbitai", "copilot"}
WORKER_STAGES = {"in_progress", "ready", "in_review"}
LIMIT_RE = re.compile(
    r"(usage|rate)[ -]?limit|limit (reached|exceeded)|out of (usage|credits)|"
    r"resets? (at|in) |too many requests|(error|status|http)\W{0,3}429\b", re.I)
LIMIT_LOG_TAIL = 2000  # лимит ищем только в самом конце лога: выше — текст самой задачи
LIMIT_HITS_MAX = 3  # столько пауз подряд по одной сессии без её продвижения; дальше — не лимит
# причины паузы, которые снимает человек командой aiflow resume; остальные снимает их же условие
MANUAL_PAUSES = ("manual", "rate_limit", "trust")
# Причины, которые держат только ревью и мерж. Красная базовая ветка не мешает ни планировать, ни писать код:
# перед ревью ветка задачи всё равно подтягивает свежую базовую. А вот мержить поверх сломанной ветки нельзя.
REVIEW_PAUSES = ("integration_red",)
# Остальные причины «Продолжить очередь» не снимает: они уходят сами, когда исчезает их условие. Здесь — что
# для этого сделать; текст идёт в панель, в событие и в ответ команды aiflow resume.
PAUSE_LIFTS = {
    "integration_red": "ревью и мерж продолжатся сами, когда сборка базовой ветки позеленеет",
    "base_missing": "пауза снимется сама, когда базовая ветка появится на origin: создайте её или поправьте "
                    "base_branch в .aiflow/config.json",
}
PAUSE_NAMES = {"integration_red": "базовая ветка красная", "base_missing": "нет базовой ветки"}


def queue_pauses(pauses: dict | None) -> list[str]:
    """Причины, по которым очередь стоит целиком: новые сессии не запускаются."""
    return sorted(r for r in pauses or {} if r not in REVIEW_PAUSES)


def kept_pauses(pauses: dict) -> list[str]:
    """Причины паузы, которые команда «продолжить» не снимает."""
    return sorted(r for r in pauses or {} if r not in MANUAL_PAUSES)


def blocking_pauses(pauses: dict | None, task: dict | None = None) -> list[str]:
    """Причины, по которым ревью и мерж этой задачи ждут: любая пауза очереди и красная базовая ветка.
    Задачу, которая чинит базовую ветку (base_fix), красная ветка не держит: иначе чинить её было бы некому."""
    reasons = sorted(pauses or {})
    if task and task.get("base_fix"):
        reasons = [r for r in reasons if r != "integration_red"]
    return reasons


def kept_pause_text(reasons: list[str]) -> str:
    """Почему очередь всё ещё стоит и что с этим делать — одной строкой."""
    return "; ".join(PAUSE_NAMES.get(r, r) + " — " + PAUSE_LIFTS.get(r, "пауза снимется сама") for r in reasons)

# «Нужен человек»: что именно от него нужно. Причина говорит, что случилось; это — что сделать.
WHAT_TO_DO = {
    "tool": "Это сбой инструмента, а не кода. Нажмите «Повторить»; если повторится — проверьте opencode и модель "
            "ревьюера (раздел «Настройки»).",
    "rounds": "Ревьюер и исполнитель не договорились за отведённые раунды. Откройте PR и решите спор: комментарий "
              "уйдёт исполнителю, затем «Повторить»; либо смержите PR или снимите задачу сами.",
    "ci": "CI не удаётся починить. Посмотрите упавшие проверки в PR; подскажите комментарием, что делать, и "
          "нажмите «Повторить».",
    "session": "Сессия остановилась и сама не продолжила. «Повторить» возобновит её с того же места; «Открыть "
               "сессию» покажет, на чём она встала.",
    "silent": "Сессия закончила ход и не сообщила результат. «Открыть сессию» — прочитайте её последний ответ и "
              "ответьте ей; либо «Повторить», чтобы она продолжила.",
    "asks": "Сессия просит вашего решения. «Открыть сессию» и ответьте ей в чате: задача продолжится сама, "
            "«Повторить» не нужен.",
    "merge": "Смержите PR сами либо проверьте автомерж и требования защиты ветки. После мержа задача закроется сама.",
    "report": "Сессия называет не тот PR. «Открыть сессию» и поправьте её; либо «Повторить».",
    "deps": "Почините то, от чего задача зависит, и нажмите «Повторить». Либо уберите зависимость: "
            "aiflow after <задача> <зависимость> --remove. Либо «Снять» эту задачу.",
    "label": "На PR стоит метка needs-human. Разберитесь в PR и нажмите «Повторить» — метка снимется.",
    "closed": "PR уже закрыт. Если задача не нужна — «Снять»; если нужна — откройте PR заново и «Повторить».",
    "stall": "Сессия числится занятой, но ревью давно не двигается: скорее всего, зависла её команда. «Открыть "
             "сессию», остановите команду (Esc) и напишите «продолжай»; либо «Повторить».",
    "noci": "Проверки CI на PR так и не появились. Посмотрите вкладку Actions: работает ли раннер и запускается ли "
            "workflow на pull_request для этих файлов. Когда проверки появятся — «Повторить». Если CI для этого "
            "репозитория не нужен — require_ci: false в .aiflow/config.json.",
    "paths": "Задаче нужен защищённый путь: «Разрешить пути…», либо попросите убрать эту правку комментарием в PR.",
}
REVIEW_ACTIONS = {"error": "tool", "no_ci": "noci", "limit_reached": "rounds", "already_closed": "closed",
                  "blocked": "asks"}
# Те же ключи — коды причины в истории задачи (history[].code) и строки статистики: что сломалось.
HUMAN_CODES = {
    "tool": "сбой инструмента: ревьюер, opencode, сеть",
    "rounds": "ревьюер и исполнитель не договорились за отведённые раунды",
    "ci": "CI не удалось починить",
    "noci": "проверки CI не появились",
    "paths": "защищённые пути",
    "session": "сессия остановилась и не продолжила",
    "silent": "сессия закончила ход без отчёта",
    "asks": "сессия просит решения",
    "stall": "ревью зависло",
    "merge": "одобренный PR не мержится",
    "report": "отчёт сессии не принят",
    "deps": "сломана зависимость",
    "label": "метка needs-human на PR",
    "closed": "PR закрыт",
    "other": "прочее",
}
assert set(WHAT_TO_DO) <= set(HUMAN_CODES)
# У переходов, записанных до появления кодов, причина — только текст: код узнаётся по словам, первое совпадение.
HUMAN_CODE_WORDS = (
    ("paths", ("защищ", "protected")),
    ("noci", ("ни одной проверки", "проверки ci так и не")),
    ("rounds", ("лимит раундов",)),
    ("ci", ("ci красный", "упал ci", "ci не ")),
    ("tool", ("json", "opencode", "ревьюер", "таймаут", "tls", "http ", "gh pr ", "gh api", "не ответил")),
    ("merge", ("не смержен",)),
    ("label", ("needs-human",)),
    ("deps", ("зависим",)),
    ("report", ("отчёт", "без номера pr")),
    ("stall", ("не двигается",)),
    ("silent", ("закончил ход", "не доведя")),
    ("session", ("сесси",)),
    ("closed", ("закрыт",)),
)


CI_PASS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
CI_FAIL = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}


def ci_summary(rollup: list | None, sha: str | None = None) -> dict:
    """Проверки CI коммита одним словарём: сколько прошло, упало, ещё идёт."""
    out = {"sha": sha, "total": 0, "passed": 0, "failed": 0, "pending": 0}
    for c in rollup or []:
        v = str(c.get("conclusion") or c.get("state") or "").upper()
        out["total"] += 1
        out["passed" if v in CI_PASS else "failed" if v in CI_FAIL else "pending"] += 1
    return out


def ci_text(ci: dict | None) -> str:
    if not ci or not ci.get("total"):
        return ""
    text = f"прошло {ci['passed']} из {ci['total']}"
    return text + (f", упало {ci['failed']}" if ci.get("failed") else "")


def short_reason(text: str, limit: int = 220) -> str:
    """Причина для панели и уведомлений: первая содержательная строка. Полный текст — в report.json."""
    line = next((x.strip() for x in str(text or "").splitlines() if x.strip()), "")
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"

EFFORTS = ("low", "medium", "high", "xhigh", "max")


def by_complexity(value, cx: str):
    """Настройка модели/effort: словарь {"simple": …, "complex": …} или одно значение на все задачи."""
    if isinstance(value, dict):
        return value.get(cx) or value.get("simple") or None
    return value or None


# Имя модели для `claude --model`: псевдоним (fable, opus, sonnet, haiku) или полный id.
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]{0,63}$")
MODEL_FIELDS = ("planner_model", "planner_effort", "worker_model", "worker_effort")


def check_model_field(field: str, value) -> str | None:
    """Проверить значение настройки модели задачи; пустое и «default» — «как в настройках проекта»."""
    v = str(value or "").strip()
    if not v or v.lower() in ("default", "по умолчанию", "none"):
        return None
    if field.endswith("_effort"):
        if v.lower() not in EFFORTS:
            raise AiflowError(f"{field}: effort должен быть одним из {', '.join(EFFORTS)}")
        return v.lower()
    if not MODEL_NAME_RE.match(v):
        raise AiflowError(f"{field}: {v!r} не похоже на имя модели (fable, opus, sonnet, haiku или полный id)")
    return v


def planner_choice(cfg: dict, t: dict, kind_default: tuple | None = None) -> tuple:
    """Модель и effort планировщика: сначала заданные задаче, затем настройки проекта. Сложность до него
    не известна — её оценивает он сам, поэтому из старого словаря {"simple": …, "complex": …} берётся
    значение для complex. kind_default — настройки для вопроса или обсуждения (ask_*, discuss_*)."""
    model, effort = kind_default or (by_complexity(cfg["planner_model"], "complex"),
                                     by_complexity(cfg["planner_effort"], "complex"))
    return t.get("planner_model") or model, t.get("planner_effort") or effort


def context_choice(cfg: dict, t: dict, role: str) -> int:
    """Окно контекста сессии роли в токенах (planner_context, worker_context); 0 — не задавать."""
    value = cfg.get("planner_context") if role == "planner" else by_complexity(
        cfg.get("worker_context"), t.get("complexity") or "simple")
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(100000, min(n, 1000000)) if n > 0 else 0  # пределы флага --autocompact


PATH_ASK_RE = re.compile(r"защищ|protected|allow-path", re.I)


def wanted_paths(t: dict, review: dict | None, cfg: dict) -> list[str]:
    """Защищённые пути, которые задаче нужны, но ещё не разрешены. Берутся из замечаний последнего ревью
    (файл замечания защищён) и из запроса самой сессии. Держатся, пока путь не разрешат: вопрос сессии
    меняется и пропадает из панели, стоит ей ответить, а путь по-прежнему нужен."""
    if t.get("state") in TERMINAL:
        return []
    allowed = t.get("allowed_protected_paths") or []
    protected = cfg.get("protected_paths") or []
    out = []
    for c in (review or {}).get("comments") or []:
        f = str(c.get("file") or "").replace("\\", "/")
        if f and path_matches(f, protected) and not path_matches(f, allowed) and f not in out:
            out.append(f)
    for f in t.get("path_request") or []:
        if not path_matches(f, allowed) and f not in out:
            out.append(f)
    return out


def unknown_model_note(model: str, known: list[str]) -> str:
    """Пояснение, если модели ревьюера нет в списке opencode: чаще всего это опечатка в имени."""
    if not model or not known or model in known:
        return ""
    import difflib
    near = difflib.get_close_matches(model, known, n=2, cutoff=0.6)
    return (f"модели «{model}» нет в списке opencode models" + (f" — возможно, вы имели в виду {' или '.join(near)}" if near else "")
            + ". Поправьте модель ревьюера в панели («Настройки») или reviewer_model в .aiflow/config.json")


def is_direct(t: dict) -> bool:
    """Задача без плана: исполнитель делает её сразу (поручил планировщик или trivial вручную)."""
    return bool(t.get("direct")) or t.get("complexity") == "trivial"


def worker_choice(cfg: dict, t: dict) -> tuple:
    """Модель и effort worker'а: настройки задачи, затем Opus-effort, затем по сложности."""
    cx = t.get("complexity") or "simple"
    model = t.get("worker_model") or by_complexity(cfg["worker_model"], cx)
    effort = t.get("worker_effort")
    if not effort and "opus" in str(model or "").lower():
        effort = cfg.get("worker_opus_effort") or None
    return model, effort or by_complexity(cfg["worker_effort"], cx)

DEFAULT_CONFIG = {
    "concurrency": 2,
    "tick_seconds": 45,
    "base_branch": "master",
    "ui_port": 8765,
    # auto — безопасные действия одобряет классификатор Claude Code, без вопросов вам; deny из
    # .claude/settings.json действуют и в нём. acceptEdits — спрашивать о каждой команде вне allow.
    "permission_mode": "auto",
    "planner_model": "opus",  # каждую задачу сначала получает он: план, поручение исполнителю или сам
    "planner_effort": "medium",
    # Окно контекста сессии в токенах (claude --autocompact): дойдя до него, сессия сжимает контекст.
    # 0 — как решит Claude Code: у Sonnet и Opus это ~1 млн, и каждый запрос перечитывает весь контекст.
    "planner_context": 180000,
    "discuss_model": "opus",  # сессия обсуждения идеи (задача kind: discuss)
    "discuss_effort": "high",
    "ask_model": "sonnet",  # вопрос о коде (задача kind: ask)
    "ask_effort": "medium",
    "answered_ttl_hours": 24,  # вопрос с ответом без продолжения закрывается сам
    "session_keep_days": 14,  # сессии законченной задачи живут ещё столько дней: их читают и продолжают. 0 — не удалять
    # Исполнитель по оценке планировщика: trivial — лёгкая задача без плана (механическая правка по точным
    # указаниям), simple — обычная, complex — сложная реализация по плану. Или одна строка на все задачи.
    "worker_model": {"trivial": "haiku", "simple": "sonnet", "complex": "opus"},
    "worker_effort": {"trivial": "high", "simple": "medium", "complex": "high"},  # или одна строка на все задачи
    "worker_context": 180000,  # окно контекста исполнителя; число или словарь по сложности, как worker_effort
    "worker_opus_effort": "medium",  # effort worker'а на Opus, без учёта сложности (перекрывает worker_effort)
    # Кто ищет по коду для планировщика и исполнителя: aiflow — подагент плагина aiflow:explorer;
    # project — как принято в самом проекте (его агенты из .claude/agents, CLAUDE.md): aiflow ничего не подсказывает
    "explorer": "aiflow",
    # одна строка о проекте: её видят планировщики соседних проектов службы, когда решают, куда поставить задачу
    "description": "",
    # задачи других проектов, поставленные «после» задачи этого, ждут не только мержа, но и workflow на коммите
    # мержа (публикация пакета): пока они идут — ждут, упали — задача уходит к человеку
    "wait_workflows": True,
    "reviewer_model": "",
    "review_identity": "self",  # self — ревью и мерж от вашего gh; bot — от бота с токеном REVIEW_BOT_TOKEN
    # Ревью плана другой моделью (тем же ревьюером opencode) до того, как план увидите вы: advisory — ревьюер
    # пишет замечания, планировщик один раз отвечает на них, и план идёт к вам в любом случае; off — выключено
    "plan_check": "advisory",
    "plan_check_rounds": 1,  # сколько раз ревьюер смотрит план одной задачи
    "plan_check_wait_minutes": 20,  # сколько ждать ответа планировщика на замечания, прежде чем показать план вам
    "max_rounds": 5,
    "nit_rounds": 1,  # в скольких первых раундах nit блокирует одобрение; дальше одобряется, если blocking нет
    "reviewer_idle_minutes": 5,  # ревьюер opencode не присылает событий дольше этого — завис: остановить
    "reviewer_timeout_minutes": 45,  # общий предел одного прогона ревьюера; большой PR читается долго
    # ветка PR не отстаёт от базовой — aiflow запускает ревью сам, не будя сессию исполнителя: ход «rebase,
    # тесты, aiflow review» без rebase механический, а пробуждение модели стоит загрузки всего её контекста
    "auto_review": True,
    "max_ci_fixes": 5,  # сколько раз подряд aiflow review возвращает ci_failed, прежде чем звать человека
    "review_concurrency": 2,  # сколько PR на ревью сразу; параллельно — только PR без общих файлов
    "merge_wait_minutes": 60,  # одобренный PR не смержился за это время — к человеку, очередь ревью идёт дальше
    "require_ci": "auto",  # auto — проверки CI обязательны, если в репозитории есть workflow; true; false
    # сколько ждать появления проверок на свежем коммите: GitHub создаёт запуск не сразу, под нагрузкой —
    # через несколько минут после push
    "ci_appear_minutes": 10,
    # что монитор пишет оркестратору (каждая строка будит модель): answers — только ответы на вопросы
    # (kind: ask); important — всё, что ждёт вас; all — все события; none — ничего
    "notify": "answers",
    "desktop_notify": True,  # системные уведомления о том, что ждёт вас (события NOTIFY_KINDS), от notify не зависят
    "auto_resume": 1,  # сколько раз подряд монитор сам возобновляет остановленную/пропавшую сессию; 0 — никогда
    "max_diff_chars": 150000,
    "ci_timeout_minutes": 45,
    "rate_limit_pause_minutes": 30,
    "watch_integration": True,
    # Базовая ветка покраснела после мержа. auto — aiflow чинит её сам: перезапускает упавшие задания сборки, а
    # если они падают снова, ставит задачу на починку; эта задача идёт несмотря на паузу. off — только пауза.
    "base_fix": "auto",
    "base_fix_reruns": 1,  # сколько раз перезапустить упавшие задания, прежде чем ставить задачу; 0 — сразу задача
    "labels": {"approved": "plan-approved", "needs_human": "needs-human", "retry": "retry"},
    "protected_paths": [
        ".github/**", ".claude/**", ".opencode/**", ".aiflow/**",
        "design/**", "components/ui/**",
        "**/migrations/**", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock",
        "pyproject.toml", "package.json", "tsconfig*.json", ".eslintrc*", "eslint.config.*",
        "jest.config*", "vitest.config*", "pytest.ini", "setup.cfg",
        # «**/auth/**» здесь нет намеренно: под него попадает и папка экрана входа во фронтенде (вёрстка, её
        # тесты), и каждая такая правка уходила к человеку. Серверный код аутентификации защищается путём
        # самого проекта, например "src/Server/Auth/**".
        "**/payments/**", "infra/**", "**/*.env*",
    ],
}


# события, которые ждут пользователя: системные уведомления и строки монитора при notify: important
NOTIFY_KINDS = {"answered", "plan_ready", "plan_updated", "needs_input", "needs_human", "merged", "done", "amend_lost", "paused", "unpaused", "error", "batch_done", "cancelled"}
# Снятие задачи при notify: important будит оркестратора (чтобы закрыть его вопрос по ней), но без системного уведомления.
NO_TOAST_KINDS = {"cancelled"}


def notify_level(cfg: dict) -> str:
    v = str(cfg.get("notify") or "answers").lower()
    return v if v in ("answers", "important", "all", "none") else "answers"


def should_notify(cfg: dict, kind: str) -> bool:
    level = notify_level(cfg)
    if level == "answers":
        return kind == "answered"
    return level == "all" or (level == "important" and kind in NOTIFY_KINDS)


def notify_status(cfg: dict) -> bool:
    """Писать ли в чат строки о самом мониторе и службе (запущен, остановлена, сбой)."""
    return notify_level(cfg) in ("important", "all")


# ---------------------------------------------------------------- utilities

class AiflowError(Exception):
    pass


class TickBusy(AiflowError):
    """Проход по проекту уже идёт в другом процессе."""


def aiflow_home() -> Path:
    return Path(os.environ.get("AIFLOW_HOME") or Path.home() / ".aiflow")


def inbox_dirs() -> tuple[Path, Path]:
    """Входящие для работающих сессий: файл <session_id>.json забирает хук плагина
    (scripts/inbox_hook.py) и перекладывает в inbox-delivered — по нему монитор отмечает доставку."""
    h = aiflow_home()
    return h / "inbox", h / "inbox-delivered"


def is_human_comment(c: dict) -> bool:
    """Комментарий человека к PR: без служебных маркеров aiflow и не от бота."""
    body = (c.get("body") or "").strip()
    login = ((c.get("author") or {}).get("login") or "").lower()
    return bool(body) and not any(m in body for m in MARKERS) and not login.endswith("[bot]") \
        and login not in BOT_LOGINS


def after(a: str | None, b: str | None) -> bool:
    """a позже b (ISO-время); пустое b — всегда раньше."""
    ta, tb = parse_iso(a), parse_iso(b)
    return bool(ta) and (tb is None or ta > tb)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime | None = None) -> str:
    return (t or now()).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        v = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def age_minutes(s: str | None) -> float:
    t = parse_iso(s)
    return (now() - t).total_seconds() / 60 if t else 1e9


NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW


def kill_tree(proc: subprocess.Popen) -> None:
    """Убить процесс вместе с потомками. Просто proc.kill() мало: команда часто запущена через обёртку
    (opencode.CMD → cmd.exe → opencode.exe), обёртка умирает, а потомок живёт и держит наши каналы
    вывода — и ожидание конца вывода не кончается никогда."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30, **NO_WINDOW)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_tree(cmd, *, cwd=None, timeout: float = 120, env=None, input: str | None = None) -> subprocess.CompletedProcess:
    """subprocess.run с настоящим таймаутом: по его истечении убивается всё дерево процессов.
    Бросает subprocess.TimeoutExpired и FileNotFoundError, как subprocess.run."""
    group = NO_WINDOW if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
                            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **group)
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            proc.communicate(timeout=10)  # забрать остатки вывода и освободить каналы
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def run(cmd: list[str], *, cwd=None, timeout: int = 120, env=None, input: str | None = None):
    try:
        return run_tree(cmd, cwd=cwd, timeout=timeout, env=env, input=input)
    except FileNotFoundError:
        raise AiflowError(f"не найдено: {cmd[0]}") from None
    except subprocess.TimeoutExpired:
        raise AiflowError(f"таймаут {timeout} с: {' '.join(cmd[:3])}") from None


# Сбой сети до отправки запроса: повторять безопасно всегда.
GH_NET_SAFE = re.compile(r"TLS handshake timeout|dial tcp|no such host|connection refused|could not resolve|"
                         r"network is unreachable", re.I)
# Сбой после отправки: запрос мог дойти — повторяем только чтение.
GH_NET_ANY = re.compile(r"i/o timeout|connection reset|unexpected EOF|\bEOF\b|context deadline exceeded|"
                        r"timeout awaiting|HTTP 50[0234]|HTTP 429|was submitted too quickly|secondary rate limit", re.I)
GH_RETRY_DELAYS = (3, 10, 30)


def gh_is_read(args) -> bool:
    a = [str(x) for x in args]
    if a[:1] == ["api"]:
        if "--method" in a or "-X" in a:
            i = a.index("--method") if "--method" in a else a.index("-X")
            return i + 1 < len(a) and a[i + 1].upper() == "GET"
        return not any(x.lstrip().startswith("query=mutation") or "mutation" in x.split("{", 1)[0]
                       for x in a if x.startswith("query="))
    return a[:2] in (["pr", "view"], ["pr", "diff"], ["pr", "checks"], ["pr", "list"], ["run", "view"],
                     ["run", "list"], ["repo", "view"])


def gh_with_retry(call, args):
    """Выполнить gh с повтором при сетевых сбоях GitHub (TLS timeout и т. п.): иначе разовый сбой
    сети уводит задачу в «нужен человек». call() → CompletedProcess."""
    p = call()
    for delay in GH_RETRY_DELAYS:
        out = (p.stderr or "") + (p.stdout or "")
        if p.returncode == 0 or not (GH_NET_SAFE.search(out) or (gh_is_read(args) and GH_NET_ANY.search(out))):
            return p
        time.sleep(delay)
        p = call()
    return p


def need(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise AiflowError(f"'{name}' не найден в PATH")
    return path


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # На Windows замена падает, пока файл открыт другим процессом (панель, монитор) — повторяем.
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 39:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def read_json(path: Path, default=None):
    for attempt in range(40):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return default
        except PermissionError:  # Windows: файл как раз заменяется другим процессом
            if attempt == 39:
                return default
            time.sleep(0.05)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_BOOT: list[float] = []


def boot_time() -> float | None:
    """Время загрузки системы (unix), если удаётся узнать."""
    if _BOOT:
        return _BOOT[0] or None
    bt = 0.0
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k32.GetTickCount64.restype = ctypes.c_ulonglong
            bt = time.time() - k32.GetTickCount64() / 1000
        elif Path("/proc/uptime").exists():
            bt = time.time() - float(Path("/proc/uptime").read_text().split()[0])
        else:
            p = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5)
            m = re.search(r"sec = (\d+)", p.stdout)
            bt = float(m.group(1)) if m else 0.0
    except Exception:
        bt = 0.0
    _BOOT.append(bt)
    return bt or None


def process_start(pid: int) -> float | None:
    """Время запуска процесса (unix); только Windows, иначе None."""
    if os.name != "nt" or pid <= 0:
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return None
    try:
        c, e, k, u = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
            return None
        ft = (c.dwHighDateTime << 32) | c.dwLowDateTime
        return ft / 1e7 - 11644473600
    finally:
        k32.CloseHandle(h)


def owner_alive(info: dict | None) -> int:
    """pid из lock/pid-файла, если его владелец всё ещё жив, иначе 0.
    Pid переиспользуются: после перезагрузки или долгой паузы в файле может стоять pid чужого
    процесса. Такой файл считается брошенным — служба не должна молча не стартовать
    и тем более убивать чужой процесс."""
    info = info or {}
    pid = int(info.get("pid") or 0)
    if not pid_alive(pid):
        return 0
    at = parse_iso(info.get("at"))
    if at:
        ts = at.timestamp()
        bt = boot_time()
        if bt and ts < bt - 60:
            return 0  # файл записан до загрузки системы
        st = process_start(pid)
        if st and st > ts + 60:
            return 0  # процесс с этим pid запущен позже, чем записан файл
    return pid


def glob_to_re(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 3] == "**/":
                out += r"(?:.*/)?"
                i += 3
                continue
            if pattern[i:i + 2] == "**":
                out += r".*"
                i += 2
                continue
            out += r"[^/]*"
        elif c == "?":
            out += r"[^/]"
        else:
            out += re.escape(c)
        i += 1
    # без учёта регистра: на Windows и macOS Migrations/ и migrations/ — одна папка, защита fail-closed
    return re.compile(r"^" + out + r"$", re.I)


def path_matches(path: str, patterns: list[str]) -> bool:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return any(glob_to_re(p).match(path) for p in patterns)


def repo_root(start: Path | None = None) -> Path:
    cwd = str(start) if start else None
    p = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd)
    if p.returncode != 0:
        p = run(["git", "rev-parse", "--git-common-dir"], cwd=cwd)
        if p.returncode != 0:
            raise AiflowError("это не git-репозиторий")
        common = (Path(cwd or os.getcwd()) / p.stdout.strip()).resolve()
    else:
        common = Path(p.stdout.strip())
    if common.name == ".git":
        return common.parent
    top = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
    return Path(top.stdout.strip())


STAGE_LABEL = {
    "new": "в очереди", "discussing": "обсуждение идеи", "answered": "ответ дан", "planning": "планирование", "plan_check": "ревью плана", "plan_review": "обсуждение плана",
    "approved": "ждёт исполнителя", "in_progress": "реализация", "ready": "ждёт ревью", "in_review": "ревью",
    "merged": "смержено", "done": "выполнено", "needs_human": "нужен человек", "cancelled": "снята",
}
