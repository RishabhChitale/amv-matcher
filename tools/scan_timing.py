#!/usr/bin/env python3
"""Scan timing: how long does one song-folder scan take, serial vs. the default (parallel) scoring?

Stdlib only. Generates deterministic synthetic media (N click-track WAVs of about three minutes, one 60 s video with a
square that jumps every half second), launches the app on its own throwaway AMV_DATA_DIR/TMPDIR, drives it over HTTP
(POST /api/scan, poll /api/scan/<id>) and prints a table plus machine-readable lines. Nothing personal is read or written.

    python tools/scan_timing.py                         # source mode: serves amv_matcher.server from this checkout
    python tools/scan_timing.py --exe "dist/AMV Matcher/AMV Matcher.exe"   # the frozen app (Windows CI, macOS bundle binary)
    options: -n 10 (songs)  --seconds 180 (song length)  --work DIR (default: a temp dir)  --keep  --settle 5

Two configurations, each as a cold scan (fresh data dir, fresh process) and a warm scan (the app restarted on the same
data dir, so only the disk caches are warm):
    par=0     AMV_SCAN_PAR=0   serial scoring (the old behaviour)
    default   (env unset)      the app's default scoring
Note: builds without the F13 parallel scoring ignore AMV_SCAN_PAR, so the two lines then match.

Output lines (stable, grep-able):
    scan_timing par=0 <cold wall seconds>
    scan_timing default <cold wall seconds>
    scan_timing_detail <cfg> cold_wall=.. cold_first=.. cold_cpu=.. warm_wall=.. warm_first=.. warm_cpu=.. own_rss_mb=.. tree_rss_mb=.. n=.. mode=..
    scan_timing WARN ...                    (only when the peak whole-tree RSS of a scan tops 2500 MB)
    scan_timing identical=yes|no            (same ranked rows from both configurations)
    scan_timing_json {...}                  (everything, one line)
Exit code: 0 ok, 1 a scan failed or timed out, 3 the two configurations ranked differently.
"""
import argparse
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SR = 22050
VIDEO_S = 60
FPS = 30
VW, VH = 80, 60          # tiny uncompressed frames keep the file small (about 26 MB) and need no codec


# ------------------------------------------------------------------ synthetic media (stdlib)
def make_song(path, bpm, seconds, offset, f0, drop=False):
    """Kick-like click on every beat, a soft hat on every half beat; with `drop` the first 40% is thinner."""
    n = int(SR * seconds)
    buf = bytearray(2 * n)

    def stamp(t, freq, amp, decay, length):
        i = int(t * SR)
        m = min(int(length * SR), n - i)
        if i < 0 or m <= 0:
            return
        chunk = struct.pack("<%dh" % m, *(int(32767 * amp * math.sin(2 * math.pi * freq * (k / SR)) * math.exp(-(k / SR) * decay))
                                          for k in range(m)))
        buf[2 * i:2 * i + 2 * m] = chunk

    period = 60.0 / bpm
    k = 0
    while offset + k * period < seconds - 0.1:
        t = offset + k * period
        if not drop or t > seconds * 0.4 or k % 2 == 0:
            stamp(t, f0, 0.9, 150.0, 0.03)
        stamp(t + period / 2, 5000.0, 0.25, 400.0, 0.012)
        k += 1
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(buf))


