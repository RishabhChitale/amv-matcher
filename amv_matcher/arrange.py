"""Section-level song arrangement: reorder / repeat whole song SECTIONS so the song's energy
shape follows the video's calm / build / peak structure.

plan_arrangement(song, video, max_moves=3, lead_s=None) -> {
    segments: [{src_start, src_end, dst_start}]   (same shape as splice.plan_splice)
    sections_used: [label, ...]                   (one per section placed, output order)
    section_order: [index, ...]                   (indexes into song["sections"])
    moves, arc_before, arc_after, total_before, total_after, ...}

Rules:
  * Every cut is at a section boundary; section edges come from structure.py and are downbeat
    (bar) aligned, so no cut lands mid-bar. Only the LAST section is trimmed, so the output length
    equals the video duration exactly (the trim point is the one non-bar-aligned edge, as in splice).
  * A "move" is a jump: the next placed section is not the one that follows the previous one in
    the song. The first section is free (not a move). At most `max_moves` moves.
  * Musical sanity: no more than 2 consecutive sections of the same repeat group, a section is never
    placed twice in a row, and starting on an intro / chorus / drop is preferred (soft penalty).
  * Search: beam search over section sequences ranked by a cheap energy-arc proxy against the
    video energy curve; every finished candidate (and the plain single trim, the baseline) is then
    scored exactly with score.score_at() on the assembled song (with lead_s) and ranked like autosync:
    total, then tight_pct, then median error, then fewer moves. Baseline wins ties, so total_after >= total_before.
"""
from __future__ import annotations

import numpy as np

from . import motif
from . import score as S
from . import splice as SP
from ._memo import pearson as _pearson, pstd

MOVE_PENALTY = 0.6        # combined-score points charged per move
START_PENALTY = 0.6       # soft penalty when the first section is not an intro / chorus / drop
MAX_REPEAT = 2            # max consecutive sections of one repeat group
BEAM = 40
MIN_SECTION_S = 1.0
GOOD_START = ("intro", "intro (hook)", "chorus", "drop")
MAX_STEPS = 40
HZ = S.ENERGY_HZ


def _empty(song, video, why, base=None):
    r = {"eligible": False, "reason": why, "segments": [], "sections_used": [], "section_order": [],
         "moves": 0, "arc_before": None, "arc_after": None, "total_before": None, "total_after": None}
    if base:
        r.update(arc_before=base["arc_pct"], arc_after=base["arc_pct"],
                 total_before=base["total_pct"], total_after=base["total_pct"])
    return r


def _norm(x):
    x = np.asarray(x, dtype=float)
    hi = float(x.max()) if len(x) else 0.0
    return x / hi if hi > 1e-9 else x


def _secs(song):
    out = []
    for i, s in enumerate(song.get("sections") or []):
        a, b = float(s["start"]), float(s["end"])
        if b - a >= MIN_SECTION_S:
            out.append({"i": i, "start": a, "end": b, "label": s.get("label", ""),
                        "group": s.get("repeat_group", str(i))})
    return out


def _segments(secs, order, vd, entry=0.0):
    """Segments for an ordered list of section positions; the last one is trimmed to end at vd.
    `entry` skips that many seconds of the FIRST section (an entry point, not a cut)."""
    segs, t = [], 0.0
    for k, ix in enumerate(order):
        s = secs[ix]
        a0 = s["start"] + (entry if k == 0 else 0.0)
        d = s["end"] - a0
        if k == len(order) - 1 or t + d >= vd - 1e-9:
            d = vd - t
        segs.append({"src_start": round(a0, 6), "src_end": round(a0 + d, 6),
                     "dst_start": round(t, 6)})
        t += d
    return segs


def _merge(segs):
    """Join segments that are contiguous in the song AND in the output (not a cut at all)."""
    out = []
    for g in segs:
        if out and abs(out[-1]["src_end"] - g["src_start"]) < 1e-6:
            out[-1] = {**out[-1], "src_end": g["src_end"]}
        else:
            out.append(dict(g))
    return out


def _assemble(song, segs, vd):
    """splice._assemble plus the grid / onset keys score.py uses, remapped through the segments."""
    asm = SP._assemble(song, segs, vd)
    g = np.asarray(song.get("beat_grid") or [], dtype=float)
    bpb = int(song.get("beats_per_bar") or 4)
    ph = int(song.get("bar_phase") or 0) % bpb
    og, first_bar = [], None
    for sg in segs:
        a, b, d0 = sg["src_start"], sg["src_end"], sg["dst_start"]
        for k in np.nonzero((g >= a) & (g < b))[0]:
            if first_bar is None and int(k) % bpb == ph:
                first_bar = len(og)
            og.append(float(g[k] - a + d0))
    asm.update({"beat_grid": og, "grid_bpm": song.get("grid_bpm", 0), "grid_used": song.get("grid_used", False),
                "grid_period": song.get("grid_period", 0), "beats_per_bar": bpb,
                "bar_phase": (first_bar or 0) % bpb})
    env, fps = song.get("onset_env"), float(song.get("onset_env_fps") or 0)
    if env is not None and len(env) and fps > 0:
        env = np.asarray(env, dtype=float)
        out = np.zeros(int(np.ceil(vd * fps)) + 1)
        for sg in segs:
            i0, o0 = int(round(sg["src_start"] * fps)), int(round(sg["dst_start"] * fps))
            n = min(int(round((sg["src_end"] - sg["src_start"]) * fps)), len(out) - o0, len(env) - i0)
            if n > 0 and i0 >= 0:
                out[o0:o0 + n] = env[i0:i0 + n]
        asm["onset_env"], asm["onset_env_fps"] = out.tolist(), fps
    return asm


