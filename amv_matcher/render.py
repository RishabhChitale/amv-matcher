"""Render an auto-sync plan into an mp4: time-warped video + rendered song, H.264 + AAC.

render_plan(video_path, plan, audio_wav, out_path, interp="auto", progress_cb=None)

* Video: each OUTPUT frame k (at the source fps) shows source time winv(warp, k/fps). Sped-up
  sections simply skip source frames; slowed sections need in-between frames, made by `interp`:
    "rife"  rife-ncnn-vulkan (official release, downloaded on first use, sha256-pinned) run only
            on the frame pairs of slowed sections (temp PNGs, deleted immediately)
    "blend" linear cross-fade of the two neighbouring source frames
    "dup"   nearest source frame (duplicates)
    "auto"  = "blend" (fast, the default). RIFE runs ONLY when interp="rife" is chosen explicitly.
* Audio: song at plan.tempo_ratio (PyAV atempo), segments spliced with media.render_audio
  (crossfades), plan.lead_silence_s of silence at the start. `audio_wav` may be a pre-rendered
  wav; None builds it from `song_path` + the plan.
* Output length = plan.retime.out_duration (or the source duration); audio and video lengths
  agree within one frame.

HTTP contract (include with ONE line in server.py:  `from .render import router as render_router;
app.include_router(render_router)`), jobs polled in the same style as /api/scan/{job_id}:

  POST /api/render-mp4   {"video_path": str, "song_path": str, "plan": {auto_sync plan},
                          "interp": "auto"|"rife"|"blend"|"dup",   (default "auto" == "blend")
                          "gpu_id": int|null}   (RIFE only; null = auto-pick the discrete GPU)
       -> {"job_id": str}
  GET  /api/render-mp4/{job_id}
       -> {"id","status":"running"|"done"|"error"|"cancelled","stage","done":0-100,
           "interp":<provider used>,"gpu":<RIFE device name or null>,
           "path","url","size","seconds","error"}   (url = /api/export-file?path=...)
  POST /api/render-mp4/{job_id}/cancel   -> {"ok": true, "status"}  kills the RIFE processes, stops
       at the next frame boundary, removes all temp files; status becomes "cancelled"; no partial
       output file is left (an earlier finished render of the same name is not touched). Also
       cancels an install job.
  POST /api/render-estimate  {"video_path": str, "plan": {...}, "interp": "auto"|..., "measure": false}
       -> {"frames","interpolated","est_seconds","estimated": bool,"interp": <resolved mode>,
           "per_frame_s", "gpu", "calibration_s"?, "reason"?}   (POST because the plan is a large body.
       A missing per-machine cost (GPU + resolution for rife; resolution for blend/base) is calibrated
       automatically on the first estimate (rife: 2 pairs at the target resolution; blend: a few real
       frames; hard timeout 8 s), cached in <cache>/tools/render_cost.json and reported with
       "estimated": false. "measure": true forces a re-calibration. If calibration fails, pessimistic
       constants (rife 1.2 s/frame at 1080p, scaled by pixels) give "estimated": true + "reason".)
  GET  /api/render-tools   -> {"rife": {"available", "installed", "tag", "size_mb"}}
  POST /api/render-tools/rife   -> {"job_id"}  (downloads + verifies RIFE; poll the job above; the
       zip is deleted after verification + extraction, only the binary, the rife-v4.6 model and
       the licence stay; a marker file prevents a re-download)

GPU: RIFE lists its Vulkan devices on startup (-v); list_vulkan_devices() parses them and
pick_gpu_order() ranks discrete > integrated (Intel UHD/Iris/HD) > software. The first device is
used; if it crashes (MoltenVK) the next one is tried and the fallback is reported in the job's "gpu".
Override with the gpu_id parameter or the AMV_RIFE_GPU env var (-1 = CPU). AMV_RIFE_J overrides -j.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
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

from . import paths, winsafe
from .media import SR, ExportError, render_audio

MIN_FREE_BYTES = 3 * 1024 ** 3
LEAKED_TEMP: list = []     # temp paths that could not be removed (locked on Windows); reported, not hidden


def _unsafe_zip_member(m: str) -> bool:
    """True for archive entries that could escape the extraction folder, on any OS (W-10)."""
    from pathlib import PurePosixPath, PureWindowsPath
    if not m or m.startswith(("/", "\\")):
        return True
    w = PureWindowsPath(m)
    if w.anchor or w.drive or ".." in w.parts or ".." in PurePosixPath(m).parts:
        return True
    return False


def _long_path(p) -> str:
    """Windows: \\?\\-prefixed absolute path when it exceeds the 260-char MAX_PATH (len > 240 leaves room for
    a file name); unchanged elsewhere / when short."""
    s = os.path.abspath(str(p))
    if not winsafe._is_win() or len(s) <= 240 or s.startswith("\\\\?\\"):
        return s
    return "\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s


def _extract_member(z, m: str, dest) -> None:
    """Extract one zip member under dest (members were checked by _unsafe_zip_member); long-path safe on Windows."""
    if m.endswith("/"):
        os.makedirs(_long_path(Path(dest) / m), exist_ok=True)
        return
    target = Path(dest) / m
    os.makedirs(_long_path(target.parent), exist_ok=True)
    with z.open(m) as src, open(_long_path(target), "wb") as out:
        shutil.copyfileobj(src, out)


def _cleanup_dir(d, root=None) -> bool:
    """Remove a temp dir with retries; if files stay locked, record and warn instead of leaking silently (W-09)."""
    d = Path(d)
    root = root or d.parent
    ok = winsafe.safe_rmtree(d, root)
    if not ok and d.exists():
        LEAKED_TEMP.append(str(d))
        import logging
        logging.getLogger(__name__).warning("could not remove temp dir (in use?): %s", d)
    return ok


def _cleanup_file(p, root=None) -> bool:
    p = Path(p)
    ok = winsafe.safe_unlink(p, root or p.parent)
    if not ok and p.exists():
        LEAKED_TEMP.append(str(p))
        import logging
        logging.getLogger(__name__).warning("could not remove temp file (in use?): %s", p)
    return ok
SLOW_EPS = 0.002          # local speed below 1 - eps counts as slowed
RIFE_WORKERS = 3
RIFE_J = "1:2:2"          # -j load:proc:save per RIFE process (tuned on the discrete GPU, see report)
# first-use default costs at 1920x1080 (seconds); scaled by pixel count until measured
DEFAULT_COST_1080 = {"base": 0.05, "blend": 0.03, "rife": 1.2}      # pessimistic fallback (never undersell)
CALIBRATION_TIMEOUT = 8.0

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


class RenderCancelled(RenderError):
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


_MARKER = ".amv-rife.json"


def _wanted_member(name: str, exe_name: str) -> bool:
    parts = Path(name).parts
    return (len(parts) >= 2 and (parts[-1] == exe_name and len(parts) == 2 or
                                 parts[1] == RIFE_MODEL or parts[-1] == "LICENSE" and len(parts) == 2))


def installed_size_bytes() -> int:
    d = rife_dir()
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.exists() else 0


def prune_rife() -> int:
    """Keep only the binary, the rife-v4.6 model dir, LICENSE and the marker. Returns bytes freed."""
    exe = rife_exe()
    if not exe:
        return 0
    root, before = exe.parent, installed_size_bytes()
    marker = root.parent / _MARKER                    # marker sits beside the extracted folder
    if not marker.exists():
        a = _asset()
        marker.write_text(json.dumps({"tag": RIFE_TAG, "name": a["name"], "sha256": a["sha256"],
                                      "size": a["size"], "verified": True}))
    for p in list(root.iterdir()):
        if p.name in (exe.name, RIFE_MODEL, "LICENSE", _MARKER):
            continue
        _cleanup_dir(p, root) if p.is_dir() else _cleanup_file(p, root)
    return before - installed_size_bytes()


def install_rife(progress_cb: Optional[Callable[[float], None]] = None) -> Path:
    """Download the official release zip, verify size + sha256 BEFORE touching anything, extract only
    the binary + rife-v4.6 model + LICENSE, then delete the zip. A marker file records the verified
    sha256 so the zip is never downloaded again. Idempotent."""
    exe = rife_exe()
    if exe:
        prune_rife()
        return exe
    a = _asset()
    need = a["size"] + MIN_FREE_BYTES
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
        if got != a["size"] or h.hexdigest() != a["sha256"]:          # verify BEFORE anything else
            raise RenderError("RIFE download failed verification (size/sha256 mismatch)")
        dest = rife_dir()
        _cleanup_dir(dest, dest.parent)
        with zipfile.ZipFile(tmp) as z:
            for m in z.namelist():                         # no path escapes
                if _unsafe_zip_member(m):
                    raise RenderError("Unsafe path in RIFE archive")
            for m in z.namelist():
                if _wanted_member(m, a["exe"]):
                    _extract_member(z, m, dest)
        (dest / _MARKER).write_text(json.dumps({"tag": RIFE_TAG, "name": a["name"], "sha256": a["sha256"],
                                                "size": a["size"], "verified": True}))
    finally:
        _cleanup_file(tmp)                                 # the zip is not kept
    exe = rife_exe()
    if not exe:
        raise RenderError("RIFE archive did not contain the expected files")
    if not sys.platform.startswith("win"):
        exe.chmod(0o755)
    if sys.platform == "darwin":
        winsafe.run(["xattr", "-dr", "com.apple.quarantine", str(dest)], capture_output=True)
    if progress_cb:
        progress_cb(1.0)
    return exe


# --------------------------------------------------------------------------- GPU choice

_DEV_RE = re.compile(r"^\[(\d+) (.+?)\]\s+queueC", re.M)
_INTEGRATED = re.compile(r"intel.*(uhd|iris|hd graphics|graphics)|\bapu\b|radeon\(tm\) graphics|vega \d+ graphics", re.I)
_SOFTWARE = re.compile(r"llvmpipe|swiftshader|software|basic render|lavapipe|cpu", re.I)


def parse_vulkan_devices(text: str) -> list:
    """Parse rife-ncnn-vulkan's -v startup lines '[0 AMD Radeon Pro 5300M]  queueC=...'."""
    seen, out = set(), []
    for m in _DEV_RE.finditer(text or ""):
        i = int(m.group(1))
        if i not in seen:
            seen.add(i)
            out.append({"id": i, "name": m.group(2).strip()})
    return out


