"""Resolve-style speed ramps: speed points with eased transitions, placed by an optimizer (U11a).

plan_retime() lands events on beats with constant-speed sections and instant speed jumps. This module
keeps the same warp contract (warp = [[src_s, out_s], ...], speed = source seconds per output second) but
the warp is the INTEGRAL of a continuous, eased speed curve:

    points = [{"t_src", "speed", "ease_in_s", "ease_out_s"}, ...]

`speed` is the plateau speed from the point onward; `ease_in_s`/`ease_out_s` are the source seconds before
and after `t_src` over which the speed eases from the previous plateau to this one (ease shape: smoothstep
by default). The first point is t_src=0 with no ease. Constraints: |speed-1| <= max_speed_dev, and the
speed's rate of change |ds/d out_s| <= max_accel (ease widths are derived from it, never wider than
ease_max_s or the room between neighbouring points).

The optimizer (`optimize`) is generic: it takes plateau speeds + boundaries and an `evaluate(warp) -> J`
callback and does coordinate descent on speeds and boundary positions plus section splits, rejecting any move
that breaks a constraint. plan_ramps() wires it to the video/score objective; temporamp.py wires the same
machinery to the song (ratio curve within +-10%).

Flag-style module: nothing in retime.py/autosync.py/tempo.py imports it yet."""
from __future__ import annotations

import numpy as np

from . import retime as R
from . import score as S

MAX_ACCEL = 0.5        # speed change per output second (video)
EASE_MAX_S = 2.0       # longest ramp, output seconds
GRID_HZ = 100.0        # integration grid, source seconds
SAMPLE_HZ = 4.0        # sampled curve density returned for the UI
WARP_HZ = 20.0         # sampled warp density returned
MIN_SECTION_S = 0.4    # plateau boundaries never closer than this (source s)
REG = 0.02             # J penalty per unit of total speed variation (prefers gentler curves)


# --- ease shapes -------------------------------------------------------------------------------------

def _linear(x):
    return x


TIGHT_WEIGHT = 0.3


def _smoothstep(x):
    return x * x * (3.0 - 2.0 * x)


def _smootherstep(x):
    return x * x * x * (x * (6.0 * x - 15.0) + 10.0)


def _cosine(x):
    return 0.5 - 0.5 * np.cos(np.pi * x)


# name -> (shape fn on [0,1], peak slope of the shape = max accel factor)
EASES = {"linear": (_linear, 1.0), "smoothstep": (_smoothstep, 1.5), "cosine": (_cosine, np.pi / 2),
         "smootherstep": (_smootherstep, 1.875)}


def _ease(name):
    if name not in EASES:
        raise ValueError("unknown ease %r (choose from %s)" % (name, ", ".join(sorted(EASES))))
    return EASES[name]


# --- parametrisation: plateau speeds + boundaries -> points -> curve -> warp --------------------------

def _halfs(speeds, bounds, vd, max_accel, ease_max_s, slope):
    """Ease half-width (source s) at each interior boundary and each boundary's max feasible speed step.
    bounds[k] is the boundary between plateau k and k+1 (len = len(speeds)-1)."""
    n = len(speeds)
    edges = [0.0] + list(bounds) + [vd]
    halfs, dmax = [], []
    for k in range(1, n):
        ds = abs(speeds[k] - speeds[k - 1])
        avg = 0.5 * (speeds[k] + speeds[k - 1])
        room = 0.45 * min(edges[k] - edges[k - 1], edges[k + 1] - edges[k])      # source s per side
        want = 0.5 * min(ease_max_s, slope * ds / max(max_accel, 1e-9)) * avg    # source s per side
        h = min(want, room)
        halfs.append(h)
        t_avail = 2.0 * h / max(avg, 1e-9)                                     # output s the ramp takes
        dmax.append(max_accel * t_avail / slope if t_avail > 0 else 0.0)
    return halfs, dmax


def points_from(speeds, bounds, vd, max_accel, ease_max_s, ease):
    _fn, slope = _ease(ease)
    halfs, _ = _halfs(speeds, bounds, vd, max_accel, ease_max_s, slope)
    pts = [{"t_src": 0.0, "speed": float(speeds[0]), "ease_in_s": 0.0, "ease_out_s": 0.0}]
    for k in range(1, len(speeds)):
        pts.append({"t_src": float(bounds[k - 1]), "speed": float(speeds[k]),
                    "ease_in_s": float(halfs[k - 1]), "ease_out_s": float(halfs[k - 1])})
    return pts


