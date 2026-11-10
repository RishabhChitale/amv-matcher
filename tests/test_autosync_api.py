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


def _wait(c, jid, want=("done", "error", "cancelled")):
    for _ in range(300):
        j = c.get(f"/api/auto-sync/{jid}").json()
        if j["status"] in want:
            return j
        time.sleep(0.05)
    return j


def test_progress_and_cooperative_cancel(tmp_path, monkeypatch):
    import threading
    v, s = _setup(tmp_path, monkeypatch)
    started = threading.Event()

    def fake_auto(song, video, progress_cb=None, should_cancel=None, **kw):
        progress_cb({"step": "retime"})
        started.set()
        for _ in range(200):
            if should_cancel():
                return {"cancelled": True}
            time.sleep(0.02)
        return {"final": {}}
    monkeypatch.setattr(server._autosync, "auto_sync", fake_auto)
    c = TestClient(server.app)
    assert c.post("/api/auto-sync/nope/cancel").status_code == 404
    jid = c.post("/api/auto-sync", json=_body(v, s)).json()["job_id"]
    assert started.wait(10)
    j = c.get(f"/api/auto-sync/{jid}").json()
    assert j["steps_so_far"] == [{"step": "retime"}] and j["current_step"] == "retime"
    assert c.post(f"/api/auto-sync/{jid}/cancel", headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post(f"/api/auto-sync/{jid}/cancel").json()["cancelled"] is True
    j = _wait(c, jid)
    assert j["status"] == "cancelled" and j["result"] is None
    assert c.post(f"/api/auto-sync/{jid}/cancel").json()["status"] == "cancelled"     # idempotent
    for _ in range(100):
        if not server._AUTOSYNC_RUNNING:
            break
        time.sleep(0.05)
    assert not server._AUTOSYNC_RUNNING and jid not in server._AUTOSYNC_CANCEL


def test_cancel_without_hooks_discards_result(tmp_path, monkeypatch):
    import threading
    v, s = _setup(tmp_path, monkeypatch)
    go = threading.Event()

    def old_auto(song, video, **kw):          # an autosync.py without the hooks
        assert "progress_cb" not in kw and "should_cancel" not in kw
        go.wait(10)
        return {"final": {"total_pct": 1}}
    monkeypatch.setattr(server._autosync, "auto_sync", old_auto)
    c = TestClient(server.app)
    jid = c.post("/api/auto-sync", json=_body(v, s)).json()["job_id"]
    assert c.post(f"/api/auto-sync/{jid}/cancel").json()["status"] == "cancelled"
    go.set()
    time.sleep(0.5)
    j = c.get(f"/api/auto-sync/{jid}").json()
    assert j["status"] == "cancelled" and j["result"] is None
    assert server._autosync_cached(v.resolve(), s.resolve()) is None


def test_exports_member_validation(tmp_path, monkeypatch):
    from amv_matcher import paths
    ex = tmp_path / "exports"
    ex.mkdir()
    (ex / "a.mp4").write_bytes(b"x")
    (ex / "sub").mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("s")
    (ex / "link").symlink_to(outside)
    monkeypatch.setattr(paths, "EXPORTS", ex)
    assert paths.exports_member(ex / "a.mp4") == (ex / "a.mp4").resolve()
    assert paths.exports_member(ex / "sub") == (ex / "sub").resolve()
    for bad in (ex, outside, ex / "missing.mp4", ex / ".." / "secret.txt", ex / "link", "", "/etc/passwd", None):
        assert paths.exports_member(bad) is None


def test_native_open_reveal_only_inside_exports(tmp_path, monkeypatch):
    import app_main
    from amv_matcher import paths
    ex = tmp_path / "exports"
    ex.mkdir()
    f = ex / "a.mp4"
    f.write_bytes(b"x")
    monkeypatch.setattr(paths, "EXPORTS", ex)
    calls = []
    monkeypatch.setattr(app_main.Api, "_launch", staticmethod(lambda cmd: calls.append(cmd)))
    monkeypatch.setattr(app_main.sys, "platform", "darwin")
    api = app_main.Api()
    assert api.open_path(str(f)) is True and calls[-1] == ["open", str(f.resolve())]
    assert api.reveal_path(str(f)) is True and calls[-1] == ["open", "-R", str(f.resolve())]
    n = len(calls)
    assert api.open_path("/etc/passwd") is False and api.reveal_path(str(tmp_path / "x")) is False
    assert api.open_path(str(ex / ".." / "exports" / ".." / "a.mp4")) is False
    assert len(calls) == n
