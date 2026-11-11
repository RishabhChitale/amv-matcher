"""Build-up / launch scoring: does the video's motion LAUNCH where the song builds into a drop?

video_launches(video)  -> [{t, strength}]  moments where a sustained motion-speed ramp ends in a peak block.
song_buildups(song, stems_feats=None) -> [{start, drop, strength}]  rising energy / onset density over 2-16 s
                                        ending at a drop (stems' drums onset density sharpens it when given).
build_stats(song, video, off) -> {build_pct, launch_pct, intensity_pct, n_launches, n_builds} or None when
                                 neither side has any build/launch (then the total keeps its old weights).
"""
from __future__ import annotations

import numpy as np

from ._memo import IdMemo, frozen

HZ = 10.0
SMOOTH_S = 1.0
RAMP_MIN_S = 2.0
RAMP_MAX_S = 16.0
LAUNCH_AFTER_S = 2.0           # window after the candidate launch compared with the ramp before it
LAUNCH_MIN_RISE = 0.25         # normalised speed rise (0..1 curve) that counts as a launch
MATCH_BEATS = 1.0              # a launch lands on a drop within +-1 beat
MATCH_MIN_S = 0.25
MATCH_MAX_S = 0.8
RECALL_W = 0.8
W_LAUNCH = 0.7
W_INTENSITY = 0.3
EDGE_STRENGTH = 0.5
CHORUS_LABELS = ("chorus", "drop")
W_BUILD = 0.20                 # share of the total when build_pct applies (others scaled by 1 - W_BUILD)


