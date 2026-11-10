"""Windows semantics of stems.py / stems_api.py, simulated (no real demucs, no downloads)."""
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from amv_matcher import stems as S
from amv_matcher import stems_api as A
from amv_matcher import winsafe


class _FakeProc:
    pid = 4321
    returncode = 0

    def __init__(self, cmd, **kw):
        _FakeProc.last = (cmd, kw)
        tmp = Path(cmd[-1])
        tmp.mkdir(parents=True, exist_ok=True)
        for n in S.STEM_NAMES["demucs"]:
            (tmp / f"{n}.wav").write_bytes(b"w")
        self.stdout = iter(["PROGRESS 0.5\n"])

    def wait(self):
        return 0

    def poll(self):
        return 0


@pytest.fixture
def fake_sep(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "demucs_installed", lambda: True)
    monkeypatch.setattr(S, "demucs_python", lambda: Path("py"))
    monkeypatch.setattr(S, "free_bytes", lambda p: 10 * 1024 ** 3)
    monkeypatch.setattr(S, "_load_stereo", lambda p: None)
    monkeypatch.setattr(S, "_write", lambda p, y: Path(p).write_bytes(b"in"))
    monkeypatch.setattr(S, "CACHE_DIR", tmp_path / "c")
    monkeypatch.setattr(S, "TORCH_HOME", tmp_path / "t")
    monkeypatch.setattr(subprocess, "Popen", _FakeProc)
    return tmp_path


@pytest.mark.parametrize("win", [True, False])
def test_demucs_popen_flags_and_cleanup(monkeypatch, fake_sep, win):
    monkeypatch.setattr(winsafe, "_is_win", lambda: win)
    out = fake_sep / f"o{win}"
    res = S.separate_demucs("song.mp3", out)
    kw = _FakeProc.last[1]
    if win:
        assert kw["creationflags"] & winsafe.CREATE_NO_WINDOW and "start_new_session" not in kw
    else:
        assert kw["start_new_session"] is True
    assert all(Path(p).is_file() for p in res["stems"].values())
    assert not (out / "_tmp").exists() and not (out / "_input.wav").exists()


def test_replace_retry_used_and_kill_tree_on_cancel(monkeypatch, fake_sep):
    calls = []
    monkeypatch.setattr(winsafe, "replace_retry", lambda a, b, *x, **k: calls.append((a, b)))
    S.separate_demucs("song.mp3", fake_sep / "o1")
    assert len(calls) == 4

    killed = []
    monkeypatch.setattr(winsafe, "kill_tree", lambda p: killed.append(p))
    ev = threading.Event()
    ev.set()
    with pytest.raises(S.Cancelled):
        S.separate_demucs("song.mp3", fake_sep / "o2", cancel=ev)
    assert killed


def test_locked_temp_is_reported_not_silent(monkeypatch, fake_sep):
    S.LEAKED_TEMP.clear()
    monkeypatch.setattr(winsafe, "safe_unlink", lambda *a, **k: False)
    f = fake_sep / "stuck.wav"
    f.write_bytes(b"x")
    assert S._cleanup_file(f, fake_sep) is False
    assert str(f) in S.LEAKED_TEMP


def test_song_key_uses_norm_case_path(monkeypatch, tmp_path):
    f = tmp_path / "Song.mp3"
    f.write_bytes(b"abc")
    seen = []
    monkeypatch.setattr(winsafe, "norm_case_path", lambda p: seen.append(str(p)) or "X")
    k1 = S.song_key(f)
    assert seen and k1 == S.song_key(f)
    monkeypatch.setattr(winsafe, "norm_case_path", lambda p: "Y")
    assert S.song_key(f) != k1


