"""/api/render-mp4 wiring: origin guard, path validation, one-at-a-time (409), tiny dup render."""
import time

from fastapi.testclient import TestClient

from amv_matcher import render, server
from tests.test_render import make_clip, make_wav, plan_of


def _setup(tmp_path, monkeypatch):
    v = make_clip(tmp_path / "in.mp4", dur=2.0)
    s = make_wav(tmp_path / "song.wav", 3.0)
    monkeypatch.setattr(server, "ALLOWED_VIDEOS", {str(v.resolve())})
    monkeypatch.setattr(server, "ALLOWED_SONGS", {str(s.resolve())})
    monkeypatch.setattr(server, "EXPORTS", tmp_path / "exports")
    monkeypatch.setattr(render.paths, "EXPORTS", tmp_path / "exports")
    return v, s


def _body(v, s):
    return {"video_path": str(v), "song_path": str(s), "plan": plan_of([[0, 0], [2, 2]]),
            "interp": "dup"}


def test_guard_and_bad_paths(tmp_path, monkeypatch):
    v, s = _setup(tmp_path, monkeypatch)
    c = TestClient(server.app)
    assert c.post("/api/render-mp4", json=_body(v, s),
                  headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/render-tools/rife", headers={"Origin": "http://evil.example"}).status_code == 403
    b = _body(v, s)
    b["video_path"] = "/etc/passwd"
    assert c.post("/api/render-mp4", json=b).status_code == 403
    b = _body(v, s)
    b["song_path"] = str(tmp_path / "nope.wav")
    assert c.post("/api/render-mp4", json=b).status_code == 403
    b = _body(v, s)
    b["interp"] = "magic"
    assert c.post("/api/render-mp4", json=b).status_code == 400
    assert c.get("/api/render-tools").status_code == 200
    assert c.get("/api/render-mp4/unknown").status_code == 404


def test_409_while_running(tmp_path, monkeypatch):
    v, s = _setup(tmp_path, monkeypatch)
    c = TestClient(server.app)
    render.JOBS["fake"] = {"id": "fake", "status": "running", "stage": "render", "done": 0}
    try:
        r = c.post("/api/render-mp4", json=_body(v, s))
        assert r.status_code == 409 and r.json()["job_id"] == "fake"
    finally:
        render.JOBS.pop("fake", None)


def test_dup_render_end_to_end(tmp_path, monkeypatch):
    v, s = _setup(tmp_path, monkeypatch)
    c = TestClient(server.app)
    r = c.post("/api/render-mp4", json=_body(v, s))
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    for _ in range(300):
        j = c.get(f"/api/render-mp4/{jid}").json()
        if j["status"] != "running":
            break
        time.sleep(0.2)
    assert j["status"] == "done", j
    assert j["path"].startswith(str((tmp_path / "exports").resolve()))
    assert c.get(j["url"]).status_code == 200


def test_cancel_is_origin_guarded(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    c = TestClient(server.app)
    assert c.post("/api/render-mp4/abc/cancel", headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/render-mp4/abc/cancel").status_code == 404      # same-site: reaches the router


def test_estimate_guard_and_path_check(tmp_path, monkeypatch):
    v, s = _setup(tmp_path, monkeypatch)
    c = TestClient(server.app)
    body = {"video_path": str(v), "plan": plan_of([[0, 0], [2, 2]]), "interp": "dup"}
    assert c.post("/api/render-estimate", json=body,
                  headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/render-estimate", json={**body, "video_path": "/etc/passwd"}).status_code == 403
    assert c.post("/api/render-estimate", json={**body, "interp": "magic"}).status_code == 400
    r = c.post("/api/render-estimate", json=body)
    assert r.status_code == 200 and "est_seconds" in r.json()
