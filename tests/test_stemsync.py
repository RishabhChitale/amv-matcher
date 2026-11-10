"""stemsync: stem targets in quiet sections, quiet_sync scoring, and the unchanged stem_targets=None path."""
import numpy as np

from amv_matcher import score as SC
from amv_matcher import stemsync as SS

LEAD = SC.PERCEPTUAL_LEAD_S


def _song():
    beats = [round(0.5 * k, 3) for k in range(1, 41)]                   # 120 bpm, 20 s
    return {"bpm": 120.0, "beat_times": beats, "duration": 20.0, "energy_curve": [0.2] * 80 + [0.9] * 120,
            "lulls": [[0.0, 8.0]], "drops": [8.0],
            "sections": [{"start": 0.0, "end": 8.0, "label": "intro", "energy": 0.2},
                         {"start": 8.0, "end": 20.0, "label": "chorus", "energy": 0.9}]}


def _feats():
    # soft hi-hat ticks on the OFF-beats of the quiet section, plus a loud-section onset that must be ignored
    detail = [{"t": 0.25 + 0.5 * k, "stem": "drums", "strength": 0.8} for k in range(16)]
    return {"quiet_detail": detail + [{"t": 12.25, "stem": "drums", "strength": 1.0}],
            "vocal_phrase_starts": [2.0, 15.0], "stems": {"bass": {"onsets": [1.0, 13.0], "onset_strength": [0.5, 0.9]}}}


def _video(times, calm_end=8.0, dur=10.0):
    return {"duration": dur, "cut_times": [t - LEAD for t in times], "motion_peaks": [],
            "energy_curve": [0.3] * int(dur * 10), "motion_tempo_bpm": 120.0, "lulls": [], "peak_segments": [],
            "segments_struct": [{"start": 0.0, "end": calm_end, "kind": "calm", "energy": 0.2},
                                {"start": calm_end, "end": dur, "kind": "peak", "energy": 0.9}]}


def test_targets_only_inside_quiet_sections():
    tg = SS.stem_targets(_song(), _feats())
    ts = [d["t"] for d in tg]
    assert all(0.0 <= t <= 8.0 for t in ts)                              # nothing in the loud section
    assert 12.25 not in ts and 15.0 not in ts and 13.0 not in ts
    assert {d["kind"] for d in tg} == {"detail", "vocal", "bass"}
    assert all(0.3 <= d["weight"] <= 0.7 for d in tg)
    assert [d for d in tg if d["kind"] == "vocal"][0]["t"] == 2.0 or any(d["t"] == 2.0 for d in tg)
    assert ts == sorted(ts)


def test_targets_empty_without_quiet_or_feats():
    s = _song()
    s["lulls"], s["sections"] = [], []
    assert SS.stem_targets(s, _feats()) == []
    assert SS.stem_targets(_song(), {}) == []


def test_quiet_sync_high_with_stems_lower_without():
    song = _song()
    tg = SS.stem_targets(song, _feats())
    off_beats = [0.25 + 0.5 * k for k in range(14)]                       # calm-section events on the ticks
    v = _video(off_beats)
    with_t = SC.score_at(song, v, 0.0, stem_targets=tg)
    without = SC.score_at(song, v, 0.0, stem_targets=[])                  # empty targets: beats only
    assert with_t["quiet_sync"] >= 90.0
    assert without["quiet_sync"] < with_t["quiet_sync"] - 50.0
    assert with_t["sync_pct"] >= without["sync_pct"]


def test_loud_events_unaffected():
    song = _song()
    tg = SS.stem_targets(song, _feats())
    v = _video([8.25 + 0.5 * k for k in range(3)], calm_end=0.0 + 1e-3)   # every event in the peak block
    a = SC.score_at(song, v, 0.0)
    b = SC.score_at(song, v, 0.0, stem_targets=tg)
    assert a["sync_pct"] == b["sync_pct"] and a["total_pct"] == b["total_pct"]
    assert b["quiet_events"] == 0


def test_none_is_identical_to_today():
    song = _song()
    v = _video([0.25 + 0.5 * k for k in range(14)])
    a = SC.score_at(song, v, 0.0)
    assert "quiet_sync" not in a
    assert SC.score_at(song, v, 0.0, stem_targets=None) == a
    s1 = SC.score(song, v)
    assert "quiet_sync" not in s1
    assert SC.score(song, v, stem_targets=None) == s1


def test_score_scan_with_targets_adds_quiet_sync():
    song = _song()
    tg = SS.stem_targets(song, _feats())
    v = _video([0.25 + 0.5 * k for k in range(14)])
    r = SC.score(song, v, stem_targets=tg)
    assert r["eligible"] and "quiet_sync" in r and r["total_pct"] >= SC.score(song, v)["total_pct"]
