import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from amv_matcher import medialib_api as media_api, medialib as media_lib
from tests.synth import make_video


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(media_lib, "APP_DATA", data)
    monkeypatch.setattr(media_lib, "CACHE", data / "cache")
    monkeypatch.setattr(media_lib, "ALLOWED", [])
    app = FastAPI()
    app.include_router(media_api.router)
    c = TestClient(app)
    assert c.post("/api/media/roots", json={"path": str(root)}).status_code == 200
    return c, root, tmp_path


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_roots_add_remove_persist(env):
    c, root, tmp = env
    paths = [r["path"] for r in c.get("/api/media/roots").json()["roots"]]
    assert str(root.resolve()) in paths
    assert (tmp / "data" / "media_roots.json").is_file()
    other = tmp / "other"
    other.mkdir()
    assert c.post("/api/media/roots", json={"path": str(other)}).status_code == 200
    assert c.post("/api/media/roots", json={"path": str(tmp / "nope")}).status_code == 400
    assert c.post("/api/media/roots", json={"path": str(tmp / "data" / "media_roots.json")}).status_code == 400
    assert str(other.resolve()) in media_lib.load_roots()  # persisted (re-read from disk)
    r = c.request("DELETE", "/api/media/roots", json={"path": str(other)}).json()["roots"]
    assert str(other.resolve()) not in [x["path"] for x in r]
    assert all(x["exists"] for x in r)


def test_list_filter_sort_hidden(env):
    c, root, _ = env
    (root / "b.mp4").write_bytes(b"x")
    (root / "A.mp3").write_bytes(b"xx")
    (root / "c.txt").write_bytes(b"x")
    (root / ".hidden.mp4").write_bytes(b"x")
    (root / "Zdir").mkdir()
    (root / "adir").mkdir()
    (root / ".hdir").mkdir()
    d = c.get("/api/fs/list", params={"path": str(root)}).json()
    assert [f["name"] for f in d["folders"]] == ["adir", "Zdir"]
    assert [f["name"] for f in d["files"]] == ["A.mp3", "b.mp4"]
    f = d["files"][0]
    assert f["kind"] == "audio" and f["ext"] == ".mp3" and f["size"] == 2 and "mtime" in f
    assert d["files"][1]["kind"] == "video"
    assert d["parent"] is None  # parent of the root is outside every root
    sub = c.get("/api/fs/list", params={"path": str(root / "adir")}).json()
    assert sub["parent"] == str(root.resolve())


def test_escape_rejected(env):
    c, root, tmp = env
    outside = tmp / "outside"
    outside.mkdir()
    (outside / "x.mp4").write_bytes(b"x")
    assert c.get("/api/fs/list", params={"path": str(outside)}).status_code == 403
    assert c.get("/api/fs/list", params={"path": str(root / ".." / "outside")}).status_code == 403
    (root / "sneaky").mkdir()
    assert c.get("/api/fs/list", params={"path": str(root / "sneaky" / ".." / ".." / "outside")}).status_code == 403
    if sys.platform != "win32":
        os.symlink(outside, root / "link")
        os.symlink(outside / "x.mp4", root / "lnk.mp4")
        assert c.get("/api/fs/list", params={"path": str(root / "link")}).status_code == 403
        d = c.get("/api/fs/list", params={"path": str(root)}).json()
        assert [f["name"] for f in d["folders"]] == ["sneaky"] and d["files"] == []
        assert c.post("/api/media/link", json={"path": str(root / "lnk.mp4")}).status_code == 403
        assert c.get("/api/media/probe", params={"path": str(root / "lnk.mp4")}).status_code == 403
    assert c.post("/api/media/link", json={"path": str(outside / "x.mp4")}).status_code == 403


def test_probe_and_cache_hit(env, monkeypatch):
    c, root, tmp = env
    v = root / "v.avi"
    make_video(v, seconds=2.0, fps=10)
    p = c.get("/api/media/probe", params={"path": str(v)}).json()
    assert p["width"] == 320 and p["height"] == 240 and p["has_audio"] is False
    assert abs(p["duration"] - 2.0) < 0.3 and abs(p["fps"] - 10) < 0.5 and p["vcodec"]
    assert (tmp / "data" / "cache" / "probe.json").is_file()

    def boom(_):
        raise AssertionError("cache miss")
    monkeypatch.setattr(media_lib, "_do_probe", boom)
    assert c.get("/api/media/probe", params={"path": str(v)}).json() == p


