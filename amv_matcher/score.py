"""Score how well a song's beat fits a video's motion rhythm."""
from __future__ import annotations

import numpy as np

W_TEMPO = 0.20
W_SYNC = 0.60
W_ARC = 0.20
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


# Human editors cut slightly BEFORE the beat (perceptual sync). Median signed event-to-nearest-beat
# error on the two ground-truth AMVs: A -65 ms, B -53 ms -> 0.06 s. Every event-vs-beat comparison
# (sync phase/coherence, fine term, phrase term, tightness) uses the SAME lead: an event is "on the
# beat" when it lands lead_s before it.
PERCEPTUAL_LEAD_S = 0.06
MAX_LEAD_S = 0.12


def _lead(lead_s=None):
    """None -> PERCEPTUAL_LEAD_S; otherwise clamped to 0..MAX_LEAD_S."""
    if lead_s is None:
        return PERCEPTUAL_LEAD_S
    return float(min(max(float(lead_s), 0.0), MAX_LEAD_S))


def _beat_level(ev_t, ev_w, beats, vd, off, lead_s=None):
    """Chance-corrected soft credit of events by distance to the nearest beat (0..1)."""
    t = ev_t + off + _lead(lead_s)          # event vs (beat - lead)
    ok = t >= 0
    if not np.any(ok) or len(beats) < 2:
        return 0.0
    t, w = t[ok], ev_w[ok]
    d = _dist_to(t, beats)
    f = float(np.sum(w * np.exp(-0.5 * (d / SYNC_SIGMA_S) ** 2)) / np.sum(w))
    n = int(np.sum((beats >= off) & (beats <= off + vd)))
    base = min(0.95, n / max(vd, 1e-6) * SYNC_SIGMA_S * np.sqrt(2 * np.pi))
    return float(np.clip((f - base) / max(1e-6, 1.0 - base), 0.0, 1.0))


def _phrase_level(song, cut_t, vd, off, lead_s=None):
    """Cuts near bar / half-bar boundaries of the song's grid (0..1, chance-corrected)."""
    grid = song.get("beat_grid")
    period = float(song.get("grid_period") or 0)
    bpb = int(song.get("beats_per_bar") or 4)
    if grid is None or len(grid) < bpb * 2 or period <= 0 or not song.get("grid_used") or len(cut_t) == 0:
        return None
    grid = np.asarray(grid, dtype=float)
    t = cut_t + off + _lead(lead_s)
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


LOCK_A = 1.5                  # logistic slope on the lock prominence z
LOCK_B = 1.5                  # z at which sync is 50%
LOCK_TOL_S = 0.06             # no penalty within this of the expected lag
LOCK_FINE_W = 0.15
LOCK_RAW_W = 0.05             # un-saturated beat credit: keeps small local gains visible to planners
LOCK_SPAN = 0.12              # tempo scan +-12% for the null
LOCK_EXCL = 0.02              # exclude +-2% around the song period from the null
_LOCK_CACHE = {}


def _grid_ref(song):
    g = _beats(song)
    if len(g) < 2:
        return None
    return float(np.median(np.diff(g))), float(g[0])


def _lock(song, ev_t, ev_w):
    """Tempo-lock of the video events to the song grid: prominence z of the events' beat
    coherence R at the song period against R at nearby (wrong) periods, plus the complex
    coherence and the grid origin. Independent of the song offset."""
    ref = _grid_ref(song)
    if ref is None or len(ev_t) < 3:
        return None
    key = (id(song), len(ev_t), float(ev_t.sum()), float(ev_w.sum()), ref)
    hit = _LOCK_CACHE.get(key)
    if hit is not None:
        return hit
    T, g0 = ref
    w = ev_w / ev_w.sum()
    ratios = np.linspace(1 - LOCK_SPAN, 1 + LOCK_SPAN, 121)
    Rs = np.abs(np.exp(2j * np.pi * ev_t[None, :] / (T * ratios[:, None])) @ w)
    mask = np.abs(ratios - 1) > LOCK_EXCL
    base = Rs[mask]
    med = float(np.median(base))
    mad = max(1.4826 * float(np.median(np.abs(base - med))), 0.5 * float(np.std(base)), 1e-3)
    z0c = complex(np.sum(w * np.exp(2j * np.pi * ev_t / T)))
    z = (abs(z0c) - med) / mad
    z *= min(1.0, len(ev_t) / 12.0)          # few events -> little evidence
    out = (z, z0c, T, g0)
    if len(_LOCK_CACHE) > 256:
        _LOCK_CACHE.clear()
    _LOCK_CACHE[key] = out
    return out


