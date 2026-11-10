"""Tiny in-memory + on-disk JSON cache (one file per cache path)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

_REGISTRY: dict = {}
_reg_lock = threading.Lock()


class JsonCache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.data = None
        self.dirty = False

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
            self.dirty = True
            if flush:
                self._flush()

    def flush(self):
        with self.lock:
            self._flush()

    def _flush(self):
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)
        self.dirty = False


def get_cache(path) -> JsonCache:
    path = Path(path)
    with _reg_lock:
        if str(path) not in _REGISTRY:
            _REGISTRY[str(path)] = JsonCache(path)
        return _REGISTRY[str(path)]
