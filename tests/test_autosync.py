"""auto_sync tests (synthetic)."""
import time

import numpy as np

from amv_matcher.autosync import auto_sync
from tests.test_retime import make_song, make_video, slow_section_cuts


def test_slow_drift_fixed():
    cuts = np.arange(1, 56) * 0.52                      # 4% slow vs 120 BPM
    song, vid = make_song(90.0), make_video(cuts, duration=30.0, tempo=115.4)
    r = auto_sync(song, vid)
    assert r["final"]["sync_pct"] >= 95
    assert any(s["tool"] in ("tempo", "retime") and s["kept"] for s in r["steps"])


def test_slow_section_retimed():
    r = auto_sync(make_song(), make_video(slow_section_cuts()), min_gain=0.05)  # sync is near-saturated; 3 of ~50 cuts drift
    assert r["final"]["sync_pct"] >= 95
    rt = r["plan"]["retime"]
    assert rt and any(abs(s["speed"] - 1.25) < 0.1 for s in rt["sections"])


def test_kept_steps_monotonic():
    r = auto_sync(make_song(90.0), make_video(np.arange(1, 56) * 0.52, tempo=115.4))
    kept = [s for s in r["steps"] if s["kept"]]
    scores = [s["score_after"] for s in kept]
    assert scores == sorted(scores)
    for s in kept:
        assert s["score_after"] >= s["score_before"]
    assert set(r) >= {"steps", "final", "plan", "limits_hit", "why_not_100"}


def test_runtime_three_minute_pair():
    song = make_song(240.0)
    vid = make_video(np.arange(1, 340) * 0.52, duration=180.0, tempo=115.4)
    t = time.perf_counter()
    r = auto_sync(song, vid)
    assert time.perf_counter() - t < 10.0
    assert r["final"]["total_pct"] >= r["steps"][0]["score_before"] - 1


def test_honest_when_unreachable():
    vid = make_video(np.arange(0.5, 28, 0.5), tempo=120.0)
    vid["energy_curve"] = np.linspace(0, 1, len(vid["energy_curve"])).tolist()   # song energy flat: arc stuck
    r = auto_sync(make_song(), vid)
    assert r["final"]["total_pct"] < 100 and "unreachable" in r["why_not_100"]


# ---- real media (numeric only, nothing committed; run after score/curves calibration) ----
import glob
import os

import pytest

from tests import realmedia

_SONG = [realmedia.song_s1()] if realmedia.song_s1() else []
_VIDEO = [realmedia.video_a()] if realmedia.video_a() else []


@pytest.mark.slow
@pytest.mark.skipif(not (_SONG and _VIDEO), reason="real video A / song S1 not configured")
@pytest.mark.skipif(not os.environ.get("AMV_REAL"), reason="set AMV_REAL=1 (run after score/curves calibration)")
def test_real_pair_a_s1():
    from amv_matcher.audio import analyze_song
    from amv_matcher.video import analyze_video
    song, video = analyze_song(_SONG[0]), analyze_video(_VIDEO[0])
    t0 = time.time()
    r = auto_sync(song, video)
    print("\nelapsed", round(time.time() - t0, 1), "s")
    for s in r["steps"]:
        print(s["tool"], s.get("score_before"), "->", s.get("score_after"), "kept", s["kept"],
              "tight", s.get("tightness_before"), "->", s.get("tightness_after"))
    print("segments", len(r["plan"].get("segments") or []), "final", r["final"])
    first = r["steps"][0]["score_before"] if r["steps"] else 0
    assert r["final"]["total_pct"] >= first        # never worse than the start


# ---- revision 1: jump limit, energy alignment, best-path invariant, sync_headline ----
from amv_matcher import score as S
from amv_matcher.retime import MAX_JUMP, apply_warp, plan_retime
from amv_matcher.splice import _assemble, plan_splice
from amv_matcher.tempo import apply_tempo, plan_tempo


def _energy(n, steps):
    e = np.full(n, 0.2)
    for a, b, v in steps:
        e[int(a * 10):int(b * 10)] = v
    return e


def drop_fixture():
    """Song: calm, drop at 20 s (loud to 40 s), calm again. Video: peak segment 6-14 s whose cuts
    sit ~10% off the beat grid; the peak should land on the drop while cuts stay on beats."""
    song = make_song(60.0)
    song["energy_curve"] = _energy(600, [(20, 40, 0.9)]).tolist()
    song["drops"], song["lulls"] = [20.0], [[0.0, 6.0], [45.0, 60.0]]
    cuts = list(np.arange(0.5, 6.0, 0.5)) + list(6.0 + 0.55 * np.arange(0, 15))
    vid = make_video(cuts, 14.0)
    vid["energy_curve"] = _energy(140, [(6, 14, 0.9)]).tolist()
    vid["peak_segments"], vid["lulls"] = [[6.0, 14.0]], [[0.0, 5.0]]
    return song, vid


def peak_fixture():
    """Two peaks and a lull, drifting 6% slow; song has two drops and a lull in between."""
    song = make_song(90.0)
    song["energy_curve"] = _energy(900, [(10, 25, 0.9), (50, 70, 0.9)]).tolist()
    song["drops"], song["lulls"] = [10.0, 50.0], [[30.0, 48.0]]
    cuts = np.arange(1, 50) * 0.53
    vid = make_video(cuts, 26.0, tempo=113.0)
    vid["energy_curve"] = _energy(260, [(3, 12, 0.9), (20, 26, 0.9)]).tolist()
    vid["peak_segments"], vid["lulls"] = [[3.0, 12.0], [20.0, 26.0]], [[13.0, 19.0]]
    return song, vid


