#!/usr/bin/env python3
"""
aiflow — очередь задач для фоновых сессий Claude Code.

Состояние живёт в <repo>/runs/, сессии хостит supervisor Claude Code,
aiflow только решает, когда и кого запустить, и записывает результат.
Только стандартная библиотека Python 3.9+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent
TEMPLATES = PLUGIN_ROOT / "templates"


def _plugin_version() -> str:
    try:
        return json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


VERSION = _plugin_version()

STAGES = ["new", "planning", "plan_review", "in_progress", "ready", "in_review", "merged"]
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

# «Нужен человек»: что именно от него нужно. Причина говорит, что случилось; это — что сделать.
WHAT_TO_DO = {
    "tool": "Это сбой инструмента, а не кода. Нажмите «Повторить»; если повторится — проверьте opencode и модель "
            "ревьюера (раздел «Настройки»).",
    "rounds": "Ревьюер и исполнитель не договорились за отведённые раунды. Откройте PR и решите спор: комментарий "
              "уйдёт исполнителю, затем «Повторить»; либо смержите PR или снимите задачу сами.",
    "ci": "CI не удаётся починить. Посмотрите упавшие проверки в PR; подскажите комментарием, что делать, и "
          "нажмите «Повторить».",
    "size": "Посмотрите изменение в PR. Устраивает — «Повторить» (разрешит ревью и мерж), нет — «Снять».",
    "session": "Сессия остановилась и сама не продолжила. «Повторить» возобновит её с того же места; «Открыть "
               "сессию» покажет, на чём она встала.",
    "silent": "Сессия закончила ход и не сообщила результат. «Открыть сессию» — прочитайте её последний ответ и "
              "ответьте ей; либо «Повторить», чтобы она продолжила.",
    "asks": "Сессия просит вашего решения. «Открыть сессию» и ответьте ей в чате: задача продолжится сама, "
            "«Повторить» не нужен.",
    "merge": "Смержите PR сами либо проверьте автомерж и требования защиты ветки. После мержа задача закроется сама.",
    "report": "Сессия называет не тот PR. «Открыть сессию» и поправьте её; либо «Повторить».",
    "deps": "«Снять» эту задачу либо поставьте зависимость заново через /aiflow:intake и нажмите «Повторить».",
    "label": "На PR стоит метка needs-human. Разберитесь в PR и нажмите «Повторить» — метка снимется.",
    "closed": "PR уже закрыт. Если задача не нужна — «Снять»; если нужна — откройте PR заново и «Повторить».",
    "stall": "Сессия числится занятой, но ревью давно не двигается: скорее всего, зависла её команда. «Открыть "
             "сессию», остановите команду (Esc) и напишите «продолжай»; либо «Повторить».",
    "noci": "Проверки CI на PR так и не появились. Посмотрите вкладку Actions: работает ли раннер и запускается ли "
            "workflow на pull_request для этих файлов. Когда проверки появятся — «Повторить». Если CI для этого "
            "репозитория не нужен — require_ci: false в runs/config.json.",
    "paths": "Задаче нужен защищённый путь: «Разрешить пути…», либо попросите убрать эту правку комментарием в PR.",
}
REVIEW_ACTIONS = {"error": "tool", "no_ci": "noci", "limit_reached": "rounds", "too_large": "size", "already_closed": "closed",
                  "blocked": "asks"}


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


def review_activity(store: "Store", t: dict, session_state: str | None, max_rounds: int) -> str | None:
    """Чем сейчас занято ревью задачи: ожидание CI, ревьюер opencode, мерж или работа исполнителя.
    Фазу пишет сам aiflow review (runs/<id>/phase.json), пока работает; сводку CI — проход очереди."""
    st = t.get("state")
    ci = ci_text(t.get("ci"))
    if st == "ready":
        return "в очереди на ревью" + (f"; CI: {ci}" if ci else "")
    if st != "in_review":
        return None
    ph = read_json(store.runs / t["id"] / "phase.json", {}) or {}
    if ph and owner_alive(ph):
        mins = int(age_minutes(ph.get("since") or ph.get("at")))
        took = f" ({mins} мин)" if mins >= 1 else ""
        kind = ph.get("phase")
        if kind == "ci_appear":
            return "ждёт, пока GitHub запустит CI" + took
        if kind == "ci":
            return "ждёт CI" + (f": {ci}" if ci else "") + took
        if kind == "reviewer":
            more = [str(ph["doing"])] if ph.get("doing") else []
            if ph.get("steps"):
                more.append(f"шаг {ph['steps']}")
            if ph.get("tokens"):
                more.append(f"{fmt_tokens(int(ph['tokens']))} токенов")
            return (f"ревьюер opencode, раунд {ph.get('round') or '?'}/{max_rounds}"
                    + (": " + ", ".join(more) if more else "") + took)
        if kind == "merge":
            return "мерж" + took
    if t.get("approved_sha"):
        return "одобрен, ждёт мержа" + (f"; CI: {ci}" if ci else "")
    if t.get("auto_review"):
        return "aiflow запускает ревью"
    if session_state == "working":
        return "исполнитель: rebase, тесты или правки"
    return None


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
    "worker_opus_effort": "medium",  # effort worker'а на Opus, без учёта сложности (перекрывает worker_effort)
    # Кто ищет по коду для планировщика и исполнителя: aiflow — подагент плагина aiflow:explorer;
    # project — как принято в самом проекте (его агенты из .claude/agents, CLAUDE.md): aiflow ничего не подсказывает
    "explorer": "aiflow",
    "reviewer_model": "",
    "review_identity": "self",  # self — ревью и мерж от вашего gh; bot — от бота с токеном REVIEW_BOT_TOKEN
    "max_rounds": 5,
    "nit_rounds": 1,  # в скольких первых раундах nit блокирует одобрение; дальше одобряется, если blocking нет
    "reviewer_idle_minutes": 5,  # ревьюер opencode не присылает событий дольше этого — завис: остановить
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
    "direct_max_files": 6,  # предел изменения для задачи без плана; больше — решает человек
    "direct_max_lines": 300,
    # что монитор пишет оркестратору (каждая строка будит модель): answers — только ответы на вопросы
    # (kind: ask); important — всё, что ждёт вас; all — все события; none — ничего
    "notify": "answers",
    "desktop_notify": True,  # системные уведомления о том, что ждёт вас (события NOTIFY_KINDS), от notify не зависят
    "auto_resume": 1,  # сколько раз подряд монитор сам возобновляет остановленную/пропавшую сессию; 0 — никогда
    "max_diff_chars": 150000,
    "ci_timeout_minutes": 45,
    "rate_limit_pause_minutes": 30,
    "watch_integration": True,
    "labels": {"approved": "plan-approved", "needs_human": "needs-human", "retry": "retry"},
    "protected_paths": [
        ".github/**", ".claude/**", ".opencode/**", "runs/**",
        "design/**", "components/ui/**",
        "**/migrations/**", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock",
        "pyproject.toml", "package.json", "tsconfig*.json", ".eslintrc*", "eslint.config.*",
        "jest.config*", "vitest.config*", "pytest.ini", "setup.cfg",
        "**/auth/**", "**/payments/**", "infra/**", "**/*.env*",
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
        raise AiflowError(f"не найдено: {cmd[0]}")
    except subprocess.TimeoutExpired:
        raise AiflowError(f"таймаут {timeout} с: {' '.join(cmd[:3])}")


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


# ---------------------------------------------------------------- storage

def sync_queue(q: dict) -> dict:
    """Пересчитать производные поля очереди: paused — главная из причин паузы (сначала бессрочные),
    review_lock — первый PR на ревью."""
    pauses = q.get("pauses") or {}
    if pauses:
        reason = sorted(pauses, key=lambda r: (bool(pauses[r].get("until")), pauses[r].get("at") or ""))[0]
        q["paused"] = {**pauses[reason], "reason": reason, "reasons": sorted(pauses)}
    else:
        q["paused"] = None
    locks = q.get("review_locks") or []
    q["review_lock"] = locks[0] if locks else None
    return q


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.runs = root / "runs"

    @property
    def initialized(self) -> bool:
        return (self.runs / "config.json").is_file()

    def config(self) -> dict:
        user = read_json(self.runs / "config.json", {}) or {}
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
        return cfg

    def migrate_config(self) -> list[str]:
        """Дописать в runs/config.json ключи, появившиеся в новой версии. Заданные значения не меняются:
        строка на месте словаря (например, worker_effort: "high") так и остаётся, списки не дополняются."""
        path = self.runs / "config.json"
        user = read_json(path, None)
        if not isinstance(user, dict):
            return []
        added = []
        for k, v in DEFAULT_CONFIG.items():
            if k not in user:
                user[k] = json.loads(json.dumps(v))
                added.append(k)
            elif isinstance(v, dict) and isinstance(user[k], dict):
                for sk, sv in v.items():
                    if sk not in user[k]:
                        user[k][sk] = sv
                        added.append(f"{k}.{sk}")
        if added:
            write_json(path, user)
        return added

    def set_config(self, key: str, value) -> None:
        """Меняет один ключ в runs/config.json, остальное содержимое файла сохраняет как есть."""
        path = self.runs / "config.json"
        user = read_json(path, None)
        if not isinstance(user, dict):
            raise AiflowError("runs/config.json не найден или повреждён — выполните aiflow init")
        user[key] = value
        write_json(path, user)

    def queue(self) -> dict:
        """pauses — причины паузы {причина: {at, until, note}}: очередь стоит, пока есть хоть одна.
        review_locks — задачи, чьи PR сейчас на ревью. paused и review_lock — производные, для панели."""
        q = read_json(self.runs / "queue.json", {}) or {}
        if not isinstance(q.get("pauses"), dict):  # файл старой версии: одна пауза, один lock
            old = q.get("paused")
            q["pauses"] = {old.get("reason") or "manual": old} if isinstance(old, dict) else {}
        if not isinstance(q.get("review_locks"), list):
            q["review_locks"] = [q["review_lock"]] if q.get("review_lock") else []
        return sync_queue(q)

    def save_queue(self, q: dict) -> None:
        write_json(self.runs / "queue.json", sync_queue(q))

    def approved_path(self, tid: str) -> Path:
        """Отпечаток диффа, который ревьюер одобрил: тот же дифф после rebase повторно не ревьюится."""
        return self.runs / tid / "approved.json"

    def task_path(self, tid: str) -> Path:
        return self.runs / tid / "task.json"

    def notes_path(self, tid: str) -> Path:
        """Заметки планировщика о коде для исполнителя (отчёт plan_ready с --details-file)."""
        return self.runs / tid / "notes.md"

    def task(self, tid: str) -> dict | None:
        return read_json(self.task_path(tid))

    def tasks(self) -> list[dict]:
        out = []
        for p in sorted(self.runs.glob("*/task.json")):
            t = read_json(p)
            if isinstance(t, dict) and t.get("id"):
                out.append(t)
        out.sort(key=lambda t: (t.get("created_at") or "", t["id"]))
        return out

    def save_task(self, t: dict) -> None:
        write_json(self.task_path(t["id"]), t)

    def report(self, tid: str) -> dict | None:
        return read_json(self.runs / tid / "report.json")

    def review(self, tid: str) -> dict | None:
        return read_json(self.runs / tid / "review.json")

    def log(self, tid: str, entry: dict) -> None:
        p = self.runs / tid / "log.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": iso(), **entry}, ensure_ascii=False) + "\n")

    def event(self, kind: str, tid: str | None, text: str) -> dict:
        ev = {"at": iso(), "kind": kind, "task": tid, "text": text}
        self.runs.mkdir(parents=True, exist_ok=True)
        with (self.runs / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev

    def events(self, limit: int = 50) -> list[dict]:
        p = self.runs / "events.jsonl"
        if not p.is_file():
            return []
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    def command(self, cmd: dict) -> None:
        self.runs.mkdir(parents=True, exist_ok=True)
        with (self.runs / "commands.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": iso(), **cmd}, ensure_ascii=False) + "\n")

    def take_commands(self) -> list[dict]:
        p = self.runs / "commands.jsonl"
        if not p.is_file():
            return []
        tmp = self.runs / f".commands.{os.getpid()}.processing"
        try:
            os.replace(p, tmp)
        except OSError:
            return []
        cmds = []
        for line in tmp.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                cmds.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        tmp.unlink(missing_ok=True)
        return cmds


def spawn_review_process(store: "Store", t: dict, cwd: str) -> int | None:
    """Запустить `aiflow review --pr N` отдельным процессом в рабочей копии задачи. → pid или None."""
    log = store.runs / t["id"] / "auto-review.log"
    try:
        out = open(log, "ab")
        flags = {"creationflags": 0x08000000 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
        env = dict(os.environ, PYTHONUTF8="1")
        proc = subprocess.Popen([sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "review", "--pr", str(t["pr"])],
                                cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env, **flags)
        out.close()
        return proc.pid
    except OSError:
        return None


def write_report(root: Path, tid: str, role: str, status: str, pr: int | None = None,
                 summary: str = "", details: str = "", complexity: str | None = None) -> dict:
    store = Store(root)
    if not store.task(tid):
        raise AiflowError(f"задача {tid} не найдена в {store.runs}")
    rep = {"rid": uuid.uuid4().hex, "at": iso(), "role": role, "status": status,
           "pr": pr, "summary": summary.strip(), "details": details.strip()}
    if complexity:
        rep["complexity"] = complexity
    write_json(store.runs / tid / "report.json", rep)
    store.log(tid, {"report": rep})
    return rep


class FileLock:
    """Простой межпроцессный lock на файле с pid владельца."""

    def __init__(self, path: Path, stale_minutes: float = 10):
        self.path = path
        self.stale = stale_minutes
        self.held = False

    def acquire(self, wait_seconds: float = 0, extra: dict | None = None) -> bool:
        deadline = time.time() + wait_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, json.dumps({"pid": os.getpid(), "at": iso(), **(extra or {})}).encode())
                os.close(fd)
                self.held = True
                return True
            except FileExistsError:
                info = read_json(self.path, {}) or {}
                if not info and self._file_age_minutes() < 0.1:
                    time.sleep(0.2)  # файл только что создан, pid ещё не записан
                    continue
                if not owner_alive(info):
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.time() >= deadline:
                    return False
                time.sleep(0.5)

    def _file_age_minutes(self) -> float:
        try:
            return (time.time() - self.path.stat().st_mtime) / 60
        except FileNotFoundError:
            return 1e9

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self.held = False

    def owner(self) -> int:
        return owner_alive(read_json(self.path, {}) or {})

    def data(self) -> dict | None:
        """Содержимое lock-файла, если его владелец жив."""
        info = read_json(self.path, {}) or {}
        return info if owner_alive(info) else None


# ---------------------------------------------------------------- external tools

PLUGIN_BIN_RE = re.compile(r"[\\/]plugins[\\/]cache[\\/][^\\/]+[\\/]aiflow[\\/][^\\/]+[\\/]bin[\\/]?$", re.I)


def claude_env() -> dict:
    """Окружение для вызовов `claude`: без каталогов bin плагина aiflow в PATH.

    Монитор живёт в окружении сессии, где в PATH уже стоит bin той версии плагина, что была
    при её запуске. Фоновый сервис Claude, поднятый из такого окружения, передаёт этот PATH
    всем новым сессиям, и после обновления плагина `aiflow` в них оказывается старым.
    Claude сам добавляет в PATH bin текущей версии."""
    env = dict(os.environ)
    key = next((k for k in env if k.upper() == "PATH"), "PATH")
    parts = env.get(key, "").split(os.pathsep)
    env[key] = os.pathsep.join(x for x in parts if not PLUGIN_BIN_RE.search(x))
    return env


class Claude:
    def __init__(self, root: Path):
        self.root = root
        self.bin = need("claude")
        self.env = claude_env()

    def agents(self) -> dict[str, dict]:
        p = run([self.bin, "agents", "--json", "--all", "--cwd", str(self.root)], cwd=self.root, timeout=60, env=self.env)
        if p.returncode != 0:
            raise AiflowError("claude agents --json: " + (p.stderr or p.stdout).strip()[:300])
        text = p.stdout.strip()
        start = text.find("[")
        data = json.loads(text[start:]) if start != -1 else []
        return {e["id"]: e for e in data if isinstance(e, dict) and e.get("id")}

    @staticmethod
    def _short_id(out: str) -> str | None:
        m = re.search(r"backgrounded\W+([0-9A-Za-z]{6,})", out)
        return m.group(1) if m else None

    def launch(self, agent: str, name: str, prompt: str, *, model=None, effort=None, mode=None,
               worktree: str | None = None) -> tuple[str | None, str]:
        cmd = [self.bin, "--bg", "--agent", agent, "--name", name]
        if worktree:
            # своя рабочая копия на задачу (.claude/worktrees/<id>): сессии не трогают основную папку и
            # друг друга; повторный запуск с тем же именем подхватывает существующий worktree
            cmd += ["--worktree", worktree]
        if mode:
            cmd += ["--permission-mode", mode]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        cmd.append(prompt)
        p = run(cmd, cwd=self.root, timeout=180, env=self.env)
        out = (p.stdout or "") + (p.stderr or "")
        return (self._short_id(out) if p.returncode == 0 else None), out.strip()

    def resume(self, session_id: str, prompt: str, mode=None, cwd: str | None = None, model=None,
               effort=None) -> tuple[str | None, str]:
        """cwd — папка сессии: --resume работает там, откуда вызван, а не там, где сессия жила.
        model и effort — те же, с которыми сессия работала: без них продолженная сессия берёт модель по
        умолчанию. Это и не та модель, и полная перезапись кэша диалога — кэш у каждой модели свой."""
        cmd = [self.bin, "--resume", session_id, "--bg"]
        if mode:
            cmd += ["--permission-mode", mode]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        where = Path(cwd) if cwd and Path(cwd).is_dir() else self.root
        p = run(cmd + [prompt], cwd=where, timeout=180, env=self.env)
        out = (p.stdout or "") + (p.stderr or "")
        return (self._short_id(out) if p.returncode == 0 else None), out.strip()

    def logs(self, sid: str) -> str:
        p = run([self.bin, "logs", sid], cwd=self.root, timeout=30, env=self.env)
        return (p.stdout or "")[-6000:]

    def stop(self, sid: str) -> bool:
        return run([self.bin, "stop", sid], cwd=self.root, timeout=60, env=self.env).returncode == 0

    def rm(self, sid: str) -> bool:
        return run([self.bin, "rm", sid], cwd=self.root, timeout=60, env=self.env).returncode == 0


PR_FIELDS_QUERY = (
    "state isDraft headRefOid headRefName body "
    "labels(first: 50) { nodes { name } } "
    "comments(last: 100) { nodes { author { login } body createdAt } } "
    "commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes { __typename "
    "... on CheckRun { name conclusion status } ... on StatusContext { context state } } } } } } }"
)


def pr_from_graphql(node: dict) -> dict:
    """Узел pullRequest из GraphQL → те же поля, что отдаёт gh pr view --json."""
    commits = ((node.get("commits") or {}).get("nodes") or [{}])
    rollup = (((commits[-1] or {}).get("commit") or {}).get("statusCheckRollup") or {})
    return {
        "state": node.get("state"), "isDraft": bool(node.get("isDraft")),
        "headRefOid": node.get("headRefOid"), "headRefName": node.get("headRefName"), "body": node.get("body") or "",
        "labels": [x for x in ((node.get("labels") or {}).get("nodes") or []) if x],
        "comments": [x for x in ((node.get("comments") or {}).get("nodes") or []) if x],
        "statusCheckRollup": [x for x in ((rollup.get("contexts") or {}).get("nodes") or []) if x],
    }


class GH:
    def __init__(self, root: Path):
        self.root = root
        self.bin = need("gh")
        self._repo_url = None
        self._slug: str | None = None
        self._prs: dict[int, dict] = {}

    def call(self, *args: str, timeout: int = 60):
        return gh_with_retry(lambda: run([self.bin, *args], cwd=self.root, timeout=timeout), args)

    def json(self, *args: str):
        p = self.call(*args)
        if p.returncode != 0:
            raise AiflowError("gh " + " ".join(args[:3]) + ": " + (p.stderr or p.stdout).strip()[:300])
        return json.loads(p.stdout or "null")

    def pr(self, n: int, fields: str) -> dict:
        cached = self._prs.get(int(n))
        if cached is not None and all(f in cached for f in fields.split(",")):
            return cached
        return self.json("pr", "view", str(n), "--json", fields)

    def prefetch(self, numbers) -> None:
        """Состояние всех PR прохода одним запросом GraphQL вместо gh pr view на каждую задачу.
        Ответ приводится к виду gh pr view --json. Не вышло — pr() спросит каждый PR отдельно."""
        self._prs = {}
        nums = sorted({int(n) for n in numbers if n})
        if not nums:
            return
        if self._slug is None:
            self._slug = gh_repo(self.root)
        owner, _, name = self._slug.partition("/")
        if not name or "{" in self._slug:
            return
        for i in range(0, len(nums), 20):
            part = nums[i:i + 20]
            query = ("query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { "
                     + " ".join(f"p{n}: pullRequest(number: {n}) {{ {PR_FIELDS_QUERY} }}" for n in part) + " } }")
            p = self.call("api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}", "-F", f"name={name}")
            try:
                repo = ((json.loads(p.stdout or "{}").get("data") or {}).get("repository") or {})
            except ValueError:
                repo = {}
            for n in part:
                node = repo.get(f"p{n}")
                if isinstance(node, dict):
                    self._prs[n] = pr_from_graphql(node)

    def forget(self, n: int) -> None:
        self._prs.pop(int(n), None)

    def behind(self, base: str, head: str) -> int | None:
        """На сколько коммитов head отстаёт от base; None — узнать не удалось."""
        if self._slug is None:
            self._slug = gh_repo(self.root)
        p = self.call("api", f"repos/{self._slug}/compare/{base}...{head}", "--jq", ".behind_by")
        try:
            return int((p.stdout or "").strip()) if p.returncode == 0 else None
        except ValueError:
            return None

    def pr_files(self, n: int) -> set[str] | None:
        """Файлы, изменённые в PR; None — узнать не удалось."""
        p = self.call("pr", "diff", str(n), "--name-only")
        if p.returncode != 0:
            return None
        return {x.strip() for x in (p.stdout or "").splitlines() if x.strip()}

    def add_label(self, n: int, label: str) -> None:
        self.call("pr", "edit", str(n), "--add-label", label)
        self.forget(n)

    def remove_label(self, n: int, label: str) -> None:
        self.call("pr", "edit", str(n), "--remove-label", label)
        self.forget(n)

    def repo_url(self) -> str:
        if self._repo_url is None:
            p = self.call("repo", "view", "--json", "url", "-q", ".url")
            self._repo_url = p.stdout.strip() if p.returncode == 0 else ""
        return self._repo_url

    def last_push_run(self, branch: str) -> dict | None:
        """Состояние CI ветки: для каждого workflow, который запускается на push, — его самый свежий
        завершённый прогон, push или ручной перезапуск (workflow_dispatch). Красный, если красный хоть
        один такой workflow. Ручной зелёный перезапуск снимает паузу integration_red."""
        p = self.call("run", "list", "--branch", branch, "--limit", "40",
                      "--json", "conclusion,status,url,headSha,event,workflowName,createdAt")
        if p.returncode != 0:
            return None
        runs = json.loads(p.stdout or "[]")
        on_push = {r.get("workflowName") for r in runs if r.get("event") == "push"}
        latest: dict[str, dict] = {}
        for r in runs:  # gh отдаёт от новых к старым
            wf = r.get("workflowName")
            if wf in on_push and r.get("event") in ("push", "workflow_dispatch") and wf not in latest:
                latest[wf] = r
        if not latest:
            return None
        if any(r.get("status") != "completed" for r in latest.values()):
            return {"status": "in_progress"}
        red = [r for r in latest.values() if r.get("conclusion") in ("failure", "timed_out", "startup_failure")]
        return red[0] if red else max(latest.values(), key=lambda r: r.get("createdAt") or "")


# ---------------------------------------------------------------- usage

# cache_write — вся запись в кэш; cache_write_1h — её часть с часовым сроком жизни (она дороже)
TOKEN_KINDS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")

# Цены API Anthropic, $ за 1 млн токенов: (вход, выход, чтение из кэша). Запись в кэш считается от цены
# входа: ×1.25 на 5 минут, ×2 на час. Это оценка «сколько стоило бы по API»: на подписке вы платите не
# за токены. Свои цены — в ~/.aiflow/config.json: "prices": {"<начало id модели>": {"input": …,
# "output": …, "cache_read": …}}. Модель ищется по самому длинному совпавшему началу id.
PRICES = {
    "claude-fable-5-2": (8.0, 40.0, 0.20), "claude-mythos-5-2": (8.0, 40.0, 0.20),
    "claude-fable-5-1": (10.0, 50.0, 1.0), "claude-mythos-5-1": (10.0, 50.0, 1.0),
    "claude-fable-5": (10.0, 50.0, 1.0), "claude-mythos-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20), "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10), "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
CACHE_WRITE_5M, CACHE_WRITE_1H = 1.25, 2.0


def price_table() -> dict[str, tuple]:
    """Встроенные цены плюс свои из ~/.aiflow/config.json."""
    table = dict(PRICES)
    user = (read_json(aiflow_home() / "config.json", {}) or {}).get("prices")
    for model, p in (user.items() if isinstance(user, dict) else ()):
        try:
            inp = float(p["input"])
            table[str(model)] = (inp, float(p["output"]), float(p.get("cache_read", inp * 0.1)))
        except (TypeError, ValueError, KeyError):
            continue
    return table


def model_price(model: str, table: dict[str, tuple]) -> tuple | None:
    hits = [k for k in table if str(model or "").startswith(k)]
    return table[max(hits, key=len)] if hits else None


def usage_cost(model: str, u: dict, table: dict[str, tuple]) -> float | None:
    """Стоимость расхода одной модели в долларах; None — цена модели неизвестна."""
    if u.get("reported_cost") is not None:
        return float(u["reported_cost"])  # цену посчитал сам инструмент (ревьюер opencode)
    price = model_price(model, table)
    if not price:
        return None
    inp, out, read = price
    write = int(u.get("cache_write") or 0)
    hour = min(write, int(u.get("cache_write_1h") or 0))
    tokens = (int(u.get("input") or 0) * inp + int(u.get("output") or 0) * out + int(u.get("cache_read") or 0) * read
              + (write - hour) * inp * CACHE_WRITE_5M + hour * inp * CACHE_WRITE_1H)
    return tokens / 1_000_000


def priced(usage: dict[str, dict], table: dict[str, tuple]) -> tuple[dict[str, dict], float, list[str]]:
    """Расход по моделям с полем cost, общая стоимость и модели без цены."""
    out, total, unknown = {}, 0.0, []
    for model, u in usage.items():
        cost = usage_cost(model, u, table)
        out[model] = {**u, "cost": cost}
        if cost is None:
            unknown.append(model)
        else:
            total += cost
    return out, total, unknown
_usage_cache: dict[str, tuple[float, dict]] = {}


def _file_msgs(path: str) -> dict[str, tuple[str, dict]]:
    """{id сообщения: (модель, usage)} одного транскрипта (с кэшем по mtime)."""
    mtime = os.path.getmtime(path)
    cached = _usage_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    per_msg: dict[str, tuple[str, dict]] = {}  # стриминг пишет одно сообщение несколько раз — берём последнюю запись
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message") if isinstance(obj, dict) else None
            if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                continue
            model = msg.get("model") or "?"
            if model == "<synthetic>":
                continue
            per_msg[msg.get("id") or f"{path}:{i}"] = (model, msg["usage"])
    _usage_cache[path] = (mtime, per_msg)
    return per_msg


def add_usage(into: dict[str, dict], models: dict[str, dict]) -> dict[str, dict]:
    for model, u in models.items():
        m = into.setdefault(model, dict.fromkeys(TOKEN_KINDS, 0))
        for k in TOKEN_KINDS:
            m[k] += int(u.get(k) or 0)
        if u.get("reported_cost") is not None:
            m["reported_cost"] = m.get("reported_cost", 0.0) + float(u["reported_cost"])
        if "cache_write_1h" not in u:
            # запись старой версии, без разбивки по сроку: Claude Code пишет кэш на час — считаем по этой цене
            m["cache_write_1h"] += int(u.get("cache_write") or 0)
    return into


def transcript_usage(session_id: str, seen: set[str] | None = None) -> dict | None:
    """Расход сессии по моделям, вместе с её сабагентами (explorer, researcher и т. п.).
    seen — id сообщений, уже посчитанных в других сессиях задачи: возобновлённая через
    --resume сессия копирует историю предыдущей, и без этого её расход считался бы дважды."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    files = glob.glob(str(base / "projects" / "*" / f"{session_id}.jsonl"))
    if not files:
        return None
    files += glob.glob(str(base / "projects" / "*" / session_id / "subagents" / "*.jsonl"))
    models: dict[str, dict] = {}
    main: dict[str, dict] = {}
    for i, path in enumerate(files):
        try:
            msgs = _file_msgs(path)
        except OSError:
            continue
        fu: dict[str, dict] = {}
        for mid, (model, u) in msgs.items():
            if seen is not None:
                if mid in seen:
                    continue
                seen.add(mid)
            m = fu.setdefault(model, dict.fromkeys(TOKEN_KINDS, 0))
            m["input"] += int(u.get("input_tokens") or 0)
            m["output"] += int(u.get("output_tokens") or 0)
            m["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
            m["cache_write"] += int(u.get("cache_creation_input_tokens") or 0)
            m["cache_write_1h"] += int((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)
        add_usage(models, fu)
        if i == 0:
            main = fu
    tot = {k: sum(u[k] for u in models.values()) for k in TOKEN_KINDS}
    model = max(main, key=lambda m: main[m]["output"], default="")
    return {**tot, "model": model, "models": models}


def transcript_age_minutes(session_id: str | None) -> float | None:
    """Сколько минут назад сессия последний раз что-то записала в свой транскрипт; None — транскрипта нет."""
    if not session_id:
        return None
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    times = []
    for path in glob.glob(str(base / "projects" / "*" / f"{session_id}.jsonl")):
        try:
            times.append(os.path.getmtime(path))
        except OSError:
            pass
    return (time.time() - max(times)) / 60 if times else None


def reviewer_runs(store: "Store", tid: str) -> list[dict]:
    data = read_json(store.runs / tid / "reviewer.json", {}) or {}
    return [x for x in data.get("runs") or [] if isinstance(x, dict)]


def reviewer_usage(store: "Store", tid: str) -> dict | None:
    """Расход ревьюера opencode по задаче (runs/<id>/reviewer.json) в том же виде, что расход сессии Claude."""
    models: dict[str, dict] = {}
    for run in reviewer_runs(store, tid):
        m = models.setdefault(str(run.get("model") or "opencode"),
                              {**dict.fromkeys(TOKEN_KINDS, 0), "reported_cost": 0.0})
        for k in ("input", "output", "cache_read", "cache_write"):
            m[k] += int(run.get(k) or 0)
        m["reported_cost"] += float(run.get("cost") or 0)
    if not models:
        return None
    return {"role": "reviewer", "model": max(models, key=lambda k: models[k]["output"]), "models": models}


def task_usage(t: dict) -> dict[str, dict]:
    """Расход задачи по моделям; старые записи без разбивки относятся к основной модели сессии."""
    out: dict[str, dict] = {}
    for u in (t.get("usage") or {}).values():
        add_usage(out, u.get("models") or {u.get("model") or "?": u})
    return out


def done_note(t: dict) -> str | None:
    """Итог задачи, законченной без PR: строка отчёта, с которой она перешла в done."""
    return next((h.get("note") for h in reversed(t.get("history") or []) if h.get("to") == "done" and h.get("note")), None)


def task_tokens(t: dict) -> int:
    return sum(u["input"] + u["output"] + u["cache_write"] for u in task_usage(t).values())


# ---------------------------------------------------------------- prompts

EXPLORERS = ("aiflow", "project")


def explorer_mode(cfg: dict) -> str:
    v = str(cfg.get("explorer") or "aiflow").lower()
    return v if v in EXPLORERS else "aiflow"


def search_rule(cfg: dict) -> str:
    """Правило поиска по коду для первого сообщения сессии. В режиме explorer: project — пусто: как искать,
    целиком решают CLAUDE.md и агенты проекта, и вторая, конкурирующая инструкция от aiflow только мешает."""
    if explorer_mode(cfg) == "project":
        return ""
    return ("How to search the code: send questions such as \"where is this\", \"who uses this\", \"how many\" to the "
            "aiflow:explorer subagent (several in parallel). Read yourself only the files that the decision depends on. "
            "If you search yourself, for a symbol (where it is defined, who uses, implements or calls it) use the LSP "
            "tool when it is available for the language (if it is deferred, load it through ToolSearch), not a text search.")


def with_search_rule(prompt: str, cfg: dict) -> str:
    """Вставить правило поиска сразу после строки с файлом задачи: до описания путей и отчётов.
    В конце длинного сообщения его не замечали — сессия начинала искать раньше, чем дочитывала."""
    rule = search_rule(cfg)
    if not rule:
        return prompt
    at = prompt.find("Task file")
    end = prompt.find("\n", at) if at != -1 else -1
    if end == -1:
        return rule + "\n" + prompt
    return prompt[:end + 1] + rule + "\n" + prompt[end + 1:]


def planner_prompt(store: Store, t: dict, cfg: dict) -> str:
    tid = t["id"]
    return with_search_rule((
        f"aiflow task {tid}: {t.get('title', '')}\n"
        f"Task file: {store.task_path(tid)}. Read it in full.\n"
        f"Base branch: {cfg['base_branch']}. Task branch: ai/{tid}.\n"
        "First assess the task (section \"Task assessment\" of your instructions) and select one route:\n"
        f"- complex: read the plan rules ({PLANNER_PLAN_MD}) and make a draft PR with a plan. Then run "
        f"aiflow report {tid} --role planner --status plan_ready "
        "--pr <number> --complexity simple|complex --summary \"<one line>\" "
        "--details-file <notes for the worker, in $CLAUDE_JOB_DIR/tmp/>;\n"
        f"- simple, delegate to the worker: aiflow report {tid} --role planner --status delegate "
        "--complexity trivial|simple --summary \"<one line>\" "
        "--details-file <instructions for the worker, in $CLAUDE_JOB_DIR/tmp/>;\n"
        "- simple, do it yourself: obey the section \"Do it yourself\" and report "
        f"aiflow report {tid} --role planner --status ready_for_review --pr <number> --summary \"<1-2 lines>\" "
        f"(or --status done if the repository did not have to change). Worker instructions: {WORKER_MD}. "
        f"When you decide to do it yourself, before the first edit run aiflow report {tid} --role planner "
        "--status self_execute --summary \"<what you do, one line>\" and continue: then the queue shows work, "
        "not planning.\n"
        "End your turn after the report."
    ), cfg)


def amend_list(items: list[dict]) -> str:
    return "\n\n".join(f"Amendment {a['n']} ({a.get('author') or 'user'}, {a['at'][:16].replace('T', ' ')}):\n"
                       f"{a['text'].strip()}" for a in items)


def amend_prompt(t: dict, role: str, state: str, items: list[dict], live: bool) -> str:
    """Дополнения пользователя к задаче. live — сессия сейчас работает (доставка хуком)."""
    head = f"The user amended task {t['id']}:\n\n{amend_list(items)}\n\n"
    if state == "discussing":
        return head + "Include this in the discussion and reply to the user."
    if state == "answered":
        return head + ("This continues your answer to the question. If the user asks for a code change, write a plan "
                       f"by the plan rules ({PLANNER_PLAN_MD}; branch ai/{t['id']}, draft PR) and run "
                       f"aiflow report {t['id']} --role planner "
                       "--status plan_ready --pr <number> --complexity simple|complex --summary \"<one line>\". "
                       f"If it is a new question, answer it and run aiflow report {t['id']} --role planner "
                       "--status answered --summary \"<the answer in 1-3 lines>\". If the user wants to discuss, "
                       "discuss and end your turn.")
    if role == "planner":
        if not t.get("pr"):
            return head + ("Include this in the plan that you write now (or in the work, if you do the task yourself)."
                           if live else
                           f"Include this in the plan, complete the plan and report aiflow report {t['id']} "
                           "--role planner --status plan_ready --pr <number> --summary \"<one line>\", as in your "
                           "instructions.")
        tail = ("Include the amendment in the plan: update the PR description (gh pr edit --body-file), add a line to "
                "\"Plan change history\", and reply in the PR with one comment whose first line is "
                "<!-- aiflow:planner -->.")
        return head + tail + (" Then continue by your instructions." if live else
                              f" Then run aiflow report {t['id']} --role planner --status plan_ready "
                              f"--pr {t['pr']} --summary \"<what changed>\".")
    head += ("This is a part of the task, equal to the plan. Include the amendment in the implementation. If finished "
             "work contradicts it, redo that work. When it is done, reply in the PR with a comment whose first line is "
             "<!-- ai-review-reply -->: what you did for the amendment, or why you did not. ")
    if live:
        return head + ("If you are in the review cycle now: edit, run the tests, push, reply and run aiflow review "
                       "again. If aiflow review already returned the same amendment as human_input, do not process it "
                       "a second time. Then continue by your instructions.")
    pr = t.get("pr")
    if not pr:
        return head + ("Then obey the section \"Task without a plan\" of your instructions: report done, or open a PR "
                       "and report ready_for_review.")
    return head + ("Run the tests, commit, git push, then run "
                   f"aiflow report {t['id']} --role worker --status ready_for_review --pr {pr} --summary \"<1-2 lines>\" "
                   "and end your turn. Do not start the review yourself: aiflow wakes you when it is your turn.")


def launch_amendments(t: dict) -> str:
    """Недоставленные дополнения — в промпт запуска новой сессии."""
    items = [a for a in t.get("amendments") or [] if not a.get("delivered_at")]
    if not items:
        return ""
    return ("\n\nUser amendments to the task (a part of the task, equal to its text and plan):\n\n" + amend_list(items)
            + "\n\nIf the task has a PR, after the implementation reply in it with a comment whose first line is "
              "<!-- ai-review-reply -->: what you did for the amendments.")


def mark_delivered(t: dict, via: str) -> None:
    for a in t.get("amendments") or []:
        if not a.get("delivered_at"):
            a["delivered_at"], a["via"] = iso(), via


def revise_prompt(t: dict, comments: list[dict]) -> str:
    quoted = "\n\n".join(f"- {c.get('author', {}).get('login', '?')} ({c.get('createdAt', '')}):\n{c.get('body', '').strip()}"
                         for c in comments)
    return (
        f"New comments on the plan in PR #{t['pr']} (task {t['id']}):\n\n{quoted}\n\n"
        "Include them: update the plan in the PR description (gh pr edit --body-file), add a line to "
        "\"Plan change history\", and reply in the PR with one comment whose first line is <!-- aiflow:planner -->. "
        f"Then run aiflow report {t['id']} --role planner --status plan_ready --pr {t['pr']} "
        "--summary \"<what changed>\"."
    )


def worker_prompt(store: Store, t: dict, cfg: dict, retry: bool = False) -> str:
    base = (
        f"aiflow task {t['id']}: {t.get('title', '')}\n"
        f"PR #{t['pr']}, branch ai/{t['id']}, base branch {cfg['base_branch']}. "
        f"The plan is in the PR description (gh pr view {t['pr']}). Task file: {store.task_path(t['id'])}.\n"
    )
    if store.notes_path(t["id"]).is_file():
        base += (
            f"The planner's notes about the code: {store.notes_path(t['id'])}. Read them before you search: they "
            "contain what the planner already found (where things are, signatures, commands that work, pitfalls). "
            "Search only for what the notes do not answer. The notes name the commit they were written at: files "
            "that changed in the base branch after it can differ from the notes.\n"
        )
    if retry:
        rep = store.report(t["id"]) or {}
        base += (
            "This is a new attempt after a stop. Read the open review threads in the PR and the last report: "
            f"{json.dumps(rep, ensure_ascii=False)}\n"
            "If the PR was already in review, fix the open blocking findings and report ready_for_review again.\n"
        )
    base += (
        "When the implementation is complete, the PR is ready (not a draft) and the checks pass, run "
        f"aiflow report {t['id']} --role worker --status ready_for_review --pr {t['pr']} --summary \"<1-2 lines>\" "
        "and end your turn. Do not start the review yourself: aiflow wakes you when it is your turn."
    )
    return with_search_rule(base, cfg)


def direct_prompt(store: Store, t: dict, cfg: dict) -> str:
    """Задача без плана: исполнитель делает её сразу — по указаниям планировщика, если они есть."""
    brief = (t.get("brief") or "").strip()
    head = (f"The planner already studied the code and delegated the task to you. Its instructions:\n\n{brief}\n\n"
            if brief else "")
    return with_search_rule((
        f"aiflow task {t['id']}: {t.get('title', '')}\n"
        f"Task file: {store.task_path(t['id'])}. Read it in full. There is no plan: "
        "do the task now by the section \"Task without a plan\" of your instructions.\n" + head +
        f"Base branch: {cfg['base_branch']}. Task branch, if the repository must change: ai/{t['id']}.\n"
        f"If the repository does not have to change, when the work is done run: aiflow report {t['id']} --role worker "
        "--status done --summary \"<what was done and how it was checked>\" and end your turn.\n"
        "If the repository must change: branch, commit, push, PR (gh pr create, not a draft), then "
        f"aiflow report {t['id']} --role worker --status ready_for_review --pr <number> --summary \"<1-2 lines>\" "
        "and end your turn. Do not start the review yourself: aiflow wakes you when it is your turn."
    ), cfg)


def discuss_prompt(store: Store, t: dict, cfg: dict) -> str:
    """Задача kind: discuss — обсуждение с пользователем в сессии, PR только по договорённости."""
    tid = t["id"]
    return with_search_rule((
        f"aiflow discussion {tid}: {t.get('title', '')}\n"
        f"Task file: {store.task_path(tid)}. Read it in full. Base branch: {cfg['base_branch']}.\n"
        f"Discussion mode: read the talk rules ({PLANNER_TALK_MD}). When the work becomes a plan, read the plan rules "
        f"({PLANNER_PLAN_MD}). The user wants to discuss how to do this, not to get a plan immediately. "
        "Study the code and the documentation as much as a useful conversation needs (send documentation questions "
        "to the aiflow:researcher subagent). Then write to the user: how you understand the task, 2 or 3 options "
        "with advantages and disadvantages, your recommendation and your questions. Then end your turn: the user "
        "replies in this session. Do not create a branch, a PR or files until the user clearly says that you agree.\n"
        "How to finish when you agree:\n"
        f"- one task: write the plan by the plan rules (branch ai/{tid}, draft PR) and run "
        f"aiflow report {tid} --role planner --status plan_ready --pr <number> --complexity simple|complex "
        "--summary \"<one line>\";\n"
        "- two or more independent tasks: put them into the queue with one aiflow enqueue command (the format is in "
        f"/aiflow:intake) and run aiflow report {tid} --role planner --status done --summary \"queued: <id>…\";\n"
        f"- nothing to do: aiflow report {tid} --role planner --status done --summary \"<result of the discussion>\"."
    ), cfg)


def ask_prompt(store: Store, t: dict, cfg: dict) -> str:
    """Задача kind: ask — вопрос о коде. Ответ, а план — только если пользователь попросит изменение."""
    tid = t["id"]
    return with_search_rule((
        f"aiflow question {tid}: {t.get('title', '')}\n"
        f"Task file: {store.task_path(tid)}. Read it in full. Base branch: {cfg['base_branch']}.\n"
        f"This is a question, not a task: read the talk rules ({PLANNER_TALK_MD}). Find the answer in the code and the "
        "documentation (send documentation questions to the aiflow:researcher subagent). Answer the user here: "
        "directly, with paths and line numbers. Do not create a branch, a PR or files. Then run "
        f"aiflow report {tid} --role planner --status answered --summary \"<the answer in 1-3 lines>\" "
        "--details-file <file with the full answer, in $CLAUDE_JOB_DIR/tmp/> and end your turn.\n"
        f"If the user then asks for a change, write a plan by the plan rules ({PLANNER_PLAN_MD}; "
        f"branch ai/{tid}, draft PR) and run aiflow report {tid} --role planner --status plan_ready "
        "--pr <number> --complexity simple|complex --summary \"<one line>\". For a new question, answer again and "
        "report --status answered."
    ), cfg)


def review_prompt(t: dict, cfg: dict) -> str:
    where = (f"by the section \"Turn 2: review\" of the worker instructions ({WORKER_MD}; read it if you did not)"
             if t.get("self_executed") else "as your instructions tell you")
    return (
        f"It is your turn for the review of PR #{t['pr']} (task {t['id']}). "
        f"git fetch origin; git rebase origin/{cfg['base_branch']}; resolve the conflicts; run the tests; "
        "git push --force-with-lease. Then run aiflow review --pr "
        f"{t['pr']} and act on the status {where}, until a final status."
    )


def review_result_prompt(store: "Store", t: dict, cfg: dict, status: str) -> str:
    """Ревью запустил сам aiflow, и оно требует работы модели: исправлений, rebase, ответа человеку."""
    where = (f"by the section \"Turn 2: review\" of the worker instructions ({WORKER_MD}; read it if you did not)"
             if t.get("self_executed") else "by the status table in your instructions (\"Turn 2: review\")")
    return (
        f"aiflow already ran the review of PR #{t['pr']} (task {t['id']}) without you: the branch was not behind "
        f"{cfg['base_branch']}. The result is the status {status}. The details are in the file "
        f"{store.runs / t['id'] / 'review.json'} (read it). Act {where}: do what this status needs, then run "
        f"aiflow review --pr {t['pr']} yourself and continue until a final status."
    )


def ci_fix_prompt(t: dict, failed: list[str]) -> str:
    return (
        f"PR #{t['pr']} is approved, but CI checks failed after that: {', '.join(failed) or 'see gh pr checks'}. "
        "Look at gh pr checks and the logs of the failed jobs. Fix, run the tests, push and run "
        f"aiflow review --pr {t['pr']} again."
    )


_BASE_SEEN: dict[tuple[str, str], tuple[float, bool]] = {}

STAGE_ROLE = {"planning": "planner", "discussing": "planner", "answered": "planner", "in_progress": "worker", "in_review": "worker"}

RESUME_FINISH = {
    "discussing": "Continue the discussion with the user. Open a PR only when you agree. The result is aiflow report "
                  "{id} --role planner (plan_ready or done), as in the first message of this session.",
    "planning": "Complete the route that you selected and report aiflow report {id} --role planner (plan_ready, "
                "delegate, ready_for_review or done), as in the first message of this session.",
    "in_progress": "Complete the implementation and report aiflow report {id} --role worker --status ready_for_review "
                   "(or --status done for a task without a plan and without repository changes), as in your instructions.",
    "in_review": "Continue the review cycle of PR #{pr} (aiflow review --pr {pr}) until a final status, as in your "
                 "instructions.",
}


def resume_prompt(t: dict, state: str, why: str, base: str) -> str:
    """Промпт для возобновления той же сессии после остановки. why — причина по-русски, как в панели."""
    return (
        f"The work on task {t['id']} was interrupted ({why}). The base branch now is {base}: "
        "if a different one was given before, use this one. Continue from where you stopped: "
        "check git status and git log in your working directory. Uncommitted edits are still there. "
        "Complete what you started. Do not start again. "
        + RESUME_FINISH.get(state, "Continue by your instructions.").format(id=t["id"], pr=t.get("pr"))
    )


# ---------------------------------------------------------------- dispatcher

class Dispatcher:
    def __init__(self, store: Store, claude=None, gh=None, spawn_review=None):
        """claude, gh и запуск фонового ревью можно подменить (тесты): по умолчанию — настоящие."""
        self.spawn_review = spawn_review or spawn_review_process
        self.store = store
        self.root = store.root
        self.claude = claude or Claude(self.root)
        self.gh = gh or GH(self.root)
        self.events: list[dict] = []
        self.cfg: dict = {}
        self.q: dict = {}
        self.sessions: dict[str, dict] = {}
        self.by_id: dict[str, dict] = {}

    # -- helpers
    def emit(self, t: dict | None, kind: str, text: str, key: str | None = None) -> None:
        if t is not None and key:
            sent = t.setdefault("notified", [])
            if key in sent:
                return
            sent.append(key)
            del sent[:-100]
        ev = self.store.event(kind, t["id"] if t else None, text)
        self.events.append(ev)

    def set_state(self, t: dict, state: str, note: str = "") -> None:
        if t.get("state") != state:
            t.setdefault("history", []).append({"at": iso(), "from": t.get("state"), "to": state, "note": note})
            del t["history"][:-60]
            t["state"] = state
            t["state_at"] = iso()

    def cur(self, t: dict, role: str) -> dict | None:
        lst = (t.get("sessions") or {}).get(role) or []
        return lst[-1] if lst else None

    def sess(self, entry: dict | None) -> dict | None:
        return self.sessions.get(entry["id"]) if entry and entry.get("id") else None

    def sstate(self, entry: dict | None) -> str | None:
        s = self.sess(entry)
        return s.get("state") if s else None

    def new_report(self, t: dict, role: str) -> dict | None:
        rep = self.store.report(t["id"])
        if not rep or rep.get("role") != role or rep.get("rid") == t.get("report_seen"):
            return None
        t["report_prev"] = t.get("report_seen")
        t["report_seen"] = rep.get("rid")
        # сессия продвинулась — счётчики автовозобновлений, напоминаний и пауз по лимиту заново
        (t.get("auto_resumed") or {}).pop(role, None)
        (t.get("nudged") or {}).pop(role, None)
        t.pop("limit_hits", None)
        return rep

    def verify_pr(self, t: dict, role: str, rep: dict, *, plan: bool) -> bool:
        """Отчёт называет PR — проверить, что это PR этой задачи, а не опечатка или чужой номер.
        Неверный отчёт возвращается сессии (до двух раз), потом — человеку."""
        n = int(rep["pr"])
        try:
            pr = self.gh.pr(n, "state,headRefName,isDraft,body")
        except AiflowError:
            t["report_seen"] = t.get("report_prev")  # GitHub не ответил: разберём отчёт в следующий проход
            raise
        want = f"ai/{t['id']}"
        problem = fix = ""  # problem — человеку (панель, событие), fix — сессии
        if pr.get("state") != "OPEN":
            problem = f"PR #{n} не открыт ({pr.get('state')})"
            fix = f"PR #{n} is not open ({pr.get('state')})"
        elif pr.get("headRefName") != want:
            problem = f"PR #{n} открыт из ветки {pr.get('headRefName')}, а ветка задачи — {want}"
            fix = f"PR #{n} is from the branch {pr.get('headRefName')}, but the task branch is {want}"
        elif plan and "<!-- ai-plan" not in (pr.get("body") or ""):
            problem = f"в описании PR #{n} нет плана по шаблону (маркер <!-- ai-plan v1 -->)"
            fix = f"the description of PR #{n} has no plan by the template (marker <!-- ai-plan v1 -->)"
        elif not plan and pr.get("isDraft"):
            problem = f"PR #{n} ещё draft"
            fix = f"PR #{n} is still a draft: run gh pr ready {n}"
        if not problem:
            (t.get("bounced") or {}).pop(role, None)
            return True
        counts = t.setdefault("bounced", {})
        counts[role] = counts.get(role, 0) + 1
        self.store.log(t["id"], {"report_rejected": problem})
        text = (f"The report {rep.get('status')} for task {t['id']} was rejected: {fix}. Correct this and report "
                f"again with aiflow report {t['id']} --role {role} and the correct PR number.")
        if counts[role] > 2 or not self.wake(t, role, text):
            self.to_human(t, f"отчёт {role} не принят: {problem}", "report")
            return False
        self.touch(t, role)
        self.emit(t, "report_rejected", f"{t['id']}: отчёт {role} не принят — {problem}", key=f"rejected:{rep.get('rid')}")
        return False

    def touch(self, t: dict, role: str) -> None:
        """Сессию только что разбудили: её снимок в claude agents устарел, отсчёты — заново."""
        e = self.cur(t, role)
        if e:
            e["resumed_at"] = iso()
        t["state_at"] = iso()

    def fresh(self, e: dict | None) -> bool:
        return bool(e and e.get("resumed_at") and age_minutes(e["resumed_at"]) < 2)

    def nudge(self, t: dict, role: str, state: str, reason: str) -> None:
        """Сессия закончила ход без отчёта: один раз напомнить ей, и только потом звать человека."""
        e = self.cur(t, role)
        if self.fresh(e):
            return
        counts = t.setdefault("nudged", {})
        if counts.get(role, 0) < 1:
            text = (f"You ended your turn on task {t['id']} without a final report, so the queue does not know the "
                    "result. If the work is not complete, complete it. If you need a reply or a decision from a human, "
                    f"run aiflow report {t['id']} --role {role} --status blocked --summary \"<what you need>\". "
                    + RESUME_FINISH.get(state, "Report by your instructions.").format(id=t["id"], pr=t.get("pr")))
            if self.wake(t, role, text):
                counts[role] = counts.get(role, 0) + 1
                self.touch(t, role)
                self.emit(t, "nudged", f"{t['id']}: {role} закончил ход без отчёта — напомнили", key=f"nudge:{role}:{iso()}")
                return
        self.to_human(t, reason, "silent")

    def blocked(self, t: dict, role: str, e: dict) -> None:
        """Сессия ждёт ответа: сказать человеку всегда; заодно проверить, не лимит ли это."""
        w = (self.sess(e) or {}).get("waitingFor") or "ответ"
        who = "планировщик" if role == "planner" else role
        self.emit(t, "needs_input", f"{t['id']}: {who} ждёт вас в своей сессии ({w})", key=f"blocked:{e['id']}:{w}")
        self.check_limit(e, t)

    def try_resume(self, t: dict, role: str, state: str, why: str, auto: bool) -> bool:
        """Возобновить ту же сессию роли. auto — монитор сам, не больше auto_resume раз подряд."""
        e = self.cur(t, role)
        if not e:
            return False
        counts = t.setdefault("auto_resumed", {})
        if auto:
            if counts.get(role, 0) >= int(self.cfg.get("auto_resume", 0) or 0):
                return False
            counts[role] = counts.get(role, 0) + 1
        if not self.wake(t, role, resume_prompt(t, state, why, self.cfg['base_branch'])):
            return False
        e = self.cur(t, role)
        e["resumed_at"] = iso()
        verb = "возобновлена автоматически" if auto else "возобновлена"
        self.emit(t, "resumed", f"{t['id']}: сессия {role} {verb} ({why})", key=f"resumed:{role}:{iso()}")
        return True

    def lost(self, t: dict, role: str, state: str, why: str, reason: str) -> None:
        """Сессия остановилась или пропала: сначала автовозобновление, иначе — к человеку."""
        e = self.cur(t, role)
        if self.fresh(e):
            return  # только что возобновили: claude agents ещё может показывать старое состояние
        if not self.try_resume(t, role, state, why, auto=True):
            self.to_human(t, reason, "session")

    def release_lock(self, t: dict) -> None:
        self.q["review_locks"] = [i for i in self.q.get("review_locks") or [] if i != t["id"]]

    def pause(self, reason: str, minutes: float | None, text: str, note: str = "") -> None:
        """Добавить причину паузы. Причины независимы: одна не затирает другую."""
        pauses = self.q.setdefault("pauses", {})
        if reason in pauses:
            return
        until = iso(now() + dt.timedelta(minutes=minutes)) if minutes else None
        pauses[reason] = {"at": iso(), "until": until, "note": note}
        sync_queue(self.q)
        self.emit(None, "paused", text)

    def unpause(self, reasons, text: str) -> None:
        """Снять причины паузы. Очередь продолжает, только когда причин не осталось."""
        pauses = self.q.setdefault("pauses", {})
        gone = [r for r in ([reasons] if isinstance(reasons, str) else reasons) if pauses.pop(r, None) is not None]
        if not gone:
            return
        sync_queue(self.q)
        if pauses:
            self.emit(None, "pause_kept", f"{text}; очередь всё ещё на паузе: {', '.join(sorted(pauses))}")
        else:
            self.emit(None, "unpaused", text)

    def check_limit(self, entry: dict | None, t: dict | None = None) -> bool:
        """Сессия упёрлась в лимит использования? Тогда пауза очереди. Признак — текст в конце лога,
        поэтому одна и та же сессия без продвижения ставит паузу не больше LIMIT_HITS_MAX раз подряд:
        дальше это, скорее всего, слова из самой задачи, и сессию разбирают как обычно."""
        if not entry:
            return False
        text = self.claude.logs(entry["id"])[-LIMIT_LOG_TAIL:]
        s = self.sess(entry) or {}
        if not (LIMIT_RE.search(text) or LIMIT_RE.search(json.dumps(s, ensure_ascii=False))):
            return False
        if "rate_limit" in (self.q.get("pauses") or {}):
            return True
        if t is not None:
            hits = t.get("limit_hits") or {}
            n = hits.get("n", 0) + 1 if hits.get("sid") == entry["id"] else 1
            if n > LIMIT_HITS_MAX:
                return False
            t["limit_hits"] = {"sid": entry["id"], "n": n}
        mins = self.cfg["rate_limit_pause_minutes"]
        self.pause("rate_limit", mins, f"лимит использования: очередь на паузе на {mins} мин")
        return True

    def to_human(self, t: dict, reason: str, action: str = "") -> None:
        """Задача ждёт человека. reason — что случилось, action — ключ WHAT_TO_DO: что от него нужно."""
        self.release_lock(t)
        full, reason = str(reason or ""), short_reason(reason) or "нужен человек"
        if "защищ" in full.lower() or "protected" in full.lower():
            action = "paths"
        t["human_reason"] = reason
        t["human_action"] = WHAT_TO_DO.get(action, "")
        role = STAGE_ROLE.get(t.get("state", ""))
        # Запоминаем, в каком состоянии была сессия: если человек ответит ей сам и она
        # снова заработает, задача вернётся в свою стадию (см. adv_needs_human).
        t["human_watch"] = {"role": role, "seen": self.sstate(self.cur(t, role)) if role else None,
                            "report": t.get("report_seen")}
        self.set_state(t, "needs_human", reason)
        if t.get("pr"):
            self.gh.add_label(t["pr"], self.cfg["labels"]["needs_human"])
        todo = f" Что сделать: {t['human_action']}" if t["human_action"] else ""
        self.emit(t, "needs_human", f"{t['id']}: нужен человек — {reason}.{todo}", key=f"human:{iso()[:16]}:{reason[:40]}")

    # -- launching
    def launch(self, t: dict, role: str, prompt: str, model=None, effort=None) -> bool:
        name = f"{role}: {t['id']}"
        t["launching"] = {"role": role, "name": name, "at": iso()}
        self.store.save_task(t)
        sid, out = self.claude.launch(f"aiflow:{role}", name, prompt, model=model, effort=effort, worktree=t["id"],
                                      mode=self.cfg.get("permission_mode"))
        if not sid:
            low = out.lower()
            if "not trusted" in low:
                self.pause("trust", None, "папка репозитория не помечена как доверенная: "
                           "откройте в ней claude интерактивно и примите доверие, затем aiflow resume")
            t.pop("launching", None)
            self.store.log(t["id"], {"launch_failed": out[-800:]})
            self.emit(t, "error", f"{t['id']}: не удалось запустить {role}: {out[-200:]}", key=f"launchfail:{role}:{out[-60:]}")
            return False
        self.adopt(t, role, sid, name, model, effort)
        return True

    def adopt(self, t: dict, role: str, sid: str, name: str, model=None, effort=None) -> None:
        entry = {"id": sid, "name": name, "started_at": iso(), "session_id": None}
        if model:
            entry["model"] = model
        if effort:
            entry["effort"] = effort
        t.setdefault("sessions", {}).setdefault(role, []).append(entry)
        t.pop("launching", None)
        self.store.log(t["id"], {"launched": entry, "role": role})

    def wake(self, t: dict, role: str, prompt: str) -> bool:
        entry = self.cur(t, role)
        sid_full = entry.get("session_id") if entry else None
        if entry and not sid_full:
            sid_full = (self.sess(entry) or {}).get("sessionId")
        if not sid_full:
            return False
        cwd = entry.get("cwd") or (self.sess(entry) or {}).get("cwd") if entry else None
        model, effort = self.session_model(t, role, entry)
        new_id, out = self.claude.resume(sid_full, prompt, mode=self.cfg.get("permission_mode"), cwd=cwd,
                                         model=model, effort=effort)
        if new_id and new_id != entry["id"]:
            self.adopt(t, role, new_id, entry.get("name", f"{role}: {t['id']}"), model, effort)
            if cwd:
                self.cur(t, role)["cwd"] = cwd
        elif not new_id:
            self.store.log(t["id"], {"resume_failed": out[-800:]})
            return False
        self.store.log(t["id"], {"woke": role})
        return True

    def session_model(self, t: dict, role: str, entry: dict) -> tuple:
        """Модель и effort, с которыми сессия была запущена. У записей старых версий их нет — тогда
        по настройкам: сессию планировщика, который сделал задачу сам и ведёт её ревью, запускали как
        планировщика."""
        if entry.get("model"):
            return entry["model"], entry.get("effort")
        as_planner = role == "planner" or str(entry.get("name") or "").startswith("planner:")
        if as_planner:
            ask, talk = t.get("kind") == "ask", t.get("kind") in TALK_KINDS
            return planner_choice(self.cfg, t, (
                self.cfg.get("ask_model" if ask else "discuss_model") or ("sonnet" if ask else "opus"),
                self.cfg.get("ask_effort" if ask else "discuss_effort") or None) if talk else None)
        return worker_choice(self.cfg, t)

    def resolve_launching(self, t: dict) -> None:
        info = t.get("launching")
        if not info:
            return
        for sid, s in self.sessions.items():
            if s.get("name") == info.get("name"):
                self.adopt(t, info["role"], sid, info["name"], *self.session_model(t, info["role"], {}))
                return
        if age_minutes(info.get("at")) > 5:
            t.pop("launching", None)

    # -- tick
    def tick(self) -> list[dict]:
        self.events = []
        self.cfg = self.store.config()
        self.q = self.store.queue()
        try:
            self.sessions = self.claude.agents()
        except AiflowError as e:
            self.emit(None, "error", f"не удалось прочитать состояние сессий: {e}")
            self.store.save_queue(self.q)
            return self.events
        tasks = self.store.tasks()
        by_id = self.by_id = {t["id"]: t for t in tasks}
        self.gh.prefetch(t.get("pr") for t in tasks if t.get("state") not in TERMINAL)
        self.process_commands(by_id)
        self.check_pause_timer()
        self.check_base()
        if self.cfg.get("watch_integration"):
            self.check_integration()
        for t in tasks:
            if t.get("state") in TERMINAL and t.get("cleaned"):
                continue
            try:
                self.fill_session_ids(t)
                self.resolve_launching(t)
                self.advance(t)
            except AiflowError as e:
                self.store.log(t["id"], {"error": str(e)})
                self.emit(t, "error", f"{t['id']}: {e}", key=f"err:{str(e)[:60]}")
            self.store.save_task(t)
        if not self.q.get("pauses"):
            self.schedule(tasks)
        self.collect_usage(tasks)
        self.check_batch_done(tasks)
        self.store.save_queue(self.q)
        return self.events

    def fill_session_ids(self, t: dict) -> None:
        for lst in (t.get("sessions") or {}).values():
            for e in lst:
                s = self.sessions.get(e["id"])
                if s and not e.get("session_id") and s.get("sessionId"):
                    e["session_id"] = s["sessionId"]
                if s and s.get("cwd") and not e.get("cwd"):
                    e["cwd"] = s["cwd"]  # для пробуждения в той же рабочей копии (запоминается один раз)

    def process_commands(self, by_id: dict[str, dict]) -> None:
        for c in self.store.take_commands():
            kind = c.get("cmd")
            if kind == "pause":
                self.pause("manual", None, "очередь поставлена на паузу вручную", note=c.get("reason", ""))
            elif kind == "resume":
                # паузы «нет базовой ветки» и «красная ветка» снимает только их условие
                self.unpause(MANUAL_PAUSES, "очередь возобновлена")
            elif kind == "amend" and c.get("id") in by_id:
                t = by_id[c["id"]]
                if t.get("state") in TERMINAL:
                    self.emit(t, "error", f"{t['id']}: задача уже завершена — дополнение не принято, поставьте новую задачу")
                elif (c.get("text") or "").strip():
                    item = self.add_amendment(t, c["text"], c.get("source") or "cli")
                    try:
                        self.post_amendment(t, item)
                    except (AiflowError, OSError) as e:
                        self.store.log(t["id"], {"amend_comment_failed": str(e)[:300]})
                    self.store.save_task(t)
            elif kind == "retry" and c.get("id") in by_id:
                t = by_id[c["id"]]
                t["retry_requested"] = True
                self.store.save_task(t)

    def check_pause_timer(self) -> None:
        due = [r for r, p in (self.q.get("pauses") or {}).items()
               if p.get("until") and now() >= (parse_iso(p["until"]) or now())]
        if due:
            self.unpause(due, "пауза по лимиту закончилась, очередь продолжает")

    def check_base(self) -> None:
        """Нет базовой ветки на origin — пауза, а не сессии, которые упрутся в это."""
        base = self.cfg["base_branch"]
        key = (str(self.root), base)
        cached = _BASE_SEEN.get(key)
        # Есть ветка — проверять раз в 10 минут; нет или не удалось проверить — раз в минуту.
        if cached is None or time.time() - cached[0] > (600 if cached[1] else 60):
            try:
                p = run(["git", "ls-remote", "--heads", "origin", base], cwd=self.root, timeout=30)
                found = bool(p.stdout.strip()) if p.returncode == 0 else None
            except AiflowError:
                found = None
            cached = _BASE_SEEN[key] = (time.time(), found)
        if cached[1] is None:
            return  # сеть или доступ: не повод останавливать очередь
        if not cached[1]:
            self.pause("base_missing", None, f"базовой ветки {base} нет на origin: поправьте base_branch "
                       "в runs/config.json либо создайте ветку")
        else:
            self.unpause("base_missing", f"базовая ветка {base} есть на origin, очередь продолжает")

    def check_integration(self) -> None:
        r = self.gh.last_push_run(self.cfg["base_branch"])
        if not r or r.get("status") != "completed":
            return
        if r.get("conclusion") in ("failure", "timed_out", "startup_failure"):
            self.pause("integration_red", None, f"ветка {self.cfg['base_branch']} покраснела после мержа: {r.get('url', '')}")
        else:
            self.unpause("integration_red", f"ветка {self.cfg['base_branch']} снова зелёная")

    def advance(self, t: dict) -> None:
        st = t.get("state", "new")
        if t.get("retry_requested") and st == "needs_human":
            return self.do_retry(t)
        if self.handle_amendments(t):
            return  # сессию только что разбудили с дополнением: её снимок в этом проходе устарел
        getattr(self, f"adv_{st}", lambda _t: None)(t)

    # -- дополнения пользователя к идущей задаче
    def add_amendment(self, t: dict, text: str, source: str, at: str | None = None, author: str | None = None) -> dict:
        am = t.setdefault("amendments", [])
        item = {"n": len(am) + 1, "at": at or iso(), "text": text.strip(), "source": source, "author": author,
                "delivered_at": None}
        am.append(item)
        self.store.log(t["id"], {"amendment": item})
        self.emit(t, "amended", f"{t['id']}: дополнение №{item['n']} принято"
                  + (f" (комментарий {author} в PR #{t['pr']})" if source == "comment" else ""), key=f"amend:{item['n']}")
        return item

    def post_amendment(self, t: dict, item: dict) -> None:
        """Дополнение из панели или CLI — ещё и комментарием в PR: для истории и для проверки в aiflow review."""
        if not t.get("pr"):
            return
        tmp = self.store.runs / t["id"] / f"amend-{item['n']}.md"
        tmp.write_text(f"{HUMAN_MARKER}\n{AMEND_MARKER}\n**Дополнение к задаче №{item['n']}**\n\n{item['text']}\n",
                       encoding="utf-8")
        self.gh.call("pr", "comment", str(t["pr"]), "--body-file", str(tmp))
        tmp.unlink(missing_ok=True)

    def scan_comments(self, t: dict) -> None:
        """Комментарии человека в PR после начала реализации — дополнения к задаче."""
        if not t.get("pr") or not (t.get("state") in WORKER_STAGES or t.get("state") == "approved"
                                   or t.get("plan_approved")):
            return
        seen = t.setdefault("comments_seen_at", t.get("plan_seen_at") or iso())
        comments = self.gh.pr(t["pr"], "comments").get("comments") or []
        fresh = sorted((c for c in comments if is_human_comment(c) and after(c.get("createdAt"), seen)),
                       key=lambda c: parse_iso(c.get("createdAt")))
        for c in fresh:
            self.add_amendment(t, c.get("body") or "", "comment", at=c.get("createdAt"),
                               author=(c.get("author") or {}).get("login"))
        if fresh:
            t["comments_seen_at"] = fresh[-1]["createdAt"]

    def sync_inbox(self, t: dict) -> None:
        """Отметить дополнения, которые хук уже отдал работающей сессии."""
        _, done = inbox_dirs()
        if not done.is_dir():
            return
        for f in done.glob("*.json"):
            d = read_json(f, {}) or {}
            if d.get("project") != str(self.root) or d.get("task") != t["id"]:
                continue
            ns = set(d.get("ns") or [])
            for a in t.get("amendments") or []:
                if a["n"] in ns and not a.get("delivered_at"):
                    a["delivered_at"], a["via"] = d.get("at") or iso(), "hook"
            f.unlink(missing_ok=True)

    def session_uuid(self, e: dict | None) -> str | None:
        return (e or {}).get("session_id") or (self.sess(e) or {}).get("sessionId")

    def drop_inbox(self, t: dict) -> None:
        inbox, _ = inbox_dirs()
        if not inbox.is_dir():
            return
        for lst in (t.get("sessions") or {}).values():
            for e in lst:
                sid = self.session_uuid(e)
                if sid:
                    (inbox / f"{sid}.json").unlink(missing_ok=True)

    def handle_amendments(self, t: dict) -> bool:
        st = t.get("state")
        if st in TERMINAL or st == "needs_human":
            if t.get("amendments"):
                self.drop_inbox(t)  # непустые входящие заставляют хук запускать Python в каждой сессии
            return False
        try:
            self.scan_comments(t)
        except AiflowError as e:
            self.store.log(t["id"], {"scan_comments_failed": str(e)[:300]})
        self.sync_inbox(t)
        pending = [a for a in t.get("amendments") or [] if not a.get("delivered_at")]
        if not pending and t.get("amendments"):
            self.drop_inbox(t)
        if not pending or st in ("new", "approved"):
            return False  # уйдут в промпт запуска сессии
        role = "worker" if st in WORKER_STAGES else "planner"
        e = self.cur(t, role)
        s = self.sstate(e)
        inbox, _ = inbox_dirs()
        sid = self.session_uuid(e)
        if s in ("working", "blocked"):
            if sid:
                data = {"project": str(self.root), "task": t["id"], "ns": [a["n"] for a in pending],
                        "text": amend_prompt(t, role, st, pending, live=True)}
                f = inbox / f"{sid}.json"
                if (read_json(f, {}) or {}).get("ns") != data["ns"]:
                    write_json(f, data)
            return False
        if s != "done":
            return False  # failed/stopped/пропала — доставим, когда сессию возобновят
        if sid:
            (inbox / f"{sid}.json").unlink(missing_ok=True)
        if not self.wake(t, role, amend_prompt(t, role, st, pending, live=False)):
            return False
        mark_delivered(t, "wake")
        if role == "planner":
            t["revising"] = True
            if st == "answered":
                self.set_state(t, "discussing", "продолжение вопроса")
        else:
            if st == "in_review" and t.get("pr"):
                self.gh.call("pr", "merge", str(t["pr"]), "--disable-auto")
            t.pop("approved_sha", None)
            t.pop("approved_at", None)
            self.store.approved_path(t["id"]).unlink(missing_ok=True)  # прежнее одобрение дополнения не видело
            self.release_lock(t)
            self.set_state(t, "in_progress", "дополнение пользователя")
        t["state_at"] = iso()  # отсчёт «закончил ход без отчёта» — заново: сессию только что разбудили
        self.emit(t, "amend_delivered", f"{t['id']}: дополнение передано {'планировщику' if role == 'planner' else 'исполнителю'}",
                  key=f"amend_delivered:{pending[-1]['n']}")
        return True

    def adv_discussing(self, t: dict) -> None:
        rep = self.new_report(t, "planner")
        if rep:
            st = rep.get("status")
            if st == "plan_ready" and rep.get("pr"):
                if not self.verify_pr(t, "planner", rep, plan=True):
                    return
                t["pr"] = int(rep["pr"])
                if rep.get("complexity") in ("simple", "complex"):
                    t["complexity"] = rep["complexity"]
                self.save_notes(t, rep)
                t["plan_seen_at"] = iso()
                self.set_state(t, "plan_review", "обсуждение: договорились, план готов")
                self.emit(t, "plan_ready", f"{t['id']}: обсуждение завершено, план готов — PR #{t['pr']}",
                          key=f"plan:{rep['rid']}")
                return
            if st == "answered":
                t["answer"] = {"summary": rep.get("summary") or "", "details": rep.get("details") or "", "at": iso()}
                self.set_state(t, "answered", "ответ дан")
                t["state_at"] = iso()
                self.emit(t, "answered", f"{t['id']}: {rep.get('summary') or 'ответ в сессии'}", key=f"answered:{rep['rid']}")
                return
            if st == "done":
                self.set_state(t, "done", rep.get("summary") or "обсуждение завершено")
                self.emit(t, "done", f"{t['id']}: обсуждение завершено — {rep.get('summary') or 'без задачи'}", key="done")
                return
            if st in ("error", "blocked"):
                return self.to_human(t, rep.get("summary") or "сессия обсуждения сообщила о проблеме", "asks")
        e = self.cur(t, "planner")
        st = self.sstate(e)
        if st in ("failed", "stopped"):
            if not self.check_limit(e, t):
                self.lost(t, "planner", "discussing", f"сессия {st}", "сессия обсуждения остановилась")
        elif st in ("done", "blocked"):
            # ход закончен — ждём ответа пользователя в сессии; напоминаем один раз на каждый ход
            mark = (self.sess(e) or {}).get("updatedAt") or e["id"]
            what = "вопрос" if t.get("kind") == "ask" else "обсуждение"
            self.emit(t, "needs_input", f"{t['id']}: {what} ждёт вас — откройте сессию (панель или "
                      f"claude attach {e['id']})", key=f"discuss:{e['id']}:{mark}")
        elif e and st is None and age_minutes(e.get("resumed_at") or e.get("started_at")) > 10:
            self.lost(t, "planner", "discussing", "сессия пропала", "сессия обсуждения пропала из claude agents")

    def adv_answered(self, t: dict) -> None:
        """Ответ дан. Пользователь может продолжить в сессии (тогда она отчитается план или новый ответ)
        или промолчать — тогда вопрос закрывается без плана."""
        rep = self.new_report(t, "planner")
        if rep:
            t["report_seen"] = None  # пусть adv_discussing разберёт тот же отчёт
            self.set_state(t, "discussing", "продолжение вопроса")
            return self.adv_discussing(t)
        e = self.cur(t, "planner")
        if self.sstate(e) == "working":
            self.set_state(t, "discussing", "продолжение вопроса")
            return
        ttl = float(self.cfg.get("answered_ttl_hours") or 24)
        if age_minutes(t.get("state_at")) > ttl * 60:
            self.set_state(t, "done", "вопрос закрыт: продолжения не было")
            self.adv_merged(t)

    def save_notes(self, t: dict, rep: dict) -> None:
        """Заметки планировщика из отчёта plan_ready — в файл задачи: отчёт перезапишут следующие отчёты,
        а исполнителю заметки нужны при запуске. Отчёт без заметок прежние не стирает."""
        notes = (rep.get("details") or "").strip()
        if notes:
            path = self.store.notes_path(t["id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(notes + "\n", encoding="utf-8")

    def adv_planning(self, t: dict) -> None:
        rep = self.new_report(t, "planner")
        if rep:
            st = rep.get("status")
            if st == "self_execute":
                # планировщик оценил задачу как простую и делает её сам: это уже не планирование, а работа.
                # Стадия та же (итог придёт отчётом ready_for_review или done), но панель показывает её как реализацию.
                t["route"], t["complexity"] = "self", "simple"
                t["route_note"] = short_reason(rep.get("summary") or "", 160)
                self.emit(t, "self_start", f"{t['id']}: простая — планировщик делает сам"
                          + (f": {t['route_note']}" if t["route_note"] else ""), key=f"selfstart:{rep['rid']}")
                return
            if st == "delegate":
                # trivial — лёгкая задача: исполнитель на самой дешёвой модели (worker_model.trivial)
                t["complexity"] = "trivial" if rep.get("complexity") == "trivial" else "simple"
                t["direct"] = True
                t["brief"] = rep.get("details") or rep.get("summary") or ""
                note = rep.get("summary") or ""
                self.emit(t, "delegated", f"{t['id']}: {'лёгкая' if t['complexity'] == 'trivial' else 'простая'}"
                          " — планировщик поручил исполнителю"
                          + (f": {note}" if note else ""), key=f"delegated:{rep['rid']}")
                if not self.start_direct(t):
                    self.set_state(t, "new", "поручено исполнителю, ждёт запуска")
                return
            if st == "ready_for_review":
                if not rep.get("pr"):
                    return self.to_human(t, "планировщик отчитался ready_for_review без номера PR", "report")
                if not self.verify_pr(t, "planner", rep, plan=False):
                    return
                # планировщик сделал задачу сам: его сессия становится сессией исполнителя для ревью
                t["pr"], t["complexity"], t["self_executed"] = int(rep["pr"]), "simple", True
                e = self.cur(t, "planner")
                if e:
                    t.setdefault("sessions", {}).setdefault("worker", []).append(dict(e))
                t.setdefault("comments_seen_at", iso())
                t.setdefault("comments_floor", iso())
                t["ready_at"] = iso()
                self.set_state(t, "ready", "планировщик сделал задачу сам")
                self.emit(t, "self_done", f"{t['id']}: простая — планировщик сделал сам, PR #{t['pr']} в очереди "
                          "на ревью", key=f"self:{rep['rid']}")
                return
            if st == "done" and not t.get("pr"):
                t["complexity"] = t.get("complexity") or "simple"
                self.set_state(t, "done", rep.get("summary") or "выполнено")
                self.emit(t, "done", f"{t['id']}: выполнено — {rep.get('summary') or 'без изменений в репозитории'}",
                          key="done")
                return
            if st == "plan_ready" and rep.get("pr"):
                if not self.verify_pr(t, "planner", rep, plan=True):
                    return
                t["pr"] = int(rep["pr"])
                # trivial у задачи с планом не бывает: это признак задачи без плана (is_direct)
                cx = rep.get("complexity") or t.get("complexity") or "complex"
                t["complexity"] = cx if cx in ("simple", "complex") else "simple"
                self.save_notes(t, rep)
                t["plan_seen_at"] = iso()
                self.set_state(t, "plan_review")
                self.emit(t, "plan_ready", f"{t['id']}: план готов к обсуждению — PR #{t['pr']}",
                          key=f"plan:{rep['rid']}")
                return
            if rep.get("status") in ("error", "blocked"):
                return self.to_human(t, rep.get("summary") or "планировщик сообщил о проблеме", "asks")
        e = self.cur(t, "planner")
        st = self.sstate(e)
        if st in ("failed", "stopped"):
            if not self.check_limit(e, t):
                self.lost(t, "planner", "planning", f"сессия {st}", "сессия планировщика завершилась без плана")
        elif st == "done" and age_minutes((self.sess(e) or {}).get("updatedAt") or t.get("state_at")) > 2:
            self.nudge(t, "planner", "planning", "планировщик закончил ход без aiflow report")
        elif st == "blocked":
            self.blocked(t, "planner", e)
        elif e and st is None and age_minutes(e.get("resumed_at") or e.get("started_at")) > 10:
            self.lost(t, "planner", "planning", "сессия пропала", "сессия планировщика пропала из claude agents")

    def adv_new(self, t: dict) -> None:
        """Задача в очереди: зависимость, которую сняли, сама не выполнится — сказать об этом."""
        gone = [d for d in t.get("depends_on") or [] if (self.by_id.get(d) or {}).get("state") in (None, "cancelled")]
        if gone:
            self.to_human(t, f"зависимость снята или не найдена: {', '.join(gone)}", "deps")

    def adv_plan_review(self, t: dict) -> None:
        pr = self.gh.pr(t["pr"], "state,labels,comments")
        if pr.get("state") in ("CLOSED", "MERGED"):
            self.set_state(t, "cancelled", "PR закрыт на стадии плана")
            self.emit(t, "cancelled", f"{t['id']}: PR с планом закрыт, задача снята", key="cancelled")
            return
        labels = {lb.get("name") for lb in pr.get("labels") or []}
        if self.cfg["labels"]["approved"] in labels or t.get("plan_approved") or is_direct(t):
            # plan_approved и задача без плана в этой стадии — записи старой версии: то же «ждёт исполнителя»
            if not is_direct(t):
                t["plan_approved"] = True
            self.set_state(t, "approved", "план одобрен")
            return
        rep = self.new_report(t, "planner")
        if rep and rep.get("status") == "plan_ready":
            t["revising"] = False
            if rep.get("complexity") in ("simple", "complex"):
                t["complexity"] = rep["complexity"]
            self.save_notes(t, rep)
            self.emit(t, "plan_updated", f"{t['id']}: план обновлён — PR #{t['pr']}", key=f"plan:{rep['rid']}")
        seen = parse_iso(t.get("plan_seen_at")) or now()
        fresh = [c for c in pr.get("comments") or []
                 if not any(m in (c.get("body") or "") for m in MARKERS)
                 and (parse_iso(c.get("createdAt")) or seen) > seen]
        e = self.cur(t, "planner")
        if fresh and self.sstate(e) != "working":
            if self.wake(t, "planner", revise_prompt(t, fresh)):
                t["plan_seen_at"] = max(c["createdAt"] for c in fresh)
                t["revising"] = True

    def adv_approved(self, t: dict) -> None:
        """Ждёт слот исполнителя (запускает schedule). Пока ждёт, PR могли закрыть."""
        if not t.get("pr"):
            return self.set_state(t, "new", "нет PR: задача начинается заново")
        state = self.gh.pr(t["pr"], "state").get("state")
        if state == "MERGED":
            self.finish_merged(t)
        elif state == "CLOSED":
            self.set_state(t, "cancelled", "PR закрыт")
            self.emit(t, "cancelled", f"{t['id']}: PR #{t['pr']} закрыт, задача снята", key="cancelled")

    def adv_in_progress(self, t: dict) -> None:
        rep = self.new_report(t, "worker")
        if rep:
            if rep.get("status") == "done" and not t.get("pr"):
                self.set_state(t, "done", rep.get("summary") or "выполнено")
                self.emit(t, "done", f"{t['id']}: выполнено — {rep.get('summary') or 'без изменений в репозитории'}",
                          key="done")
                return
            if rep.get("status") == "ready_for_review":
                n = t.get("pr") or rep.get("pr")
                if not n:
                    return self.to_human(t, "worker отчитался ready_for_review без номера PR", "report")
                if not self.verify_pr(t, "worker", {**rep, "pr": n}, plan=False):
                    return
                t["pr"] = int(n)
                t["ready_at"] = iso()
                self.set_state(t, "ready", "реализация готова")
                return
            if rep.get("status") in ("error", "blocked", "limit_reached"):
                return self.to_human(t, rep.get("summary") or f"worker: {rep.get('status')}", "asks")
        self.watch_session(t, "worker", "сессия worker'а завершилась до готовности PR")

    def watch_session(self, t: dict, role: str, dead_reason: str) -> None:
        e = self.cur(t, role)
        st = self.sstate(e)
        if st in ("failed", "stopped"):
            if not self.check_limit(e, t):
                self.lost(t, role, t.get("state", "in_progress"), f"сессия {st}", dead_reason)
        elif st == "blocked":
            self.blocked(t, role, e)
        elif st == "done" and age_minutes(t.get("state_at")) > 2 and t.get("state") == "in_progress":
            self.nudge(t, role, "in_progress", f"{role} закончил ход без aiflow report")
        elif e and st is None and age_minutes(e.get("resumed_at") or e.get("started_at")) > 10:
            self.lost(t, role, t.get("state", "in_progress"), "сессия пропала", f"сессия {role} пропала из claude agents")

    def adv_ready(self, t: dict) -> None:
        pr = self.gh.pr(t["pr"], "state,headRefOid,statusCheckRollup")
        t["ci"] = ci_summary(pr.get("statusCheckRollup"), pr.get("headRefOid"))
        if pr.get("state") == "MERGED":
            self.finish_merged(t)
        elif pr.get("state") == "CLOSED":
            self.set_state(t, "cancelled", "PR закрыт")
            self.emit(t, "cancelled", f"{t['id']}: PR #{t['pr']} закрыт, задача снята", key="cancelled")

    def adv_in_review(self, t: dict) -> None:
        pr = self.gh.pr(t["pr"], "state,labels,headRefOid,statusCheckRollup")
        t["ci"] = ci_summary(pr.get("statusCheckRollup"), pr.get("headRefOid"))
        if pr.get("state") == "MERGED":
            return self.finish_merged(t)
        if pr.get("state") == "CLOSED":
            self.release_lock(t)
            self.set_state(t, "cancelled", "PR закрыт")
            self.emit(t, "cancelled", f"{t['id']}: PR #{t['pr']} закрыт, задача снята", key="cancelled")
            return
        labels = {lb.get("name") for lb in pr.get("labels") or []}
        if self.watch_auto_review(t):
            return
        rep = self.new_report(t, "worker")
        if rep:
            st = rep.get("status")
            if st == "approved":
                t["approved_sha"] = pr.get("headRefOid")
                t["approved_at"] = iso()
            elif st == "paused":
                # очередь на паузе: мерж отложен. PR возвращается в очередь ревью на своё место; после паузы
                # исполнителя разбудят снова, и одобренный дифф второй раз не ревьюится
                self.release_lock(t)
                t.pop("approved_sha", None)
                t.pop("approved_at", None)
                self.set_state(t, "ready", "ревью отложено: очередь на паузе")
                return
            elif st == "too_large":
                t["size_hold"] = True  # «Повторить» от человека снимет предел для этой задачи
                return self.to_human(t, rep.get("summary") or "изменение больше предела для задачи без плана", "size")
            elif st in ("limit_reached", "error", "no_ci", "already_closed", "blocked"):
                action = "ci" if st == "limit_reached" and "CI" in (rep.get("summary") or "") else REVIEW_ACTIONS[st]
                return self.to_human(t, rep.get("summary") or f"ревью: {st}", action)
        if self.cfg["labels"]["needs_human"] in labels:
            return self.to_human(t, "PR помечен needs-human", "label")
        if t.get("approved_sha") and t["approved_sha"] == pr.get("headRefOid"):
            failed = [c.get("name") or c.get("context") for c in pr.get("statusCheckRollup") or []
                      if (c.get("conclusion") or c.get("state") or "").upper() in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED")]
            if failed and t.get("ci_wake") != pr.get("headRefOid"):
                t["ci_wake"] = pr.get("headRefOid")
                if not self.wake(t, "worker", ci_fix_prompt(t, failed)):
                    self.to_human(t, "после одобрения упал CI, а сессию worker'а не удалось разбудить", "session")
            elif age_minutes(t.get("approved_at") or t.get("state_at")) > float(
                    self.cfg.get("merge_wait_minutes") or 60):
                # одобрен, но не мержится (автомерж выключен, защита ветки ждёт одобрения): не держим очередь
                # ревью. Когда PR смержат, задача закроется сама из «нужен человек».
                self.to_human(t, f"PR #{t['pr']} одобрен, но не смержен за {self.cfg.get('merge_wait_minutes') or 60} мин",
                              "merge")
            return
        e = self.cur(t, "worker")
        last_review = (self.store.review(t["id"]) or {}).get("at") or t.get("state_at")
        if self.sstate(e) in ("failed", "stopped"):
            if not self.check_limit(e, t):
                self.lost(t, "worker", "in_review", f"сессия {self.sstate(e)}", "сессия worker'а завершилась во время ревью")
        elif e and self.sstate(e) is None and age_minutes(e.get("resumed_at") or e.get("started_at")) > 10 \
                and age_minutes(last_review) > 10:
            self.lost(t, "worker", "in_review", "сессия пропала", "сессия worker'а пропала из claude agents во время ревью")
        elif self.sstate(e) == "done" and age_minutes(last_review) > 5 and age_minutes(t.get("state_at")) > 5:
            self.nudge(t, "worker", "in_review", "worker закончил ход, не доведя ревью до итогового статуса")
        elif self.sstate(e) == "blocked":
            self.blocked(t, "worker", e)
        elif self.sstate(e) == "working" and not self.fresh(e):
            # Сессия «занята», но это ещё не значит, что ревью идёт: она могла закончить ход и ждать фоновую
            # команду, которая зависла. Самый долгий честный простой — ожидание CI плюс три попытки ревьюера.
            limit = float(self.cfg.get("ci_timeout_minutes") or 45) + 60
            quiet = [age_minutes(last_review), age_minutes(t.get("state_at"))]
            written = transcript_age_minutes(self.session_uuid(e))
            if written is not None:
                quiet.append(written)
            if min(quiet) > limit:
                self.to_human(t, f"ревью не двигается {int(min(quiet))} мин, хотя сессия числится занятой", "stall")

    def adv_needs_human(self, t: dict) -> None:
        if self.continued_by_human(t):
            return
        if not t.get("pr"):
            return
        pr = self.gh.pr(t["pr"], "state,labels")
        if pr.get("state") == "MERGED":
            return self.finish_merged(t)
        labels = {lb.get("name") for lb in pr.get("labels") or []}
        if self.cfg["labels"]["retry"] in labels:
            t["retry_requested"] = True
            self.do_retry(t)

    def add_reviewer_usage(self, t: dict) -> None:
        u = reviewer_usage(self.store, t["id"])
        if u:
            t.setdefault("usage", {})["reviewer"] = u

    def adv_merged(self, t: dict) -> None:
        self.add_reviewer_usage(t)  # последний раунд ревью заканчивается мержем: забрать его расход до уборки
        lost = [a for a in t.get("amendments") or [] if not a.get("delivered_at")]
        if lost and not t.get("amend_lost_told"):
            t["amend_lost_told"] = True
            self.emit(t, "amend_lost", f"{t['id']}: смержено раньше, чем дошли дополнения "
                      f"{', '.join('№' + str(a['n']) for a in lost)} — поставьте их новой задачей")
        if t.get("cleaned"):
            return
        # Сессии законченной задачи сразу не удаляем: в них ход работы и итог (у задачи без PR — только там),
        # их читают и в них продолжают разговор. claude rm удаляет и рабочую копию, поэтому срок всё же есть.
        days = float(self.cfg.get("session_keep_days") or 0)
        mine = [e["id"] for lst in (t.get("sessions") or {}).values() for e in lst if e["id"] in self.sessions]
        if not mine:
            t["cleaned"] = True  # сессий уже нет (удалены вручную): следить больше не за чем
        elif days > 0 and age_minutes(t.get("state_at")) > days * 24 * 60:
            for sid in mine:
                self.claude.rm(sid)
            t["cleaned"] = True

    adv_done = adv_merged  # задача без PR: итог в событии, в report.json и в её сессии

    def adv_cancelled(self, t: dict) -> None:
        self.release_lock(t)
        t["cleaned"] = True

    def continued_by_human(self, t: dict) -> bool:
        """Человек ответил сессии напрямую (дал доступ, «всё норм»), и она продолжила работу:
        вернуть задачу в прежнюю стадию без «Повторить»."""
        w = t.get("human_watch") or {}
        role = w.get("role")
        prev = next((h.get("from") for h in reversed(t.get("history") or []) if h.get("to") == "needs_human"), None)
        if not role or STAGE_ROLE.get(prev or "") != role:
            return False
        st = self.sstate(self.cur(t, role))
        rep = self.store.report(t["id"]) or {}
        new_report = rep.get("role") == role and rep.get("rid") not in (None, w.get("report"))
        woke = st == "working" and w.get("seen") != "working"
        if st != w.get("seen"):
            w["seen"] = st
        if not (woke or new_report):
            return False
        if prev == "in_review":
            locks = self.q.setdefault("review_locks", [])
            if t["id"] not in locks:
                if not self.review_free(t, locks):
                    return False  # ревью занято — вернём, когда освободится
                locks.append(t["id"])
        t["human_reason"] = None
        t.pop("human_action", None)
        t.pop("human_watch", None)
        t.pop("auto_resumed", None)
        if t.get("pr"):
            self.gh.remove_label(t["pr"], self.cfg["labels"]["needs_human"])
        self.set_state(t, prev, "сессия продолжила работу после ответа человека")
        self.emit(t, "continued", f"{t['id']}: {role} продолжил работу — задача снова в стадии «{STAGE_LABEL.get(prev, prev)}»",
                  key=f"continued:{iso()}")
        return True

    def do_retry(self, t: dict) -> None:
        t["retry_requested"] = False
        # Всё, что сессия сообщила до этого момента, человек уже видел. Иначе отчёт об ошибке, записанный,
        # пока задача ждала человека, сработал бы как новый и сразу вернул её в «нужен человек».
        stale = self.store.report(t["id"])
        if stale:
            t["report_seen"] = stale.get("rid")
        reason = t.get("human_reason") or "нужен человек"
        note = t.pop("retry_note", None)
        if note:
            reason += f"»; {note}; «продолжай"
        t["human_reason"] = None
        t.pop("human_action", None)
        t.pop("human_watch", None)
        if t.get("pr"):
            self.gh.remove_label(t["pr"], self.cfg["labels"]["needs_human"])
            self.gh.remove_label(t["pr"], self.cfg["labels"]["retry"])
        self.release_lock(t)
        t.pop("auto_resumed", None)
        t.pop("nudged", None)
        t.pop("bounced", None)
        t.pop("limit_hits", None)
        t.pop("approved_sha", None)
        t.pop("approved_at", None)
        t.pop("auto_review", None)
        if t.pop("size_hold", None):
            t["size_ok"] = True  # человек видел размер изменения и велел продолжать
        # Сначала — та же сессия с её контекстом и рабочим каталогом, в той же стадии.
        prev = next((h.get("from") for h in reversed(t.get("history") or []) if h.get("to") == "needs_human"), None)
        locks = self.q.setdefault("review_locks", [])
        if prev == "in_review" and self.sstate(self.cur(t, "worker")) == "working" and self.review_free(t, locks):
            # Сессия исполнителя не останавливалась и уже ведёт ревью: будить её нечем и незачем.
            # Задача сразу занимает своё место в ревью, а не числится в очереди без него.
            locks.append(t["id"])
            self.touch(t, "worker")
            self.set_state(t, "in_review", "повтор: сессия уже ведёт ревью")
            self.emit(t, "retry", f"{t['id']}: повтор — сессия уже ведёт ревью", key=f"retry:{iso()[:16]}")
            return
        if prev == "in_review" and self.cur(t, "worker"):
            # Ревью идёт строго по одному: задача снова встаёт в очередь ревью, и монитор будит
            # эту же сессию worker'а (с её контекстом), когда освободится lock.
            self.set_state(t, "ready", "повтор: ревью той же сессией")
            self.emit(t, "retry", f"{t['id']}: повтор — снова в очереди на ревью", key=f"retry:{iso()[:16]}")
            return
        role = {"planning": "planner", "discussing": "planner", "in_progress": "worker"}.get(prev or "")
        if role and self.try_resume(t, role, prev, f"повтор после «{reason}»", auto=False):
            self.set_state(t, prev, "повтор: та же сессия")
            return
        # Сессию не вернуть — новая. Куда встаёт задача:
        #   нет PR                           → new: планировщик (или исполнитель, если задача без плана) с начала;
        #   есть PR, план одобрен или не нужен → approved: новый исполнитель на той же ветке;
        #   есть PR, план ещё не одобрен      → plan_review: ждёт одобрения, как и до сбоя.
        t["retry_pending"] = True
        if not t.get("pr"):
            self.set_state(t, "new", "повтор")
        elif t.get("plan_approved") or is_direct(t):
            self.set_state(t, "approved", "повтор: новый исполнитель")
        else:
            self.set_state(t, "plan_review", "повтор")
        self.emit(t, "retry", f"{t['id']}: повтор поставлен в очередь", key=f"retry:{iso()[:16]}")

    def finish_merged(self, t: dict) -> None:
        self.release_lock(t)
        self.set_state(t, "merged")
        self.emit(t, "merged", f"{t['id']}: смержено в {self.cfg['base_branch']} — PR #{t['pr']}", key="merged")
        self.adv_merged(t)

    def start_direct(self, t: dict) -> bool:
        """Исполнитель для задачи без плана: сразу делает её по тексту и указаниям планировщика."""
        retry = bool(t.pop("retry_pending", False))
        model, effort = worker_choice(self.cfg, t)
        prompt = direct_prompt(self.store, t, self.cfg) + launch_amendments(t)
        t.setdefault("comments_seen_at", iso())
        t.setdefault("comments_floor", iso())
        if t.get("pr"):
            prompt += (f"\nThe task already has PR #{t['pr']} from the branch ai/{t['id']}: work in this branch "
                       f"(git fetch origin; git switch ai/{t['id']}). Do not open a new PR. Complete the work and "
                       f"report ready_for_review with --pr {t['pr']}.")
        if retry:
            prompt += ("\nThis is a new attempt after a stop. The last report: "
                       + json.dumps(self.store.report(t["id"]) or {}, ensure_ascii=False))
        if not self.launch(t, "worker", prompt, model=model, effort=effort):
            if retry:
                t["retry_pending"] = True
            return False
        mark_delivered(t, "launch")
        self.set_state(t, "in_progress")
        return True

    def deps_ok(self, t: dict, by_id: dict[str, dict]) -> bool:
        return all((by_id.get(d) or {}).get("state") in FINISHED for d in t.get("depends_on") or [])

    def schedule(self, tasks: list[dict]) -> None:
        by_id = {t["id"]: t for t in tasks}
        conc = int(self.cfg["concurrency"])
        active = sum(1 for t in tasks if t.get("state") in IMPLEMENTING or t.get("launching"))
        for t in tasks:
            if active >= conc and t.get("kind") not in TALK_KINDS:
                continue
            if t.get("launching"):
                continue
            st = t.get("state")
            if st == "new" and self.deps_ok(t, by_id) and t.get("kind") in TALK_KINDS:
                # разговор ждёт человека, а не модель: слот concurrency не занимает
                ask = t.get("kind") == "ask"
                prompt = (ask_prompt if ask else discuss_prompt)(self.store, t, self.cfg) + launch_amendments(t)
                model, effort = planner_choice(self.cfg, t, (
                    self.cfg.get("ask_model" if ask else "discuss_model") or ("sonnet" if ask else "opus"),
                    self.cfg.get("ask_effort" if ask else "discuss_effort") or None))
                ok = self.launch(t, "planner", prompt, model=model, effort=effort)
                if ok:
                    mark_delivered(t, "launch")
                    self.set_state(t, "discussing")
                self.store.save_task(t)
                continue
            if st == "new" and self.deps_ok(t, by_id) and is_direct(t):
                if self.start_direct(t):
                    active += 1
                self.store.save_task(t)
            elif st == "new" and self.deps_ok(t, by_id):
                model, effort = planner_choice(self.cfg, t)
                ok = self.launch(t, "planner", planner_prompt(self.store, t, self.cfg) + launch_amendments(t),
                                 model=model, effort=effort)
                if ok:
                    t.pop("retry_pending", None)
                    mark_delivered(t, "launch")
                    self.set_state(t, "planning")
                    active += 1
                self.store.save_task(t)
            elif st == "approved" and is_direct(t):
                if self.start_direct(t):
                    active += 1
                self.store.save_task(t)
            elif st == "approved":
                retry = bool(t.pop("retry_pending", False))
                model, effort = worker_choice(self.cfg, t)
                t.setdefault("comments_floor", iso())
                ok = self.launch(t, "worker", worker_prompt(self.store, t, self.cfg, retry=retry) + launch_amendments(t),
                                 model=model, effort=effort)
                if ok:
                    mark_delivered(t, "launch")
                    self.set_state(t, "in_progress")
                    active += 1
                elif retry:
                    t["retry_pending"] = True
                self.store.save_task(t)
        self.schedule_reviews(tasks, by_id)

    def changed_files(self, t: dict) -> set[str] | None:
        """Файлы PR задачи (с кэшем по коммиту); None — узнать не удалось."""
        try:
            sha = self.gh.pr(t["pr"], "headRefOid").get("headRefOid")
        except AiflowError:
            return None
        cached = t.get("files_cache") or {}
        if sha and cached.get("sha") == sha:
            return set(cached.get("files") or [])
        files = self.gh.pr_files(t["pr"])
        if files is not None and sha:
            t["files_cache"] = {"sha": sha, "files": sorted(files)}
        return files

    def review_free(self, t: dict, locks: list[str], waiting: tuple = ()) -> bool:
        """Можно ли взять PR на ревью сейчас. Свободно — да. Иначе — только если есть место
        (review_concurrency) и у PR нет общих файлов ни с PR на ревью, ни с теми, кто ждёт в очереди
        раньше него: такие PR не конфликтуют при мерже, а порядок очереди для остальных сохраняется."""
        others = [self.by_id[i] for i in locks if i in self.by_id and i != t["id"]] + list(waiting)
        if not others:
            return True
        if len(locks) >= max(1, int(self.cfg.get("review_concurrency") or 1)):
            return False
        mine = self.changed_files(t)
        if mine is None:
            return False  # не знаем файлов — считаем, что пересекаются
        for o in others:
            theirs = self.changed_files(o) if o.get("pr") else None
            if theirs is None or mine & theirs:
                return False
        return True

    def schedule_reviews(self, tasks: list[dict], by_id: dict[str, dict]) -> None:
        """Очередь ревью: по порядку готовности; сразу несколько — только PR без общих файлов."""
        locks = self.q["review_locks"] = [i for i in self.q.get("review_locks") or []
                                          if (by_id.get(i) or {}).get("state") == "in_review"]
        limit = max(1, int(self.cfg.get("review_concurrency") or 1))
        ready = sorted((t for t in tasks if t.get("state") == "ready"), key=lambda t: t.get("ready_at") or "")
        skipped: list[dict] = []
        for t in ready:
            if len(locks) >= limit:
                break
            if self.sstate(self.cur(t, "worker")) == "working" or not self.review_free(t, locks, tuple(skipped)):
                skipped.append(t)  # сессия ещё не закончила ход либо PR пересекается с идущим ревью
                continue
            woke = self.start_auto_review(t) or self.wake_for_review(t, review_prompt(t, self.cfg))
            if woke:
                locks.append(t["id"])
                if not t.get("auto_review"):
                    self.touch(t, "worker")
                self.set_state(t, "in_review")
            else:
                skipped.append(t)
            self.store.save_task(t)

    def wake_for_review(self, t: dict, prompt: str) -> bool:
        """Разбудить исполнителя на ревью; не вышло — запустить нового с контекстом задачи."""
        if self.wake(t, "worker", prompt):
            return True
        model, effort = worker_choice(self.cfg, t)
        return self.launch(t, "worker", prompt + "\n\nTask context: " + worker_prompt(self.store, t, self.cfg),
                           model=model, effort=effort)

    def start_auto_review(self, t: dict) -> bool:
        """Запустить aiflow review самому, не будя модель. Только когда ветка PR не отстаёт от базовой:
        тогда rebase и прогон тестов перед ревью не нужны (CI их и так гоняет), и ход исполнителя сводится
        к одной команде. Отстаёт или не удалось узнать — будим исполнителя, как раньше."""
        if not self.cfg.get("auto_review", True):
            return False
        e = self.cur(t, "worker")
        cwd = (e or {}).get("cwd") or (self.sess(e) or {}).get("cwd")
        if not cwd or not Path(cwd).is_dir():
            return False  # ревью читает код из рабочей копии задачи
        try:
            head = self.gh.pr(t["pr"], "headRefOid").get("headRefOid")
        except AiflowError:
            return False
        if not head or self.gh.behind(self.cfg["base_branch"], head) != 0:
            return False
        pid = self.spawn_review(self.store, t, cwd)
        if not pid:
            return False
        t["auto_review"] = {"pid": pid, "at": iso(), "head": head}
        self.store.log(t["id"], {"auto_review": t["auto_review"]})
        return True

    def watch_auto_review(self, t: dict) -> bool:
        """Ревью, запущенное самим aiflow. True — оно ещё идёт или его итог только что разобран."""
        info = t.get("auto_review")
        if not info:
            return False
        rv = self.store.review(t["id"]) or {}
        finished = after(rv.get("at"), info.get("at")) or (rv.get("at") == info.get("at") and rv.get("status"))
        if not finished:
            if owner_alive(info) or age_minutes(info.get("at")) < 1:
                return True  # идёт: сессия исполнителя при этом простаивает, и это нормально
            t.pop("auto_review", None)  # процесс исчез без результата — пусть ревью ведёт исполнитель
            self.store.log(t["id"], {"auto_review_lost": info})
            if not self.wake_for_review(t, review_prompt(t, self.cfg)):
                self.to_human(t, "ревью не запустилось, а исполнителя не удалось разбудить", "session")
            else:
                self.touch(t, "worker")
            return True
        t.pop("auto_review", None)
        status = rv.get("status")
        if status in ("changes_requested", "ci_failed", "merge_conflict", "human_input"):
            # нужна работа модели: будим исполнителя с готовым результатом, дальше цикл ведёт он
            if self.wake_for_review(t, review_result_prompt(self.store, t, self.cfg, status)):
                self.touch(t, "worker")
            else:
                self.to_human(t, f"ревью: {status}, а исполнителя не удалось разбудить", "session")
            return True
        return False  # итоговые статусы пришли отчётом — их разберёт обычная ветка

    def collect_usage(self, tasks: list[dict]) -> None:
        for t in tasks:
            if t.get("cleaned"):
                continue
            entries = sorted(((e.get("started_at") or "", role, e) for role, lst in (t.get("sessions") or {}).items()
                              for e in lst if e.get("session_id")), key=lambda x: x[0])
            seen: set[str] = set()
            counted: set[str] = set()
            for _, role, e in entries:  # по времени: общая история достаётся сессии, где она появилась
                if e["session_id"] in counted:
                    continue  # одна сессия в двух ролях (планировщик сделал задачу сам): считаем один раз
                counted.add(e["session_id"])
                u = transcript_usage(e["session_id"], seen)
                if u:
                    t.setdefault("usage", {})[e["session_id"]] = {"role": role, **u}
            self.add_reviewer_usage(t)
            self.store.save_task(t)

    def check_batch_done(self, tasks: list[dict]) -> None:
        live = [t for t in tasks if t.get("state") not in TERMINAL and t.get("state") not in ("needs_human", "answered")
                and t.get("kind") not in TALK_KINDS]
        merged_unannounced = [t for t in tasks if t.get("state") in FINISHED and not t.get("batch_announced")]
        if not live and merged_unannounced:
            for t in merged_unannounced:
                t["batch_announced"] = True
                self.store.save_task(t)
            human = [t["id"] for t in tasks if t.get("state") == "needs_human"]
            n_merged = sum(1 for t in merged_unannounced if t.get("state") == "merged")
            parts = [f"смержено {n_merged}"] if n_merged else []
            if len(merged_unannounced) > n_merged:
                parts.append(f"выполнено без PR {len(merged_unannounced) - n_merged}")
            text = "пачка завершена: " + ", ".join(parts)
            if human:
                text += f", ждут вас: {', '.join(human)}"
            self.emit(None, "batch_done", text)


ORCHESTRATOR_NAME = "aiflow"


def orchestrator_session(store: "Store", sessions: dict[str, dict]) -> str | None:
    """Короткий id сессии координатора проекта, к которой можно подключиться, или None.
    Сначала — сессия, в которой работает монитор (она записала себя в runs/orchestrator.json), затем —
    фоновая сессия с именем aiflow. Остановленная и чужая интерактивная (без id) не годятся."""
    usable = {sid: s for sid, s in sessions.items() if sid and s.get("state") != "stopped"}
    seen = (read_json(store.runs / "orchestrator.json", {}) or {}).get("session_id")
    if seen:
        for sid, s in usable.items():
            if s.get("sessionId") == seen:
                return sid
    root = str(store.root).replace("\\", "/").rstrip("/").lower()
    named = [sid for sid, s in usable.items() if s.get("name") == ORCHESTRATOR_NAME
             and str(s.get("cwd") or "").replace("\\", "/").rstrip("/").lower() in ("", root)]
    return max(named, key=lambda sid: usable[sid].get("startedAt") or 0) if named else None


def open_orchestrator(store: "Store", claude=None) -> tuple[str, bool]:
    """Сессия координатора проекта: существующая или только что запущенная. → (id, запущена ли новая)."""
    claude = claude or Claude(store.root)
    sid = orchestrator_session(store, claude.agents())
    if sid:
        return sid, False
    sid, out = claude.launch("aiflow:orchestrator", ORCHESTRATOR_NAME,
                             "Be ready for tasks. The user gives them through /aiflow:intake.",
                             mode=store.config().get("permission_mode"))
    if not sid:
        raise AiflowError("не удалось запустить сессию координатора: " + out[-200:])
    return sid, True


# ---------------------------------------------------------------- status / state for UI

STAGE_LABEL = {
    "new": "в очереди", "discussing": "обсуждение идеи", "answered": "ответ дан", "planning": "планирование", "plan_review": "обсуждение плана",
    "approved": "ждёт исполнителя", "in_progress": "реализация", "ready": "ждёт ревью", "in_review": "ревью",
    "merged": "смержено", "done": "выполнено", "needs_human": "нужен человек", "cancelled": "снята",
}


def build_state(store: Store, sessions: dict[str, dict] | None = None, repo_url: str = "") -> dict:
    cfg = store.config()
    q = store.queue()
    tasks = store.tasks()
    prices = price_table()
    project_usage: dict[str, dict] = {}
    out = []
    for t in tasks:
        st = t.get("state", "new")
        usage, cost, _ = priced(task_usage(t), prices)
        add_usage(project_usage, task_usage(t))
        cur = {}
        for role in ("worker", "planner"):
            lst = (t.get("sessions") or {}).get(role) or []
            if lst:
                e = lst[-1]
                live = (sessions or {}).get(e["id"]) or {}
                cur = {"role": role, "id": e["id"], "state": live.get("state"), "waiting": live.get("waitingFor")}
                break
        review = store.review(t["id"]) or {}
        pr = t.get("pr")
        needs = None
        if st == "plan_review" and not t.get("plan_approved"):
            needs = "обсудить и одобрить план"
        elif st == "needs_human":
            needs = short_reason(t.get("human_reason")) or "нужен человек"
        elif st == "discussing" and cur.get("state") != "working":
            needs = ("вопрос ждёт вас — откройте сессию" if t.get("kind") == "ask"
                     else "обсуждение ждёт вас — откройте сессию")
        elif cur.get("state") == "blocked":
            needs = f"сессия ждёт ответа ({cur.get('waiting') or 'вопрос'})"
        last_stage = None
        if st not in STAGES:
            for h in reversed(t.get("history") or []):
                if h.get("from") in STAGES:
                    last_stage = STAGES.index(h["from"])
                    break
        doing_self = st == "planning" and t.get("route") == "self"
        stage = STAGES.index("in_progress") if doing_self else STAGES.index("planning") if st in ("discussing", "answered") else STAGES.index("plan_review") if st == "approved" else STAGES.index("merged") if st == "done" else STAGES.index(st) if st in STAGES else last_stage
        # Вопрос или обсуждение без PR дальше «плана» не идёт: остальные шаги доски к нему не относятся,
        # и панель их не рисует. Появился PR — задача идёт обычным путём, и шаги показываются все.
        last_station = STAGES.index("planning") if t.get("kind") in TALK_KINDS and not pr else None
        if last_station is not None and stage is not None:
            stage = min(stage, last_station)
        out.append({
            "id": t["id"], "title": t.get("title", ""), "state": st,
            "label": "планировщик делает сам" if doing_self else
                     "вопрос" if st == "discussing" and t.get("kind") == "ask" else STAGE_LABEL.get(st, st),
            "kind": t.get("kind") or "task", "answer": (t.get("answer") or {}).get("summary") if st == "answered" else done_note(t) if st == "done" else None,
            "stage": stage, "last_station": last_station,
            "complexity": t.get("complexity"), "pr": pr,
            "pr_url": f"{repo_url}/pull/{pr}" if pr and repo_url else None,
            "rounds": review.get("round"), "max_rounds": cfg["max_rounds"],
            "allowed_paths": t.get("allowed_protected_paths") or [],
            "tokens": task_tokens(t), "usage": usage, "cost": round(cost, 4), "session": cur, "needs": needs,
            "action": t.get("human_action") if st == "needs_human" else None,
            "activity": ("планировщик делает сам" + (f": {t['route_note']}" if t.get("route_note") else "")) if doing_self
                        else review_activity(store, t, cur.get("state"), int(cfg["max_rounds"])),
            "ci": t.get("ci") if st in ("ready", "in_review") else None,
            "models": {f: t[f] for f in MODEL_FIELDS if t.get(f)},
            "reviewer_session": next((r.get("session") for r in reversed(reviewer_runs(store, t["id"])) if r.get("session")), None),
            "depends_on": t.get("depends_on") or [], "state_at": t.get("state_at"),
            "launching": bool(t.get("launching")), "review_lock": t["id"] in (q.get("review_locks") or []),
            "amendments": len(t.get("amendments") or []),
            "amend_pending": sum(1 for a in t.get("amendments") or [] if not a.get("delivered_at")),
        })
    total_usage, total_cost, unpriced = priced(project_usage, prices)
    return {
        "version": VERSION, "repo": store.root.name, "repo_url": repo_url, "base": cfg["base_branch"],
        "paused": q.get("paused"), "review_lock": q.get("review_lock"), "concurrency": cfg["concurrency"],
        "monitor_pid": FileLock(store.runs / "monitor.lock").owner(),
        "usage": total_usage, "cost": round(total_cost, 4), "unpriced": unpriced,
        "tasks": out, "events": list(reversed(store.events(40))), "now": iso(),
    }


def fmt_tokens(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def print_status(state: dict) -> None:
    p = state["paused"]
    head = f"aiflow · {state['repo']} · база {state['base']}"
    head += f" · ПАУЗА ({', '.join(p.get('reasons') or [p.get('reason')])})" if p else ""
    head += "" if state["monitor_pid"] else " · монитор не запущен"
    print(head)
    if not state["tasks"]:
        print("Задач нет. Добавьте их через /aiflow:intake.")
        return
    rows = [("задача", "состояние", "PR", "раунды", "токены", "цена", "сессия")]
    for t in state["tasks"]:
        s = t["session"]
        rows.append((t["id"], t["label"] + (" 🔒" if t["review_lock"] else "")
                     + (f" · {t['activity']}" if t.get("activity") and t["state"] == "in_review" else ""),
                     f"#{t['pr']}" if t["pr"] else "—",
                     f"{t['rounds']}/{t['max_rounds']}" if t["rounds"] else "—",
                     fmt_tokens(t["tokens"]), f"${t['cost']:.2f}" if t.get("cost") else "—",
                     f"{s.get('role')} {s.get('id')} {s.get('state') or ''}".strip() if s else "—"))
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))
    if state.get("cost"):
        print(f"Всего по проекту: ${state['cost']:.2f} по ценам API"
              + (f" (без цены: {', '.join(state['unpriced'])})" if state.get("unpriced") else ""))
    waiting = [t for t in state["tasks"] if t["needs"]]
    if waiting:
        print("\nЖдут вас:")
        for t in waiting:
            print(f"  {t['id']}: {t['needs']}" + (f" — {t['pr_url']}" if t["pr_url"] else ""))
            if t.get("action"):
                print(f"    что сделать: {t['action']}")


# ---------------------------------------------------------------- commands

def cmd_init(args, root: Path) -> int:
    store = Store(root)
    store.runs.mkdir(exist_ok=True)
    created = []
    if not (store.runs / "config.json").exists():
        cfg = dict(DEFAULT_CONFIG)
        p = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=root, timeout=30)
        if p.returncode == 0 and p.stdout.strip().startswith("origin/"):
            cfg["base_branch"] = p.stdout.strip()[len("origin/"):]
        write_json(store.runs / "config.json", cfg)
        created.append("runs/config.json")
    else:
        added = store.migrate_config()
        if added:
            created.append("runs/config.json (+" + ", ".join(added) + ")")
    gi = root / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    add = [x for x in ("runs/", ".claude/worktrees/") if x not in lines]
    if add:
        gi.write_text("\n".join(lines + add) + "\n", encoding="utf-8")
        created.append(".gitignore (+" + ", ".join(add) + ")")
    for src, dst in (("ci.yml", ".github/workflows/ci.yml"),
                     ("ARCHITECTURE.md", "docs/ARCHITECTURE.md")):
        d = root / dst
        if not d.exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(TEMPLATES / src, d)
            created.append(dst)
    sp = root / ".claude" / "settings.json"
    settings = read_json(sp, {}) or {}
    tpl = read_json(TEMPLATES / "settings.json", {})
    perms = settings.setdefault("permissions", {})
    for key in ("allow", "deny"):
        cur = perms.setdefault(key, [])
        for rule in tpl["permissions"][key]:
            if rule not in cur:
                cur.append(rule)
    write_json(sp, settings)
    created.append(".claude/settings.json (права для фоновых сессий)")
    created.append(setup_desktop_preview(root, store))
    if shutil.which("gh"):
        cfg = store.config()
        for name, color, desc in ((cfg["labels"]["approved"], "0E8A16", "План одобрен, можно реализовывать"),
                                  (cfg["labels"]["needs_human"], "D93F0B", "aiflow: нужен человек"),
                                  (cfg["labels"]["retry"], "1D76DB", "aiflow: повторить задачу")):
            run(["gh", "label", "create", name, "--color", color, "--description", desc, "--force"], cwd=root)
        created.append("метки GitHub: plan-approved, needs-human, retry")
    import aiflow_service as svc
    if svc.register(root):
        created.append("проект добавлен в службу aiflow (~/.aiflow/projects.json)")
    print("Готово:\n  " + "\n  ".join(created))
    print("\nДальше:\n"
          "  1. Впишите модель ревьюера в runs/config.json → reviewer_model (opencode models deepseek).\n"
          f"  2. Заполните .github/workflows/ci.yml и сделайте job обязательной проверкой для {store.config()['base_branch']}.\n"
          "  3. Добавьте в .claude/settings.json allow-правила для сборки и тестов проекта.\n"
          "  4. Закоммитьте .claude/settings.json, .github/, docs/.\n"
          "  5. aiflow doctor")
    return 0


def setup_desktop_preview(root: Path, store: Store) -> str:
    """Добавляет панель aiflow в .claude/launch.json — Browser pane вкладки Code в Claude Desktop."""
    import aiflow_service as svc
    port = int(svc.global_config().get("ui_port") or svc.DEFAULT_PORT)
    entries = [
        # подключиться к панели, которую уже поднял монитор
        {"name": "aiflow", "url": f"http://127.0.0.1:{port}", "port": port},
        # или запустить службу (монитор всех проектов и панель), если она не работает
        {"name": "aiflow (запустить службу)", "runtimeExecutable": sys.executable,
         "runtimeArgs": [str(Path(__file__).resolve()), "up", "--foreground"],
         "port": port, "autoPort": False},
    ]
    lp = root / ".claude" / "launch.json"
    if lp.exists():
        data = read_json(lp)
        if not isinstance(data, dict):
            return (".claude/launch.json не тронут (в нём комментарии или нестандартный JSON) — "
                    "добавьте конфигурации aiflow вручную, см. README")
    else:
        data = {"version": "0.0.1", "configurations": []}
    confs = [c for c in data.setdefault("configurations", []) if not str(c.get("name", "")).startswith("aiflow")]
    data["configurations"] = confs + entries
    write_json(lp, data)
    return ".claude/launch.json (панель aiflow в Browser pane Claude Desktop)"


def check(label: str, ok: bool, hint: str = "") -> tuple[bool, str]:
    return ok, ("✓ " if ok else "✗ ") + label + ("" if ok or not hint else f" — {hint}")


def doctor_checks(root: Path | None, quick: bool) -> list[tuple[bool, str]]:
    res = []
    res.append(check(f"python {sys.version.split()[0]}", sys.version_info >= (3, 9), "нужен 3.9+"))
    for tool in ("git", "gh", "claude", "opencode"):
        res.append(check(tool, bool(shutil.which(tool)), "не найден в PATH"))
    if root is None:
        res.append(check("git-репозиторий", False, "запустите в репозитории"))
        return res
    store = Store(root)
    res.append(check("runs/config.json", store.initialized, "выполните aiflow init"))
    identity = store.config().get("review_identity") or "self"
    if identity == "bot":
        token = os.environ.get("REVIEW_BOT_TOKEN") or (Path.home() / ".config" / "ai-review" / "token").is_file()
        res.append(check("токен бота", bool(token), "REVIEW_BOT_TOKEN или ~/.config/ai-review/token"))
    else:
        res.append(check(f"review_identity: {identity}", identity == "self", "ожидается self или bot"))
    if store.initialized:
        cfg = store.config()
        res.append(check("reviewer_model в runs/config.json", bool(cfg.get("reviewer_model")),
                         "впишите id модели из opencode models"))
    if quick:
        return res
    if shutil.which("gh"):
        p = run(["gh", "auth", "status"], cwd=root, timeout=30)
        res.append(check("gh авторизован", p.returncode == 0, "gh auth login"))
    if shutil.which("claude"):
        p = run(["claude", "agents", "--json", "--cwd", str(root)], cwd=root, timeout=60)
        ok = p.returncode == 0 and p.stdout.strip().startswith("[")
        res.append(check("agent view (claude agents --json)", ok, "claude update; agent view ещё не раскатан?"))
    if store.initialized:
        base = store.config()["base_branch"]
        p = run(["git", "ls-remote", "--heads", "origin", base], cwd=root, timeout=30)
        res.append(check(f"ветка {base} на origin", p.returncode == 0 and bool(p.stdout.strip()),
                         f"git push origin {base} или поправьте base_branch в runs/config.json"))
    return res


def cmd_doctor(args, root: Path | None) -> int:
    if args.hook:
        if root is None or not Store(root).initialized:
            return 0
        bad = [m for ok, m in doctor_checks(root, quick=True) if not ok]
        if bad:
            print(json.dumps({"systemMessage": "aiflow: среда не готова\n" + "\n".join(bad)}, ensure_ascii=False))
        return 0
    results = doctor_checks(root, quick=args.quick)
    for _, m in results:
        print(m)
    return 0 if all(ok for ok, _ in results) else 1


def normalize_task(raw: dict) -> dict:
    tid = str(raw.get("id", "")).strip().lower()
    if not TASK_ID_RE.match(tid):
        raise AiflowError(f"некорректный id задачи: {tid!r} (латиница, цифры, дефисы)")
    kind = raw.get("kind") or "task"
    if kind not in ("task",) + TALK_KINDS:
        raise AiflowError(f"{tid}: kind должен быть task, discuss или ask")
    cx = raw.get("complexity") or None  # обычно не задаётся: оценивает планировщик
    if cx is not None and cx not in COMPLEXITIES:
        raise AiflowError(f"{tid}: complexity должен быть одним из {', '.join(COMPLEXITIES)}")
    text = str(raw.get("text") or "").strip()
    if not text:
        raise AiflowError(f"{tid}: пустой text")
    try:
        models = {f: check_model_field(f, raw.get(f)) for f in MODEL_FIELDS}
    except AiflowError as e:
        raise AiflowError(f"{tid}: {e}") from None
    try:
        items = max(1, int(raw.get("items") or 1))  # сколько независимых мелких пунктов в одной задаче
    except (TypeError, ValueError):
        raise AiflowError(f"{tid}: items должен быть числом пунктов задачи") from None
    return {
        "id": tid, "title": str(raw.get("title") or tid).strip(), "text": text, "complexity": cx, "kind": kind,
        "items": items,
        "depends_on": [str(d) for d in raw.get("depends_on") or []],
        "ui": raw.get("ui") or None,
        **models,
        "allowed_protected_paths": [],
        "state": "new", "pr": None, "sessions": {"planner": [], "worker": []},
        "usage": {}, "created_at": iso(), "state_at": iso(),
        "history": [{"at": iso(), "event": "created", "by": "intake"}],
    }


def dependency_cycle(tasks: list[dict]) -> list[str]:
    """Цикл в depends_on внутри пачки (с уже существующими задачами цикла быть не может: они на новые
    не ссылаются). Возвращает путь цикла или пустой список."""
    deps = {t["id"]: [d for d in t.get("depends_on") or []] for t in tasks}
    done: set[str] = set()

    def walk(node: str, path: list[str]) -> list[str]:
        if node in path:
            return path[path.index(node):] + [node]
        if node in done or node not in deps:
            return []
        for d in deps[node]:
            found = walk(d, path + [node])
            if found:
                return found
        done.add(node)
        return []

    for tid in deps:
        found = walk(tid, [])
        if found:
            return found
    return []


def cmd_enqueue(args, root: Path) -> int:
    store = Store(root)
    if not store.initialized:
        raise AiflowError("репозиторий не инициализирован: aiflow init")
    # Байты и явный UTF-8: на Windows sys.stdin декодирует в кодировке ANSI (cp1251),
    # и кириллица из heredoc превращается в мусор или роняет запись.
    blob = sys.stdin.buffer.read() if args.file in (None, "-") else Path(args.file).read_bytes()
    try:
        raw = blob.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise AiflowError(f"задачи должны быть в UTF-8: {e}") from None
    data = json.loads(raw)
    items = data if isinstance(data, list) else [data]
    tasks = [normalize_task(x) for x in items]
    ids = [t["id"] for t in tasks]
    if len(ids) != len(set(ids)):
        raise AiflowError("в пачке повторяются id задач")
    # Тот же lock, что у прохода монитора: проверка и запись идут одним куском,
    # параллельный enqueue или tick ждут, пока мы закончим.
    lock = FileLock(store.runs / "tick.lock", stale_minutes=15)
    if not lock.acquire(wait_seconds=120):
        raise AiflowError("очередь занята дольше 2 минут, повторите позже")
    try:
        existing = {t["id"]: t for t in store.tasks()}
        for t in tasks:
            if t["id"] in existing and existing[t["id"]].get("state") not in TERMINAL:
                raise AiflowError(f"задача {t['id']} уже есть в очереди")
            for d in t["depends_on"]:
                if d not in ids and d not in existing:
                    raise AiflowError(f"{t['id']}: неизвестная зависимость {d}")
                if d not in ids and existing[d].get("state") == "cancelled":
                    raise AiflowError(f"{t['id']}: зависимость {d} снята — она не выполнится")
        cycle = dependency_cycle(tasks)
        if cycle:
            raise AiflowError("зависимости замкнуты в цикл: " + " → ".join(cycle))
        for t in tasks:
            old = store.runs / t["id"]
            if old.exists():  # завершённая задача с тем же id уходит в архив вместе с её отчётами
                arch = store.runs / ".archive" / f"{t['id']}-{now().strftime('%Y%m%d-%H%M%S')}"
                arch.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old, arch)
            store.save_task(t)
            store.event("enqueued", t["id"], f"{t['id']}: поставлена в очередь"
                        + (f" ({t['complexity']})" if t.get("complexity") else ""))
    finally:
        lock.release()
    print(f"В очереди: {len(tasks)} — " + ", ".join(ids))
    if not FileLock(store.runs / "monitor.lock").owner():
        print("Монитор не запущен: в сессии оркестратора выполните /aiflow:resume.")
    return 0


def cmd_report(args, root: Path) -> int:
    details = Path(args.details_file).read_text(encoding="utf-8") if args.details_file else ""
    rep = write_report(root, args.id, args.role, args.status, args.pr, args.summary or "", details,
                       getattr(args, "complexity", None))
    print(f"Отчёт записан: {args.id} {rep['role']} {rep['status']}")
    return 0


def cmd_status(args, root: Path) -> int:
    store = Store(root)
    sessions = {}
    try:
        sessions = Claude(root).agents()
    except AiflowError:
        pass
    url = ""
    try:
        url = GH(root).repo_url()
    except AiflowError:
        pass
    state = build_state(store, sessions, url)
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        print_status(state)
    return 0


def run_tick_now(store: Store, wait: float = 60) -> list[dict]:
    lock = FileLock(store.runs / "tick.lock", stale_minutes=15)
    if not lock.acquire(wait_seconds=wait):
        raise TickBusy("tick уже выполняется")
    try:
        return Dispatcher(store).tick()
    finally:
        lock.release()


def queue_or_apply(store: Store, cmd: dict) -> None:
    store.command(cmd)
    if not FileLock(store.runs / "monitor.lock").owner():
        for ev in run_tick_now(store):
            print(ev["text"])


def cmd_pause(args, root: Path) -> int:
    queue_or_apply(Store(root), {"cmd": "pause", "reason": args.reason or ""})
    print("Пауза поставлена (новые сессии не запускаются, работающие продолжают).")
    return 0


def cmd_resume(args, root: Path) -> int:
    queue_or_apply(Store(root), {"cmd": "resume"})
    print("Очередь возобновлена.")
    return 0


def cmd_retry(args, root: Path) -> int:
    store = Store(root)
    if not store.task(args.id):
        raise AiflowError(f"нет задачи {args.id}")
    queue_or_apply(store, {"cmd": "retry", "id": args.id})
    print(f"Повтор {args.id} поставлен.")
    return 0


def cancel_tasks(store: Store, ids: list[str], *, close_pr: bool = True, note: str = "aiflow cancel") -> list[str]:
    """Снять задачи на любой стадии: остановить их сессии, закрыть PR (ветка остаётся),
    освободить очередь ревью. Возвращает предупреждения (что не удалось сделать)."""
    if not store.initialized:
        raise AiflowError("репозиторий не инициализирован: aiflow init")
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise AiflowError("не указаны задачи")
    lock = FileLock(store.runs / "tick.lock", stale_minutes=15)
    if not lock.acquire(wait_seconds=120):
        raise AiflowError("очередь занята дольше 2 минут, повторите позже")
    warnings: list[str] = []
    try:
        tasks = {t["id"]: t for t in store.tasks()}
        for tid in ids:
            t = tasks.get(tid)
            if not t:
                raise AiflowError(f"нет задачи {tid}")
            if t.get("state") in TERMINAL:
                raise AiflowError(f"{tid} уже {'смержена' if t['state'] == 'merged' else 'снята'}")
            if t.get("launching"):
                raise AiflowError(f"{tid}: сессия как раз запускается, повторите через минуту")
        for t in tasks.values():
            if t["id"] in ids or t.get("state") in TERMINAL:
                continue
            blocked = [d for d in t.get("depends_on") or [] if d in ids]
            if blocked:
                raise AiflowError(f"от {', '.join(blocked)} зависит {t['id']}: снимите её тоже или поставьте заново без зависимости")
        live: dict = {}
        if any(e.get("id") for tid in ids for lst in (tasks[tid].get("sessions") or {}).values() for e in lst):
            claude = Claude(store.root)
            live = claude.agents()
        for tid in ids:
            t = tasks[tid]
            for lst in (t.get("sessions") or {}).values():
                for e in lst:
                    s = live.get(e.get("id")) or {}
                    if s.get("state") in ("working", "blocked"):
                        if not claude.stop(e["id"]):
                            warnings.append(f"{tid}: не удалось остановить сессию {e['id']}")
            if close_pr and t.get("pr"):
                gh = GH(store.root)
                try:
                    state = gh.pr(t["pr"], "state").get("state")
                except AiflowError as ex:
                    state = None
                    warnings.append(f"{tid}: не удалось прочитать PR #{t['pr']}: {ex}")
                if state == "OPEN":
                    p = gh.call("pr", "close", str(t["pr"]), "--comment", "Задача снята в aiflow. Ветка оставлена.")
                    if p.returncode != 0:
                        warnings.append(f"{tid}: PR #{t['pr']} не закрыт: {(p.stderr or p.stdout).strip()[:200]}")
            prev = t.get("state")
            t.setdefault("history", []).append({"at": iso(), "from": prev, "to": "cancelled", "note": note})
            t["state"], t["state_at"], t["cleaned"] = "cancelled", iso(), True
            t.pop("retry_requested", None)
            store.save_task(t)
            store.event("cancelled", tid, f"{tid}: снята" + (f" (была в стадии «{STAGE_LABEL.get(prev, prev)}»)" if prev != "new" else ""))
        q = store.queue()
        if any(i in ids for i in q.get("review_locks") or []):
            q["review_locks"] = [i for i in q["review_locks"] if i not in ids]
            store.save_queue(q)
    finally:
        lock.release()
    return warnings


def cmd_cancel(args, root: Path) -> int:
    warnings = cancel_tasks(Store(root), args.ids, close_pr=not args.keep_pr)
    for w in warnings:
        print("Внимание: " + w)
    print("Сняты: " + ", ".join(dict.fromkeys(args.ids)))
    return 0


def allow_paths(store: Store, tid: str, paths: list[str]) -> list[str]:
    """Разрешить задаче менять защищённые пути (только человек). Если задача ждёт человека — повтор."""
    clean = []
    for raw in paths:
        g = str(raw).strip().strip("`'\"").replace("\\", "/")
        while g.startswith("./"):
            g = g[2:]
        if not g:
            continue
        if len(g) > 200 or g.startswith("/") or re.match(r"^[A-Za-z]:", g) or ".." in g.split("/"):
            raise AiflowError(f"некорректный путь: {raw} (нужен glob относительно корня репозитория)")
        if g in ("**", "*", "**/*"):
            raise AiflowError("разрешать все пути разом нельзя — укажите конкретную папку или файлы")
        clean.append(g)
    if not clean or len(clean) > 20:
        raise AiflowError("укажите от 1 до 20 путей")
    lock = FileLock(store.runs / "tick.lock", stale_minutes=15)
    if not lock.acquire(wait_seconds=120):
        raise AiflowError("очередь занята дольше 2 минут, повторите позже")
    try:
        t = store.task(tid)
        if not t:
            raise AiflowError(f"нет задачи {tid}")
        if t.get("state") in TERMINAL:
            raise AiflowError(f"задача {tid} уже завершена")
        allowed = t.setdefault("allowed_protected_paths", [])
        added = [g for g in clean if g not in allowed]
        allowed.extend(added)
        t.setdefault("history", []).append({"at": iso(), "from": t.get("state"), "to": t.get("state"),
                                            "note": "разрешены защищённые пути: " + ", ".join(clean)})
        if t.get("state") == "needs_human":
            t["retry_note"] = "человек разрешил защищённые пути: " + ", ".join(allowed)
        store.save_task(t)
        store.event("config", tid, f"{tid}: разрешены защищённые пути — " + ", ".join(clean))
        needs_retry = t.get("state") == "needs_human"
    finally:
        lock.release()
    if needs_retry:
        queue_or_apply(store, {"cmd": "retry", "id": tid})
    return added


def cmd_allow_path(args, root: Path) -> int:
    store = Store(root)
    allow_paths(store, args.id, args.paths)
    t = store.task(args.id) or {}
    print(f"{args.id}: разрешены {', '.join(t.get('allowed_protected_paths') or [])}"
          + (" — задача продолжит работу." if t.get("state") != "needs_human" or t.get("retry_requested") else ""))
    return 0


def cmd_amend(args, root: Path) -> int:
    store = Store(root)
    t = store.task(args.id)
    if not t:
        raise AiflowError(f"задача {args.id} не найдена")
    if t.get("state") in TERMINAL:
        raise AiflowError(f"задача {args.id} уже завершена ({STAGE_LABEL.get(t['state'], t['state'])}) — поставьте новую")
    if args.file:
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
    else:
        text = " ".join(args.text or [])
    if not text.strip():
        raise AiflowError("пустое дополнение: передайте текст аргументом или --file")
    queue_or_apply(store, {"cmd": "amend", "id": args.id, "text": text, "source": "cli"})
    print(f"{args.id}: дополнение принято — его получит "
          + ("сессия вопроса или обсуждения" if t.get("state") in ("discussing", "answered") else
             "планировщик" if t.get("state") in ("new", "planning", "plan_review") and not t.get("plan_approved")
             else "исполнитель") + ".")
    return 0


def set_models(store: Store, tid: str, changes: dict) -> tuple[dict, list[str]]:
    """Задать задаче модель и effort планировщика и исполнителя (None — вернуть настройки проекта).
    Действует на сессии, которые ещё не запущены. → (итоговые настройки, предупреждения)."""
    clean = {f: check_model_field(f, v) for f, v in changes.items() if f in MODEL_FIELDS}
    if not clean:
        raise AiflowError("укажите, что менять: --planner, --planner-effort, --worker или --worker-effort")
    lock = FileLock(store.runs / "tick.lock", stale_minutes=15)
    if not lock.acquire(wait_seconds=120):
        raise AiflowError("очередь занята дольше 2 минут, повторите позже")
    try:
        t = store.task(tid)
        if not t:
            raise AiflowError(f"нет задачи {tid}")
        if t.get("state") in TERMINAL:
            raise AiflowError(f"задача {tid} уже завершена")
        notes = []
        started = {role: bool((t.get("sessions") or {}).get(role)) for role in ("planner", "worker")}
        for field, value in clean.items():
            role = field.split("_")[0]
            if value is None:
                t.pop(field, None)
            else:
                t[field] = value
            if started[role] and not (role == "planner" and t.get("state") == "new"):
                who = "планировщик" if role == "planner" else "исполнитель"
                notes.append(f"{who} этой задачи уже запущен: новая настройка подействует только на следующую его сессию")
        t.setdefault("history", []).append({"at": iso(), "from": t.get("state"), "to": t.get("state"),
                                            "note": "модели: " + ", ".join(f"{f}={v or 'по умолчанию'}" for f, v in clean.items())})
        store.save_task(t)
        store.event("config", tid, f"{tid}: " + ", ".join(f"{f} = {v or 'по умолчанию'}" for f, v in clean.items()))
        return {f: t.get(f) for f in MODEL_FIELDS}, list(dict.fromkeys(notes))
    finally:
        lock.release()


def cmd_model(args, root: Path) -> int:
    changes = {f: v for f, v in (("planner_model", args.planner), ("planner_effort", args.planner_effort),
                                 ("worker_model", args.worker), ("worker_effort", args.worker_effort)) if v is not None}
    now_set, notes = set_models(Store(root), args.id, changes)
    shown = ", ".join(f"{f} = {v}" for f, v in now_set.items() if v) or "всё по настройкам проекта"
    print(f"{args.id}: {shown}")
    for n in notes:
        print("Внимание: " + n)
    return 0


def cmd_approve(args, root: Path) -> int:
    store = Store(root)
    t = store.task(args.id)
    if not t or not t.get("pr"):
        raise AiflowError(f"у задачи {args.id} нет PR с планом")
    GH(root).add_label(t["pr"], store.config()["labels"]["approved"])
    print(f"План {args.id} одобрен (PR #{t['pr']}).")
    return 0


def gh_repo(cwd, env=None) -> str:
    """owner/name репозитория по версии GitHub. После переименования или переноса remote ещё
    указывает на старый адрес: gh подставляет его в {owner}/{repo}, а запросы с методом
    (POST/PATCH) редирект 307 не проходят. gh repo view редирект проходит и даёт новый адрес."""
    p = gh_with_retry(lambda: run([need("gh"), "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                                  cwd=cwd, env=env, timeout=60), ["repo", "view"])
    name = (p.stdout or "").strip()
    return name if p.returncode == 0 and re.fullmatch(r"[\w.-]+/[\w.-]+", name) else "{owner}/{repo}"


def cmd_reply(args, root: Path) -> int:
    body = Path(args.body_file).read_text(encoding="utf-8")
    p = run([need("gh"), "api", "--method", "POST",
             f"repos/{gh_repo(Path.cwd())}/pulls/{args.pr}/comments/{args.comment_id}/replies",
             "-f", f"body={body}"], cwd=Path.cwd(), timeout=60)
    if p.returncode != 0:
        raise AiflowError("ответ в тред не отправлен: " + (p.stderr or p.stdout).strip()[:300])
    print(f"Ответ отправлен в тред {args.comment_id}.")
    return 0


def cmd_tick(args, root: Path) -> int:
    for ev in run_tick_now(Store(root)):
        print(f"[aiflow] {ev['text']}")
    return 0


def _toast(title: str, text: str) -> None:
    """Системное уведомление; молча ничего не делает, если показать нечем."""
    env = dict(os.environ, AIFLOW_TOAST_TITLE=title, AIFLOW_TOAST_TEXT=text)
    flags = 0
    if os.name == "nt":
        exe = shutil.which("powershell.exe") or shutil.which("powershell")  # WinRT есть только в Windows PowerShell 5.1
        if not exe:
            return
        script = (
            "$ErrorActionPreference='Stop';"
            "[void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime];"
            "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$t=$x.GetElementsByTagName('text');"
            "[void]$t.Item(0).AppendChild($x.CreateTextNode($env:AIFLOW_TOAST_TITLE));"
            "[void]$t.Item(1).AppendChild($x.CreateTextNode($env:AIFLOW_TOAST_TEXT));"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
            "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
            ".Show([Windows.UI.Notifications.ToastNotification]::new($x))"
        )
        cmd = [exe, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    elif sys.platform == "darwin":
        cmd = ["osascript", "-e", 'display notification (system attribute "AIFLOW_TOAST_TEXT") '
                                  'with title (system attribute "AIFLOW_TOAST_TITLE")']
    else:
        exe = shutil.which("notify-send")
        if not exe:
            return
        cmd = [exe, title, text]
    try:
        subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError:
        pass


def desktop_notify(cfg: dict, events: list[dict], project: str | None = None) -> None:
    """Системные уведомления о событиях, которые ждут пользователя (desktop_notify в конфиге)."""
    if not cfg.get("desktop_notify", True):
        return
    title = f"aiflow · {project}" if project else "aiflow"
    important = [ev for ev in events if ev.get("kind") in NOTIFY_KINDS - NO_TOAST_KINDS]
    if len(important) > 3:
        _toast(title, f"{len(important)} событий, подробности в панели")
        return
    for ev in important:
        _toast(title, ev.get("text", ""))


def cmd_run(args, root: Path) -> int:
    """Монитор плагина (`--events`): поднять службу и пересказывать события проекта.
    Без `--events` — служба на переднем плане (для отладки)."""
    import aiflow_service as svc
    store = Store(root)
    if not store.initialized:
        print("[aiflow] репозиторий не инициализирован: выполните aiflow init", flush=True)
        return 1
    if args.events:
        return svc.relay_events(store)
    svc.register(root)
    return svc.serve()


def cmd_service(args, root: Path | None) -> int:
    import aiflow_service as svc
    if args.spawn:
        svc.spawn(via_helper=False)
        return 0
    return svc.serve()


def cmd_up(args, root: Path | None) -> int:
    import aiflow_service as svc
    if root is not None and Store(root).initialized:
        if svc.register(root):
            print(f"Проект {root.name} добавлен в службу.")
    elif root is not None:
        print(f"{root.name} не инициализирован (aiflow init) — в службу не добавлен.")
    if args.autostart is not None:
        where = svc.autostart(args.autostart)
        print(("Автозапуск при входе в систему включён: " if args.autostart else "Автозапуск выключен: ") + where)
    if args.foreground:
        return svc.serve()
    data = svc.ensure()
    url = svc.panel_url(data)
    print(f"Служба aiflow {data.get('version', '?')} работает (pid {data['pid']}); проектов: {len(svc.projects())}"
          + (f"; панель: {url}" if url else ""))
    if args.open and url:
        svc.open_window(url)
    return 0


def cmd_down(args, root: Path | None) -> int:
    import aiflow_service as svc
    if args.no_autostart:
        print("Автозапуск выключен: " + svc.autostart(False))
    if svc.stop():
        print("Служба aiflow остановлена. Фоновые сессии Claude продолжают работать; "
              "продолжить очередь — aiflow up или /aiflow:resume.")
    else:
        print("Служба aiflow не запущена.")
    return 0


def cmd_projects(args, root: Path | None) -> int:
    import aiflow_service as svc
    data = svc.info()
    print(f"Служба: {'работает, pid ' + str(data['pid']) + ', версия ' + str(data.get('version', '?')) if data else 'не запущена'}"
          f"; автозапуск: {'включён' if svc.autostart_enabled() else 'выключен'}"
          + (f"; панель: {svc.panel_url(data)}" if data and svc.panel_url(data) else ""))
    roots = svc.projects()
    if not roots:
        print("Проектов нет: aiflow up в каталоге проекта.")
    for r in roots:
        st = Store(Path(r))
        if not st.initialized:
            note = "нет runs/config.json"
        else:
            tasks = st.tasks()
            live = sum(1 for t in tasks if t.get("state") not in TERMINAL)
            note = f"задач в работе: {live}" + (", пауза" if st.queue().get("paused") else "")
        print(f"  {Path(r).name:<24} {note:<28} {r}")
    return 0


def cmd_forget(args, root: Path | None) -> int:
    import aiflow_service as svc
    target = Path(args.path).resolve() if args.path else root
    if target is None:
        raise AiflowError("укажите путь проекта или запустите внутри него")
    print(f"{target.name} убран из службы." if svc.forget(target) else f"{target} не был в службе.")
    return 0


def _port_busy(port: int) -> bool:
    import socket
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _kill(pid: int) -> bool:
    if not pid_alive(pid):
        return False
    try:
        if os.name == "nt":
            run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=30)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except (OSError, AiflowError):
        return False
    for _ in range(20):
        if not pid_alive(pid):
            return True
        time.sleep(0.25)
    return not pid_alive(pid)


def stop_ui(store: Store) -> bool:
    info = read_json(store.runs / "ui.pid", {}) or {}
    ok = _kill(owner_alive(info))
    (store.runs / "ui.pid").unlink(missing_ok=True)
    return ok


def cmd_ui(args, root: Path) -> int:
    import aiflow_service as svc
    store = Store(root)
    if args.stop:
        print("Панель остановлена." if stop_ui(store) else
              "Отдельно запущенной панели нет. Общая панель останавливается вместе со службой: aiflow down.")
        return 0
    if not args.port and (args.detach or svc.info()):
        if store.initialized:
            svc.register(root)
        data = svc.ensure()
        url = svc.panel_url(data)
        if not url:
            raise AiflowError("панель службы недоступна: " + (data.get("panel_error")
                              or "выключена (ui_port: 0 в ~/.aiflow/config.json)"))
        where = svc.open_window(url) if args.open else ""
        print(f"Панель aiflow: {url}" + (f" (открыта: {where})" if where else ""))
        return 0
    # отдельная панель только этого проекта на переднем плане (служба не запущена)
    port = args.port or int(store.config().get("ui_port") or 8765)
    if _port_busy(port):
        raise AiflowError(f"порт {port} занят")
    from aiflow_ui import start_server
    srv, url = start_server(lambda: [store], port, threading.Event())
    write_json(store.runs / "ui.pid", {"pid": os.getpid(), "port": port, "at": iso()})
    print(f"Панель aiflow: {url}  (остановить: Ctrl+C или aiflow ui --stop)", flush=True)
    if args.open:
        svc.open_window(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()
    finally:
        (store.runs / "ui.pid").unlink(missing_ok=True)
    return 0


def cmd_stop(args, root: Path) -> int:
    import aiflow_service as svc
    store = Store(root)
    stopped = []
    if svc.stop():
        stopped.append("служба (все проекты)")
    pid = FileLock(store.runs / "monitor.lock").owner()
    if pid and _kill(pid):  # монитор сессии старой версии
        (store.runs / "monitor.lock").unlink(missing_ok=True)
        stopped.append("монитор")
    if stop_ui(store):
        stopped.append("панель")
    if stopped:
        store.event("stopped", None, "монитор остановлен командой aiflow stop")
        print("Остановлено: " + ", ".join(stopped) + ". Фоновые сессии Claude продолжают работать; "
              "очередь продолжит aiflow up или /aiflow:resume.")
    else:
        print("Служба и панель не запущены.")
    return 0


def cmd_statusline(args, root: Path | None) -> int:
    if root is None:
        return 0
    store = Store(root)
    if not store.initialized:
        return 0
    tasks = store.tasks()
    q = store.queue()
    working = sum(1 for t in tasks if t.get("state") in ("planning", "in_progress", "in_review"))
    waiting = sum(1 for t in tasks if t.get("state") == "needs_human"
                  or (t.get("state") == "plan_review" and not t.get("plan_approved")))
    queued = sum(1 for t in tasks if t.get("state") in ("new", "approved", "ready"))
    parts = []
    if working:
        parts.append(f"{working} в работе")
    if queued:
        parts.append(f"{queued} в очереди")
    if waiting:
        parts.append(f"{waiting} ждут вас")
    if q.get("paused"):
        parts.append("пауза")
    if not FileLock(store.runs / "monitor.lock").owner() and any(t.get("state") not in TERMINAL for t in tasks):
        parts.append("монитор остановлен")
    if parts:
        print("aiflow: " + ", ".join(parts))
    return 0


def cmd_review(args, root: Path) -> int:
    import review
    return review.main(["--pr", str(args.pr)])


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="aiflow", description="Очередь задач для фоновых сессий Claude Code")
    ap.add_argument("--version", action="version", version=f"aiflow {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="подготовить репозиторий")
    d = sub.add_parser("doctor", help="проверить среду")
    d.add_argument("--quick", action="store_true")
    d.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)
    e = sub.add_parser("enqueue", help="поставить задачи в очередь (JSON со stdin)")
    e.add_argument("file", nargs="?")
    r = sub.add_parser("report", help="отчёт сессии")
    r.add_argument("id")
    r.add_argument("--role", required=True, choices=["planner", "worker"])
    r.add_argument("--status", required=True,
                   choices=["plan_ready", "delegate", "self_execute", "answered", "ready_for_review", "done", "approved", "limit_reached", "error", "blocked"])
    r.add_argument("--pr", type=int)
    r.add_argument("--complexity", choices=["trivial", "simple", "complex"],
                   help="оценка реализации (планировщик): trivial — только с delegate")
    r.add_argument("--summary")
    r.add_argument("--details-file")
    s = sub.add_parser("status", help="состояние очереди")
    s.add_argument("--json", action="store_true")
    sub.add_parser("tick", help="один проход")
    rn = sub.add_parser("run", help="монитор плагина: поднять службу и пересказывать события проекта")
    rn.add_argument("--events", action="store_true")
    rn.add_argument("--detach", action="store_true", help=argparse.SUPPRESS)  # совместимость: служба всегда отдельно
    sv = sub.add_parser("service", help=argparse.SUPPRESS)
    sv.add_argument("--spawn", action="store_true")
    up = sub.add_parser("up", help="запустить службу (все проекты и панель), добавить в неё текущий проект")
    up.add_argument("--open", action="store_true", help="открыть панель")
    up.add_argument("--foreground", action="store_true", help="служба в этом терминале")
    up.add_argument("--autostart", action="store_true", default=None, help="запускать службу при входе в систему")
    up.add_argument("--no-autostart", dest="autostart", action="store_false", help="выключить автозапуск")
    dn = sub.add_parser("down", help="остановить службу")
    dn.add_argument("--no-autostart", action="store_true", help="и выключить автозапуск")
    sub.add_parser("projects", help="проекты службы")
    fg = sub.add_parser("forget", help="убрать проект из службы")
    fg.add_argument("path", nargs="?")
    p = sub.add_parser("pause")
    p.add_argument("reason", nargs="?")
    sub.add_parser("resume")
    rt = sub.add_parser("retry")
    rt.add_argument("id")
    al = sub.add_parser("allow-path", help="разрешить задаче менять защищённые пути (только человек)")
    al.add_argument("id")
    al.add_argument("paths", nargs="+", help="glob относительно корня, например 'src/*/Migrations/**'")
    am = sub.add_parser("amend", help="дополнить идущую задачу: правку получит её текущая сессия")
    am.add_argument("id")
    am.add_argument("text", nargs="*")
    am.add_argument("--file", help="файл с текстом дополнения; - — stdin")
    md = sub.add_parser("model", help="задать задаче модель и effort планировщика и исполнителя")
    md.add_argument("id")
    md.add_argument("--planner", help="модель планировщика: fable, opus, sonnet, haiku или полный id; default — как в проекте")
    md.add_argument("--planner-effort", help="effort планировщика: " + ", ".join(EFFORTS))
    md.add_argument("--worker", help="модель исполнителя")
    md.add_argument("--worker-effort", help="effort исполнителя")
    cn = sub.add_parser("cancel", help="снять задачи: остановить их сессии и закрыть PR")
    cn.add_argument("ids", nargs="+")
    cn.add_argument("--keep-pr", action="store_true", help="не закрывать PR")
    apv = sub.add_parser("approve", help="одобрить план (метка plan-approved)")
    apv.add_argument("id")
    rp = sub.add_parser("reply", help="ответить в тред ревью")
    rp.add_argument("--pr", type=int, required=True)
    rp.add_argument("--comment-id", required=True)
    rp.add_argument("--body-file", required=True)
    rv = sub.add_parser("review", help="один раунд ревью PR")
    rv.add_argument("--pr", type=int, required=True)
    u = sub.add_parser("ui", help="панель в браузере")
    u.add_argument("--port", type=int)
    u.add_argument("--open", action="store_true")
    u.add_argument("--detach", action="store_true")
    u.add_argument("--stop", action="store_true", help="остановить отдельно запущенную панель")
    sub.add_parser("stop", help="остановить службу и панель (сессии Claude продолжают работать)")
    sub.add_parser("statusline", help="одна строка для statusLine")
    args = ap.parse_args(argv)

    try:
        try:
            root = repo_root()
        except AiflowError:
            root = None
        rootless = {"doctor": cmd_doctor, "statusline": cmd_statusline, "service": cmd_service, "up": cmd_up,
                    "down": cmd_down, "projects": cmd_projects, "forget": cmd_forget}
        if args.cmd in rootless:
            return rootless[args.cmd](args, root)
        if root is None:
            raise AiflowError("запустите aiflow внутри git-репозитория")
        handlers = {
            "init": cmd_init, "enqueue": cmd_enqueue, "report": cmd_report, "status": cmd_status,
            "tick": cmd_tick, "run": cmd_run, "pause": cmd_pause, "resume": cmd_resume,
            "retry": cmd_retry, "cancel": cmd_cancel, "allow-path": cmd_allow_path, "model": cmd_model, "approve": cmd_approve, "amend": cmd_amend, "reply": cmd_reply, "review": cmd_review,
            "ui": cmd_ui, "stop": cmd_stop,
        }
        return handlers[args.cmd](args, root)
    except AiflowError as e:
        print(f"aiflow: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
