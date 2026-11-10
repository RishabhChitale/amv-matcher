"""PyAV helpers: embedded-audio extraction, audio rendering (splice/trim) and mp4 export.

PyAV's wheel bundles the ffmpeg libraries, so no system ffmpeg is needed. OpenCV stays the
frame-analysis library; PyAV is used only here.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable, Optional

from fractions import Fraction

import av
import numpy as np
import soundfile as sf

SR = 44100
COPYABLE_VIDEO = {"h264", "hevc", "mpeg4", "av1", "vp9"}   # codecs we remux into mp4 as-is


# ---------------------------------------------------------------- extraction
def _extract_key(path: Path) -> str:
    st = path.stat()
    return hashlib.sha256(f"{path.resolve()}|{int(st.st_mtime)}|{st.st_size}".encode()).hexdigest()[:12]


def has_audio_stream(video_path) -> bool:
    with av.open(str(video_path)) as c:
        return len(c.streams.audio) > 0


def extract_audio(video_path, out_dir: Path) -> Optional[Path]:
    """Decode the first audio stream to out_dir/<sha12>.wav (44.1 kHz stereo). None if no audio."""
    vp = Path(video_path)
    out = Path(out_dir) / f"{_extract_key(vp)}.wav"
    if out.is_file() and out.stat().st_size > 44:
        return out
    chunks = []
    with av.open(str(vp)) as c:
        if not c.streams.audio:
            return None
        rs = av.AudioResampler(format="s16", layout="stereo", rate=SR)
        # packet by packet so one undecodable packet (seen in real files) does not cost the track.
        # The bad packet is replaced by silence of its duration so later samples keep their timestamps.
        last_dur = 0.0
        for pkt in c.demux(c.streams.audio[0]):
            if pkt.size == 0 and pkt.pts is None:
                continue  # demux flush marker
            if pkt.duration is not None and pkt.time_base is not None:
                last_dur = float(pkt.duration * pkt.time_base)
            try:
                frames = pkt.decode()
            except av.error.FFmpegError:
                n = int(round(last_dur * SR))
                if n > 0:
                    chunks.append(np.zeros((1, n * 2), np.int16))
                continue
            for frame in frames:
                for f in rs.resample(frame):
                    chunks.append(f.to_ndarray())
        for f in rs.resample(None):
            chunks.append(f.to_ndarray())
    if not chunks:
        return None
    # packed s16 stereo -> shape (1, n*2); interleaved L R
    data = np.concatenate([a.reshape(-1) for a in chunks]).reshape(-1, 2)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.wav")
    sf.write(str(tmp), data, SR, subtype="PCM_16")
    os.replace(tmp, out)
    return out


# ------------------------------------------------------------------- render
def render_audio(song_path, segments, video_duration: float, out_path: Path,
                 crossfade_ms: float = 40.0) -> float:
    """Assemble `segments` ({src_start, src_end, dst_start}) of the song into a wav of exactly
    round(video_duration * SR) samples. Joins get an equal-power crossfade centred on the cut;
    the ends get a 10 ms fade. Returns the output duration in seconds."""
    import librosa
    y, _ = librosa.load(str(song_path), sr=SR, mono=False)
    y = np.atleast_2d(y).astype(np.float32)
    if y.shape[0] == 1:
        y = np.repeat(y, 2, axis=0)
    y = y[:2]
    n_total = int(round(video_duration * SR))
    out = np.zeros((2, n_total), np.float32)
    half = int(round(crossfade_ms / 1000.0 * SR / 2))
    segs = sorted(segments, key=lambda s: s["dst_start"])
    for i, s in enumerate(segs):
        a, b = int(round(s["src_start"] * SR)), int(round(s["src_end"] * SR))
        d = int(round(s["dst_start"] * SR))
        pre = half if i > 0 else 0
        post = half if i < len(segs) - 1 else 0
        a0, b0, d0 = max(0, a - pre), min(y.shape[1], b + post), d - pre
        if d0 < 0:
            a0, d0 = a0 - d0, 0
        piece = y[:, a0:b0].copy()
        piece = piece[:, : max(0, n_total - d0)]
        n = piece.shape[1]
        if n <= 0:
            continue
        env = np.ones(n, np.float32)
        if pre and n > 2 * pre:
            t = np.linspace(0, np.pi / 2, 2 * pre, dtype=np.float32)
            env[: 2 * pre] = np.sin(t)
        if post and n > 2 * post:
            t = np.linspace(0, np.pi / 2, 2 * post, dtype=np.float32)
            env[-2 * post:] = np.cos(t)
        out[:, d0:d0 + n] += piece * env
    f = min(int(0.010 * SR), n_total // 2)
    if f > 0:
        ramp = np.linspace(0, 1, f, dtype=np.float32)
        out[:, :f] *= ramp
        out[:, -f:] *= ramp[::-1]
    out = np.clip(out, -1.0, 1.0)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp.wav")
    sf.write(str(tmp), out.T, SR, subtype="PCM_16")
    os.replace(tmp, out_path)
    return n_total / SR


# ---------------------------------------------------------------- mp4 export
class ExportError(Exception):
    pass


def export_mp4(video_path, audio_path, out_path: Path,
               progress: Optional[Callable[[float], None]] = None) -> None:
    """Remux the video with `audio_path` (wav) as its only soundtrack into an mp4.
    Video is stream-copied when its codec is mp4-safe, otherwise re-encoded to H.264."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.stem + ".tmp.mp4")
    try:
        try:
            _export(video_path, audio_path, tmp, progress, copy=None)
        except ExportError:
            raise
        except Exception:
            _export(video_path, audio_path, tmp, progress, copy=False)   # copy failed: re-encode
        os.replace(tmp, out_path)
    finally:
        tmp.unlink(missing_ok=True)


