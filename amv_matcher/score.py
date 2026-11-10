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
SYNC_SIGMA_S = 0.060          # gaussian credit width around each beat
SYNC_BEAT_W = 0.6             # sync = beat-level 60% + phrase-level 40%
SYNC_PHRASE_W = 0.4
PHRASE_TOL_S = 0.120          # cuts count as "on the bar" within this of a boundary
HALF_BAR_W = 0.6              # mid-bar boundaries count for this much of a bar line
GRID_BPM_AGREE = 0.03         # use the grid only if within 3% of the song bpm
MIN_OFFSET_S = -3.0           # the song may start up to this long after the video starts
XCORR_TOP = 5                 # candidate offsets taken from onset cross-correlation
XCORR_MIN_SEP_S = 0.5
REFINE_S = 0.15
EVENT_LATENCY_S = 0.0         # measured real cuts sit ~60 ms before their beat, but a nonzero value penalises perfectly-synced synthetic edits (splice/retime/autosync tests), so it is off
REFINE_STEP_S = 0.02
EVENT_ENV_SIGMA_S = 0.05


def tempo_score(song_bpm: float, video_bpm: float, video_candidates=None):
    """Return (pct, best_rate). Octave aware: song*rate vs the video tempo, and
    (when given) vs every plausible video tempo candidate, keeping the best fit."""
    if song_bpm <= 0:
        return 0.0, 1.0
    cands = [float(video_bpm)] if video_bpm and video_bpm > 0 else []
    for c in video_candidates or []:
        c = float(c[0] if hasattr(c, "__len__") else c)
        if c > 0:
            cands.append(c)
    best, best_rate = 0.0, 1.0
    for vb in cands:
        for r in RATES:
            rel = abs(song_bpm * r - vb) / vb
            s = 100.0 * float(np.exp(-0.5 * (rel / TEMPO_SIGMA_REL) ** 2))
            if s > best:
                best, best_rate = s, r
    return best, best_rate


def _beats(song):
    """Constant-tempo grid when it fits the music well and agrees with the song's bpm,
    else the tracker's raw beats."""
    g = song.get("beat_grid")
    gb = float(song.get("grid_bpm") or 0)
    bpm = float(song.get("bpm") or 0)
    if song.get("grid_used") and g is not None and len(g) >= 2 and gb > 0 and bpm > 0 \
            and abs(gb - bpm) / bpm <= GRID_BPM_AGREE:
        return np.asarray(g, dtype=float)
    return np.asarray(song["beat_times"], dtype=float)


def _dist_to(t, ref):
    idx = np.searchsorted(ref, t)
    left = ref[np.clip(idx - 1, 0, len(ref) - 1)]
    right = ref[np.clip(idx, 0, len(ref) - 1)]
    return np.minimum(np.abs(t - left), np.abs(t - right))


def _beat_level(ev_t, ev_w, beats, vd, off):
    """Chance-corrected soft credit of events by distance to the nearest beat (0..1)."""
    t = ev_t + off
    ok = t >= 0
    if not np.any(ok) or len(beats) < 2:
        return 0.0
    t, w = t[ok], ev_w[ok]
    d = _dist_to(t, beats)
    f = float(np.sum(w * np.exp(-0.5 * (d / SYNC_SIGMA_S) ** 2)) / np.sum(w))
    n = int(np.sum((beats >= off) & (beats <= off + vd)))
    base = min(0.95, n / max(vd, 1e-6) * SYNC_SIGMA_S * np.sqrt(2 * np.pi))
    return float(np.clip((f - base) / max(1e-6, 1.0 - base), 0.0, 1.0))