def _fixtures():
    return {
        "slow_drift": (make_song(90.0), make_video(np.arange(1, 56) * 0.52, 30.0, 115.4)),
        "slow_section": (make_song(), make_video(slow_section_cuts())),
        "synced": (make_song(), make_video(np.arange(0.5, 28, 0.5))),
        "drop": drop_fixture(),
        "peaks": peak_fixture(),
    }


def _single_tools(song, vid, **kw):
    """Total after applying each tool directly to the baseline (best offset where relevant)."""
    from amv_matcher.autosync import _best_offset, _tot
    off = _best_offset(song, vid) if float(song["duration"]) >= float(vid["duration"]) else 0.0
    out = {"offset": _tot(S.score_at(song, vid, off))}
    rp = plan_retime(song, vid, off)
    out["retime"] = _tot(S.score_at(song, apply_warp(vid, rp["warp"]), off))
    pt = plan_tempo(song, vid, 0.08)
    st = apply_tempo(song, pt["ratio"])
    out["tempo"] = _tot(S.score_at(st, vid, _best_offset(st, vid)))
    from amv_matcher.splice import _assemble, plan_splice
    sp = plan_splice(song, vid, 3, allow_negative=True)
    if sp.get("eligible") and len(sp["segments"]) > 1:
        asm = _assemble(song, sp["segments"], float(vid["duration"]))
        out["splice"] = _tot(S.score_at(asm, vid, 0.0))
    return out


_AUTO = {}


def _auto(name):
    """auto_sync result per fixture, computed once for the whole module."""
    if name not in _AUTO:
        song, vid = _fixtures()[name]
        _AUTO[name] = auto_sync(song, vid)
    return _AUTO[name]


@pytest.mark.slow
def test_speed_jump_limit_all_fixtures():
    for name, (song, vid) in _fixtures().items():
        rp = plan_retime(song, vid, 0.0)
        assert rp["max_speed_jump"] <= MAX_JUMP + 1e-9, name
        w = np.asarray(rp["warp"])
        sp = np.diff(w[:, 0]) / np.diff(w[:, 1])
        assert np.all(np.abs(np.diff(sp)) <= MAX_JUMP + 1e-3), name
        r = _auto(name)
        rt = r["plan"]["retime"]
        assert rt is None or rt["max_speed_jump"] <= MAX_JUMP + 1e-9, name
        assert "speed_jump_over_0.15" not in r["limits_hit"], name


def test_arc_rises_on_drop_peak_fixtures():
    for name in ("drop", "peaks"):
        song, vid = _fixtures()[name]
        r = _auto(name)
        assert r["final"]["arc_pct"] > 60, (name, r["final"])
        assert r["final"]["arc_pct"] >= S.score(song, vid)["arc_pct"] - 1.0, name


def test_final_at_least_best_single_tool():
    for name, (song, vid) in _fixtures().items():
        r = _auto(name)
        singles = _single_tools(song, vid)
        assert r["final"]["total_pct"] >= round(max(singles.values())) - 1, (name, r["final"], singles)


def test_sync_headline():
    for name, (song, vid) in _fixtures().items():
        f = _auto(name)["final"]
        assert f["sync_headline"] == round(0.6 * f["sync_pct"] + 0.4 * f["tempo_pct"]), name
        assert "total_pct" in f


# ---- round 2: negative offsets, tail ramp, budget ----
def _neg_pair():
    cuts = np.arange(0, 55) * 0.5 + 0.63              # beats at k*0.5: best offsets are -0.13 / -0.63
    return make_song(90.0), make_video(cuts, duration=28.0, tempo=120.0)


def test_negative_offset_auto_sync_valid_plan():
    song, vid = _neg_pair()
    r = auto_sync(song, vid, budget_s=8.0)
    base = r["steps"][0]["score_before"]
    assert r["final"]["total_pct"] >= base - 1
    assert "lead_silence_s" in r["plan"] and r["plan"]["lead_silence_s"] >= 0.0
    assert r["plan"]["offset"] >= -3.0 - 1e-9


def test_tail_ramps_to_speed_one_and_durations_reported():
    r = auto_sync(make_song(), make_video(slow_section_cuts()), min_gain=0.05)  # sync is near-saturated; 3 of ~50 cuts drift
    assert r["final"]["video_duration_s"] == 30.0
    assert r["final"]["out_duration_s"] > 0
    rt = r["plan"]["retime"]
    if rt:
        w = np.asarray(rt["warp"], dtype=float)
        spd = np.diff(w[:, 0]) / np.diff(w[:, 1])
        assert abs(spd[-1] - 1.0) < 1e-2 or w[-1][0] - w[-2][0] < 1e-6
        assert np.max(np.abs(np.diff(spd))) <= 0.15 + 1e-9


def test_budget_prunes_but_stays_valid():
    song = make_song(90.0)
    vid = make_video(np.arange(1, 56) * 0.52, duration=30.0, tempo=115.4)
    r = auto_sync(song, vid, budget_units=3)
    assert r["pruned"] and r["final"]["work_units"]["budget"] == 3.0 and "budget" in r["limits_hit"]
    assert r["final"]["total_pct"] >= r["steps"][0]["score_before"] - 1
    assert "elapsed_s" in r
    full = auto_sync(song, vid, budget_s=20.0)
    assert full["final"]["total_pct"] >= r["final"]["total_pct"]


