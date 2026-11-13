#!/usr/bin/env python3
"""
Один раунд AI-ревью pull request'а (вызывается как `aiflow review --pr N`).

Порядок: PR открыт → пауза очереди → лимит раундов → комментарии человека → CI → защищённые пути →
размер изменения (задачи без плана) → ревьюер DeepSeek через opencode → inline-ревью → мерж.
Дифф, который ревьюер уже одобрил, после rebase повторно не ревьюится (сверка по git patch-id).
Перед мержем: очередь не на паузе, ветка PR не отстаёт от базовой, мержится ровно проверенный коммит.
Ревью публикуется от вашего аккаунта gh (review_identity: self, событие COMMENT — GitHub не даёт
одобрить свой PR) или от бота с отдельным токеном (review_identity: bot, APPROVE/REQUEST_CHANGES).
Печатает JSON для worker'а. Итоговые статусы записываются в runs/<task>/report.json сами.
Неизвестное и непроверенное считается блокирующим (fail-closed).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aiflow  # noqa: E402

REVIEW_MARKER = "<!-- ai-review -->"
REPLY_MARKER = "<!-- ai-review-reply -->"
HUMAN_MARKER = "<!-- human -->"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PARSE_ATTEMPTS = 3
TERMINAL = {"approved", "limit_reached", "already_closed", "error"}
# статусы, о которых узнаёт очередь (runs/<task>/report.json): итоговые и те, где решает не worker
REPORTED = TERMINAL | {"paused", "no_ci"}
HUMAN_INPUT_REPEATS = 3  # столько раз подряд возвращаем один и тот же комментарий человека

CTX: dict = {"root": None, "task": None, "pr": None, "round": None}


def set_phase(phase: str | None, **detail) -> None:
    """Чем сейчас занят раунд — для панели: ожидание CI, ревьюер, мерж. None — раунд закончен.
    since — когда фаза началась: подробности (что читает ревьюер) обновляются, отсчёт времени — нет."""
    root, task = CTX["root"], CTX["task"]
    if not (root and task) or CTX.get("phase") == (phase, detail):
        return
    if (CTX.get("phase") or (None,))[0] != phase:
        CTX["phase_since"] = aiflow.iso()
    CTX["phase"] = (phase, detail)
    path = root / "runs" / task["id"] / "phase.json"
    try:
        if phase is None:
            path.unlink(missing_ok=True)
        else:
            aiflow.write_json(path, {"pid": os.getpid(), "at": aiflow.iso(), "since": CTX["phase_since"],
                                     "phase": phase, "pr": CTX["pr"], **detail})
    except OSError:
        pass  # подсказка для панели не должна ломать ревью


def finish(status: str, code: int = 0, **data) -> None:
    out = {"status": status, **data}
    root, task = CTX["root"], CTX["task"]
    set_phase(None)
    if root and task:
        try:
            aiflow.write_json(root / "runs" / task["id"] / "review.json",
                              {"at": aiflow.iso(), "pr": CTX["pr"], "round": CTX["round"], **out})
            if status in REPORTED:
                aiflow.write_report(root, task["id"], "worker", status, CTX["pr"],
                                    data.get("message") or data.get("summary") or status)
        except Exception as e:  # отчёт не должен ломать вывод для worker'а
            out["report_error"] = str(e)
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    sys.exit(code)


def load_token() -> str:
    token = os.environ.get("REVIEW_BOT_TOKEN", "").strip()
    if not token:
        f = Path.home() / ".config" / "ai-review" / "token"
        if f.is_file():
            token = f.read_text(encoding="utf-8").strip()
    if not token:
        finish("error", 1, message="нет токена бота: REVIEW_BOT_TOKEN или ~/.config/ai-review/token")
    return token


IDENTITIES = ("self", "bot")


class Runner:
    def __init__(self, gh: str, opencode: str, token: str | None, cwd: Path):
        """token=None — gh работает от текущей авторизации пользователя (review_identity: self)."""
        self.gh, self.opencode, self.cwd = gh, opencode, cwd
        base = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN", "REVIEW_BOT_TOKEN")}
        if token:
            self.gh_env = {**base, "GH_TOKEN": token}
        else:
            self.gh_env = {k: v for k, v in os.environ.items() if k != "REVIEW_BOT_TOKEN"}
        self.plain_env = base

    def run(self, cmd, *, env, check=True, timeout=180, input=None):
        # run_tree: по таймауту убивается всё дерево процессов. opencode на Windows запускается через
        # opencode.CMD, и без этого зависший ревьюер переживал таймаут, а aiflow review ждал его вечно.
        proc = aiflow.run_tree(cmd, cwd=self.cwd, timeout=timeout, env=env, input=input)
        if check and proc.returncode != 0:
            raise RuntimeError(f"{Path(cmd[0]).stem} {' '.join(cmd[1:3])}: {(proc.stderr or proc.stdout).strip()[:500]}")
        return proc

    @property
    def repo(self) -> str:
        if not getattr(self, "_repo", None):
            self._repo = aiflow.gh_repo(self.cwd, self.gh_env)
        return self._repo

    def gh_run(self, *args, check=True, timeout=180):
        p = aiflow.gh_with_retry(lambda: self.run([self.gh, *args], env=self.gh_env, check=False, timeout=timeout), args)
        if check and p.returncode != 0:
            raise RuntimeError(f"gh {' '.join(map(str, args[:2]))}: {(p.stderr or p.stdout).strip()[:500]}")
        return p

    def gh_json(self, *args):
        return json.loads(self.gh_run(*args).stdout or "null")


# ------------------------------------------------------------------ helpers

def find_task(root: Path, pr: int) -> dict | None:
    for t in aiflow.Store(root).tasks():
        if t.get("pr") == pr:
            return t
    return None


def bot_reviews(r: Runner, pr: int, login: str) -> list[dict]:
    out = r.gh_run("api", f"repos/{r.repo}/pulls/{pr}/reviews", "--paginate",
                   "--jq", f'.[] | select(.user.login == "{login}") | {{id, state, body, submitted_at}}').stdout
    items = [json.loads(x) for x in out.splitlines() if x.strip()]
    return [i for i in items if REVIEW_MARKER in (i.get("body") or "")]


def review_comments(r: Runner, pr: int) -> list[dict]:
    out = r.gh_run("api", f"repos/{r.repo}/pulls/{pr}/comments", "--paginate",
                   "--jq", ".[] | {id, in_reply_to_id, path, line, body, created_at, user: .user.login}").stdout
    return [json.loads(x) for x in out.splitlines() if x.strip()]


THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id isResolved comments(first: 1) { nodes { databaseId author { login } } } }
      }
    }
  }
}"""
RESOLVE_MUTATION = "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { id } } }"
SEVERITY_TAG = re.compile(r"\*\*(blocking|nit|блокирующее|необязательное)\*\*")