def speed_curve(points, vd, ease="smoothstep", hz=GRID_HZ):
    """(source grid, speed on grid) for eased speed points."""
    fn, _ = _ease(ease)
    vg = np.arange(0.0, vd, 1.0 / hz)
    vg = np.append(vg, vd) if (len(vg) == 0 or vg[-1] < vd) else vg
    prev = float(points[0]["speed"])
    s = np.full_like(vg, prev)
    for p in points[1:]:
        a, b = p["t_src"] - p["ease_in_s"], p["t_src"] + p["ease_out_s"]
        sp = float(p["speed"])
        s = np.where(vg >= b, sp, s)
        if b - a > 1e-9:
            m = (vg > a) & (vg < b)
            s = np.where(m, prev + (sp - prev) * fn((vg - a) / (b - a)), s)
        prev = sp
    return vg, s


def integrate(vg, s):
    """Warp = integral of 1/speed over source time (trapezoid)."""
    inv = 1.0 / s
    return np.concatenate([[0.0], np.cumsum(0.5 * (inv[1:] + inv[:-1]) * np.diff(vg))])


def warp_from_points(points, vd, ease="smoothstep", hz=GRID_HZ):
    vg, s = speed_curve(points, vd, ease, hz)
    return np.stack([vg, integrate(vg, s)], axis=1), s


def stats(warp, dt=0.1):
    """Smoothness of a warp polyline: instant speed jumps (max |ds| across a vertex), max accel measured on a
    dt-second output grid, and total speed variation. Step plans score high jump/accel, ramps ~0 jump."""
    w = np.asarray(warp, dtype=float)
    if len(w) < 3:
        return {"max_jump": 0.0, "max_accel": 0.0, "speed_tv": 0.0}
    sp = np.diff(w[:, 0]) / np.maximum(np.diff(w[:, 1]), 1e-9)
    jump = float(np.abs(np.diff(sp)).max())
    og = np.arange(0.0, w[-1, 1], dt)
    vg = np.interp(og, w[:, 1], w[:, 0])
    sg = np.diff(vg) / dt
    acc = np.abs(np.diff(sg)) / dt
    return {"max_jump": round(jump, 4), "max_accel": round(float(acc.max()) if len(acc) else 0.0, 3),
            "speed_tv": round(float(np.abs(np.diff(sg)).sum()) if len(sg) else 0.0, 3)}


# --- optimizer ----------------------------------------------------------------------------------------

def _project(speeds, bounds, vd, lo, hi, max_accel, ease_max_s, slope, passes=60):
    """Pull neighbouring plateau speeds together until every boundary's step is feasible (and in [lo, hi])."""
    sp = [float(np.clip(s, lo, hi)) for s in speeds]
    for _ in range(passes):
        _h, dmax = _halfs(sp, bounds, vd, max_accel, ease_max_s, slope)
        bad = False
        for k in range(1, len(sp)):
            d = sp[k] - sp[k - 1]
            if abs(d) > dmax[k - 1] * 0.999:
                bad = True
                cut = (abs(d) - dmax[k - 1] * 0.98) / 2.0
                sp[k] -= np.sign(d) * cut
                sp[k - 1] += np.sign(d) * cut
        sp = [float(np.clip(s, lo, hi)) for s in sp]
        if not bad:
            break
    return sp