def test_tightness_reported_and_lexicographic():
    """Steps and final carry tightness when the scorer emits it; the objective never trades tightness down at equal total."""
    song, video = make_song(), make_video(slow_section_cuts())
    if "tightness" not in S.score_at(song, video, 0.0):
        pytest.skip("scorer has no tightness block yet")
    r = auto_sync(song, video, min_gain=0.05)
    assert r["final"].get("tight_pct") is not None
    for st in r["steps"]:
        if st["kept"] and st["tightness_before"] and st["tightness_after"]:
            if abs(st["score_after"] - st["score_before"]) < 1e-9:
                assert st["tightness_after"]["tight_pct"] >= st["tightness_before"]["tight_pct"]


def test_retime_headline_tightness_uses_lead():
    """Retime lands cuts LEAD ahead of the beat, so the headline (lead-compensated) tightness is high."""
    song, video = make_song(), make_video(slow_section_cuts())
    if "tightness" not in S.score_at(song, video, 0.0):
        pytest.skip("scorer has no tightness block yet")
    rp = plan_retime(song, video, 0.0, lead_s=S.PERCEPTUAL_LEAD_S)
    t = S.score_at(song, apply_warp(video, rp["warp"]), 0.0)["tightness"]
    assert t["tight_pct"] >= 80 and t["median_err_ms"] < 20, t


def test_lead_s_clamped_recorded_and_zero_on_beat():
    song, video = make_song(), make_video(slow_section_cuts())
    assert auto_sync(song, video, lead_s=0.5, budget_s=5)["plan"]["lead_s"] == 0.12
    assert auto_sync(song, video, lead_s=-1, budget_s=5)["final"]["lead_s"] == 0.0
    r = auto_sync(song, video, budget_s=5)
    assert r["plan"]["lead_s"] == r["final"]["lead_s"] == S.PERCEPTUAL_LEAD_S
    rp = plan_retime(song, video, 0.0, lead_s=0.0)       # lead 0: events on the beat
    ev = np.asarray(apply_warp(video, rp["warp"])["cut_times"])
    b = np.asarray(song["beat_times"])
    err = np.abs(ev[:, None] - b[None, :]).min(axis=1)
    assert np.median(err) < 0.02


# ---- round 5: length rule + effort ----

def _short_pair():
    return make_song(20.0), make_video(np.arange(1, 56) * 0.5, duration=30.0)


def test_shorter_song_synced_over_video_stretch():
    # F2: a shorter song is no longer disqualified; it syncs to the best-fitting stretch of the video
    song, vid = _short_pair()
    r = auto_sync(song, vid)
    assert r["final"].get("eligible", True) is not False
    vw = r["plan"]["video_window"]
    assert abs((vw["end_s"] - vw["start_s"]) - 20.0) < 0.1
    assert r["final"]["video_use"]["label"].startswith("uses 0:20 of 0:30")


def test_tiny_song_still_ineligible():
    r = auto_sync(make_song(3.0), make_video(np.arange(1, 56) * 0.5, duration=30.0))
    f = r["final"]
    assert f["eligible"] is False and r["eligible"] is False
    assert f["total_pct"] == 0 and f["fit_after_effort"] == 0
    assert "song shorter than video" in (f.get("reason") or r.get("reason"))
    assert r["plan"] is None


def test_shorter_song_allow_loop_repeats():
    song, vid = _short_pair()
    r = auto_sync(song, vid, allow_loop=True)
    assert r["final"]["eligible"] is True and r["plan"]["looped"] is True
    segs = r["plan"]["segments"]
    assert len(segs) >= 2
    assert max(s["dst_start"] + (s["src_end"] - s["src_start"]) for s in segs) >= vid["duration"] - 1e-6
    assert r["plan"]["loop"]["repeats"] >= 2


def test_long_song_not_looped():
    r = auto_sync(make_song(90.0), make_video(np.arange(1, 56) * 0.5, duration=30.0))
    assert r["plan"]["looped"] is False


def test_untouched_plan_zero_effort():
    from amv_matcher.autosync import EFFORT_K, effort_of
    plan = {"tempo_ratio": 1.0, "segments": [{"src_start": 0, "src_end": 30, "dst_start": 0}], "retime": None}
    e = effort_of(plan, 30.0)
    assert e["effort_pct"] == 0 and e["sections"] == 0 and e["splices"] == 0
    heavy = {"tempo_ratio": 1.05,
             "segments": [{"src_start": 0, "src_end": 10, "dst_start": 0},
                          {"src_start": 40, "src_end": 50, "dst_start": 10},
                          {"src_start": 5, "src_end": 15, "dst_start": 20}],
             "retime": {"sections": [{"v_start": 0, "v_end": 15, "speed": 1.3}]}}
    assert effort_of(heavy, 30.0)["effort_pct"] > e["effort_pct"]
    assert 0 < EFFORT_K
    r = auto_sync(make_song(60.0), make_video(np.arange(1, 56) * 0.5, duration=30.0))
    f = r["final"]
    assert 0 <= f["effort_pct"] <= 100
    assert abs(f["fit_after_effort"] - max(0, f["total_pct"] - EFFORT_K * f["effort_pct"])) <= 0.1


def test_heavy_retime_effort_penalised():
    r = auto_sync(make_song(), make_video(slow_section_cuts()), min_gain=0.05)
    f = r["final"]
    assert f["effort"]["sections"] > 0 and f["effort"]["retimed_fraction"] > 0
    assert f["effort_pct"] > 0 and f["fit_after_effort"] < f["total_pct"]


# ---- arrange step (synthetic) ----
from amv_matcher import autosync as AS
from amv_matcher.arrange import plan_arrangement
from tests import test_arrange as TA


