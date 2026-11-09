#!/usr/bin/env python3
"""
Один раунд AI-ревью pull request'а (вызывается как `aiflow review --pr N`).

Порядок: PR открыт → лимит раундов → комментарии человека → CI → защищённые пути →
ревьюер DeepSeek через opencode → inline-ревью → автомерж.
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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aiflow  # noqa: E402

REVIEW_MARKER = "<!-- ai-review -->"
REPLY_MARKER = "<!-- ai-review-reply -->"
HUMAN_MARKER = "<!-- human -->"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PARSE_ATTEMPTS = 3
OPENCODE_TIMEOUT = 15 * 60
TERMINAL = {"approved", "limit_reached", "already_closed", "error"}

CTX: dict = {"root": None, "task": None, "pr": None, "round": None}


def finish(status: str, code: int = 0, **data) -> None:
    out = {"status": status, **data}
    root, task = CTX["root"], CTX["task"]
    if root and task:
        try:
            aiflow.write_json(root / "runs" / task["id"] / "review.json",
                              {"at": aiflow.iso(), "pr": CTX["pr"], "round": CTX["round"], **out})
            if status in TERMINAL:
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

    def run(self, cmd, *, env, check=True, timeout=180):
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=timeout, cwd=self.cwd, **aiflow.NO_WINDOW)
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
    parts = ["# Открытые треды прошлых раундов", "",
             "Для каждого треда верни вердикт в поле threads (comment_id — число в квадратных скобках).", ""]
    for t in items:
        parts.append(f"[{t['comment_id']}] `{t['path']}:{t['line']}` {t['body']}")
        parts += [f"  ↳ {x}" for x in t["replies"]] or ["  ↳ (ответа исполнителя нет)"]
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
    items = [(rv.get("submitted_at") or "", "Ревьюер (итог раунда)", rv.get("body") or "") for rv in reviews]
    roots = {c["id"]: c for c in threads if not c.get("in_reply_to_id")}
    for root_id, c in roots.items():
        replies = [x for x in threads if x.get("in_reply_to_id") == root_id]
        lines = [f"`{c.get('path')}:{c.get('line')}` {c.get('user')}: {strip_markers(c.get('body') or '')}"]
        lines += [f"  ↳ {x.get('user')}: {strip_markers(x.get('body') or '')}" for x in replies]
        items.append((c.get("created_at") or "", "Тред", "\n".join(lines)))
    for c in comments:
        body = c.get("body") or ""
        if REPLY_MARKER in body:
            items.append((c.get("createdAt") or "", "Исполнитель", body))
        elif body.lstrip().startswith(HUMAN_MARKER):
            items.append((c.get("createdAt") or "", "Человек", body))
    if not items:
        return "Это первый раунд, истории нет."
    items.sort(key=lambda x: x[0])
    text = "\n\n".join(f"### {who} ({ts})\n\n{strip_markers(body)}" for ts, who, body in items)
    return text if len(text) <= limit else "[… начало истории обрезано]\n" + text[-limit:]


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
    return ("# Дополнения пользователя к задаче\n\nЭто часть задачи наравне с планом: изменения по ним — не работа "
            "вне плана. Проверь, что они выполнены.\n\n" + aiflow.amend_list(items) + "\n")


def build_context(plan: str, pr: dict, history: str, diff: str, max_chars: int) -> tuple[str, bool]:
    truncated = len(diff) > max_chars
    if truncated:
        diff = diff[:max_chars] + "\n\n[... дифф обрезан. Дочитай остаток через git diff и верни \"diff_fully_read\": true]\n"
    ctx = (f"# План задачи и описание PR: {pr.get('title', '')}\n\n{plan or '(описания нет)'}\n\n"
           f"# История ревью\n\n{history}\n\n# Дифф\n\n```diff\n{diff}\n```\n")
    return ctx, truncated


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
    "bash": {"*": "deny", "git diff*": "allow", "git log*": "allow", "git show*": "allow"},
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


def call_reviewer(r: Runner, pr: int, rnd: int, max_rounds: int, model: str, ctx_path: Path) -> dict:
    message = (f"Review pull request #{pr}, round {rnd} of {max_rounds}. All materials are in the attached file. "
               "Reply with exactly one JSON object following your instructions.")
    env = reviewer_config(r, ctx_path.parent)
    cmd = [r.opencode, "run", message, "--agent", AGENT]
    if model:
        cmd += ["--model", model]
    cmd += ["--file", str(ctx_path)]
    last = err = ""
    for _ in range(PARSE_ATTEMPTS):
        proc = r.run(cmd, env=env, check=False, timeout=OPENCODE_TIMEOUT)
        last = ANSI_RE.sub("", proc.stdout or "")
        err = ANSI_RE.sub("", proc.stderr or "").strip()
        data = extract_json(last)
        if data is not None and "comments" in data:
            return data
        if proc.returncode != 0 and not last.strip() and "--pure" not in cmd:
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
                         "text": "Дифф был обрезан, а ревьюер не подтвердил, что прочитал остаток. "
                                 "Разбейте изменения или повторите раунд."})
    # nit тоже исправляется: одобрение — только когда замечаний нет совсем
    verdict = "changes" if comments else "approve"
    return verdict, comments, str(data.get("summary", "")).strip()


def format_body(rnd: int, max_rounds: int, verdict: str, summary: str, off_diff: list[dict], inline_count: int) -> str:
    head = "✅ замечаний нет" if verdict == "approve" else "❌ требуются исправления"
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


def wait_checks(r: Runner, pr: int, timeout_min: int) -> tuple[str, list[dict]]:
    r.gh_run("pr", "checks", str(pr), "--watch", "--interval", "30", check=False, timeout=timeout_min * 60 + 60)
    proc = r.gh_run("pr", "checks", str(pr), "--json", "name,state,bucket,link,workflow", check=False)
    text = (proc.stdout or "").strip()
    if not text.startswith("["):
        if "no checks" in (proc.stderr + proc.stdout).lower():
            return "none", []
        raise RuntimeError("gh pr checks: " + (proc.stderr or proc.stdout).strip()[:300])
    checks = json.loads(text)
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


def try_merge(r: Runner, pr: int) -> str:
    if r.gh_json("pr", "view", str(pr), "--json", "mergeable").get("mergeable") == "CONFLICTING":
        return "conflict"
    if r.gh_run("pr", "merge", str(pr), "--auto", "--squash", check=False).returncode == 0:
        return "auto_merge_enabled"
    direct = r.gh_run("pr", "merge", str(pr), "--squash", check=False)
    if direct.returncode == 0:
        return "merged"
    if r.gh_json("pr", "view", str(pr), "--json", "mergeable").get("mergeable") == "CONFLICTING":
        return "conflict"
    raise RuntimeError("не удалось смержить: " + (direct.stderr or "").strip()[:300])


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

        reviews = bot_reviews(r, args.pr, login)
        done = len(reviews)
        if done >= max_rounds:
            label = cfg["labels"]["needs_human"]
            r.gh_run("label", "create", label, "--color", "D93F0B", "--force", check=False)
            r.gh_run("pr", "edit", str(args.pr), "--add-label", label, check=False)
            finish("limit_reached", rounds_done=done, max_rounds=max_rounds,
                   message="лимит раундов исчерпан, PR помечен needs-human")
        rnd = CTX["round"] = done + 1

        # комментарии и дополнения человека, на которые worker ещё не ответил
        comments = pr.get("comments") or []
        floor = (task or {}).get("comments_floor")
        human = human_comments(comments, reviews, floor)
        if human:
            CTX["round"] = done
            finish("human_input", comments=human,
                   message="учти комментарии человека, ответь комментарием с <!-- ai-review-reply --> и снова запусти ревью")

        # CI до ревью: раунд не расходуется
        state, failed = wait_checks(r, args.pr, int(cfg["ci_timeout_minutes"]))
        if state == "pending":
            CTX["round"] = done
            finish("error", 1, message=f"CI не завершился за {cfg['ci_timeout_minutes']} мин")
        if state == "fail":
            CTX["round"] = done
            finish("ci_failed", failed=[c.get("name") for c in failed], logs=failed_logs(r, failed))

        head = pr.get("headRefOid")
        diff = r.gh_run("pr", "diff", str(args.pr)).stdout
        lines = diff_lines(diff)

        # защищённые пути — без вызова модели
        files = [f for f in r.gh_run("pr", "diff", str(args.pr), "--name-only").stdout.splitlines() if f.strip()]
        allowed = (task or {}).get("allowed_protected_paths") or []
        viol = [f for f in files if aiflow.path_matches(f, cfg["protected_paths"]) and not aiflow.path_matches(f, allowed)]
        if viol:
            cs = [{"severity": "blocking", "file": f, "line": None,
                   "text": "Изменение защищённого пути без явного разрешения в задаче. Откатите или попросите человека "
                           f"разрешить путь: aiflow allow-path {(task or {}).get('id') or '<id>'} '<glob>' (кнопка в панели)."} for f in viol]
            posted = publish(r, args.pr, head, "changes", rnd, max_rounds, "Нарушены защищённые пути.", cs, {}, tmp,
                             identity)
            finish("changes_requested", round=rnd, max_rounds=max_rounds, summary="защищённые пути", comments=posted)

        thread_comments = review_comments(r, args.pr)
        history = build_history(reviews, thread_comments, comments)
        pending = open_threads(thread_comments, review_threads(r, args.pr), login) if reviews else []
        ctx, truncated = build_context(pr.get("body") or "", pr, history, diff, int(cfg["max_diff_chars"]))
        ctx += "\n" + format_amendments(store.task(task["id"]) if task else None) + "\n" + format_open_threads(pending)
        ctx_path = tmp / "review-context.md"
        ctx_path.write_text(ctx, encoding="utf-8")

        data = call_reviewer(r, args.pr, rnd, max_rounds, cfg.get("reviewer_model") or "", ctx_path)
        verdict, cs, summary = normalize(data, truncated)
        still_open = answer_threads(r, args.pr, pending, thread_verdicts(data), tmp) if pending else []
        if still_open:
            verdict = "changes"
        elif verdict == "changes" and rnd >= max_rounds and not any(c["severity"] == "blocking" for c in cs):
            verdict = "approve"  # последний раунд: оставшиеся nit не повод для needs-human, они видны в PR
        if verdict == "approve":
            # пока работал ревьюер, человек мог дописать правку: не одобряем и не мержим мимо неё
            late = human_comments(r.gh_json("pr", "view", str(args.pr), "--json", "comments").get("comments") or [],
                                  reviews, floor)
            if late:
                CTX["round"] = done
                finish("human_input", comments=late,
                       message="во время ревью пришли комментарии человека: учти их, ответь комментарием с "
                               "<!-- ai-review-reply --> и снова запусти ревью (раунд не израсходован)")
        posted = publish(r, args.pr, head, verdict, rnd, max_rounds, summary, cs, lines, tmp, identity)
        result = {"round": rnd, "max_rounds": max_rounds, "summary": summary, "comments": posted,
                  "open_threads": [{"comment_id": t["comment_id"], "file": t["path"], "line": t["line"],
                                    "reason": t["reason"]} for t in still_open]}
        if verdict != "approve":
            finish("changes_requested", **result)

        merge = try_merge(r, args.pr)
        if merge == "conflict":
            finish("merge_conflict", **result, message="одобрено, но конфликт с базовой веткой: нужен rebase")
        finish("approved", merge=merge, **result)

    except subprocess.TimeoutExpired:
        finish("error", 1, message="внешняя команда не уложилась в таймаут")
    except (RuntimeError, json.JSONDecodeError, KeyError, OSError) as e:
        finish("error", 1, message=str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    main()
