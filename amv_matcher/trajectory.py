"""F22: intensity-trajectory coupling, scored as closeness to a reference profile.

Videos edited to music build their motion up into drops. This module compares the slow intensity
trajectory of the video with the song's over the scored overlap (song time = video time + offset,
as in score._arc_pct_at):

  video trajectory  score._video_story_curve (motion energy + cut density + camera + subject motion)
  song trajectory   the song energy_curve (loudness + onset density + low band)

Both are box-smoothed over SMOOTH_S and robust-normalised over the WHOLE song / video (2nd..98th
percentile), so the numbers compare across media although the stored curves are min-max normalised
per file.

Coupling profile (profile_of):
  follow    best Pearson of the two trajectories within +-LAG_S (lag-tolerant shape match)
  lag_s     the lag that gives it (song later than video > 0)
  dyn_video / dyn_song   p90 - p10 of each trajectory over the overlap; ratio = dyn_song / dyn_video
  peak      share of the video's top-quartile time where the song sits in its own upper half
            (0.5 = chance, 1 = every visual peak lands on a loud stretch)

sub_score() is 100 * closeness of a profile to REFERENCE: the profile of the user's gold-standard
pair (an edit with its own audio, measured on real media). Generic constants only: nothing here
knows a timestamp or a file. The reference reaches 100 exactly. Weights: follow W_FOLLOW, ratio
W_RATIO, peak W_PEAK.

Two consumers:
  * score._pack: only when AMV_F22=1, total = (1 - W_TRAJ) * total + W_TRAJ * sub_score
    (weighted_total). With the flag OFF nothing in the scorer changes.
  * autosync: result_block() is ALWAYS emitted as final["trajectory"] (UI data: curves in output
    time for the faint lines under V1/A1, ramps into the strongest visual booms, the profile and
    the sub-score). It never feeds a total or a plan choice.
"""
from __future__ import annotations

import os

import numpy as np

from ._memo import IdMemo, frozen

F22_ENABLED = os.environ.get("AMV_F22", "") == "1"

HZ = 10.0
SMOOTH_S = 4.0          # slow trajectory: build-ups / arcs, not single hits
LAG_S = 2.0             # lag tolerance for the shape correlation
LAG_STEP_S = 0.5
MIN_OVERLAP_S = 8.0

# Reference coupling profile (gold-standard pair, measured) and how closeness is scored.
REFERENCE = {"follow": 0.76, "ratio": 1.18, "peak": 1.0}
RATIO_DEAD_OCT = 0.15   # within +-0.15 octave of the reference ratio counts as a full match
RATIO_SPAN_OCT = 1.5    # closeness reaches 0 this many octaves beyond the dead zone
PEAK_CHANCE = 0.5
W_FOLLOW, W_RATIO, W_PEAK = 0.5, 0.25, 0.25
W_TRAJ = 0.15           # share of the total the sub-score takes when the flag is on

UI_HZ = 4.0             # resolution of the curves handed to the UI
RAMP_S = 6.0            # ramp = rise over the RAMP_S before a visual boom (1 s smoothing)
RAMP_MIN_STRENGTH = 0.5
RAMP_MAX = 8

_CURVE = IdMemo(64)


