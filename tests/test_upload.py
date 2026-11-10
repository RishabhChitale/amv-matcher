import time

import pytest
from fastapi.testclient import TestClient

from tests.synth import make_click_track, make_video


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from amv_matcher import server
    monkeypatch.setattr(server, "UPLOADS", tmp_path / "uploads")
    monkeypatch.setattr(server, "EXTRACTED", tmp_path / "extracted")
    monkeypatch.setattr(server, "EXPORTS", tmp_path / "exports")
    from amv_matcher import paths
    monkeypatch.setattr(paths, "CACHE", tmp_path / "cache")   # proxy cache stays out of the real data dir
    server.SPLICE_CACHE.clear()
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
    g = c.get("/api/uploads").json()
    assert (g["count"], g["bytes"]) == (1, len(data))
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
    server.EXPORTS.mkdir()
    (server.EXPORTS / "x.wav").write_bytes(b"12")
    g = c.get("/api/uploads").json()
    base = {k: g[k] for k in ("count", "bytes", "uploads_count", "uploads_bytes", "exports_count", "exports_bytes")}
    assert base == {"count": 3, "bytes": 11, "uploads_count": 2, "uploads_bytes": 9,
                    "exports_count": 1, "exports_bytes": 2}
    assert g["proxy_count"] == 0
    assert c.delete("/api/uploads").json() == {"freed": 11, "uploads_freed": 9, "exports_freed": 2}
    assert c.get("/api/uploads").json()["count"] == 0
    assert keep.exists() and s.exists() and v.exists()


