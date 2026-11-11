"""F20: an own-soundtrack edit whose cuts sit consistently on the off-beat is syncopated, not out of sync.

score_at(offbeat=True) (the fixed-offset own-audio path) credits a half-beat lock with OFFBEAT_W; the default
(offset scans, auto-sync planners) stays on-beat only, so no planner ever prefers an off-beat placement.
"""
import numpy as np

from amv_matcher.score import OFFBEAT_W, PERCEPTUAL_LEAD_S, score, score_at


def _song(sd=40.0, T=0.5):
    beats = np.arange(0.25, sd, T)
    return {"duration": sd, "bpm": 60.0 / T, "beat_times": list(beats), "energy_curve": [0.5] * int(sd * 10),
            "drops": [], "lulls": []}


def _video(events, vd=30.0):
    return {"duration": vd, "cut_times": list(events), "motion_peaks": [], "energy_curve": [0.5] * int(vd * 10),
            "peak_segments": [], "lulls": [], "motion_tempo_bpm": 120.0}


def _events(phase, T=0.5, n=40, seed=3):
    rng = np.random.default_rng(seed)
    k = np.sort(rng.choice(np.arange(2, 58), n, replace=False))
    return 0.25 + k * T + phase * T - PERCEPTUAL_LEAD_S       # cuts at `phase` of a beat, with the usual lead


def test_offbeat_edit_gets_partial_credit():
    song = _song()
    off = _video(_events(0.5))
    plain = score_at(song, off, 0.0)["sync_pct"]
    credited = score_at(song, off, 0.0, offbeat=True)["sync_pct"]
    on = score_at(song, _video(_events(0.0)), 0.0)["sync_pct"]
    assert plain < 20 and on > 80, (plain, on)
    assert credited > plain + 40
    assert abs(credited - OFFBEAT_W * on) < 0.1 * on, (credited, on)


def test_onbeat_edit_unchanged():
    song, v = _song(), _video(_events(0.0))
    a, b = score_at(song, v, 0.0), score_at(song, v, 0.0, offbeat=True)
    assert a["sync_pct"] == b["sync_pct"] and a["total_pct"] == b["total_pct"]


def test_random_cuts_get_no_offbeat_credit():
    rng = np.random.default_rng(11)
    song, v = _song(), _video(np.sort(rng.uniform(1, 29, 40)))
    assert score_at(song, v, 0.0, offbeat=True)["sync_pct"] < 30


def test_default_and_scan_stay_on_beat():
    """Planners and offset scans never use the off-beat credit: a free offset finds the on-beat placement."""
    song, v = _song(), _video(_events(0.5))
    r = score(song, v)
    ph = (r["best_offset_s"] / 0.5) % 1.0
    assert min(ph, 1 - ph) > 0.3, r["best_offset_s"]           # scan shifts the song by ~half a beat
    assert r["sync_pct"] > 80
