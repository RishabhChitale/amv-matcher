"""Locate the user's real media for the gated real-media tests (never by file name).

Order: env AMV_REAL_VIDEO_A / AMV_REAL_SONG_S1, else the (untracked) real_cases.json
(env AMV_REAL_CASES, default <project>/real_cases.json): case 'A' video and its first song file.
Returns None when not configured, so the tests skip.
"""
import json
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _cases():
    p = Path(os.environ.get("AMV_REAL_CASES") or _ROOT / "real_cases.json")
    try:
        return json.loads(p.read_text()).get("cases", [])
    except Exception:
        return []


def _ok(p):
    return str(p) if p and Path(p).is_file() else None


def video_a():
    v = _ok(os.environ.get("AMV_REAL_VIDEO_A"))
    if v:
        return v
    return next((_ok(c.get("video")) for c in _cases() if c.get("id") == "A" and _ok(c.get("video"))), None)


def song_s1():
    s = _ok(os.environ.get("AMV_REAL_SONG_S1"))
    if s:
        return s
    for c in _cases():
        if c.get("id") == "A":
            for t in c.get("songs", []):
                if not t.startswith("@") and _ok(t):
                    return _ok(t)
    return None
