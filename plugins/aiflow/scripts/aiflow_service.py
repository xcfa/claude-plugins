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

import aiflow
from aiflow import AiflowError, FileLock, Store, iso, pid_alive, read_json, write_json

DEFAULT_PORT = 8765


def home() -> Path:
    return Path(os.environ.get("AIFLOW_HOME") or Path.home() / ".aiflow")


def norm(root) -> str:
    return str(Path(root).resolve())


def project_key(root) -> str:
    return hashlib.sha1(os.path.normcase(norm(root)).encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------- реестр проектов

def _registry_path() -> Path:
    return home() / "projects.json"


def projects() -> list[str]:
    data = read_json(_registry_path(), {}) or {}
    out, seen = [], set()
    for e in data.get("projects") or []:
        root = e.get("root") if isinstance(e, dict) else None
        if root and project_key(root) not in seen:
            seen.add(project_key(root))
            out.append(norm(root))
    return out


def _save(roots: list[str]) -> None:
    home().mkdir(parents=True, exist_ok=True)
    old = {project_key(e["root"]): e for e in (read_json(_registry_path(), {}) or {}).get("projects") or []
           if isinstance(e, dict) and e.get("root")}
    write_json(_registry_path(), {"projects": [old.get(project_key(r)) or {"root": r, "added_at": iso()} for r in roots]})


def register(root) -> bool:
    """Добавить проект в реестр службы. True, если его там не было."""
    roots = projects()
    if any(project_key(r) == project_key(root) for r in roots):
        return False
    _save(roots + [norm(root)])
    return True


def forget(root) -> bool:
    roots = projects()
    rest = [r for r in roots if project_key(r) != project_key(root)]
    if len(rest) == len(roots):
        return False
    _save(rest)
    return True


def global_config() -> dict:
    cfg = {"ui_port": DEFAULT_PORT}
    cfg.update(read_json(home() / "config.json", {}) or {})
    return cfg


# ---------------------------------------------------------------- процесс службы

def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


def info() -> dict | None:
    """Работающая служба: {pid, version, port, script, started_at} или None."""
    pid = FileLock(home() / "service.lock").owner()
    if not pid:
        return None
    data = read_json(home() / "service.json", {}) or {}
    if int(data.get("pid") or 0) != pid:
        data = {"pid": pid}
    return data


def panel_url(data: dict | None = None) -> str:
    data = data or info() or {}
    port = data.get("port") if data else None
    if port == 0:
        return ""
    return f"http://127.0.0.1:{port or global_config().get('ui_port') or DEFAULT_PORT}/"


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
    log = (home() / "service.log").open("ab")
    env = aiflow.claude_env()
    env["PYTHONUTF8"] = "1"
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
    log.close()


def stop() -> bool:
    data = info()
    if not data:
        return False
    ok = aiflow._kill(int(data["pid"]))
    if ok:
        (home() / "service.json").unlink(missing_ok=True)
        (home() / "service.lock").unlink(missing_ok=True)
    return ok


def ensure(wait: float = 15) -> dict:
    """Служба работает и не старше этой версии плагина: иначе (пере)запустить."""
    data = info()
    if data and _version_tuple(data.get("version", "0")) < _version_tuple(aiflow.VERSION):
        stop()
        data = None
    if not data:
        spawn()
        deadline = time.time() + wait
        while time.time() < deadline:
            data = info()
            if data and data.get("version"):
                break
            time.sleep(0.2)
        else:
            raise AiflowError(f"служба aiflow не запустилась, см. {home() / 'service.log'}")
    return data


def serve() -> int:
    """Цикл службы: проходы по всем проектам реестра и общая панель."""
    home().mkdir(parents=True, exist_ok=True)
    lock = FileLock(home() / "service.lock", stale_minutes=0)
    if not lock.acquire():
        return 0  # служба уже работает
    import signal

    def _stop(*_):
        raise KeyboardInterrupt
    for sig in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _stop)
    wake = threading.Event()
    held: dict[str, FileLock] = {}
    next_at: dict[str, float] = {}
    srv = None
    port = int(global_config().get("ui_port") or 0)
    try:
        if port:
            from aiflow_ui import start_server
            try:
                srv, _ = start_server(lambda: [Store(Path(r)) for r in projects()], port, wake, service=True)
            except OSError as e:
                print(f"{iso()} панель не поднялась на порту {port}: {e}", flush=True)
                port = 0
        write_json(home() / "service.json", {"pid": os.getpid(), "version": aiflow.VERSION, "port": port,
                                            "script": str(Path(aiflow.__file__).resolve()), "started_at": iso()})
        print(f"{iso()} служба aiflow {aiflow.VERSION} запущена, панель: {panel_url() or 'нет'}", flush=True)
        while True:
            forced = wake.is_set()
            wake.clear()
            roots = projects()
            keys = {project_key(r) for r in roots}
            for k in [k for k in held if k not in keys]:
                held.pop(k).release()
            many = len(roots) > 1
            for root in roots:
                k = project_key(root)
                store = Store(Path(root))
                if not store.initialized:
                    continue
                if k not in held:
                    lk = FileLock(store.runs / "monitor.lock", stale_minutes=0)
                    if not lk.acquire():
                        continue  # проект ведёт монитор сессии старой версии
                    held[k] = lk
                if not forced and time.time() < next_at.get(k, 0):
                    continue
                events: list[dict] = []
                try:
                    events = aiflow.run_tick_now(store)
                except AiflowError:
                    pass  # проход уже идёт (aiflow tick вручную) — в следующий раз
                except Exception as e:  # служба не должна падать из-за одного проекта
                    store.event("error", None, f"сбой тика: {e!r}")
                cfg = store.config()
                aiflow.desktop_notify(cfg, events, project=store.root.name if many else None)
                next_at[k] = time.time() + max(10, int(cfg.get("tick_seconds", 45)))
            wake.wait(timeout=5)
    except KeyboardInterrupt:
        return 0
    finally:
        for lk in held.values():
            lk.release()
        if srv:
            srv.shutdown()
        data = read_json(home() / "service.json", {}) or {}
        if int(data.get("pid") or 0) == os.getpid():
            (home() / "service.json").unlink(missing_ok=True)
        lock.release()


