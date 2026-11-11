"""plan_splice tests on synthetic analysis dicts (no media)."""
import time

import numpy as np

from amv_matcher.splice import plan_splice

HZ = 10


def make_song(duration=240.0, bpm=120.0, drops=(40.0,), loud_len=8.0):
    beat = 60.0 / bpm
    bt = np.arange(0, duration, beat)
    en = np.full(int(duration * HZ), 0.15)
    for d in drops:
        en[int(d * HZ):int((d + loud_len) * HZ)] = 0.95
    return {"bpm": bpm, "beat_times": bt.tolist(), "beat_strengths": [1.0] * len(bt), "duration": duration,
            "energy_curve": en.tolist(), "energy_hz": HZ, "drops": list(drops), "lulls": []}


def make_video(duration=40.0, peaks=(10.0, 25.0), peak_len=4.0):
    en = np.full(int(duration * HZ), 0.1)
    for p in peaks:
        en[int(p * HZ):int((p + peak_len) * HZ)] = 0.95
    cuts = []
    for p in peaks:
        cuts += list(np.arange(p, p + peak_len, 0.5))
    return {"duration": duration, "fps": 30, "energy_curve": en.tolist(), "energy_hz": HZ,
            "segments": [], "lulls": [], "peak_segments": [[p, p + peak_len] for p in peaks],
            "shots": [], "cut_times": [], "motion_curve": [], "motion_peaks": [float(c) for c in cuts],
            "motion_tempo_bpm": 120.0}


def bar_starts(song):
    return np.asarray(song["beat_times"])[::4]


def on_bar(song, t):
    return bool(np.min(np.abs(bar_starts(song) - t)) < 1e-4)


def accent_song(phase, duration=60.0, bpm=120.0):
    song = make_song(duration, bpm, drops=())
    bt = np.asarray(song["beat_times"])
    fps = 20.0
    on = np.full(int(duration * fps), 0.1)
    for i, t in enumerate(bt):
        if i % 4 == phase:
            on[int(round(t * fps))] = 1.0
    song["onset_env"], song["onset_env_fps"] = on.tolist(), fps
    return song


def test_bar_phase_estimated():
    for ph in (0, 1, 2, 3):
        assert plan_splice(accent_song(ph), make_video(20.0, peaks=(5.0,)), 0)["bar_phase"] == ph


def test_short_song_splices_on_a_video_stretch():
    # F2b: a song shorter than the video is planned on its best video stretch, never refused for length
    r = plan_splice(make_song(30.0), make_video(40.0), 2)
    assert r["eligible"] is not False and r["segments"] and "bar_phase" in r
    vw = r["video_window"]
    assert 0.0 <= vw["start_s"] and vw["end_s"] <= 40.0 + 1e-6 and vw["end_s"] - vw["start_s"] <= 30.0 + 0.05
    assert all(s["dst_start"] < 30.0 for s in r["segments"])       # clip time inside the window
    assert plan_splice(make_song(), make_video(), 2)["eligible"] is True


def test_unreadable_song_not_spliced():
    s = make_song(30.0); s["duration"] = 0.0
    r = plan_splice(s, make_video(40.0), 2)
    assert r["eligible"] is False and r["reason"] == "unreadable"
    assert r["segments"] == [] and r["cuts"] == [] and "bar_phase" in r


def test_zero_cuts_is_baseline():
    r = plan_splice(make_song(), make_video(), 0)
    assert r["cuts"] == [] and len(r["segments"]) == 1
    assert r["total_pct"] == r["baseline"]["total_pct"] and r["improvement_pct"] == 0
    assert abs(r["segments"][0]["src_start"] - r["baseline"]["offset"]) < 1e-6


def test_two_cuts_hit_both_peaks():
    song, video = make_song(), make_video()
    r = plan_splice(song, video, 2)
    assert r["improvement_pct"] >= 2, r
    en = np.asarray(song["energy_curve"])

    def src_at(t):
        seg = next(s for s in r["segments"] if s["dst_start"] <= t < s["dst_start"] + s["src_end"] - s["src_start"] - 1e-9)
        return seg["src_start"] + (t - seg["dst_start"])

    # strongest peak (first, equal energy) has a song drop within +-0.25 s
    assert abs(src_at(10.0) - 40.0) <= 0.25, r["segments"]
    # every peak has high song energy within +-1 beat (0.5 s)
    for p in (10.0, 25.0):
        assert en[int(src_at(p + 0.5) * HZ)] > 0.5, (p, r["segments"])
    assert r["objective"] > 0 and r["total_pct"] - r["baseline"]["total_pct"] == r["improvement_pct"]


