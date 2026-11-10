"""U10: Do-not-disturb flag API + tools/relaunch_app.sh DND logic (always --dry-run / temp data dir; never touches the real app)."""
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from amv_matcher import project as P
from amv_matcher.dnd_api import router

SCRIPT = str(Path(__file__).resolve().parent.parent / "tools" / "relaunch_app.sh")


def run(d, *args, timeout=30):
    return subprocess.run(["bash", SCRIPT, "--dry-run", "--data-dir", str(d), "--app", str(d / "X.app"), *args],
                          capture_output=True, text=True, timeout=timeout)


def set_dnd(d, on):
    d.mkdir(parents=True, exist_ok=True)
    (d / "dnd.json").write_text(json.dumps({"on": on, "since": 1, "by": "user"}))


def test_off_or_missing_proceeds_at_once(tmp_path):
    r = run(tmp_path)                       # no dnd.json
    assert r.returncode == 0 and "proceeding" in r.stdout and "dry-run" in r.stdout
    set_dnd(tmp_path, False)
    assert run(tmp_path).returncode == 0


def test_on_times_out_with_exit_3(tmp_path):
    set_dnd(tmp_path, True)
    r = run(tmp_path, "2", "--poll", "1")
    assert r.returncode == 3 and "untouched" in r.stderr and "dry-run" not in r.stdout


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
    assert run(tmp_path / "ad", "1", "--poll", "1").returncode == 3     # the script sees the API's file
    assert c.post("/api/dnd", json={"on": False}).json()["on"] is False
    assert run(tmp_path / "ad").returncode == 0
    f.write_text("garbage"); assert c.get("/api/dnd").json()["on"] is False
