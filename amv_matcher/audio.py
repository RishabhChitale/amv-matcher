"""Song analysis: BPM, beat times, onset envelope, with an on-disk cache."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import librosa
import numpy as np
from scipy.ndimage import gaussian_filter1d

from . import curves
from .cache import get_cache

SR = 22050
HOP = 512
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}


def exc_reason(e: BaseException) -> str:
    """Skip reason for a song that failed: "<Class>: <message>", but an empty message (audioread's NoBackendError, a bare
    decode error) or one that is only a class name becomes "unreadable audio file (<Class>)"."""
    import re as _re
    n, m = type(e).__name__, str(e).strip()
    if not m:
        return f"unreadable audio file ({n})"
    if _re.fullmatch(r"[A-Za-z_][\w.]*:?", m):     # e.g. "NoBackendError: " carried through a worker's ValueError
        return f"unreadable audio file ({m.rstrip(':').rsplit('.', 1)[-1]})"
    return f"{n}: {m}"

from . import paths as _paths
CACHE_PATH = _paths.CACHE / "songs.json"
WF_DIR = CACHE_PATH.parent / "wf"
WF_POINTS = 4000
CACHE_VERSION = "v9"   # v9: half-beat phase check against the kick band (F20)
GRID_MAX_FIT_MS = 70.0     # use the constant-tempo grid when the robust beat residual is below this
GRID_MAX_DRIFT_MS = 100.0   # ...and no 16-beat window drifts further than this from the line
GRID_MIN_BEATS = 8
GRID_SMOOTH_S = 0.015
BEATS_PER_BAR = 4


def _comb(env, fps, per, ph):
    g = np.arange(ph, (len(env) - 1) / fps, per)
    return float(np.interp(g, np.arange(len(env)) / fps, env).mean()) if len(g) else 0.0


def fit_grid(env, fps, beat_times, bpm_hint,
             max_fit_ms: float = GRID_MAX_FIT_MS, max_drift_ms: float = GRID_MAX_DRIFT_MS):
    """Constant-tempo beat grid: comb fit (period + phase) to the onset envelope, then a
    robust check of the tracker's beats against it.

    (The tracker's own beats wander +-100 ms in places even on a constant-tempo song, so a
    line through them is a poor grid; the comb over the whole envelope is much sharper.)
    Returns beat_grid, grid_bpm, grid_fit_ms (1.4826 * median |tracked beat - nearest grid
    beat|, ms), grid_drift_ms (largest phase shift of a quarter of the song vs the whole),
    grid_used (small residual and no drift: consumers may use the grid instead of raw beats).
    """
    bt = np.asarray(beat_times, dtype=float)
    env = np.asarray(env, dtype=float)
    empty = {"beat_grid": [], "grid_bpm": 0.0, "grid_fit_ms": 0.0, "grid_drift_ms": 0.0,
             "grid_used": False, "grid_t0": 0.0, "grid_period": 0.0}
    if len(bt) < GRID_MIN_BEATS or bpm_hint <= 0 or len(env) < 8:
        return empty
    e = gaussian_filter1d(env / (np.percentile(env, 95) + 1e-9), GRID_SMOOTH_S * fps)
    p0 = 60.0 / bpm_hint
    best = (-1.0, p0, 0.0)
    for per in np.arange(p0 * 0.97, p0 * 1.03, 0.0002):
        for ph in np.arange(0.0, per, 0.005):
            v = _comb(e, fps, per, ph)
            if v > best[0]:
                best = (v, float(per), float(ph))
    _, per, ph = best
    for step_p, step_t in ((0.00005, 0.002), (0.00001, 0.0005)):
        cand = [(_comb(e, fps, per + dp, ph + dt), per + dp, ph + dt)
                for dp in np.arange(-4, 5) * step_p for dt in np.arange(-4, 5) * step_t]
        _, per, ph = max(cand)
    n_pre = int(np.floor(ph / per))
    ph -= n_pre * per
    dur = (len(env) - 1) / fps
    grid = np.arange(ph, dur, per)
    # tracked beats vs the grid (robust: median of the nearest-beat distances)
    near = np.abs(bt[:, None] - grid[None, :]).min(axis=1)
    fit_ms = 1000.0 * 1.4826 * float(np.median(near))
    # drift: re-fit the phase on each quarter of the song with the period fixed
    drift = 0.0
    q = len(env) // 4
    for k in range(4):
        sub = e[k * q:(k + 1) * q]
        if len(sub) < 8 * fps:
            continue
        t0 = k * q / fps
        sc = [(_comb(sub, fps, per, (ph - t0) % per + dt), dt) for dt in np.arange(-0.1, 0.1001, 0.004)]
        drift = max(drift, abs(max(sc)[1]) * 1000.0)
    return {"beat_grid": [round(float(t), 4) for t in grid],
            "grid_bpm": round(60.0 / per, 3), "grid_fit_ms": round(fit_ms, 1),
            "grid_drift_ms": round(drift, 1),
            "grid_used": bool(fit_ms <= max_fit_ms and drift <= max_drift_ms),
            "grid_t0": round(ph, 4), "grid_period": round(per, 5)}


def bar_phase(grid, strength_t, strength_v, per_bar: int = BEATS_PER_BAR) -> int:
    """Which grid beat (index mod per_bar) is the downbeat: the one with most onset+bass energy."""
    grid = np.asarray(grid, dtype=float)
    if len(grid) < per_bar * 2:
        return 0
    v = np.interp(grid, strength_t, strength_v)
    sums = [float(v[k::per_bar].sum()) for k in range(per_bar)]
    return int(np.argmax(sums))


def _key(path: str) -> str:
    st = os.stat(path)
    return f"{CACHE_VERSION}|{os.path.abspath(path)}|{int(st.st_mtime)}|{st.st_size}"


def _refine_bpm(beat_times: np.ndarray, bpm: float) -> float:
    """librosa's tempo is quantized to coarse bins, and individual beats snap to
    ~23 ms frames. The mean beat spacing over the whole track is far more
    precise; accept it when it stays in the tracker's octave (+-10%)."""
    if len(beat_times) < 8:
        return bpm
    span = float(beat_times[-1] - beat_times[0])
    if span <= 0:
        return bpm
    refined = 60.0 * (len(beat_times) - 1) / span
    return refined if abs(refined - bpm) / bpm < 0.1 else bpm


