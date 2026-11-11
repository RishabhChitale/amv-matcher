"""U11b: song tempo RAMP (time-varying playback ratio within +-10%) - same eased speed points as speedramp.

ratio r(tau) = song playback speed at song time tau (r > 1: faster, beats closer). Warp = integral of 1/r,
mapping song time -> output time. New module, no edits to tempo.py: `plan_tempo_ramp` returns a plan that
`apply_tempo_warp` turns into the song dict as heard, and `stretch_warped` renders the audio (time-varying
phase vocoder, pitch preserved) so the beats really land on the mapped times."""
from __future__ import annotations

import numpy as np

from . import retime as R
from . import score as S
from . import speedramp as SR

DEV = 0.10                # tempo ramp stays within +-10% (same bound as the global tempo stretch)
MAX_ACCEL = 0.03          # ratio change per output second (a 10% swing takes >= ~3 s)
EASE_MAX_S = 8.0
N_FFT, HOP = 2048, 512


def ratio_points(speeds, bounds, dur, ease="smoothstep", max_accel=MAX_ACCEL, ease_max_s=EASE_MAX_S):
    return SR.points_from(speeds, bounds, dur, max_accel, ease_max_s, ease)


def tempo_warp(points, dur, ease="smoothstep"):
    """(warp [[tau, out_t]], ratio-on-grid) for eased ratio points."""
    return SR.warp_from_points(points, dur, ease, hz=50.0)


def _map(w, x):
    return R._wmap(w, x)


def apply_tempo_warp(song: dict, warp) -> dict:
    """Song dict as heard after the tempo ramp: every time (beats, grid, sections, drops, lulls) goes through
    the warp; energy curve resampled; bpm = beats per output minute over the whole song."""
    w = np.asarray(warp, dtype=float)
    od = float(w[-1, 1])
    out = dict(song)
    out["duration"] = od
    bt = np.asarray(song["beat_times"], dtype=float)
    out["beat_times"] = _map(w, bt).tolist()
    if len(bt) >= 2:
        out["bpm"] = float((len(bt) - 1) * 60.0 / max(1e-6, out["beat_times"][-1] - out["beat_times"][0]))
    if song.get("beat_grid") is not None:
        g = _map(w, np.asarray(song["beat_grid"], dtype=float))
        out["beat_grid"] = g.tolist()
        if len(g) >= 2:
            per = float(np.median(np.diff(g)))
            out["grid_period"], out["grid_bpm"] = per, 60.0 / per
        if "grid_t0" in song:
            out["grid_t0"] = float(_map(w, song["grid_t0"]))
    if song.get("sections"):
        out["sections"] = [dict(x, start=float(_map(w, x["start"])), end=float(_map(w, x["end"])))
                           for x in song["sections"]]
    out["drops"] = [float(_map(w, d)) for d in song.get("drops", [])]
    out["lulls"] = [[float(_map(w, a)), float(_map(w, b))] for a, b in song.get("lulls", [])]
    se = np.asarray(song.get("energy_curve", []), dtype=float)
    if len(se):
        hz = S.ENERGY_HZ
        n = max(1, int(round(od * hz)))
        src = R._winv(w, np.arange(n) / hz)
        out["energy_curve"] = np.interp(src, np.arange(len(se)) / hz, se).tolist()
    from .boom import map_song_booms
    out.update(map_song_booms(song, lambda t: float(_map(w, t))))
    return out


def _objective(song, video, offset, lead_s):
    def ev(warp):
        sw = apply_tempo_warp(song, warp)
        ev_t, ev_w = S._events(video)
        has = len(ev_t) and len(sw["beat_times"]) >= 2
        sync = S._sync_pct_at(sw, ev_t, ev_w, float(video["duration"]), offset, lead_s) if has else 0.0
        tempo_pct, _r = S._tempo(sw, video)
        return S._combine(tempo_pct, sync, S._arc_pct_at(sw, video, offset))
    return ev