def _smooth(x, n):
    n = max(1, int(n))
    if n == 1 or len(x) < 2:
        return np.asarray(x, dtype=float)
    k = np.ones(n) / n
    return np.convolve(np.pad(np.asarray(x, dtype=float), (n // 2, n - 1 - n // 2), mode="edge"), k, mode="valid")


def _norm01(x):
    x = np.asarray(x, dtype=float)
    if len(x) == 0:
        return x
    lo, hi = np.percentile(x, 5), np.percentile(x, 95)
    return np.clip((x - lo) / max(hi - lo, 1e-9), 0.0, 1.0)


def speed_curve(video: dict) -> np.ndarray:
    """Normalised (0..1) motion speed at HZ: the per-frame motion curve resampled and smoothed;
    falls back to the 10 Hz energy curve."""
    mc = np.asarray(video.get("motion_curve") or [], dtype=float)
    dur = float(video.get("duration") or 0.0)
    if len(mc) >= 8 and dur > 0:
        n = max(2, int(round(dur * HZ)))
        src = np.linspace(0.0, dur, len(mc))
        mc = np.interp(np.linspace(0.0, dur, n), src, mc)
    else:
        mc = np.asarray(video.get("energy_curve") or [], dtype=float)
    return _norm01(_smooth(mc, SMOOTH_S * HZ))


def _ramp_launches(curve, min_rise, min_gap_s):
    """Indices where mean(after) - mean(before ramp) is a local max above min_rise and the lead-in rises."""
    n = len(curve)
    a = int(LAUNCH_AFTER_S * HZ)
    b = int(RAMP_MIN_S * HZ)
    if n < a + b + 2:
        return []
    pre = np.convolve(curve, np.ones(b) / b, mode="full")[:n]            # mean of the b samples ending at i
    post = np.convolve(curve, np.ones(a) / a, mode="full")[a - 1:][:n]   # mean of the a samples starting at i
    rise = np.full(n, -1.0)
    rise[b:n - a] = (post - np.roll(pre, 1))[b:n - a]
    out, last = [], -1e9
    order = np.argsort(-rise)
    taken = []
    for i in order:
        if rise[i] < min_rise:
            break
        if all(abs(i - j) >= min_gap_s * HZ for j in taken):
            taken.append(int(i))
    for i in sorted(taken):
        out.append((i, float(rise[i])))
    return out


_VMEMO = IdMemo(32)


def _vkey(video):
    return (video, video.get("motion_curve"), video.get("energy_curve"), video.get("segments_struct"))


def _speed(video) -> np.ndarray:
    """speed_curve(video), memoised on the video's feature lists (read-only)."""
    objs = _vkey(video) + ("speed",)
    key, hit = _VMEMO.get(objs, (video.get("duration"),))
    if hit is not None:
        return hit
    return _VMEMO.put(key, objs, frozen(speed_curve(video)))


def _hits_cached(video) -> tuple:
    objs = _vkey(video) + ("hits",)
    key, hit = _VMEMO.get(objs, (video.get("duration"),))
    if hit is not None:
        return hit
    return _VMEMO.put(key, objs, tuple(video_hits(video)))


def video_launches(video: dict) -> list:
    """[{t, strength}] where a sustained speed ramp ends in a peak block (t = block start, video time)."""
    c = _speed(video)
    out = []
    for i, r in _ramp_launches(c, LAUNCH_MIN_RISE, RAMP_MIN_S):
        # the step is found on smoothed curves and lands early: the launch is where the speed first reaches
        # ~90% of the peak that follows (end of the ramp)
        w = c[i:i + int((RAMP_MIN_S + LAUNCH_AFTER_S) * HZ)]
        k = int(np.argmax(w >= 0.9 * w.max())) if len(w) else 0
        out.append({"t": round((i + k) / HZ, 2), "strength": round(min(1.0, r), 3)})
    return out


def video_hits(video: dict) -> list:
    """[{t, strength, kind}] video moments that can hit a drop: motion launches (speed ramp ends in a peak)
    plus the structural edges of the video's build/peak blocks (a block that starts out of calm = the
    build-up is released; the end of a peak block = the impact the cut is held for). Real AMVs mark a
    drop with either, so both count."""
    out = [dict(l, kind="launch") for l in video_launches(video)]
    segs = video.get("segments_struct") or []
    for i, sg in enumerate(segs):
        k = sg.get("kind")
        if k in ("build", "peak") and i > 0 and segs[i - 1].get("kind") == "calm":
            out.append({"t": round(float(sg["start"]), 2), "strength": EDGE_STRENGTH, "kind": "onset"})
        if k == "peak" and i + 1 < len(segs):
            out.append({"t": round(float(segs[i + 1]["start"]), 2), "strength": EDGE_STRENGTH, "kind": "impact"})
    return sorted(out, key=lambda d: d["t"])


def song_buildups(song: dict, stems_feats: dict = None) -> list:
    """[{start, drop, strength}] for each song drop that is preceded by a rising-energy ramp (2-16 s)."""
    e = _norm01(_smooth(song.get("energy_curve") or [], SMOOTH_S * HZ))
    drops = [float(d) for d in (song.get("drops") or [])]
    dr = None
    if stems_feats:
        d = (stems_feats.get("stems") or {}).get("drums")
        if d and d.get("onset_strength"):
            dr = _norm01(_smooth(d["onset_strength"], 1))
    out = []
    # drop candidates: detected drops plus chorus-section entries (the detector can miss a drop on songs
    # whose energy rises gradually), merged within 2 s
    cand = list(drops)
    for sec in song.get("sections") or []:
        if str(sec.get("label", "")).startswith(CHORUS_LABELS) and all(abs(float(sec["start"]) - d) > 2.0 for d in cand):
            cand.append(float(sec["start"]))
    for d in sorted(cand):
        i = int(round(d * HZ))
        if i <= int(RAMP_MIN_S * HZ) or i >= len(e):
            continue
        lo = max(0, i - int(RAMP_MAX_S * HZ))
        seg = e[lo:i]
        if len(seg) < int(RAMP_MIN_S * HZ):
            continue
        # a drop is the recovery after the build's last dip: compare the energy after it with the lowest
        # point of the window before it
        j = lo + int(np.argmin(seg))
        rise = float(e[i:i + int(LAUNCH_AFTER_S * HZ)].mean() - e[j])
        lead = 0.0
        s = min(1.0, max(0.0, rise))
        if dr is not None and len(dr) > i:
            s = 0.7 * s + 0.3 * float(max(0.0, dr[i:i + int(LAUNCH_AFTER_S * HZ)].mean() - dr[j:i].mean()))
        if rise >= LAUNCH_MIN_RISE:
            out.append({"start": round(j / HZ, 2), "drop": round(d, 2), "strength": round(min(1.0, s), 3)})
    return out


_SMEMO = IdMemo(64)


def _song_energy01(song) -> np.ndarray:
    ec = song.get("energy_curve")
    if not isinstance(ec, list) or not ec:
        return _norm01(np.asarray(ec or [], dtype=float))
    key, hit = _SMEMO.get((ec, "e01"))
    if hit is not None:
        return hit
    return _SMEMO.put(key, (ec, "e01"), frozen(_norm01(np.asarray(ec, dtype=float))))


def _buildups_cached(song, stems_feats=None) -> list:
    """song_buildups(song) memoised on the song's feature lists (stems_feats bypasses the memo)."""
    if stems_feats:
        return song_buildups(song, stems_feats)
    objs = (song, song.get("energy_curve"), song.get("drops"), song.get("sections"), "builds")
    key, hit = _SMEMO.get(objs)
    if hit is not None:
        return hit
    return _SMEMO.put(key, objs, tuple(song_buildups(song)))


def _intensity_pct(song, video, off):
    sp = _speed(video)
    se = _song_energy01(song)
    i0 = int(round(off * HZ))
    j0 = max(0, -i0)
    seg = se[max(i0, 0):max(i0, 0) + max(len(sp) - j0, 0)]
    m = min(len(seg), len(sp) - j0)
    if m < 10:
        return 50.0
    v = sp[j0:j0 + m]
    s = seg[:m]
    if np.std(v) < 1e-9 or np.std(s) < 1e-9:
        return 50.0
    return float(50.0 * (np.corrcoef(v, s)[0, 1] + 1.0))


def build_stats(song: dict, video: dict, off: float, stems_feats: dict = None):
    """None when neither side has a build/launch. Otherwise build_pct = W_LAUNCH * launch match (strength
    weighted share of builds+launches that coincide within +-1 beat) + W_INTENSITY * speed~energy correlation."""
    vd = float(video.get("duration") or 0.0)
    L = _hits_cached(video)
    B = [b for b in _buildups_cached(song, stems_feats) if -1.0 <= b["drop"] - off <= vd + 1.0]
    if not B:
        return None                  # nothing to build into: build_pct does not apply
    bt = np.asarray(song.get("beat_times") or [], dtype=float)
    beat = float(np.median(np.diff(bt))) if len(bt) > 2 else 0.5
    tol = float(np.clip(MATCH_BEATS * beat, MATCH_MIN_S, MATCH_MAX_S))
    drops = np.array([b["drop"] - off for b in B])
    lt = np.array([l["t"] for l in L])
    wb = np.array([b["strength"] for b in B]) if B else np.array([])
    wl = np.array([l["strength"] for l in L]) if L else np.array([])
    hit_b = np.array([bool(len(lt) and np.min(np.abs(lt - d)) <= tol) for d in drops]) if len(drops) else np.array([], bool)
    hit_l = np.array([bool(len(drops) and np.min(np.abs(drops - t)) <= tol) for t in lt]) if len(lt) else np.array([], bool)
    recall = 100.0 * wb[hit_b].sum() / max(wb.sum(), 1e-9)                 # song builds the video launches into
    prec = 100.0 * wl[hit_l].sum() / max(wl.sum(), 1e-9) if len(wl) else 0.0
    launch = RECALL_W * recall + (1.0 - RECALL_W) * prec
    inten = _intensity_pct(song, video, off)
    return {"build_pct": round(W_LAUNCH * launch + W_INTENSITY * inten, 1), "launch_pct": round(launch, 1),
            "intensity_pct": round(inten, 1), "n_launches": len(L), "n_builds": len(B)}


def blend_total(base_total: float, stats) -> float:
    """Fold build_pct into a 0..100 total as a bonus: (1-W_BUILD)*base + W_BUILD*build, but never below base
    (like stem sync: well-landed launches lift the score, missing ones do not punish a pair that already syncs;
    ground-truth own pairs stay >= 80). Unchanged when stats is None."""
    if stats is None:
        return base_total
    return max(base_total, (1.0 - W_BUILD) * base_total + W_BUILD * stats["build_pct"])
