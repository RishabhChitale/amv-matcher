"""Video analysis: cuts, motion curve, motion peaks, motion tempo."""
from __future__ import annotations

import cv2
import numpy as np
from scipy.signal import find_peaks
from scipy.ndimage import gaussian_filter1d, median_filter, uniform_filter1d

from . import curves

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
MAX_FPS = 60.0       # analyse up to this many frames per second (full rate for 60 fps sources)
CUT_RATIO = 3.5          # a cut: frame diff >= this x the local median diff (+-CUT_WINDOW_S)
CUT_ISOLATION = 1.2      # ...and >= this x the larger neighbouring diff (single-frame spike)
CUT_WINDOW_S = 0.5
CUT_MIN_DIFF = 4.0   # and at least this many gray levels (mean abs diff), so static noise never cuts
CUT_MIN_GAP_FRAMES = 4
FLASH_FRAMES = 3     # a brightness spike that returns within this many frames is a flash, not a cut
FLASH_RETURN = 0.4   # "returns" = frame i+1+k within 40% of the spike size of frame i
FADE_DARK = 30.0     # fade through black: brightness minimum below this ...
FADE_DEPTH = 40.0    # ... with this much brighter on both sides within FADE_SPAN_S
FADE_SPAN_S = 1.0
TEMPO_HZ = 200.0     # impulse-train resolution for event-periodicity tempo
EVENT_SIGMA_S = 0.04
MIN_TEMPO_EVENTS = 8
MIN_TEMPO_STRENGTH = 0.05
TEMPO_CAND_REL = 0.6  # keep tempo candidates this fraction as strong as the best
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


def detect_cuts(raw: np.ndarray, thumbs: np.ndarray, bright: np.ndarray, fps: float):
    """Cut times (s) from per-frame data. raw[i] = mean |diff| between samples i and i+1.

    Local contrast (diff vs the +-0.5 s median), flash suppression, fades through black,
    minimum gap of 4 frames.
    """
    n = len(raw)
    win = 2 * int(round(CUT_WINDOW_S * fps)) + 1
    med = median_filter(raw, size=win, mode="nearest")
    thr = np.maximum(CUT_RATIO * np.maximum(med, 0.5), CUT_MIN_DIFF)
    idx, _ = find_peaks(raw, height=thr, distance=CUT_MIN_GAP_FRAMES)
    # a cut is a single-frame spike: sustained fast motion has similar diffs on both sides
    idx = [i for i in idx
           if raw[i] >= CUT_ISOLATION * max(raw[max(0, i - 2):i].max(initial=0.0),
                                            raw[i + 1:i + 3].max(initial=0.0), 1e-6)]
    t16 = thumbs.astype(np.float32)

    def dd(a, b):
        return float(np.mean(np.abs(t16[a] - t16[b])))

    keep = []
    for i in idx:
        d0 = dd(i, i + 1)
        flash = False
        for k in range(1, FLASH_FRAMES + 1):
            if i + 1 + k < len(t16) and dd(i, i + 1 + k) < FLASH_RETURN * d0:
                flash = True          # flash on: the picture comes back
            if i - k >= 0 and dd(i - k, i + 1) < FLASH_RETURN * d0:
                flash = True          # flash off: this is the way back
        if not flash:
            keep.append(int(i))
    cuts = [(i + 1) / fps for i in keep]

    # Fade through black: one cut at the darkest frame when no spike cut is near.
    span = max(1, int(round(FADE_SPAN_S * fps)))
    sm = uniform_filter1d(bright, size=max(3, int(round(0.1 * fps))), mode="nearest")
    mins, _ = find_peaks(-sm, distance=span)
    for j in mins:
        if sm[j] >= FADE_DARK:
            continue
        before = sm[max(0, j - span):j + 1].max()
        after = sm[j:j + span + 1].max()
        if before - sm[j] >= FADE_DEPTH and after - sm[j] >= FADE_DEPTH:
            tj = j / fps
            if all(abs(tj - c) > 0.5 for c in cuts):
                cuts.append(tj)
    return sorted(round(float(c), 3) for c in cuts)