def make_video(path, seconds=VIDEO_S, jump_every=0.5):
    """Uncompressed (BI_RGB) AVI: a white square jumping between four spots every `jump_every` seconds."""
    spots = [(8, 8), (56, 36), (48, 8), (12, 34)]
    frames = []
    for x, y in spots:
        rows = []
        for r in range(VH - 1, -1, -1):          # bottom-up
            row = bytearray(VW * 3)
            if y <= r < y + 16:
                row[x * 3:(x + 16) * 3] = b"\xff" * 48
            rows.append(bytes(row))
        frames.append(b"".join(rows))
    fsize = VW * VH * 3
    total = int(seconds * FPS)
    movi = bytearray(b"movi")
    index = bytearray()
    for f in range(total):
        seg = int((f / FPS) / jump_every + 1e-6)
        off = len(movi)
        movi += b"00db" + struct.pack("<I", fsize) + frames[seg % 4]
        index += b"00db" + struct.pack("<III", 0x10, off, fsize)
    avih = struct.pack("<IIIIIIIIII4I", 1000000 // FPS, fsize * FPS, 0, 0x10, total, 0, 1, fsize, VW, VH, 0, 0, 0, 0)
    strh = b"vids" + b"DIB " + struct.pack("<IHHIIIIIIIIhhhh", 0, 0, 0, 0, 1, FPS, 0, total, fsize, 0xFFFFFFFF, 0, 0, 0, VW, VH)
    strf = struct.pack("<IiiHHIIiiII", 40, VW, VH, 1, 24, 0, fsize, 0, 0, 0, 0)

    def ck(tag, data):
        return tag + struct.pack("<I", len(data)) + data + (b"\0" if len(data) % 2 else b"")

    def lst(kind, data):
        return b"LIST" + struct.pack("<I", 4 + len(data)) + kind + data

    hdrl = lst(b"hdrl", ck(b"avih", avih) + lst(b"strl", ck(b"strh", strh) + ck(b"strf", strf)))
    body = b"AVI " + hdrl + b"LIST" + struct.pack("<I", len(movi)) + bytes(movi) + ck(b"idx1", bytes(index))
    Path(path).write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def make_video_hd(path, seconds=240, width=1920, height=1080, fps=60, cut_every=4.0):
    """Realistic-cost synthetic clip: 1080p60 H.264 (needs PyAV, which the app itself decodes with). A panning noise
    texture, moving blocks and a hard cut every `cut_every` s, so decode, cut detection and dense flow all have real work."""
    import numpy as np
    import av
    rng = np.random.default_rng(7)
    tex = []
    for _ in range(3):                                   # three "shots": smooth-ish colour noise, wider than the frame to pan over
        small = rng.integers(0, 256, (height // 8, (width * 2) // 8, 3), dtype=np.uint8)
        tex.append(np.repeat(np.repeat(small, 8, axis=0), 8, axis=1)[:height, :width * 2].copy())
    total = int(seconds * fps)
    out = av.open(str(path), "w")
    st = out.add_stream("libx264", rate=fps)             # what real clips are; mpeg4 here segfaults the app's threaded NONREF decode on macOS
    st.width, st.height, st.pix_fmt, st.bit_rate = width, height, "yuv420p", 12_000_000
    st.options = {"preset": "ultrafast", "g": "120"}
    st.codec_context.thread_type = "AUTO"
    frame = np.empty((height, width, 3), dtype=np.uint8)
    per_cut = max(1, int(cut_every * fps))
    for f in range(total):
        shot, k = divmod(f, per_cut)
        x0 = (k * 5) % width
        frame[:] = tex[shot % 3][:, x0:x0 + width]
        for j in range(4):                               # blocks sliding at different speeds / directions
            bx = int((k * (7 + 3 * j) + j * 400) % (width - 160))
            by = int(120 + 220 * j + 60 * math.sin(k / 15.0 + j))
            frame[by:by + 120, bx:bx + 160] = (255 - 40 * j, 40 * j, 200)
        for pkt in st.encode(av.VideoFrame.from_ndarray(frame, format="rgb24")):
            out.mux(pkt)
    for pkt in st.encode():
        out.mux(pkt)
    out.close()


def build_fixtures(work, n, seconds, hd_seconds=0):
    fx = work / "fixtures"
    songs = fx / "music"
    songs.mkdir(parents=True, exist_ok=True)
    video = fx / ("video_hd.mp4" if hd_seconds else "video.avi")
    if not video.exists():
        if hd_seconds:
            make_video_hd(video, hd_seconds)
        else:
            make_video(video)
    want = {"song_%02d_%ds.wav" % (i, int(seconds)) for i in range(1, n + 1)}
    for stale in songs.glob("song_*.wav"):               # a reused --work dir may hold songs from an earlier -n / --seconds
        if stale.name not in want:
            stale.unlink()
    for i in range(1, n + 1):
        p = songs / ("song_%02d_%ds.wav" % (i, int(seconds)))
        if not p.exists():
            bpm = 80 + (i * 37) % 91                    # 80..170, deterministic
            make_song(p, bpm, seconds, offset=((i * 7) % 10) / 10.0, f0=700.0 + 60 * (i % 7), drop=bool(i % 2))
    return video, songs


# ------------------------------------------------------------------ process tree + CPU (psutil-free)
def _cpu_seconds(s):
    s = s.strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    v = 0.0
    for part in s.split(":"):
        v = v * 60 + float(part)
    return days * 86400 + v


def proc_table():
    """{pid: (ppid, cpu_seconds, rss_bytes)} for every process."""
    out = {}
    if sys.platform == "win32":
        ps = ("Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId) "
              "$([int64]$_.UserModeTime + [int64]$_.KernelModeTime) $([int64]$_.WorkingSetSize)\" }")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60)
        for line in r.stdout.splitlines():
            p = line.split()
            if len(p) == 4:
                out[int(p[0])] = (int(p[1]), int(p[2]) / 1e7, int(p[3]))
    else:
        r = subprocess.run(["ps", "-axo", "pid=,ppid=,time=,rss="], capture_output=True, text=True, timeout=30)
        for line in r.stdout.splitlines():
            p = line.split()
            if len(p) == 4:
                try:
                    out[int(p[0])] = (int(p[1]), _cpu_seconds(p[2]), int(p[3]) * 1024)
                except ValueError:
                    pass
    return out


def tree(root, table=None):
    table = table or proc_table()
    s = {root}
    grew = True
    while grew:
        grew = False
        for pid, (ppid, _c, _r) in table.items():
            if ppid in s and pid not in s:
                s.add(pid)
                grew = True
    return s


def tree_cpu(root):
    try:
        t = proc_table()
        return sum(t[p][1] for p in tree(root, t) if p in t)
    except Exception:
        return float("nan")


def sample_rss(root):
    """(own RSS MB, whole-tree RSS MB) right now; (0, 0) when the process table is unavailable."""
    try:
        t = proc_table()
        own = t[root][2] if root in t else 0
        return own / 1048576.0, sum(t[p][2] for p in tree(root, t) if p in t) / 1048576.0
    except Exception:
        return 0.0, 0.0


def kill_tree(proc):
    try:
        pids = tree(proc.pid)
    except Exception:
        pids = {proc.pid}
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        import signal
        for pid in sorted(pids, reverse=True):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        proc.wait(20)
    except Exception:
        pass


# ------------------------------------------------------------------ the app
LAUNCHER = '''import sys
sys.path.insert(0, %(repo)r)
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    import uvicorn
    from amv_matcher import server as srv
    srv.allow_port(%(port)d)
    uvicorn.run(srv.app, host="127.0.0.1", port=%(port)d, log_level="warning", log_config=None)
'''


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(port, path, body=None, timeout=60):
    url = "http://127.0.0.1:%d%s" % (port, path)
    if body is None:
        return json.load(urllib.request.urlopen(url, timeout=timeout))
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json",
                                                                 "Origin": "http://127.0.0.1:%d" % port})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def launch(exe, work, tag, par, data, tmp, startup_timeout=180):
    env = dict(os.environ, AMV_DATA_DIR=str(data), TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp))
    env.pop("AMV_SCAN_PAR", None)
    if par is not None:
        env["AMV_SCAN_PAR"] = str(par)
    log = open(work / ("stdout-%s.txt" % tag), "w")
    port = None
    if exe:
        cmd, cwd = [str(exe)], None
        try:
            os.remove(data / "app.log")
        except OSError:
            pass
    else:
        port = free_port()
        script = work / "launch_server.py"
        script.write_text(LAUNCHER % {"repo": str(REPO), "port": port})
        cmd, cwd = [sys.executable, str(script)], str(REPO)
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
    while time.perf_counter() - t0 < startup_timeout:
        if proc.poll() is not None:
            raise RuntimeError("app exited early (code %s); see %s" % (proc.returncode, work / ("stdout-%s.txt" % tag)))
        if exe:
            try:
                m = re.findall(r"server on http://127\.0\.0\.1:(\d+)", (data / "app.log").read_text(errors="replace"))
                port = int(m[-1]) if m else None
            except OSError:
                port = None
        if port:
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=2).read(1)
                return proc, port, time.perf_counter() - t0
            except Exception:
                pass
        time.sleep(0.25)
    kill_tree(proc)
    raise RuntimeError("app did not come up within %d s" % startup_timeout)


