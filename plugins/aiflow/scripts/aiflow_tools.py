"""Внешние программы: фоновые сессии Claude Code (`claude`) и GitHub (`gh`).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from aiflow_core import AiflowError, gh_with_retry, need, run


def settled_session(e: dict) -> dict:
    """Сессия закончила ход, но сама назвала своё состояние «working»: так она пишет, когда ждёт чего-то
    внешнего («план на ревью, жду»). Ход при этом окончен — status "idle", — и очередь ждала бы такую сессию
    вечно. Для очереди это законченный ход."""
    if e.get("state") == "working" and e.get("status") == "idle":
        return {**e, "state": "done", "said_working": True}
    return e


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
        return {e["id"]: settled_session(e) for e in data if isinstance(e, dict) and e.get("id")}

    @staticmethod
    def _short_id(out: str) -> str | None:
        m = re.search(r"backgrounded\W+([0-9A-Za-z]{6,})", out)
        return m.group(1) if m else None

    def launch(self, agent: str, name: str, prompt: str, *, model=None, effort=None, mode=None,
               worktree: str | None = None, window: int = 0) -> tuple[str | None, str]:
        """window — окно контекста в токенах. Через окружение его не передать: фоновые сессии
        окружение запускающего процесса не наследуют."""
        cmd = [self.bin, "--bg", "--agent", agent, "--name", name]
        if window:
            cmd += ["--autocompact", str(window)]
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
               effort=None, window: int = 0, name: str | None = None) -> tuple[str | None, str]:
        """cwd — папка сессии: --resume работает там, откуда вызван, а не там, где сессия жила.
        model и effort — те же, с которыми сессия работала: без них продолженная сессия берёт модель по
        умолчанию. Это и не та модель, и полная перезапись кэша диалога — кэш у каждой модели свой."""
        cmd = [self.bin, "--resume", session_id, "--bg"]
        if name:
            # без имени Claude Code называет продолженную сессию по теме разговора («reviewer: pr-309»),
            # и вкладку исполнителя задачи по названию уже не найти
            cmd += ["--name", name]
        if window:
            cmd += ["--autocompact", str(window)]
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


def gh_repo(cwd, env=None) -> str:
    """owner/name репозитория по версии GitHub. После переименования или переноса remote ещё
    указывает на старый адрес: gh подставляет его в {owner}/{repo}, а запросы с методом
    (POST/PATCH) редирект 307 не проходят. gh repo view редирект проходит и даёт новый адрес."""
    p = gh_with_retry(lambda: run([need("gh"), "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                                  cwd=cwd, env=env, timeout=60), ["repo", "view"])
    name = (p.stdout or "").strip()
    return name if p.returncode == 0 and re.fullmatch(r"[\w.-]+/[\w.-]+", name) else "{owner}/{repo}"
