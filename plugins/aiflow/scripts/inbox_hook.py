"""Хук aiflow (PostToolUse и Stop): отдать работающей сессии дополнения пользователя к её задаче.

Монитор кладёт ~/.aiflow/inbox/<session_id>.json, когда пользователь дополнил задачу, а её сессия
занята. Хук забирает файл атомарно (переносом в inbox-delivered, по нему монитор отмечает доставку)
и возвращает текст: после вызова инструмента — как additionalContext, перед концом хода — как
причину продолжить работу. Без файла для этой сессии хук ничего не выводит.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    sid = str(data.get("session_id") or "")
    event = data.get("hook_event_name") or ""
    if not sid or "/" in sid or "\\" in sid:
        return 0
    home = Path(os.environ.get("AIFLOW_HOME") or Path.home() / ".aiflow")
    src = home / "inbox" / f"{sid}.json"
    if not src.is_file():
        return 0
    done = home / "inbox-delivered"
    done.mkdir(parents=True, exist_ok=True)
    dst = done / f"{sid}-{int(time.time() * 1000)}.json"
    try:
        os.replace(src, dst)  # забрать ровно один раз, даже если хуки сработали параллельно
        item = json.loads(dst.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    item["at"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    dst.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
    text = "[aiflow] " + str(item.get("text") or "")
    if event == "Stop":
        out = {"decision": "block", "reason": text}
    else:
        out = {"hookSpecificOutput": {"hookEventName": event or "PostToolUse", "additionalContext": text}}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
