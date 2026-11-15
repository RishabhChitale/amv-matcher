"""F25: preview proxy quality setting, never-upscale, cache key per quality, quick-then-full staging."""
import av
import pytest

from amv_matcher import paths, proxy
from tests.synth import make_video


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(paths, "DATA", tmp_path / "data")
    return tmp_path


def test_quality_default_persist_and_validation(env):
    assert proxy.get_quality() == 1080
    assert proxy.set_quality(1440) == 1440 and proxy.get_quality() == 1440
    assert proxy.target_height(0) == 1080
    with pytest.raises(ValueError):
        proxy.set_quality(540)
    assert proxy.get_quality() == 1440


def test_cache_key_includes_quality(env):
    v = env / "a.avi"
    make_video(v, seconds=1)
    k = {q: proxy.proxy_path(v, q) for q in (540, 720, 1080)}
    assert len(set(k.values())) == 3
    assert proxy.quick_path(v) != proxy.proxy_path(v, 1080)


def test_never_upscales_and_crf(env):
    v = env / "a.avi"
    make_video(v, seconds=1)
    with av.open(str(v)) as c:
        src_h = c.streams.video[0].height
    out = proxy.build(v, 1440)
    with av.open(str(out)) as c:
        assert c.streams.video[0].height <= src_h
        assert c.streams.video[0].codec_context.name == "h264"


def test_staged_build_and_status(env):
    v = env / "a.avi"
    make_video(v, seconds=1)
    assert proxy.status(v) in ("building",)
    out = proxy.build(v)
    assert out == proxy.proxy_path(v) and proxy.status(v) == "ready"
    assert proxy.best_ready(v) == out and proxy.progress(v) == 1.0


def test_quality_endpoints(env):
    from fastapi.testclient import TestClient
    from amv_matcher import server
    c = TestClient(server.app, headers={"Origin": "http://127.0.0.1:8765"})
    r = c.get("/api/preview-quality").json()
    assert r["quality"] == 1080 and r["options"] == [720, 1080, 1440, 0]
    assert c.post("/api/preview-quality", json={"quality": 720}).json()["quality"] == 720
    assert c.post("/api/preview-quality", json={"quality": 5}).status_code == 400


def test_preview_status_and_pq(env):
    from fastapi.testclient import TestClient
    from amv_matcher import server
    v = env / "a.avi"
    make_video(v, seconds=1)
    c = TestClient(server.app, headers={"Origin": "http://127.0.0.1:8765"})
    server.ALLOWED_VIDEOS.add(str(v.resolve()))
    out = proxy.build(v, 720)
    r = c.get("/api/preview-status", params={"path": str(v), "pq": 720})
    assert r.status_code == 200 and r.json() == {"state": "ready", "height": 720, "target": 720, "playable": True}
    assert c.get("/api/video", params={"path": str(v), "pq": 720}).status_code == 200


def _client_with_video(env, monkeypatch):
    from fastapi.testclient import TestClient
    from amv_matcher import server
    v = env / "a.avi"
    make_video(v, seconds=1)
    server.ALLOWED_VIDEOS.add(str(v.resolve()))
    monkeypatch.setattr(server, "_wp", lambda: None)
    assert proxy.needs_proxy(v)
    return TestClient(server.app, headers={"Origin": "http://127.0.0.1:8765"}), v


def test_quick_prebuilt_default_quality_reports_lower_height_then_target(env, monkeypatch):
    """The F25 defect: quick proxy already on disk before the first /api/video at default quality."""
    import time
    from urllib.parse import quote
    c, v = _client_with_video(env, monkeypatch)
    q = proxy.quick_path(v)
    q.parent.mkdir(parents=True, exist_ok=True)
    proxy._encode(v, q, proxy.QUICK_H)           # stage 1 only
    assert q.is_file() and not proxy.proxy_path(v, 1080).is_file()
    u = "/api/video?path=" + quote(str(v), safe="") + "&pq=1080"
    r = c.get(u)
    assert r.status_code == 200
    assert r.headers["x-preview-height"] == str(proxy.QUICK_H) and r.headers["x-preview-target"] == "1080"
    assert r.headers["cache-control"] == "no-store"
    st = c.get("/api/preview-status?path=" + quote(str(v), safe="") + "&pq=1080").json()
    assert st["state"] == "building" and st["height"] == proxy.QUICK_H and st["target"] == 1080
    for _ in range(300):                          # the request itself started the full build
        if proxy.proxy_path(v, 1080).is_file():
            break
        time.sleep(0.1)
    assert proxy.proxy_path(v, 1080).is_file()
    r2 = c.get(u)
    assert r2.headers["x-preview-height"] == "1080" and "no-store" not in r2.headers.get("cache-control", "")
    st = c.get("/api/preview-status?path=" + quote(str(v), safe="") + "&pq=1080").json()
    assert st == {"state": "ready", "height": 1080, "target": 1080, "playable": True}


def test_range_past_end_is_not_416(env, monkeypatch):
    from urllib.parse import quote
    c, v = _client_with_video(env, monkeypatch)
    out = proxy.build(v)
    size = out.stat().st_size
    u = "/api/video?path=" + quote(str(v), safe="") + "&pq=1080"
    assert c.get(u, headers={"Range": "bytes=0-9"}).status_code == 206
    r = c.get(u, headers={"Range": f"bytes={size + 1000}-"})   # stale length from another proxy
    assert r.status_code == 200 and len(r.content) == size