def test_link_does_not_copy(env, monkeypatch):
    from amv_matcher import server
    seen = []
    monkeypatch.setattr(server, "allow_path", lambda p: seen.append(str(p)))
    c, root, tmp = env
    v = root / "v.avi"
    make_video(v, seconds=1.0, fps=10)
    before_root, before_tmp = _tree(root), _tree(tmp)
    r = c.post("/api/media/link", json={"path": str(v)})
    assert r.status_code == 200
    j = r.json()
    assert j["path"] == str(v.resolve()) and j["name"] == "v.avi" and j["kind"] == "video"
    assert j["size"] == v.stat().st_size
    assert seen == [str(v.resolve())]
    assert _tree(root) == before_root and _tree(tmp) == before_tmp
    (root / "n.txt").write_text("x")
    assert c.post("/api/media/link", json={"path": str(root / "n.txt")}).status_code == 404


def test_thumb_width_and_filmstrip(env):
    c, root, _ = env
    v = root / "v.avi"
    make_video(v, seconds=3.0, fps=10)
    r = c.get("/api/thumb", params={"path": str(v), "t": 1.0, "w": 100})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[1] == 100
    assert c.get("/api/thumb", params={"path": str(v)}).status_code == 200  # default t
    fs = c.get("/api/filmstrip", params={"path": str(v), "n": 4, "w": 80}).json()["frames"]
    assert len(fs) == 4 and fs[0]["t"] < fs[-1]["t"]
    assert c.get(fs[1]["url"]).status_code == 200


def test_probe_includes_download_source(env, monkeypatch):
    c, root, _ = env
    v = root / "v.avi"
    make_video(v, seconds=1.0, fps=10)
    import amv_matcher.mediameta as mm
    monkeypatch.setattr(mm, "download_source", lambda p: {"domain": "example.com"})
    assert c.get("/api/media/probe", params={"path": str(v)}).json()["download"] == {"domain": "example.com"}


def test_windows_default_roots(tmp_path, monkeypatch):
    for n in ("Videos", "Music", "Downloads"):
        (tmp_path / n).mkdir()
    monkeypatch.setattr(media_lib.sys, "platform", "win32")
    monkeypatch.setattr(media_lib.Path, "home", classmethod(lambda cls: tmp_path))
    r = media_lib.default_roots()
    assert r == [str(tmp_path / n) for n in ("Videos", "Music", "Downloads")]  # Desktop missing -> skipped
    monkeypatch.setattr(media_lib.sys, "platform", "darwin")
    assert media_lib.default_roots() == [str(tmp_path / n) for n in ("Music", "Downloads")]  # no Movies/Desktop


def test_windows_case_insensitive_containment(tmp_path, monkeypatch):
    import ntpath
    from amv_matcher import winsafe
    monkeypatch.setattr(winsafe.os, "name", "nt")
    monkeypatch.setattr(winsafe.os.path, "normcase", lambda p: p.lower())
    monkeypatch.setattr(winsafe.os, "sep", "/")
    root = tmp_path / "Root"
    assert media_lib.is_relative_to_ci(tmp_path / "ROOT" / "a.mp4", root)
    assert not media_lib.is_relative_to_ci(tmp_path / "RootX" / "a.mp4", root)


def test_uses_replace_retry(env, monkeypatch):
    calls = []
    real = media_lib.replace_retry
    monkeypatch.setattr(media_lib, "replace_retry", lambda a, b: (calls.append(1), real(a, b))[1])
    media_lib.add_root(str(env[2]))
    assert calls


def test_probe_persistence_is_throttled(env, tmp_path, monkeypatch):
    """probe() must not rewrite probe.json on every call (U3 perf)."""
    c, root, tmp = env
    writes = []
    real = media_lib._probe_flush

    def spy(cf, force=False):
        before = cf.stat().st_mtime_ns if cf.exists() else None
        real(cf, force)
        after = cf.stat().st_mtime_ns if cf.exists() else None
        writes.append(before != after)
    monkeypatch.setattr(media_lib, "_probe_flush", spy)
    vids = []
    for i in range(5):
        v = root / f"v{i}.mp4"
        make_video(str(v), seconds=1.0)
        vids.append(v)
    for v in vids:
        media_lib.probe(str(v))
    assert sum(writes) <= 2, writes


def test_thumb_decodes_are_capped(env, monkeypatch):
    """Concurrent thumb requests never run more than 2 decodes at once."""
    import threading
    import time
    c, root, tmp = env
    live = {"n": 0, "max": 0}
    lock = threading.Lock()
    paths = []
    for i in range(6):
        v = root / f"t{i}.mp4"
        make_video(str(v), seconds=1.0)
        paths.append(str(v))

    import av
    real_open = av.open

    def slow(*a, **k):
        with lock:
            live["n"] += 1
            live["max"] = max(live["max"], live["n"])
        time.sleep(0.1)
        try:
            return real_open(*a, **k)
        finally:
            with lock:
                live["n"] -= 1
    monkeypatch.setattr(av, "open", slow)
    ts = [threading.Thread(target=media_lib.thumb, args=(p, 0.1)) for p in paths]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert live["max"] <= 2, live
