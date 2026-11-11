"""F6d timeline logs: user bookmarks + the app's detected events for one video/song pair, as JSON (+ CSV).

Format spec: docs/TIMELINE-LOG-FORMAT.md. Media are identified by duration, size and a sampled content
hash, never by file name. Bookmarks live in the project (`bookmarks`, `perfect_sync_example`).
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import math
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

FORMAT = "amv-timeline-log"
FORMAT_VERSION = 1
TRACKS = ("video", "audio")
BOOKMARK_TYPES = ("drop", "initial_boom", "encore", "visual_boom", "perfect_sync_moment", "other")
AUDIO_CLASSES = ("drop", "initial_boom", "encore", "boom")     # detected audio boom classes
HASH_ALGO = "sha256-sample-3x1MiB"
HASH_CHUNK = 1 << 20
NOTE_MAX = 2000
MAX_BOOKMARKS = 5000
CSV_FIELDS = ("track", "source", "type", "t", "timeline_t", "mmss", "conf", "kind", "end", "id", "note")


class LogError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_VERSION = None


def app_version() -> str:
    """AMV_APP_VERSION, else `git describe` of the source tree, else 'unknown' (cached)."""
    global _VERSION
    if _VERSION is None:
        v = os.environ.get("AMV_APP_VERSION", "").strip()
        if not v:
            try:
                root = Path(__file__).resolve().parent.parent
                v = subprocess.run(["git", "-C", str(root), "describe", "--tags", "--always", "--dirty"],
                                   capture_output=True, text=True, timeout=3).stdout.strip()
            except Exception:
                v = ""
        _VERSION = v or "unknown"
    return _VERSION


def mmss(t) -> str:
    t = float(t)
    sign = "-" if t < 0 else ""
    t = abs(t)
    m = int(t // 60)
    return f"{sign}{m}:{t - 60 * m:06.3f}"


# ---------------------------------------------------------------- media identity
def content_hash(path) -> str:
    """sha256 of the size plus three 1 MiB samples (start, middle, end): fast on multi-GB videos."""
    size = os.path.getsize(path)
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as f:
        for off in sorted({0, max(0, size // 2 - HASH_CHUNK // 2), max(0, size - HASH_CHUNK)}):
            f.seek(off)
            h.update(f.read(HASH_CHUNK))
    return h.hexdigest()


def media_identity(path=None, analysis=None) -> dict:
    """{duration_s, size, hash, hash_algo[, fps, width, height]}; never a name or path."""
    a = analysis or {}
    out = {"duration_s": _r(a.get("duration")), "size": None, "hash": None, "hash_algo": HASH_ALGO}
    if path:
        try:
            out["size"] = os.path.getsize(path)
            out["hash"] = content_hash(path)
        except OSError:
            pass
    for k in ("fps", "width", "height"):
        if a.get(k) is not None:
            out[k] = a[k]
    return out


def _r(x, n=3):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, n) if math.isfinite(x) else None


# ---------------------------------------------------------------- bookmarks
def new_bookmark(track, t, type="other", note="", id=None, created=None) -> dict:
    """A validated bookmark. `t` is media time of that track (video time / song time), seconds."""
    if track not in TRACKS:
        raise LogError(f"track must be one of {TRACKS}")
    if type not in BOOKMARK_TYPES:
        raise LogError(f"type must be one of {BOOKMARK_TYPES}")
    tt = _r(t)
    if tt is None or tt < 0:
        raise LogError("t must be a finite number >= 0")
    note = "" if note is None else str(note)
    if len(note) > NOTE_MAX:
        raise LogError(f"note longer than {NOTE_MAX} characters")
    now = _now()
    return {"id": str(id) if id else uuid.uuid4().hex[:12], "track": track, "t": tt, "type": type,
            "note": note, "created": created or now, "modified": now}


def _bm_list(data: dict) -> list:
    bms = data.get("bookmarks")
    if not isinstance(bms, list):
        bms = []
        data["bookmarks"] = bms
    return bms


def list_bookmarks(data: dict, track=None) -> list:
    bms = [b for b in _bm_list(data) if isinstance(b, dict)]
    if track:
        bms = [b for b in bms if b.get("track") == track]
    return sorted(bms, key=lambda b: (b.get("track", ""), float(b.get("t") or 0.0), b.get("id", "")))


def add_bookmark(data: dict, track, t, type="other", note="") -> dict:
    bms = _bm_list(data)
    if len(bms) >= MAX_BOOKMARKS:
        raise LogError(f"at most {MAX_BOOKMARKS} bookmarks per project")
    b = new_bookmark(track, t, type, note)
    while any(x.get("id") == b["id"] for x in bms):
        b["id"] = uuid.uuid4().hex[:12]
    bms.append(b)
    return b


def update_bookmark(data: dict, bid, **fields) -> dict:
    for i, b in enumerate(_bm_list(data)):
        if isinstance(b, dict) and b.get("id") == bid:
            merged = {**b, **{k: v for k, v in fields.items() if k in ("track", "t", "type", "note") and v is not None}}
            nb = new_bookmark(merged["track"], merged["t"], merged.get("type", "other"), merged.get("note", ""),
                              id=bid, created=b.get("created"))
            data["bookmarks"][i] = nb
            return nb
    raise KeyError(bid)


def delete_bookmark(data: dict, bid) -> bool:
    bms = _bm_list(data)
    n = len(bms)
    data["bookmarks"] = [b for b in bms if not (isinstance(b, dict) and b.get("id") == bid)]
    return len(data["bookmarks"]) < n


# ---------------------------------------------------------------- detected events
def detected_video(video: dict | None) -> dict | None:
    if not video:
        return None
    cam = []
    for e in video.get("camera_events") or []:
        cam.append({"t": _r(0.5 * (float(e["start"]) + float(e["end"]))), "start": _r(e.get("start")),
                    "end": _r(e.get("end")), "kind": e.get("kind"), "speed": _r(e.get("speed")),
                    "peak": _r(e.get("peak"))})
    return {
        "cuts": [_r(t) for t in video.get("cut_times") or []],
        "visual_booms": [{"t": _r(b.get("t")), "kind": b.get("kind"), "conf": _r(b.get("conf"))}
                         for b in video.get("visual_booms") or []],
        "camera_peaks": cam,
    }


def _audio_class(b: dict) -> str:
    k = b.get("class") or b.get("kind") or "boom"
    return k if k in AUDIO_CLASSES else "boom"


def detected_audio(song: dict | None) -> dict | None:
    if not song:
        return None
    bt = [float(t) for t in song.get("beat_times") or []]
    iv = sorted(b - a for a, b in zip(bt, bt[1:]))
    main = song.get("main_boom") or {}
    return {
        "booms": [{"t": _r(b.get("t")), "class": _audio_class(b), "conf": _r(b.get("conf")),
                   "build": b.get("build"), "main": bool(main) and _r(b.get("t")) == _r(main.get("t"))}
                  for b in song.get("booms") or []],
        "main_boom": _r(main.get("t")) if main else None,
        "energy_drops": [_r(t) for t in song.get("drops") or []],
        "beats": {"bpm": _r(song.get("bpm"), 2), "count": len(bt), "first": _r(bt[0]) if bt else None,
                  "last": _r(bt[-1]) if bt else None,
                  "median_interval": _r(iv[len(iv) // 2], 4) if iv else None},
    }


# ---------------------------------------------------------------- build / validate / parse
def build(project: dict, video=None, song=None, video_path=None, song_path=None, audio_role=None,
          version=None, now=None) -> dict:
    """The log for a project. video/song: cached analyses (None = not analysed: detected is null)."""
    offset = float(project.get("offset") or 0.0)
    bms = list_bookmarks(project)

    def track_bms(track):
        out = []
        for b in bms:
            if b.get("track") != track:
                continue
            x = {k: b.get(k) for k in ("id", "t", "type", "note", "created", "modified")}
            x["timeline_t"] = _r(float(b["t"]) - offset) if track == "audio" else x["t"]
            out.append(x)
        return out

    return {
        "format": FORMAT, "format_version": FORMAT_VERSION,
        "app": {"name": "AMV Matcher", "version": version or app_version()},
        "exported_at": now or _now(),
        "project": {"perfect_sync_example": bool(project.get("perfect_sync_example")), "offset": _r(offset),
                    "lead_s": _r((project.get("autosync") or {}).get("lead_s"))},
        "video": {"media": media_identity(video_path, video), "bookmarks": track_bms("video"),
                  "detected": detected_video(video)},
        "audio": {"media": media_identity(song_path, song), "role": audio_role, "bookmarks": track_bms("audio"),
                  "detected": detected_audio(song)},
    }


def validate(log) -> list:
    """List of problems (empty = valid)."""
    errs = []
    if not isinstance(log, dict):
        return ["log must be a JSON object"]
    if log.get("format") != FORMAT:
        errs.append(f"format must be {FORMAT!r}")
    fv = log.get("format_version")
    if not isinstance(fv, int) or fv < 1 or fv > FORMAT_VERSION:
        errs.append(f"unsupported format_version {fv!r}")
    for tr in TRACKS:
        sec = log.get(tr)
        if not isinstance(sec, dict):
            errs.append(f"{tr}: missing section")
            continue
        if not isinstance(sec.get("media", {}), dict):
            errs.append(f"{tr}.media must be an object")
        bms = sec.get("bookmarks", [])
        if not isinstance(bms, list):
            errs.append(f"{tr}.bookmarks must be a list")
            continue
        for i, b in enumerate(bms):
            try:
                new_bookmark(tr, (b or {}).get("t"), (b or {}).get("type", "other"), (b or {}).get("note", ""))
            except (LogError, AttributeError) as e:
                errs.append(f"{tr}.bookmarks[{i}]: {e}")
        det = sec.get("detected")
        if det is not None and not isinstance(det, dict):
            errs.append(f"{tr}.detected must be an object or null")
    return errs


def parse(obj) -> dict:
    """Validate a log (dict or JSON text) and return a normalised copy; raises LogError."""
    import json
    if isinstance(obj, (str, bytes)):
        try:
            obj = json.loads(obj)
        except ValueError as e:
            raise LogError(f"not JSON: {e}")
    errs = validate(obj)
    if errs:
        raise LogError("; ".join(errs[:10]))
    out = copy.deepcopy(obj)
    for tr in TRACKS:
        out[tr].setdefault("media", {})
        out[tr].setdefault("detected", None)
        out[tr]["bookmarks"] = sorted(out[tr].get("bookmarks", []), key=lambda b: float(b["t"]))
    out.setdefault("project", {})
    return out


def bookmarks_from_log(log: dict) -> list:
    """Project bookmarks ({id, track, t, type, note, created, modified}) from a parsed log."""
    out = []
    for tr in TRACKS:
        for b in log[tr]["bookmarks"]:
            nb = new_bookmark(tr, b["t"], b.get("type", "other"), b.get("note", ""), id=b.get("id"),
                              created=b.get("created"))
            if b.get("modified"):
                nb["modified"] = b["modified"]
            out.append(nb)
    return out


def import_into(project: dict, log, mode="replace") -> dict:
    """Put a log's bookmarks (and perfect_sync_example) into project data. mode replace|merge (by id)."""
    lg = parse(log)
    new = bookmarks_from_log(lg)
    if mode == "merge":
        ids = {b["id"] for b in new}
        new = [b for b in _bm_list(project) if b.get("id") not in ids] + new
    elif mode != "replace":
        raise LogError("mode must be replace or merge")
    project["bookmarks"] = new
    if "perfect_sync_example" in (lg.get("project") or {}):
        project["perfect_sync_example"] = bool(lg["project"]["perfect_sync_example"])
    return project


