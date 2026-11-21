"""Состояние очереди для панели и `aiflow status`; статистика (`aiflow stats`).
"""
from __future__ import annotations

import datetime as dt
import math

from aiflow_core import (
    FINISHED, HUMAN_CODES, HUMAN_CODE_WORDS, MODEL_FIELDS, PATH_ASK_RE, PAUSE_LIFTS, STAGE_LABEL, STATION, TALK_KINDS,
    TERMINAL, kept_pauses,
    VERSION, age_minutes, ci_text, iso, now, owner_alive, parse_iso, read_json, short_reason, visited_stations,
    wanted_paths,
)
from aiflow_store import FileLock, Store
from aiflow_usage import (
    add_usage, coordinator_state, done_note, price_table, priced, reviewer_runs, task_tokens, task_usage,
)
from aiflow_prompts import waiting_deps


def review_activity(store: "Store", t: dict, session_state: str | None, max_rounds: int) -> str | None:
    """Чем сейчас занято ревью задачи: ожидание CI, ревьюер opencode, мерж или работа исполнителя.
    Фазу пишет сам aiflow review (.aiflow/<id>/phase.json), пока работает; сводку CI — проход очереди."""
    st = t.get("state")
    ci = ci_text(t.get("ci"))
    if st == "ready":
        wait = t.get("review_wait")
        return ((f"ждёт ревью {wait}: общие файлы" if wait else "в очереди на ревью")
                + (f"; CI: {ci}" if ci else ""))
    if st != "in_review":
        return None
    ph = read_json(store.runs / t["id"] / "phase.json", {}) or {}
    if ph and owner_alive(ph):
        mins = int(age_minutes(ph.get("since") or ph.get("at")))
        took = f" ({mins} мин)" if mins >= 1 else ""
        kind = ph.get("phase")
        if kind == "ci_appear":
            return "ждёт, пока GitHub запустит CI" + took
        if kind == "ci":
            return "ждёт CI" + (f": {ci}" if ci else "") + took
        if kind == "reviewer":
            more = [str(ph["doing"])] if ph.get("doing") else []
            if ph.get("steps"):
                more.append(f"шаг {ph['steps']}")
            if ph.get("tokens"):
                more.append(f"{fmt_tokens(int(ph['tokens']))} токенов")
            return (f"ревьюер opencode, раунд {ph.get('round') or '?'}/{max_rounds}"
                    + (": " + ", ".join(more) if more else "") + took)
        if kind == "merge":
            return "мерж" + took
    if t.get("approved_sha"):
        return "одобрен, ждёт мержа" + (f"; CI: {ci}" if ci else "")
    if t.get("auto_review"):
        return "aiflow запускает ревью"
    if session_state == "working":
        rv = store.review(t["id"]) or {}
        if rv.get("status") == "ci_failed":
            return "CI упал" + (": " + ", ".join(map(str, rv["failed"])) if rv.get("failed") else "") + " — исполнитель чинит"
        if rv.get("status") == "changes_requested":
            return f"исполнитель правит замечания ревьюера (раунд {rv.get('round') or '?'}/{max_rounds})"
        return "исполнитель: слияние с базой, тесты или правки"
    return None


# ---------------------------------------------------------------- status / state for UI