def _arr_pair():
    """Song with loud/quiet sections vs a video that wants peak/calm/peak/calm: a rearrangement helps."""
    song, vid = TA.make_song(), TA.make_video()
    vid = dict(vid, duration=3 * TA.SEC)
    vid["energy_curve"] = vid["energy_curve"][:int(3 * TA.SEC * TA.HZ)]
    vid["motion_peaks"] = [c for c in vid["motion_peaks"] if c < 3 * TA.SEC]
    vid["lulls"] = [[TA.SEC, 2 * TA.SEC]]
    vid["peak_segments"] = [[0, TA.SEC], [2 * TA.SEC, 3 * TA.SEC]]
    return song, vid


def test_arrange_step_picked_when_it_helps(monkeypatch):
    monkeypatch.setattr(AS, "plan_splice", lambda *a, **k: {"eligible": False})   # leave the win to arrange
    r = auto_sync(TA.make_song(), TA.make_video(), budget_s=60.0)
    kept = [s for s in r["steps"] if s["tool"] == "arrange" and s["kept"]]
    assert kept and set(kept[0]["params"]) == {"moves", "sections_used"}
    assert kept[0]["score_after"] > kept[0]["score_before"] + 5
    segs = r["plan"]["segments"]
    assert len(segs) > 1 and r["final"]["effort"]["splices"] == len(segs) - 1
    assert r["final"]["effort_pct"] > 0
    assert r["plan"]["lead_silence_s"] == 0.0 or r["plan"]["lead_silence_s"] >= 0


def test_arrange_kept_forced(monkeypatch):
    """Stubbed planner whose plan is accepted: recorded as 'arrange', moves counted as splices."""
    song, vid = _arr_pair()
    segs = [{"src_start": 16.0, "src_end": 32.0, "dst_start": 0.0},
            {"src_start": 0.0, "src_end": 16.0, "dst_start": 16.0},
            {"src_start": 48.0, "src_end": 64.0, "dst_start": 32.0}]
    calls = []

    def fake(s, v, max_moves, lead_s=None):
        calls.append(max_moves)
        return {"eligible": True, "segments": segs, "sections_used": ["a", "b", "c"], "moves": 2}
    monkeypatch.setattr(AS, "plan_arrangement", fake)
    monkeypatch.setattr(AS, "plan_splice", lambda *a, **k: {"eligible": False})
    monkeypatch.setattr(AS, "_done", lambda r, t: False)
    monkeypatch.setattr(AS, "_gain", lambda new, old, mg: True)
    monkeypatch.setattr(AS, "ORDER_COST", ("splice_first",))
    r = auto_sync(song, vid, budget_s=60.0, prefer_energy=False)
    arr = [s for s in r["steps"] if s["tool"] == "arrange"]
    assert calls and calls[0] == 3 and arr and arr[0]["kept"] and arr[0]["params"]["moves"] == 2
    assert AS.effort_of({"segments": segs}, 48.0)["splices"] == 2


def test_arrange_not_picked_when_no_gain(monkeypatch):
    song, vid = make_song(90.0), make_video(np.arange(1, 56) * 0.52, tempo=115.4)
    monkeypatch.setattr(AS, "plan_arrangement", lambda s, v, m, lead_s=None: {
        "eligible": True, "segments": [{"src_start": 0.0, "src_end": 30.0, "dst_start": 0.0},
                                       {"src_start": 40.0, "src_end": 50.0, "dst_start": 30.0}], "moves": 1,
        "sections_used": ["x"]})
    monkeypatch.setattr(AS, "_gain", lambda new, old, mg: False)
    r = auto_sync(song, vid, budget_s=60.0, prefer_energy=False)
    assert not any(s["kept"] for s in r["steps"] if s["tool"] == "arrange")
    assert not (r["plan"]["segments"] or [])


def test_arrange_not_called_when_ineligible(monkeypatch):
    called = []
    monkeypatch.setattr(AS, "plan_arrangement", lambda *a, **k: called.append(1))
    r = auto_sync(make_song(3.0), make_video(np.arange(1, 56) * 0.52, duration=30.0))   # below PARTIAL_MIN_S
    assert r["eligible"] is False and not called and r["steps"] == []


def test_arrange_skipped_under_tight_budget(monkeypatch):
    called = []
    monkeypatch.setattr(AS, "plan_arrangement", lambda *a, **k: called.append(1) or {"eligible": False})
    auto_sync(make_song(90.0), make_video(np.arange(1, 56) * 0.52, tempo=115.4), budget_units=5)
    assert not called


# ---- round 7: effort = deviation from untouched material; natural total; original-audio lock ----
def test_effort_rearranged_fraction_and_scale():
    from amv_matcher.autosync import effort_of, rearranged_fraction
    plain = [{"src_start": 3, "src_end": 13, "dst_start": 0}, {"src_start": 13, "src_end": 33, "dst_start": 10}]
    assert rearranged_fraction(plain) == 0.0                      # contiguous = one plain trim
    one_move = [{"src_start": 0, "src_end": 27, "dst_start": 0}, {"src_start": 50, "src_end": 53, "dst_start": 27}]
    assert abs(rearranged_fraction(one_move) - 0.1) < 1e-9        # 3 of 30 s differ
    half = [{"src_start": 0, "src_end": 15, "dst_start": 0}, {"src_start": 60, "src_end": 75, "dst_start": 15}]
    assert abs(rearranged_fraction(half) - 0.5) < 1e-9
    small = effort_of({"segments": one_move}, 30.0)["effort_pct"]
    big = effort_of({"segments": half}, 30.0)["effort_pct"]
    assert 0 < small < big <= 100
    # retime term: weighted dev / guardrail * retimed fraction; tempo: |ratio-1| / guardrail
    rt = {"sections": [{"v_start": 0, "v_end": 30, "speed": 1.25}]}
    assert abs(effort_of({"retime": rt}, 30.0)["effort_pct"] - 35.0) < 0.1
    assert abs(effort_of({"tempo_ratio": 1.08}, 30.0, max_tempo_dev=0.08)["effort_pct"] - 20.0) < 0.1
    mx = effort_of({"segments": half, "retime": rt, "tempo_ratio": 1.08}, 30.0)["effort_pct"]
    assert 0 < mx <= 100


