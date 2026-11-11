"""Хук aiflow (PreToolUse для Bash и Grep): один раз за сессию задачи остановить текстовый поиск по коду
и напомнить про LSP.

Подсказка в первом сообщении сессии на практике не срабатывает: модель по привычке сразу пишет
`git grep`. Хук срабатывает в момент самого поиска. Первый такой вызов в сессии отклоняется с
объяснением; повтор той же команды проходит — решение остаётся за моделью, но принятое осознанно.

Действует только в сессиях задач aiflow (рабочая копия <проект>/.claude/worktrees/<id> при
существующем runs/<id>/task.json) и только если в проекте включён плагин языкового сервера.
Выключается настройкой search_nudge: false в runs/config.json.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# команда поиска в начале строки или после ; && ( — но не фильтр после | (… | grep …)
SEARCH_RE = re.compile(r"(?:^|[;&(]\s*|\bxargs\s+)(?:git\s+(?:-C\s+\S+\s+)?grep|rg|grep|findstr)\s", re.I)
WORKTREE_RE = re.compile(r"^(?P<root>.+?)[\\/]\.claude[\\/]worktrees[\\/](?P<task>[a-z0-9][a-z0-9-]*)(?:[\\/]|$)", re.I)
KEEP_DAYS = 7


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_search(tool: str, tool_input: dict) -> bool:
    if tool == "Grep":
        return True
    return tool == "Bash" and bool(SEARCH_RE.search(str(tool_input.get("command") or "")))


def lsp_plugins(root: Path) -> list[str]:
    """Включённые в проекте плагины языковых серверов (…-lsp@…)."""
    found: dict[str, bool] = {}
    for name in ("settings.json", "settings.local.json"):  # local перекрывает project
        for key, on in (read_json(root / ".claude" / name).get("enabledPlugins") or {}).items():
            if "-lsp@" in str(key):
                found[str(key)] = bool(on)
    return sorted(k.split("@")[0] for k, on in found.items() if on)


def decide(data: dict, home: Path) -> str | None:
    """Текст, с которым вызов нужно отклонить, или None — пропустить."""
    if not is_search(str(data.get("tool_name") or ""), data.get("tool_input") or {}):
        return None
    m = WORKTREE_RE.match(str(data.get("cwd") or ""))
    if not m:
        return None
    root = Path(m.group("root"))
    if not (root / "runs" / m.group("task").lower() / "task.json").is_file():
        return None  # рабочая копия не задачи aiflow
    cfg = read_json(root / "runs" / "config.json")
    if cfg.get("search_nudge") is False:
        return None
    plugins = lsp_plugins(root)
    if not plugins:
        return None  # LSP в проекте не включён — напоминать не о чем
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("session_id") or ""))
    if not sid:
        return None
    marks = home / "search-hints"
    mark = marks / sid
    if mark.exists():
        return None  # в этой сессии уже напоминали
    try:
        marks.mkdir(parents=True, exist_ok=True)
        mark.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
        for old in marks.iterdir():
            if time.time() - old.stat().st_mtime > KEEP_DAYS * 86400:
                old.unlink(missing_ok=True)
    except OSError:
        return None  # не можем запомнить — не рискуем отклонять каждый вызов
    explorer = "" if str(cfg.get("explorer") or "aiflow").lower() == "project" else \
        " Либо отдай вопрос сабагенту aiflow:explorer."
    return (
        f"[aiflow] В проекте включён языковой сервер ({', '.join(plugins)}). Если ты ищешь символ — где он определён, "
        "кто его использует, реализует или вызывает, — возьми инструмент LSP: он отложенный, загрузи его через "
        "ToolSearch запросом select:LSP (операции findReferences, goToDefinition, goToImplementation, incomingCalls, "
        f"workspaceSymbol).{explorer} Если это поиск по тексту, а не по символу, или LSP для этого языка не отвечает — "
        "просто повтори ту же команду: второй раз она пройдёт. Это напоминание приходит один раз за сессию."
    )


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    home = Path(os.environ.get("AIFLOW_HOME") or Path.home() / ".aiflow")
    text = decide(data, home)
    if not text:
        return 0
    sys.stderr.buffer.write(text.encode("utf-8"))
    return 2  # код 2 в PreToolUse: вызов отклонён, stderr уходит модели


if __name__ == "__main__":
    sys.exit(main())
