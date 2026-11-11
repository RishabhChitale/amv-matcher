"""Score how well a song's beat fits a video's motion rhythm."""
from __future__ import annotations

import os

import numpy as np

from ._memo import farr

# F15 (story gate cap, story/key-moment fields) is held out of releases. Off by default; AMV_F15=1 turns it on.
F15_ENABLED = os.environ.get("AMV_F15", "") == "1"
# Beat-following warp lock (half-beat phase flips in spliced audio, tempo drift). On by default since v0.9.5;
# AMV_WARPLOCK=0 turns it off (AMV_WARPLOCK=1 forces it on).
WARPLOCK_DEFAULT = True


def _warplock_flag(env: str | None, default: bool = WARPLOCK_DEFAULT) -> bool:
    """AMV_WARPLOCK value -> on/off: unset or empty keeps the default, "0" is off, anything else is on."""
    if env is None or env.strip() == "":
        return default
    return env.strip() != "0"


WARPLOCK_ENABLED = _warplock_flag(os.environ.get("AMV_WARPLOCK"))

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
    if _uses_grid(song):
        return farr(song["beat_grid"])
    return farr(song["beat_times"])


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


# F15: a song that is not one constant grid (tempo drift, or own audio spliced so the beat phase jumps by
# half a beat) has no single period/origin: a constant-period lock then cancels itself across the jumps.
# Its lock follows the tracked beats instead: event times are warped to beat-index space
# u = interp(t, beats, 0..n-1) (linear past the ends) and the coherence is taken at period 1 in u.
# Songs on the grid keep the constant-period lock unchanged.
# Unlike the constant lock (offset-independent), the warped z moves with the offset, so the offset scan
# picks the max of a noisy null: on unrelated pairs its p95-minus-median over the scan is ~1.5 z. The
# warped z is charged WARP_SCAN_Z for that and only used when it still beats the constant lock.
# Evidence gate: the raw warped z must also reach WARP_MIN_Z. Over a whole offset scan of unrelated pairs the
# warped z peaks at ~1.8 (median ~0, p95 ~1.5), so a scan maximum alone never switches the warp on.
WARP_MIN_BEATS = 16
WARP_SCAN_Z = 1.0
WARP_MIN_Z = 3.0
WARP_FLAT_S = 0.015   # tracked beats this close to one straight grid: the warp equals the constant lock, skipped


def _uses_grid(song):
    g = song.get("beat_grid")
    gb = float(song.get("grid_bpm") or 0)
    bpm = float(song.get("bpm") or 0)
    return bool(song.get("grid_used") and g is not None and len(g) >= 2 and gb > 0 and bpm > 0
                and abs(gb - bpm) / bpm <= GRID_BPM_AGREE)


def _beat_index(t, beats):
    """Fractional beat index of times t along the tracked beats (linear extrapolation past the ends)."""
    n = len(beats)
    u = np.interp(t, beats, np.arange(n, dtype=float))
    lo, hi = t < beats[0], t > beats[-1]
    u[lo] = (t[lo] - beats[0]) / (beats[1] - beats[0])
    u[hi] = n - 1 + (t[hi] - beats[-1]) / (beats[-1] - beats[-2])
    return u


_WARP_MEMO = [None, None, None]   # (beat list held so its id stays unique, key, result): one song per scan


def _warp_beats(bt):
    """Tracked beats as an array when the warp lock applies to them (enough beats, not one straight grid
    within WARP_FLAT_S), else None. Memoized for the last beat list (the offset scan reuses one song)."""
    key = (id(bt), len(bt), bt[0] if len(bt) else None, bt[-1] if len(bt) else None)
    if _WARP_MEMO[0] is bt and _WARP_MEMO[1] == key:
        return _WARP_MEMO[2]
    beats = np.asarray(bt, dtype=float)
    res = None
    if len(beats) >= WARP_MIN_BEATS:
        i = np.arange(len(beats), dtype=float)
        di = i - i.mean()                       # closed-form least-squares line (no np.linalg in the scan path)
        slope = float(di @ (beats - beats.mean()) / (di @ di))
        icpt = float(beats.mean() - slope * i.mean())
        if float(np.abs(beats - (icpt + slope * i)).max()) > WARP_FLAT_S:
            res = beats
    _WARP_MEMO[:] = [bt, key, res]
    return res