def test_analyze_video_and_empty_score_songs(env):
    server, c, v, s, tmp = env
    uv = up(c, "video", "jump.avi", v.read_bytes()).json()["path"]
    r = c.post("/api/analyze-video", json={"video_path": uv})
    assert r.status_code == 200, r.text
    info = r.json()["video"]
    for k in ("duration", "fps", "motion_tempo_bpm", "cut_times", "energy_curve", "segments", "shots"):
        assert k in info
    # same object as the scan's video section
    jid = c.post("/api/score-songs", json={"video_path": uv, "song_paths": []}).json()
    assert jid["video"] == info and jid["results"] == [] and jid["too_short"] == []
    # second call served from the cache
    assert c.post("/api/analyze-video", json={"video_path": uv}).json()["video"] == info
    # guards: bad Origin, unscanned path outside uploads, not a video
    assert c.post("/api/analyze-video", json={"video_path": uv}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/analyze-video", json={"video_path": str(s)}).status_code == 403
    assert c.post("/api/analyze-video", json={"video_path": "/etc/passwd"}).status_code == 403
    assert c.post("/api/analyze-video", json={"video_path": str(tmp / "uploads" / ".." / "x.avi")}).status_code == 403


@pytest.fixture()
def avenv(env, monkeypatch):
    from tests.synth import make_video_with_audio
    server, c, v, s, tmp = env
    vm = tmp / "clip.mp4"
    make_video_with_audio(vm, s)
    return server, c, vm, s, tmp


def test_original_audio_is_a_fixed_offset_source(avenv):
    server, c, vm, s, tmp = avenv
    uv = up(c, "video", "clip.mp4", vm.read_bytes()).json()["path"]
    r = c.post("/api/score-songs", json={"video_path": uv, "song_paths": []}).json()
    assert r["video"]["has_audio"] is True
    oa = r["video"]["original_audio"]
    assert oa["name"].startswith("Original audio (")
    assert [x["source"] for x in r["results"]] == ["original"]
    o = r["results"][0]
    assert o["best_offset_s"] == 0 and o["bpm"] > 0
    # the extracted wav is a usable song path, and songs are tagged 'song'
    assert c.get("/api/audio", params={"path": oa["path"]}).status_code == 200
    us = up(c, "song", "c120.wav", s.read_bytes()).json()["path"]
    r2 = c.post("/api/score-songs", json={"video_path": uv, "song_paths": [us]}).json()
    assert sorted(x["source"] for x in r2["results"]) == ["original", "song"]
    # a video without audio has no original source
    nv = up(c, "video", "jump.avi", (tmp / "jump.avi").read_bytes()).json()["path"]
    assert c.post("/api/analyze-video", json={"video_path": nv}).json()["video"]["has_audio"] is False


def _wait(c, jid, t=120):
    import time
    end = time.time() + t
    while time.time() < end:
        j = c.get(f"/api/scan/{jid}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.3)
    raise AssertionError("job timed out")


def _pipeline(c, vm, s):
    uv = up(c, "video", vm.name, vm.read_bytes()).json()["path"]
    us = up(c, "song", "c120.wav", s.read_bytes()).json()["path"]
    return uv, us


def test_splice_render_export_and_guards(avenv):
    import av
    server, c, vm, s, tmp = avenv
    uv, us = _pipeline(c, vm, s)
    sp = c.post("/api/splice", json={"video_path": uv, "song_path": us, "max_cuts": 1})
    assert sp.status_code == 200, sp.text
    plan = sp.json()
    assert plan["segments"] and "total_pct" in plan
    assert c.post("/api/splice", json={"video_path": uv, "song_path": us, "max_cuts": 9}).status_code == 422
    # render: length == video duration (+-1 frame)
    r = c.post("/api/render-audio", json={"video_path": uv, "song_path": us,
                                          "segments": plan["segments"], "crossfade_ms": 40})
    assert r.status_code == 200, r.text
    rj = r.json()
    import soundfile as sf
    info = sf.info(rj["path"])
    vdur = c.post("/api/analyze-video", json={"video_path": uv}).json()["video"]["duration"]
    assert abs(info.frames / info.samplerate - vdur) <= 1 / 30 + 1e-6
    assert abs(rj["duration"] - vdur) <= 1 / 30 + 1e-6
    assert c.get(rj["url"]).status_code == 200
    # bad segments
    bad = [{"src_start": 0, "src_end": 9999, "dst_start": 0}]
    assert c.post("/api/render-audio", json={"video_path": uv, "song_path": us, "segments": bad}).status_code == 422
    # export-file only serves from .exports
    from urllib.parse import quote
    assert c.get("/api/export-file", params={"path": us}).status_code == 403
    assert c.get("/api/export-file", params={"path": str(server.EXPORTS / ".." / "uploads")}).status_code == 403
    # mp4 export (h264 -> stream copy)
    j = c.post("/api/export-mp4", json={"video_path": uv, "audio_path": rj["path"]})
    assert j.status_code == 200, j.text
    done = _wait(c, j.json()["job_id"])
    assert done["status"] == "done", done
    with av.open(done["path"]) as o:
        assert len(o.streams.video) == 1 and len(o.streams.audio) == 1
        assert abs(float(o.duration / av.time_base) - vdur) <= 0.1
    assert c.get(done["url"]).status_code == 200
    # export-mp4 rejects audio outside exports
    assert c.post("/api/export-mp4", json={"video_path": uv, "audio_path": us}).status_code == 403
    # origin guard on every new write endpoint
    evil = {"Origin": "http://evil.example"}
    for path, body in [("/api/splice", {"video_path": uv, "song_path": us}),
                       ("/api/render-audio", {"video_path": uv, "song_path": us, "segments": plan["segments"]}),
                       ("/api/export-mp4", {"video_path": uv, "audio_path": rj["path"]})]:
        assert c.post(path, json=body, headers=evil).status_code == 403, path


def test_export_mp4_reencodes_mjpeg_avi(env):
    import av
    from tests.synth import make_video_with_audio
    server, c, v, s, tmp = env
    vm = tmp / "clip.avi"
    make_video_with_audio(vm, s, seconds=4.0, vcodec="mjpeg", acodec="pcm_s16le")
    uv, us = _pipeline(c, vm, s)
    seg = [{"src_start": 0, "src_end": 4.0, "dst_start": 0}]
    rj = c.post("/api/render-audio", json={"video_path": uv, "song_path": us, "segments": seg}).json()
    done = _wait(c, c.post("/api/export-mp4", json={"video_path": uv, "audio_path": rj["path"]}).json()["job_id"])
    assert done["status"] == "done", done
    with av.open(done["path"]) as o:
        assert o.streams.video[0].codec_context.name == "h264" and len(o.streams.audio) == 1
        assert abs(float(o.duration / av.time_base) - 4.0) <= 0.15