def device_class(name: str) -> int:
    """0 = discrete, 1 = integrated, 2 = software."""
    if _SOFTWARE.search(name):
        return 2
    return 1 if _INTEGRATED.search(name) else 0


def pick_gpu_order(devices: list, override: Optional[int] = None) -> list:
    """Device ids to try, best first: discrete, then integrated, then software; ties by id.
    `override` (or env AMV_RIFE_GPU) forces that id first (-1 = CPU) and still falls back."""
    ranked = [d["id"] for d in sorted(devices, key=lambda d: (device_class(d["name"]), d["id"]))]
    if override is None:
        env = os.environ.get("AMV_RIFE_GPU", "").strip()
        override = int(env) if re.fullmatch(r"-?\d+", env) else None
    if override is not None:
        ranked = [override] + [i for i in ranked if i != override]
    return ranked


_DEVICES: dict = {}


def list_vulkan_devices(exe: Optional[Path] = None) -> list:
    exe = exe or rife_exe()
    if not exe:
        return []
    key = str(exe)
    if key not in _DEVICES:
        d = Path(tempfile.mkdtemp(prefix="amv-rife-"))
        try:
            _write_png(np.zeros((32, 32, 3), np.uint8), d / "a.png")
            _write_png(np.full((32, 32, 3), 255, np.uint8), d / "b.png")
            r = winsafe.run([str(exe), "-v", "-0", str(d / "a.png"), "-1", str(d / "b.png"), "-o",
                                str(d / "o.png"), "-m", str(exe.parent / RIFE_MODEL)],
                               capture_output=True, text=True, timeout=120)
            _DEVICES[key] = parse_vulkan_devices((r.stderr or "") + (r.stdout or ""))
        except Exception:                                  # noqa: BLE001
            _DEVICES[key] = []
        finally:
            _cleanup_dir(d, tempfile.gettempdir())
    return _DEVICES[key]


