"""U8b: "launch onto drop" anchors for the speed-ramp planner.

A video hit (a motion launch, a calm->build onset, or the end of a peak block: see buildup.video_hits) that
the step plan lands NEAR a song drop (or chorus entry) gets pulled ONTO it by a forced event->time anchor. That
is the same kind of anchor the beat chain uses (src time -> output time), so speedramp.plan_anchored(...,
extra_anchors=anchors_for_ramps(...)) solves it with the rest of the plan under the same speed/accel limits.

Convention: speed = source seconds per output second; out_t = drop - offset - lead.
Pure functions; nothing here reads or writes app data. Not wired into autosync (flag-style).

MEASURED (2026-11-10, real cases A/B + song S1, +A own audio): forcing a launch anchor meets its target to ~0 ms but
costs the surrounding beat-sync, because pulling a hit by 1-3 s (not a whole number of beats) forces the chain
anchors between its neighbours to be dropped (16-38 of ~60). Per single anchor, total 87->20..60 (A+S1), 89->71..87
(B+S1), launch_pct up only on A+S1 (0->15..30). So this stays an opt-in experiment: do not enable it by default."""
from __future__ import annotations

import numpy as np

from . import buildup as B
from . import score as S

SNAP_S = 4.0            # hit and drop closer than this (output s, in the step plan) are paired (spec: +-4 s)
KIND_W = {"launch": 1.0, "onset": 0.8, "impact": 0.8}
FORCED_WEIGHT = 3.0     # relative pull of a launch anchor (a normal beat event weighs ~1)


def _hits(video, hits):
    if hits is not None:
        return list(hits)
    fn = getattr(B, "video_hits", None)
    if fn is not None:
        return fn(video)
    return [dict(l, kind="launch") for l in B.video_launches(video)]


def _drops(song, drops):
    """[(drop_time, build_strength)] sorted by time."""
    if drops is not None:
        return sorted((float(d[0]), float(d[1])) if isinstance(d, (tuple, list)) else (float(d), 1.0) for d in drops)
    return sorted((float(b["drop"]), float(b.get("strength", 1.0))) for b in B.song_buildups(song))


def plan_launch_anchors(song: dict, video: dict, offset: float, step_warp, *, max_speed_dev: float = 0.25,
                        hits=None, drops=None, lead_s: float = None, snap_s: float = SNAP_S, tol_s: float = 0.0) -> list:
    """[{t_src, out_t, weight, feasible, speed_needed, kind, drop, shift_s}] one per paired (hit, drop).

    shift_s = out_t - where the step plan puts the hit (positive = the hit has to move later). speed_needed is the
    mean speed from the previous anchor (the origin for the first) to this one if the hit lands exactly; feasible
    means it fits [1 - max_speed_dev, 1 + max_speed_dev]. A drop pairs with the nearest hit in the step plan's
    output time (kind-weighted tie-break) and a hit pairs with at most one drop. tol_s > 0: a hit already within
    tol_s of its drop is left alone (marked ok, not feasible-for-forcing) and a farther one is only pulled to
    0.8 * tol_s from the drop, so the anchor moves it by the least that meets the 'within a beat' success rule."""
    lo, hi = 1.0 - max_speed_dev, 1.0 + max_speed_dev
    lead = S._lead(lead_s)
    sw = np.asarray(step_warp, dtype=float)
    hs = _hits(video, hits)
    ds = _drops(song, drops)
    if not hs or not ds:
        return []
    ht = np.array([float(h["t"]) for h in hs])
    ho = np.interp(ht, sw[:, 0], sw[:, 1])
    pairs, used = [], set()
    for d, bs in ds:
        tgt = d - float(offset) - lead
        if tgt <= 0.1:
            continue
        best = None
        for i, h in enumerate(hs):
            if i in used:
                continue
            gap = abs(ho[i] - tgt)
            if gap > snap_s:
                continue
            score = gap / KIND_W.get(h.get("kind", "launch"), 0.8)
            if best is None or score < best[0]:
                best = (score, i)
        if best is not None:
            used.add(best[1])
            pairs.append((best[1], d, tgt, bs))
    pairs.sort(key=lambda p: ht[p[0]])
    out, prev_e, prev_b = [], 0.0, 0.0
    for i, d, tgt, bs in pairs:
        e = float(ht[i])
        shift0 = tgt - float(ho[i])
        if tol_s > 0:
            if abs(shift0) <= tol_s:
                out.append({"t_src": round(e, 3), "out_t": round(float(ho[i]), 3), "weight": 0.0, "feasible": False,
                            "ok": True, "speed_needed": None, "kind": hs[i].get("kind", "launch"),
                            "drop": round(float(d), 2), "shift_s": round(shift0, 3)})
                continue
            tgt = tgt - float(np.sign(shift0)) * 0.8 * tol_s
        gap_b = tgt - prev_b
        sp = (e - prev_e) / gap_b if gap_b > 1e-6 else float("inf")
        ok = bool(lo - 1e-9 <= sp <= hi + 1e-9 and gap_b >= 0.05)
        h = hs[i]
        out.append({"t_src": round(e, 3), "out_t": round(float(tgt), 3),
                    "weight": round(float(h.get("strength", 0.5)) * bs * FORCED_WEIGHT, 3),
                    "feasible": ok, "speed_needed": round(float(sp), 4) if np.isfinite(sp) else None,
                    "kind": h.get("kind", "launch"), "drop": round(float(d), 2),
                    "shift_s": round(float(tgt - ho[i]), 3), "ok": False})
        if ok:
            prev_e, prev_b = e, float(tgt)
    return out


def anchors_for_ramps(launch_anchors) -> list:
    """[(src_s, out_s, weight)] of the feasible launch anchors, for speedramp.plan_anchored(extra_anchors=...)."""
    return [(a["t_src"], a["out_t"], a["weight"]) for a in launch_anchors if a.get("feasible")]


def residuals_ms(launch_anchors, warp) -> list:
    """Per anchor: how far (ms, signed; + = hit lands late) the hit lands from its drop under `warp`."""
    w = np.asarray(warp, dtype=float)
    return [round(float((np.interp(a["t_src"], w[:, 0], w[:, 1]) - a["out_t"]) * 1000.0), 1) for a in launch_anchors]
