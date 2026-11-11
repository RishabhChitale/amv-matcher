"""Scan prefetch: every input song yields exactly one result or an explicit error entry, with prefetch on or off,
even when prefetch submits or song analyses fail; ranking is identical with prefetch=0."""
import json
from pathlib import Path

import pytest

NAMES = [f"s{i:02d}" for i in range(12)]
BAD = {"s03", "s07"}                                  # analyses that fail (fault injection)


class _Pool:
    """Stand-in worker pool: submit() fails for some songs (prefetch must swallow it, the later _song() still runs)."""
    def __init__(self, fail_submit):
        self.fail_submit, self.calls = fail_submit, []

    def submit(self, kind, path, prio):
        self.calls.append(Path(path).stem)
        if Path(path).stem in self.fail_submit:
            raise RuntimeError("submit boom")


def _run(server, monkeypatch, prefetch, fail_submit=()):
    def fake_song(p):
        stem = Path(p).stem
        if stem in BAD:
            raise RuntimeError(f"analysis failed {stem}")
        return {"bpm": 120.0, "duration": 30.0, "name": stem}
    monkeypatch.setattr(server, "SCAN_PREFETCH", prefetch)
    monkeypatch.setattr(server, "_wp", lambda: _Pool(set(fail_submit)))
    monkeypatch.setattr(server, "_song", fake_song)
    monkeypatch.setattr(server, "score", lambda song, video, allow_negative=False: {
        "eligible": True, "total_pct": (int(song["name"][1:]) * 37) % 101, "sync_pct": 50.0})
    monkeypatch.setattr(server, "best_tempo", lambda *a, **k: {})
    monkeypatch.setattr(server, "_cached_for", lambda *a, **k: None)
    job = {"id": "pf", "status": "running", "results": None, "partial": [], "too_short": [], "skipped": [], "video_path": "v"}
    songs = [Path(f"{n}.mp3") for n in NAMES]
    server._score_songs(job, {"duration": 10.0}, songs)
    return job, songs


@pytest.mark.parametrize("prefetch,fail_submit", [(0, ()), (8, ()), (8, ("s01", "s05", "s09")), (3, NAMES)])
def test_every_song_accounted_for_exactly_once(monkeypatch, prefetch, fail_submit):
    from amv_matcher import server
    job, songs = _run(server, monkeypatch, prefetch, fail_submit)
    seen = [r["name"] for r in job["results"] + job["too_short"]] + [s["name"] for s in job["skipped"]]
    assert sorted(seen) == sorted(p.name for p in songs)          # none dropped, none duplicated
    assert {s["name"] for s in job["skipped"]} == {f"{b}.mp3" for b in BAD}
    assert all(s["reason"] for s in job["skipped"])               # an explicit error entry, not silence


def test_ranking_identical_with_and_without_prefetch(monkeypatch):
    from amv_matcher import server
    base, _ = _run(server, monkeypatch, 0)
    for _ in range(10):
        pf, _ = _run(server, monkeypatch, 8, ("s02",))
        assert json.dumps(pf["results"], sort_keys=True) == json.dumps(base["results"], sort_keys=True)
        assert pf["skipped"] == base["skipped"]