def gpu_name(devices: list, gid: Optional[int]) -> Optional[str]:
    if gid == -1:
        return "cpu"
    return next((d["name"] for d in devices if d["id"] == gid), None)


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


class RifeRunner:
    """Runs rife-ncnn-vulkan once per frame pair on the best GPU; on a crash it moves to the next
    device (MoltenVK failures happen). Thread safe; kill_all() aborts running processes."""

    def __init__(self, exe: Path, gpu_id: Optional[int] = None, cancel: Optional[threading.Event] = None):
        self.exe, self.cancel = Path(exe), cancel
        self.devices = list_vulkan_devices(self.exe)
        self.order = pick_gpu_order(self.devices, gpu_id) or [None]     # None = rife's own default
        self.pos = 0
        self.failed: list = []                                         # [(gpu id, reason)]
        self.lock = threading.Lock()
        self.procs: set = set()
        self.j = os.environ.get("AMV_RIFE_J") or RIFE_J

    @property
    def gpu(self):
        return self.order[min(self.pos, len(self.order) - 1)]

    @property
    def gpu_label(self) -> Optional[str]:
        return gpu_name(self.devices, self.gpu) if self.gpu is not None else None

    def kill_all(self):
        with self.lock:
            procs = list(self.procs)
        for p in procs:
            try:
                winsafe.kill_tree(p)
            except Exception:                                          # noqa: BLE001
                pass

    def _once(self, d: Path, t: float, gid) -> None:
        cmd = [str(self.exe), "-0", str(d / "a.png"), "-1", str(d / "b.png"), "-o", str(d / "o.png"),
               "-m", str(self.exe.parent / RIFE_MODEL), "-s", f"{t:.4f}", "-j", self.j]
        if gid is not None:
            cmd += ["-g", str(gid)]
        _cleanup_file(d / "o.png", d)
        kw = {} if winsafe._is_win() else {"start_new_session": True}   # POSIX: own group so the tree can be killed
        p = winsafe.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)
        with self.lock:
            self.procs.add(p)
        try:
            try:
                _, err = p.communicate(timeout=300)
            except subprocess.TimeoutExpired:
                winsafe.kill_tree(p)
                p.communicate()
                raise RenderError("RIFE timed out")
        finally:
            with self.lock:
                self.procs.discard(p)
        if self.cancel is not None and self.cancel.is_set():
            raise RenderCancelled("cancelled")
        if p.returncode != 0 or not (d / "o.png").exists():
            raise RenderError(f"RIFE failed (code {p.returncode}): " + (err or "")[-200:])

    def pair(self, a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
        d = Path(tempfile.mkdtemp(prefix="amv-rife-"))
        try:
            _write_png(a, d / "a.png")
            _write_png(b, d / "b.png")
            while True:
                if self.cancel is not None and self.cancel.is_set():
                    raise RenderCancelled("cancelled")
                gid = self.gpu
                try:
                    self._once(d, t, gid)
                    break
                except RenderCancelled:
                    raise
                except RenderError as e:
                    with self.lock:
                        if self.order[min(self.pos, len(self.order) - 1)] == gid:   # first to notice
                            self.failed.append((gid, str(e)[:120]))
                            if self.pos + 1 >= len(self.order):
                                raise RenderError("RIFE failed on every GPU: " + str(e)[:160])
                            self.pos += 1
            out = _read_png(d / "o.png")
            if out.shape != a.shape:
                raise RenderError("RIFE returned a frame of the wrong size")
            return out
        finally:
            _cleanup_dir(d, tempfile.gettempdir())


def rife_works(exe: Optional[Path] = None, gpu_id: Optional[int] = None) -> bool:
    """2-frame smoke test."""
    exe = exe or rife_exe()
    if not exe:
        return False
    try:
        a = np.zeros((64, 64, 3), np.uint8)
        b = np.full((64, 64, 3), 255, np.uint8)
        return RifeRunner(exe, gpu_id).pair(a, b, 0.5).shape == a.shape
    except Exception:
        return False


def resolve_interp(interp: str, gpu_id: Optional[int] = None) -> str:
    """"auto" is blend (fast); RIFE only when asked for explicitly."""
    if interp not in ("auto", "rife", "blend", "dup"):
        raise RenderError(f"Unknown interp {interp!r}")
    if interp == "auto":
        return "blend"
    if interp == "rife" and not rife_works(gpu_id=gpu_id):
        raise RenderError("RIFE is not installed or fails its smoke test")
    return interp


# --------------------------------------------------------------------------- cost / estimate

def _cost_file() -> Path:
    return tools_dir() / "render_cost.json"


def _load_costs() -> dict:
    try:
        return json.loads(_cost_file().read_text())
    except Exception:                                      # noqa: BLE001
        return {}


def _save_cost(kind: str, key: str, value: float) -> None:
    try:
        c = _load_costs()
        c.setdefault(kind, {})[key] = round(float(value), 5)
        _cost_file().write_text(json.dumps(c, indent=1))
    except Exception:                                      # noqa: BLE001
        pass


def _cost(kind: str, w: int, h: int, gpu: Optional[str] = None):
    """(seconds, cached?) for 'base' (per output frame), 'blend' / 'rife' (extra per interpolated
    frame) at this resolution (and GPU for rife). Falls back to pessimistic constants."""
    key = f"{w}x{h}" + (f"|{gpu}" if kind == "rife" and gpu else "")
    v = _load_costs().get(kind, {}).get(key)
    if v is not None:
        return float(v), True
    return DEFAULT_COST_1080[kind] * (w * h) / (1920 * 1080), False


def _run_with_timeout(fn, timeout: float, on_timeout: Optional[Callable] = None):
    """Run fn() in a daemon thread; raise RenderError when it takes longer than `timeout`."""
    box: dict = {}

    def work():
        try:
            box["v"] = fn()
        except BaseException as e:                         # noqa: BLE001
            box["e"] = e

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        if on_timeout:
            try:
                on_timeout()
            except Exception:                              # noqa: BLE001
                pass
        raise RenderError(f"calibration timed out after {timeout:.0f}s")
    if "e" in box:
        raise box["e"]
    return box["v"]


def calibrate_blend_cost(w: int, h: int, n: int = 6) -> dict:
    """~1 s: time n frames through the real paths. base = H.264 encode (render settings) + frame
    conversion per output frame; blend = the float32 cross-fade of two frames. Cached per resolution."""
    rng = np.random.default_rng(1)
    a = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
    b = np.roll(a, 5, axis=1)
    t0 = time.time()
    for k in range(n):
        f = 0.2 + 0.1 * k
        ((1 - f) * a.astype(np.float32) + f * b.astype(np.float32) + 0.5).astype(np.uint8)
    blend = (time.time() - t0) / n
    tmp = Path(tempfile.mkdtemp(prefix="amv-cal-"))
    try:
        t0 = time.time()
        with av.open(str(tmp / "c.mp4"), "w") as out:
            vs = out.add_stream("libx264", rate=30)
            vs.width, vs.height, vs.pix_fmt = w, h, "yuv420p"
            vs.options = {"preset": "veryfast", "crf": "20"}
            for k in range(n):
                fr = av.VideoFrame.from_ndarray(a if k % 2 else b, format="rgb24")
                for pk in vs.encode(fr):
                    out.mux(pk)
            for pk in vs.encode():
                out.mux(pk)
        base = (time.time() - t0) / n
    finally:
        _cleanup_dir(tmp, tempfile.gettempdir())
    _save_cost("blend", f"{w}x{h}", blend)
    _save_cost("base", f"{w}x{h}", base)
    return {"base": base, "blend": blend}


def _probe(video_path):
    with av.open(str(video_path)) as vin:
        vs = vin.streams.video[0]
        rate = vs.average_rate or vs.guessed_rate or Fraction(30)
        fps = float(rate)
        dur = float(vin.duration / av.time_base) if vin.duration else (vs.frames or 0) / fps
        return {"fps": fps, "rate": rate, "w": vs.width - vs.width % 2, "h": vs.height - vs.height % 2,
                "dur": dur, "frames": vs.frames or int(round(dur * fps))}


def _interp_mask(f, slow):
    return slow & (f > 0.04) & (f < 0.96)


def measure_rife_cost(exe: Optional[Path] = None, w: int = 1920, h: int = 1080, n: int = 6,
                      gpu_id: Optional[int] = None, workers: Optional[int] = None,
                      timeout: Optional[float] = None) -> dict:
    """Time n synthetic frame pairs through RIFE (`workers` in parallel) and store s/frame.
    With `timeout` the RIFE processes are killed and RenderError raised when it takes longer."""
    exe = exe or rife_exe()
    if not exe:
        raise RenderError("RIFE is not installed")
    if free_bytes(tempfile.gettempdir()) < MIN_FREE_BYTES:
        raise RenderError("Less than 3 GB of free disk space; skipping calibration")
    rng = np.random.default_rng(0)
    a = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
    b = np.roll(a, 7, axis=1)
    runner = RifeRunner(exe, gpu_id)

    def run():
        runner.pair(a[:64, :64], b[:64, :64], 0.5)                   # warm-up / device pick
        t0 = time.time()
        with ThreadPoolExecutor(workers or RIFE_WORKERS) as pool:
            list(pool.map(lambda k: runner.pair(a, b, 0.2 + 0.05 * k), range(n)))
        return (time.time() - t0) / n

    per = _run_with_timeout(run, timeout, runner.kill_all) if timeout else run()
    _save_cost("rife", f"{w}x{h}|{runner.gpu_label}", per)
    return {"s_per_frame": per, "gpu": runner.gpu_label, "failed": runner.failed}


def estimate_render(plan: dict, interp: str, video, measure: bool = False,
                    gpu_id: Optional[int] = None) -> dict:
    """{frames, interpolated, est_seconds, estimated, ...} without rendering anything.

    A missing cost (this GPU + resolution for rife; this resolution for base/blend) is calibrated
    automatically (rife: 2 pairs at the target resolution; blend/base: a few real frames), cached in
    <cache>/tools/render_cost.json and reported with estimated=False. measure=True forces a fresh
    calibration. Every calibration has a hard timeout (CALIBRATION_TIMEOUT); when it fails the
    pessimistic constants are used and the result has estimated=True plus a "reason"."""
    mode = interp if interp != "auto" else "blend"
    if mode not in ("rife", "blend", "dup"):
        raise RenderError(f"Unknown interp {interp!r}")
    pr = _probe(video)
    rt = plan.get("retime") or {}
    warp = rt.get("warp") or [[0.0, 0.0], [pr["dur"], pr["dur"]]]
    out_dur = float(rt.get("out_duration") or warp[-1][1])
    i, f, slow = frame_plan(warp, out_dur, pr["fps"], max(1, pr["frames"]))
    n_out = len(i)
    n_int = 0 if mode == "dup" else int(_interp_mask(f, slow).sum())
    w, h = pr["w"], pr["h"]
    gname, reasons, t_cal = None, [], 0.0
    exe = rife_exe() if mode == "rife" else None
    if mode == "rife":
        if exe:
            devs = list_vulkan_devices(exe)
            order = pick_gpu_order(devs, gpu_id)
            gname = gpu_name(devs, order[0] if order else None)
        else:
            reasons.append("RIFE is not installed")
    base, m_base = _cost("base", w, h)
    if measure or not m_base or (mode == "blend" and not _cost("blend", w, h)[1]):
        t0 = time.time()
        try:
            _run_with_timeout(lambda: calibrate_blend_cost(w, h), CALIBRATION_TIMEOUT)
        except Exception as e:                             # noqa: BLE001
            reasons.append(f"blend calibration failed: {e}")
        t_cal += time.time() - t0
        base, m_base = _cost("base", w, h)
    extra, m_extra = (0.0, True) if mode == "dup" else _cost(mode, w, h, gname)
    if mode == "rife" and exe and n_int and (measure or not m_extra):
        t0 = time.time()
        try:
            got = measure_rife_cost(exe, w, h, n=2, gpu_id=gpu_id, workers=2, timeout=CALIBRATION_TIMEOUT)
            gname = got["gpu"]
        except Exception as e:                             # noqa: BLE001
            reasons.append(f"RIFE calibration failed: {e}")
        t_cal += time.time() - t0
        extra, m_extra = _cost("rife", w, h, gname)
    elif mode == "blend" and n_int:
        extra, m_extra = _cost("blend", w, h)
    if n_int == 0:
        m_extra = True
    out = {"frames": n_out, "interpolated": n_int, "interp": mode, "gpu": gname,
           "est_seconds": math.ceil((n_out * base + n_int * extra) * 1000) / 1000,   # round UP, never undersell
           "per_frame_s": round(base + (n_int / max(n_out, 1)) * extra, 4),
           "estimated": not (m_base and m_extra)}
    if t_cal:
        out["calibration_s"] = round(t_cal, 2)
    if out["estimated"]:
        out["reason"] = "; ".join(reasons) or "no cached cost"
    return out


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

def _tempo_wav(song_path, ratio: float, out: Path, max_in_s: Optional[float] = None) -> None:
    """Song at `ratio` x speed through PyAV's atempo (chained for ratios outside 0.5..2). Only the first
    `max_in_s` input seconds are processed when given (an excerpt must not tempo a whole long track)."""
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
            if max_in_s is not None and pts / SR >= max_in_s:
                break
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
        ends = [float(g["src_end"]) for g in (plan.get("segments") or []) if "src_end" in g]
        if not ends:
            off = float(plan.get("offset") or 0.0)
            ends = [max(0.0, off) + out_duration]
        _tempo_wav(song_path, ratio, tmp, max_in_s=max(ends) * ratio + 2.0)   # segments are on the tempo'd timeline
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
            _cleanup_file(tmp)


# --------------------------------------------------------------------------- boom / pump effect

PUMP_DEFAULTS = {"boom": 0.0, "decay": 180.0, "drops_only": False, "shake": False}


def pump_params(d: Optional[dict]) -> dict:
    """Validate/clamp the Boom params: boom 0-100 (default 0 = off), decay 50-400 ms (default 180),
    drops_only / shake bool. Other keys (e.g. "song") pass through."""
    d = dict(d or {})

    def num(k, lo, hi):
        try:
            v = float(d.get(k, PUMP_DEFAULTS[k]))
        except (TypeError, ValueError):
            v = PUMP_DEFAULTS[k]
        if v != v:
            v = PUMP_DEFAULTS[k]
        return min(hi, max(lo, v))
    d["boom"], d["decay"] = num("boom", 0.0, 100.0), num("decay", 50.0, 400.0)
    d["drops_only"], d["shake"] = bool(d.get("drops_only", False)), bool(d.get("shake", False))
    return d


def output_song_events(song: dict, plan: dict, out_duration: float) -> dict:
    """Song analysis (beat_times, beat_strengths, drops) in OUTPUT time, mapped as the audio is built:
    tempo ratio r (time / r), then the plan's segments (src_start..src_end -> dst_start, same selection
    and shift as splice._assemble; src_start < 0 = lead silence) or, without segments, the plain offset
    (negative = song starts late)."""
    r = float(plan.get("tempo_ratio") or 1.0) or 1.0
    bt = np.asarray(song.get("beat_times", []), dtype=float) / r
    bs = song.get("beat_strengths")
    bs = np.asarray(bs, dtype=float) if bs is not None and len(bs) == len(bt) else np.ones(len(bt))
    dr = np.asarray(song.get("drops", []), dtype=float) / r
    segs = plan.get("segments")
    if not segs:
        off = float(plan.get("offset") or 0.0)
        lead = max(0.0, -off)
        segs = [{"src_start": max(0.0, off), "src_end": max(0.0, off) + out_duration - lead, "dst_start": lead}]
    beats, strengths, drops = [], [], []
    for g in segs:
        a, b, d0 = float(g["src_start"]), float(g["src_end"]), float(g["dst_start"])
        sel = (bt >= a) & (bt < b)
        beats += list(bt[sel] - a + d0)
        strengths += list(bs[sel])
        drops += list(dr[(dr >= a) & (dr < b)] - a + d0)
    order = np.argsort(beats, kind="stable") if len(beats) else []
    return {"beat_times": [beats[i] for i in order], "beat_strengths": [strengths[i] for i in order],
            "drops": sorted(drops), "duration": out_duration}


def pump_track(pump: Optional[dict], plan: dict, out_duration: float, fps: float, n_out: int):
    """Per output frame k: (scale, brightness, dx, dy) from pump.pump_keyframes on the output-time song
    (t = output time, clip-relative), or None when the effect is off (boom <= 0)."""
    if not pump:
        return None
    pp = pump_params(pump)
    if pp["boom"] <= 0:
        return None
    if not pp.get("song"):
        raise RenderError("Boom needs the song analysis")
    from . import pump as P
    ev = output_song_events(pp["song"], plan, out_duration)
    kf = P.pump_keyframes(ev, boom=pp["boom"], decay_ms=pp["decay"], drops_only=pp["drops_only"],
                          fps=fps, duration=n_out / fps, shake=pp["shake"])
    return [(k["scale"], k["brightness"], k["dx"], k["dy"]) for k in kf]


def apply_pump(arr: np.ndarray, scale: float, bright: float, dx: float = 0.0, dy: float = 0.0) -> np.ndarray:
    """Scale about the centre (frame size kept), shift by dx,dy px, multiply brightness.
    Returns `arr` itself when all are neutral."""
    if scale == 1.0 and bright == 1.0 and not dx and not dy:
        return arr
    out = arr
    if scale != 1.0 or dx or dy:
        h, w = arr.shape[:2]
        M = np.float32([[scale, 0, (1 - scale) * w / 2.0 + dx], [0, scale, (1 - scale) * h / 2.0 + dy]])
        import cv2
        out = cv2.warpAffine(np.ascontiguousarray(arr), M, (w, h), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_REPLICATE)
    if bright != 1.0:
        out = np.clip(out.astype(np.float32) * bright + 0.5, 0, 255).astype(np.uint8)
    return out


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
                progress_cb: Optional[Callable[[float], None]] = None, song_path=None,
                gpu_id: Optional[int] = None, cancel: Optional[threading.Event] = None,
                info_cb: Optional[Callable[[dict], None]] = None,
                pump: Optional[dict] = None) -> dict:
    """Render the plan to out_path (mp4). Returns {"interp","frames","interpolated","out_duration",
    "seconds","gpu"}. audio_wav=None builds the audio from song_path + plan. `cancel` (Event) stops at the
    next frame boundary, kills RIFE, removes temps and raises RenderCancelled (no out_path written).
    `pump` = {boom 0-100 (0/None = off: pixels untouched), decay 50-400, drops_only, shake, song: analysis
    of the ORIGINAL song}: Boom (pump.py math) applied per output frame after interpolation, beats mapped
    to output time through the plan (output_song_events)."""
    t_start = time.time()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if free_bytes(out_path.parent) < MIN_FREE_BYTES:
        raise RenderError("Less than 3 GB of free disk space; refusing to render")
    mode = resolve_interp(interp, gpu_id)
    exe = rife_exe() if mode == "rife" else None
    runner = RifeRunner(exe, gpu_id, cancel) if exe else None
    if runner and info_cb:
        info_cb({"gpu": runner.gpu_label})

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

    track = pump_track(pump, plan, out_dur, fps, n_out)

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
                if cancel is not None and cancel.is_set():
                    raise RenderCancelled("cancelled")
                idx = range(b0, min(n_out, b0 + BATCH))
                frames, jobs = {}, {}
                for k in idx:
                    i, f = int(i_arr[k]), float(f_arr[k])
                    interp_here = mode != "dup" and bool(_interp_mask(np.array([f]), np.array([bool(slow[k])]))[0])
                    if not interp_here:
                        frames[k] = reader.get(i + (1 if f >= 0.5 else 0))
                        continue
                    a, b = reader.get(i), reader.get(i + 1)
                    n_interp += 1
                    if mode == "rife":
                        jobs[k] = pool.submit(runner.pair, a, b, round(f, 3))
                    else:
                        frames[k] = ((1 - f) * a.astype(np.float32) + f * b.astype(np.float32) + 0.5).astype(np.uint8)
                for k, j in jobs.items():
                    frames[k] = j.result()
                if cancel is not None and cancel.is_set():
                    raise RenderCancelled("cancelled")
                for k in idx:
                    arr = frames[k][:height, :width]
                    if track is not None:
                        arr = apply_pump(arr, *track[min(k, len(track) - 1)])
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
            if runner and cancel is not None and cancel.is_set():
                runner.kill_all()
            reader.close()
            if pool:
                pool.shutdown(wait=True, cancel_futures=True)
            out.close()
        winsafe.replace_retry(tmp, out_path)
    finally:
        _cleanup_dir(tmp_dir, out_path.parent)
        _cleanup_file(out_path.with_name(out_path.stem + ".tmp.mp4"))
    if progress_cb:
        progress_cb(1.0)
    secs = time.time() - t_start
    if runner and n_interp >= 8:                      # remember what RIFE really cost on this machine
        base, _ = _cost("base", width, height)
        _save_cost("rife", f"{width}x{height}|{runner.gpu_label}", max(0.0, (secs - n_out * base) / n_interp))
    return {"interp": mode, "frames": n_out, "interpolated": n_interp, "out_duration": n_out / fps,
            "seconds": round(secs, 2), "gpu": runner.gpu_label if runner else None,
            "gpu_failures": runner.failed if runner else []}