def _export(video_path, audio_path, tmp: Path, progress, copy: Optional[bool]) -> None:
    tmp.unlink(missing_ok=True)
    with av.open(str(video_path)) as vin:
        if not vin.streams.video:
            raise ExportError("The video has no video stream")
        vs_in = vin.streams.video[0]
        vs_in.thread_type = "AUTO"
        vs_in.thread_count = 0
        codec = vs_in.codec_context.name
        do_copy = (codec in COPYABLE_VIDEO) if copy is None else copy
        dur = float(vin.duration / av.time_base) if vin.duration else 0.0
        out = av.open(str(tmp), "w", format="mp4")
        try:
            if do_copy:
                vs = out.add_stream_from_template(vs_in)
            else:
                vs = out.add_stream("libx264", rate=vs_in.average_rate or 30)
                vs.width, vs.height = vs_in.width - vs_in.width % 2, vs_in.height - vs_in.height % 2
                vs.pix_fmt = "yuv420p"
                vs.options = {"preset": "veryfast", "crf": "20"}
            ast = out.add_stream("aac", rate=SR)
            ast.layout = "stereo"
            ast.bit_rate = 192000

            # pre-encode the audio into packets, then interleave with video by dts
            apk = []
            data, sr = sf.read(str(audio_path), dtype="float32", always_2d=True)
            if data.shape[1] == 1:
                data = np.repeat(data, 2, axis=1)
            rs = av.AudioResampler(format="fltp", layout="stereo", rate=SR, frame_size=1024)
            fr = av.AudioFrame.from_ndarray(np.ascontiguousarray(data[:, :2].T.reshape(1, -1)),
                                            format="flt", layout="stereo")
            fr.sample_rate = sr
            for r in rs.resample(fr) + rs.resample(None):
                apk.extend(ast.encode(r))
            apk.extend(ast.encode(None))

            def vtime(p):
                t = p.dts if p.dts is not None else p.pts
                return float(t * p.time_base) if t is not None else 0.0
            ai = 0

            def flush_audio(upto):
                nonlocal ai
                while ai < len(apk) and vtime(apk[ai]) <= upto:
                    out.mux(apk[ai])
                    ai += 1

            def send(p):
                flush_audio(vtime(p))
                if p.dts is None:
                    return
                out.mux(p)
                if progress and dur:
                    progress(min(0.99, max(0.0, float((p.pts or p.dts) * p.time_base) / dur)))

            if do_copy:
                for p in vin.demux(vs_in):
                    if p.dts is None:
                        continue
                    p.stream = vs
                    send(p)
            else:
                tb = Fraction(1, 1) / Fraction(vs_in.average_rate or 30)
                for i, frame in enumerate(vin.decode(vs_in)):
                    nf = frame.reformat(width=vs.width, height=vs.height, format="yuv420p")
                    nf.pts, nf.time_base = i, tb
                    for p in vs.encode(nf):
                        send(p)
                for p in vs.encode(None):
                    send(p)
            flush_audio(float("inf"))
        finally:
            out.close()
    if progress:
        progress(1.0)
