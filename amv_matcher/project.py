"""Projects (.amvproj): a JSON file that remembers the video, songs, edit settings and auto-sync plan.

Media is referenced by path + size + mtime, never copied. Writes are atomic (tmp file + os.replace).
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import paths

APP_DATA = getattr(paths, "APP_DATA", paths.DATA)
EXT = ".amvproj"
VERSION = 1
MAX_RECENT = 10
MAX_WALK_FILES = 2000      # per root, for relink
MTIME_TOL_S = 2.0          # FAT/exFAT keep 2 s mtime granularity
_lock = threading.Lock()

DEFAULTS: dict = {
    "version": VERSION,
    "name": "Untitled",
    "created": None,
    "modified": None,
    "video": None,
    "music_dirs": [],
    "sources": [],
    "in_use": None,
    "offset": 0.0,
    "splice": {"on": False, "max_cuts": 0},
    "autosync": {"plan": None, "guardrails": {}, "lead_s": 0.06},
    "effects": {"boom": 0.0, "decay": 0.0, "drops_only": False, "shake": 0.0},
    "export": {"quality": "balanced"},
    "ui": {},
}


class ProjectError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_ext(path) -> Path:
    p = Path(str(path)).expanduser()
    if p.suffix.lower() != EXT:
        raise ProjectError(f"project paths must end with {EXT}")
    return p.resolve()


def _fill(data: dict, defaults: dict) -> dict:
    out = {}
    for k, dv in defaults.items():
        v = data.get(k, None) if isinstance(data, dict) else None
        if isinstance(dv, dict) and dv:
            out[k] = _fill(v if isinstance(v, dict) else {}, dv)
        elif v is None:
            out[k] = copy.deepcopy(dv)
        else:
            out[k] = v
    # keep unknown keys inside free-form dicts (guardrails, ui) and at top level (forward compat)
    if isinstance(data, dict):
        for k, v in data.items():
            if k not in out:
                out[k] = v
    return out


def normalize(data: dict) -> dict:
    """Fill defaults for missing keys; reject non-objects and newer versions."""
    if not isinstance(data, dict):
        raise ProjectError("project data must be an object")
    ver = data.get("version", VERSION)
    if not isinstance(ver, int) or ver < 1 or ver > VERSION:
        raise ProjectError(f"unsupported project version: {ver!r}")
    out = _fill(data, DEFAULTS)
    out["version"] = VERSION
    if not isinstance(out["sources"], list):
        out["sources"] = []
    if not isinstance(out["music_dirs"], list):
        out["music_dirs"] = []
    return out


def media_entry(path) -> dict:
    """{path,size,mtime} for an existing file (helper for callers building a project)."""
    p = Path(str(path))
    st = p.stat()
    return {"path": str(p), "size": st.st_size, "mtime": st.st_mtime}


def _is_missing(entry: dict) -> bool:
    try:
        st = os.stat(entry["path"])
    except (OSError, KeyError, TypeError):
        return True
    if not os.path.isfile(entry["path"]):
        return True
    size, mtime = entry.get("size"), entry.get("mtime")
    if size is not None and st.st_size != size:
        return True
    if mtime is not None and abs(st.st_mtime - float(mtime)) > MTIME_TOL_S:
        return True
    return False


def _media_entries(data: dict):
    if isinstance(data.get("video"), dict):
        yield data["video"]
    for s in data.get("sources") or []:
        if isinstance(s, dict):
            yield s


def mark_missing(data: dict) -> list:
    """Set missing:true on each media entry that is gone or changed (else drop the flag); return those entries."""
    missing = []
    for e in _media_entries(data):
        if _is_missing(e):
            e["missing"] = True
            missing.append(e)
        else:
            e.pop("missing", None)
    return missing


def save(path, data: dict) -> dict:
    """Write the project atomically; returns the saved (normalized) data."""
    p = _check_ext(path)
    d = normalize(data)
    now = _now()
    d["created"] = d["created"] or now
    d["modified"] = now
    for e in _media_entries(d):
        e.pop("missing", None)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return d


def load(path) -> dict:
    """Read + validate + fill defaults + flag missing media."""
    p = _check_ext(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ProjectError(f"not a valid project file: {e}") from e
    d = normalize(raw)
    mark_missing(d)
    return d


def relink(entry: dict, roots) -> list:
    """Candidate paths under `roots` with the same file name and size as `entry` (hidden dirs skipped)."""
    try:
        name = os.path.basename(str(entry["path"]).replace("\\", "/"))
        size = entry.get("size")
    except (KeyError, TypeError, AttributeError):
        return []
    found: list = []
    for root in roots or []:
        r = Path(str(root)).expanduser()
        if not r.is_dir():
            continue
        seen = 0
        for dirpath, dirnames, filenames in os.walk(r, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fn in filenames:
                seen += 1
                if seen > MAX_WALK_FILES:
                    break
                if fn != name:
                    continue
                fp = Path(dirpath) / fn
                try:
                    if size is None or fp.stat().st_size == size:
                        found.append(str(fp))
                except OSError:
                    pass
            if seen > MAX_WALK_FILES:
                break
    return found


# ---- recent projects -------------------------------------------------------------------------
def _recent_file() -> Path:
    return APP_DATA / "recent_projects.json"


def _read_recent() -> list:
    try:
        v = json.loads(_recent_file().read_text(encoding="utf-8"))
        return [x for x in v if isinstance(x, dict) and x.get("path")] if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def _atomic_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add_recent(path, name: str = "") -> None:
    p = str(_check_ext(path))
    with _lock:
        items = [x for x in _read_recent() if x["path"] != p]
        items.insert(0, {"path": p, "name": name or Path(p).stem, "opened": time.time()})
        _atomic_json(_recent_file(), items[:MAX_RECENT])


def recent() -> list:
    """Most recent first, max 10; each flagged missing:true when the project file is gone."""
    out = []
    for x in _read_recent()[:MAX_RECENT]:
        out.append({**x, "missing": not os.path.isfile(x["path"])})
    return out


# ---- autosave --------------------------------------------------------------------------------
def _safe_name(name: str) -> str:
    s = "".join(c if (c.isalnum() or c in " ._-") else "_" for c in str(name or "")).strip(" .")
    return s[:80] or "Untitled"


def autosave_path(name: str) -> Path:
    return APP_DATA / "autosave" / f"{_safe_name(name)}{EXT}"


def autosave(data: dict) -> Path:
    d = normalize(data)
    p = autosave_path(d["name"])
    save(p, d)
    return p
