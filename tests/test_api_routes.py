"""API route coverage for routes no other test hits: jobs cancel/bump, waveform, pump, index, static."""
import pytest
from fastapi.testclient import TestClient

from tests.synth import make_click_track

GOOD = {"Origin": "http://127.0.0.1:8765"}
EVIL = {"Origin": "http://evil.example"}


class _WP:
    def __init__(self):
        self.calls = []

    def cancel(self, jid):
        self.calls.append(("cancel", jid))
        return jid == "known"

    def bump(self, jid, pri):
        if pri not in (0, 1, 2):
            raise ValueError("priority must be 0, 1 or 2")
        return jid == "known"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AMV_DATA_DIR", str(tmp_path / "data"))
    from amv_matcher import server
    monkeypatch.setattr(server, "ALLOWED_SONGS", set())
    monkeypatch.setattr(server, "UPLOADS", tmp_path / "uploads")
    return server, TestClient(server.app, headers=GOOD), tmp_path


@pytest.fixture()
def fake_wp(env, monkeypatch):
    server, c, tmp = env
    wp = _WP()
    monkeypatch.setattr(server, "_wp", lambda: wp)
    return c, wp


# ---- jobs cancel / bump
def test_jobs_cancel_and_bump_workers_disabled(env, monkeypatch):
    server, c, _ = env
    monkeypatch.setattr(server, "_wp", lambda: None)
    assert c.post("/api/jobs/x/cancel").status_code == 503
    assert c.post("/api/jobs/x/bump", json={"priority": 1}).status_code == 503


def test_jobs_cancel(fake_wp):
    c, wp = fake_wp
    assert c.post("/api/jobs/known/cancel").json() == {"cancelled": True}
    assert c.post("/api/jobs/nope/cancel").json() == {"cancelled": False}
    assert c.post("/api/jobs/known/cancel", headers=EVIL).status_code == 403
    assert ("cancel", "known") in wp.calls


def test_jobs_bump(fake_wp):
    c, _ = fake_wp
    assert c.post("/api/jobs/known/bump", json={"priority": 2}).json() == {"bumped": True}
    assert c.post("/api/jobs/nope/bump", json={"priority": 1}).json() == {"bumped": False}
    assert c.post("/api/jobs/known/bump", json={"priority": 9}).status_code == 400
    assert c.post("/api/jobs/known/bump", json={"priority": "high"}).status_code == 422
    assert c.post("/api/jobs/known/bump", json={"priority": 1}, headers=EVIL).status_code == 403


# ---- waveform / pump: validation and traversal
@pytest.mark.parametrize("url", ["/api/waveform", "/api/pump"])
def test_song_routes_reject_bad_paths(env, url):
    _, c, tmp = env
    assert c.get(url).status_code == 422                                     # path missing
    assert c.get(url, params={"path": str(tmp / "nope.wav")}).status_code == 403
    assert c.get(url, params={"path": "/etc/passwd"}).status_code == 403
    assert c.get(url, params={"path": str(tmp / ".." / ".." / "etc" / "passwd")}).status_code == 403
    txt = tmp / "a.txt"
    txt.write_text("x")
    assert c.get(url, params={"path": str(txt)}).status_code == 403          # not an audio ext


def test_pump_bad_param_types(env):
    _, c, tmp = env
    assert c.get("/api/pump", params={"path": "x", "boom": "loud"}).status_code == 422


def test_waveform_and_pump_on_allowed_song(env):
    server, c, tmp = env
    wav = tmp / "click.wav"
    make_click_track(wav, bpm=120, seconds=8.0)
    server.ALLOWED_SONGS.add(str(wav.resolve()))
    r = c.get("/api/waveform", params={"path": str(wav)})
    assert r.status_code == 200, r.text
    d = r.json()
    for k in ("peaks", "duration", "bpm", "beat_times", "drops", "lulls", "energy_curve", "energy_hz", "sections"):
        assert k in d
    assert 7 < d["duration"] < 9
    r = c.get("/api/pump", params={"path": str(wav), "fps": 24})
    assert r.status_code == 200
    j = r.json()
    assert j["fps"] == 24 and isinstance(j["keyframes"], list)


# ---- index / static
def test_index_served(env):
    _, c, _ = env
    r = c.get("/")
    assert r.status_code == 200 and "html" in r.headers["content-type"]


def test_static_traversal_and_missing(env):
    server, c, _ = env
    assert c.get("/static/does-not-exist.js").status_code == 404
    assert c.get("/static/../server.py").status_code in (404,)
    assert c.get("/static/%2e%2e/server.py").status_code == 404
    assert c.get("/static/..%2fserver.py").status_code == 404
    assert c.get("/static/").status_code == 404
    some = next(p for p in server.STATIC.iterdir() if p.is_file())
    assert c.get(f"/static/{some.name}").status_code == 200
