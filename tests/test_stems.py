import os
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from amv_matcher import stems as S

SR = 44100
from tests import realmedia

UPLOADED_SONG = Path(realmedia.song_s1()) if realmedia.song_s1() else None


def _clicks(seconds, period=0.5, seed=1):
    rng = np.random.default_rng(seed)
    y = np.zeros(int(seconds * SR), dtype=np.float32)
    for t in np.arange(0.1, seconds - 0.1, period):
        i = int(t * SR)
        n = int(0.012 * SR)
        y[i:i + n] += (rng.standard_normal(n) * np.exp(-np.arange(n) / (0.003 * SR))).astype(np.float32) * 0.9
    return y


def _melody(seconds):
    t = np.arange(int(seconds * SR)) / SR
    f = np.where((t // 1.0) % 2 == 0, 440.0, 523.25)
    ph = 2 * np.pi * np.cumsum(f) / SR
    return (0.35 * np.sin(ph)).astype(np.float32)


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "CACHE_DIR", tmp_path / "stems")
    return tmp_path


def _mix(path, seconds=12.0):
    clicks, mel = _clicks(seconds), _melody(seconds)
    sf.write(str(path), np.stack([clicks + mel] * 2, 1), SR)
    return clicks, mel


def _energy(p):
    y, _ = sf.read(p)
    return float((y ** 2).sum())


def test_hpss_energy_ratio(cache):
    song = cache / "mix.wav"
    _mix(song)
    res = S.separate(str(song), "hpss")
    assert res["provider"] == "hpss" and set(res["stems"]) == {"drums", "music"}
    # clicks live in drums, the sine in music: compare each source's energy against both stems
    clicks_in_drums = _energy(res["stems"]["drums"])
    music = _energy(res["stems"]["music"])
    assert clicks_in_drums > 0 and music > 0
    d, _ = sf.read(res["stems"]["drums"])
    m, _ = sf.read(res["stems"]["music"])
    # project each stem onto the two known sources
    clicks, mel = _clicks(12.0), _melody(12.0)
    n = min(len(d), len(clicks))

    def share(stem, src):
        s = stem[:n].mean(1) if stem.ndim == 2 else stem[:n]
        return float(abs(np.dot(s, src[:n])) / (np.linalg.norm(src[:n]) ** 2 + 1e-9))
    assert share(d, clicks) > 0.6 and share(m, mel) > 0.6
    assert share(d, mel) < 0.2 and share(m, clicks) < 0.4
    # cached second call
    assert S.cached(str(song), "hpss")["stems"] == res["stems"]


def test_texture_labels_change_when_a_stem_drops(cache):
    seconds = 12.0
    clicks, mel = _clicks(seconds), _melody(seconds)
    clicks[int(6 * SR):] = 0.0          # drums stop at 6 s
    paths = {}
    for n, y in (("drums", clicks), ("music", mel)):
        p = cache / f"{n}.wav"
        sf.write(str(p), np.stack([y, y], 1), SR)
        paths[n] = str(p)
    f = S.stem_features(paths)
    first, second = f["textures"][2], f["textures"][9]
    assert "drums" in first["active"] and first["label"].startswith("drums+music")
    assert "drums" not in second["active"] and second["label"] == "music, no drums"
    labels = [s["label"] for s in f["texture_sections"]]
    assert len(labels) >= 2 and labels[0] != labels[-1]
    assert f["stems"]["drums"]["onsets"] and len(f["stems"]["music"]["energy_curve"]) in range(115, 125)


def test_quiet_detail_and_vocal_phrase_starts(cache):
    seconds = 20.0
    rng = np.random.default_rng(3)
    t = np.arange(int(seconds * SR)) / SR
    loud = np.zeros_like(t, dtype=np.float32)
    loud[int(10 * SR):] = (0.8 * rng.standard_normal(len(t) - int(10 * SR))).astype(np.float32)   # loud second half
    drums = loud.copy()
    for tt in (2.0, 4.0, 6.0):                        # soft hi-hat ticks in the quiet half
        i = int(tt * SR)
        drums[i:i + 400] += (0.05 * rng.standard_normal(400)).astype(np.float32)
    vocals = np.zeros_like(t, dtype=np.float32)
    for a, b in ((1.0, 2.0), (4.0, 5.0), (12.0, 14.0)):   # phrase starts at 1, 4, 12 (gaps >= 0.6 s)
        i, j = int(a * SR), int(b * SR)
        vocals[i:j] = (0.3 * np.sin(2 * np.pi * 300 * t[i:j])).astype(np.float32)
    paths = {}
    for n, y in (("drums", drums), ("vocals", vocals)):
        p = cache / f"{n}.wav"
        sf.write(str(p), np.stack([y, y], 1), SR)
        paths[n] = str(p)
    f = S.stem_features(paths)
    assert f["vocal_phrase_starts"][:3] == pytest.approx([1.0, 4.0, 12.0], abs=0.25)
    assert f["quiet_sections"] and all(q["end"] <= 10.5 for q in f["quiet_sections"])
    qd = [e for e in f["quiet_detail"] if e["stem"] == "drums"]
    assert qd and all(e["t"] < 10.5 for e in qd)
    assert any(abs(e["t"] - 6.0) < 0.15 for e in qd)


@pytest.fixture()
def api(cache, monkeypatch):
    from amv_matcher import server
    monkeypatch.setattr(server, "UPLOADS", cache / "uploads")
    c = TestClient(server.app)
    song = cache / "mix.wav"
    _mix(song, 8.0)
    r = c.post("/api/upload", params={"kind": "song", "name": "mix.wav"}, content=song.read_bytes())
    assert r.status_code == 200, r.text
    return c, r.json()["path"]


def _poll(c, sp, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get("/api/stems/status", params={"song_path": sp}).json()
        if s["state"] != "running":
            return s
        time.sleep(0.2)
    raise AssertionError("stems job timed out")


def test_api_job_flow(api):
    c, sp = api
    s0 = c.get("/api/stems/status", params={"song_path": sp}).json()
    assert s0["state"] == "none" and "demucs_installed" in s0["capabilities"] and s0["capabilities"]["free_gb"] > 0
    assert c.get("/api/stem-audio", params={"song_path": sp, "stem": "drums"}).status_code == 404
    j = c.post("/api/stems", json={"song_path": sp, "provider": "hpss"})
    assert j.status_code == 200, j.text
    assert j.json()["provider"] == "hpss" and j.json()["stem_names"] == ["drums", "music"]
    s = _poll(c, sp)
    assert s["state"] == "ready" and s["provider"] == "hpss" and s["stems"] == ["drums", "music"]
    a = c.get("/api/stem-audio", params={"song_path": sp, "stem": "drums"})
    assert a.status_code == 200 and a.headers["content-type"] == "audio/wav" and a.content[:4] == b"RIFF"
    assert c.get("/api/stem-audio", params={"song_path": sp, "stem": "bass"}).status_code == 404
    f = c.get("/api/stem-features", params={"song_path": sp}).json()
    assert set(f["stems"]) == {"drums", "music"} and f["textures"] and "quiet_detail" in f
    # second POST is served from the cache
    j2 = c.post("/api/stems", json={"song_path": sp, "provider": "hpss"}).json()
    assert j2["status"] == "done" and j2.get("cached") is True
    # auto falls back to hpss when demucs is not installed
    if not S.demucs_installed():
        assert c.post("/api/stems", json={"song_path": sp, "provider": "auto"}).json()["provider"] == "hpss"
        assert c.post("/api/stems", json={"song_path": sp, "provider": "demucs"}).status_code == 409


def test_api_guards(api):
    c, sp = api
    evil = {"Origin": "http://evil.example"}
    for path, body in (("/api/stems", {"song_path": sp, "provider": "hpss"}),
                       ("/api/stems/cancel", {"song_path": sp}), ("/api/stems/install", None)):
        r = c.post(path, json=body, headers=evil) if body else c.post(path, headers=evil)
        assert r.status_code == 403, path
    assert c.post("/api/stems", json={"song_path": "/etc/hosts", "provider": "hpss"}).status_code == 403
    assert c.post("/api/stems", json={"song_path": sp, "provider": "nope"}).status_code == 400
    ok = {"Origin": "http://127.0.0.1:8765"}
    assert c.post("/api/stems", json={"song_path": sp, "provider": "hpss"}, headers=ok).status_code == 200


def test_disk_space_refusal(api, monkeypatch):
    c, sp = api
    monkeypatch.setattr(S, "demucs_installed", lambda: True)
    monkeypatch.setattr(S, "free_bytes", lambda p=None: 1 * 1024 ** 3)
    r = c.post("/api/stems", json={"song_path": sp, "provider": "demucs"})
    assert r.status_code == 507 and "3 GB" in r.json()["detail"]
    assert c.post("/api/stems/install").status_code in (200, 507)


@pytest.mark.skipif(os.environ.get("AMV_DEMUCS") != "1" or UPLOADED_SONG is None, reason="set AMV_DEMUCS=1 (needs .venv-stems + a configured real song S1)")
def test_demucs_real_s1(cache, capsys):
    assert S.demucs_installed()
    dur = float(sf.info(str(UPLOADED_SONG)).duration) if UPLOADED_SONG.suffix != ".mp3" else None
    import librosa
    dur = librosa.get_duration(path=str(UPLOADED_SONG))
    t0 = time.time()
    res = S.separate(str(UPLOADED_SONG), "demucs")
    took = time.time() - t0
    assert set(res["stems"]) == {"drums", "bass", "vocals", "other"}
    for n, p in res["stems"].items():
        d = sf.info(p).duration
        assert abs(d - dur) <= 0.05, (n, d, dur)
    f = S.stem_features(res["stems"])
    print(f"DEMUCS seconds={took:.1f} song_seconds={dur:.1f} vocal_starts={len(f['vocal_phrase_starts'])} quiet_detail={len(f['quiet_detail'])}")
    (Path(os.environ.get("TMPDIR", "/tmp")) / "demucs_time.txt").write_text(f"{took:.1f} {dur:.1f}\n")
