"""FastAPI server: scan jobs with polling progress, ranked results, audio preview."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import urllib.parse
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .audio import AUDIO_EXTS, analyze_song, song_waveform
from . import media
from .pump import pump_keyframes
from .score import MIN_OFFSET_S, score, score_at
from .splice import plan_splice
from .video import VIDEO_EXTS, analyze_video
from . import paths, proxy, winsafe
from . import autosync as _autosync
from .cache import get_cache

ROOT = Path(__file__).resolve().parent.parent
STATIC = paths.resource_root() / "static"
app = FastAPI(title="AMV Matcher")

JOBS: dict = {}
ALLOWED_DIRS: set = set()   # music dirs that have been scanned
ALLOWED_VIDEOS: set = set()  # video files that have been scanned
VIDEOS: dict = {}           # resolved video path -> analysis (for /api/score)
_jobs_lock = threading.Lock()

EXTRACTED = paths.CACHE / "extracted"   # embedded audio decoded from videos
EXPORTS = paths.EXPORTS                 # rendered wavs / mp4s the user can download
UPLOADS = paths.UPLOADS   # dropped files land here (module var so tests can redirect it)
UPLOAD_LIMITS = {"video": 8 * 1024 ** 3, "song": 300 * 1024 ** 2}
ORIGINAL_SLACK_S = 0.5
ALLOWED_ORIGINS = {"http://127.0.0.1:8765", "http://localhost:8765"}
ALLOWED_SONGS: set = set()   # individually picked/dropped song files (native paths)


def allow_port(port: int) -> None:
    """The desktop shell serves on a free port: accept that origin for POST/DELETE."""
    ALLOWED_ORIGINS.update({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})


def _origin_guard(request: Request):
    """Reject cross-site POST/DELETE: a present Origin header must be this server."""
    origin = request.headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        raise HTTPException(403, f"Origin {origin} is not allowed")


def _in_uploads(p: Path) -> bool:
    try:
        return winsafe.is_relative_to_ci(p, UPLOADS.resolve())
    except Exception:
        return False


def _under(p: Path, base: Path) -> bool:
    try:
        return winsafe.is_relative_to_ci(p, base.resolve())
    except Exception:
        return False


def _in_music_dirs(p: Path) -> bool:
    return any(winsafe.is_relative_to_ci(p, d) for d in ALLOWED_DIRS)



# ---- background analysis workers: heavy work runs in a process pool, never on the event loop
_WP = None
_WP_LOCK = threading.Lock()


def _wp():
    """The shared WorkerPool (created on first use); None when AMV_WORKERS=0 (inline, for tests)."""
    global _WP
    if os.environ.get("AMV_WORKERS") == "0":
        return None
    with _WP_LOCK:
        if _WP is None:
            from .workers import WorkerPool
            n = os.environ.get("AMV_WORKERS")
            _WP = WorkerPool(workers=int(n) if n and n.isdigit() else None)
        return _WP


def shutdown_workers():
    global _WP
    with _WP_LOCK:
        wp, _WP = _WP, None
    if wp is not None:
        wp.shutdown()


def _wait(kind: str, path, priority: int = 0, **params):
    """Submit a worker job and block THIS (sync endpoint) thread until it finishes; returns its result."""
    wp = _wp()
    jid = wp.submit(kind, str(path), priority, **params)
    while True:
        st = wp.status(jid)
        if st is None or st["state"] in ("error", "cancelled"):
            raise ValueError((st or {}).get("error") or f"{kind} job {(st or {}).get('state', 'lost')}")
        if st["state"] == "done":
            return st["result"]
        time.sleep(0.03)


def _song(path):
    if _wp() is None:
        return analyze_song(path)
    try:
        res = _wait("song_analysis", path, 0)
    except ValueError:
        raise
    except Exception:
        return analyze_song(path)
    try:    # the worker wrote songs.json: drop this process's stale in-memory copy
        from . import audio as _audio
        c = get_cache(_audio.CACHE_PATH)
        with c.lock:
            c.data = None
    except Exception:
        pass
    return res


def _video(path):
    if _wp() is None:
        return analyze_video(path)
    try:
        return _wait("video_analysis", path, 0)
    except ValueError:
        raise
    except Exception:
        return analyze_video(path)


def _full_video(vp: Path) -> dict:
    """Cached video analysis plus has_audio / original_audio (embedded audio extracted to a wav)."""
    video = VIDEOS.get(str(vp))
    if video is None:
        video = _video(str(vp))
        video["has_audio"], video["original_audio"] = False, None
        try:
            wav = media.extract_audio(vp, EXTRACTED)
            if wav is not None:
                video["has_audio"] = True
                video["original_audio"] = {"path": str(wav.resolve()),
                                           "name": f"Original audio ({vp.name})"}
        except Exception as e:   # a broken audio track must not break video analysis
            video["audio_error"] = f"{type(e).__name__}: {e}"
        try:
            if _needs_proxy(vp):
                video["preview_proxy"] = True
                _proxy_bg(vp)
        except Exception:
            pass
        VIDEOS[str(vp)] = video
    return video


def _video_info(video: dict) -> dict:
    info = {k: video[k] for k in ("duration", "fps", "motion_tempo_bpm", "energy_curve",
                                  "energy_hz", "segments", "segments_struct", "lulls", "peak_segments",
                                  "cut_times", "shots")}
    info["has_audio"] = bool(video.get("has_audio"))
    info["original_audio"] = video.get("original_audio")
    info["preview_proxy"] = bool(video.get("preview_proxy"))
    info["cut_count"] = len(video["cut_times"])
    info["peak_count"] = len(video["motion_peaks"])
    return info


def _cached_for(video_path, song_path):
    try:
        return _autosync_cached(Path(video_path), Path(song_path))
    except Exception:
        return None


def _score_songs(job: dict, video: dict, songs: list):
    """Analyze + score songs into job (results/too_short/skipped); shared by scan and score-songs."""
    job.update(stage="songs", total=len(songs), done=0)
    results, skipped = [], []
    orig = video.get("original_audio")
    if orig:      # the video's own soundtrack: already aligned, so offset 0 is fixed
        job.update(message="Analyzing original audio")
        try:
            song = _song(orig["path"])
            if song["duration"] >= float(video["duration"]) - ORIGINAL_SLACK_S:
                sc = score_at(song, video, 0.0)
            else:
                sc = score(song, video, allow_negative=True)       # clearly shorter than the video: too_short
            results.append({"path": orig["path"], "name": orig["name"], "source": "original",
                            "bpm": song["bpm"], "duration": song["duration"], **sc,
                            "natural_total_pct": sc.get("total_pct"),
                            "auto_sync_cached": _cached_for(job.get("video_path"), orig["path"])})
        except Exception as e:
            skipped.append({"path": orig["path"], "name": orig["name"], "source": "original",
                            "reason": f"{type(e).__name__}: {e}"})
    for i, p in enumerate(songs, 1):
        job.update(done=i - 1, message=f"Analyzing song {i}/{len(songs)}: {p.name}")
        try:
            song = _song(str(p))
            sc = score(song, video, allow_negative=True)
            results.append({"path": str(p), "name": p.name, "source": "song", "bpm": song["bpm"],
                            "duration": song["duration"], **sc, "natural_total_pct": sc.get("total_pct"),
                            "auto_sync_cached": _cached_for(job.get("video_path"), p)})
        except Exception as e:  # reported, never silently skipped
            skipped.append({"path": str(p), "name": p.name, "reason": f"{type(e).__name__}: {e}"})
    ok = [r for r in results if r["eligible"]]
    ref = next((r for r in ok if r.get("source") == "original"), None)
    reference = {"original_total": ref["total_pct"], "original_sync": ref["sync_pct"]} if ref else None
    if reference:       # how each song compares with the video's own soundtrack
        for r in ok:
            r["vs_original"] = {"total": r["total_pct"] - reference["original_total"],
                                "sync": round(r["sync_pct"] - reference["original_sync"], 1)}
    short = [r for r in results if not r["eligible"]]
    # primary ranking = natural total (plain trim at the best offset, no auto-sync edits); sync_pct breaks ties
    ok.sort(key=lambda r: (r["natural_total_pct"], r["sync_pct"]), reverse=True)
    for rank, r in enumerate(ok, 1):
        r["rank"] = rank
    short.sort(key=lambda r: r["duration"], reverse=True)
    job.update(done=len(songs), reference=reference, results=ok, too_short=short, skipped=skipped, status="done",
               message=f"Done: {len(ok)} ranked, {len(skipped)} skipped")


class ScanRequest(BaseModel):
    video_path: str
    music_dir: str
    recursive: bool = False


def _list_songs(music_dir: Path, recursive: bool):
    it = music_dir.rglob("*") if recursive else music_dir.glob("*")
    return sorted(p for p in it if p.is_file() and p.suffix.lower() in AUDIO_EXTS
                  and not p.name.startswith("."))


def _run_job(job_id: str, req: ScanRequest):
    job = JOBS[job_id]
    try:
        video_path = Path(req.video_path).expanduser()
        music_dir = Path(req.music_dir).expanduser().resolve()
        job.update(stage="video", message=f"Analyzing video: {video_path.name}")
        video = _full_video(video_path.resolve())
        job["video_path"] = str(video_path.resolve())
        job["video"] = _video_info(video)
        _score_songs(job, video, _list_songs(music_dir, req.recursive))
    except Exception as e:
        job.update(status="error", error=f"{type(e).__name__}: {e}", message=str(e))


_pool = ThreadPoolExecutor(max_workers=2)


@app.post("/api/scan", dependencies=[Depends(_origin_guard)])
def scan(req: ScanRequest):
    vp = Path(req.video_path).expanduser()
    md = Path(req.music_dir).expanduser()
    if not vp.is_file():
        raise HTTPException(400, f"Video file not found: {vp}")
    if vp.suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(400, f"Unsupported video type {vp.suffix}; use {sorted(VIDEO_EXTS)}")
    if not md.is_dir():
        raise HTTPException(400, f"Music folder not found: {md}")
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        JOBS[job_id] = {"id": job_id, "status": "running", "stage": "starting", "done": 0,
                        "total": 0, "message": "Starting", "video": None,
                        "results": None, "reference": None, "too_short": [], "skipped": [], "error": None}
        ALLOWED_DIRS.add(winsafe.norm_case_path(md.resolve()))
        ALLOWED_VIDEOS.add(winsafe.norm_case_path(vp.resolve()))
    _pool.submit(_run_job, job_id, req)
    return {"job_id": job_id}


@app.get("/api/scan/{job_id}")
def scan_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return JSONResponse(job)


def allow_path(path) -> str:
    """Add an existing media file (linked in place, never copied) to the allowed set by its extension."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise ValueError("not a file")
    if p.suffix.lower() in VIDEO_EXTS:
        ALLOWED_VIDEOS.add(winsafe.norm_case_path(p))
    elif p.suffix.lower() in AUDIO_EXTS:
        ALLOWED_SONGS.add(winsafe.norm_case_path(p))
    else:
        raise ValueError("not a media file")
    return str(p)


