"""auto_sync: iterative optimizer (offset -> tempo -> splice -> retime -> micro nudges).

Pure module (no I/O). Every step is re-scored with score.py's functions on the transformed
timelines and kept only if it helps by >= min_gain.
"""
from __future__ import annotations

import time

import numpy as np

from . import score as S
from . import stemsync
from .retime import Cancelled, apply_warp, plan_retime
from .arrange import plan_arrangement
from .splice import _assemble, lead_silence, plan_splice


from .tempo import apply_tempo, plan_tempo

# ---- effort: how much the plan deviates from the untouched material ----
# effort_pct = min(100, 100 * (EFFORT_W_REARRANGE * rearranged_fraction
#                              + EFFORT_W_RETIME * retimed_fraction * (mean_abs_speed_dev / max_speed_dev)
#                              + EFFORT_W_TEMPO * (|tempo_ratio - 1| / max_tempo_dev)))   with the weights summing to 1.
#   rearranged_fraction = share of the OUTPUT duration whose source differs from the plain trim (arrange and
#     splice both land in plan.segments; the plain trim is the source shift that covers the most output time).
#   mean_abs_speed_dev  = duration-weighted mean |speed-1| over the retimed sections (|speed-1| > SPEED_EPS).
# Every term is clamped to 0..1 and the formula is monotone in each; 0 only for an untouched plan.
EFFORT_W_REARRANGE = 0.45
EFFORT_W_RETIME = 0.35
EFFORT_W_TEMPO = 0.20
SHIFT_EPS = 0.02          # source shifts closer than this (s) count as the same trim
# fit_after_effort = total - EFFORT_K * effort_pct: information only (ranking uses the natural total).
EFFORT_K = 1.0
LENGTH_SLACK_S = 0.5      # a song up to this much shorter than the video still counts as covering it (own audio is a few ms short)
SPEED_EPS = 0.01          # |speed-1| at or below this counts as not retimed


def rearranged_fraction(segments) -> float:
    """Share of the output whose source differs from the plain trim (the dominant source shift)."""
    segs = segments or []
    if len(segs) <= 1:
        return 0.0
    items = []
    for g in segs:
        d = float(g["src_end"]) - float(g["src_start"])
        if d > 0:
            items.append((float(g["src_start"]) - float(g["dst_start"]), d))
    total = sum(d for _, d in items)
    if total <= 0:
        return 0.0
    best = max(sum(d for sh2, d in items if abs(sh2 - sh) <= SHIFT_EPS) for sh, _ in items)
    return min(1.0, max(0.0, 1.0 - best / total))


def effort_of(plan: dict, video_duration: float, max_speed_dev: float = 0.25, max_tempo_dev: float = 0.10) -> dict:
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
    rfrac = rearranged_fraction(segs)
    stretch = abs(float((plan or {}).get("tempo_ratio", 1.0)) - 1.0) * 100.0
    t_retime = frac * min(1.0, mean_dev / max(max_speed_dev, 1e-9))
    t_tempo = min(1.0, stretch / 100.0 / max(max_tempo_dev, 1e-9))
    pct = 100.0 * (EFFORT_W_REARRANGE * rfrac + EFFORT_W_RETIME * t_retime + EFFORT_W_TEMPO * t_tempo)
    return {"sections": len(secs), "mean_abs_speed_dev": round(mean_dev, 4), "max_speed_dev": round(max_dev, 4),
            "retimed_fraction": round(frac, 4), "splices": splices, "rearranged_fraction": round(rfrac, 4),
            "tempo_stretch_pct": round(stretch, 3), "effort_pct": round(min(100.0, pct), 1)}


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


