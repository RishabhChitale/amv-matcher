"""SCAN-PARTIAL: a running scan job streams scored songs (`partial`) while the final ranking stays deterministic."""
import threading
from pathlib import Path

from fastapi.testclient import TestClient


def _fake(server, monkeypatch, gate):
    scores = {"a": 40, "b": 90, "c": 65}

    def fake_song(p):
        return {"bpm": 120.0, "duration": 30.0}

    def fake_score(song, video, allow_negative=False):
        name = song["name"]
        return {"eligible": True, "total_pct": scores[name], "sync_pct": scores[name]}
    monkeypatch.setattr(server, "_song", lambda p: dict(fake_song(p), name=Path(p).stem))
    def slow_score(song, video, allow_negative=False):
        gate.wait(5)                 # the test releases songs one by one
        gate.clear()
        return fake_score(song, video)
    monkeypatch.setattr(server, "score", slow_score)
    monkeypatch.setattr(server, "best_tempo", lambda *a, **k: {})
    monkeypatch.setattr(server, "_cached_for", lambda *a, **k: None)


def test_partial_streams_then_final_is_ranked(monkeypatch):
    from amv_matcher import server
    gate = threading.Event()
    _fake(server, monkeypatch, gate)
    job = {"id": "t1", "status": "running", "results": None, "partial": [], "too_short": [], "skipped": [], "video_path": "v"}
    server.JOBS["t1"] = job
    video = {"duration": 10.0}
    songs = [Path("a.mp3"), Path("b.mp3"), Path("c.mp3")]
    th = threading.Thread(target=server._score_songs, args=(job, video, songs))
    th.start()
    c = TestClient(server.app)
    try:
        for n in (1, 2):
            gate.set()
            for _ in range(200):
                j = c.get("/api/scan/t1").json()
                if len(j["partial"]) == n:
                    break
                threading.Event().wait(0.02)
            assert j["status"] == "running" and j["results"] is None
            assert len(j["partial"]) == n and j["done"] == n   # progress: songs finished so far
        assert [r["name"] for r in j["partial"]] == ["a.mp3", "b.mp3"]    # scan order, unranked
        assert "rank" not in j["partial"][0]
        gate.set()
        th.join(5)
        j = c.get("/api/scan/t1").json()
    finally:
        gate.set(); th.join(5); server.JOBS.pop("t1", None)
    assert j["status"] == "done" and j["partial"] == []
    assert [r["name"] for r in j["results"]] == ["b.mp3", "c.mp3", "a.mp3"]
    assert [r["rank"] for r in j["results"]] == [1, 2, 3]
