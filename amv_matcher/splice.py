"""Song splicing: cut the SONG on bar boundaries so it fits the video better than one trim.

plan_splice() does an exact dynamic program over output bar slots. State =
(source bar of the current segment, cuts used, run length capped at the minimum
segment length). Transition = continue with the next source bar, or cut (jump
to any other bar) when the current segment is long enough. The DP maximises a
decomposable proxy of score.py's sync / arc math plus drop/lull priority
bonuses minus a per-cut penalty; the best plan for each cut count (and the
plain single trim) are then re-scored exactly with score.score_at() on the
assembled song, and the best one is returned.
"""
from __future__ import annotations

import math

import numpy as np

from . import score as S

BEAT_PER_BAR = 4
CUT_PENALTY = 1.5          # combined-score points charged per cut
DROP_PEAK_TOL_S = 0.25     # song drop within this of a video peak start
DROP_PEAK_PTS = 7.0        # priority bonus per drop-on-peak match (score.py arc bonus is already 4)
LULL_CALM_PTS = 4.0        # priority bonus for song lulls covering video calm segments
NEG = -1e18


# ----------------------------------------------------------------- helpers
def _bars(song):
    """Bar start times from the beat grid (4 beats per bar). Returns array of
    nbars+1 boundaries (bar k = [b[k], b[k+1])) or None if too few beats."""
    bt = np.asarray(song["beat_times"], dtype=float)
    nb = (len(bt) - 1) // BEAT_PER_BAR
    if nb < 2:
        return None, bt
    return bt[: nb * BEAT_PER_BAR + 1: BEAT_PER_BAR], bt


def _z(x):
    x = np.asarray(x, dtype=float)
    sd = float(x.std())
    return (x - x.mean()) / sd if sd > 1e-9 else np.zeros_like(x)


def _single(off, vd):
    return [{"src_start": round(off, 6), "src_end": round(off + vd, 6), "dst_start": 0.0}]


def _assemble(song, segments, vd):
    """Build a song dict as heard after splicing (output time 0..vd)."""
    hz = S.ENERGY_HZ
    se = np.asarray(song["energy_curve"], dtype=float)
    n_out = int(math.ceil(vd * hz))
    en = np.zeros(n_out)
    bt = np.asarray(song["beat_times"], dtype=float)
    drops = np.asarray(song.get("drops", []), dtype=float)
    lulls = song.get("lulls", [])
    beats, dr, lu = [], [], []
    for sg in segments:
        a, b, d0 = sg["src_start"], sg["src_end"], sg["dst_start"]
        i0, o0 = int(round(a * hz)), int(round(d0 * hz))
        n = min(int(round((b - a) * hz)), n_out - o0, max(0, len(se) - i0))
        if n > 0:
            en[o0:o0 + n] = se[i0:i0 + n]
        beats += list(bt[(bt >= a) & (bt < b)] - a + d0)
        dr += list(drops[(drops >= a) & (drops < b)] - a + d0)
        for la, lb in lulls:
            ca, cb = max(la, a), min(lb, b)
            if cb > ca:
                lu.append([ca - a + d0, cb - a + d0])
    return {"bpm": song["bpm"], "duration": vd, "beat_times": sorted(beats), "energy_curve": en.tolist(),
            "energy_hz": hz, "drops": sorted(dr), "lulls": lu}


def _bonus(asm, video):
    """Priority bonus (points of the combined score) for drops on peaks, lulls over calm."""
    pts = 0.0
    drops = np.asarray(asm["drops"], dtype=float)
    for a, _b in video.get("peak_segments", []):
        if len(drops) and np.min(np.abs(drops - a)) <= DROP_PEAK_TOL_S:
            pts += DROP_PEAK_PTS
    for va, vb in video.get("lulls", []):
        ov = sum(max(0.0, min(vb, sb) - max(va, sa)) for sa, sb in asm["lulls"])
        pts += LULL_CALM_PTS * min(1.0, ov / max(vb - va, 1e-6))
    return pts


def _eval(song, video, segments, ncuts):
    vd = float(video["duration"])
    asm = _assemble(song, segments, vd)
    r = S.score_at(asm, video, 0.0)
    raw = S._combine(r["tempo_pct"], r["sync_pct"], r["arc_pct"])
    return {"r": r, "obj": raw + _bonus(asm, video) - CUT_PENALTY * ncuts}


