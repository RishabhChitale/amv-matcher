"""F18b content match: when the video's own audio is a hand splice of the song, auto-sync rebuilds that splice.

Synthetic signals only (no media): a song with non-repeating energy/onset curves, a reference made by cutting it.
"""
import json

import numpy as np
import pytest

from amv_matcher import autosync, contentmatch
from tests.test_retime import make_video

HZ = 100


def _smooth_noise(n, seed, k=25):
    rng = np.random.default_rng(seed)
    x = np.convolve(rng.random(n + k), np.ones(k) / k, mode="valid")[:n]
    return np.clip(x - x.min(), 0, None) + 0.05


def make_song(duration=120.0, seed=1, bpm=120.0):
    n = int(duration * HZ)
    en, on = _smooth_noise(n, seed), _smooth_noise(n, seed + 100, k=5)
    bt = np.arange(0, duration, 60.0 / bpm)
    return {"bpm": bpm, "beat_times": bt.tolist(), "beat_strengths": [1.0] * len(bt), "duration": duration,
            "energy_curve": en.tolist(), "energy_hz": HZ, "onset_env": on.tolist(), "onset_env_fps": HZ,
            "drops": [], "lulls": []}


def splice_ref(song, pieces):
    """The 'video's own audio': pieces [(src_start, src_end)] of the song laid end to end."""
    en, on = np.asarray(song["energy_curve"]), np.asarray(song["onset_env"])
    e = np.concatenate([en[int(a * HZ):int(b * HZ)] for a, b in pieces])
    o = np.concatenate([on[int(a * HZ):int(b * HZ)] for a, b in pieces])
    return {"duration": len(e) / HZ, "energy_curve": e.tolist(), "energy_hz": HZ,
            "onset_env": o.tolist(), "onset_env_fps": HZ}


def test_splice_is_rebuilt():
    song = make_song()
    ref = splice_ref(song, [(20.0, 80.0), (95.0, 115.0)])
    cm = contentmatch.match(song, ref)
    assert cm is not None and len(cm["segments"]) == 2
    a, b = cm["segments"]
    assert abs(a["src_start"] - 20.0) < 0.1 and abs(a["dst_start"]) < 1e-6
    assert abs(b["dst_start"] - 60.0) < 0.1 and abs(b["src_start"] - 95.0) < 0.1
    assert abs(b["src_end"] - 115.0) < 0.1
    assert [round(s["offset"], 1) for s in cm["spans"]] == [20.0, 35.0]


def test_single_offset_found():
    song = make_song()
    cm = contentmatch.match(song, splice_ref(song, [(30.0, 90.0)]))
    assert cm is not None and len(cm["segments"]) == 1
    assert abs(cm["segments"][0]["src_start"] - 30.0) < 0.05


def test_unrelated_audio_is_no_match():
    assert contentmatch.match(make_song(seed=1), splice_ref(make_song(seed=7), [(10.0, 70.0)])) is None


def _video(duration):
    return make_video(list(np.arange(0.5, duration - 0.5, 0.5)), duration=duration)


def test_auto_sync_offers_the_rebuilt_edit(monkeypatch):
    song = make_song()
    ref = splice_ref(song, [(20.0, 80.0), (95.0, 115.0)])
    vid = _video(ref["duration"])
    vid["original_audio"] = {"analysis": ref}
    monkeypatch.setattr(autosync, "CM_TOTAL_TOL", 1000)          # always keep it: check the plan it gives
    r = autosync.auto_sync(song, vid)
    cmi = r["plan"]["content_match"]
    assert cmi["kept"] and r["plan"]["content_matched"] and not r["plan"]["looped"]
    segs = r["plan"]["segments"]
    assert len(segs) == 2 and abs(segs[0]["src_start"] - 20.0) < 0.1 and abs(segs[1]["src_start"] - 95.0) < 0.1
    assert any("content match" in s for s in r["limits_hit"])


def test_auto_sync_kept_only_when_it_scores(monkeypatch):
    song = make_song()
    ref = splice_ref(song, [(20.0, 80.0), (95.0, 115.0)])
    vid = _video(ref["duration"])
    vid["original_audio"] = {"analysis": ref}
    monkeypatch.setattr(autosync, "CM_TOTAL_TOL", -1000)         # never good enough: the other paths stand
    r = autosync.auto_sync(song, vid)
    assert r["plan"]["content_match"]["kept"] is False and r["plan"]["content_matched"] is False
    del vid["original_audio"]
    plain = autosync.auto_sync(song, vid)
    assert r["plan"]["segments"] == plain["plan"]["segments"]
    assert r["final"]["total_pct"] == plain["final"]["total_pct"]


def _strip(r):
    return json.dumps(r, sort_keys=True, default=str)


@pytest.mark.parametrize("case", ["no_original", "unrelated", "is_original", "missing_file"])
def test_no_change_when_it_does_not_apply(case, monkeypatch):
    song = make_song()
    vid = _video(80.0)
    kw = {}
    if case == "unrelated":
        vid["original_audio"] = {"analysis": splice_ref(make_song(seed=7), [(10.0, 90.0)])}
    elif case == "is_original":
        vid["original_audio"] = {"analysis": splice_ref(song, [(20.0, 80.0), (95.0, 115.0)])}
        kw["original"] = True
    elif case == "missing_file":
        vid["original_audio"] = {"path": "/nonexistent/f18b-test.wav"}
    r = autosync.auto_sync(song, vid, **kw)
    assert "content_match" not in r["plan"]
    monkeypatch.setattr(autosync, "CONTENT_MATCH", False)
    off = autosync.auto_sync(song, vid, **kw)
    a, b = json.loads(_strip(r)), json.loads(_strip(off))
    for x in (a, b):
        x.pop("elapsed_s", None)
        x.get("final", {}).pop("elapsed_s", None)
    assert a == b


