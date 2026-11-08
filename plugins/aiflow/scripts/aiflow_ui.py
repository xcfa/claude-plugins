"""Локальная панель aiflow: http://127.0.0.1:<port>/ (только loopback, действия с токеном)."""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiflow

PAGE = Path(__file__).resolve().parent / "ui.html"
# provider/model, как его печатает `opencode models`; пустая строка — модель по умолчанию из конфига opencode
MODEL_RE = re.compile(r"^[A-Za-z0-9][\w.\-]*/[\w.:@+\-/]+$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
MODELS_TTL = 600


class _Models:
    """Список моделей opencode с кэшем: `opencode models` работает несколько секунд."""

    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()
        self.items: list[str] = []
        self.error = ""
        self.at = 0.0

    def get(self, refresh: bool = False) -> tuple[list[str], str]:
        with self.lock:
            if refresh or not self.at or time.time() - self.at > MODELS_TTL:
                self.items, self.error = self._load()
                self.at = time.time()
            return self.items, self.error

    def _load(self) -> tuple[list[str], str]:
        exe = shutil.which("opencode")
        if not exe:
            return [], "opencode не найден в PATH"
        try:
            p = aiflow.run([exe, "models"], cwd=self.root, timeout=90)
        except aiflow.AiflowError as e:
            return [], str(e)
        lines = [ANSI_RE.sub("", x).strip() for x in (p.stdout or "").splitlines()]
        items = sorted({x for x in lines if MODEL_RE.match(x)})
        if p.returncode != 0 and not items:
            tail = ANSI_RE.sub("", (p.stderr or p.stdout or "").strip())[-300:]
            return [], f"opencode models завершился с ошибкой: {tail}"
        return items, ""


def _task_session(t: dict) -> str | None:
    """Последняя сессия задачи: исполнитель, если он уже был, иначе планировщик."""
    for role in ("worker", "planner"):
        lst = (t.get("sessions") or {}).get(role) or []
        if lst and lst[-1].get("id"):
            return str(lst[-1]["id"])
    return None


def open_terminal(root: Path, sid: str) -> str:
    """Открыть `claude attach <sid>` в новом окне терминала. Возвращает, где открыли."""
    if not re.fullmatch(r"[A-Za-z0-9][\w\-]{0,100}", sid):
        raise aiflow.AiflowError("некорректный id сессии")
    claude = shutil.which("claude") or "claude"
    title = f"aiflow {sid}"
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        wt = shutil.which("wt.exe") or shutil.which("wt")
        if wt:
            subprocess.Popen([wt, "-w", "new", "new-tab", "-d", str(root), "--title", title, claude, "attach", sid],
                             cwd=root, creationflags=flags, env=aiflow.claude_env())
            return "Windows Terminal"
        subprocess.Popen(["cmd", "/c", "start", title, "/D", str(root), claude, "attach", sid],
                         cwd=root, creationflags=flags, env=aiflow.claude_env())
        return "cmd"
    cmd = f"cd {shlex.quote(str(root))} && {shlex.quote(claude)} attach {sid}"
    if sys.platform == "darwin":
        esc = cmd.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{esc}"',
                          "-e", 'tell application "Terminal" to activate'], cwd=root)
        return "Terminal"
    for term, args in (("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]), ("xterm", ["-e"])):
        exe = shutil.which(term)
        if exe:
            subprocess.Popen([exe, *args, "sh", "-c", cmd], cwd=root, start_new_session=True)
            return term
    raise aiflow.AiflowError("не нашёл терминал; выполните вручную: claude attach " + sid)


class _Cache:
    """Сессии и адрес репозитория одного проекта: `claude agents` и `gh repo view` дорогие."""

    def __init__(self, store: aiflow.Store):
        self.store = store
        self.lock = threading.Lock()
        self.sessions: dict = {}
        self.sessions_at = 0.0
        self.repo_url: str | None = None

    def state(self) -> dict:
        with self.lock:
            if time.time() - self.sessions_at > 15:
                try:
                    self.sessions = aiflow.Claude(self.store.root).agents()
                except aiflow.AiflowError:
                    self.sessions = {}
                self.sessions_at = time.time()
            if self.repo_url is None:
                try:
                    self.repo_url = aiflow.GH(self.store.root).repo_url()
                except aiflow.AiflowError:
                    self.repo_url = ""
            return aiflow.build_state(self.store, self.sessions, self.repo_url or "")


def start_server(provider, port: int, wake: threading.Event, service: bool = False):
    """provider() — список Store проектов панели. service=True — общая панель службы."""
    import aiflow_service as svc
    token = secrets.token_urlsafe(24)
    caches: dict[str, _Cache] = {}
    models: dict[str, _Models] = {}
    lock = threading.Lock()

    def stores() -> dict[str, aiflow.Store]:
        return {svc.project_key(st.root): st for st in provider()}

    def cache(key: str, store: aiflow.Store) -> _Cache:
        with lock:
            if key not in caches:
                caches[key] = _Cache(store)
            return caches[key]

    def project_state(key: str, store: aiflow.Store) -> dict:
        base = {"key": key, "name": store.root.name, "root": str(store.root)}
        if not store.initialized:
            return {**base, "error": "проект не инициализирован или удалён (нет runs/config.json)", "tasks": [], "events": []}
        try:
            return {**cache(key, store).state(), **base}
        except Exception as e:  # один сломанный проект не должен ронять всю панель
            return {**base, "error": f"не удалось прочитать состояние: {e}", "tasks": [], "events": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # тишина в stdout монитора
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            if self.path in ("/", "/index.html"):
                html = PAGE.read_text(encoding="utf-8").replace("__AIFLOW_TOKEN__", token)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            url = urlsplit(self.path)
            if url.path == "/api/state":
                return self._json(200, {"version": aiflow.VERSION, "service": service, "now": aiflow.iso(),
                                        "projects": [project_state(k, st) for k, st in stores().items()]})
            if url.path == "/api/settings":
                q = parse_qs(url.query)
                st = stores().get((q.get("project") or [""])[0])
                if not st or not st.initialized:
                    return self._json(404, {"error": "нет такого проекта"})
                key = svc.project_key(st.root)
                with lock:
                    m = models.setdefault(key, _Models(st.root))
                items, error = m.get(q.get("refresh") == ["1"])
                return self._json(200, {"reviewer_model": st.config().get("reviewer_model") or "",
                                        "models": items, "models_error": error})
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._host_ok() or self.headers.get("X-Aiflow-Token") != token:
                return self._json(403, {"error": "forbidden"})
            if self.path != "/api/action":
                return self._json(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                action, tid = body.get("action"), body.get("id")
                if action == "tick":
                    wake.set()
                    return self._json(200, {"ok": True})
                all_stores = stores()
                store = all_stores.get(body.get("project") or "")
                if store is None and len(all_stores) == 1 and not body.get("project"):
                    store = next(iter(all_stores.values()))
                if store is None:
                    return self._json(400, {"error": "не указан проект"})
                if action == "forget":
                    if not service:
                        return self._json(400, {"error": "проекты убираются только из общей панели службы"})
                    svc.forget(store.root)
                    return self._json(200, {"ok": True})
                if not store.initialized:
                    return self._json(400, {"error": "проект не инициализирован"})
                if action == "pause":
                    store.command({"cmd": "pause", "reason": "из панели"})
                elif action == "resume":
                    store.command({"cmd": "resume"})
                elif action == "retry" and tid:
                    store.command({"cmd": "retry", "id": tid})
                elif action == "approve" and tid:
                    t = store.task(tid)
                    if not t or not t.get("pr"):
                        return self._json(400, {"error": "у задачи нет PR с планом"})
                    aiflow.GH(store.root).add_label(t["pr"], store.config()["labels"]["approved"])
                elif action == "open_session" and tid:
                    t = store.task(tid)
                    sid = _task_session(t) if t else None
                    if not sid:
                        return self._json(400, {"error": "у задачи ещё нет сессии"})
                    where = open_terminal(store.root, sid)
                    return self._json(200, {"ok": True, "where": where, "command": f"claude attach {sid}"})
                elif action == "cancel" and tid:
                    warnings = aiflow.cancel_tasks(store, [tid], note="из панели")
                    wake.set()
                    return self._json(200, {"ok": True, "warnings": warnings})
                elif action == "allow_paths" and tid:
                    value = body.get("value")
                    paths = value if isinstance(value, list) else re.split(r"[,\n]+", str(value or ""))
                    aiflow.allow_paths(store, tid, [p for p in paths if str(p).strip()])
                elif action == "set_reviewer_model":
                    value = body.get("value")
                    value = value.strip() if isinstance(value, str) else None
                    if value is None or len(value) > 200 or (value and not MODEL_RE.match(value)):
                        return self._json(400, {"error": "модель указывается как provider/model, например deepseek/deepseek-chat"})
                    store.set_config("reviewer_model", value)
                    store.event("config", None, f"модель ревьюера: {value or 'по умолчанию opencode'} (из панели)")
                    return self._json(200, {"ok": True, "reviewer_model": value})
                else:
                    return self._json(400, {"error": "неизвестное действие"})
                wake.set()
                return self._json(200, {"ok": True})
            except (ValueError, OSError, aiflow.AiflowError) as e:
                return self._json(400, {"error": str(e)})

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="aiflow-ui").start()
    return srv, f"http://127.0.0.1:{port}/"
