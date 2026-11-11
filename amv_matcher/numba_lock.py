"""Cross-process lock around the first (cold) numba compile.

Several processes JIT-compiling the same ``cache=True`` kernels (librosa's beat tracker) into one EMPTY numba cache
directory at the same time can SIGSEGV inside numba's gufunc call (seen on macOS with a fresh data dir: the pre-warm
pool and the first song analyses all start cold together).  Serialising the first compile fixes it: the first process
compiles and writes the cache, the others wait, then load it.

Never blocks analysis: if the lock cannot be taken (no dir, no permission, timeout) we proceed unlocked.  flock /
msvcrt locks are released by the OS when the holder dies, so a killed process cannot leave a stale lock; the timeout
is a second safety net."""
from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import time

log = logging.getLogger("amv.numba_lock")
TIMEOUT_S = 120.0


def _lock_path() -> str:
    d = os.environ.get("NUMBA_CACHE_DIR") or tempfile.gettempdir()
    return os.path.join(d, ".warm.lock")


def _try_lock(f) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(f) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.contextmanager
def numba_warm_lock(timeout: float = TIMEOUT_S):
    f = None
    got = False
    try:
        try:
            p = _lock_path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            f = open(p, "a+b")
            deadline = time.monotonic() + timeout
            while True:
                got = _try_lock(f)
                if got or time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            if not got:
                log.warning("numba warm lock not acquired in %.0fs; compiling unlocked", timeout)
        except Exception as e:  # noqa: BLE001 - locking must never block analysis
            log.warning("numba warm lock unavailable (%s); compiling unlocked", e)
        yield got
    finally:
        if f is not None:
            if got:
                _unlock(f)
            try:
                f.close()
            except OSError:
                pass
