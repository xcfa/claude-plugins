"""Служба aiflow: один фоновый процесс на пользователя ведёт очереди всех зарегистрированных
проектов и держит общую панель. Не зависит от сессий Claude: её поднимает `aiflow up`,
монитор сессии оркестратора или автозапуск при входе в систему."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import aiflow
from aiflow import AiflowError, FileLock, Store, iso, pid_alive, read_json, write_json

DEFAULT_PORT = 8765
PANEL_RETRY_SECONDS = 30


def home() -> Path:
    return Path(os.environ.get("AIFLOW_HOME") or Path.home() / ".aiflow")


def norm(root) -> str:
    return str(Path(root).resolve())


def project_key(root) -> str:
    return hashlib.sha1(os.path.normcase(norm(root)).encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------- реестр проектов

def _registry_path() -> Path:
    return home() / "projects.json"


def _entries() -> list[dict]:
    data = read_json(_registry_path(), {}) or {}
    return [e for e in data.get("projects") or [] if isinstance(e, dict) and e.get("root")]


def projects() -> list[str]:
    out, seen = [], set()
    for e in _entries():
        k = project_key(e["root"])
        if k not in seen:
            seen.add(k)
            out.append(norm(e["root"]))
    return out


def _edit_registry(change) -> bool:
    """Изменить реестр под lock'ом: change(list[dict]) -> list[dict] | None (None — без изменений)."""
    home().mkdir(parents=True, exist_ok=True)
    lock = FileLock(home() / "projects.lock", stale_minutes=1)
    if not lock.acquire(wait_seconds=10):
        raise AiflowError("реестр проектов занят, повторите")
    try:
        new = change(_entries())
        if new is None:
            return False
        write_json(_registry_path(), {"projects": new})
        return True
    finally:
        lock.release()


def register(root) -> bool:
    """Добавить проект в реестр службы. True, если его там не было."""
    key = project_key(root)
    return _edit_registry(lambda es: None if any(project_key(e["root"]) == key for e in es)
                          else es + [{"root": norm(root), "added_at": iso()}])


def forget(root) -> bool:
    key = project_key(root)

    def change(es):
        rest = [e for e in es if project_key(e["root"]) != key]
        return rest if len(rest) != len(es) else None
    return _edit_registry(change)


def panel_token() -> str:
    """Токен панели службы переживает её перезапуск: открытая страница не ловит forbidden."""
    p = home() / "panel.token"
    try:
        tok = p.read_text(encoding="utf-8").strip()
        if len(tok) >= 24:
            return tok
    except OSError:
        pass
    import secrets
    tok = secrets.token_urlsafe(24)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tok, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return tok


def global_config() -> dict:
    cfg = {"ui_port": DEFAULT_PORT}
    cfg.update(read_json(home() / "config.json", {}) or {})
    return cfg


# ---------------------------------------------------------------- процесс службы

def _version_tuple(v) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


def _lock() -> FileLock:
    return FileLock(home() / "service.lock", stale_minutes=0)


def info() -> dict | None:
    """Работающая служба: {pid, version, port, panel_error, ...} или None.
    Версия пишется в lock при старте; port появляется, когда служба подняла панель."""
    data = _lock().data()
    if not data:
        return None
    extra = read_json(home() / "service.json", {}) or {}
    if int(extra.get("pid") or 0) == int(data["pid"]):
        data = {**extra, **data}
    return data


def panel_url(data: dict | None = None) -> str:
    data = data if data is not None else info()
    port = (data or {}).get("port")
    if not port:
        return ""
    return f"http://127.0.0.1:{port}/"


def _python(windowless: bool = False) -> str:
    py = Path(sys.executable)
    if windowless and os.name == "nt" and (py.parent / "pythonw.exe").is_file():
        return str(py.parent / "pythonw.exe")
    return str(py)


