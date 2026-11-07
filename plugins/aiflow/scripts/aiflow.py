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
SIDE_STATES = ["needs_human", "cancelled"]
IMPLEMENTING = {"planning", "in_progress"}
TERMINAL = {"merged", "cancelled"}
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
MARKERS = ("<!-- aiflow", "<!-- ai-review", "<!-- human")
LIMIT_RE = re.compile(
    r"(usage|rate)[ -]?limit|limit (reached|exceeded)|out of (usage|credits)|"
    r"resets? (at|in) |too many requests|\b429\b", re.I)

def by_complexity(value, cx: str):
    """Настройка модели/effort: словарь {"simple": …, "complex": …} или одно значение на все задачи."""
    if isinstance(value, dict):
        return value.get(cx) or value.get("simple") or None
    return value or None


def worker_choice(cfg: dict, t: dict) -> tuple:
    """Модель и effort worker'а: настройки задачи, затем Opus-effort, затем по сложности."""
    cx = t.get("complexity", "simple")
    model = t.get("worker_model") or by_complexity(cfg["worker_model"], cx)
    effort = t.get("worker_effort")
    if not effort and "opus" in str(model or "").lower():
        effort = cfg.get("worker_opus_effort") or None
    return model, effort or by_complexity(cfg["worker_effort"], cx)

EFFORTS = ("low", "medium", "high", "xhigh", "max")

DEFAULT_CONFIG = {
    "concurrency": 2,
    "tick_seconds": 45,
    "base_branch": "master",
    "ui_port": 8765,
    "permission_mode": "acceptEdits",
    "planner_model": {"complex": "opus", "simple": "sonnet"},
    "planner_effort": {"complex": "medium", "simple": "medium"},
    "worker_model": "sonnet",
    "worker_effort": {"complex": "high", "simple": "medium"},  # или одна строка на все задачи
    "worker_opus_effort": "medium",  # effort worker'а на Opus (worker_model: opus), без учёта сложности
    "reviewer_model": "",
    "review_identity": "self",  # self — ревью и мерж от вашего gh; bot — от бота с токеном REVIEW_BOT_TOKEN
    "max_rounds": 5,
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


# ---------------------------------------------------------------- utilities

class AiflowError(Exception):
    pass


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


def run(cmd: list[str], *, cwd=None, timeout: int = 120, env=None, input: str | None = None):
    try:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=env, input=input, **NO_WINDOW,
        )
    except FileNotFoundError:
        raise AiflowError(f"не найдено: {cmd[0]}")
    except subprocess.TimeoutExpired:
        raise AiflowError(f"таймаут {timeout} с: {' '.join(cmd[:3])}")


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
    return re.compile(r"^" + out + r"$")


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

    def set_config(self, key: str, value) -> None:
        """Меняет один ключ в runs/config.json, остальное содержимое файла сохраняет как есть."""
        path = self.runs / "config.json"
        user = read_json(path, None)
        if not isinstance(user, dict):
            raise AiflowError("runs/config.json не найден или повреждён — выполните aiflow init")
        user[key] = value
        write_json(path, user)

    def queue(self) -> dict:
        q = read_json(self.runs / "queue.json", {}) or {}
        q.setdefault("paused", None)
        q.setdefault("review_lock", None)
        return q

    def save_queue(self, q: dict) -> None:
        write_json(self.runs / "queue.json", q)

    def task_path(self, tid: str) -> Path:
        return self.runs / tid / "task.json"

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


def write_report(root: Path, tid: str, role: str, status: str, pr: int | None = None,
                 summary: str = "", details: str = "") -> dict:
    store = Store(root)
    if not store.task(tid):
        raise AiflowError(f"задача {tid} не найдена в {store.runs}")
    rep = {"rid": uuid.uuid4().hex, "at": iso(), "role": role, "status": status,
           "pr": pr, "summary": summary.strip(), "details": details.strip()}
    write_json(store.runs / tid / "report.json", rep)
    store.log(tid, {"report": rep})
    return rep


class FileLock:
    """Простой межпроцессный lock на файле с pid владельца."""

    def __init__(self, path: Path, stale_minutes: float = 10):
        self.path = path
        self.stale = stale_minutes
        self.held = False

    def acquire(self, wait_seconds: float = 0) -> bool:
        deadline = time.time() + wait_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, json.dumps({"pid": os.getpid(), "at": iso()}).encode())
                os.close(fd)
                self.held = True
                return True
            except FileExistsError:
                info = read_json(self.path, {}) or {}
                pid = int(info.get("pid") or 0)
                if not info and age_minutes(None) and self._file_age_minutes() < 0.1:
                    time.sleep(0.2)  # файл только что создан, pid ещё не записан
                    continue
                if not pid_alive(pid):
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
        info = read_json(self.path, {}) or {}
        pid = int(info.get("pid") or 0)
        return pid if pid_alive(pid) else 0


# ---------------------------------------------------------------- external tools

class Claude:
    def __init__(self, root: Path):
        self.root = root
        self.bin = need("claude")

    def agents(self) -> dict[str, dict]:
        p = run([self.bin, "agents", "--json", "--all", "--cwd", str(self.root)], cwd=self.root, timeout=60)
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

    def launch(self, agent: str, name: str, prompt: str, *, model=None, effort=None, mode=None) -> tuple[str | None, str]:
        cmd = [self.bin, "--bg", "--agent", agent, "--name", name]
        if mode:
            cmd += ["--permission-mode", mode]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        cmd.append(prompt)
        p = run(cmd, cwd=self.root, timeout=180)
        out = (p.stdout or "") + (p.stderr or "")
        return (self._short_id(out) if p.returncode == 0 else None), out.strip()

    def resume(self, session_id: str, prompt: str) -> tuple[str | None, str]:
        p = run([self.bin, "--resume", session_id, "--bg", prompt], cwd=self.root, timeout=180)
        out = (p.stdout or "") + (p.stderr or "")
        return (self._short_id(out) if p.returncode == 0 else None), out.strip()

    def logs(self, sid: str) -> str:
        p = run([self.bin, "logs", sid], cwd=self.root, timeout=30)
        return (p.stdout or "")[-6000:]

    def rm(self, sid: str) -> bool:
        return run([self.bin, "rm", sid], cwd=self.root, timeout=60).returncode == 0