def _key(r, moves):
    """Same lexicographic objective as autosync: displayed total, then tight_pct, then a lower
    median error; fewer moves only as the last tie-breaker (no score points)."""
    t = r.get("tightness") or {}
    me = t.get("median_err_ms")
    return (int(round(float(r["total_pct"]))), float(t.get("tight_pct") or 0.0),
            -(float(me) if me is not None else 1e9), -moves)


ENERGY_GUARD_TOTAL = 0     # prefer_energy: candidate total may be at most this many points below the baseline
ENERGY_GUARD_TIGHT = 0.5   # ... tight_pct at most this much below
ENERGY_GUARD_SYNC = 0.5    # ... sync_pct at most this much below


def _energy_key(r, moves, guard):
    """prefer_energy objective: highest arc among candidates that stay within the guards of the
    baseline (total, tight_pct, sync_pct), then total, tight_pct, fewer moves."""
    t = r.get("tightness") or {}
    tp = float(t.get("tight_pct") or 0.0)
    ok = (r["total_pct"] >= guard["total"] - ENERGY_GUARD_TOTAL and tp >= guard["tight"] - ENERGY_GUARD_TIGHT
          and float(r.get("sync_pct", 0)) >= guard["sync"] - ENERGY_GUARD_SYNC)
    return (1 if ok else 0, round(float(r["arc_pct"]), 1), int(round(float(r["total_pct"]))), tp, -moves)


def _eval(song, video, segs, ncuts, lead_s=None, guard=None):
    vd = float(video["duration"])
    asm = _assemble(song, segs, vd)
    r = S.score_at(asm, video, 0.0, lead_s=lead_s)
    return {"r": r, "key": _energy_key(r, ncuts, guard) if guard else _key(r, ncuts)}


def _entries(song, secs, o2, off):
    """Entry points into the first section that keep the song's bar lattice aligned with the
    baseline's best offset: bar-multiples only, so the video cuts still land on the same beats."""
    s0 = secs[o2[0]]
    bar = float(song.get("grid_period") or 0) * int(song.get("beats_per_bar") or 4)
    if bar <= 0.2 or not song.get("grid_used"):
        return [0.0]
    first = (off - s0["start"]) % bar
    out = [0.0]
    e = first
    while e < s0["end"] - s0["start"] - 1.5 * bar and len(out) < 12:
        if e > 1e-6:
            out.append(round(e, 6))
        e += bar
    return out


def _ok_next(secs, order, nxt):
    if order and order[-1] == nxt:
        return False
    run = 1
    g = secs[nxt]["group"]
    for ix in reversed(order):
        if secs[ix]["group"] == g:
            run += 1
        else:
            break
    return run <= MAX_REPEAT


def _proxy(secs, order, venv, vd, pearson=False):
    """Mean squared error between normalised song energy as arranged and the video energy, over
    the covered prefix (lower is better)."""
    parts, n = [], 0
    for ix in order:
        parts.append(secs[ix]["en"])
    cat = np.concatenate(parts) if parts else np.zeros(0)
    m = min(len(cat), len(venv))
    if m == 0:
        return 1e9
    if pearson:       # prefer_energy: rank by video-vs-song energy correlation (what the arc score rewards)
        a, b = cat[:m], venv[:m]
        if m < 5 or pstd(a) < 1e-9 or pstd(b) < 1e-9:
            return 1e9
        return float((1.0 - _pearson(a, b)) * 50.0)
    return float(np.mean((cat[:m] - venv[:m]) ** 2)) * 100.0


