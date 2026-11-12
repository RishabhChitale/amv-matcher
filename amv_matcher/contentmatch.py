"""F18b content match: rebuild a song's hand splice from the video's own (embedded) soundtrack.

When the video already carries an edit of the song (the user cut the song by hand and the video's own audio is
that edit), the video-event scorer cannot tell the real edit from a near one: cuts and peaks score the same a
beat either way. The audio itself can. match(song, ref) lines the song up against the video's own audio:

1. coarse: 8 s windows of the reference every 2 s, each correlated (Pearson, energy + onset at 20 Hz) against
   every lag of the song;
2. a Viterbi over the lag picks one contiguous path (staying on a lag is free within +-TOL frames, switching to
   another lag costs SWITCH_PEN), so a loop that matches elsewhere equally well never breaks a contiguous run;
   near-silent reference windows (fade-in, tail silence) are neutral;
3. the path is grouped into spans of one offset (spans shorter than MIN_SPAN_S fold into a neighbour);
4. each span's offset is refined on the 100 Hz energy over the whole span (+-REFINE_S, 10 ms steps);
5. each cut between two spans is put where the reference stops matching the first offset and starts matching
   the second (least squared error of the two readings, 10 ms steps).

Rate is 1.0 (a hand splice keeps the song's speed). No match (mean window r < MIN_R, e.g. unrelated audio) gives
None. General: nothing here knows any case; plain numpy, no np.linalg.
"""
from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as _swv

COARSE_HZ = 20.0
WIN_S = 8.0
STEP_S = 2.0
SWITCH_PEN = 0.6          # Viterbi cost of a lag switch (in window-r units)
TOL = 3                   # frames (at COARSE_HZ) a lag may drift and still be "the same" offset
MIN_R = 0.6               # mean window r of the path below this = not the same audio (unrelated: ~0.2)
MIN_SPAN_S = 8.0
FINE_HZ = 100.0
REFINE_S = 1.0
SILENT_FRAC = 0.05        # reference window whose mean energy is below this share of the loud level is neutral


def _curve(a, key, fk, hz, dur):
    x = a.get(key)
    f = float(a.get(fk) or 0)
    if x is None or len(x) < 2 or f <= 0:
        return None
    x = np.asarray(x, dtype=float)
    t = np.arange(0.0, dur, 1.0 / hz)
    return np.interp(t, np.arange(len(x)) / f, x)


def _z(x):
    return (x - x.mean()) / (x.std() + 1e-9)


def _win_corr(win, song):
    """Pearson r of `win` against every same-length window of `song` (len(song) - len(win) + 1 values)."""
    sw = _swv(song, len(win))
    m = sw.mean(axis=1)
    sd = sw.std(axis=1) + 1e-9
    return ((sw - m[:, None]) @ _z(win)) / len(win) / sd


def _viterbi(Rm, neutral):
    """Rm[k, s]: window k's r at lag state s (-1 where invalid). Exact backtrack."""
    K, N = Rm.shape
    w = 2 * TOL + 1
    score = np.where(neutral[0], 0.0, Rm[0]).copy()
    back = np.zeros((K, N), dtype=np.int64)
    idx = np.arange(N)
    for k in range(1, K):
        pad = np.concatenate([np.full(TOL, -np.inf), score, np.full(TOL, -np.inf)])
        win = _swv(pad, w)
        am = np.argmax(win, axis=1)
        stay = win[idx, am]
        stay_from = idx + am - TOL
        j = int(np.argmax(score))
        sw = score[j] - SWITCH_PEN
        take_stay = stay >= sw
        back[k] = np.where(take_stay, stay_from, j)
        row = 0.0 if neutral[k] else Rm[k]
        score = np.where(take_stay, stay, sw) + row
    path = np.zeros(K, dtype=np.int64)
    path[-1] = int(np.argmax(score))
    for k in range(K - 1, 0, -1):
        path[k - 1] = back[k][path[k]]
    return path