def _smooth(x, hz=HZ, smooth_s=SMOOTH_S):
    x = np.asarray(x, dtype=float)
    if len(x) < 3:
        return np.zeros(len(x))
    w = max(1, int(round(smooth_s * hz)))
    pad = np.pad(x, (w // 2, w - 1 - w // 2), mode="edge")
    return np.convolve(pad, np.ones(w) / w, mode="valid")[:len(x)]


def _norm(s):
    if len(s) < 3:
        return np.zeros(len(s))
    lo, hi = np.percentile(s, 2), np.percentile(s, 98)
    if hi - lo < 1e-9:
        return np.zeros(len(s))
    return np.clip((s - lo) / (hi - lo), 0.0, 1.0)


def _smooth_norm(x, hz=HZ, smooth_s=SMOOTH_S):
    return _norm(_smooth(x, hz, smooth_s))


def _on_grid(x, hz):
    from .score import _on_grid as g
    return g(x, hz)


def _song_raw(song):
    return _on_grid(song.get("energy_curve") or [], song.get("energy_hz"))


def _video_raw(video):
    from .score import _video_story_curve
    return _video_story_curve(video)


def song_trajectory(song, smooth_s=SMOOTH_S) -> np.ndarray:
    """Normalised slow intensity of the song at HZ (read-only, memoised on the energy list)."""
    e = song.get("energy_curve") or []
    key, hit = _CURVE.get((e,), ("s", smooth_s))
    if hit is not None:
        return hit
    return _CURVE.put(key, (e,), frozen(_smooth_norm(_song_raw(song), smooth_s=smooth_s)))


def video_trajectory(video, smooth_s=SMOOTH_S) -> np.ndarray:
    """Normalised slow intensity of the video at HZ (story curve: motion energy + subject)."""
    e, s = video.get("energy_curve") or [], video.get("subject_curve") or []
    key, hit = _CURVE.get((e, s), ("v", smooth_s))
    if hit is not None:
        return hit
    return _CURVE.put(key, (e, s), frozen(_smooth_norm(_video_raw(video), smooth_s=smooth_s)))


def dyn_range(x) -> float:
    x = np.asarray(x, dtype=float)
    return float(np.percentile(x, 90) - np.percentile(x, 10)) if len(x) >= 3 else 0.0


def _pearson(a, b):
    a = a - a.mean()
    b = b - b.mean()
    d = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 1e-12 else 0.0


def follow_lag(v, s):
    """(best Pearson, lag in s) of the two trajectories within +-LAG_S; lag > 0 = song later."""
    m = min(len(v), len(s))
    if m < 5:
        return 0.0, 0.0
    v, s = v[:m], s[:m]
    L = int(round(LAG_S * HZ))
    step = max(1, int(round(LAG_STEP_S * HZ)))
    best, best_lag = -1.0, 0
    for lag in [0] + [x for x in range(-L, L + 1, step) if x != 0]:
        a = v[:m - lag] if lag >= 0 else v[-lag:]
        b = s[lag:] if lag >= 0 else s[:m + lag]
        if len(a) >= 5:
            r = _pearson(a, b)
            if r > best + 1e-9:
                best, best_lag = r, lag
    return best, best_lag / HZ


def peak_share(v, s) -> float:
    """Share of the video's top-quartile time where the song is in its own upper half."""
    if len(v) < 5:
        return 0.0
    top = v >= np.percentile(v, 75)
    return float((s[top] >= np.percentile(s, 50)).mean()) if top.any() else 0.0


def profile_of(v, s) -> dict:
    dv, ds = dyn_range(v), dyn_range(s)
    fr, lag = follow_lag(v, s)
    return {"follow": round(fr, 3), "lag_s": round(lag, 2), "dyn_video": round(dv, 3),
            "dyn_song": round(ds, 3), "ratio": round(ds / dv if dv > 1e-9 else 1.0, 3),
            "peak": round(peak_share(v, s), 3)}


def closeness(p) -> dict:
    """Per-component closeness (0..1) of profile p to REFERENCE."""
    ref = REFERENCE
    c_f = float(np.clip(p["follow"] / ref["follow"], 0.0, 1.0))
    oct_ = abs(np.log2(max(p["ratio"], 1e-3) / ref["ratio"]))
    c_r = float(np.clip(1.0 - max(0.0, oct_ - RATIO_DEAD_OCT) / RATIO_SPAN_OCT, 0.0, 1.0))
    c_p = float(np.clip((p["peak"] - PEAK_CHANCE) / (ref["peak"] - PEAK_CHANCE), 0.0, 1.0))
    return {"follow": c_f, "ratio": c_r, "peak": c_p}


def sub_score(p) -> float:
    """0..100 closeness of a coupling profile to the reference (reference = 100)."""
    c = closeness(p)
    return round(100.0 * (W_FOLLOW * c["follow"] + W_RATIO * c["ratio"] + W_PEAK * c["peak"]), 1)


def _overlap(song, video, off):
    s, v = song_trajectory(song), video_trajectory(video)
    i0 = int(round(float(off) * HZ))
    j0 = max(0, -i0)
    seg = s[max(i0, 0):]
    m = min(len(seg), len(v) - j0)
    if m < MIN_OVERLAP_S * HZ:
        return None, None
    return np.asarray(v[j0:j0 + m]), np.asarray(seg[:m])


def traj_stats(song, video, off) -> dict | None:
    """Coupling profile + sub_score over the overlap at offset off (None if the overlap is too short)."""
    v, s = _overlap(song, video, off)
    if v is None:
        return None
    p = profile_of(v, s)
    p["sub_score"] = sub_score(p)
    return p


def weighted_total(total, tj):
    """Total with the sub-score as a W_TRAJ-weighted part (unchanged when tj is missing)."""
    if not tj or tj.get("sub_score") is None:
        return total
    return (1.0 - W_TRAJ) * float(total) + W_TRAJ * float(tj["sub_score"])


# ---------------------------------------------------------------- UI data (always on, data only)

def _warp_of(plan):
    rr = (plan or {}).get("retime_ramps") or {}
    w = rr.get("warp") if isinstance(rr, dict) else None
    if w is None:
        w = ((plan or {}).get("retime") or {}).get("warp")
    return w if w is not None and len(w) >= 2 else None


def _sample(curve, t, hz=HZ):
    curve = np.asarray(curve, dtype=float)
    if not len(curve):
        return np.zeros(len(t))
    return np.interp(t, np.arange(len(curve)) / hz, curve, left=np.nan, right=np.nan)


def result_block(song, video, plan) -> dict | None:
    """UI/trajectory data for an auto-sync result, in OUTPUT time (after any retime warp).

    song is the song actually used by the plan (song time = output time + plan offset); video is
    the original video (its curves are mapped through the warp). Never raises; None on missing data.
    """
    try:
        from .retime import _wmap, _winv
        plan = plan or {}
        off = float(plan.get("offset") or 0.0)
        warp = _warp_of(plan)
        vd = float(video.get("duration") or 0.0)
        od = float(np.asarray(warp, float)[-1, 1]) if warp is not None else vd
        if od <= 0:
            return None
        t = np.arange(int(od * HZ)) / HZ
        src = _winv(warp, t) if warp is not None else t
        st = t + off
        v4, s4 = _sample(video_trajectory(video), src), _sample(song_trajectory(song), st)
        ok = ~(np.isnan(v4) | np.isnan(s4))
        prof = None
        if ok.sum() >= MIN_OVERLAP_S * HZ:
            prof = profile_of(v4[ok], s4[ok])
            prof["sub_score"] = sub_score(prof)
        v1 = _sample(video_trajectory(video, 1.0), src)
        s1 = _sample(song_trajectory(song, 1.0), st)
        ramps = []
        booms = [b for b in (video.get("visual_booms") or []) if float(b.get("strength") or 0) >= RAMP_MIN_STRENGTH]
        for b in sorted(booms, key=lambda b: -float(b["strength"]))[:RAMP_MAX]:
            bt = float(_wmap(warp, b["t"])) if warp is not None else float(b["t"])
            i, w = int(round(bt * HZ)), int(round(RAMP_S * HZ))
            if i - w < 0 or i >= len(t):
                continue
            pre = slice(i - w, i - w + int(2 * HZ))
            vr, sr = v1[i] - np.nanmean(v1[pre]), s1[i] - np.nanmean(s1[pre])
            if np.isnan(vr) or np.isnan(sr):
                continue
            ramps.append({"boom_t": round(bt, 2), "strength": round(float(b["strength"]), 3),
                          "video_ramp": round(float(vr), 3), "song_ramp": round(float(sr), 3),
                          "match": bool(np.sign(vr) == np.sign(sr))})
        ramps.sort(key=lambda r: r["boom_t"])
        iu = np.minimum(np.round(np.arange(int(od * UI_HZ)) / UI_HZ * HZ).astype(int), len(t) - 1)

        def ui(x):
            return [None if np.isnan(y) else round(float(y), 3) for y in x[iu]]

        out = {"hz": UI_HZ, "video_curve": ui(v4), "song_curve": ui(s4), "ramps": ramps,
               "reference": dict(REFERENCE), "flag": F22_ENABLED}
        out.update(prof or {"follow": None, "lag_s": None, "dyn_video": None, "dyn_song": None,
                            "ratio": None, "peak": None, "sub_score": None})
        return out
    except Exception:
        return None