def scan(proc, port, video, music, settle, timeout=1800):
    time.sleep(settle)
    c0 = tree_cpu(proc.pid)
    t0 = time.perf_counter()
    jid = http(port, "/api/scan", {"video_path": str(video), "music_dir": str(music)})["job_id"]
    first = video_s = None
    peak_own = peak_tree = 0.0
    last_rss = 0.0
    every = 2.0 if sys.platform == "win32" else 0.5          # a PowerShell process listing is slow
    while True:
        j = http(port, "/api/scan/" + jid)
        t = time.perf_counter() - t0
        if t - last_rss >= every:
            last_rss = t
            o, tr = sample_rss(proc.pid)
            peak_own, peak_tree = max(peak_own, o), max(peak_tree, tr)
        if first is None and j.get("partial"):
            first = t
        if video_s is None and j.get("stage") not in (None, "video"):
            video_s = t                                  # video analysis done once the job moves on to the songs
        if j["status"] != "running":
            break
        if t > timeout:
            return {"status": "timeout", "wall_s": t}
        time.sleep(0.05)
    wall = time.perf_counter() - t0
    res = j.get("results") or []
    return {"status": j["status"], "message": j.get("message") or j.get("error"), "wall_s": round(wall, 2),
            "first_s": None if first is None else round(first, 2),
            "video_s": None if video_s is None else round(video_s, 2), "cpu_s": round(tree_cpu(proc.pid) - c0, 1),
            "peak_own_rss_mb": round(peak_own), "peak_tree_rss_mb": round(peak_tree), "ranked": len(res), "skipped": len(j.get("skipped") or []),
            "det": [[Path(r["path"]).name, r.get("bpm"), r.get("total_pct"), r.get("tempo_pct"), r.get("sync_pct")] for r in res]}


