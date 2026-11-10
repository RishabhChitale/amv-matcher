"""Render an auto-sync plan into an mp4: time-warped video + rendered song, H.264 + AAC.

render_plan(video_path, plan, audio_wav, out_path, interp="auto", progress_cb=None)

* Video: each OUTPUT frame k (at the source fps) shows source time winv(warp, k/fps). Sped-up
  sections simply skip source frames; slowed sections need in-between frames, made by `interp`:
    "rife"  rife-ncnn-vulkan (official release, downloaded on first use, sha256-pinned) run only
            on the frame pairs of slowed sections (temp PNGs, deleted immediately)
    "blend" linear cross-fade of the two neighbouring source frames
    "dup"   nearest source frame (duplicates)
    "auto"  rife when the binary is present and a 2-frame smoke test passes, else blend
* Audio: song at plan.tempo_ratio (PyAV atempo), segments spliced with media.render_audio
  (crossfades), plan.lead_silence_s of silence at the start. `audio_wav` may be a pre-rendered
  wav; None builds it from `song_path` + the plan.
* Output length = plan.retime.out_duration (or the source duration); audio and video lengths
  agree within one frame.

HTTP contract (include with ONE line in server.py:  `from .render import router as render_router;
app.include_router(render_router)`), jobs polled in the same style as /api/scan/{job_id}:

  POST /api/render-mp4   {"video_path": str, "song_path": str, "plan": {auto_sync plan},
                          "interp": "auto"|"rife"|"blend"|"dup"}   (default "auto")
       -> {"job_id": str}
  GET  /api/render-mp4/{job_id}
       -> {"id","status":"running"|"done"|"error","stage","done":0-100,"interp":<provider used>,
           "path","url","size","seconds","error"}   (url = /api/export-file?path=...)
  GET  /api/render-tools   -> {"rife": {"available", "installed", "tag", "size_mb"}}
  POST /api/render-tools/rife   -> {"job_id"}  (downloads + verifies RIFE; poll the job above)
"""
from __future__ import annotations

import hashlib
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

import av
import numpy as np
import soundfile as sf

from . import paths
from .media import SR, ExportError, render_audio

MIN_FREE_BYTES = 3 * 1024 ** 3
SLOW_EPS = 0.002          # local speed below 1 - eps counts as slowed
RIFE_WORKERS = 3

RIFE_TAG = "20221029"
_BASE = f"https://github.com/nihui/rife-ncnn-vulkan/releases/download/{RIFE_TAG}/"
RIFE_ASSETS = {
    "darwin": {"name": f"rife-ncnn-vulkan-{RIFE_TAG}-macos.zip", "size": 436537917,
               "sha256": "4a63a1f3c9c715773c57d2ee51df1b315ed20cd6c63103e45c483ecc4400b595",
               "exe": "rife-ncnn-vulkan"},
    "win32": {"name": f"rife-ncnn-vulkan-{RIFE_TAG}-windows.zip", "size": 431540241,
              "sha256": "d8e4d772d26cd8006ef0ad0bc82eb191b53c68677d1ae2f42506d74cbbbea606",
              "exe": "rife-ncnn-vulkan.exe"},
}
RIFE_MODEL = "rife-v4.6"   # supports arbitrary timesteps (-s)


class RenderError(ExportError):
    pass


# --------------------------------------------------------------------------- RIFE tool

def tools_dir() -> Path:
    d = Path(paths.CACHE) / "tools"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _asset():
    key = "win32" if sys.platform.startswith("win") else sys.platform
    a = RIFE_ASSETS.get(key)
    if not a:
        raise RenderError(f"RIFE has no official build for this OS ({sys.platform})")
    return a


def rife_dir() -> Path:
    return tools_dir() / "rife"


def rife_exe() -> Optional[Path]:
    try:
        a = _asset()
    except RenderError:
        return None
    for p in rife_dir().rglob(a["exe"]) if rife_dir().exists() else []:
        if p.is_file() and (p.parent / RIFE_MODEL).is_dir():
            return p
    return None