class GH:
    def __init__(self, root: Path):
        self.root = root
        self.bin = need("gh")
        self._repo_url = None

    def call(self, *args: str, timeout: int = 60):
        return run([self.bin, *args], cwd=self.root, timeout=timeout)

    def json(self, *args: str):
        p = self.call(*args)
        if p.returncode != 0:
            raise AiflowError("gh " + " ".join(args[:3]) + ": " + (p.stderr or p.stdout).strip()[:300])
        return json.loads(p.stdout or "null")

    def pr(self, n: int, fields: str) -> dict:
        return self.json("pr", "view", str(n), "--json", fields)

    def add_label(self, n: int, label: str) -> None:
        self.call("pr", "edit", str(n), "--add-label", label)

    def remove_label(self, n: int, label: str) -> None:
        self.call("pr", "edit", str(n), "--remove-label", label)

    def repo_url(self) -> str:
        if self._repo_url is None:
            p = self.call("repo", "view", "--json", "url", "-q", ".url")
            self._repo_url = p.stdout.strip() if p.returncode == 0 else ""
        return self._repo_url

    def last_push_run(self, branch: str) -> dict | None:
        p = self.call("run", "list", "--branch", branch, "--event", "push", "--limit", "1",
                      "--json", "conclusion,status,url,headSha")
        if p.returncode != 0:
            return None
        data = json.loads(p.stdout or "[]")
        return data[0] if data else None


# ---------------------------------------------------------------- usage

_usage_cache: dict[str, tuple[float, dict]] = {}


