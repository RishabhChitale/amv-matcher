"""Stems-pack install (G-PACK) with mocked venv/pip/verify: no real downloads."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from amv_matcher import stems as S
from amv_matcher import stems_api as A
from amv_matcher import winsafe

PIP_LINES = [
    "Collecting numpy==1.26.4\n", "  Downloading numpy-1.26.4.whl (18 MB)\n",
    "Collecting torch==2.2.2\n", "  Downloading torch-2.2.2.whl (700 MB)\n",
    "Collecting demucs==4.0.1\n", "Installing collected packages: numpy, torch, demucs\n",
    "Successfully installed numpy-1.26.4 torch-2.2.2 demucs-4.0.1\n",
]


class FakePip:
    pid = 99

    def __init__(self, lines, rc=0, on_line=None):
        self._lines, self.rc, self.on_line, self.killed = lines, rc, on_line, False
        self.returncode = None
        self.stdout = self._gen()

    def _gen(self):
        for ln in self._lines:
            if self.on_line:
                self.on_line(ln)
            yield ln

    def wait(self):
        self.returncode = self.rc
        return self.rc

    def poll(self):
        return self.returncode


@pytest.fixture
def env(monkeypatch, tmp_path):
    v = tmp_path / "app" / "stems-pack"
    monkeypatch.setattr(S, "VENV_DIR", v)
    monkeypatch.setattr(S, "free_bytes", lambda p=None: 50 * 1024 ** 3)
    monkeypatch.setattr(S, "weights_cached", lambda: False)
    monkeypatch.setattr(A, "find_system_python", lambda: ["python3"])
    monkeypatch.setattr(A, "_py_version", lambda c: (3, 11))
    A.INSTALL.clear()
    A.INSTALL.update({"status": "idle", "progress": 0.0, "message": "", "error": None})
    A.INSTALL_JOBS.clear()
    e = {"calls": [], "v": v, "verify_rc": 0, "pip": lambda: FakePip(PIP_LINES)}

    def run(cmd, **kw):
        e["calls"].append(list(cmd))
        if "venv" in cmd:
            (v / "bin").mkdir(parents=True)
            (v / "bin" / "python").write_text("")
            (v / "partial.bin").write_text("x")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, e["verify_rc"], "torch 2.2.2\ndemucs 4.0.1\n", "boom")
    monkeypatch.setattr(winsafe, "run", run)
    monkeypatch.setattr(winsafe, "popen", lambda cmd, **kw: (e["calls"].append(list(cmd)), e["pip"]())[1])
    monkeypatch.setattr(A, "_spawn", lambda fn, *a: fn(*a))      # run inline
    monkeypatch.setattr(S, "demucs_installed", lambda: (v / "pack.json").exists())
    return e


def test_stage_parsing_and_success_marker_last(env):
    seen = []
    orig = A._upd

    def spy(job, **kw):
        if "stage" in kw:
            seen.append((kw["stage"], kw.get("progress")))
        if kw.get("status") == "done":
            assert (env["v"] / "pack.json").exists()      # marker written before status flips to done
        return orig(job, **kw)
    A._upd = spy
    try:
        job = A.install_demucs()
    finally:
        A._upd = orig
    j = A.INSTALL_JOBS[job["id"]]
    assert j["status"] == "done" and j["stage"] == "done" and j["progress"] == 1.0
    stages = [s for s, _ in seen]
    assert stages[:3] == ["venv", "downloading", "downloading"]
    for st in ("installing", "verifying", "done"):
        assert st in stages
    prog = [p for _, p in seen if p is not None]
    assert prog == sorted(prog)
    m = json.loads((env["v"] / "pack.json").read_text())
    assert m["version"] == A.PACK_VERSION and m["torch"] == "2.2.2" and m["demucs"] == "4.0.1"
    assert not (env["v"] / "pack.json.tmp").exists()
    pip = [c for c in env["calls"] if "pip" in c][0]
    assert "-r" in pip and pip[pip.index("-r") + 1].endswith("requirements-stems.txt")


def test_marker_not_written_when_pip_fails_or_verify_fails(env):
    env["pip"] = lambda: FakePip(PIP_LINES[:2], rc=1)
    j = A.INSTALL_JOBS[A.install_demucs()["id"]]
    assert j["status"] == "error" and "pip failed" in j["error"]
    assert not (env["v"] / "pack.json").exists()
    env["pip"] = lambda: FakePip(PIP_LINES)
    env["verify_rc"] = 1
    j = A.INSTALL_JOBS[A.install_demucs()["id"]]
    assert j["status"] == "error" and "Verification failed" in j["error"]
    assert not (env["v"] / "pack.json").exists()


def test_disk_check_507(env, monkeypatch):
    monkeypatch.setattr(S, "free_bytes", lambda p=None: 1 * 1024 ** 3)
    with pytest.raises(HTTPException) as e:
        A.install_demucs()
    assert e.value.status_code == 507 and "3 GB" in e.value.detail
    assert not A.INSTALL_JOBS


def test_second_install_409(env, monkeypatch):
    monkeypatch.setattr(A, "_spawn", lambda fn, *a: None)        # job stays "running"
    first = A.install_demucs()
    assert first["status"] == "running"
    with pytest.raises(HTTPException) as e:
        A.install_demucs()
    assert e.value.status_code == 409


def test_cancel_kills_tree_and_removes_half_venv(env, monkeypatch):
    killed = []
    monkeypatch.setattr(winsafe, "kill_tree", lambda p: killed.append(p))

    def on_line(ln):
        if ln.startswith("Collecting torch"):
            A.cancel_install(A.INSTALL["id"])
    env["pip"] = lambda: FakePip(PIP_LINES, on_line=on_line)
    job = A.install_demucs()
    j = A.INSTALL_JOBS[job["id"]]
    assert j["status"] == "cancelled" and killed
    assert not env["v"].exists()
    with pytest.raises(HTTPException) as e:
        A.cancel_install("nope")
    assert e.value.status_code == 404


@pytest.mark.parametrize("marker", [None, {"version": "0"}])
def test_resume_half_installed_is_rebuilt(env, marker):
    env["v"].mkdir(parents=True)
    (env["v"] / "stale.bin").write_text("old")
    if marker:
        (env["v"] / "pack.json").write_text(json.dumps(marker))
    assert A.pack_status()["half_installed"] is True
    j = A.INSTALL_JOBS[A.install_demucs()["id"]]
    assert j["status"] == "done"
    assert not (env["v"] / "stale.bin").exists() and (env["v"] / "partial.bin").exists()
    st = A.pack_status()
    assert st["installed"] and not st["half_installed"] and st["version"] == A.PACK_VERSION


def test_already_installed_is_noop(env):
    env["v"].mkdir(parents=True)
    (env["v"] / "pack.json").write_text(json.dumps({"version": A.PACK_VERSION}))
    r = A.install_demucs()
    assert r["status"] == "done" and r["id"] is None and not env["calls"]


@pytest.mark.parametrize("win,rel", [(True, ("Scripts", "python.exe")), (False, ("bin", "python"))])
def test_venv_python_paths(monkeypatch, tmp_path, win, rel):
    monkeypatch.setattr(winsafe, "_is_win", lambda: win)
    assert S.venv_python(tmp_path) == tmp_path.joinpath(*rel)


def test_windows_install_uses_scripts_python_no_new_session(env, monkeypatch):
    monkeypatch.setattr(winsafe, "_is_win", lambda: True)
    kws = []
    monkeypatch.setattr(winsafe, "popen", lambda cmd, **kw: (kws.append(kw), env["calls"].append(cmd), FakePip(PIP_LINES))[2])
    A.install_demucs()
    pip = [c for c in env["calls"] if "pip" in c][0]
    assert pip[0].endswith(str(Path("Scripts") / "python.exe")) and "start_new_session" not in kws[0]


def test_weights_stage_and_status(monkeypatch):
    monkeypatch.setattr(S, "weights_cached", lambda: True)
    monkeypatch.setattr(S, "VENV_DIR", Path("/nonexistent-xyz"))
    assert A.pack_status()["weights_cached"] is True
    job = {"_cancel": None, "_proc": {}, "provider": "demucs", "started": 0, "progress": 0}

    def fake_sep(song, prov, progress, cancel, proc_holder):
        progress(0.02, S.WEIGHTS_MSG)
        assert job["stage"] == "downloading_weights"
        progress(0.5, "Separating with Demucs (CPU)")
        assert job["stage"] == "separating"
        return {"stems": {"vocals": "v.wav"}}
    monkeypatch.setattr(S, "separate", fake_sep)
    A._run(job, "song.wav")
    assert job["status"] == "done"


# ---- dev-venv guard: the installer must never rmtree the dev .venv-stems
def test_install_blocked_for_dev_venv(monkeypatch, tmp_path):
    monkeypatch.delenv("AMV_STEMS_VENV", raising=False)
    monkeypatch.setattr(S._paths, "FROZEN", False)
    monkeypatch.setattr(S, "VENV_DIR", S.DEV_VENV)
    assert "dev .venv-stems" in S.install_blocked()
    with pytest.raises(HTTPException) as e:
        A.install_demucs()
    assert e.value.status_code == 409
    job = A._new_install_job()
    A._install_job(job)                      # direct call: errors out, deletes nothing
    assert job["status"] == "error" and "Refusing" in job["error"]


def test_install_allowed_with_override_or_other_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(S._paths, "FROZEN", False)
    monkeypatch.setattr(S, "VENV_DIR", tmp_path / "v")
    monkeypatch.delenv("AMV_STEMS_VENV", raising=False)
    assert S.install_blocked() is None
    monkeypatch.setattr(S, "VENV_DIR", S.DEV_VENV)
    monkeypatch.setenv("AMV_STEMS_VENV", str(tmp_path / "x"))
    assert S.install_blocked() is None
    monkeypatch.delenv("AMV_STEMS_VENV")
    monkeypatch.setattr(S._paths, "FROZEN", True)
    assert S.install_blocked() is None       # frozen unchanged


def test_venv_dir_env_override():
    import os
    out = subprocess.run([sys.executable, "-c", "from amv_matcher import stems as S; print(S.VENV_DIR)"],
                         env={**os.environ, "AMV_STEMS_VENV": "/tmp/zz-venv"}, capture_output=True, text=True)
    from pathlib import Path
    assert Path(out.stdout.strip()) == Path("/tmp/zz-venv")