def _sync_pct_at(song, ev_t, ev_w, vd, off, lead_s=None):
    """Discriminative sync 0..100: logistic of the tempo-lock prominence z, times how well
    the offset puts the events at the expected lag from the beat (cosine of phase error)."""
    ev_t = np.asarray(ev_t, dtype=float)
    ev_w = np.asarray(ev_w, dtype=float)
    k = _lock(song, ev_t, ev_w)
    if k is None:
        return 0.0
    z, z0c, T, g0 = k
    # coherence of events shifted by off, relative to the grid origin and expected lag
    ph = np.exp(2j * np.pi * (off - g0 + _lead(lead_s)) / T) * z0c
    r = abs(z0c)
    err = abs(float(np.angle(ph))) / (2 * np.pi) * T          # seconds from the expected lag
    align = 1.0 if err <= LOCK_TOL_S else float(np.exp(-0.5 * ((err - LOCK_TOL_S) / LOCK_TOL_S) ** 2))
    lock = 100.0 / (1.0 + np.exp(-LOCK_A * (z - LOCK_B))) * align
    # small fine-alignment term: keeps retime/splice/nudge improvements visible once the
    # lock itself has saturated (real edits are only loosely sample-accurate, so weight is low)
    b = _beat_level(ev_t, ev_w, _beats(song), vd, off, lead_s)
    fine = 100.0 * (1.0 - np.exp(-b / 0.05))
    return float(np.clip((1 - LOCK_FINE_W - LOCK_RAW_W) * lock + LOCK_FINE_W * fine + LOCK_RAW_W * 100.0 * b, 0, 100))


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


TIGHT_TOL_S = 0.040           # an event is "tight" when within +-40 ms of the grid


def _tight_stats(t, w, ref, T):
    r1 = abs(np.sum(w * np.exp(2j * np.pi * t / T)))
    r2 = abs(np.sum(w * np.exp(2j * np.pi * t / (T / 2))))
    grid, per, g = "beat", T, ref
    if r2 > r1:
        g = np.sort(np.concatenate([ref, (ref[:-1] + ref[1:]) / 2]))
        grid, per = "half_beat", T / 2
    err = _dist_to(t, g)
    return {
        "tight_pct": round(float(100.0 * np.sum(w[err <= TIGHT_TOL_S])), 1),
        "median_err_ms": round(float(np.median(err)) * 1000.0, 1),
        "p90_err_ms": round(float(np.percentile(err, 90)) * 1000.0, 1),
        "grid": grid,
        "chance_pct": round(100.0 * min(1.0, 2 * TIGHT_TOL_S / per), 1),
    }


def tightness(song: dict, video: dict, offset: float, lead_s: float = None) -> dict:
    """Non-saturating alignment of the video's locked events (cuts + peaks, same weights as
    the sync score) to the song's beat grid at `offset`. The main numbers are measured against
    the lead-compensated grid (beat_time - PERCEPTUAL_LEAD_S, since editors cut before the beat);
    `raw` is the same measure against the bare beat grid. The half-beat grid is used when the
    edit locks at 8ths (2nd-harmonic coherence beats the beat's). tight_pct = weighted % of
    events within +-40 ms of the grid; median/p90 are absolute errors in ms."""
    lead = _lead(lead_s)
    ev_t, ev_w = _events(video)
    beats = _beats(song)
    blank = {"tight_pct": 0.0, "median_err_ms": None, "p90_err_ms": None, "grid": "beat", "chance_pct": 0.0}
    if len(ev_t) == 0 or len(beats) < 2:
        return {**blank, "n_events": 0, "lead_ms": round(lead * 1000), "raw": dict(blank)}
    ref = np.asarray(beats, dtype=float)
    T = float(np.median(np.diff(ref)))
    w = ev_w / ev_w.sum()
    t = ev_t + float(offset)
    out = _tight_stats(t + lead, w, ref, T)   # event vs (beat - lead)
    out["n_events"] = int(len(ev_t))
    out["lead_ms"] = round(lead * 1000)
    out["raw"] = _tight_stats(t, w, ref, T)
    return out


def _pack(song, video, off, tempo_pct, rate, sync_pct, arc_pct, lead_s=None, quiet=None):
    from . import buildup
    bs = buildup.build_stats(song, video, off) if buildup.W_BUILD > 0 else None
    out = {
        "eligible": True,
        "total_pct": int(round(buildup.blend_total(_combine(tempo_pct, sync_pct, arc_pct), bs))),
        "tempo_pct": round(tempo_pct, 1),
        "sync_pct": round(sync_pct, 1),
        "arc_pct": round(arc_pct, 1),
        "best_offset_s": round(off, 2),
        "trim_start_s": round(off, 2),
        "trim_end_s": round(off + float(video["duration"]), 2),
        "best_rate": rate,
        "tightness": tightness(song, video, off, lead_s),
    }
    if bs is not None:
        out.update(build_pct=bs["build_pct"], launch_pct=bs["launch_pct"], n_launches=bs["n_launches"], n_builds=bs["n_builds"])
    if quiet is not None:
        out["quiet_sync"] = quiet["quiet_sync_pct"]
        out["quiet_sync_beats"] = quiet["quiet_sync_beats_pct"]
        out["quiet_events"] = quiet["n_calm"]
    return out


