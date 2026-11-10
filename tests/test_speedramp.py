"""speedramp (U11a) on synthetic analysis dicts: eased curves, constraints, ramp directions."""
import numpy as np

from amv_matcher import speedramp as SR
from amv_matcher.retime import apply_warp
from test_retime import make_song, make_video


def _ramp_cuts(i0, i1, dur=28.0):
    """Cut times whose source interval glides linearly i0 -> i1 over dur source seconds."""
    t, cuts = 0.0, []
    while True:
        iv = i0 + (i1 - i0) * min(t / dur, 1.0)
        t += iv
        if t >= dur:
            return cuts
        cuts.append(t)


def test_curve_integral_and_limits():
    pts = [{"t_src": 0.0, "speed": 1.0, "ease_in_s": 0, "ease_out_s": 0},
           {"t_src": 10.0, "speed": 1.2, "ease_in_s": 1.0, "ease_out_s": 1.0}]
    warp, s = SR.warp_from_points(pts, 20.0)
    assert abs(s[0] - 1.0) < 1e-9 and abs(s[-1] - 1.2) < 1e-9
    assert s.min() >= 1.0 - 1e-9 and s.max() <= 1.2 + 1e-9
    assert np.all(np.diff(warp[:, 1]) > 0)
    # integral: out = 10 (speed 1) + ramp (2 src s at mean ~1.1) + 8/1.2 ...
    assert 17.0 < warp[-1, 1] < 18.5
    # smooth: no instant jumps
    assert SR.stats(warp)["max_jump"] < 0.01


def test_accel_limit_sets_ease_width():
    pts = SR.points_from([1.0, 1.25], [10.0], 30.0, max_accel=0.5, ease_max_s=4.0, ease="smoothstep")
    p = pts[1]
    warp, s = SR.warp_from_points(pts, 30.0)
    assert SR.stats(warp)["max_accel"] <= 0.5 * 1.15            # measured on a 0.1 s grid
    assert p["ease_in_s"] > 0.3


def test_direction_decel_video_speeds_up():
    song = make_song()
    vid = make_video(_ramp_cuts(0.58, 0.58), duration=28.0)         # slow pacing: needs speed ~1.16
    r = SR.plan_ramps(song, vid, 0.0, 0.25)
    assert r["speed_range"][0] > 1.08


def test_direction_accelerating_video_ramps_down():
    song = make_song()
    vid = make_video(_ramp_cuts(0.57, 0.46), duration=28.0)
    r = SR.plan_ramps(song, vid, 0.0, 0.25)
    pts = r["points"]
    assert len(pts) >= 3                                          # intermediate speeds, not a two-step
    dirs = [d for _t, d in SR.ramp_directions(pts)]
    assert dirs.count(-1) > dirs.count(1)
    assert pts[0]["speed"] > pts[-1]["speed"] + 0.08
    assert r["sync_after"] >= r["sync_before"]


def test_direction_decelerating_video_ramps_up():
    song = make_song()
    vid = make_video(_ramp_cuts(0.46, 0.57), duration=28.0)
    r = SR.plan_ramps(song, vid, 0.0, 0.25)
    pts = r["points"]
    assert len(pts) >= 3
    dirs = [d for _t, d in SR.ramp_directions(pts)]
    assert dirs.count(1) > dirs.count(-1)
    assert pts[-1]["speed"] > pts[0]["speed"] + 0.08


def test_ramp_smoother_than_step_and_constraints():
    song = make_song()
    vid = make_video(_ramp_cuts(0.57, 0.46), duration=28.0)
    r = SR.plan_ramps(song, vid, 0.0, 0.25)
    assert r["smooth_ramp"]["max_jump"] < 0.02 <= r["smooth_step"]["max_jump"] + 1
    assert r["smooth_ramp"]["max_accel"] <= 0.5 * 1.2
    lo, hi = r["speed_range"]
    assert lo >= 0.75 - 1e-3 and hi <= 1.25 + 1e-3
    w = np.asarray(r["warp"])
    assert np.all(np.diff(w[:, 0]) > 0) and np.all(np.diff(w[:, 1]) > 0)
    assert abs(apply_warp(vid, r["warp"])["duration"] - r["out_duration"]) < 0.05
