"""Regression: proxy.build / export_mp4 on a MJPG mp4 (yuvj420p, stream time_base 1/600).
The old proxy._encode set f.pts=None, leaving frames in the source base -> mux ValueError(22)."""
from fractions import Fraction

import av
import numpy as np
import soundfile as sf

from amv_matcher import media, proxy

SECONDS, FPS = 24.0, 30


def _fixture(tmp_path):
    """MJPG video whose stream time_base is 1/600, written with PyAV directly. The muxer picks the final
    time_base when the header is written and by default rewrites it (mp4 1/19200, mkv 1/1000); the mov/mp4
    muxer option video_track_timescale=600 pins it to 1/600. That is libavformat logic (same bundled
    ffmpeg in the PyAV wheel on every OS), unlike a cv2/helper-written file."""
    p = tmp_path / "slow.mp4"
    tb = Fraction(1, 600)
    n = int(SECONDS * FPS)
    with av.open(str(p), "w", options={"video_track_timescale": "600"}) as c:
        s = c.add_stream("mjpeg", rate=FPS)
        s.width, s.height, s.pix_fmt = 64, 48, "yuvj420p"
        s.time_base = tb
        s.codec_context.time_base = tb
        for i in range(n):
            arr = np.full((48, 64, 3), (i * 7) % 256, np.uint8)
            f = av.VideoFrame.from_ndarray(arr, format="rgb24").reformat(format="yuvj420p")
            f.pts, f.time_base = i * (600 // FPS), tb
            for pk in s.encode(f):
                c.mux(pk)
        for pk in s.encode():
            c.mux(pk)
    return p


def _count(p):
    with av.open(str(p)) as c:
        return sum(1 for _ in c.decode(c.streams.video[0]))


def test_proxy_build_mjpg_tb600(tmp_path, monkeypatch):
    src = _fixture(tmp_path)
    with av.open(str(src)) as c:
        vs = c.streams.video[0]
        assert vs.codec_context.name == "mjpeg" and str(vs.time_base) == "1/600"
    dst = tmp_path / "p.mp4"
    proxy._encode(src, dst)
    assert _count(dst) == int(SECONDS * FPS)
    with av.open(str(dst)) as c:
        pts = [p.pts for p in c.demux(c.streams.video[0]) if p.pts is not None]
    assert pts == sorted(set(pts))


def test_export_mp4_reencode_mjpg(tmp_path):
    src = _fixture(tmp_path)
    wav = tmp_path / "a.wav"
    sf.write(str(wav), np.zeros(44100, dtype="float32"), 44100)
    out = tmp_path / "o.mp4"
    media.export_mp4(src, wav, out)
    assert _count(out) == int(SECONDS * FPS)
