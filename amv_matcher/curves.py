"""Shared helpers for 10 Hz energy curves, lulls, drops and peak segments."""
from __future__ import annotations

import numpy as np
from scipy.ndimage import uniform_filter1d

HZ = 10.0
LULL_THR = 0.25          # normalized energy below this counts as calm / quiet
LULL_MIN_S = 1.5         # a lull must last at least this long
DROP_BEFORE_S = 2.0      # a drop follows a lower-energy stretch of >= 2 s
DROP_AFTER_S = 0.5
DROP_JUMP = 0.30         # minimum energy jump
DROP_SEP_S = 2.0
PEAK_PERCENTILE = 90     # video peaks = top-decile bursts
PEAK_MIN_LEVEL = 0.5


def normalize01(x):
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return x
    lo, hi = float(x.min()), float(x.max())
    if hi - lo < 1e-9:
        return np.zeros_like(x)
    return (x - lo) / (hi - lo)


def bin_mean(x, src_hz, n_out, dst_hz=HZ):
    """Average samples (at src_hz) into n_out bins at dst_hz; gaps are forward-filled."""
    x = np.asarray(x, dtype=float)
    idx = (np.arange(len(x)) / src_hz * dst_hz).astype(int)
    ok = idx < n_out
    sums = np.bincount(idx[ok], weights=x[ok], minlength=n_out)
    cnt = np.bincount(idx[ok], minlength=n_out)
    out = np.where(cnt > 0, sums / np.maximum(cnt, 1), np.nan)
    for i in range(n_out):
        if np.isnan(out[i]):
            out[i] = out[i - 1] if i > 0 else 0.0
    return out


def smooth(x, n=3):
    return uniform_filter1d(np.asarray(x, dtype=float), size=n, mode="nearest")


def _runs(mask):
    runs, start = [], None
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        elif not m and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(mask)))
    return runs


def find_lulls(e, hz=HZ, thr=LULL_THR, min_len_s=LULL_MIN_S):
    out = []
    for a, b in _runs(np.asarray(e) < thr):
        if (b - a) / hz >= min_len_s - 1e-9:
            out.append([round(a / hz, 2), round(b / hz, 2)])
    return out


def find_drops(e, hz=HZ):
    """Big energy jump right after a lower-energy stretch of >= 2 s."""
    e = np.asarray(e, dtype=float)
    L, A = int(DROP_BEFORE_S * hz), max(1, int(DROP_AFTER_S * hz))
    cands = []
    for i in range(L, len(e) - A + 1):
        jump = float(e[i:i + A].mean() - e[i - L:i].mean())
        if jump >= DROP_JUMP:
            cands.append((jump, i))
    chosen = []
    for jump, i in sorted(cands, reverse=True):
        if all(abs(i - j) / hz >= DROP_SEP_S for j in chosen):
            chosen.append(i)
    return [round(i / hz, 2) for i in sorted(chosen)]


def find_peak_segments(e, hz=HZ):
    e = np.asarray(e, dtype=float)
    if e.size == 0:
        return []
    thr = max(float(np.percentile(e, PEAK_PERCENTILE)), PEAK_MIN_LEVEL)
    segs = [[a / hz, b / hz] for a, b in _runs(e >= thr)]
    merged = []
    for s in segs:
        if merged and s[0] - merged[-1][1] < 0.5:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    return [[round(a, 2), round(b, 2)] for a, b in merged]


def label_segments(e, hz=HZ):
    """calm (lulls), peak (top-decile bursts), build (the rise into a peak)."""
    lulls = find_lulls(e, hz)
    peaks = find_peak_segments(e, hz)
    segs = [{"start": a, "end": b, "label": "calm"} for a, b in lulls]
    segs += [{"start": a, "end": b, "label": "peak"} for a, b in peaks]
    for a, b in peaks:
        prev_end = max([le for ls, le in lulls if le <= a] + [0.0])
        start = max(prev_end, a - 4.0)
        if a - start >= 0.5:
            segs.append({"start": round(start, 2), "end": a, "label": "build"})
    return sorted(segs, key=lambda s: s["start"])