def board_position(t: dict, has_pr: bool) -> tuple[int | None, list[int]]:
    """Где задача на доске и какие шаги она пропустила. → (шаг, пропущенные шаги).
    Законченная задача (смержено, выполнено, ответ дан) стоит на последнем шаге «Готово»; шаги, через
    которые она не проходила, панель рисует бледными. Вопрос или обсуждение без PR идёт «план → готово»:
    шаги между ними бледные заранее; появился PR — задача идёт обычным путём."""
    st = t.get("state", "new")
    final = STATION["done"]
    history = t.get("history") or []
    visited = visited_stations(t)
    if t.get("route") == "self" or t.get("self_executed"):
        visited.add(STATION["in_progress"])  # планировщик делал сам: состояние было planning, шаг — реализация
    if st in ("merged", "done", "answered"):
        stage = final
    elif st == "planning" and t.get("route") == "self":
        stage = STATION["in_progress"]
    elif st == "in_review" and t.get("awaiting_merge"):
        stage = STATION["merged"]  # ревьюер одобрил этот коммит, CI не красный: PR ждёт мержа — шаг «Мерж»
    elif st in STATION:
        stage = STATION[st]
    else:  # нужен человек, снята: шаг, на котором задача остановилась
        stage = next((STATION[h["from"]] for h in reversed(history) if h.get("from") in STATION), None)
    if stage is None:
        return None, []
    if stage != final:
        visited.add(stage)
    skipped = [i for i in range(1, stage) if i not in visited]
    if t.get("kind") in TALK_KINDS and not has_pr and stage != final:
        skipped += [i for i in range(max(stage, STATION["planning"]) + 1, final) if i not in skipped]
    return stage, skipped


def plan_check_activity(t: dict, store: "Store | None" = None) -> str:
    """Чем занято ревью плана — строка под названием задачи в панели."""
    pc = t.get("plan_check") or {}
    if pc.get("phase") == "reviewing" and store:
        # ревью плана идёт десятки минут: без шагов и минут в панели оно выглядит зависшим
        ph = read_json(store.runs / t["id"] / "phase.json", {}) or {}
        more = [str(ph["doing"])] if ph.get("doing") else []
        if ph.get("steps"):
            more.append(f"шаг {ph['steps']}")
        if ph.get("tokens"):
            more.append(f"{fmt_tokens(int(ph['tokens']))} токенов")
        mins = int(age_minutes(ph.get("since") or pc.get("started_at")))
        if ph.get("phase") == "plan_reviewer" and more:
            return ("ревьюер проверяет план: " + ", ".join(more) + (f" ({mins} мин)" if mins >= 1 else "")
                    + "; одобрение — после ревью")
    return {"reviewing": "ревьюер проверяет план, одобрение — после ревью",
            "answering": f"планировщик отвечает на замечания ревьюера ({pc.get('findings') or 0})",
            }.get(pc.get("phase") or "", "план ждёт ревьюера, одобрение — после ревью")


def base_fix_note(q: dict, cfg: dict | None, by_id: dict[str, dict] | None) -> str:
    """Что aiflow делает с красной базовой веткой — или что сделать человеку, если сам он не чинит."""
    by_hand = "почините ветку или перезапустите упавшую сборку"
    if str((cfg or {}).get("base_fix") or "off").lower() == "off":
        return by_hand
    fix = q.get("base_fix") or {}
    if fix.get("task"):
        state = ((by_id or {}).get(fix["task"]) or {}).get("state")
        if state in TERMINAL:
            return f"задача {fix['task']} закончилась, а ветка всё ещё красная — {by_hand}"
        if state == "needs_human":
            return f"чинит задача {fix['task']}, и ей нужны вы"
        return f"чинит задача {fix['task']}"
    if fix.get("reruns"):
        return "aiflow перезапустил упавшие задания сборки; упадут снова — поставит задачу на починку"
    return "aiflow чинит сам: перезапускает упавшие задания сборки, а если они падают снова — ставит задачу на починку"


def paused_state(q: dict, cfg: dict | None = None, by_id: dict[str, dict] | None = None) -> dict | None:
    """Пауза очереди для панели. kept — причины, которые кнопка «Продолжить очередь» не снимает: у каждой, что
    её снимет, и адрес (красная сборка), если он известен. Когда других причин нет, кнопка не работает совсем."""
    paused = q.get("paused")
    if not paused:
        return None
    pauses = q.get("pauses") or {}
    kept = []
    for r in kept_pauses(pauses):
        note = str((pauses.get(r) or {}).get("note") or "")
        lifts = PAUSE_LIFTS.get(r, "пауза снимется сама")
        if r == "integration_red":
            lifts += ": " + base_fix_note(q, cfg, by_id)
        kept.append({"reason": r, "lifts": lifts, "url": note if note.startswith("http") else ""})
    return {**paused, "kept": kept}


