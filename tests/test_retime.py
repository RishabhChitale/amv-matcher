"""retime + tempo tests on synthetic analysis dicts."""
import numpy as np

from amv_matcher import score as S
from amv_matcher.retime import apply_warp, plan_retime
from amv_matcher.tempo import apply_tempo, plan_tempo

HZ = 10


def make_song(duration=60.0, bpm=120.0):
    bt = np.arange(0, duration, 60.0 / bpm)
    return {"bpm": bpm, "beat_times": bt.tolist(), "beat_strengths": [1.0] * len(bt), "duration": duration,
            "energy_curve": np.full(int(duration * HZ), 0.5).tolist(), "energy_hz": HZ, "drops": [], "lulls": []}


def make_video(cuts, duration=30.0, tempo=120.0):
    return {"duration": duration, "fps": 30, "energy_curve": np.full(int(duration * HZ), 0.5).tolist(),
            "energy_hz": HZ, "segments": [], "lulls": [], "peak_segments": [], "shots": [],
            "cut_times": [float(c) for c in cuts], "motion_curve": [], "motion_peaks": [],
            "motion_tempo_bpm": tempo}


def slow_section_cuts():
    cuts = list(np.arange(0.5, 10.0, 0.5))
    cuts += list(10.0 + 0.625 * np.arange(1, 4))      # 10..12 s at 0.8x pacing
    cuts += list(np.arange(12.0, 28.0, 0.5))
    return cuts


def test_warp_invariants():
    r = plan_retime(make_song(), make_video(slow_section_cuts()), 0.0)
    w = np.asarray(r["warp"])
    assert np.all(np.diff(w[:, 0]) > 0) and np.all(np.diff(w[:, 1]) > 0)
    sp = np.diff(w[:, 0]) / np.diff(w[:, 1])
    assert sp.min() >= 0.75 - 1e-3 and sp.max() <= 1.25 + 1e-3
    assert all(abs(s["speed"] - 1) > 0.01 for s in r["sections"])
    assert r["out_duration"] == w[-1, 1]


def test_slow_section_found():
    song, vid = make_song(), make_video(slow_section_cuts())
    r = plan_retime(song, vid, 0.0)
    assert r["sync_after"] >= 95 > r["sync_before"]
    sp = [s for s in r["sections"] if s["v_start"] < 12 and s["v_end"] > 10]
    assert sp and abs(sp[0]["speed"] - 1.25) < 0.1


def test_no_change_when_synced():
    r = plan_retime(make_song(), make_video(np.arange(0.5, 28, 0.5)), 0.0)
    assert r["sections"] == [] and r["sync_after"] >= r["sync_before"]


def test_speed_dev_respected():
    r = plan_retime(make_song(), make_video(slow_section_cuts()), 0.0, max_speed_dev=0.1)
    for s in r["sections"]:
        assert 0.9 - 0.02 <= s["speed"] <= 1.1 + 0.02


def test_apply_warp_remaps_times():
    vid = make_video([1.0, 2.0])
    w = apply_warp(vid, [[0, 0], [1, 0.5], [30, 29.5]])
    assert abs(w["cut_times"][0] - 0.5) < 1e-6 and abs(w["duration"] - 29.5) < 1e-6


def test_tempo_ratio_and_octave():
    t = plan_tempo(make_song(bpm=120), make_video([], tempo=115.2))
    assert abs(t["ratio"] - 0.96) < 1e-3 and t["bpm_to"] < 120
    t = plan_tempo(make_song(bpm=60), make_video([], tempo=124.0))       # octave: 60*2 ~ 124
    assert abs(t["ratio"] - 124 / 120) < 1e-3
    t = plan_tempo(make_song(bpm=120), make_video([], tempo=150.0))
    assert t["ratio"] == 1.08 and t["clamped"]


def test_apply_tempo():
    s = apply_tempo(make_song(60, 120), 1.05)
    assert abs(s["bpm"] - 126) < 1e-6 and abs(s["duration"] - 60 / 1.05) < 1e-6
    assert abs(s["beat_times"][2] - 1.0 / 1.05) < 1e-6
