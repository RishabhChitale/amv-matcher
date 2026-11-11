"""launchsync (U8b): launch->drop anchor pairing, feasibility, forced anchors in the anchored ramp planner."""
import numpy as np

from amv_matcher import launchsync as L
from amv_matcher import speedramp as SR
from test_retime import make_song, make_video
from test_speedramp import _ramp_cuts

IDENT = [[0.0, 0.0], [200.0, 200.0]]


def _hit(t, kind="launch", s=0.8):
    return {"t": t, "strength": s, "kind": kind}


def test_pairs_nearest_hit_within_snap():
    hits = [_hit(10.0), _hit(58.0), _hit(97.9, "impact")]
    a = L.plan_launch_anchors({}, {}, 0.0, IDENT, hits=hits, drops=[(60.0, 1.0), (100.0, 0.5)], lead_s=0.0)
    assert [x["t_src"] for x in a] == [58.0, 97.9]
    assert [x["kind"] for x in a] == ["launch", "impact"]
    assert a[0]["out_t"] == 60.0 and abs(a[0]["shift_s"] - 2.0) < 1e-6
    assert a[0]["drop"] == 60.0


def test_no_pair_beyond_snap_and_each_hit_used_once():
    a = L.plan_launch_anchors({}, {}, 0.0, IDENT, hits=[_hit(50.0)], drops=[(60.0, 1.0)], lead_s=0.0)
    assert a == []
    a = L.plan_launch_anchors({}, {}, 0.0, IDENT, hits=[_hit(58.0)], drops=[(59.0, 1.0), (61.0, 1.0)], lead_s=0.0)
    assert len(a) == 1


def test_offset_and_lead_shift_target():
    a = L.plan_launch_anchors({}, {}, 5.0, IDENT, hits=[_hit(53.0)], drops=[(60.0, 1.0)], lead_s=0.1)
    assert abs(a[0]["out_t"] - 54.9) < 1e-6


def test_feasibility_uses_speed_limits_and_chain():
    # hit 40 -> drop 60 needs speed 0.667 (infeasible); hit 100 -> drop 101 measured from the origin: 0.99 (ok)
    hits = [_hit(40.0), _hit(100.0)]
    a = L.plan_launch_anchors({}, {}, 0.0, IDENT, hits=hits, drops=[(41.0, 1.0), (101.0, 1.0)], lead_s=0.0)
    assert [x["feasible"] for x in a] == [True, True]
    a = L.plan_launch_anchors({}, {}, 0.0, IDENT, hits=[_hit(40.0), _hit(100.0)], drops=[(60.0, 1.0), (101.0, 1.0)],
                              lead_s=0.0, snap_s=25.0)
    assert not a[0]["feasible"] and abs(a[0]["speed_needed"] - 40.0 / 60.0) < 1e-3
    assert a[1]["feasible"]                      # measured from the origin, the infeasible anchor is skipped
    assert len(L.anchors_for_ramps(a)) == 1


def test_residuals_zero_when_warp_hits_target():
    a = L.plan_launch_anchors({}, {}, 0.0, IDENT, hits=[_hit(59.0)], drops=[(60.0, 1.0)], lead_s=0.0)
    assert L.residuals_ms(a, IDENT) == [-1000.0]
    w = [[0.0, 0.0], [59.0, 60.0], [200.0, 201.0]]
    assert abs(L.residuals_ms(a, w)[0]) < 1e-6


def test_forced_anchor_is_met_and_plan_stays_bounded():
    song = make_song()
    vid = make_video(_ramp_cuts(0.57, 0.46), duration=28.0)
    base = SR.plan_ramps(song, vid, 0.0, 0.25, mode="anchored")
    warp = np.asarray(base["warp"])
    t_src = 14.0
    out0 = float(np.interp(t_src, warp[:, 0], warp[:, 1]))
    r = SR.plan_ramps(song, vid, 0.0, 0.25, mode="anchored", extra_anchors=[(t_src, out0 + 0.15, 3.0)])
    w = np.asarray(r["warp"])
    got = float(np.interp(t_src, w[:, 0], w[:, 1]))
    assert abs(got - (out0 + 0.15)) < 0.03
    assert r["anchored"]["forced"] == 1 and r["anchored"]["forced_err_ms"][0] < 30.0
    lo, hi = r["speed_range"]
    assert lo >= 0.75 - 1e-6 and hi <= 1.25 + 1e-6
    assert np.all(np.diff(w[:, 0]) > 0) and np.all(np.diff(w[:, 1]) > 0)
    assert SR.stats(w)["max_jump"] < 0.03


def test_forced_anchor_never_dropped_even_when_error_large():
    song = make_song()
    vid = make_video(_ramp_cuts(0.57, 0.46), duration=28.0)
    pts, info = SR.plan_anchored(song, vid, 0.0, SR_step(song, vid), extra_anchors=[(14.0, 15.0, 3.0)])
    assert info["forced"] == 1 and len(info["forced_err_ms"]) == 1


def SR_step(song, vid):
    from amv_matcher import retime as R
    return R.plan_retime(song, vid, 0.0, 0.25)


def test_tolerance_leaves_close_hits_and_pulls_far_ones_only_to_the_edge():
    a = L.plan_launch_anchors({}, {}, 0.0, IDENT, hits=[_hit(59.7), _hit(98.0)], drops=[(60.0, 1.0), (100.0, 1.0)],
                              lead_s=0.0, tol_s=0.5)
    assert a[0]["ok"] and not a[0]["feasible"] and a[0]["weight"] == 0.0
    assert not a[1]["ok"] and abs(a[1]["out_t"] - 99.6) < 1e-6
    assert len(L.anchors_for_ramps(a)) == 1