def spawn(via_helper: bool = True) -> None:
    """Запустить службу отдельным процессом. Через промежуточный процесс, который сразу
    выходит: у службы не остаётся живого родителя, и завершение дерева процессов того,
    кто её запустил (например, инструмента Monitor), её не заденет."""
    home().mkdir(parents=True, exist_ok=True)
    cmd = [_python(), "-X", "utf8", str(Path(aiflow.__file__).resolve()), "service"]
    if via_helper:
        cmd.append("--spawn")
    env = aiflow.claude_env()
    env["PYTHONUTF8"] = "1"
    with (home() / "service.log").open("ab") as log:
        kw: dict = {"cwd": str(home()), "stdin": subprocess.DEVNULL, "stdout": log, "stderr": log,
                    "close_fds": True, "env": env}
        if os.name == "nt":
            base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            try:  # выйти из job-объекта родителя, если он это позволяет
                subprocess.Popen(cmd, creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError:
                subprocess.Popen(cmd, creationflags=base, **kw)
        else:
            subprocess.Popen(cmd, start_new_session=True, **kw)


def _down_marker() -> Path:
    return home() / "service.down"


def stopped_by_user() -> bool:
    return _down_marker().is_file()


def stop(mark_down: bool = True) -> bool:
    """Остановить службу. mark_down — пользователь остановил её сам: мониторы сессий
    не поднимают её снова, пока он не попросит (aiflow up, /aiflow:intake, /aiflow:resume)."""
    if mark_down:
        home().mkdir(parents=True, exist_ok=True)
        _down_marker().write_text(iso(), encoding="utf-8")
    data = info()
    if not data:
        return False
    pid = int(data["pid"])
    ok = aiflow._kill(pid)
    if ok:
        for name in ("service.json", "service.lock"):
            path = home() / name
            if int((read_json(path, {}) or {}).get("pid") or 0) == pid:  # не трогать файлы новой службы
                path.unlink(missing_ok=True)
    return ok


def ensure(wait: float = 20) -> dict:
    """Служба работает и не старше этой версии плагина: иначе (пере)запустить.
    Явный запрос пользователя: снимает отметку «остановлена вручную»."""
    _down_marker().unlink(missing_ok=True)
    data = info()
    if data and data.get("version") and _version_tuple(data["version"]) < _version_tuple(aiflow.VERSION):
        stop(mark_down=False)
        data = None
    if not data:
        spawn()
    deadline = time.time() + wait
    while True:
        data = info()
        if data and data.get("version") and ("port" in data or time.time() >= deadline):
            return data  # port появляется, когда служба закончила старт (поднялась панель или нет)
        if time.time() >= deadline:
            raise AiflowError(f"служба aiflow не запустилась, см. {home() / 'service.log'}")
        time.sleep(0.2)


def _foreign_monitor(store: Store) -> int:
    """pid монитора сессии старой версии, который держит проект, или 0."""
    pid = FileLock(store.runs / "monitor.lock").owner()
    svc = info()
    return pid if pid and pid != os.getpid() and (not svc or pid != int(svc["pid"])) else 0


class _Panel:
    def __init__(self, wake: threading.Event):
        self.wake = wake
        self.srv = None
        self.port = int(global_config().get("ui_port") or 0)
        self.error = ""
        self.tried = 0.0

    def ensure(self) -> None:
        if self.srv or not self.port or time.time() - self.tried < PANEL_RETRY_SECONDS:
            return
        self.tried = time.time()
        from aiflow_ui import start_server
        try:
            self.srv, _ = start_server(lambda: [Store(Path(r)) for r in projects()], self.port, self.wake, service=True)
            self.error = ""
        except OSError as e:
            self.error = f"порт {self.port} занят ({e.strerror or e}); повтор через {PANEL_RETRY_SECONDS} с"

    def shutdown(self) -> None:
        if self.srv:
            self.srv.shutdown()


def _write_info(panel: _Panel) -> None:
    write_json(home() / "service.json", {
        "pid": os.getpid(), "version": aiflow.VERSION, "script": str(Path(aiflow.__file__).resolve()),
        "port": panel.port if panel.srv else 0, "panel_error": panel.error, "started_at": iso()})


def _tick_project(root: str, held: dict, next_at: dict, errors: dict, warned: set, forced: bool, many: bool) -> None:
    k = project_key(root)
    store = Store(Path(root))
    if not store.initialized:
        return
    if k not in held:
        lk = FileLock(store.runs / "monitor.lock", stale_minutes=0)
        if not lk.acquire():
            other = _foreign_monitor(store)
            if other and k not in warned:
                warned.add(k)
                store.event("error", None, f"проект ведёт монитор сессии старой версии (pid {other}), "
                                           "служба его не трогает: aiflow stop в каталоге проекта или перезапустите ту сессию")
            return
        held[k] = lk
        warned.discard(k)
    if not forced and time.time() < next_at.get(k, 0):
        return
    cfg = aiflow.DEFAULT_CONFIG
    try:
        cfg = store.config()
        events = aiflow.run_tick_now(store, wait=0)
        errors.pop(k, None)
        aiflow.desktop_notify(cfg, events, project=store.root.name if many else None)
    except aiflow.TickBusy:
        pass  # проход уже идёт (aiflow tick вручную) — в следующий раз
    except Exception as e:  # ошибка проекта не должна останавливать службу и другие проекты
        text = f"сбой тика: {e}" if isinstance(e, AiflowError) else f"сбой тика: {e!r}"
        if errors.get(k) != text:  # одну и ту же ошибку пишем один раз, пока не сменится
            errors[k] = text
            try:
                store.event("error", None, text)
                aiflow.desktop_notify(cfg, [{"kind": "error", "text": text}], project=store.root.name)
            except Exception:
                pass
    try:
        interval = max(10, int(cfg.get("tick_seconds", 45)))
    except (TypeError, ValueError):
        interval = 45
    next_at[k] = time.time() + interval


def serve() -> int:
    """Цикл службы: проходы по всем проектам реестра и общая панель."""
    home().mkdir(parents=True, exist_ok=True)
    lock = _lock()
    if not lock.acquire(extra={"version": aiflow.VERSION}):
        return 0  # служба уже работает
    import signal

    def _stop(*_):
        raise KeyboardInterrupt
    for sig in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _stop)
    wake = threading.Event()
    panel = _Panel(wake)
    held: dict[str, FileLock] = {}
    next_at: dict[str, float] = {}
    errors: dict[str, str] = {}
    warned: set[str] = set()
    try:
        panel.ensure()
        _write_info(panel)
        print(f"{iso()} служба aiflow {aiflow.VERSION} запущена, панель: {panel_url(info()) or panel.error or 'выключена'}",
              flush=True)
        while True:
            had_panel = bool(panel.srv)
            panel.ensure()
            if bool(panel.srv) != had_panel:
                _write_info(panel)
            forced = wake.is_set()
            wake.clear()
            roots = projects()
            keys = {project_key(r) for r in roots}
            for k in [k for k in held if k not in keys]:
                held.pop(k).release()
            for root in roots:
                _tick_project(root, held, next_at, errors, warned, forced, len(roots) > 1)
            wake.wait(timeout=5)
    except KeyboardInterrupt:
        return 0
    finally:
        for lk in held.values():
            lk.release()
        panel.shutdown()
        if int((read_json(home() / "service.json", {}) or {}).get("pid") or 0) == os.getpid():
            (home() / "service.json").unlink(missing_ok=True)
        lock.release()


