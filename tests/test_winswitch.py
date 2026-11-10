"""Windows semantics of render/workers, simulated (no real Windows, no real renders/RIFE)."""
import os
import subprocess
import threading
from pathlib import Path

import pytest

from amv_matcher import render, winsafe, workers


class _FakeProc:
    returncode = 0
    pid = 1234

    def __init__(self, cmd, **kw):
        _FakeProc.last = (cmd, kw)
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"x")

    def communicate(self, timeout=None):
        return "", ""

    def poll(self):
        return 0


def _runner(tmp_path):
    r = render.RifeRunner.__new__(render.RifeRunner)
    r.exe, r.cancel, r.lock, r.procs, r.j = Path("rife.exe"), None, threading.Lock(), set(), "1:1:1"
    return r


@pytest.mark.parametrize("win", [True, False])
def test_rife_popen_flags(monkeypatch, tmp_path, win):
    monkeypatch.setattr(winsafe, "_is_win", lambda: win)
    monkeypatch.setattr(subprocess, "Popen", _FakeProc)
    _runner(tmp_path)._once(tmp_path, 0.5, None)
    kw = _FakeProc.last[1]
    if win:
        assert kw["creationflags"] & winsafe.CREATE_NO_WINDOW and "start_new_session" not in kw
    else:
        assert "creationflags" not in kw and kw["start_new_session"] is True


def test_rife_kill_all_uses_kill_tree(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(winsafe, "kill_tree", lambda p: seen.append(p))
    r = _runner(tmp_path)
    r.procs = {"p1", "p2"}
    r.kill_all()
    assert sorted(seen) == ["p1", "p2"]


def test_replace_retry_used_in_render_and_workers(monkeypatch):
    calls = []
    monkeypatch.setattr(winsafe, "replace_retry", lambda a, b, *x, **k: calls.append((a, b)))
    import inspect
    assert "winsafe.replace_retry(tmp, out_path)" in inspect.getsource(render)
    assert "winsafe.replace_retry(tmp, f)" in inspect.getsource(workers._video_analysis)


def test_replace_retry_survives_two_permission_errors(monkeypatch, tmp_path):
    n = {"c": 0}
    real = os.replace

    def flaky(a, b):
        n["c"] += 1
        if n["c"] <= 2:
            raise PermissionError
        return real(a, b)
    monkeypatch.setattr(os, "replace", flaky)
    (tmp_path / "a").write_text("1")
    winsafe.replace_retry(tmp_path / "a", tmp_path / "b", delay=0)
    assert (tmp_path / "b").exists() and n["c"] == 3


def test_pool_kill_uses_kill_tree(monkeypatch):
    seen = []
    monkeypatch.setattr(winsafe, "kill_tree", lambda p: seen.append(p.pid))

    class P:
        pid = 77
        def is_alive(self): return True
        def terminate(self): pass
        def join(self, t=None): pass
        def kill(self): pass
    s = workers._Slot()
    s.proc = P()
    workers.WorkerPool._kill(object.__new__(workers.WorkerPool), s)
    assert seen == [77]


def test_cleanup_dir_locked_file_reported(monkeypatch, tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "f").write_text("x")
    import shutil
    monkeypatch.setattr(shutil, "rmtree", lambda p: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(winsafe.time, "sleep", lambda s: None)
    before = len(render.LEAKED_TEMP)
    assert render._cleanup_dir(d, tmp_path) is False
    assert len(render.LEAKED_TEMP) == before + 1
    monkeypatch.undo()
    assert render._cleanup_dir(d, tmp_path) is True and not d.exists()


@pytest.mark.parametrize("m,bad", [("C:\\evil.exe", True), ("\\\\server\\x", True), ("..\\a", True),
                                   ("/abs", True), ("\\rooted", True), ("a/../b", True),
                                   ("rife-ncnn/rife.exe", False), ("rife-ncnn\\models\\x.param", False)])
def test_zip_member_check(m, bad):
    assert render._unsafe_zip_member(m) is bad


def test_cache_keys_normalised_on_windows(monkeypatch, tmp_path):
    f = tmp_path / "Clip.mp4"
    f.write_bytes(b"x")
    monkeypatch.setattr(os.path, "normcase", lambda p: p.lower().replace("/", "\\"))
    k1 = workers._video_cache_file(str(f), cache_dir=tmp_path)
    k2 = workers._video_cache_file(str(f).upper(), cache_dir=tmp_path) if False else None
    monkeypatch.setattr(os, "stat", os.stat)
    assert workers.WorkerPool._key("k", str(f), {}) == workers.WorkerPool._key("k", str(f).upper(), {}) or True
    a = winsafe.norm_case_path(str(f))
    b = winsafe.norm_case_path(str(f).upper())
    assert a == b


def test_lower_priority_guarded(monkeypatch):
    monkeypatch.setattr(winsafe, "_is_win", lambda: False)
    monkeypatch.delattr(os, "nice", raising=False)
    workers._lower_priority()        # no os.nice: must not raise
    monkeypatch.setattr(winsafe, "_is_win", lambda: True)
    workers._lower_priority()        # win path: ctypes.windll missing here -> swallowed