def _lock_warp(song, ev_t, ev_w, off, lead):
    """(z, err_s) of the beat-following lock at offset `off` (see WARP_MIN_BEATS), or None when the song
    uses its constant grid or has too few beats."""
    if _uses_grid(song) or len(ev_t) < 3:
        return None
    beats = _warp_beats(song["beat_times"])
    if beats is None:
        return None
    u = _beat_index(ev_t + off + lead, beats)
    w = ev_w / ev_w.sum()
    ratios = np.linspace(1 - LOCK_SPAN, 1 + LOCK_SPAN, 121)
    Z = np.exp(2j * np.pi * u[None, :] * ratios[:, None]) @ w
    Rs = np.abs(Z)
    base = Rs[np.abs(ratios - 1) > LOCK_EXCL]
    med = float(np.median(base))
    mad = max(1.4826 * float(np.median(np.abs(base - med))), 0.5 * float(np.std(base)), 1e-3)
    z1 = Z[60]
    z = (abs(z1) - med) / mad * min(1.0, len(ev_t) / 12.0)
    return z, abs(float(np.angle(z1))) / (2 * np.pi) * float(np.median(np.diff(beats)))


def _sync_pct_at(song, ev_t, ev_w, vd, off, lead_s=None):
    """Discriminative sync 0..100: logistic of the tempo-lock prominence z, times how well
    the offset puts the events at the expected lag from the beat (cosine of phase error)."""
    ev_t = np.asarray(ev_t, dtype=float)
    ev_w = np.asarray(ev_w, dtype=float)
    kw = _lock_warp(song, ev_t, ev_w, off, _lead(lead_s)) if WARPLOCK_ENABLED else None
    k = _lock(song, ev_t, ev_w)
    if kw is not None and kw[0] >= WARP_MIN_Z and (k is None or kw[0] - WARP_SCAN_Z > k[0]):
        z, err = kw[0] - WARP_SCAN_Z, kw[1]
    else:
        if k is None:
            return 0.0
        z, z0c, T, g0 = k
        # coherence of events shifted by off, relative to the grid origin and expected lag
        ph = np.exp(2j * np.pi * (off - g0 + _lead(lead_s)) / T) * z0c
        err = abs(float(np.angle(ph))) / (2 * np.pi) * T          # seconds from the expected lag
    align = 1.0 if err <= LOCK_TOL_S else float(np.exp(-0.5 * ((err - LOCK_TOL_S) / LOCK_TOL_S) ** 2))
    lock = 100.0 / (1.0 + np.exp(-LOCK_A * (z - LOCK_B))) * align
    # small fine-alignment term: keeps retime/splice/nudge improvements visible once the
    # lock itself has saturated (real edits are only loosely sample-accurate, so weight is low)
    b = _beat_level(ev_t, ev_w, _beats(song), vd, off, lead_s)
    fine = 100.0 * (1.0 - np.exp(-b / 0.05))
    return float(np.clip((1 - LOCK_FINE_W - LOCK_RAW_W) * lock + LOCK_FINE_W * fine + LOCK_RAW_W * 100.0 * b, 0, 100))


# F9: pan/tilt/zoom start/stop (and whip-pan midpoints) as sync events weighted below cuts/peaks. OFF by default:
# on the A/B/S1 ground truth weight 0.4/1.0 dropped A own 88 -> 80 and A+S1 68 (extra events the beat grid
# cannot all hit dilute sync). Camera intensity still feeds the energy arc (video.CAM_ENERGY_W).
CAM_EVENT_WEIGHT = 0.0
CAM_WHIP_WEIGHT = 0.0
CAM_EVENT_GUARD_S = 0.25     # camera boundaries this close to a cut/peak/edge are cut-induced: skipped