def review_threads(r: Runner, pr: int) -> dict[int, dict]:
    """{id первого комментария треда: {"thread": node id, "resolved": bool}}."""
    owner, _, name = r.repo.partition("/")
    out: dict[int, dict] = {}
    after = None
    for _ in range(20):
        args = ["api", "graphql", "-f", f"query={THREADS_QUERY}", "-F", f"owner={owner}", "-F", f"name={name}",
                "-F", f"number={pr}"]
        if after:
            args += ["-F", f"after={after}"]
        data = json.loads(r.gh_run(*args).stdout or "{}")
        rt = (((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("reviewThreads") or {}
        for n in rt.get("nodes") or []:
            first = ((n.get("comments") or {}).get("nodes") or [{}])[0]
            if first.get("databaseId"):
                out[int(first["databaseId"])] = {"thread": n["id"], "resolved": bool(n.get("isResolved"))}
        info = rt.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info.get("endCursor")
    return out


def open_threads(threads: list[dict], states: dict[int, dict], login: str) -> list[dict]:
    """Нерешённые треды замечаний ревьюера из прошлых раундов вместе с ответами в них."""
    res = []
    for c in threads:
        if c.get("in_reply_to_id") or c.get("user") != login or not SEVERITY_TAG.match(c.get("body") or ""):
            continue
        st = states.get(int(c["id"]))
        if not st or st["resolved"]:
            continue
        replies = [x for x in threads if x.get("in_reply_to_id") == c["id"]]
        res.append({"comment_id": c["id"], "thread": st["thread"], "path": c.get("path"), "line": c.get("line"),
                    "body": strip_markers(c.get("body") or ""),
                    "replies": [f"{x.get('user')}: {strip_markers(x.get('body') or '')}" for x in replies]})
    return res


def format_open_threads(items: list[dict]) -> str:
    if not items:
        return ""
    parts = ["# Open threads from earlier rounds", "",
             "For each thread, return a verdict in the field threads (comment_id is the number in square brackets).", ""]
    for t in items:
        parts.append(f"[{t['comment_id']}] `{t['path']}:{t['line']}` {t['body']}")
        parts += [f"  ↳ {x}" for x in t["replies"]] or ["  ↳ (no reply from the worker)"]
        parts.append("")
    return "\n".join(parts)


def answer_threads(r: Runner, pr: int, items: list[dict], verdicts: list[dict], tmp: Path) -> list[dict]:
    """Ответить в каждом открытом треде: «Accepted: …» и закрыть тред либо «Not accepted: …»
    и оставить открытым. Возвращает треды, которые остаются открытыми: они блокируют одобрение."""
    by_id = {v["comment_id"]: v for v in verdicts}
    still_open = []
    for t in items:
        v = by_id.get(int(t["comment_id"]))
        if v is None:
            still_open.append({**t, "reason": "ревьюер не дал вердикт по треду"})
            continue
        accepted = v["accepted"]
        text = v["text"] or ("исправлено" if accepted else "замечание остаётся в силе")
        p = tmp / f"reply-{t['comment_id']}.md"
        p.write_text(("Accepted: " if accepted else "Not accepted: ") + text, encoding="utf-8")
        r.gh_run("api", "--method", "POST", f"repos/{r.repo}/pulls/{pr}/comments/{t['comment_id']}/replies",
                 "-F", f"body=@{p}", check=False)
        if accepted:
            r.gh_run("api", "graphql", "-f", f"query={RESOLVE_MUTATION}", "-F", f"id={t['thread']}", check=False)
        else:
            still_open.append({**t, "reason": text})
    return still_open


def strip_markers(text: str) -> str:
    for m in (REVIEW_MARKER, REPLY_MARKER, HUMAN_MARKER):
        text = text.replace(m, "")
    return text.strip()


def build_history(reviews: list[dict], threads: list[dict], comments: list[dict], limit: int = 30000) -> str:
    items = [(rv.get("submitted_at") or "", "Reviewer (round result)", rv.get("body") or "") for rv in reviews]
    roots = {c["id"]: c for c in threads if not c.get("in_reply_to_id")}
    for root_id, c in roots.items():
        replies = [x for x in threads if x.get("in_reply_to_id") == root_id]
        lines = [f"`{c.get('path')}:{c.get('line')}` {c.get('user')}: {strip_markers(c.get('body') or '')}"]
        lines += [f"  ↳ {x.get('user')}: {strip_markers(x.get('body') or '')}" for x in replies]
        items.append((c.get("created_at") or "", "Thread", "\n".join(lines)))
    for c in comments:
        body = c.get("body") or ""
        if REPLY_MARKER in body:
            items.append((c.get("createdAt") or "", "Worker", body))
        elif body.lstrip().startswith(HUMAN_MARKER):
            items.append((c.get("createdAt") or "", "Human", body))
    if not items:
        return "This is the first round. There is no history."
    items.sort(key=lambda x: x[0])
    text = "\n\n".join(f"### {who} ({ts})\n\n{strip_markers(body)}" for ts, who, body in items)
    return text if len(text) <= limit else "[… the start of the history is cut]\n" + text[-limit:]


def diff_lines(diff: str) -> dict[str, set[int]]:
    """Номера строк новой версии, на которые GitHub принимает inline-комментарии."""
    out: dict[str, set[int]] = {}
    cur, new_ln = None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            cur = None if path == "/dev/null" else (path[2:] if path.startswith("b/") else path)
            if cur is not None:
                out.setdefault(cur, set())
        elif line.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            new_ln = int(m.group(1)) if m else 0
        elif cur is None or line.startswith(("--- ", "diff --git", "index ")):
            continue
        elif line.startswith("+"):
            out[cur].add(new_ln)
            new_ln += 1
        elif line.startswith(" "):
            out[cur].add(new_ln)
            new_ln += 1
        elif line.startswith("-") or line.startswith("\\"):
            continue
    return out


def human_comments(comments: list[dict], reviews: list[dict], floor: str | None) -> list[dict]:
    """Комментарии человека (с маркером <!-- human --> или обычные, не от бота), на которые worker ещё не
    ответил: позже последнего раунда ревью, последнего ответа worker'а и начала реализации."""
    marks = [rv.get("submitted_at") or "" for rv in reviews]
    marks += [c.get("createdAt") or "" for c in comments if REPLY_MARKER in (c.get("body") or "")]
    if floor:
        marks.append(floor)
    watermark = max(marks, key=lambda m: aiflow.parse_iso(m) or aiflow.dt.datetime.min.replace(tzinfo=aiflow.dt.timezone.utc)) if marks else ""
    return [{"author": (c.get("author") or {}).get("login"), "text": strip_markers(c.get("body") or ""),
             "at": c.get("createdAt")}
            for c in comments
            if ((c.get("body") or "").lstrip().startswith(HUMAN_MARKER) or aiflow.is_human_comment(c))
            and aiflow.after(c.get("createdAt"), watermark)]


def format_amendments(task: dict | None) -> str:
    items = (task or {}).get("amendments") or []
    if not items:
        return ""
    return ("# User amendments to the task\n\nThey are a part of the task, equal to the plan: changes for them are "
            "not work outside the plan. Check that they are done.\n\n" + aiflow.amend_list(items) + "\n")


BIG_DIFF_CHARS = 30000  # больше — вывод gh pr diff у ревьюера может обрезаться: требуем подтверждения


def build_brief(n: int, pr: dict, base: str, history: str, big_diff: bool) -> str:
    """Задание ревьюеру. План и дифф он читает сам (gh pr view / gh pr diff): так aiflow не гоняет
    через себя сотни килобайт, а модель читает столько, сколько нужно. Здесь — только то, чего в PR
    не видно напрямую: раунд, история ревью, дополнения и открытые треды."""
    read = (f"# PR #{n}: {pr.get('title', '')}\n\nBase branch: {base}. The current directory is the PR branch.\n"
            f"Read yourself: the PR description with the plan (`gh pr view {n}`) and the changes (`gh pr diff {n}`). "
            f"If the output is truncated, read file by file: `gh pr diff {n} --name-only`, then "
            f"`git diff origin/{base}...HEAD -- <file>`. For the code around the changes, read the repository files.\n")
    if big_diff:
        read += ("The diff is large: read all of it (file by file) and return \"diff_fully_read\": true. "
                 "If you could not, return false.\n")
    return read + f"\n# Review history\n\n{history}\n"


def extract_json(text: str) -> dict | None:
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)[::-1]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for chunk in candidates:
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


AGENT = "aiflow-reviewer"
REVIEWER_PERMISSION = {
    "edit": "deny", "webfetch": "deny",
    "bash": {"*": "deny", "git diff*": "allow", "git log*": "allow", "git show*": "allow",
             "gh pr view*": "allow", "gh pr diff*": "allow"},
}


def reviewer_prompt(root: Path) -> str:
    """Инструкции ревьюера: своя версия проекта (.opencode/aiflow-reviewer.md), иначе шаблон
    плагина. Старая копия .opencode/agent/reviewer.md от aiflow init не используется: в ней
    устаревший формат ответа. Frontmatter отбрасывается."""
    for p in (root / ".opencode" / "aiflow-reviewer.md", aiflow.TEMPLATES / "reviewer.md"):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if text.startswith("---"):
            text = text.split("---", 2)[2] if text.count("---") >= 2 else text
        return text.strip() + "\n"
    raise RuntimeError("нет инструкций ревьюера: шаблон плагина не найден")


def reviewer_config(r: Runner, tmp: Path) -> dict:
    """Окружение opencode с агентом aiflow-reviewer из отдельного конфига: в репозиторий ничего
    не пишется, и агент не путается с собственным reviewer проекта (.opencode/agent(s)/reviewer.md)."""
    prompt = tmp / "aiflow-reviewer.md"
    prompt.write_text(reviewer_prompt(r.cwd), encoding="utf-8")
    cfg: dict = {}
    user_cfg = r.plain_env.get("OPENCODE_CONFIG")
    if user_cfg:
        try:
            cfg = json.loads(Path(user_cfg).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    cfg.setdefault("agent", {})[AGENT] = {
        "description": "Read-only ревьюер pull request'ов aiflow. Возвращает только JSON.",
        "mode": "primary",
        "prompt": "{file:" + prompt.as_posix() + "}",
        "permission": REVIEWER_PERMISSION,
    }
    path = tmp / "opencode.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return {**r.plain_env, "OPENCODE_CONFIG": str(path)}


class ReviewerStalled(RuntimeError):
    pass


def stream_events(cmd, *, env, cwd, input: str, timeout: float, idle: float, on_event=None) -> tuple[int, list[dict], str, str]:
    """Запустить opencode run --format json и читать его события по мере появления.
    Возвращает (код выхода, события, прочий stdout, stderr). Нет событий дольше idle секунд — процесс
    завис: дерево убивается и бросается ReviewerStalled; общий предел timeout — subprocess.TimeoutExpired."""
    group = aiflow.NO_WINDOW if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **group)
    lines: Queue = Queue()
    err: list[str] = []

    def pump(stream, sink) -> None:
        try:
            for line in stream:
                sink(line)
        except (OSError, ValueError):
            pass
        finally:
            sink(None)

    threading.Thread(target=pump, args=(proc.stdout, lines.put), daemon=True).start()
    threading.Thread(target=pump, args=(proc.stderr, lambda x: err.append(x) if x is not None else None), daemon=True).start()
    try:
        proc.stdin.write(input)
        proc.stdin.close()
    except (OSError, ValueError):
        pass
    events, other = [], []
    started = last = time.time()
    while True:
        now = time.time()
        if now - started > timeout:  # проверяется на каждом шаге: поток событий может не умолкать
            aiflow.kill_tree(proc)
            raise subprocess.TimeoutExpired(cmd, timeout)
        if idle and now - last > idle:
            aiflow.kill_tree(proc)
            raise ReviewerStalled(f"ревьюер opencode молчит {int(idle // 60)} мин и был остановлен")
        try:
            line = lines.get(timeout=1)
        except Empty:
            continue
        if line is None:
            break
        last = time.time()
        text = ANSI_RE.sub("", line).strip()
        if not text:
            continue
        try:
            ev = json.loads(text)
        except json.JSONDecodeError:
            ev = None
        if isinstance(ev, dict) and ev.get("type"):
            events.append(ev)
            if on_event:
                on_event(ev)
        else:
            other.append(text)
    try:
        code = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        aiflow.kill_tree(proc)
        code = -1
    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except (OSError, ValueError):
            pass
    return code, events, "\n".join(other), ANSI_RE.sub("", "".join(err)).strip()


def events_usage(events: list[dict]) -> dict:
    """Расход и цена прогона opencode — сумма по шагам (step_finish). Цену считает сам opencode."""
    u = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0, "steps": 0}
    for ev in events:
        part = ev.get("part") or {}
        if ev.get("type") != "step_finish" or not isinstance(part.get("tokens"), dict):
            continue
        t = part["tokens"]
        cache = t.get("cache") or {}
        u["input"] += int(t.get("input") or 0)
        u["output"] += int(t.get("output") or 0) + int(t.get("reasoning") or 0)  # рассуждения оплачиваются как выход
        u["cache_read"] += int(cache.get("read") or 0)
        u["cache_write"] += int(cache.get("write") or 0)
        u["cost"] += float(part.get("cost") or 0)
        u["steps"] += 1
    return u


def events_text(events: list[dict]) -> str:
    """Ответ модели: текстовые части после последнего вызова инструмента."""
    texts: list[str] = []
    for ev in events:
        part = ev.get("part") or {}
        if ev.get("type") == "tool_use":
            texts = []
        elif ev.get("type") == "text" and part.get("text"):
            texts.append(str(part["text"]))
    return "\n".join(texts)


def tool_note(part: dict) -> str:
    """Короткая подпись действия ревьюера для панели."""
    tool = str(part.get("tool") or "")
    args = ((part.get("state") or {}).get("input") or {})
    if tool == "read" and args.get("filePath"):
        return "читает " + Path(str(args["filePath"])).name
    if tool == "bash" and args.get("command"):
        return "выполняет " + " ".join(str(args["command"]).split())[:48]
    if tool in ("grep", "glob") and args.get("pattern"):
        return "ищет " + str(args["pattern"])[:40]
    return tool or "думает"


def record_reviewer(root: Path | None, task: dict | None, entry: dict) -> None:
    """Дописать прогон ревьюера в runs/<id>/reviewer.json: по нему очередь считает расход, панель
    открывает сессию opencode."""
    if not (root and task):
        return
    path = root / "runs" / task["id"] / "reviewer.json"
    try:
        data = aiflow.read_json(path, {}) or {}
        runs = data.get("runs") if isinstance(data.get("runs"), list) else []
        runs.append(entry)
        aiflow.write_json(path, {"runs": runs[-200:]})
    except OSError:
        pass


def call_reviewer(r: Runner, pr: int, rnd: int, max_rounds: int, model: str, brief: str, tmp: Path,
                  idle_minutes: float = 5, timeout_minutes: float = 45) -> dict:
    # Задание — через stdin: история ревью бывает длиннее лимита командной строки Windows (~32k),
    # а файл во временной папке лежит вне проекта, и opencode отклоняет его чтение (external_directory).
    message = (f"Review pull request #{pr}, round {rnd} of {max_rounds}. The brief follows this message: read the "
               "plan and the diff yourself as it says. Reply with exactly one JSON object following your instructions.")
    materials = brief
    env = reviewer_config(r, tmp)
    cmd = [r.opencode, "run", message, "--agent", AGENT, "--format", "json",
           "--title", f"aiflow: ревью PR #{pr}, раунд {rnd}"]
    if model:
        cmd += ["--model", model]
    last = err = ""
    for attempt in range(1, PARSE_ATTEMPTS + 1):
        seen: list[dict] = []
        started = aiflow.iso()

        def progress(ev: dict) -> None:
            seen.append(ev)
            part = ev.get("part") or {}
            doing = tool_note(part) if ev.get("type") == "tool_use" else None
            if doing or ev.get("type") == "step_finish":
                u = events_usage(seen)
                set_phase("reviewer", round=rnd, session=ev.get("sessionID"), steps=u["steps"],
                          tokens=u["input"] + u["output"] + u["cache_write"],
                          doing=doing or (CTX.get("phase") or (None, {}))[1].get("doing"))

        try:
            code, events, other, err = stream_events(cmd, env=env, cwd=r.cwd, input=materials, timeout=timeout_minutes * 60,
                                                    idle=idle_minutes * 60, on_event=progress)
        finally:
            # расход записывается и за неудавшийся прогон: токены на него потрачены
            usage = events_usage(seen)
            session = next((ev.get("sessionID") for ev in seen if ev.get("sessionID")), None)
            if usage["steps"] or session:
                record_reviewer(CTX["root"], CTX["task"], {
                    "session": session, "round": rnd, "attempt": attempt, "at": started, "pr": pr,
                    "model": model or "opencode", "dir": str(r.cwd), **usage})
                CTX["reviewer_session"] = session
        last = events_text(events) or other
        data = extract_json(last)
        if data is not None and "comments" in data:
            return data
        if code != 0 and not last.strip() and "--pure" not in cmd:
            # opencode упал до ответа модели. Бывает, что в каталоге проекта ломается один из
            # встроенных плагинов opencode; ревьюеру они не нужны — повторяем без них.
            cmd = cmd[:2] + ["--pure"] + cmd[2:]
    tail = last[-2000:] if last.strip() else "(пусто)"
    raise RuntimeError("ревьюер не вернул валидный JSON. Конец вывода:\n" + tail
                       + (f"\nstderr opencode: {err[-800:]}" if err else ""))


SEVERITY = {"blocking": "blocking", "nit": "nit", "minor": "nit"}


def thread_verdicts(data: dict) -> list[dict]:
    out = []
    for v in data.get("threads") or []:
        if not isinstance(v, dict):
            continue
        try:
            cid = int(v.get("comment_id"))
        except (TypeError, ValueError):
            continue
        out.append({"comment_id": cid, "accepted": v.get("accepted") is True, "text": str(v.get("text") or "").strip()})
    return out


def normalize(data: dict, truncated: bool) -> tuple[str, list[dict], str]:
    comments = []
    for c in data.get("comments") or []:
        if not isinstance(c, dict):
            continue
        sev = SEVERITY.get(str(c.get("severity") or "").lower(), "blocking")  # fail-closed
        try:
            line = int(c["line"]) if c.get("line") is not None else None
        except (TypeError, ValueError):
            line = None
        text = str(c.get("text", "")).strip()
        if text:
            comments.append({"severity": sev, "file": str(c.get("file") or ""), "line": line, "text": text})
    if truncated and data.get("diff_fully_read") is not True:
        comments.append({"severity": "blocking", "file": "", "line": None,
                         "text": "Дифф большой, а ревьюер не подтвердил, что прочитал его целиком. "
                                 "Разбейте изменения или повторите раунд."})
    # nit тоже исправляется: одобрение — только когда замечаний нет совсем
    verdict = "changes" if comments else "approve"
    return verdict, comments, str(data.get("summary", "")).strip()


def format_body(rnd: int, max_rounds: int, verdict: str, summary: str, off_diff: list[dict], inline_count: int) -> str:
    left = inline_count or off_diff
    head = ("❌ требуются исправления" if verdict != "approve" else
            "✅ одобрено, оставшиеся замечания необязательны" if left else "✅ замечаний нет")
    parts = [REVIEW_MARKER, f"**AI-ревью, раунд {rnd}/{max_rounds}**: {head}", ""]
    if summary:
        parts += [summary, ""]
    if inline_count:
        parts += [f"Замечаний в строках диффа: {inline_count}.", ""]
    for title, sev in (("Blocking", "blocking"), ("Nit", "nit")):
        group = [c for c in off_diff if c["severity"] == sev]
        if group:
            parts.append(f"### {title} (вне диффа)")
            for i, c in enumerate(group, 1):
                where = f"`{c['file']}:{c['line']}` " if c["file"] and c["line"] else f"`{c['file']}` " if c["file"] else ""
                parts.append(f"{i}. {where}{c['text']}")
            parts.append("")
    return "\n".join(parts)


def review_event(identity: str, verdict: str) -> str:
    # От своего аккаунта GitHub не принимает ни APPROVE, ни REQUEST_CHANGES на собственный PR;
    # вердикт тогда несёт текст ревью, а мерж включает сам review.py.
    if identity == "self":
        return "COMMENT"
    return "APPROVE" if verdict == "approve" else "REQUEST_CHANGES"


def publish(r: Runner, pr: int, head_sha: str, verdict: str, rnd: int, max_rounds: int,
            summary: str, comments: list[dict], lines: dict[str, set[int]], tmp: Path,
            identity: str = "bot") -> list[dict]:
    inline, off = [], []
    for c in comments:
        if c["file"] in lines and c["line"] in lines[c["file"]]:
            inline.append(c)
        else:
            off.append(c)
    payload = {
        "commit_id": head_sha,
        "event": review_event(identity, verdict),
        "body": format_body(rnd, max_rounds, verdict, summary, off, len(inline)),
        "comments": [{"path": c["file"], "line": c["line"], "side": "RIGHT",
                      "body": f"**{c['severity']}**: {c['text']}"}
                     for c in inline],
    }
    p = tmp / "review.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    res = json.loads(r.gh_run("api", "--method", "POST", f"repos/{r.repo}/pulls/{pr}/reviews",
                              "--input", str(p)).stdout or "{}")
    ids = []
    if inline and res.get("id"):
        posted = json.loads(r.gh_run("api", f"repos/{r.repo}/pulls/{pr}/reviews/{res['id']}/comments").stdout or "[]")
        for c, pc in zip(inline, sorted(posted, key=lambda x: x.get("id", 0))):
            ids.append({**c, "comment_id": pc.get("id")})
    return ids + [{**c, "comment_id": None} for c in off]