def test_natural_total_exposed():
    r = auto_sync(make_song(90.0), make_video(np.arange(1, 56) * 0.52, duration=30.0, tempo=115.4))
    f = r["final"]
    assert f["natural_total_pct"] == int(round(r["steps"][0]["score_after"]))
    assert f["natural_total_pct"] <= f["total_pct"]


def test_original_audio_locked():
    cuts = np.arange(1, 56) * 0.52
    song, vid = make_song(60.0), make_video(cuts, duration=30.0, tempo=115.4)
    r = auto_sync(song, vid, original=True)
    p = r["plan"]
    assert p["original_locked"] is True and "original audio locked" in r["limits_hit"]
    assert p["offset"] == 0 and p["tempo_ratio"] == 1.0
    assert len(p["segments"]) == 1 and p["segments"][0]["src_start"] == 0 and p["segments"][0]["dst_start"] == 0
    assert not any(s["tool"] in ("tempo", "splice", "arrange") and s["kept"] for s in r["steps"])
    assert r["final"]["effort"]["rearranged_fraction"] == 0
    assert any(s["tool"] == "retime" for s in r["steps"]) or r["plan"]["retime"] is None
    r2 = auto_sync(make_song(), make_video(slow_section_cuts()), original=True, min_gain=0.05)
    assert r2["plan"]["retime"]                                   # retime is still allowed
    assert r2["plan"]["offset"] == 0


def test_original_unlock_restores_old_behaviour():
    cuts = np.arange(1, 56) * 0.52
    song, vid = make_song(90.0), make_video(cuts, duration=30.0, tempo=115.4)
    a = auto_sync(song, vid)
    b = auto_sync(song, vid, original=True, unlock_original=True)
    assert b["plan"]["original_locked"] is False and "original audio locked" not in b["limits_hit"]
    assert b["final"]["total_pct"] == a["final"]["total_pct"]


def _cb_case():
    cuts = np.arange(1, 56) * 0.52
    return make_song(90.0), make_video(cuts, duration=30.0, tempo=115.4)


def test_progress_events_pair_up_and_match_steps():
    song, vid = _cb_case()
    ev = []
    r = auto_sync(song, vid, progress_cb=ev.append)
    assert ev and set(ev[0]) == {"step", "status", "score_before", "score_after", "kept", "elapsed_s"}
    assert ev[0]["step"] == "offset" and ev[0]["status"] == "start"
    assert ev[0]["score_after"] is None and ev[0]["kept"] is None
    stack = []
    for e in ev:                                     # strictly alternating start/end per step
        if e["status"] == "start":
            assert not stack
            stack.append(e["step"])
        else:
            assert stack == [e["step"]] and e["kept"] in (True, False) and e["score_after"] is not None
            stack.pop()
    assert not stack
    assert [e["elapsed_s"] for e in ev] == sorted(e["elapsed_s"] for e in ev)
    ends = [e for e in ev if e["status"] == "end"]
    # every recorded step of the winning path has a matching end event (same tool, kept flag, scores)
    for s in r["steps"]:
        assert any(e["step"] == s["tool"] and e["kept"] == s["kept"] and abs(e["score_after"] - s["score_after"]) < 0.01
                   and abs(e["score_before"] - s["score_before"]) < 0.01 for e in ends)
    assert {e["step"] for e in ev} <= {"offset", "tempo", "splice", "arrange", "retime", "micro"}


def test_progress_callback_exception_is_swallowed():
    song, vid = _cb_case()

    def boom(_e):
        raise RuntimeError("nope")
    a = auto_sync(song, vid, progress_cb=boom, should_cancel=lambda: 1 / 0)
    b = auto_sync(song, vid)
    assert a["final"]["total_pct"] == b["final"]["total_pct"] and "cancelled" not in a["limits_hit"]


def test_cancel_before_start_returns_baseline():
    song, vid = _cb_case()
    ev = []
    r = auto_sync(song, vid, progress_cb=ev.append, should_cancel=lambda: True)
    assert "cancelled" in r["limits_hit"] and r["eligible"]
    assert [s["tool"] for s in r["steps"]] == ["offset"]
    assert r["final"]["total_pct"] == r["final"]["natural_total_pct"]
    assert r["plan"]["retime"] is None and not r["plan"]["segments"] or len(r["plan"]["segments"]) <= 1
    assert [e["step"] for e in ev] == ["offset", "offset"]


def test_cancel_mid_run_returns_valid_plan_at_least_baseline():
    song, vid = _cb_case()
    base = auto_sync(song, vid, should_cancel=lambda: True)["final"]["total_pct"]
    n = {"ends": 0}

    def cb(e):
        if e["status"] == "end":
            n["ends"] += 1
    r = auto_sync(song, vid, progress_cb=cb, should_cancel=lambda: n["ends"] >= 1)
    assert "cancelled" in r["limits_hit"]
    assert r["final"]["total_pct"] >= base and r["plan"]["offset"] is not None
    full = auto_sync(song, vid)
    assert "cancelled" not in full["limits_hit"]