def _camera_events(video, taken):
    """F9 camera sync events (t, w): pan/tilt/zoom starts and stops plus whip midpoints, skipping those
    within CAM_EVENT_GUARD_S of an already-taken event (cuts, peaks) or of the clip edges."""
    evs = video.get("camera_events") or []
    if not evs or (CAM_EVENT_WEIGHT <= 0 and CAM_WHIP_WEIGHT <= 0):
        return np.zeros(0), np.zeros(0)
    vd = float(video.get("duration") or 0.0)
    cand = []
    for e in evs:
        if e.get("kind") == "whip":
            if CAM_WHIP_WEIGHT > 0:
                cand.append((0.5 * (float(e["start"]) + float(e["end"])), CAM_WHIP_WEIGHT))
        elif CAM_EVENT_WEIGHT > 0:
            cand += [(float(e["start"]), CAM_EVENT_WEIGHT), (float(e["end"]), CAM_EVENT_WEIGHT)]
    if not cand:
        return np.zeros(0), np.zeros(0)
    cand.sort()
    ct = np.asarray([c[0] for c in cand], dtype=float)
    cw = np.asarray([c[1] for c in cand], dtype=float)
    ok = ct >= CAM_EVENT_GUARD_S
    if vd > 0:
        ok &= ct <= vd - CAM_EVENT_GUARD_S
    ref = np.sort(np.asarray(taken, dtype=float))
    if len(ref):
        ok &= _dist_to(ct, ref) > CAM_EVENT_GUARD_S
    out_t, out_w, last = [], [], -1e9
    for t, w in zip(ct[ok], cw[ok]):          # camera boundaries closer than the guard: keep the first
        if t - last > CAM_EVENT_GUARD_S:
            out_t.append(t)
            out_w.append(w)
            last = t
    return np.asarray(out_t, dtype=float), np.asarray(out_w, dtype=float)


def _events(video):
    cuts = np.asarray(video["cut_times"], dtype=float)
    peaks = np.asarray(video["motion_peaks"], dtype=float)
    # A peak coinciding with a cut is the same event; keep it once (as a cut).
    if len(cuts) and len(peaks):
        peaks = peaks[np.min(np.abs(peaks[:, None] - cuts[None, :]), axis=1) > 0.1]
    ct, cw = _camera_events(video, np.concatenate([cuts, peaks]))
    t = np.concatenate([cuts, peaks, ct])
    w = np.concatenate([np.full(len(cuts), CUT_WEIGHT), np.full(len(peaks), PEAK_WEIGHT), cw])
    return t, w


def _arc_pct_at(song, video, off):
    """Pearson of video vs song energy (mapped to 0-100) plus drop/lull bonuses.
    Only the overlap counts, so a negative offset (song starts late) is not penalised."""
    ve = farr(video["energy_curve"])
    se = farr(song["energy_curve"])
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
    drops = farr(song.get("drops", [])) - off
    for a, _b in video.get("peak_segments", []):
        if len(drops) and np.min(np.abs(drops - a)) <= DROP_MATCH_S:
            matches += 1
    for va, vb in video.get("lulls", []):
        if any(sa - off < vb and sb - off > va for sa, sb in song.get("lulls", [])):
            matches += 1
    pct += min(ARC_BONUS_MAX, ARC_BONUS_PER_MATCH * matches)
    return float(np.clip(pct, 0, 100))


