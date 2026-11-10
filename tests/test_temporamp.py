"""U11b: song tempo ramp (new module temporamp.py)."""
import numpy as np

from amv_matcher import temporamp as TR
from amv_matcher import speedramp as SR
from amv_matcher import tempo as T
from test_retime import make_song, make_video
from test_speedramp import _ramp_cuts


def _ratios(warp):
    w = np.asarray(warp, dtype=float)
    return np.diff(w[:, 0]) / np.maximum(np.diff(w[:, 1]), 1e-9)


def test_constant_warp_matches_apply_tempo():
    song = make_song(duration=30)
    w = np.array([[0.0, 0.0], [30.0, 30.0 / 1.05]])
    a, b = TR.apply_tempo_warp(song, w), T.apply_tempo(song, 1.05)
    assert abs(a["duration"] - b["duration"]) < 1e-6
    assert np.allclose(a["beat_times"], b["beat_times"], atol=1e-6)
    assert abs(a["bpm"] - b["bpm"]) < 0.2
    assert np.allclose(a["drops"], b["drops"], atol=1e-6)


def test_ramp_curve_limits_and_beats_follow_curve():
    pts = TR.ratio_points([0.9, 1.1], [10.0], 30.0)
    w, s = TR.tempo_warp(pts, 30.0)
    assert s.min() >= 0.9 - 1e-9 and s.max() <= 1.1 + 1e-9
    assert np.all(np.diff(w[:, 1]) > 0)
    sw = TR.apply_tempo_warp(make_song(duration=30), w)
    gaps = np.diff(sw["beat_times"])
    assert gaps[0] > 0.5 / 0.9 * 0.0 + 0.54 and gaps[-1] < 0.47          # slow start, fast end
    assert np.all(np.abs(np.diff(gaps)) < 0.01)                             # no jump between beats


def test_plan_follows_accelerating_video():
    song = make_song(duration=40)                          # 120 bpm: beats every 0.5 s
    vid = make_video(_ramp_cuts(0.54, 0.46), duration=28.0)  # cuts speed up: song must speed up too
    r = TR.plan_tempo_ramp(song, vid, 0.0)
    ratios = _ratios(r["warp"])
    assert r["ratio_range"][0] >= 0.9 - 1e-3 and r["ratio_range"][1] <= 1.1 + 1e-3
    assert ratios[-1] > ratios[0] + 0.06
    assert r["j_ramp"] >= r["j_const"]


def test_plan_follows_decelerating_video():
    song = make_song(duration=40)
    vid = make_video(_ramp_cuts(0.46, 0.54), duration=28.0)
    r = TR.plan_tempo_ramp(song, vid, 0.0)
    ratios = _ratios(r["warp"])
    assert ratios[0] > ratios[-1] + 0.06
    assert r["j_ramp"] >= r["j_const"]


def test_stretch_warped_keeps_pitch_and_moves_clicks():
    sr = 22050
    t = np.arange(int(6 * sr)) / sr
    y = 0.3 * np.sin(2 * np.pi * 440 * t).astype(np.float32)
    clicks = np.arange(0.5, 5.5, 0.5)
    for c in clicks:
        i = int(c * sr)
        y[i:i + 200] += 0.9 * np.hanning(200).astype(np.float32)
    pts = TR.ratio_points([0.9, 1.1], [3.0], 6.0)
    w, _s = TR.tempo_warp(pts, 6.0)
    out = TR.stretch_warped(y, sr, w)
    assert abs(len(out) / sr - w[-1, 1]) < 0.05
    seg = out[int(0.2 * sr):int(0.45 * sr)]
    f = np.fft.rfftfreq(len(seg), 1 / sr)[np.argmax(np.abs(np.fft.rfft(seg * np.hanning(len(seg)))))]
    assert abs(f - 440) < 12
    env = np.abs(out)
    for c in clicks[1:-1]:
        want = float(np.interp(c, w[:, 0], w[:, 1]))
        a, b = int((want - 0.08) * sr), int((want + 0.08) * sr)
        got = (a + int(np.argmax(env[a:b]))) / sr
        assert abs(got - want) < 0.05, (c, got, want)