def optimize(vd, speeds, bounds, evaluate, *, dev, max_accel=MAX_ACCEL, ease_max_s=EASE_MAX_S, ease="smoothstep",
             min_bound=0.0, max_bound=None, split_len=2.0, split=True, steps=None, should_cancel=None,
             reg=REG, hz=GRID_HZ, split_cands=None):
    """Coordinate descent on plateau speeds / boundaries / splits. evaluate(warp_ndarray) -> J (higher better).
    Returns (speeds, bounds, J, J0). Raises R.Cancelled via should_cancel (caller keeps best-so-far)."""
    _fn, slope = _ease(ease)
    lo, hi = 1.0 - dev, 1.0 + dev
    max_bound = vd if max_bound is None else max_bound
    speeds, bounds = list(speeds), list(bounds)
    speeds = _project(speeds, bounds, vd, lo, hi, max_accel, ease_max_s, slope)

    def build(sp, bd):
        pts = points_from(sp, bd, vd, max_accel, ease_max_s, ease)
        return warp_from_points(pts, vd, ease, hz)[0]

    def feasible(sp, bd):
        if any(s < lo - 1e-9 or s > hi + 1e-9 for s in sp):
            return False
        edges = [0.0] + list(bd) + [vd]
        if any(edges[k + 1] - edges[k] < MIN_SECTION_S - 1e-9 for k in range(len(edges) - 1)):
            return False
        if bd and (bd[0] < min_bound - 1e-9 or bd[-1] > max_bound + 1e-9):
            return False
        _h, dmax = _halfs(sp, bd, vd, max_accel, ease_max_s, slope)
        return all(abs(sp[k] - sp[k - 1]) <= dmax[k - 1] * 1.0001 + 1e-9 for k in range(1, len(sp)))

    def J(sp, bd):
        tv = sum(abs(sp[k] - sp[k - 1]) for k in range(1, len(sp)))
        return float(evaluate(build(sp, bd))) - reg * 100.0 * tv

    steps = steps or [(0.04, 0.15), (0.02, 0.06), (0.01, 0.03)]
    cur = J(speeds, bounds)
    j0 = cur

    def poll():
        if should_cancel is not None and should_cancel():
            raise R.Cancelled()

    try:
        for sweep, (d_sp, d_bd) in enumerate(steps):
            improved = False
            k = 0
            while k < len(speeds):
                poll()
                for dk in (d_sp, -d_sp):                                   # plateau speed
                    sp = list(speeds)
                    sp[k] = sp[k] + dk
                    if feasible(sp, bounds):
                        j = J(sp, bounds)
                        if j > cur + 1e-3:
                            speeds, cur, improved = sp, j, True
                            break
                if 1 <= k < len(speeds) and d_bd > 0:                      # boundary between k-1 and k
                    for dk in (d_bd, -d_bd):
                        bd = list(bounds)
                        bd[k - 1] = bd[k - 1] + dk
                        if feasible(speeds, bd):
                            j = J(speeds, bd)
                            if j > cur + 1e-3:
                                bounds, cur, improved = bd, j, True
                                break
                k += 1
            if split and sweep < len(steps):                                    # try new boundaries in long plateaus
                edges = [0.0] + list(bounds) + [vd]
                kk = 0
                while kk < len(speeds):
                    poll()
                    a, b = edges[kk], edges[kk + 1]
                    cands = [c for c in (split_cands or []) if a + 0.8 < c < b - 0.8] if split_cands else []
                    if b - a >= split_len and not cands:
                        cands = [a + (b - a) * f for f in (0.33, 0.5, 0.67)]
                    best = None
                    for c in cands:
                        for dk in (0.06, -0.06):
                            sp = speeds[:kk + 1] + [speeds[kk] + dk] + speeds[kk + 1:]
                            bd = bounds[:kk] + [c] + bounds[kk:]
                            sp = _project(sp, bd, vd, lo, hi, max_accel, ease_max_s, slope, passes=8)
                            if feasible(sp, bd):
                                j = J(sp, bd)
                                if j > cur + 0.05 and (best is None or j > best[0]):
                                    best = (j, sp, bd)
                    if best is not None:
                        cur, speeds, bounds, improved = best[0], best[1], best[2], True
                        edges = [0.0] + list(bounds) + [vd]
                        kk += 2
                    else:
                        kk += 1
            if not improved and sweep >= 1:
                break
    except R.Cancelled:
        pass
    return speeds, bounds, cur, j0


def _merge_flat(speeds, bounds, tol=0.004):
    sp, bd = [speeds[0]], []
    for k in range(1, len(speeds)):
        if abs(speeds[k] - sp[-1]) < tol:
            continue
        sp.append(speeds[k])
        bd.append(bounds[k - 1])
    return sp, bd


# --- phase-locked tracker: event -> beat anchors, then a smooth least-squares plateau fit ---------------

def _chain(ev, beats, lo, hi, i0, b0, max_rate_step):
    """Greedy phase-locked walk starting with event i0 locked to beat b0; returns (anchors, total_rate_dev)."""
    pv, po = float(ev[i0]), float(b0)
    s_ref = float(np.clip(pv / po, lo, hi))
    out, dev = [(pv, po)], 0.0
    for e in ev[i0 + 1:]:
        if e - pv < 0.08:
            continue
        j0 = np.searchsorted(beats, po + (e - pv) / hi - 1e-6)
        j1 = np.searchsorted(beats, po + (e - pv) / lo + 1e-6)
        best = None
        for b in beats[j0:j1]:
            if b - po < 0.05:
                continue
            imp = (e - pv) / (b - po)
            d = abs(imp - s_ref)
            if d <= max_rate_step and (best is None or d < best[0]):
                best = (d, b, imp)
        if best is None:
            continue
        d, b, imp = best
        out.append((float(e), float(b)))
        dev += d
        s_ref = float(np.clip(0.5 * s_ref + 0.5 * imp, lo, hi))
        pv, po = float(e), float(b)
    return out, dev


