"""Fast video frame reading: hardware decode (PyAV hwaccel) with automatic software fallback.

    fs = open_frames(path, width=160, fps=10, start=None, end=None, hw="auto", pix_fmt="rgb24")
    for t, img in fs: ...          # t seconds (float), img ndarray HxWx3 (HxW for "gray")
    fs.info                        # DecodeInfo: which path was used, why it fell back, counts
    fs.close()                     # releases the container (also a context manager / on exhaustion)

hw: "auto" | "off" | "videotoolbox" | "d3d11va" | "dxva2" | "cuda".
  auto = VideoToolbox on macOS, D3D11VA then DXVA2 on Windows, software elsewhere.
Fallback to software happens when the device is unavailable, the stream can't be opened or
decoded by the HW decoder, or a decode error happens mid-stream.  A mid-stream fallback re-opens,
seeks to the keyframe before the last good frame and drops frames up to it: no frame is
duplicated or skipped.

pix_fmt: "rgb24" | "bgr24" (OpenCV order) | "gray" | "yuv420p" (planar bytes, (H*3/2, W), even dims).
fps: frame picking by timestamp (nearest earlier source frame for each grid time); no
interpolation.  fps >= source rate leaves frames untouched.  Output t is the grid time.
fast=True: codec_context.skip_frame="NONREF" (opt-in: drops non-reference frames; e.g. B-frames).
Reduced-resolution decode (lowres) only exists for a few codecs (mpeg2/mjpeg) and is NOT used;
H.264/HEVC decode is always full size and the downscale is done by swscale afterwards.
"""
from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from typing import Optional

import av
from av.video.reformatter import VideoReformatter
import numpy as np

try:  # PyAV >= 14
    from av.codec.hwaccel import HWAccel, hwdevices_available
except Exception:  # pragma: no cover - very old PyAV
    HWAccel = None

    def hwdevices_available():
        return []

_EPS = 1e-6
_FMTS = {"rgb24", "bgr24", "gray", "yuv420p"}


def _auto_devices() -> list:
    if sys.platform == "darwin":
        return ["videotoolbox"]
    if sys.platform.startswith("win"):
        return ["d3d11va", "dxva2"]
    return []


def _make_hwaccel(device: str):
    """Factory (monkeypatchable in tests). Raises if the device can't be created."""
    if HWAccel is None:
        raise RuntimeError("PyAV without hwaccel support")
    return HWAccel(device_type=device, allow_software_fallback=False)


@dataclass
class DecodeInfo:
    requested: str = "auto"
    path: str = "software"            # device name actually decoding now, or "software"
    fallback_reason: Optional[str] = None
    tried: list = field(default_factory=list)   # devices attempted
    codec: str = ""
    frames_decoded: int = 0           # raw source frames read (after start filter)
    frames_out: int = 0
    fell_back_midstream: bool = False
    threads: str = ""


def _frame_time(frame, idx: int, rate) -> float:
    t = frame.time
    if t is None:
        t = idx / float(rate or 30)
    return float(t)


def _raw_frames(path, device: Optional[str], start: float, last_t: Optional[float],
                info: DecodeInfo, fast: bool, threaded: bool, holder: dict):
    """Yield (t, av.VideoFrame) from `path`, from the keyframe before max(start, last_t),
    skipping frames with t <= last_t (resume after fallback) or t < start.
    `holder['c']` exposes the open container so close() can release it."""
    kw = {}
    if device:
        kw["hwaccel"] = _make_hwaccel(device)
    c = av.open(str(path), **kw)
    holder["c"] = c
    try:
        if not c.streams.video:
            raise ValueError("no video stream")
        vs = c.streams.video[0]
        if threaded:
            vs.thread_type = "AUTO"
            vs.thread_count = 0
        if fast:
            vs.codec_context.skip_frame = "NONREF"
        info.codec = vs.codec_context.name
        info.threads = "auto" if threaded else "1"
        seek_to = max(start or 0.0, last_t if last_t is not None else 0.0)
        if seek_to > 0.5:
            # keyframe at/before seek_to (backward); frames before are dropped below
            c.seek(int(max(0.0, seek_to - 0.001) / float(vs.time_base)), stream=vs, backward=True)
        rate = vs.average_rate
        idx = 0
        got = False
        for frame in c.decode(vs):
            t = _frame_time(frame, idx, rate)
            idx += 1
            if device and not got:
                if not vs.codec_context.is_hwaccel:
                    raise RuntimeError("hardware decoder not used by this stream")
            got = True
            if last_t is not None and t <= last_t + _EPS:
                continue
            if start and t < start - _EPS:
                continue
            yield t, frame
    finally:
        holder["c"] = None
        try:
            c.close()
        except Exception:
            pass