# --------------------------------------------------------------------------- HTTP router

JOBS: dict = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(1)


def _upd(job, **kw):
    with _lock:
        job.update(kw)


_CANCELS: dict = {}


def _run_render(job, video_path, song_path, plan, interp, out, gpu_id=None, pump=None):
    ev = _CANCELS.setdefault(job["id"], threading.Event())
    try:
        if ev.is_set():
            raise RenderCancelled("cancelled")
        pump = pump_params(pump) if pump else None
        if pump and pump["boom"] > 0 and not pump.get("song"):
            from .audio import analyze_song
            pump["song"] = analyze_song(str(song_path))
        info = render_plan(video_path, plan, None, out, interp=interp, song_path=song_path, pump=pump,
                           progress_cb=lambda f: _upd(job, done=int(f * 100)),
                           gpu_id=gpu_id, cancel=ev, info_cb=lambda d: _upd(job, **d))
        _upd(job, status="done", done=100, path=str(out), size=Path(out).stat().st_size,
             interp=info["interp"], seconds=info["seconds"], gpu=info["gpu"],
             url=f"/api/export-file?path={out}")
    except RenderCancelled:
        _cleanup_file(Path(out).with_name(Path(out).stem + ".tmp.mp4"))
        _upd(job, status="cancelled", error=None)
    except Exception as e:                                # noqa: BLE001
        _upd(job, status="error", error=str(e))
    finally:
        _CANCELS.pop(job["id"], None)


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
        gpu_id: Optional[int] = None
        boom: float = 0.0
        decay: float = 180.0
        drops_only: bool = False
        shake: bool = False

    class EstimateRequest(BaseModel):
        video_path: str
        plan: dict
        interp: str = "auto"
        measure: bool = False
        gpu_id: Optional[int] = None

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
        _CANCELS[job["id"]] = threading.Event()
        _pool.submit(_run_render, job, req.video_path, req.song_path, req.plan, req.interp, out, req.gpu_id,
                     pump_params({"boom": req.boom, "decay": req.decay, "drops_only": req.drops_only,
                                  "shake": req.shake}))
        return {"job_id": job["id"]}

    @r.post("/api/render-mp4/{job_id}/cancel")
    def render_cancel(job_id: str):
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Unknown job")
        ev = _CANCELS.get(job_id)
        if ev is not None and job.get("status") == "running":
            ev.set()
        return JSONResponse({"job_id": job_id, "status": job.get("status")})

    @r.post("/api/render-estimate")
    def render_estimate(req: EstimateRequest):
        if not Path(req.video_path).is_file():
            raise HTTPException(404, f"File not found: {req.video_path}")
        try:
            return estimate_render(req.plan, req.interp, req.video_path, req.measure, req.gpu_id)
        except RenderError as e:
            raise HTTPException(400, str(e))

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