@pytest.mark.parametrize("tol", [1000, -1000])
def test_plan_not_chosen_is_offered(tol, monkeypatch):
    song = make_song()
    ref = splice_ref(song, [(20.0, 80.0), (95.0, 115.0)])
    vid = _video(ref["duration"])
    vid["original_audio"] = {"analysis": ref}
    monkeypatch.setattr(autosync, "CM_TOTAL_TOL", tol)
    r = autosync.auto_sync(song, vid)
    assert len(r["alternatives"]) == 1
    alt = r["alternatives"][0]
    kept = r["plan"]["content_match"]["kept"]
    assert alt["id"] == ("best_score" if kept else "content_match") and alt["chosen"] is False
    assert alt["label"] == (autosync.AUTO_LABEL if kept else autosync.CM_LABEL)
    assert alt["plan"]["content_matched"] is (not kept) and "offset" in alt["plan"]
    if not kept:                                   # the hand edit is still there to pick
        segs = alt["plan"]["segments"]
        assert len(segs) == 2 and abs(segs[0]["src_start"] - 20.0) < 0.1 and abs(segs[1]["src_start"] - 95.0) < 0.1
        assert alt["final"]["total"] == r["plan"]["content_match"]["total"]
    assert _strip(autosync.auto_sync(song, vid)["alternatives"]) == _strip(r["alternatives"])   # deterministic
    for fin, plan in ((r["final"], r["plan"]), (alt["final"], alt["plan"])):     # 'Starts on' for both options
        assert "starts_on" in fin and fin["starts_on"] == autosync.plan_starts_on(song, plan)


def test_no_alternative_without_match():
    r = autosync.auto_sync(make_song(), _video(80.0))
    assert r["alternatives"] == []


def test_plan_starts_on_reads_the_first_heard_position(monkeypatch):
    seen = []
    monkeypatch.setattr(autosync.motif, "starts_on", lambda song, o: seen.append(o) or "x")
    assert autosync.plan_starts_on({}, {"offset": 12.5, "segments": None}) == "x"
    autosync.plan_starts_on({}, {"offset": 0.0, "segments": [{"src_start": 95.0, "dst_start": 40.0},
                                                             {"src_start": 20.0, "dst_start": 0.0}]})
    autosync.plan_starts_on({}, {"offset": -0.6, "segments": None})          # lead silence opens on the file start
    assert seen == [12.5, 20.0, 0.0]


def lead_ref(song, lead_s, pieces):
    """A reference that opens on `lead_s` of silence before the song's own start, then `pieces`."""
    r = splice_ref(song, pieces)
    n = int(lead_s * HZ)
    r["energy_curve"] = [0.0] * n + r["energy_curve"]
    r["onset_env"] = [0.0] * n + r["onset_env"]
    r["duration"] = len(r["energy_curve"]) / HZ
    return r


def assert_valid_plan(plan, song_dur, video_dur):
    """What POST /api/render-audio accepts (server RenderRequest): every segment 0 <= src_start < src_end <=
    song length (+0.05) and dst_start inside the video."""
    for g in plan.get("segments") or []:
        assert 0 <= g["src_start"] < g["src_end"] <= song_dur + 0.05, g
        assert 0 <= g["dst_start"] < video_dur, g
    assert plan.get("lead_silence_s", 0) >= 0


@pytest.mark.parametrize("tol", [1000, -1000])
@pytest.mark.parametrize("lead", [0.0, 0.63, 2.0])
def test_every_plan_is_renderable(lead, tol, monkeypatch):
    """Main plan and every alternative: no negative src_start (a lead silence becomes a later dst_start)."""
    song = make_song()
    ref = lead_ref(song, lead, [(0.0, 60.0), (80.0, 110.0)])
    vid = _video(ref["duration"])
    vid["original_audio"] = {"analysis": ref}
    monkeypatch.setattr(autosync, "CM_TOTAL_TOL", tol)
    r = autosync.auto_sync(song, vid)
    plans = [r["plan"]] + [a["plan"] for a in r["alternatives"]]
    assert len(plans) == 2
    for p in plans:
        assert_valid_plan(p, song["duration"], vid["duration"])
    cm = next(p for p in plans if p.get("content_matched"))
    first = min(cm["segments"], key=lambda g: g["dst_start"])
    assert abs(first["src_start"]) < 1e-9 and abs(first["dst_start"] - lead) < 0.1      # still in sync
    assert abs(cm["lead_silence_s"] - first["dst_start"]) < 1e-6 if lead > 0 else cm["lead_silence_s"] == 0


def test_nonneg_segments_keeps_the_mapping():
    from amv_matcher import render
    from amv_matcher.splice import nonneg_segments
    segs = [{"src_start": -0.63, "src_end": 37.35, "dst_start": 0.0},
            {"src_start": 131.15, "src_end": 137.24, "dst_start": 37.98},
            {"src_start": -5.0, "src_end": -1.0, "dst_start": 50.0}]
    out = nonneg_segments(segs)
    assert out[0] == {"src_start": 0.0, "src_end": 37.35, "dst_start": 0.63} and out[1] is segs[1] and len(out) == 2
    assert segs[0]["src_start"] == -0.63                                    # input untouched
    song = {"beat_times": list(np.arange(0, 140, 0.5)), "drops": [10.0, 132.0]}
    a = render.output_song_events(song, {"segments": segs[:2]}, 45.0)
    b = render.output_song_events(song, {"segments": out}, 45.0)
    assert np.allclose(a["beat_times"], b["beat_times"]) and np.allclose(a["drops"], b["drops"])
    assert nonneg_segments(None) is None and nonneg_segments([]) == []
