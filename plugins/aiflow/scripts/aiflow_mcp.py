#!/usr/bin/env python3
"""MCP-сервер aiflow для чата Claude: задачи и расход токенов, только чтение.

Транспорт — stdio, по одному JSON-сообщению в строке. Инструменты возвращают короткий текст для модели
и structuredContent для встроенного интерфейса (MCP Apps, ресурс ui://aiflow/board.html): клиент, который
умеет MCP Apps, рисует доску или график прямо в чате, остальные показывают текст.

Сервер подключается в настройках приложения Claude (claude_desktop_config.json), а не в плагине Claude Code:
сессиям задач его инструменты не нужны и только занимали бы место в контексте.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import aiflow  # noqa: E402
import aiflow_service as svc  # noqa: E402

APP_URI = "ui://aiflow/board.html"
APP_MIME = "text/html;profile=mcp-app"
APP_PAGE = HERE / "mcp_app.html"
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")  # серверу хватает рукопожатия initialize
ACTIVE_DONE = ("merged", "done", "cancelled", "answered")
MAX_TASKS = 60  # в один ответ: чат — не панель, длинный хвост завершённых задач ему не нужен

PROJECT_ARG = {"type": "string", "description": "Project name or part of it. Omit for all projects."}
TOOLS = [
    {
        "name": "aiflow_tasks",
        "title": "Задачи aiflow",
        "description": ("Show the aiflow task board: tasks of the user's projects with stage, PR, what waits for the "
                        "user, tokens and cost. Use for questions such as \"what is in work\", \"show the tasks of "
                        "project X\". The board is drawn in the chat: do not repeat the list in your reply, add only "
                        "a short comment."),
        "inputSchema": {"type": "object", "properties": {
            "project": PROJECT_ARG,
            "scope": {"type": "string", "enum": ["active", "all"],
                      "description": "active (default): tasks in work and tasks finished in the last 24 hours. "
                                     "all: also older finished tasks."}}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "_meta": {"ui": {"resourceUri": APP_URI}, "ui/resourceUri": APP_URI},
    },
    {
        "name": "aiflow_usage",
        "title": "Расход токенов aiflow",
        "description": ("Show aiflow token usage and cost: by day and model, by project, and the most expensive "
                        "tasks. The chart is drawn in the chat: do not repeat the numbers in your reply, add only a "
                        "short comment."),
        "inputSchema": {"type": "object", "properties": {
            "project": PROJECT_ARG,
            "days": {"type": "integer", "minimum": 1, "maximum": 90,
                     "description": "How many last days to show. Default: 14."}}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
        "_meta": {"ui": {"resourceUri": APP_URI}, "ui/resourceUri": APP_URI},
    },
]


# -- данные

def load_projects() -> list[dict]:
    """Состояние проектов: у работающей службы (там же живые сессии), иначе — прямо из runs/."""
    url = svc.panel_url()
    if url:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/state", timeout=8) as r:
                return json.loads(r.read().decode("utf-8")).get("projects") or []
        except Exception:
            pass
    out = []
    for root in svc.projects():
        store = aiflow.Store(Path(root))
        base = {"key": svc.project_key(store.root), "name": store.root.name}
        if not store.initialized:
            continue
        try:
            out.append({**aiflow.build_state(store), **base})
        except Exception as e:
            out.append({**base, "error": str(e), "tasks": []})
    return out


def pick_projects(projects: list[dict], want: str | None) -> list[dict]:
    w = (want or "").strip().lower()
    if not w:
        return projects
    exact = [p for p in projects if str(p.get("name", "")).lower() == w or str(p.get("key")) == w]
    return exact or [p for p in projects if w in str(p.get("name", "")).lower()]


def model_family(model: str) -> str:
    m = model.lower()
    for name in ("opus", "sonnet", "haiku", "fable"):
        if name in m:
            return name.capitalize()
    return "Ревьюер" if "/" in model else "Другое"


def task_row(t: dict) -> dict:
    return {k: t.get(k) for k in ("id", "title", "state", "label", "kind", "stage", "complexity", "pr", "pr_url",
                                  "needs", "action", "activity", "tokens", "cost", "created_at", "updated_at")}


def tasks_result(args: dict) -> dict:
    scope = "all" if args.get("scope") == "all" else "active"
    found = pick_projects(load_projects(), args.get("project"))
    projects, lines = [], []
    for p in found:
        tasks = p.get("tasks") or []
        shown = [t for t in tasks if scope == "all" or t.get("state") not in ACTIVE_DONE
                 or aiflow.age_minutes(t.get("updated_at") or t.get("state_at")) < 24 * 60]
        shown.sort(key=lambda t: (t.get("state") in ACTIVE_DONE, -_stamp(t.get("updated_at"))))
        shown = shown[:MAX_TASKS]
        waiting = [t for t in shown if t.get("needs")]
        projects.append({"name": p.get("name"), "key": p.get("key"), "paused": bool(p.get("paused")),
                         "error": p.get("error"), "total": len(tasks), "hidden": len(tasks) - len(shown),
                         "tasks": [task_row(t) for t in shown]})
        active = [t for t in shown if t.get("state") not in ACTIVE_DONE]
        lines.append(f"{p.get('name')}: {len(active)} in work, {len(waiting)} wait for the user, "
                     f"{len(tasks)} tasks in total" + (" (queue paused)" if p.get("paused") else ""))
        lines += [f"  - {t['id']} [{t.get('label') or t.get('state')}]"
                  + (f" PR #{t['pr']}" if t.get("pr") else "")
                  + (f" — waits: {t['needs']}" if t.get("needs") else "") for t in active]
    if not found:
        lines.append("No such project in aiflow." if args.get("project") else "aiflow has no projects yet.")
    return {"view": "tasks", "scope": scope, "generated": aiflow.iso(), "projects": projects, "text": "\n".join(lines)}


def _stamp(ts: str | None) -> float:
    try:
        when = aiflow.parse_iso(ts)
        return when.timestamp() if when else 0.0
    except Exception:
        return 0.0


def usage_result(args: dict) -> dict:
    try:
        days = max(1, min(90, int(args.get("days") or 14)))
    except (TypeError, ValueError):
        days = 14
    found = pick_projects(load_projects(), args.get("project"))
    by_day: dict[str, dict[str, float]] = {}
    by_model: dict[str, dict] = {}
    by_project, top = [], []
    for p in found:
        p_cost = p_tokens = 0.0
        for t in p.get("tasks") or []:
            # день расхода — день последнего изменения задачи: расход по часам очередь не хранит
            day = str(t.get("updated_at") or t.get("created_at") or "")[:10]
            fresh = bool(day) and aiflow.age_minutes(day + "T00:00:00+00:00") <= days * 24 * 60
            if not fresh:
                continue
            for model, u in (t.get("usage") or {}).items():
                fam = model_family(model)
                cost = float(u.get("cost") or 0)
                tokens = sum(int(u.get(k) or 0) for k in ("input", "output", "cache_read", "cache_write"))
                by_day.setdefault(day, {})[fam] = by_day.get(day, {}).get(fam, 0.0) + cost
                m = by_model.setdefault(fam, {"model": fam, "cost": 0.0, "tokens": 0, "output": 0, "cache_read": 0})
                m["cost"] += cost
                m["tokens"] += tokens
                m["output"] += int(u.get("output") or 0)
                m["cache_read"] += int(u.get("cache_read") or 0)
            p_cost += float(t.get("cost") or 0)
            p_tokens += int(t.get("tokens") or 0)
            top.append({"id": t.get("id"), "title": t.get("title"), "project": p.get("name"), "pr_url": t.get("pr_url"),
                        "cost": round(float(t.get("cost") or 0), 2), "tokens": int(t.get("tokens") or 0)})
        by_project.append({"name": p.get("name"), "cost": round(p_cost, 2), "tokens": int(p_tokens)})
    top.sort(key=lambda x: -x["cost"])
    order = [f for f in ("Opus", "Sonnet", "Haiku", "Fable", "Ревьюер", "Другое") if f in by_model]
    total = round(sum(m["cost"] for m in by_model.values()), 2)
    lines = [f"aiflow usage for the last {days} days: ${total:.2f} in total."]
    lines += [f"  - {f}: ${by_model[f]['cost']:.2f}, {by_model[f]['tokens']:,} tokens" for f in order]
    lines += [f"  most expensive task: {top[0]['id']} (${top[0]['cost']:.2f})"] if top else []
    return {"view": "usage", "days": days, "generated": aiflow.iso(), "total": total, "models": order,
            "by_day": [{"day": d, **{f: round(by_day[d].get(f, 0.0), 2) for f in order}} for d in sorted(by_day)],
            "by_model": [{**by_model[f], "cost": round(by_model[f]["cost"], 2)} for f in order],
            "by_project": by_project, "top": top[:8], "text": "\n".join(lines)}


CALLS = {"aiflow_tasks": tasks_result, "aiflow_usage": usage_result}


# -- протокол

def call_tool(name: str, args: dict) -> dict:
    fn = CALLS.get(name)
    if not fn:
        return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}
    try:
        data = fn(args or {})
    except Exception as e:  # ошибка данных — ответ инструмента, а не сбой сервера
        return {"content": [{"type": "text", "text": f"aiflow: {e}"}], "isError": True}
    text = data.pop("text")
    return {"content": [{"type": "text", "text": text}], "structuredContent": data}


def handle(msg: dict) -> dict | None:
    """Ответ на одно сообщение JSON-RPC; None — отвечать не нужно (уведомление)."""
    method, mid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}
    if mid is None:
        return None
    if method == "initialize":
        want = params.get("protocolVersion")
        result = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                  "capabilities": {"tools": {}, "resources": {}},
                  "serverInfo": {"name": "aiflow", "title": "aiflow", "version": aiflow.VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = call_tool(str(params.get("name")), params.get("arguments") or {})
    elif method == "resources/list":
        result = {"resources": [{"uri": APP_URI, "name": "aiflow board", "mimeType": APP_MIME,
                                 "description": "Task board and token usage of aiflow, drawn in the chat."}]}
    elif method == "resources/read":
        if params.get("uri") != APP_URI:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32002, "message": "resource not found"}}
        result = {"contents": [{"uri": APP_URI, "mimeType": APP_MIME, "text": APP_PAGE.read_text(encoding="utf-8"),
                                "_meta": {"ui": {"prefersBorder": True}}}]}
    else:
        # в том числе server/discover: клиент новой версии протокола после этой ошибки переходит на initialize
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            out = handle(msg) if isinstance(msg, dict) else None
        if out is not None:
            stdout.write(json.dumps(out, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()
    return 0


# -- подключение к приложению Claude

LAUNCHER = '''"""MCP-сервер aiflow: самая новая версия плагина из того же маркетплейса."""
import runpy, sys
from pathlib import Path

CACHE = Path({cache!r})
FALLBACK = {fallback!r}


def ver(p):
    try:
        return tuple(int(x) for x in p.parent.parent.name.split("."))
    except ValueError:
        return (0,)


found = sorted(CACHE.glob("*/scripts/aiflow_mcp.py"), key=ver)
script = str(found[-1]) if found else FALLBACK
sys.argv = [script]
runpy.run_path(script, run_name="__main__")
'''


def write_launcher() -> Path:
    """Запускалка с постоянным адресом: путь к плагину содержит версию и меняется при каждом обновлении."""
    home = svc.home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / "mcp_launcher.py"
    path.write_text(LAUNCHER.format(cache=str(aiflow.PLUGIN_ROOT.parent), fallback=str(HERE / "aiflow_mcp.py")),
                    encoding="utf-8")
    return path


def desktop_entry() -> dict:
    """Запись для mcpServers в claude_desktop_config.json."""
    return {"command": sys.executable, "args": ["-X", "utf8", str(write_launcher())]}


if __name__ == "__main__":
    if "--config" in sys.argv:
        # запись для ручной вставки в claude_desktop_config.json (Settings → Developer → Edit Config)
        print(json.dumps({"mcpServers": {"aiflow": desktop_entry()}}, ensure_ascii=False, indent=2))
        sys.exit(0)
    sys.exit(serve())
