"""aiflow — очередь задач для фоновых сессий Claude Code.

Состояние живёт в <repo>/.aiflow/, сессии хостит supervisor Claude Code,
aiflow только решает, когда и кого запустить, и записывает результат.
Только стандартная библиотека Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# Всё, что определено в модулях aiflow_*, доступно и как aiflow.<имя>: так к нему обращаются review.py,
# служба, панель и тесты. Поэтому здесь импортируется всё, а не только то, что нужно командам.
from aiflow_core import (
    AMEND_MARKER, AiflowError, BOT_LOGINS, CI_FAIL, CI_PASS, COMPLEXITIES, DEFAULT_CONFIG, EFFORTS, ENTRY, FINISHED,
    GH_NET_ANY, GH_NET_SAFE, GH_RETRY_DELAYS, HERE, HISTORY_KEEP, HUMAN_CODES, HUMAN_CODE_WORDS, HUMAN_MARKER,
    IMPLEMENTING, LIMIT_HITS_MAX, LIMIT_LOG_TAIL, LIMIT_RE, MANUAL_PAUSES, MARKERS, MODEL_FIELDS, MODEL_NAME_RE,
    NOTIFY_KINDS, NO_TOAST_KINDS, NO_WINDOW, PATH_ASK_RE, PLANNER_PLAN_MD, PLANNER_TALK_MD, PLUGIN_ROOT,
    REVIEW_ACTIONS, SIDE_STATES, STAGES, STAGE_LABEL, STATION, TALK_KINDS, TASK_ID_RE, TEMPLATES, TERMINAL, TickBusy,
    VERSION, WHAT_TO_DO, WORKER_MD, WORKER_STAGES, _BOOT, _plugin_version, after, age_minutes, aiflow_home,
    boot_time, by_complexity, check_model_field, ci_summary, ci_text, context_choice, gh_is_read, gh_with_retry,
    glob_to_re, inbox_dirs, is_direct, is_human_comment, iso, kill_tree, need, notify_level, notify_status, now,
    owner_alive, parse_iso, path_matches, pid_alive, planner_choice, process_start, read_json, repo_root, run,
    run_tree, short_reason, should_notify, unknown_model_note, visited_stations, wanted_paths, worker_choice,
    write_json,
)
from aiflow_store import (
    FileLock, LEGACY_STATE_DIR, STATE_DIR, Store, migrate_state_dir, stale_settings, state_dir, sync_queue,
    write_report,
)
from aiflow_tools import (
    Claude, GH, PLUGIN_BIN_RE, PR_FIELDS_QUERY, claude_env, gh_repo, pr_from_graphql, settled_session,
)
from aiflow_usage import (
    CACHE_WRITE_1H, CACHE_WRITE_5M, PRICES, TOKEN_KINDS, _file_msgs, _usage_cache, add_usage,
    collect_coordinator_usage, coordinator_sessions, coordinator_state, done_note, last_words, model_price,
    price_table, priced, reviewer_runs, reviewer_usage, task_tokens, task_usage, track_coordinator,
    transcript_age_minutes, transcript_usage, usage_cost,
)
from aiflow_prompts import (
    EXPLORERS, RESUME_FINISH, SEARCH_HEAD, STAGE_ROLE, amend_list, amend_prompt, ask_prompt, ci_fix_prompt, dep_task,
    deps_note, direct_prompt, discuss_prompt, explorer_mode, launch_amendments, mark_delivered, merge_workflows,
    neighbours, neighbours_note, plan_check_prompt, planner_prompt, resume_prompt, review_prompt,
    review_result_prompt, revise_prompt, search_rule, set_after, split_dep, split_note, waiting_deps,
    with_search_rule, worker_prompt,
)
from aiflow_dispatcher import (
    Dispatcher, _BASE_SEEN, spawn_plan_check_process, spawn_review_process,
)
from aiflow_view import (
    STATS_DAYS, STATS_STAGES, board_position, build_state, build_stats, fmt_hours, fmt_tokens, fmt_when, human_code,
    median, percentile, plan_check_activity, print_stats, print_status, review_activity, stage_hours, task_updated_at,
)

ORCHESTRATOR_NAME = "aiflow"


# Меняется вместе с правилами координатора (agents/orchestrator.md, приветствие): сессия, запущенная
# по прежним правилам, заменяется новой при следующем «Дать задачу».
ORCHESTRATOR_REV = 3


def orchestrator_greeting(name: str) -> str:
    return f"Готов. Напишите, что сделать в проекте {name}, — поставлю задачу в очередь."


def orchestrator_prompt(name: str) -> str:
    return (
        f"You are the task chat of the project {name}. The user opens you to give tasks and does not type "
        "/aiflow:intake: every message of the user is a task for the queue, unless it is a question about the "
        "queue or a reply to your question. Before you queue, run `aiflow status --intake-rules` and obey what it prints "
        "(these are the rules of /aiflow:intake; the user's message is the task text). "
        f"Now reply with exactly this one line and end your turn: {orchestrator_greeting(name)}"
    )


def open_orchestrator(store: "Store", claude=None) -> tuple[str, bool]:
    """Чат координатора проекта, готовый принимать задачи: уже запущенный отсюда или новый.
    → (id, запущена ли новая). Сессию узнаём по записи .aiflow/orchestrator_chat.json, а не по имени: имя
    Claude Code меняет сам. Сессия, где пользователь когда-то набрал /aiflow:resume, не годится: это
    обычный Claude без правил координатора, и задачи в нём нужно давать командой."""
    claude = claude or Claude(store.root)
    sessions = claude.agents()
    mark_path = store.runs / "orchestrator_chat.json"
    mark = read_json(mark_path, {}) or {}
    mine = next((sid for sid, s in sessions.items() if mark.get("session_id") and s.get("sessionId") == mark["session_id"]
                 and s.get("state") != "stopped"), None)
    if mine and mark.get("rev") == ORCHESTRATOR_REV:
        track_coordinator(store, mark["session_id"], mine)
        return mine, False
    root = str(store.root).replace("\\", "/").rstrip("/").lower()
    for sid, s in sessions.items():  # прежние чаты координатора: два чата с одним именем только путают
        old = sid == mine or (s.get("name") == ORCHESTRATOR_NAME
                              and str(s.get("cwd") or "").replace("\\", "/").rstrip("/").lower() in ("", root))
        if old:
            track_coordinator(store, s.get("sessionId"), sid)  # расход прежних чатов не теряется
        if old and s.get("state") not in ("stopped", "working"):
            claude.stop(sid)
    sid, out = claude.launch("aiflow:orchestrator", ORCHESTRATOR_NAME, orchestrator_prompt(store.root.name),
                             mode=store.config().get("permission_mode"))
    if not sid:
        raise AiflowError("не удалось запустить сессию координатора: " + out[-200:])
    session_id = (claude.agents().get(sid) or {}).get("sessionId")
    write_json(mark_path, {"sid": sid, "session_id": session_id, "rev": ORCHESTRATOR_REV, "at": iso()})
    track_coordinator(store, session_id, sid)
    return sid, True


def cmd_intake_rules(args, root: Path | None) -> int:
    """Правила приёма задач самой новой установленной версии плагина — для чата координатора."""
    import aiflow_service as svc
    path = svc.newest_install()[1].parent.parent / "skills" / "intake" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)  # без шапки навыка
    text = text.replace("$ARGUMENTS", "(the user's last message in this chat)")
    sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


def cmd_stats(args, root: Path) -> int:
    store = Store(root)
    if not store.initialized:
        raise AiflowError("репозиторий не инициализирован: aiflow init")
    stats = build_stats(store, max(0, int(args.days)))
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        print_stats(store.root.name, stats)
    return 0


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
        created.append(".aiflow/config.json")
    else:
        added = store.migrate_config()
        if added:
            created.append(".aiflow/config.json (+" + ", ".join(added) + ")")
    gi = root / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    add = [x for x in (store.runs.name + "/", ".claude/worktrees/") if x not in lines]
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
          "  1. Впишите модель ревьюера в .aiflow/config.json → reviewer_model (opencode models deepseek).\n"
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
         "runtimeArgs": [str(ENTRY), "up", "--foreground"],
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
    res.append(check(".aiflow/config.json", store.initialized, "выполните aiflow init"))
    identity = store.config().get("review_identity") or "self"
    if identity == "bot":
        token = os.environ.get("REVIEW_BOT_TOKEN") or (Path.home() / ".config" / "ai-review" / "token").is_file()
        res.append(check("токен бота", bool(token), "REVIEW_BOT_TOKEN или ~/.config/ai-review/token"))
    else:
        res.append(check(f"review_identity: {identity}", identity == "self", "ожидается self или bot"))
    if store.initialized:
        cfg = store.config()
        res.append(check("reviewer_model в .aiflow/config.json", bool(cfg.get("reviewer_model")),
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
                         f"git push origin {base} или поправьте base_branch в .aiflow/config.json"))
        # не ошибки среды: настройки из прежних версий, которые стоит пересмотреть
        res += [(True, "• .aiflow/config.json: " + text) for text in store.advice().values()]
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
    home_root, project, then = root, getattr(args, "project", None), getattr(args, "then", None)
    split_of = getattr(args, "split_of", None)
    if split_of and project and project != root.name:
        raise AiflowError("--split-of: the parts stay in the project of the task that is split, do not give --project")
    if project and project != root.name:
        others = neighbours(root)
        if project not in others:
            raise AiflowError(f"no project {project} in the aiflow service. Projects: "
                              + (", ".join(sorted(others)) or "none besides this one"))
        root = others[project]
    else:
        project = None
    if then and not Store(home_root).task(then):
        raise AiflowError(f"--then: нет задачи {then}")
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
    if len(tasks) > 1 and not getattr(args, "separate", False) and not split_of:
        # Сессия координатора живёт днями и ставит задачи по памяти о старых правилах: правило «одно
        # сообщение — одна задача» держит сама команда, а не только текст инструкции.
        raise AiflowError(
            f"{len(tasks)} tasks in one call, nothing is queued. One user message is one task: put small unrelated "
            "things into ONE task and write its text as a numbered list (one PR, one planner session). "
            f"The current rules: {PLUGIN_ROOT / 'skills' / 'intake' / 'SKILL.md'} (step 3) - read them again, "
            "they change with plugin updates. Repeat with --separate only if the user asked for separate tasks, "
            "or each task is a feature of its own, or the tasks depend on each other (depends_on).")
    # Тот же lock, что у прохода монитора: проверка и запись идут одним куском,
    # параллельный enqueue или tick ждут, пока мы закончим.
    lock = FileLock(store.runs / "tick.lock", stale_minutes=15)
    if not lock.acquire(wait_seconds=120):
        raise AiflowError("очередь занята дольше 2 минут, повторите позже")
    try:
        existing = {t["id"]: t for t in store.tasks()}
        parent = existing.get(split_of) if split_of else None
        if split_of:
            if not parent or parent.get("state") in TERMINAL:
                raise AiflowError(f"--split-of: no task {split_of} in work, nothing is queued")
            for t in tasks:  # часть — та же задача пользователя: дизайн и выбор моделей переходят к ней
                t["split_of"] = split_of
                for f in ("ui",) + MODEL_FIELDS:
                    if not t.get(f) and parent.get(f):
                        t[f] = parent[f]
        for t in tasks:
            if t["id"] in existing and existing[t["id"]].get("state") not in TERMINAL:
                raise AiflowError(f"задача {t['id']} уже есть в очереди")
            for d in t["depends_on"]:
                if split_dep(d)[0]:
                    other = dep_task(store, d)[1]
                    if not other or other.get("state") == "cancelled":
                        raise AiflowError(f"{t['id']}: зависимости {d} нет или она снята")
                    continue
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
        if parent:
            parent["split_into"] = list(dict.fromkeys((parent.get("split_into") or []) + ids))
            first_title = str(getattr(args, "title", None) or "").strip()
            if first_title:  # задача стала первой частью: прежнее название описывает всю работу
                parent["title"] = first_title
            parent.setdefault("history", []).append({"at": iso(), "event": "split", "into": ids})
            store.save_task(parent)
            store.event("split", split_of, f"{split_of}: задача разбита — она остаётся первой частью, "
                        "остальные части: " + ", ".join(ids))
    finally:
        lock.release()
    print(f"В очереди{' проекта ' + project if project else ''}: {len(tasks)} — " + ", ".join(ids))
    if then:
        for i in ids:
            set_after(Store(home_root), then, f"{project}/{i}" if project else i)
        print(f"{then} ждёт: " + ", ".join(f"{project}/{i}" if project else i for i in ids))
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
    if getattr(args, "intake_rules", False):
        # под командой status: она уже разрешена фоновым сессиям во всех подготовленных проектах
        return cmd_intake_rules(args, root)
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
        t.pop("path_asked", None)
        t.pop("path_request", None)
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
    else:
        queue_or_apply(store, {"cmd": "paths_allowed", "id": tid, "paths": clean})
    return added


def cmd_after(args, root: Path) -> int:
    set_after(Store(root), args.id, args.dep, remove=args.remove)
    print(f"{args.id}: больше не ждёт {args.dep}" if args.remove else
          f"{args.id} начнётся после {args.dep}"
          + (" (после мержа и публикации)" if split_dep(args.dep)[0] else " (после мержа)"))
    return 0


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
             "планировщик" if t.get("state") in ("new", "planning", "plan_check", "plan_review") and not t.get("plan_approved")
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


def toast_xml(title: str, text: str, link: str | None, actions: list[tuple[str, str]] | None) -> str:
    """Уведомление Windows: клик по нему и каждая кнопка открывают свой адрес (activationType=protocol),
    поэтому процесс, показавший уведомление, ждать клика не должен."""
    from xml.sax.saxutils import escape, quoteattr
    head = f'<toast activationType="protocol" launch={quoteattr(link)}>' if link else "<toast>"
    buttons = "".join(f'<action content={quoteattr(label)} activationType="protocol" arguments={quoteattr(url)}/>'
                      for label, url in (actions or [])[:5])  # Windows показывает не больше пяти кнопок
    return (f'{head}<visual><binding template="ToastGeneric"><text>{escape(title)}</text><text>{escape(text)}</text>'
            f'</binding></visual>{"<actions>" + buttons + "</actions>" if buttons else ""}</toast>')


def _toast(title: str, text: str, link: str | None = None, actions: list[tuple[str, str]] | None = None) -> None:
    """Системное уведомление; молча ничего не делает, если показать нечем.
    link — адрес по клику на уведомление, actions — кнопки (подпись, адрес); и то и другое только в Windows."""
    env = dict(os.environ, AIFLOW_TOAST_TITLE=title, AIFLOW_TOAST_TEXT=text,
               AIFLOW_TOAST_XML=toast_xml(title, text, link, actions))
    flags = 0
    if os.name == "nt":
        exe = shutil.which("powershell.exe") or shutil.which("powershell")  # WinRT есть только в Windows PowerShell 5.1
        if not exe:
            return
        script = (
            "$ErrorActionPreference='Stop';"
            "[void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime];"
            "[void][Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime];"
            "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
            "$x.LoadXml($env:AIFLOW_TOAST_XML);"
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


def toast_link(panel: dict | None, ev: dict | None = None) -> str | None:
    """Адрес по клику на само уведомление: панель. panel = {url, project, token, repo_url, prs}."""
    if not panel or not panel.get("url"):
        return None
    return str(panel["url"]).rstrip("/") + "/"


def toast_actions(panel: dict | None, ev: dict | None) -> list[tuple[str, str]]:
    """Кнопки уведомления о задаче — те же, что у неё в панели: одобрить план или повторить, открыть сессию,
    открыть PR. Действие выполняет страница панели по адресу с #…; одобрение и повтор несут токен панели:
    без него посторонняя страница могла бы одобрить план, просто открыв такой адрес."""
    base = toast_link(panel)
    if not base or not ev or not ev.get("task"):
        return []
    from urllib.parse import quote
    tid, kind = str(ev["task"]), ev.get("kind")
    tail = f"={quote(tid, safe='')}&project={quote(str(panel.get('project') or ''), safe='')}"
    key = f"&k={quote(str(panel.get('token') or ''), safe='')}"
    out = []
    if kind in ("plan_ready", "plan_updated") and panel.get("token"):
        out.append(("Одобрить план", f"{base}#approve{tail}{key}"))
    if kind == "needs_human" and panel.get("token"):
        out.append(("Повторить", f"{base}#retry{tail}{key}"))
    out.append(("Открыть чат", f"{base}#open{tail}"))
    pr = (panel.get("prs") or {}).get(tid)
    if pr and panel.get("repo_url"):
        out.append(("Открыть PR", f"{str(panel['repo_url']).rstrip('/')}/pull/{int(pr)}"))
    return out


def desktop_notify(cfg: dict, events: list[dict], project: str | None = None, panel: dict | None = None) -> None:
    """Системные уведомления о событиях, которые ждут пользователя (desktop_notify в конфиге)."""
    if not cfg.get("desktop_notify", True):
        return
    title = f"aiflow · {project}" if project else "aiflow"
    important = [ev for ev in events if ev.get("kind") in NOTIFY_KINDS - NO_TOAST_KINDS]
    if len(important) > 3:
        _toast(title, f"{len(important)} событий, подробности в панели", toast_link(panel))
        return
    for ev in important:
        _toast(title, ev.get("text", ""), toast_link(panel), toast_actions(panel, ev))


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


def cmd_app(args, root: Path | None) -> int:
    """aiflow как приложение: автозапуск службы при входе в систему и установка панели приложением (PWA)."""
    import webbrowser
    import aiflow_service as svc
    if args.remove:
        print("Автозапуск выключен: " + svc.autostart(False))
        if os.name == "nt":
            svc.drop_old_shortcut()
        print("Само приложение удаляется в его окне: меню «…» → «Параметры приложения» → «Удалить», "
              "либо в параметрах Windows, раздел «Приложения».")
        print("Служба продолжает работать до aiflow down. Значок рядом с часами: \"tray\": false в "
              f"{svc.home() / 'config.json'}.")
        return 0
    if root is not None and Store(root).initialized and svc.register(root):
        print(f"Проект {root.name} добавлен в службу.")
    print("Автозапуск при входе в систему включён: " + svc.autostart(True))
    if os.name == "nt":
        svc.drop_old_shortcut()
    data = svc.ensure()
    url = svc.panel_url(data)
    print(f"Служба aiflow {data.get('version', '?')} работает (pid {data['pid']})" + (f"; панель: {url}" if url else ""))
    if not url:
        return 0
    if svc.app_installed():
        print("Приложение aiflow уже установлено" + ("." if args.no_open else ": открываю."))
        if not args.no_open:
            svc.open_window(url)
        return 0
    print("Установите приложение: в открывшейся панели нажмите «Установить приложение» (Edge или Chrome). "
          "Оно появится в меню «Пуск» со своим значком и окном.")
    if not args.no_open:
        webbrowser.open(url)  # обычная вкладка: в окне без адресной строки браузер установку не предлагает
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
            note = "нет .aiflow/config.json"
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


def _kill(pid: int, tree: bool = True) -> bool:
    """tree=False — только сам процесс: его потомки (ревьюеры, запущенные службой) доработают без него."""
    if not pid_alive(pid):
        return False
    try:
        if os.name == "nt":
            run(["taskkill", "/PID", str(pid)] + (["/T"] if tree else []) + ["/F"], timeout=30)
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


def cmd_chat(args, root: Path) -> int:
    """Чат с координатором проекта в этом терминале. Заголовок вкладки — имя проекта, и он не меняется:
    сам Claude Code переименовывает вкладку по теме разговора («aiflow resume»), здесь это отключено."""
    store = Store(root)
    if not store.initialized:
        raise AiflowError("репозиторий не инициализирован: aiflow init")
    sid, started = open_orchestrator(store)
    print("Запустил координатора." if started else "Подключаюсь к координатору.", flush=True)
    sys.stdout.write(f"]0;{root.name}")  # OSC 0: заголовок вкладки терминала
    sys.stdout.flush()
    env = dict(claude_env(), CLAUDE_CODE_DISABLE_TERMINAL_TITLE="1")
    return subprocess.call([need("claude"), "attach", sid], cwd=root, env=env)


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
        srv.server_close()
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
    working = sum(1 for t in tasks if t.get("state") in ("planning", "plan_check", "in_progress", "in_review"))
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


def cmd_review_plan(args, root: Path) -> int:
    import review
    return review.plan_main(["--pr", str(args.pr)])


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
    e.add_argument("--separate", action="store_true",
                   help="несколько задач за один вызов: пользователь просил раздельно или это отдельные фичи")
    e.add_argument("--project", help="поставить в другой проект службы (имя папки репозитория)")
    e.add_argument("--then", metavar="ID", help="задача этого проекта, которая подождёт поставленные")
    e.add_argument("--split-of", metavar="ID", dest="split_of",
                   help="поставленные задачи — остальные части задачи ID, которую пользователь решил разбить")
    e.add_argument("--title", help="с --split-of: новое название задачи ID — того, что осталось в ней как первая часть")
    af = sub.add_parser("after", help="поставить задачу после другой: id или проект/id")
    af.add_argument("id")
    af.add_argument("dep")
    af.add_argument("--remove", action="store_true", help="убрать зависимость")
    r = sub.add_parser("report", help="отчёт сессии")
    r.add_argument("id")
    r.add_argument("--role", required=True, choices=["planner", "worker"])
    r.add_argument("--status", required=True,
                   choices=["plan_ready", "delegate", "self_execute", "started", "answered", "ready_for_review", "done", "approved", "limit_reached", "error", "blocked"])
    r.add_argument("--pr", type=int)
    r.add_argument("--complexity", choices=["trivial", "simple", "complex"],
                   help="оценка реализации (планировщик): trivial — только с delegate")
    r.add_argument("--summary")
    r.add_argument("--details-file")
    s = sub.add_parser("status", help="состояние очереди")
    s.add_argument("--json", action="store_true")
    s.add_argument("--intake-rules", action="store_true", help=argparse.SUPPRESS)
    sts = sub.add_parser("stats", help="статистика: причины ухода к человеку, раунды ревью, время по стадиям, цена")
    sts.add_argument("--days", type=int, default=STATS_DAYS, help="задачи, созданные за столько дней; 0 — за всё время")
    sts.add_argument("--json", action="store_true")
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
    app_p = sub.add_parser("app", help="aiflow как приложение: автозапуск службы и установка панели приложением (PWA)")
    app_p.add_argument("--remove", action="store_true", help="выключить автозапуск")
    app_p.add_argument("--no-open", action="store_true", help="не открывать окно панели")
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
    rp = sub.add_parser("review-plan", help=argparse.SUPPRESS)  # ревью плана: запускает очередь
    rp.add_argument("--pr", type=int, required=True)
    sub.add_parser("intake-rules", help=argparse.SUPPRESS)
    sub.add_parser("chat", help="чат с координатором проекта в этом терминале; вкладка названа именем проекта")
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
        rootless = {"doctor": cmd_doctor, "statusline": cmd_statusline, "service": cmd_service, "up": cmd_up, "app": cmd_app, "intake-rules": cmd_intake_rules,
                    "down": cmd_down, "projects": cmd_projects, "forget": cmd_forget}
        if args.cmd in rootless:
            return rootless[args.cmd](args, root)
        if root is None:
            raise AiflowError("запустите aiflow внутри git-репозитория")
        handlers = {
            "init": cmd_init, "enqueue": cmd_enqueue, "report": cmd_report, "status": cmd_status, "stats": cmd_stats,
            "tick": cmd_tick, "run": cmd_run, "pause": cmd_pause, "resume": cmd_resume,
            "retry": cmd_retry, "cancel": cmd_cancel, "allow-path": cmd_allow_path, "after": cmd_after, "model": cmd_model, "approve": cmd_approve, "amend": cmd_amend, "reply": cmd_reply, "review": cmd_review, "review-plan": cmd_review_plan,
            "ui": cmd_ui, "chat": cmd_chat, "stop": cmd_stop,
        }
        return handlers[args.cmd](args, root)
    except AiflowError as e:
        print(f"aiflow: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