def build_state(store: Store, sessions: dict[str, dict] | None = None, repo_url: str = "") -> dict:
    cfg = store.config()
    q = store.queue()
    tasks = store.tasks()
    prices = price_table()
    project_usage: dict[str, dict] = {}
    out = []
    by_id = {t["id"]: t for t in tasks}
    for t in tasks:
        st = t.get("state", "new")
        waits_for = waiting_deps(store, t, by_id) if st in ("new", "approved") else []
        usage, cost, _ = priced(task_usage(t), prices)
        add_usage(project_usage, task_usage(t))
        cur = {}
        for role in ("worker", "planner"):
            lst = (t.get("sessions") or {}).get(role) or []
            if lst:
                e = lst[-1]
                live = (sessions or {}).get(e["id"]) or {}
                cur = {"role": role, "id": e["id"], "state": live.get("state"), "waiting": live.get("waitingFor")}
                break
        review = store.review(t["id"]) or {}
        pr = t.get("pr")
        needs = None
        if st == "plan_review" and t.get("approve_hold"):
            needs = None  # одобрено: ждём, пока планировщик закончит доработку
        elif st == "plan_review" and not t.get("plan_approved"):
            needs = "обсудить и одобрить план"
            if t.get("split_into"):  # задачу разбили: одобряется план только её части
                needs += f" первой части (всего частей: {len(t['split_into']) + 1}; остальные: {', '.join(t['split_into'])})"
            elif t.get("split_of"):
                needs += f" этой части задачи {t['split_of']}"
        elif st == "needs_human":
            needs = short_reason(t.get("human_reason")) or "нужен человек"
        elif st == "discussing" and cur.get("state") != "working":
            needs = ("вопрос ждёт вас — откройте сессию" if t.get("kind") == "ask"
                     else "обсуждение ждёт вас — откройте сессию")
        elif cur.get("state") == "blocked" and st not in TERMINAL:
            # у завершённой задачи сессия хранится, но её вопрос уже никого не ждёт
            said = (t.get("waits") or {}).get("text") if (t.get("waits") or {}).get("sid") == cur.get("id") else ""
            needs = f"сессия ждёт ответа: {said}" if said else f"сессия ждёт ответа ({cur.get('waiting') or 'вопрос'})"
            if (st == "plan_check" and not said and not cur.get("waiting")
                    and (t.get("plan_check") or {}).get("phase") != "answering"):
                needs = None  # планировщик закончил ход и ждёт ревьюера, а не пользователя
        doing_self = st == "planning" and t.get("route") == "self"
        stage, skipped = board_position(t, bool(pr))
        out.append({
            "id": t["id"], "title": t.get("title", ""), "state": st,
            "label": "планировщик делает сам" if doing_self else
                     "вопрос" if st == "discussing" and t.get("kind") == "ask" else STAGE_LABEL.get(st, st),
            "kind": t.get("kind") or "task", "answer": (t.get("answer") or {}).get("summary") if st == "answered" else done_note(t) if st == "done" else None,
            "stage": stage, "skipped": skipped,
            "complexity": t.get("complexity"), "pr": pr,
            "pr_url": f"{repo_url}/pull/{pr}" if pr and repo_url else None,
            "rounds": review.get("round"), "max_rounds": cfg["max_rounds"],
            "allowed_paths": t.get("allowed_protected_paths") or [],
            "tokens": task_tokens(t), "usage": usage, "cost": round(cost, 4), "session": cur, "needs": needs,
            "action": t.get("human_action") if st == "needs_human" else None,
            "activity": ("планировщик делает сам, PR появится по готовности"
                         + (f". Что делает: {t['route_note']}" if t.get("route_note") else "")) if doing_self
                        else ("ждёт задачу: " + ", ".join(waits_for)) if waits_for
                        else plan_check_activity(t, store) if st == "plan_check"
                        else review_activity(store, t, cur.get("state"), int(cfg["max_rounds"])),
            "waits_for": waits_for,
            "ci": t.get("ci") if st in ("ready", "in_review") else None,
            "models": {f: t[f] for f in MODEL_FIELDS if t.get(f)},
            "reviewer_session": next((r.get("session") for r in reversed(reviewer_runs(store, t["id"])) if r.get("session")), None),
            "depends_on": t.get("depends_on") or [], "state_at": t.get("state_at"),
            "split_of": t.get("split_of"), "split_into": t.get("split_into") or [],
            "created_at": t.get("created_at"), "updated_at": task_updated_at(t),
            "launching": bool(t.get("launching")), "review_lock": t["id"] in (q.get("review_locks") or []),
            "amendments": len(t.get("amendments") or []),
            "amend_pending": sum(1 for a in t.get("amendments") or [] if not a.get("delivered_at")),
            # защищённые пути, которые задаче нужны и не разрешены: кнопка «Разрешить пути…» держится до разрешения
            "wants_paths": wanted_paths(t, review, cfg),
            "asks_paths": bool(wanted_paths(t, review, cfg) or (t.get("path_asked") and st not in TERMINAL)
                               or (st == "needs_human" and PATH_ASK_RE.search(str(t.get("human_reason") or "")))),
        })
    coordinator = coordinator_state(store, prices)
    add_usage(project_usage, coordinator.pop("raw"))
    total_usage, total_cost, unpriced = priced(project_usage, prices)
    return {
        "version": VERSION, "repo": store.root.name, "repo_url": repo_url, "base": cfg["base_branch"],
        "paused": paused_state(q, cfg, by_id), "review_lock": q.get("review_lock"), "concurrency": cfg["concurrency"],
        "monitor_pid": FileLock(store.runs / "monitor.lock").owner(),
        "usage": total_usage, "cost": round(total_cost, 4), "unpriced": unpriced,
        "coordinator": coordinator,  # чаты «Дать задачу»: входят в итог проекта, но не относятся ни к одной задаче
        "tasks": out, "events": list(reversed(store.events(40))), "now": iso(),
        "stats": build_stats(store, STATS_DAYS, tasks),
    }


