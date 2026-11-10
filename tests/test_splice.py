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


def test_ineligible_short_song():
    r = plan_splice(make_song(30.0), make_video(40.0), 2)
    assert r["eligible"] is False and r["reason"] == "song shorter than video"
    assert r["segments"] == [] and r["cuts"] == [] and "bar_phase" in r
    assert plan_splice(make_song(), make_video(), 2)["eligible"] is True


def test_zero_cuts_is_baseline():
    r = plan_splice(make_song(), make_video(), 0)
    assert r["cuts"] == [] and len(r["segments"]) == 1
    assert r["total_pct"] == r["baseline"]["total_pct"] and r["improvement_pct"] == 0
    assert abs(r["segments"][0]["src_start"] - r["baseline"]["offset"]) < 1e-6


def test_two_cuts_hit_both_peaks():
    song, video = make_song(), make_video()
    r = plan_splice(song, video, 2)
    assert r["improvement_pct"] > 5, r
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
