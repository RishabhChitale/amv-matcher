"""P0: the user's own cut-driven edit must keep its drops on its cuts.

1. retime must not slide cuts that already sit on a beat by a whole beat (a beat slip keeps sync but moves every
   drop off the cut the editor put it on): retime.DISP_COST prefers the nearest beat among equal hits;
2. a hard cut counts as a picture hit in the F21 drop match (boom.drop_matches), not only motion/flash booms.
"""
import numpy as np

from amv_matcher import boom, retime
from amv_matcher import score as S
from amv_matcher.retime import _wmap, plan_retime

HZ = 10
PER = 0.48


def _song(duration=80.0):
    bt = np.arange(0.18, duration, PER)
    return {"bpm": 60.0 / PER, "beat_times": bt.tolist(), "beat_strengths": [1.0] * len(bt), "duration": duration,
            "energy_curve": np.full(int(duration * HZ), 0.5).tolist(), "energy_hz": HZ, "drops": [], "lulls": []}


def _video(cuts, duration=75.0):
    return {"duration": duration, "fps": 30, "energy_curve": np.full(int(duration * HZ), 0.5).tolist(),
            "energy_hz": HZ, "segments": [], "lulls": [], "peak_segments": [], "shots": [],
            "cut_times": [float(c) for c in cuts], "motion_curve": [], "motion_peaks": [], "motion_tempo_bpm": 60.0 / PER}


def _own_edit_cuts():
    lead = S._lead(None)
    on = 0.18 + PER * np.array([25, 33, 40, 48, 56, 70, 80, 90, 104, 112, 120, 128, 136, 144])  - lead   # on the beat
    off = 0.18 + PER * np.array([8.6, 10.7, 12.4, 18.5, 63.4])                                       # between beats
    return np.sort(np.concatenate([on, off])), on


def test_retime_keeps_on_beat_cuts_on_their_own_beat():
    cuts, on = _own_edit_cuts()
    r = plan_retime(_song(), _video(cuts), 0.0, lead_s=None)
    moved = np.abs(_wmap(r["warp"], on) - on)
    assert np.percentile(moved, 90) < 0.1, moved.round(3).tolist()        # no whole-beat (0.48 s) slip


def test_drop_on_a_hard_cut_is_matched():
    song = {"booms": [{"t": 30.0, "conf": 1.0, "class": "drop"}], "main_boom": 30.0}
    vid = {"visual_booms": [{"t": 26.0, "conf": 0.9, "kind": "motion"}], "cut_times": [29.95]}
    r = boom.drop_matches(song, {"offset": 0.0}, vid, 60.0, lead=0.06)
    m = r["drop_matches"][0]
    assert r["drops_matched"] == 1 and m["vis_kind"] == "cut" and m["status"] == "green"
    r2 = boom.drop_matches(song, {"offset": 0.0}, {"visual_booms": vid["visual_booms"]}, 60.0, lead=0.06)
    assert r2["drops_matched"] == 0                                       # without the cut: the boom is 4 s away


def test_own_cut_driven_edit_matches_most_drops_after_retime():
    cuts, on = _own_edit_cuts()
    lead = S._lead(None)
    drops = [float(t + lead) for t in on[[1, 4, 8, 12]]]                  # the song's drops sit on the edit's cuts
    song = dict(_song(), booms=[{"t": t, "conf": 1.0, "class": "drop"} for t in drops], main_boom=drops[0])
    vid = _video(cuts)
    r = plan_retime(song, vid, 0.0, lead_s=None)
    out = retime.apply_warp(vid, r["warp"])
    m = boom.drop_matches(song, {"offset": 0.0}, out, float(out["duration"]), lead=lead)
    assert m["drops_matched"] >= 3 and m["drops_total"] == 4, [(x["t_out"], x["delta_ms"]) for x in m["drop_matches"]]
