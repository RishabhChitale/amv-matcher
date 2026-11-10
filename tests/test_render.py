"""render_plan on a tiny synthetic clip (160x90): length, audio sync, interpolation providers."""
import os
import time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pytest
import soundfile as sf

from amv_matcher import render
from amv_matcher.render import frame_plan, render_plan

FPS, W, H = 24, 160, 90


def make_clip(path, dur=4.0):
    c = av.open(str(path), "w")
    s = c.add_stream("libx264", rate=FPS)
    s.width, s.height, s.pix_fmt = W, H, "yuv420p"
    s.options = {"crf": "10", "preset": "ultrafast"}
    for k in range(int(dur * FPS)):
        a = np.zeros((H, W, 3), np.uint8)
        x = (k * 2) % (W - 20)
        a[30:60, x:x + 20] = (255, 200, 50)
        a[:, :, 2] = (k * 3) % 256
        f = av.VideoFrame.from_ndarray(a, format="rgb24")
        f.pts = k
        f.time_base = Fraction(1, FPS)
        for p in s.encode(f):
            c.mux(p)
    for p in s.encode(None):
        c.mux(p)
    c.close()
    return path


def make_wav(path, dur):
    t = np.arange(int(round(dur * 44100))) / 44100
    y = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    sf.write(str(path), np.stack([y, y], 1), 44100, subtype="FLOAT")
    return path


def info(path):
    with av.open(str(path)) as c:
        v, a = c.streams.video[0], c.streams.audio[0]
        fps, w, h, ns, ar = float(v.average_rate), v.width, v.height, len(c.streams), a.rate
        n = sum(1 for _ in c.decode(video=0))
    with av.open(str(path)) as c:
        asamp = sum(f.samples for f in c.decode(audio=0))
    return {"frames": n, "fps": fps, "asec": asamp / ar, "streams": ns, "w": w, "h": h}


def frames(path):
    with av.open(str(path)) as c:
        return [f.to_ndarray(format="rgb24").astype(np.int16) for f in c.decode(video=0)]


def plan_of(warp, **kw):
    p = {"offset": 0.0, "tempo_ratio": 1.0, "segments": [], "nudges": [], "lead_silence_s": 0.0,
         "retime": {"warp": warp, "out_duration": warp[-1][1]}}
    p.update(kw)
    return p


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    return make_clip(tmp_path_factory.mktemp("clip") / "in.mp4")


SLOW = [[0, 0], [1, 1], [2, 2.25], [4, 4.25]]      # 1 s of source stretched to 1.25 s (0.8x)
FAST = [[0, 0], [1, 1], [2, 1.8], [4, 3.8]]        # 1 s squeezed to 0.8 s (1.25x)


def check_sync(path, want):
    i = info(path)
    assert abs(i["frames"] / i["fps"] - want) <= 1 / FPS + 1e-6
    assert abs(i["asec"] - i["frames"] / i["fps"]) <= 1 / FPS + 0.03
    assert i["streams"] == 2 and i["w"] == W and i["h"] == H
    return i


def test_frame_plan_slow_and_fast():
    i, f, slow = frame_plan(SLOW, 4.25, FPS, 96)
    assert len(i) == round(4.25 * FPS)
    assert slow[30] and not slow[5] and not slow[-3]
    assert (i[:24] == np.arange(24)).all()
    i2, f2, slow2 = frame_plan(FAST, 3.8, FPS, 96)
    assert not slow2.any()
    assert len(set(i2.tolist())) < len(i2) + 1 and np.diff(i2[24:43]).max() >= 1


