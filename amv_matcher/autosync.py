"""auto_sync: iterative optimizer (offset -> tempo -> splice -> retime -> micro nudges).

Pure module (no I/O). Every step is re-scored with score.py's functions on the transformed
timelines and kept only if it helps by >= min_gain.
"""
from __future__ import annotations

import time

import numpy as np

from . import score as S
from .retime import apply_warp, plan_retime
from .splice import _assemble, plan_splice
from .tempo import apply_tempo, plan_tempo


def _tot(r):
    return S._combine(r["tempo_pct"], r["sync_pct"], r["arc_pct"])


def _best_offset(song, video, center=None, half=None, step=0.05):
    """Best offset by sync+arc (same rule as score.score); full scan or a window around center."""
    vd, sd = float(video["duration"]), float(song["duration"])
    hi = max(0.0, sd - vd)
    if center is None:
        offs = np.arange(0.0, hi + 1e-9, max(0.1, round(hi / S.MAX_OFFSET_STEPS, 1))) if hi > 0 else np.array([0.0])
    else:
        offs = np.arange(max(0.0, center - half), min(hi, center + half) + 1e-9, step)
        if len(offs) == 0:
            offs = np.array([min(max(center, 0.0), hi)])
    ev_t, ev_w = S._events(video)
    ok = len(ev_t) > 0 and len(song["beat_times"]) >= 2
    best = None
    for o in offs:
        o = float(o)
        sy = S._sync_pct_at(song, ev_t, ev_w, vd, o) if ok else 0.0
        c = S.W_SYNC * sy + S.W_ARC * S._arc_pct_at(song, video, o)
        if best is None or c > best[0]:
            best = (c, o)
    return best[1]


def _nudge(song, video, offset, max_shift):
    """Move cuts/peaks by at most max_shift s (one frame) when that brings them inside the tolerance."""
    beats = np.asarray(song["beat_times"], dtype=float)
    if len(beats) < 2:
        return video, []
    nudges, new = [], {}
    for key in ("cut_times", "motion_peaks"):
        arr = np.asarray(video.get(key, []), dtype=float)
        res = arr.copy()
        for i, e in enumerate(arr):
            t = e + offset
            b = beats[int(np.argmin(np.abs(beats - t)))]
            d = b - t
            if S.TOLERANCE_S < abs(d) <= S.TOLERANCE_S + max_shift:
                delta = float(np.sign(d) * min(abs(d), max_shift))
                res[i] = e + delta
                nudges.append({"kind": key, "t": round(float(e), 4), "delta": round(delta, 4)})
        new[key] = res.tolist()
    return dict(video, **new), nudges


