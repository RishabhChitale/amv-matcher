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
Exit code: 0 ok, 1 a scan/auto-sync failed or timed out.
"""
import argparse
import hashlib
import json
import statistics
import sys
import tempfile
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe")
    ap.add_argument("--work")
    ap.add_argument("-n", type=int, default=2, help="songs to auto-sync (each also scanned)")
    ap.add_argument("--seconds", type=int, default=180)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--budget", type=float, default=60.0)
    ap.add_argument("--ref", default="local")
    a = ap.parse_args()
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