def track_anchors(ev_t, beats_out, lo, hi, max_rate_step=0.12, starts=4, free_start=False):
    """Match events (in order) to beats so the implied local speed stays inside [lo, hi] and changes by
    <= max_rate_step per event (a phase-locked loop on the event train). The start lock is ambiguous, so try
    every plausible (first-few-events x beat) lock (free_start: the start ratio need not be in range, e.g. a song
    whose window does not begin at 0) and keep the chain with the most anchors, then the least
    rate wobble. Returns [(src_s, out_s)]."""
    ev = np.sort(np.asarray(ev_t, dtype=float))
    beats = np.asarray(beats_out, dtype=float)
    best, key = [], None
    for i0 in range(min(starts, len(ev))):
        if ev[i0] < 0.05:
            continue
        for b0 in beats[(beats > 0.05) & (beats < ev[i0] / lo * (1.5 if free_start else 1.0) + 1e-6)]:
            if not free_start and not (lo - 1e-9 <= ev[i0] / b0 <= hi + 1e-9):
                continue
            if free_start and not (0.5 <= ev[i0] / b0 <= 2.0):
                continue
            ch, dv = _chain(ev, beats, lo, hi, i0, b0, max_rate_step)
            k = (len(ch), -dv)
            if key is None or k > key:
                best, key = ch, k
    return best


def fit_plateaus(anchors, vd, dev, knot_s=3.0, max_accel=MAX_ACCEL, ease_max_s=EASE_MAX_S, ease="smoothstep",
                 beat_period=0.5, should_cancel=None):
    """Least-squares fit of equal-length plateaus (smooth, eased) to the tracked event->beat anchors."""
    if len(anchors) < 2:
        return [1.0], []
    av = np.array([a[0] for a in anchors])
    ao = np.array([a[1] for a in anchors])
    n = int(max(1, round(vd / knot_s)))
    bounds = [vd * k / n for k in range(1, n)]
    speeds = [1.0] * n
    # seed: average implied speed per knot interval
    for k in range(n):
        m = (av >= vd * k / n) & (av < vd * (k + 1) / n)
        if m.sum() >= 2:
            speeds[k] = float(np.clip((av[m][-1] - av[m][0]) / max(ao[m][-1] - ao[m][0], 1e-6), 1 - dev, 1 + dev))

    def ev_fn(warp):
        r = np.interp(av, warp[:, 0], warp[:, 1]) - ao
        return -400.0 * float(np.mean((r / beat_period) ** 2))

    sp, bd, _j, _j0 = optimize(vd, speeds, bounds, ev_fn, dev=dev, max_accel=max_accel, ease_max_s=ease_max_s,
                               ease=ease, split=False, reg=0.0,
                               steps=[(0.04, 0.0), (0.02, 0.0), (0.01, 0.0), (0.005, 0.0), (0.0025, 0.0)],
                               should_cancel=should_cancel)
    return sp, bd


# --- video planner -------------------------------------------------------------------------------------

def _sections_from_warp(warp):
    w = np.asarray(warp, dtype=float)
    secs = []
    for k in range(len(w) - 1):
        dv, do = w[k + 1, 0] - w[k, 0], w[k + 1, 1] - w[k, 1]
        if dv > 1e-9 and do > 1e-9:
            secs.append([w[k, 0], w[k + 1, 0], dv / do])
    merged = []
    for s in secs:
        if merged and abs(merged[-1][2] - s[2]) < 0.01:
            m = merged[-1]
            o_prev = (m[1] - m[0]) / m[2]
            o_new = (s[1] - s[0]) / s[2]
            m[1] = s[1]
            m[2] = (m[1] - m[0]) / (o_prev + o_new)
        else:
            merged.append(list(s))
    return merged


def _init_from_step(step_warp, vd, dev):
    """Plateau speeds + boundaries (source s) from a plan_retime warp; short sections folded into neighbours."""
    secs = _sections_from_warp(step_warp)
    if not secs:
        return [1.0], []
    secs[0][0], secs[-1][1] = 0.0, vd
    speeds = [s[2] for s in secs]
    bounds = [s[0] for s in secs[1:]]
    keep_s, keep_b = [speeds[0]], []
    for sp, b in zip(speeds[1:], bounds):                    # drop boundaries closer than MIN_SECTION_S
        if b - (keep_b[-1] if keep_b else 0.0) < MIN_SECTION_S:
            keep_s[-1] = sp
            continue
        keep_s.append(sp)
        keep_b.append(b)
    if keep_b and vd - keep_b[-1] < MIN_SECTION_S:
        keep_s.pop()
        keep_b.pop()
    return keep_s, keep_b


