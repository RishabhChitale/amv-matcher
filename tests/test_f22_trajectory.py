"""F22: intensity-trajectory coupling: reference-distance sub-score (flagged) + UI data block (synthetic only)."""
import numpy as np

from amv_matcher import score as S
from amv_matcher import trajectory as T


def _arc(n, period_s, phase=0.0, hz=10.0):
    t = np.arange(n) / hz
    return (0.5 + 0.5 * np.sin(2 * np.pi * t / period_s + phase)).tolist()


def _pair(seconds=90.0, phase=0.0):
    n = int(seconds * 10)
    video = {"duration": seconds, "energy_curve": _arc(n, 30.0), "energy_hz": 10.0,
             "subject_curve": _arc(n, 30.0), "subject_hz": 10.0, "cut_times": [], "fps": 24.0, "motion_peaks": [], "motion_curve": [],
             "visual_booms": [{"t": 37.5, "conf": 0.9, "strength": 1.0}]}
    song = {"duration": seconds, "energy_curve": _arc(n, 30.0, phase), "energy_hz": 10.0, "bpm": 120.0,
            "beat_times": np.arange(0, seconds, 0.5).tolist(), "downbeat_times": np.arange(0, seconds, 2.0).tolist(),
            "onset_env": _arc(n, 30.0, phase), "onset_env_fps": 10.0}
    return song, video


def test_reference_region_scores_100():
    assert T.sub_score(dict(T.REFERENCE)) == 100.0
    better = {"loc_r": 0.95, "agree": 0.9, "rise": 0.9}
    assert T.sub_score(better) == 100.0                     # more coupling than the weakest gold pair is not punished
    for k, (lo, med, hi) in T.GOLD_SPREAD.items():
        assert lo <= med <= hi
        if k in T.REFERENCE:
            assert T.REFERENCE[k] == lo and T.FLOOR[k] < lo


def test_flat_or_unrelated_song_scores_low():
    assert T.sub_score(dict(T.FLOOR)) == 0.0
    assert T.sub_score({"loc_r": -0.3, "agree": 0.2, "rise": 0.1}) == 0.0
    mid = {k: (T.REFERENCE[k] + T.FLOOR[k]) / 2 for k in T.REFERENCE}
    assert abs(T.sub_score(mid) - 50.0) < 0.1


def test_unrelated_pairs_score_near_chance_floor():
    rng = np.random.default_rng(0)

    def walk(n):                                             # smooth random curve (4 s box filter)
        return np.convolve(rng.standard_normal(n + 39), np.ones(40) / 40, mode="valid")

    null = [T.local_features(walk(1200), walk(1200)) for _ in range(60)]   # 120 s, independent song
    for k in ("agree", "rise"):                              # FLOOR is chance: ~0.5 for the sign features
        assert abs(np.mean([f[k] for f in null]) - T.FLOOR[k]) < 0.05
    subs = [T.sub_score(f) for f in null]
    assert np.mean(subs) < 45 and np.median(subs) < 40        # uncoupled pairs land well below the gold pairs
    coupled = []
    for _ in range(20):
        v = walk(1200)
        coupled.append(T.sub_score(T.local_features(v, v + 0.05 * walk(1200))))
    assert min(coupled) == 100.0 and np.mean(subs) < min(coupled) - 50


def test_local_features_short_and_opposed():
    n = 200                                                  # 20 s: under one local window -> whole-overlap r
    v = np.array(_arc(n, 10.0))
    f = T.local_features(v, v)
    assert f["loc_r"] > 0.99 and f["agree"] > 0.99 and f["rise"] == 1.0
    g = T.local_features(v, 1.0 - v)
    assert g["loc_r"] < -0.99 and g["agree"] < 0.01 and g["rise"] == 0.0
    assert T.local_features(v[:20], v[:20]) == {"loc_r": 0.0, "agree": 0.0, "rise": 0.0}


def test_traj_stats_following_vs_opposed():
    song, video = _pair()
    good = T.traj_stats(song, video, 0.0)
    assert good["follow"] > 0.95 and good["lag_s"] == 0.0 and good["peak"] > 0.95 and good["sub_score"] == 100.0
    song2, video2 = _pair(phase=np.pi)
    bad = T.traj_stats(song2, video2, 0.0)
    assert bad["follow"] < 0.2 and bad["sub_score"] < good["sub_score"] - 40


def test_weighted_total():
    assert T.weighted_total(80.0, None) == 80.0
    assert T.weighted_total(80.0, {"sub_score": None}) == 80.0
    assert abs(T.weighted_total(80.0, {"sub_score": 100.0}) - (80.0 + T.W_TRAJ * 20.0)) < 1e-9


def test_flag_off_pack_has_no_trajectory_and_same_total(monkeypatch):
    song, video = _pair()
    monkeypatch.setattr(T, "F22_ENABLED", False)
    off = S._pack(song, video, 0.0, 90.0, 1.0, 80.0, 70.0)
    assert "trajectory" not in off
    monkeypatch.setattr(T, "F22_ENABLED", True)
    on = S._pack(song, video, 0.0, 90.0, 1.0, 80.0, 70.0)
    assert on["trajectory"]["sub_score"] is not None
    monkeypatch.setattr(T, "F22_ENABLED", False)
    assert S._pack(song, video, 0.0, 90.0, 1.0, 80.0, 70.0) == off


def test_result_block_output_time_through_warp():
    song, video = _pair()
    # warp: original 0..90 s plays at 2x speed for the first 30 s, then 1x (output 15 s shorter)
    warp = [[0.0, 0.0], [30.0, 15.0], [90.0, 75.0]]
    r = T.result_block(song, video, {"offset": 0.0, "retime": {"warp": warp}})
    assert r["hz"] == T.UI_HZ
    assert len(r["video_curve"]) == len(r["song_curve"]) == int(75.0 * T.UI_HZ)
    # output 15 s = original 30 s: the video curve there equals the video trajectory at 30 s
    vt = T.video_trajectory(video)
    assert abs(r["video_curve"][int(15 * T.UI_HZ)] - round(float(vt[300]), 3)) < 1e-6
    assert [round(x["boom_t"], 2) for x in r["ramps"]] == [22.5]   # visual boom at 37.5 s -> output 15 + 7.5 = 22.5 s
    for k in ("follow", "lag_s", "dyn_video", "dyn_song", "ratio", "peak", "loc_r", "agree", "rise", "sub_score", "reference"):
        assert k in r


def test_result_block_offset_and_retime_ramps_preferred():
    song, video = _pair()
    plain = T.result_block(song, video, {"offset": 0.0})
    assert len(plain["video_curve"]) == int(90 * T.UI_HZ) and plain["follow"] > 0.95
    shifted = T.result_block(song, video, {"offset": 60.0})
    assert shifted["song_curve"][-1] is None                 # past the song end: no data, not a fake value
    w_r = [[0.0, 0.0], [90.0, 45.0]]
    both = T.result_block(song, video, {"offset": 0.0, "retime": {"warp": [[0.0, 0.0], [90.0, 90.0]]},
                                        "retime_ramps": {"warp": w_r}})
    assert len(both["video_curve"]) == int(45 * T.UI_HZ)


def test_result_block_never_raises():
    assert T.result_block({}, {}, None) is None
    assert T.result_block({"energy_curve": [1, 2]}, {"duration": 5.0}, {"offset": 0.0})["sub_score"] is None