def _frames_with_fallback(path, hw: str, start: float, info: DecodeInfo, fast: bool, holder: dict):
    if hw in (None, "off", "none", "software"):
        devices = []
        info.fallback_reason = "hw off" if hw else None
    elif hw == "auto":
        avail = set(hwdevices_available())
        devices = [d for d in _auto_devices() if d in avail]
        if not devices:
            info.fallback_reason = "no hardware decode device available"
    else:
        devices = [hw]
    last_t = None
    for dev in devices:
        info.tried.append(dev)
        info.path = dev
        try:
            for t, fr in _raw_frames(path, dev, start, last_t, info, fast, True, holder):
                last_t = t
                info.frames_decoded += 1
                yield t, fr
            return                                   # clean end of stream
        except GeneratorExit:
            raise
        except Exception as e:  # noqa: BLE001 - any HW failure => try next / software
            info.fallback_reason = f"{dev}: {type(e).__name__}: {e}"[:300]
            if last_t is not None:
                info.fell_back_midstream = True
    info.path = "software"
    for t, fr in _raw_frames(path, None, start, last_t, info, fast, True, holder):
        info.frames_decoded += 1
        yield t, fr


def _pick_fps(raw, fps: Optional[float], src_rate: float):
    """Nearest-earlier frame per grid time; passthrough if fps is None/>= source rate."""
    if not fps or fps >= src_rate * 0.999:
        yield from raw
        return
    dt = 1.0 / fps
    grid = None
    prev = None
    last_t = None
    for t, fr in raw:
        if grid is None:
            grid = t
        while prev is not None and grid < t - _EPS:
            yield grid, prev
            grid += dt
        prev, last_t = fr, t
    if prev is not None:
        while grid <= last_t + _EPS:
            yield grid, prev
            grid += dt


def _target_size(w, h, width, height, even):
    if not width and not height:
        tw, th = w, h
    elif width and height:
        tw, th = int(width), int(height)
    elif width:
        tw, th = int(width), int(round(h * int(width) / w))
    else:
        th, tw = int(height), int(round(w * int(height) / h))
    tw, th = max(1, tw), max(1, th)
    if even:
        tw, th = max(2, tw - tw % 2), max(2, th - th % 2)
    return tw, th


def _convert(rf, fr, tw, th, pix_fmt, interp):
    if (fr.width, fr.height) != (tw, th) or fr.format.name != pix_fmt:
        fr = rf.reformat(fr, width=tw, height=th, format=pix_fmt, interpolation=interp)
    return fr.to_ndarray()


def _converted(src, tw, th, pix_fmt, interp, threads: int):
    """(t, ndarray) for each (t, frame) of `src`, in order. threads > 1 runs the swscale downscale
    on that many worker threads (swscale releases the GIL), each with its own reformatter (one
    cached swscale context per thread): the same pixels as the serial path, only faster."""
    if threads <= 1:
        rf = VideoReformatter()     # one cached swscale context per stream (bit-identical, no per-frame init)
        for t, fr in src:
            yield t, _convert(rf, fr, tw, th, pix_fmt, interp)
        return
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor
    local = threading.local()

    def work(fr):
        rf = getattr(local, "rf", None)
        if rf is None:
            rf = local.rf = VideoReformatter()
        return _convert(rf, fr, tw, th, pix_fmt, interp)

    ahead = 2 * threads + 2           # frames in flight: bounds the extra memory to a few frames
    pend = deque()
    ex = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="amv-reformat")
    try:
        for t, fr in src:
            pend.append((t, ex.submit(work, fr)))
            if len(pend) >= ahead:
                t0, f = pend.popleft()
                yield t0, f.result()
        while pend:
            t0, f = pend.popleft()
            yield t0, f.result()
    finally:
        for _t, f in pend:
            f.cancel()
        ex.shutdown(wait=True)