# ------------------------------------------------------------ DP machinery
def _gains(song, video, bars, bt, L, nslots):
    """gain[s, k]: proxy score points for putting source bar k in output slot s."""
    vd = float(video["duration"])
    hz = S.ENERGY_HZ
    K = len(bars) - 1
    ve = np.asarray(video["energy_curve"], dtype=float)
    se = np.asarray(song["energy_curve"], dtype=float)
    N = len(ve)
    nb = max(1, int(round(L * hz)))
    vz, sz = _z(ve), _z(se)
    VS = np.zeros((nslots, nb))
    VW = np.zeros((nslots, nb))
    vlull = np.zeros(N)
    for va, vb in video.get("lulls", []):
        i0, i1 = int(round(va * hz)), min(N, int(round(vb * hz)))
        if i1 > i0:
            vlull[i0:i1] = np.maximum(vlull[i0:i1], 1.0 / (i1 - i0))
    for s in range(nslots):
        a = int(round(s * L * hz))
        n = max(0, min(nb, N - a))
        VS[s, :n] = vz[a:a + n]
        VW[s, :n] = vlull[a:a + n]
    slull = np.zeros(len(se))
    for la, lb in song.get("lulls", []):
        slull[int(round(la * hz)):int(round(lb * hz))] = 1.0
    SB = np.zeros((K, nb))
    SM = np.zeros((K, nb))
    for k in range(K):
        a = int(round(bars[k] * hz))
        n = max(0, min(nb, len(se) - a))
        SB[k, :n] = sz[a:a + n]
        SM[k, :n] = slull[a:a + n]
    gain = 0.4 * 50.0 * (VS @ SB.T) / max(N, 1)                    # arc: Pearson ~ mean of z-products
    gain += LULL_CALM_PTS * (VW @ SM.T)                             # lulls over calm

    # sync: weighted fraction of video events within tolerance of a beat in the slot
    ev_t, ev_w = S._events(video)
    if len(ev_t) and len(bt) >= 2:
        wtot = float(ev_w.sum())
        base = min(0.95, len(bt) / max(float(song["duration"]), 1e-6) * 2 * S.TOLERANCE_S)
        scale = 0.4 * 100.0 / (wtot * (1.0 - base))
        off4 = np.stack([bt[BEAT_PER_BAR * np.arange(K) + j] - bars[:K] for j in range(BEAT_PER_BAR)], axis=1)  # (K,4)
        for s in range(nslots):
            m = (ev_t >= s * L - 1e-9) & (ev_t < (s + 1) * L - 1e-9)
            if not m.any():
                continue
            t, w = ev_t[m], ev_w[m]
            d = np.abs(t[:, None, None] - (s * L + off4)[None, :, :]).min(axis=2)       # (E,K)
            gain[s] += scale * ((d <= S.TOLERANCE_S) * w[:, None]).sum(axis=0)

    # drops on peaks (priority)
    peaks = np.asarray([a for a, _b in video.get("peak_segments", [])], dtype=float)
    drops = np.asarray(song.get("drops", []), dtype=float)
    if len(peaks) and len(drops):
        kd = np.clip(np.searchsorted(bars, drops, side="right") - 1, 0, K - 1)
        for d, k in zip(drops, kd):
            if not (bars[k] <= d < bars[k + 1]):
                continue
            o = d - bars[k]
            for a in peaks:
                s = int(round((a - o) / L))                # slot whose start puts the drop on the peak
                for s2 in (s - 1, s, s + 1):
                    if 0 <= s2 < nslots and abs(s2 * L + o - a) <= DROP_PEAK_TOL_S:
                        gain[s2, k] += DROP_PEAK_PTS
    return gain


def _dp(gain, max_cuts, R, min_cut_slot_ok):
    """Exact DP. Returns best (value, cuts, path) per cut count; path = source bar per slot."""
    nslots, K = gain.shape
    C = max_cuts + 1
    V = np.full((C, R, K), NEG)
    V[0, 0, :] = gain[0]
    bp = []
    ar = np.arange(K)
    for s in range(1, nslots):
        NV = np.full((C, R, K), NEG)
        pc = np.zeros((C, R, K), dtype=np.int8)
        pr = np.zeros((C, R, K), dtype=np.int8)
        pk = np.zeros((C, R, K), dtype=np.int32)
        # continue: (c, r, k) -> (c, min(r+1,R-1), k+1)
        for r in range(R):
            r2 = min(r + 1, R - 1)
            cand = V[:, r, :-1]
            tgt = NV[:, r2, 1:]
            better = cand > tgt
            tgt[better] = cand[better]
            pc[:, r2, 1:][better] = np.broadcast_to(np.arange(C)[:, None], cand.shape)[better]
            pr[:, r2, 1:][better] = r
            pk[:, r2, 1:][better] = np.broadcast_to(ar[:-1], cand.shape)[better]
        # cut: from (c, R-1, k*) to (c+1, 0, k') with k' != k*+1; top-2 over k*
        if min_cut_slot_ok(s):
            for c in range(C - 1):
                row = V[c, R - 1]
                if row.max() <= NEG / 2:
                    continue
                o = np.argsort(row)[::-1][:2]
                m1, m2 = int(o[0]), int(o[1]) if K > 1 else int(o[0])
                v1, v2 = row[m1], row[m2]
                use1 = (ar != m1 + 1)
                val = np.where(use1, v1, v2)
                who = np.where(use1, m1, m2)
                val = val - CUT_PENALTY
                better = val > NV[c + 1, 0]
                NV[c + 1, 0][better] = val[better]
                pc[c + 1, 0][better] = c
                pr[c + 1, 0][better] = R - 1
                pk[c + 1, 0][better] = who[better]
        NV += np.where(NV > NEG / 2, gain[s][None, None, :], 0.0)
        V = NV
        bp.append((pc, pr, pk))
    out = []
    for c in range(C):
        fl = V[c].reshape(-1)
        j = int(np.argmax(fl))
        if fl[j] <= NEG / 2:
            continue
        r, k = divmod(j, K)
        path = [k]
        cc, rr, kk = c, r, k
        for s in range(nslots - 1, 0, -1):
            pc, pr, pk = bp[s - 1]
            cc, rr, kk = int(pc[cc, rr, kk]), int(pr[cc, rr, kk]), int(pk[cc, rr, kk])
            path.append(kk)
        out.append((float(fl[j]), c, path[::-1]))
    return out


