#!/usr/bin/env python3
"""Auto-sync timing: how long does one auto-sync take in the (frozen) app, per stage and per step?

Stdlib only. Reuses scan_timing's synthetic media (click-track WAVs + a 60 s video with a square that jumps every half
second), launches the app on its own throwaway AMV_DATA_DIR/TMPDIR, scans the music folder once (that also registers the
songs and analyses the video), then drives POST /api/auto-sync and polls GET /api/auto-sync/<id>. Nothing personal is
read or written. Run it against two builds with the same media and compare the plan hashes (identical = a pure speed-up).

    python tools/sync_timing.py --exe "dist/AMV Matcher/AMV Matcher.exe" -n 2 --seconds 180 --rounds 2
    python tools/sync_timing.py                         # source mode: serves amv_matcher.server from this checkout

Per round: fresh data dir and fresh process; per song one auto-sync (run=1, cold in this process) and one repeat
(run=2, same process: shows what the in-process memo saves).

Output lines (stable, grep-able):
    sync_timing_scan   ref=<ref> round=.. startup_s=.. scan_wall_s=.. det_hash=..
    sync_timing_step   ref=<ref> round=.. song=.. run=.. step=<tool> status=end start_s=.. dur_s=.. score_before=.. score_after=.. kept=..
    sync_timing        ref=<ref> round=.. song=.. run=.. total_s=.. analyze_s=.. autosync_s=.. engine_s=.. steps=.. final_total=.. plan_hash=.. deterministic=..
    sync_timing_sum    ref=<ref> runs=.. median_total_s=.. median_analyze_s=.. median_autosync_s=.. plan_hash=<hash over all plans> det_hash=..
    sync_timing_batch  ref=<ref> round=.. mode=serial|parallel songs=.. wall_s=.. peak_own_rss_mb=.. peak_tree_rss_mb=.. plan_hashes=a,b,.. ok=..
    sync_timing_batch_sum ref=<ref> serial_wall_s=.. parallel_wall_s=.. speedup=.. peak_tree_rss_mb_serial=.. peak_tree_rss_mb_parallel=.. plans_equal=..
Exit code: 0 ok, 1 a scan/auto-sync failed or timed out.

--batch: F16 check for running several songs' auto-sync at once (what "Auto-sync all" would do with 2 in flight). Per round,
two fresh processes with the same fixtures: one runs the songs one after another, one submits them all at the same moment.
Both sample the process tree RSS; the per-song plan hashes of the two modes must be equal.
"""
import argparse
import hashlib
import json
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_timing as st  # noqa: E402


def _strip(x):
    """Canonical copy without wall-clock fields, so two runs of the same plan hash the same."""
    if isinstance(x, dict):
        return {k: _strip(v) for k, v in sorted(x.items()) if "elapsed" not in k and k not in ("t_wall", "wall_s")}
    if isinstance(x, list):
        return [_strip(v) for v in x]
    return x


def _hash(x):
    return hashlib.sha1(json.dumps(_strip(x), sort_keys=True, default=str).encode()).hexdigest()[:12]


def autosync(port, video, song, budget_s, timeout=900):
    t0 = time.perf_counter()
    r = st.http(port, "/api/auto-sync", {"video_path": str(video), "song_path": str(song), "budget_s": budget_s})
    jid = r["job_id"]
    t_auto = None
    seen = 0
    ends = []                                          # (poll time, step event)
    while True:
        j = st.http(port, "/api/auto-sync/" + jid)
        t = time.perf_counter() - t0
        if t_auto is None and j.get("stage") not in (None, "starting", "analyze"):
            t_auto = t                                 # analysis done once the job moves on to the auto-sync stage
        steps = j.get("steps_so_far") or []
        for ev in steps[seen:]:
            ends.append((t, ev))
        seen = len(steps)
        if j["status"] != "running":
            break
        if t > timeout:
            return {"status": "timeout", "total_s": t}
        time.sleep(0.05)
    total = time.perf_counter() - t0
    res = j.get("result") or {}
    return {"status": j["status"], "error": j.get("error"), "total_s": total, "analyze_s": t_auto,
            "events": ends, "result": res}


def _plan_hash(res):
    fin = res.get("final") or {}
    return _hash({"final": fin, "plan": res.get("plan"), "path": res.get("path"), "steps": [
        [s.get("step"), s.get("kept"), s.get("score_before"), s.get("score_after")] for s in (res.get("steps") or []) if isinstance(s, dict)]})