# ---------------------------------------------------------------- события для сессии оркестратора

def relay_events(store: Store) -> int:
    """Монитор сессии: поднять службу и пересказывать события своего проекта в stdout.
    Сам ничего не решает. Если служба упала — поднимает снова; если её остановил
    пользователь (aiflow down) — сообщает и выходит."""
    events = store.runs / "events.jsonl"
    pos = events.stat().st_size if events.is_file() else 0
    owner = int(os.environ.get("CLAUDE_PID") or 0)
    register(store.root)
    other = _foreign_monitor(store)
    if other:  # монитор сессии старой версии держит проект — службе он больше не нужен
        aiflow._kill(other)
    try:
        data = ensure()
    except AiflowError as e:
        print(f"[aiflow] {e}", flush=True)
        return 1
    quiet = aiflow.notify_level(store.config()) == "none"
    if not quiet:
        url = panel_url(data)
        note = f"; панель: {url}" if url else (f"; панель недоступна: {data['panel_error']}" if data.get("panel_error") else "")
        print(f"[aiflow] монитор запущен{note}", flush=True)
    while True:
        time.sleep(2)
        if owner and not pid_alive(owner):
            return 0
        if not info():
            if stopped_by_user():
                if aiflow.notify_level(store.config()) != "none":
                    print("[aiflow] служба остановлена командой aiflow down; продолжить — aiflow up или /aiflow:resume",
                          flush=True)
                return 0
            time.sleep(3)  # служба могла как раз перезапускаться с новой версией
            if not info():
                try:
                    ensure()
                except AiflowError as e:
                    if aiflow.notify_level(store.config()) != "none":
                        print(f"[aiflow] монитор остановился: {e}", flush=True)
                    return 1
        if not events.is_file():
            continue
        size = events.stat().st_size
        if size < pos:
            pos = 0  # файл событий пересоздан
        if size <= pos:
            continue
        with events.open("rb") as f:
            f.seek(pos)
            chunk = f.read()
        end = chunk.rfind(b"\n") + 1
        pos += end
        cfg = store.config()
        for line in chunk[:end].decode("utf-8", "replace").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if aiflow.should_notify(cfg, ev.get("kind", "")):
                print(f"[aiflow] {ev.get('text', '')}", flush=True)


# ---------------------------------------------------------------- окно панели и автозапуск