def _safe_song(path: str) -> Path:
    try:
        p = Path(path).expanduser().resolve()
    except Exception:
        raise HTTPException(400, "Bad path")
    if not (_in_music_dirs(p) or winsafe.in_allowed(p, ALLOWED_SONGS) or _in_uploads(p) or _under(p, EXTRACTED)) or p.suffix.lower() not in AUDIO_EXTS or not p.is_file():
        raise HTTPException(403, "Path is not a song inside a scanned music folder or the uploads folder")
    return p


def _safe_video(path: str) -> Path:
    try:
        p = Path(path).expanduser().resolve()
    except Exception:
        raise HTTPException(400, "Bad path")
    if not (winsafe.in_allowed(p, ALLOWED_VIDEOS) or _in_uploads(p)) or p.suffix.lower() not in VIDEO_EXTS or not p.is_file():
        raise HTTPException(403, "Path is not a scanned or uploaded video")
    return p


@app.get("/api/audio")
def audio(path: str):
    return FileResponse(_safe_song(path))  # supports Range requests for seeking


_PROXY_POOL = ThreadPoolExecutor(max_workers=1)


def _proxy_bg(vp: Path):
    wp = _wp()
    if wp is None:
        proxy.build_async(vp, _PROXY_POOL)
    else:
        wp.submit("proxy", str(vp), 1)