def _quiet_at(song, video, ev_t, ev_w, stem_targets, vd, off, lead_s):
    """Stem-aware calm-block stats at `off` (see stemsync.quiet_stats)."""
    from . import stemsync
    return stemsync.quiet_stats(_beats(song), stemsync.target_times(stem_targets), ev_t, ev_w,
                                stemsync.calm_mask(video, ev_t), vd, off, _lead(lead_s))


def _tempo(song, video):
    pct, rate = tempo_score(song["bpm"], video["motion_tempo_bpm"], video.get("motion_tempo_candidates"))
    # events that cohere at the song's own period are tempo evidence too (the events-tempo
    # estimator can lock onto a wrong ratio, e.g. 175 vs 126 bpm)
    ev_t, ev_w = _events(video)
    k = _lock(song, ev_t, ev_w)
    if k is not None:
        pct = max(pct, float(100.0 / (1.0 + np.exp(-LOCK_A * (k[0] - LOCK_B)))))
    return pct, rate


def score_at(song: dict, video: dict, offset: float, lead_s: float = None, stem_targets=None) -> dict:
    """Score one explicit offset (manual placement of the song on the timeline). lead_s: perceptual
    lead in seconds (None = PERCEPTUAL_LEAD_S, clamped 0..0.12), applied to sync and tightness alike.
    stem_targets (stemsync.stem_targets(...), optional): soft targets inside quiet song sections; calm
    video events may then also lock to them (adds a 'quiet_sync' key). None = unchanged results."""
    tempo_pct, rate = _tempo(song, video)
    ev_t, ev_w = _events(video)
    vd = float(video["duration"])
    has = len(ev_t) and len(song["beat_times"]) >= 2
    sync = _sync_pct_at(song, ev_t, ev_w, vd, offset, lead_s) if has else 0.0
    quiet = None
    if stem_targets is not None:
        quiet = _quiet_at(song, video, ev_t, ev_w, stem_targets, vd, offset, lead_s) if has else \
            {"quiet_sync_pct": 0.0, "quiet_sync_beats_pct": 0.0, "n_calm": 0, "calm_frac": 0.0, "gain": 0.0}
        from . import stemsync
        sync = stemsync.blend_sync(sync, quiet)
    return _pack(song, video, offset, tempo_pct, rate, sync, _arc_pct_at(song, video, offset), lead_s, quiet)


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


def score(song: dict, video: dict, allow_negative: bool = False, lead_s: float = None, stem_targets=None) -> dict:
    """Best placement of the video window inside the song; ineligible if the song is shorter.
    With allow_negative the song may also start up to 3 s after the video (padded with
    silence, no length penalty). Off by default: splice/auto-sync need trim_start_s >= 0.
    lead_s: perceptual lead in seconds (None = PERCEPTUAL_LEAD_S, clamped 0..0.12).
    stem_targets: optional soft targets (see score_at); None = unchanged results."""
    vd, sd = float(video["duration"]), float(song["duration"])
    if sd < vd:
        return {
            "eligible": False, "total_pct": 0, "tempo_pct": 0.0, "sync_pct": 0.0, "arc_pct": 0.0,
            "best_offset_s": 0.0, "trim_start_s": 0.0, "trim_end_s": round(vd, 2), "best_rate": 1.0,
            "reason": "too_short",
            "tightness": tightness({"beat_times": []}, {"cut_times": [], "motion_peaks": []}, 0.0), "song_duration": round(sd, 2), "video_duration": round(vd, 2),
        }
    tempo_pct, rate = _tempo(song, video)
    ev_t, ev_w = _events(video)
    has_sync = len(ev_t) > 0 and len(song["beat_times"]) >= 2
    max_off = sd - vd
    lo = MIN_OFFSET_S if allow_negative else 0.0
    step = max(0.1, round((max_off - lo) / MAX_OFFSET_STEPS, 1))
    offsets = np.arange(lo, max_off + 1e-9, step)

    def comb_at(o):
        sync = _sync_pct_at(song, ev_t, ev_w, vd, o, lead_s) if has_sync else 0.0
        if stem_targets is not None and has_sync:
            from . import stemsync
            sync = stemsync.blend_sync(sync, _quiet_at(song, video, ev_t, ev_w, stem_targets, vd, o, lead_s))
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
    quiet = _quiet_at(song, video, ev_t, ev_w, stem_targets, vd, off, lead_s) if (stem_targets is not None and has_sync) else None
    return _pack(song, video, off, tempo_pct, rate, sync, arc, lead_s, quiet)
