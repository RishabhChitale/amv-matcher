"""Tiny in-memory + on-disk JSON cache (one file per cache path)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_REGISTRY: dict = {}
_reg_lock = threading.Lock()


class _FileLock:
    """Advisory cross-process lock on a sidecar file (fcntl on POSIX, msvcrt on Windows); best effort: if locking is
    unavailable the flush still proceeds (unique temp file + atomic rename keep the file valid)."""
    def __init__(self, path):
        self.path, self.fh = path, None

    def __enter__(self):
        try:
            self.fh = open(self.path, "a+b")
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                for _ in range(200):
                    try:
                        msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.01)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                self.fh.close()      # closing releases the flock
            except Exception:
                pass


STALE_TMP_S = 60


def _remove_stale_tmps(path: Path, max_age_s: float = STALE_TMP_S):
    """Delete `<stem>.<pid>.<tid>.tmp` files left beside a cache by a killed process. Called while holding the
    cache's file lock (live writers write and rename their temp inside it), and only for files older than
    `max_age_s`, so an in-flight write is never touched. Best effort."""
    now = time.time()
    try:
        for f in path.parent.glob(f"{path.stem}.*.tmp"):
            try:
                if now - f.stat().st_mtime > max_age_s:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


class JsonCache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.data = None
        self.dirty = False
        self._pending: dict = {}      # keys set since the last flush (merged over the on-disk copy)
        self._swept = False           # stale temp files are swept once per process, at the first flush

    def _load(self):
        if self.data is None:
            try:
                self.data = json.loads(self.path.read_text())
            except Exception:
                self.data = {}

    def get(self, key):
        with self.lock:
            self._load()
            return self.data.get(key)

    def set(self, key, value, flush=True):
        with self.lock:
            self._load()
            self.data[key] = value
            self._pending[key] = value
            self.dirty = True
            if flush:
                self._flush()

    def flush(self):
        with self.lock:
            self._flush()

    def _flush(self):
        """Write to disk. Several worker processes share one cache file, so: (1) each writer uses its own temp
        file (a shared one made one process's rename fail with FileNotFoundError and lost the other's entry) and
        (2) our new keys are merged over what is on disk instead of overwriting the file with our older snapshot."""
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.stem}.{os.getpid()}.{threading.get_ident()}.tmp")
        with _FileLock(self.path.with_name(self.path.stem + ".lock")):
            if not self._swept:
                self._swept = True
                _remove_stale_tmps(self.path)
            try:
                disk = json.loads(self.path.read_text())
                merged = disk if isinstance(disk, dict) else dict(self.data)
            except Exception:
                merged = dict(self.data)
            merged.update(self._pending)
            try:
                tmp.write_text(json.dumps(merged))
                for attempt in range(5):     # Windows: the target can be briefly open in another process
                    try:
                        tmp.replace(self.path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.02 * (attempt + 1))
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        self.data, self._pending, self.dirty = merged, {}, False


def get_cache(path) -> JsonCache:
    path = Path(path)
    with _reg_lock:
        if str(path) not in _REGISTRY:
            _REGISTRY[str(path)] = JsonCache(path)
        return _REGISTRY[str(path)]
