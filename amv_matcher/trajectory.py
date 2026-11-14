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
  loc_r / agree / rise   local coupling (local_features): window r, slope agreement, build-up rise

sub_score() is 100 * mean closeness of the LOCAL features to REFERENCE: the common region of the
user's gold pairs (edits with their own audio, measured on real media; lower bound = the weakest
gold value, so every gold pair scores ~100). Generic constants only: nothing here knows a timestamp
or a file. follow, ratio and peak are reported, not scored (the gold pairs spread too widely on
them: GOLD_SPREAD).

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

# Reference = the common region of the user's gold pairs (four edits, each with its own audio),
# measured on LOCAL coupling features (local_features). Each bound is the lowest gold value (a
# one-sided band: more coupling than the weakest gold pair is never punished); closeness falls
# linearly from the bound to 0 at FLOOR. GOLD_SPREAD is the measured min / median / max per feature
# over the four gold pairs (info for reports and the UI; ratio and peak are info only).
REFERENCE = {"loc_r": 0.475, "agree": 0.535, "rise": 0.515}
FLOOR = {"loc_r": 0.0, "agree": 0.30, "rise": 0.25}
GOLD_SPREAD = {"loc_r": (0.475, 0.792, 0.883), "agree": (0.535, 0.619, 0.692), "rise": (0.515, 0.545, 0.631),
               "follow": (0.055, 0.653, 0.766), "ratio": (0.54, 1.044, 1.556), "peak": (0.541, 0.823, 1.0)}
LOC_WIN_S, LOC_HOP_S = 30.0, 10.0   # local Pearson windows (one window when the overlap is shorter)
LOC_Q = 75                          # loc_r = this percentile of the window r's
SLOPE_S = 4.0                       # slope span for agree / rise
RISE_TOP = 80                       # rise looks at the video's strongest build-ups (top 20 % slope)
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


def local_features(v, s) -> dict:
    """Local coupling: how the song moves WITH the video stretch by stretch (not only globally).

    loc_r  LOC_Q-th percentile of Pearson r over LOC_WIN_S windows (hop LOC_HOP_S)
    agree  over SLOPE_S slopes, the |video slope|-weighted share of the bigger half of the video's
           moves where the song moves the same way
    rise   share of the video's strongest build-ups (top 20 % slope) where the song rises too
    """
    v, s = np.asarray(v, float), np.asarray(s, float)
    m = min(len(v), len(s))
    v, s = v[:m], s[:m]
    W, H, L = int(LOC_WIN_S * HZ), int(LOC_HOP_S * HZ), int(SLOPE_S * HZ)
    if m <= L + 4:
        return {"loc_r": 0.0, "agree": 0.0, "rise": 0.0}
    lr = [_pearson(v[i:i + W], s[i:i + W]) for i in range(0, m - W + 1, H)] if m >= W else [_pearson(v, s)]
    dv, ds = v[L:] - v[:-L], s[L:] - s[:-L]
    w = np.abs(dv)
    big = w > np.percentile(w, 50)
    agree = float(np.sum(w[big] * (np.sign(dv[big]) == np.sign(ds[big]))) / max(float(w[big].sum()), 1e-9))
    up = dv > np.percentile(dv, RISE_TOP)
    rise = float(np.mean(ds[up] > 0)) if up.any() else 0.0
    return {"loc_r": round(float(np.percentile(lr, LOC_Q)), 3), "agree": round(agree, 3), "rise": round(rise, 3)}


def profile_of(v, s) -> dict:
    dv, ds = dyn_range(v), dyn_range(s)
    fr, lag = follow_lag(v, s)
    p = {"follow": round(fr, 3), "lag_s": round(lag, 2), "dyn_video": round(dv, 3),
         "dyn_song": round(ds, 3), "ratio": round(ds / dv if dv > 1e-9 else 1.0, 3),
         "peak": round(peak_share(v, s), 3)}
    p.update(local_features(v, s))
    return p


def closeness(p) -> dict:
    """Per-feature closeness (0..1) of profile p to the gold common region (REFERENCE lower bounds)."""
    out = {}
    for k, ref in REFERENCE.items():
        lo = FLOOR[k]
        out[k] = float(np.clip((float(p[k]) - lo) / (ref - lo), 0.0, 1.0))
    return out


def sub_score(p) -> float:
    """0..100: mean closeness of the local features to the gold region (inside the region = 100)."""
    c = closeness(p)
    return round(100.0 * sum(c.values()) / len(c), 1)


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
               "reference": {"bounds": dict(REFERENCE), "floor": dict(FLOOR),
                             "gold_spread": {k: list(v) for k, v in GOLD_SPREAD.items()}},
               "flag": F22_ENABLED}
        out.update(prof or {"follow": None, "lag_s": None, "dyn_video": None, "dyn_song": None,
                            "ratio": None, "peak": None, "loc_r": None, "agree": None,
                            "rise": None, "sub_score": None})
        return out
    except Exception:
        return None
