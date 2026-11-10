import time

import pytest
from fastapi.testclient import TestClient

from amv_matcher.audio import analyze_song
from amv_matcher.score import score
from amv_matcher.video import analyze_video
from amv_matcher.pump import pump_keyframes
from tests.synth import make_burst_video, make_click_track, make_quiet_loud, make_video


@pytest.fixture(scope="module")
def video_file(tmp_path_factory):
    p = tmp_path_factory.mktemp("v") / "jump.avi"
    make_video(p)
    return p


def test_click_track_120_bpm(tmp_path):
    p = tmp_path / "c120.wav"
    make_click_track(p, 120)
    r = analyze_song(str(p), cache_path=tmp_path / "c.json")
    assert 118 <= r["bpm"] <= 122, r["bpm"]


def test_cache_hit(tmp_path):
    p = tmp_path / "c.wav"
    make_click_track(p, 120, seconds=10)
    cp = tmp_path / "c.json"
    analyze_song(str(p), cache_path=cp)
    t = time.time()
    analyze_song(str(p), cache_path=cp)
    assert time.time() - t < 0.2


def test_video_motion_tempo(video_file):
    v = analyze_video(str(video_file))
    bpm = v["motion_tempo_bpm"]
    assert any(abs(bpm - t) <= 4 for t in (60, 120, 240)), bpm
    assert len(v["cut_times"]) >= 10


def test_120_beats_97(tmp_path, video_file):
    v = analyze_video(str(video_file))
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    make_click_track(a, 120)
    make_click_track(b, 97)
    sa = score(analyze_song(str(a), tmp_path / "x.json"), v)
    sb = score(analyze_song(str(b), tmp_path / "x.json"), v)
    assert sa["total_pct"] > sb["total_pct"], (sa, sb)


def test_api_scan_and_audio_guard(tmp_path, video_file):
    from amv_matcher import server
    music = tmp_path / "music"
    music.mkdir()
    for bpm in (90, 120, 150):
        make_click_track(music / f"click{bpm}.wav", bpm)
    (music / "broken.mp3").write_bytes(b"not audio")
    c = TestClient(server.app)
    jid = c.post("/api/scan", json={"video_path": str(video_file), "music_dir": str(music)}).json()["job_id"]
    for _ in range(300):
        j = c.get(f"/api/scan/{jid}").json()
        if j["status"] != "running":
            break
        time.sleep(0.2)
    assert j["status"] == "done", j
    assert j["results"][0]["name"] == "click120.wav"
    assert j["too_short"] == []
    assert [s["name"] for s in j["skipped"]] == ["broken.mp3"]
    assert c.get("/api/audio", params={"path": str(music / "click90.wav")}).status_code == 200
    assert c.get("/api/audio", params={"path": str(music / ".." / "music" / ".." / "x.wav")}).status_code == 403
    assert c.get("/api/audio", params={"path": "/etc/passwd"}).status_code == 403


def test_song_shorter_than_video_is_ineligible(tmp_path, video_file):
    v = analyze_video(str(video_file))          # 10 s video
    p = tmp_path / "short.wav"
    make_click_track(p, 120, seconds=6)
    r = score(analyze_song(str(p), tmp_path / "x.json"), v)
    assert r["eligible"] is False and r["total_pct"] == 0
    assert r["song_duration"] < r["video_duration"]
    ok = tmp_path / "long.wav"
    make_click_track(ok, 120, seconds=30)
    r2 = score(analyze_song(str(ok), tmp_path / "x.json"), v)
    assert r2["eligible"] and r2["trim_end_s"] - r2["trim_start_s"] == pytest.approx(10.0, abs=0.01)


def test_arc_prefers_matching_energy_shape(tmp_path):
    vp = tmp_path / "burst.avi"
    make_burst_video(vp)
    v = analyze_video(str(vp))
    a, b = tmp_path / "quiet_loud.wav", tmp_path / "loud_quiet.wav"
    make_quiet_loud(a)
    make_quiet_loud(b, loud_first=True)
    cp = tmp_path / "x.json"
    ra = score(analyze_song(str(a), cp), v)
    rb = score(analyze_song(str(b), cp), v)
    assert ra["arc_pct"] > rb["arc_pct"], (ra, rb)


def _song_stub():
    return {"duration": 10.0, "beat_times": [1.0, 2.0, 3.0, 4.0, 5.0],
            "beat_strengths": [0.8] * 5, "drops": [3.0]}


def test_pump_boom_zero_is_flat():
    kf = pump_keyframes(_song_stub(), boom=0, decay_ms=180, drops_only=False, fps=30)
    assert all(k["scale"] == 1.0 for k in kf)


def test_pump_drop_stronger_than_beat():
    kf = pump_keyframes(_song_stub(), boom=40, decay_ms=180, drops_only=False, fps=30)
    at = lambda t: next(k for k in kf if abs(k["t"] - t) < 1e-3)
    assert at(3.0)["scale"] > at(2.0)["scale"] > 1.0
    assert at(3.0)["brightness"] > 1.0 and at(2.0)["brightness"] == 1.0


def test_pump_drops_only_has_no_other_pulses():
    kf = pump_keyframes(_song_stub(), boom=40, decay_ms=180, drops_only=True, fps=30)
    for k in kf:
        if k["t"] < 3.0 or k["t"] > 3.0 + 6 * 0.18:
            assert k["scale"] == 1.0, k
    assert max(k["scale"] for k in kf) > 1.0