def _refine(uo, us, a, b, off0):
    """Offset (s) in off0 +- REFINE_S maximising the z-scored 100 Hz energy correlation over ref [a, b)."""
    lo_l, hi_l = int(round((off0 - REFINE_S) * FINE_HZ)), int(round((off0 + REFINE_S) * FINE_HZ))
    i0 = max(int(round(a * FINE_HZ)), -lo_l, 0)                   # every lag in range stays inside the song
    i1 = min(int(round(b * FINE_HZ)), len(uo), len(us) - hi_l)
    x = uo[i0:i1]
    if len(x) < FINE_HZ or x.std() < 1e-9:
        return off0, 0.0
    x = _z(x)
    best = (-2.0, off0)
    for L in range(lo_l, hi_l + 1):
        y = us[i0 + L:i1 + L]
        r = float(np.mean(x * _z(y)))
        if r > best[0] + 1e-12:
            best = (r, L / FINE_HZ)
    return best[1], best[0]


def _cut(uo, us, lo, hi, oa, ob):
    """Cut time in [lo, hi] where ref switches from reading the song at offset oa to offset ob."""
    i0, i1 = int(round(lo * FINE_HZ)), int(round(hi * FINE_HZ))
    la, lb = int(round(oa * FINE_HZ)), int(round(ob * FINE_HZ))
    i0 = max(i0, -la, -lb, 0)
    i1 = min(i1, len(uo), len(us) - la, len(us) - lb)
    if i1 - i0 < 2:
        return 0.5 * (lo + hi)
    x = uo[i0:i1]
    ya, yb = us[i0 + la:i1 + la], us[i0 + lb:i1 + lb]
    sc = max(np.median(np.abs(x)), 1e-9)
    ea, eb = ((x - ya) / sc) ** 2, ((x - yb) / sc) ** 2
    # cost(c) = sum ea[:c] + sum eb[c:]
    ca = np.concatenate([[0.0], np.cumsum(ea)])
    cb = np.concatenate([[0.0], np.cumsum(eb[::-1])])[::-1]
    c = int(np.argmin(ca + cb))
    return (i0 + c) / FINE_HZ