def _fit_anchors(step_warp, speeds, bounds, vd, lo, hi, max_accel, ease_max_s, ease, iters=6):
    """Scale plateau speeds so the eased curve spends the step plan's output time in each section."""
    w = np.asarray(step_warp, dtype=float)
    edges = [0.0] + list(bounds) + [vd]
    target = [float(np.interp(edges[k + 1], w[:, 0], w[:, 1]) - np.interp(edges[k], w[:, 0], w[:, 1]))
              for k in range(len(speeds))]
    _fn, slope = _ease(ease)
    sp = list(speeds)
    for _ in range(iters):
        warp, _s = warp_from_points(points_from(sp, bounds, vd, max_accel, ease_max_s, ease), vd, ease)
        for k in range(len(sp)):
            got = float(np.interp(edges[k + 1], warp[:, 0], warp[:, 1]) - np.interp(edges[k], warp[:, 0], warp[:, 1]))
            if target[k] > 1e-6 and got > 1e-6:
                sp[k] = float(np.clip(sp[k] * got / target[k], lo, hi))
        sp = _project(sp, bounds, vd, lo, hi, max_accel, ease_max_s, slope, passes=20)
    return sp


def _objective(song, video, offset, lead_s, tight_weight=TIGHT_WEIGHT):
    """J = combined (tempo, sync, arc) score of the video as warped (the pieces score_at uses) plus
    tight_weight * tightness %, so eased ramps are pushed to land events on the grid, not just near it."""
    def ev(warp):
        wv = R.apply_warp(video, warp)
        ev_t, ev_w = S._events(wv)
        has = len(ev_t) and len(song["beat_times"]) >= 2
        sync = S._sync_pct_at(song, ev_t, ev_w, float(wv["duration"]), offset, lead_s) if has else 0.0
        tempo_pct, _rate = S._tempo(song, wv)
        j = S._combine(tempo_pct, sync, S._arc_pct_at(song, wv, offset))
        if tight_weight:
            j += tight_weight * float(S.tightness(song, wv, offset, lead_s).get("tight_pct") or 0.0)
        return j
    return ev


# --- anchored planner: hold speed through every event->beat anchor, ramp only between -----------------

def _grid_targets(song, offset, lead, grid):
    ref = np.asarray(S._beats(song), dtype=float) - float(offset) - lead
    if grid == "half_beat" and len(ref) > 1:
        ref = np.sort(np.concatenate([ref, (ref[:-1] + ref[1:]) / 2]))
    return ref


def select_anchors(ev_t, ev_w, targets, step_warp, lo, hi, win_s=0.3, max_skip=3, rate_w=40.0, pos_w=8.0):
    """Choose a weighted-max chain of (event src time, grid out time) anchors: consecutive anchors imply a speed in
    [lo, hi] (the virtual anchor (0,0) starts the chain); candidates for each event are grid points within win_s
    of where the step plan puts it, and an anchor is charged for straying from the step plan's local speed/position."""
    order = np.argsort(ev_t)
    e = np.asarray(ev_t, dtype=float)[order]
    w = np.asarray(ev_w, dtype=float)[order]
    w = w / max(w.sum(), 1e-12)
    sw = np.asarray(step_warp, dtype=float)
    ws = np.interp(e, sw[:, 0], sw[:, 1])
    cands = []
    for i in range(len(e)):
        c = targets[(np.abs(targets - ws[i]) <= win_s) & (targets > 0.05)]
        cands.append(c if e[i] > 0.05 else np.zeros(0))
    NEG = -1e18
    best = [np.full(len(c), NEG) for c in cands]
    back = [[None] * len(c) for c in cands]
    for i in range(len(e)):
        for k, b in enumerate(cands[i]):
            gain = 100.0 * w[i] - pos_w * abs(b - ws[i])
            r0 = e[i] / b
            if lo - 1e-9 <= r0 <= hi + 1e-9:
                best[i][k], back[i][k] = gain - rate_w * abs(r0 - e[i] / max(ws[i], 1e-6)), None
            for j in range(max(0, i - 1 - max_skip), i):
                if e[i] - e[j] < 0.05:
                    continue
                rs = (e[i] - e[j]) / max(ws[i] - ws[j], 1e-6)
                for m, bm in enumerate(cands[j]):
                    if best[j][m] <= NEG / 2 or b - bm < 0.05:
                        continue
                    r = (e[i] - e[j]) / (b - bm)
                    if not (lo - 1e-9 <= r <= hi + 1e-9):
                        continue
                    v = best[j][m] + gain - rate_w * abs(r - rs)
                    if v > best[i][k]:
                        best[i][k], back[i][k] = v, (j, m)
    top, at = NEG, None
    for i in range(len(e)):
        for k in range(len(cands[i])):
            if best[i][k] > top:
                top, at = best[i][k], (i, k)
    out = []
    while at is not None:
        i, k = at
        out.append((float(e[i]), float(cands[i][k]), float(w[i])))
        at = back[i][k]
    return out[::-1]


