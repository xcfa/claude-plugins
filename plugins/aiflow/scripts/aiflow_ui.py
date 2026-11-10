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


def _win_focus(title: str, wait: float = 0.0) -> bool:
    """Windows: вывести на передний план окно, в заголовке которого есть title.
    Служба — фоновый процесс, и Windows не даёт ему просто так забрать фокус: перед
    SetForegroundWindow нажимаем и отпускаем Alt — это снимает блокировку переднего плана."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if title in buf.value:
                    found.append(hwnd)
                    return False
        return True

    deadline = time.time() + wait
    while True:
        found.clear()
        user32.EnumWindows(each, 0)
        if found or time.time() >= deadline:
            break
        time.sleep(0.25)
    if not found:
        return False
    hwnd = found[0]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU down
    user32.keybd_event(0x12, 0, 2, 0)  # VK_MENU up
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    return True


WT_WINDOW = "aiflow"  # имя окна Windows Terminal: все сессии открываются в нём вкладками


def session_title(t: dict | None, sid: str) -> str:
    """Заголовок вкладки сессии: id задачи и её название. Без символов, которые терминал или
    командная строка поймут по-своему (`;` в wt разделяет команды)."""
    t = t or {}
    tid, name = str(t.get("id") or ""), str(t.get("title") or "")
    text = tid + (f": {name}" if name and name != tid else "") if tid else f"aiflow {sid}"
    text = re.sub(r"[^\w .,:()#№+\-—]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text if len(text) <= 60 else text[:59].rstrip() + "…") or f"aiflow {sid}"


def open_terminal(root: Path, sid: str, title: str | None = None, tool: str = "claude") -> str:
    """Открыть сессию в терминале: `claude attach <sid>` или, для ревьюера, `opencode -s <sid>`.
    В Windows Terminal — вкладкой общего окна aiflow с заголовком задачи. Если вкладка этой сессии уже
    на виду — вывести её окно на передний план. Возвращает, где открыли."""
    if not re.fullmatch(r"[A-Za-z0-9][\w\-]{0,100}", sid):
        raise aiflow.AiflowError("некорректный id сессии")
    if tool == "opencode":
        exe = shutil.which("opencode")
        if not exe:
            raise aiflow.AiflowError("opencode не найден в PATH")
        return _open_tab(root, [exe, "-s", sid], title or f"ревьюер {sid}", f"opencode -s {sid}")
    claude = shutil.which("claude") or "claude"
    return _open_tab(root, [claude, "attach", sid], title or f"aiflow {sid}", f"claude attach {sid}")


def _open_tab(root: Path, command: list[str], title: str, manual: str) -> str:
    claude, sid = command[0], command[-1]
    attach = command[1:-1]
    if os.name == "nt":
        if _win_focus(title):
            return "уже открытой вкладке"
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        wt = shutil.which("wt.exe") or shutil.which("wt")
        if wt:
            # -w aiflow: вкладка в окне с этим именем (нет окна — оно создаётся).
            # --suppressApplicationTitle: Claude Code меняет заголовок, а по нему вкладка ищется в следующий раз.
            subprocess.Popen([wt, "-w", WT_WINDOW, "new-tab", "-d", str(root), "--title", title,
                              "--suppressApplicationTitle", claude, *attach, sid],
                             cwd=root, creationflags=flags, env=aiflow.claude_env())
            where = "вкладке Windows Terminal"
        else:
            subprocess.Popen(["cmd", "/c", "start", title, "/D", str(root), claude, *attach, sid],
                             cwd=root, creationflags=flags, env=aiflow.claude_env())
            where = "cmd"
        threading.Thread(target=_win_focus, args=(title, 8.0), daemon=True).start()
        return where
    cmd = f"cd {shlex.quote(str(root))} && {shlex.quote(claude)} {' '.join(attach)} {sid}"
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
    raise aiflow.AiflowError("не нашёл терминал; выполните вручную: " + manual)


class _Cache:
    """Сессии и адрес репозитория одного проекта. `claude agents` и `gh repo view` дорогие (после
    загрузки системы — секунды), поэтому запрос панели их не ждёт: отдаёт задачи сразу, а сессии и
    адрес обновляет фоновый поток. Адрес репозитория хранится на диске между перезапусками."""

    SESSIONS_TTL = 15

    def __init__(self, store: aiflow.Store):
        self.store = store
        self.lock = threading.Lock()
        self.sessions: dict = {}
        self.sessions_at = 0.0
        self.loaded = False
        self.refreshing = False
        self.repo_url: str | None = _repo_urls().get(str(store.root))

    def refresh(self) -> None:
        """Обновить в фоне, если устарело; не больше одного обновления за раз."""
        with self.lock:
            if self.refreshing or time.time() - self.sessions_at <= self.SESSIONS_TTL:
                return
            self.refreshing = True
        threading.Thread(target=self._refresh, daemon=True).start()

    def _refresh(self) -> None:
        try:
            try:
                sessions = aiflow.Claude(self.store.root).agents()
            except aiflow.AiflowError:
                sessions = self.sessions
            with self.lock:
                self.sessions, self.loaded = sessions, True
            if not self.repo_url:
                try:
                    url = aiflow.GH(self.store.root).repo_url()
                except aiflow.AiflowError:
                    url = ""
                if url:
                    self.repo_url = url
                    _save_repo_url(str(self.store.root), url)
        finally:
            with self.lock:
                self.sessions_at = time.time()
                self.refreshing = False

    def state(self) -> dict:
        self.refresh()
        with self.lock:
            sessions, loaded = self.sessions, self.loaded
        st = aiflow.build_state(self.store, sessions, self.repo_url or "")
        st["sessions_loading"] = not loaded
        return st


def _repo_urls_path() -> Path:
    return aiflow.aiflow_home() / "repo_urls.json"


def _repo_urls() -> dict:
    return aiflow.read_json(_repo_urls_path(), {}) or {}


_REPO_URLS_LOCK = threading.Lock()


def _save_repo_url(root: str, url: str) -> None:
    with _REPO_URLS_LOCK:
        data = _repo_urls()
        if data.get(root) != url:
            data[root] = url
            aiflow.write_json(_repo_urls_path(), data)


def start_server(provider, port: int, wake: threading.Event, service: bool = False):
    """provider() — список Store проектов панели. service=True — общая панель службы."""
    import aiflow_service as svc
    token = svc.panel_token() if service else secrets.token_urlsafe(24)
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

    def warm() -> None:
        try:
            for k, st in stores().items():
                if st.initialized:
                    cache(k, st).refresh()
        except Exception:
            pass

    threading.Thread(target=warm, daemon=True).start()

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
                    where = open_terminal(store.root, sid, session_title(t, sid))
                    return self._json(200, {"ok": True, "where": where, "command": f"claude attach {sid}"})
                elif action == "open_reviewer" and tid:
                    t = store.task(tid)
                    run = next((r for r in reversed(aiflow.reviewer_runs(store, tid)) if r.get("session")), None) if t else None
                    if not run:
                        return self._json(400, {"error": "у задачи ещё нет сессии ревьюера"})
                    where = Path(str(run.get("dir") or ""))  # opencode ищет сессию по проекту, где она шла
                    where = where if where.is_dir() else store.root
                    title = "ревьюер " + session_title(t, run["session"])
                    place = open_terminal(where, run["session"], title[:60], tool="opencode")
                    return self._json(200, {"ok": True, "where": place, "command": f"opencode -s {run['session']}"})
                elif action == "cancel" and tid:
                    warnings = aiflow.cancel_tasks(store, [tid], note="из панели")
                    wake.set()
                    return self._json(200, {"ok": True, "warnings": warnings})
                elif action == "amend" and tid:
                    text = str(body.get("value") or "").strip()
                    t = store.task(tid)
                    if not t or t.get("state") in aiflow.TERMINAL:
                        return self._json(400, {"error": "задача уже завершена — поставьте новую"})
                    if not text or len(text) > 20000:
                        return self._json(400, {"error": "пустое или слишком длинное дополнение"})
                    store.command({"cmd": "amend", "id": tid, "text": text, "source": "panel"})
                    wake.set()
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