def match(song: dict, ref: dict) -> dict | None:
    """Segments {src_start, src_end, dst_start} (dst = reference/video time) that rebuild `ref` (the video's own
    audio analysis) from `song`, or None when `ref` is not an edit of `song`. Also: spans [{dst_start, dst_end,
    offset, r}], mean_r."""
    sd, rd = float(song.get("duration") or 0), float(ref.get("duration") or 0)
    if sd < WIN_S or rd < WIN_S:
        return None
    fo = [_curve(ref, "energy_curve", "energy_hz", COARSE_HZ, rd), _curve(ref, "onset_env", "onset_env_fps", COARSE_HZ, rd)]
    fs = [_curve(song, "energy_curve", "energy_hz", COARSE_HZ, sd), _curve(song, "onset_env", "onset_env_fps", COARSE_HZ, sd)]
    keep = [i for i in range(2) if fo[i] is not None and fs[i] is not None]
    if 0 not in keep:
        return None
    fo, fs = [fo[i] for i in keep], [fs[i] for i in keep]
    W, STEP = int(WIN_S * COARSE_HZ), int(STEP_S * COARSE_HZ)
    n_ref, n_song = len(fo[0]), len(fs[0])
    starts = list(range(0, n_ref - W + 1, STEP))
    if starts[-1] != n_ref - W:
        starts.append(n_ref - W)
    nl = n_song - W + 1
    if nl < 1:
        return None
    e0 = fo[0]
    loud = float(np.percentile(e0, 90)) or 1e-9
    neutral = np.zeros(len(starts), dtype=bool)
    offs = np.arange(-n_ref, n_song)                 # lag state: song index = ref index + off
    Rm = np.full((len(starts), len(offs)), -1.0)
    for k, i in enumerate(starts):
        win = e0[i:i + W]
        if win.mean() < SILENT_FRAC * loud or win.std() < 1e-6:
            neutral[k] = True
            continue
        r = np.zeros(nl)
        for f_o, f_s in zip(fo, fs):
            ww = f_o[i:i + W]
            if ww.std() < 1e-9:
                continue
            r += _win_corr(ww, f_s) / len(fo)
        j = i + offs
        ok = (j >= 0) & (j < nl)
        Rm[k, ok] = r[j[ok]]
    if neutral.all():
        return None
    path = _viterbi(Rm, neutral)
    live = [k for k in range(len(starts)) if not neutral[k]]
    mean_r = float(np.mean([Rm[k, path[k]] for k in live]))
    if mean_r < MIN_R:
        return None
    # group windows into spans of one offset (neutral windows join whichever span they sit in)
    spans = []
    for k in range(len(starts)):
        o = int(offs[path[k]])
        if spans and (neutral[k] or abs(o - spans[-1]["o"]) <= TOL):
            spans[-1]["k1"] = k
        else:
            spans.append({"o": o, "k0": k, "k1": k})
    min_w = int(np.ceil(MIN_SPAN_S / STEP_S))
    changed = True
    while changed and len(spans) > 1:                # fold short spans into the neighbour with the closer offset
        changed = False
        for n, sp in enumerate(spans):
            if sp["k1"] - sp["k0"] + 1 >= min_w:
                continue
            nb = [m for m in (n - 1, n + 1) if 0 <= m < len(spans)]
            m = min(nb, key=lambda q: (abs(spans[q]["o"] - sp["o"]), q))
            spans[m]["k0"], spans[m]["k1"] = min(spans[m]["k0"], sp["k0"]), max(spans[m]["k1"], sp["k1"])
            del spans[n]
            changed = True
            break
    uo = _curve(ref, "energy_curve", "energy_hz", FINE_HZ, rd)
    us = _curve(song, "energy_curve", "energy_hz", FINE_HZ, sd)
    H = COARSE_HZ
    # cut search region between consecutive spans: from the last window of A to the end of B's first window
    bounds = [0.0]
    for a, b in zip(spans, spans[1:]):
        bounds.append((starts[a["k1"]] / H, starts[b["k0"]] / H + WIN_S))
    bounds.append(rd)
    # refine offsets on each span's own stretch (away from the cut regions), then the cuts
    out = []
    for n, sp in enumerate(spans):
        a = 0.0 if n == 0 else bounds[n][1]
        b = rd if n == len(spans) - 1 else bounds[n + 1][0]
        if b - a < 2.0:
            a, b = starts[sp["k0"]] / H, starts[sp["k1"]] / H + WIN_S
        live_a = min((starts[k] / H for k in range(sp["k0"], sp["k1"] + 1) if not neutral[k]), default=a)
        live_b = max((starts[k] / H + WIN_S for k in range(sp["k0"], sp["k1"] + 1) if not neutral[k]), default=b)
        a, b = max(a, live_a), min(b, live_b)
        off, r = _refine(uo, us, a, max(b, a + 1.0), sp["o"] / H)
        out.append({"offset": off, "r": r})
    cuts = [_cut(uo, us, lo, hi, out[n]["offset"], out[n + 1]["offset"])
            for n, (lo, hi) in enumerate(bounds[1:-1])]
    edges = [0.0] + cuts + [rd]
    segs, spans_out = [], []
    for n, sp in enumerate(out):
        d0, d1 = edges[n], edges[n + 1]
        s0, s1 = d0 + sp["offset"], min(sd, d1 + sp["offset"])
        if s1 - s0 <= 0.05:
            continue
        segs.append({"src_start": round(s0, 3), "src_end": round(s1, 3), "dst_start": round(d0, 3)})
        spans_out.append({"dst_start": round(d0, 3), "dst_end": round(d1, 3), "offset": round(sp["offset"], 3),
                          "r": round(sp["r"], 3)})
    if not segs:
        return None
    return {"segments": segs, "spans": spans_out, "mean_r": round(mean_r, 3)}