def free_bytes(path) -> int:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def install_rife(progress_cb: Optional[Callable[[float], None]] = None) -> Path:
    """Download the official release zip, verify size + sha256, unpack. Idempotent."""
    exe = rife_exe()
    if exe:
        return exe
    a = _asset()
    need = a["size"] * 2 + MIN_FREE_BYTES
    if free_bytes(tools_dir()) < need:
        raise RenderError("Not enough free disk space to install RIFE")
    import urllib.request
    tmp = tools_dir() / (a["name"] + ".part")
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(_BASE + a["name"], timeout=60) as r, open(tmp, "wb") as f:
            got = 0
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
                h.update(b)
                got += len(b)
                if progress_cb:
                    progress_cb(min(0.95, got / a["size"]))
        if got != a["size"] or h.hexdigest() != a["sha256"]:
            raise RenderError("RIFE download failed verification (size/sha256 mismatch)")
        dest = rife_dir()
        shutil.rmtree(dest, ignore_errors=True)
        with zipfile.ZipFile(tmp) as z:
            for m in z.namelist():                         # no path escapes
                if m.startswith("/") or ".." in Path(m).parts:
                    raise RenderError("Unsafe path in RIFE archive")
            z.extractall(dest)
    finally:
        tmp.unlink(missing_ok=True)
    exe = rife_exe()
    if not exe:
        raise RenderError("RIFE archive did not contain the expected files")
    if not sys.platform.startswith("win"):
        exe.chmod(0o755)
    if sys.platform == "darwin":
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(dest)], capture_output=True)
    if progress_cb:
        progress_cb(1.0)
    return exe


# --------------------------------------------------------------------------- PNG helpers

def _write_png(arr: np.ndarray, path: Path) -> None:
    c = av.open(str(path), "w", format="image2")
    try:
        s = c.add_stream("png", rate=1)
        s.width, s.height, s.pix_fmt = arr.shape[1], arr.shape[0], "rgb24"
        for p in s.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")):
            c.mux(p)
        for p in s.encode(None):
            c.mux(p)
    finally:
        c.close()


def _read_png(path: Path) -> np.ndarray:
    with av.open(str(path)) as c:
        return next(c.decode(video=0)).to_ndarray(format="rgb24")