def _seg_times(sp, e, hold, ease_fn, K=40):
    """Output seconds spent in each source gap [e_i, e_i+1] when speed holds `hold` (src s) at both ends and
    eases between (sp[i] -> sp[i+1]) in the middle. Vectorised midpoint quadrature."""
    G = np.diff(e)
    h = np.minimum(hold, 0.3 * G)
    u = (np.arange(K) + 0.5) / K
    x = G[:, None] * u[None, :]
    z = np.clip((x - h[:, None]) / np.maximum(G - 2 * h, 1e-9)[:, None], 0.0, 1.0)
    a = sp[:-1, None]
    speed = a + (sp[1:, None] - a) * ease_fn(z)
    return (G / K) * np.sum(1.0 / speed, axis=1)


def solve_anchor_speeds(e, b, lo, hi, max_accel, ease, hold=0.05, sigma_s=0.012, lam_smooth=0.6, lam_dev=0.05,
                        lam_acc=20.0, w=None):
    """Regularised least squares for the plateau speed at each anchor (e[0],b[0] = (0,0)). Residual = how far the
    cumulative output time at each anchor lands from its beat; penalties: speed steps (smoothness), distance from 1,
    and ramps steeper than max_accel (output-time width of a ramp = gap / speed)."""
    from scipy.optimize import least_squares
    fn, slope = _ease(ease)
    e = np.asarray(e, float)
    b = np.asarray(b, float)
    n = len(e)
    G = np.diff(e)
    h = np.minimum(hold, 0.3 * G)
    wt = np.ones(n - 1)
    if w is not None:                       # heavier events pull harder (mean weight 1, floor 0.5)
        ww = np.asarray(w[1:], float)
        wt = np.maximum(0.5, ww / max(ww.mean(), 1e-9))

    def res(sp):
        cum = np.cumsum(_seg_times(sp, e, hold, fn)) - b[1:]
        r1 = wt * cum / sigma_s
        ds = np.diff(sp)
        r2 = lam_smooth * ds / 0.03
        avg = 0.5 * (sp[1:] + sp[:-1])
        t_ramp = np.maximum(G - 2 * h, 1e-3) / avg
        viol = np.maximum(0.0, slope * np.abs(ds) / t_ramp - max_accel) / max_accel
        return np.concatenate([r1, r2, lam_acc * viol, lam_dev * (sp - 1.0) / 0.1])

    x0 = np.clip(np.append(G / np.maximum(np.diff(b), 1e-6), 1.0), lo, hi)     # implied gap speeds, last repeated
    x0 = np.clip(np.concatenate([[x0[0]], x0[:n - 1]]), lo, hi)
    sol = least_squares(res, x0, bounds=(lo, hi), method="trf", max_nfev=200)
    cum = np.cumsum(_seg_times(sol.x, e, hold, fn)) - b[1:]
    return sol.x, np.concatenate([[0.0], cum])


def anchored_points(e, speeds, vd, hold, ease_max_s=EASE_MAX_S):
    """Speed points from anchor times + plateau speeds: plateau i holds `hold` src-s either side of anchor i, ramps
    fill the rest of each gap (each point's ease is centred on its gap)."""
    G = np.diff(e)
    h = np.minimum(hold, 0.3 * G)
    pts = [{"t_src": 0.0, "speed": float(speeds[0]), "ease_in_s": 0.0, "ease_out_s": 0.0}]
    for i in range(1, len(e)):
        half = max(0.0, 0.5 * (G[i - 1] - 2 * h[i - 1]))
        mid = e[i - 1] + h[i - 1] + half
        pts.append({"t_src": float(mid), "speed": float(speeds[i]), "ease_in_s": float(half), "ease_out_s": float(half)})
    return pts