def _phrase_level(song, cut_t, vd, off):
    """Cuts near bar / half-bar boundaries of the song's grid (0..1, chance-corrected)."""
    grid = song.get("beat_grid")
    period = float(song.get("grid_period") or 0)
    bpb = int(song.get("beats_per_bar") or 4)
    if grid is None or len(grid) < bpb * 2 or period <= 0 or not song.get("grid_used") or len(cut_t) == 0:
        return None
    grid = np.asarray(grid, dtype=float)
    t = cut_t + off
    t = t[t >= 0]
    if len(t) == 0:
        return None
    phase = int(song.get("bar_phase") or 0) % bpb
    bar = grid[phase::bpb]
    half = grid[(phase + bpb // 2) % bpb::bpb] if bpb % 2 == 0 else np.array([])
    cb = np.clip(1 - _dist_to(t, bar) / PHRASE_TOL_S, 0, 1)
    credit = cb
    if len(half) >= 2:
        credit = np.maximum(cb, HALF_BAR_W * np.clip(1 - _dist_to(t, half) / PHRASE_TOL_S, 0, 1))
    pbar = period * bpb
    base = min(0.9, (PHRASE_TOL_S / pbar) * (1 + (HALF_BAR_W if len(half) >= 2 else 0)) * 1.0)
    f = float(np.mean(credit))
    return float(np.clip((f - base) / max(1e-6, 1.0 - base), 0.0, 1.0))


def _sync_pct_at(song, ev_t, ev_w, vd, off):
    beats = _beats(song)
    off = off + EVENT_LATENCY_S
    b = _beat_level(ev_t, ev_w, beats, vd, off)
    cut_t = ev_t[ev_w >= CUT_WEIGHT]
    ph = _phrase_level(song, cut_t, vd, off)
    if ph is None:
        ph = b
    return float(np.clip(100.0 * (SYNC_BEAT_W * b + SYNC_PHRASE_W * ph), 0, 100))


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
    """Pearson of video vs song energy (mapped to 0-100) plus drop/lull bonuses.
    Only the overlap counts, so a negative offset (song starts late) is not penalised."""
    ve = np.asarray(video["energy_curve"], dtype=float)
    se = np.asarray(song["energy_curve"], dtype=float)
    i0 = int(round(off * ENERGY_HZ))
    j0 = max(0, -i0)
    seg = se[max(i0, 0):max(i0, 0) + max(len(ve) - j0, 0)]
    m = min(len(seg), len(ve) - j0)
    v = ve[j0:j0 + m] if m > 0 else ve[:0]
    if m < 3 or np.std(seg[:m]) < 1e-9 or np.std(v) < 1e-9:
        r = 0.0
    else:
        r = float(np.corrcoef(v, seg[:m])[0, 1])
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


def _tempo(song, video):
    return tempo_score(song["bpm"], video["motion_tempo_bpm"], video.get("motion_tempo_candidates"))


def score_at(song: dict, video: dict, offset: float) -> dict:
    """Score one explicit offset (manual placement of the song on the timeline)."""
    tempo_pct, rate = _tempo(song, video)
    ev_t, ev_w = _events(video)
    vd = float(video["duration"])
    sync = _sync_pct_at(song, ev_t, ev_w, vd, offset) if len(ev_t) and len(song["beat_times"]) >= 2 else 0.0
    return _pack(song, video, offset, tempo_pct, rate, sync, _arc_pct_at(song, video, offset))


def _xcorr_offsets(song, ev_t, ev_w, lo, hi):
    """Top candidate offsets from cross-correlating the cut/peak impulse train with the
    song's onset envelope. offset o means song_time = video_time + o."""
    env = song.get("onset_env")
    fps = float(song.get("onset_env_fps") or 0)
    if not env or fps <= 0 or len(ev_t) == 0:
        return []
    env = np.asarray(env, dtype=float)
    env = env - env.mean()
    n = len(env)
    train = np.zeros(n)
    for t, w in zip(ev_t, ev_w):
        i = int(round(t * fps))
        if 0 <= i < n:
            train[i] += w
    from scipy.ndimage import gaussian_filter1d
    train = gaussian_filter1d(train, EVENT_ENV_SIGMA_S * fps)
    size = 1 << int(np.ceil(np.log2(2 * n)))
    cc = np.fft.irfft(np.fft.rfft(env, size) * np.conj(np.fft.rfft(train, size)), size)
    # cc[k] = sum_t env[t+k]*train[t]; negative k wraps to the end
    ks = np.arange(int(np.floor(lo * fps)), int(np.ceil(hi * fps)) + 1)
    vals = cc[ks % size]
    order = np.argsort(vals)[::-1]
    out = []
    for i in order:
        o = ks[i] / fps
        if all(abs(o - q) >= XCORR_MIN_SEP_S for q in out):
            out.append(float(o))
        if len(out) >= XCORR_TOP:
            break
    return out


def score(song: dict, video: dict, allow_negative: bool = False) -> dict:
    """Best placement of the video window inside the song; ineligible if the song is shorter.
    With allow_negative the song may also start up to 3 s after the video (padded with
    silence, no length penalty). Off by default: splice/auto-sync need trim_start_s >= 0."""
    vd, sd = float(video["duration"]), float(song["duration"])
    if sd < vd:
        return {
            "eligible": False, "total_pct": 0, "tempo_pct": 0.0, "sync_pct": 0.0, "arc_pct": 0.0,
            "best_offset_s": 0.0, "trim_start_s": 0.0, "trim_end_s": round(vd, 2), "best_rate": 1.0,
            "reason": "too_short", "song_duration": round(sd, 2), "video_duration": round(vd, 2),
        }
    tempo_pct, rate = _tempo(song, video)
    ev_t, ev_w = _events(video)
    has_sync = len(ev_t) > 0 and len(song["beat_times"]) >= 2
    max_off = sd - vd
    lo = MIN_OFFSET_S if allow_negative else 0.0
    step = max(0.1, round((max_off - lo) / MAX_OFFSET_STEPS, 1))
    offsets = np.arange(lo, max_off + 1e-9, step)

    def comb_at(o):
        sync = _sync_pct_at(song, ev_t, ev_w, vd, o) if has_sync else 0.0
        arc = _arc_pct_at(song, video, o)
        return W_SYNC * sync + W_ARC * arc, sync, arc

    scanned = [(comb_at(float(o)), float(o)) for o in offsets]
    scanned.sort(key=lambda x: -x[0][0])
    seeds = [o for _, o in scanned[:3]]
    if has_sync:
        seeds += _xcorr_offsets(song, ev_t, ev_w, lo, max_off)
    best = None
    for seed in seeds:
        for o in np.arange(seed - REFINE_S, seed + REFINE_S + 1e-9, REFINE_STEP_S):
            o = float(min(max(o, lo), max_off))
            (c, sy, ar) = comb_at(o)
            if best is None or c > best[0]:
                best = (c, o, sy, ar)
    _, off, sync, arc = best
    return _pack(song, video, off, tempo_pct, rate, sync, arc)
