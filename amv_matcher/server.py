"""FastAPI server: scan jobs with polling progress, ranked results, audio preview."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .audio import AUDIO_EXTS, analyze_song, song_waveform
from . import media
from .pump import pump_keyframes
from .score import score, score_at
from .video import VIDEO_EXTS, analyze_video

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
app = FastAPI(title="AMV Matcher")

JOBS: dict = {}
ALLOWED_DIRS: set = set()   # music dirs that have been scanned
ALLOWED_VIDEOS: set = set()  # video files that have been scanned
VIDEOS: dict = {}           # resolved video path -> analysis (for /api/score)
_jobs_lock = threading.Lock()

EXTRACTED = ROOT / ".cache" / "extracted"   # embedded audio decoded from videos
EXPORTS = ROOT / ".exports"                 # rendered wavs / mp4s the user can download
UPLOADS = ROOT / ".uploads"   # dropped files land here (module var so tests can redirect it)
UPLOAD_LIMITS = {"video": 8 * 1024 ** 3, "song": 300 * 1024 ** 2}
ORIGINAL_SLACK_S = 0.5
ALLOWED_ORIGINS = {"http://127.0.0.1:8765", "http://localhost:8765"}


def _origin_guard(request: Request):
    """Reject cross-site POST/DELETE: a present Origin header must be this server."""
    origin = request.headers.get("origin")
    if origin is not None and origin not in ALLOWED_ORIGINS:
        raise HTTPException(403, f"Origin {origin} is not allowed")


def _in_uploads(p: Path) -> bool:
    try:
        return p.is_relative_to(UPLOADS.resolve())
    except Exception:
        return False


def _under(p: Path, base: Path) -> bool:
    try:
        return p.is_relative_to(base.resolve())
    except Exception:
        return False


def _in_music_dirs(p: Path) -> bool:
    return any(p == Path(d) or Path(d) in p.parents for d in ALLOWED_DIRS)


def _full_video(vp: Path) -> dict:
    """Cached video analysis plus has_audio / original_audio (embedded audio extracted to a wav)."""
    video = VIDEOS.get(str(vp))
    if video is None:
        video = analyze_video(str(vp))
        video["has_audio"], video["original_audio"] = False, None
        try:
            wav = media.extract_audio(vp, EXTRACTED)
            if wav is not None:
                video["has_audio"] = True
                video["original_audio"] = {"path": str(wav.resolve()),
                                           "name": f"Original audio ({vp.name})"}
        except Exception as e:   # a broken audio track must not break video analysis
            video["audio_error"] = f"{type(e).__name__}: {e}"
        VIDEOS[str(vp)] = video
    return video


def _video_info(video: dict) -> dict:
    info = {k: video[k] for k in ("duration", "fps", "motion_tempo_bpm", "energy_curve",
                                  "energy_hz", "segments", "lulls", "peak_segments",
                                  "cut_times", "shots")}
    info["has_audio"] = bool(video.get("has_audio"))
    info["original_audio"] = video.get("original_audio")
    info["cut_count"] = len(video["cut_times"])
    info["peak_count"] = len(video["motion_peaks"])
    return info


def _score_songs(job: dict, video: dict, songs: list):
    """Analyze + score songs into job (results/too_short/skipped); shared by scan and score-songs."""
    job.update(stage="songs", total=len(songs), done=0)
    results, skipped = [], []
    orig = video.get("original_audio")
    if orig:      # the video's own soundtrack: already aligned, so offset 0 is fixed
        job.update(message="Analyzing original audio")
        try:
            song = analyze_song(orig["path"])
            if song["duration"] >= float(video["duration"]) - ORIGINAL_SLACK_S:
                sc = score_at(song, video, 0.0)
            else:
                sc = score(song, video)       # clearly shorter than the video: too_short
            results.append({"path": orig["path"], "name": orig["name"], "source": "original",
                            "bpm": song["bpm"], "duration": song["duration"], **sc})
        except Exception as e:
            skipped.append({"path": orig["path"], "name": orig["name"], "source": "original",
                            "reason": f"{type(e).__name__}: {e}"})
    for i, p in enumerate(songs, 1):
        job.update(done=i - 1, message=f"Analyzing song {i}/{len(songs)}: {p.name}")
        try:
            song = analyze_song(str(p))
            sc = score(song, video)
            results.append({"path": str(p), "name": p.name, "source": "song", "bpm": song["bpm"],
                            "duration": song["duration"], **sc})
        except Exception as e:  # reported, never silently skipped
            skipped.append({"path": str(p), "name": p.name, "reason": f"{type(e).__name__}: {e}"})
    ok = [r for r in results if r["eligible"]]
    short = [r for r in results if not r["eligible"]]
    ok.sort(key=lambda r: (r["total_pct"], r["sync_pct"]), reverse=True)
    for rank, r in enumerate(ok, 1):
        r["rank"] = rank
    short.sort(key=lambda r: r["duration"], reverse=True)
    job.update(done=len(songs), results=ok, too_short=short, skipped=skipped, status="done",
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
                        "results": None, "too_short": [], "skipped": [], "error": None}
        ALLOWED_DIRS.add(str(md.resolve()))
        ALLOWED_VIDEOS.add(str(vp.resolve()))
    _pool.submit(_run_job, job_id, req)
    return {"job_id": job_id}


@app.get("/api/scan/{job_id}")
def scan_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return JSONResponse(job)


def _safe_song(path: str) -> Path:
    try:
        p = Path(path).expanduser().resolve()
    except Exception:
        raise HTTPException(400, "Bad path")
    if not (_in_music_dirs(p) or _in_uploads(p) or _under(p, EXTRACTED)) or p.suffix.lower() not in AUDIO_EXTS or not p.is_file():
        raise HTTPException(403, "Path is not a song inside a scanned music folder or the uploads folder")
    return p


def _safe_video(path: str) -> Path:
    try:
        p = Path(path).expanduser().resolve()
    except Exception:
        raise HTTPException(400, "Bad path")
    if not (str(p) in ALLOWED_VIDEOS or _in_uploads(p)) or p.suffix.lower() not in VIDEO_EXTS or not p.is_file():
        raise HTTPException(403, "Path is not a scanned or uploaded video")
    return p


@app.get("/api/audio")
def audio(path: str):
    return FileResponse(_safe_song(path))  # supports Range requests for seeking


@app.get("/api/video")
def video_file(path: str):
    return FileResponse(_safe_video(path))  # Starlette FileResponse honours Range -> 206


@app.get("/api/waveform")
def waveform(path: str):
    p = _safe_song(path)
    song = analyze_song(str(p))
    return {"peaks": song_waveform(str(p)), "duration": song["duration"], "bpm": song["bpm"],
            "beat_times": song["beat_times"], "drops": song["drops"], "lulls": song["lulls"],
            "beat_strengths": song.get("beat_strengths", []),
            "energy_curve": song["energy_curve"], "energy_hz": song["energy_hz"]}


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
    song = analyze_song(str(_safe_song(req.song_path)))
    off = max(0.0, min(req.offset, max(0.0, song["duration"] - video["duration"])))
    return score_at(song, video, off)


@app.get("/api/pump")
def pump(path: str, boom: float = 40, decay: float = 180, drops_only: bool = False,
         fps: float = 30, shake: bool = False, start: float = 0.0, duration: float = None):
    song = analyze_song(str(_safe_song(path)))
    return {"fps": fps, "keyframes": pump_keyframes(song, boom, decay, drops_only, fps,
                                                    duration=duration, shake=shake, start=start)}


SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ ()\-]")


def _sanitize_name(name: str) -> str:
    base = name.replace("\\", "/").split("/")[-1]
    base = SAFE_NAME_RE.sub("_", base).strip(" .")
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    stem = (stem or "file")[:100]
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
            os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    return {"path": str(final.resolve()), "name": clean, "size": size, "kind": kind, "deduped": deduped}


def _uploads_files():
    if not UPLOADS.is_dir():
        return []
    return [p for p in UPLOADS.rglob("*") if p.is_file() and not p.is_symlink()]


@app.get("/api/uploads")
def uploads_info():
    files = _uploads_files()
    return {"count": len(files), "bytes": sum(p.stat().st_size for p in files)}


@app.delete("/api/uploads", dependencies=[Depends(_origin_guard)])
def uploads_clear():
    freed = sum(p.stat().st_size for p in _uploads_files())
    if UPLOADS.is_dir():
        for child in UPLOADS.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    for k in [k for k in VIDEOS if _in_uploads(Path(k))]:
        VIDEOS.pop(k, None)
    return {"freed": freed}


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
                        "results": None, "too_short": [], "skipped": [], "error": None}
    if len(songs) <= 1:      # synchronous (0 songs = video analysis only): finished job inline
        _run_score_songs(job_id, vp, songs)
        return JSONResponse({"job_id": job_id, **JOBS[job_id]})
    _pool.submit(_run_score_songs, job_id, vp, songs)
    return {"job_id": job_id, "status": "running"}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/static/{name}")
def static_file(name: str):
    p = (STATIC / name).resolve()
    if STATIC.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)
