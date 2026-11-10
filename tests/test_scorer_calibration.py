"""Scorer calibration: flash suppression, negative offsets, constant-tempo grid fit."""
import cv2
import numpy as np

from amv_matcher.audio import analyze_song
from amv_matcher.score import MIN_OFFSET_S, score
from amv_matcher.video import analyze_video
from tests.synth import make_click_track


def _scene_video(path, flash_frames=(), cuts=(3.0, 6.0), seconds=9.0, fps=30, size=(320, 240)):
    """Two alternating textured scenes (hard cuts at `cuts`), optional white-flash frames."""
    w, h = size
    rng = np.random.default_rng(1)
    scenes = [rng.integers(0, 120, (h, w, 3), dtype=np.uint8),
              rng.integers(100, 255, (h, w, 3), dtype=np.uint8)]
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, size)
    assert vw.isOpened()
    for f in range(int(seconds * fps)):
        n = sum(1 for c in cuts if f / fps >= c)
        img = scenes[n % 2].copy()
        if f in flash_frames:
            img[:] = 255
        vw.write(img)
    vw.release()


def test_flash_is_not_a_cut(tmp_path):
    plain, flashed = tmp_path / "plain.avi", tmp_path / "flash.avi"
    _scene_video(plain)
    _scene_video(flashed, flash_frames=(135, 136))      # 2-frame white flash at 4.5 s
    n_plain = len(analyze_video(str(plain))["cut_times"])
    v = analyze_video(str(flashed))
    assert n_plain == 2
    assert len(v["cut_times"]) == 2, v["cut_times"]
    assert all(abs(t - 4.5) > 0.3 for t in v["cut_times"])


def _flat_video(events, vd):
    n = int(vd * 10)
    return {"duration": vd, "cut_times": list(events), "motion_peaks": [], "energy_curve": [0.5] * n,
            "peak_segments": [], "lulls": [], "motion_tempo_bpm": 120.0}


def _song(beats, sd):
    return {"duration": sd, "bpm": 120.0, "beat_times": list(beats), "energy_curve": [0.5] * int(sd * 10),
            "drops": [], "lulls": []}


def test_negative_offset_found():
    rng = np.random.default_rng(7)
    events = np.sort(rng.uniform(2, 28, 14))
    song = _song(np.sort(np.r_[events - 1.37, rng.uniform(0, 60, 20)]), 60.0)
    video = _flat_video(events, 30.0)
    r = score(song, video, allow_negative=True)
    assert MIN_OFFSET_S <= r["best_offset_s"] < 0
    assert abs(r["best_offset_s"] + 1.37) <= 0.1, r     
    assert r["trim_start_s"] == r["best_offset_s"]
    assert score(song, video)["best_offset_s"] >= 0             # default: splice-safe, no negatives


def test_constant_tempo_grid_fit(tmp_path):
    p = tmp_path / "c120.wav"
    make_click_track(p, 120, seconds=40)
    s = analyze_song(str(p), cache_path=tmp_path / "c.json")
    assert s["grid_used"] and abs(s["grid_bpm"] - 120) <= 1.0, s["grid_bpm"]
    assert s["grid_fit_ms"] <= 30
    gaps = np.diff(s["beat_grid"])
    assert np.allclose(gaps, gaps[0], atol=1e-3)                 # perfectly constant tempo


def test_tightness_metric():
    from amv_matcher.score import score_at, tightness
    beats = [i * 0.5 for i in range(1, 121)]
    song = {"bpm": 120.0, "beat_times": beats, "duration": 60.0, "energy_curve": [0.5] * 60,
            "drops": [], "lulls": []}

    def vid(cuts):
        return {"cut_times": cuts, "motion_peaks": [], "duration": 20.0, "energy_curve": [0.5] * 20,
                "motion_tempo_bpm": 120.0, "motion_tempo_candidates": []}
    on = tightness(song, vid([1.0 + 0.5 * i for i in range(20)]), 0.0)
    assert on["tight_pct"] == 100.0 and on["median_err_ms"] < 1 and on["n_events"] == 20
    late = tightness(song, vid([1.0 + 0.5 * i + 0.030 for i in range(20)]), 0.0)
    assert late["tight_pct"] == 100.0 and 29 < late["median_err_ms"] < 31
    off = tightness(song, vid([1.0 + 0.5 * i + 0.100 for i in range(20)]), 0.0)
    assert off["tight_pct"] == 0.0 and 99 < off["p90_err_ms"] < 101
    assert score_at(song, vid([1.0, 1.5, 2.0, 2.5]), 0.0)["tightness"]["n_events"] == 4
    assert tightness(song, vid([]), 0.0)["median_err_ms"] is None