def _alt_offsets(song, video, exclude, k=3, min_sep=0.5, lead_s=None):
    """Up to k other plausible offsets (best plain sync+arc first, >= min_sep s apart and from `exclude`,
    always also including offset 0): the offset that is best for the plain trim is not always the one a
    retime can improve, so the retime tool tries these too."""
    song, video = _fast(song), _fast(video)
    vd, sd = float(video["duration"]), float(song["duration"])
    lo_lim, hi = S.MIN_OFFSET_S, max(0.0, sd - vd)
    ev_t, ev_w = S._events(video)
    if len(ev_t) == 0 or len(song["beat_times"]) < 2:
        return []
    offs = np.arange(lo_lim, hi + 1e-9, 0.1)
    sc = [S.W_SYNC * S._sync_pct_at(song, ev_t, ev_w, vd, float(o), lead_s) + S.W_ARC * S._arc_pct_at(song, video, float(o))
          for o in offs]
    out = []
    taken = list(exclude)
    for i in np.argsort(sc)[::-1]:
        o = round(float(offs[i]), 3) + 0.0
        if all(abs(o - x) >= min_sep for x in taken):
            out.append(o); taken.append(o)
        if len(out) >= k:
            break
    if 0.0 >= lo_lim and all(abs(x) >= 1e-9 for x in out + list(exclude)) and hi >= 0.0:
        out.append(0.0)
    return out


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
    "default": ("tempo", "splice", "arrange", "retime", "micro"),
    "retime_first": ("retime", "tempo", "splice", "arrange", "retime", "micro"),
    "splice_first": ("splice", "arrange", "retime", "micro"),
    "retime_only": ("retime", "micro"),
    "tempo_only": ("tempo", "micro"),
}
ORDER_COST = ("tempo_only", "retime_only", "splice_first", "default", "retime_first")   # cheapest first


ARRANGE_MAX_MOVES = 3
# Deterministic budget: every step costs a fixed number of WORK UNITS (never wall-clock), so the search depth, and
# therefore the plan and the percentages, cannot depend on machine load. Wall-clock is only a hard safety cap
# (budget_s * TIME_CAP_MULT) that is REPORTED as limits_hit 'budget: time cap hit' when it ever fires.
WORK_COST = {"tempo": 5, "splice": 4, "arrange": 6, "retime": 3, "retime_alt": 1, "micro": 1}
DEFAULT_WORK_UNITS = 100          # > the cost of every order together (~60): by default nothing is pruned
RAMPS_MODE = "anchored"           # speedramp.plan_ramps mode used by the opt-in ramps hook
TIME_CAP_MULT = 3.0


class _Work:
    """Shared work-unit ledger of one auto_sync call (+ the wall-clock safety cap)."""

    def __init__(self, units, cap_s):
        self.units, self.used, self.cap_hit = float(units), 0.0, False
        self.cap_at = time.perf_counter() + float(cap_s)

    def timecap(self):
        if not self.cap_hit and time.perf_counter() > self.cap_at:
            self.cap_hit = True
        return self.cap_hit

    def left(self):
        return 0.0 if self.timecap() else self.units - self.used

    def exhausted(self):
        return self.left() <= 0

    def can(self, cost):
        return self.left() >= cost

    def spend(self, cost):
        self.used += cost


