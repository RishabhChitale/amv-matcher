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


def _win_pairs_toolhelp():
    """[[pid, ppid], ...] via a Toolhelp32 snapshot (ctypes, no subprocess, milliseconds). None if unavailable."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PE32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PE32)]
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PE32)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
        if not snap or snap == wintypes.HANDLE(-1).value:
            return None
        pairs = []
        try:
            pe = PE32()
            pe.dwSize = ctypes.sizeof(PE32)
            ok = k32.Process32FirstW(snap, ctypes.byref(pe))
            while ok:
                pairs.append([str(pe.th32ProcessID), str(pe.th32ParentProcessID)])
                ok = k32.Process32NextW(snap, ctypes.byref(pe))
        finally:
            k32.CloseHandle(snap)
        return pairs or None
    except Exception:  # noqa: BLE001
        return None


def _parent_map() -> dict:
    """{parent pid: [child pids]} for every process (psutil if present, else ps / PowerShell CIM)."""
    kids: dict = {}
    try:
        import psutil  # type: ignore
        for pr in psutil.process_iter(["pid", "ppid"]):
            kids.setdefault(pr.info["ppid"], []).append(pr.info["pid"])
        return kids
    except ImportError:
        pass
    if sys.platform.startswith("win"):
        pairs = _win_pairs_toolhelp()
        if pairs is None:   # ctypes snapshot unavailable -> PowerShell CIM (slow on busy runners: 60 s, one retry)
            pairs = []
            for attempt in (1, 2):
                try:
                    out = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }"],
                        capture_output=True, text=True, timeout=60).stdout
                    pairs = [ln.split() for ln in out.splitlines()]
                    break
                except subprocess.TimeoutExpired:
                    if attempt == 2:
                        raise RuntimeError("process list (PowerShell CIM) timed out twice (60 s each)")
    else:
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=5).stdout
        pairs = [ln.split() for ln in out.splitlines()]
    for a in pairs:
        if len(a) == 2 and a[0].isdigit() and a[1].isdigit():
            kids.setdefault(int(a[1]), []).append(int(a[0]))
    return kids


def descendants(root=None) -> list:
    """All descendant pids of root (default: this process), deepest first. Never includes root itself."""
    me = os.getpid() if root is None else root
    kids = _parent_map()
    order, todo = [], list(kids.get(me, []))
    while todo:
        p = todo.pop()
        if p == me or p in order:
            continue
        order.append(p)
        todo.extend(kids.get(p, []))
    return order[::-1]


def kill_children() -> None:
    """Hard-kill every descendant of this process (worker pool, resource tracker, ffmpeg/rife they started).
    Never touches our own pid."""
    me = os.getpid()
    try:
        pids = [p for p in descendants(me) if p != me]
        import signal as _sg
        for p in pids:
            try:
                if sys.platform.startswith("win"):
                    subprocess.run(["taskkill", "/PID", str(p), "/F"], capture_output=True, timeout=10)
                else:
                    os.kill(p, _sg.SIGKILL)
            except Exception:  # noqa: BLE001
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
        self.cancel_wait = __import__("threading").Event()   # set by a repeated signal: stop waiting, exit now

    def trigger(self, *_):
        """First call starts the guard; a second call (the user is impatient) skips the grace period."""
        import threading
        with self._lock:
            if self.fired:
                self.cancel_wait.set()
                return False
            self.fired = True
        threading.Thread(target=self._run, daemon=True, name="shutdown-guard").start()
        return True

    def _run(self):
        # request_close runs in ITS OWN thread: it can block for ever (evaluate_js waits on a wedged Cocoa main
        # thread), and the fallback clock must not depend on it returning.
        import threading
        try:
            def ask():
                try:
                    self.request_close()
                except Exception:  # noqa: BLE001
                    log.exception("shutdown request_close")
            threading.Thread(target=ask, daemon=True, name="shutdown-close").start()
            deadline = time.time() + self.fallback
            while not self.cancel.is_set() and not self.cancel_wait.is_set() and time.time() < deadline:
                self.cancel.wait(0.05)
            if not self.cancel.is_set():
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


# ---- worker-child guard + single instance ------------------------------------------------------------------------

def is_worker_child(argv=None) -> bool:
    """True inside a multiprocessing child / resource tracker: those must never build a window."""
    argv = sys.argv if argv is None else argv
    if any(a.startswith("--multiprocessing-fork") for a in argv):
        return True
    if "-c" in argv and any("multiprocessing" in a for a in argv):
        return True
    try:
        import multiprocessing
        return multiprocessing.parent_process() is not None
    except Exception:
        return False


class SingleInstance:
    """One app per data dir. The first instance holds an exclusive lock file and listens on a loopback socket; a
    second launch hands its args over (token-checked) so the first one focuses its window, then exits."""

    def __init__(self, data_dir, on_activate=None):
        self.dir = Path(data_dir)
        self.on_activate = on_activate
        self._lock = None
        self._srv = None
        self.token = ""

    @property
    def _lock_path(self) -> Path:
        return self.dir / "instance.lock"

    @property
    def _info_path(self) -> Path:
        return self.dir / "instance.json"

    def acquire(self) -> bool:
        self.dir.mkdir(parents=True, exist_ok=True)
        f = open(self._lock_path, "a+")
        try:
            if sys.platform.startswith("win"):
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._lock = f
        self._listen()
        return True

    def _listen(self) -> None:
        import secrets
        import socket
        import threading
        self.token = secrets.token_hex(16)
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        tmp = self._info_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "port": self._srv.getsockname()[1], "token": self.token}))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self._info_path)
        threading.Thread(target=self._serve, daemon=True, name="single-instance").start()

    def _serve(self) -> None:
        import hmac
        while True:
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            try:
                c.settimeout(2.0)
                data = b""
                while not data.endswith(b"\n") and len(data) < 65536:
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                msg = json.loads(data.decode("utf-8"))
                if hmac.compare_digest(str(msg.get("token", "")), self.token) and self.on_activate:
                    self.on_activate([str(a) for a in msg.get("args", [])])
            except Exception:
                log.exception("single-instance message")
            finally:
                c.close()

    @staticmethod
    def notify_existing(data_dir, args=()) -> bool:
        """Tell the running instance to focus (and open args). False if nobody answered."""
        import socket
        try:
            info = json.loads((Path(data_dir) / "instance.json").read_text())
            with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=2.0) as s:
                s.sendall((json.dumps({"token": info["token"], "args": list(args)}) + "\n").encode("utf-8"))
            return True
        except Exception:
            return False

    def release(self) -> None:
        try:
            if self._srv:
                self._srv.close()
            if self._lock:
                self._lock.close()   # closing drops the flock
            self._info_path.unlink()
        except OSError:
            pass
