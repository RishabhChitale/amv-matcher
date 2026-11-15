"""P2: a drop marker moves to the boom's bass entry (> 0.5 s off) or back to its pre-downbeat-snap time only when
the pair's picture hit lands there and none lands on the boom itself (the user's own edits cut on the bass entry)."""
import numpy as np

from amv_matcher import boom as B

FPS = 100.0


def _low(entry_t, dur=20.0):
    x = np.full(int(dur * FPS), -40.0)
    x[int(entry_t * FPS):] = -10.0
    return x


def test_bass_entry_far_from_the_boom_is_stored():
    out = [{"t": 10.0}]
    B._bass_entry(out, _low(10.8), FPS, np.arange(0.0, 20.0, 0.4))
    assert out[0]["bass_t"] == 10.8


def test_bass_entry_on_the_boom_is_not_an_alternative():
    out = [{"t": 10.0}]
    B._bass_entry(out, _low(10.1), FPS, np.array([]))
    assert "bass_t" not in out[0]


def _song(**b):
    return {"booms": [dict({"t": 10.0, "conf": 1.0, "class": "drop"}, **b)], "main_boom": 10.0, "duration": 30.0}


def _m(song, cuts, vis=()):
    r = B.drop_matches(song, {"offset": 0.0}, {"cut_times": list(cuts), "visual_booms": [{"t": t, "conf": 1.0} for t in vis]}, 30.0)
    return r["drop_matches"][0], r["drops_matched"]


def test_marker_moves_to_the_bass_entry_when_the_picture_cuts_there():
    row, n = _m(_song(bass_t=10.9), [10.92])
    assert n == 1 and row["t_out"] == 10.9 and row["moved_from"] == 10.0 and row["moved_why"] == "bass_entry"


def test_marker_stays_when_the_picture_hits_the_boom():
    row, n = _m(_song(bass_t=10.9), [10.9], vis=[10.02])
    assert n == 1 and row["t_out"] == 10.0 and "moved_from" not in row


def test_marker_stays_when_no_picture_hit_agrees():
    row, n = _m(_song(bass_t=10.9), [11.4])
    assert n == 0 and row["t_out"] == 10.0 and "moved_from" not in row


def test_downbeat_snap_does_not_take_a_boom_off_its_cut():
    row, n = _m(_song(t=10.26, downbeat_shift=0.26), [10.0])
    assert n == 1 and row["t_out"] == 10.0 and row["moved_why"] == "pre_snap"


def test_song_drops_carries_the_alternatives():
    d = B.song_drops(_song(downbeat_shift=0.5, bass_t=11.2))[0]
    assert d["alts"] == [(9.5, "pre_snap"), (11.2, "bass_entry")]
