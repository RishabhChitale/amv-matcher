"""Structural validation of the Resolve export (Resolve itself is not available here)."""
from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from amv_matcher import resolve as R

FPS = 30.0
SONG = {"beat_times": [i * 0.5 for i in range(80)], "drops": [10.0, 20.0], "duration": 40.0}
VIDEO = {"duration": 30.0, "fps": FPS, "cut_times": [3.0, 9.0, 15.0], "motion_peaks": []}


def _frames(s):
    if s == "0s":
        return 0
    n, d = s[:-1].split("/")
    return int(n) / int(d)


def _parse(plan, dur=30.0, tmp=None):
    clips = R.video_clips(plan, dur, FPS)
    xml = R.build_fcpxml(Path("/x/vid.mp4"), dur, FPS, 1920, 1080, plan, SONG, VIDEO, "audio/song.wav", 40.0)
    return clips, ET.fromstring(xml)


def _check(plan, n_video, dur=30.0):
    clips, root = _parse(plan, dur)
    seq = root.find(".//sequence")
    vids = seq.find("spine").findall("asset-clip")
    assert len(vids) == n_video == len(clips)
    tot = sum(_frames(v.get("duration")) for v in vids)
    assert abs(tot - _frames(seq.get("duration"))) < 1e-6
    out = R.out_duration_of(plan, dur)
    assert abs(tot - out) <= 1.0 / FPS + 1e-6
    # contiguous
    pos = 0
    for v in vids:
        assert abs(_frames(v.get("offset")) - pos) < 1e-6
        pos += _frames(v.get("duration"))
    # audio on lanes, relative path
    auds = root.findall(".//asset-clip[@lane]")
    assert auds
    src = root.find(".//asset[@id='r3']/media-rep").get("src")
    assert not src.startswith("/") and "://" not in src
    return root, vids, auds


def test_no_retime_negative_offset():
    plan = {"offset": -2.0, "tempo_ratio": 1.0, "segments": None, "retime": None}
    root, vids, auds = _check(plan, 1)
    assert len(auds) == 1
    assert abs(_frames(auds[0].get("start"))) < 1e-9
    assert abs(_frames(auds[0].get("offset")) - 2.0) < 1e-6   # lead silence
    assert root.find(".//timeMap") is None


def test_no_retime_positive_offset_markers():
    plan = {"offset": 3.0, "tempo_ratio": 1.0, "segments": None, "retime": None}
    root, vids, auds = _check(plan, 1)
    names = [m.get("value") for m in root.iter("marker")]
    assert "DROP" in names and "beat" in names and names.count("CUT") == 3
    assert names.count("DROP") == 2
    # one beat marker per bar (4 beats), not per beat
    assert names.count("beat") < 25


def test_three_sections():
    warp = [[0, 0], [5, 5], [10, 8], [15, 13], [20, 18], [30, 28]]  # speed 1,1.667,1,1,1.0?? merged
    plan = {"offset": 0.0, "tempo_ratio": 1.0, "segments": None,
            "retime": {"warp": warp, "out_duration": 28.0, "sections": []}}
    root, vids, auds = _check(plan, 3, dur=30.0)
    assert len(root.findall(".//timeMap")) == 1
    sp = root.find(".//timeMap").findall("timept")
    assert len(sp) == 2
    # clip durations sum to 28 s
    assert abs(sum(_frames(v.get("duration")) for v in vids) - 28.0) <= 1 / FPS


def test_splices_overlap_and_fades():
    plan = {"offset": 0.0, "tempo_ratio": 1.0,
            "segments": [{"src_start": -1.0, "src_end": 8.0, "dst_start": 0.0},
                         {"src_start": 20.0, "src_end": 30.0, "dst_start": 8.0},
                         {"src_start": 5.0, "src_end": 17.0, "dst_start": 18.0}],
            "retime": None}
    root, vids, auds = _check(plan, 1)
    assert len(auds) == 3
    lanes = [a.get("lane") for a in auds]
    assert lanes == ["-1", "-2", "-1"]
    assert len(root.findall(".//fadeIn")) == 2 and len(root.findall(".//fadeOut")) == 2
    # first segment had negative src_start -> shifted later, start 0
    assert _frames(auds[0].get("start")) == 0
    assert abs(_frames(auds[0].get("offset")) - 1.0) < 1e-6


def test_zero_sections_empty_retime():
    plan = {"offset": 0.0, "tempo_ratio": 1.0, "segments": None,
            "retime": {"warp": [[0, 0], [30, 30]], "out_duration": 30.0, "sections": []}}
    root, vids, auds = _check(plan, 1)
    assert root.find(".//timeMap") is None


def test_ntsc_rate_tiling():
    fd = R.frame_dur(29.97)
    assert fd == (1001, 30000)
    warp = [[0, 0], [10, 9.3], [20, 21.7], [30.0, 30.0]]
    clips = R.video_clips({"retime": {"warp": warp}}, 30.0, 29.97)
    assert sum(c["dur_f"] for c in clips) == R.to_frames(30.0, 29.97)


def test_pump_setting_and_drfx(tmp_path):
    plan = {"offset": 0.0, "tempo_ratio": 1.0, "segments": None, "retime": None}
    s = R.build_pump_setting(SONG, plan, 30.0, FPS, 1920, 1080, boom=40, decay=180)
    assert s.count("{") == s.count("}") and "AMVPumpScale" in s and "[0]" in s
    out = R.build_drfx(s, tmp_path / "AMV_Pump.drfx")
    assert "Edit/Effects/AMV/AMV Pump.setting" in zipfile.ZipFile(out).namelist()


def test_export_bundle_end_to_end(tmp_path):
    from tests.synth import make_click_track, make_video
    song = tmp_path / "song.wav"
    vid = tmp_path / "clip.avi"
    make_click_track(song, 120, 20)
    make_video(vid, seconds=10.0, fps=30, jump_every=0.5, size=(160, 120))
    plan = {"offset": -1.0, "tempo_ratio": 1.0, "segments": None, "retime": None}
    video = {"duration": 10.0, "fps": 30.0, "cut_times": [2.0], "motion_peaks": []}
    import numpy as np
    song_an = {"beat_times": [i * 0.5 for i in range(40)], "drops": [4.0], "duration": 20.0, "bpm": 120}
    res = R.export_bundle(vid, song, plan, song_an, video, tmp_path / "exports")
    folder = Path(res["folder"])
    assert folder.name == "clip__song_resolve"
    for f in ("timeline.fcpxml", "AMV_Pump.drfx", "AMV_Pump_plan.setting", "HOW-TO-IMPORT.txt"):
        assert (folder / f).is_file()
    root = ET.parse(folder / "timeline.fcpxml").getroot()
    rel = root.find(".//asset[@id='r3']/media-rep").get("src")
    assert (folder / rel).is_file()
    assert Path(res["zip"]).is_file()
    assert res["clips"] == 1 and res["markers"] >= 2
    assert abs(res["out_duration"] - 10.0) <= 1 / 30


def test_endpoint_guards(tmp_path):
    from fastapi.testclient import TestClient
    from amv_matcher import server
    c = TestClient(server.app)
    body = {"video_path": "/etc/passwd", "song_path": "/etc/passwd", "plan": {}}
    r = c.post("/api/export-resolve", json=body, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = c.post("/api/export-resolve", json=body, headers={"Origin": "http://127.0.0.1:8765"})
    assert r.status_code in (400, 403)
