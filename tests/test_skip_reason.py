"""Skipped-song reasons: an empty exception message never reaches the Skipped panel as 'Class: '."""
import time

import pytest

from fastapi.testclient import TestClient

from amv_matcher.audio import exc_reason
from tests.synth import make_click_track, make_video


@pytest.fixture(scope="module")
def video_file(tmp_path_factory):
    p = tmp_path_factory.mktemp("v") / "jump.avi"
    make_video(p)
    return p


class NoBackendError(Exception):
    pass


def test_exc_reason_empty_and_bare_class_name():
    assert exc_reason(NoBackendError()) == "unreadable audio file (NoBackendError)"
    assert exc_reason(NoBackendError("  ")) == "unreadable audio file (NoBackendError)"
    assert exc_reason(ValueError("NoBackendError: ")) == "unreadable audio file (NoBackendError)"
    assert exc_reason(ValueError("audioread.exceptions.NoBackendError")) == "unreadable audio file (NoBackendError)"


def test_exc_reason_keeps_real_message():
    assert exc_reason(ValueError("bad sample rate 0")) == "ValueError: bad sample rate 0"
    assert exc_reason(RuntimeError("worker crashed x2, in-process failed: boom")).startswith("RuntimeError: worker crashed")


def _scan(c, video_file, music):
    jid = c.post("/api/scan", json={"video_path": str(video_file), "music_dir": str(music)}).json()["job_id"]
    for _ in range(300):
        j = c.get(f"/api/scan/{jid}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.2)
    return j


def test_scan_skip_reasons_never_empty(tmp_path, video_file):
    from amv_matcher import server
    music = tmp_path / "music"
    music.mkdir()
    make_click_track(music / "click120.wav", 120)
    (music / "broken.mp3").write_bytes(b"not audio")
    (music / "garbage.wav").write_bytes(b"\x00\x01garbage")
    j = _scan(TestClient(server.app), video_file, music)
    assert j["status"] == "done", j
    sk = {s["name"]: s["reason"] for s in j["skipped"]}
    assert set(sk) == {"broken.mp3", "garbage.wav"}, sk
    for name, reason in sk.items():
        assert reason.strip() and not reason.rstrip().endswith(":"), (name, reason)
