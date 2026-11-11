"""U10: Do-not-disturb flag API + tools/relaunch_app.sh DND logic (always --dry-run / temp data dir; never touches the real app)."""
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from amv_matcher import project as P
from amv_matcher.dnd_api import router

SCRIPT = str(Path(__file__).resolve().parent.parent / "tools" / "relaunch_app.sh")


def _real_bash():
    """The relaunch script is macOS-only; need a real POSIX bash (Windows runners often have the WSL stub bash.exe)."""
    if sys.platform == "win32":
        return False
    b = shutil.which("bash")
    if not b:
        return False
    try:
        r = subprocess.run([b, "-c", "echo ok"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and r.stdout.strip() == "ok"
    except (OSError, subprocess.SubprocessError):
        return False


needs_bash = pytest.mark.skipif(not _real_bash(), reason="tools/relaunch_app.sh is macOS-only; needs a real POSIX bash")


def run(d, *args, timeout=30):
    return subprocess.run(["bash", SCRIPT, "--dry-run", "--data-dir", str(d), "--app", str(d / "X.app"), *args],
                          capture_output=True, text=True, timeout=timeout)


def set_dnd(d, on):
    d.mkdir(parents=True, exist_ok=True)
    (d / "dnd.json").write_text(json.dumps({"on": on, "since": 1, "by": "user"}))


@needs_bash
def test_off_or_missing_proceeds_at_once(tmp_path):
    r = run(tmp_path)                       # no dnd.json
    assert r.returncode == 0 and "proceeding" in r.stdout and "dry-run" in r.stdout
    set_dnd(tmp_path, False)
    assert run(tmp_path).returncode == 0


@needs_bash
def test_on_times_out_with_exit_3(tmp_path):
    set_dnd(tmp_path, True)
    r = run(tmp_path, "2", "--poll", "1")
    assert r.returncode == 3 and "untouched" in r.stderr and "dry-run" not in r.stdout


@needs_bash
def test_on_waits_then_proceeds_when_flipped_off(tmp_path):
    set_dnd(tmp_path, True)
    threading.Timer(1.5, set_dnd, (tmp_path, False)).start()
    t = time.time()
    r = run(tmp_path, "30", "--poll", "1")
    assert r.returncode == 0 and "waiting" in r.stdout and "proceeding" in r.stdout and time.time() - t >= 1.0


def test_dnd_api_roundtrip_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "APP_DATA", tmp_path / "ad")
    app = FastAPI(); app.include_router(router); c = TestClient(app)
    assert c.get("/api/dnd").json()["on"] is False                      # default OFF
    j = c.post("/api/dnd", json={"on": True}).json()
    assert j["on"] is True and j["by"] == "user" and j["since"]
    f = tmp_path / "ad" / "dnd.json"
    assert json.loads(f.read_text())["on"] is True and not list(f.parent.glob("*.tmp"))
    assert c.get("/api/dnd").json()["on"] is True
    assert c.post("/api/dnd", json={"on": False}).json()["on"] is False
    f.write_text("garbage"); assert c.get("/api/dnd").json()["on"] is False


@needs_bash
def test_script_sees_api_file(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "APP_DATA", tmp_path / "ad")
    app = FastAPI(); app.include_router(router); c = TestClient(app)
    c.post("/api/dnd", json={"on": True})
    assert run(tmp_path / "ad", "1", "--poll", "1").returncode == 3     # the script sees the API's file
    c.post("/api/dnd", json={"on": False})
    assert run(tmp_path / "ad").returncode == 0


def _fake_app(root, name="Fake"):
    """A throwaway 'app' (shell script that sleeps) under root/real, plus root/link -> root/real."""
    real = root / "real"; exe = real / f"{name}.app" / "Contents" / "MacOS" / name
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\nwhile :; do sleep 1; done\n"); exe.chmod(0o755)
    (root / "link").symlink_to(real)
    return real / f"{name}.app", root / "link" / f"{name}.app", exe


def _live(env_extra, root, app_arg, *extra):
    import os
    env = {**os.environ, "RELAUNCH_NO_OSASCRIPT": "1", "RELAUNCH_SETTLE": "0", **env_extra}
    return subprocess.run(["bash", SCRIPT, "--data-dir", str(root / "d"), "--app", str(app_arg), *extra],
                          capture_output=True, text=True, timeout=60, env=env)


@needs_bash
def test_symlinked_app_path_matches_resolved_process(tmp_path):
    """Process runs from the resolved path; --app is the symlinked one: dry-run sees it, a live run quits it then opens."""
    import os, signal
    real_app, link_app, exe = _fake_app(tmp_path)
    marker = tmp_path / "opened"
    opener = tmp_path / "opener.sh"; opener.write_text(f"#!/bin/sh\necho \"$1\" > {marker}\n"); opener.chmod(0o755)
    proc = subprocess.Popen([str(exe)])
    try:
        time.sleep(0.5)
        d = subprocess.run(
            ["bash", SCRIPT, "--dry-run", "--data-dir", str(tmp_path / "d"), "--app", str(link_app)],
            capture_output=True, text=True, timeout=30)
        assert str(proc.pid) in d.stdout and "real/Fake.app/Contents/MacOS/" in d.stdout
        r = _live({"RELAUNCH_OPEN": str(opener)}, tmp_path, link_app)
        assert r.returncode == 0, r.stderr
        assert proc.wait(timeout=5) is not None            # old process was terminated
        assert marker.read_text().strip() == str(link_app)  # then opened
    finally:
        if proc.poll() is None:
            proc.kill()


@needs_bash
def test_exit_4_when_old_instance_will_not_die(tmp_path):
    real_app, link_app, exe = _fake_app(tmp_path)
    exe.write_text("#!/bin/sh\ntrap '' TERM\nwhile :; do sleep 1; done\n")
    marker = tmp_path / "opened"
    opener = tmp_path / "opener.sh"; opener.write_text(f"#!/bin/sh\ntouch {marker}\n"); opener.chmod(0o755)
    proc = subprocess.Popen([str(exe)])
    try:
        time.sleep(0.5)
        r = _live({"RELAUNCH_OPEN": str(opener)}, tmp_path, link_app)
        assert r.returncode == 4 and "still running" in r.stderr
        assert not marker.exists()                           # never opened
    finally:
        proc.kill(); proc.wait()