def task_updated_at(t: dict) -> str | None:
    """Когда с задачей последний раз что-то произошло: смена стадии, пробуждение сессии, дополнение."""
    marks = [t.get("created_at"), t.get("state_at")]
    marks += [h.get("at") for h in t.get("history") or []]
    marks += [a.get("at") for a in t.get("amendments") or []]
    times = [(d, m) for d, m in ((parse_iso(m), m) for m in marks) if d]
    return max(times)[1] if times else None


def fmt_when(s: str | None) -> str:
    """Дата для таблицы статуса, в местном времени: сегодня — время, иначе день и месяц (и год, если не этот)."""
    d = parse_iso(s)
    if not d:
        return "—"
    d, today = d.astimezone(), dt.datetime.now().astimezone()
    if d.date() == today.date():
        return d.strftime("%H:%M")
    return d.strftime("%d.%m" if d.year == today.year else "%d.%m.%Y")


def fmt_tokens(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def print_status(state: dict) -> None:
    p = state["paused"]
    head = f"aiflow · {state['repo']} · база {state['base']}"
    head += f" · ПАУЗА ({', '.join(p.get('reasons') or [p.get('reason')])})" if p else ""
    head += "" if state["monitor_pid"] else " · монитор не запущен"
    print(head)
    if not state["tasks"]:
        print("Задач нет. Добавьте их через /aiflow:intake.")
        return
    rows = [("задача", "состояние", "PR", "раунды", "токены", "цена", "создана", "обновлена", "сессия")]
    for t in state["tasks"]:
        s = t["session"]
        rows.append((t["id"], t["label"] + (" 🔒" if t["review_lock"] else "")
                     + (f" · {t['activity']}" if t.get("activity") and t["state"] == "in_review" else ""),
                     f"#{t['pr']}" if t["pr"] else "—",
                     f"{t['rounds']}/{t['max_rounds']}" if t["rounds"] else "—",
                     fmt_tokens(t["tokens"]), f"${t['cost']:.2f}" if t.get("cost") else "—",
                     fmt_when(t.get("created_at")), fmt_when(t.get("updated_at")),
                     f"{s.get('role')} {s.get('id')} {s.get('state') or ''}".strip() if s else "—"))
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))
    co = state.get("coordinator") or {}
    if co.get("tokens"):
        print(f"Координатор: {fmt_tokens(co['tokens'])} токенов" + (f", ${co['cost']:.2f}" if co.get("cost") else ""))
    if state.get("cost"):
        print(f"Всего по проекту: ${state['cost']:.2f} по ценам API"
              + (f" (без цены: {', '.join(state['unpriced'])})" if state.get("unpriced") else ""))
    waiting = [t for t in state["tasks"] if t["needs"]]
    if waiting:
        print("\nЖдут вас:")
        for t in waiting:
            print(f"  {t['id']}: {t['needs']}" + (f" — {t['pr_url']}" if t["pr_url"] else ""))
            if t.get("action"):
                print(f"    что сделать: {t['action']}")