def ci_required(cfg: dict, cwd: Path) -> bool:
    """Обязательны ли проверки CI на PR: require_ci true/false, auto — если в репозитории есть workflow."""
    v = cfg.get("require_ci", "auto")
    if isinstance(v, bool):
        return v
    if str(v).lower() in ("true", "false"):
        return str(v).lower() == "true"
    wf = cwd / ".github" / "workflows"
    return wf.is_dir() and any(wf.glob("*.y*ml"))


def read_checks(r: Runner, pr: int) -> list[dict]:
    proc = r.gh_run("pr", "checks", str(pr), "--json", "name,state,bucket,link,workflow", check=False)
    text = (proc.stdout or "").strip()
    if not text.startswith("["):
        if "no checks" in ((proc.stderr or "") + (proc.stdout or "")).lower():
            return []
        raise RuntimeError("gh pr checks: " + (proc.stderr or proc.stdout).strip()[:300])
    return json.loads(text)


def wait_checks(r: Runner, pr: int, timeout_min: int, required: bool = False, appear_seconds: float = 0,
                sleep=None) -> tuple[str, list[dict]]:
    """Дождаться проверок CI. После push их какое-то время нет: GitHub создаёт запуск не сразу, под
    нагрузкой — через несколько минут. Если проверки обязательны, ждём их появления appear_seconds,
    и «проверок нет» — не «прошло», а отдельный исход none."""
    deadline = time.time() + appear_seconds
    checks = read_checks(r, pr)
    while not checks and required and time.time() < deadline:
        set_phase("ci_appear")
        (sleep or time.sleep)(15)
        checks = read_checks(r, pr)
    if checks:
        set_phase("ci")
        r.gh_run("pr", "checks", str(pr), "--watch", "--interval", "30", check=False, timeout=timeout_min * 60 + 60)
        checks = read_checks(r, pr)
    if not checks:
        return "none", []
    if any(c.get("bucket") == "pending" for c in checks):
        return "pending", checks
    failed = [c for c in checks if c.get("bucket") in ("fail", "cancel")]
    return ("fail" if failed else "pass"), failed


