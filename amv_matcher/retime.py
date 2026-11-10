"""Video section retime: a monotonic piecewise-linear warp that lands cuts/motion peaks on beats."""
from __future__ import annotations

import numpy as np

from . import score as S

MAX_JUMP = 0.15          # enforced limit on neighbouring section speed change
DP_JUMP = 0.14           # DP margin under MAX_JUMP (warp points are rounded)
BUCKET = 0.05            # incoming-speed buckets kept per DP state
MERGE_S = 0.15           # events closer than this are placed together
LOOKBACK = 6             # matched events a chain may skip back over
NEAR = 2                 # candidate beats per side of an event
TAIL_STEP_S = 1.0        # video seconds per tail ramp step
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


def _bucket(speed):
    return int(round(speed / BUCKET))


def _events_for_dp(song, video, offset, ev_t, ev_w, vd):
    """Events the warp should place: sync events (cuts/peaks -> beats) plus energy events
    (peak-segment starts -> drops, lull starts -> song lulls), merged when closer than MERGE_S."""
    W = max(float(np.sum(ev_w)), 1e-6)
    sync_unit = 100.0 * S.W_SYNC / W
    arc_events = [("drop", float(a)) for a, _b in video.get("peak_segments", [])] + \
                 [("lull", float(a), float(b - a)) for a, b in video.get("lulls", [])]
    arc_events = [e for e in arc_events if 0.05 < e[1] < vd]
    n_arc = len(arc_events)
    arc_unit = S.W_ARC * S.ARC_BONUS_PER_MATCH * min(1.0, (S.ARC_BONUS_MAX / S.ARC_BONUS_PER_MATCH) / max(1, n_arc))
    raw = [{"t": float(t), "sync": float(w), "drop": False, "lull": None} for t, w in zip(ev_t, ev_w)]
    for e in arc_events:
        raw.append({"t": e[1], "sync": 0.0, "drop": e[0] == "drop", "lull": e[2] if e[0] == "lull" else None})
    raw.sort(key=lambda d: d["t"])
    merged = []
    for e in raw:
        if merged and e["t"] - merged[-1]["t"] <= MERGE_S:
            m = merged[-1]
            m["sync"] += e["sync"]
            m["drop"] = m["drop"] or e["drop"]
            m["lull"] = m["lull"] if m["lull"] is not None else e["lull"]
        else:
            merged.append(dict(e))
    drops = np.asarray(song.get("drops", []), dtype=float) - offset
    lulls = [(float(a) - offset, float(b) - offset) for a, b in song.get("lulls", [])]
    return merged, sync_unit, arc_unit, drops, lulls