def _run(song, video, order, target, work, max_speed_dev, max_tempo_dev, min_section_s, max_cuts,
         min_gain, max_passes, allow_splice, lead_s=0.0, loop_segs=None, memo=None, lock_original=False,
         progress_cb=None, should_cancel=None, t_start=None, stem_targets=None, prefer_energy=False):
    t0 = time.perf_counter()
    t_start = t0 if t_start is None else t_start
    steps, limits = [], []
    vd = float(video["duration"])
    fps = float(video.get("fps", 30) or 30)

    def mm(key, objs, fn):                  # pure-function memo shared by all orders of one auto_sync call
        if memo is None:
            return fn()
        hit = memo.get(key)
        if hit is None:
            hit = memo[key] = (fn(), objs)  # objs keep the id()-keyed arguments alive
        return hit[0]

    stem_kw = {"stem_targets": stem_targets} if stem_targets is not None else {}   # None: call exactly as before
    xt = stemsync.target_times(stem_targets) if stem_targets is not None else None
    sid = id(stem_targets) if stem_targets is not None else 0

    def sa(s_, v_, o_):
        return mm(("sa", id(s_), id(v_), float(o_), lead_s, sid), (s_, v_, stem_targets),
                  lambda: S.score_at(s_, v_, o_, lead_s=lead_s, **stem_kw))

    def bo(s_, v_, **kw):
        return mm(("bo", id(s_), id(v_), tuple(sorted(kw.items())), lead_s), (s_, v_),
                  lambda: _best_offset(s_, v_, lead_s=lead_s, **kw))

    def at(rt):
        return mm(("at", id(song), rt), (song,), lambda: apply_tempo(song, rt))

    def aw(w_):
        return mm(("aw", id(w_["warp"])), (w_,), lambda: apply_warp(video, w_["warp"]))

    def asm_(s_, segs_, dur_):
        return mm(("as", id(s_), id(segs_), dur_), (s_, segs_), lambda: _assemble(s_, segs_, dur_))

    def left():
        return work.left()

    state = {"cancelled": False, "open": None, "pass": 0}

    def emit(tool, status, before, after=None, kept=None):
        if progress_cb is None:
            return
        try:
            progress_cb({"step": tool, "status": status, "score_before": round(float(before), 2),
                         "score_after": None if after is None else round(float(after), 2),
                         "kept": None if kept is None else bool(kept),
                         "elapsed_s": round(time.perf_counter() - t_start, 3)})
        except Exception:
            pass

    def cancel():
        if state["cancelled"]:
            return True
        if should_cancel is not None:
            try:
                if should_cancel():
                    state["cancelled"] = True
            except Exception:
                pass
        return state["cancelled"]

    def begin(tool, before):
        state["open"] = tool
        emit(tool, "start", before)

    def rec(tool, params, before, after, kept, rb=None, ra=None):
        """rb/ra: score results before/after (tightness is reported from them; ra only when kept)."""
        state["open"] = None
        if state["pass"] > 0:               # later pass: say so, and say when an earlier kept result still stands
            params = dict(params, **{"pass": state["pass"] + 1})
            if not kept and any(s["tool"] == tool and s["kept"] for s in steps):
                params["retained"] = True   # not replaced: the earlier kept step of this tool is still in the plan
        emit(tool, "end", before, after, kept)
        steps.append({"tool": tool, "params": params, "score_before": round(before, 2),
                      "score_after": round(after, 2), "kept": bool(kept),
                      "tightness_before": _tvals(rb) if rb is not None else None,
                      "tightness_after": _tvals(ra if (kept and ra is not None) else rb) if rb is not None else None})

    cur_song, offset = song, 0.0
    cur_video = video                       # original video + retime (nudges are applied on top)
    tempo_ratio, segs, retime, nudges = 1.0, None, None, []
    loop_segs_given = loop_segs
    r0 = sa(song, video, 0.0)
    begin("offset", _tot(r0))
    if loop_segs_given:                     # looped song: segments fixed, offset locked at 0, no tempo/splice
        segs = loop_segs_given
    elif lock_original:                     # video's own audio: offset 0, one plain trim, no tempo/splice/arrange
        segs = [{"src_start": 0.0, "src_end": vd, "dst_start": 0.0}]
    elif float(song["duration"]) >= vd:
        offset = bo(song, video)
    else:
        limits.append("song_shorter_than_video")
    cur = sa(cur_song, cur_video, offset)
    rec("offset", {"offset": round(offset, 2)}, _tot(r0), _tot(cur), True, r0, cur)

    def src_video():                        # video the song-side tools see (retimed, no nudges)
        return aw(retime) if retime else video

    for ps in range(max_passes):
        state["pass"] = ps
        start_cur = cur
        for tool in order:
            if _done(cur, target) or left() <= 0 or cancel():
                break
            sv = src_video()
            svd = float(sv["duration"])
            if tool == "tempo" and segs is None:
                begin("tempo", _tot(cur))
                work.spend(WORK_COST["tempo"])
                pt = plan_tempo(song, sv, max_tempo_dev)
                ratios = sorted({round(x, 3) for x in np.arange(1 - max_tempo_dev, 1 + max_tempo_dev + 1e-9, 0.01)}
                                | {pt["ratio"]})
                best = (_tot(cur), tempo_ratio, cur_song, offset)
                for rt in ratios:
                    if abs(rt - tempo_ratio) < 1e-9 or left() <= 0 or cancel():
                        continue
                    st = at(rt) if abs(rt - 1.0) > 1e-9 else song
                    o2 = bo(st, sv, center=offset * tempo_ratio / rt, half=1.0, step=0.05)
                    sc = _tot(sa(st, sv, o2))
                    if sc > best[0]:
                        best = (sc, rt, st, o2)
                if best[1] != tempo_ratio:
                    st = best[2]
                    o3 = bo(st, sv, center=best[3], half=0.06, step=0.01)
                    sc3 = _tot(sa(st, sv, o3))
                    if sc3 >= best[0]:
                        best = (sc3, best[1], st, o3)
                kept = best[0] >= _tot(cur) + min_gain
                newr = sa(best[2], sv, best[3]) if kept else cur
                rec("tempo", {"ratio": best[1], "bpm_from": pt["bpm_from"], "bpm_to": round(song["bpm"] * best[1], 2)},
                    _tot(cur), best[0] if kept else _tot(cur), kept, cur, newr)
                if kept:
                    tempo_ratio, cur_song, offset = best[1], best[2], best[3]
                    nudges, cur_video = [], sv
                    cur = newr
                if abs(best[1] - 1.0) >= max_tempo_dev - 1e-9:
                    limits.append("max_tempo_dev")
            elif tool == "splice" and allow_splice and segs is None and work.can(WORK_COST["splice"]) \
                    and float(cur_song["duration"]) >= svd:
                begin("splice", _tot(cur))
                work.spend(WORK_COST["splice"])
                try:
                    sp = mm(("sp", id(cur_song), id(sv), max_cuts, lead_s), (cur_song, sv),
                            lambda: plan_splice(cur_song, sv, max_cuts, allow_negative=True, lead_s=lead_s,
                                                should_cancel=cancel))
                except Cancelled:
                    break
                if sp.get("eligible") and len(sp["segments"]) > 1:
                    asm = asm_(cur_song, sp["segments"], svd)
                    asm_r = sa(asm, sv, 0.0)
                    kept = _gain(asm_r, cur, min_gain)
                    rec("splice", {"max_cuts": max_cuts, "cuts": sp["cuts"]}, _tot(cur),
                        _tot(asm_r) if kept else _tot(cur), kept, cur, asm_r)
                    if kept:
                        segs, cur_song, offset, cur = sp["segments"], asm, 0.0, asm_r
                        nudges, cur_video = [], sv
            elif tool == "arrange" and allow_splice and segs is None and work.can(WORK_COST["arrange"]) \
                    and float(cur_song["duration"]) >= svd:
                begin("arrange", _tot(cur))
                work.spend(WORK_COST["arrange"])
                key = (id(cur_song), id(sv), lead_s, bool(prefer_energy))
                if memo is not None and key in memo:
                    ap = memo[key][2]
                else:
                    ap = (plan_arrangement(cur_song, sv, ARRANGE_MAX_MOVES, lead_s=lead_s, prefer_energy=True) if prefer_energy
                          else plan_arrangement(cur_song, sv, ARRANGE_MAX_MOVES, lead_s=lead_s))
                    if memo is not None:
                        memo[key] = (cur_song, sv, ap)       # keep the objects alive so ids stay unique
                cancel()                    # arrange is not interruptible: poll again once it returns
                if ap.get("eligible") and len(ap.get("segments") or []) > 1:
                    asm = asm_(cur_song, ap["segments"], svd)
                    asm_r = sa(asm, sv, 0.0)
                    kept = _gain(asm_r, cur, min_gain)
                    rec("arrange", {"moves": ap["moves"], "sections_used": ap["sections_used"]}, _tot(cur),
                        _tot(asm_r) if kept else _tot(cur), kept, cur, asm_r)
                    if kept:
                        segs, cur_song, offset, cur = ap["segments"], asm, 0.0, asm_r
                        nudges, cur_video = [], sv
            elif tool == "retime":
                # always planned from the ORIGINAL video against the current song/offset (replaces)
                begin("retime", _tot(cur))
                work.spend(WORK_COST["retime"])
                alt_off = None
                try:
                    rp = mm(("rt", id(cur_song), id(video), float(offset), max_speed_dev, min_section_s, lead_s, sid),
                            (cur_song, video, stem_targets),
                            lambda: plan_retime(cur_song, video, offset, max_speed_dev, min_section_s, lead_s=lead_s,
                                                should_cancel=cancel, extra_targets=xt))
                except Cancelled:
                    break
                wv = aw(rp)
                rr = sa(cur_song, wv, offset)
                base = sa(cur_song, sv, offset)       # current retime state, no nudges
                ref = base if (nudges or _tot(base) > _tot(cur)) else cur
                kept = bool(rp["sections"]) and rp["speed_jump_ok"] and _gain(rr, ref, min_gain)
                if not kept and segs is None and not lock_original and not retime:
                    # the plain-trim best offset can sit where the retime planner finds nothing: try the
                    # other plausible offsets (plain trim only: segments / looped songs fix the offset at 0)
                    try:
                        for o2 in mm(("alt", id(cur_song), id(video), float(offset), lead_s), (cur_song, video),
                                     lambda: _alt_offsets(cur_song, video, [offset], lead_s=lead_s)):
                            if cancel() or not work.can(WORK_COST["retime_alt"]):
                                break
                            work.spend(WORK_COST["retime_alt"])
                            rp2 = mm(("rt", id(cur_song), id(video), float(o2), max_speed_dev, min_section_s, lead_s, sid),
                                     (cur_song, video, stem_targets),
                                     lambda: plan_retime(cur_song, video, o2, max_speed_dev, min_section_s, lead_s=lead_s,
                                                         should_cancel=cancel, extra_targets=xt))
                            if not rp2["sections"] or not rp2["speed_jump_ok"]:
                                continue
                            wv2 = aw(rp2)
                            rr2 = sa(cur_song, wv2, o2)
                            if _gain(rr2, ref, min_gain) and (not kept or _rank(rr2) > _rank(rr)):
                                rp, wv, rr, kept, alt_off = rp2, wv2, rr2, True, o2
                    except Cancelled:
                        break
                rparams = {"sections": len(rp["sections"]), "max_speed_dev": max_speed_dev}
                if kept and alt_off is not None:
                    rparams["offset"] = round(alt_off, 3)       # the retime moved the offset (plan.offset)
                rec("retime", rparams,
                    _tot(cur), _tot(rr) if kept else _tot(cur), kept, cur, rr)
                if kept:
                    retime, cur_video, cur, nudges = rp, wv, rr, []
                    if alt_off is not None:
                        offset = alt_off
                    if segs is not None and allow_splice and work.can(WORK_COST["splice"]) and not cancel():
                        # events moved: re-splice against the retimed video (one extra pass)
                        begin("splice", _tot(cur))
                        work.spend(WORK_COST["splice"])
                        try:
                            sp = plan_splice(song if tempo_ratio == 1.0 else at(tempo_ratio),
                                             wv, max_cuts, allow_negative=True, lead_s=lead_s,
                                             should_cancel=cancel)
                        except Cancelled:
                            sp = {}
                        if sp.get("eligible") and len(sp["segments"]) > 1:
                            base_s = song if tempo_ratio == 1.0 else at(tempo_ratio)
                            asm = asm_(base_s, sp["segments"], float(wv["duration"]))
                            ar = sa(asm, wv, 0.0)
                            if _gain(ar, cur, min_gain):
                                rec("splice", {"max_cuts": max_cuts, "cuts": sp["cuts"], "after_retime": True},
                                    _tot(cur), _tot(ar), True, cur, ar)
                                segs, cur_song, offset, cur = sp["segments"], asm, 0.0, ar
            elif tool == "micro" and not nudges:
                begin("micro", _tot(cur))
                work.spend(WORK_COST["micro"])
                nv, nl = mm(("nd", id(cur_song), id(cur_video), float(offset), fps, lead_s), (cur_song, cur_video),
                            lambda: _nudge(cur_song, cur_video, offset, 1.0 / fps, lead_s))
                nr = sa(cur_song, nv, offset)
                kept = bool(nl) and _gain(nr, cur, min_gain)
                rec("micro", {"nudges": len(nl), "max_shift_s": round(1.0 / fps, 4)}, _tot(cur),
                    _tot(nr) if kept else _tot(cur), kept, cur, nr)
                if kept:
                    nudges, cur_video, cur = nl, nv, nr
            if state["open"]:               # step started but nothing to record (not eligible / not applicable)
                emit(state["open"], "end", _tot(cur), _tot(cur), False)
                state["open"] = None
        if _done(cur, target) or not _gain(cur, start_cur, min_gain) or cancel():
            break
    if state["open"]:                       # interrupted mid-step (cancel): close it, nothing kept
        emit(state["open"], "end", _tot(cur), _tot(cur), False)
        state["open"] = None
    if not state["cancelled"] and not _done(cur, target):
        tried = {s["tool"] for s in steps}
        for tool in order:                  # never silently drop a tool: say why it was not even tried
            if tool in tried:
                continue
            if left() <= 0:
                limits.append("budget: skipped %s" % tool)
            else:
                limits.append("skipped %s: not applicable" % tool)
    if state["cancelled"]:
        limits.append("cancelled")
    if left() <= 0 and not _done(cur, target):
        limits.append("budget: time cap hit" if work.cap_hit else "budget")
    if retime and any(abs(s["speed"] - 1) >= max_speed_dev - 0.01 for s in retime["sections"]):
        limits.append("max_speed_dev")
    if retime and not retime.get("speed_jump_ok", True):
        limits.append("speed_jump_over_0.15")
    out_dur = float(cur_video["duration"])
    return {"steps": steps, "cur": cur, "limits": limits, "out_duration": out_dur,
            "plan": {"offset": round(offset, 3), "lead_s": lead_s, "tempo_ratio": tempo_ratio, "segments": segs,
                     "retime": retime, "nudges": nudges,
                     "lead_silence_s": lead_silence(segs) if segs else round(max(0.0, -offset), 6),
                     "original_locked": bool(lock_original)}}