def plan_arrangement(song: dict, video: dict, max_moves: int = 3, lead_s: float | None = None,
                     prefer_energy: bool = False) -> dict:
    """prefer_energy=False (default) is the original objective. True ranks the beam by energy
    correlation and picks the highest-arc arrangement that keeps total / tight_pct / sync_pct within
    the ENERGY_GUARD_* margins of the baseline (opt-in; see docs/ARC-RESEARCH.md on g-arc)."""
    vd, sd = float(video["duration"]), float(song["duration"])
    if 0.0 < sd < vd:
        # F2b: a song shorter than the video is arranged on the best video stretch of its length; the plan's
        # times are clip times and plan["video_window"] names the source stretch.
        return SP.on_video_window(song, video, lead_s, lambda clip: plan_arrangement(
            song, clip, max_moves, lead_s, prefer_energy))
    base = S.score(song, video, lead_s=lead_s)
    if not base.get("eligible", True):
        return _empty(song, video, base.get("reason") or "unreadable")
    off = float(base["best_offset_s"])
    base_segs = SP._single(off, vd)
    base_total = int(base["total_pct"])
    base_arc = float(base["arc_pct"])

    def pack(segs, order, secs, r, obj, moves):
        return {"eligible": True, "segments": _merge(segs), "sections_used": [secs[i]["label"] for i in order],
                "section_order": [secs[i]["i"] for i in order], "moves": moves,
                "arc_before": base_arc, "arc_after": float(r["arc_pct"]),
                "total_before": base_total, "total_after": int(r["total_pct"]),
                "objective": list(obj), "baseline": {"offset": off},
                "improvement_pct": int(r["total_pct"]) - base_total}

    secs = _secs(song)
    gate = motif.gate(song)
    guard = None
    if prefer_energy:
        _bt = base.get("tightness") or {}
        guard = {"total": base_total, "tight": float(_bt.get("tight_pct") or 0.0),
                 "sync": float(base.get("sync_pct", 0))}
    base_r = _eval(song, video, base_segs, 0, lead_s, guard)
    best = (base_segs, [], base, base_r["key"], 0)
    if max_moves < 0 or len(secs) < 2 or float(song["duration"]) < vd:
        return pack(best[0], [], secs, best[2], best[3], 0)

    se = np.asarray(song["energy_curve"], dtype=float)
    shz = float(song.get("energy_hz", HZ))
    ve = np.asarray(video["energy_curve"], dtype=float)
    vhz = float(video.get("energy_hz", HZ))
    # resample both energy curves onto a common 10 Hz grid, normalised to their own max
    nv = int(round(vd * HZ))
    venv = _norm(np.interp(np.arange(nv) / HZ, np.arange(len(ve)) / vhz, ve)) if len(ve) else np.zeros(nv)
    senv = _norm(se)
    for s in secs:
        a, b = int(round(s["start"] * shz)), max(int(round(s["end"] * shz)), int(round(s["start"] * shz)) + 1)
        seg = senv[a:b]
        s["en"] = np.interp(np.arange(int(round((s["end"] - s["start"]) * HZ))) / HZ,
                            np.arange(len(seg)) / shz, seg) if len(seg) else np.zeros(1)
        s["dur"] = s["end"] - s["start"]

    k0 = next((j for j, sc in enumerate(secs) if sc["start"] <= off < sc["end"]), None)
    # beam: state = (order tuple, moves, covered seconds)
    beam = [((k,), 0, secs[k]["dur"]) for k in range(len(secs))]
    done = []
    for _ in range(MAX_STEPS):
        nxt_beam = []
        for order, mv, cov in beam:
            if cov >= vd - 1e-9:
                done.append((order, mv))
                continue
            for k in range(len(secs)):
                if not _ok_next(secs, list(order), k):
                    continue
                jump = k != order[-1] + 1
                m2 = mv + (1 if jump else 0)
                if m2 > max_moves:
                    continue
                nxt_beam.append((order + (k,), m2, cov + secs[k]["dur"]))
        if not nxt_beam:
            break
        # rank by proxy + soft penalties; keep the best BEAM (separately per move count so a
        # cheap move-free prefix is never crowded out)
        scored = sorted(nxt_beam, key=lambda st: _proxy(secs, st[0], venv, vd, prefer_energy) + MOVE_PENALTY * st[1]
                        + (0 if secs[st[0][0]]["label"] in GOOD_START else START_PENALTY))
        beam = scored[:BEAM]
        # keep a protected lane of states that start in the section the baseline trim starts in:
        # these keep the baseline's beat/drop alignment and only rearrange what follows
        if k0 is not None:
            have = set(b[0] for b in beam)
            beam += [st for st in [x for x in scored if x[0][0] == k0][:BEAM // 2] if st[0] not in have]
    done += [(o, m) for o, m, c in beam if c >= vd - 1e-9]

    seen = set()
    for order, mv in done:
        # drop sections that start after the video is already filled (the trim handles the tail)
        t, o2 = 0.0, []
        for ix in order:
            if t >= vd - 1e-9:
                break
            o2.append(ix)
            t += secs[ix]["dur"]
        key = tuple(o2)
        if key in seen:
            continue
        seen.add(key)
        for ent in _entries(song, secs, o2, off):
            segs = _segments(secs, o2, vd, ent)
            if gate is not None and not gate.ok(segs[0]["src_start"]):
                continue                          # F17: the arranged song must start on a valid start
            if any(g["src_end"] - g["src_start"] <= 1e-6 or g["src_end"] > float(song["duration"]) + 1e-6 for g in segs):
                continue
            merged = _merge(segs)
            moves = max(0, len(merged) - 1)
            if moves > max_moves:
                continue
            e = _eval(song, video, merged, 0, lead_s, guard)
            obj = e["key"]
            if obj > best[3]:
                best = (segs, o2, e["r"], obj, moves)
    return pack(best[0], best[1], secs, best[2], best[3], best[4])