def plan_anchored(song, video, offset, step, *, max_speed_dev=0.25, max_accel=MAX_ACCEL, ease="smoothstep",
                  ease_max_s=EASE_MAX_S, lead=None, grid=None, hold_s=0.05, sigma_s=0.012, lam_smooth=0.6,
                  win_s=0.3, drop_iters=2, drop_ms=30.0, **_unused):
    """Anchor-hold ramps: lock events to the grid like the step plan, but with a continuous eased speed curve.
    Returns (points, info)."""
    vd = float(video["duration"])
    lo, hi = 1.0 - max_speed_dev, 1.0 + max_speed_dev
    lead = S._lead(lead)
    if grid is None:
        grid = S.tightness(song, R.apply_warp(video, step["warp"]), offset, lead).get("grid", "beat")
    targets = _grid_targets(song, offset, lead, grid)
    ev_t, ev_w = S._events(video)
    anchors = select_anchors(ev_t, ev_w, targets, step["warp"], lo, hi, win_s=win_s)
    info = {"grid": grid, "n_events": int(len(ev_t)), "n_candidates_chain": len(anchors), "dropped": 0}
    if len(anchors) < 2:
        return [{"t_src": 0.0, "speed": 1.0, "ease_in_s": 0.0, "ease_out_s": 0.0}], info
    cur = anchors
    for it in range(drop_iters + 1):
        e = np.array([0.0] + [a[0] for a in cur])
        b = np.array([0.0] + [a[1] for a in cur])
        w = np.array([0.0] + [a[2] for a in cur])
        sp, err = solve_anchor_speeds(e, b, lo, hi, max_accel, ease, hold=hold_s, sigma_s=sigma_s,
                                      lam_smooth=lam_smooth, w=w)
        bad = np.abs(err[1:]) * 1000.0 > drop_ms
        if it == drop_iters or not bad.any():
            break
        info["dropped"] += int(bad.sum())
        cur = [a for a, x in zip(cur, bad) if not x]
        if len(cur) < 2:
            break
    info["n_anchors"] = len(cur)
    info["anchor_err_ms_median"] = round(float(np.median(np.abs(err[1:])) * 1000.0), 1) if len(err) > 1 else None
    info["anchor_err_ms_p90"] = round(float(np.percentile(np.abs(err[1:]) * 1000.0, 90)), 1) if len(err) > 1 else None
    pts = anchored_points(e, sp, vd, hold_s, ease_max_s)
    return pts, info


def _finish(song, video, offset, lead, step, pts, ease, max_speed_dev, max_accel, extra=None):
    """Score + package a points plan (shared by the coordinate-descent and anchored planners)."""
    vd = float(video["duration"])
    full, sarr = warp_from_points(pts, vd, ease)
    wv = R.apply_warp(video, full)
    after = S.score_at(song, wv, offset, lead_s=lead)
    base = S.score_at(song, R.apply_warp(video, step["warp"]), offset, lead_s=lead)
    orig = S.score_at(song, video, offset, lead_s=lead)
    step_ = int(max(1, round(GRID_HZ / WARP_HZ)))
    idx = np.unique(np.append(np.arange(0, len(full), step_), len(full) - 1))
    wsamp = [[round(float(full[i, 0]), 3), round(float(full[i, 1]), 4)] for i in idx]
    cstep = int(max(1, round(GRID_HZ / SAMPLE_HZ)))
    cidx = np.unique(np.append(np.arange(0, len(full), cstep), len(full) - 1))
    curve = [[round(float(full[i, 0]), 3), round(float(full[i, 1]), 3), round(float(sarr[i]), 4)] for i in cidx]
    out = {
        "points": [{"t_src": round(p["t_src"], 3), "speed": round(p["speed"], 4),
                    "ease_in_s": round(p["ease_in_s"], 3), "ease_out_s": round(p["ease_out_s"], 3)} for p in pts],
        "curve": curve, "warp": wsamp, "out_duration": round(float(full[-1, 1]), 3),
        "ease": ease, "max_speed_dev": max_speed_dev, "max_accel": max_accel,
        "sync_before": orig["sync_pct"], "sync_step": base["sync_pct"], "sync_after": after["sync_pct"],
        "total_before": orig["total_pct"], "total_step": base["total_pct"], "total_after": after["total_pct"],
        "tight_step": base["tightness"], "tight_after": after["tightness"],
        "smooth_step": stats(step["warp"]), "smooth_ramp": stats(full),
        "speed_range": [round(float(sarr.min()), 3), round(float(sarr.max()), 3)],
        "step": step,
    }
    if extra:
        out.update(extra)
    return out


