"""Cross-process numba warm lock: never blocks analysis, times out, works with a mocked Windows backend."""
import multiprocessing as mp
import os
import sys
import time

import pytest

from amv_matcher import numba_lock as nl


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NUMBA_CACHE_DIR", str(tmp_path / "nc"))
    return tmp_path / "nc"


def test_lock_file_lives_in_numba_cache_dir(cache_dir):
    with nl.numba_warm_lock() as got:
        assert got is True
        assert (cache_dir / ".warm.lock").exists()


def _hold(d, started, secs):
    os.environ["NUMBA_CACHE_DIR"] = d
    with nl.numba_warm_lock() as got:
        started.set()
        time.sleep(secs)


@pytest.mark.skipif(os.name == "nt", reason="uses fork-free spawn on posix is fine; nt covered by the mock test")
def test_second_process_waits_then_times_out_unlocked(cache_dir):
    ctx = mp.get_context("spawn")
    started = ctx.Event()
    p = ctx.Process(target=_hold, args=(str(cache_dir), started, 3.0))
    p.start()
    try:
        assert started.wait(30)
        t = time.monotonic()
        with nl.numba_warm_lock(timeout=0.3) as got:
            assert got is False                      # timed out: proceeds unlocked, never raises
        assert time.monotonic() - t < 2.0
        with nl.numba_warm_lock(timeout=30) as got:   # holder exits -> OS releases -> we get it
            assert got is True
    finally:
        p.join(10)


def test_killed_holder_leaves_no_stale_lock(cache_dir):
    ctx = mp.get_context("spawn")
    started = ctx.Event()
    p = ctx.Process(target=_hold, args=(str(cache_dir), started, 60.0))
    p.start()
    assert started.wait(30)
    p.kill()
    p.join(10)
    with nl.numba_warm_lock(timeout=5) as got:
        assert got is True


def test_unwritable_dir_never_blocks(monkeypatch, tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    monkeypatch.setenv("NUMBA_CACHE_DIR", str(f / "sub"))     # parent is a file: makedirs fails
    with nl.numba_warm_lock(timeout=0.1) as got:
        assert got is False


def test_body_exception_propagates_and_unlocks(cache_dir):
    with pytest.raises(ValueError):
        with nl.numba_warm_lock():
            raise ValueError("boom")
    with nl.numba_warm_lock(timeout=1) as got:
        assert got is True


def test_windows_backend_uses_msvcrt_nonblocking_retry(monkeypatch, cache_dir):
    """Mock msvcrt + os.name == 'nt': LK_NBLCK retried until it succeeds, LK_UNLCK on exit."""
    calls = []

    class FakeMsvcrt:
        LK_NBLCK, LK_UNLCK = 2, 0
        fails = 3

        @classmethod
        def locking(cls, fd, mode, n):
            calls.append(mode)
            if mode == cls.LK_NBLCK and cls.fails > 0:
                cls.fails -= 1
                raise OSError(13, "locked")

    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)
    monkeypatch.setattr(nl.os, "name", "nt")
    real_makedirs = os.makedirs
    monkeypatch.setattr(nl.os, "makedirs", real_makedirs)
    with nl.numba_warm_lock(timeout=10) as got:
        assert got is True
    assert calls == [2, 2, 2, 2, 0]


def test_windows_backend_timeout_proceeds_unlocked(monkeypatch, cache_dir):
    class FakeMsvcrt:
        LK_NBLCK, LK_UNLCK = 2, 0

        @staticmethod
        def locking(fd, mode, n):
            raise OSError(13, "locked")

    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)
    monkeypatch.setattr(nl.os, "name", "nt")
    with nl.numba_warm_lock(timeout=0.2) as got:
        assert got is False


def test_ensure_warm_runs_once_under_lock(monkeypatch):
    from amv_matcher import audio
    monkeypatch.setattr(audio, "_WARMED", False)
    entered = []
    real = nl.numba_warm_lock

    import contextlib

    @contextlib.contextmanager
    def spy(*a, **k):
        entered.append(1)
        with real(*a, **k) as g:
            yield g
    monkeypatch.setattr(nl, "numba_warm_lock", spy)
    audio.ensure_warm()
    audio.ensure_warm()
    assert entered == [1]
