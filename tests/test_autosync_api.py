"""/api/auto-sync: guard, path validation, clamping, 409, job result verbatim, cached final in scans."""
import time

from fastapi.testclient import TestClient

from amv_matcher import server
from tests.test_render import make_clip, make_wav


def _setup(tmp_path, monkeypatch):
    v = make_clip(tmp_path / "in.mp4", dur=2.0)
    s = make_wav(tmp_path / "song.wav", 3.0)
    monkeypatch.setattr(server, "ALLOWED_VIDEOS", {str(v.resolve())})
    monkeypatch.setattr(server, "ALLOWED_SONGS", {str(s.resolve())})
    monkeypatch.setattr(server, "_autosync_cache_file", tmp_path / "as.json")
    return v, s


def _body(v, s, **kw):
    return {"video_path": str(v), "song_path": str(s), **kw}


def test_guard_paths_and_clamp(tmp_path, monkeypatch):
    v, s = _setup(tmp_path, monkeypatch)
    c = TestClient(server.app)
    assert c.post("/api/auto-sync", json=_body(v, s), headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/auto-sync", json=_body("/etc/passwd", s)).status_code == 403
    assert c.post("/api/auto-sync", json=_body(v, tmp_path / "nope.wav")).status_code == 403
    assert c.get("/api/auto-sync/unknown").status_code == 404
    from amv_matcher.server import AutoSyncRequest, _autosync_options
    o = _autosync_options(AutoSyncRequest(video_path="a", song_path="b", lead_s=5, budget_s=9999,
                                          guardrails={"max_speed_dev": 3, "max_tempo_pct": 50, "max_cuts": 99}))
    assert o == {"lead_s": 0.12, "allow_loop": False, "max_speed_dev": 0.25, "max_tempo_pct": 8.0,
                 "max_cuts": 3, "budget_s": 60.0}
    o = _autosync_options(AutoSyncRequest(video_path="a", song_path="b", guardrails={"max_cuts": -2, "max_speed_dev": -1}))
    assert o["max_cuts"] == 0 and o["max_speed_dev"] == 0.0


def test_409_while_running(tmp_path, monkeypatch):
    v, s = _setup(tmp_path, monkeypatch)
    c = TestClient(server.app)
    server.JOBS["fakeas"] = {"id": "fakeas", "status": "running", "options": {}}
    server._AUTOSYNC_RUNNING[(str(v.resolve()), str(s.resolve()))] = "fakeas"
    try:
        r = c.post("/api/auto-sync", json=_body(v, s))
        assert r.status_code == 409 and r.json()["job_id"] == "fakeas"
    finally:
        server._AUTOSYNC_RUNNING.clear()
        server.JOBS.pop("fakeas", None)


def test_job_result_verbatim_and_cached(tmp_path, monkeypatch):
    v, s = _setup(tmp_path, monkeypatch)
    seen = {}
    fake = {"steps": [], "final": {"total_pct": 91, "fit_after_effort": 88.5, "effort": {}, "tightness": None},
            "plan": {"segments": []}, "why_not_100": "x"}

    def fake_auto(song, video, **kw):
        seen.update(kw)
        return fake
    monkeypatch.setattr(server._autosync, "auto_sync", fake_auto)
    c = TestClient(server.app)
    r = c.post("/api/auto-sync", json=_body(v, s, allow_loop=True, guardrails={"max_cuts": 2, "max_tempo_pct": 4}))
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    for _ in range(300):
        j = c.get(f"/api/auto-sync/{jid}").json()
        if j["status"] != "running":
            break
        time.sleep(0.2)
    assert j["status"] == "done", j
    assert j["result"] == fake
    assert seen["allow_loop"] is True and seen["max_cuts"] == 2 and abs(seen["max_tempo_dev"] - 0.04) < 1e-9
    assert not server._AUTOSYNC_RUNNING
    assert server._autosync_cached(v.resolve(), s.resolve())["fit_after_effort"] == 88.5
    # a score-songs result carries it
    r = c.post("/api/score-songs", json={"video_path": str(v), "song_paths": [str(s)]})
    res = r.json()["results"] + r.json()["too_short"]
    assert res and all("auto_sync_cached" in x for x in res)
    assert [x for x in res if x["path"] == str(s.resolve())][0]["auto_sync_cached"]["fit_after_effort"] == 88.5
