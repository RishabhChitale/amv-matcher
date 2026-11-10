import time

import pytest
from fastapi.testclient import TestClient

from tests.synth import make_click_track, make_video


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from amv_matcher import server
    monkeypatch.setattr(server, "UPLOADS", tmp_path / "uploads")
    c = TestClient(server.app)
    v = tmp_path / "jump.avi"
    make_video(v)
    s = tmp_path / "c120.wav"
    make_click_track(s, 120)
    return server, c, v, s, tmp_path


def up(c, kind, name, data, **kw):
    return c.post("/api/upload", params={"kind": kind, "name": name}, content=data, **kw)


def test_upload_roundtrip_and_dedupe(env):
    server, c, v, s, tmp = env
    data = s.read_bytes()
    r = up(c, "song", "c120.wav", data)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["size"] == len(data) and j["kind"] == "song" and j["deduped"] is False
    assert open(j["path"], "rb").read() == data
    assert c.get("/api/audio", params={"path": j["path"]}).status_code == 200
    r2 = up(c, "song", "c120.wav", data).json()
    assert r2["deduped"] is True and r2["path"] == j["path"]
    assert c.get("/api/uploads").json() == {"count": 1, "bytes": len(data)}
    assert not list((tmp / "uploads" / "song").glob(".tmp-*"))


def test_bad_extension_and_kind(env):
    _, c, *_ = env
    assert up(c, "song", "a.exe", b"x").status_code == 400
    assert up(c, "song", "a.mp4", b"x").status_code == 400
    assert up(c, "video", "a.mp3", b"x").status_code == 400
    assert up(c, "other", "a.mp3", b"x").status_code == 400
    assert up(c, "song", "a.mp3", b"").status_code == 400


def test_too_large(env, monkeypatch):
    server, c, *_ = env
    monkeypatch.setitem(server.UPLOAD_LIMITS, "song", 100)
    assert up(c, "song", "a.mp3", b"x" * 500).status_code == 413
    assert c.get("/api/uploads").json()["count"] == 0


def test_bad_origin_403_everywhere(env):
    server, c, v, s, _ = env
    bad = {"Origin": "https://evil.example"}
    assert up(c, "song", "a.mp3", b"x", headers=bad).status_code == 403
    assert c.delete("/api/uploads", headers=bad).status_code == 403
    assert c.post("/api/scan", json={"video_path": str(v), "music_dir": str(s.parent)}, headers=bad).status_code == 403
    assert c.post("/api/score", json={"video_path": str(v), "song_path": str(s), "offset": 0}, headers=bad).status_code == 403
    assert c.post("/api/score-songs", json={"video_path": str(v), "song_paths": [str(s)]}, headers=bad).status_code == 403
    for ok in ("http://127.0.0.1:8765", "http://localhost:8765"):
        assert up(c, "song", "a.mp3", b"x", headers={"Origin": ok}).status_code == 200


def test_traversal_name_sanitized(env):
    server, c, *_ = env
    j = up(c, "song", "../../x.mp3", b"abc").json()
    assert j["name"] == "x.mp3"
    p = server.UPLOADS.resolve()
    assert j["path"].startswith(str(p / "song")) and "/../" not in j["path"]
    j2 = up(c, "song", "..\\..\\evil name?.MP3", b"abcd").json()
    assert j2["name"] == "evil name_.mp3" and j2["path"].startswith(str(p / "song"))


def test_safe_paths_reject_traversal_out_of_uploads(env):
    server, c, v, s, _ = env
    j = up(c, "song", "c120.wav", s.read_bytes()).json()
    assert c.get("/api/audio", params={"path": j["path"] + "/../../../../" + str(s)}).status_code == 403
    assert c.get("/api/audio", params={"path": str(s)}).status_code == 403  # not scanned, not uploaded
    assert c.get("/api/video", params={"path": str(v)}).status_code == 403


def test_score_songs_matches_scan(env):
    server, c, v, s, tmp = env
    music = tmp / "music"
    music.mkdir()
    make_click_track(music / "c120.wav", 120)
    jid = c.post("/api/scan", json={"video_path": str(v), "music_dir": str(music)}).json()["job_id"]
    for _ in range(300):
        scan = c.get(f"/api/scan/{jid}").json()
        if scan["status"] != "running":
            break
        time.sleep(0.2)
    assert scan["status"] == "done"
    r = c.post("/api/score-songs", json={"video_path": str(v), "song_paths": [str(music / "c120.wav")]})
    assert r.status_code == 200, r.text
    one = r.json()   # single song: synchronous, finished job inline
    assert one["status"] == "done"
    assert one["results"] == scan["results"] and one["too_short"] == scan["too_short"]
    assert one["video"] == scan["video"]
    # several songs: job + polling, short song lands in too_short
    short = music / "short.wav"
    make_click_track(short, 100, seconds=6)
    r = c.post("/api/score-songs", json={"video_path": str(v),
                                         "song_paths": [str(music / "c120.wav"), str(short)]}).json()
    assert r["status"] == "running"
    for _ in range(300):
        j = c.get(f"/api/scan/{r['job_id']}").json()
        if j["status"] != "running":
            break
        time.sleep(0.2)
    assert j["status"] == "done" and [x["name"] for x in j["results"]] == ["c120.wav"]
    assert [x["name"] for x in j["too_short"]] == ["short.wav"]
    # an uploaded video + uploaded song work together
    uv = up(c, "video", "jump.avi", v.read_bytes()).json()["path"]
    us = up(c, "song", "c120.wav", s.read_bytes()).json()["path"]
    j = c.post("/api/score-songs", json={"video_path": uv, "song_paths": [us]}).json()
    assert j["status"] == "done" and j["results"][0]["total_pct"] > 0
    assert c.get("/api/video", params={"path": uv}, headers={"Range": "bytes=0-100"}).status_code == 206


def test_delete_clears_only_uploads(env):
    server, c, v, s, tmp = env
    keep = tmp / "keep.txt"
    keep.write_text("x")
    up(c, "song", "a.mp3", b"abc")
    up(c, "video", "a.mp4", b"abcdef")
    assert c.get("/api/uploads").json() == {"count": 2, "bytes": 9}
    assert c.delete("/api/uploads").json() == {"freed": 9}
    assert c.get("/api/uploads").json() == {"count": 0, "bytes": 0}
    assert keep.exists() and s.exists() and v.exists()