def plan_ramps(song: dict, video: dict, offset: float, max_speed_dev: float = 0.25,
               max_accel: float = MAX_ACCEL, ease: str = "smoothstep", ease_max_s: float = EASE_MAX_S,
               lead_s: float = None, *, step_plan: dict = None, optimize_points: bool = True,
               should_cancel=None, extra_targets=None, mode: str = "coord", **anchored_kw) -> dict:
    """Eased speed-point plan for `video` against `song` at `offset`.

    mode="coord": start from retime.plan_retime's event placement (step_plan, computed if not given),
    re-express it as plateaus + eased ramps, then optimize speeds/boundaries/splits against the real
    sync+arc+tempo score. mode="anchored": hold a constant speed through every event->beat anchor and ramp only
    between anchors (see plan_anchored); anchored_kw are its knobs (sigma_s, lam_smooth, hold_s, snap_s, ...).
    Returns {points, curve, warp, out_duration, sync_before, sync_step, sync_after, total_step, total_after,
    smooth_step, smooth_ramp, ...}; `warp` is sampled at WARP_HZ and feeds retime.apply_warp / the renderer."""
    vd = float(video["duration"])
    lead = S._lead(lead_s)
    step = step_plan or R.plan_retime(song, video, offset, max_speed_dev, lead_s=lead_s,
                                      should_cancel=should_cancel, extra_targets=extra_targets)
    if mode == "anchored":
        pts, info = plan_anchored(song, video, offset, step, max_speed_dev=max_speed_dev, max_accel=max_accel,
                                  ease=ease, ease_max_s=ease_max_s, lead=lead, **anchored_kw)
        return _finish(song, video, offset, lead, step, pts, ease, max_speed_dev, max_accel, {"anchored": info})
    lo, hi = 1.0 - max_speed_dev, 1.0 + max_speed_dev
    _fn, slope = _ease(ease)
    evaluate = _objective(song, video, offset, lead)
    cands = []
    sp0, bd0 = _init_from_step(step["warp"], vd, max_speed_dev)          # (a) the step plan, eased
    sp0 = _project(sp0, bd0, vd, lo, hi, max_accel, ease_max_s, slope)
    if bd0:
        sp0 = _fit_anchors(step["warp"], sp0, bd0, vd, lo, hi, max_accel, ease_max_s, ease)
    cands.append((sp0, bd0))
    ev_t, _w = S._events(video)                                          # (b) tracked event->beat anchors
    beats_out = np.asarray(S._beats(song), dtype=float) - float(offset) - lead
    beats_out = beats_out[(beats_out > 0) & (beats_out < vd / max(1e-6, lo) + 1.0)]
    if len(ev_t) >= 3 and len(beats_out) >= 3:
        anchors = track_anchors(ev_t, beats_out, lo, hi)
        if len(anchors) >= max(3, 0.3 * len(ev_t)):
            per = float(np.median(np.diff(beats_out)))
            cands.append(fit_plateaus(anchors, vd, max_speed_dev, max_accel=max_accel, ease_max_s=ease_max_s,
                                      ease=ease, beat_period=per, should_cancel=should_cancel))
    best = None
    for sp, bd in cands:
        if optimize_points:
            sp, bd, j, _j0 = optimize(vd, sp, bd, evaluate, dev=max_speed_dev, max_accel=max_accel,
                                      ease_max_s=ease_max_s, ease=ease, split=True, should_cancel=should_cancel)
        else:
            j = evaluate(warp_from_points(points_from(sp, bd, vd, max_accel, ease_max_s, ease), vd, ease)[0])
        if best is None or j > best[0]:
            best = (j, sp, bd)
    _j, speeds, bounds = best
    speeds, bounds = _merge_flat(speeds, bounds)
    pts = points_from(speeds, bounds, vd, max_accel, ease_max_s, ease)
    return _finish(song, video, offset, lead, step, pts, ease, max_speed_dev, max_accel)


def ramp_directions(points, vd=None):
    """Small helper for tests/UI: list of (t_src, +1 speeds up | -1 slows down | 0) per ramp."""
    out, prev = [], float(points[0]["speed"])
    for p in points[1:]:
        d = float(p["speed"]) - prev
        out.append((float(p["t_src"]), 0 if abs(d) < 0.005 else (1 if d > 0 else -1)))
        prev = float(p["speed"])
    return out
