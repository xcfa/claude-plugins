"""Состояние очереди на диске: папка .aiflow проекта (Store), настройки, отчёты сессий, файловый lock.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from aiflow_core import (
    AiflowError, COMPLEXITIES, DEFAULT_CONFIG, MODEL_FIELDS, TALK_KINDS, TASK_ID_RE, TERMINAL, check_model_field, iso,
    owner_alive, read_json, write_json,
)

# ---------------------------------------------------------------- storage

def sync_queue(q: dict) -> dict:
    """Пересчитать производные поля очереди: paused — главная из причин паузы (сначала бессрочные),
    review_lock — первый PR на ревью."""
    pauses = q.get("pauses") or {}
    if pauses:
        reason = sorted(pauses, key=lambda r: (bool(pauses[r].get("until")), pauses[r].get("at") or ""))[0]
        q["paused"] = {**pauses[reason], "reason": reason, "reasons": sorted(pauses)}
    else:
        q["paused"] = None
    locks = q.get("review_locks") or []
    q["review_lock"] = locks[0] if locks else None
    return q


STATE_DIR = ".aiflow"
LEGACY_STATE_DIR = "runs"  # так папка называлась до 0.2.85


def state_dir(root: Path) -> Path:
    """Папка состояния очереди в репозитории. Старая runs/ действует, пока проект не переехал."""
    new, old = Path(root) / STATE_DIR, Path(root) / LEGACY_STATE_DIR
    if not new.exists() and (old / "config.json").is_file():
        return old
    return new


def migrate_state_dir(root: Path) -> str | None:
    """Переезд runs/ → .aiflow/. Только когда нет идущих задач: у живых сессий старые пути уже в промптах.
    Возвращает строку для журнала или None, если переезжать нечего или пока нельзя."""
    root = Path(root)
    new, old = root / STATE_DIR, root / LEGACY_STATE_DIR
    if new.exists() or not (old / "config.json").is_file():
        return None
    store = Store(root)
    if any(t.get("state") not in TERMINAL for t in store.tasks()):
        return None
    os.rename(old, new)
    store = Store(root)
    cfg = read_json(new / "config.json", None)
    if isinstance(cfg, dict) and isinstance(cfg.get("protected_paths"), list) and "runs/**" in cfg["protected_paths"]:
        cfg["protected_paths"] = [".aiflow/**" if x == "runs/**" else x for x in cfg["protected_paths"]]
        write_json(new / "config.json", cfg)
    gi = root / ".gitignore"
    if gi.exists():
        raw = gi.read_bytes().decode("utf-8")
        eol = "\r\n" if "\r\n" in raw else "\n"
        lines = raw.splitlines()
        if ".aiflow/" not in lines:
            lines = [".aiflow/" if x.strip() in ("runs/", "runs", "/runs/", "/runs") else x for x in lines]
            if ".aiflow/" not in lines:
                lines.append(".aiflow/")
            gi.write_bytes((eol.join(lines) + eol).encode("utf-8"))
    store.event("config", None, "папка состояния переименована: runs/ → .aiflow/")
    return "runs/ → .aiflow/"


def stale_settings(user: dict) -> dict[str, str]:
    """Значения в .aiflow/config.json, которые были умолчаниями прежних версий: ключ совета → текст.
    aiflow их не меняет — пользователь мог выбрать их сам, — а говорит о них: один раз событием в панели
    (Store.new_advice) и в `aiflow doctor`."""
    out = {}
    if user.get("notify") == "important":
        out["notify"] = ('"notify": "important" — умолчание до версии 0.2.47: монитор будит оркестратора на каждое '
                         'событие, которое ждёт вас, а это токены. Теперь умолчание — "answers" (только ответы на '
                         'вопросы): об остальном говорят системные уведомления и панель')
    if isinstance(user.get("worker_model"), str):
        out["worker_model"] = (f'"worker_model": "{user["worker_model"]}" — одна модель исполнителя на все задачи '
                               '(умолчание до версии 0.2.57). Модель по сложности задачи: "worker_model": '
                               '{"trivial": "haiku", "simple": "sonnet", "complex": "opus"} и "worker_effort": '
                               '{"trivial": "high", "simple": "medium", "complex": "high"}')
    if "**/auth/**" in (user.get("protected_paths") or []):
        out["auth_path"] = ('"**/auth/**" в protected_paths — умолчание до версии 0.2.107: под него попадает и папка '
                            'экрана входа во фронтенде. Если защищать нужно серверный код, замените на его путь')
    return out


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.runs = state_dir(root)

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

    def migrate_config(self) -> list[str]:
        """Дописать в .aiflow/config.json ключи, появившиеся в новой версии. Заданные значения не меняются:
        строка на месте словаря (например, worker_effort: "high") так и остаётся, списки не дополняются."""
        path = self.runs / "config.json"
        user = read_json(path, None)
        if not isinstance(user, dict):
            return []
        added = []
        for k, v in DEFAULT_CONFIG.items():
            if k not in user:
                user[k] = json.loads(json.dumps(v))
                added.append(k)
            elif isinstance(v, dict) and isinstance(user[k], dict):
                for sk, sv in v.items():
                    if sk not in user[k]:
                        user[k][sk] = sv
                        added.append(f"{k}.{sk}")
        if added:
            write_json(path, user)
        return added

    def advice(self) -> dict[str, str]:
        """Советы по настройкам проекта, оставшимся от умолчаний прежних версий: ключ → текст."""
        user = read_json(self.runs / "config.json", None)
        return stale_settings(user) if isinstance(user, dict) else {}

    def new_advice(self) -> dict[str, str]:
        """Советы, о которых проект ещё не слышал. Каждый отдаётся один раз: что уже сказано, помнит
        .aiflow/advice.json."""
        path = self.runs / "advice.json"
        seen = (read_json(path, {}) or {}).get("seen") or []
        fresh = {k: v for k, v in self.advice().items() if k not in seen}
        if fresh:
            write_json(path, {"seen": sorted({*seen, *fresh})})
        return fresh

    def set_config(self, key: str, value) -> None:
        """Меняет один ключ в .aiflow/config.json, остальное содержимое файла сохраняет как есть."""
        path = self.runs / "config.json"
        user = read_json(path, None)
        if not isinstance(user, dict):
            raise AiflowError(".aiflow/config.json не найден или повреждён — выполните aiflow init")
        user[key] = value
        write_json(path, user)

    def queue(self) -> dict:
        """pauses — причины паузы {причина: {at, until, note}}: очередь стоит, пока есть хоть одна.
        review_locks — задачи, чьи PR сейчас на ревью. paused и review_lock — производные, для панели."""
        q = read_json(self.runs / "queue.json", {}) or {}
        if not isinstance(q.get("pauses"), dict):  # файл старой версии: одна пауза, один lock
            old = q.get("paused")
            q["pauses"] = {old.get("reason") or "manual": old} if isinstance(old, dict) else {}
        if not isinstance(q.get("review_locks"), list):
            q["review_locks"] = [q["review_lock"]] if q.get("review_lock") else []
        return sync_queue(q)

    def save_queue(self, q: dict) -> None:
        write_json(self.runs / "queue.json", sync_queue(q))

    def approved_path(self, tid: str) -> Path:
        """Отпечаток диффа, который ревьюер одобрил: тот же дифф после rebase повторно не ревьюится."""
        return self.runs / tid / "approved.json"

    def task_path(self, tid: str) -> Path:
        return self.runs / tid / "task.json"

    def plan_review_path(self, tid: str) -> Path:
        """Итог ревью плана (`aiflow review-plan`): замечания ревьюера или причина сбоя."""
        return self.runs / tid / "plan-review.json"

    def notes_path(self, tid: str) -> Path:
        """Заметки планировщика о коде для исполнителя (отчёт plan_ready с --details-file)."""
        return self.runs / tid / "notes.md"

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
                 summary: str = "", details: str = "", complexity: str | None = None) -> dict:
    store = Store(root)
    if not store.task(tid):
        raise AiflowError(f"задача {tid} не найдена в {store.runs}")
    rep = {"rid": uuid.uuid4().hex, "at": iso(), "role": role, "status": status,
           "pr": pr, "summary": summary.strip(), "details": details.strip()}
    if complexity:
        rep["complexity"] = complexity
    write_json(store.runs / tid / "report.json", rep)
    store.log(tid, {"report": rep})
    return rep


class FileLock:
    """Простой межпроцессный lock на файле с pid владельца."""

    def __init__(self, path: Path, stale_minutes: float = 10):
        self.path = path
        self.stale = stale_minutes
        self.held = False

    def acquire(self, wait_seconds: float = 0, extra: dict | None = None) -> bool:
        deadline = time.time() + wait_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, json.dumps({"pid": os.getpid(), "at": iso(), **(extra or {})}).encode())
                os.close(fd)
                self.held = True
                return True
            except FileExistsError:
                info = read_json(self.path, {}) or {}
                if not info and self._file_age_minutes() < 0.1:
                    time.sleep(0.2)  # файл только что создан, pid ещё не записан
                    continue
                if not owner_alive(info):
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
        return owner_alive(read_json(self.path, {}) or {})

    def data(self) -> dict | None:
        """Содержимое lock-файла, если его владелец жив."""
        info = read_json(self.path, {}) or {}
        return info if owner_alive(info) else None


def normalize_task(raw: dict) -> dict:
    tid = str(raw.get("id", "")).strip().lower()
    if not TASK_ID_RE.match(tid):
        raise AiflowError(f"некорректный id задачи: {tid!r} (латиница, цифры, дефисы)")
    kind = raw.get("kind") or "task"
    if kind not in ("task",) + TALK_KINDS:
        raise AiflowError(f"{tid}: kind должен быть task, discuss или ask")
    cx = raw.get("complexity") or None  # обычно не задаётся: оценивает планировщик
    if cx is not None and cx not in COMPLEXITIES:
        raise AiflowError(f"{tid}: complexity должен быть одним из {', '.join(COMPLEXITIES)}")
    text = str(raw.get("text") or "").strip()
    if not text:
        raise AiflowError(f"{tid}: пустой text")
    try:
        models = {f: check_model_field(f, raw.get(f)) for f in MODEL_FIELDS}
    except AiflowError as e:
        raise AiflowError(f"{tid}: {e}") from None
    try:
        items = max(1, int(raw.get("items") or 1))  # сколько независимых мелких пунктов в одной задаче
    except (TypeError, ValueError):
        raise AiflowError(f"{tid}: items должен быть числом пунктов задачи") from None
    return {
        "id": tid, "title": str(raw.get("title") or tid).strip(), "text": text, "complexity": cx, "kind": kind,
        "items": items,
        "depends_on": [str(d) for d in raw.get("depends_on") or []],
        "ui": raw.get("ui") or None,
        **models,
        "allowed_protected_paths": [],
        "state": "new", "pr": None, "sessions": {"planner": [], "worker": []},
        "usage": {}, "created_at": iso(), "state_at": iso(),
        "history": [{"at": iso(), "event": "created", "by": "intake"}],
    }
