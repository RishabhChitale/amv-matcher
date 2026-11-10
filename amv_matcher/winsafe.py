"""Windows-safe helpers (see docs/WINDOWS-AUDIT.md). Pure stdlib; behave like the plain call on POSIX."""
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000


def _is_win() -> bool:
    return os.name == "nt"


def _win_kw(kw: dict) -> dict:
    if _is_win():
        kw["creationflags"] = kw.get("creationflags", 0) | CREATE_NO_WINDOW
    return kw


def run(cmd, **kw):
    """subprocess.run without a console window on Windows (W-03)."""
    return subprocess.run(cmd, **_win_kw(kw))


def popen(cmd, **kw):
    """subprocess.Popen without a console window on Windows (W-03). On POSIX nothing is added."""
    return subprocess.Popen(cmd, **_win_kw(kw))


def replace_retry(src, dst, tries=10, delay=0.05):
    """os.replace that retries PermissionError (target briefly open on Windows) (W-06)."""
    for i in range(max(1, tries)):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(delay * (1 + i))


def norm_case_path(p) -> str:
    """Absolute, normalised string; lower-cased on Windows (W-04). Does not touch the filesystem."""
    return os.path.normcase(os.path.abspath(str(p)))


def is_relative_to_ci(child, parent) -> bool:
    """child is parent or inside it; case-insensitive on Windows, exact on POSIX (W-05)."""
    c, p = norm_case_path(child), norm_case_path(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def _inside(path, root) -> bool:
    try:
        return is_relative_to_ci(Path(path).resolve(), Path(root).resolve()) \
            and norm_case_path(Path(path).resolve()) != norm_case_path(Path(root).resolve())
    except (OSError, RuntimeError):
        return False


def safe_unlink(path, root=None, tries=10, delay=0.05) -> bool:
    """Delete a file, retrying PermissionError. If root is given, refuse paths outside it.
    Returns True when the file is gone, False when refused or still locked (W-09)."""
    p = Path(path)
    if root is not None and not _inside(p, root):
        return False
    for i in range(max(1, tries)):
        try:
            p.unlink(missing_ok=True)
            return True
        except PermissionError:
            if i < tries - 1:
                time.sleep(delay * (1 + i))
        except OSError:
            return False
    return False


def safe_rmtree(path, root, tries=10, delay=0.05) -> bool:
    """rmtree that refuses root itself and anything outside root, retrying PermissionError (W-09)."""
    p = Path(path)
    if not _inside(p, root):
        return False
    for i in range(max(1, tries)):
        try:
            shutil.rmtree(p)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if i < tries - 1:
                time.sleep(delay * (1 + i))
        except OSError:
            return False
    return False


def kill_tree(proc) -> None:
    """Kill proc and its descendants (W-08). Windows: taskkill /T /F. POSIX: killpg, which needs the child
    started with start_new_session=True; otherwise only the child is killed."""
    if proc.poll() is not None:
        return
    try:
        if _is_win():
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=CREATE_NO_WINDOW)
        else:
            try:
                pgid = os.getpgid(proc.pid)
                if pgid != os.getpgid(0):
                    os.killpg(pgid, signal.SIGKILL)
                    return
            except (ProcessLookupError, PermissionError):
                pass
            proc.kill()
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