def test_cuts_on_bars_contiguous_exact():
    song, video = make_song(), make_video()
    r = plan_splice(song, video, 3)
    segs = r["segments"]
    assert len(r["cuts"]) <= 3 and len(segs) == len(r["cuts"]) + 1
    t = 0.0
    for i, s in enumerate(segs):
        assert abs(s["dst_start"] - t) < 1e-4
        ln = s["src_end"] - s["src_start"]
        assert ln > 0 and (ln >= 2.0 - 1e-6 or len(segs) == 1)
        assert s["src_start"] >= 0 and s["src_end"] <= song["duration"] + 1e-6
        if i > 0:
            assert on_bar(song, s["src_start"])
        if i < len(segs) - 1:
            assert on_bar(song, s["src_end"])
        t += ln
    assert abs(t - video["duration"]) < 1e-4


def test_runtime_bound():
    song, video = make_song(240.0, drops=(40.0, 150.0)), make_video(120.0, peaks=(20.0, 70.0, 100.0))
    t0 = time.time()
    r = plan_splice(song, video, 3)
    dt = time.time() - t0
    print("splice runtime", round(dt, 2), "s; improvement", r["improvement_pct"])
    assert dt < 3.0


def _intro_song(duration, drops, loud_len=8.0, intro=30.0):
    song = make_song(duration, 120.0, drops=drops, loud_len=loud_len)
    en = np.asarray(song["energy_curve"])
    k = int(intro * HZ)
    en[:k] = np.where(en[:k] > 0.5, en[:k], 0.05)
    song["energy_curve"] = en.tolist()
    return song


def _check_peaks(song, video, r, strongest=10.0, others=(25.0,), drop=40.0):
    en = np.asarray(song["energy_curve"])
    assert r["cuts"], r

    def src_at(t):
        seg = next(s for s in r["segments"] if s["dst_start"] <= t < s["dst_start"] + s["src_end"] - s["src_start"] - 1e-9)
        return seg["src_start"] + (t - seg["dst_start"])

    assert min(abs(src_at(strongest) - d) for d in song["drops"]) <= 0.25, r["segments"]
    for p in others:
        assert en[int(src_at(p + 0.5) * HZ)] > 0.5, (p, r["segments"])
    assert r["improvement_pct"] >= 2, r


def test_short_song_splices():
    # 60 s song, quiet intro + ONE drop at 40 s; 30 s video, peaks 10 s and 25 s. Video lengths that
    # are not round numbers used to push the drop-aligned trim out of the candidate set (o + vd > song).
    song = _intro_song(60.0, (40.0,))
    for vd in (30.0, 30.03, 29.97):
        video = make_video(vd)
        _check_peaks(song, video, plan_splice(song, video, 2))


def test_tv_size_song_two_drops():
    song = _intro_song(90.0, (30.0, 70.0), loud_len=12.0, intro=0.0)
    video = make_video(30.03)
    r = plan_splice(song, video, 2)
    assert r["cuts"] and r["improvement_pct"] >= 2, r


def test_negative_offset_assemble_and_plan():
    from amv_matcher.splice import _assemble, lead_silence
    song, video = make_song(), make_video()
    vd = video["duration"]
    segs = [{"src_start": -0.63, "src_end": vd - 0.63, "dst_start": 0.0}]
    asm = _assemble(song, segs, vd)               # used to raise ValueError
    en = np.asarray(asm["energy_curve"])
    assert lead_silence(segs) == 0.63 and np.all(en[:int(0.6 * 10)] == 0)
    r = plan_splice(song, video, 2, allow_negative=True)
    assert r["eligible"] and r["lead_silence_s"] >= 0.0
    if r["lead_silence_s"] > 0:
        assert r["segments"][0]["src_start"] < 0


# ---- _assemble carries every analysis key the scorer reads
def _grid_song(duration=120.0, bpm=120.0):
    from amv_matcher import score as S
    s = make_song(duration=duration, bpm=bpm, drops=(40.0, 80.0))
    beat = 60.0 / bpm
    g = np.arange(0.0, duration, beat)
    fps = 43.0
    rng = np.random.default_rng(3)
    env = np.abs(rng.normal(0, 0.1, int(duration * fps)))
    env[(g * fps).astype(int)[g * fps < len(env)]] += 1.0
    s.update({"beat_grid": g.tolist(), "grid_bpm": bpm, "grid_period": beat, "grid_used": True,
              "beats_per_bar": 4, "bar_phase": 1, "onset_env": env.tolist(), "onset_env_fps": fps})
    return s


def _vid_for_grid(dur=30.0):
    v = make_video(duration=dur, peaks=(8.0, 20.0))
    cuts = np.arange(0.0, dur, 0.5)
    v["cut_times"] = cuts.tolist()
    v["shots"] = [{"start": float(c), "end": float(c) + 0.5} for c in cuts]
    return v


def test_assemble_single_trim_rescored_equals_direct():
    from amv_matcher import score as S
    from amv_matcher.splice import _assemble
    song, video = _grid_song(), _vid_for_grid()
    vd = video["duration"]
    for o in (0.0, 12.5, 33.0):
        asm = _assemble(song, [{"src_start": o, "src_end": o + vd, "dst_start": 0.0}], vd)
        direct = S.score_at(song, video, o)["total_pct"]
        again = S.score_at(asm, video, 0.0)["total_pct"]
        assert abs(direct - again) <= 0.5, (o, direct, again)