def _anchors(events, sync_unit, arc_unit, drops, lulls, beats_out, dev, jump):
    """DP over (event, candidate output time, incoming speed bucket).

    Chain links have speed in [1-dev, 1+dev]; consecutive link speeds differ by <= jump;
    plan_retime ramps the tail back to 1.0 after the last anchor.
    Returns [(video_t, out_t)]."""
    n = len(events)
    lo, hi = 1.0 - dev, 1.0 + dev
    cands = []
    for ev in events:
        e = ev["t"]
        k = int(np.searchsorted(beats_out, e))
        cs = {round(float(beats_out[j]), 4): True for j in range(max(0, k - NEAR), min(len(beats_out), k + NEAR))}
        if ev["drop"] and len(drops):
            for d in drops[np.argsort(np.abs(drops - e))[:2]]:
                cs.setdefault(round(float(d), 4), False)
        if ev["lull"] is not None:
            for sa, _sb in lulls:
                if abs(sa - e) < e * dev + 2.0:
                    cs.setdefault(round(sa, 4), False)
        lst = []
        for c, is_beat in sorted(cs.items()):
            if c <= 0:
                continue
            v = 0.0
            if ev["sync"] and (is_beat or (len(beats_out) and np.min(np.abs(beats_out - c)) <= 0.02)):
                v += sync_unit * ev["sync"]
            if ev["drop"] and len(drops) and np.min(np.abs(drops - c)) <= S.DROP_MATCH_S - 0.05:
                v += arc_unit
            if ev["lull"] is not None and any(sa < c + ev["lull"] and sb > c for sa, sb in lulls):
                v += arc_unit
            if v > 0:
                lst.append((c, v))
        cands.append(lst)
    ent = [[dict() for _ in lst] for lst in cands]   # ent[i][ci][bucket] = (score, prev_key|None, speed)
    for i, ev in enumerate(events):
        e = ev["t"]
        for ci, (c, val) in enumerate(cands[i]):
            slot = ent[i][ci]
            s0 = e / c
            if lo - 1e-9 <= s0 <= hi + 1e-9:     # head link from (0, 0): its own section, no jump
                slot[_bucket(s0)] = (val - SPEED_COST * abs(s0 - 1.0) * e * sync_unit, None, s0)
            for p in range(max(0, i - LOOKBACK), i):
                ep = events[p]["t"]
                if e - ep < 1e-6:
                    continue
                for pci, (cp, _pv) in enumerate(cands[p]):
                    if c <= cp + 1e-9:
                        continue
                    s = (e - ep) / (c - cp)
                    if s < lo - 1e-9 or s > hi + 1e-9:
                        continue
                    for pb, (ps, _pp, psp) in ent[p][pci].items():
                        if abs(psp - s) > jump + 1e-9:
                            continue
                        sc = ps + val - SPEED_COST * abs(s - 1.0) * (e - ep) * sync_unit
                        b = _bucket(s)
                        if b not in slot or sc > slot[b][0]:
                            slot[b] = (sc, (p, pci, pb), s)
    best, bkey = 0.0, None
    for i in range(n):
        for ci, slot in enumerate(ent[i]):
            for b, (sc, _pk, sp) in slot.items():
                if sc > best + 1e-12:
                    best, bkey = sc, (i, ci, b)
    out = []
    key = bkey
    while key is not None:
        i, ci, b = key
        out.append((float(events[i]["t"]), float(cands[i][ci][0])))
        key = ent[i][ci][b][1]
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
    events, sync_unit, arc_unit, drops, lulls = _events_for_dp(song, video, offset, ev_t, ev_w, vd)
    anchors = _anchors(events, sync_unit, arc_unit, drops, lulls, beats, max_speed_dev, DP_JUMP) \
        if events and (len(beats) or len(drops) or lulls) else []
    warp = [[0.0, 0.0]] + [[a, b] for a, b in anchors]
    if not anchors:
        warp.append([vd, vd])
    elif warp[-1][0] < vd:
        (v0, o0), (v1, o1) = warp[-2], warp[-1]
        sp = (v1 - v0) / max(o1 - o0, 1e-9)
        # tail: ramp the speed back to 1.0 in steps within the jump limit (TAIL_STEP_S video seconds
        # each), then play at 1.0, so out_duration only changes by what anchored sections need
        v, o = v1, o1
        while abs(sp - 1.0) > 1e-9 and v < vd - 1e-6:
            sp = sp + max(-DP_JUMP, min(DP_JUMP, 1.0 - sp))
            nv = min(vd, v + TAIL_STEP_S)
            o += (nv - v) / sp
            v = nv
            warp.append([v, o])
        if v < vd - 1e-9:
            warp.append([vd, o + (vd - v)])
    warp = [[round(a, 4), round(b, 4)] for a, b in warp]
    w = np.asarray(warp)
    spd = np.diff(w[:, 0]) / np.maximum(np.diff(w[:, 1]), 1e-9)
    max_jump = float(np.max(np.abs(np.diff(spd)))) if len(spd) > 1 else 0.0
    groups = _sections(warp, min_section_s)
    sections, moved_set = [], set(round(a, 4) for a, b in anchors if abs(a - b) > 0.02)
    for g in groups:
        sp = (g[1] - g[0]) / max(g[3] - g[2], 1e-9)
        if abs(sp - 1.0) <= 0.01:
            continue
        moved = sum(1 for e in ev_t if g[0] - 1e-6 <= e <= g[1] + 1e-6 and round(float(e), 4) in moved_set)
        sections.append({"v_start": round(g[0], 3), "v_end": round(g[1], 3), "speed": round(sp, 4),
                         "out_start": round(g[2], 3), "out_end": round(g[3], 3), "events_moved": moved,
                         "reason": "land %d event(s) on the beat grid at %.2fx" % (moved, sp)})
    wv = apply_warp(video, warp)
    after = S.score_at(song, wv, offset)["sync_pct"]
    return {"sections": sections, "warp": warp, "out_duration": round(float(warp[-1][1]), 3),
            "sync_before": before, "sync_after": after, "max_speed_jump": round(max_jump, 3),
            "speed_jump_ok": max_jump <= MAX_JUMP + 1e-9, "max_speed_dev": max_speed_dev}
