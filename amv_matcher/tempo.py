"""Global song tempo stretch: one ratio so the song's BPM matches the video's motion tempo."""
from __future__ import annotations

import numpy as np

from . import score as S

RATIO_MIN, RATIO_MAX = 0.90, 1.10
SWEEP_DEV = 0.10             # ranking sweep: song speed 0.90x..1.10x
SWEEP_OFFS = (-0.1, -0.05, 0.0, 0.05, 0.1)   # offset nudges (s) tried at each ratio



def plan_tempo(song: dict, video: dict, max_dev: float = 0.10) -> dict:
    """ratio = playback speed factor for the song (beats arrive 1/ratio apart).

    Octave aware: the song BPM is compared at x0.5, x1 and x2 (score.RATES) and the closest
    octave is matched. The ratio is clamped to [1-max_dev, 1+max_dev] (and to 0.90..1.10).
    """
    bpm, vb = float(song.get("bpm", 0) or 0), float(video.get("motion_tempo_bpm", 0) or 0)
    if bpm <= 0 or vb <= 0:
        return {"ratio": 1.0, "bpm_from": round(bpm, 2), "bpm_to": round(bpm, 2), "octave": 1.0, "clamped": False}
    octave = min(S.RATES, key=lambda r: abs(bpm * r - vb))
    want = vb / (bpm * octave)
    lo, hi = max(RATIO_MIN, 1.0 - max_dev), min(RATIO_MAX, 1.0 + max_dev)
    ratio = float(np.clip(want, lo, hi))
    return {"ratio": round(ratio, 4), "bpm_from": round(bpm, 2), "bpm_to": round(bpm * ratio, 2),
            "octave": octave, "clamped": abs(ratio - want) > 1e-9}


def apply_tempo(song: dict, ratio: float) -> dict:
    """Song dict as heard when played `ratio` times faster (all times divided by ratio)."""
    if abs(ratio - 1.0) < 1e-9:
        return song
    out = dict(song)
    out["bpm"] = float(song["bpm"]) * ratio
    out["duration"] = float(song["duration"]) / ratio
    out["beat_times"] = (np.asarray(song["beat_times"], dtype=float) / ratio).tolist()
    if song.get("beat_grid") is not None:
        out["beat_grid"] = (np.asarray(song["beat_grid"], dtype=float) / ratio).tolist()
        out["grid_bpm"] = float(song.get("grid_bpm") or 0) * ratio
        out["grid_period"] = float(song.get("grid_period") or 0) / ratio
        if "grid_t0" in song:
            out["grid_t0"] = float(song["grid_t0"]) / ratio
    if song.get("sections"):
        out["sections"] = [dict(x, start=x["start"] / ratio, end=x["end"] / ratio) for x in song["sections"]]
    out["drops"] = [d / ratio for d in song.get("drops", [])]
    out["lulls"] = [[a / ratio, b / ratio] for a, b in song.get("lulls", [])]
    hz = S.ENERGY_HZ
    se = np.asarray(song["energy_curve"], dtype=float)
    if len(se):
        n = max(1, int(round(len(se) / ratio)))
        out["energy_curve"] = np.interp(np.arange(n) / hz * ratio, np.arange(len(se)) / hz, se).tolist()
    if "onset_env_fps" in song:
        out["onset_env_fps"] = float(song["onset_env_fps"]) * ratio
    from .boom import map_song_booms
    out.update(map_song_booms(song, lambda t: t / ratio))
    return out


def best_tempo(song: dict, video: dict, base: dict, lead_s: float = None, dev: float = SWEEP_DEV) -> dict:
    """Best song speed in 1-dev..1+dev for a song already scored at 1.00x (`base` = score() result).

    Coarse (step 0.02) then fine (step 0.01 around the best). Each ratio scales the cached analysis
    (apply_tempo), never re-analyses the audio; the offset is carried over (offset / ratio) and nudged a
    little. Returns {best_tempo_ratio, best_tempo_total_pct, best_tempo_label, tempo_gain_pct}.
    """
    t1 = int(base.get("total_pct", 0))
    off0 = float(base.get("best_offset_s", 0.0))
    vd = float(video["duration"])
    cache = {}

    def total(r):
        r = round(float(r), 2)
        if r not in cache:
            st = apply_tempo(song, r)
            top = float(st["duration"]) - vd
            best = 0
            for d in SWEEP_OFFS:
                o = off0 / r + d
                if o < 0 or o > max(top, 0.0) + 1e-9:
                    continue
                best = max(best, S.score_at(st, video, o, lead_s)["total_pct"])
            cache[r] = best
        return cache[r]

    n = int(round(dev / 0.02))
    coarse = [round(1.0 + 0.02 * k, 2) for k in range(-n, n + 1) if k]
    best_r, best_t = 1.0, t1
    for r in coarse:
        t = total(r)
        if t > best_t:
            best_r, best_t = r, t
    for r in (round(best_r - 0.01, 2), round(best_r + 0.01, 2)):
        if best_r != 1.0 and abs(r - 1.0) <= dev + 1e-9 and r != 1.0:
            t = total(r)
            if t > best_t:
                best_r, best_t = r, t
    pct = round((best_r - 1.0) * 100)
    label = "fits best at 1.00x" if best_r == 1.0 else f"fits best at {pct:+d}% speed"
    return {"best_tempo_ratio": best_r, "best_tempo_total_pct": int(best_t), "best_tempo_label": label,
            "tempo_gain_pct": int(best_t - t1)}
