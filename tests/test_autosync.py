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

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SONG = glob.glob(os.path.join(_ROOT, ".uploads", "song", "*songS1*.mp3"))
_VIDEO = glob.glob(os.path.join(_ROOT, ".uploads", "video", "*videoA*.mp4"))


@pytest.mark.slow
@pytest.mark.skipif(not (_SONG and _VIDEO), reason="real songS1/videoA files missing")
@pytest.mark.skipif(not os.environ.get("AMV_REAL"), reason="set AMV_REAL=1 (run after score/curves calibration)")
def test_real_songS1_videoA():
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
    r = auto_sync(song, vid, budget_s=0.01)
    assert r["pruned"] and "budget" in r["limits_hit"]
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


def test_shorter_song_ineligible_by_default():
    song, vid = _short_pair()
    r = auto_sync(song, vid)
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
    heavy = {"tempo_ratio": 1.05, "segments": [{}, {}, {}],
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
