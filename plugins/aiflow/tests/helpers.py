"""Общее для тестов aiflow: подделки `claude` и `gh`, временный проект, сдвиг времени.

Тесты не запускают ни Claude Code, ни GitHub: Dispatcher получает подделки через конструктор.
Запуск: python -m unittest discover -s plugins/aiflow/tests
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import aiflow  # noqa: E402

PLAN = "<!-- ai-plan v1 -->\n## Цель и границы\nплан"


def ago(minutes: float) -> str:
    return aiflow.iso(aiflow.now() - dt.timedelta(minutes=minutes))


class FakeClaude:
    """Фоновые сессии: launch создаёт работающую, resume будит по sessionId."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.launched: list[dict] = []
        self.resumed: list[dict] = []
        self.removed: list[str] = []
        self.log_text = ""
        self.fail_launch = False
        self.fail_resume = False

    def agents(self) -> dict:
        return {k: dict(v) for k, v in self.sessions.items()}

    def launch(self, agent, name, prompt, **kw):
        if self.fail_launch:
            return None, "launch failed"
        sid = f"s{len(self.sessions) + 1}"
        self.sessions[sid] = {"id": sid, "name": name, "state": "working", "sessionId": f"uuid-{sid}",
                              "updatedAt": aiflow.iso()}
        self.launched.append({"agent": agent, "name": name, "prompt": prompt, "sid": sid, **kw})
        return sid, ""

    def resume(self, session_id, prompt, mode=None, cwd=None):
        if self.fail_resume:
            return None, "resume failed"
        for sid, s in self.sessions.items():
            if s["sessionId"] == session_id:
                s["state"] = "working"
                self.resumed.append({"sid": sid, "prompt": prompt})
                return sid, ""
        return None, "no such session"

    def logs(self, sid):
        return self.log_text

    def stop(self, sid):
        return True

    def rm(self, sid):
        self.removed.append(sid)
        return True

    def finish_turn(self, sid: str, state: str = "done", minutes_ago: float = 0) -> None:
        self.sessions[sid].update(state=state, updatedAt=ago(minutes_ago))


class FakeGH:
    """PR в памяти в том виде, в каком их отдаёт gh pr view --json."""

    def __init__(self):
        self.prs: dict[int, dict] = {}
        self.files: dict[int, set | None] = {}
        self.push_run: dict | None = None
        self.calls: list[tuple] = []
        self.down = False  # GitHub не отвечает

    def add_pr(self, n: int, tid: str, *, body: str = PLAN, draft: bool = False, state: str = "OPEN",
               branch: str | None = None, files=()) -> dict:
        self.prs[n] = {"state": state, "isDraft": draft, "headRefName": branch or f"ai/{tid}", "headRefOid": f"sha{n}",
                       "body": body, "labels": [], "comments": [], "statusCheckRollup": []}
        self.files[n] = set(files)
        return self.prs[n]

    def prefetch(self, numbers) -> None:
        pass

    def forget(self, n) -> None:
        pass

    def pr(self, n, fields):
        if self.down or int(n) not in self.prs:
            raise aiflow.AiflowError(f"gh pr view {n}: нет ответа")
        return self.prs[int(n)]

    def pr_files(self, n):
        return self.files.get(int(n))

    def add_label(self, n, label):
        self.prs[int(n)]["labels"].append({"name": label})

    def remove_label(self, n, label):
        pr = self.prs[int(n)]
        pr["labels"] = [x for x in pr["labels"] if x["name"] != label]

    def call(self, *args, timeout=60):
        self.calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def last_push_run(self, branch):
        return self.push_run

    def repo_url(self):
        return "https://example.test/o/r"

    def labels(self, n) -> set:
        return {x["name"] for x in self.prs[int(n)]["labels"]}


