"""Browser-preview proxy: a cached video-only H.264 mp4 (default 1080p, CRF 20) for videos a <video> tag
can't play (mkv, avi/MJPG, prores mov, hevc/av1 ...). A fast low-res "quick" proxy is built first so
playback can start while the full-quality one is still encoding. Export and scoring always use the
original file."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

from fractions import Fraction

import av

from . import paths, winsafe

QUALITIES = (720, 1080, 1440, 0)     # 0 = "Original if playable" (an unplayable source still gets a 1080p proxy)
DEFAULT_QUALITY = 1080
PROXY_CRF = "20"
QUICK_H = 480                        # stage-1 proxy: plays almost immediately
WEB_CODECS = {".mp4": {"h264"}, ".m4v": {"h264"}, ".mov": {"h264"}, ".webm": {"vp8", "vp9"}}
_locks: dict = {}
_glock = threading.Lock()
_ready: dict = {}          # key -> Path | Exception


def proxy_dir() -> Path:
    return paths.CACHE / "proxy"


def _settings_file() -> Path:
    return paths.DATA / "preview_quality.json"


def get_quality() -> int:
    """Persisted preview quality (read from disk each call: worker processes see changes too)."""
    try:
        q = int(json.loads(_settings_file().read_text()).get("quality", DEFAULT_QUALITY))
    except Exception:
        return DEFAULT_QUALITY
    return q if q in QUALITIES else DEFAULT_QUALITY


def set_quality(q: int) -> int:
    q = int(q)
    if q not in QUALITIES:
        raise ValueError(f"quality must be one of {QUALITIES}")
    f = _settings_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"quality": q}))
    winsafe.replace_retry(tmp, f)
    return q


def target_height(quality: int | None = None) -> int:
    q = get_quality() if quality is None else quality
    return DEFAULT_QUALITY if q == 0 else q


def parse_pq(pq) -> int | None:
    """?pq= value -> quality in QUALITIES, or None (absent/invalid = server default)."""
    try:
        q = int(pq)
    except (TypeError, ValueError):
        return None
    return q if q in QUALITIES else None


def _key(vp: Path, height: int | None = None) -> str:
    st = vp.stat()
    h = target_height() if height is None else height
    return hashlib.sha1(f"{winsafe.norm_case_path(vp.resolve())}|{st.st_size}|{int(st.st_mtime)}|v2|h{h}".encode()).hexdigest()[:16]


def video_codec(vp: Path) -> str:
    with av.open(str(vp)) as c:
        return c.streams.video[0].codec_context.name if c.streams.video else ""


def needs_proxy(vp: Path) -> bool:
    try:
        return video_codec(vp) not in WEB_CODECS.get(vp.suffix.lower(), set())
    except Exception:
        return True


def proxy_path(vp: Path, height: int | None = None) -> Path:
    return proxy_dir() / f"{_key(vp, height)}.mp4"


def quick_path(vp: Path) -> Path:
    return proxy_dir() / f"{_key(vp, QUICK_H)}.mp4"


def progress(vp: Path, height: int | None = None) -> float:
    """0..1 encode progress of the full-quality proxy (0 when not started, 1 when ready)."""
    out = proxy_path(vp, height)
    if out.is_file():
        return 1.0
    try:
        return float(out.with_suffix(".progress").read_text())
    except Exception:
        return 0.0


def best_ready(vp: Path, height: int | None = None) -> Path | None:
    """Full-quality proxy if built, else the quick one if built, else None."""
    for p in (proxy_path(vp, height), quick_path(vp)):
        if p.is_file():
            return p
    return None


def build(vp: Path, height: int | None = None) -> Path:
    """Create (or reuse) the proxy; blocks until the full-quality one exists (a quick low-res one is
    built first, so /api/video can start from it). Raises if the video can't be decoded."""
    return build_staged(vp, height)


def _build_one(vp: Path, h: int) -> Path:
    h = int(h)
    out = proxy_path(vp, h)
    if out.is_file():
        return out
    with _glock:
        lock = _locks.setdefault(str(out), threading.Lock())
    with lock:
        if out.is_file():
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp.mp4")
        winsafe.safe_unlink(tmp)
        try:
            _encode(vp, tmp, h, progress_file=out.with_suffix(".progress"))
            winsafe.replace_retry(tmp, out)
        finally:
            winsafe.safe_unlink(tmp)
            winsafe.safe_unlink(out.with_suffix(".progress"))
    return out


def build_staged(vp: Path, height: int | None = None) -> Path:
    """Quick low-res proxy first (so the preview starts fast), then the full-quality one."""
    height = target_height() if height is None else int(height)
    full = proxy_path(vp, height)
    if not full.is_file() and height > QUICK_H:
        try:
            with av.open(str(vp)) as c:
                src_h = c.streams.video[0].height if c.streams.video else 0
            if src_h > QUICK_H:
                _build_one(vp, QUICK_H)
        except Exception:
            pass
    return _build_one(vp, height)


def _encode(vp: Path, tmp: Path, height: int = DEFAULT_QUALITY, progress_file: Path | None = None) -> None:
    with av.open(str(vp)) as vin:
        if not vin.streams.video:
            raise RuntimeError("no video stream")
        vs_in = vin.streams.video[0]
        vs_in.thread_type = "AUTO"
        vs_in.thread_count = 0
        w, h = vs_in.width, vs_in.height
        nh = min(height, h)   # never upscale
        nh -= nh % 2
        nw = int(round(w * nh / h / 2)) * 2
        rate = vs_in.average_rate or 30
        with av.open(str(tmp), "w", format="mp4") as out:
            vs = out.add_stream("libx264", rate=rate)
            vs.width, vs.height, vs.pix_fmt = nw, nh, "yuv420p"
            quick = height <= QUICK_H
            vs.options = {"preset": "ultrafast" if quick else "veryfast",
                          "crf": "28" if quick else PROXY_CRF, "movflags": "+faststart"}
            total = int(vs_in.frames or 0) or int(float(vin.duration or 0) / 1_000_000 * float(rate))
            tb = Fraction(1, 1) / Fraction(rate)
            for i, frame in enumerate(vin.decode(vs_in)):
                f = frame.reformat(width=nw, height=nh, format="yuv420p")
                f.pts = i            # source time_base (e.g. 1/600) must not leak into the 1/rate encoder
                f.time_base = tb
                for pkt in vs.encode(f):
                    out.mux(pkt)
                if progress_file is not None and total and i % 60 == 0:
                    try:
                        progress_file.write_text(f"{min(0.99, i / total):.3f}")
                    except OSError:
                        pass
            for pkt in vs.encode(None):
                out.mux(pkt)


def build_async(vp: Path, pool, height: int | None = None) -> None:
    """Kick the proxy build off in the background; errors are remembered in status()."""
    k = str(proxy_path(vp, height))
    if k in _ready:
        return
    _ready[k] = None

    def run():
        try:
            _ready[k] = build(vp, height)
        except Exception as e:  # noqa: BLE001
            _ready[k] = e
    pool.submit(run)


def status(vp: Path, height: int | None = None) -> str:
    """'ready' | 'building' | 'failed' | 'none' (original plays natively)"""
    if not needs_proxy(vp):
        return "none"
    k = str(proxy_path(vp, height))
    if proxy_path(vp, height).is_file():
        return "ready"
    if quick_path(vp).is_file():
        return "quick"      # low-res preview playable; full quality still encoding
    v = _ready.get(k, "x")
    return "failed" if isinstance(v, Exception) else "building"