def auto_sync(song: dict, video: dict, target: float = 100, budget_s: float = 20.0,
              max_speed_dev: float = 0.25, max_tempo_dev: float = 0.08, min_section_s: float = 0.75,
              max_cuts: int = 3, min_gain: float = 0.5, max_passes: int = 2, allow_splice: bool = True) -> dict:
    t0 = time.perf_counter()
    steps, limits = [], []
    vd = float(video["duration"])
    fps = float(video.get("fps", 30) or 30)

    def left():
        return budget_s - (time.perf_counter() - t0)

    def rec(tool, params, before, after, kept):
        steps.append({"tool": tool, "params": params, "score_before": round(before, 2),
                      "score_after": round(after, 2), "kept": bool(kept)})

    # state
    cur_song, cur_video, offset = song, video, 0.0
    tempo_ratio, segs, retime, nudges = 1.0, None, None, []
    r0 = S.score_at(cur_song, cur_video, 0.0)
    cur = r0
    # 1. best offset (always kept: offset 0 is one of the candidates)
    if float(song["duration"]) >= vd:
        offset = _best_offset(song, video)
    else:
        limits.append("song_shorter_than_video")
    cur = S.score_at(cur_song, cur_video, offset)
    rec("offset", {"offset": round(offset, 2)}, _tot(r0), _tot(cur), True)

    for ps in range(max_passes):
        start_total = _tot(cur)
        if _tot(cur) >= target - 1e-9 or left() <= 0:
            break
        # 2. tempo stretch (applied to the original song; replaces a previous tempo)
        if segs is None and retime is None and left() > 0:
            base_song = song
            pt = plan_tempo(base_song, cur_video, max_tempo_dev)
            ratios = sorted({round(x, 3) for x in np.arange(1 - max_tempo_dev, 1 + max_tempo_dev + 1e-9, 0.01)}
                            | {pt["ratio"]})
            best = (_tot(cur), 1.0, cur_song, offset)
            for rt in ratios:
                if abs(rt - 1.0) < 1e-9 or left() <= 0:
                    continue
                st = apply_tempo(base_song, rt)
                o2 = _best_offset(st, cur_video, center=offset / rt, half=1.0, step=0.05)
                sc = _tot(S.score_at(st, cur_video, o2))
                if sc > best[0]:
                    best = (sc, rt, st, o2)
            if best[1] != 1.0:
                st = best[2]
                o3 = _best_offset(st, cur_video, center=best[3], half=0.06, step=0.01)
                sc3 = _tot(S.score_at(st, cur_video, o3))
                if sc3 >= best[0]:
                    best = (sc3, best[1], st, o3)
            kept = best[0] >= _tot(cur) + min_gain
            rec("tempo", {"ratio": best[1], "bpm_from": pt["bpm_from"], "bpm_to": round(song["bpm"] * best[1], 2)},
                _tot(cur), best[0] if kept else _tot(cur), kept)
            if kept:
                tempo_ratio, cur_song, offset = best[1], best[2], best[3]
                cur = S.score_at(cur_song, cur_video, offset)
            if abs(best[1]) >= 1 + max_tempo_dev - 1e-9 or abs(best[1]) <= 1 - max_tempo_dev + 1e-9:
                limits.append("max_tempo_dev")
        if _tot(cur) >= target - 1e-9:
            break
        # 3. splice
        if allow_splice and segs is None and left() > 2.0 and float(cur_song["duration"]) >= vd:
            sp = plan_splice(cur_song, video, max_cuts)
            if sp.get("eligible") and len(sp["segments"]) > 1:
                asm = _assemble(cur_song, sp["segments"], vd)
                asm_r = S.score_at(asm, cur_video, 0.0)
                kept = _tot(asm_r) >= _tot(cur) + min_gain
                rec("splice", {"max_cuts": max_cuts, "cuts": sp["cuts"]}, _tot(cur),
                    _tot(asm_r) if kept else _tot(cur), kept)
                if kept:
                    segs, cur_song, offset, cur = sp["segments"], asm, 0.0, asm_r
        if _tot(cur) >= target - 1e-9:
            break
        # 4. video section retime
        if retime is None and left() > 0:
            rp = plan_retime(cur_song, video, offset, max_speed_dev, min_section_s)
            wv = apply_warp(video, rp["warp"])
            rr = S.score_at(cur_song, wv, offset)
            kept = bool(rp["sections"]) and _tot(rr) >= _tot(cur) + min_gain
            rec("retime", {"sections": len(rp["sections"]), "max_speed_dev": max_speed_dev},
                _tot(cur), _tot(rr) if kept else _tot(cur), kept)
            if kept:
                retime, cur_video, cur = rp, wv, rr
        # 5. micro-pass: nudge events by at most one frame
        if not nudges and left() > 0:
            nv, nl = _nudge(cur_song, cur_video, offset, 1.0 / fps)
            nr = S.score_at(cur_song, nv, offset)
            kept = bool(nl) and _tot(nr) >= _tot(cur) + min_gain
            rec("micro", {"nudges": len(nl), "max_shift_s": round(1.0 / fps, 4)}, _tot(cur),
                _tot(nr) if kept else _tot(cur), kept)
            if kept:
                nudges, cur_video, cur = nl, nv, nr
        if _tot(cur) < start_total + min_gain:
            break

    if left() <= 0 and _tot(cur) < target:
        limits.append("budget_s")
    if retime and any(abs(s["speed"] - 1) >= max_speed_dev - 0.01 for s in retime["sections"]):
        limits.append("max_speed_dev")
    if retime and not retime.get("speed_jump_ok", True):
        limits.append("speed_jump_over_0.15")
    final = {k: cur[k] for k in ("total_pct", "tempo_pct", "sync_pct", "arc_pct")}
    final["total_pct"] = int(round(_tot(cur)))
    return {"steps": steps, "final": final,
            "plan": {"offset": round(offset, 3), "tempo_ratio": tempo_ratio, "segments": segs,
                     "retime": retime, "nudges": nudges},
            "limits_hit": sorted(set(limits)), "why_not_100": _why(final, target, limits),
            "elapsed_s": round(time.perf_counter() - t0, 2)}


def _why(final, target, limits):
    if final["total_pct"] >= target:
        return "Target reached."
    parts = []
    if final["arc_pct"] < 95:
        parts.append("the song's energy arc (drops, lulls, loud/quiet shape) does not match the video's "
                     "(%.0f%% arc), and tempo, splice and retime cannot reshape the energy" % final["arc_pct"])
    if final["sync_pct"] < 95:
        why = "speed limits stop some cuts/peaks reaching a beat" if "max_speed_dev" in limits else \
            "some cuts/peaks have no reachable beat within the guardrails"
        parts.append("%.0f%% sync: %s" % (final["sync_pct"], why))
    if final["tempo_pct"] < 95:
        parts.append("tempo is %.0f%%: BPM gap exceeds the allowed stretch" % final["tempo_pct"])
    return "100 is unreachable because " + "; ".join(parts) + "." if parts else \
        "Remaining gap is under 5% per component (rounding and tolerance)."
