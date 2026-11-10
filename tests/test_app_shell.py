"""App-shell pieces: data paths, preview proxy, native register-paths, frozen selftest entry."""
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.synth import make_click_track, make_video

ROOT = Path(__file__).resolve().parent.parent


def test_paths_data_dir_env_override(tmp_path):
    r = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, %r); from amv_matcher import paths; print(paths.DATA); print(paths.FROZEN)" % str(ROOT)],
        env={"AMV_DATA_DIR": str(tmp_path / "d"), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = r.stdout.split()
    assert Path(out[0]).resolve() == (tmp_path / "d").resolve() and out[1] == "False"


def test_proxy_for_non_web_codec(tmp_path, monkeypatch):
    from amv_matcher import paths, proxy
    monkeypatch.setattr(paths, "CACHE", tmp_path / "cache")
    v = tmp_path / "mjpg.avi"
    make_video(v, seconds=2)
    assert proxy.needs_proxy(v)
    out = proxy.build(v)
    assert out.is_file() and out.suffix == ".mp4"
    assert proxy.video_codec(out) == "h264"
    assert proxy.build(v) == out      # cached


def test_register_paths_in_place_no_upload_copy(tmp_path, monkeypatch):
    from amv_matcher import server
    monkeypatch.setattr(server, "UPLOADS", tmp_path / "uploads")
    c = TestClient(server.app)
    v = tmp_path / "clip.avi"
    make_video(v, seconds=2)
    folder = tmp_path / "songs"
    (folder / ".hidden").mkdir(parents=True)
    make_click_track(folder / "a.wav", 120, seconds=5)
    make_click_track(folder / ".hidden" / "skip.wav", 120, seconds=5)
    (folder / "notes.txt").write_text("x")
    r = c.post("/api/register-paths", json={"paths": [str(v), str(folder), str(tmp_path / "missing.mp4")]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["videos"] == [str(v.resolve())]
    assert j["songs"] == [str((folder / "a.wav").resolve())]
    assert not (tmp_path / "uploads").exists()
    assert c.get("/api/audio", params={"path": j["songs"][0]}).status_code == 200


def test_register_paths_origin_guard(tmp_path):
    from amv_matcher import server
    c = TestClient(server.app)
    r = c.post("/api/register-paths", json={"paths": []}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_app_main_selftest():
    r = subprocess.run([sys.executable, "app_main.py", "--selftest"], cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and "selftest ok" in r.stdout, r.stdout + r.stderr
