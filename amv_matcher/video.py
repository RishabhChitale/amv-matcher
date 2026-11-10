"""Video analysis: cuts, motion curve, motion peaks, motion tempo."""
from __future__ import annotations

import cv2
import numpy as np
from scipy.signal import find_peaks
from scipy.ndimage import uniform_filter1d

from . import curves

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
MAX_FPS = 30.0
CUT_K = 6.0          # median + K*MAD for cuts
PEAK_K = 2.5         # median + K*MAD for motion peaks
MIN_BPM, MAX_BPM = 60.0, 200.0


def _mad_threshold(x: np.ndarray, k: float) -> float:
    med = float(np.median(x))
    mad = float(np.median(np.abs(x - med))) * 1.4826
    return med + k * max(mad, 1e-3)


def motion_tempo(curve: np.ndarray, fps: float) -> float:
    """Autocorrelation of the motion curve over the 60-200 BPM lag range."""
    if len(curve) < 8:
        return 0.0
    x = curve - curve.mean()
    n = len(x)
    f = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    if ac[0] <= 0:
        return 0.0
    ac = ac / ac[0]
    lo = max(1, int(np.floor(fps * 60.0 / MAX_BPM)))
    hi = min(n - 1, int(np.ceil(fps * 60.0 / MIN_BPM)))
    if hi <= lo:
        return 0.0
    lags = np.arange(lo, hi + 1)
    seg = ac[lo:hi + 1]
    # Parabolic refinement around the best lag; slight bias to shorter lags is
    # avoided on purpose (octave tolerance is handled in scoring).
    i = int(np.argmax(seg))
    lag = float(lags[i])
    if 0 < i < len(seg) - 1:
        a, b, c = seg[i - 1], seg[i], seg[i + 1]
        d = a - 2 * b + c
        if d != 0:
            lag += 0.5 * (a - c) / d
    return float(60.0 * fps / lag)


def analyze_video(path: str) -> dict:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ValueError("OpenCV could not open the video")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    if src_fps <= 0 or src_fps > 1000:
        src_fps = 30.0
    step = max(1, int(round(src_fps / MAX_FPS)))
    fps = src_fps / step

    diffs = []
    prev = None
    idx = 0
    while True:
        if step > 1:
            ok = cap.grab()
            if not ok:
                break
            if idx % step != 0:
                idx += 1
                continue
            ok, frame = cap.retrieve()
        else:
            ok, frame = cap.read()
        if not ok or frame is None:
            break
        idx += 1
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (160, max(1, int(round(h * 160 / w)))), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev is not None:
            diffs.append(float(np.mean(np.abs(gray - prev))))
        prev = gray
    cap.release()
    if len(diffs) < 8:
        raise ValueError("video too short or no decodable frames")

    raw = np.array(diffs)           # diffs[i] = change between sample i and i+1
    times = (np.arange(len(raw)) + 1) / fps
    duration = float((len(raw) + 1) / fps)

    # Cuts: spikes in the raw frame difference.
    cut_thr = _mad_threshold(raw, CUT_K)
    cut_idx, _ = find_peaks(raw, height=cut_thr, distance=max(1, int(0.25 * fps)))
    cut_times = [round(float(times[i]), 3) for i in cut_idx]

    # Motion curve: lightly smoothed.
    curve = uniform_filter1d(raw, size=3, mode="nearest")
    peak_thr = _mad_threshold(curve, PEAK_K)
    pk_idx, _ = find_peaks(curve, height=peak_thr, distance=max(1, int(0.12 * fps)))
    motion_peaks = [round(float(times[i]), 3) for i in pk_idx]

    # Energy arc @10 Hz: motion magnitude + cut density over a sliding 1 s window.
    n10 = int(np.ceil(duration * curves.HZ))
    mot = curves.bin_mean(curve, fps, n10)
    mot = mot / (np.percentile(mot, 95) + 1e-9)
    cut_hist = np.zeros(n10)
    for t in cut_times:
        cut_hist[min(n10 - 1, int(t * curves.HZ))] += 1
    density = uniform_filter1d(cut_hist, size=int(curves.HZ), mode="nearest") * curves.HZ  # cuts/s
    density = density / (density.max() + 1e-9) if density.max() > 0 else density
    energy = curves.normalize01(uniform_filter1d(np.clip(mot, 0, 1.5) + 0.5 * density, size=int(curves.HZ), mode="nearest"))
    shots = _shots(cut_times, duration, energy, curve, fps)

    return {
        "energy_curve": [round(float(v), 3) for v in energy],
        "energy_hz": curves.HZ,
        "segments": curves.label_segments(energy),
        "lulls": curves.find_lulls(energy),
        "peak_segments": curves.find_peak_segments(energy),
        "shots": shots,
        "duration": round(duration, 3),
        "fps": round(fps, 3),
        "cut_times": cut_times,
        "motion_curve": [round(float(v), 3) for v in curve],
        "motion_peaks": motion_peaks,
        "motion_tempo_bpm": round(motion_tempo(curve, fps), 2),
    }


def _shots(cut_times, duration, energy, curve, fps):
    """Per-shot summary (start, end, mean/peak energy, motion tempo); future auto-edit input."""
    bounds = [0.0] + [t for t in cut_times] + [duration]
    out = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b - a < 0.1:
            continue
        e = energy[int(a * curves.HZ):max(int(a * curves.HZ) + 1, int(b * curves.HZ))]
        c = curve[int(a * fps):int(b * fps)]
        out.append({
            "start": round(a, 3), "end": round(b, 3),
            "mean_energy": round(float(e.mean()), 3), "peak_energy": round(float(e.max()), 3),
            "motion_tempo_bpm": round(motion_tempo(c, fps), 1) if len(c) >= 24 else 0.0,
        })
    return out
