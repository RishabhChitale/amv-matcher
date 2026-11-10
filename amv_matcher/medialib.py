"""Media library in place: persisted roots, safe directory listing, probe + thumbnail caches.

Nothing here copies or analyses media. Paths are validated with resolve() + is_relative_to, so `..`
and symlinks that leave a root are rejected.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import paths
from .winsafe import is_relative_to_ci, norm_case_path, replace_retry

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}

# Looked up at call time so tests can monkeypatch them.
APP_DATA = paths.DATA
CACHE = paths.CACHE

_lock = threading.Lock()
ALLOWED: list = []          # fallback registry used when server.allow_path does not exist


def allow_path(path) -> None:
    """Fallback allow-list hook (server.allow_path is used instead when it exists)."""
    p = str(path)
    if p not in ALLOWED:
        ALLOWED.append(p)


def kind_of(p: Path) -> Optional[str]:
    ext = p.suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return None


# ---------------------------------------------------------------- roots
def _roots_file() -> Path:
    return Path(APP_DATA) / "media_roots.json"


def default_roots() -> list:
    home = Path.home()
    names = ["Videos", "Music", "Downloads", "Desktop"] if sys.platform.startswith("win") \
        else ["Movies", "Music", "Downloads", "Desktop"]
    return [str(home / n) for n in names if (home / n).is_dir()]


def load_roots() -> list:
    f = _roots_file()
    try:
        data = json.loads(f.read_text())
        if isinstance(data, list):
            return [str(x) for x in data]
    except (OSError, ValueError):
        pass
    return default_roots()


def _save_roots(roots: list) -> None:
    f = _roots_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(roots, indent=2))
    replace_retry(tmp, f)


def roots_info() -> list:
    return [{"path": r, "name": Path(r).name or r, "exists": Path(r).is_dir()} for r in load_roots()]


def add_root(path: str) -> str:
    p = Path(str(path)).expanduser().resolve()
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    with _lock:
        roots = load_roots()
        if norm_case_path(p) not in {norm_case_path(r) for r in roots}:
            roots.append(str(p))
        _save_roots(roots)
    return str(p)


def remove_root(path: str) -> bool:
    key = str(path)
    try:
        key2 = str(Path(key).expanduser().resolve())
    except (OSError, RuntimeError):
        key2 = key
    with _lock:
        roots = load_roots()
        drop = {norm_case_path(key), norm_case_path(key2)}
        new = [r for r in roots if norm_case_path(r) not in drop]
        if len(new) == len(roots):
            return False
        _save_roots(new)
    return True


def _resolved_roots() -> list:
    out = []
    for r in load_roots():
        try:
            rp = Path(r).expanduser().resolve()
            if rp.is_dir():
                out.append(rp)
        except (OSError, RuntimeError):
            pass
    return out


def under_root(p: Path) -> bool:
    """p must already be resolved."""
    return any(is_relative_to_ci(p, r) for r in _resolved_roots())


def safe_path(path: str) -> Path:
    """Resolved Path under a root, else PermissionError (symlinks resolved first); ValueError if malformed."""
    try:
        p = Path(str(path)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        raise ValueError("bad path")
    if not under_root(p):
        raise PermissionError("path is outside the media roots")
    return p


# ---------------------------------------------------------------- listing
def list_dir(path: str) -> dict:
    p = safe_path(path)
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    folders, files = [], []
    roots = _resolved_roots()   # once per listing (was re-read from disk for every entry)
    for e in p.iterdir():
        if e.name.startswith("."):
            continue
        try:
            rp = e.resolve()
            if not any(is_relative_to_ci(rp, r) for r in roots):       # a symlink pointing out of every root is not listed
                continue
            if e.is_dir():
                folders.append({"name": e.name, "path": str(e)})
            elif e.is_file():
                k = kind_of(e)
                if k:
                    st = e.stat()
                    files.append({"name": e.name, "path": str(e), "size": st.st_size, "mtime": st.st_mtime,
                                  "kind": k, "ext": e.suffix.lower()})
        except OSError:
            continue
    folders.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda d: d["name"].lower())
    parent = str(p.parent) if p.parent != p and under_root(p.parent) else None
    return {"path": str(p), "parent": parent, "folders": folders, "files": files}


# ---------------------------------------------------------------- probe
def _key(p: Path, *extra) -> str:
    st = p.stat()
    return "|".join([norm_case_path(p), str(st.st_size), str(int(st.st_mtime)), *[str(x) for x in extra]])


def _do_probe(p: Path) -> dict:
    import av
    out = {"duration": None, "width": None, "height": None, "fps": None,
           "vcodec": None, "acodec": None, "has_audio": False}
    with av.open(str(p)) as c:
        if c.duration:
            out["duration"] = c.duration / av.time_base
        v = next((s for s in c.streams if s.type == "video"), None)
        a = next((s for s in c.streams if s.type == "audio"), None)
        if v is not None:
            out["width"], out["height"] = v.codec_context.width, v.codec_context.height
            out["vcodec"] = v.codec_context.name
            r = v.average_rate or v.guessed_rate
            out["fps"] = float(r) if r else None
            if out["duration"] is None and v.duration and v.time_base:
                out["duration"] = float(v.duration * v.time_base)
        if a is not None:
            out["acodec"] = a.codec_context.name
            out["has_audio"] = True
            if out["duration"] is None and a.duration and a.time_base:
                out["duration"] = float(a.duration * a.time_base)
    return out


_PROBE_MEM: dict = {}      # cache-file path -> {key: result}; loaded once, flushed at most once a second
_PROBE_FLUSH: dict = {}    # cache-file path -> time of the last write
_PROBE_FLUSH_EVERY = 1.0


def _probe_cache(cf: Path) -> dict:
    k = str(cf)
    if k not in _PROBE_MEM:
        try:
            _PROBE_MEM[k] = json.loads(cf.read_text())
        except (OSError, ValueError):
            _PROBE_MEM[k] = {}
    return _PROBE_MEM[k]


def _probe_flush(cf: Path, force: bool = False) -> None:
    k = str(cf)
    now = time.monotonic()
    if not force and now - _PROBE_FLUSH.get(k, 0.0) < _PROBE_FLUSH_EVERY:
        return
    _PROBE_FLUSH[k] = now
    cf.parent.mkdir(parents=True, exist_ok=True)
    tmp = cf.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_PROBE_MEM[k]))
    replace_retry(tmp, cf)


def probe(path: str) -> dict:
    p = safe_path(path)
    if not p.is_file() or kind_of(p) is None:
        raise FileNotFoundError(str(p))
    key = _key(p)
    cf = Path(CACHE) / "probe.json"
    with _lock:
        hit = _probe_cache(cf).get(key)
    if hit is not None:
        return hit
    res = _do_probe(p)
    with _lock:
        _probe_cache(cf)[key] = res
        _probe_flush(cf)   # throttled: not a whole-file rewrite per probe
    return res


# ---------------------------------------------------------------- thumbnails
_THUMB_SLOTS = threading.BoundedSemaphore(2)


def thumb(path: str, t: Optional[float] = None, w: int = 240) -> Path:
    """Path of a cached JPEG frame at t seconds (default 10% of the duration), scaled to width w."""
    import av
    import cv2
    p = safe_path(path)
    if not p.is_file() or kind_of(p) != "video":
        raise FileNotFoundError(str(p))
    w = max(16, min(int(w), 1920))
    if t is None:
        d = probe(str(p)).get("duration") or 0.0
        t = round(d * 0.1, 3)
    t = max(0.0, float(t))
    name = hashlib.sha1(_key(p, t, w).encode()).hexdigest() + ".jpg"
    out = Path(CACHE) / "thumbs" / name
    if out.is_file():
        return out
    frame = None
    with _THUMB_SLOTS:   # at most 2 decodes at once, so a folder of lazy thumbnails can't starve the UI
        if out.is_file():
            return out
        with av.open(str(p)) as c:
            s = c.streams.video[0]
            c.seek(int(t / float(s.time_base)), stream=s, backward=True, any_frame=False)
            for f in c.decode(s):
                frame = f
                if f.time is not None and f.time >= t:
                    break
    if frame is None:
        raise RuntimeError("no frame")
    h = max(2, int(round(frame.height * w / frame.width / 2)) * 2)
    img = frame.reformat(width=w, height=h, format="bgr24").to_ndarray()
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(buf.tobytes())
    replace_retry(tmp, out)
    return out