class ProjectCase(unittest.TestCase):
    """Временный проект с runs/config.json и подделками вместо claude и gh."""

    config: dict = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiflow-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "repo"
        (self.root / "runs").mkdir(parents=True)
        self._env = {k: os.environ.get(k) for k in ("AIFLOW_HOME", "CLAUDE_CONFIG_DIR")}
        os.environ["AIFLOW_HOME"] = str(self.tmp / "home")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp / "claude")
        self.addCleanup(self._restore_env)
        self.store = aiflow.Store(self.root)
        aiflow.write_json(self.root / "runs" / "config.json", {"watch_integration": True, **self.config})
        aiflow._BASE_SEEN[(str(self.root), "master")] = (time.time(), True)  # без git ls-remote
        self.claude = FakeClaude()
        self.gh = FakeGH()

    def _restore_env(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # -- действия
    def add(self, tid: str, **fields) -> dict:
        t = aiflow.normalize_task({"id": tid, "text": f"сделать {tid}", **{k: v for k, v in fields.items()
                                                                          if k in ("depends_on", "kind", "complexity")}})
        t.update({k: v for k, v in fields.items() if k not in ("depends_on", "kind", "complexity")})
        self.store.save_task(t)
        return t

    def tick(self) -> list[dict]:
        return aiflow.Dispatcher(self.store, self.claude, self.gh).tick()

    def report(self, tid: str, role: str, status: str, pr: int | None = None, **kw) -> None:
        aiflow.write_report(self.root, tid, role, status, pr, kw.get("summary", ""), kw.get("details", ""),
                            kw.get("complexity"))

    def task(self, tid: str) -> dict:
        return self.store.task(tid)

    def state(self, tid: str) -> str:
        return self.task(tid)["state"]

    def patch(self, tid: str, **fields) -> None:
        t = self.task(tid)
        t.update(fields)
        self.store.save_task(t)

    def sid(self, tid: str, role: str) -> str:
        return self.task(tid)["sessions"][role][-1]["id"]

    def age(self, tid: str, minutes: float, role: str | None = None) -> None:
        """Состарить задачу (и последнюю сессию роли): отсчёты «закончил ход» и «только что разбудили»."""
        t = self.task(tid)
        t["state_at"] = ago(minutes)
        for lst in (t.get("sessions") or {}).values():
            for e in lst:
                if e.get("resumed_at"):
                    e["resumed_at"] = ago(minutes)
        self.store.save_task(t)
        if role:
            self.claude.sessions[self.sid(tid, role)]["updatedAt"] = ago(minutes)

    def kinds(self, events: list[dict]) -> list[str]:
        return [e["kind"] for e in events]

    # -- готовые положения
    def to_plan_review(self, tid: str, pr: int, files=()) -> None:
        """Задача с планом, который ждёт одобрения."""
        self.add(tid)
        self.tick()
        self.gh.add_pr(pr, tid, files=files)
        self.report(tid, "planner", "plan_ready", pr, complexity="complex")
        self.claude.finish_turn(self.sid(tid, "planner"))
        self.tick()

    def to_ready(self, tid: str, pr: int, files=(), ready_minutes_ago: float = 0) -> None:
        """Задача, PR которой ждёт очереди ревью (исполнитель закончил ход)."""
        t = self.add(tid)
        self.gh.add_pr(pr, tid, files=files)
        self.claude.sessions[f"w-{tid}"] = {"id": f"w-{tid}", "name": f"worker: {tid}", "state": "done",
                                            "sessionId": f"uuid-w-{tid}", "updatedAt": aiflow.iso()}
        t.update(state="ready", pr=pr, plan_approved=True, ready_at=ago(ready_minutes_ago),
                 comments_seen_at=aiflow.iso(), comments_floor=aiflow.iso(),
                 sessions={"planner": [], "worker": [{"id": f"w-{tid}", "name": f"worker: {tid}",
                                                      "session_id": f"uuid-w-{tid}", "started_at": ago(30)}]})
        self.store.save_task(t)
