"""Расход токенов и цена: журналы сессий Claude Code, прогоны ревьюера, чаты координатора.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from pathlib import Path

from aiflow_core import aiflow_home, iso, read_json, write_json
from aiflow_store import Store

# ---------------------------------------------------------------- usage

# cache_write — вся запись в кэш; cache_write_1h — её часть с часовым сроком жизни (она дороже)
TOKEN_KINDS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")

# Цены API Anthropic, $ за 1 млн токенов: (вход, выход, чтение из кэша). Запись в кэш считается от цены
# входа: ×1.25 на 5 минут, ×2 на час. Это оценка «сколько стоило бы по API»: на подписке вы платите не
# за токены. Свои цены — в ~/.aiflow/config.json: "prices": {"<начало id модели>": {"input": …,
# "output": …, "cache_read": …}}. Модель ищется по самому длинному совпавшему началу id.
PRICES = {
    "claude-fable-5-2": (8.0, 40.0, 0.20), "claude-mythos-5-2": (8.0, 40.0, 0.20),
    "claude-fable-5-1": (10.0, 50.0, 1.0), "claude-mythos-5-1": (10.0, 50.0, 1.0),
    "claude-fable-5": (10.0, 50.0, 1.0), "claude-mythos-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20), "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10), "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
CACHE_WRITE_5M, CACHE_WRITE_1H = 1.25, 2.0


def price_table() -> dict[str, tuple]:
    """Встроенные цены плюс свои из ~/.aiflow/config.json."""
    table = dict(PRICES)
    user = (read_json(aiflow_home() / "config.json", {}) or {}).get("prices")
    for model, p in (user.items() if isinstance(user, dict) else ()):
        try:
            inp = float(p["input"])
            table[str(model)] = (inp, float(p["output"]), float(p.get("cache_read", inp * 0.1)))
        except (TypeError, ValueError, KeyError):
            continue
    return table


def model_price(model: str, table: dict[str, tuple]) -> tuple | None:
    hits = [k for k in table if str(model or "").startswith(k)]
    return table[max(hits, key=len)] if hits else None


def usage_cost(model: str, u: dict, table: dict[str, tuple]) -> float | None:
    """Стоимость расхода одной модели в долларах; None — цена модели неизвестна."""
    if u.get("reported_cost") is not None:
        return float(u["reported_cost"])  # цену посчитал сам инструмент (ревьюер opencode)
    price = model_price(model, table)
    if not price:
        return None
    inp, out, read = price
    write = int(u.get("cache_write") or 0)
    hour = min(write, int(u.get("cache_write_1h") or 0))
    tokens = (int(u.get("input") or 0) * inp + int(u.get("output") or 0) * out + int(u.get("cache_read") or 0) * read
              + (write - hour) * inp * CACHE_WRITE_5M + hour * inp * CACHE_WRITE_1H)
    return tokens / 1_000_000


def priced(usage: dict[str, dict], table: dict[str, tuple]) -> tuple[dict[str, dict], float, list[str]]:
    """Расход по моделям с полем cost, общая стоимость и модели без цены."""
    out, total, unknown = {}, 0.0, []
    for model, u in usage.items():
        cost = usage_cost(model, u, table)
        out[model] = {**u, "cost": cost}
        if cost is None:
            unknown.append(model)
        else:
            total += cost
    return out, total, unknown
_usage_cache: dict[str, tuple[float, dict]] = {}


def _file_msgs(path: str) -> dict[str, tuple[str, dict]]:
    """{id сообщения: (модель, usage)} одного транскрипта (с кэшем по mtime)."""
    mtime = os.path.getmtime(path)
    cached = _usage_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    per_msg: dict[str, tuple[str, dict]] = {}  # стриминг пишет одно сообщение несколько раз — берём последнюю запись
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message") if isinstance(obj, dict) else None
            if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                continue
            model = msg.get("model") or "?"
            if model == "<synthetic>":
                continue
            per_msg[msg.get("id") or f"{path}:{i}"] = (model, msg["usage"])
    _usage_cache[path] = (mtime, per_msg)
    return per_msg


def last_words(session_id: str | None, limit: int = 420) -> str:
    """Начало последнего сообщения сессии: на чём она остановилась и чего ждёт. Пусто, если журнала нет."""
    if not session_id:
        return ""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    files = glob.glob(str(base / "projects" / "*" / f"{session_id}.jsonl"))
    if not files:
        return ""
    try:
        with open(files[0], "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 400_000))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        msg = obj.get("message") if isinstance(obj, dict) else None
        if not isinstance(msg, dict) or obj.get("type") != "assistant" or obj.get("isSidechain"):
            continue
        parts = msg.get("content") if isinstance(msg.get("content"), list) else []
        text = " ".join(str(b.get("text") or "") for b in parts if isinstance(b, dict) and b.get("type") == "text")
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
    return ""


def add_usage(into: dict[str, dict], models: dict[str, dict]) -> dict[str, dict]:
    for model, u in models.items():
        m = into.setdefault(model, dict.fromkeys(TOKEN_KINDS, 0))
        for k in TOKEN_KINDS:
            m[k] += int(u.get(k) or 0)
        if u.get("reported_cost") is not None:
            m["reported_cost"] = m.get("reported_cost", 0.0) + float(u["reported_cost"])
        if "cache_write_1h" not in u:
            # запись старой версии, без разбивки по сроку: Claude Code пишет кэш на час — считаем по этой цене
            m["cache_write_1h"] += int(u.get("cache_write") or 0)
    return into


def transcript_usage(session_id: str, seen: set[str] | None = None) -> dict | None:
    """Расход сессии по моделям, вместе с её сабагентами (explorer, researcher и т. п.).
    seen — id сообщений, уже посчитанных в других сессиях задачи: возобновлённая через
    --resume сессия копирует историю предыдущей, и без этого её расход считался бы дважды."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    files = glob.glob(str(base / "projects" / "*" / f"{session_id}.jsonl"))
    if not files:
        return None
    files += glob.glob(str(base / "projects" / "*" / session_id / "subagents" / "*.jsonl"))
    models: dict[str, dict] = {}
    main: dict[str, dict] = {}
    for i, path in enumerate(files):
        try:
            msgs = _file_msgs(path)
        except OSError:
            continue
        fu: dict[str, dict] = {}
        for mid, (model, u) in msgs.items():
            if seen is not None:
                if mid in seen:
                    continue
                seen.add(mid)
            m = fu.setdefault(model, dict.fromkeys(TOKEN_KINDS, 0))
            m["input"] += int(u.get("input_tokens") or 0)
            m["output"] += int(u.get("output_tokens") or 0)
            m["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
            m["cache_write"] += int(u.get("cache_creation_input_tokens") or 0)
            m["cache_write_1h"] += int((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)
        add_usage(models, fu)
        if i == 0:
            main = fu
    tot = {k: sum(u[k] for u in models.values()) for k in TOKEN_KINDS}
    model = max(main, key=lambda m: main[m]["output"], default="")
    return {**tot, "model": model, "models": models}


def transcript_age_minutes(session_id: str | None) -> float | None:
    """Сколько минут назад сессия последний раз что-то записала в свой транскрипт; None — транскрипта нет."""
    if not session_id:
        return None
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    times = []
    for path in glob.glob(str(base / "projects" / "*" / f"{session_id}.jsonl")):
        try:
            times.append(os.path.getmtime(path))
        except OSError:
            pass
    return (time.time() - max(times)) / 60 if times else None


def reviewer_runs(store: "Store", tid: str) -> list[dict]:
    data = read_json(store.runs / tid / "reviewer.json", {}) or {}
    return [x for x in data.get("runs") or [] if isinstance(x, dict)]


def reviewer_usage(store: "Store", tid: str) -> dict | None:
    """Расход ревьюера opencode по задаче (.aiflow/<id>/reviewer.json) в том же виде, что расход сессии Claude."""
    models: dict[str, dict] = {}
    for run in reviewer_runs(store, tid):
        m = models.setdefault(str(run.get("model") or "opencode"),
                              {**dict.fromkeys(TOKEN_KINDS, 0), "reported_cost": 0.0})
        for k in ("input", "output", "cache_read", "cache_write"):
            m[k] += int(run.get(k) or 0)
        m["reported_cost"] += float(run.get("cost") or 0)
    if not models:
        return None
    return {"role": "reviewer", "model": max(models, key=lambda k: models[k]["output"]), "models": models}


def task_usage(t: dict) -> dict[str, dict]:
    """Расход задачи по моделям; старые записи без разбивки относятся к основной модели сессии."""
    out: dict[str, dict] = {}
    for u in (t.get("usage") or {}).values():
        add_usage(out, u.get("models") or {u.get("model") or "?": u})
    return out


def coordinator_sessions(store: "Store") -> dict[str, dict]:
    """Чаты координатора проекта, которые запускал aiflow: id сессии Claude → запись с расходом."""
    data = read_json(store.runs / "coordinator.json", {}) or {}
    return data.get("sessions") if isinstance(data.get("sessions"), dict) else {}


def track_coordinator(store: "Store", session_id: str | None, sid: str | None = None) -> None:
    """Запомнить чат координатора: его расход считается вместе с расходом задач проекта."""
    if not session_id:
        return
    sessions = coordinator_sessions(store)
    if session_id not in sessions:
        sessions[session_id] = {"sid": sid, "started_at": iso()}
        write_json(store.runs / "coordinator.json", {"sessions": sessions})


def collect_coordinator_usage(store: "Store") -> None:
    """Обновить расход чатов координатора по их транскриптам. Чат, который молчит больше часа, не перечитывается."""
    sessions = coordinator_sessions(store)
    changed = False
    for session_id, e in sessions.items():
        age = transcript_age_minutes(session_id)
        if age is None or (e.get("usage") and age > 60):
            continue
        u = transcript_usage(session_id)
        if u and u != e.get("usage"):
            e["usage"], e["at"] = u, iso()
            changed = True
    if changed:
        write_json(store.runs / "coordinator.json", {"sessions": sessions})


def coordinator_state(store: "Store", prices: dict) -> dict:
    """Расход координатора для панели: общий по моделям и по чатам (день — когда чат последний раз работал)."""
    total: dict[str, dict] = {}
    chats = []
    for e in coordinator_sessions(store).values():
        models = (e.get("usage") or {}).get("models") or {}
        if not models:
            continue
        add_usage(total, models)
        usage, cost, _ = priced(models, prices)
        chats.append({"at": e.get("at") or e.get("started_at"), "usage": usage, "cost": round(cost, 4)})
    usage, cost, _ = priced(total, prices)
    return {"usage": usage, "cost": round(cost, 4), "raw": total, "chats": chats,
            "tokens": sum(u["input"] + u["output"] + u["cache_write"] for u in total.values())}


def done_note(t: dict) -> str | None:
    """Итог задачи, законченной без PR: строка отчёта, с которой она перешла в done."""
    return next((h.get("note") for h in reversed(t.get("history") or []) if h.get("to") == "done" and h.get("note")), None)


def task_tokens(t: dict) -> int:
    return sum(u["input"] + u["output"] + u["cache_write"] for u in task_usage(t).values())