# ---------------------------------------------------------------- статистика

STATS_DAYS = 30
# стадии, время в которых показывает статистика, в порядке конвейера
STATS_STAGES = ("planning", "plan_check", "plan_review", "approved", "in_progress", "ready", "in_review", "needs_human")


def human_code(h: dict) -> str:
    """Код причины перехода в «нужен человек» (ключ HUMAN_CODES)."""
    if h.get("code") in HUMAN_CODES:
        return h["code"]
    note = str(h.get("note") or "").lower()
    return next((code for code, words in HUMAN_CODE_WORDS if any(w in note for w in words)), "other")


def median(values: list[float]) -> float | None:
    v = sorted(values)
    if not v:
        return None
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def percentile(values: list[float], q: float) -> float | None:
    """Значение, не больше которого доля q значений (ближайший ранг)."""
    v = sorted(values)
    return v[max(0, min(len(v) - 1, math.ceil(q * len(v)) - 1))] if v else None


def stage_hours(t: dict) -> dict[str, float]:
    """Сколько часов задача провела в каждой стадии — по истории переходов. Незаконченная стадия идущей
    задачи считается до текущего момента. История хранит последние HISTORY_KEEP переходов: у задачи,
    которая ходила по кругу дольше, начало не учитывается."""
    out: dict[str, float] = {}
    hist = sorted((h for h in t.get("history") or [] if parse_iso(h.get("at"))), key=lambda h: parse_iso(h["at"]))
    for i, h in enumerate(hist):
        start = parse_iso(h["at"])
        if i + 1 < len(hist):
            end = parse_iso(hist[i + 1]["at"])
        elif h.get("to") in TERMINAL:
            continue
        else:
            end = now()
        if h.get("to"):
            out[h["to"]] = out.get(h["to"], 0.0) + max(0.0, (end - start).total_seconds() / 3600)
    return out


