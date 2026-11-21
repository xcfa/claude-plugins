"""Тексты, с которыми запускаются и будятся сессии задач, и то, что в них подставляется:
зависимости задачи, соседние проекты, дополнения пользователя.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from aiflow_core import (
    AiflowError, FINISHED, PLANNER_PLAN_MD, PLANNER_TALK_MD, TERMINAL, WORKER_MD, age_minutes, aiflow_home, iso,
    read_json, run,
)
from aiflow_store import FileLock, Store, state_dir

# ---------------------------------------------------------------- prompts

EXPLORERS = ("aiflow", "project")


def explorer_mode(cfg: dict) -> str:
    v = str(cfg.get("explorer") or "aiflow").lower()
    return v if v in EXPLORERS else "aiflow"


SEARCH_HEAD = (
    "How to search the code. There are two cases. "
    "1) A file that you change, or that your decision depends on: read it yourself, one time, in full, with Read. "
    "2) A question about the code base (where is X declared, who calls or creates X, every usage of a symbol, "
    "how many places; for example \"every place that creates TaxonRefDto\"): do not answer it with grep, git grep, "
    "sed or cat through Bash. Their full output stays in your context and you pay for it in each later request. ")


def search_rule(cfg: dict) -> str:
    """Правило поиска по коду для первого сообщения сессии: одной строкой, с примером вопроса.
    Без него сессия ищет своим grep и sed, и весь вывод оседает в её контексте. В режиме explorer: project
    средства aiflow не названы: чем искать, решают CLAUDE.md и агенты проекта, и две инструкции спорили бы."""
    if explorer_mode(cfg) == "project":
        return SEARCH_HEAD + ("Send such a question the way the project CLAUDE.md says: to the subagent or the tool "
                              "that it names (several questions in parallel).")
    return SEARCH_HEAD + ("Send such a question to the aiflow:explorer subagent (several in parallel). For one symbol "
                          "you can use the LSP tool when it is available for the language (if it is deferred, load "
                          "it through ToolSearch).")


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


def neighbours(root: Path) -> dict[str, Path]:
    """Другие проекты службы aiflow на этой машине: имя папки → корень репозитория."""
    data = read_json(aiflow_home() / "projects.json", {}) or {}
    me = os.path.normcase(str(Path(root).resolve()))
    out: dict[str, Path] = {}
    for e in data.get("projects") or []:
        r = Path(str((e or {}).get("root") or ""))
        try:
            if not r.is_dir() or os.path.normcase(str(r.resolve())) == me:
                continue
        except OSError:
            continue
        if (state_dir(r) / "config.json").is_file():
            out.setdefault(r.name, r)
    return out


def split_dep(d: str) -> tuple[str | None, str]:
    """Зависимость: "id" — задача этого проекта, "проект/id" — задача соседнего."""
    project, _, tid = str(d).rpartition("/")
    return (project or None), tid


def dep_task(store: Store, d: str, by_id: dict[str, dict] | None = None) -> tuple[Store | None, dict | None]:
    project, tid = split_dep(d)
    if not project:
        return store, (by_id.get(tid) if by_id is not None else store.task(tid))
    root = neighbours(store.root).get(project)
    other = Store(root) if root else None
    return other, (other.task(tid) if other else None)


def merge_workflows(root: Path, task: dict) -> tuple[str, str]:
    """Workflow на коммите мержа задачи (публикация пакета). ("ok" | "wait" | "failed", пояснение)."""
    p = run(["gh", "pr", "view", str(task["pr"]), "--json", "mergeCommit,mergedAt"], cwd=root, timeout=60)
    try:
        data = json.loads(p.stdout) if p.returncode == 0 else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return "wait", "не удалось узнать коммит мержа"
    sha = (data.get("mergeCommit") or {}).get("oid")
    if not sha:
        return "ok", ""
    p = run(["gh", "run", "list", "--commit", sha, "--limit", "50", "--json", "status,conclusion,workflowName"],
            cwd=root, timeout=60)
    try:
        runs = json.loads(p.stdout) if p.returncode == 0 else None
    except ValueError:
        runs = None
    if not isinstance(runs, list):
        return "wait", "не удалось узнать состояние workflow"
    if not runs:
        return ("wait", "ждёт запуска workflow после мержа") if age_minutes(data.get("mergedAt")) < 3 else ("ok", "")
    names = lambda rs: ", ".join(sorted({str(r.get("workflowName") or "?") for r in rs}))  # noqa: E731
    going = [r for r in runs if r.get("status") != "completed"]
    if going:
        return "wait", f"идёт {names(going)}"
    bad = [r for r in runs if r.get("conclusion") not in ("success", "skipped", "neutral")]
    if bad:
        return "failed", f"после мержа упал workflow {names(bad)}"
    return "ok", ""


def waiting_deps(store: Store, t: dict, by_id: dict[str, dict]) -> list[str]:
    """Зависимости, которых задача ещё ждёт, — для панели."""
    out = []
    for d in t.get("depends_on") or []:
        _, other = dep_task(store, d, by_id)
        st = (other or {}).get("state")
        if st not in FINISHED:
            out.append(d + (" (снята)" if st in (None, "cancelled") else ""))
        elif split_dep(d)[0] and d not in (t.get("deps_published") or []):
            note = ((t.get("deps_checked") or {}).get(d) or {}).get("note")
            if note:
                out.append(f"{d} ({note})")
    return out


def deps_note(store: Store, t: dict) -> str:
    """Что сделали задачи, после которых поставлена эта, — в промпт запуска."""
    lines = []
    for d in t.get("depends_on") or []:
        other_store, other = dep_task(store, d)
        if not other or other.get("state") not in FINISHED:
            continue
        rep = other_store.report(other["id"]) or {}
        line = f"- {d}: {other.get('title') or ''}"
        if other.get("pr"):
            line += f" (merged PR #{other['pr']}" + (f", repository {other_store.root}" if split_dep(d)[0] else "") + ")"
        if rep.get("summary"):
            line += f". Result: {rep['summary']}"
        lines.append(line)
    if not lines:
        return ""
    return ("\nThis task was queued after other tasks, and they are finished. Their result is already in the base "
            "branch (for another repository: merged and published there):\n" + "\n".join(lines) + "\n")


def neighbours_note(store: Store, tid: str) -> str:
    """Соседние проекты службы — планировщику: куда поставить задачу, если она не про этот репозиторий."""
    others = neighbours(store.root)
    if not others:
        return ""
    items = []
    for name, root in sorted(others.items()):
        about = str(Store(root).config().get("description") or "").strip()
        items.append(f"{name} ({about[:160]})" if about else name)
    return (
        "\nOther repositories in this aiflow queue: " + "; ".join(items) + ". You cannot change them from this "
        "session. If the task needs a change there, queue a task for that repository: "
        f"aiflow enqueue --project <name> --then {tid} (JSON on stdin: id, title, text; write in the text what to "
        "change, why, and the contract that this task expects). This task then waits for it: write the plan for the "
        "part in this repository (route complex, do not do it yourself); the worker starts when that task is merged "
        "and its publish workflows are complete.\n"
    )


def set_after(store: Store, tid: str, dep: str, remove: bool = False) -> dict:
    """Поставить задачу tid после dep ("id" или "проект/id") либо убрать такую зависимость."""
    lock = FileLock(store.runs / "tick.lock", stale_minutes=15)
    if not lock.acquire(wait_seconds=120):
        raise AiflowError("очередь занята дольше 2 минут, повторите позже")
    try:
        t = store.task(tid)
        if not t:
            raise AiflowError(f"нет задачи {tid}")
        deps = list(t.get("depends_on") or [])
        if remove:
            if dep not in deps:
                raise AiflowError(f"{tid} не зависит от {dep}")
            deps.remove(dep)
            note = f"{tid}: больше не ждёт {dep}"
        else:
            if t.get("state") in TERMINAL:
                raise AiflowError(f"{tid} уже завершена")
            if t.get("state") in ("in_progress", "ready", "in_review"):
                raise AiflowError(f"{tid}: исполнитель уже работает, поставить её после другой задачи поздно")
            project, did = split_dep(dep)
            if not project and did == tid:
                raise AiflowError("задача не может ждать саму себя")
            other_store, other = dep_task(store, dep)
            if project and not other_store:
                raise AiflowError(f"нет проекта {project} в службе aiflow. Есть: "
                                  + (", ".join(sorted(neighbours(store.root))) or "других проектов нет"))
            if not other:
                raise AiflowError(f"нет задачи {dep}")
            if other.get("state") == "cancelled":
                raise AiflowError(f"{dep} снята — она не выполнится")
            if dep in deps:
                raise AiflowError(f"{tid} уже ждёт {dep}")
            if project:
                if f"{store.root.name}/{tid}" in (other.get("depends_on") or []):
                    raise AiflowError(f"{dep} сама ждёт {tid}: получится круг")
            else:
                seen, todo = set(), [did]
                while todo:
                    cur = todo.pop()
                    if cur == tid:
                        raise AiflowError(f"{dep} сама ждёт {tid}: получится круг")
                    if cur in seen:
                        continue
                    seen.add(cur)
                    todo += [x for x in (store.task(cur) or {}).get("depends_on") or [] if not split_dep(x)[0]]
            deps.append(dep)
            note = f"{tid}: поставлена после {dep}"
        t["depends_on"] = deps
        t["deps_published"] = [d for d in t.get("deps_published") or [] if d in deps]
        t.pop("deps_checked", None)
        store.save_task(t)
        store.event("deps", tid, note)
    finally:
        lock.release()
    return t


def split_note(store: Store, t: dict) -> str:
    """Задача — часть другой, разбитой по решению пользователя: откуда она и где исходный план."""
    first = store.task(t.get("split_of") or "") if t.get("split_of") else None
    if not first:
        return ""
    return (f"This task is a part of task {first['id']}: the user decided to split it into several PRs"
            + (f" (its plan is PR #{first['pr']})" if first.get("pr") else "") + ". The task text already carries "
            "the decisions of that plan for this part: keep them, and do not plan the other parts again.\n")


def planner_prompt(store: Store, t: dict, cfg: dict) -> str:
    tid = t["id"]
    return with_search_rule((
        f"aiflow task {tid}: {t.get('title', '')}\n"
        f"Task file: {store.task_path(tid)}. Read it in full.\n"
        f"Base branch: {cfg['base_branch']}. Task branch: ai/{tid}.\n"
        + deps_note(store, t) + split_note(store, t) + neighbours_note(store, tid) +
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
        f"When you decide to do it yourself, before the first edit open a draft PR and run aiflow report {tid} "
        "--role planner --status self_execute --pr <number> --summary \"<what you do, one line>\" and continue: "
        "then the queue shows work and the PR, not planning.\n"
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


def plan_check_prompt(t: dict, result: dict) -> str:
    """Замечания ревьюера плана — планировщику. Один ход: исправить или возразить, затем снова plan_ready."""
    items = "\n".join(f"{i}. [{f.get('severity')}]" + (f" ({f['section']})" if f.get("section") else "")
                      + f" {f.get('text', '')}" for i, f in enumerate(result.get("findings") or [], 1))
    return (
        f"Another model reviewed your plan in PR #{t['pr']} (task {t['id']}) before the user sees it. Its findings:\n\n"
        f"{items}\n\n"
        "Obey the section \"Plan check\" of the plan rules. For each finding: check it against the code, then fix "
        "the plan, or disagree and say why. The reviewer can be wrong. A `question` is for the user: put it into "
        "the plan where the user will see it, do not decide it yourself. Update the PR description "
        "(gh pr edit --body-file), add a line to \"Plan change history\", and reply in the PR with one comment "
        "whose first line is <!-- aiflow:planner -->: one line for each finding, \"fixed: how\" or \"disagree: why\". "
        f"Then run aiflow report {t['id']} --role planner --status plan_ready --pr {t['pr']} "
        "--summary \"<what changed>\" and end your turn. Do not wait for a second review."
    )


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
    base += deps_note(store, t).lstrip("\n")
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


def base_fix_text(base: str, run: dict, log: str, recent: list[dict], reruns: int) -> str:
    """Текст задачи, которую очередь ставит сама, когда базовая ветка покраснела после мержа."""
    merged = "\n".join(f"- {t['id']}: {t.get('title', '')}" + (f", PR #{t['pr']}" if t.get("pr") else "") for t in recent)
    run_id = run.get("databaseId") or "<run id>"
    return "\n".join(x for x in (
        f"The CI of the base branch {base} is red after a merge. The aiflow queue is stopped until it is green again. "
        "Make it green.",
        "",
        f"Failed run: {run.get('url') or ''}",
        f"Commit: {run.get('headSha') or ''}" + (f" ({run['displayTitle']})" if run.get("displayTitle") else ""),
        "aiflow ran the failed jobs again, and they failed again." if reruns else None,
        "",
        ("The end of the log of the failed jobs:\n```\n" + log.strip() + "\n```\n") if log.strip() else None,
        ("The tasks that were merged last (the cause is probably in one of them):\n" + merged + "\n") if merged else None,
        "What to do:",
        f"1. Read the full log: gh run view {run_id} --log-failed",
        f"2. Find the cause in the code of {base} and correct it. Run the failed tests locally before the push.",
        "3. If a test fails without a relation to the last changes (it is unstable), make it stable. Do not delete it, "
        "do not skip it and do not weaken what it checks.",
        "4. Change only what the correction needs.",
        "If the cause is outside the repository (a service that does not respond, a secret, the runner), report "
        "blocked and say what is necessary.",
    ) if x is not None)


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
        "If the repository must change: open a draft PR before the first edit and run "
        f"aiflow report {t['id']} --role worker --status started --pr <number>. Then do the work, commit, push, "
        "gh pr ready, and run "
        f"aiflow report {t['id']} --role worker --status ready_for_review --pr <number> --summary \"<1-2 lines>\" "
        "and end your turn. Do not start the review yourself: aiflow wakes you when it is your turn."
    ), cfg)


def discuss_prompt(store: Store, t: dict, cfg: dict) -> str:
    """Задача kind: discuss — обсуждение с пользователем в сессии, PR только по договорённости."""
    tid = t["id"]
    return with_search_rule((
        f"aiflow discussion {tid}: {t.get('title', '')}\n"
        f"Task file: {store.task_path(tid)}. Read it in full. Base branch: {cfg['base_branch']}.\n"
        + deps_note(store, t) + neighbours_note(store, tid) +
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
        f"git fetch origin; git merge origin/{cfg['base_branch']} (do not rebase and do not force-push); "
        "resolve the conflicts; run the tests; git push. Then run aiflow review --pr "
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
