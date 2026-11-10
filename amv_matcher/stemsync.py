"""Use the stems (stems.py) to score and sync calm video motion against the soft details of the quiet parts.

stem_targets(song, stems_feats) -> soft sync targets inside the song's low-energy sections (quiet
hi-hats, vocal entries, bass pulses). quiet_stats(...) measures how well the video's CALM-block events
(cuts + motion peaks) land on the nearest of {beat grid, stem targets}; score.py blends it into sync.
"""
from __future__ import annotations

import numpy as np

QUIET_STEM_WEIGHT = 0.5      # share of the calm-event sync taken from the stem-aware measure
QUIET_TOL_S = 0.060          # an event is "in sync" within +-60 ms of a beat or stem target
SOFT_W_MIN = 0.3
SOFT_W_MAX = 0.7
VOCAL_START_W = 0.7
LOW_LABELS = ("verse", "breakdown", "intro")
DEDUPE_S = 0.05
BASS_PULSE_MIN_STRENGTH = 0.15      # bass onsets weaker than this are noise


def quiet_windows(song: dict) -> list:
    """[(a, b)] low-energy spans of the song: its lulls plus verse/breakdown/intro sections whose
    energy is below the median section energy."""
    wins = [(float(a), float(b)) for a, b in (song.get("lulls") or [])]
    secs = song.get("sections") or []
    if secs:
        med = float(np.median([float(s.get("energy", 0.0)) for s in secs]))
        wins += [(float(s["start"]), float(s["end"])) for s in secs
                 if s.get("label") in LOW_LABELS and float(s.get("energy", 0.0)) < med]
    wins.sort()
    merged = []
    for a, b in wins:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def _in(t, wins):
    return any(a <= t <= b for a, b in wins)


def _w(strength: float) -> float:
    return float(SOFT_W_MIN + (SOFT_W_MAX - SOFT_W_MIN) * min(1.0, max(0.0, strength)))


def stem_targets(song: dict, stems_feats: dict) -> list:
    """Soft targets [{t, weight, stem, kind}] inside the song's low-energy sections only; outside them
    nothing (the beat grid already covers the loud parts). kind: detail | vocal | bass."""
    if not stems_feats:
        return []
    wins = quiet_windows(song)
    if not wins:
        return []
    out = []
    for e in stems_feats.get("quiet_detail") or []:
        t = float(e["t"])
        if _in(t, wins):
            out.append({"t": t, "weight": _w(float(e.get("strength", 0.5))), "stem": e.get("stem", ""), "kind": "detail"})
    for t in stems_feats.get("vocal_phrase_starts") or []:
        if _in(float(t), wins):
            out.append({"t": float(t), "weight": VOCAL_START_W, "stem": "vocals", "kind": "vocal"})
    bass = (stems_feats.get("stems") or {}).get("bass")
    if bass:
        for t, s in zip(bass.get("onsets") or [], bass.get("onset_strength") or []):
            if float(s) >= BASS_PULSE_MIN_STRENGTH and _in(float(t), wins):
                out.append({"t": float(t), "weight": _w(float(s)), "stem": "bass", "kind": "bass"})
    out.sort(key=lambda d: d["t"])
    ded = []
    for d in out:                                  # same instant from several sources -> keep the strongest
        if ded and d["t"] - ded[-1]["t"] < DEDUPE_S:
            if d["weight"] > ded[-1]["weight"]:
                ded[-1] = d
        else:
            ded.append(d)
    return ded


def target_times(stem_targets) -> np.ndarray:
    return np.array(sorted(float(d["t"]) for d in (stem_targets or [])), dtype=float)


def calm_mask(video: dict, ev_t) -> np.ndarray:
    """Which video events fall in the video's calm segments_struct blocks."""
    m = np.zeros(len(ev_t), dtype=bool)
    for s in video.get("segments_struct") or []:
        if s.get("kind") == "calm":
            m |= (ev_t >= float(s["start"])) & (ev_t < float(s["end"]))
    return m


def _chance(window_hits_per_s: float) -> float:
    return float(min(0.95, window_hits_per_s * 2 * QUIET_TOL_S))


def quiet_stats(song_beats, stem_t, ev_t, ev_w, calm, vd, off, lead) -> dict:
    """Calm-event sync at offset `off`. quiet_sync_pct = weighted % of calm events within +-QUIET_TOL_S
    of a beat or stem target (event vs target - lead, the same perceptual lead as the main score).
    gain is the chance-corrected improvement over beats alone (0..1), what score.py blends in."""
    from .score import _dist_to
    res = {"quiet_sync_pct": 0.0, "quiet_sync_beats_pct": 0.0, "calm_frac": 0.0, "n_calm": int(calm.sum()), "gain": 0.0}
    if not np.any(calm) or len(song_beats) < 2:
        return res
    t = ev_t[calm] + off + lead
    w = ev_w[calm]
    ok = t >= 0
    if not np.any(ok):
        return res
    t, w = t[ok], w[ok]
    sw = w.sum()
    db = _dist_to(t, song_beats)
    union = np.sort(np.concatenate([song_beats, stem_t])) if len(stem_t) else song_beats
    du = _dist_to(t, union)
    hit_b = float(np.sum(w[db <= QUIET_TOL_S]) / sw)
    hit_u = float(np.sum(w[du <= QUIET_TOL_S]) / sw)
    lo, hi = off, off + vd

    def cc(hit, tgt):
        n = int(np.sum((tgt >= lo) & (tgt <= hi)))
        base = _chance(n / max(vd, 1e-6))
        return float(np.clip((hit - base) / max(1e-6, 1.0 - base), 0.0, 1.0))

    gain = max(0.0, cc(hit_u, union) - cc(hit_b, song_beats))
    res.update(quiet_sync_pct=round(100.0 * hit_u, 1), quiet_sync_beats_pct=round(100.0 * hit_b, 1),
               calm_frac=float(w.sum() / max(1e-9, ev_w.sum())), gain=gain)
    return res


def blend_sync(sync_pct: float, stats: dict) -> float:
    """sync + QUIET_STEM_WEIGHT * calm share of events * 100 * gain. Only calm events contribute, loud
    sections are untouched, and it never lowers the beat-only sync."""
    wgt = QUIET_STEM_WEIGHT * stats["calm_frac"]
    return float(min(100.0, sync_pct + wgt * 100.0 * stats["gain"]))