class FrameStream:
    """Iterator of (t, ndarray) with .info and close(). Not thread-safe."""

    def __init__(self, path, width, height, fps, start, end, hw, pix_fmt, fast, interpolation,
                 reformat_threads=1):
        if pix_fmt not in _FMTS:
            raise ValueError(f"pix_fmt must be one of {sorted(_FMTS)}")
        self.info = DecodeInfo(requested=str(hw))
        self._holder = {"c": None}
        self._lock = threading.Lock()
        self._end = end
        self._threads = max(1, int(reformat_threads or 1))
        self._gen = self._run(str(path), width, height, fps, start or 0.0, pix_fmt, fast, interpolation, hw)
        self._closed = False

    def _run(self, path, width, height, fps, start, pix_fmt, fast, interp, hw):
        raw = _frames_with_fallback(path, hw, start, self.info, fast, self._holder)
        try:
            c0 = av.open(path)
            try:
                vs0 = c0.streams.video[0]
                src_rate = float(vs0.average_rate or vs0.guessed_rate or 30)
                sw, sh = vs0.width, vs0.height
            finally:
                c0.close()
            tw, th = _target_size(sw, sh, width, height, pix_fmt == "yuv420p")
            if interp is None:
                interp = "AREA" if (tw < sw or th < sh) else "BILINEAR"
            def picked():
                for t, fr in _pick_fps(raw, fps, src_rate):
                    if self._end is not None and t >= self._end - _EPS:
                        return
                    yield t, fr

            for t, img in _converted(picked(), tw, th, pix_fmt, interp, self._threads):
                self.info.frames_out += 1
                yield t, img
        finally:
            raw.close()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._gen)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._gen.close()
        finally:
            c = self._holder.get("c")
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def open_frames(path, width=None, fps=None, start=None, end=None, hw="auto",
                pix_fmt="rgb24", height=None, fast=False, interpolation=None,
                reformat_threads=1) -> FrameStream:
    """See module docstring. Returns a FrameStream (iterator of (t, ndarray); has .info).
    reformat_threads > 1 runs the downscale on that many threads (same output, faster)."""
    return FrameStream(path, width, height, fps, start, end, hw, pix_fmt, fast, interpolation,
                       reformat_threads)


def decode_info(path, hw="auto") -> DecodeInfo:
    """Which decode path would be used for `path` (opens it and decodes 1 frame)."""
    with open_frames(path, width=16, hw=hw) as fs:
        next(iter(fs), None)
        return fs.info


def probe_video(path) -> dict:
    """{duration, fps, width, height, codec, pix_fmt, hw_capable, frames}; hw_capable = a hardware
    device decodes this file on this machine (tried for real on the first frame)."""
    with av.open(str(path)) as c:
        if not c.streams.video:
            raise ValueError("no video stream")
        vs = c.streams.video[0]
        cc = vs.codec_context
        dur = float(c.duration / av.time_base) if c.duration else (
            float(vs.duration * vs.time_base) if vs.duration else 0.0)
        rate = float(vs.average_rate or vs.guessed_rate or 0.0)
        frames = vs.frames or (int(round(dur * rate)) if dur and rate else 0)
        out = {"duration": dur, "fps": rate, "width": vs.width, "height": vs.height,
               "codec": cc.name, "pix_fmt": cc.pix_fmt, "frames": int(frames)}
    try:
        out["hw_capable"] = decode_info(path).path != "software"
    except Exception:
        out["hw_capable"] = False
    return out