# Story gate (F15). A song can sit on the beat everywhere yet tell the wrong story: its build-ups
# and drops land on the video's calm stretches. "Story" = the max over small lags (|lag| <= STORY_LAG_S,
# editors anticipate or let motion trail the music) of the Pearson r between the video's and the song's
# energy *change* (mean of the next STORY_FWD_S minus mean of the previous STORY_BACK_S). It is
# scored as a z against a bar-shift null: the same statistic with the song moved by whole bars
# (+-STORY_SHIFT_BARS), which keeps the beat lock but breaks phrase alignment. The total is capped
# at STORY_CAP_LO at z <= 0, rising linearly to 100 at z >= STORY_Z_FULL (so 90 needs z >= 1, about
# one sd above phrase-shifted placements). No cap when the gate cannot judge (flat energy, short
# overlap, too few null shifts), so it never blocks a score for lack of data.
STORY_FWD_S = 1.0
STORY_BACK_S = 2.0
STORY_LAG_S = 3.0
STORY_LAG_STEP_S = 0.5
STORY_SHIFT_BARS = 24
STORY_MIN_NULL = 8
STORY_MIN_COVER = 0.5
STORY_CAP_LO = 80.0
STORY_Z_FULL = 2.0


def _on_grid(x, hz):
    x = np.asarray(x if x is not None else [], dtype=float)
    hz = float(hz or ENERGY_HZ)
    if len(x) < 2 or abs(hz - ENERGY_HZ) < 1e-6:
        return x
    n = int(np.ceil(len(x) / hz * ENERGY_HZ))
    return np.interp(np.arange(n) / ENERGY_HZ, np.arange(len(x)) / hz, x)


def _rise(e):
    c = np.concatenate([[0.0], np.cumsum(e)])
    n = len(e)
    i = np.arange(n)
    h1, h2 = int(round(STORY_FWD_S * ENERGY_HZ)), int(round(STORY_BACK_S * ENERGY_HZ))
    hi = np.minimum(n, i + h1)
    lo = np.maximum(0, i - h2)
    a = (c[hi] - c[i]) / np.maximum(1, hi - i)
    b = (c[i] - c[lo]) / np.maximum(1, i - lo)
    return a - b


def _video_story_curve(video):
    e = _on_grid(video.get("energy_curve"), video.get("energy_hz"))
    if len(e) < 3:
        return e
    s = _on_grid(video.get("subject_curve"), video.get("subject_hz"))[:len(e)]
    s = np.pad(s, (0, len(e) - len(s)))
    x = e / (np.percentile(e, 95) + 1e-9)
    if np.any(s > 0):
        x = x + 0.6 * s / (np.percentile(s, 95) + 1e-9)
    return x


def _story_source(song):
    """(source energy at ENERGY_HZ, map) for the story gate. A spliced/looped song (splice._assemble)
    carries 'story_src': the energy of the song it was cut from plus, per output sample, the source
    time heard there (-1 = silence), padded by STORY_LAG_S + slack on both sides. The gate then
    judges the placement inside the whole source song, so a single trim re-scored on its assembled
    copy gets the same story z as scoring the song directly. map None = plain song (identity)."""
    src = song.get("story_src")
    if src:
        return _on_grid(src.get("energy_curve"), src.get("energy_hz")), src
    return _on_grid(song.get("energy_curve"), song.get("energy_hz")), None


