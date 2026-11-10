"""Regression: proxy.build / export_mp4 re-encode with MJPG sources whose stream time_base is 1/600."""
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from amv_matcher import proxy, media

N = 24


def _mjpg_mov(path: Path):
    with av.open(str(path), "w", format="mov") as out:
        vs = out.add_stream("mjpeg", rate=24)
        vs.width, vs.height, vs.pix_fmt = 64, 48, "yuvj420p"
        vs.codec_context.time_base = Fraction(1, 600)
        for i in range(N):
            a = np.full((48, 64, 3), (i * 10) % 255, dtype=np.uint8)
            fr = av.VideoFrame.from_ndarray(a, format="rgb24").reformat(format="yuvj420p")
            fr.pts, fr.time_base = i * 25, Fraction(1, 600)
            for p in vs.encode(fr):
                out.mux(p)
        for p in vs.encode(None):
            out.mux(p)


def _count(p):
    with av.open(str(p)) as c:
        return sum(1 for _ in c.decode(c.streams.video[0]))


def test_proxy_encode_mjpg_tb600(tmp_path):
    src = tmp_path / "s.mov"
    _mjpg_mov(src)
    with av.open(str(src)) as c:
        assert c.streams.video[0].time_base != 1 / c.streams.video[0].average_rate  # tb is not 1/rate
    dst = tmp_path / "p.mp4"
    proxy._encode(src, dst)
    assert _count(dst) == N
    with av.open(str(dst)) as c:
        pts = [p.pts for p in c.demux(c.streams.video[0]) if p.pts is not None]
    assert pts == sorted(set(pts))   # strictly increasing, no collisions


def test_export_mp4_reencode_mjpg(tmp_path):
    import soundfile as sf
    src = tmp_path / "s.mov"
    _mjpg_mov(src)
    wav = tmp_path / "a.wav"
    sf.write(str(wav), np.zeros(44100, dtype="float32"), 44100)
    out = tmp_path / "o.mp4"
    media.export_mp4(src, wav, out)
    assert _count(out) == N
