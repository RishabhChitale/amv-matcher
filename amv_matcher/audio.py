"""Song analysis: BPM, beat times, onset envelope, with an on-disk cache."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import librosa
import numpy as np

from . import curves
from .cache import get_cache

SR = 22050
HOP = 512
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"}
CACHE_PATH = Path(__file__).resolve().parent.parent / ".cache" / "songs.json"
WF_DIR = CACHE_PATH.parent / "wf"
WF_POINTS = 4000
CACHE_VERSION = "v3"


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


def _compute(path: str):
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

    result = {
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
    return result, waveform_peaks(y)


def analyze_song(path: str, cache_path: Path = CACHE_PATH, flush: bool = True) -> dict:
    cache = get_cache(cache_path)
    key = _key(path)
    hit = cache.get(key)
    if hit is not None:
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
