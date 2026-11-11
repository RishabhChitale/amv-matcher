"""A crashed song worker is retried once, then analysed in-process; a final failure carries its reason.
Also: the per-build numba cache dir."""
import os

import pytest

from tests.test_amv import make_click_track


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AMV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AMV_WORKERS", "2")
    monkeypatch.setenv("AMV_TEST_CRASH_WORKER", "1")      # inherited by spawned workers: every song worker dies
    from amv_matcher import server
    server.shutdown_workers()
    yield server, tmp_path
    server.shutdown_workers()


def test_crashing_worker_falls_back_in_process(env):
    server, tmp = env
    p = tmp / "c120.wav"
    make_click_track(p, 120)
    got = server._song(str(p))                              # worker dies twice (retry), then in-process
    want = server.analyze_song(str(p))
    assert got["bpm"] == want["bpm"] and got["duration"] == want["duration"]
    assert set(got) == set(want)


def test_always_failing_song_reason_reaches_skipped(env, monkeypatch):
    server, tmp = env
    p = tmp / "bad.wav"
    make_click_track(p, 120)

    def boom(path, *a, **k):
        raise RuntimeError("decode exploded")
    monkeypatch.setattr(server, "analyze_song", boom)
    with pytest.raises(ValueError) as ei:
        server._song(str(p))
    assert "worker crashed x2, in-process failed: RuntimeError: decode exploded" in str(ei.value)


def test_numba_cache_dir_per_version_and_prune(tmp_path, monkeypatch):
    from amv_matcher import paths, timelinelog
    monkeypatch.setenv("AMV_DATA_DIR", str(tmp_path))
    root = tmp_path / "numba-cache"
    seen = []
    for i, v in enumerate(["1.0", "1.1", "1.2"]):
        monkeypatch.setattr(timelinelog, "_VERSION", v)
        d = paths.setup_numba_cache()
        assert d == root / v and os.environ["NUMBA_CACHE_DIR"] == str(d)
        os.utime(d, (1000 + i, 1000 + i))
        seen.append(d)
    assert sorted(x.name for x in root.iterdir()) == ["1.1", "1.2"]      # current + previous kept, 1.0 pruned
