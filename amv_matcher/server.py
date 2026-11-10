"""FastAPI server: scan jobs with polling progress, ranked results, audio preview."""
from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
import shutil
import threading
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
from . import paths, proxy

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
        try:
            if _needs_proxy(vp):
                video["preview_proxy"] = True
                proxy.build_async(vp, _PROXY_POOL)
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
                sc = score(song, video, allow_negative=True)       # clearly shorter than the video: too_short
            results.append({"path": orig["path"], "name": orig["name"], "source": "original",
                            "bpm": song["bpm"], "duration": song["duration"], **sc})
        except Exception as e:
            skipped.append({"path": orig["path"], "name": orig["name"], "source": "original",
                            "reason": f"{type(e).__name__}: {e}"})
    for i, p in enumerate(songs, 1):
        job.update(done=i - 1, message=f"Analyzing song {i}/{len(songs)}: {p.name}")
        try:
            song = analyze_song(str(p))
            sc = score(song, video, allow_negative=True)
            results.append({"path": str(p), "name": p.name, "source": "song", "bpm": song["bpm"],
                            "duration": song["duration"], **sc})
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
    ok.sort(key=lambda r: (r["total_pct"], r["sync_pct"]), reverse=True)
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
    if not (_in_music_dirs(p) or str(p) in ALLOWED_SONGS or _in_uploads(p) or _under(p, EXTRACTED)) or p.suffix.lower() not in AUDIO_EXTS or not p.is_file():
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


_PROXY_POOL = ThreadPoolExecutor(max_workers=1)
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
            return FileResponse(proxy.build(vp))
        except Exception:
            pass           # last resort: the original (the UI shows its "can't preview" message)
    return FileResponse(vp)  # Starlette FileResponse honours Range -> 206


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


@app.get("/api/waveform")
def waveform(path: str):
    p = _safe_song(path)
    song = analyze_song(str(p))
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
    song = analyze_song(str(_safe_song(req.song_path)))
    off = max(MIN_OFFSET_S, min(req.offset, max(0.0, song["duration"] - video["duration"])))
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


def _files_under(base: Path):
    if not base.is_dir():
        return []
    return [p for p in base.rglob("*") if p.is_file() and not p.is_symlink()]


def _uploads_files():
    return _files_under(UPLOADS)


def _bytes(files):
    return sum(p.stat().st_size for p in files)


def _clear_dir(base: Path) -> int:
    """Delete everything inside base (never base itself, never anything resolving outside it)."""
    files = _files_under(base)
    freed = _bytes(files)
    if base.is_dir():
        root = base.resolve()
        for child in list(base.iterdir()):
            if not _under(child.resolve(), root):
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    return freed


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
    uf = _clear_dir(UPLOADS)
    ef = _clear_dir(EXPORTS)
    for k in [k for k in VIDEOS if _in_uploads(Path(k))]:
        VIDEOS.pop(k, None)
    return {"freed": uf + ef, "uploads_freed": uf, "exports_freed": ef}


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
            ALLOWED_VIDEOS.add(str(p)); videos.append(str(p))
        elif ext in AUDIO_EXTS:
            ALLOWED_SONGS.add(str(p)); songs.append(str(p))
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
        video, song = _vid_for(vp), analyze_song(str(sp))
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
    sd = analyze_song(str(sp))["duration"]
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


def _run_export(job_id: str, vp: Path, ap: Path, out: Path):
    job = JOBS[job_id]
    try:
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
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        JOBS[job_id] = {"id": job_id, "status": "running", "stage": "export", "done": 0,
                        "total": 100, "message": "Exporting mp4", "error": None}
    _pool.submit(_run_export, job_id, vp, ap, out)
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
        song = analyze_song(str(sp))
        video = analyze_video(str(vp))
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


@app.get("/static/{name}")
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
        _render._pool.submit(_render._run_render, job, str(vp), str(sp), req.plan, req.interp, out)
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
