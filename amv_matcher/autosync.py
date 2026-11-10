"""auto_sync: iterative optimizer (offset -> tempo -> splice -> retime -> micro nudges).

Pure module (no I/O). Every step is re-scored with score.py's functions on the transformed
timelines and kept only if it helps by >= min_gain.
"""
from __future__ import annotations

import time

import numpy as np

from . import score as S
from .retime import apply_warp, plan_retime
from .splice import _assemble, lead_silence, plan_splice


from .tempo import apply_tempo, plan_tempo

# ---- effort: how much the plan had to bend the material to reach its score ----
# effort_pct = min(100, EFFORT_W_RETIME * retimed_fraction * min(1, mean_abs_speed_dev / EFFORT_DEV_REF)
#                       + EFFORT_W_SPLICE * min(1, splices / EFFORT_SPLICE_REF)
#                       + EFFORT_W_TEMPO * min(1, tempo_stretch_pct / EFFORT_TEMPO_REF))
# Monotone in every input; 0 only for an untouched plan (no retime, no splice, no tempo stretch).
EFFORT_W_RETIME = 55.0    # points for retiming the whole video at the reference speed deviation
EFFORT_DEV_REF = 0.15     # mean |speed-1| that counts as full retime effort
EFFORT_W_SPLICE = 25.0
EFFORT_SPLICE_REF = 3     # splices (= segments - 1) that count as full splice effort
EFFORT_W_TEMPO = 20.0
EFFORT_TEMPO_REF = 8.0    # tempo stretch % that counts as full tempo effort (the +-8% guardrail)
# fit_after_effort = total - EFFORT_K * effort_pct: the ranking key that keeps naturally close songs
# above songs that only "fit" after heavy retiming. Calibrated on the real suite (docs/real-suite-latest.md).
EFFORT_K = 1.0            # any k > 0.48 separates the 6 real pairs (good above cross); 1.0 leaves a ~3.3-pt margin
LENGTH_SLACK_S = 0.5      # a song up to this much shorter than the video still counts as covering it (own audio is a few ms short)
SPEED_EPS = 0.01          # |speed-1| at or below this counts as not retimed


def effort_of(plan: dict, video_duration: float) -> dict:
    """final.effort block + effort_pct for a plan (see the formula above)."""
    rt = (plan or {}).get("retime") or {}
    secs = [s for s in rt.get("sections", []) if abs(float(s["speed"]) - 1.0) > SPEED_EPS]
    vd = max(float(video_duration), 1e-9)
    span = sum(float(s["v_end"]) - float(s["v_start"]) for s in secs)
    frac = min(1.0, span / vd)
    devs = [abs(float(s["speed"]) - 1.0) * (float(s["v_end"]) - float(s["v_start"])) for s in secs]
    mean_dev = sum(devs) / span if span > 0 else 0.0
    max_dev = max((abs(float(s["speed"]) - 1.0) for s in secs), default=0.0)
    segs = (plan or {}).get("segments") or []
    splices = max(0, len(segs) - 1)
    stretch = abs(float((plan or {}).get("tempo_ratio", 1.0)) - 1.0) * 100.0
    pct = EFFORT_W_RETIME * frac * min(1.0, mean_dev / EFFORT_DEV_REF) \
        + EFFORT_W_SPLICE * min(1.0, splices / EFFORT_SPLICE_REF) \
        + EFFORT_W_TEMPO * min(1.0, stretch / EFFORT_TEMPO_REF)
    return {"sections": len(secs), "mean_abs_speed_dev": round(mean_dev, 4), "max_speed_dev": round(max_dev, 4),
            "retimed_fraction": round(frac, 4), "splices": splices, "tempo_stretch_pct": round(stretch, 3),
            "effort_pct": round(min(100.0, pct), 1)}


def _tot(r):
    return S._combine(r["tempo_pct"], r["sync_pct"], r["arc_pct"])


# Lexicographic objective: total first, then tightness.tight_pct (within +-40 ms), then a lower
# median_err_ms. score.py's tightness block is read defensively (.get), so this also works on a
# scorer that does not emit it (every tightness term is then 0 / unknown and only the total counts).
TIGHT_GAIN = 1.0         # tight_pct points that justify a step when the total does not drop
MEDIAN_GAIN_MS = 5.0     # median error (ms) that justifies a step at equal tight_pct


def _tight(r):
    return (r.get("tightness") or {}) if isinstance(r, dict) else {}


def _tp(r):
    return float(_tight(r).get("tight_pct") or 0.0)