def test_assemble_carries_all_keys_and_remaps_repeat_and_lead():
    from amv_matcher.splice import _assemble
    song = _grid_song()
    segs = [{"src_start": -2.0, "src_end": 6.0, "dst_start": 0.0},
            {"src_start": 4.0, "src_end": 10.0, "dst_start": 8.0}]       # repeat of 4..6, lead silence
    asm = _assemble(song, segs, 14.0)
    for k in ("beat_times", "beat_strengths", "beat_grid", "grid_bpm", "grid_period", "grid_used",
              "beats_per_bar", "bar_phase", "onset_env", "onset_env_fps", "energy_curve", "drops", "lulls"):
        assert k in asm, k
    assert len(asm["beat_strengths"]) == len(asm["beat_times"])
    g = np.asarray(asm["beat_grid"])
    assert g[0] == 2.0 and abs(g[1] - g[0] - 0.5) < 1e-9          # lead silence of 2 s, no grid before it
    assert np.min(np.abs(g - 8.0)) < 1e-9                         # repeated source second 4.0 lands at 8.0
    fps = asm["onset_env_fps"]
    assert max(asm["onset_env"][:int(2 * fps) - 1]) == 0.0        # silent lead
    # bar phase: the first kept grid point is source beat 4 (index 4, phase 1 -> bar starts at odd idx)
    assert asm["bar_phase"] == (1 - 4) % 4 or asm["bar_phase"] in range(4)


def test_apply_tempo_scales_grid():
    from amv_matcher.tempo import apply_tempo
    song = _grid_song()
    out = apply_tempo(song, 1.05)
    assert abs(out["grid_bpm"] - 126.0) < 1e-6 and abs(out["bpm"] - 126.0) < 1e-6
    assert abs(out["beat_grid"][4] - song["beat_grid"][4] / 1.05) < 1e-9


import glob as _glob
import os as _os
import pytest as _pytest

from tests import realmedia as _realmedia

_RS = [_realmedia.song_s1()] if _realmedia.song_s1() else []
_RV = [_realmedia.video_a()] if _realmedia.video_a() else []


@_pytest.mark.skipif(not (_RS and _RV), reason="real video A / song S1 not configured")
@_pytest.mark.skipif(_os.environ.get("AMV_REAL") != "1", reason="AMV_REAL=1 only")
def test_assemble_trim_real_pair_a_s1():
    from amv_matcher import score as S
    from amv_matcher.audio import analyze_song
    from amv_matcher.splice import _assemble
    from amv_matcher.video import analyze_video
    song, video = analyze_song(_RS[0]), analyze_video(_RV[0])
    vd = float(video["duration"])
    for o in (0.0, 20.0, 40.0):
        asm = _assemble(song, [{"src_start": o, "src_end": o + vd, "dst_start": 0.0}], vd)
        d = S.score_at(song, video, o)["total_pct"]
        a = S.score_at(asm, video, 0.0)["total_pct"]
        print("offset", o, "direct", d, "assembled", a)
        assert abs(d - a) <= 0.5


def test_valid_allows_negative_first_segment_only():
    from amv_matcher.splice import _valid
    ok = [{"src_start": -2.0, "src_end": 10.0, "dst_start": 0.0}, {"src_start": 20.0, "src_end": 28.0, "dst_start": 12.0}]
    assert _valid(ok, 20.0, 2.0, 100.0)
    bad = [{"src_start": 0.0, "src_end": 10.0, "dst_start": 0.0}, {"src_start": -2.0, "src_end": 6.0, "dst_start": 10.0}]
    assert not _valid(bad, 18.0, 2.0, 100.0)
    far = [{"src_start": -4.0, "src_end": 10.0, "dst_start": 0.0}, {"src_start": 20.0, "src_end": 26.0, "dst_start": 14.0}]
    assert not _valid(far, 20.0, 2.0, 100.0)


def test_cuts_not_rejected_by_baseline_build_bonus(monkeypatch):
    """A build_pct bonus that only the baseline layout earns must not veto a cut plan that improves sync
    (the accept gate compares pre-bonus totals)."""
    from amv_matcher import buildup, score as S
    song, video = make_song(), make_video()
    monkeypatch.setattr(buildup, "W_BUILD", 0.0)
    ref = plan_splice(song, video, 2)
    assert ref["cuts"], ref
    base_off = S.score(song, video)["best_offset_s"]
    monkeypatch.setattr(buildup, "W_BUILD", 0.2)
    monkeypatch.setattr(buildup, "build_stats", lambda s, v, off: {
        "build_pct": 100.0 if abs(off - base_off) < 1e-6 else 0.0, "launch_pct": 0.0, "n_launches": 0, "n_builds": 0})
    r = plan_splice(song, video, 2)
    assert len(r["cuts"]) >= 1, r