def events_tempo(cut_times, peak_times, duration: float):
    """Tempo from the periodicity of the cut + motion-peak impulse train (60-200 BPM).

    Returns (bpm, candidates) where candidates = [(bpm, strength), ...] strongest first
    (strength = normalised autocorrelation); (0.0, []) when too few events.
    """
    cuts = np.asarray(cut_times, dtype=float)
    peaks = np.asarray(peak_times, dtype=float)
    if len(cuts) and len(peaks):
        peaks = peaks[np.min(np.abs(peaks[:, None] - cuts[None, :]), axis=1) > 0.1]
    if len(cuts) + len(peaks) < MIN_TEMPO_EVENTS:
        return 0.0, []
    n = int(np.ceil(duration * TEMPO_HZ)) + 1
    x = np.zeros(n)
    for t, w in ((cuts, 2.0), (peaks, 1.0)):
        for v in t:
            x[min(n - 1, int(round(v * TEMPO_HZ)))] += w
    x = gaussian_filter1d(x, EVENT_SIGMA_S * TEMPO_HZ)
    x = x - x.mean()
    f = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    if ac[0] <= 0:
        return 0.0, []
    ac = ac / ac[0] * n / np.maximum(n - np.arange(n), 1)    # unbiased in the lag overlap
    lo = int(np.floor(TEMPO_HZ * 60.0 / MAX_BPM))
    hi = min(n - 2, int(np.ceil(TEMPO_HZ * 60.0 / MIN_BPM)))
    if hi <= lo + 2:
        return 0.0, []
    seg = gaussian_filter1d(ac[lo:hi + 1], 1.5)
    pk, _ = find_peaks(seg)
    cands = []
    for i in pk:
        lag = float(lo + i)
        if 0 < i < len(seg) - 1:
            a, b, c = seg[i - 1], seg[i], seg[i + 1]
            d = a - 2 * b + c
            if d != 0:
                lag += 0.5 * (a - c) / d
        cands.append((60.0 * TEMPO_HZ / lag, float(seg[i])))
    if not cands:
        return 0.0, []
    cands.sort(key=lambda c: -c[1])
    top = cands[0][1]
    if top < MIN_TEMPO_STRENGTH:
        return 0.0, []
    cands = [(round(b, 2), round(s, 3)) for b, s in cands if s >= TEMPO_CAND_REL * top][:4]
    return cands[0][0], cands


def analyze_video(path: str) -> dict:
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ValueError("OpenCV could not open the video")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    if src_fps <= 0 or src_fps > 1000:
        src_fps = 30.0
    step = max(1, int(round(src_fps / MAX_FPS)))
    fps = src_fps / step

    diffs, thumbs, bright = [], [], []
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
        bright.append(float(gray.mean()))
        thumbs.append(cv2.resize(gray, (32, 18), interpolation=cv2.INTER_AREA).astype(np.uint8))
        prev = gray
    cap.release()
    if len(diffs) < 8:
        raise ValueError("video too short or no decodable frames")

    raw = np.array(diffs)           # diffs[i] = change between sample i and i+1
    times = (np.arange(len(raw)) + 1) / fps
    duration = float((len(raw) + 1) / fps)

    # Cuts: local-contrast spikes, flash-suppressed (see detect_cuts).
    cut_times = detect_cuts(raw, np.array(thumbs), np.array(bright), fps)

    # Motion curve: lightly smoothed.
    curve = uniform_filter1d(raw, size=max(3, int(round(0.1 * fps))), mode="nearest")
    peak_thr = _mad_threshold(curve, PEAK_K)
    pk_idx, _ = find_peaks(curve, height=peak_thr, distance=max(1, int(0.12 * fps)))
    motion_peaks = [round(float(times[i]), 3) for i in pk_idx]

    # Tempo from event periodicity; the old motion-curve autocorrelation is the fallback.
    motion_bpm = motion_tempo(curve, fps)
    ev_bpm, ev_cands = events_tempo(cut_times, motion_peaks, duration)

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
        "segments_struct": struct_segments(curves.label_segments(energy), duration, energy),
        "lulls": curves.find_lulls(energy),
        "peak_segments": curves.find_peak_segments(energy),
        "shots": shots,
        "duration": round(duration, 3),
        "fps": round(fps, 3),
        "cut_times": cut_times,
        "motion_curve": [round(float(v), 3) for v in curve],
        "motion_peaks": motion_peaks,
        "motion_tempo_bpm": round(ev_bpm if ev_bpm > 0 else motion_bpm, 2),
        "motion_tempo_source": "events" if ev_bpm > 0 else "motion",
        "motion_tempo_motion_bpm": round(motion_bpm, 2),
        "motion_tempo_candidates": [[float(x) for x in c] for c in ev_cands],
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


