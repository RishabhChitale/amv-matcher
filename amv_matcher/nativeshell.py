"""Pure helpers behind the desktop shell's native APIs (window state, notifications, Resolve launch).
No pywebview import here, so everything is unit-testable."""
from __future__ import annotations

import json
import os
import base64
import logging
import ntpath
import subprocess
import sys
import time
from pathlib import Path

from . import paths, winsafe

log = logging.getLogger(__name__)

MIN_W, MIN_H = 1100, 700
DEFAULT_GEOM = {"width": 1440, "height": 900}


def _state_file() -> Path:
    return paths.DATA / "window.json"


def load_geometry(screens=None) -> dict:
    """Saved window geometry {width,height[,x,y]}; falls back to the default size. If `screens` ([(x,y,w,h)...])
    is given and the saved position is not on any screen, the position is dropped (the OS centres the window)."""
    g = dict(DEFAULT_GEOM)
    try:
        d = json.loads(_state_file().read_text())
        w, h = int(d["width"]), int(d["height"])
        g["width"], g["height"] = max(MIN_W, min(w, 10000)), max(MIN_H, min(h, 10000))
        if "x" in d and "y" in d:
            x, y = int(d["x"]), int(d["y"])
            if screens is None or any(sx <= x + 40 < sx + sw and sy <= y + 20 < sy + sh for sx, sy, sw, sh in screens):
                g["x"], g["y"] = x, y
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return g


def save_geometry(x, y, width, height) -> bool:
    try:
        vals = [int(v) for v in (x, y, width, height)]
        paths.DATA.mkdir(parents=True, exist_ok=True)
        tmp = _state_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(zip(("x", "y", "width", "height"), vals))))
        winsafe.replace_retry(tmp, _state_file())
        return True
    except (OSError, ValueError, TypeError):
        return False


def applescript_str(s) -> str:
    """Quote a string as an AppleScript literal (backslash and double quote escaped, newlines flattened)."""
    s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")
    return '"' + s[:300] + '"'


def notify_cmd(title, body, platform=None):
    """argv that shows a native notification, or None when unsupported (Windows is best-effort: no-op)."""
    if (platform or sys.platform) == "darwin":
        return ["osascript", "-e", f"display notification {applescript_str(body)} with title {applescript_str(title)}"]
    return None


def _xml_esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;")


def toast_script(title, body) -> str:
    """PowerShell script showing a toast via Windows.UI.Notifications. Text is XML-escaped, then embedded in a
    single-quoted here-string-free literal (single quotes doubled), so no quoting can break out."""
    xml = ('<toast><visual><binding template="ToastGeneric"><text>%s</text><text>%s</text></binding></visual></toast>'
           % (_xml_esc(str(title)[:200]), _xml_esc(str(body)[:300])))
    lit = xml.replace("'", "''")
    return ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null\n"
            "$x = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
            "$x.LoadXml('%s')\n"
            "$t = [Windows.UI.Notifications.ToastNotification]::new($x)\n"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('AMV Matcher').Show($t)\n" % lit)


