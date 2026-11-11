"""F6d: timeline-log export/import (JSON + CSV), bookmarks CRUD, no file names in exports."""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from amv_matcher import project as P
from amv_matcher import timelinelog as TL
from amv_matcher import timelinelog_api as API
from amv_matcher.timelinelog_api import router

VIDEO = {"duration": 30.0, "fps": 24.0, "width": 1280, "height": 720,
         "cut_times": [1.0, 2.5, 15.56],
         "visual_booms": [{"t": 15.56, "conf": 0.8, "kind": "motion+flash", "strength": 3.1}],
         "camera_events": [{"start": 4.0, "end": 5.0, "kind": "pan", "speed": 0.4, "peak": 0.9}],
         "camera_motion": {}}
SONG = {"duration": 40.0, "bpm": 128.0, "beat_times": [0.5 + i * 60 / 128 for i in range(60)],
        "drops": [16.0], "booms": [{"t": 16.0, "conf": 0.9, "kind": "drop"},
                                   {"t": 2.0, "conf": 0.4, "class": "initial_boom"}],
        "main_boom": {"t": 16.0}}


@pytest.fixture
def media(tmp_path):
    v, s = tmp_path / "Secret Show Name EP1.mp4", tmp_path / "Secret Artist - Track.wav"
    v.write_bytes(b"v" * 3_000_000)
    s.write_bytes(b"s" * 1000)
    return v, s


@pytest.fixture
def proj(tmp_path, monkeypatch, media):
    monkeypatch.setattr(P, "APP_DATA", tmp_path / "appdata")
    v, s = media
    monkeypatch.setattr(API, "_cached", lambda vp, sp: (VIDEO if vp else None, SONG if sp else None))
    path = tmp_path / "proj.amvproj"
    P.save(path, {"name": "Private Project", "video": P.media_entry(v), "in_use": str(s), "offset": 0.5})
    return path


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_bookmark_validation():
    with pytest.raises(TL.LogError):
        TL.new_bookmark("subtitles", 1.0)
    with pytest.raises(TL.LogError):
        TL.new_bookmark("video", 1.0, "boom!!")
    with pytest.raises(TL.LogError):
        TL.new_bookmark("audio", -1.0)
    b = TL.new_bookmark("audio", 90.0004, "drop", "strong")
    assert b["t"] == 90.0 and len(b["id"]) == 12


def test_build_has_no_names_and_shape(media):
    v, s = media
    d = {"offset": 0.5, "perfect_sync_example": True, "name": "Private Project"}
    TL.add_bookmark(d, "video", 15.56, "visual_boom", "first human movement")
    TL.add_bookmark(d, "audio", 16.0, "drop")
    log = TL.build(d, VIDEO, SONG, str(v), str(s), "own", version="test", now="2026-11-11T00:00:00+00:00")
    text = json.dumps(log)
    for leak in ("Secret", "Private Project", ".mp4", ".wav", str(v.parent)):
        assert leak not in text
    assert log["format"] == TL.FORMAT and log["project"]["perfect_sync_example"] is True
    assert log["video"]["media"]["size"] == 3_000_000 and len(log["video"]["media"]["hash"]) == 64
    assert log["audio"]["bookmarks"][0]["timeline_t"] == pytest.approx(15.5)       # song_t - offset
    det = log["audio"]["detected"]
    assert [b["class"] for b in det["booms"]] == ["drop", "initial_boom"] and det["main_boom"] == 16.0
    assert det["beats"]["count"] == 60 and det["beats"]["median_interval"] == pytest.approx(60 / 128, abs=1e-3)
    assert log["video"]["detected"]["camera_peaks"][0]["t"] == 4.5
    assert TL.validate(log) == []
    real = TL.detected_audio({**SONG, "main_boom": 16.0})     # audio.py stores main_boom as a float
    assert real["main_boom"] == 16.0 and [b["main"] for b in real["booms"]] == [True, False]


def test_undetected_tracks_are_null():
    log = TL.build({}, None, None, version="x")
    assert log["video"]["detected"] is None and log["audio"]["detected"] is None and TL.validate(log) == []


