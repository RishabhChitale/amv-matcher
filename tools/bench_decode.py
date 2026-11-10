#!/usr/bin/env python3
"""Decode benchmark: software single-thread vs threaded vs hardware, widths 160/960/full.

Usage: .venv/bin/python tools/bench_decode.py [--synthetic] [--real] [--repeats 2] [--secs 30]
Synthetic = 1080p60 H.264 clip in a temp dir (deleted after; refuses < 3 GB free).
Real = case id "A" from <app data>/real_cases.json; numeric only, never prints its name/path.
Prints load average with every table. Never views frames.
"""
from __future__ import annotations

import argparse, json, os, shutil, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import av
import numpy as np
from amv_matcher import decode, paths


def make_synth(path, secs=30, fps=60, w=1920, h=1080):
    rng = np.random.default_rng(1)
    noise = rng.integers(0, 40, (h, w, 3), dtype=np.uint8)
    xx = np.arange(w, dtype=np.uint16)[None, :, None]
    yy = np.arange(h, dtype=np.uint16)[:, None, None]
    with av.open(str(path), "w") as c:
        s = c.add_stream("libx264", rate=fps)
        s.width, s.height, s.pix_fmt = w, h, "yuv420p"
        s.options = {"preset": "ultrafast", "crf": "23", "g": "60"}
        s.thread_type = "AUTO"
        for i in range(secs * fps):
            img = ((xx + i * 5) % 256 + (yy * 0)).astype(np.uint8)
            img = np.broadcast_to(img, (h, w, 3)).copy()
            img[..., 1] = ((yy + i * 3) % 256).astype(np.uint8)[:, :, 0]
            img = np.clip(img.astype(np.uint16) + np.roll(noise, i * 7, axis=0), 0, 255).astype(np.uint8)
            for p in s.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                c.mux(p)
        for p in s.encode():
            c.mux(p)


def run_one(path, mode, width, max_frames=None):
    t0 = time.perf_counter()
    n = 0
    if mode == "sw1":
        c = av.open(str(path)); vs = c.streams.video[0]; vs.thread_type = "NONE"; vs.thread_count = 1
        fmt = "rgb24"
        for f in c.decode(vs):
            if width:
                f = f.reformat(width=width, height=int(round(f.height * width / f.width)) // 1, format=fmt,
                               interpolation="AREA")
            else:
                f = f.reformat(format=fmt)
            f.to_ndarray(); n += 1
            if max_frames and n >= max_frames: break
        c.close()
    else:
        with decode.open_frames(path, width=width, hw="off" if mode == "swN" else "videotoolbox") as fs:
            for _ in fs:
                n += 1
                if max_frames and n >= max_frames: break
            used = fs.info.path
    return n, time.perf_counter() - t0


def bench(path, label, repeats, widths, fps_src, max_frames):
    rows, base = [], {}
    for width in widths:
        for mode in ("sw1", "swN", "hw"):
            best = None
            for _ in range(repeats):
                n, dt = run_one(path, mode, width, max_frames)
                r = n / dt
                best = r if best is None or r > best else best
            base[(width, mode)] = best
            rows.append((label, {"sw1": "software 1 thread", "swN": "software threaded", "hw": "videotoolbox"}[mode],
                         width or "full", best))
    print(f"\n{label}   load average: {os.getloadavg()}")
    print("path | width | frames/s | x realtime | speedup vs threaded sw")
    for lab, name, width, r in rows:
        w = 0 if width == "full" else width
        mode = {"software 1 thread": "sw1", "software threaded": "swN", "videotoolbox": "hw"}[name]
        print(f"{name} | {width} | {r:.0f} | {r / fps_src:.1f}x | {r / base[(w, 'swN')]:.2f}x")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true"); ap.add_argument("--real", action="store_true")
    ap.add_argument("--repeats", type=int, default=2); ap.add_argument("--secs", type=int, default=30)
    ap.add_argument("--max-frames", type=int, default=0)
    a = ap.parse_args()
    if not (a.synthetic or a.real):
        a.synthetic = True
    widths = [160, 960, 0]
    if a.synthetic:
        d = Path(tempfile.mkdtemp(prefix="decbench_"))
        try:
            if shutil.disk_usage(d).free < 3 * 2**30:
                sys.exit("refusing: < 3 GB free")
            p = d / "s.mp4"; make_synth(p, a.secs)
            bench(p, "synthetic 1080p60 H.264 %ds" % a.secs, a.repeats, widths, 60, a.max_frames or None)
        finally:
            shutil.rmtree(d, ignore_errors=True)
    if a.real:
        cases = json.load(open(paths.DATA / "real_cases.json"))["cases"]
        case = next((c for c in cases if c["id"] == "A"), None)
        if not case:
            sys.exit("no case A")
        p = case["video"]
        pr = decode.probe_video(p)
        print(f"video A: {pr['width']}x{pr['height']} {pr['fps']:.2f}fps {pr['duration']:.1f}s {pr['codec']} {pr['pix_fmt']} hw_capable={pr['hw_capable']}")
        bench(p, "video A", a.repeats, widths, pr["fps"] or 30, a.max_frames or None)


if __name__ == "__main__":
    main()
