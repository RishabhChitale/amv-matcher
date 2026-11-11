"""F2: a song shorter than the video is synced over (and rendered/exported from) a stretch of the video."""
from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from amv_matcher import autosync as A
from amv_matcher import render as RD
from amv_matcher import resolve as R
from amv_matcher import score as S
from tests.test_retime import make_song, make_video


def _song(dur, bpm=120.0):
    return make_song(dur, bpm)


def _video(dur=20.0):
    cuts = [round(1.0 + 0.5 * i, 3) for i in range(int((dur - 2) / 0.5))]
    return make_video(cuts, duration=dur, tempo=120.0)


def test_clip_video_rebases_times():
    v = _video(20.0)
    c = S.clip_video(v, 5.0, 11.0)
    assert c["duration"] == 6.0
    assert all(0.0 < t < 6.0 for t in c["cut_times"])
    assert c["cut_times"][0] == pytest.approx(0.5)
    assert len(c["energy_curve"]) == pytest.approx(60, abs=1)
    assert c["video_window"]["start_s"] == 5.0 and c["video_window"]["label"] == "uses 0:06 of 0:20 video"


def test_auto_sync_shorter_song_uses_window():
    song, v = _song(8.0), _video(20.0)
    r1 = A.auto_sync(song, v, budget_s=5.0)
    r2 = A.auto_sync(song, v, budget_s=5.0)
    vw = r1["plan"]["video_window"]
    assert 0.0 <= vw["start_s"] and vw["end_s"] <= 20.0 + 1e-6
    assert vw["used_s"] == pytest.approx(8.0, abs=0.05)
    assert r1["final"]["video_use"]["label"].startswith("uses 0:08 of 0:20")
    assert r1["plan"] == r2["plan"] and r1["final"]["total_pct"] == r2["final"]["total_pct"]   # deterministic
    assert r1["final"].get("eligible", True)


def test_auto_sync_longer_song_unchanged():
    r = A.auto_sync(_song(30.0), _video(20.0), budget_s=5.0)
    assert "video_window" not in (r.get("plan") or {})


def test_render_warp_shifts_into_window():
    plan = {"video_window": {"start_s": 4.0, "end_s": 10.0}}
    warp, out = RD.video_warp(plan, 20.0)
    assert out == pytest.approx(6.0)
    assert warp[0] == [4.0, 0.0] and warp[-1][0] == pytest.approx(10.0)
    plan["retime"] = {"warp": [[0.0, 0.0], [3.0, 3.6], [6.0, 6.6]], "out_duration": 6.6}
    warp, out = RD.video_warp(plan, 20.0)
    assert warp[0][0] == 4.0 and warp[1] == [7.0, 3.6] and out == pytest.approx(6.6)
    w0, o0 = RD.video_warp({}, 20.0)                                       # no window: whole file
    assert o0 == pytest.approx(20.0) and w0[0][0] == 0.0


def test_resolve_export_offsets_source_by_window():
    fps = 30.0
    v = _video(20.0)
    song = {"beat_times": [i * 0.5 for i in range(20)], "drops": [], "duration": 8.0}
    plan = {"offset": 0.0, "video_window": {"start_s": 5.0, "end_s": 13.0}}
    clips = R.video_clips(plan, 8.0, fps)
    assert R.out_duration_of(plan, 20.0) == pytest.approx(8.0)
    xml = R.build_fcpxml(Path("/x/vid.mp4"), 20.0, fps, 1920, 1080, plan, song, v, "audio/song.wav", 8.0)
    root = ET.fromstring(xml)
    vids = root.find(".//sequence").find("spine").findall("asset-clip")
    assert len(vids) == len(clips) == 1
    st = vids[0].get("start")
    n, d = st[:-1].split("/")
    assert int(n) / int(d) == pytest.approx(5.0, abs=1.0 / fps)
    dur = vids[0].get("duration")
    n, d = dur[:-1].split("/")
    assert int(n) / int(d) == pytest.approx(8.0, abs=1.0 / fps)
    # markers sit inside the clip's source range
    for m in vids[0].findall("marker"):
        n, d = m.get("start")[:-1].split("/") if m.get("start") != "0s" else ("0", "1")
        assert 5.0 - 1e-6 <= int(n) / int(d) <= 13.0 + 1e-6