def failed_logs(r: Runner, failed: list[dict], budget: int = 3000) -> str:
    out = []
    seen = set()
    for c in failed:
        m = re.search(r"/actions/runs/(\d+)", c.get("link") or "")
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        p = r.gh_run("run", "view", m.group(1), "--log-failed", check=False, timeout=120)
        out.append(f"## {c.get('workflow') or ''} / {c.get('name')}\n" + (p.stdout or p.stderr or "")[-budget:])
    text = "\n\n".join(out)
    return text[-budget:]


def try_merge(r: Runner, pr: int, head: str | None = None) -> str:
    """Смержить PR. head — проверенный коммит: если в ветке уже другой, GitHub мерж отклонит."""
    pin = ["--match-head-commit", head] if head else []
    if r.gh_json("pr", "view", str(pr), "--json", "mergeable").get("mergeable") == "CONFLICTING":
        return "conflict"
    if r.gh_run("pr", "merge", str(pr), "--auto", "--squash", *pin, check=False).returncode == 0:
        return "auto_merge_enabled"
    direct = r.gh_run("pr", "merge", str(pr), "--squash", *pin, check=False)
    if direct.returncode == 0:
        return "merged"
    if r.gh_json("pr", "view", str(pr), "--json", "mergeable").get("mergeable") == "CONFLICTING":
        return "conflict"
    raise RuntimeError("не удалось смержить: " + (direct.stderr or "").strip()[:300])