def powershell_cmd(title, body) -> list:
    enc = base64.b64encode(toast_script(title, body).encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", enc]


def _toast_winotify(title, body) -> bool:
    from winotify import Notification  # optional dependency (not in requirements)
    Notification(app_id="AMV Matcher", title=str(title), msg=str(body)).show()
    return True


def _toast_win10toast(title, body) -> bool:
    from win10toast import ToastNotifier  # optional dependency (not in requirements)
    ToastNotifier().show_toast(str(title), str(body), duration=5, threaded=True)
    return True


def _toast_powershell(title, body) -> bool:
    r = winsafe.run(powershell_cmd(title, body), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=10)
    return r.returncode == 0


_last_notify: dict = {}
RATE_S = 3.0


def notify(title, body="") -> bool:
    """Best-effort native notification; never raises. macOS: osascript. Windows: winotify, win10toast, then
    PowerShell toast. Other OSes: no-op (False). The same message within 3 s is ignored."""
    try:
        key = (str(title), str(body))
        now = time.monotonic()
        if now - _last_notify.get(key, -1e9) < RATE_S:
            return False
        if sys.platform == "darwin":
            ok = run(notify_cmd(title, body))
        elif sys.platform == "win32" or os.name == "nt":
            ok = False
            for fn in (_toast_winotify, _toast_win10toast, _toast_powershell):
                try:
                    if fn(title, body):
                        ok = True
                        break
                except Exception as e:  # ImportError or backend failure: try the next one
                    log.debug("toast backend %s failed: %s", fn.__name__, e)
        else:
            return False
        if ok:
            _last_notify[key] = now
        return bool(ok)
    except Exception:
        log.exception("notify failed")
        return False


def _program_files() -> list:
    """%ProgramFiles% (and %ProgramW6432%) with the stock location as the last resort (W-11)."""
    out = []
    for v in ("ProgramFiles", "ProgramW6432"):
        d = os.environ.get(v)
        if d and d not in out:
            out.append(d)
    return out or [r"C:\Program Files"]


def resolve_app(platform=None, exists=os.path.exists):
    """Path of the installed DaVinci Resolve, or None."""
    p = platform or sys.platform
    cands = {"darwin": ["/Applications/DaVinci Resolve/DaVinci Resolve.app"],
             "win32": [ntpath.join(pf, r"Blackmagic Design\DaVinci Resolve\Resolve.exe") for pf in _program_files()]}.get(p, [])
    return next((c for c in cands if exists(c)), None)


def resolve_cmd(src, platform=None, exists=os.path.exists):
    """argv that opens `src` in Resolve, or None if Resolve is absent."""
    app = resolve_app(platform, exists)
    if not app:
        return None
    return ["open", "-a", app, str(src)] if (platform or sys.platform) == "darwin" else [app, str(src)]


def project_name(name) -> str:
    """Safe default file name for a project (.amvproj)."""
    stem = "".join(c for c in str(name or "") if c.isalnum() or c in " -_.")[:80].strip(" .") or "Untitled"
    return stem if stem.endswith(".amvproj") else stem + ".amvproj"


MENU_ACTIONS = ("new", "open", "save", "save_as", "add_folder", "export", "undo", "redo", "delete",
                "toggle_media", "toggle_stems", "toggle_effects", "play_pause", "help")


def explorer_select_cmd(p) -> list:
    """Explorer argv that selects a file: ONE token '/select,<path>' (W-13)."""
    return ["explorer", "/select," + str(p)]


def normalize_drop_path(p, platform=None) -> str:
    """Native drop paths (pywebviewFullPath) -> clean OS path: forward slashes and a lowercase drive letter
    become backslashes / upper-case, a \\\\?\\ prefix is dropped (Windows only; POSIX untouched) (W-17)."""
    p = str(p)
    if (platform or sys.platform) != "win32":
        return p
    if p.startswith("\\\\?\\") and not p.startswith("\\\\?\\UNC"):
        p = p[4:]
    p = ntpath.normpath(p)
    if len(p) > 1 and p[1] == ":":
        p = p[0].upper() + p[1:]
    return p


def run(cmd) -> bool:
    if not cmd:
        return False
    try:
        winsafe.popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


class CloseGuard:
    """Non-blocking window-close handler.

    pywebview's Cocoa backend runs `closing` handlers on the main thread; evaluate_js from there
    waits on the main thread itself and deadlocks (Cmd-Q / AppleScript quit never finish). So the first
    close request is cancelled (returns False), the autosave hook runs on a worker thread with a
    timeout, then the window is destroyed (which does not re-enter the closing handler)."""

    def __init__(self, save_geom, run_js, destroy, grace=0.3, js_timeout=1.5):
        import threading
        self._threading = threading
        self.save_geom, self.run_js, self.destroy = save_geom, run_js, destroy
        self.grace, self.js_timeout = grace, js_timeout
        self.started = False
        self.done = False
        self.finished = threading.Event()

    def __call__(self):
        if self.done:
            return None
        if self.started:
            return False
        self.started = True
        self.save_geom()
        self._threading.Thread(target=self._finish, daemon=True, name="close-guard").start()
        return False

    def _finish(self):
        try:
            t = self._threading.Thread(target=self.run_js, daemon=True)
            t.start()
            t.join(self.js_timeout)
            time.sleep(self.grace)
        except Exception:
            log.exception("close guard")
        finally:
            self.done = True
            try:
                self.destroy()
            except Exception:
                log.exception("close guard destroy")
            self.finished.set()


def kill_children() -> None:
    """SIGKILL every descendant of this process (worker pool, resource tracker, ffmpeg/rife they started)."""
    me = os.getpid()
    if sys.platform.startswith("win"):
        try:
            subprocess.run(["taskkill", "/PID", str(me), "/T", "/F"], capture_output=True)
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=5).stdout
        kids: dict = {}
        for ln in out.splitlines():
            a = ln.split()
            if len(a) == 2:
                kids.setdefault(int(a[1]), []).append(int(a[0]))
        todo, seen = list(kids.get(me, [])), []
        while todo:
            p = todo.pop()
            seen.append(p)
            todo.extend(kids.get(p, []))
        import signal as _sg
        for p in seen:
            try:
                os.kill(p, _sg.SIGKILL)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        log.exception("kill_children")