def media_match(log: dict, video_path=None, song_path=None) -> dict:
    """{video, audio}: True/False when both hashes are known, else None."""
    out = {}
    for tr, p in (("video", video_path), ("audio", song_path)):
        h = (log.get(tr, {}).get("media") or {}).get("hash")
        try:
            out[tr] = (content_hash(p) == h) if (p and h) else None
        except OSError:
            out[tr] = None
    return out


# ---------------------------------------------------------------- CSV
def _rows(log: dict):
    for tr in TRACKS:
        sec = log[tr]
        for b in sec.get("bookmarks", []):
            yield {"track": tr, "source": "bookmark", "type": b.get("type"), "t": b.get("t"),
                   "timeline_t": b.get("timeline_t", b.get("t")), "id": b.get("id"), "note": b.get("note", "")}
        det = sec.get("detected") or {}
        off = float((log.get("project") or {}).get("offset") or 0.0)
        if tr == "video":
            for t in det.get("cuts", []):
                yield {"track": tr, "source": "detected", "type": "cut", "t": t, "timeline_t": t}
            for b in det.get("visual_booms", []):
                yield {"track": tr, "source": "detected", "type": "visual_boom", "t": b["t"], "timeline_t": b["t"],
                       "conf": b.get("conf"), "kind": b.get("kind")}
            for c in det.get("camera_peaks", []):
                yield {"track": tr, "source": "detected", "type": "camera_peak", "t": c["t"], "timeline_t": c["t"],
                       "kind": c.get("kind"), "conf": c.get("peak"), "end": c.get("end")}
        else:
            for b in det.get("booms", []):
                yield {"track": tr, "source": "detected", "type": b.get("class", "boom"), "t": b["t"],
                       "timeline_t": _r(float(b["t"]) - off), "conf": b.get("conf"),
                       "kind": "main" if b.get("main") else ""}