def _path_to_plan(path, bars, vd, L):
    """Source-bar-per-slot path -> segments (contiguous runs) with exact durations."""
    runs = []
    for s, k in enumerate(path):
        if runs and k == runs[-1][1] + runs[-1][2]:
            runs[-1][2] += 1
        else:
            runs.append([s, k, 1])
    segs, dst = [], 0.0
    for i, (_s, k, n) in enumerate(runs):
        a = float(bars[k])
        if i == len(runs) - 1:
            b = a + (vd - dst)
        else:
            b = float(bars[k + n])
        a, b = round(a, 6), round(b, 6)
        segs.append({"src_start": a, "src_end": b, "dst_start": round(dst, 6)})
        dst = round(dst + (b - a), 6)
    return segs


def _valid(segs, vd, min_seg, sd):
    for i, sg in enumerate(segs):
        ln = sg["src_end"] - sg["src_start"]
        if ln < min_seg - 1e-6 and len(segs) > 1:
            return False
        if sg["src_end"] > sd + 1e-6 or sg["src_start"] < -1e-9:
            return False
    last = segs[-1]
    return abs(last["dst_start"] + last["src_end"] - last["src_start"] - vd) < 1e-5


def _cuts(segs):
    return [{"dst_time": b["dst_start"], "src_from": a["src_end"], "src_to": b["src_start"]}
            for a, b in zip(segs[:-1], segs[1:])]


# ------------------------------------------------------------------- API
def plan_splice(song: dict, video: dict, max_cuts: int, min_segment_s: float = 2.0) -> dict:
    vd, sd = float(video["duration"]), float(song["duration"])
    base = S.score(song, video)
    if max_cuts <= 0 and not base["eligible"]:
        raise ValueError("song is shorter than the video; splicing needs max_cuts > 0")
    off = float(base["best_offset_s"])
    base_segs = _single(off, vd)
    base_total = int(base["total_pct"])

    def pack(segs, r):
        return {"segments": segs, "cuts": _cuts(segs), "total_pct": r["total_pct"], "tempo_pct": r["tempo_pct"],
                "sync_pct": r["sync_pct"], "arc_pct": r["arc_pct"],
                "baseline": {"offset": off, "total_pct": base_total},
                "improvement_pct": r["total_pct"] - base_total}

    base_res = {k: base[k] for k in ("total_pct", "tempo_pct", "sync_pct", "arc_pct")}
    if max_cuts <= 0:
        return pack(base_segs, base_res)

    bars, bt = _bars(song)
    if bars is None:
        return pack(base_segs, base_res)
    K = len(bars) - 1
    L = float(np.median(np.diff(bars)))
    nslots = max(1, int(math.ceil(vd / L - 1e-9)))
    R = max(1, int(math.ceil(min_segment_s / L - 1e-9)))
    if nslots < 2 * R:
        return pack(base_segs, base_res)
    gain = _gains(song, video, bars, bt, L, nslots)
    # a cut at slot s leaves vd - s*L for the tail segment; it must still be >= min_segment_s
    plans = _dp(gain, int(max_cuts), R, lambda s: vd - s * L >= min_segment_s - 1e-9)

    best_obj = _eval(song, video, base_segs, 0)["obj"]
    best = (base_segs, base_res)
    for _v, c, path in plans:
        if c == 0:
            continue
        segs = _path_to_plan(path, bars, vd, L)
        if len(segs) - 1 > max_cuts or not _valid(segs, vd, min_segment_s, sd):
            continue
        e = _eval(song, video, segs, len(segs) - 1)
        if e["obj"] > best_obj and e["r"]["total_pct"] >= base_total:
            best_obj, best = e["obj"], (segs, e["r"])
    return pack(*best)