def behind_base(r: Runner, base: str, head: str) -> int:
    """На сколько коммитов ветка PR отстаёт от базовой. Отстаёт — CI и ревью видели не то, что получится
    после мержа: сначала rebase."""
    out = r.gh_run("api", f"repos/{r.repo}/compare/{base}...{head}", "--jq", ".behind_by").stdout.strip()
    return int(out or 0)


def patch_id(r: Runner, diff: str) -> str | None:
    """Отпечаток диффа, не зависящий от номеров строк и базового коммита (git patch-id --stable)."""
    git = shutil.which("git")
    if not git or not diff.strip():
        return None
    proc = r.run([git, "patch-id", "--stable"], env=r.plain_env, check=False, input=diff)
    parts = (proc.stdout or "").split()
    return parts[0] if proc.returncode == 0 and parts else None


def no_plan(task: dict | None) -> bool:
    """Задача шла без плана: её поручил или сделал сам планировщик (либо trivial вручную)."""
    return bool(task) and (aiflow.is_direct(task) or bool(task.get("self_executed")))


def task_contract(task: dict | None) -> str:
    """Для задачи без плана контракт — её текст и указания планировщика: в PR их нет."""
    if not no_plan(task):
        return ""
    out = ("# Task without a plan\n\nThe PR description has no plan: the contract is the task text below. Check that "
           "the changes do exactly this and nothing more.\n\n" + str(task.get("text") or "").strip() + "\n")
    brief = str(task.get("brief") or "").strip()
    if brief:
        out += "\n## The planner's instructions for the worker\n\n" + brief + "\n"
    return out


