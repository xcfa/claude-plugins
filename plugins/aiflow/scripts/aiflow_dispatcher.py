"""Диспетчер очереди: один проход (tick) ведёт каждую задачу по стадиям — запускает и будит сессии,
читает их отчёты, ставит PR в очередь ревью, зовёт человека.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from aiflow_core import (
    AMEND_MARKER, AiflowError, ENTRY, FINISHED, HISTORY_KEEP, HUMAN_CODES, HUMAN_MARKER, IMPLEMENTING, LIMIT_HITS_MAX,
    LIMIT_LOG_TAIL, LIMIT_RE, MANUAL_PAUSES, MARKERS, PATH_ASK_RE, PAUSE_LIFTS, REVIEW_ACTIONS, STAGE_LABEL, STATION,
    TALK_KINDS, blocking_pauses, kept_pause_text, kept_pauses, queue_pauses,
    TERMINAL, WHAT_TO_DO, WORKER_STAGES, after, age_minutes, ci_summary, context_choice, inbox_dirs, is_direct,
    is_human_comment, iso, now, owner_alive, parse_iso, pid_alive, planner_choice, read_json, run, short_reason,
    visited_stations, worker_choice, write_json,
)
from aiflow_store import Store, normalize_task, sync_queue
from aiflow_tools import Claude, GH
from aiflow_usage import collect_coordinator_usage, last_words, reviewer_usage, transcript_age_minutes, transcript_usage
from aiflow_prompts import (
    base_fix_text,
    RESUME_FINISH, STAGE_ROLE, amend_prompt, ask_prompt, ci_fix_prompt, dep_task, direct_prompt, discuss_prompt,
    launch_amendments, mark_delivered, merge_workflows, plan_check_prompt, planner_prompt, resume_prompt,
    review_prompt, review_result_prompt, revise_prompt, split_dep, worker_prompt,
)


def spawn_plan_check_process(store: "Store", t: dict, cwd: str) -> int | None:
    """Запустить `aiflow review-plan --pr N` отдельным процессом. → pid или None."""
    return spawn_review_process(store, t, cwd, command="review-plan")


def spawn_review_process(store: "Store", t: dict, cwd: str, command: str = "review") -> int | None:
    """Запустить `aiflow review --pr N` отдельным процессом в рабочей копии задачи. → pid или None."""
    log = store.runs / t["id"] / "auto-review.log"
    try:
        out = open(log, "ab")
        flags = {"creationflags": 0x08000000 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
        env = dict(os.environ, PYTHONUTF8="1")
        proc = subprocess.Popen([sys.executable, "-X", "utf8", str(ENTRY), command, "--pr", str(t["pr"])],
                                cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env, **flags)
        out.close()
        return proc.pid
    except OSError:
        return None


BASE_RERUN_WAIT = 10  # минут: столько ждём, пока GitHub покажет перезапущенную сборку как идущую
_BASE_SEEN: dict[tuple[str, str], tuple[float, bool]] = {}


# ---------------------------------------------------------------- dispatcher

class Dispatcher:
    def __init__(self, store: Store, claude=None, gh=None, spawn_review=None, spawn_plan_check=None):
        """claude, gh и запуск фонового ревью можно подменить (тесты): по умолчанию — настоящие."""
        self.spawn_review = spawn_review or spawn_review_process
        self.spawn_plan_check = spawn_plan_check or spawn_plan_check_process
        self.store = store
        self.root = store.root
        self.claude = claude or Claude(self.root)
        self.gh = gh or GH(self.root)
        self.events: list[dict] = []
        self.cfg: dict = {}
        self.q: dict = {}
        self.sessions: dict[str, dict] = {}
        self.by_id: dict[str, dict] = {}
        self.tasks: list[dict] = []

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

    def set_state(self, t: dict, state: str, note: str = "", code: str = "") -> None:
        """code — код причины перехода (ключ HUMAN_CODES): по нему считает статистика, note — текст для человека."""
        if t.get("state") != state:
            t.setdefault("history", []).append({"at": iso(), "from": t.get("state"), "to": state, "note": note,
                                                **({"code": code} if code else {})})
            seen = visited_stations(t) | {STATION[s] for s in (t.get("state"), state) if s in STATION and s != "done"}
            t["stations"] = sorted(seen)
            del t["history"][:-HISTORY_KEEP]
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
        t["report_prev"] = t.get("report_seen")
        t["report_seen"] = rep.get("rid")
        # сессия продвинулась — счётчики автовозобновлений, напоминаний и пауз по лимиту заново
        (t.get("auto_resumed") or {}).pop(role, None)
        (t.get("nudged") or {}).pop(role, None)
        t.pop("limit_hits", None)
        return rep

    def early_pr(self, t: dict, rep: dict) -> None:
        """Задача без плана открывает черновой PR в начале работы: запомнить его, чтобы он был виден
        в панели сразу. Чужой или закрытый номер не берём — итоговый отчёт всё равно проверит verify_pr."""
        n = rep.get("pr")
        if not n or t.get("pr"):
            return
        try:
            pr = self.gh.pr(int(n), "state,headRefName")
        except AiflowError:
            return
        if pr.get("state") == "OPEN" and pr.get("headRefName") == f"ai/{t['id']}":
            t["pr"] = int(n)

    def verify_pr(self, t: dict, role: str, rep: dict, *, plan: bool) -> bool:
        """Отчёт называет PR — проверить, что это PR этой задачи, а не опечатка или чужой номер.
        Неверный отчёт возвращается сессии (до двух раз), потом — человеку."""
        n = int(rep["pr"])
        try:
            pr = self.gh.pr(n, "state,headRefName,isDraft,body")
        except AiflowError:
            t["report_seen"] = t.get("report_prev")  # GitHub не ответил: разберём отчёт в следующий проход
            raise
        want = f"ai/{t['id']}"
        problem = fix = ""  # problem — человеку (панель, событие), fix — сессии
        if pr.get("state") != "OPEN":
            problem = f"PR #{n} не открыт ({pr.get('state')})"
            fix = f"PR #{n} is not open ({pr.get('state')})"
        elif pr.get("headRefName") != want:
            problem = f"PR #{n} открыт из ветки {pr.get('headRefName')}, а ветка задачи — {want}"
            fix = f"PR #{n} is from the branch {pr.get('headRefName')}, but the task branch is {want}"
        elif plan and "<!-- ai-plan" not in (pr.get("body") or ""):
            problem = f"в описании PR #{n} нет плана по шаблону (маркер <!-- ai-plan v1 -->)"
            fix = f"the description of PR #{n} has no plan by the template (marker <!-- ai-plan v1 -->)"
        elif not plan and pr.get("isDraft"):
            problem = f"PR #{n} ещё draft"
            fix = f"PR #{n} is still a draft: run gh pr ready {n}"
        if not problem:
            (t.get("bounced") or {}).pop(role, None)
            return True
        counts = t.setdefault("bounced", {})
        counts[role] = counts.get(role, 0) + 1
        self.store.log(t["id"], {"report_rejected": problem})
        text = (f"The report {rep.get('status')} for task {t['id']} was rejected: {fix}. Correct this and report "
                f"again with aiflow report {t['id']} --role {role} and the correct PR number.")
        if counts[role] > 2 or not self.wake(t, role, text):
            self.to_human(t, f"отчёт {role} не принят: {problem}", "report")
            return False
        self.touch(t, role)
        self.emit(t, "report_rejected", f"{t['id']}: отчёт {role} не принят — {problem}", key=f"rejected:{rep.get('rid')}")
        return False

    def touch(self, t: dict, role: str) -> None:
        """Сессию только что разбудили: её снимок в claude agents устарел, отсчёты — заново."""
        e = self.cur(t, role)
        if e:
            e["resumed_at"] = iso()
        t["state_at"] = iso()

    def fresh(self, e: dict | None) -> bool:
        return bool(e and e.get("resumed_at") and age_minutes(e["resumed_at"]) < 2)

    def nudge(self, t: dict, role: str, state: str, reason: str) -> None:
        """Сессия закончила ход без отчёта: один раз напомнить ей, и только потом звать человека."""
        e = self.cur(t, role)
        if self.fresh(e):
            return
        counts = t.setdefault("nudged", {})
        if counts.get(role, 0) < 1:
            text = (f"You ended your turn on task {t['id']} without a final report, so the queue does not know the "
                    "result. If the work is not complete, complete it. If you need a reply or a decision from a human, "
                    f"run aiflow report {t['id']} --role {role} --status blocked --summary \"<what you need>\". "
                    + RESUME_FINISH.get(state, "Report by your instructions.").format(id=t["id"], pr=t.get("pr")))
            if self.wake(t, role, text):
                counts[role] = counts.get(role, 0) + 1
                self.touch(t, role)
                self.emit(t, "nudged", f"{t['id']}: {role} закончил ход без отчёта — напомнили", key=f"nudge:{role}:{iso()}")
                return
        self.to_human(t, reason, "silent")

    def blocked(self, t: dict, role: str, e: dict) -> None:
        """Сессия ждёт ответа: сказать человеку всегда; заодно проверить, не лимит ли это."""
        w = (self.sess(e) or {}).get("waitingFor") or "ответ"
        who = "планировщик" if role == "planner" else role
        # чего именно она ждёт — в её последнем сообщении: без него в панели только «сессия ждёт ответа»
        said = last_words(e.get("session_id") or (self.sess(e) or {}).get("sessionId"))
        t["waits"] = {"sid": e["id"], "text": said}
        if PATH_ASK_RE.search(said):
            # пути из её слов: `…` с косой чертой; пусто — панель всё равно предложит кнопку
            asked = [re.sub(r"^(\./)+", "", m) for m in re.findall(r"[`\"']([^`\"'\s]+/[^`\"'\s]*)[`\"']", said)]
            asked = [p for p in asked if p and not p.endswith("/")]  # папку целиком не предлагаем: только файлы
            t["path_request"] = sorted(set((t.get("path_request") or []) + asked))
            t["path_asked"] = True
        self.emit(t, "needs_input", f"{t['id']}: {who} ждёт вас в своей сессии ({w})"
                  + (f": {short_reason(said, 200)}" if said else ""), key=f"blocked:{e['id']}:{w}")
        self.check_limit(e, t)

    def try_resume(self, t: dict, role: str, state: str, why: str, auto: bool) -> bool:
        """Возобновить ту же сессию роли. auto — монитор сам, не больше auto_resume раз подряд."""
        e = self.cur(t, role)
        if not e:
            return False
        counts = t.setdefault("auto_resumed", {})
        if auto:
            if counts.get(role, 0) >= int(self.cfg.get("auto_resume", 0) or 0):
                return False
            counts[role] = counts.get(role, 0) + 1
        if not self.wake(t, role, resume_prompt(t, state, why, self.cfg['base_branch'])):
            return False
        e = self.cur(t, role)
        e["resumed_at"] = iso()
        verb = "возобновлена автоматически" if auto else "возобновлена"
        self.emit(t, "resumed", f"{t['id']}: сессия {role} {verb} ({why})", key=f"resumed:{role}:{iso()}")
        return True

    def lost(self, t: dict, role: str, state: str, why: str, reason: str) -> None:
        """Сессия остановилась или пропала: сначала автовозобновление, иначе — к человеку."""
        e = self.cur(t, role)
        if self.fresh(e):
            return  # только что возобновили: claude agents ещё может показывать старое состояние
        if not self.try_resume(t, role, state, why, auto=True):
            self.to_human(t, reason, "session")

    def release_lock(self, t: dict) -> None:
        self.q["review_locks"] = [i for i in self.q.get("review_locks") or [] if i != t["id"]]

    def pause(self, reason: str, minutes: float | None, text: str, note: str = "") -> None:
        """Добавить причину паузы. Причины независимы: одна не затирает другую."""
        pauses = self.q.setdefault("pauses", {})
        if reason in pauses:
            return
        until = iso(now() + dt.timedelta(minutes=minutes)) if minutes else None
        pauses[reason] = {"at": iso(), "until": until, "note": note}
        sync_queue(self.q)
        self.emit(None, "paused", text)

    def unpause(self, reasons, text: str) -> None:
        """Снять причины паузы. Очередь продолжает, когда не осталось причин, которые держат её целиком."""
        pauses = self.q.setdefault("pauses", {})
        gone = [r for r in ([reasons] if isinstance(reasons, str) else reasons) if pauses.pop(r, None) is not None]
        if not gone:
            return
        sync_queue(self.q)
        if queue_pauses(pauses):
            self.emit(None, "pause_kept", f"{text}; очередь всё ещё на паузе: {', '.join(queue_pauses(pauses))}")
        else:
            self.emit(None, "unpaused", text + ("; ревью и мерж ждут: базовая ветка красная" if pauses else ""))

    def check_limit(self, entry: dict | None, t: dict | None = None) -> bool:
        """Сессия упёрлась в лимит использования? Тогда пауза очереди. Признак — текст в конце лога,
        поэтому одна и та же сессия без продвижения ставит паузу не больше LIMIT_HITS_MAX раз подряд:
        дальше это, скорее всего, слова из самой задачи, и сессию разбирают как обычно."""
        if not entry:
            return False
        text = self.claude.logs(entry["id"])[-LIMIT_LOG_TAIL:]
        s = self.sess(entry) or {}
        if not (LIMIT_RE.search(text) or LIMIT_RE.search(json.dumps(s, ensure_ascii=False))):
            return False
        if "rate_limit" in (self.q.get("pauses") or {}):
            return True
        if t is not None:
            hits = t.get("limit_hits") or {}
            n = hits.get("n", 0) + 1 if hits.get("sid") == entry["id"] else 1
            if n > LIMIT_HITS_MAX:
                return False
            t["limit_hits"] = {"sid": entry["id"], "n": n}
        mins = self.cfg["rate_limit_pause_minutes"]
        self.pause("rate_limit", mins, f"лимит использования: очередь на паузе на {mins} мин")
        return True

    def to_human(self, t: dict, reason: str, action: str = "") -> None:
        """Задача ждёт человека. reason — что случилось, action — ключ WHAT_TO_DO: что от него нужно."""
        self.release_lock(t)
        full, reason = str(reason or ""), short_reason(reason) or "нужен человек"
        if "защищ" in full.lower() or "protected" in full.lower():
            action = "paths"
        t["human_reason"] = reason
        t["human_action"] = WHAT_TO_DO.get(action, "")
        role = STAGE_ROLE.get(t.get("state", ""))
        # Запоминаем, в каком состоянии была сессия: если человек ответит ей сам и она
        # снова заработает, задача вернётся в свою стадию (см. adv_needs_human).
        t["human_watch"] = {"role": role, "seen": self.sstate(self.cur(t, role)) if role else None,
                            "report": t.get("report_seen")}
        self.set_state(t, "needs_human", reason, code=action if action in HUMAN_CODES else "other")
        if t.get("pr"):
            self.gh.add_label(t["pr"], self.cfg["labels"]["needs_human"])
        todo = f" Что сделать: {t['human_action']}" if t["human_action"] else ""
        self.emit(t, "needs_human", f"{t['id']}: нужен человек — {reason}.{todo}", key=f"human:{iso()[:16]}:{reason[:40]}")

    # -- launching
    def launch(self, t: dict, role: str, prompt: str, model=None, effort=None) -> bool:
        red = (self.q.get("pauses") or {}).get("integration_red")
        if red and role == "worker" and not t.get("base_fix"):
            # иначе исполнитель, увидев упавшие тесты базовой ветки, чинит их у себя: вторая починка того же
            prompt += (f"\nThe CI of the base branch {self.cfg['base_branch']} is red now"
                       + (f" ({red['note']})" if str(red.get("note") or "").startswith("http") else "")
                       + ". Another task corrects it: do not correct that failure in your PR. Tests that fail on "
                         "the base branch for the same cause are not your regression.")
        name = f"{role}: {t['id']}"
        t["launching"] = {"role": role, "name": name, "at": iso()}
        self.store.save_task(t)
        sid, out = self.claude.launch(f"aiflow:{role}", name, prompt, model=model, effort=effort, worktree=t["id"],
                                      mode=self.cfg.get("permission_mode"),
                                      window=context_choice(self.cfg, t, role))
        if not sid:
            low = out.lower()
            if "not trusted" in low:
                self.pause("trust", None, "папка репозитория не помечена как доверенная: "
                           "откройте в ней claude интерактивно и примите доверие, затем aiflow resume")
            t.pop("launching", None)
            self.store.log(t["id"], {"launch_failed": out[-800:]})
            self.emit(t, "error", f"{t['id']}: не удалось запустить {role}: {out[-200:]}", key=f"launchfail:{role}:{out[-60:]}")
            return False
        self.adopt(t, role, sid, name, model, effort)
        return True

    def adopt(self, t: dict, role: str, sid: str, name: str, model=None, effort=None) -> None:
        entry = {"id": sid, "name": name, "started_at": iso(), "session_id": None}
        if model:
            entry["model"] = model
        if effort:
            entry["effort"] = effort
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
        cwd = entry.get("cwd") or (self.sess(entry) or {}).get("cwd") if entry else None
        model, effort = self.session_model(t, role, entry)
        new_id, out = self.claude.resume(sid_full, prompt, mode=self.cfg.get("permission_mode"), cwd=cwd,
                                         model=model, effort=effort,
                                         window=context_choice(self.cfg, t, role),
                                         name=entry.get("name") or f"{role}: {t['id']}")
        if new_id and new_id != entry["id"]:
            self.adopt(t, role, new_id, entry.get("name", f"{role}: {t['id']}"), model, effort)
            if cwd:
                self.cur(t, role)["cwd"] = cwd
        elif not new_id:
            self.store.log(t["id"], {"resume_failed": out[-800:]})
            return False
        self.store.log(t["id"], {"woke": role})
        return True

    def session_model(self, t: dict, role: str, entry: dict) -> tuple:
        """Модель и effort, с которыми сессия была запущена. У записей старых версий их нет — тогда
        по настройкам: сессию планировщика, который сделал задачу сам и ведёт её ревью, запускали как
        планировщика."""
        if entry.get("model"):
            return entry["model"], entry.get("effort")
        as_planner = role == "planner" or str(entry.get("name") or "").startswith("planner:")
        if as_planner:
            ask, talk = t.get("kind") == "ask", t.get("kind") in TALK_KINDS
            return planner_choice(self.cfg, t, (
                self.cfg.get("ask_model" if ask else "discuss_model") or ("sonnet" if ask else "opus"),
                self.cfg.get("ask_effort" if ask else "discuss_effort") or None) if talk else None)
        return worker_choice(self.cfg, t)

    def resolve_launching(self, t: dict) -> None:
        info = t.get("launching")
        if not info:
            return
        for sid, s in self.sessions.items():
            if s.get("name") == info.get("name"):
                self.adopt(t, info["role"], sid, info["name"], *self.session_model(t, info["role"], {}))
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
        tasks = self.tasks = self.store.tasks()
        by_id = self.by_id = {t["id"]: t for t in tasks}
        self.gh.prefetch(t.get("pr") for t in tasks if t.get("state") not in TERMINAL)
        self.process_commands(by_id)
        self.check_pause_timer()
        self.check_base()
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
        pauses = self.q.get("pauses") or {}
        if not queue_pauses(pauses):
            # Красная базовая ветка держит только ревью и мерж: планирование и реализация идут. Задачу,
            # которая чинит ветку, не держит и она.
            self.schedule(tasks, reviewable=[t for t in tasks if not blocking_pauses(pauses, t)])
        self.collect_usage(tasks)
        collect_coordinator_usage(self.store)
        self.check_batch_done(tasks)
        self.store.save_queue(self.q)
        return self.events

    def fill_session_ids(self, t: dict) -> None:
        for lst in (t.get("sessions") or {}).values():
            for e in lst:
                s = self.sessions.get(e["id"])
                if s and not e.get("session_id") and s.get("sessionId"):
                    e["session_id"] = s["sessionId"]
                if s and s.get("cwd") and not e.get("cwd"):
                    e["cwd"] = s["cwd"]  # для пробуждения в той же рабочей копии (запоминается один раз)

    def process_commands(self, by_id: dict[str, dict]) -> None:
        for c in self.store.take_commands():
            kind = c.get("cmd")
            if kind == "pause":
                self.pause("manual", None, "очередь поставлена на паузу вручную", note=c.get("reason", ""))
            elif kind == "resume":
                # паузы «нет базовой ветки» и «красная ветка» снимает только их условие
                kept = [r for r in kept_pauses(self.q.get("pauses")) if r in queue_pauses(self.q.get("pauses"))]
                if kept and not any(r in MANUAL_PAUSES for r in self.q.get("pauses") or {}):
                    # снимать нечего, а очередь стоит: молча проглоченное «продолжить» выглядит как сбой
                    self.emit(None, "pause_kept", "«Продолжить очередь» эту паузу не снимает: " + kept_pause_text(kept))
                self.unpause(MANUAL_PAUSES, "очередь возобновлена")
            elif kind == "amend" and c.get("id") in by_id:
                t = by_id[c["id"]]
                if t.get("state") in TERMINAL:
                    self.emit(t, "error", f"{t['id']}: задача уже завершена — дополнение не принято, поставьте новую задачу")
                elif (c.get("text") or "").strip():
                    item = self.add_amendment(t, c["text"], c.get("source") or "cli")
                    try:
                        self.post_amendment(t, item)
                    except (AiflowError, OSError) as e:
                        self.store.log(t["id"], {"amend_comment_failed": str(e)[:300]})
                    self.store.save_task(t)
            elif kind == "retry" and c.get("id") in by_id:
                t = by_id[c["id"]]
                t["retry_requested"] = True
                self.store.save_task(t)
            elif kind == "paths_allowed" and c.get("id") in by_id:
                # задача не в «нужен человек»: её сессия спросила разрешение и ждёт ответа в чате
                t = by_id[c["id"]]
                role = "worker" if self.cur(t, "worker") else "planner"
                if t.get("state") not in TERMINAL and self.sstate(self.cur(t, role)) != "working":
                    self.wake(t, role, f"The user allowed these protected paths for task {t['id']}: "
                              + ", ".join(c.get("paths") or []) + ". You can change them now. Continue the work.")
                    t.pop("waits", None)
                    self.store.save_task(t)

    def check_pause_timer(self) -> None:
        due = [r for r, p in (self.q.get("pauses") or {}).items()
               if p.get("until") and now() >= (parse_iso(p["until"]) or now())]
        if due:
            self.unpause(due, "пауза по лимиту закончилась, очередь продолжает")

    def check_base(self) -> None:
        """Нет базовой ветки на origin — пауза, а не сессии, которые упрутся в это."""
        base = self.cfg["base_branch"]
        key = (str(self.root), base)
        cached = _BASE_SEEN.get(key)
        # Есть ветка — проверять раз в 10 минут; нет или не удалось проверить — раз в минуту.
        if cached is None or time.time() - cached[0] > (600 if cached[1] else 60):
            try:
                p = run(["git", "ls-remote", "--heads", "origin", base], cwd=self.root, timeout=30)
                found = bool(p.stdout.strip()) if p.returncode == 0 else None
            except AiflowError:
                found = None
            cached = _BASE_SEEN[key] = (time.time(), found)
        if cached[1] is None:
            return  # сеть или доступ: не повод останавливать очередь
        if not cached[1]:
            self.pause("base_missing", None, f"базовой ветки {base} нет на origin: поправьте base_branch "
                       "в .aiflow/config.json либо создайте ветку")
        else:
            self.unpause("base_missing", f"базовая ветка {base} есть на origin, очередь продолжает")

    def check_integration(self) -> None:
        r = self.gh.last_push_run(self.cfg["base_branch"])
        if not r or r.get("status") != "completed":
            return
        if r.get("conclusion") in ("failure", "timed_out", "startup_failure"):
            auto = str(self.cfg.get("base_fix") or "off").lower() != "off"
            # note — адрес красной сборки: панель ведёт на неё ссылкой
            self.pause("integration_red", None, f"ветка {self.cfg['base_branch']} покраснела после мержа: "
                       f"{r.get('url', '')}. Задачи планируются и пишутся как обычно; {PAUSE_LIFTS['integration_red']}"
                       + (". Чиню сам: перезапускаю упавшие задания, а если упадут снова — ставлю задачу на починку"
                          if auto else ""), note=r.get("url") or "")
            if auto:
                self.fix_base(r)
        else:
            self.q.pop("base_fix", None)
            self.unpause("integration_red", f"ветка {self.cfg['base_branch']} снова зелёная: ревью и мерж продолжают")

    def fix_base(self, r: dict) -> None:
        """Красная базовая ветка чинится без человека. Сначала перезапускаются упавшие задания сборки:
        нестабильный тест второй раз обычно проходит, и это не стоит ни токена. Упали снова — ставится задача
        на починку. Что уже сделано для этой сборки, помнит queue.json (base_fix): sha, reruns, task."""
        sha = str(r.get("headSha") or r.get("url") or "")
        fix = self.q.get("base_fix") or {}
        if fix.get("sha") != sha:
            fix = {"sha": sha, "url": r.get("url") or "", "reruns": 0}  # покраснел другой коммит: всё заново
        self.q["base_fix"] = fix
        if fix.get("task"):
            return
        base, attempt = self.cfg["base_branch"], int(r.get("attempt") or 1)
        if fix.get("rerun_attempt") == attempt and age_minutes(fix.get("rerun_at")) < BASE_RERUN_WAIT:
            return  # перезапуск запрошен, но GitHub ещё показывает прежнюю, упавшую попытку
        try:
            limit = int(self.cfg.get("base_fix_reruns") or 0)
        except (TypeError, ValueError):
            limit = 0
        if fix["reruns"] < limit and r.get("databaseId") and fix.get("rerun_attempt") != attempt \
                and self.gh.rerun_failed(r["databaseId"]):
            fix.update(reruns=fix["reruns"] + 1, rerun_at=iso(), rerun_attempt=attempt)
            self.emit(None, "base_fix", f"ветка {base} красная: перезапустил упавшие задания сборки {fix['url']}. "
                                        "Упадут снова — поставлю задачу на починку")
            return
        fix["task"] = self.enqueue_base_fix(r, fix)

    def enqueue_base_fix(self, r: dict, fix: dict) -> str:
        """Поставить задачу на починку базовой ветки. Плана у неё нет: исполнитель начинает сразу, а пауза
        «ветка красная» её не останавливает (blocking_pauses)."""
        base, sha = self.cfg["base_branch"], str(r.get("headSha") or "")
        tid = "fix-base-" + (sha[:7].lower() if re.fullmatch(r"[0-9a-fA-F]{7,}", sha) else now().strftime("%m%d%H%M"))
        if self.store.task(tid):
            return tid  # задача для этого коммита уже есть (queue.json потерял запись о ней)
        recent = sorted((t for t in self.by_id.values() if t.get("state") == "merged" and t.get("pr")),
                        key=lambda t: t.get("state_at") or "", reverse=True)[:3]
        log = self.gh.failed_log(r["databaseId"]) if r.get("databaseId") else ""
        t = normalize_task({"id": tid, "title": f"Починить {base}: CI красный на {sha[:7] or 'последнем коммите'}",
                            "text": base_fix_text(base, r, log, recent, fix.get("reruns") or 0)})
        t.update(direct=True, complexity="complex", base_fix=True)
        self.store.save_task(t)
        self.store.log(tid, {"created": "base_fix", "run": r.get("url")})
        self.by_id[tid] = t
        self.tasks.append(t)
        self.emit(t, "base_fix", f"{tid}: ветка {base} красная" + (" и после перезапуска сборки" if fix.get("reruns") else "")
                  + f" — поставил задачу на починку. Сборка: {r.get('url', '')}")
        return tid

    def advance(self, t: dict) -> None:
        st = t.get("state", "new")
        if t.get("retry_requested") and st == "needs_human":
            return self.do_retry(t)
        if self.handle_amendments(t):
            return  # сессию только что разбудили с дополнением: её снимок в этом проходе устарел
        getattr(self, f"adv_{st}", lambda _t: None)(t)

    # -- дополнения пользователя к идущей задаче
    def add_amendment(self, t: dict, text: str, source: str, at: str | None = None, author: str | None = None) -> dict:
        am = t.setdefault("amendments", [])
        item = {"n": len(am) + 1, "at": at or iso(), "text": text.strip(), "source": source, "author": author,
                "delivered_at": None}
        am.append(item)
        self.store.log(t["id"], {"amendment": item})
        self.emit(t, "amended", f"{t['id']}: дополнение №{item['n']} принято"
                  + (f" (комментарий {author} в PR #{t['pr']})" if source == "comment" else ""), key=f"amend:{item['n']}")
        return item

    def post_amendment(self, t: dict, item: dict) -> None:
        """Дополнение из панели или CLI — ещё и комментарием в PR: для истории и для проверки в aiflow review."""
        if not t.get("pr"):
            return
        tmp = self.store.runs / t["id"] / f"amend-{item['n']}.md"
        tmp.write_text(f"{HUMAN_MARKER}\n{AMEND_MARKER}\n**Дополнение к задаче №{item['n']}**\n\n{item['text']}\n",
                       encoding="utf-8")
        self.gh.call("pr", "comment", str(t["pr"]), "--body-file", str(tmp))
        tmp.unlink(missing_ok=True)

    def scan_comments(self, t: dict) -> None:
        """Комментарии человека в PR после начала реализации — дополнения к задаче."""
        if not t.get("pr") or not (t.get("state") in WORKER_STAGES or t.get("state") == "approved"
                                   or t.get("plan_approved")):
            return
        seen = t.setdefault("comments_seen_at", t.get("plan_seen_at") or iso())
        comments = self.gh.pr(t["pr"], "comments").get("comments") or []
        fresh = sorted((c for c in comments if is_human_comment(c) and after(c.get("createdAt"), seen)),
                       key=lambda c: parse_iso(c.get("createdAt")))
        for c in fresh:
            self.add_amendment(t, c.get("body") or "", "comment", at=c.get("createdAt"),
                               author=(c.get("author") or {}).get("login"))
        if fresh:
            t["comments_seen_at"] = fresh[-1]["createdAt"]

    def sync_inbox(self, t: dict) -> None:
        """Отметить дополнения, которые хук уже отдал работающей сессии."""
        _, done = inbox_dirs()
        if not done.is_dir():
            return
        for f in done.glob("*.json"):
            d = read_json(f, {}) or {}
            if d.get("project") != str(self.root) or d.get("task") != t["id"]:
                continue
            ns = set(d.get("ns") or [])
            for a in t.get("amendments") or []:
                if a["n"] in ns and not a.get("delivered_at"):
                    a["delivered_at"], a["via"] = d.get("at") or iso(), "hook"
            f.unlink(missing_ok=True)

    def session_uuid(self, e: dict | None) -> str | None:
        return (e or {}).get("session_id") or (self.sess(e) or {}).get("sessionId")

    def drop_inbox(self, t: dict) -> None:
        inbox, _ = inbox_dirs()
        if not inbox.is_dir():
            return
        for lst in (t.get("sessions") or {}).values():
            for e in lst:
                sid = self.session_uuid(e)
                if sid:
                    (inbox / f"{sid}.json").unlink(missing_ok=True)

    def handle_amendments(self, t: dict) -> bool:
        st = t.get("state")
        if st in TERMINAL or st == "needs_human":
            if t.get("amendments"):
                self.drop_inbox(t)  # непустые входящие заставляют хук запускать Python в каждой сессии
            return False
        try:
            self.scan_comments(t)
        except AiflowError as e:
            self.store.log(t["id"], {"scan_comments_failed": str(e)[:300]})
        self.sync_inbox(t)
        pending = [a for a in t.get("amendments") or [] if not a.get("delivered_at")]
        if not pending and t.get("amendments"):
            self.drop_inbox(t)
        if not pending or st in ("new", "approved"):
            return False  # уйдут в промпт запуска сессии
        role = "worker" if st in WORKER_STAGES else "planner"
        e = self.cur(t, role)
        s = self.sstate(e)
        inbox, _ = inbox_dirs()
        sid = self.session_uuid(e)
        if s in ("working", "blocked"):
            if sid:
                data = {"project": str(self.root), "task": t["id"], "ns": [a["n"] for a in pending],
                        "text": amend_prompt(t, role, st, pending, live=True)}
                f = inbox / f"{sid}.json"
                if (read_json(f, {}) or {}).get("ns") != data["ns"]:
                    write_json(f, data)
            return False
        if s != "done":
            return False  # failed/stopped/пропала — доставим, когда сессию возобновят
        if sid:
            (inbox / f"{sid}.json").unlink(missing_ok=True)
        if not self.wake(t, role, amend_prompt(t, role, st, pending, live=False)):
            return False
        mark_delivered(t, "wake")
        if role == "planner":
            t["revising"] = True
            if st == "answered":
                self.set_state(t, "discussing", "продолжение вопроса")
        else:
            if st == "in_review" and t.get("pr"):
                self.gh.call("pr", "merge", str(t["pr"]), "--disable-auto")
            t.pop("approved_sha", None)
            t.pop("approved_at", None)
            self.store.approved_path(t["id"]).unlink(missing_ok=True)  # прежнее одобрение дополнения не видело
            self.release_lock(t)
            self.set_state(t, "in_progress", "дополнение пользователя")
        t["state_at"] = iso()  # отсчёт «закончил ход без отчёта» — заново: сессию только что разбудили
        self.emit(t, "amend_delivered", f"{t['id']}: дополнение передано {'планировщику' if role == 'planner' else 'исполнителю'}",
                  key=f"amend_delivered:{pending[-1]['n']}")
        return True

    def adv_discussing(self, t: dict) -> None:
        rep = self.new_report(t, "planner")
        if rep:
            st = rep.get("status")
            if st == "plan_ready" and rep.get("pr"):
                if not self.verify_pr(t, "planner", rep, plan=True):
                    return
                t["pr"] = int(rep["pr"])
                if rep.get("complexity") in ("simple", "complex"):
                    t["complexity"] = rep["complexity"]
                self.save_notes(t, rep)
                t["plan_seen_at"] = iso()
                if self.start_plan_check(t):
                    return
                self.set_state(t, "plan_review", "обсуждение: договорились, план готов")
                self.emit(t, "plan_ready", f"{t['id']}: обсуждение завершено, план готов — PR #{t['pr']}",
                          key=f"plan:{rep['rid']}")
                return
            if st == "answered":
                t["answer"] = {"summary": rep.get("summary") or "", "details": rep.get("details") or "", "at": iso()}
                self.set_state(t, "answered", "ответ дан")
                t["state_at"] = iso()
                self.emit(t, "answered", f"{t['id']}: {rep.get('summary') or 'ответ в сессии'}", key=f"answered:{rep['rid']}")
                return
            if st == "done":
                self.set_state(t, "done", rep.get("summary") or "обсуждение завершено")
                self.emit(t, "done", f"{t['id']}: обсуждение завершено — {rep.get('summary') or 'без задачи'}", key="done")
                return
            if st in ("error", "blocked"):
                return self.to_human(t, rep.get("summary") or "сессия обсуждения сообщила о проблеме", "asks")
        e = self.cur(t, "planner")
        st = self.sstate(e)
        if st in ("failed", "stopped"):
            if not self.check_limit(e, t):
                self.lost(t, "planner", "discussing", f"сессия {st}", "сессия обсуждения остановилась")
        elif st in ("done", "blocked"):
            # ход закончен — ждём ответа пользователя в сессии; напоминаем один раз на каждый ход
            mark = (self.sess(e) or {}).get("updatedAt") or e["id"]
            what = "вопрос" if t.get("kind") == "ask" else "обсуждение"
            self.emit(t, "needs_input", f"{t['id']}: {what} ждёт вас — откройте сессию (панель или "
                      f"claude attach {e['id']})", key=f"discuss:{e['id']}:{mark}")
        elif e and st is None and age_minutes(e.get("resumed_at") or e.get("started_at")) > 10:
            self.lost(t, "planner", "discussing", "сессия пропала", "сессия обсуждения пропала из claude agents")

    def adv_answered(self, t: dict) -> None:
        """Ответ дан. Пользователь может продолжить в сессии (тогда она отчитается план или новый ответ)
        или промолчать — тогда вопрос закрывается без плана."""
        rep = self.new_report(t, "planner")
        if rep:
            t["report_seen"] = None  # пусть adv_discussing разберёт тот же отчёт
            self.set_state(t, "discussing", "продолжение вопроса")
            return self.adv_discussing(t)
        e = self.cur(t, "planner")
        if self.sstate(e) == "working":
            self.set_state(t, "discussing", "продолжение вопроса")
            return
        ttl = float(self.cfg.get("answered_ttl_hours") or 24)
        if age_minutes(t.get("state_at")) > ttl * 60:
            self.set_state(t, "done", "вопрос закрыт: продолжения не было")
            self.adv_merged(t)

    def save_notes(self, t: dict, rep: dict) -> None:
        """Заметки планировщика из отчёта plan_ready — в файл задачи: отчёт перезапишут следующие отчёты,
        а исполнителю заметки нужны при запуске. Отчёт без заметок прежние не стирает."""
        notes = (rep.get("details") or "").strip()
        if notes:
            path = self.store.notes_path(t["id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(notes + "\n", encoding="utf-8")

    def adv_planning(self, t: dict) -> None:
        rep = self.new_report(t, "planner")
        if rep:
            st = rep.get("status")
            if st == "self_execute":
                # планировщик оценил задачу как простую и делает её сам: это уже не планирование, а работа.
                # Стадия та же (итог придёт отчётом ready_for_review или done), но панель показывает её как реализацию.
                t["route"], t["complexity"] = "self", "simple"
                t["route_note"] = short_reason(rep.get("summary") or "", 160)
                self.early_pr(t, rep)
                self.emit(t, "self_start", f"{t['id']}: простая — планировщик делает сам"
                          + (f": {t['route_note']}" if t["route_note"] else ""), key=f"selfstart:{rep['rid']}")
                return
            if st == "delegate":
                # trivial — лёгкая задача: исполнитель на самой дешёвой модели (worker_model.trivial)
                t["complexity"] = "trivial" if rep.get("complexity") == "trivial" else "simple"
                t["direct"] = True
                t["brief"] = rep.get("details") or rep.get("summary") or ""
                note = rep.get("summary") or ""
                self.emit(t, "delegated", f"{t['id']}: {'лёгкая' if t['complexity'] == 'trivial' else 'простая'}"
                          " — планировщик поручил исполнителю"
                          + (f": {note}" if note else ""), key=f"delegated:{rep['rid']}")
                if not self.start_direct(t):
                    self.set_state(t, "new", "поручено исполнителю, ждёт запуска")
                return
            if st == "ready_for_review":
                if not rep.get("pr"):
                    return self.to_human(t, "планировщик отчитался ready_for_review без номера PR", "report")
                if not self.verify_pr(t, "planner", rep, plan=False):
                    return
                # планировщик сделал задачу сам: его сессия становится сессией исполнителя для ревью
                t["pr"], t["complexity"], t["self_executed"] = int(rep["pr"]), "simple", True
                e = self.cur(t, "planner")
                if e:
                    t.setdefault("sessions", {}).setdefault("worker", []).append(dict(e))
                t.setdefault("comments_seen_at", iso())
                t.setdefault("comments_floor", iso())
                t["ready_at"] = iso()
                self.set_state(t, "ready", "планировщик сделал задачу сам")
                self.emit(t, "self_done", f"{t['id']}: простая — планировщик сделал сам, PR #{t['pr']} в очереди "
                          "на ревью", key=f"self:{rep['rid']}")
                return
            if st == "done" and (not t.get("pr") or t.get("route") == "self"):
                # менять репозиторий не пришлось: ранний черновой PR планировщик закрыл сам
                t.pop("pr", None)
                t["complexity"] = t.get("complexity") or "simple"
                self.set_state(t, "done", rep.get("summary") or "выполнено")
                self.emit(t, "done", f"{t['id']}: выполнено — {rep.get('summary') or 'без изменений в репозитории'}",
                          key="done")
                return
            if st == "plan_ready" and rep.get("pr"):
                if not self.verify_pr(t, "planner", rep, plan=True):
                    return
                t["pr"] = int(rep["pr"])
                # trivial у задачи с планом не бывает: это признак задачи без плана (is_direct)
                cx = rep.get("complexity") or t.get("complexity") or "complex"
                t["complexity"] = cx if cx in ("simple", "complex") else "simple"
                self.save_notes(t, rep)
                t["plan_seen_at"] = iso()
                if self.start_plan_check(t):
                    return
                self.set_state(t, "plan_review")
                self.emit(t, "plan_ready", f"{t['id']}: план готов к обсуждению — PR #{t['pr']}",
                          key=f"plan:{rep['rid']}")
                return
            if rep.get("status") in ("error", "blocked"):
                return self.to_human(t, rep.get("summary") or "планировщик сообщил о проблеме", "asks")
        e = self.cur(t, "planner")
        st = self.sstate(e)
        if st in ("failed", "stopped"):
            if not self.check_limit(e, t):
                self.lost(t, "planner", "planning", f"сессия {st}", "сессия планировщика завершилась без плана")
        elif st == "done" and age_minutes((self.sess(e) or {}).get("updatedAt") or t.get("state_at")) > 2:
            self.nudge(t, "planner", "planning", "планировщик закончил ход без aiflow report")
        elif st == "blocked":
            self.blocked(t, "planner", e)
        elif e and st is None and age_minutes(e.get("resumed_at") or e.get("started_at")) > 10:
            self.lost(t, "planner", "planning", "сессия пропала", "сессия планировщика пропала из claude agents")

    def adv_new(self, t: dict) -> None:
        """Задача в очереди: зависимость, которую сняли, сама не выполнится — сказать об этом."""
        self.deps_broken(t)

    def dep_status(self, t: dict, d: str, by_id: dict[str, dict] | None = None) -> tuple[str, str]:
        """Готова ли зависимость: ("ok" | "wait" | "gone" | "failed", пояснение). Задача соседнего проекта готова,
        когда смержена и завершились workflow на коммите мержа (там публикуется пакет)."""
        other_store, other = dep_task(self.store, d, self.by_id if by_id is None else by_id)
        st = (other or {}).get("state")
        if st in (None, "cancelled"):
            return "gone", ""
        if st not in FINISHED:
            return "wait", ""
        if not split_dep(d)[0] or st == "done" or not other.get("pr") or d in (t.get("deps_published") or []):
            return "ok", ""
        if not other_store.config().get("wait_workflows", True):
            return "ok", ""
        last = (t.get("deps_checked") or {}).get(d)
        if last and age_minutes(last.get("at")) < 1:
            return last.get("status") or "wait", last.get("note") or ""
        status, note = merge_workflows(other_store.root, other)
        if status == "ok":
            t.setdefault("deps_published", []).append(d)
            (t.get("deps_checked") or {}).pop(d, None)
        else:
            t.setdefault("deps_checked", {})[d] = {"at": iso(), "status": status, "note": note}
        self.store.save_task(t)
        return status, note

    def deps_broken(self, t: dict) -> bool:
        """Зависимость, которая сама не выполнится (снята) или чья публикация упала, — к человеку."""
        found = [(d, *self.dep_status(t, d)) for d in t.get("depends_on") or []]
        gone = [d for d, st, _ in found if st == "gone"]
        failed = [f"{d}: {note}" for d, st, note in found if st == "failed"]
        if gone:
            self.to_human(t, f"зависимость снята или не найдена: {', '.join(gone)}", "deps")
        elif failed:
            self.to_human(t, "; ".join(failed) + " — пакет мог не опубликоваться", "deps")
        return bool(gone or failed)

    def start_plan_check(self, t: dict) -> bool:
        """План готов: сначала его смотрит ревьюер (plan_check в конфиге), пользователь — после."""
        if str(self.cfg.get("plan_check") or "off").lower() == "off":
            return False
        t["plan_check"] = {"phase": "queued", "runs": 0}
        self.set_state(t, "plan_check", "план готов: ревью плана")
        return True

    def plan_checked(self, t: dict, note: str) -> None:
        """Ревью плана закончено (или не удалось): план идёт к пользователю в любом случае."""
        pc = t.setdefault("plan_check", {})
        pc.update(phase="done", note=note)
        pc.pop("pid", None)
        self.set_state(t, "plan_review", f"ревью плана: {note}")
        self.emit(t, "plan_ready", f"{t['id']}: план готов к обсуждению — PR #{t['pr']}. Ревью плана: {note}",
                  key=f"plan:check:{pc.get('started_at') or iso()}")

    def adv_plan_check(self, t: dict) -> None:
        """Ревью плана до пользователя. queued → reviewing (ревьюер opencode) → answering (планировщик
        отвечает на замечания, один ход) → plan_review. Советующее: любой сбой ведёт к plan_review."""
        pc = t.setdefault("plan_check", {})
        state = self.gh.pr(t["pr"], "state").get("state")
        if state in ("CLOSED", "MERGED"):
            self.set_state(t, "cancelled", "PR закрыт на стадии плана")
            self.emit(t, "cancelled", f"{t['id']}: PR с планом закрыт, задача снята", key="cancelled")
            return
        e = self.cur(t, "planner")
        path = self.store.plan_review_path(t["id"])
        phase = pc.get("phase") or "queued"
        if phase == "queued":
            if self.sstate(e) == "working":
                return  # планировщик ещё дописывает план: ревьюер прочитал бы незаконченный текст
            path.unlink(missing_ok=True)
            cwd = (e or {}).get("cwd") or (self.sess(e) or {}).get("cwd")
            pid = self.spawn_plan_check(self.store, t, cwd if cwd and Path(cwd).is_dir() else str(self.root))
            if not pid:
                return self.plan_checked(t, "не удалось запустить ревьюера")
            pc.update(phase="reviewing", pid=pid, started_at=iso(), runs=int(pc.get("runs") or 0) + 1)
            return
        if phase == "reviewing":
            res = read_json(path, None)
            if not isinstance(res, dict):
                if pid_alive(int(pc.get("pid") or 0)) and age_minutes(pc.get("started_at")) < 60:
                    return
                return self.plan_checked(t, "ревьюер не дал ответа")
            findings = [f for f in res.get("findings") or [] if isinstance(f, dict)]
            if res.get("status") == "error":
                return self.plan_checked(t, "не удалось — " + short_reason(res.get("message") or "ошибка ревьюера", 160))
            if not findings:
                return self.plan_checked(t, "замечаний нет")
            pc["findings"] = len(findings)
            if self.sstate(e) == "working":
                return
            self.new_report(t, "planner")  # отчёт, пришедший во время ревью (дополнение), — не ответ на замечания
            if not self.wake(t, "planner", plan_check_prompt(t, res)):
                return self.plan_checked(t, f"замечаний: {len(findings)}, планировщика разбудить не удалось — "
                                         "они в комментарии к PR")
            pc.update(phase="answering", asked_at=iso())
            return
        if phase == "answering":
            n = pc.get("findings") or 0
            rep = self.new_report(t, "planner")
            if rep and rep.get("status") == "plan_ready":
                if rep.get("complexity") in ("simple", "complex"):
                    t["complexity"] = rep["complexity"]
                self.save_notes(t, rep)
                if int(pc.get("runs") or 0) < int(self.cfg.get("plan_check_rounds") or 1):
                    pc["phase"] = "queued"  # ревьюер смотрит исправленный план ещё раз
                    return
                return self.plan_checked(t, f"замечаний: {n}, планировщик ответил на них в PR")
            if rep and rep.get("status") in ("error", "blocked"):
                return self.to_human(t, rep.get("summary") or "планировщик сообщил о проблеме", "asks")
            wait = float(self.cfg.get("plan_check_wait_minutes") or 20)
            if self.sstate(e) != "working" and not self.fresh(e) and age_minutes(pc.get("asked_at")) > wait:
                return self.plan_checked(t, f"замечаний: {n}, планировщик не ответил — они в комментарии к PR")
            return
        self.plan_checked(t, pc.get("note") or "завершено")

    def adv_plan_review(self, t: dict) -> None:
        pr = self.gh.pr(t["pr"], "state,labels,comments")
        if pr.get("state") in ("CLOSED", "MERGED"):
            self.set_state(t, "cancelled", "PR закрыт на стадии плана")
            self.emit(t, "cancelled", f"{t['id']}: PR с планом закрыт, задача снята", key="cancelled")
            return
        labels = {lb.get("name") for lb in pr.get("labels") or []}
        if self.cfg["labels"]["approved"] in labels or t.get("plan_approved") or is_direct(t):
            # plan_approved и задача без плана в этой стадии — записи старой версии: то же «ждёт исполнителя»
            if not is_direct(t):
                held = t.get("approve_hold")
                # дольше 20 минут не ждём: зависший планировщик не должен держать одобренный план вечно
                if self.sstate(self.cur(t, "planner")) == "working" and not (held and age_minutes(held) > 20):
                    # План одобрили, пока планировщик его дорабатывает (ответ в его чате, комментарий в PR).
                    # Исполнитель, запущенный сейчас, взял бы план, который через минуту изменится: ждём конца хода.
                    if not held:
                        t["approve_hold"] = iso()
                        self.emit(t, "approve_hold", f"{t['id']}: план одобрен, но планировщик ещё дорабатывает его — "
                                  "жду конца его хода", key=f"hold:{iso()[:16]}")
                    return
                rep = self.new_report(t, "planner")
                if rep and rep.get("status") == "plan_ready":
                    if rep.get("complexity") in ("simple", "complex"):
                        t["complexity"] = rep["complexity"]
                    self.save_notes(t, rep)
                    if t.pop("approve_hold", None):
                        # одобрен был прежний текст: новый план пользователь ещё не видел
                        self.gh.remove_label(t["pr"], self.cfg["labels"]["approved"])
                        t["plan_approved"] = False
                        t["revising"] = False
                        self.emit(t, "plan_updated", f"{t['id']}: план изменён после одобрения — посмотрите и одобрите "
                                  f"снова, PR #{t['pr']}", key=f"plan:{rep['rid']}")
                        return
                t.pop("approve_hold", None)
                t["plan_approved"] = True
                self.settle(t, ("planner",))  # планировщик закончил ход вопросом «одобрите план» — он решён
            self.set_state(t, "approved", "план одобрен")
            return
        rep = self.new_report(t, "planner")
        if rep and rep.get("status") == "plan_ready":
            t["revising"] = False
            if rep.get("complexity") in ("simple", "complex"):
                t["complexity"] = rep["complexity"]
            self.save_notes(t, rep)
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

    def adv_approved(self, t: dict) -> None:
        """Ждёт слот исполнителя (запускает schedule). Пока ждёт, PR могли закрыть."""
        if self.deps_broken(t):
            return
        if not t.get("pr"):
            return self.set_state(t, "new", "нет PR: задача начинается заново")
        state = self.gh.pr(t["pr"], "state").get("state")
        if state == "MERGED":
            self.finish_merged(t)
        elif state == "CLOSED":
            self.set_state(t, "cancelled", "PR закрыт")
            self.emit(t, "cancelled", f"{t['id']}: PR #{t['pr']} закрыт, задача снята", key="cancelled")

    def adv_in_progress(self, t: dict) -> None:
        rep = self.new_report(t, "worker")
        if rep:
            if rep.get("status") == "started":
                self.early_pr(t, rep)  # исполнитель задачи без плана открыл черновой PR и работает дальше
                return
            if rep.get("status") == "done" and not t.get("pr"):
                self.set_state(t, "done", rep.get("summary") or "выполнено")
                self.emit(t, "done", f"{t['id']}: выполнено — {rep.get('summary') or 'без изменений в репозитории'}",
                          key="done")
                return
            if rep.get("status") == "ready_for_review":
                n = t.get("pr") or rep.get("pr")
                if not n:
                    return self.to_human(t, "worker отчитался ready_for_review без номера PR", "report")
                if not self.verify_pr(t, "worker", {**rep, "pr": n}, plan=False):
                    return
                t["pr"] = int(n)
                t["ready_at"] = iso()
                self.set_state(t, "ready", "реализация готова")
                return
            if rep.get("status") in ("error", "blocked", "limit_reached"):
                return self.to_human(t, rep.get("summary") or f"worker: {rep.get('status')}", "asks")
        self.watch_session(t, "worker", "сессия worker'а завершилась до готовности PR")

    def watch_session(self, t: dict, role: str, dead_reason: str) -> None:
        e = self.cur(t, role)
        st = self.sstate(e)
        if st in ("failed", "stopped"):
            if not self.check_limit(e, t):
                self.lost(t, role, t.get("state", "in_progress"), f"сессия {st}", dead_reason)
        elif st == "blocked":
            self.blocked(t, role, e)
        elif st == "done" and age_minutes(t.get("state_at")) > 2 and t.get("state") == "in_progress":
            self.nudge(t, role, "in_progress", f"{role} закончил ход без aiflow report")
        elif e and st is None and age_minutes(e.get("resumed_at") or e.get("started_at")) > 10:
            self.lost(t, role, t.get("state", "in_progress"), "сессия пропала", f"сессия {role} пропала из claude agents")

    def adv_ready(self, t: dict) -> None:
        pr = self.gh.pr(t["pr"], "state,headRefOid,statusCheckRollup")
        t["ci"] = ci_summary(pr.get("statusCheckRollup"), pr.get("headRefOid"))
        if pr.get("state") == "MERGED":
            self.finish_merged(t)
        elif pr.get("state") == "CLOSED":
            self.set_state(t, "cancelled", "PR закрыт")
            self.emit(t, "cancelled", f"{t['id']}: PR #{t['pr']} закрыт, задача снята", key="cancelled")

    def adv_in_review(self, t: dict) -> None:
        pr = self.gh.pr(t["pr"], "state,labels,headRefOid,statusCheckRollup")
        t["ci"] = ci_summary(pr.get("statusCheckRollup"), pr.get("headRefOid"))
        # Шаг доски «Мерж»: ревьюер одобрил ровно этот коммит, и его CI не красный. approved_sha для этого мало:
        # он остаётся и тогда, когда после одобрения упал CI и исполнитель снова правит код.
        t.pop("awaiting_merge", None)
        if pr.get("state") == "MERGED":
            return self.finish_merged(t)
        if pr.get("state") == "CLOSED":
            self.release_lock(t)
            self.set_state(t, "cancelled", "PR закрыт")
            self.emit(t, "cancelled", f"{t['id']}: PR #{t['pr']} закрыт, задача снята", key="cancelled")
            return
        labels = {lb.get("name") for lb in pr.get("labels") or []}
        if self.watch_auto_review(t):
            return
        rep = self.new_report(t, "worker")
        if rep:
            st = rep.get("status")
            if st != "error":
                t.pop("review_retried", None)  # раунд прошёл: следующий сбой снова повторяется сам
            if st == "approved":
                t["approved_sha"] = pr.get("headRefOid")
                t["approved_at"] = iso()
            elif st == "paused":
                # очередь на паузе: мерж отложен. PR возвращается в очередь ревью на своё место; после паузы
                # исполнителя разбудят снова, и одобренный дифф второй раз не ревьюится
                self.release_lock(t)
                t.pop("approved_sha", None)
                t.pop("approved_at", None)
                self.set_state(t, "ready", "ревью отложено: очередь на паузе")
                return
            elif st == "error" and not t.get("review_retried"):
                # сбой инструмента (ревьюер не ответил, его прервали выключением), а не кода: один раз
                # повторяем сами, и только второй сбой подряд идёт к человеку
                t["review_retried"] = True
                t.pop("auto_review", None)
                self.release_lock(t)
                self.set_state(t, "ready", "сбой ревью: повтор")
                self.emit(t, "retry", f"{t['id']}: сбой ревью ({short_reason(rep.get('summary') or 'ошибка', 80)}) — "
                          "повторяю сам", key=f"autoretry:{rep.get('rid')}")
                return
            elif st in ("limit_reached", "error", "no_ci", "already_closed", "blocked"):
                action = "ci" if st == "limit_reached" and "CI" in (rep.get("summary") or "") else REVIEW_ACTIONS[st]
                return self.to_human(t, rep.get("summary") or f"ревью: {st}", action)
        if self.cfg["labels"]["needs_human"] in labels:
            return self.to_human(t, "PR помечен needs-human", "label")
        if t.get("approved_sha") and t["approved_sha"] == pr.get("headRefOid"):
            failed = [c.get("name") or c.get("context") for c in pr.get("statusCheckRollup") or []
                      if (c.get("conclusion") or c.get("state") or "").upper() in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED")]
            if not failed:
                t["awaiting_merge"] = True
            if failed and t.get("ci_wake") != pr.get("headRefOid"):
                t["ci_wake"] = pr.get("headRefOid")
                if not self.wake(t, "worker", ci_fix_prompt(t, failed)):
                    self.to_human(t, "после одобрения упал CI, а сессию worker'а не удалось разбудить", "session")
            elif age_minutes(t.get("approved_at") or t.get("state_at")) > float(
                    self.cfg.get("merge_wait_minutes") or 60):
                # одобрен, но не мержится (автомерж выключен, защита ветки ждёт одобрения): не держим очередь
                # ревью. Когда PR смержат, задача закроется сама из «нужен человек».
                self.to_human(t, f"PR #{t['pr']} одобрен, но не смержен за {self.cfg.get('merge_wait_minutes') or 60} мин",
                              "merge")
            return
        e = self.cur(t, "worker")
        last_review = (self.store.review(t["id"]) or {}).get("at") or t.get("state_at")
        if self.sstate(e) in ("failed", "stopped"):
            if not self.check_limit(e, t):
                self.lost(t, "worker", "in_review", f"сессия {self.sstate(e)}", "сессия worker'а завершилась во время ревью")
        elif e and self.sstate(e) is None and age_minutes(e.get("resumed_at") or e.get("started_at")) > 10 \
                and age_minutes(last_review) > 10:
            self.lost(t, "worker", "in_review", "сессия пропала", "сессия worker'а пропала из claude agents во время ревью")
        elif self.sstate(e) == "done" and age_minutes(last_review) > 5 and age_minutes(t.get("state_at")) > 5:
            self.nudge(t, "worker", "in_review", "worker закончил ход, не доведя ревью до итогового статуса")
        elif self.sstate(e) == "blocked":
            self.blocked(t, "worker", e)
        elif self.sstate(e) == "working" and not self.fresh(e):
            # Сессия «занята», но это ещё не значит, что ревью идёт: она могла закончить ход и ждать фоновую
            # команду, которая зависла. Самый долгий честный простой — ожидание CI плюс три попытки ревьюера.
            limit = float(self.cfg.get("ci_timeout_minutes") or 45) + 60
            quiet = [age_minutes(last_review), age_minutes(t.get("state_at"))]
            written = transcript_age_minutes(self.session_uuid(e))
            if written is not None:
                quiet.append(written)
            if min(quiet) > limit:
                self.to_human(t, f"ревью не двигается {int(min(quiet))} мин, хотя сессия числится занятой", "stall")

    def adv_needs_human(self, t: dict) -> None:
        if self.continued_by_human(t):
            return
        if not t.get("pr"):
            return
        pr = self.gh.pr(t["pr"], "state,labels")
        if pr.get("state") == "MERGED":
            return self.finish_merged(t)
        labels = {lb.get("name") for lb in pr.get("labels") or []}
        if self.cfg["labels"]["retry"] in labels:
            t["retry_requested"] = True
            self.do_retry(t)

    def add_reviewer_usage(self, t: dict) -> None:
        u = reviewer_usage(self.store, t["id"])
        if u:
            t.setdefault("usage", {})["reviewer"] = u

    def settle(self, t: dict, roles: tuple = ("planner", "worker")) -> None:
        """Закрыть ожидание сессий, чей вопрос уже никого не ждёт: план одобрен, задача закончена.
        Иначе они остаются в списке «Needs input» у claude agents, пока их хранят. claude stop разговор
        не удаляет: сессию по-прежнему можно открыть и продолжить."""
        for role in roles:
            for e in (t.get("sessions") or {}).get(role) or []:
                if self.sstate(e) == "blocked" and self.claude.stop(e["id"]):
                    self.sessions.get(e["id"], {})["state"] = "stopped"
                    self.store.log(t["id"], {"settled": e["id"]})

    def adv_merged(self, t: dict) -> None:
        self.add_reviewer_usage(t)  # последний раунд ревью заканчивается мержем: забрать его расход до уборки
        lost = [a for a in t.get("amendments") or [] if not a.get("delivered_at")]
        if lost and not t.get("amend_lost_told"):
            t["amend_lost_told"] = True
            self.emit(t, "amend_lost", f"{t['id']}: смержено раньше, чем дошли дополнения "
                      f"{', '.join('№' + str(a['n']) for a in lost)} — поставьте их новой задачей")
        if t.get("cleaned"):
            return
        self.settle(t)
        # Сессии законченной задачи сразу не удаляем: в них ход работы и итог (у задачи без PR — только там),
        # их читают и в них продолжают разговор. claude rm удаляет и рабочую копию, поэтому срок всё же есть.
        days = float(self.cfg.get("session_keep_days") or 0)
        mine = [e["id"] for lst in (t.get("sessions") or {}).values() for e in lst if e["id"] in self.sessions]
        if not mine:
            t["cleaned"] = True  # сессий уже нет (удалены вручную): следить больше не за чем
        elif days > 0 and age_minutes(t.get("state_at")) > days * 24 * 60:
            for sid in mine:
                self.claude.rm(sid)
            t["cleaned"] = True

    adv_done = adv_merged  # задача без PR: итог в событии, в report.json и в её сессии

    def adv_cancelled(self, t: dict) -> None:
        self.release_lock(t)
        t["cleaned"] = True

    def continued_by_human(self, t: dict) -> bool:
        """Человек ответил сессии напрямую (дал доступ, «всё норм»), и она продолжила работу:
        вернуть задачу в прежнюю стадию без «Повторить»."""
        w = t.get("human_watch") or {}
        role = w.get("role")
        prev = next((h.get("from") for h in reversed(t.get("history") or []) if h.get("to") == "needs_human"), None)
        if not role or STAGE_ROLE.get(prev or "") != role:
            return False
        st = self.sstate(self.cur(t, role))
        rep = self.store.report(t["id"]) or {}
        new_report = rep.get("role") == role and rep.get("rid") not in (None, w.get("report"))
        woke = st == "working" and w.get("seen") != "working"
        if st != w.get("seen"):
            w["seen"] = st
        if not (woke or new_report):
            return False
        if prev == "in_review":
            locks = self.q.setdefault("review_locks", [])
            if t["id"] not in locks:
                if not self.review_free(t, locks):
                    return False  # ревью занято — вернём, когда освободится
                locks.append(t["id"])
        t["human_reason"] = None
        t.pop("human_action", None)
        t.pop("human_watch", None)
        t.pop("auto_resumed", None)
        if t.get("pr"):
            self.gh.remove_label(t["pr"], self.cfg["labels"]["needs_human"])
        self.set_state(t, prev, "сессия продолжила работу после ответа человека")
        self.emit(t, "continued", f"{t['id']}: {role} продолжил работу — задача снова в стадии «{STAGE_LABEL.get(prev, prev)}»",
                  key=f"continued:{iso()}")
        return True

    def do_retry(self, t: dict) -> None:
        t["retry_requested"] = False
        # Всё, что сессия сообщила до этого момента, человек уже видел. Иначе отчёт об ошибке, записанный,
        # пока задача ждала человека, сработал бы как новый и сразу вернул её в «нужен человек».
        stale = self.store.report(t["id"])
        if stale:
            t["report_seen"] = stale.get("rid")
        reason = t.get("human_reason") or "нужен человек"
        note = t.pop("retry_note", None)
        if note:
            reason += f"»; {note}; «продолжай"
        t["human_reason"] = None
        t.pop("human_action", None)
        t.pop("human_watch", None)
        t.pop("deps_checked", None)
        if t.get("pr"):
            self.gh.remove_label(t["pr"], self.cfg["labels"]["needs_human"])
            self.gh.remove_label(t["pr"], self.cfg["labels"]["retry"])
        self.release_lock(t)
        t.pop("auto_resumed", None)
        t.pop("nudged", None)
        t.pop("bounced", None)
        t.pop("limit_hits", None)
        t.pop("approved_sha", None)
        t.pop("approved_at", None)
        t.pop("auto_review", None)
        t.pop("review_retried", None)
        # Сначала — та же сессия с её контекстом и рабочим каталогом, в той же стадии.
        prev = next((h.get("from") for h in reversed(t.get("history") or []) if h.get("to") == "needs_human"), None)
        locks = self.q.setdefault("review_locks", [])
        if prev == "in_review" and self.sstate(self.cur(t, "worker")) == "working" and self.review_free(t, locks):
            # Сессия исполнителя не останавливалась и уже ведёт ревью: будить её нечем и незачем.
            # Задача сразу занимает своё место в ревью, а не числится в очереди без него.
            locks.append(t["id"])
            self.touch(t, "worker")
            self.set_state(t, "in_review", "повтор: сессия уже ведёт ревью")
            self.emit(t, "retry", f"{t['id']}: повтор — сессия уже ведёт ревью", key=f"retry:{iso()[:16]}")
            return
        if prev == "in_review" and self.cur(t, "worker"):
            # Ревью идёт строго по одному: задача снова встаёт в очередь ревью, и монитор будит
            # эту же сессию worker'а (с её контекстом), когда освободится lock.
            self.set_state(t, "ready", "повтор: ревью той же сессией")
            self.emit(t, "retry", f"{t['id']}: повтор — снова в очереди на ревью", key=f"retry:{iso()[:16]}")
            return
        role = {"planning": "planner", "discussing": "planner", "in_progress": "worker"}.get(prev or "")
        if role and self.try_resume(t, role, prev, f"повтор после «{reason}»", auto=False):
            self.set_state(t, prev, "повтор: та же сессия")
            return
        # Сессию не вернуть — новая. Куда встаёт задача:
        #   нет PR                           → new: планировщик (или исполнитель, если задача без плана) с начала;
        #   есть PR, план одобрен или не нужен → approved: новый исполнитель на той же ветке;
        #   есть PR, план ещё не одобрен      → plan_review: ждёт одобрения, как и до сбоя.
        t["retry_pending"] = True
        if not t.get("pr"):
            self.set_state(t, "new", "повтор")
        elif t.get("plan_approved") or is_direct(t) or t.get("route") == "self":
            if t.get("route") == "self":
                t["direct"] = True  # планировщик делал сам и уже открыл PR: новый исполнитель продолжит в нём
            self.set_state(t, "approved", "повтор: новый исполнитель")
        else:
            self.set_state(t, "plan_review", "повтор")
        self.emit(t, "retry", f"{t['id']}: повтор поставлен в очередь", key=f"retry:{iso()[:16]}")

    def finish_merged(self, t: dict) -> None:
        self.release_lock(t)
        self.set_state(t, "merged")
        self.emit(t, "merged", f"{t['id']}: смержено в {self.cfg['base_branch']} — PR #{t['pr']}", key="merged")
        self.adv_merged(t)

    def start_direct(self, t: dict) -> bool:
        """Исполнитель для задачи без плана: сразу делает её по тексту и указаниям планировщика."""
        retry = bool(t.pop("retry_pending", False))
        model, effort = worker_choice(self.cfg, t)
        prompt = direct_prompt(self.store, t, self.cfg) + launch_amendments(t)
        t.setdefault("comments_seen_at", iso())
        t.setdefault("comments_floor", iso())
        if t.get("pr"):
            prompt += (f"\nThe task already has PR #{t['pr']} from the branch ai/{t['id']}: work in this branch "
                       f"(git fetch origin; git switch ai/{t['id']}). Do not open a new PR. Complete the work and "
                       f"report ready_for_review with --pr {t['pr']}.")
        if retry:
            prompt += ("\nThis is a new attempt after a stop. The last report: "
                       + json.dumps(self.store.report(t["id"]) or {}, ensure_ascii=False))
        if not self.launch(t, "worker", prompt, model=model, effort=effort):
            if retry:
                t["retry_pending"] = True
            return False
        mark_delivered(t, "launch")
        self.set_state(t, "in_progress")
        return True

    def deps_ok(self, t: dict, by_id: dict[str, dict]) -> bool:
        return all(self.dep_status(t, d, by_id)[0] == "ok" for d in t.get("depends_on") or [])

    def schedule(self, tasks: list[dict], reviewable: list[dict] | None = None) -> None:
        """Запустить сессии задач, которым пора, и начать ревью готовых PR. reviewable — чьи PR можно
        ревьюить сейчас; None — всех."""
        by_id = {t["id"]: t for t in tasks}
        conc = int(self.cfg["concurrency"])
        active = sum(1 for t in tasks if t.get("state") in IMPLEMENTING or t.get("launching"))
        for t in tasks:
            if active >= conc and t.get("kind") not in TALK_KINDS:
                continue
            if t.get("launching"):
                continue
            st = t.get("state")
            if st == "new" and self.deps_ok(t, by_id) and t.get("kind") in TALK_KINDS:
                # разговор ждёт человека, а не модель: слот concurrency не занимает
                ask = t.get("kind") == "ask"
                prompt = (ask_prompt if ask else discuss_prompt)(self.store, t, self.cfg) + launch_amendments(t)
                model, effort = planner_choice(self.cfg, t, (
                    self.cfg.get("ask_model" if ask else "discuss_model") or ("sonnet" if ask else "opus"),
                    self.cfg.get("ask_effort" if ask else "discuss_effort") or None))
                ok = self.launch(t, "planner", prompt, model=model, effort=effort)
                if ok:
                    mark_delivered(t, "launch")
                    self.set_state(t, "discussing")
                self.store.save_task(t)
                continue
            if st == "new" and self.deps_ok(t, by_id) and is_direct(t):
                if self.start_direct(t):
                    active += 1
                self.store.save_task(t)
            elif st == "new" and self.deps_ok(t, by_id):
                model, effort = planner_choice(self.cfg, t)
                ok = self.launch(t, "planner", planner_prompt(self.store, t, self.cfg) + launch_amendments(t),
                                 model=model, effort=effort)
                if ok:
                    t.pop("retry_pending", None)
                    mark_delivered(t, "launch")
                    self.set_state(t, "planning")
                    active += 1
                self.store.save_task(t)
            elif st == "approved" and not self.deps_ok(t, by_id):
                pass  # зависимость добавили после плана (задача в соседнем проекте): исполнитель ждёт её
            elif st == "approved" and is_direct(t):
                if self.start_direct(t):
                    active += 1
                self.store.save_task(t)
            elif st == "approved":
                retry = bool(t.pop("retry_pending", False))
                model, effort = worker_choice(self.cfg, t)
                t.setdefault("comments_floor", iso())
                ok = self.launch(t, "worker", worker_prompt(self.store, t, self.cfg, retry=retry) + launch_amendments(t),
                                 model=model, effort=effort)
                if ok:
                    mark_delivered(t, "launch")
                    self.set_state(t, "in_progress")
                    active += 1
                elif retry:
                    t["retry_pending"] = True
                self.store.save_task(t)
        self.schedule_reviews(tasks if reviewable is None else reviewable, by_id)

    def changed_files(self, t: dict) -> set[str] | None:
        """Файлы PR задачи (с кэшем по коммиту); None — узнать не удалось."""
        try:
            sha = self.gh.pr(t["pr"], "headRefOid").get("headRefOid")
        except AiflowError:
            return None
        cached = t.get("files_cache") or {}
        if sha and cached.get("sha") == sha:
            return set(cached.get("files") or [])
        files = self.gh.pr_files(t["pr"])
        if files is not None and sha:
            t["files_cache"] = {"sha": sha, "files": sorted(files)}
        return files

    def review_free(self, t: dict, locks: list[str], waiting: tuple = ()) -> bool:
        """Можно ли взять PR на ревью сейчас. Свободно — да. Иначе — только если есть место
        (review_concurrency) и у PR нет общих файлов ни с PR на ревью, ни с теми, кто ждёт в очереди
        раньше него: такие PR не конфликтуют при мерже, а порядок очереди для остальных сохраняется."""
        others = [self.by_id[i] for i in locks if i in self.by_id and i != t["id"]] + list(waiting)
        if not others:
            return True
        if len(locks) >= max(1, int(self.cfg.get("review_concurrency") or 1)):
            return False
        mine = self.changed_files(t)
        if mine is None:
            return False  # не знаем файлов — считаем, что пересекаются
        for o in others:
            theirs = self.changed_files(o) if o.get("pr") else None
            if theirs is None or mine & theirs:
                return False
        return True

    def review_blocker(self, t: dict, locks: list[str], waiting: tuple = ()) -> str | None:
        """Задача, из-за общих файлов с которой PR ждёт ревью, хотя место есть. Места нет — None."""
        if len(locks) >= max(1, int(self.cfg.get("review_concurrency") or 1)):
            return None
        mine = self.changed_files(t)
        for o in [self.by_id[i] for i in locks if i in self.by_id and i != t["id"]] + list(waiting):
            theirs = self.changed_files(o) if o.get("pr") else None
            if mine is None or theirs is None or mine & theirs:
                return o["id"]
        return None

    def schedule_reviews(self, tasks: list[dict], by_id: dict[str, dict]) -> None:
        """Очередь ревью: по порядку готовности; сразу несколько — только PR без общих файлов."""
        locks = self.q["review_locks"] = [i for i in self.q.get("review_locks") or []
                                          if (by_id.get(i) or {}).get("state") == "in_review"]
        limit = max(1, int(self.cfg.get("review_concurrency") or 1))
        ready = sorted((t for t in tasks if t.get("state") == "ready"), key=lambda t: t.get("ready_at") or "")
        skipped: list[dict] = []
        for t in ready:
            if len(locks) >= limit:
                break
            if self.sstate(self.cur(t, "worker")) == "working" or not self.review_free(t, locks, tuple(skipped)):
                # сессия ещё не закончила ход либо PR пересекается с идущим ревью: панель скажет, кого он ждёт
                wait = self.review_blocker(t, locks, tuple(skipped))
                if t.get("review_wait") != wait:
                    t["review_wait"] = wait
                    self.store.save_task(t)
                skipped.append(t)
                continue
            woke = self.start_auto_review(t) or self.wake_for_review(t, review_prompt(t, self.cfg))
            if woke:
                t.pop("review_wait", None)
                locks.append(t["id"])
                if not t.get("auto_review"):
                    self.touch(t, "worker")
                self.set_state(t, "in_review")
            else:
                skipped.append(t)
            self.store.save_task(t)

    def wake_for_review(self, t: dict, prompt: str) -> bool:
        """Разбудить исполнителя на ревью; не вышло — запустить нового с контекстом задачи."""
        if self.wake(t, "worker", prompt):
            return True
        model, effort = worker_choice(self.cfg, t)
        return self.launch(t, "worker", prompt + "\n\nTask context: " + worker_prompt(self.store, t, self.cfg),
                           model=model, effort=effort)

    def start_auto_review(self, t: dict) -> bool:
        """Запустить aiflow review самому, не будя модель. Только когда ветка PR не отстаёт от базовой:
        тогда rebase и прогон тестов перед ревью не нужны (CI их и так гоняет), и ход исполнителя сводится
        к одной команде. Отстаёт или не удалось узнать — будим исполнителя, как раньше."""
        if not self.cfg.get("auto_review", True):
            return False
        e = self.cur(t, "worker")
        cwd = (e or {}).get("cwd") or (self.sess(e) or {}).get("cwd")
        if not cwd or not Path(cwd).is_dir():
            return False  # ревью читает код из рабочей копии задачи
        try:
            head = self.gh.pr(t["pr"], "headRefOid").get("headRefOid")
        except AiflowError:
            return False
        if not head or self.gh.behind(self.cfg["base_branch"], head) != 0:
            return False
        pid = self.spawn_review(self.store, t, cwd)
        if not pid:
            return False
        t["auto_review"] = {"pid": pid, "at": iso(), "head": head}
        self.store.log(t["id"], {"auto_review": t["auto_review"]})
        return True

    def watch_auto_review(self, t: dict) -> bool:
        """Ревью, запущенное самим aiflow. True — оно ещё идёт или его итог только что разобран."""
        info = t.get("auto_review")
        if not info:
            return False
        rv = self.store.review(t["id"]) or {}
        finished = after(rv.get("at"), info.get("at")) or (rv.get("at") == info.get("at") and rv.get("status"))
        if not finished:
            if owner_alive(info) or age_minutes(info.get("at")) < 1:
                return True  # идёт: сессия исполнителя при этом простаивает, и это нормально
            t.pop("auto_review", None)  # процесс исчез без результата — пусть ревью ведёт исполнитель
            self.store.log(t["id"], {"auto_review_lost": info})
            if not self.wake_for_review(t, review_prompt(t, self.cfg)):
                self.to_human(t, "ревью не запустилось, а исполнителя не удалось разбудить", "session")
            else:
                self.touch(t, "worker")
            return True
        t.pop("auto_review", None)
        status = rv.get("status")
        if status in ("changes_requested", "ci_failed", "merge_conflict", "human_input"):
            # нужна работа модели: будим исполнителя с готовым результатом, дальше цикл ведёт он
            if self.wake_for_review(t, review_result_prompt(self.store, t, self.cfg, status)):
                self.touch(t, "worker")
            else:
                self.to_human(t, f"ревью: {status}, а исполнителя не удалось разбудить", "session")
            return True
        return False  # итоговые статусы пришли отчётом — их разберёт обычная ветка

    def collect_usage(self, tasks: list[dict]) -> None:
        for t in tasks:
            if t.get("cleaned"):
                continue
            entries = sorted(((e.get("started_at") or "", role, e) for role, lst in (t.get("sessions") or {}).items()
                              for e in lst if e.get("session_id")), key=lambda x: x[0])
            seen: set[str] = set()
            counted: set[str] = set()
            for _, role, e in entries:  # по времени: общая история достаётся сессии, где она появилась
                if e["session_id"] in counted:
                    continue  # одна сессия в двух ролях (планировщик сделал задачу сам): считаем один раз
                counted.add(e["session_id"])
                u = transcript_usage(e["session_id"], seen)
                if u:
                    t.setdefault("usage", {})[e["session_id"]] = {"role": role, **u}
            self.add_reviewer_usage(t)
            self.store.save_task(t)

    def check_batch_done(self, tasks: list[dict]) -> None:
        live = [t for t in tasks if t.get("state") not in TERMINAL and t.get("state") not in ("needs_human", "answered")
                and t.get("kind") not in TALK_KINDS]
        merged_unannounced = [t for t in tasks if t.get("state") in FINISHED and not t.get("batch_announced")]
        if not live and merged_unannounced:
            for t in merged_unannounced:
                t["batch_announced"] = True
                self.store.save_task(t)
            human = [t["id"] for t in tasks if t.get("state") == "needs_human"]
            n_merged = sum(1 for t in merged_unannounced if t.get("state") == "merged")
            parts = [f"смержено {n_merged}"] if n_merged else []
            if len(merged_unannounced) > n_merged:
                parts.append(f"выполнено без PR {len(merged_unannounced) - n_merged}")
            text = "пачка завершена: " + ", ".join(parts)
            if human:
                text += f", ждут вас: {', '.join(human)}"
            self.emit(None, "batch_done", text)
