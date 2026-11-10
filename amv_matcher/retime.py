"""Video section retime: a monotonic piecewise-linear warp that lands cuts/motion peaks on beats."""
from __future__ import annotations

import numpy as np

from . import score as S

MAX_JUMP = 0.15          # preferred limit on neighbouring section speed change
LOOKBACK = 8             # matched events a chain may skip back over
NEAR = 2                 # candidate beats per side of an event
SPEED_COST = 0.02        # tie-break: prefer speeds near 1


def _wmap(warp, t):
    w = np.asarray(warp, dtype=float)
    return np.interp(np.asarray(t, dtype=float), w[:, 0], w[:, 1])


def _winv(warp, t):
    w = np.asarray(warp, dtype=float)
    return np.interp(np.asarray(t, dtype=float), w[:, 1], w[:, 0])


def apply_warp(video: dict, warp) -> dict:
    """Video dict as seen after the warp (output time). Used to re-score with score.py."""
    w = np.asarray(warp, dtype=float)
    od = float(w[-1, 1])
    out = dict(video)
    out["duration"] = od
    out["cut_times"] = _wmap(w, video.get("cut_times", [])).tolist()
    out["motion_peaks"] = _wmap(w, video.get("motion_peaks", [])).tolist()
    out["peak_segments"] = [[float(_wmap(w, a)), float(_wmap(w, b))] for a, b in video.get("peak_segments", [])]
    out["lulls"] = [[float(_wmap(w, a)), float(_wmap(w, b))] for a, b in video.get("lulls", [])]
    out["segments"] = [dict(s, start=float(_wmap(w, s["start"])), end=float(_wmap(w, s["end"])))
                       for s in video.get("segments", [])]
    ve = np.asarray(video.get("energy_curve", []), dtype=float)
    if len(ve):
        hz = S.ENERGY_HZ
        n = max(1, int(round(od * hz)))
        src = _winv(w, np.arange(n) / hz)
        out["energy_curve"] = np.interp(src, np.arange(len(ve)) / hz, ve).tolist()
    vd = float(video["duration"])
    if od > 0 and video.get("motion_tempo_bpm"):
        out["motion_tempo_bpm"] = float(video["motion_tempo_bpm"]) * vd / od
    return out


def _anchors(ev_t, ev_w, beats_out, dev):
    """DP: monotone event->beat matching, interval speeds in [1-dev, 1+dev]. Returns [(e, b)]."""
    n = len(ev_t)
    cands = []
    for e in ev_t:
        k = int(np.searchsorted(beats_out, e))
        cands.append(list(range(max(0, k - NEAR), min(len(beats_out), k + NEAR))))
    lo, hi = 1.0 - dev, 1.0 + dev
    NEG = -1e18
    val = [dict() for _ in range(n)]      # val[i][j] = (score, prev_i, prev_j)
    for i in range(n):
        for j in cands[i]:
            b, e = beats_out[j], ev_t[i]
            best = None
            if b > 0 and lo - 1e-9 <= e / b <= hi + 1e-9:     # from the fixed start (0, 0)
                best = (ev_w[i] - SPEED_COST * abs(e / b - 1.0) * e, -1, -1)
            for p in range(max(0, i - LOOKBACK), i):
                for pj, (ps, _, _) in val[p].items():
                    bp = beats_out[pj]
                    if b <= bp + 1e-9:
                        continue
                    s = (e - ev_t[p]) / (b - bp)
                    if s < lo - 1e-9 or s > hi + 1e-9:
                        continue
                    sc = ps + ev_w[i] - SPEED_COST * abs(s - 1.0) * (e - ev_t[p])
                    if best is None or sc > best[0]:
                        best = (sc, p, pj)
            if best is not None:
                val[i][j] = best
    end = max(((v[0], i, j) for i in range(n) for j, v in val[i].items()), default=None)
    if end is None:
        return []
    out, i, j = [], end[1], end[2]
    while i >= 0:
        out.append((float(ev_t[i]), float(beats_out[j])))
        _, i, j = val[i][j]
    return out[::-1]


def _sections(warp, min_section_s, speed_tol=0.02):
    """Group warp intervals of near-equal speed; drop speed==1 and sections shorter than min."""
    w = np.asarray(warp, dtype=float)
    iv = []
    for k in range(len(w) - 1):
        dv, do = w[k + 1, 0] - w[k, 0], w[k + 1, 1] - w[k, 1]
        if dv > 1e-9:
            iv.append([w[k, 0], w[k + 1, 0], w[k, 1], w[k + 1, 1]])
    groups = []
    for a in iv:
        sp = (a[1] - a[0]) / max(a[3] - a[2], 1e-9)
        if groups:
            g = groups[-1]
            gs = (g[1] - g[0]) / max(g[3] - g[2], 1e-9)
            if abs(sp - gs) <= speed_tol or (g[1] - g[0]) < min_section_s or (a[1] - a[0]) < min_section_s:
                g[1], g[3] = a[1], a[3]
                continue
        groups.append(list(a))
    return groups


def plan_retime(song: dict, video: dict, offset: float, max_speed_dev: float = 0.25,
                min_section_s: float = 0.75) -> dict:
    """Warp video time so events land on the nearest suitable beats (song at `offset`).

    Output time t_out = song time - offset. speed = video seconds per output second.
    """
    vd = float(video["duration"])
    ev_t, ev_w = S._events(video)
    order = np.argsort(ev_t)
    ev_t, ev_w = ev_t[order], ev_w[order]
    keep = ev_t > 0.05
    ev_t, ev_w = ev_t[keep], ev_w[keep]
    beats = np.asarray(song["beat_times"], dtype=float) - float(offset)
    beats = beats[(beats > 0) & (beats < vd / max(1e-6, 1.0 - max_speed_dev) + 1.0)]
    before = S.score_at(song, video, offset)["sync_pct"]
    anchors = _anchors(ev_t, ev_w, beats, max_speed_dev) if len(ev_t) and len(beats) else []
    warp = [[0.0, 0.0]] + [[a, b] for a, b in anchors]
    if not anchors or warp[-1][0] < vd:
        lv, lo = warp[-1]
        warp.append([vd, lo + (vd - lv)])
    warp = [[round(a, 4), round(b, 4)] for a, b in warp]
    groups = _sections(warp, min_section_s)
    sections, moved_set = [], set(round(a, 4) for a, b in anchors if abs(a - b) > 0.02)
    prev_speed = 1.0
    max_jump = 0.0
    for g in groups:
        sp = (g[1] - g[0]) / max(g[3] - g[2], 1e-9)
        max_jump = max(max_jump, abs(sp - prev_speed)) if sections else max_jump
        if abs(sp - 1.0) <= 0.01:
            prev_speed = sp
            continue
        moved = sum(1 for e in ev_t if g[0] - 1e-6 <= e <= g[1] + 1e-6 and round(float(e), 4) in moved_set)
        sections.append({"v_start": round(g[0], 3), "v_end": round(g[1], 3), "speed": round(sp, 4),
                         "out_start": round(g[2], 3), "out_end": round(g[3], 3), "events_moved": moved,
                         "reason": "land %d event(s) on the beat grid at %.2fx" % (moved, sp)})
        prev_speed = sp
    wv = apply_warp(video, warp)
    after = S.score_at(song, wv, offset)["sync_pct"]
    return {"sections": sections, "warp": warp, "out_duration": round(float(warp[-1][1]), 3),
            "sync_before": before, "sync_after": after, "max_speed_jump": round(max_jump, 3),
            "speed_jump_ok": max_jump <= MAX_JUMP + 1e-9, "max_speed_dev": max_speed_dev}