def transcript_usage(session_id: str) -> dict | None:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    files = glob.glob(str(base / "projects" / "*" / f"{session_id}.jsonl"))
    if not files:
        return None
    path = files[0]
    mtime = os.path.getmtime(path)
    cached = _usage_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    per_msg: dict[str, dict] = {}
    model = ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message") if isinstance(obj, dict) else None
            if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                continue
            per_msg[msg.get("id") or f"line{i}"] = msg["usage"]
            model = msg.get("model") or model
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "model": model}
    for u in per_msg.values():
        tot["input"] += int(u.get("input_tokens") or 0)
        tot["output"] += int(u.get("output_tokens") or 0)
        tot["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        tot["cache_write"] += int(u.get("cache_creation_input_tokens") or 0)
    _usage_cache[path] = (mtime, tot)
    return tot


def task_tokens(t: dict) -> int:
    return sum(int(u.get("input", 0)) + int(u.get("output", 0)) + int(u.get("cache_write", 0))
               for u in (t.get("usage") or {}).values())


# ---------------------------------------------------------------- prompts

def planner_prompt(store: Store, t: dict, cfg: dict) -> str:
    return (
        f"aiflow task {t['id']}: {t.get('title', '')}\n"
        f"Файл задачи: {store.task_path(t['id'])} — прочитай его целиком.\n"
        f"Сложность: {t.get('complexity', 'simple')}. Базовая ветка: {cfg['base_branch']}. "
        f"Ветка задачи: ai/{t['id']}.\n"
        f"Когда draft PR с планом создан, выполни: "
        f"aiflow report {t['id']} --role planner --status plan_ready --pr <номер> --summary \"<одна строка>\""
    )


def revise_prompt(t: dict, comments: list[dict]) -> str:
    quoted = "\n\n".join(f"— {c.get('author', {}).get('login', '?')} ({c.get('createdAt', '')}):\n{c.get('body', '').strip()}"
                         for c in comments)
    return (
        f"Новые комментарии к плану в PR #{t['pr']} (задача {t['id']}):\n\n{quoted}\n\n"
        "Учти их: обнови план в описании PR (gh pr edit --body-file), допиши «Историю изменений плана», "
        "ответь в PR одним комментарием, первая строка которого <!-- aiflow:planner -->, "
        f"затем выполни aiflow report {t['id']} --role planner --status plan_ready --pr {t['pr']} "
        "--summary \"<что изменено>\"."
    )


def worker_prompt(store: Store, t: dict, cfg: dict, retry: bool = False) -> str:
    base = (
        f"aiflow task {t['id']}: {t.get('title', '')}\n"
        f"PR #{t['pr']}, ветка ai/{t['id']}, базовая ветка {cfg['base_branch']}. "
        f"План — в описании PR (gh pr view {t['pr']}). Файл задачи: {store.task_path(t['id'])}.\n"
    )
    if retry:
        rep = store.report(t["id"]) or {}
        base += (
            "Это повторная попытка после остановки. Прочитай открытые треды ревью в PR и последний отчёт: "
            f"{json.dumps(rep, ensure_ascii=False)}\n"
            "Если PR уже был на ревью, исправь открытые blocking-замечания и снова отчитайся ready_for_review.\n"
        )
    base += (
        f"Когда реализация готова, PR переведён в ready и проверки пройдены, выполни "
        f"aiflow report {t['id']} --role worker --status ready_for_review --pr {t['pr']} --summary \"<1–2 строки>\" "
        "и закончи ход. Ревью сам не запускай — тебя разбудят, когда подойдёт очередь."
    )
    return base


def review_prompt(t: dict, cfg: dict) -> str:
    return (
        f"Твоя очередь на ревью PR #{t['pr']} (задача {t['id']}). "
        f"git fetch origin; git rebase origin/{cfg['base_branch']}; разреши конфликты; прогони тесты; "
        "git push --force-with-lease. Затем запусти aiflow review --pr "
        f"{t['pr']} и действуй по статусу, как описано в твоих инструкциях, до терминального статуса."
    )


def ci_fix_prompt(t: dict, failed: list[str]) -> str:
    return (
        f"PR #{t['pr']} одобрен, но после этого упали проверки CI: {', '.join(failed) or 'см. gh pr checks'}. "
        "Посмотри gh pr checks и логи упавших job, исправь, прогони тесты, запушь и снова запусти "
        f"aiflow review --pr {t['pr']}."
    )


# ---------------------------------------------------------------- dispatcher

class Dispatcher:
    def __init__(self, store: Store):
        self.store = store
        self.root = store.root
        self.claude = Claude(self.root)
        self.gh = GH(self.root)
        self.events: list[dict] = []
        self.cfg: dict = {}
        self.q: dict = {}
        self.sessions: dict[str, dict] = {}

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
        t["report_seen"] = rep.get("rid")
        return rep

    def release_lock(self, t: dict) -> None:
        if self.q.get("review_lock") == t["id"]:
            self.q["review_lock"] = None

    def pause(self, reason: str, minutes: float | None, text: str) -> None:
        if self.q.get("paused") and self.q["paused"].get("reason") == reason:
            return
        until = iso(now() + dt.timedelta(minutes=minutes)) if minutes else None
        self.q["paused"] = {"reason": reason, "at": iso(), "until": until}
        self.emit(None, "paused", text)

    def unpause(self, text: str) -> None:
        if self.q.get("paused"):
            self.q["paused"] = None
            self.emit(None, "resumed", text)

    def check_limit(self, entry: dict | None) -> bool:
        if not entry:
            return False
        text = self.claude.logs(entry["id"])
        s = self.sess(entry) or {}
        if LIMIT_RE.search(text) or LIMIT_RE.search(json.dumps(s, ensure_ascii=False)):
            mins = self.cfg["rate_limit_pause_minutes"]
            self.pause("rate_limit", mins, f"лимит использования: очередь на паузе на {mins} мин")
            return True
        return False

    def to_human(self, t: dict, reason: str) -> None:
        self.release_lock(t)
        t["human_reason"] = reason
        self.set_state(t, "needs_human", reason)
        if t.get("pr"):
            self.gh.add_label(t["pr"], self.cfg["labels"]["needs_human"])
        self.emit(t, "needs_human", f"{t['id']}: нужен человек — {reason}", key=f"human:{iso()[:16]}:{reason[:40]}")

    # -- launching
    def launch(self, t: dict, role: str, prompt: str, model=None, effort=None) -> bool:
        name = f"{role}: {t['id']}"
        t["launching"] = {"role": role, "name": name, "at": iso()}
        self.store.save_task(t)
        sid, out = self.claude.launch(f"aiflow:{role}", name, prompt, model=model, effort=effort,
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
        self.adopt(t, role, sid, name)
        return True

    def adopt(self, t: dict, role: str, sid: str, name: str) -> None:
        entry = {"id": sid, "name": name, "started_at": iso(), "session_id": None}
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
        new_id, out = self.claude.resume(sid_full, prompt)
        if new_id and new_id != entry["id"]:
            self.adopt(t, role, new_id, entry.get("name", f"{role}: {t['id']}"))
        elif not new_id:
            self.store.log(t["id"], {"resume_failed": out[-800:]})
            return False
        self.store.log(t["id"], {"woke": role})
        return True

    def resolve_launching(self, t: dict) -> None:
        info = t.get("launching")
        if not info:
            return
        for sid, s in self.sessions.items():
            if s.get("name") == info.get("name"):
                self.adopt(t, info["role"], sid, info["name"])
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
        by_id = {t["id"]: t for t in tasks}
        self.process_commands(by_id)
        self.check_pause_timer()
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
        if not self.q.get("paused"):
            self.schedule(tasks)
        self.collect_usage(tasks)
        self.check_batch_done(tasks)
        self.store.save_queue(self.q)
        return self.events

    def fill_session_ids(self, t: dict) -> None:
        for lst in (t.get("sessions") or {}).values():
            for e in lst:
                if not e.get("session_id"):
                    s = self.sessions.get(e["id"])
                    if s and s.get("sessionId"):
                        e["session_id"] = s["sessionId"]

    def process_commands(self, by_id: dict[str, dict]) -> None:
        for c in self.store.take_commands():
            kind = c.get("cmd")
            if kind == "pause":
                self.q["paused"] = {"reason": "manual", "at": iso(), "until": None, "note": c.get("reason", "")}
                self.emit(None, "paused", "очередь поставлена на паузу вручную")
            elif kind == "resume":
                self.unpause("очередь возобновлена")
            elif kind == "retry" and c.get("id") in by_id:
                t = by_id[c["id"]]
                t["retry_requested"] = True
                self.store.save_task(t)

    def check_pause_timer(self) -> None:
        p = self.q.get("paused")
        if p and p.get("until") and now() >= (parse_iso(p["until"]) or now()):
            self.unpause("пауза по лимиту закончилась, очередь продолжает")

    def check_integration(self) -> None:
        r = self.gh.last_push_run(self.cfg["base_branch"])
        if not r or r.get("status") != "completed":
            return
        red = r.get("conclusion") in ("failure", "timed_out", "startup_failure")
        paused = self.q.get("paused") or {}
        if red and paused.get("reason") != "integration_red":
            self.pause("integration_red", None, f"ветка {self.cfg['base_branch']} покраснела после мержа: {r.get('url', '')}")
        elif not red and paused.get("reason") == "integration_red":
            self.unpause(f"ветка {self.cfg['base_branch']} снова зелёная")

    def advance(self, t: dict) -> None:
        st = t.get("state", "new")
        if t.get("retry_requested") and st == "needs_human":
            return self.do_retry(t)
        getattr(self, f"adv_{st}", lambda _t: None)(t)

    def adv_planning(self, t: dict) -> None:
        rep = self.new_report(t, "planner")
        if rep:
            if rep.get("status") == "plan_ready" and rep.get("pr"):
                t["pr"] = int(rep["pr"])
                t["plan_seen_at"] = iso()
                self.set_state(t, "plan_review")
                if t.get("auto_approve_plan") and t.get("complexity") == "simple" and self.try_auto_approve(t):
                    return
                self.emit(t, "plan_ready", f"{t['id']}: план готов к обсуждению — PR #{t['pr']}",
                          key=f"plan:{rep['rid']}")
                return
            if rep.get("status") in ("error", "blocked"):
                return self.to_human(t, rep.get("summary") or "планировщик сообщил о проблеме")
        e = self.cur(t, "planner")
        st = self.sstate(e)
        if st in ("failed", "stopped"):
            if not self.check_limit(e):
                self.to_human(t, "сессия планировщика завершилась без плана")
        elif st == "done" and age_minutes((self.sess(e) or {}).get("updatedAt") or t.get("state_at")) > 2:
            self.to_human(t, "планировщик закончил ход без aiflow report")
        elif st == "blocked":
            if not self.check_limit(e):
                w = (self.sess(e) or {}).get("waitingFor") or "ответ"
                self.emit(t, "needs_input", f"{t['id']}: планировщик ждёт вас в своей сессии ({w})",
                          key=f"blocked:{e['id']}:{w}")
        elif e and st is None and age_minutes(e.get("started_at")) > 10:
            self.to_human(t, "сессия планировщика пропала из claude agents")

    def try_auto_approve(self, t: dict) -> bool:
        body = (self.gh.pr(t["pr"], "body").get("body") or "")
        ok, missing = plan_checklist(body, self.cfg, t)
        if ok:
            self.gh.add_label(t["pr"], self.cfg["labels"]["approved"])
            t["plan_approved"] = True
            t["plan_auto_approved"] = True
            self.store.log(t["id"], {"auto_approved": True})
            return True
        t["plan_checklist_missing"] = missing
        return False

    def adv_plan_review(self, t: dict) -> None:
        pr = self.gh.pr(t["pr"], "state,labels,comments")
        if pr.get("state") in ("CLOSED", "MERGED"):
            self.set_state(t, "cancelled", "PR закрыт на стадии плана")
            self.emit(t, "cancelled", f"{t['id']}: PR с планом закрыт, задача снята", key="cancelled")
            return
        labels = {lb.get("name") for lb in pr.get("labels") or []}
        if self.cfg["labels"]["approved"] in labels:
            t["plan_approved"] = True
            return
        rep = self.new_report(t, "planner")
        if rep and rep.get("status") == "plan_ready":
            t["revising"] = False
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
        if t.get("auto_approve_plan") and t.get("complexity") == "simple" and not t.get("plan_checklist_missing"):
            self.try_auto_approve(t)

    def adv_in_progress(self, t: dict) -> None:
        rep = self.new_report(t, "worker")
        if rep:
            if rep.get("status") == "ready_for_review":
                t["ready_at"] = iso()
                self.set_state(t, "ready", "реализация готова")
                return
            if rep.get("status") in ("error", "blocked", "limit_reached"):
                return self.to_human(t, rep.get("summary") or f"worker: {rep.get('status')}")
        self.watch_session(t, "worker", "сессия worker'а завершилась до готовности PR")

    def watch_session(self, t: dict, role: str, dead_reason: str) -> None:
        e = self.cur(t, role)
        st = self.sstate(e)
        if st in ("failed", "stopped"):
            if not self.check_limit(e):
                self.to_human(t, dead_reason)
        elif st == "blocked":
            if not self.check_limit(e):
                w = (self.sess(e) or {}).get("waitingFor") or "ответ"
                self.emit(t, "needs_input", f"{t['id']}: {role} ждёт вас в своей сессии ({w})",
                          key=f"blocked:{e['id']}:{w}")
        elif st == "done" and age_minutes(t.get("state_at")) > 2 and t.get("state") == "in_progress":
            self.to_human(t, f"{role} закончил ход без aiflow report")
        elif e and st is None and age_minutes(e.get("started_at")) > 10:
            self.to_human(t, f"сессия {role} пропала из claude agents")

    def adv_ready(self, t: dict) -> None:
        pr = self.gh.pr(t["pr"], "state")
        if pr.get("state") == "MERGED":
            self.finish_merged(t)
        elif pr.get("state") == "CLOSED":
            self.set_state(t, "cancelled", "PR закрыт")

    def adv_in_review(self, t: dict) -> None:
        pr = self.gh.pr(t["pr"], "state,labels,headRefOid,statusCheckRollup")
        if pr.get("state") == "MERGED":
            return self.finish_merged(t)
        if pr.get("state") == "CLOSED":
            self.release_lock(t)
            self.set_state(t, "cancelled", "PR закрыт")
            return
        labels = {lb.get("name") for lb in pr.get("labels") or []}
        rep = self.new_report(t, "worker")
        if rep:
            st = rep.get("status")
            if st == "approved":
                t["approved_sha"] = pr.get("headRefOid")
            elif st in ("limit_reached", "error", "already_closed", "blocked"):
                return self.to_human(t, rep.get("summary") or f"ревью: {st}")
        if self.cfg["labels"]["needs_human"] in labels:
            return self.to_human(t, t.get("human_reason") or "PR помечен needs-human")
        if t.get("approved_sha") and t["approved_sha"] == pr.get("headRefOid"):
            failed = [c.get("name") or c.get("context") for c in pr.get("statusCheckRollup") or []
                      if (c.get("conclusion") or c.get("state") or "").upper() in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED")]
            if failed and t.get("ci_wake") != pr.get("headRefOid"):
                t["ci_wake"] = pr.get("headRefOid")
                if not self.wake(t, "worker", ci_fix_prompt(t, failed)):
                    self.to_human(t, "после одобрения упал CI, а сессию worker'а не удалось разбудить")
            return
        e = self.cur(t, "worker")
        last_review = (self.store.review(t["id"]) or {}).get("at") or t.get("state_at")
        if self.sstate(e) in ("failed", "stopped") and not self.check_limit(e):
            self.to_human(t, "сессия worker'а завершилась во время ревью")
        elif self.sstate(e) == "done" and age_minutes(last_review) > 5 and age_minutes(t.get("state_at")) > 5:
            self.to_human(t, "worker закончил ход, не доведя ревью до итогового статуса")
        elif self.sstate(e) == "blocked" and not self.check_limit(e):
            w = (self.sess(e) or {}).get("waitingFor") or "ответ"
            self.emit(t, "needs_input", f"{t['id']}: worker ждёт вас в своей сессии ({w})", key=f"blocked:{e['id']}:{w}")

    def adv_needs_human(self, t: dict) -> None:
        if not t.get("pr"):
            return
        pr = self.gh.pr(t["pr"], "state,labels")
        if pr.get("state") == "MERGED":
            return self.finish_merged(t)
        labels = {lb.get("name") for lb in pr.get("labels") or []}
        if self.cfg["labels"]["retry"] in labels:
            t["retry_requested"] = True
            self.do_retry(t)

    def adv_merged(self, t: dict) -> None:
        if not t.get("cleaned"):
            for lst in (t.get("sessions") or {}).values():
                for e in lst:
                    if e["id"] in self.sessions:
                        self.claude.rm(e["id"])
            t["cleaned"] = True

    def adv_cancelled(self, t: dict) -> None:
        self.release_lock(t)
        t["cleaned"] = True

    def do_retry(self, t: dict) -> None:
        t["retry_requested"] = False
        t["retry_pending"] = True
        t["human_reason"] = None
        if t.get("pr"):
            self.gh.remove_label(t["pr"], self.cfg["labels"]["needs_human"])
            self.gh.remove_label(t["pr"], self.cfg["labels"]["retry"])
        self.release_lock(t)
        if t.get("pr") and t.get("plan_approved"):
            self.set_state(t, "plan_review", "повтор: новый worker")
        else:
            self.set_state(t, "new" if not t.get("pr") else "plan_review", "повтор")
        self.emit(t, "retry", f"{t['id']}: повтор поставлен в очередь", key=f"retry:{iso()[:16]}")

    def finish_merged(self, t: dict) -> None:
        self.release_lock(t)
        self.set_state(t, "merged")
        self.emit(t, "merged", f"{t['id']}: смержено в {self.cfg['base_branch']} — PR #{t['pr']}", key="merged")
        self.adv_merged(t)

    def deps_ok(self, t: dict, by_id: dict[str, dict]) -> bool:
        return all((by_id.get(d) or {}).get("state") == "merged" for d in t.get("depends_on") or [])

    def schedule(self, tasks: list[dict]) -> None:
        by_id = {t["id"]: t for t in tasks}
        conc = int(self.cfg["concurrency"])
        active = sum(1 for t in tasks if t.get("state") in IMPLEMENTING or t.get("launching"))
        for t in tasks:
            if active >= conc:
                break
            if t.get("launching"):
                continue
            st = t.get("state")
            if st == "new" and self.deps_ok(t, by_id):
                cx = t.get("complexity", "simple")
                ok = self.launch(t, "planner", planner_prompt(self.store, t, self.cfg),
                                 model=t.get("planner_model") or by_complexity(self.cfg["planner_model"], cx),
                                 effort=by_complexity(self.cfg["planner_effort"], cx))
                if ok:
                    self.set_state(t, "planning")
                    active += 1
                self.store.save_task(t)
            elif st == "plan_review" and t.get("plan_approved"):
                retry = bool(t.pop("retry_pending", False))
                model, effort = worker_choice(self.cfg, t)
                ok = self.launch(t, "worker", worker_prompt(self.store, t, self.cfg, retry=retry),
                                 model=model, effort=effort)
                if ok:
                    self.set_state(t, "in_progress")
                    active += 1
                elif retry:
                    t["retry_pending"] = True
                self.store.save_task(t)
        # очередь ревью: один PR за раз
        lock = self.q.get("review_lock")
        if lock and (by_id.get(lock) or {}).get("state") != "in_review":
            self.q["review_lock"] = lock = None
        if lock:
            return
        ready = sorted((t for t in tasks if t.get("state") == "ready"), key=lambda t: t.get("ready_at") or "")
        for t in ready:
            woke = self.wake(t, "worker", review_prompt(t, self.cfg))
            if not woke:
                model, effort = worker_choice(self.cfg, t)
                woke = self.launch(t, "worker", review_prompt(t, self.cfg) + "\n\nКонтекст задачи: "
                                   + worker_prompt(self.store, t, self.cfg), model=model, effort=effort)
            if woke:
                self.q["review_lock"] = t["id"]
                self.set_state(t, "in_review")
                self.store.save_task(t)
                return

    def collect_usage(self, tasks: list[dict]) -> None:
        for t in tasks:
            if t.get("cleaned"):
                continue
            for role, lst in (t.get("sessions") or {}).items():
                for e in lst:
                    if e.get("session_id"):
                        u = transcript_usage(e["session_id"])
                        if u:
                            t.setdefault("usage", {})[e["session_id"]] = {"role": role, **u}
            self.store.save_task(t)

    def check_batch_done(self, tasks: list[dict]) -> None:
        live = [t for t in tasks if t.get("state") not in TERMINAL and t.get("state") != "needs_human"]
        merged_unannounced = [t for t in tasks if t.get("state") == "merged" and not t.get("batch_announced")]
        if not live and merged_unannounced:
            for t in merged_unannounced:
                t["batch_announced"] = True
                self.store.save_task(t)
            human = [t["id"] for t in tasks if t.get("state") == "needs_human"]
            text = f"пачка завершена: смержено {len(merged_unannounced)}"
            if human:
                text += f", ждут вас: {', '.join(human)}"
            self.emit(None, "batch_done", text)


def plan_checklist(body: str, cfg: dict, t: dict) -> tuple[bool, list[str]]:
    missing = []
    if "<!-- ai-plan" not in body:
        missing.append("маркер <!-- ai-plan -->")

    def section(title: str) -> str:
        m = re.search(r"^##\s+" + re.escape(title) + r".*?$(.*?)(?=^##\s|\Z)", body, re.M | re.S | re.I)
        return m.group(1).strip() if m else ""

    if "- [ ]" not in section("Критерии приёмки") and "- [x]" not in section("Критерии приёмки"):
        missing.append("критерии приёмки (чекбоксы)")
    for s in ("Тесты", "Файлы", "Цель и границы"):
        if not section(s):
            missing.append(s)
    files = re.findall(r"`([^`]+\.[A-Za-z0-9]+)`", section("Файлы"))
    allowed = t.get("allowed_protected_paths") or []
    bad = [f for f in files if path_matches(f, cfg["protected_paths"]) and not path_matches(f, allowed)]
    if bad:
        missing.append("защищённые пути: " + ", ".join(bad))
    return (not missing), missing


# ---------------------------------------------------------------- status / state for UI

STAGE_LABEL = {
    "new": "в очереди", "planning": "планирование", "plan_review": "обсуждение плана",
    "in_progress": "реализация", "ready": "ждёт ревью", "in_review": "ревью",
    "merged": "смержено", "needs_human": "нужен человек", "cancelled": "снята",
}


def build_state(store: Store, sessions: dict[str, dict] | None = None, repo_url: str = "") -> dict:
    cfg = store.config()
    q = store.queue()
    tasks = store.tasks()
    out = []
    for t in tasks:
        st = t.get("state", "new")
        cur = {}
        for role in ("worker", "planner"):
            lst = (t.get("sessions") or {}).get(role) or []
            if lst:
                e = lst[-1]
                live = (sessions or {}).get(e["id"]) or {}
                cur = {"role": role, "id": e["id"], "state": live.get("state"), "waiting": live.get("waitingFor")}
                break
        review = store.review(t["id"]) or {}
        needs = None
        if st == "plan_review" and not t.get("plan_approved"):
            needs = "обсудить и одобрить план"
        elif st == "needs_human":
            needs = t.get("human_reason") or "нужен человек"
        elif cur.get("state") == "blocked":
            needs = f"сессия ждёт ответа ({cur.get('waiting') or 'вопрос'})"
        pr = t.get("pr")
        last_stage = None
        if st not in STAGES:
            for h in reversed(t.get("history") or []):
                if h.get("from") in STAGES:
                    last_stage = STAGES.index(h["from"])
                    break
        out.append({
            "id": t["id"], "title": t.get("title", ""), "state": st, "label": STAGE_LABEL.get(st, st),
            "stage": STAGES.index(st) if st in STAGES else last_stage,
            "complexity": t.get("complexity"), "pr": pr,
            "pr_url": f"{repo_url}/pull/{pr}" if pr and repo_url else None,
            "rounds": review.get("round"), "max_rounds": cfg["max_rounds"],
            "tokens": task_tokens(t), "session": cur, "needs": needs,
            "depends_on": t.get("depends_on") or [], "state_at": t.get("state_at"),
            "launching": bool(t.get("launching")), "review_lock": q.get("review_lock") == t["id"],
        })
    return {
        "version": VERSION, "repo": store.root.name, "repo_url": repo_url, "base": cfg["base_branch"],
        "paused": q.get("paused"), "review_lock": q.get("review_lock"), "concurrency": cfg["concurrency"],
        "monitor_pid": FileLock(store.runs / "monitor.lock").owner(),
        "tasks": out, "events": list(reversed(store.events(40))), "now": iso(),
    }


def fmt_tokens(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def print_status(state: dict) -> None:
    p = state["paused"]
    head = f"aiflow · {state['repo']} · база {state['base']}"
    head += f" · ПАУЗА ({p.get('reason')})" if p else ""
    head += "" if state["monitor_pid"] else " · монитор не запущен"
    print(head)
    if not state["tasks"]:
        print("Задач нет. Добавьте их через /aiflow:intake.")
        return
    rows = [("задача", "состояние", "PR", "раунды", "токены", "сессия")]
    for t in state["tasks"]:
        s = t["session"]
        rows.append((t["id"], t["label"] + (" 🔒" if t["review_lock"] else ""),
                     f"#{t['pr']}" if t["pr"] else "—",
                     f"{t['rounds']}/{t['max_rounds']}" if t["rounds"] else "—",
                     fmt_tokens(t["tokens"]),
                     f"{s.get('role')} {s.get('id')} {s.get('state') or ''}".strip() if s else "—"))
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))
    waiting = [t for t in state["tasks"] if t["needs"]]
    if waiting:
        print("\nЖдут вас:")
        for t in waiting:
            print(f"  {t['id']}: {t['needs']}" + (f" — {t['pr_url']}" if t["pr_url"] else ""))


# ---------------------------------------------------------------- commands

def cmd_init(args, root: Path) -> int:
    store = Store(root)
    store.runs.mkdir(exist_ok=True)
    created = []
    if not (store.runs / "config.json").exists():
        write_json(store.runs / "config.json", DEFAULT_CONFIG)
        created.append("runs/config.json")
    gi = root / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    add = [x for x in ("runs/", ".claude/worktrees/") if x not in lines]
    if add:
        gi.write_text("\n".join(lines + add) + "\n", encoding="utf-8")
        created.append(".gitignore (+" + ", ".join(add) + ")")
    for src, dst in (("reviewer.md", ".opencode/agent/reviewer.md"),
                     ("ci.yml", ".github/workflows/ci.yml"),
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
    print("Готово:\n  " + "\n  ".join(created))
    print("\nДальше:\n"
          "  1. Впишите модель ревьюера в runs/config.json → reviewer_model (opencode models deepseek).\n"
          f"  2. Заполните .github/workflows/ci.yml и сделайте job обязательной проверкой для {store.config()['base_branch']}.\n"
          "  3. Добавьте в .claude/settings.json allow-правила для сборки и тестов проекта.\n"
          "  4. Закоммитьте .claude/settings.json, .opencode/, .github/, docs/.\n"
          "  5. aiflow doctor")
    return 0


def setup_desktop_preview(root: Path, store: Store) -> str:
    """Добавляет панель aiflow в .claude/launch.json — Browser pane вкладки Code в Claude Desktop."""
    port = int(store.config().get("ui_port") or 8765)
    entries = [
        # подключиться к панели, которую уже поднял монитор
        {"name": "aiflow", "url": f"http://127.0.0.1:{port}", "port": port},
        # или запустить панель отдельно, если монитор не работает
        {"name": "aiflow (без монитора)", "runtimeExecutable": sys.executable,
         "runtimeArgs": [str(Path(__file__).resolve()), "ui", "--port", str(port)],
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
    cx = raw.get("complexity", "simple")
    if cx not in ("simple", "complex"):
        raise AiflowError(f"{tid}: complexity должен быть simple или complex")
    text = str(raw.get("text") or "").strip()
    if not text:
        raise AiflowError(f"{tid}: пустой text")
    effort = raw.get("worker_effort") or None
    if effort is not None and effort not in EFFORTS:
        raise AiflowError(f"{tid}: worker_effort должен быть одним из {', '.join(EFFORTS)}")
    return {
        "id": tid, "title": str(raw.get("title") or tid).strip(), "text": text, "complexity": cx,
        "depends_on": [str(d) for d in raw.get("depends_on") or []],
        "auto_approve_plan": bool(raw.get("auto_approve_plan")) and cx == "simple",
        "ui": raw.get("ui") or None,
        "planner_model": raw.get("planner_model") or None,
        "worker_model": raw.get("worker_model") or None,
        "worker_effort": effort,
        "allowed_protected_paths": [],
        "state": "new", "pr": None, "sessions": {"planner": [], "worker": []},
        "usage": {}, "created_at": iso(), "state_at": iso(),
        "history": [{"at": iso(), "event": "created", "by": "intake"}],
    }


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
        for t in tasks:
            old = store.runs / t["id"]
            if old.exists():  # завершённая задача с тем же id уходит в архив вместе с её отчётами
                arch = store.runs / ".archive" / f"{t['id']}-{now().strftime('%Y%m%d-%H%M%S')}"
                arch.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old, arch)
            store.save_task(t)
            store.event("enqueued", t["id"], f"{t['id']}: поставлена в очередь ({t['complexity']})")
    finally:
        lock.release()
    print(f"В очереди: {len(tasks)} — " + ", ".join(ids))
    if not FileLock(store.runs / "monitor.lock").owner():
        print("Монитор не запущен: в сессии оркестратора выполните /aiflow:resume.")
    return 0


def cmd_report(args, root: Path) -> int:
    details = Path(args.details_file).read_text(encoding="utf-8") if args.details_file else ""
    rep = write_report(root, args.id, args.role, args.status, args.pr, args.summary or "", details)
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


def run_tick_now(store: Store) -> list[dict]:
    lock = FileLock(store.runs / "tick.lock", stale_minutes=15)
    if not lock.acquire(wait_seconds=60):
        raise AiflowError("tick уже выполняется")
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


def cmd_cancel(args, root: Path) -> int:
    """Снять задачи, которые ещё не начались (state new). Начатые снимаются закрытием PR."""
    store = Store(root)
    if not store.initialized:
        raise AiflowError("репозиторий не инициализирован: aiflow init")
    ids = list(dict.fromkeys(args.ids))
    lock = FileLock(store.runs / "tick.lock", stale_minutes=15)
    if not lock.acquire(wait_seconds=120):
        raise AiflowError("очередь занята дольше 2 минут, повторите позже")
    try:
        tasks = {t["id"]: t for t in store.tasks()}
        for tid in ids:
            t = tasks.get(tid)
            if not t:
                raise AiflowError(f"нет задачи {tid}")
            if t.get("state") != "new" or t.get("launching") or t.get("pr"):
                raise AiflowError(f"{tid} уже в работе ({t.get('state')}): закройте её PR, aiflow снимет задачу сам")
        for t in tasks.values():
            if t["id"] in ids or t.get("state") in TERMINAL:
                continue
            blocked = [d for d in t.get("depends_on") or [] if d in ids]
            if blocked:
                raise AiflowError(f"от {', '.join(blocked)} зависит {t['id']}: снимите её тоже или поставьте заново без зависимости")
        for tid in ids:
            t = tasks[tid]
            t.setdefault("history", []).append({"at": iso(), "from": t["state"], "to": "cancelled", "note": "aiflow cancel"})
            t["state"], t["state_at"], t["cleaned"] = "cancelled", iso(), True
            store.save_task(t)
            store.event("cancelled", tid, f"{tid}: снята")
    finally:
        lock.release()
    print("Сняты: " + ", ".join(ids))
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
             f"repos/{{owner}}/{{repo}}/pulls/{args.pr}/comments/{args.comment_id}/replies",
             "-f", f"body={body}"], cwd=Path.cwd(), timeout=60)
    if p.returncode != 0:
        raise AiflowError("ответ в тред не отправлен: " + (p.stderr or p.stdout).strip()[:300])
    print(f"Ответ отправлен в тред {args.comment_id}.")
    return 0


def cmd_tick(args, root: Path) -> int:
    for ev in run_tick_now(Store(root)):
        print(f"[aiflow] {ev['text']}")
    return 0


def cmd_run(args, root: Path) -> int:
    store = Store(root)
    if not store.initialized:
        print("[aiflow] репозиторий не инициализирован: выполните aiflow init", flush=True)
        return 1
    mon = FileLock(store.runs / "monitor.lock", stale_minutes=0)
    if not mon.acquire():
        return 0  # уже работает другой монитор — молча выходим
    wake = threading.Event()
    srv = None
    import signal

    def _stop(*_):
        raise KeyboardInterrupt
    for sig in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _stop)
    try:
        cfg = store.config()
        url = ""
        if cfg.get("ui_port") and not args.no_ui:
            from aiflow_ui import start_server
            try:
                srv, url = start_server(store, int(cfg["ui_port"]), wake)
            except OSError:
                url = ""  # порт занят: панель уже открыта отдельным aiflow ui
        if args.events:
            print(f"[aiflow] монитор запущен{'; панель: ' + url if url else ''}", flush=True)
        while True:
            try:
                for ev in run_tick_now(store):
                    if args.events:
                        print(f"[aiflow] {ev['text']}", flush=True)
            except AiflowError as e:
                if args.events:
                    print(f"[aiflow] ошибка тика: {e}", flush=True)
            except Exception as e:  # монитор не должен падать из-за одного тика
                store.event("error", None, f"сбой тика: {e!r}")
            wake.wait(timeout=max(10, int(store.config().get("tick_seconds", 45))))
            wake.clear()
    except KeyboardInterrupt:
        return 0
    finally:
        if srv:
            srv.shutdown()
        mon.release()


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
    ok = _kill(int(info.get("pid") or 0))
    (store.runs / "ui.pid").unlink(missing_ok=True)
    return ok


def cmd_ui(args, root: Path) -> int:
    store = Store(root)
    if args.stop:
        print("Панель остановлена." if stop_ui(store) else
              "Отдельно запущенной панели нет. Панель монитора останавливается вместе с ним: aiflow stop.")
        return 0
    port = args.port or int(store.config().get("ui_port") or 8765)
    url = f"http://127.0.0.1:{port}/"
    if not _port_busy(port):
        if args.detach:
            py = Path(sys.executable)
            if os.name == "nt" and (py.parent / "pythonw.exe").is_file():
                py = py.parent / "pythonw.exe"  # без консольного окна
            kw = {"cwd": str(root), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                  "stdin": subprocess.DEVNULL}
            if os.name == "nt":
                kw["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
            else:
                kw["start_new_session"] = True
            subprocess.Popen([str(py), str(Path(__file__)), "ui", "--port", str(port)], **kw)
            for _ in range(20):
                if _port_busy(port):
                    break
                time.sleep(0.25)
        else:
            from aiflow_ui import start_server
            srv, url = start_server(store, port, threading.Event())
            write_json(store.runs / "ui.pid", {"pid": os.getpid(), "port": port, "at": iso()})
            print(f"Панель aiflow: {url}  (остановить: Ctrl+C или aiflow ui --stop)", flush=True)
            if args.open:
                import webbrowser
                webbrowser.open(url)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                srv.shutdown()
            finally:
                (store.runs / "ui.pid").unlink(missing_ok=True)
            return 0
    print(f"Панель aiflow: {url}  (остановить: aiflow ui --stop)")
    if args.open:
        import webbrowser
        webbrowser.open(url)
    return 0


def cmd_stop(args, root: Path) -> int:
    store = Store(root)
    pid = FileLock(store.runs / "monitor.lock").owner()
    stopped = []
    if pid and _kill(pid):
        (store.runs / "monitor.lock").unlink(missing_ok=True)
        store.event("stopped", None, "монитор остановлен командой aiflow stop")
        stopped.append("монитор")
    if stop_ui(store):
        stopped.append("панель")
    if stopped:
        print("Остановлено: " + ", ".join(stopped) + ". Фоновые сессии Claude продолжают работать; "
              "очередь продолжит /aiflow:resume.")
    else:
        print("Монитор и панель не запущены.")
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
    queued = sum(1 for t in tasks if t.get("state") in ("new", "ready"))
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
                   choices=["plan_ready", "ready_for_review", "approved", "limit_reached", "error", "blocked"])
    r.add_argument("--pr", type=int)
    r.add_argument("--summary")
    r.add_argument("--details-file")
    s = sub.add_parser("status", help="состояние очереди")
    s.add_argument("--json", action="store_true")
    sub.add_parser("tick", help="один проход")
    rn = sub.add_parser("run", help="цикл (запускается монитором плагина)")
    rn.add_argument("--events", action="store_true")
    rn.add_argument("--no-ui", action="store_true")
    p = sub.add_parser("pause")
    p.add_argument("reason", nargs="?")
    sub.add_parser("resume")
    rt = sub.add_parser("retry")
    rt.add_argument("id")
    cn = sub.add_parser("cancel", help="снять задачи, которые ещё не начались")
    cn.add_argument("ids", nargs="+")
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
    sub.add_parser("stop", help="остановить монитор и панель (сессии Claude продолжают работать)")
    sub.add_parser("statusline", help="одна строка для statusLine")
    args = ap.parse_args(argv)

    try:
        try:
            root = repo_root()
        except AiflowError:
            root = None
        if args.cmd in ("doctor", "statusline"):
            return {"doctor": cmd_doctor, "statusline": cmd_statusline}[args.cmd](args, root)
        if root is None:
            raise AiflowError("запустите aiflow внутри git-репозитория")
        handlers = {
            "init": cmd_init, "enqueue": cmd_enqueue, "report": cmd_report, "status": cmd_status,
            "tick": cmd_tick, "run": cmd_run, "pause": cmd_pause, "resume": cmd_resume,
            "retry": cmd_retry, "cancel": cmd_cancel, "approve": cmd_approve, "reply": cmd_reply, "review": cmd_review,
            "ui": cmd_ui, "stop": cmd_stop,
        }
        return handlers[args.cmd](args, root)
    except AiflowError as e:
        print(f"aiflow: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
