"""Heavy analysis runs in the worker pool: the server stays responsive and the job API works."""
import time

import pytest
from fastapi.testclient import TestClient

from tests.synth import make_video


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AMV_DATA_DIR", str(tmp_path / "data"))   # inherited by spawned workers
    monkeypatch.setenv("AMV_WORKERS", "2")
    from amv_matcher import server
    server.shutdown_workers()
    c = TestClient(server.app, headers={"Origin": "http://127.0.0.1:8765"})
    yield server, c, tmp_path
    server.shutdown_workers()


@pytest.mark.slow
def test_responsive_during_video_analysis(env):
    server, c, tmp = env
    v = tmp / "big.avi"
    make_video(v, seconds=60, fps=30, size=(640, 480))
    server.ALLOWED_VIDEOS.add(str(v.resolve()))
    assert c.get("/api/video", params={"path": str(v)}).status_code == 200   # warm the (one-off) preview proxy
    idle = []
    for _ in range(5):
        t0 = time.perf_counter()
        assert c.get("/api/video", params={"path": str(v)}, headers={"Range": "bytes=0-1023"}).status_code == 206
        idle.append(time.perf_counter() - t0)
    idle_med = sorted(idle)[len(idle) // 2]
    r = c.post("/api/jobs", json={"kind": "video_analysis", "path": str(v), "priority": 0})
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    lat, polls, deadline = [], 0, time.time() + 300
    while time.time() < deadline:
        t0 = time.perf_counter()
        rr = c.get("/api/video", params={"path": str(v)}, headers={"Range": "bytes=0-1023"})
        lat.append(time.perf_counter() - t0)
        assert rr.status_code == 206
        t0 = time.perf_counter()
        st = c.get(f"/api/jobs/{jid}")
        lat.append(time.perf_counter() - t0)
        polls += 1
        if st.json()["state"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert st.json()["state"] == "done", st.json()
    assert polls >= 3, "analysis finished too fast to measure"
    # Robust to a loaded CI/dev box (one slow sample must not fail it) yet still catches a real stall:
    # the p95 must stay near the idle baseline and no single request may block for ~a second.
    lat.sort()
    p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
    bound = max(0.3, 10 * idle_med)
    assert p95 < bound, f"p95 latency {p95:.3f}s (bound {bound:.3f}s, idle median {idle_med:.3f}s)"
    assert lat[-1] < 1.5, f"max latency {lat[-1]:.3f}s"


def test_job_api_guards(env):
    server, c, tmp = env
    assert c.post("/api/jobs", json={"kind": "nope", "path": "/x"}).status_code == 400
    assert c.post("/api/jobs", json={"kind": "probe", "path": "/etc/passwd"}).status_code == 403
    assert c.post("/api/prewarm/pause", headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/prewarm/pause").json() == {"paused": True}
    assert c.post("/api/prewarm/resume").json() == {"paused": False}
    assert c.get("/api/jobs/stats").json()["workers"] >= 1
    assert c.get("/api/jobs/zzz").status_code == 404