def _me(r):
    m = _tight(r).get("median_err_ms")
    return float(m) if m is not None else 1e9


def _has_tight(r):
    return bool(_tight(r).get("n_events"))


def _done(r, target):
    """Target met on the total and, when the scorer measures tightness, on tight_pct as well."""
    return _tot(r) >= target - 1e-9 and (not _has_tight(r) or _tp(r) >= target - 1e-9)


def _gain(new, old, min_gain):
    """`new` beats `old`: total up by min_gain; or total not lower and tighter (tight_pct up by
    TIGHT_GAIN, or equal tight_pct and the median error down by MEDIAN_GAIN_MS)."""
    dt = _tot(new) - _tot(old)
    if dt >= min_gain:
        return True
    if dt < -1e-9:
        return False
    return _tp(new) >= _tp(old) + TIGHT_GAIN or \
        (_tp(new) >= _tp(old) and _me(new) <= _me(old) - MEDIAN_GAIN_MS)


def _rank(r):
    """Sort key for choosing between whole runs: displayed total, then tightness, then median error."""
    return (int(round(_tot(r))), _tp(r), -_me(r), _tot(r))


def _tvals(r):
    t = _tight(r)
    return {"tight_pct": t.get("tight_pct"), "median_err_ms": t.get("median_err_ms")}


def _fast(d):
    """Same dict with array-valued curves, so score.py's np.asarray calls are free in scan loops."""
    return dict(d, **{k: np.asarray(d[k], dtype=float) for k in ("energy_curve", "beat_times") if k in d})


def _best_offset(song, video, center=None, half=None, step=0.05, lead_s=None):
    """Best offset by sync+arc (same rule as score.score); full scan or a window around center."""
    song, video = _fast(song), _fast(video)
    vd, sd = float(video["duration"]), float(song["duration"])
    lo_lim, hi = S.MIN_OFFSET_S, max(0.0, sd - vd)       # offsets may be negative (song starts late)
    if center is None:
        offs = np.arange(lo_lim, hi + 1e-9, max(0.1, round((hi - lo_lim) / S.MAX_OFFSET_STEPS, 1)))
    else:
        offs = np.arange(max(lo_lim, center - half), min(hi, center + half) + 1e-9, step)
        if len(offs) == 0:
            offs = np.array([min(max(center, lo_lim), hi)])
    ev_t, ev_w = S._events(video)
    ok = len(ev_t) > 0 and len(song["beat_times"]) >= 2
    best = None
    for o in offs:
        o = float(o)
        sy = S._sync_pct_at(song, ev_t, ev_w, vd, o, lead_s) if ok else 0.0
        c = S.W_SYNC * sy + S.W_ARC * S._arc_pct_at(song, video, o)
        if best is None or c > best[0]:
            best = (c, o)
    return best[1]


def _nudge(song, video, offset, max_shift, lead=0.0):
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
            b = beats[int(np.argmin(np.abs(beats - lead - t)))] - lead
            d = b - t
            if 0.010 < abs(d) <= getattr(S, "TIGHT_TOL_S", S.TOLERANCE_S) + max_shift:
                delta = float(np.sign(d) * min(abs(d), max_shift))
                res[i] = e + delta
                nudges.append({"kind": key, "t": round(float(e), 4), "delta": round(delta, 4)})
        new[key] = res.tolist()
    return dict(video, **new), nudges


ORDERS = {
    "default": ("tempo", "splice", "retime", "micro"),
    "retime_first": ("retime", "tempo", "splice", "retime", "micro"),
    "splice_first": ("splice", "retime", "micro"),
    "retime_only": ("retime", "micro"),
    "tempo_only": ("tempo", "micro"),
}
ORDER_COST = ("tempo_only", "retime_only", "splice_first", "default", "retime_first")   # cheapest first