def _rife_pair(exe: Path, a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    """One interpolated frame at fraction t between a and b; temp PNGs deleted immediately."""
    d = Path(tempfile.mkdtemp(prefix="amv-rife-"))
    try:
        _write_png(a, d / "a.png")
        _write_png(b, d / "b.png")
        r = subprocess.run([str(exe), "-0", str(d / "a.png"), "-1", str(d / "b.png"), "-o", str(d / "o.png"),
                            "-m", str(exe.parent / RIFE_MODEL), "-s", f"{t:.4f}"],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not (d / "o.png").exists():
            raise RenderError("RIFE failed: " + (r.stderr or "")[-200:])
        out = _read_png(d / "o.png")
        if out.shape != a.shape:
            raise RenderError("RIFE returned a frame of the wrong size")
        return out
    finally:
        shutil.rmtree(d, ignore_errors=True)


def rife_works(exe: Optional[Path] = None) -> bool:
    """2-frame smoke test."""
    exe = exe or rife_exe()
    if not exe:
        return False
    try:
        a = np.zeros((64, 64, 3), np.uint8)
        b = np.full((64, 64, 3), 255, np.uint8)
        return _rife_pair(exe, a, b, 0.5).shape == a.shape
    except Exception:
        return False


def resolve_interp(interp: str) -> str:
    if interp not in ("auto", "rife", "blend", "dup"):
        raise RenderError(f"Unknown interp {interp!r}")
    if interp == "auto":
        return "rife" if rife_works() else "blend"
    if interp == "rife" and not rife_works():
        raise RenderError("RIFE is not installed or fails its smoke test")
    return interp


# --------------------------------------------------------------------------- frame plan

def frame_plan(warp, out_duration: float, fps: float, src_frames: int):
    """Per output frame: (i, f, slowed). Source position = i + f in source-frame units."""
    n = int(round(out_duration * fps))
    w = np.asarray(warp, dtype=float)
    t_out = (np.arange(n) + 0.0) / fps
    src_t = np.interp(t_out, w[:, 1], w[:, 0])
    pos = src_t * fps
    speed = np.interp(t_out, (w[:-1, 1] + w[1:, 1]) / 2.0,
                      np.diff(w[:, 0]) / np.maximum(np.diff(w[:, 1]), 1e-9)) if len(w) > 2 else \
        np.full(n, (w[-1, 0] - w[0, 0]) / max(w[-1, 1] - w[0, 1], 1e-9))
    i = np.clip(np.floor(pos + 1e-6).astype(int), 0, src_frames - 1)
    f = np.clip(pos - i, 0.0, 1.0)
    f[i >= src_frames - 1] = 0.0
    return i, f, speed < 1.0 - SLOW_EPS


# --------------------------------------------------------------------------- audio

def _tempo_wav(song_path, ratio: float, out: Path) -> None:
    """Whole song at `ratio` x speed through PyAV's atempo (chained for ratios outside 0.5..2)."""
    g = av.filter.Graph()
    src = g.add_abuffer(sample_rate=SR, format="fltp", layout="stereo", time_base=Fraction(1, SR))
    chain, r = [], ratio
    while r > 2.0 + 1e-9:
        chain.append(2.0)
        r /= 2.0
    while r < 0.5 - 1e-9:
        chain.append(0.5)
        r /= 0.5 * 1.0
    chain.append(r)
    prev = src
    for v in chain:
        n = g.add("atempo", f"{v:.6f}")
        prev.link_to(n)
        prev = n
    sink = g.add("abuffersink")
    prev.link_to(sink)
    g.configure()
    rs = av.AudioResampler(format="fltp", layout="stereo", rate=SR)
    rs_out = av.AudioResampler(format="fltp", layout="stereo", rate=SR)
    chunks = []

    def drain():
        while True:
            try:
                fr = sink.pull()
            except (av.error.BlockingIOError, av.error.EOFError):
                return
            for o in rs_out.resample(fr):
                chunks.append(o.to_ndarray())

    with av.open(str(song_path)) as c:
        st = c.streams.audio[0]
        pts = 0
        for fr in c.decode(st):
            for r_ in rs.resample(fr):
                r_.pts, r_.time_base = pts, Fraction(1, SR)
                pts += r_.samples
                src.push(r_)
                drain()
    src.push(None)
    drain()
    y = np.concatenate(chunks, axis=1) if chunks else np.zeros((2, 0), np.float32)
    sf.write(str(out), y.T, SR, subtype="FLOAT")


def build_audio(song_path, plan: dict, out_duration: float, out_wav: Path) -> None:
    """Rendered song for the plan: tempo, spliced segments with crossfades, lead silence."""
    ratio = float(plan.get("tempo_ratio") or 1.0)
    src = Path(song_path)
    tmp = None
    if abs(ratio - 1.0) > 1e-6:
        tmp = out_wav.with_name(out_wav.stem + ".tempo.wav")
        _tempo_wav(song_path, ratio, tmp)
        src = tmp
    try:
        segs = plan.get("segments")
        if not segs:
            off = float(plan.get("offset") or 0.0)
            lead = max(0.0, -off)                       # negative offset = song starts late
            segs = [{"src_start": max(0.0, off), "src_end": max(0.0, off) + out_duration - lead,
                     "dst_start": lead}]
        render_audio(src, segs, out_duration, out_wav)
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- render

class _Frames:
    """Sequential source frame reader with a small look-behind cache (rgb24 ndarrays)."""

    def __init__(self, path, fps):
        self.c = av.open(str(path))
        self.st = self.c.streams.video[0]
        self.fps = fps
        self.it = self.c.decode(self.st)
        self.cache = {}
        self.last = -1
        self.eof = False
        self.t0 = None

    def _next(self):
        for fr in self.it:
            t = float(fr.pts * fr.time_base) if fr.pts is not None else (self.last + 1) / self.fps
            if self.t0 is None:
                self.t0 = t
            idx = int(round((t - self.t0) * self.fps))
            self.last = idx
            self.cache[idx] = fr.to_ndarray(format="rgb24")
            return True
        self.eof = True
        return False

    def get(self, i):
        while i not in self.cache and not self.eof and self.last < i:
            self._next()
        if i not in self.cache:                      # missing / past the end: nearest available
            keys = [k for k in self.cache if k <= i] or list(self.cache)
            return self.cache[max(keys)]
        return self.cache[i]

    def prune(self, keep_from):
        for k in [k for k in self.cache if k < keep_from]:
            del self.cache[k]

    def close(self):
        self.c.close()


def render_plan(video_path, plan: dict, audio_wav, out_path, interp: str = "auto",
                progress_cb: Optional[Callable[[float], None]] = None, song_path=None) -> dict:
    """Render the plan to out_path (mp4). Returns {"interp","frames","interpolated","out_duration",
    "seconds"}. audio_wav=None builds the audio from song_path + plan."""
    t_start = time.time()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if free_bytes(out_path.parent) < MIN_FREE_BYTES:
        raise RenderError("Less than 3 GB of free disk space; refusing to render")
    mode = resolve_interp(interp)
    exe = rife_exe() if mode == "rife" else None

    with av.open(str(video_path)) as vin:
        if not vin.streams.video:
            raise RenderError("The video has no video stream")
        vs_in = vin.streams.video[0]
        rate = vs_in.average_rate or vs_in.guessed_rate or Fraction(30)
        fps = float(rate)
        width, height = vs_in.width - vs_in.width % 2, vs_in.height - vs_in.height % 2
        src_dur = float(vin.duration / av.time_base) if vin.duration else (vs_in.frames or 0) / fps
        src_frames = vs_in.frames or int(round(src_dur * fps))
    rt = plan.get("retime") or {}
    warp = rt.get("warp") or [[0.0, 0.0], [src_dur, src_dur]]
    out_dur = float(rt.get("out_duration") or warp[-1][1])
    i_arr, f_arr, slow = frame_plan(warp, out_dur, fps, max(1, src_frames))
    n_out = len(i_arr)
    if n_out < 1:
        raise RenderError("Nothing to render")

    tmp_dir = Path(tempfile.mkdtemp(prefix="amv-render-", dir=out_path.parent))
    try:
        wav = Path(audio_wav) if audio_wav else tmp_dir / "audio.wav"
        if not audio_wav:
            if not song_path:
                raise RenderError("Need audio_wav or song_path")
            build_audio(song_path, plan, out_dur, wav)
        data, a_sr = sf.read(str(wav), dtype="float32", always_2d=True)
        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        want = int(round(n_out / fps * a_sr))            # audio matches the video frame count
        if len(data) < want:
            data = np.vstack([data, np.zeros((want - len(data), data.shape[1]), np.float32)])
        data = data[:want]

        tmp = out_path.with_name(out_path.stem + ".tmp.mp4")
        out = av.open(str(tmp), "w", format="mp4")
        vs = out.add_stream("libx264", rate=rate)
        vs.width, vs.height, vs.pix_fmt = width, height, "yuv420p"
        vs.options = {"preset": "veryfast", "crf": "20"}
        ast = out.add_stream("aac", rate=a_sr)
        ast.layout, ast.bit_rate = "stereo", 192000
        apk = []
        rs = av.AudioResampler(format="fltp", layout="stereo", rate=a_sr, frame_size=1024)
        fr = av.AudioFrame.from_ndarray(np.ascontiguousarray(data[:, :2]).reshape(1, -1),
                                        format="flt", layout="stereo")
        fr.sample_rate = a_sr
        for r in rs.resample(fr) + rs.resample(None):
            apk.extend(ast.encode(r))
        apk.extend(ast.encode(None))
        ai = 0

        def vtime(p):
            t = p.dts if p.dts is not None else p.pts
            return float(t * p.time_base) if t is not None else 0.0

        def send(p):
            nonlocal ai
            while ai < len(apk) and vtime(apk[ai]) <= vtime(p):
                out.mux(apk[ai])
                ai += 1
            if p.dts is not None:
                out.mux(p)

        reader = _Frames(video_path, fps)
        pool = ThreadPoolExecutor(RIFE_WORKERS) if mode == "rife" else None
        n_interp = 0
        try:
            BATCH = 24
            for b0 in range(0, n_out, BATCH):
                idx = range(b0, min(n_out, b0 + BATCH))
                frames, jobs = {}, {}
                for k in idx:
                    i, f = int(i_arr[k]), float(f_arr[k])
                    interp_here = mode != "dup" and bool(slow[k]) and 0.04 < f < 0.96
                    if not interp_here:
                        frames[k] = reader.get(i + (1 if f >= 0.5 else 0))
                        continue
                    a, b = reader.get(i), reader.get(i + 1)
                    n_interp += 1
                    if mode == "rife":
                        jobs[k] = pool.submit(_rife_pair, exe, a, b, round(f, 3))
                    else:
                        frames[k] = ((1 - f) * a.astype(np.float32) + f * b.astype(np.float32) + 0.5).astype(np.uint8)
                for k, j in jobs.items():
                    frames[k] = j.result()
                for k in idx:
                    arr = frames[k][:height, :width]
                    vf = av.VideoFrame.from_ndarray(np.ascontiguousarray(arr), format="rgb24")
                    vf.pts = k
                    vf.time_base = Fraction(1) / rate
                    for p in vs.encode(vf):
                        send(p)
                reader.prune(int(i_arr[idx[-1]]) - 1)
                if progress_cb:
                    progress_cb(min(0.99, (idx[-1] + 1) / n_out))
            for p in vs.encode(None):
                send(p)
            while ai < len(apk):
                out.mux(apk[ai])
                ai += 1
        finally:
            reader.close()
            if pool:
                pool.shutdown(wait=True)
            out.close()
        os.replace(tmp, out_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        out_path.with_name(out_path.stem + ".tmp.mp4").unlink(missing_ok=True)
    if progress_cb:
        progress_cb(1.0)
    return {"interp": mode, "frames": n_out, "interpolated": n_interp, "out_duration": n_out / fps,
            "seconds": round(time.time() - t_start, 2)}


# --------------------------------------------------------------------------- HTTP router

JOBS: dict = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(1)


def _upd(job, **kw):
    with _lock:
        job.update(kw)


def _run_render(job, video_path, song_path, plan, interp, out):
    try:
        info = render_plan(video_path, plan, None, out, interp=interp, song_path=song_path,
                           progress_cb=lambda f: _upd(job, done=int(f * 100)))
        _upd(job, status="done", done=100, path=str(out), size=Path(out).stat().st_size,
             interp=info["interp"], seconds=info["seconds"],
             url=f"/api/export-file?path={out}")
    except Exception as e:                                # noqa: BLE001
        _upd(job, status="error", error=str(e))


def _run_install(job):
    try:
        install_rife(lambda f: _upd(job, done=int(f * 100)))
        _upd(job, status="done", done=100)
    except Exception as e:                                # noqa: BLE001
        _upd(job, status="error", error=str(e))


def _make_router():
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel

    class RenderRequest(BaseModel):
        video_path: str
        song_path: str
        plan: dict
        interp: str = "auto"

    r = APIRouter()

    def _new(stage):
        job = {"id": uuid.uuid4().hex[:12], "status": "running", "stage": stage, "done": 0}
        JOBS[job["id"]] = job
        return job

    @r.post("/api/render-mp4")
    def render_mp4(req: RenderRequest):
        for p in (req.video_path, req.song_path):
            if not Path(p).is_file():
                raise HTTPException(404, f"File not found: {p}")
        out = paths.EXPORTS / (Path(req.video_path).stem + "-synced.mp4")
        job = _new("render")
        _pool.submit(_run_render, job, req.video_path, req.song_path, req.plan, req.interp, out)
        return {"job_id": job["id"]}

    @r.get("/api/render-mp4/{job_id}")
    def render_status(job_id: str):
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Unknown job")
        return JSONResponse(job)

    @r.get("/api/render-tools")
    def render_tools():
        a = RIFE_ASSETS.get("win32" if sys.platform.startswith("win") else sys.platform)
        return {"rife": {"available": bool(a), "installed": rife_exe() is not None, "tag": RIFE_TAG,
                         "size_mb": round(a["size"] / 1e6) if a else None}}

    @r.post("/api/render-tools/rife")
    def install_tool():
        job = _new("install")
        _pool.submit(_run_install, job)
        return {"job_id": job["id"]}

    return r


try:
    router = _make_router()
except ImportError:          # fastapi absent (tests of the pure functions)
    router = None