def _browser() -> list[str] | None:
    import shutil
    if os.name == "nt":
        cands = [shutil.which("msedge"), shutil.which("chrome")]
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                cands += [str(Path(base) / "Microsoft/Edge/Application/msedge.exe"),
                          str(Path(base) / "Google/Chrome/Application/chrome.exe")]
        for c in cands:
            if c and Path(c).is_file():
                return [c]
        return None
    if sys.platform == "darwin":
        for app in ("Google Chrome", "Microsoft Edge", "Chromium"):
            if Path(f"/Applications/{app}.app").exists():
                return ["open", "-na", app, "--args"]
        return None
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        exe = shutil.which(name)
        if exe:
            return [exe]
    return None


def open_window(url: str) -> str:
    """Открыть панель отдельным окном (режим приложения Edge/Chrome), иначе — в браузере."""
    b = _browser()
    if b:
        try:
            kw = {"creationflags": subprocess.DETACHED_PROCESS} if os.name == "nt" else {"start_new_session": True}
            subprocess.Popen([*b, f"--app={url}", "--window-size=1320,900"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
            return "окно приложения"
        except OSError:
            pass
    import webbrowser
    webbrowser.open(url)
    return "браузер"


LAUNCHER = '''"""Автозапуск службы aiflow: самая новая версия плагина из того же маркетплейса."""
import os, subprocess, sys
from pathlib import Path

CACHE = Path({cache!r})
FALLBACK = {fallback!r}


def ver(p):
    try:
        return tuple(int(x) for x in p.parent.parent.name.split("."))
    except ValueError:
        return (0,)


found = sorted(CACHE.glob("*/scripts/aiflow.py"), key=ver)
script = str(found[-1]) if found else FALLBACK
if not Path(script).is_file():
    sys.exit(0)  # плагин удалён — автозапуск ничего не делает
home = Path(os.environ.get("AIFLOW_HOME") or Path.home() / ".aiflow")
home.mkdir(parents=True, exist_ok=True)
os.environ["PYTHONUTF8"] = "1"
os.environ["PATH"] = {path!r} + os.pathsep + os.environ.get("PATH", "")
with open(home / "service.log", "ab") as log:
    sys.exit(subprocess.call([sys.executable, "-X", "utf8", script, "service"], stdout=log, stderr=log))
'''

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _write_launcher() -> Path:
    script = Path(aiflow.__file__).resolve()
    # .../plugins/cache/<маркетплейс>/aiflow/<версия>/scripts/aiflow.py → .../cache/<маркетплейс>/aiflow
    cache = script.parent.parent.parent
    env = aiflow.claude_env()
    raw = next((v for k, v in env.items() if k.upper() == "PATH"), "")
    path = os.pathsep.join(x for x in raw.split(os.pathsep) if x)
    launcher = home() / "launcher.py"
    home().mkdir(parents=True, exist_ok=True)
    launcher.write_text(LAUNCHER.format(cache=str(cache), fallback=str(script), path=path), encoding="utf-8")
    return launcher


def autostart(enable: bool) -> str:
    launcher = _write_launcher() if enable else home() / "launcher.py"
    if os.name == "nt":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, "aiflow", 0, winreg.REG_SZ, f'"{_python(windowless=True)}" "{launcher}"')
            else:
                try:
                    winreg.DeleteValue(k, "aiflow")
                except FileNotFoundError:
                    pass
        return "HKCU\\" + RUN_KEY + " → aiflow"
    if sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents/dev.aiflow.service.plist"
        if enable:
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>dev.aiflow.service</string>
<key>ProgramArguments</key><array><string>{xml_escape(_python())}</string><string>{xml_escape(str(launcher))}</string></array>
<key>RunAtLoad</key><true/>
</dict></plist>
""", encoding="utf-8")
        else:
            plist.unlink(missing_ok=True)
        return str(plist)
    desktop = Path.home() / ".config/autostart/aiflow.desktop"
    if enable:
        def q(x: str) -> str:  # кавычки по спецификации Desktop Entry
            return '"' + x.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"'
        desktop.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_text(f"[Desktop Entry]\nType=Application\nName=aiflow\nExec={q(_python())} {q(str(launcher))}\n"
                           "X-GNOME-Autostart-enabled=true\nNoDisplay=true\n", encoding="utf-8")
    else:
        desktop.unlink(missing_ok=True)
    return str(desktop)


def autostart_enabled() -> bool:
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, "aiflow")
            return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return (Path.home() / "Library/LaunchAgents/dev.aiflow.service.plist").is_file()
    return (Path.home() / ".config/autostart/aiflow.desktop").is_file()
