"""Worker pool tests: synthetic, fast. Kinds live in tests/_worker_kinds.py."""
import asyncio
import json
import multiprocessing as mp
import os
import time

import pytest

from amv_matcher.workers import WorkerPool
from tests import _worker_kinds  # noqa: F401  (registers kinds in the parent too)

MODS = ["tests._worker_kinds"]


@pytest.fixture
def mkpool():
    pools = []

    def make(workers=1, **kw):
        p = WorkerPool(workers=workers, kind_modules=MODS, **kw)
        pools.append(p)
        return p
    yield make
    for p in pools:
        p.shutdown()
    assert mp.active_children() == [], "orphan worker processes after shutdown"


def wait_for(pool, ids, timeout=30):
    ids = [ids] if isinstance(ids, str) else ids
    end = time.time() + timeout
    while time.time() < end:
        if all(pool.status(i)["state"] in ("done", "error", "cancelled") for i in ids):
            return
        time.sleep(0.01)
    raise AssertionError("timeout: %s" % [pool.status(i)["state"] for i in ids])


def test_ordering_single_worker(mkpool, tmp_path):
    p = mkpool(1)
    log = str(tmp_path / "log")
    b = p.submit("sleep", "", priority=0, secs=0.6, tag="blocker")
    time.sleep(0.3)  # blocker is running
    ids = [p.submit("rec", "", priority=pr, log=log, name=n)
           for n, pr in [("p2a", 2), ("p1a", 1), ("p0a", 0), ("p1b", 1), ("p2b", 2), ("p0b", 0)]]
    wait_for(p, [b] + ids)
    assert open(log).read().split() == ["p0a", "p0b", "p1a", "p1b", "p2a", "p2b"]


def test_cancel_queued_and_running(mkpool, tmp_path):
    p = mkpool(1)
    run = p.submit("sleep", "", secs=30, tag="long")
    time.sleep(0.4)
    queued = p.submit("sleep", "", secs=30, tag="queued")
    after = p.submit("echo", "", value=7)
    assert p.cancel(queued) and p.status(queued)["state"] == "cancelled"
    assert p.status(run)["state"] == "running"
    t = time.time()
    assert p.cancel(run)
    assert p.status(run)["state"] == "cancelled"
    wait_for(p, after)
    assert time.time() - t < 10
    assert p.status(after)["result"] == {"value": 7}
    assert not p.cancel(after)


def test_crash_isolation(mkpool):
    p = mkpool(2)
    bad = p.submit("crash", "", code=1)
    good = p.submit("echo", "", value=1)
    wait_for(p, [bad, good])
    s = p.status(bad)
    assert s["state"] == "error" and "worker crashed" in s["error"] and s["attempts"] == 2
    assert p.status(good)["state"] == "done"
    later = p.submit("echo", "", value=2)
    wait_for(p, later)
    assert p.status(later)["result"] == {"value": 2}
    f = p.submit("fail", "")
    wait_for(p, f)
    assert "boom" in p.status(f)["error"]


def test_timeout(mkpool):
    p = mkpool(1, timeout=0.5)
    j = p.submit("sleep", "", secs=30)
    wait_for(p, j)
    assert p.status(j)["state"] == "error" and "timeout" in p.status(j)["error"]
    k = p.submit("echo", "", value=3)
    wait_for(p, k)
    assert p.status(k)["state"] == "done"


def test_dedupe(mkpool, tmp_path):
    p = mkpool(1)
    f = tmp_path / "in.bin"
    f.write_bytes(b"a" * 10)
    marker = str(tmp_path / "m")
    a = p.submit("echo", str(f), marker=marker)
    b = p.submit("echo", str(f), marker=marker)
    assert a == b
    wait_for(p, a)
    c = p.submit("echo", str(f), marker=marker)  # done + unchanged -> no recompute
    assert c == a and p.status(c)["state"] == "done"
    d = p.submit("echo", str(f), marker=marker, value=1)  # different params
    assert d != a
    wait_for(p, d)
    st = os.stat(f)
    f.write_bytes(b"b" * 11)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    e = p.submit("echo", str(f), marker=marker)
    assert e not in (a, d)
    wait_for(p, e)
    assert len(open(marker).read().split()) == 3


def test_dedupe_running_bumps_priority(mkpool):
    p = mkpool(1)
    blocker = p.submit("sleep", "", secs=0.5, tag="b")
    time.sleep(0.3)
    a = p.submit("echo", "", priority=2, value=1)
    assert p.submit("echo", "", priority=0, value=1) == a
    assert p.status(a)["priority"] == 0
    wait_for(p, [blocker, a])


