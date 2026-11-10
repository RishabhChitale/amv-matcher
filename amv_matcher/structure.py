"""Song structure: bar-synchronous self-similarity -> novelty boundaries -> repeat groups -> labels.

sections = [{start, end, label, energy, repeat_group}]  (times in song seconds, group = "A", "B", ...)
"""
from __future__ import annotations

import librosa
import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.ndimage import gaussian_filter1d
from scipy.signal import find_peaks

from . import curves

HOP = 512
KERNEL_BARS = 4          # half-width of the checkerboard kernel
MIN_SECTION_BARS = 4
CLUSTER_DIST = 0.9       # average-linkage cut on z-scored section features (see _cluster)
LABELS = ("intro", "verse", "pre", "chorus", "bridge", "breakdown", "drop", "outro")


def _z(x):
    x = np.asarray(x, dtype=float)
    sd = x.std(axis=0, keepdims=True)
    return (x - x.mean(axis=0, keepdims=True)) / np.where(sd < 1e-9, 1.0, sd)


def _bar_edges(song: dict) -> np.ndarray:
    """Bar start times: the grid downbeats (bar_phase) when there is a grid, else every 4th tracked beat."""
    per_bar = int(song.get("beats_per_bar", 4))
    grid = np.asarray(song.get("beat_grid") or [], dtype=float)
    if len(grid) >= 2 * per_bar:
        edges = grid[int(song.get("bar_phase", 0)) % per_bar::per_bar]
    else:
        bt = np.asarray(song.get("beat_times") or [], dtype=float)
        edges = bt[::per_bar] if len(bt) >= 2 * per_bar else np.array([])
    if len(edges) < 2:
        return edges
    bar = float(np.median(np.diff(edges)))
    dur = float(song["duration"])
    pre = []
    t = edges[0] - bar            # bars before the first downbeat (pickup / intro)
    while t > -0.25 * bar:
        pre.append(max(0.0, t))
        t -= bar
    edges = np.concatenate([pre[::-1], edges])
    if edges[-1] < dur - 0.5 * bar:
        edges = np.append(edges, edges[-1] + bar)
    return np.unique(np.round(edges, 4))


def bar_features(y: np.ndarray, sr: int, edges: np.ndarray, dur: float):
    """Per-bar [chroma(12), mfcc(12)] means, each block z-scored. Rows = bars."""
    chroma = librosa.feature.chroma_stft(y=y, sr=sr, hop_length=HOP)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, hop_length=HOP, n_mfcc=13)[1:]
    ft = librosa.frames_to_time(np.arange(chroma.shape[1]), sr=sr, hop_length=HOP)
    bounds = np.append(edges, max(dur, edges[-1] + 1e-3))
    rows_c, rows_m = [], []
    for a, b in zip(bounds[:-1], bounds[1:]):
        m = (ft >= a) & (ft < b)
        if not m.any():
            m = np.zeros_like(m)
            m[min(len(ft) - 1, int(np.searchsorted(ft, a)))] = True
        rows_c.append(chroma[:, m].mean(1))
        rows_m.append(mfcc[:, m].mean(1))
    return np.hstack([_z(rows_c) * 0.7, _z(rows_m)])


def novelty(F: np.ndarray, k: int = KERNEL_BARS) -> np.ndarray:
    """Foote novelty over the bar-level self-similarity matrix (cosine on z-scored features)."""
    n = len(F)
    nrm = np.linalg.norm(F, axis=1, keepdims=True)
    U = F / np.where(nrm < 1e-9, 1.0, nrm)
    S = U @ U.T
    g = np.exp(-0.5 * (np.arange(-k, k) + 0.5) ** 2 / (0.5 * k) ** 2)
    sign = np.outer(np.sign(np.arange(-k, k) + 0.5), np.sign(np.arange(-k, k) + 0.5))
    K = sign * np.outer(g, g)
    P = np.pad(S, k, mode="edge")
    out = np.zeros(n)
    for i in range(n):
        out[i] = (P[i:i + 2 * k, i:i + 2 * k] * K).sum()   # boundary between bar i-1 and bar i
    return np.clip(out, 0, None)


def boundaries(nov: np.ndarray, min_bars: int = MIN_SECTION_BARS) -> list:
    """Bar indices (>0) where a new section starts."""
    n = len(nov)
    if n < 2 * min_bars:
        return []
    sm = gaussian_filter1d(nov, 0.7)
    thr = float(np.median(sm) + 0.5 * (sm.max() - np.median(sm))) if sm.max() > 0 else 1.0
    pk, _ = find_peaks(sm, height=thr, distance=min_bars)
    pk = [int(p) for p in pk if min_bars <= p <= n - min_bars]
    return pk


def _cluster(F: np.ndarray, spans: list) -> list:
    """Repeat groups: average-linkage clustering on section-mean features (+ length similarity)."""
    if len(spans) == 1:
        return [0]
    M = np.array([F[a:b].mean(0) for a, b in spans])
    # cosine on un-centered means is too forgiving; centre across sections first
    Zc = linkage(M - M.mean(0, keepdims=True), "average", metric="cosine")
    lab = fcluster(Zc, t=CLUSTER_DIST, criterion="distance")
    order, out = {}, []
    for v in lab:
        order.setdefault(int(v), len(order))
        out.append(order[int(v)])
    return out


def _name(i: int) -> str:
    return chr(ord("A") + i) if i < 26 else f"G{i}"


