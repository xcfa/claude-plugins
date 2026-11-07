"""Локальная панель aiflow: http://127.0.0.1:<port>/ (только loopback, действия с токеном)."""
from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import aiflow

PAGE = Path(__file__).resolve().parent / "ui.html"


class _Cache:
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


def start_server(store: aiflow.Store, port: int, wake: threading.Event):
    token = secrets.token_urlsafe(24)
    cache = _Cache(store)

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
            if self.path == "/api/state":
                return self._json(200, cache.state())
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
                elif action == "tick":
                    pass
                else:
                    return self._json(400, {"error": "неизвестное действие"})
                wake.set()
                return self._json(200, {"ok": True})
            except (ValueError, aiflow.AiflowError) as e:
                return self._json(400, {"error": str(e)})

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="aiflow-ui").start()
    return srv, f"http://127.0.0.1:{port}/"