def auto_sync(song: dict, video: dict, target: float = 100, budget_s: float = 20.0,
              max_speed_dev: float = 0.25, max_tempo_dev: float = 0.10, min_section_s: float = 0.75,
              max_cuts: int = 3, min_gain: float = 0.5, max_passes: int = 2, allow_splice: bool = True, lead_s: float | None = None,
              allow_loop: bool = False, original: bool = False, unlock_original: bool = False,
              *, progress_cb=None, should_cancel=None, stem_targets=None, prefer_energy=True, ramps=False,
              budget_units: float = DEFAULT_WORK_UNITS) -> dict:
    """Try several tool orders (each from the baseline) and keep the best path, so the result is never
    worse than any single tool applied directly.

    Length rule: a song shorter than the video is INELIGIBLE (same as score.score): the result has
    eligible False, total 0, reason 'song shorter than video', and nothing is retimed or filled.
    allow_loop=True (default False) lifts that: the song is repeated end to end (as plan.segments,
    crossfaded by the renderer) until it covers the video and its retime headroom; plan.looped is True
    and plan.loop = {song_duration_s, repeats}. A looped plan uses retime + micro nudges only (offset
    locked at 0, no tempo stretch, no splice), so the segments stay valid.

    original=True marks the song as the video's OWN audio (analysis entry source == 'original'). Unless
    unlock_original=True the offset is fixed at 0, plan.segments is one plain trim, tempo/splice/arrange are
    off and only retime + micro may run; plan.original_locked is True and limits_hit has 'original audio locked'.
    final.natural_total_pct is the total of the plain trim at the best offset (the primary ranking key;
    fit_after_effort is information only).

    progress_cb(event) (keyword-only, optional) is called at the start and end of every step with
    {step, status: 'start'|'end', score_before, score_after (None at start), kept (None at start), elapsed_s}
    (step = tool name: offset, tempo, splice, arrange, retime, micro; elapsed_s counts from the call start;
    exceptions in it are ignored). should_cancel() -> bool is polled between steps and inside the retime and
    splice planners; once it returns True the best valid result so far is returned (same shape) with
    limits_hit containing 'cancelled'.

    stem_targets (optional, stemsync.stem_targets(song, stems_feats)) go to every score call and, as extra
    soft snap targets for calm-block events, to the retime planner; None = unchanged behaviour.

    prefer_energy (keyword-only, default True; False = the original arrange objective): the arrange step
    ranks by energy correlation and keeps the highest-arc arrangement within the total/tight/sync guards
    of the baseline.

    Determinism: the search is bounded by budget_units (keyword-only, default DEFAULT_WORK_UNITS) of WORK, each
    step costing a fixed number of units (WORK_COST), never by the clock, so the same inputs give the same plan
    and percentages on any machine and under any load. budget_s is only a hard wall-clock safety cap
    (budget_s * TIME_CAP_MULT seconds); if it ever fires, limits_hit contains 'budget: time cap hit' and the
    result is NOT reproducible. Running out of work units reports plain 'budget' (+ 'budget: skipped <tool>').
    The result echoes final.work_units {used, budget}.

    ramps (keyword-only, default False = results unchanged): after the best path is chosen, plan eased speed
    ramps (speedramp.plan_ramps) from that path's retime. Only for a plain-trim retime plan (no splice/arrange
    segments, no tempo stretch, not looped). The step-retime plan stays the plan; the ramps are ADDED as
    plan.retime_ramps (plan_ramps result minus 'step': points, curve, warp, out_duration, sync/total before/step/after)
    and final.ramps {total_step, total_after, kept} when they beat the step plan, else limits_hit gets 'ramps: no gain'
    (or 'ramps: not applicable')."""
    lead_s = S.PERCEPTUAL_LEAD_S if lead_s is None else lead_s
    lead_s = min(0.12, max(0.0, float(lead_s)))   # cuts/peaks land lead_s before the beat (0 = on it)
    t0 = time.perf_counter()
    cflag = {"on": False}

    def cancelled():                      # latching, exception-safe wrapper around the caller's should_cancel
        if not cflag["on"] and should_cancel is not None:
            try:
                cflag["on"] = bool(should_cancel())
            except Exception:
                pass
        return cflag["on"]

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
    lock = bool(original) and not unlock_original and not loop_segs
    orders = dict(ORDERS)
    if lock:                             # only retime + micro may run on the video's own audio
        orders = {k: tuple(t for t in v if t in ("retime", "micro")) for k, v in ORDERS.items()}
        seen, keep = set(), []
        for n in names:
            if orders[n] and orders[n] not in seen:
                seen.add(orders[n]); keep.append(n)
        names = keep
        allow_splice = False
    best, pruned, runs, skipped_names = None, False, [], []
    memo = {}
    work = _Work(budget_units, max(1.0, float(budget_s)) * TIME_CAP_MULT)
    for i, name in enumerate(names):
        if best and cancelled():
            break
        if work.exhausted() and best:
            pruned = True
            skipped_names = names[i:]
            break
        run = _run(song, video, orders[name], target, work, max_speed_dev,
                   max_tempo_dev, min_section_s, max_cuts, min_gain, max_passes, allow_splice, lead_s,
                   loop_segs=loop_segs, memo=memo, lock_original=lock,
                   progress_cb=progress_cb, should_cancel=cancelled, t_start=t0, stem_targets=stem_targets, prefer_energy=prefer_energy)
        run["path"] = name
        runs.append(run)
        if best is None or _rank(run["cur"]) > _rank(best["cur"]):
            best = run
        if _done(best["cur"], target):
            break
    cur, limits = best["cur"], list(best["limits"])
    if pruned:
        limits.append("budget: time cap hit" if work.cap_hit else "budget")
        seen_tools = {s["tool"] for r_ in runs for s in r_["steps"]}
        for n in skipped_names:
            for tool in orders[n]:
                if tool not in seen_tools:
                    seen_tools.add(tool)
                    limits.append("budget: skipped %s" % tool)
    # tools that the winning order does not contain but another order tried: keep a (not kept) record of them
    mine = {s["tool"] for s in best["steps"]}
    for r_ in runs:
        if r_ is best:
            continue
        for tool in dict.fromkeys(s["tool"] for s in r_["steps"]):
            if tool in mine or tool == "offset":
                continue
            mine.add(tool)
            theirs = [s for s in r_["steps"] if s["tool"] == tool]
            k = [s for s in theirs if s["kept"]]
            tot = round(_tot(best["cur"]), 2)
            best["steps"].append({"tool": tool, "params": dict(theirs[-1]["params"], path=r_["path"],
                                                                  not_in_path=True, kept_in_that_path=bool(k),
                                                                  total_in_that_path=round(_tot(r_["cur"]), 2)),
                                  "score_before": tot, "score_after": tot, "kept": False,
                                  "tightness_before": None, "tightness_after": None})
    if ramps:
        _add_ramps(song, video, best, max_speed_dev, lead_s, cancelled, limits)
    if lock:
        limits.append("original audio locked")
    if cflag["on"]:
        limits.append("cancelled")
    final = {k: cur[k] for k in ("total_pct", "tempo_pct", "sync_pct", "arc_pct")}
    final["total_pct"] = int(round(_tot(cur)))
    final["work_units"] = {"used": round(work.used, 2), "budget": float(budget_units)}
    base0 = best["steps"][0]["score_after"] if best["steps"] else _tot(cur)
    final["natural_total_pct"] = int(round(base0))
    for _k in ("quiet_sync", "quiet_sync_beats", "quiet_events"):      # only present when stem_targets were given
        if _k in cur:
            final[_k] = cur[_k]
    if best["plan"].get("retime_ramps"):
        _rr = best["plan"]["retime_ramps"]
        final["ramps"] = {"kept": True, "mode": _rr.get("mode", RAMPS_MODE),
                          **{k: _rr.get(k) for k in ("total_step", "total_after", "tight_step", "tight_after",
                                                     "sync_step", "sync_after", "smooth_step", "smooth_ramp")}}
    final["tightness"] = _tight(cur) or None
    final["tight_pct"] = _tight(cur).get("tight_pct")
    final["median_err_ms"] = _tight(cur).get("median_err_ms")
    final["out_duration_s"] = round(float(best["out_duration"]), 3)
    final["video_duration_s"] = round(float(video["duration"]), 3)
    final["sync_headline"] = int(round(0.6 * cur["sync_pct"] + 0.4 * cur["tempo_pct"]))
    first = best["steps"][0] if best["steps"] else {}
    final["lead_s"] = lead_s
    final["tightness_start"] = first.get("tightness_before")
    ef = effort_of(best["plan"], video["duration"], max_speed_dev, max_tempo_dev)
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