def _p95_norm(x):
    x = np.asarray(x, dtype=float)
    return np.clip(x / (np.percentile(x, 95) + 1e-9), 0, 1.5)


def waveform_peaks(y: np.ndarray, n: int = WF_POINTS) -> list:
    """Flat [min0, max0, min1, max1, ...] peak pairs over the whole song."""
    edges = np.linspace(0, len(y), n + 1).astype(int)
    out = []
    for a, b in zip(edges[:-1], edges[1:]):
        seg = y[a:max(b, a + 1)]
        out += [round(float(seg.min()), 3), round(float(seg.max()), 3)]
    return out


def _wf_file(key: str, wf_dir: Path) -> Path:
    return wf_dir / (hashlib.sha1(key.encode()).hexdigest() + ".json")


_WARMED = False


def ensure_warm() -> None:
    """Once per process: JIT (or load from the on-disk numba cache) the audio kernels on a tiny synthetic signal,
    under a cross-process lock so several cold processes never compile into the same empty cache at once."""
    global _WARMED
    if _WARMED:
        return
    from .numba_lock import numba_warm_lock
    with numba_warm_lock():
        y = (np.sin(2 * np.pi * 220 * np.arange(SR * 3) / SR) * (np.arange(SR * 3) % (SR // 2) < 800)).astype("float32")
        env = librosa.onset.onset_strength(y=y, sr=SR, hop_length=HOP)
        librosa.beat.beat_track(onset_envelope=env, sr=SR, hop_length=HOP)
        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
        librosa.feature.rms(S=S, frame_length=2048)
    _WARMED = True


KICK_HZ = (30.0, 120.0)
HALF_FLIP_RATIO = 1.3      # the kick band must be this much stronger half a beat later to move the beats


def kick_env(S, freqs):
    """Kick-band onset envelope (positive log-spectral flux, 30-120 Hz), one value per STFT frame."""
    b = S[(freqs >= KICK_HZ[0]) & (freqs < KICK_HZ[1])]
    if not len(b):
        return np.zeros(S.shape[1])
    return np.concatenate([[0.0], np.maximum(0.0, np.diff(np.log1p(b), axis=1)).sum(axis=0)])


def half_beat_fix(grid: dict, beat_times, kenv, fps):
    """Beat trackers on full-band onsets can lock onto the off-beat (open hats on the "and" in house), so every
    beat sits half a beat early or late of the kicks. Compare the kick band on the beats with the kick band half a
    beat later; when the "and" is clearly stronger, move the beats there. Generic: one rule for every song, no
    effect on songs whose kicks are on the beat (or that have no steady kick).
    Returns (grid, beat_times, flipped_grid, flipped_beats)."""
    bt = np.asarray(beat_times, dtype=float)
    k = np.asarray(kenv, dtype=float)
    if len(k) < 8 or not np.any(k > 0):
        return grid, bt, False, False
    e = gaussian_filter1d(k / (np.percentile(k, 95) + 1e-9), GRID_SMOOTH_S * fps)
    t = np.arange(len(e)) / fps
    dur = (len(e) - 1) / fps
    per = float(grid.get("grid_period") or 0.0)
    has_grid = bool(grid.get("beat_grid")) and per > 0
    want_g = want_b = False
    if has_grid:
        ph = float(grid["grid_t0"])
        want_g = _comb(e, fps, per, (ph + per / 2) % per) > HALF_FLIP_RATIO * _comb(e, fps, per, ph)
    if len(bt) >= GRID_MIN_BEATS:
        mid = (bt[:-1] + bt[1:]) / 2
        want_b = np.interp(mid, t, e).mean() > HALF_FLIP_RATIO * np.interp(bt, t, e).mean()
    # Two independent views of the kick (comb on the grid, mean at the tracker beats) must agree; a weak or
    # irregular kick that convinces only one of them is not a phase error and nothing moves.
    fg = fb = bool(want_g and want_b) if has_grid else bool(want_b)
    if fg:
        ph = (ph + per / 2) % per
        g = np.arange(ph, dur, per)
        grid = {**grid, "beat_grid": [round(float(x), 4) for x in g], "grid_t0": round(ph, 4)}
    if fb:
        d0, d1 = bt[1] - bt[0], bt[-1] - bt[-2]
        pre = [bt[0] - d0 / 2] if bt[0] - d0 / 2 >= 0 else []
        post = [bt[-1] + d1 / 2] if bt[-1] + d1 / 2 < dur else []
        bt = np.concatenate([pre, mid, post])
    if fg:      # the residual of the (moved) tracker beats against the (moved) grid
        g = np.asarray(grid["beat_grid"])
        near = np.abs(bt[:, None] - g[None, :]).min(axis=1)
        fit_ms = 1000.0 * 1.4826 * float(np.median(near))
        grid = {**grid, "grid_fit_ms": round(fit_ms, 1),
                "grid_used": bool(fit_ms <= GRID_MAX_FIT_MS and grid.get("grid_drift_ms", 0.0) <= GRID_MAX_DRIFT_MS)}
    return grid, bt, fg, fb


def _compute(path: str):
    ensure_warm()
    y, sr = librosa.load(path, sr=SR, mono=True)  # raises if undecodable
    if y.size == 0:
        raise ValueError("decoded audio is empty")
    duration = float(len(y) / sr)
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    tempo, beats = librosa.beat.beat_track(onset_envelope=env, sr=sr, hop_length=HOP)
    beat_times = librosa.frames_to_time(beats, sr=sr, hop_length=HOP)
    bpm = float(np.atleast_1d(tempo)[0])
    bpm = float(_refine_bpm(beat_times, bpm))

    # Energy curve @10 Hz: RMS loudness + onset strength + low-band (<150 Hz).
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    rms = librosa.feature.rms(S=S, frame_length=2048)[0]
    low = S[freqs < 150].sum(axis=0)
    m = min(len(rms), len(low), len(env))
    raw = (_p95_norm(rms[:m]) + _p95_norm(env[:m]) + _p95_norm(low[:m])) / 3.0
    n10 = int(np.ceil(duration * curves.HZ))
    energy = curves.normalize01(curves.smooth(curves.bin_mean(raw, sr / HOP, n10), 3))

    grid = fit_grid(env, sr / HOP, beat_times, bpm)
    grid, beat_times, flip_g, flip_b = half_beat_fix(grid, beat_times, kick_env(S, freqs), sr / HOP)
    beats = np.clip(np.round(beat_times * sr / HOP).astype(int), 0, len(env) - 1)
    ft = np.arange(m) * HOP / sr
    strength = _p95_norm(env[:m]) + _p95_norm(low[:m])
    phase = bar_phase(grid["beat_grid"], ft, strength) if grid["beat_grid"] else 0

    result = {
        **grid,
        "bar_phase": phase,
        "half_beat_fix": [bool(flip_g), bool(flip_b)],
        "beats_per_bar": BEATS_PER_BAR,
        "bpm": round(bpm, 2),
        "beat_times": [round(float(t), 4) for t in beat_times],
        "beat_strengths": [round(float(v), 3) for v in
                           np.clip(env[np.clip(beats, 0, len(env) - 1)] / (np.percentile(env, 95) + 1e-9), 0, 1)],
        "duration": round(duration, 3),
        "onset_env": [round(float(v), 3) for v in env[::2]],
        "onset_env_fps": sr / HOP / 2,
        "energy_curve": [round(float(v), 3) for v in energy],
        "energy_hz": curves.HZ,
        "drops": curves.find_drops(energy),
        "lulls": curves.find_lulls(energy),
    }
    result.update(_booms(path, S, freqs, env, result.get("beat_grid")))
    from .structure import analyze_structure
    try:
        result["sections"] = analyze_structure(y, sr, result)
    except Exception:          # structure is an extra; never fail the analysis over it
        result["sections"] = []
    return result, waveform_peaks(y)


BOOM_FIELDS = ("booms", "main_boom", "boom_source", "boom_version")


def is_current(hit) -> bool:
    """A cached analysis has every field this version computes (older entries are upgraded on read)."""
    from .boom import BOOM_VERSION
    return isinstance(hit, dict) and all(f in hit for f in BOOM_FIELDS) and hit.get("boom_version") == BOOM_VERSION


def _stem_low(path, n_frames):
    """|STFT| of the cached drums(+bass) stems, or None. Never separates stems itself."""
    try:
        from . import stems
        for prov, names in (("demucs", ("drums", "bass")), ("hpss", ("drums",))):
            c = stems.cached(path, prov)
            if not c:
                continue
            ys = [librosa.load(c["stems"][n], sr=SR, mono=True)[0] for n in names]
            n = min(len(v) for v in ys)
            y = np.sum([v[:n] for v in ys], axis=0)
            L = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
            return L[:, :n_frames] if L.shape[1] >= n_frames * 0.98 else None
    except Exception:
        return None
    return None


def _booms(path, S, freqs, env, beat_grid):
    """F6 audio booms (sub-bass / kick re-entry), see boom.audio_booms."""
    from . import boom
    low = _stem_low(path, S.shape[1])
    try:
        r = boom.audio_booms(S, freqs, SR, HOP, env, low_S=low,
                             source="stems" if low is not None else "mix", beat_grid=beat_grid)
    except Exception:
        r = {"booms": [], "main": None, "source": "mix"}
    return {"booms": r["booms"], "main_boom": r["main"], "boom_source": r["source"], "boom_version": boom.BOOM_VERSION}


def _boom_inputs(path: str):
    """Booms only (upgrade of an older cached analysis): one decode + STFT, no beat tracking."""
    ensure_warm()
    y, sr = librosa.load(path, sr=SR, mono=True)
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    return S, freqs, env


def analyze_song(path: str, cache_path: Path = CACHE_PATH, flush: bool = True) -> dict:
    cache = get_cache(cache_path)
    key = _key(path)
    hit = cache.get(key)
    if hit is not None:
        if not is_current(hit):
            S, freqs, env = _boom_inputs(path)
            hit = {**hit, **_booms(path, S, freqs, env, hit.get("beat_grid"))}
            cache.set(key, hit, flush=flush)
        return hit
    result, peaks = _compute(path)
    wf_dir = Path(cache_path).parent / "wf"
    wf_dir.mkdir(parents=True, exist_ok=True)
    _wf_file(key, wf_dir).write_text(json.dumps(peaks))
    cache.set(key, result, flush=flush)
    return result


def song_waveform(path: str, cache_path: Path = CACHE_PATH) -> list:
    """Flat min/max peaks (~4000 pairs); computed during analysis, cached on disk."""
    wf = _wf_file(_key(path), Path(cache_path).parent / "wf")
    if wf.exists():
        return json.loads(wf.read_text())
    y, _ = librosa.load(path, sr=SR, mono=True)
    peaks = waveform_peaks(y)
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text(json.dumps(peaks))
    return peaks