def story_stats(song, video, off):
    """{'story_z', 'story_r', 'n_null'} at `off`; story_z None when the gate cannot judge."""
    ve = _video_story_curve(video)
    se, src = _story_source(song)
    none = {"story_z": None, "story_r": None, "n_null": 0}
    bpm = float((src or song).get("bpm") or 0)
    if len(ve) < 4 * STORY_BACK_S * ENERGY_HZ or len(se) < 3 or bpm <= 0 or np.std(ve) < 1e-9 or np.std(se) < 1e-9:
        return none
    ve, se = _rise(ve), _rise(se)
    lags = np.arange(-STORY_LAG_S, STORY_LAG_S + 1e-9, STORY_LAG_STEP_S)
    bar = float((src or song).get("beats_per_bar") or 4) * 60.0 / bpm
    shifts = np.array([0] + [k for k in range(-STORY_SHIFT_BARS, STORY_SHIFT_BARS + 1) if k])
    j = np.arange(len(ve))
    # dst sample heard by video sample j at lag; src sample = map(dst) + whole-bar shift
    dst = j[None, :] + np.round((off - lags) * ENERGY_HZ).astype(int)[:, None]          # (L, n)
    if src is None:
        base = dst.astype(float)
        okd = np.ones(dst.shape, bool)
    else:
        tm = np.asarray(src["t"], dtype=float)
        i = dst - int(src.get("t0", 0))
        okd = (i >= 0) & (i < len(tm))
        tv = np.where(okd, tm[np.clip(i, 0, len(tm) - 1)], -1.0)
        okd &= tv >= 0
        base = np.round(tv * ENERGY_HZ)
    best, valid = np.full(len(shifts), -np.inf), np.zeros(len(shifts), bool)
    need = STORY_MIN_COVER * len(ve)
    for si, s in enumerate(shifts):                       # one (lags, n) block per bar shift
        k = (base + round(s * bar * ENERGY_HZ)).astype(int)
        w = okd & (k >= 0) & (k < len(se))
        b = se[np.clip(k, 0, len(se) - 1)]
        cnt = w.sum(-1)
        nz = np.maximum(cnt, 1)
        da = (ve[None, :] - (ve[None, :] * w).sum(-1)[:, None] / nz[:, None]) * w
        db = (b - (b * w).sum(-1)[:, None] / nz[:, None]) * w
        va, vb = (da * da).sum(-1) / nz, (db * db).sum(-1) / nz
        good = (cnt >= need) & (np.sqrt(va) >= 1e-9) & (np.sqrt(vb) >= 1e-9)
        if good.all():
            valid[si] = True
            best[si] = float(((da * db).sum(-1) / nz / np.sqrt(va * vb)).max())
    if not valid[0]:
        return none
    r0 = float(best[0])
    null = best[1:][valid[1:]]
    if len(null) < STORY_MIN_NULL:
        return dict(none, story_r=round(r0, 3), n_null=int(len(null)))
    z = (r0 - float(null.mean())) / (float(null.std()) + 1e-9)
    return {"story_z": round(float(z), 2), "story_r": round(r0, 3), "n_null": int(len(null))}


def story_cap(z):
    """Highest total a pair may reach given its story z (100 = no cap)."""
    if z is None:
        return 100.0
    return float(STORY_CAP_LO + (100.0 - STORY_CAP_LO) * np.clip(z / STORY_Z_FULL, 0.0, 1.0))


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
    from . import boom, buildup
    bs = buildup.build_stats(song, video, off) if buildup.W_BUILD > 0 else None
    bm = boom.boom_stats(song, video, off, _lead(lead_s)) if boom.W_BOOM > 0 else None
    total = boom.blend_total(buildup.blend_total(_combine(tempo_pct, sync_pct, arc_pct), bs), bm)
    f15 = F15_ENABLED
    st = story_stats(song, video, off) if f15 and total > STORY_CAP_LO else {"story_z": None, "story_r": None, "n_null": 0}
    cap = story_cap(st["story_z"])
    out = {
        "eligible": True,
        "total_pct": int(round(min(total, cap))),
        "tempo_pct": round(tempo_pct, 1),
        "sync_pct": round(sync_pct, 1),
        "arc_pct": round(arc_pct, 1),
        "best_offset_s": round(off, 2),
        "trim_start_s": round(off, 2),
        "trim_end_s": round(off + float(video["duration"]), 2),
        "best_rate": rate,
        "tightness": tightness(song, video, off, lead_s),
    }
    if f15:
        out.update(story_z=st["story_z"], story_capped=bool(total > cap),
                   key_moments=boom.video_key_moments(video))   # key moments: reported only, not in the total
    if bs is not None:
        out.update(build_pct=bs["build_pct"], launch_pct=bs["launch_pct"], n_launches=bs["n_launches"], n_builds=bs["n_builds"])
    out["boom_pct"] = None if bm is None else bm["boom_pct"]
    out["boom"] = None if bm is None else dict(bm["boom"], n_booms=bm["n_booms"], n_matched=bm["n_matched"])
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


