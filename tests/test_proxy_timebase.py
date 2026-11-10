"""Regression: proxy.build / export_mp4 on a cv2-written MJPG .mp4 (yuvj420p, stream time_base 1/600).
The old proxy._encode set f.pts=None, leaving frames in the source base -> mux ValueError(22)."""
import av
import numpy as np
import soundfile as sf

from amv_matcher import media, proxy
from tests import synth

SECONDS, FPS = 24.0, 30


def _fixture(tmp_path):
    p = tmp_path / "slow.mp4"
    synth.make_video_jumps(p, synth.slow_section_jump_times(SECONDS), seconds=SECONDS, fps=FPS)
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