def test_bump_reorders(mkpool, tmp_path):
    p = mkpool(1)
    log = str(tmp_path / "log")
    b = p.submit("sleep", "", secs=0.5, tag="b")
    time.sleep(0.3)
    x = p.submit("rec", "", priority=1, log=log, name="x")
    y = p.submit("rec", "", priority=1, log=log, name="y")
    z = p.submit("rec", "", priority=2, log=log, name="z")
    assert p.bump(z, 0)
    wait_for(p, [b, x, y, z])
    assert open(log).read().split() == ["z", "x", "y"]


def test_pause_prewarm_and_battery(mkpool, tmp_path):
    p = mkpool(1)
    p.pause_prewarm()
    low = p.submit("echo", "", priority=2, value="low")
    hi = p.submit("echo", "", priority=1, value="hi")
    wait_for(p, hi)
    time.sleep(0.3)
    assert p.status(low)["state"] == "queued"
    assert p.stats()["queued"][2] == 1 and p.stats()["prewarm_paused"]
    p.resume_prewarm()
    wait_for(p, low)
    # battery detector holds class 2 until power returns
    state = {"batt": True}
    q = mkpool(1, battery_detector=lambda: state["batt"], battery_poll=0.1)
    l2 = q.submit("echo", "", priority=2, value="l2")
    time.sleep(0.5)
    assert q.status(l2)["state"] == "queued"
    state["batt"] = False
    wait_for(q, l2)


def test_progress_events(mkpool):
    p = mkpool(1)
    seen = []
    unsub = p.subscribe(seen.append)
    j = p.submit("progress", "", steps=5, delay=0.1)
    wait_for(p, j)
    unsub()
    evs = [e for e in p.events(0) if e["job_id"] == j]
    seqs = [e["seq"] for e in p.events(0)]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    states = [e["state"] for e in evs]
    assert states[0] == "queued" and states[1] == "running" and states[-1] == "done"
    assert states.index("done") == len(states) - 1
    prog = [e["progress"] for e in evs]
    assert prog == sorted(prog) and prog[-1] == 100 and len(set(prog)) >= 4
    assert [e["seq"] for e in seen] == [e["seq"] for e in evs]
    assert p.events(evs[-1]["seq"]) == []


def test_responsiveness_under_cpu_load(mkpool):
    p = mkpool(2)
    ids = [p.submit("burn", "", secs=3.0, tag=i) for i in range(2)]

    async def window():
        worst, last, end = 0.0, time.perf_counter(), time.perf_counter() + 0.7
        while time.perf_counter() < end:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            worst = max(worst, now - last - 0.01)
            last = now
        return worst

    async def probe():  # best of 3 windows: ambient machine load can spike one
        return min([await window() for _ in range(3)])
    time.sleep(0.5)
    assert all(p.status(i)["state"] == "running" for i in ids)
    worst = asyncio.run(probe())
    wait_for(p, ids)
    assert worst < 0.2, worst


def test_real_kinds_smoke(mkpool, tmp_path):
    pytest.importorskip("librosa")
    cv2 = pytest.importorskip("cv2")  # noqa: F841
    from amv_matcher import audio, video
    from tests import synth

    song = tmp_path / "click.wav"
    synth.make_click_track(song, 120, seconds=8.0, offset=0.1)
    vid = tmp_path / "v.avi"
    synth.make_video(vid, seconds=3.0, fps=15, jump_every=0.5, size=(160, 120))
    sc, vc = tmp_path / "songs.json", tmp_path / "videos"

    def norm(o):
        return json.loads(json.dumps(o, default=lambda x: x.tolist() if hasattr(x, "tolist") else str(x)))

    p = mkpool(2)
    s = p.submit("song_analysis", str(song), cache_path=str(sc))
    v = p.submit("video_analysis", str(vid), cache_dir=str(vc))
    t = p.submit("thumbnails", str(vid), n=3, width=64, cache_dir=str(tmp_path / "th"))
    pr = p.submit("probe", str(vid))
    wait_for(p, [s, v, t, pr], timeout=120)
    for i in (s, v, t, pr):
        assert p.status(i)["state"] == "done", p.status(i)
    assert norm(p.status(s)["result"]) == norm(audio.analyze_song(str(song), cache_path=tmp_path / "direct.json"))
    assert norm(p.status(v)["result"]) == norm(video.analyze_video(str(vid)))
    assert sc.exists() and list(vc.glob("*.json"))
    assert all(os.path.exists(f) for f in p.status(t)["result"]["files"])
    # a fresh submit is answered from the cache without a worker
    s2 = p.submit("song_analysis", str(song), cache_path=str(sc), force=False)
    assert s2 == s
    q = mkpool(1)
    v2 = q.submit("video_analysis", str(vid), cache_dir=str(vc))
    assert q.status(v2)["state"] == "done" and q.status(v2)["cached"]
    assert q.stats()["busy"] == 0