def _stem_refine(secs: list, stems: dict):
    """stems = {'drums': [rms @10Hz], 'vocals': [...]}. Drum-less low sections -> breakdown, vocal-led -> verse/chorus."""
    hz = curves.HZ
    d = stems.get("drums")
    v = stems.get("vocals")
    for i, s in enumerate(secs):
        a, b = int(s["start"] * hz), max(int(s["start"] * hz) + 1, int(s["end"] * hz))
        if d is not None and len(d) > a:
            s["drums"] = round(float(np.mean(d[a:b])), 3)
        if v is not None and len(v) > a:
            s["vocals"] = round(float(np.mean(v[a:b])), 3)
    if any("drums" in s for s in secs):
        for i, s in enumerate(secs[:-1]):
            if "drums" in s and s["drums"] < 0.15 and 0 < i and s["label"] in ("verse", "bridge", "pre", "intro"):
                if secs[i + 1]["energy"] > s["energy"] + 0.15:
                    s["label"] = "breakdown"
    if any("vocals" in s for s in secs):
        for s in secs:
            if s["label"] == "pre" and s.get("vocals", 1) < 0.1:
                s["label"] = "breakdown" if s.get("drums", 1) < 0.15 else "pre"
    return secs


def label_sections(secs: list):
    """Labels from energy, position and repetition (in place)."""
    n = len(secs)
    en = np.array([s["energy"] for s in secs])
    groups = {}
    for i, s in enumerate(secs):
        groups.setdefault(s["repeat_group"], []).append(i)
    rep = {g: ix for g, ix in groups.items() if len(ix) >= 2}
    chorus_g = None
    if rep:
        chorus_g = max(rep, key=lambda g: (np.mean(en[rep[g]]), len(rep[g])))
    elif n >= 2:
        chorus_g = secs[int(np.argmax(en))]["repeat_group"]
    med = float(np.median(en)) if n else 0.0
    for i, s in enumerate(secs):
        g = s["repeat_group"]
        e = s["energy"]
        nxt = secs[i + 1]["energy"] if i + 1 < n else None
        prv = secs[i - 1]["energy"] if i > 0 else None
        if i == 0 and n > 2 and g == chorus_g and len(groups[g]) >= 2 \
                and e < max(en[j] for j in groups[g]) - 0.03:
            lab = "intro"          # a quieter first occurrence of the chorus material is the intro
        elif g == chorus_g and (n == 1 or e >= med - 0.05 or len(groups[g]) >= 2):
            lab = "chorus"
        elif i == 0 and (chorus_g != g):
            lab = "intro"
        elif i == n - 1 and n > 2:
            lab = "outro"
        elif nxt is not None and e < 0.5 * max(med, 0.2) + 0.1 and nxt >= e + 0.2 and i > 0:
            lab = "breakdown"
        elif nxt is not None and secs[i + 1]["repeat_group"] == chorus_g and nxt > e + 0.05 \
                and (s["end"] - s["start"]) <= 0.75 * (secs[i + 1]["end"] - secs[i + 1]["start"]) + 1e-6:
            lab = "pre"
        elif len(groups[g]) == 1 and i > 0 and any(secs[j]["label"] == "chorus" for j in range(i)) is False:
            lab = "verse"
        elif len(groups[g]) == 1 and i > 0:
            lab = "bridge"
        else:
            lab = "verse"
        s["label"] = lab
    # a chorus straight after a breakdown with a big energy jump is a drop
    for i in range(1, n):
        if secs[i]["label"] == "chorus" and secs[i - 1]["label"] == "breakdown" \
                and secs[i]["energy"] - secs[i - 1]["energy"] >= 0.3:
            secs[i]["label"] = "drop"
    if n >= 3 and secs[-1]["label"] == "chorus" and secs[-1]["energy"] < 0.6 * max(en):
        secs[-1]["label"] = "outro"
    return secs


def analyze_structure(y: np.ndarray, sr: int, song: dict, stems: dict | None = None) -> list:
    """Sections for a decoded song. `song` carries beat_grid/bar_phase/beat_times/duration/energy_curve."""
    dur = float(song["duration"])
    edges = _bar_edges(song)
    energy = np.asarray(song["energy_curve"], dtype=float)
    if len(edges) < 2 * MIN_SECTION_BARS:       # no usable bars: one section, honestly unlabelled
        return [{"start": 0.0, "end": round(dur, 3), "label": "verse",
                 "energy": round(float(energy.mean()) if len(energy) else 0.0, 3), "repeat_group": "A"}]
    F = bar_features(y, sr, edges, dur)
    nov = novelty(F)
    cuts = boundaries(nov)
    idx = [0] + cuts + [len(edges)]
    spans = list(zip(idx[:-1], idx[1:]))
    grp = _cluster(F, spans)
    bounds = np.append(edges, dur)
    secs = []
    for (a, b), g in zip(spans, grp):
        t0, t1 = (0.0 if a == 0 else float(bounds[a])), float(min(dur, bounds[b]))
        e = energy[int(t0 * curves.HZ):max(int(t0 * curves.HZ) + 1, int(t1 * curves.HZ))]
        secs.append({"start": round(t0, 3), "end": round(t1, 3), "label": "verse",
                     "energy": round(float(e.mean()) if len(e) else 0.0, 3), "repeat_group": _name(g)})
    secs[-1]["end"] = round(dur, 3)
    label_sections(secs)
    if stems:
        _stem_refine(secs, stems)
    return secs


def stems_activity(provider_wavs: dict) -> dict:
    """{'drums': rms@10Hz normalised, 'vocals': ...} from separated stem wavs (only the two we use)."""
    from . import stems as _st
    out = {}
    for n in ("drums", "vocals"):
        if n in provider_wavs:
            rms, _, _ = _st._mono_rms(provider_wavs[n])
            out[n] = _st._norm(rms)
    return out
