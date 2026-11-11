"""Identity memo for pure functions of feature dicts / lists (bit-identical speed-ups, F16).

Feature lists in song/video dicts are never mutated in place: every transform (tempo, retime, splice,
arrange, clip) builds a new dict with new lists. So a value derived from those lists stays valid while
the very same list objects are alive. The memo keys on their ids and holds a reference to each, so an
id cannot be reused by a new object while its entry exists. Cached arrays are read-only: a caller that
tried to modify one in place would raise instead of silently corrupting the cache.
"""
from __future__ import annotations

import threading
from collections import OrderedDict

import numpy as np


class IdMemo:
    def __init__(self, size: int = 64):
        self.size = size
        self._d = OrderedDict()
        self._lock = threading.Lock()

    def get(self, objs: tuple, extra: tuple = ()):
        key = tuple(id(o) for o in objs) + tuple(extra)
        with self._lock:
            hit = self._d.get(key)
            if hit is None or len(hit[0]) != len(objs) or any(a is not b for a, b in zip(hit[0], objs)):
                return key, None
            self._d.move_to_end(key)
            return key, hit[1]

    def put(self, key, objs: tuple, value):
        with self._lock:
            self._d[key] = (tuple(objs), value)
            self._d.move_to_end(key)
            while len(self._d) > self.size:
                self._d.popitem(last=False)
        return value

    def clear(self):
        with self._lock:
            self._d.clear()


def frozen(a: np.ndarray) -> np.ndarray:
    a.flags.writeable = False
    return a


_ARR = IdMemo(256)


def farr(x) -> np.ndarray:
    """np.asarray(x, dtype=float), memoised for non-empty lists / tuples (read-only result).
    ndarrays and other inputs go straight through np.asarray (never cached, never frozen)."""
    if not isinstance(x, (list, tuple)) or not x:
        return np.asarray(x, dtype=float)
    key, hit = _ARR.get((x,))
    if hit is not None:
        return hit
    return _ARR.put(key, (x,), frozen(np.asarray(x, dtype=float)))


# Exact low-overhead replicas of np.std / np.corrcoef(a, b)[0, 1] for 1-D float inputs (F16 pass 2). They
# run the very same ufunc reductions and the same dot as numpy, in the same order, so the results match
# numpy bit for bit (checked on random, rounded, constant and anti-correlated inputs); they only skip
# numpy's wrapper overhead, which dominated the auto-sync scan's thousands of short calls.

def pstd(a: np.ndarray) -> float:
    """np.std(a) for a non-empty 1-D array."""
    n = a.shape[0]
    d = a - np.true_divide(np.add.reduce(a, keepdims=True), n)
    np.multiply(d, d, out=d)
    return np.sqrt(np.true_divide(np.add.reduce(d), n))


def pearson(a, b) -> float:
    """np.corrcoef(a, b)[0, 1] for two 1-D arrays of the same length >= 2."""
    X = np.empty((2, len(a)))
    X[0] = a
    X[1] = b
    X -= X.mean(axis=1)[:, None]
    c = np.dot(X, X.T.conj())
    c *= np.true_divide(1, len(a) - 1)
    v = c[0, 1] / np.sqrt(c[0, 0]) / np.sqrt(c[1, 1])
    return min(max(v, -1.0), 1.0)
