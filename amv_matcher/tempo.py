"""Global song tempo stretch: one ratio so the song's BPM matches the video's motion tempo."""
from __future__ import annotations

import numpy as np

from . import score as S

RATIO_MIN, RATIO_MAX = 0.92, 1.08


def plan_tempo(song: dict, video: dict, max_dev: float = 0.08) -> dict:
    """ratio = playback speed factor for the song (beats arrive 1/ratio apart).

    Octave aware: the song BPM is compared at x0.5, x1 and x2 (score.RATES) and the closest
    octave is matched. The ratio is clamped to [1-max_dev, 1+max_dev] (and to 0.92..1.08).
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
    out["drops"] = [d / ratio for d in song.get("drops", [])]
    out["lulls"] = [[a / ratio, b / ratio] for a, b in song.get("lulls", [])]
    hz = S.ENERGY_HZ
    se = np.asarray(song["energy_curve"], dtype=float)
    if len(se):
        n = max(1, int(round(len(se) / ratio)))
        out["energy_curve"] = np.interp(np.arange(n) / hz * ratio, np.arange(len(se)) / hz, se).tolist()
    if "onset_env_fps" in song:
        out["onset_env_fps"] = float(song["onset_env_fps"]) * ratio
    return out