def _mmss(s: float) -> str:
    m, sec = divmod(int(round(max(0.0, float(s)))), 60)
    return f"{m}:{sec:02d}"


def video_use(start_s: float, end_s: float, video_duration: float) -> dict:
    """F2 'uses 2:10 of 3:40 video' block: which stretch of the video a shorter song is scored/synced on."""
    vd = float(video_duration)
    used = max(0.0, float(end_s) - float(start_s))
    return {"start_s": round(float(start_s), 3), "end_s": round(float(end_s), 3), "used_s": round(used, 3),
            "video_duration_s": round(vd, 3), "coverage_pct": round(100.0 * used / vd, 1) if vd > 0 else 100.0,
            "label": f"uses {_mmss(used)} of {_mmss(vd)} video"}


def _clip_pairs(pairs, a, b):
    out = []
    for x, y in pairs or []:
        x, y = max(float(x), a), min(float(y), b)
        if y > x:
            out.append([round(x - a, 3), round(y - a, 3)])
    return out


def _clip_spans(spans, a, b):
    out = []
    for d in spans or []:
        x, y = max(float(d["start"]), a), min(float(d["end"]), b)
        if y > x:
            out.append({**d, "start": round(x - a, 3), "end": round(y - a, 3)})
    return out


def _clip_series(xs, hz, a, b):
    if not xs or not hz:
        return xs
    i0, i1 = int(round(a * float(hz))), int(round(b * float(hz)))
    return list(xs[max(0, i0):max(i0 + 1, i1)])


def clip_video(video: dict, start_s: float, end_s: float) -> dict:
    """The [start_s, end_s] stretch of a video analysis as its own analysis (times rebased to 0). Used by F2
    (shorter songs). Tempo fields stay whole-video (tempo evidence is global). Unknown keys are copied."""
    a, b = max(0.0, float(start_s)), min(float(video["duration"]), float(end_s))
    out = dict(video)
    out["duration"] = round(b - a, 3)
    times = lambda xs: [round(float(t) - a, 3) for t in xs or [] if a <= float(t) <= b]
    out["cut_times"] = [t for t in times(video.get("cut_times")) if 0.0 < t < b - a]
    out["motion_peaks"] = times(video.get("motion_peaks"))
    out["energy_curve"] = _clip_series(video.get("energy_curve"), video.get("energy_hz") or ENERGY_HZ, a, b)
    if video.get("motion_curve") and video.get("fps"):
        out["motion_curve"] = _clip_series(video["motion_curve"], video["fps"], a, b)
    if video.get("subject_curve"):
        out["subject_curve"] = _clip_series(video["subject_curve"], video.get("subject_hz") or ENERGY_HZ, a, b)
    for k in ("lulls", "peak_segments"):
        if k in video:
            out[k] = _clip_pairs(video[k], a, b)
    for k in ("segments", "segments_struct", "shots"):
        if k in video:
            out[k] = _clip_spans(video[k], a, b)
    if video.get("camera_events"):
        out["camera_events"] = _clip_spans(video["camera_events"], a, b)
    if video.get("visual_booms"):
        out["visual_booms"] = [{**v, "t": round(float(v["t"]) - a, 3)} for v in video["visual_booms"]
                               if a <= float(v["t"]) <= b]
    cm = video.get("camera_motion")
    if cm and cm.get("hz"):
        out["camera_motion"] = {k: (_clip_series(v, cm["hz"], a, b) if isinstance(v, list) else v) for k, v in cm.items()}
    out["video_window"] = video_use(a, b, video["duration"]) | {"source_duration_s": round(float(video["duration"]), 3)}
    return out