def build_stats(store: Store, days: int = STATS_DAYS, tasks: list[dict] | None = None) -> dict:
    """Сводка по задачам проекта, созданным за последние days дней (0 — за всё время): сколько раз и почему
    задачи уходили к человеку, сколько раундов ревью, время по стадиям, цена. Вопросы и обсуждения
    (kind ask, discuss) не считаются: у них нет ни ревью, ни мержа."""
    since = now() - dt.timedelta(days=days) if days else None
    picked = []
    for t in tasks if tasks is not None else store.tasks():
        created = parse_iso(t.get("created_at"))
        if t.get("kind") in TALK_KINDS or (since and created and created < since):
            continue
        picked.append(t)
    prices = price_table()
    codes: dict[str, int] = {}
    asked, rounds, lead, costs = 0, {}, [], []
    stages: dict[str, list[float]] = {}
    for t in picked:
        to_human = [h for h in t.get("history") or [] if h.get("to") == "needs_human"]
        asked += bool(to_human)
        for h in to_human:
            code = human_code(h)
            codes[code] = codes.get(code, 0) + 1
        for st, hours in stage_hours(t).items():
            if st in STATS_STAGES:
                stages.setdefault(st, []).append(hours)
        if t.get("state") not in FINISHED:
            continue
        rnd = (store.review(t["id"]) or {}).get("round")
        if t.get("state") == "merged" and isinstance(rnd, int) and rnd > 0:
            rounds[rnd] = rounds.get(rnd, 0) + 1
        done = [h.get("at") for h in t.get("history") or [] if h.get("to") in FINISHED] or [t.get("state_at")]
        a, b = parse_iso(t.get("created_at")), parse_iso(done[-1])
        if a and b:
            lead.append(max(0.0, (b - a).total_seconds() / 3600))
        cost = priced(task_usage(t), prices)[1]
        if cost:
            costs.append(cost)
    reviewed = sum(rounds.values())

    def r1(x: float | None) -> float | None:
        return None if x is None else round(x, 1)

    return {
        "days": days, "tasks": len(picked),
        "finished": sum(1 for t in picked if t.get("state") in FINISHED),
        "cancelled": sum(1 for t in picked if t.get("state") == "cancelled"),
        "active": sum(1 for t in picked if t.get("state") not in TERMINAL),
        "human": {"tasks": asked, "times": sum(codes.values()),
                  "codes": [{"code": c, "label": HUMAN_CODES[c], "n": n}
                            for c, n in sorted(codes.items(), key=lambda x: (-x[1], x[0]))]},
        "rounds": {"by_round": {str(k): rounds[k] for k in sorted(rounds)}, "reviewed": reviewed,
                   "average": r1(sum(k * n for k, n in rounds.items()) / reviewed) if reviewed else None},
        "hours": {"median": r1(median(lead)), "p90": r1(percentile(lead, 0.9)), "tasks": len(lead)},
        "stages": [{"state": st, "label": STAGE_LABEL.get(st, st), "tasks": len(stages[st]),
                    "median": round(median(stages[st]) or 0.0, 2), "total": r1(sum(stages[st]))}
                   for st in STATS_STAGES if stages.get(st)],
        "cost": {"total": round(sum(costs), 2), "median": None if not costs else round(median(costs) or 0.0, 2),
                 "tasks": len(costs)},
    }


def fmt_hours(h: float | None) -> str:
    if h is None:
        return "—"
    return f"{h * 60:.0f} мин" if h < 1 else f"{h:.1f} ч" if h < 48 else f"{h / 24:.1f} дн"


def print_stats(name: str, s: dict) -> None:
    span = f"за {s['days']} дн" if s["days"] else "за всё время"
    print(f"aiflow · {name} · статистика {span}")
    if not s["tasks"]:
        print("Задач за это время нет.")
        return
    print(f"Задач: {s['tasks']} — выполнено {s['finished']}, снято {s['cancelled']}, идут {s['active']}")
    h = s["human"]
    print(f"Уходили к человеку: {h['tasks']} из {s['tasks']} ({h['tasks'] * 100 // s['tasks']}%), всего {h['times']} раз")
    for c in h["codes"]:
        print(f"  {c['n']:3d}  {c['label']}")
    r = s["rounds"]
    if r["reviewed"]:
        print("Раундов ревью до одобрения: " + ", ".join(f"{k} — {n}" for k, n in r["by_round"].items())
              + f" (в среднем {r['average']})")
    if s["hours"]["tasks"]:
        print(f"От постановки до готовности: медиана {fmt_hours(s['hours']['median'])}, "
              f"90% задач — до {fmt_hours(s['hours']['p90'])}")
    if s["stages"]:
        print("Время в стадии (медиана по задачам, которые в ней были):")
        for st in s["stages"]:
            print(f"  {fmt_hours(st['median']):>8}  {st['label']} ({st['tasks']})")
    if s["cost"]["tasks"]:
        print(f"Цена выполненной задачи по тарифам API: медиана ${s['cost']['median']:.2f}, всего ${s['cost']['total']:.2f}")
