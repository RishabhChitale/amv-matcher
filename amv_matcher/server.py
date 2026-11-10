"""FastAPI server: scan jobs with polling progress, ranked results, audio preview."""
from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .audio import AUDIO_EXTS, analyze_song, song_waveform
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
        video = analyze_video(str(video_path))
        VIDEOS[str(video_path.resolve())] = video
        job["video_path"] = str(video_path.resolve())
        job["video"] = {k: video[k] for k in ("duration", "fps", "motion_tempo_bpm", "energy_curve",
                                             "energy_hz", "segments", "lulls", "peak_segments",
                                             "cut_times", "shots")}
        job["video"]["cut_count"] = len(video["cut_times"])
        job["video"]["peak_count"] = len(video["motion_peaks"])

        songs = _list_songs(music_dir, req.recursive)
        job.update(stage="songs", total=len(songs), done=0)
        results, skipped = [], []
        for i, p in enumerate(songs, 1):
            job.update(done=i - 1, message=f"Analyzing song {i}/{len(songs)}: {p.name}")
            try:
                song = analyze_song(str(p))
                s = score(song, video)
                results.append({"path": str(p), "name": p.name, "bpm": song["bpm"],
                                "duration": song["duration"], **s})
            except Exception as e:  # reported, never silently skipped
                skipped.append({"path": str(p), "name": p.name, "reason": f"{type(e).__name__}: {e}"})
        ok = [r for r in results if r["eligible"]]
        short = [r for r in results if not r["eligible"]]
        ok.sort(key=lambda r: (r["total_pct"], r["sync_pct"]), reverse=True)
        for rank, r in enumerate(ok, 1):
            r["rank"] = rank
        short.sort(key=lambda r: r["duration"], reverse=True)
        results = ok
        job.update(done=len(songs), results=results, too_short=short, skipped=skipped, status="done",
                   message=f"Done: {len(results)} ranked, {len(skipped)} skipped")
    except Exception as e:
        job.update(status="error", error=f"{type(e).__name__}: {e}", message=str(e))


_pool = ThreadPoolExecutor(max_workers=2)


@app.post("/api/scan")
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


@app.get("/api/audio")
def audio(path: str):
    try:
        p = Path(path).expanduser().resolve()
    except Exception:
        raise HTTPException(400, "Bad path")
    ok = any(p == Path(d) or Path(d) in p.parents for d in ALLOWED_DIRS)
    if not ok or p.suffix.lower() not in AUDIO_EXTS or not p.is_file():
        raise HTTPException(403, "Path is not a song inside a scanned music folder")
    return FileResponse(p)  # supports Range requests for seeking


def _safe_song(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    ok = any(p == Path(d) or Path(d) in p.parents for d in ALLOWED_DIRS)
    if not ok or p.suffix.lower() not in AUDIO_EXTS or not p.is_file():
        raise HTTPException(403, "Path is not a song inside a scanned music folder")
    return p


@app.get("/api/video")
def video_file(path: str):
    p = Path(path).expanduser().resolve()
    if str(p) not in ALLOWED_VIDEOS or not p.is_file():
        raise HTTPException(403, "Path is not a scanned video")
    return FileResponse(p)  # Starlette FileResponse honours Range -> 206


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


@app.post("/api/score")
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


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/static/{name}")
def static_file(name: str):
    p = (STATIC / name).resolve()
    if STATIC.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)
