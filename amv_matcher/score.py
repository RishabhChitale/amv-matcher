"""Score how well a song's beat fits a video's motion rhythm."""
from __future__ import annotations

import numpy as np

W_TEMPO = 0.20
W_SYNC = 0.40
W_ARC = 0.40
DROP_MATCH_S = 0.5        # video peak within this of a song drop
ARC_BONUS_PER_MATCH = 10.0
ARC_BONUS_MAX = 20.0
ENERGY_HZ = 10.0
TEMPO_SIGMA_REL = 0.0679   # gaussian: 50% at 8% relative error (sqrt(2 ln2)=1.1774)
TOLERANCE_S = 0.070
CUT_WEIGHT = 2.0
PEAK_WEIGHT = 1.0
OFFSET_STEP_S = 0.05
MAX_OFFSET_STEPS = 2000
RATES = (0.5, 1.0, 2.0)


def tempo_score(song_bpm: float, video_bpm: float):
    """Return (pct, best_rate). Octave tolerant: song*rate vs video."""
    if song_bpm <= 0 or video_bpm <= 0:
        return 0.0, 1.0
    best, best_rate = 0.0, 1.0
    for r in RATES:
        rel = abs(song_bpm * r - video_bpm) / video_bpm
        s = 100.0 * float(np.exp(-0.5 * (rel / TEMPO_SIGMA_REL) ** 2))
        if s > best:
            best, best_rate = s, r
    return best, best_rate


def _hit_fraction(ev_t, ev_w, beats, offset):
    """Weighted fraction of events within tolerance of a beat shifted by offset."""
    t = ev_t + offset
    idx = np.searchsorted(beats, t)
    left = beats[np.clip(idx - 1, 0, len(beats) - 1)]
    right = beats[np.clip(idx, 0, len(beats) - 1)]
    d = np.minimum(np.abs(t - left), np.abs(t - right))
    return float(np.sum(ev_w * (d <= TOLERANCE_S)) / np.sum(ev_w))


def _sync_pct_at(song, ev_t, ev_w, vd, off):
    beats = np.asarray(song["beat_times"], dtype=float)
    f = _hit_fraction(ev_t, ev_w, beats, off)
    n = int(np.sum((beats >= off) & (beats <= off + vd)))
    baseline = min(0.95, n / max(vd, 1e-6) * 2 * TOLERANCE_S)   # density * 140 ms
    return float(np.clip(100.0 * max(0.0, f - baseline) / max(1e-6, 1.0 - baseline), 0, 100))


def _events(video):
    cuts = np.asarray(video["cut_times"], dtype=float)
    peaks = np.asarray(video["motion_peaks"], dtype=float)
    # A peak coinciding with a cut is the same event; keep it once (as a cut).
    if len(cuts) and len(peaks):
        peaks = peaks[np.min(np.abs(peaks[:, None] - cuts[None, :]), axis=1) > 0.1]
    t = np.concatenate([cuts, peaks])
    w = np.concatenate([np.full(len(cuts), CUT_WEIGHT), np.full(len(peaks), PEAK_WEIGHT)])
    return t, w


def _arc_pct_at(song, video, off):
    """Pearson of video vs song energy (mapped to 0-100) plus drop/lull bonuses."""
    ve = np.asarray(video["energy_curve"], dtype=float)
    se = np.asarray(song["energy_curve"], dtype=float)
    i0 = int(round(off * ENERGY_HZ))
    seg = se[i0:i0 + len(ve)]
    m = min(len(seg), len(ve))
    if m < 3 or np.std(seg[:m]) < 1e-9 or np.std(ve[:m]) < 1e-9:
        r = 0.0
    else:
        r = float(np.corrcoef(ve[:m], seg[:m])[0, 1])
    pct = 50.0 * (r + 1.0)
    matches = 0
    drops = np.asarray(song.get("drops", []), dtype=float) - off
    for a, _b in video.get("peak_segments", []):
        if len(drops) and np.min(np.abs(drops - a)) <= DROP_MATCH_S:
            matches += 1
    for va, vb in video.get("lulls", []):
        if any(sa - off < vb and sb - off > va for sa, sb in song.get("lulls", [])):
            matches += 1
    pct += min(ARC_BONUS_MAX, ARC_BONUS_PER_MATCH * matches)
    return float(np.clip(pct, 0, 100))


def _combine(tempo_pct, sync_pct, arc_pct):
    return W_TEMPO * tempo_pct + W_SYNC * sync_pct + W_ARC * arc_pct


def _pack(song, video, off, tempo_pct, rate, sync_pct, arc_pct):
    return {
        "eligible": True,
        "total_pct": int(round(_combine(tempo_pct, sync_pct, arc_pct))),
        "tempo_pct": round(tempo_pct, 1),
        "sync_pct": round(sync_pct, 1),
        "arc_pct": round(arc_pct, 1),
        "best_offset_s": round(off, 2),
        "trim_start_s": round(off, 2),
        "trim_end_s": round(off + float(video["duration"]), 2),
        "best_rate": rate,
    }


def score_at(song: dict, video: dict, offset: float) -> dict:
    """Score one explicit offset (manual placement of the song on the timeline)."""
    tempo_pct, rate = tempo_score(song["bpm"], video["motion_tempo_bpm"])
    ev_t, ev_w = _events(video)
    vd = float(video["duration"])
    sync = _sync_pct_at(song, ev_t, ev_w, vd, offset) if len(ev_t) and len(song["beat_times"]) >= 2 else 0.0
    return _pack(song, video, offset, tempo_pct, rate, sync, _arc_pct_at(song, video, offset))


def score(song: dict, video: dict) -> dict:
    """Best placement of the video window inside the song; ineligible if song is shorter."""
    vd, sd = float(video["duration"]), float(song["duration"])
    if sd < vd:
        return {
            "eligible": False, "total_pct": 0, "tempo_pct": 0.0, "sync_pct": 0.0, "arc_pct": 0.0,
            "best_offset_s": 0.0, "trim_start_s": 0.0, "trim_end_s": round(vd, 2), "best_rate": 1.0,
            "reason": "too_short", "song_duration": round(sd, 2), "video_duration": round(vd, 2),
        }
    tempo_pct, rate = tempo_score(song["bpm"], video["motion_tempo_bpm"])
    ev_t, ev_w = _events(video)
    has_sync = len(ev_t) > 0 and len(song["beat_times"]) >= 2
    max_off = sd - vd
    step = max(0.1, round(max_off / MAX_OFFSET_STEPS, 1))
    offsets = np.arange(0.0, max_off + 1e-9, step) if max_off > 0 else np.array([0.0])
    best = None
    for off in offsets:
        sync = _sync_pct_at(song, ev_t, ev_w, vd, float(off)) if has_sync else 0.0
        arc = _arc_pct_at(song, video, float(off))
        comb = W_SYNC * sync + W_ARC * arc      # offset chosen by the combined sync+arc score
        if best is None or comb > best[0]:
            best = (comb, float(off), sync, arc)
    _, off, sync, arc = best
    return _pack(song, video, off, tempo_pct, rate, sync, arc)