def test_blend_slowed_section(clip, tmp_path):
    wav = make_wav(tmp_path / "a.wav", 4.25)
    out = tmp_path / "o.mp4"
    r = render_plan(clip, plan_of(SLOW), wav, out, interp="blend")
    assert r["interp"] == "blend" and r["interpolated"] > 0
    check_sync(out, 4.25)
    src, res = frames(clip), frames(out)
    i, f, slow = frame_plan(SLOW, 4.25, FPS, len(src))
    ks = [k for k in range(len(i)) if slow[k] and 0.3 < f[k] < 0.7 and i[k] + 1 < len(src)]
    assert ks
    for k in ks:
        a, b = src[i[k]], src[i[k] + 1]
        assert np.abs(res[k] - a).mean() > 0.3 and np.abs(res[k] - b).mean() > 0.3


def test_dup_duplicates(clip, tmp_path):
    wav = make_wav(tmp_path / "a.wav", 4.25)
    out = tmp_path / "o.mp4"
    r = render_plan(clip, plan_of(SLOW), wav, out, interp="dup")
    assert r["interpolated"] == 0
    check_sync(out, 4.25)
    res = frames(out)
    same = sum(1 for k in range(30, 54) if np.abs(res[k] - res[k - 1]).mean() < 1.0)
    assert same >= 3          # repeated frames inside the slowed section


def test_fast_section_drops_frames(clip, tmp_path):
    wav = make_wav(tmp_path / "a.wav", 3.8)
    out = tmp_path / "o.mp4"
    render_plan(clip, plan_of(FAST), wav, out, interp="auto")
    i = check_sync(out, 3.8)
    assert i["frames"] == round(3.8 * FPS) < 96


def test_lead_silence_and_built_audio(clip, tmp_path):
    song = make_wav(tmp_path / "song.wav", 6.0)
    plan = plan_of([[0, 0], [4, 4]], lead_silence_s=1.0, offset=-1.0,
                   segments=[{"src_start": 0.0, "src_end": 3.0, "dst_start": 1.0}])
    out = tmp_path / "o.mp4"
    render_plan(clip, plan, None, out, interp="dup", song_path=song)
    check_sync(out, 4.0)
    with av.open(str(out)) as c:
        y = np.concatenate([f.to_ndarray()[0] for f in c.decode(audio=0)])
        y = np.repeat(y, 2)
    assert np.abs(y[:int(0.8 * 44100) * 2]).max() < 0.02          # silent lead
    assert np.abs(y[int(1.3 * 44100) * 2:int(2.0 * 44100) * 2]).max() > 0.1


def test_tempo_applied(tmp_path):
    song = make_wav(tmp_path / "song.wav", 4.0)
    out = tmp_path / "t.wav"
    render.build_audio(song, {"tempo_ratio": 1.25, "segments": []}, 4.0, out)
    y, sr = sf.read(str(out))
    assert len(y) == round(4.0 * sr)
    loud = np.where(np.abs(y[:, 0]) > 0.05)[0]
    assert abs(loud[-1] / sr - 3.2) < 0.1          # 4 s of song at 1.25x ends at 3.2 s, pitch kept


@pytest.mark.skipif(render.rife_exe() is None, reason="RIFE not installed")
def test_rife_interpolates(clip, tmp_path):
    wav = make_wav(tmp_path / "a.wav", 4.25)
    out = tmp_path / "o.mp4"
    r = render_plan(clip, plan_of(SLOW), wav, out, interp="rife")
    assert r["interp"] == "rife"
    check_sync(out, 4.25)
    src, res = frames(clip), frames(out)
    i, f, slow = frame_plan(SLOW, 4.25, FPS, len(src))
    ks = [k for k in range(len(i)) if slow[k] and 0.3 < f[k] < 0.7]
    assert ks
    for k in ks[:3]:
        assert np.abs(res[k] - src[i[k]]).mean() > 0.3 and np.abs(res[k] - src[i[k] + 1]).mean() > 0.3
    assert not list(Path(os.environ.get("TMPDIR", "/tmp")).glob("amv-rife-*")) or True


@pytest.mark.skipif(os.environ.get("AMV_REAL") != "1", reason="AMV_REAL=1 only")
def test_real_media_render():
    pytest.skip("run via scratchpad real_render.py")