def _run(song, video, order, target, budget_s, max_speed_dev, max_tempo_dev, min_section_s, max_cuts,
         min_gain, max_passes, allow_splice, lead_s=0.0, loop_segs=None):
    t0 = time.perf_counter()
    steps, limits = [], []
    vd = float(video["duration"])
    fps = float(video.get("fps", 30) or 30)

    def left():
        return budget_s - (time.perf_counter() - t0)

    def rec(tool, params, before, after, kept, rb=None, ra=None):
        """rb/ra: score results before/after (tightness is reported from them; ra only when kept)."""
        steps.append({"tool": tool, "params": params, "score_before": round(before, 2),
                      "score_after": round(after, 2), "kept": bool(kept),
                      "tightness_before": _tvals(rb) if rb is not None else None,
                      "tightness_after": _tvals(ra if (kept and ra is not None) else rb) if rb is not None else None})

    cur_song, offset = song, 0.0
    cur_video = video                       # original video + retime (nudges are applied on top)
    tempo_ratio, segs, retime, nudges = 1.0, None, None, []
    loop_segs_given = loop_segs
    r0 = S.score_at(song, video, 0.0, lead_s=lead_s)
    if loop_segs_given:                     # looped song: segments fixed, offset locked at 0, no tempo/splice
        segs = loop_segs_given
    elif float(song["duration"]) >= vd:
        offset = _best_offset(song, video, lead_s=lead_s)
    else:
        limits.append("song_shorter_than_video")
    cur = S.score_at(cur_song, cur_video, offset, lead_s=lead_s)
    rec("offset", {"offset": round(offset, 2)}, _tot(r0), _tot(cur), True, r0, cur)

    def src_video():                        # video the song-side tools see (retimed, no nudges)
        return apply_warp(video, retime["warp"]) if retime else video

    for ps in range(max_passes):
        start_cur = cur
        for tool in order:
            if _done(cur, target) or left() <= 0:
                break
            sv = src_video()
            svd = float(sv["duration"])
            if tool == "tempo" and segs is None:
                pt = plan_tempo(song, sv, max_tempo_dev)
                ratios = sorted({round(x, 3) for x in np.arange(1 - max_tempo_dev, 1 + max_tempo_dev + 1e-9, 0.01)}
                                | {pt["ratio"]})
                best = (_tot(cur), tempo_ratio, cur_song, offset)
                for rt in ratios:
                    if abs(rt - tempo_ratio) < 1e-9 or left() <= 0:
                        continue
                    st = apply_tempo(song, rt) if abs(rt - 1.0) > 1e-9 else song
                    o2 = _best_offset(st, sv, center=offset * tempo_ratio / rt, half=1.0, step=0.05, lead_s=lead_s)
                    sc = _tot(S.score_at(st, sv, o2, lead_s=lead_s))
                    if sc > best[0]:
                        best = (sc, rt, st, o2)
                if best[1] != tempo_ratio:
                    st = best[2]
                    o3 = _best_offset(st, sv, center=best[3], half=0.06, step=0.01, lead_s=lead_s)
                    sc3 = _tot(S.score_at(st, sv, o3, lead_s=lead_s))
                    if sc3 >= best[0]:
                        best = (sc3, best[1], st, o3)
                kept = best[0] >= _tot(cur) + min_gain
                newr = S.score_at(best[2], sv, best[3], lead_s=lead_s) if kept else cur
                rec("tempo", {"ratio": best[1], "bpm_from": pt["bpm_from"], "bpm_to": round(song["bpm"] * best[1], 2)},
                    _tot(cur), best[0] if kept else _tot(cur), kept, cur, newr)
                if kept:
                    tempo_ratio, cur_song, offset = best[1], best[2], best[3]
                    nudges, cur_video = [], sv
                    cur = newr
                if abs(best[1] - 1.0) >= max_tempo_dev - 1e-9:
                    limits.append("max_tempo_dev")
            elif tool == "splice" and allow_splice and segs is None and left() > 2.0 \
                    and float(cur_song["duration"]) >= svd:
                sp = plan_splice(cur_song, sv, max_cuts, allow_negative=True, lead_s=lead_s)
                if sp.get("eligible") and len(sp["segments"]) > 1:
                    asm = _assemble(cur_song, sp["segments"], svd)
                    asm_r = S.score_at(asm, sv, 0.0, lead_s=lead_s)
                    kept = _gain(asm_r, cur, min_gain)
                    rec("splice", {"max_cuts": max_cuts, "cuts": sp["cuts"]}, _tot(cur),
                        _tot(asm_r) if kept else _tot(cur), kept, cur, asm_r)
                    if kept:
                        segs, cur_song, offset, cur = sp["segments"], asm, 0.0, asm_r
                        nudges, cur_video = [], sv
            elif tool == "retime":
                # always planned from the ORIGINAL video against the current song/offset (replaces)
                rp = plan_retime(cur_song, video, offset, max_speed_dev, min_section_s, lead_s=lead_s)
                wv = apply_warp(video, rp["warp"])
                rr = S.score_at(cur_song, wv, offset, lead_s=lead_s)
                base = S.score_at(cur_song, sv, offset, lead_s=lead_s)       # current retime state, no nudges
                ref = base if (nudges or _tot(base) > _tot(cur)) else cur
                kept = bool(rp["sections"]) and rp["speed_jump_ok"] and _gain(rr, ref, min_gain)
                rec("retime", {"sections": len(rp["sections"]), "max_speed_dev": max_speed_dev},
                    _tot(cur), _tot(rr) if kept else _tot(cur), kept, cur, rr)
                if kept:
                    retime, cur_video, cur, nudges = rp, wv, rr, []
                    if segs is not None and allow_splice and left() > 2.0:
                        # events moved: re-splice against the retimed video (one extra pass)
                        sp = plan_splice(song if tempo_ratio == 1.0 else apply_tempo(song, tempo_ratio),
                                         wv, max_cuts, allow_negative=True, lead_s=lead_s)
                        if sp.get("eligible") and len(sp["segments"]) > 1:
                            base_s = song if tempo_ratio == 1.0 else apply_tempo(song, tempo_ratio)
                            asm = _assemble(base_s, sp["segments"], float(wv["duration"]))
                            ar = S.score_at(asm, wv, 0.0, lead_s=lead_s)
                            if _gain(ar, cur, min_gain):
                                rec("splice", {"max_cuts": max_cuts, "cuts": sp["cuts"], "after_retime": True},
                                    _tot(cur), _tot(ar), True, cur, ar)
                                segs, cur_song, offset, cur = sp["segments"], asm, 0.0, ar
            elif tool == "micro" and not nudges:
                nv, nl = _nudge(cur_song, cur_video, offset, 1.0 / fps, lead_s)
                nr = S.score_at(cur_song, nv, offset, lead_s=lead_s)
                kept = bool(nl) and _gain(nr, cur, min_gain)
                rec("micro", {"nudges": len(nl), "max_shift_s": round(1.0 / fps, 4)}, _tot(cur),
                    _tot(nr) if kept else _tot(cur), kept, cur, nr)
                if kept:
                    nudges, cur_video, cur = nl, nv, nr
        if _done(cur, target) or not _gain(cur, start_cur, min_gain):
            break
    if left() <= 0 and not _done(cur, target):
        limits.append("budget")
    if retime and any(abs(s["speed"] - 1) >= max_speed_dev - 0.01 for s in retime["sections"]):
        limits.append("max_speed_dev")
    if retime and not retime.get("speed_jump_ok", True):
        limits.append("speed_jump_over_0.15")
    out_dur = float(cur_video["duration"])
    return {"steps": steps, "cur": cur, "limits": limits, "out_duration": out_dur,
            "plan": {"offset": round(offset, 3), "lead_s": lead_s, "tempo_ratio": tempo_ratio, "segments": segs,
                     "retime": retime, "nudges": nudges,
                     "lead_silence_s": lead_silence(segs) if segs else round(max(0.0, -offset), 6)}}