def to_csv(log: dict) -> str:
    """Readable CSV: '# key: value' header lines (identity, version), then one row per event, by time."""
    buf = io.StringIO()
    buf.write(f"# format: {log['format']} {log['format_version']}\n")
    buf.write(f"# app_version: {log['app']['version']}\n# exported_at: {log.get('exported_at')}\n")
    pr = log.get("project") or {}
    buf.write(f"# perfect_sync_example: {str(bool(pr.get('perfect_sync_example'))).lower()}\n")
    buf.write(f"# offset: {pr.get('offset')}\n")
    for tr in TRACKS:
        m = log[tr].get("media") or {}
        buf.write(f"# {tr}: duration_s={m.get('duration_s')} size={m.get('size')} hash={m.get('hash')}\n")
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    rows = sorted(_rows(log), key=lambda r: (float(r["timeline_t"] if r.get("timeline_t") is not None else r["t"]),
                                            r["track"], r["source"]))
    for r in rows:
        r = dict(r)
        r["mmss"] = mmss(r["timeline_t"] if r.get("timeline_t") is not None else r["t"])
        w.writerow(r)
    return buf.getvalue()


def parse_csv(text: str) -> dict:
    """Bookmarks back out of a CSV export (detected rows are informational and dropped): a minimal log."""
    meta = {}
    text = text.lstrip("\ufeff")
    while text.startswith("#"):                    # '# key: value' lines only at the top (notes may hold newlines)
        ln, _, text = text.partition("\n")
        k, _, v = ln[1:].partition(":")
        meta[k.strip()] = v.strip().rstrip("\r")
    lines = io.StringIO(text)
    log = {"format": FORMAT, "format_version": FORMAT_VERSION, "app": {"version": meta.get("app_version")},
           "project": {"perfect_sync_example": meta.get("perfect_sync_example") == "true"},
           "video": {"media": {}, "bookmarks": [], "detected": None},
           "audio": {"media": {}, "bookmarks": [], "detected": None}}
    try:
        log["project"]["offset"] = float(meta.get("offset"))
    except (TypeError, ValueError):
        pass
    for tr in TRACKS:
        kv = dict(p.split("=", 1) for p in meta.get(tr, "").split() if "=" in p)
        log[tr]["media"] = {"duration_s": _r(kv.get("duration_s")),
                            "size": int(kv["size"]) if kv.get("size", "None").isdigit() else None,
                            "hash": None if kv.get("hash") in (None, "None") else kv["hash"], "hash_algo": HASH_ALGO}
    for r in csv.DictReader(lines):
        if r.get("source") != "bookmark" or r.get("track") not in TRACKS:
            continue
        log[r["track"]]["bookmarks"].append({"id": r.get("id") or None, "t": float(r["t"]), "type": r.get("type"),
                                             "note": r.get("note") or "",
                                             "timeline_t": _r(r.get("timeline_t"))})
    return parse(log)