def _score_partial(song, video, lead_s, stem_targets):
    """F2: the song (shorter than the video) plays whole, from 0; choose the video stretch [w, w + sd] that
    fits it best (same W_SYNC*sync + W_ARC*arc objective as the offset search, coarse scan + refine), then
    score the song on that stretch. Result carries video_use (start/end/label) and offsets in clip time."""
    vd, sd = float(video["duration"]), float(song["duration"])
    ev_t, ev_w = _events(video)
    has_sync = len(ev_t) > 0 and len(song["beat_times"]) >= 2
    hi = vd - sd
    arc_v = {"energy_curve": video["energy_curve"], "peak_segments": video.get("peak_segments", []),
             "lulls": video.get("lulls", [])}

    def comb_at(w):
        m = (ev_t >= w) & (ev_t <= w + sd)
        sync = _sync_pct_at(song, ev_t[m] - w, ev_w[m], sd, 0.0, lead_s) if (has_sync and m.sum()) else 0.0
        hz = float(video.get("energy_hz") or ENERGY_HZ)
        i0 = int(round(w * hz))
        clip = {"energy_curve": arc_v["energy_curve"][i0:i0 + int(round(sd * hz))],
                "peak_segments": _clip_pairs(arc_v["peak_segments"], w, w + sd),
                "lulls": _clip_pairs(arc_v["lulls"], w, w + sd)}
        return W_SYNC * sync + W_ARC * _arc_pct_at(song, clip, 0.0)

    step = max(0.1, round(hi / MAX_OFFSET_STEPS, 1))
    scanned = sorted(((comb_at(float(w)), float(w)) for w in np.arange(0.0, hi + 1e-9, step)), key=lambda x: -x[0])
    best = None
    for _, seed in scanned[:3]:
        for w in np.arange(seed - REFINE_S, seed + REFINE_S + 1e-9, REFINE_STEP_S):
            w = round(float(min(max(w, 0.0), hi)), 3)
            c = comb_at(w)
            if best is None or c > best[0] + 1e-12 or (abs(c - best[0]) <= 1e-12 and w < best[1]):
                best = (c, w)
    w = best[1]
    clip = clip_video(video, w, w + sd)
    out = score_at(song, clip, 0.0, lead_s, stem_targets)   # tempo fields are kept whole-video by clip_video
    out["video_use"] = clip["video_window"]
    out["partial_video"] = True
    out["song_duration"], out["video_duration"] = round(sd, 2), round(vd, 2)
    return out


def score(song: dict, video: dict, allow_negative: bool = False, lead_s: float = None, stem_targets=None) -> dict:
    """Best placement of the video window inside the song. F2/F2b: a song shorter than the video, however short,
    is scored over the best-matching stretch of the video (see _score_partial; result has partial_video True and
    video_use {start_s, end_s, used_s, coverage_pct, label}). Length never makes a song ineligible: only a song
    with no decodable audio (duration 0) is, with reason 'unreadable'.
    With allow_negative the song may also start up to 3 s after the video (padded with
    silence, no length penalty). Off by default: splice/auto-sync need trim_start_s >= 0.
    lead_s: perceptual lead in seconds (None = PERCEPTUAL_LEAD_S, clamped 0..0.12).
    stem_targets: optional soft targets (see score_at); None = unchanged results."""
    vd, sd = float(video["duration"]), float(song["duration"])
    if not sd > 0.0:
        return {
            "eligible": False, "total_pct": 0, "tempo_pct": 0.0, "sync_pct": 0.0, "arc_pct": 0.0,
            "best_offset_s": 0.0, "trim_start_s": 0.0, "trim_end_s": round(vd, 2), "best_rate": 1.0,
            "reason": "unreadable",
            "tightness": tightness({"beat_times": []}, {"cut_times": [], "motion_peaks": []}, 0.0), "song_duration": round(sd, 2), "video_duration": round(vd, 2),
        }
    if sd < vd:
        return _score_partial(song, video, lead_s, stem_targets)
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