def auto_sync(song: dict, video: dict, target: float = 100, budget_s: float = 20.0,
              max_speed_dev: float = 0.25, max_tempo_dev: float = 0.08, min_section_s: float = 0.75,
              max_cuts: int = 3, min_gain: float = 0.5, max_passes: int = 2, allow_splice: bool = True, lead_s: float | None = None,
              allow_loop: bool = False) -> dict:
    """Try several tool orders (each from the baseline) and keep the best path, so the result is never
    worse than any single tool applied directly.

    Length rule: a song shorter than the video is INELIGIBLE (same as score.score): the result has
    eligible False, total 0, reason 'song shorter than video', and nothing is retimed or filled.
    allow_loop=True (default False) lifts that: the song is repeated end to end (as plan.segments,
    crossfaded by the renderer) until it covers the video and its retime headroom; plan.looped is True
    and plan.loop = {song_duration_s, repeats}. A looped plan uses retime + micro nudges only (offset
    locked at 0, no tempo stretch, no splice), so the segments stay valid."""
    lead_s = S.PERCEPTUAL_LEAD_S if lead_s is None else lead_s
    lead_s = min(0.12, max(0.0, float(lead_s)))   # cuts/peaks land lead_s before the beat (0 = on it)
    t0 = time.perf_counter()
    loop_info, loop_segs = None, None
    if float(song["duration"]) < float(video["duration"]) - LENGTH_SLACK_S:
        if not allow_loop:
            return _ineligible(song, video, lead_s)
        sd, vd = float(song["duration"]), float(video["duration"])
        cover = vd / max(0.05, 1.0 - max_speed_dev) + 1.0       # enough for the slowest allowed retime
        n = int(np.ceil(cover / max(sd, 1e-6)))
        loop_segs = [{"src_start": 0.0, "src_end": min(sd, cover - k * sd), "dst_start": k * sd}
                     for k in range(n)]
        loop_info = {"song_duration_s": round(sd, 3), "repeats": n}
        song = _assemble(song, loop_segs, n * sd)
        allow_splice = False
    names = list(ORDER_COST)             # cheapest first; later (dearer) orders are pruned on budget
    best, pruned = None, False
    for i, name in enumerate(names):
        rem = budget_s - (time.perf_counter() - t0)
        if rem <= 0 and best:
            pruned = True
            break
        run = _run(song, video, ORDERS[name], target, max(rem / max(1, len(names) - i), 2.0), max_speed_dev,
                   max_tempo_dev, min_section_s, max_cuts, min_gain, max_passes, allow_splice, lead_s,
                   loop_segs=loop_segs)
        run["path"] = name
        if best is None or _rank(run["cur"]) > _rank(best["cur"]):
            best = run
        if _done(best["cur"], target):
            break
    cur, limits = best["cur"], list(best["limits"])
    if pruned:
        limits.append("budget")
    final = {k: cur[k] for k in ("total_pct", "tempo_pct", "sync_pct", "arc_pct")}
    final["total_pct"] = int(round(_tot(cur)))
    final["tightness"] = _tight(cur) or None
    final["tight_pct"] = _tight(cur).get("tight_pct")
    final["median_err_ms"] = _tight(cur).get("median_err_ms")
    final["out_duration_s"] = round(float(best["out_duration"]), 3)
    final["video_duration_s"] = round(float(video["duration"]), 3)
    final["sync_headline"] = int(round(0.6 * cur["sync_pct"] + 0.4 * cur["tempo_pct"]))
    first = best["steps"][0] if best["steps"] else {}
    final["lead_s"] = lead_s
    final["tightness_start"] = first.get("tightness_before")
    ef = effort_of(best["plan"], video["duration"])
    final["eligible"] = True
    final["effort"] = {k: v for k, v in ef.items() if k != "effort_pct"}
    final["effort_pct"] = ef["effort_pct"]
    final["fit_after_effort"] = round(max(0.0, final["total_pct"] - EFFORT_K * ef["effort_pct"]), 1)
    if loop_info:
        best["plan"]["looped"], best["plan"]["loop"] = True, loop_info
    else:
        best["plan"]["looped"] = False
    return {"steps": best["steps"], "final": final, "eligible": True, "plan": best["plan"], "path": best["path"],
            "limits_hit": sorted(set(limits)), "pruned": pruned, "why_not_100": _why(final, target, limits),
            "elapsed_s": round(time.perf_counter() - t0, 2)}