def test_cancel_endpoint_uses_kill_tree(monkeypatch):
    killed = []
    monkeypatch.setattr(winsafe, "kill_tree", lambda p: killed.append(p))

    class P:
        def poll(self):
            return None
    ev = threading.Event()
    monkeypatch.setattr(A, "_song", lambda s: "song")
    monkeypatch.setattr(S, "song_key", lambda s: "k")
    A.JOBS["j"] = {"status": "running", "_cancel": ev, "_proc": {"proc": P()}}
    A.LATEST[("k", "demucs")] = "j"
    try:
        r = A.cancel_stems(type("R", (), {"song_path": "x"})())
    finally:
        A.JOBS.pop("j", None)
        A.LATEST.pop(("k", "demucs"), None)
    assert r["cancelled"] == 1 and killed and ev.is_set()


# ---------------------------------------------------------------- python discovery / pack status
def _fake_run(versions):
    def run(cmd, **kw):
        v = versions.get(tuple(cmd[:-2]))
        return subprocess.CompletedProcess(cmd, 0 if v else 1, stdout=(v or "") + "\n", stderr="")
    return run


def test_find_system_python_windows_uses_py_launcher(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(winsafe, "_is_win", lambda: True)
    monkeypatch.setattr(A.shutil, "which", lambda c: c)
    monkeypatch.setattr(winsafe, "run", _fake_run({("py", "-3.11"): "3.11", ("py", "-3.10"): "3.10"}))
    assert A.find_system_python() == ["py", "-3.11"]


def test_find_system_python_never_returns_app_exe_and_checks_version(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(winsafe, "_is_win", lambda: False)
    monkeypatch.setattr(A.shutil, "which", lambda c: c)
    monkeypatch.setattr(winsafe, "run", _fake_run({("python3",): "3.13", ("/usr/bin/python3",): "3.8"}))
    assert A.find_system_python() is None
    monkeypatch.setattr(winsafe, "run", _fake_run({("python3",): "3.12"}))
    r = A.find_system_python()
    assert r == ["python3"] and sys.executable not in r


def test_pack_status_shapes(monkeypatch, tmp_path):
    monkeypatch.setattr(S, "VENV_DIR", tmp_path / "none")
    monkeypatch.setattr(S, "demucs_installed", lambda: False)
    monkeypatch.setattr(S, "weights_cached", lambda: False)
    st = A.pack_status()
    assert st == {"installed": False, "version": None, "python_path": None, "size_mb": 0.0, "source": None,
                  "half_installed": False, "weights_cached": False}
    (tmp_path / "pack.json").write_text(json.dumps({"version": A.PACK_VERSION}))
    (tmp_path / "f.bin").write_bytes(b"x" * 1024 * 1024)
    monkeypatch.setattr(S, "VENV_DIR", tmp_path)
    monkeypatch.setattr(S, "demucs_installed", lambda: True)
    monkeypatch.setattr(S, "demucs_python", lambda: tmp_path / "bin" / "python")
    monkeypatch.setattr(S, "paths_frozen", lambda: True)
    st = A.pack_status()
    assert st["installed"] and st["version"] == A.PACK_VERSION and st["source"] == "pack"
    assert st["size_mb"] >= 1.0 and not st["half_installed"]


def test_install_job_frozen_builds_venv_with_system_python(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(S, "VENV_DIR", tmp_path / "v")
    monkeypatch.setattr(A, "find_system_python", lambda: ["py", "-3.11"])
    monkeypatch.setattr(A, "_py_version", lambda c: (3, 11))
    seen = []
    monkeypatch.setattr(winsafe, "run", lambda cmd, **kw: seen.append(cmd))

    class Pip:
        stdout = iter([])

        def __init__(self, cmd, **kw):
            seen.append(cmd)

        def wait(self):
            return 1
    monkeypatch.setattr(subprocess, "Popen", Pip)
    A._install_job()
    assert seen[0][:3] == ["py", "-3.11", "-m"] and sys.executable not in seen[0]
    assert A.INSTALL["status"] == "error"
    # no system python -> clear error, nothing run
    seen.clear()
    monkeypatch.setattr(A, "find_system_python", lambda: None)
    A._install_job()
    assert not seen and "No system Python" in A.INSTALL["error"]