def plan_tempo_ramp(song: dict, video: dict, offset: float, max_dev: float = DEV, ease: str = "smoothstep",
                    max_accel: float = MAX_ACCEL, ease_max_s: float = EASE_MAX_S, lead_s: float = None,
                    base_ratio: float = 1.0, should_cancel=None) -> dict:
    """Optimize eased ratio plateaus over the part of the song the video uses ([0, offset+vd/(1-dev)]).
    Starts from the constant `base_ratio` (e.g. tempo.plan_tempo's ratio) and keeps it if no ramp beats it.
    Returns {points [{t_src, speed, ease_in_s, ease_out_s}], curve, warp, ratio_range, total_*, sync_*}."""
    lead = S._lead(lead_s)
    sdur = float(song["duration"])
    vd = float(video["duration"])
    win = float(min(sdur, max(offset, 0.0) + vd / max(1e-6, 1.0 - max_dev) + 2.0))
    base_ratio = float(np.clip(base_ratio, 1.0 - max_dev, 1.0 + max_dev))

    def full_warp(sp, bd):
        w = SR.warp_from_points(SR.points_from(sp, bd, win, max_accel, ease_max_s, ease), win, ease, 50.0)[0]
        if win < sdur - 1e-6:                         # beyond the window the last ratio just continues
            tail = sdur - win
            w = np.vstack([w, [sdur, w[-1, 1] + tail / sp[-1]]])
        return w

    evaluate = _objective(song, video, offset, lead)

    def ext(w):                                       # window warp -> whole-song warp (last ratio continues)
        if win >= sdur - 1e-6:
            return w
        return np.vstack([w, [sdur, w[-1, 1] + (sdur - win) / sp_last(w)]])

    cands = [([base_ratio], [])]                      # (a) the constant tempo
    ev_t, _w = S._events(video)                       # (b) tracked beat->event anchors, LSQ plateau fit
    bt = np.asarray(S._beats(song), dtype=float)
    bt = bt[bt < win]
    ev_o = np.sort(np.asarray(ev_t, dtype=float)) + float(offset) + lead     # video event, on the song's output clock
    lo, hi = 1.0 - max_dev, 1.0 + max_dev
    if len(ev_o) >= 3 and len(bt) >= 3:
        anchors = SR.track_anchors(bt, ev_o, lo, hi, starts=8, free_start=True)  # (tau_beat, out_event): implied speed = d tau / d out = ratio
        if len(anchors) >= max(3, 0.3 * len(ev_o)):
            per = float(np.median(np.diff(ev_o)))
            cands.append(SR.fit_plateaus(anchors, win, max_dev, knot_s=4.0, max_accel=max_accel,
                                         ease_max_s=ease_max_s, ease=ease, beat_period=per,
                                         should_cancel=should_cancel))
    steps = [(0.02, 0.5), (0.01, 0.25), (0.005, 0.1)]
    best = None
    for sp0, bd0 in cands:
        sp, bd, j, _j0 = SR.optimize(win, sp0, bd0, lambda w: evaluate(ext(w)), dev=max_dev, max_accel=max_accel,
                                     ease_max_s=ease_max_s, ease=ease, split=True, split_len=4.0, steps=steps,
                                     should_cancel=should_cancel)
        if best is None or j > best[0]:
            best = (j, sp, bd)
    _j, sp, bd = best
    sp, bd = SR._merge_flat(sp, bd)
    pts = SR.points_from(sp, bd, win, max_accel, ease_max_s, ease)
    full = full_warp(sp, bd)
    const = np.array([[0.0, 0.0], [sdur, sdur / base_ratio]])
    after, base = evaluate(full), evaluate(const)
    ratios = np.diff(full[:, 0]) / np.maximum(np.diff(full[:, 1]), 1e-9)
    idx = np.unique(np.append(np.arange(0, len(full), 5), len(full) - 1))
    return {"points": [{"t_src": round(p["t_src"], 3), "speed": round(p["speed"], 4),
                        "ease_in_s": round(p["ease_in_s"], 3), "ease_out_s": round(p["ease_out_s"], 3)}
                       for p in pts],
            "warp": [[round(float(full[i, 0]), 3), round(float(full[i, 1]), 4)] for i in idx],
            "out_duration": round(float(full[-1, 1]), 3), "ease": ease, "max_dev": max_dev,
            "ratio_range": [round(float(ratios.min()), 4), round(float(ratios.max()), 4)],
            "j_const": round(float(base), 2), "j_ramp": round(float(after), 2),
            "smooth": SR.stats(full), "base_ratio": base_ratio}


def sp_last(w):
    """Slope (ratio) of the final warp segment."""
    return float((w[-1, 0] - w[-2, 0]) / max(1e-9, w[-1, 1] - w[-2, 1]))


def stretch_warped(y: np.ndarray, sr: int, warp) -> np.ndarray:
    """Time-varying phase vocoder: mono float signal `y` rendered so source time tau plays at output time
    warp(tau). Pitch is preserved; the phase advance follows the local ratio frame by frame."""
    import librosa
    w = np.asarray(warp, dtype=float)
    y = np.asarray(y, dtype=np.float32)
    D = librosa.stft(y, n_fft=N_FFT, hop_length=HOP)
    n_out = int(np.ceil(w[-1, 1] * sr / HOP)) + 1
    src_t = R._winv(w, np.arange(n_out) * HOP / sr)                 # source time at each output frame
    steps = np.clip(src_t * sr / HOP, 0, D.shape[1] - 1.0001)
    omega = 2 * np.pi * HOP * np.arange(D.shape[0]) / N_FFT         # expected phase advance per hop
    phase = np.angle(D[:, 0])
    pad = np.pad(D, [(0, 0), (0, 2)])
    out = np.empty((D.shape[0], n_out), dtype=np.complex64)
    for k, st in enumerate(steps):
        i = int(np.floor(st))
        frac = st - i
        cols = pad[:, i:i + 2]
        mag = (1 - frac) * np.abs(cols[:, 0]) + frac * np.abs(cols[:, 1])
        out[:, k] = mag * np.exp(1j * phase)
        dphi = np.angle(cols[:, 1]) - np.angle(cols[:, 0]) - omega
        dphi -= 2 * np.pi * np.round(dphi / (2 * np.pi))
        phase = phase + omega + dphi
    n = int(round(w[-1, 1] * sr))
    return librosa.istft(out, hop_length=HOP, length=n).astype(np.float32)