class ShutdownGuard:
    """SIGTERM/SIGINT -> orderly quit. trigger() only starts a thread (safe from a signal handler / any thread):
    it asks for a normal close (CloseGuard: autosave off-thread, then window destroy -> Server.stop), and if the
    process is still alive after `fallback` seconds it kills the worker descendants and exits."""

    def __init__(self, request_close, kill_kids, exit_fn, fallback=5.0):
        self.request_close, self.kill_kids, self.exit_fn, self.fallback = request_close, kill_kids, exit_fn, fallback
        self._lock = __import__("threading").Lock()
        self.fired = False
        self.cancel = __import__("threading").Event()   # set when a normal shutdown completed
        self.finished = __import__("threading").Event()

    def trigger(self, *_):
        import threading
        with self._lock:
            if self.fired:
                return False
            self.fired = True
        threading.Thread(target=self._run, daemon=True, name="shutdown-guard").start()
        return True

    def _run(self):
        try:
            try:
                self.request_close()
            except Exception:  # noqa: BLE001
                log.exception("shutdown request_close")
            if not self.cancel.wait(self.fallback):
                log.warning("graceful shutdown timed out; killing workers")
                try:
                    self.kill_kids()
                except Exception:  # noqa: BLE001
                    log.exception("shutdown kill_kids")
                self.exit_fn(0)
        finally:
            self.finished.set()

    def install(self):
        """POSIX: a Python-level handler never runs while the main thread sits in the Cocoa loop, so the C-level
        handler writes the signal number to a wakeup pipe that a dedicated thread reads (no signal mask: spawned
        workers keep default signal behaviour). Windows: plain handlers that only start the guard thread."""
        import signal
        import threading
        sigs = [signal.SIGTERM, signal.SIGINT]
        if sys.platform.startswith("win"):
            for s in sigs + [getattr(signal, "SIGBREAK", signal.SIGINT)]:
                signal.signal(s, self.trigger)
            return
        r, w = os.pipe()
        os.set_blocking(w, False)
        for s in sigs:
            signal.signal(s, lambda *a: None)
        signal.set_wakeup_fd(w, warn_on_full_buffer=False)

        def waiter():
            while True:
                try:
                    if os.read(r, 16):
                        self.trigger()
                except OSError:
                    return
        threading.Thread(target=waiter, daemon=True, name="signal-waiter").start()