STRUCT_MIN_S = 2.0


def struct_segments(segs, duration: float, energy, min_s: float = STRUCT_MIN_S):
    """Phrase-level video segments covering [0, duration]: [{start, end, kind calm|build|peak, energy}].

    Merges the fine calm/build/peak segments: each fine segment takes its own kind (gaps are
    'build' when the energy rises into the next block, else 'calm'), neighbours of one kind join,
    and any block shorter than `min_s` is absorbed by its more energetic-matching neighbour."""
    hz = curves.HZ
    energy = np.asarray(energy, dtype=float)
    duration = float(duration)
    if duration <= 0:
        return []
    kinds = {"calm": 0, "build": 1, "peak": 2}
    step = 0.1
    n = max(1, int(round(duration / step)))
    lab = np.full(n, -1, dtype=int)
    for s in sorted(segs, key=lambda s: kinds.get(s["label"], 0)):      # peak overrides build overrides calm
        a, b = int(round(s["start"] / step)), int(round(s["end"] / step))
        lab[max(0, a):min(n, b)] = kinds.get(s["label"], 0)
    for i in range(n):                                                   # gaps: plain calm
        if lab[i] < 0:
            lab[i] = 0
    blocks = []
    for i, k in enumerate(lab):
        if blocks and blocks[-1][2] == k:
            blocks[-1][1] = i + 1
        else:
            blocks.append([i, i + 1, int(k)])
    def _e(a, b):
        seg = energy[int(a * step * hz):max(int(a * step * hz) + 1, int(b * step * hz))]
        return float(seg.mean()) if len(seg) else 0.0
    changed = True
    while changed and len(blocks) > 1:
        changed = False
        short = [i for i, b in enumerate(blocks) if (b[1] - b[0]) * step < min_s - 1e-9]
        if not short:
            break
        i = min(short, key=lambda j: blocks[j][1] - blocks[j][0])
        cand = [j for j in (i - 1, i + 1) if 0 <= j < len(blocks)]
        me = _e(blocks[i][0], blocks[i][1])
        j = min(cand, key=lambda j: abs(_e(blocks[j][0], blocks[j][1]) - me))
        blocks[j][0], blocks[j][1] = min(blocks[j][0], blocks[i][0]), max(blocks[j][1], blocks[i][1])
        del blocks[i]
        merged = []
        for b in blocks:
            if merged and merged[-1][2] == b[2]:
                merged[-1][1] = b[1]
            else:
                merged.append(b)
        blocks = merged
        changed = True
    out = []
    for a, b, k in blocks:
        t0, t1 = round(a * step, 3), round(b * step, 3)
        out.append({"start": t0, "end": t1 if b < n else round(duration, 3),
                    "kind": ("calm", "build", "peak")[k], "energy": round(_e(a, b), 3)})
    out[0]["start"] = 0.0
    return out
