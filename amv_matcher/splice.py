"""Song splicing: cut the SONG on bar boundaries so it fits the video better than one trim.

Conventions (deliberate, not bugs):
  * The FIRST segment's src_start is free (0.05 s grid, like score.py) so the most
    important song drop can land exactly on the strongest video peak. Every cut
    after it is on a bar boundary on BOTH sides (src_from and src_to).
  * The FINAL segment's src_end is trimmed so the output equals the video duration
    exactly; it is therefore not bar aligned.
  * Bars are 4 beats; the downbeat phase (0-3) is estimated from onset strength and
    the low-band-weighted energy curve at beats and returned as 'bar_phase'.
  * total_pct is the raw score.score_at() result on the assembled song and is what
    improvement_pct uses; 'objective' = raw combined score + drop/lull priority
    bonus - CUT_PENALTY per cut (what the planner maximises).
  * A song shorter than the video (F2b) is planned on the best video stretch of its length (the window
    score.score picks): segments are in clip time and plan["video_window"] (score.video_use) says which
    source stretch they refer to. Only a song with no decodable audio returns {eligible: False,
    reason: 'unreadable', segments: [], ...}.

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
from .retime import Cancelled

BEAT_PER_BAR = 4
CUT_PENALTY = 1.5          # combined-score points charged per cut
DROP_PEAK_TOL_S = 0.25     # song drop within this of a video peak start
DROP_PEAK_PTS = 7.0        # priority bonus per drop-on-peak match (score.py arc bonus is already 4)
LULL_CALM_PTS = 4.0        # priority bonus for song lulls covering video calm segments
NEG = -1e18


# ----------------------------------------------------------------- helpers
def _bar_phase(song):
    """Downbeat phase 0-3: beat phase whose beats carry the most onset strength + low-band energy."""
    bt = np.asarray(song["beat_times"], dtype=float)
    if len(bt) < 8:
        return 0
    en = np.asarray(song["energy_curve"], dtype=float)
    ehz = float(song.get("energy_hz", S.ENERGY_HZ))
    ei = np.clip(np.round(bt * ehz).astype(int), 0, len(en) - 1)
    f = en[ei]
    on = np.asarray(song.get("onset_env", []), dtype=float)
    if len(on):
        ofps = float(song.get("onset_env_fps", 1.0))
        oi = np.round(bt * ofps).astype(int)
        o = np.array([on[max(0, i - 1):i + 2].max() if 0 <= i < len(on) else 0.0 for i in oi])
        f = _z(f) + _z(o)
    best, bs = 0, -1e18
    for p in range(BEAT_PER_BAR):
        v = float(f[p::BEAT_PER_BAR].mean())
        if v > bs + 1e-9:
            best, bs = p, v
    return best


def _bars(song, phase=0):
    """Bar start times (every 4th beat from `phase`). Returns (nbars+1 boundaries or None, beats)."""
    bt = np.asarray(song["beat_times"], dtype=float)[phase:]
    nb = (len(bt) - 1) // BEAT_PER_BAR
    if nb < 2:
        return None, np.asarray(song["beat_times"], dtype=float)
    return bt[: nb * BEAT_PER_BAR + 1: BEAT_PER_BAR], np.asarray(song["beat_times"], dtype=float)


def _z(x):
    x = np.asarray(x, dtype=float)
    sd = float(x.std())
    return (x - x.mean()) / sd if sd > 1e-9 else np.zeros_like(x)


def _single(off, vd):
    return [{"src_start": round(off, 6), "src_end": round(off + vd, 6), "dst_start": 0.0}]


def lead_silence(segments):
    """Seconds of silence before the song starts (negative first src_start), 0.0 if none."""
    if not segments:
        return 0.0
    first = min(segments, key=lambda g: g["dst_start"])
    return round(max(0.0, -float(first["src_start"])) if first["dst_start"] <= 1e-9 else 0.0, 6)


def _assemble(song, segments, vd):
    """Build a song dict as heard after splicing (output time 0..vd), carrying EVERY analysis key
    score.py reads, remapped through the segments (repeats and seams included):
    beat_times/beat_strengths, beat_grid (+ grid_bpm/grid_period/grid_used/beats_per_bar carried,
    bar_phase recomputed as the position of the first kept downbeat), onset_env (+fps), energy_curve,
    drops, lulls, sections (clipped). Scalars bpm and duration: bpm carried, duration = vd.

    A segment with src_start < 0 means the song starts AFTER that segment's start: the first
    -src_start seconds of the segment are silence (zero energy, no beats, zero onset). Only the first
    segment (dst_start == 0) may do this; plan_splice reports it as plan["lead_silence_s"].
    Segments are hard cuts (no crossfade in the model), so seams need no special handling."""
    hz = S.ENERGY_HZ
    se = np.asarray(song["energy_curve"], dtype=float)
    n_out = int(math.ceil(vd * hz))
    en = np.zeros(n_out)
    bt = np.asarray(song["beat_times"], dtype=float)
    bs = song.get("beat_strengths")
    bs = np.asarray(bs, dtype=float) if bs is not None and len(bs) == len(bt) else None
    g = np.asarray(song.get("beat_grid") if song.get("beat_grid") is not None else [], dtype=float)
    bpb = int(song.get("beats_per_bar") or 4)
    ph = int(song.get("bar_phase") or 0) % bpb
    drops = np.asarray(song.get("drops", []), dtype=float)
    lulls = song.get("lulls", [])
    env = song.get("onset_env")
    fps = float(song.get("onset_env_fps") or 0)
    has_env = env is not None and len(env) > 0 and fps > 0
    if has_env:
        env = np.asarray(env, dtype=float)
        oe = np.zeros(int(math.ceil(vd * fps)) + 1)
    beats, strengths, grid, dr, lu, secs = [], [], [], [], [], []
    first_bar = None
    for sg in segments:
        a, b, d0 = sg["src_start"], sg["src_end"], sg["dst_start"]
        i0, o0 = int(round(a * hz)), int(round(d0 * hz))
        if i0 < 0:                      # silence padding before the song starts
            o0 -= i0
            i0 = 0
        n = min(int(round((b - a) * hz)), n_out - o0, max(0, len(se) - i0))
        if n > 0:
            en[o0:o0 + n] = se[i0:i0 + n]
        if has_env:
            j0, p0 = int(round(a * fps)), int(round(d0 * fps))
            if j0 < 0:
                p0 -= j0
                j0 = 0
            m = min(int(round((b - a) * fps)), len(oe) - p0, len(env) - j0)
            if m > 0:
                oe[p0:p0 + m] = env[j0:j0 + m]
        sel = (bt >= a) & (bt < b)
        beats += list(bt[sel] - a + d0)
        if bs is not None:
            strengths += list(bs[sel])
        for k in np.nonzero((g >= a) & (g < b))[0]:
            if first_bar is None and int(k) % bpb == ph:
                first_bar = len(grid)
            grid.append(float(g[k] - a + d0))
        dr += list(drops[(drops >= a) & (drops < b)] - a + d0)
        for la, lb in lulls:
            ca, cb = max(la, a), min(lb, b)
            if cb > ca:
                lu.append([ca - a + d0, cb - a + d0])
        for s in song.get("sections") or []:
            ca, cb = max(s["start"], a), min(s["end"], b)
            if cb - ca > 1e-6:
                secs.append(dict(s, start=ca - a + d0, end=cb - a + d0))
    order = np.argsort(beats, kind="stable") if len(beats) else []
    out = {"bpm": song["bpm"], "duration": vd, "beat_times": [beats[i] for i in order],
           "energy_curve": en.tolist(), "energy_hz": hz, "drops": sorted(dr), "lulls": lu}
    if bs is not None:
        out["beat_strengths"] = [strengths[i] for i in order]
    if "beat_grid" in song:
        out.update({"beat_grid": grid, "grid_bpm": song.get("grid_bpm", 0.0),
                    "grid_period": song.get("grid_period", 0.0), "grid_used": song.get("grid_used", False),
                    "beats_per_bar": bpb, "bar_phase": (first_bar or 0) % bpb})
    if has_env:
        out["onset_env"], out["onset_env_fps"] = oe.tolist(), fps
    if "sections" in song:
        out["sections"] = secs
    if S.F15_ENABLED:                         # only the F15 story gate reads it
        out["story_src"] = _story_map(song, segments, n_out, hz)
    from .boom import splice_song_booms
    out.update(splice_song_booms(song, segments))
    return out


def _story_map(song, segments, n_out, hz):
    """score.story_stats source: the energy of the song the output was cut from (the original when
    `song` is itself assembled) plus, per output sample, the source time heard there (-1 = silence),
    padded by STORY_LAG_S + 1 s each side by running the first/last segment on."""
    tm = np.full(n_out, -1.0)
    for sg in segments:
        a, b, d0 = sg["src_start"], sg["src_end"], sg["dst_start"]
        o0, o1 = int(round(d0 * hz)), min(n_out, int(round((d0 + b - a) * hz)))
        if o1 > o0:
            tm[o0:o1] = int(round(a * hz)) / hz + np.arange(o1 - o0) / hz
    tm[tm < 0] = -1.0
    m = int(round((S.STORY_LAG_S + 1.0) * hz))
    pre = segments[0]["src_start"] + (np.arange(-m, 0) + int(round(segments[0]["dst_start"] * hz))) / hz if segments else np.zeros(m)
    last = segments[-1] if segments else None
    post = (int(round(last["src_start"] * hz)) + n_out - int(round(last["dst_start"] * hz)) + np.arange(m)) / hz if last else np.zeros(m)
    tm = np.concatenate([np.where(pre >= 0, pre, -1.0), tm, post])
    src = song.get("story_src")
    if src:                                   # compose with the source map of an assembled input
        old = np.asarray(src["t"], dtype=float)
        i = np.round(tm * hz).astype(int) - int(src.get("t0", 0))
        ok = (tm >= 0) & (i >= 0) & (i < len(old))
        tm = np.where(ok, old[np.clip(i, 0, len(old) - 1)], -1.0)
        return dict(src, t=tm.tolist(), t0=-m)
    return {"energy_curve": song["energy_curve"], "energy_hz": song.get("energy_hz", hz), "bpm": song.get("bpm"),
            "beats_per_bar": song.get("beats_per_bar"), "t": tm.tolist(), "t0": -m}


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


def _raw_total(r):
    """total_pct without the build_pct bonus (tempo/sync/arc only)."""
    return S._combine(r["tempo_pct"], r["sync_pct"], r["arc_pct"])


def _eval(song, video, segments, ncuts, lead_s=None):
    vd = float(video["duration"])
    asm = _assemble(song, segments, vd)
    r = S.score_at(asm, video, 0.0, lead_s=lead_s)
    raw = S._combine(r["tempo_pct"], r["sync_pct"], r["arc_pct"])
    return {"r": r, "obj": raw + _bonus(asm, video) - CUT_PENALTY * ncuts}


# ------------------------------------------------------------ DP machinery
def _gains(song, video, bars, bt, L, g, nslots, lead_s=None):
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
        a = int(round((g + s * L) * hz))
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
            m = (ev_t >= g + s * L - 1e-9) & (ev_t < g + (s + 1) * L - 1e-9)
            if not m.any():
                continue
            t, w = ev_t[m], ev_w[m]
            d = np.abs(t[:, None, None] + S._lead(lead_s) - (g + s * L + off4)[None, :, :]).min(axis=2)       # (E,K)
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
                s = int(round((a - o - g) / L))                # slot whose start puts the drop on the peak
                for s2 in (s - 1, s, s + 1):
                    if 0 <= s2 < nslots and abs(g + s2 * L + o - a) <= DROP_PEAK_TOL_S:
                        gain[s2, k] += DROP_PEAK_PTS
    return gain


def _dp(gain, max_cuts, R, min_cut_slot_ok, init):
    """Exact DP. Returns best (value, cuts, path) per cut count; path = source bar per slot."""
    nslots, K = gain.shape
    C = max_cuts + 1
    V = np.full((C, R, K), NEG)
    V[0, init[1], init[0]] = gain[0, init[0]]
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


def _path_to_plan(path, bars, vd, L, first_start):
    """Source-bar-per-slot path -> segments. The first segment starts at first_start (free trim),
    later ones on bars; the last is trimmed to fill the video exactly."""
    runs = []
    for s, k in enumerate(path):
        if runs and k == runs[-1][1] + runs[-1][2]:
            runs[-1][2] += 1
        else:
            runs.append([s, k, 1])
    segs, dst = [], 0.0
    for i, (_s, k, n) in enumerate(runs):
        a = float(first_start) if i == 0 else float(bars[k])
        b = a + (vd - dst) if i == len(runs) - 1 else float(bars[k + n])
        a, b = round(a, 6), round(b, 6)
        segs.append({"src_start": a, "src_end": b, "dst_start": round(dst, 6)})
        dst = round(dst + (b - a), 6)
    return segs


def _valid(segs, vd, min_seg, sd):
    for i, sg in enumerate(segs):
        ln = sg["src_end"] - sg["src_start"]
        if ln < min_seg - 1e-6 and len(segs) > 1:
            return False
        # only the FIRST segment may start before 0 (lead silence, allow_negative plans, down to -3 s)
        if sg["src_end"] > sd + 1e-6 or sg["src_start"] < (-3.0 - 1e-6 if i == 0 else -1e-9):
            return False
    last = segs[-1]
    return abs(last["dst_start"] + last["src_end"] - last["src_start"] - vd) < 1e-5


def _cuts(segs):
    return [{"dst_time": b["dst_start"], "src_from": a["src_end"], "src_to": b["src_start"]}
            for a, b in zip(segs[:-1], segs[1:])]


# ------------------------------------------------------------------- API
def _strongest_peak(video):
    ve = np.asarray(video["energy_curve"], dtype=float)
    hz = float(video.get("energy_hz", S.ENERGY_HZ))
    best, bv = None, -1.0
    for a, b in video.get("peak_segments", []):
        seg = ve[int(a * hz):max(int(b * hz), int(a * hz) + 1)]
        v = float(seg.mean()) if len(seg) else 0.0
        if v > bv + 1e-9:
            best, bv = float(a), v
    return best


def _empty(reason, phase):
    return {"eligible": False, "reason": reason, "segments": [], "cuts": [], "total_pct": 0, "tempo_pct": 0.0,
            "sync_pct": 0.0, "arc_pct": 0.0, "objective": 0.0, "bar_phase": phase, "lead_silence_s": 0.0,
            "baseline": {"offset": 0.0, "total_pct": 0}, "improvement_pct": 0}


def on_video_window(song: dict, video: dict, lead_s, plan_fn) -> dict:
    """F2b: plan a song shorter than the video on the video stretch of its length that score.score picks.
    plan_fn(clip) plans on that stretch (score.clip_video, times rebased to 0); the result gets
    video_window = score.video_use(start_s, end_s, video duration), the contract render.video_warp and the
    UI already read (times in the plan are clip times)."""
    sd, vd = float(song["duration"]), float(video["duration"])
    pick = S.score(song, video, lead_s=lead_s)
    w = float((pick.get("video_use") or {}).get("start_s") or 0.0)
    end = min(vd, w + sd) - 1e-6
    clip = S.clip_video(video, w, end)
    clip["duration"] = min(float(clip["duration"]), sd)   # rounding can never make the clip longer than the song
    res = plan_fn(clip)
    res["video_window"] = S.video_use(w, end, vd)
    return res


def plan_splice(song: dict, video: dict, max_cuts: int, min_segment_s: float = 2.0,
                allow_negative: bool = False, lead_s: float = None, *, should_cancel=None) -> dict:
    """Plan: {"eligible", "segments": [{"src_start","src_end","dst_start"}], "cuts", scores...,
    "lead_silence_s"}.

    PLAN FORMAT (negative offsets): with allow_negative the baseline offset may be down to -3 s
    (song starts after the video). Then segments[0]["src_start"] is negative and
    plan["lead_silence_s"] = -src_start is the silence the renderer must pad before the song
    (output 0..lead_silence_s is silent; the song plays from 0 at output time lead_silence_s).
    lead_silence_s is 0.0 for ordinary plans."""
    vd, sd = float(video["duration"]), float(song["duration"])
    if 0.0 < sd < vd:
        return on_video_window(song, video, lead_s, lambda clip: plan_splice(
            song, clip, max_cuts, min_segment_s, allow_negative, lead_s, should_cancel=should_cancel))
    phase = _bar_phase(song)
    base = S.score(song, video, allow_negative=allow_negative, lead_s=lead_s)
    if not base.get("eligible", True):
        return _empty(base.get("reason") or "unreadable", phase)
    off = float(base["best_offset_s"])
    base_segs = _single(off, vd)
    base_total = int(base["total_pct"])
    base_raw = _raw_total(base)   # accept/reject on the pre-bonus total: the build bonus is not comparable across layouts
    base_res = {k: base[k] for k in ("total_pct", "tempo_pct", "sync_pct", "arc_pct")}
    base_obj = _eval(song, video, base_segs, 0, lead_s)["obj"]

    def pack(segs, r, obj):
        return {"eligible": True, "segments": segs, "cuts": _cuts(segs), "total_pct": r["total_pct"],
                "tempo_pct": r["tempo_pct"], "sync_pct": r["sync_pct"], "arc_pct": r["arc_pct"],
                "objective": round(obj, 3), "bar_phase": phase,
                "baseline": {"offset": off, "total_pct": base_total},
                "improvement_pct": int(round(_raw_total(r))) - int(round(base_raw)),   # sync gain, bonus excluded
                "lead_silence_s": lead_silence(segs)}

    best = (base_segs, base_res, base_obj)
    bars, bt = _bars(song, phase)
    if max_cuts <= 0 or bars is None:
        return pack(*best)
    K = len(bars) - 1
    L = float(np.median(np.diff(bars)))
    R = max(1, int(math.ceil(min_segment_s / L - 1e-9)))

    # candidate free first trims: baseline offset + each drop aligned to the strongest peak.
    # The first segment only has to fit (>= min_segment_s), NOT the whole video: later cuts jump back
    # (repeat the drop), so o + video duration may exceed the song length.
    cands = [off]
    pk = _strongest_peak(video)
    if pk is not None:
        for d in song.get("drops", []):
            o = round(round((d - pk - S._lead(lead_s)) / 0.05) * 0.05, 2)
            if 0 <= o <= sd - min_segment_s and all(abs(o - c) > 1e-6 for c in cands):
                cands.append(o)
    for o in cands:
        if should_cancel is not None and should_cancel():
            raise Cancelled()
        k0 = int(np.searchsorted(bars, o - 1e-6))
        if k0 >= K:
            continue
        g = float(bars[k0] - o)
        nslots = max(1, int(math.ceil((vd - g) / L - 1e-9)))
        if nslots < 2:
            continue
        gain = _gains(song, video, bars, bt, L, g, nslots, lead_s)
        r0 = R - 1 if g + L >= min_segment_s - 1e-9 else min(R - 1, int(math.ceil((g + L) / L - 1e-9)) - 1)
        plans = _dp(gain, int(max_cuts), R, lambda s, g=g: vd - (g + s * L) >= min_segment_s - 1e-9, (k0, r0))
        for _v, c, path in plans:
            if should_cancel is not None and should_cancel():
                raise Cancelled()
            segs = _path_to_plan(path, bars, vd, L, o)
            if len(segs) - 1 > max_cuts or not _valid(segs, vd, min_segment_s, sd):
                continue
            e = _eval(song, video, segs, len(segs) - 1, lead_s)
            if e["obj"] > best[2] and _raw_total(e["r"]) >= base_raw - 1e-9:
                best = (segs, e["r"], e["obj"])
        # the free-trim single segment (no cuts) is also a candidate
        e = _eval(song, video, _single(o, vd), 0, lead_s)
        if e["obj"] > best[2] and _raw_total(e["r"]) >= base_raw - 1e-9:
            best = (_single(o, vd), e["r"], e["obj"])
    return pack(*best)
