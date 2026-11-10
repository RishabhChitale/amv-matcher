"""Browser-preview proxy: a cached 540p video-only H.264 mp4 for videos a <video> tag can't play
(mkv, avi/MJPG, prores mov, ...). Scoring always uses the original file."""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

from fractions import Fraction

import av

from . import paths, winsafe

PROXY_H = 540
WEB_CODECS = {".mp4": {"h264"}, ".m4v": {"h264"}, ".mov": {"h264"}, ".webm": {"vp8", "vp9"}}
_locks: dict = {}
_glock = threading.Lock()
_ready: dict = {}          # key -> Path | Exception


def proxy_dir() -> Path:
    return paths.CACHE / "proxy"


def _key(vp: Path) -> str:
    st = vp.stat()
    return hashlib.sha1(f"{winsafe.norm_case_path(vp.resolve())}|{st.st_size}|{int(st.st_mtime)}|v1".encode()).hexdigest()[:16]


def video_codec(vp: Path) -> str:
    with av.open(str(vp)) as c:
        return c.streams.video[0].codec_context.name if c.streams.video else ""


def needs_proxy(vp: Path) -> bool:
    try:
        return video_codec(vp) not in WEB_CODECS.get(vp.suffix.lower(), set())
    except Exception:
        return True


def proxy_path(vp: Path) -> Path:
    return proxy_dir() / f"{_key(vp)}.mp4"


def build(vp: Path) -> Path:
    """Create (or reuse) the proxy; blocks until it exists. Raises if the video can't be decoded."""
    out = proxy_path(vp)
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
            _encode(vp, tmp)
            winsafe.replace_retry(tmp, out)
        finally:
            winsafe.safe_unlink(tmp)
    return out


def _encode(vp: Path, tmp: Path) -> None:
    with av.open(str(vp)) as vin:
        if not vin.streams.video:
            raise RuntimeError("no video stream")
        vs_in = vin.streams.video[0]
        vs_in.thread_type = "AUTO"
        vs_in.thread_count = 0
        w, h = vs_in.width, vs_in.height
        nh = min(PROXY_H, h)
        nh -= nh % 2
        nw = int(round(w * nh / h / 2)) * 2
        rate = vs_in.average_rate or 30
        with av.open(str(tmp), "w", format="mp4") as out:
            vs = out.add_stream("libx264", rate=rate)
            vs.width, vs.height, vs.pix_fmt = nw, nh, "yuv420p"
            vs.options = {"preset": "veryfast", "crf": "26", "movflags": "+faststart"}
            tb = Fraction(1, 1) / Fraction(rate)
            for i, frame in enumerate(vin.decode(vs_in)):
                f = frame.reformat(width=nw, height=nh, format="yuv420p")
                f.pts = i            # source time_base (e.g. 1/600) must not leak into the 1/rate encoder
                f.time_base = tb
                for pkt in vs.encode(f):
                    out.mux(pkt)
            for pkt in vs.encode(None):
                out.mux(pkt)


def build_async(vp: Path, pool) -> None:
    """Kick the proxy build off in the background; errors are remembered in status()."""
    k = str(proxy_path(vp))
    if k in _ready:
        return
    _ready[k] = None

    def run():
        try:
            _ready[k] = build(vp)
        except Exception as e:  # noqa: BLE001
            _ready[k] = e
    pool.submit(run)


def status(vp: Path) -> str:
    """'ready' | 'building' | 'failed' | 'none' (original plays natively)"""
    if not needs_proxy(vp):
        return "none"
    k = str(proxy_path(vp))
    if proxy_path(vp).is_file():
        return "ready"
    v = _ready.get(k, "x")
    return "failed" if isinstance(v, Exception) else "building"