# ---------------------------------------------------------------- события для сессии оркестратора

def relay_events(store: Store) -> int:
    """Монитор сессии: поднять службу и пересказывать события своего проекта в stdout.
    Пока служба жива, сам ничего не решает; если она упала — поднимает снова."""
    events = store.runs / "events.jsonl"
    pos = events.stat().st_size if events.is_file() else 0
    owner = int(os.environ.get("CLAUDE_PID") or 0)
    register(store.root)
    try:
        data = ensure()
    except AiflowError as e:
        print(f"[aiflow] {e}", flush=True)
        return 1
    cfg = store.config()
    if aiflow.notify_level(cfg) != "none":
        url = panel_url(data)
        print(f"[aiflow] монитор запущен{'; панель: ' + url if url else ''}", flush=True)
    while True:
        time.sleep(2)
        if owner and not pid_alive(owner):
            return 0
        if not info():
            try:
                ensure()
            except AiflowError as e:
                if aiflow.notify_level(store.config()) != "none":
                    print(f"[aiflow] монитор остановился: {e}", flush=True)
                return 1
        if not events.is_file() or events.stat().st_size <= pos:
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


LAUNCHER = '''"""Автозапуск службы aiflow: берёт самую новую установленную версию плагина."""
import os, re, subprocess, sys
from pathlib import Path

def ver(p):
    try:
        return tuple(int(x) for x in p.parent.parent.name.split("."))
    except ValueError:
        return (0,)

found = sorted(Path.home().glob(".claude/plugins/cache/*/aiflow/*/scripts/aiflow.py"), key=ver)
script = str(found[-1]) if found else {fallback!r}
os.environ["PYTHONUTF8"] = "1"
sys.exit(subprocess.call([sys.executable, "-X", "utf8", script, "service"]))
'''

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart(enable: bool) -> str:
    launcher = home() / "launcher.py"
    if enable:
        home().mkdir(parents=True, exist_ok=True)
        launcher.write_text(LAUNCHER.format(fallback=str(Path(aiflow.__file__).resolve())), encoding="utf-8")
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
<key>ProgramArguments</key><array><string>{_python()}</string><string>{launcher}</string></array>
<key>RunAtLoad</key><true/>
</dict></plist>
""", encoding="utf-8")
        else:
            plist.unlink(missing_ok=True)
        return str(plist)
    desktop = Path.home() / ".config/autostart/aiflow.desktop"
    if enable:
        desktop.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_text(f"[Desktop Entry]\nType=Application\nName=aiflow\nExec={_python()} {launcher}\n"
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