def final_verdict(verdict: str, comments: list[dict], still_open: list[dict], rnd: int, max_rounds: int,
                  nit_rounds: int) -> str:
    """Итог раунда. Открытые треды блокируют всегда. Одни nit блокируют только первые nit_rounds раундов
    (и никогда — последний): дальше они остаются в PR необязательными."""
    if still_open:
        return "changes"
    if verdict == "changes" and not any(c["severity"] == "blocking" for c in comments) \
            and rnd > min(max(0, nit_rounds), max_rounds - 1):
        return "approve"
    return verdict


def bump_streak(root: Path, tid: str, status: str, key: str = "") -> int:
    """Сколько раз подряд раунд заканчивается одним и тем же (ci_failed; human_input по тому же
    комментарию). Счётчик ведёт код: такие исходы не тратят раунд ревью и иначе не ограничены."""
    path = root / "runs" / tid / "review_state.json"
    st = aiflow.read_json(path, {}) or {}
    n = int(st.get("n") or 0) + 1 if (st.get("status"), st.get("key")) == (status, key) else 1
    aiflow.write_json(path, {"status": status, "key": key, "n": n})
    return n


def clear_streak(root: Path, tid: str) -> None:
    (root / "runs" / tid / "review_state.json").unlink(missing_ok=True)


