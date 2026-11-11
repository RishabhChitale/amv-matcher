"""plan_arrangement: synthetic quiet/loud song vs peak/calm video, constraints, gated real check."""
import glob
import os

import numpy as np
import pytest

from amv_matcher.arrange import plan_arrangement

HZ = 10
BPM = 120.0
BAR = 2.0
SEC = 16.0          # 8 bars


def make_song():
    dur = 4 * SEC
    bt = np.arange(0, dur, 60.0 / BPM)
    en = np.full(int(dur * HZ), 0.12)
    sections, drops, lulls = [], [], []
    for k, loud in enumerate((False, True, False, True)):
        a, b = k * SEC, (k + 1) * SEC
        if loud:
            en[int(a * HZ):int(b * HZ)] = 0.95
            drops.append(a)
        else:
            lulls.append([a, b])
        sections.append({"start": a, "end": b, "label": "chorus" if loud else "verse",
                         "energy": 0.95 if loud else 0.12, "repeat_group": "B" if loud else "A"})
    return {"bpm": BPM, "beat_times": bt.tolist(), "beat_strengths": [1.0] * len(bt), "duration": dur,
            "energy_curve": en.tolist(), "energy_hz": HZ, "drops": drops, "lulls": lulls, "sections": sections}


def make_video():
    dur = 4 * SEC
    en = np.full(int(dur * HZ), 0.1)
    peaks = [(0.0, SEC), (2 * SEC, 3 * SEC)]
    cuts = []
    for a, b in peaks:
        en[int(a * HZ):int(b * HZ)] = 0.95
        cuts += list(np.arange(a + 0.5, b, 0.5))
    return {"duration": dur, "fps": 30, "energy_curve": en.tolist(), "energy_hz": HZ, "segments": [],
            "lulls": [[SEC, 2 * SEC], [3 * SEC, 4 * SEC]], "peak_segments": [[a, b] for a, b in peaks],
            "shots": [], "cut_times": [], "motion_curve": [], "motion_peaks": [float(c) for c in cuts],
            "motion_tempo_bpm": BPM,
            "segments_struct": [{"start": 0, "end": SEC, "kind": "peak"}, {"start": SEC, "end": 2 * SEC, "kind": "calm"},
                                {"start": 2 * SEC, "end": 3 * SEC, "kind": "peak"}, {"start": 3 * SEC, "end": 4 * SEC, "kind": "calm"}]}


def test_loud_goes_on_peaks():
    song, vid = make_song(), make_video()
    r = plan_arrangement(song, vid, max_moves=3)
    assert r["eligible"] and r["moves"] <= 3
    assert r["arc_after"] >= r["arc_before"] + 20
    assert r["total_after"] >= r["total_before"]
    assert r["sections_used"][0] == "chorus" and r["sections_used"][2] == "chorus"
    segs = r["segments"]
    assert abs(segs[-1]["src_end"] - segs[-1]["src_start"] + segs[-1]["dst_start"] - vid["duration"]) < 1e-3
    for g in segs:                                    # cuts only on section (bar) boundaries
        assert g["src_start"] % BAR < 1e-6 or abs(g["src_start"] % BAR - BAR) < 1e-6


def test_constraints_and_no_regression():
    song, vid = make_song(), make_video()
    r0 = plan_arrangement(song, vid, max_moves=0)
    assert r0["moves"] == 0 and r0["total_after"] == r0["total_before"]
    r = plan_arrangement(song, vid, max_moves=3)
    order = r["section_order"]
    assert all(a != b for a, b in zip(order, order[1:]))
    groups = [song["sections"][i]["repeat_group"] for i in order]
    assert all(not (groups[i] == groups[i + 1] == groups[i + 2]) for i in range(len(groups) - 2))


def test_short_song_and_no_sections():
    song, vid = make_song(), make_video()
    s2 = dict(song, sections=[])
    r = plan_arrangement(s2, vid)
    assert r["total_after"] == r["total_before"] and r["sections_used"] == []
    # F2b: a song shorter than the video is still arranged, on its best video stretch (never excluded for length).
    vid2 = dict(vid, duration=float(song["duration"]) + 40.0)
    r2 = plan_arrangement(song, vid2)
    assert r2["eligible"] is not False
    w = r2.get("video_window")
    assert w and 0.0 <= w["start_s"] < w["end_s"] <= vid2["duration"] + 1e-6


from tests import realmedia

_SONG = [realmedia.song_s1()] if realmedia.song_s1() else []
_VIDEO = [realmedia.video_a()] if realmedia.video_a() else []


@pytest.mark.skipif(not (_SONG and _VIDEO), reason="real video A / song S1 not configured")
@pytest.mark.skipif(os.environ.get("AMV_REAL") != "1", reason="AMV_REAL=1 only")
def test_real_pair_a_s1():
    from amv_matcher.audio import analyze_song
    from amv_matcher.video import analyze_video
    song, video = analyze_song(_SONG[0]), analyze_video(_VIDEO[0])
    r = plan_arrangement(song, video, max_moves=3)
    print("\nsections", [(s["label"], s["start"], s["end"]) for s in song["sections"]])
    print("order", r["sections_used"], r["section_order"], "moves", r["moves"])
    print("arc", r["arc_before"], "->", r["arc_after"], "total", r["total_before"], "->", r["total_after"])
    assert r["total_after"] >= r["total_before"]


def test_prefer_energy_default_off_identical_and_flag_valid():
    song, vid = make_song(), make_video()
    d0 = plan_arrangement(song, vid, max_moves=3)
    assert plan_arrangement(song, vid, max_moves=3, prefer_energy=False) == d0
    r = plan_arrangement(song, vid, max_moves=3, prefer_energy=True)
    assert r["eligible"] and r["moves"] <= 3
    assert r["arc_after"] >= d0["arc_after"] - 1e-9          # flag never lowers arc
    assert r["total_after"] >= r["total_before"] - 0         # guard: total not below baseline
    assert r["objective"] != d0["objective"]                  # different objective tuple (arc-first)