def test_cancel_inside_retime_loop():
    from amv_matcher.retime import Cancelled, plan_retime
    import pytest
    song, vid = make_song(), make_video(slow_section_cuts())
    with pytest.raises(Cancelled):
        plan_retime(song, vid, 0.0, should_cancel=lambda: True)
    r = auto_sync(song, vid, min_gain=0.05, should_cancel=lambda: True)
    assert "cancelled" in r["limits_hit"] and r["final"]["total_pct"] >= 0


# ---- retime planned at the wrong offset (beedrill's slow.mp4: 0.625 s cuts 5-15 s vs a 120 BPM click) ----
def slow_wide_cuts(dur=20.0, a=5.0, b=15.0, step=0.625):
    c = list(np.arange(0.5, a, 0.5))
    c += list(a + step * np.arange(0, int((b - a) / step) + 1))
    c += list(np.arange(c[-1] + 0.5, dur - 0.2, 0.5))
    return c


def test_slow_wide_fixture_keeps_retime():
    """Reliable retime fixture for the V1 bands: make_song(22.0) + make_video(slow_wide_cuts(), duration=20)."""
    r = auto_sync(make_song(22.0), make_video(slow_wide_cuts(), duration=20.0))
    rt = r["plan"]["retime"]
    assert rt and len(rt["sections"]) >= 1
    assert any(s["tool"] == "retime" and s["kept"] for s in r["steps"])
    assert r["final"]["sync_pct"] >= 93


def test_alt_offsets_basic():
    from amv_matcher.autosync import _alt_offsets
    song, vid = make_song(22.0), make_video(slow_wide_cuts(), duration=20.0)
    alts = _alt_offsets(song, vid, [1.4], k=3, min_sep=0.5, lead_s=0.0)
    assert 0.0 in alts and 1.4 not in alts and len(alts) <= 4
    assert all(abs(a - 1.4) >= 0.5 - 1e-9 for a in alts)


def test_retime_tries_other_offsets_when_best_offset_finds_nothing(monkeypatch):
    """plan_retime finds nothing at the plain-trim best offset but a real plan elsewhere: auto_sync must
    still keep the retime (and move the offset to where it works)."""
    from amv_matcher import autosync as A
    song, vid = make_song(22.0), make_video(slow_wide_cuts(), duration=20.0)
    best = A._best_offset(song, vid)
    real = A.plan_retime

    def fake(s, v, offset, *a, **k):
        if abs(offset - best) < 1e-6:
            out = real(s, v, offset, *a, **k)
            out = dict(out, sections=[], warp=[[0.0, 0.0], [float(v["duration"]), float(v["duration"])]])
            return out
        return real(s, v, offset, *a, **k)
    monkeypatch.setattr(A, "plan_retime", fake)
    r = auto_sync(song, vid)
    assert r["plan"]["retime"] and r["plan"]["retime"]["sections"]
    assert abs(r["plan"]["offset"] - best) >= 0.5


# ---- steps must describe exactly the plan the user gets ----
def slow24_cuts():
    """24 s video: 0.5 s cuts, a slow 0.625 s section from 3 to 9 s, 0.5 s cuts again."""
    c = list(np.arange(0.5, 3.0, 0.5)) + list(3.0 + 0.625 * np.arange(0, 10) )
    c = [x for x in c if x < 9.0] + list(np.arange(9.0, 24.0, 0.5))
    return sorted(set(c))


def _consistency_pairs():
    return {
        "slow24": (make_song(26.0), make_video(slow24_cuts(), duration=24.0)),
        "slow_wide": (make_song(22.0), make_video(slow_wide_cuts(), duration=20.0)),
        "slow_section": (make_song(), make_video(slow_section_cuts())),
        "slow_drift": (make_song(90.0), make_video(np.arange(1, 56) * 0.52, 30.0, 115.4)),
        "arrange": _arr_pair(),
        "drop": drop_fixture(),
    }


def check_steps_match_plan(r):
    steps, plan = r["steps"], r["plan"]
    kept = lambda tool: [s for s in steps if s["tool"] == tool and s["kept"]]
    # a tool's final record is either kept or explicitly marked as retained from an earlier kept step
    for tool in {s["tool"] for s in steps}:
        last = [s for s in steps if s["tool"] == tool][-1]
        if not last["kept"] and kept(tool):
            assert last["params"].get("retained") is True, (tool, last)
    movers = [s for s in steps if s["kept"] and (s["tool"] in ("offset", "arrange", "splice")
                                                  or (s["tool"] == "retime" and "offset" in s["params"]))]
    if abs(plan["offset"]) > 1e-9:
        assert movers, "offset in plan without a kept step"
        assert abs(movers[-1]["params"].get("offset", 0.0) - plan["offset"]) <= 0.01, (movers[-1], plan["offset"])
    if plan["tempo_ratio"] != 1:
        assert kept("tempo")
    if plan["segments"] and len(plan["segments"]) > 1:
        assert kept("arrange") or kept("splice")
    if plan["retime"] and plan["retime"]["sections"]:
        rt = kept("retime")
        assert rt and rt[-1]["params"]["sections"] == len(plan["retime"]["sections"])
    else:
        assert not kept("retime") or not plan["retime"]
    if plan["nudges"]:
        assert kept("micro") and kept("micro")[-1]["params"]["nudges"] == len(plan["nudges"])
    else:
        assert not kept("micro")


@pytest.mark.parametrize("name", ["slow24", "slow_wide", "slow_section", "slow_drift", "arrange", "drop"])
def test_steps_describe_plan(name):
    song, vid = _consistency_pairs()[name]
    r = auto_sync(song, vid, budget_s=40.0)
    check_steps_match_plan(r)