def run_config(exe, work, cfg, par, video, music, settle):
    data = work / ("data-" + cfg)
    tmp = work / ("tmp-" + cfg)
    for d in (data, tmp):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    out = {}
    for phase in ("cold", "warm"):                       # warm = restart the app on the same data dir
        proc, port, startup = launch(exe, work, "%s-%s" % (cfg, phase), par, data, tmp)
        try:
            r = scan(proc, port, video, music, settle)
        finally:
            kill_tree(proc)
        r["startup_s"] = round(startup, 2)
        out[phase] = r
        print("  %-7s %-4s wall %6.2f s  video stage %s s  first row %s s  cpu %s s  rss peak own/tree %s/%s MB  ranked %s skipped %s  [%s]" % (
            cfg, phase, r["wall_s"], r.get("video_s"), r.get("first_s"), r.get("cpu_s"), r.get("peak_own_rss_mb"), r.get("peak_tree_rss_mb"), r.get("ranked"), r.get("skipped"), r["status"]), flush=True)
        if r["status"] != "done":
            break
        time.sleep(2)
    return out


def substage_bench(video):
    """Source mode only (imports the app's modules): decode alone, decode + dense flow, and the whole analyze_video().
    flow ~= (decode+flow) - decode; the remainder of the full pass is diffs/thumbs/cuts/tempo/camera/subject fields."""
    sys.path.insert(0, str(REPO))
    import cv2
    from amv_matcher import decode, flow, video as vid
    t = time.perf_counter()
    fps, frames = vid._frames(str(video))
    n = sum(1 for _ in frames)
    t_dec = time.perf_counter() - t
    t = time.perf_counter()
    fps, frames = vid._frames(str(video))
    fp = flow.FlowPass(fps)
    for g in frames:
        fp.feed(g)
    t_flow = time.perf_counter() - t
    t = time.perf_counter()
    vid.analyze_video(str(video))
    t_all = time.perf_counter() - t
    print("scan_timing_substages decode=%.1f s (%d frames @%.0f fps analysed) decode+flow=%.1f s -> flow~%.1f s | analyze_video total=%.1f s -> cuts/camera/subject/other~%.1f s | decode backend: %s" % (
        t_dec, n, fps, t_flow, max(0.0, t_flow - t_dec), t_all, max(0.0, t_all - t_flow), "PyAV software"), flush=True)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--substages-child":
        substage_bench(sys.argv[2])
        return 0
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-n", "--songs", type=int, default=10)
    ap.add_argument("--seconds", type=float, default=180.0, help="length of each synthetic song")
    ap.add_argument("--exe", help="frozen app executable; default: source mode")
    ap.add_argument("--work", help="working dir (default: a fresh temp dir)")
    ap.add_argument("--keep", action="store_true", help="keep the working dir")
    ap.add_argument("--hd-video", type=float, default=0.0, metavar="SECONDS",
                    help="use a realistic 1920x1080 60 fps H.264 clip of this many seconds (needs PyAV) instead of the tiny AVI")
    ap.add_argument("--substages", action="store_true", help="also time decode / flow / full video analysis in-process (needs the source tree)")
    ap.add_argument("--only", choices=("par=0", "default"), help="run just this config (halves the time; skips the identical-results check)")
    ap.add_argument("--settle", type=float, default=5.0, help="seconds to let a fresh app idle before scanning")
    a = ap.parse_args()

    own = a.work is None
    work = Path(a.work or tempfile.mkdtemp(prefix="amv-scan-timing-")).resolve()
    work.mkdir(parents=True, exist_ok=True)
    mode = "frozen" if a.exe else "source"
    print("scan_timing: mode=%s songs=%d x %d s, work dir %s" % (mode, a.songs, int(a.seconds), work), flush=True)
    t0 = time.perf_counter()
    video, music = build_fixtures(work, a.songs, a.seconds, a.hd_video)
    print("  fixtures ready in %.1f s" % (time.perf_counter() - t0), flush=True)

    res, fail = {}, False
    try:
        if a.substages:
            r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--substages-child", str(video)])   # own process: a native crash must not take the timing run down
            if r.returncode:
                print("scan_timing_substages unavailable: child exit code %s" % r.returncode, flush=True)
        for cfg, par in [c for c in (("par=0", 0), ("default", None)) if a.only in (None, c[0])]:
            res[cfg] = run_config(a.exe, work, cfg, par, video, music, a.settle)
            fail |= any(r["status"] != "done" for r in res[cfg].values()) or len(res[cfg]) < 2
    finally:
        if own and not a.keep:
            shutil.rmtree(work, ignore_errors=True)

    def g(cfg, phase, key):
        v = res.get(cfg, {}).get(phase, {}).get(key)
        return "n/a" if v is None else v

    def peak(cfg, key):
        vals = [v for v in (res.get(cfg, {}).get(p, {}).get(key) for p in ("cold", "warm")) if v is not None]
        return max(vals) if vals else "n/a"

    print()
    print("%-9s | %-38s | %-38s" % ("", "cold (fresh data)", "warm (app restarted)"))
    print("%-9s | %8s %8s %8s %5s %5s | %8s %8s %8s %5s %5s" % ("config", "wall s", "1st row", "cpu s", "own", "tree", "wall s", "1st row", "cpu s", "own", "tree"))
    for cfg in res:
        print("%-9s | %8s %8s %8s %5s %5s | %8s %8s %8s %5s %5s" % (
            cfg, g(cfg, "cold", "wall_s"), g(cfg, "cold", "first_s"), g(cfg, "cold", "cpu_s"), g(cfg, "cold", "peak_own_rss_mb"), g(cfg, "cold", "peak_tree_rss_mb"),
            g(cfg, "warm", "wall_s"), g(cfg, "warm", "first_s"), g(cfg, "warm", "cpu_s"), g(cfg, "warm", "peak_own_rss_mb"), g(cfg, "warm", "peak_tree_rss_mb")))
    print("(own/tree = peak resident MB of the app process / the app plus its workers during the scan)")
    print()
    for cfg in res:
        print("scan_timing %s %s" % (cfg, g(cfg, "cold", "wall_s")))
    for cfg in res:
        print("scan_timing_detail %s cold_wall=%s cold_video=%s warm_video=%s cold_first=%s cold_cpu=%s warm_wall=%s warm_first=%s warm_cpu=%s own_rss_mb=%s tree_rss_mb=%s n=%d mode=%s" % (
            cfg, g(cfg, "cold", "wall_s"), g(cfg, "cold", "video_s"), g(cfg, "warm", "video_s"), g(cfg, "cold", "first_s"), g(cfg, "cold", "cpu_s"),
            g(cfg, "warm", "wall_s"), g(cfg, "warm", "first_s"), g(cfg, "warm", "cpu_s"),
            peak(cfg, "peak_own_rss_mb"), peak(cfg, "peak_tree_rss_mb"), a.songs, mode))
        tr = peak(cfg, "peak_tree_rss_mb")
        if isinstance(tr, (int, float)) and tr > 2500:
            print("scan_timing WARN %s tree RSS %s MB is over 2500 MB: cap AMV_SCAN_PAR lower on this platform" % (cfg, tr))
    dets = [res[c].get("cold", {}).get("det") for c in res]
    same = len(dets) == 2 and dets[0] is not None and dets[0] == dets[1]
    if a.only:
        same = bool(dets and dets[0] is not None)
    print("scan_timing identical=%s" % ("yes" if same else ("n/a" if a.only else "no")))
    payload = {"mode": mode, "songs": a.songs, "song_seconds": a.seconds, "video_seconds": a.hd_video or 60,
               "results": {c: {p: {k: v for k, v in r.items() if k != "det"} for p, r in ph.items()} for c, ph in res.items()},
               "identical": same}
    print("scan_timing_json " + json.dumps(payload, sort_keys=True))
    if fail:
        return 1
    return 0 if same else 3


if __name__ == "__main__":
    sys.exit(main())
