"""P3: a surge (the picture's motion jumping from calm to a sustained high) is an extra picture hit for
"drops matched" only: kept apart from visual_booms, so the boom bonus, auto-sync anchors and scores never see it."""
import numpy as np

from amv_matcher import boom as B

FPS = 60.0


def _motion(surge_t, dur=20.0):
    m = np.full(int(dur * FPS), 1.0)
    m[int(surge_t * FPS):int((surge_t + 1.5) * FPS)] = 10.0
    return m


def test_surge_found_at_the_rise():
    out = B.surge_booms(_motion(8.0), FPS)
    assert len(out) == 1 and abs(out[0]["t"] - 8.0) <= 0.25 and out[0]["kind"] == "surge" and 0 < out[0]["conf"] <= 1


def test_flat_motion_has_no_surge():
    assert B.surge_booms(np.full(1200, 3.0), FPS) == []


def test_surge_on_an_existing_visual_boom_is_not_added():
    assert B.surge_booms(_motion(8.0), FPS, existing=[8.1]) == []


def test_ensure_surges_is_lazy_and_leaves_visual_booms_alone():
    v = {"motion_curve": _motion(8.0).tolist(), "fps": FPS, "visual_booms": [{"t": 3.0, "conf": 1.0}], "duration": 20.0}
    v2 = B.ensure_surges(v)
    assert v2["visual_booms"] == v["visual_booms"] and len(v2["surge_booms"]) == 1 and "surge_booms" not in v
    assert B.ensure_surges(v2) is v2


def _song(t):
    return {"booms": [{"t": t, "conf": 1.0, "class": "drop"}], "main_boom": t, "duration": 30.0}


def test_drop_matches_counts_a_surge_hit_unless_surge_off():
    v = {"motion_curve": _motion(8.0).tolist(), "fps": FPS, "visual_booms": [], "cut_times": [2.0], "duration": 20.0}
    t = B.ensure_surges(v)["surge_booms"][0]["t"]
    on = B.drop_matches(_song(t), {"offset": 0.0}, v, 20.0)
    off = B.drop_matches(_song(t), {"offset": 0.0}, v, 20.0, surge=False)
    assert on["drops_matched"] == 1 and on["drop_matches"][0]["vis_kind"] == "surge" and off["drops_matched"] == 0


def test_boom_stats_ignores_surges():
    v = {"motion_curve": _motion(8.0).tolist(), "fps": FPS, "visual_booms": [{"t": 3.0, "conf": 1.0}], "duration": 20.0}
    s = _song(B.ensure_surges(v)["surge_booms"][0]["t"])
    assert B.boom_stats(s, B.ensure_surges(v), 0.0) == B.boom_stats(s, v, 0.0)