def pause_message(reasons: list[str]) -> str:
    return (f"the aiflow queue is paused ({', '.join(reasons)}): the review and the merge wait. End your turn with "
            "one line. aiflow wakes you when the queue continues")


# ------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Один раунд AI-ревью pull request'а")
    ap.add_argument("--pr", type=int, required=True)
    args = ap.parse_args(argv)
    CTX["pr"] = args.pr

    try:
        root = aiflow.repo_root()
    except aiflow.AiflowError as e:
        finish("error", 1, message=str(e))
    CTX["root"] = root
    store = aiflow.Store(root)
    cfg = store.config()
    CTX["task"] = task = find_task(root, args.pr)
    max_rounds = int(cfg["max_rounds"])

    gh, oc = shutil.which("gh"), shutil.which("opencode")
    if not gh or not oc:
        finish("error", 1, message="нужны gh и opencode в PATH")
    identity = cfg.get("review_identity") or "self"
    if identity not in IDENTITIES:
        finish("error", 1, message=f"review_identity в runs/config.json: {identity!r}, ожидается self или bot")
    r = Runner(gh, oc, load_token() if identity == "bot" else None, Path.cwd())
    tmp = Path(tempfile.mkdtemp(prefix="aiflow-review-"))
    try:
        login = r.gh_run("api", "user", "--jq", ".login").stdout.strip()
        pr = r.gh_json("pr", "view", str(args.pr), "--json", "state,title,body,comments,headRefOid,author")
        if pr.get("state") != "OPEN":
            finish("already_closed", state=pr.get("state"))
        if identity == "bot" and (pr.get("author") or {}).get("login") == login:
            finish("error", 1, message="PR создан ботом — бот не может его одобрить. "
                                       "Создавайте PR своим аккаунтом или переключите review_identity на self")

        tid = task["id"] if task else None

        def needs_human_label() -> None:
            label = cfg["labels"]["needs_human"]
            r.gh_run("label", "create", label, "--color", "D93F0B", "--force", check=False)
            r.gh_run("pr", "edit", str(args.pr), "--add-label", label, check=False)

        # очередь на паузе (вручную, красная базовая ветка, лимит): ни ревью, ни мержа
        reasons = sorted(store.queue().get("pauses") or {})
        if reasons:
            finish("paused", reasons=reasons, message=pause_message(reasons))

        reviews = bot_reviews(r, args.pr, login)
        done = len(reviews)
        head = pr.get("headRefOid")
        diff = r.gh_run("pr", "diff", str(args.pr)).stdout
        lines = diff_lines(diff)
        files = [f for f in r.gh_run("pr", "diff", str(args.pr), "--name-only").stdout.splitlines() if f.strip()]

        # тот же дифф, что ревьюер уже одобрил (после rebase на ушедшую вперёд базу): второй раз не ревьюим
        pid = patch_id(r, diff)
        approved_file = store.approved_path(tid) if tid else None
        reuse = bool(pid and approved_file and (aiflow.read_json(approved_file, {}) or {}).get("patch_id") == pid)
        if approved_file and not reuse:
            approved_file.unlink(missing_ok=True)

        if done >= max_rounds and not reuse:
            needs_human_label()
            finish("limit_reached", rounds_done=done, max_rounds=max_rounds,
                   message="лимит раундов исчерпан, PR помечен needs-human")
        rnd = CTX["round"] = done if reuse else done + 1

        # комментарии и дополнения человека, на которые worker ещё не ответил
        comments = pr.get("comments") or []
        floor = (task or {}).get("comments_floor")
        human = human_comments(comments, reviews, floor)
        if human:
            CTX["round"] = done
            if tid and bump_streak(root, tid, "human_input", str(human[-1].get("at") or "")) > HUMAN_INPUT_REPEATS:
                finish("error", 1, message="комментарий человека возвращается исполнителю уже "
                                           f"{HUMAN_INPUT_REPEATS} раза, а ответа с <!-- ai-review-reply --> нет")
            finish("human_input", comments=human,
                   message="do what the human comments ask, reply with a comment that starts with <!-- ai-review-reply --> "
                           "and run the review again")

        # CI до ревью: раунд не расходуется
        required = ci_required(cfg, r.cwd)
        appear = float(cfg.get("ci_appear_minutes", 10) or 0)
        state, failed = wait_checks(r, args.pr, int(cfg["ci_timeout_minutes"]), required, appear * 60)
        if state == "none" and required:
            CTX["round"] = done
            finish("no_ci", 1, waited_minutes=appear,
                   message=f"за {appear:g} мин после push на PR не появилось ни одной проверки CI, а без неё мержить нельзя")
        if state == "pending":
            CTX["round"] = done
            finish("error", 1, message=f"CI не завершился за {cfg['ci_timeout_minutes']} мин")
        if state == "fail":
            CTX["round"] = done
            limit = int(cfg.get("max_ci_fixes") or 0)
            if tid and limit and bump_streak(root, tid, "ci_failed") > limit:
                needs_human_label()
                finish("limit_reached", failed=[c.get("name") for c in failed],
                       message=f"CI красный {limit} раз подряд: починить не удаётся, PR помечен needs-human")
            finish("ci_failed", failed=[c.get("name") for c in failed], logs=failed_logs(r, failed))
        if tid:
            clear_streak(root, tid)

        # защищённые пути — без вызова модели
        allowed = (task or {}).get("allowed_protected_paths") or []
        viol = [f for f in files if aiflow.path_matches(f, cfg["protected_paths"]) and not aiflow.path_matches(f, allowed)]
        if viol:
            cs = [{"severity": "blocking", "file": f, "line": None,
                   "text": "Изменение защищённого пути без явного разрешения в задаче. Откатите или попросите человека "
                           f"разрешить путь: aiflow allow-path {(task or {}).get('id') or '<id>'} '<glob>' (кнопка в панели)."} for f in viol]
            posted = publish(r, args.pr, head, "changes", rnd, max_rounds, "Нарушены защищённые пути.", cs, {}, tmp,
                             identity)
            finish("changes_requested", round=rnd, max_rounds=max_rounds, summary="защищённые пути", comments=posted)

        if reuse:
            result = {"round": done, "max_rounds": max_rounds, "comments": [], "open_threads": [], "reused_approval": True,
                      "summary": "изменения те же, что ревьюер уже одобрил: повторного ревью нет"}
        else:
            thread_comments = review_comments(r, args.pr)
            history = build_history(reviews, thread_comments, comments)
            pending = open_threads(thread_comments, review_threads(r, args.pr), login) if reviews else []
            truncated = len(diff) > BIG_DIFF_CHARS  # ревьюер должен подтвердить, что дочитал
            fresh_task = store.task(tid) if tid else None
            brief = build_brief(args.pr, pr, cfg["base_branch"], history, truncated)
            brief += ("\n" + task_contract(fresh_task) + "\n" + format_amendments(fresh_task) + "\n"
                      + format_open_threads(pending))
            (tmp / "review-brief.md").write_text(brief, encoding="utf-8")  # для разбора, ревьюеру не нужен

            set_phase("reviewer", round=rnd)
            data = call_reviewer(r, args.pr, rnd, max_rounds, cfg.get("reviewer_model") or "", brief, tmp,
                                 float(cfg.get("reviewer_idle_minutes", 5) or 0),
                                 float(cfg.get("reviewer_timeout_minutes") or 45))
            verdict, cs, summary = normalize(data, truncated)
            still_open = answer_threads(r, args.pr, pending, thread_verdicts(data), tmp) if pending else []
            verdict = final_verdict(verdict, cs, still_open, rnd, max_rounds, int(cfg.get("nit_rounds", 1) or 0))
            if verdict == "approve":
                # пока работал ревьюер, человек мог дописать правку: не одобряем и не мержим мимо неё
                late = human_comments(r.gh_json("pr", "view", str(args.pr), "--json", "comments").get("comments") or [],
                                      reviews, floor)
                if late:
                    CTX["round"] = done
                    finish("human_input", comments=late,
                           message="human comments came during the review: do what they ask, reply with a comment "
                                   "that starts with <!-- ai-review-reply --> and run the review again (this did not "
                                   "use a round)")
            posted = publish(r, args.pr, head, verdict, rnd, max_rounds, summary, cs, lines, tmp, identity)
            result = {"round": rnd, "max_rounds": max_rounds, "summary": summary, "comments": posted,
                      "open_threads": [{"comment_id": t["comment_id"], "file": t["path"], "line": t["line"],
                                        "reason": t["reason"]} for t in still_open]}
            if verdict != "approve":
                finish("changes_requested", **result)
            if approved_file and pid:
                aiflow.write_json(approved_file, {"patch_id": pid, "round": rnd, "head": head, "at": aiflow.iso()})

        # Мерж — по одному: под общим lock проверяем паузу и то, что ветка не отстала от базовой.
        set_phase("merge")
        gate = aiflow.FileLock(root / "runs" / "merge.lock")
        if not gate.acquire(wait_seconds=180):
            raise RuntimeError("мерж занят другим PR дольше 3 минут")
        try:
            reasons = sorted(store.queue().get("pauses") or {})
            if reasons:
                finish("paused", **result, reasons=reasons, message="approved, but " + pause_message(reasons))
            if behind_base(r, cfg["base_branch"], head) > 0:
                finish("merge_conflict", **result,
                       message="approved, but the base branch moved ahead: git fetch, git merge of the base "
                               "branch (no rebase, no force push), tests, push and aiflow review again. "
                               "The same changes get no second review")
            merge = try_merge(r, args.pr, head)
        finally:
            gate.release()
        if merge == "conflict":
            finish("merge_conflict", **result, message="approved, but there is a conflict with the base branch: merge the base branch "
                                                      "into the PR branch and resolve it (no rebase, no force push)")
        finish("approved", merge=merge, **result)

    except subprocess.TimeoutExpired as e:
        what = Path(str(e.cmd[0] if isinstance(e.cmd, (list, tuple)) else e.cmd)).stem
        finish("error", 1, message=f"{what} не ответил за {int(e.timeout)} с и был остановлен")
    except (RuntimeError, json.JSONDecodeError, KeyError, OSError) as e:
        finish("error", 1, message=str(e))  # ReviewerStalled — тоже RuntimeError
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    main()