_NEEDS_PROXY: dict = {}


def _needs_proxy(vp: Path) -> bool:
    k = f"{vp}|{vp.stat().st_mtime_ns}"
    if k not in _NEEDS_PROXY:
        _NEEDS_PROXY[k] = proxy.needs_proxy(vp)
    return _NEEDS_PROXY[k]


@app.get("/api/video")
def video_file(path: str):
    vp = _safe_video(path)
    if _needs_proxy(vp):   # browser can't decode this one: serve the cached 540p H.264 proxy
        try:
            wp = _wp()
            return FileResponse(Path(_wait("proxy", vp, 0)["proxy"]) if wp else proxy.build(vp))
        except Exception:
            pass           # last resort: the original (the UI shows its "can't preview" message)
    return FileResponse(vp)  # Starlette FileResponse honours Range -> 206


# ---- worker-pool job API (non-blocking; the UI polls /api/jobs?since=)
class JobRequest(BaseModel):
    kind: str
    path: str
    priority: int = 1
    params: dict = Field(default_factory=dict)


def _need_wp():
    wp = _wp()
    if wp is None:
        raise HTTPException(503, "background workers are disabled")
    return wp


@app.post("/api/jobs", dependencies=[Depends(_origin_guard)])
def jobs_submit(req: JobRequest):
    p = Path(req.path).expanduser().resolve()
    if req.kind == "song_analysis":
        p = _safe_song(str(p))
    elif req.kind in ("video_analysis", "probe", "thumbnails", "filmstrip", "proxy"):
        p = _safe_video(str(p))
    else:
        raise HTTPException(400, f"unknown job kind: {req.kind}")
    params = {k: v for k, v in req.params.items() if k in ("n", "width")}
    try:
        jid = _need_wp().submit(req.kind, str(p), req.priority, **params)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": jid, **(_need_wp().status(jid) or {})}


@app.get("/api/jobs/stats")
def jobs_stats():
    return _need_wp().stats()


@app.get("/api/jobs")
def jobs_list(since: int = 0, state: Optional[str] = None):
    wp = _need_wp()
    return {"events": wp.events(since), "stats": wp.stats(), **({"jobs": wp.jobs(state)} if state else {})}


@app.get("/api/jobs/{job_id}")
def jobs_get(job_id: str):
    st = _need_wp().status(job_id)
    if st is None:
        raise HTTPException(404, "unknown job")
    return st


@app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(_origin_guard)])
def jobs_cancel(job_id: str):
    return {"cancelled": _need_wp().cancel(job_id)}


class BumpRequest(BaseModel):
    priority: int = 0


@app.post("/api/jobs/{job_id}/bump", dependencies=[Depends(_origin_guard)])
def jobs_bump(job_id: str, req: BumpRequest):
    try:
        return {"bumped": _need_wp().bump(job_id, req.priority)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/prewarm/pause", dependencies=[Depends(_origin_guard)])
def prewarm_pause():
    _need_wp().pause_prewarm()
    return {"paused": True}


@app.post("/api/prewarm/resume", dependencies=[Depends(_origin_guard)])
def prewarm_resume():
    _need_wp().resume_prewarm()
    return {"paused": False}



def _sections_with_stems(path, song: dict) -> list:
    """Song sections; refined with drums/vocals activity when stems are ALREADY cached (never separates)."""
    secs = [dict(s) for s in song.get("sections", [])]
    if not secs:
        return secs
    try:
        from . import stems as _st
        from .structure import _stem_refine, stems_activity
        for prov in ("demucs", "hpss"):
            hit = _st.cached(str(path), prov)
            if hit:
                act = stems_activity(hit["stems"])
                if act:
                    _stem_refine(secs, act)
                break
    except Exception:
        pass
    return secs


def _cached_stem_targets(path, song: dict) -> list:
    """Soft stem sync targets for the quiet parts when stems are ALREADY cached (never separates)."""
    try:
        from . import stems as _st, stemsync
        for prov in ("demucs", "hpss"):
            if _st.cached(str(path), prov):
                return stemsync.stem_targets(song, _st.features_for(str(path), prov))
    except Exception:
        pass
    return []


@app.get("/api/waveform")
def waveform(path: str):
    p = _safe_song(path)
    song = _song(str(p))
    return {"peaks": song_waveform(str(p)), "duration": song["duration"], "bpm": song["bpm"],
            "beat_times": song["beat_times"], "drops": song["drops"], "lulls": song["lulls"],
            "beat_strengths": song.get("beat_strengths", []),
            "energy_curve": song["energy_curve"], "energy_hz": song["energy_hz"],
            "sections": _sections_with_stems(p, song)}


class ScoreRequest(BaseModel):
    video_path: str
    song_path: str
    offset: float


@app.post("/api/score", dependencies=[Depends(_origin_guard)])
def score_offset(req: ScoreRequest):
    vp = str(Path(req.video_path).expanduser().resolve())
    video = VIDEOS.get(vp)
    if video is None:
        raise HTTPException(404, "Video not scanned yet")
    song = _song(str(_safe_song(req.song_path)))
    off = max(MIN_OFFSET_S, min(req.offset, max(0.0, song["duration"] - video["duration"])))
    return score_at(song, video, off)


@app.get("/api/pump")
def pump(path: str, boom: float = 40, decay: float = 180, drops_only: bool = False,
         fps: float = 30, shake: bool = False, start: float = 0.0, duration: float = None):
    song = _song(str(_safe_song(path)))
    return {"fps": fps, "keyframes": pump_keyframes(song, boom, decay, drops_only, fps,
                                                    duration=duration, shake=shake, start=start)}


SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ ()\-]")
_RESERVED_STEMS = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _sanitize_name(name: str) -> str:
    base = name.replace("\\", "/").split("/")[-1]
    base = SAFE_NAME_RE.sub("_", base).strip(" .")
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    stem = (stem or "file")[:100]
    if stem.split(".")[0].strip().upper() in _RESERVED_STEMS:   # W-07: CON, NUL, COM1.. are device names on Windows
        stem = "_" + stem
    return f"{stem}.{ext.lower()}" if ext else stem