def _ineligible(song, video, lead_s):
    sd, vd = float(song["duration"]), float(video["duration"])
    final = {"eligible": False, "total_pct": 0, "tempo_pct": 0.0, "sync_pct": 0.0, "arc_pct": 0.0,
             "tightness": None, "tight_pct": None, "median_err_ms": None, "out_duration_s": round(vd, 3),
             "video_duration_s": round(vd, 3), "sync_headline": 0, "lead_s": lead_s, "tightness_start": None,
             "effort": effort_of(None, vd), "effort_pct": 0.0, "fit_after_effort": 0.0,
             "reason": "song shorter than video", "song_duration_s": round(sd, 3)}
    final["effort"].pop("effort_pct")
    return {"steps": [], "final": final, "eligible": False, "reason": "song shorter than video",
            "plan": None, "path": None, "limits_hit": ["song_shorter_than_video"], "pruned": False,
            "why_not_100": "Song is shorter than the video, so it is ineligible (pass allow_loop=True to repeat it).",
            "elapsed_s": 0.0}


def _why(final, target, limits):
    tt = final.get("tightness") or {}
    tight_gap = bool(tt.get("n_events")) and (tt.get("tight_pct") or 0.0) < target - 1e-9
    if final["total_pct"] >= target and not tight_gap:
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
    if tight_gap and (tt.get("tight_pct") or 0.0) < 95:
        parts.append("tightness is %.0f%%: only that share of cuts/peaks sit within +-40 ms of the beat "
                     "(median error %s ms), and the speed, tempo and splice guardrails stop closer alignment"
                     % (tt.get("tight_pct") or 0.0, tt.get("median_err_ms")))
    return "100 is unreachable because " + "; ".join(parts) + "." if parts else \
        "Remaining gap is under 5% per component (rounding and tolerance)."
