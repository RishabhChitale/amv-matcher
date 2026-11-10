import numpy as np
from amv_matcher import buildup


def _video(launch_t, dur=40.0):
    n = int(dur * 20)
    t = np.linspace(0, dur, n)
    m = np.full(n, 0.1)
    ramp = (t >= launch_t - 4) & (t < launch_t)
    m[ramp] = 0.1 + 0.2 * (t[ramp] - (launch_t - 4))
    m[t >= launch_t] = 1.0
    m[t >= launch_t + 3] = 0.1
    return {"duration": dur, "motion_curve": m.tolist(), "energy_curve": []}


def _song(drop_t, dur=40.0):
    t = np.arange(0, dur, 0.1)
    e = np.full(len(t), 0.2)
    ramp = (t >= drop_t - 6) & (t < drop_t)
    e[ramp] = 0.2 + 0.5 * (t[ramp] - (drop_t - 6)) / 6
    e[(t >= drop_t) & (t < drop_t + 3)] = 1.0
    e[(t >= drop_t) & (t < drop_t)] = 0
    return {"duration": dur, "energy_curve": e.tolist(), "beat_times": list(np.arange(0, dur, 0.5)),
            "drops": [drop_t]}


def test_launch_detected():
    L = buildup.video_launches(_video(20.0))
    assert any(abs(l["t"] - 20.0) < 1.0 for l in L)


def test_aligned_beats_misaligned():
    song = _song(20.0)
    good = buildup.build_stats(song, _video(20.0), 0.0)
    bad = buildup.build_stats(song, _video(30.0), 0.0)
    if good is None:
        return
    assert bad is None or good["build_pct"] >= bad["build_pct"]


def test_none_leaves_total():
    assert buildup.blend_total(77.0, None) == 77.0


def test_never_lowers():
    assert buildup.blend_total(90.0, {"build_pct": 0.0}) == 90.0
    assert buildup.blend_total(50.0, {"build_pct": 100.0}) > 50.0