@app.post("/api/upload", dependencies=[Depends(_origin_guard)])
async def upload(request: Request, kind: str, name: str):
    if kind not in UPLOAD_LIMITS:
        raise HTTPException(400, "kind must be 'video' or 'song'")
    exts = VIDEO_EXTS if kind == "video" else AUDIO_EXTS
    clean = _sanitize_name(name)
    ext = Path(clean).suffix.lower()
    if ext not in exts:
        raise HTTPException(400, f"Unsupported {kind} type '{ext}'; use {sorted(exts)}")
    limit = UPLOAD_LIMITS[kind]
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > limit:
        raise HTTPException(413, f"{kind} is larger than the {limit // 1024 ** 2} MB limit")
    d = UPLOADS / kind
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".tmp-{uuid.uuid4().hex}"
    h, size = hashlib.sha256(), 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"{kind} is larger than the {limit // 1024 ** 2} MB limit")
                h.update(chunk)
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, "Empty upload")
        final = d / f"{h.hexdigest()[:12]}-{clean}"
        deduped = final.exists()
        if not deduped:
            winsafe.replace_retry(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    return {"path": str(final.resolve()), "name": clean, "size": size, "kind": kind, "deduped": deduped}


def _files_under(base: Path):
    if not base.is_dir():
        return []
    return [p for p in base.rglob("*") if p.is_file() and not p.is_symlink()]


def _uploads_files():
    return _files_under(UPLOADS)


def _bytes(files):
    return sum(p.stat().st_size for p in files)


def _clear_dir(base: Path, skipped: Optional[list] = None) -> int:
    """Delete everything inside base (never base itself, never anything resolving outside it).
    Locked files (Windows) are skipped, not raised; their names are appended to `skipped` (W-09)."""
    skipped = skipped if skipped is not None else []
    before = _files_under(base)
    sizes = {}
    for p in before:
        try:
            sizes[p] = p.stat().st_size
        except OSError:
            pass
    if base.is_dir():
        root = base.resolve()
        for child in list(base.iterdir()):
            try:
                if not _under(child.resolve(), root):
                    continue
                if child.is_dir() and not child.is_symlink():
                    ok = winsafe.safe_rmtree(child, root)
                else:
                    ok = winsafe.safe_unlink(child, root)
            except OSError:
                ok = False
            if not ok:
                skipped.append(child.name)
    return sum(sz for p, sz in sizes.items() if not p.exists())


@app.get("/api/uploads")
def uploads_info():
    u, e = _uploads_files(), _files_under(EXPORTS)
    pc = _files_under(proxy.proxy_dir())
    return {"proxy_count": len(pc), "proxy_bytes": _bytes(pc), "frozen": paths.FROZEN,
            "data_dir": str(paths.data_dir()), "count": len(u) + len(e), "bytes": _bytes(u) + _bytes(e),
            "uploads_count": len(u), "uploads_bytes": _bytes(u),
            "exports_count": len(e), "exports_bytes": _bytes(e)}


@app.delete("/api/uploads", dependencies=[Depends(_origin_guard)])
def uploads_clear():
    skipped: list = []
    uf = _clear_dir(UPLOADS, skipped)
    ef = _clear_dir(EXPORTS, skipped)
    for k in [k for k in VIDEOS if _in_uploads(Path(k))]:
        VIDEOS.pop(k, None)
    if skipped:
        print(f"[uploads] skipped locked files: {skipped}", flush=True)
    return {"freed": uf + ef, "uploads_freed": uf, "exports_freed": ef, "skipped": skipped}


class RegisterRequest(BaseModel):
    paths: list = Field(default_factory=list, max_length=5000)


@app.post("/api/register-paths", dependencies=[Depends(_origin_guard)])
def register_paths(req: RegisterRequest):
    """Native shell: the user picked/dropped real files. Allow them in place (no upload copy);
    dropped folders are expanded recursively (L8)."""
    videos, songs = [], []

    def add(p: Path):
        ext = p.suffix.lower()
        if ext in VIDEO_EXTS:
            ALLOWED_VIDEOS.add(winsafe.norm_case_path(p)); videos.append(str(p))
        elif ext in AUDIO_EXTS:
            ALLOWED_SONGS.add(winsafe.norm_case_path(p)); songs.append(str(p))
    for raw in req.paths:
        try:
            p = Path(str(raw)).expanduser().resolve()
        except Exception:
            continue
        if p.is_file():
            add(p)
        elif p.is_dir():
            for q in sorted(p.rglob("*")):
                if q.is_file() and not any(part.startswith(".") for part in q.relative_to(p).parts):
                    add(q.resolve())
    return {"videos": videos, "songs": songs}


class ScoreSongsRequest(BaseModel):
    video_path: str
    song_paths: list


def _run_score_songs(job_id: str, vp: Path, songs: list):
    job = JOBS[job_id]
    try:
        job.update(stage="video", message=f"Analyzing video: {vp.name}")
        video = _full_video(vp)
        job["video_path"] = str(vp)
        job["video"] = _video_info(video)
        _score_songs(job, video, songs)
    except Exception as e:
        job.update(status="error", error=f"{type(e).__name__}: {e}", message=str(e))


class AnalyzeVideoRequest(BaseModel):
    video_path: str


@app.post("/api/analyze-video", dependencies=[Depends(_origin_guard)])
def analyze_video_endpoint(req: AnalyzeVideoRequest):
    vp = _safe_video(req.video_path)
    try:
        video = _full_video(vp)
    except Exception as e:
        raise HTTPException(422, f"Could not analyze video: {type(e).__name__}: {e}")
    return JSONResponse({"video_path": str(vp), "video": _video_info(video)})


@app.post("/api/score-songs", dependencies=[Depends(_origin_guard)])
def score_songs(req: ScoreSongsRequest):
    vp = _safe_video(req.video_path)
    songs = [_safe_song(str(x)) for x in req.song_paths]
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        JOBS[job_id] = {"id": job_id, "status": "running", "stage": "starting", "done": 0,
                        "total": 0, "message": "Starting", "video": None,
                        "results": None, "reference": None, "too_short": [], "skipped": [], "error": None}
    if len(songs) <= 1:      # synchronous (0 songs = video analysis only): finished job inline
        _run_score_songs(job_id, vp, songs)
        return JSONResponse({"job_id": job_id, **JOBS[job_id]})
    _pool.submit(_run_score_songs, job_id, vp, songs)
    return {"job_id": job_id, "status": "running"}


SPLICE_CACHE: dict = {}
MAX_SEGMENTS = 8


def _vid_for(vp: Path) -> dict:
    return _full_video(vp)


class SpliceRequest(BaseModel):
    video_path: str
    song_path: str
    max_cuts: int = Field(2, ge=0, le=3)
    crossfade_ms: float = Field(40, ge=0, le=500)


@app.post("/api/splice", dependencies=[Depends(_origin_guard)])
def splice(req: SpliceRequest):
    vp, sp = _safe_video(req.video_path), _safe_song(req.song_path)
    key = (str(sp), str(vp), req.max_cuts)
    if key not in SPLICE_CACHE:
        video, song = _vid_for(vp), _song(str(sp))
        SPLICE_CACHE[key] = plan_splice(song, video, req.max_cuts)
    return SPLICE_CACHE[key]


class Segment(BaseModel):
    src_start: float = Field(ge=0)
    src_end: float
    dst_start: float = Field(ge=0)


class RenderRequest(BaseModel):
    video_path: str
    song_path: str
    segments: list[Segment]
    crossfade_ms: float = Field(40, ge=0, le=500)


def _stem(p: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", p.stem).strip("._")[:60] or "file"


def _export_url(p: Path) -> str:
    return "/api/export-file?path=" + urllib.parse.quote(str(p), safe="")


@app.post("/api/render-audio", dependencies=[Depends(_origin_guard)])
def render_audio(req: RenderRequest):
    vp, sp = _safe_video(req.video_path), _safe_song(req.song_path)
    segs = [s.model_dump() for s in req.segments]
    if not segs or len(segs) > MAX_SEGMENTS:
        raise HTTPException(422, f"Need 1-{MAX_SEGMENTS} segments")
    video = _vid_for(vp)
    vd = float(video["duration"])
    sd = _song(str(sp))["duration"]
    for s in segs:
        if not (0 <= s["src_start"] < s["src_end"] <= sd + 0.05) or s["dst_start"] >= vd:
            raise HTTPException(422, "Segment outside the song or the video")
    h = hashlib.sha256((str(sp) + str(vp) + repr(segs) + str(req.crossfade_ms)).encode()).hexdigest()[:8]
    out = EXPORTS / f"{_stem(vp)}__{_stem(sp)}__{h}.wav"
    dur = media.render_audio(sp, segs, vd, out, req.crossfade_ms)
    out = out.resolve()
    return {"path": str(out), "url": _export_url(out), "duration": dur}


@app.get("/api/export-file")
def export_file(path: str):
    try:
        p = Path(path).expanduser().resolve()
    except Exception:
        raise HTTPException(400, "Bad path")
    if not _under(p, EXPORTS.resolve()) or not p.is_file():
        raise HTTPException(403, "Not an exported file")
    return FileResponse(p, filename=p.name)   # attachment disposition


class ExportMp4Request(BaseModel):
    video_path: str
    audio_path: str
    boom: float = 0.0                 # 0 = off (plain mux, unchanged); 0-100 bakes the Boom pump
    decay: float = 180.0
    drops_only: bool = False
    shake: bool = False
    song_path: Optional[str] = None   # optional: original song for the beat grid (else the wav is analysed)
    offset: float = 0.0               # song offset (s) of audio_path inside song_path


def _run_export(job_id: str, vp: Path, ap: Path, out: Path, pump: Optional[dict] = None,
                song: Optional[Path] = None, offset: float = 0.0):
    job = JOBS[job_id]
    try:
        if pump and pump["boom"] > 0:
            from . import render as R
            pump = dict(pump)
            pump["song"] = _song(str(song)) if song else _song(str(ap))
            plan = {"offset": offset} if song else {}
            R.render_plan(vp, plan, ap, out, interp="dup", pump=pump,
                          progress_cb=lambda f: job.update(done=int(f * 100)))
        else:
            media.export_mp4(vp, ap, out, progress=lambda f: job.update(done=int(f * 100)))
        out = out.resolve()
        job.update(status="done", done=100, path=str(out), url=_export_url(out),
                   size=out.stat().st_size, message="Done")
    except media.ExportError as e:
        job.update(status="error", error=str(e))
    except Exception as e:
        job.update(status="error", error=f"{type(e).__name__}: {e}")


@app.post("/api/export-mp4", dependencies=[Depends(_origin_guard)])
def export_mp4(req: ExportMp4Request):
    vp = _safe_video(req.video_path)
    try:
        ap = Path(req.audio_path).expanduser().resolve()
    except Exception:
        raise HTTPException(400, "Bad path")
    if not _under(ap, EXPORTS.resolve()) or ap.suffix.lower() != ".wav" or not ap.is_file():
        raise HTTPException(403, "audio_path must be a wav rendered into the exports folder")
    stem = ap.stem.rsplit("__", 1)[0] if ap.stem.count("__") >= 2 else ap.stem
    out = EXPORTS / f"{stem}.mp4"
    from .render import pump_params
    pump = pump_params({"boom": req.boom, "decay": req.decay, "drops_only": req.drops_only,
                        "shake": req.shake})
    sp = _safe_song(req.song_path) if (req.song_path and pump["boom"] > 0) else None
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        JOBS[job_id] = {"id": job_id, "status": "running", "stage": "export", "done": 0,
                        "total": 100, "message": "Exporting mp4", "error": None}
    _pool.submit(_run_export, job_id, vp, ap, out, pump, sp, req.offset)
    return {"job_id": job_id, "status": "running"}


class ExportResolveRequest(BaseModel):
    video_path: str
    song_path: str
    plan: dict = {}
    boom: float = 40
    decay: float = 180
    drops_only: bool = False
    shake: bool = False


def _run_export_resolve(job_id: str, vp: Path, sp: Path, req: ExportResolveRequest):
    from . import resolve
    job = JOBS[job_id]
    try:
        song = _song(str(sp))
        video = _video(str(vp))
        res = resolve.export_bundle(
            vp, sp, req.plan or {}, song, video, EXPORTS, boom=req.boom, decay=req.decay,
            drops_only=req.drops_only, shake=req.shake,
            progress=lambda stage, n: job.update(stage=stage, done=n))
        z = Path(res["zip"]).resolve()
        job.update(status="done", done=3, total=3, message="Done", path=res["folder"],
                   zip=str(z), url=_export_url(z), clips=res["clips"], markers=res["markers"],
                   out_duration=res["out_duration"])
    except Exception as e:
        job.update(status="error", error=f"{type(e).__name__}: {e}")


@app.post("/api/export-resolve", dependencies=[Depends(_origin_guard)])
def export_resolve(req: ExportResolveRequest):
    """DaVinci Resolve bundle (FCPXML + Fusion macro + audio + how-to). Untested in Resolve."""
    vp = _safe_video(req.video_path)
    sp = _safe_song(req.song_path)
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        JOBS[job_id] = {"id": job_id, "status": "running", "stage": "analyze", "done": 0,
                        "total": 3, "message": "Exporting Resolve bundle", "error": None}
    _pool.submit(_run_export_resolve, job_id, vp, sp, req)
    return {"job_id": job_id, "status": "running"}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/static/{name:path}")
def static_file(name: str):
    p = (STATIC / name).resolve()
    if STATIC.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


from . import stems_api  # noqa: E402
app.include_router(stems_api.router)


# ---- render router (render.py, owned by another pawn): guarded, paths validated, one job at a time
from . import render as _render  # noqa: E402


class RenderMp4Request(BaseModel):
    video_path: str
    song_path: str
    plan: dict
    interp: str = "auto"
    boom: float = 0.0
    decay: float = 180.0
    drops_only: bool = False
    shake: bool = False


_render_lock = threading.Lock()


@app.post("/api/render-mp4", dependencies=[Depends(_origin_guard)])
def render_mp4(req: RenderMp4Request):
    """Registered before the router so it shadows render.py's unvalidated POST of the same path."""
    if req.interp not in ("auto", "rife", "blend", "dup"):
        raise HTTPException(400, "interp must be auto, rife, blend or dup")
    vp, sp = _safe_video(req.video_path), _safe_song(req.song_path)
    with _render_lock:
        for j in list(_render.JOBS.values()):
            if j.get("stage") == "render" and j.get("status") == "running":
                return JSONResponse({"detail": "A render is already running", "job_id": j["id"]},
                                    status_code=409)
        job = {"id": uuid.uuid4().hex[:12], "status": "running", "stage": "render", "done": 0}
        _render.JOBS[job["id"]] = job
        out = (EXPORTS / f"{_stem(vp)}__{_stem(sp)}__{job['id']}.mp4").resolve()
        EXPORTS.mkdir(parents=True, exist_ok=True)
        _render._pool.submit(_render._run_render, job, str(vp), str(sp), req.plan, req.interp, out,
                             None, _render.pump_params({"boom": req.boom, "decay": req.decay,
                                                        "drops_only": req.drops_only, "shake": req.shake}))
    return {"job_id": job["id"]}


class RenderEstimateRequest(BaseModel):
    video_path: str
    plan: dict
    interp: str = "auto"
    measure: bool = False
    gpu_id: Optional[int] = None


@app.post("/api/render-estimate", dependencies=[Depends(_origin_guard)])
def render_estimate(req: RenderEstimateRequest):
    """Shadows render.py's route of the same path: video_path must be a known, allowed video."""
    if req.interp not in ("auto", "rife", "blend", "dup"):
        raise HTTPException(400, "interp must be auto, rife, blend or dup")
    vp = _safe_video(req.video_path)
    try:
        return _render.estimate_render(req.plan, req.interp, str(vp), req.measure, req.gpu_id)
    except _render.RenderError as e:
        raise HTTPException(400, str(e))


if _render.router is not None:
    app.include_router(_render.router, dependencies=[Depends(_origin_guard)])


# ---- auto-sync (autosync.py): guarded, paths validated, clamped options, one job per pair
AUTOSYNC_LIMITS = {"max_speed_dev": 0.25, "max_tempo_pct": 8.0, "max_cuts": 3, "budget_s": 60.0, "lead_s": 0.12}
_AUTOSYNC_RUNNING: dict = {}      # (video, song) -> job id
_autosync_cache_file = None       # tests may point this at a tmp file
_AUTOSYNC_CANCEL: dict = {}       # job id -> threading.Event (set by POST .../cancel)


def _autosync_hooks_supported() -> dict:
    """Which optional auto_sync hooks the installed autosync.py has (progress_cb / should_cancel)."""
    try:
        ps = inspect.signature(_autosync.auto_sync).parameters
    except (TypeError, ValueError):
        return {"progress_cb": False, "should_cancel": False}
    return {"progress_cb": "progress_cb" in ps, "should_cancel": "should_cancel" in ps}


def _jsonable(x):
    try:
        return json.loads(json.dumps(x, default=str))
    except Exception:
        return str(x)


def _step_name(st) -> str:
    if isinstance(st, dict):
        for k in ("step", "name", "label", "tool", "order"):
            if st.get(k):
                return str(st[k])
        return ""
    return str(st)


def _as_cache():
    return get_cache(_autosync_cache_file or (paths.CACHE / "autosync.json"))


def _fingerprint(p: Path) -> str:
    st = p.stat()
    return f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}"


def _pair_key(vp: Path, sp: Path) -> str:
    return "pair:" + hashlib.sha1((_fingerprint(vp) + "||" + _fingerprint(sp)).encode()).hexdigest()


def _opts_key(vp: Path, sp: Path, opts: dict) -> str:
    raw = _fingerprint(vp) + "||" + _fingerprint(sp) + "||" + repr(sorted(opts.items()))
    return "run:" + hashlib.sha1(raw.encode()).hexdigest()


def _autosync_cached(vp: Path, sp: Path):
    """Last auto-sync `final` for this pair (any options), or None."""
    if not (vp.is_file() and sp.is_file()):
        return None
    hit = _as_cache().get(_pair_key(vp, sp))
    return hit.get("final") if hit else None


class AutoSyncGuardrails(BaseModel):
    max_speed_dev: Optional[float] = None
    max_tempo_pct: Optional[float] = None
    max_cuts: Optional[int] = None


class AutoSyncRequest(BaseModel):
    video_path: str
    song_path: str
    lead_s: Optional[float] = None
    allow_loop: bool = False
    guardrails: Optional[AutoSyncGuardrails] = None
    budget_s: Optional[float] = None
    unlock_original: bool = False      # only matters when song_path is the video's own audio
    prefer_energy: bool = True         # default on: arrange step favours the energy arc within sync/tight guards; false = old objective


def _clamp(v, lo, hi, default):
    if v is None:
        return default
    return min(hi, max(lo, float(v)))


def _autosync_options(req: AutoSyncRequest) -> dict:
    g = req.guardrails or AutoSyncGuardrails()
    L = AUTOSYNC_LIMITS
    lead = None if req.lead_s is None else _clamp(req.lead_s, 0.0, L["lead_s"], None)
    return {"lead_s": lead, "allow_loop": bool(req.allow_loop),
            "max_speed_dev": _clamp(g.max_speed_dev, 0.0, L["max_speed_dev"], L["max_speed_dev"]),
            "max_tempo_pct": _clamp(g.max_tempo_pct, 0.0, L["max_tempo_pct"], L["max_tempo_pct"]),
            "max_cuts": int(_clamp(g.max_cuts, 0, L["max_cuts"], L["max_cuts"])),
            "budget_s": _clamp(req.budget_s, 1.0, L["budget_s"], 20.0),
            "unlock_original": bool(req.unlock_original),
            "prefer_energy": bool(req.prefer_energy)}   # always echoed: default and explicit true share a key; false is cached separately (and old pre-flag entries are not reused)


def _run_autosync(job_id: str, vp: Path, sp: Path, opts: dict, pair):
    job = JOBS[job_id]
    try:
        job.update(stage="analyze", message="Analyzing")
        song = _song(str(sp))
        video = _full_video(vp)
        job.update(stage="auto-sync", message="Auto-syncing")
        ev = _AUTOSYNC_CANCEL.setdefault(job_id, threading.Event())
        if ev.is_set():
            raise _Cancelled()
        kw = {}
        orig = video.get("original_audio") or {}
        is_orig = False
        try:
            is_orig = bool(orig.get("path")) and Path(orig["path"]).resolve() == sp.resolve()
        except OSError:
            pass
        job["source"] = "original" if is_orig else "song"
        sup = _autosync_hooks_supported()
        if "original" in inspect.signature(_autosync.auto_sync).parameters:
            kw["original"] = is_orig
            kw["unlock_original"] = bool(opts.get("unlock_original"))
        if sup["progress_cb"]:
            def _progress(*a, **k):
                st = _jsonable(a[0] if len(a) == 1 and not k else (list(a) if a else k))
                job["steps_so_far"] = job.get("steps_so_far", []) + [st]
                job["current_step"] = _step_name(st)
            kw["progress_cb"] = _progress
        if sup["should_cancel"]:
            kw["should_cancel"] = ev.is_set
        if "stem_targets" in inspect.signature(_autosync.auto_sync).parameters:
            _stt = _cached_stem_targets(sp, song)
            if _stt:
                kw["stem_targets"] = _stt
        if "prefer_energy" in inspect.signature(_autosync.auto_sync).parameters:
            kw["prefer_energy"] = bool(opts.get("prefer_energy", True))
        res = _autosync.auto_sync(song, video, budget_s=opts["budget_s"], max_speed_dev=opts["max_speed_dev"],
                                  max_tempo_dev=opts["max_tempo_pct"] / 100.0, max_cuts=opts["max_cuts"],
                                  lead_s=opts["lead_s"], allow_loop=opts["allow_loop"], **kw)
        hit = isinstance(res, dict) and (res.get("cancelled") or "cancelled" in (res.get("limits_hit") or []))
        if ev.is_set() or job.get("status") == "cancelled" or hit:
            # the real hook returns the best-so-far result with 'cancelled' in limits_hit: keep it (never cached)
            keep = res if isinstance(res, dict) and res.get("final") and job.get("status") != "cancelled" else None
            job.update(status="cancelled", result=keep, message="Cancelled", current_step="")
            return
        try:
            c = _as_cache()
            entry = {"final": res.get("final"), "options": opts}
            c.set(_opts_key(vp, sp, opts), entry, flush=False)
            c.set(_pair_key(vp, sp), entry)
        except Exception:
            pass      # the cache is a convenience; never fail the job over it
        job.update(status="done", result=res, message="Done", current_step="")
    except _Cancelled:
        job.update(status="cancelled", result=None, message="Cancelled", current_step="")
    except Exception as e:
        job.update(status="error", error=f"{type(e).__name__}: {e}", message=str(e))
    finally:
        _AUTOSYNC_CANCEL.pop(job_id, None)
        with _jobs_lock:
            if _AUTOSYNC_RUNNING.get(pair) == job_id:
                del _AUTOSYNC_RUNNING[pair]


class _Cancelled(Exception):
    pass


@app.post("/api/auto-sync", dependencies=[Depends(_origin_guard)])
def auto_sync_endpoint(req: AutoSyncRequest):
    vp, sp = _safe_video(req.video_path), _safe_song(req.song_path)
    opts = _autosync_options(req)
    pair = (str(vp), str(sp))
    with _jobs_lock:
        running = _AUTOSYNC_RUNNING.get(pair)
        if running and JOBS.get(running, {}).get("status") == "running":
            return JSONResponse({"detail": "An auto-sync job for this pair is already running", "job_id": running},
                                status_code=409)
        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = {"id": job_id, "status": "running", "stage": "starting", "done": 0, "total": 1,
                        "message": "Starting", "options": opts, "result": None, "error": None,
                        "steps_so_far": [], "current_step": ""}
        _AUTOSYNC_RUNNING[pair] = job_id
        _AUTOSYNC_CANCEL[job_id] = threading.Event()
    _pool.submit(_run_autosync, job_id, vp, sp, opts, pair)
    return {"job_id": job_id, "status": "running", "options": opts}


@app.post("/api/auto-sync/{job_id}/cancel", dependencies=[Depends(_origin_guard)])
def auto_sync_cancel(job_id: str):
    """Ask a running auto-sync to stop. Idempotent; a finished job is returned unchanged (200)."""
    with _jobs_lock:
        job = JOBS.get(job_id)
        if not job or "options" not in job:
            raise HTTPException(404, "Unknown job")
        if job.get("status") != "running":
            return {"job_id": job_id, "status": job["status"], "cancelled": job["status"] == "cancelled"}
        ev = _AUTOSYNC_CANCEL.setdefault(job_id, threading.Event())
        ev.set()
        job.update(message="Cancelling", cancel_requested=True)
        if not _autosync_hooks_supported()["should_cancel"]:
            # no cooperative hook in autosync.py yet: the worker thread finishes on its own, its result is discarded
            job.update(status="cancelled", result=None, message="Cancelled", current_step="")
            for k, v in list(_AUTOSYNC_RUNNING.items()):
                if v == job_id:
                    del _AUTOSYNC_RUNNING[k]
    return {"job_id": job_id, "status": job["status"], "cancelled": True}


@app.get("/api/auto-sync/{job_id}")
def auto_sync_status(job_id: str):
    job = JOBS.get(job_id)
    if not job or "options" not in job:
        raise HTTPException(404, "Unknown job")
    return JSONResponse(job)
from .project_api import router as project_router; app.include_router(project_router, dependencies=[Depends(_origin_guard)])
from .medialib_api import router as medialib_router; app.include_router(medialib_router, dependencies=[Depends(_origin_guard)])
from .dnd_api import router as dnd_router; app.include_router(dnd_router, dependencies=[Depends(_origin_guard)])