def test_json_and_csv_round_trip(media):
    v, s = media
    d = {"offset": 0.5, "perfect_sync_example": True}
    TL.add_bookmark(d, "video", 77.3, "encore", 'spliced, "no build-up", ok')
    TL.add_bookmark(d, "audio", 90.0, "drop", "strong\nmust detect")
    TL.add_bookmark(d, "audio", 14.93, "initial_boom")
    log = TL.build(d, VIDEO, SONG, str(v), str(s), "own", version="t")
    want = sorted((b["track"], b["t"], b["type"], b["note"], b["id"]) for b in d["bookmarks"])
    for back in (TL.parse(json.dumps(log)), TL.parse_csv(TL.to_csv(log))):
        p2 = TL.import_into({}, back)
        assert sorted((b["track"], b["t"], b["type"], b["note"], b["id"]) for b in p2["bookmarks"]) == want
        assert p2["perfect_sync_example"] is True
    csv_back = TL.parse_csv(TL.to_csv(log))
    assert csv_back["video"]["media"]["hash"] == log["video"]["media"]["hash"]
    assert csv_back["project"]["offset"] == 0.5
    assert TL.media_match(csv_back, str(v), str(s)) == {"video": True, "audio": True}


def test_import_merge_and_errors():
    d = {}
    TL.add_bookmark(d, "video", 1.0, "other")
    log = TL.build({"bookmarks": [TL.new_bookmark("audio", 2.0, "drop")]}, None, None, version="x")
    assert len(TL.import_into(dict(d), log, "merge")["bookmarks"]) == 2
    assert len(TL.import_into(dict(d), log, "replace")["bookmarks"]) == 1
    with pytest.raises(TL.LogError):
        TL.parse({"format": "nope"})
    with pytest.raises(TL.LogError):
        TL.parse("{not json")
    bad = json.loads(json.dumps(log))
    bad["audio"]["bookmarks"][0]["type"] = "explosion"
    with pytest.raises(TL.LogError):
        TL.parse(bad)


def test_api_crud_export_import(client, proj):
    r = client.post("/api/project/bookmarks", json={"project": str(proj), "track": "audio", "t": 90.0,
                                                    "type": "drop", "note": "strong"})
    assert r.status_code == 200, r.text
    bid = r.json()["result"]["id"]
    r = client.patch(f"/api/project/bookmarks/{bid}", json={"project": str(proj), "note": "missed"})
    assert r.json()["result"]["note"] == "missed" and r.json()["result"]["t"] == 90.0
    assert client.post("/api/project/perfect-sync", json={"project": str(proj), "value": True}).status_code == 200
    r = client.get("/api/project/bookmarks", params={"project": str(proj)})
    assert len(r.json()["bookmarks"]) == 1 and r.json()["perfect_sync_example"] is True
    assert "perfect_sync_moment" in r.json()["types"]

    log = client.get("/api/timeline-log", params={"project": str(proj)}).json()
    assert "Secret" not in json.dumps(log) and "Private" not in json.dumps(log)
    assert log["audio"]["role"] == "song" and log["audio"]["bookmarks"][0]["timeline_t"] == 89.5
    csv_text = client.get("/api/timeline-log", params={"project": str(proj), "format": "csv"}).text
    assert csv_text.startswith("# format: amv-timeline-log 1") and "Secret" not in csv_text

    assert client.delete(f"/api/project/bookmarks/{bid}", params={"project": str(proj)}).status_code == 200
    assert client.delete(f"/api/project/bookmarks/{bid}", params={"project": str(proj)}).status_code == 404
    r = client.post("/api/timeline-log/import", json={"project": str(proj), "log": csv_text, "format": "csv"})
    assert r.status_code == 200, r.text
    assert [b["id"] for b in r.json()["bookmarks"]] == [bid] and r.json()["media_match"]["video"] is True
    assert TL.list_bookmarks(P.load(proj))[0]["note"] == "missed"


def test_api_unsaved_and_bad_input(client, proj):
    d = P.load(proj)
    r = client.post("/api/timeline-log", json={"data": d})
    assert r.status_code == 200 and r.json()["format"] == TL.FORMAT
    r = client.post("/api/timeline-log/import", json={"data": d, "log": r.json()})
    assert r.status_code == 200 and r.json()["data"] is not None
    assert client.post("/api/project/bookmarks", json={"project": str(proj), "track": "x", "t": 1}).status_code == 400
    assert client.post("/api/timeline-log/import", json={"data": d, "log": {"format": "x"}}).status_code == 400
    assert client.get("/api/timeline-log", params={"project": str(proj), "format": "xml"}).status_code == 400