def _add_ramps(song, video, best, max_speed_dev, lead_s, cancelled, limits):
    """Opt-in (auto_sync ramps=True): eased speed ramps on top of the chosen plain-trim retime plan."""
    plan = best["plan"]
    rt = plan.get("retime")
    if not rt or not rt.get("warp") or plan.get("segments") is not None or plan.get("tempo_ratio", 1.0) != 1.0 \
            or plan.get("nudges"):
        limits.append("ramps: not applicable")
        return
    from . import speedramp
    try:
        res = speedramp.plan_ramps(song, video, float(plan["offset"]), max_speed_dev, lead_s=lead_s, step_plan=rt,
                                   should_cancel=cancelled, mode=RAMPS_MODE)
    except Exception:
        limits.append("ramps: not applicable")
        return
    if res["total_after"] > res["total_step"] + 1e-9 and not cancelled():
        plan["retime_ramps"] = {k: v for k, v in res.items() if k != "step"}
    else:
        limits.append("ramps: no gain")


def _ineligible(song, video, lead_s):
    sd, vd = float(song["duration"]), float(video["duration"])
    final = {"eligible": False, "total_pct": 0, "tempo_pct": 0.0, "sync_pct": 0.0, "arc_pct": 0.0,
             "tightness": None, "tight_pct": None, "median_err_ms": None, "out_duration_s": round(vd, 3),
             "video_duration_s": round(vd, 3), "sync_headline": 0, "lead_s": lead_s, "tightness_start": None,
             "effort": effort_of(None, vd), "effort_pct": 0.0, "fit_after_effort": 0.0, "natural_total_pct": 0,
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