def _scan(port, video, songs):
    jid = st.http(port, "/api/scan", {"video_path": str(video), "music_dir": str(songs)})["job_id"]
    t0 = time.perf_counter()
    while True:
        j = st.http(port, "/api/scan/" + jid)
        if j["status"] != "running":
            break
        if time.perf_counter() - t0 > 900:
            raise RuntimeError("scan timed out")
        time.sleep(0.1)
    if j["status"] != "done":
        raise RuntimeError("scan %s: %s" % (j["status"], j.get("message") or j.get("error")))


def batch_round(exe, work, ref, rnd, mode, video, songs, song_files, budget):
    """One fresh process; auto-sync every song either one after another or all at once."""
    import shutil
    tag = "%s%d" % (mode[:3], rnd)
    data, tmp = work / ("bdata-%s" % tag), work / ("btmp-%s" % tag)
    for d in (data, tmp):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    proc, port, _startup = st.launch(exe, work, "batch" + tag, None, data, tmp)
    peak = {"own": 0.0, "tree": 0.0}
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            o, t = st.sample_rss(proc.pid)
            peak["own"], peak["tree"] = max(peak["own"], o), max(peak["tree"], t)
            stop.wait(0.5)

    out = {"mode": mode, "ok": True, "hashes": [], "wall": None, "own": 0, "tree": 0}
    th = threading.Thread(target=sampler, daemon=True)
    try:
        _scan(port, video, songs)
        th.start()
        results = [None] * len(song_files)
        t0 = time.perf_counter()
        if mode == "serial":
            for i, sp in enumerate(song_files):
                results[i] = autosync(port, video, sp, budget)
        else:
            def one(i, sp):
                results[i] = autosync(port, video, sp, budget)
            ts = [threading.Thread(target=one, args=(i, sp)) for i, sp in enumerate(song_files)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        out["wall"] = time.perf_counter() - t0
        for r in results:
            if not r or r["status"] != "done":
                out["ok"] = False
                out["hashes"].append("FAIL:%s" % (r and (r.get("error") or r["status"])))
            else:
                out["hashes"].append(_plan_hash(r["result"]))
    except Exception as e:
        out["ok"] = False
        out["hashes"].append("FAIL:%s: %s" % (type(e).__name__, e))
    finally:
        stop.set()
        o, t = st.sample_rss(proc.pid)
        peak["own"], peak["tree"] = max(peak["own"], o), max(peak["tree"], t)
        out["own"], out["tree"] = peak["own"], peak["tree"]
        st.kill_tree(proc)
    return out


def batch_main(a):
    work = Path(a.work or tempfile.mkdtemp(prefix="amv-sync-batch-")).resolve()
    work.mkdir(parents=True, exist_ok=True)
    video, songs = st.build_fixtures(work, a.n, a.seconds)
    song_files = sorted(songs.glob("song_*.wav"))
    exe = Path(a.exe).resolve() if a.exe else None
    bad = 0
    walls = {"serial": [], "parallel": []}
    trees = {"serial": [], "parallel": []}
    hashes = {"serial": [], "parallel": []}
    for rnd in range(1, a.rounds + 1):
        for mode in (("serial", "parallel") if rnd % 2 else ("parallel", "serial")):     # alternate order
            r = batch_round(exe, work, a.ref, rnd, mode, video, songs, song_files, a.budget)
            print("sync_timing_batch ref=%s round=%d mode=%s songs=%d wall_s=%s peak_own_rss_mb=%.0f peak_tree_rss_mb=%.0f plan_hashes=%s ok=%s"
                  % (a.ref, rnd, mode, len(song_files), None if r["wall"] is None else "%.2f" % r["wall"], r["own"], r["tree"],
                     ",".join(r["hashes"]), r["ok"]), flush=True)
            if not r["ok"]:
                bad += 1
                continue
            walls[mode].append(r["wall"]); trees[mode].append(r["tree"]); hashes[mode].append(tuple(r["hashes"]))
    if walls["serial"] and walls["parallel"]:
        ms, mp = statistics.median(walls["serial"]), statistics.median(walls["parallel"])
        equal = len(set(hashes["serial"]) | set(hashes["parallel"])) == 1
        print("sync_timing_batch_sum ref=%s serial_wall_s=%.2f parallel_wall_s=%.2f speedup=%.2f peak_tree_rss_mb_serial=%.0f peak_tree_rss_mb_parallel=%.0f plans_equal=%s"
              % (a.ref, ms, mp, ms / mp if mp else float("nan"), max(trees["serial"]), max(trees["parallel"]), equal), flush=True)
        if not equal:
            bad += 1
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe")
    ap.add_argument("--work")
    ap.add_argument("-n", type=int, default=2, help="songs to auto-sync (each also scanned)")
    ap.add_argument("--seconds", type=int, default=180)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--budget", type=float, default=60.0)
    ap.add_argument("--ref", default="local")
    ap.add_argument("--batch", action="store_true", help="F16: auto-sync all songs serially vs all at once (wall, RSS, plan hashes)")
    a = ap.parse_args()
    if a.batch:
        return batch_main(a)
    work = Path(a.work or tempfile.mkdtemp(prefix="amv-sync-timing-")).resolve()
    work.mkdir(parents=True, exist_ok=True)
    video, songs = st.build_fixtures(work, a.n, a.seconds)
    song_files = sorted(songs.glob("song_*.wav"))
    exe = Path(a.exe).resolve() if a.exe else None
    ref = a.ref
    bad = 0
    totals, analyses, autos, plans, dets = [], [], [], [], []
    for rnd in range(1, a.rounds + 1):
        data, tmp = work / ("data-%d" % rnd), work / ("tmp-%d" % rnd)
        for d in (data, tmp):
            if d.exists():
                import shutil
                shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True)
        proc, port, startup = st.launch(exe, work, "sync%d" % rnd, None, data, tmp)
        try:
            t0 = time.perf_counter()
            jid = st.http(port, "/api/scan", {"video_path": str(video), "music_dir": str(songs)})["job_id"]
            while True:
                j = st.http(port, "/api/scan/" + jid)
                if j["status"] != "running":
                    break
                if time.perf_counter() - t0 > 900:
                    raise RuntimeError("scan timed out")
                time.sleep(0.1)
            scan_wall = time.perf_counter() - t0
            if j["status"] != "done":
                raise RuntimeError("scan %s: %s" % (j["status"], j.get("message") or j.get("error")))
            det = [[Path(r["path"]).name, r.get("bpm"), r.get("total_pct"), r.get("tempo_pct"), r.get("sync_pct")]
                   for r in (j.get("results") or [])]
            dh = _hash(det)
            dets.append(dh)
            print("sync_timing_scan ref=%s round=%d startup_s=%.2f scan_wall_s=%.2f det_hash=%s" % (ref, rnd, startup, scan_wall, dh), flush=True)
            for sp in song_files:
                for run in (1, 2):
                    r = autosync(port, video, sp, a.budget)
                    if r["status"] != "done":
                        bad += 1
                        print("sync_timing FAIL ref=%s round=%d song=%s run=%d status=%s error=%s" % (ref, rnd, sp.name, run, r["status"], r.get("error")), flush=True)
                        continue
                    res = r["result"]
                    fin = res.get("final") or {}
                    ph = _hash({"final": fin, "plan": res.get("plan"), "path": res.get("path"), "steps": [
                        [s.get("step"), s.get("kept"), s.get("score_before"), s.get("score_after")] for s in (res.get("steps") or []) if isinstance(s, dict)]})
                    starts = {}
                    for t, ev in r["events"]:
                        if not isinstance(ev, dict):
                            continue
                        if ev.get("status") == "start":
                            starts[ev.get("step")] = ev.get("elapsed_s")
                        elif ev.get("status") == "end":
                            s0 = starts.get(ev.get("step"))
                            e1 = ev.get("elapsed_s")
                            dur = None if s0 is None or e1 is None else e1 - s0
                            print("sync_timing_step ref=%s round=%d song=%s run=%d step=%s status=end start_s=%s dur_s=%s score_before=%s score_after=%s kept=%s"
                                  % (ref, rnd, sp.name, run, ev.get("step"), s0, None if dur is None else round(dur, 3), ev.get("score_before"), ev.get("score_after"), ev.get("kept")), flush=True)
                    ta = r["analyze_s"] or 0.0
                    print("sync_timing ref=%s round=%d song=%s run=%d total_s=%.2f analyze_s=%.2f autosync_s=%.2f engine_s=%s steps=%d final_total=%s plan_hash=%s deterministic=%s"
                          % (ref, rnd, sp.name, run, r["total_s"], ta, r["total_s"] - ta, res.get("elapsed_s"), len(res.get("steps") or []),
                             fin.get("total_pct", fin.get("total")), ph, res.get("deterministic")), flush=True)
                    totals.append(r["total_s"]); analyses.append(ta); autos.append(r["total_s"] - ta); plans.append(ph)
        except Exception as e:
            bad += 1
            print("sync_timing FAIL ref=%s round=%d %s: %s" % (ref, rnd, type(e).__name__, e), flush=True)
        finally:
            st.kill_tree(proc)
    if totals:
        print("sync_timing_sum ref=%s runs=%d median_total_s=%.2f median_analyze_s=%.2f median_autosync_s=%.2f plan_hash=%s det_hash=%s"
              % (ref, len(totals), statistics.median(totals), statistics.median(analyses), statistics.median(autos),
                 _hash(plans), _hash(dets)), flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
