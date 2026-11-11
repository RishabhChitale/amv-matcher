"""F13 parallel scoring: a scan with song_score jobs in flight in the worker pool gives byte-identical results to the
serial loop, in scan order, with bounded concurrency, cancel-on-exit and the crash fallback intact."""
import json
from pathlib import Path

import pytest

from tests.test_amv import make_click_track
from tests.synth import make_video

BPMS = [100, 110, 120, 128, 140, 90]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AMV_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AMV_WORKERS", "3")
    from amv_matcher import server
    server.shutdown_workers()
    songs = []
    for i, b in enumerate(BPMS):
        p = tmp_path / f"s{i}.wav"
        make_click_track(p, b, seconds=20.0, offset=0.1 * i)
        songs.append(p)
    vp = tmp_path / "v.avi"
    make_video(vp, seconds=8.0)
    video = server.analyze_video(str(vp))
    monkeypatch.setattr(server, "_cached_for", lambda *a, **k: None)
    yield server, songs, video
    server.shutdown_workers()


def _job():
    return {"id": "pj", "status": "running", "results": None, "partial": [], "too_short": [], "skipped": [],
            "video_path": "v"}


def _scan(server, video, songs, par, monkeypatch):
    monkeypatch.setenv("AMV_SCAN_PAR", str(par))
    job = _job()
    server._score_songs(job, video, list(songs))
    return job


def _canon(job):
    return json.dumps({k: job[k] for k in ("results", "too_short", "skipped")}, sort_keys=True, default=str)


def test_parallel_equals_serial(env, monkeypatch):
    server, songs, video = env
    serial = _scan(server, video, songs, 0, monkeypatch)
    for par in (2, 3, 6):
        got = _scan(server, video, songs, par, monkeypatch)
        assert _canon(got) == _canon(serial)
    assert len(serial["results"]) + len(serial["too_short"]) + len(serial["skipped"]) == len(songs)


def test_partial_streams_in_scan_order(env, monkeypatch):
    server, songs, video = env
    seen = []
    orig = server._publish_partial

    def spy(job, row):
        seen.append(row["name"])
        orig(job, row)
    monkeypatch.setattr(server, "_publish_partial", spy)
    _scan(server, video, songs, 3, monkeypatch)
    assert seen == [p.name for p in songs]


def test_concurrency_bounded_and_outstanding_cancelled(monkeypatch):
    from amv_matcher import server

    class P:
        def __init__(self):
            self.live, self.peak, self.cancelled, self.n = set(), 0, [], 0
        def stats(self):
            return {"workers": 8}
        def submit(self, kind, path, prio=1, **params):
            assert kind == "song_score" and prio == 1
            self.n += 1
            jid = f"j{self.n}:{Path(path).stem}"
            self.live.add(jid)
            self.peak = max(self.peak, len(self.live))
            return jid
        def status(self, jid):
            stem = jid.split(":")[1]
            if stem == "s4":                       # the scan dies here: stands in for the user cancelling
                raise RuntimeError("stop the scan")
            self.live.discard(jid)
            return {"state": "done", "result": {"bpm": 120.0, "duration": 30.0,
                                                "sc": {"eligible": True, "total_pct": 50, "sync_pct": 50.0}}}
        def cancel(self, jid):
            self.cancelled.append(jid)
            self.live.discard(jid)

    p = P()
    monkeypatch.setattr(server, "_wp", lambda: p)
    monkeypatch.setattr(server, "_cached_for", lambda *a, **k: None)
    monkeypatch.setenv("AMV_SCAN_PAR", "3")
    job = _job()
    with pytest.raises(RuntimeError):
        server._score_loop_parallel(job, {"duration": 10.0}, [Path(f"s{i}.wav") for i in range(8)], [], [], 3)
    assert p.peak <= 3 + 1                         # window (3) plus the one being consumed
    assert p.cancelled, "outstanding jobs must be cancelled when the loop exits"


def test_window_is_zero_without_a_usable_pool(monkeypatch):
    from amv_matcher import server
    monkeypatch.setattr(server, "_wp", lambda: None)
    assert server._score_par(10) == 0
    class Fake:                                    # the fake pool of test_scan_prefetch: no stats()
        pass
    monkeypatch.setattr(server, "_wp", lambda: Fake())
    assert server._score_par(10) == 0
    class W:
        def __init__(self, n): self.n = n
        def stats(self): return {"workers": self.n}
    monkeypatch.setattr(server, "_wp", lambda: W(1))
    assert server._score_par(10) == 0
    monkeypatch.setattr(server, "_wp", lambda: W(12))
    monkeypatch.delenv("AMV_SCAN_PAR", raising=False)
    assert server._score_par(10) == 4              # default min(workers, 4)
    assert server._score_par(3) == 3               # never more than the songs
    monkeypatch.setenv("AMV_SCAN_PAR", "0")
    assert server._score_par(10) == 0
    monkeypatch.setenv("AMV_SCAN_PAR", "6")
    assert server._score_par(10) == 6


def test_crash_fallback_still_ranks_with_same_scores(env, monkeypatch):
    server, songs, video = env
    serial = _scan(server, video, songs[:3], 0, monkeypatch)
    monkeypatch.setenv("AMV_TEST_CRASH_WORKER", "1")           # every song worker dies: serial fallback per song
    server.shutdown_workers()
    got = _scan(server, video, songs[:3], 3, monkeypatch)
    assert _canon(got) == _canon(serial)


def test_finished_song_score_jobs_release_the_video_but_still_dedup(env, monkeypatch):
    server, songs, video = env
    serial = _scan(server, video, songs[:3], 0, monkeypatch)
    first = _scan(server, video, songs[:3], 3, monkeypatch)
    wp = server._wp()
    jobs = [j for j in wp._jobs.values() if j["kind"] == "song_score"]
    assert len(jobs) == 3 and all(j["state"] == "done" for j in jobs)
    assert all("video" not in j["params"] for j in jobs), "the video must not stay pinned in finished jobs"
    n_before = wp._nid
    again = _scan(server, video, songs[:3], 3, monkeypatch)            # an instant rescan: same keys, no new jobs
    assert wp._nid == n_before, "rescan must hit the dedup (done jobs), not resubmit"
    assert _canon(again) == _canon(first) == _canon(serial)