def test_slow24_retime_kept_and_reported():
    song, vid = _consistency_pairs()["slow24"]
    r = auto_sync(song, vid)
    assert r["plan"]["retime"] and r["plan"]["retime"]["sections"]
    rts = [s for s in r["steps"] if s["tool"] == "retime"]
    assert rts[0]["kept"]
    assert all(s["kept"] or s["params"].get("retained") for s in rts)


# ---- invariant: every tool is tried once or its skip is reported --------------------------------
ALL_TOOLS = ("offset", "tempo", "micro", "retime", "splice")


def _tools_accounted(res):
    seen = {s["tool"] for s in res["steps"]}
    lim = " ".join(res.get("limits_hit", []))
    return seen, lim


def test_limits_hit_reports_budget_skipped_tools():
    song, vid = _consistency_pairs()["slow24"]
    res = AS.auto_sync(song, vid, budget_units=3)
    seen, lim = _tools_accounted(res)
    if "budget" in res.get("limits_hit", []):
        assert "budget: skipped" in lim


def test_slow24_real_media_retime_tried_or_reported(tmp_path):
    from amv_matcher import audio, video
    from tests import synth
    wav, avi = tmp_path / "c.wav", tmp_path / "v.avi"
    synth.make_click_track(wav, 120, 26.0)
    synth.make_video_jumps(avi, synth.slow24_jump_times(), seconds=24.0)
    song = audio.analyze_song(str(wav))
    vid = video.analyze_video(str(avi))
    res = AS.auto_sync(song, vid, budget_s=20.0)
    seen, lim = _tools_accounted(res)
    assert "retime" in seen or "skipped retime" in lim, (res["path"], sorted(seen), res.get("limits_hit"))
    rt = [s for s in res["steps"] if s["tool"] == "retime"]
    assert rt and all(s["kept"] is False or s["kept"] is True for s in rt)
    print([(s["params"].get("not_in_path"), s["kept"]) for s in rt])


# ---- stem_targets threading ---------------------------------------------------------------------
def _stem_pair():
    song = make_song(duration=40.0)
    vid = make_video(slow_section_cuts(), duration=28.0)
    vid["segments_struct"] = [{"kind": "calm", "start": 0.0, "end": 28.0}]
    return song, vid


def test_stem_targets_threaded_to_score_at(monkeypatch):
    from amv_matcher import score as S
    seen = []
    real = S.score_at

    def spy(*a, **k):
        seen.append("stem_targets" in k)
        return real(*a, **k)
    monkeypatch.setattr(S, "score_at", spy)
    song, vid = _stem_pair()
    tg = [{"t": 0.25 + 0.5 * i, "weight": 0.5, "stem": "hh", "kind": "detail"} for i in range(60)]
    auto_sync(song, vid, budget_s=5.0)
    assert seen and not any(seen)                     # None -> byte-identical call, no kwarg
    seen.clear()
    auto_sync(song, vid, budget_s=5.0, stem_targets=tg)
    assert any(seen)                                  # the _run scoring calls carry the targets (offset search stays beat-only)


def test_stem_targets_none_matches_default():
    song, vid = _stem_pair()
    a = auto_sync(song, vid, budget_s=5.0)
    b = auto_sync(song, vid, budget_s=5.0, stem_targets=None)
    assert a["final"] == b["final"]


def test_prefer_energy_routes_to_arrange(monkeypatch):
    from amv_matcher import autosync as AS
    calls = []
    real = AS.plan_arrangement

    def spy(song, video, max_moves, lead_s=None, **kw):
        calls.append(kw)
        return real(song, video, max_moves, lead_s=lead_s, **kw)
    monkeypatch.setattr(AS, "plan_arrangement", spy)
    monkeypatch.setattr(AS, "plan_splice", lambda *a, **k: {"eligible": False})
    monkeypatch.setattr(AS, "_done", lambda r, t: False)
    monkeypatch.setattr(AS, "ORDER_COST", ("splice_first",))
    song, vid = _arr_pair()
    AS.auto_sync(song, vid, budget_s=60, prefer_energy=False)
    assert calls and all(k == {} for k in calls)                    # explicit false: called exactly as the old default
    calls.clear()
    AS.auto_sync(song, vid, budget_s=60)
    assert calls and all(k == {"prefer_energy": True} for k in calls)     # default is on
    calls.clear()
    AS.auto_sync(song, vid, budget_s=60, prefer_energy=True)
    assert calls and all(k == {"prefer_energy": True} for k in calls)


def test_ramps_flag_default_off_and_opt_in():
    """ramps=False (default) leaves results exactly as before; ramps=True only ADDS plan.retime_ramps."""
    def _ramp_cuts(i0, i1, dur=28.0):
        t, cuts = 0.0, []
        while True:
            t += i0 + (i1 - i0) * min(t / dur, 1.0)
            if t >= dur:
                return cuts
            cuts.append(t)
    song, vid = make_song(), make_video(_ramp_cuts(0.57, 0.46), duration=28.0)
    a = auto_sync(song, vid, budget_s=30.0)
    assert "ramps" not in a["final"] and "retime_ramps" not in a["plan"]
    assert not any(l.startswith("ramps") for l in a["limits_hit"])
    b = auto_sync(song, vid, budget_s=30.0, ramps=True)
    assert b["final"]["total_pct"] == a["final"]["total_pct"] and b["plan"]["retime"] == a["plan"]["retime"]
    rr = b["plan"].get("retime_ramps")
    if rr:
        assert rr["total_after"] > rr["total_step"] and "step" not in rr and rr["warp"]
        assert b["final"]["ramps"]["kept"] is True
    else:
        assert any(l.startswith("ramps") for l in b["limits_hit"])


