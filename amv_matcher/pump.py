"""Screen-pump ("Boom") keyframes. Pure functions, no I/O.

FORMULA (the JS preview in static/app.js MUST match this; keep both in sync):
  For every beat i at time b_i with strength s_i in 0..1 (drops use strength 1):
    amp_i   = 0.03 * (boom / 100) * s_i            beat
    amp_i   = 4 * 0.03 * (boom / 100) * 1.0        drop (a beat within DROP_MATCH_S of a drop,
                                                   or the drop time itself)
    k_i(t)  = exp(-(t - b_i) / (decay_ms / 1000)) for t >= b_i, else 0
  scale(t)      = 1 + max_i amp_i * k_i(t)
  brightness(t) = 1 + 0.35 * max over drops of k_i(t)      (drops only; peak 1.35)
  shake (drops, shake=True): dx,dy = +-(2..6 px) * k_i(t), deterministic per (drop, frame)
  drops_only=True ignores ordinary beats. boom=0 => scale 1.0 everywhere.
"""
from __future__ import annotations

import math
import random

BASE_AMP = 0.03
DROP_MULT = 4.0
DROP_BRIGHT = 0.35
DROP_MATCH_S = 0.15
SHAKE_MIN_PX, SHAKE_MAX_PX = 2.0, 6.0


def pump_events(song: dict, drops_only: bool = False):
    """[(time, strength, is_drop)] from a song analysis dict."""
    drops = [float(d) for d in song.get("drops", [])]
    beats = [float(b) for b in song.get("beat_times", [])]
    strengths = song.get("beat_strengths") or [1.0] * len(beats)
    ev = []
    for b, s in zip(beats, strengths):
        is_drop = any(abs(b - d) <= DROP_MATCH_S for d in drops)
        if is_drop or not drops_only:
            ev.append((b, 1.0 if is_drop else float(s), is_drop))
    have = {round(e[0], 3) for e in ev}
    for d in drops:  # a drop with no tracked beat nearby still pulses
        if not any(abs(d - e[0]) <= DROP_MATCH_S for e in ev):
            ev.append((d, 1.0, True))
    return sorted(ev)


def pump_at(events, t, boom, decay_ms, shake=False):
    """Return (scale, brightness, dx, dy) at time t."""
    tau = max(1e-3, decay_ms / 1000.0)
    scale, bright, dx, dy = 1.0, 1.0, 0.0, 0.0
    if boom <= 0:
        return scale, bright, dx, dy
    for k, (b, s, is_drop) in enumerate(events):
        if t < b:
            break
        if t - b > 6 * tau:
            continue
        env = math.exp(-(t - b) / tau)
        amp = BASE_AMP * (boom / 100.0) * s * (DROP_MULT if is_drop else 1.0)
        scale = max(scale, 1.0 + amp * env)
        if is_drop:
            bright = max(bright, 1.0 + DROP_BRIGHT * (boom / 100.0) * env)
            if shake:
                rng = random.Random(k * 7919 + int(t * 1000) // 16)
                mag = (SHAKE_MIN_PX + (SHAKE_MAX_PX - SHAKE_MIN_PX) * rng.random()) * env
                ang = rng.random() * 2 * math.pi
                dx, dy = mag * math.cos(ang), mag * math.sin(ang)
    return scale, bright, dx, dy


def pump_keyframes(song_analysis: dict, boom: float = 40, decay_ms: float = 180,
                   drops_only: bool = False, fps: float = 30, duration: float = None,
                   shake: bool = False, start: float = 0.0):
    """Keyframes at video fps: [{t, scale, brightness, dx, dy}]. t is song time minus `start`."""
    dur = float(duration if duration is not None else song_analysis.get("duration", 0))
    ev = pump_events(song_analysis, drops_only)
    n = int(dur * fps) + 1
    out = []
    for i in range(n):
        t = i / fps
        sc, br, dx, dy = pump_at(ev, t + start, boom, decay_ms, shake)
        out.append({"t": round(t, 4), "scale": round(sc, 4), "brightness": round(br, 4),
                    "dx": round(dx, 2), "dy": round(dy, 2)})
    return out