def _det_key(r):
    import json
    return json.dumps({k: r[k] for k in ("steps", "final", "plan", "path", "limits_hit", "pruned", "deterministic")},
                      sort_keys=True, default=float)


def _cpu_burners(n, stop, max_s=120.0):
    """n allocation-free busy threads; each stops on `stop` OR its own hard deadline, so a failed assert,
    a hung run or a slow runner can never leak an unbounded CPU/memory hog into later tests."""
    import threading

    def burn():
        end = time.perf_counter() + max_s
        x = 0
        while not stop.is_set() and time.perf_counter() < end:
            for i in range(2000):
                x = (x * 31 + i) & 0xFFFF
    return [threading.Thread(target=burn, daemon=True, name="det-burner-%d" % i) for i in range(n)]


def _assert_same_or_flagged(r, base):
    """A result is either byte-identical to the unloaded one, or it carries the explicit not-reproducible marker
    (the wall-clock emergency cap fired); it may never silently differ."""
    if r["deterministic"]:
        assert _det_key(r) == base
    else:
        assert AS.NOT_REPRODUCIBLE in r["limits_hit"] and "budget: time cap hit" in r["limits_hit"]
        assert r["final"]["deterministic"] is False


def test_deterministic_under_cpu_load():
    """Same inputs -> byte-identical plan/totals, repeated, under CPU load and with a different budget_s: the
    work-unit budget alone decides the result; the wall clock is only an emergency cap that marks its result."""
    import threading
    fixtures = [(make_song(90.0), make_video(np.arange(1, 56) * 0.52, duration=30.0, tempo=115.4)),
                (make_song(), make_video(slow_section_cuts(), duration=28.0)),
                (make_song(60.0), make_video(np.arange(1, 40) * 0.47, duration=24.0, tempo=127.0))]
    bases = []
    for song, vid in fixtures:                                   # unloaded reference runs first, no burners alive
        r = auto_sync(song, vid, budget_s=20.0)
        assert r["deterministic"] is True and r["final"]["deterministic"] is True
        assert AS.NOT_REPRODUCIBLE not in r["limits_hit"]
        bases.append(_det_key(r))
    assert _det_key(auto_sync(*fixtures[0], budget_s=20.0)) == bases[0]          # plain repeat
    stop = threading.Event()
    burners = _cpu_burners(2, stop)
    try:
        for b in burners:
            b.start()
        for (song, vid), base in zip(fixtures, bases):
            _assert_same_or_flagged(auto_sync(song, vid, budget_s=20.0), base)
        _assert_same_or_flagged(auto_sync(*fixtures[1], budget_s=60.0), bases[1])  # budget_s only scales the cap
    finally:
        stop.set()
        for b in burners:
            b.join(timeout=10.0)
    assert not any(b.is_alive() for b in burners)


def test_cap_is_generous_and_floored():
    assert AS.TIME_CAP_MIN_S >= 120.0 and AS.TIME_CAP_MULT >= 10.0


def test_time_cap_is_reported(monkeypatch):
    t = iter(range(0, 100000, 1000))
    monkeypatch.setattr(AS.time, "perf_counter", lambda: next(t))
    r = auto_sync(make_song(90.0), make_video(np.arange(1, 56) * 0.52, duration=30.0, tempo=115.4), budget_s=5.0)
    assert "budget: time cap hit" in r["limits_hit"]
    assert AS.NOT_REPRODUCIBLE in r["limits_hit"]                    # explicit marker, shown by the UI ('budget' prefix)
    assert r["deterministic"] is False and r["final"]["deterministic"] is False


def test_smooth_mode_off_identical_and_on_forces_eased_ramps():
    """smooth=False is byte-identical to not passing it; smooth=True forces a retime-only eased-ramp plan."""
    def _ramp_cuts(i0, i1, dur=28.0):
        t, cuts = 0.0, []
        while True:
            t += i0 + (i1 - i0) * min(t / dur, 1.0)
            if t >= dur:
                return cuts
            cuts.append(t)
    song, vid = make_song(), make_video(_ramp_cuts(0.57, 0.46), duration=28.0)
    a = auto_sync(song, vid, budget_s=30.0)
    off = auto_sync(song, vid, budget_s=30.0, smooth=False)
    assert _det_key(a) == _det_key(off) and "smooth" not in off and "smooth" not in off["final"]
    runs = [auto_sync(song, vid, budget_s=30.0, smooth=True) for _ in range(2)]
    on = runs[0]
    assert _det_key(on) == _det_key(runs[1])
    sm = on["smooth"]
    assert set(sm) >= {"applied", "reason", "smooth", "best", "best_path"}
    if sm["applied"]:
        rt = on["plan"]["retime"]
        assert rt["points"] and rt["warp"] and rt is on["plan"]["retime_ramps"] or rt == on["plan"]["retime_ramps"]
        assert on["final"]["ramps"]["forced"] is True and on["path"] == "smooth"
        assert sm["smooth"]["max_jump"] <= 0.01  # fine-grid planner figure (step plans ~0.13)
        assert set(sm["smooth"]) == {"total", "sync", "tight", "arc", "max_jump", "max_accel"}
        assert set(sm["best"]) == set(sm["smooth"])
        assert on["final"]["ramps"]["total_after"] == sm["smooth"]["total"]
    else:
        assert sm["reason"] and "smooth: not applicable" in on["limits_hit"]
