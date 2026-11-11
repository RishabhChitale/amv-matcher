"""F20: the beat grid sits on the kicks, not on the off-beat hats.

Full-band beat tracking (librosa beat_track and fit_grid's comb) locks onto loud open hats on the "and" in house
music, so every beat lands half a beat away from the kick. audio.half_beat_fix moves the grid and the tracker beats
by half a period when the kick band clearly sits on the "and" (both checks agree). One rule for every song.
"""
import numpy as np
import pytest
import soundfile as sf

from amv_matcher import audio

SR = 22050
FPS = audio.SR / audio.HOP


def _house(path, bpm=125.0, dur=24.0, kick_on_beat=True, kicks=True, hat_gain=0.9):
    """Kick (55 Hz thump) on every beat, open hat (noise burst, louder) on every off-beat."""
    rng = np.random.default_rng(0)
    y = np.zeros(int(SR * dur), dtype=np.float32)
    T = 60.0 / bpm
    nk = int(0.12 * SR)
    tk = np.arange(nk) / SR
    kick = (np.sin(2 * np.pi * 55 * tk) * np.exp(-tk / 0.04)).astype(np.float32)
    nh = int(0.08 * SR)
    hat = (rng.standard_normal(nh) * np.exp(-np.arange(nh) / SR / 0.03)).astype(np.float32)
    hat = np.diff(hat, prepend=0).astype(np.float32)          # brighten
    kick_t = np.arange(0.5, dur - 0.3, T) + (0.0 if kick_on_beat else T / 2)
    hat_t = kick_t + T / 2
    for t in kick_t if kicks else []:
        i = int(t * SR)
        y[i:i + nk] += 0.6 * kick[:len(y) - i]
    for t in hat_t:
        i = int(t * SR)
        if i < len(y):
            y[i:i + nh] += hat_gain * hat[:len(y) - i]
    sf.write(str(path), y / max(1e-6, np.abs(y).max()) * 0.8, SR)
    return kick_t, T


def _phase(times, ref, T):
    """Mean circular phase (0..1 of a beat) of `times` relative to the reference grid `ref`."""
    ph = ((np.asarray(times)[:, None] - np.asarray(ref)[None, :]) / T) % 1.0
    ph = ph[:, 0]
    a = np.angle(np.mean(np.exp(2j * np.pi * ph))) / (2 * np.pi)
    return a % 1.0


def _circ_dist(a, b=0.0):
    d = abs((a - b) % 1.0)
    return min(d, 1 - d)


def test_kick_env_peaks_on_kicks():
    y = np.zeros(SR * 4, dtype=np.float32)
    t = np.arange(int(0.12 * SR)) / SR
    k = np.sin(2 * np.pi * 55 * t) * np.exp(-t / 0.04)
    for s in (1.0, 2.0, 3.0):
        y[int(s * SR):int(s * SR) + len(k)] += k
    import librosa
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=audio.HOP))
    e = audio.kick_env(S, librosa.fft_frequencies(sr=SR, n_fft=2048))
    pk = np.argsort(e)[-3:] / FPS
    assert np.all(np.min(np.abs(pk[:, None] - np.array([1.0, 2.0, 3.0])[None, :]), axis=1) < 0.05)


def _grid(t0, per, dur):
    return {"beat_grid": list(np.arange(t0, dur, per)), "grid_t0": t0, "grid_period": per,
            "grid_fit_ms": 5.0, "grid_used": True, "grid_drift_ms": 0.0}


def _kenv(kick_t, dur):
    e = np.zeros(int(dur * FPS) + 1)
    e[np.round(np.asarray(kick_t) * FPS).astype(int)] = 1.0
    return e


@pytest.mark.parametrize("on_beat", [True, False])
def test_half_beat_fix_moves_only_off_kick_grids(on_beat):
    dur, T = 30.0, 0.48
    beats = np.arange(0.5, dur - 0.3, T)                           # tracker beats on the grid
    kick_t = beats if on_beat else beats + T / 2
    g, bt, fg, fb = audio.half_beat_fix(_grid(0.5, T, dur), beats, _kenv(kick_t, dur), FPS)
    assert fg == fb == (not on_beat)
    assert _circ_dist(_phase(kick_t, g["beat_grid"], T)) < 0.05
    assert _circ_dist(_phase(kick_t, bt, T)) < 0.05


def test_half_beat_fix_needs_both_checks():
    """A kick that convinces the grid comb but not the tracker beats (or vice versa) moves nothing."""
    dur, T = 30.0, 0.48
    beats = np.arange(0.5, dur - 0.3, T)
    kick_t = beats + T / 2
    shifted = beats + T / 2                                        # tracker beats already on the kicks
    g0 = _grid(0.5, T, dur)
    g, bt, fg, fb = audio.half_beat_fix(g0, shifted, _kenv(kick_t, dur), FPS)
    assert not fg and not fb and g is g0 and np.array_equal(bt, shifted)


def test_half_beat_fix_no_kick_no_change():
    dur, T = 30.0, 0.48
    beats = np.arange(0.5, dur - 0.3, T)
    g0 = _grid(0.5, T, dur)
    g, bt, fg, fb = audio.half_beat_fix(g0, beats, np.zeros(int(dur * FPS)), FPS)
    assert not fg and not fb and g is g0


@pytest.mark.parametrize("kick_on_beat", [True, False])
def test_analysis_grid_lands_on_kicks(tmp_path, kick_on_beat):
    """End to end through the song analysis: with loud off-beat hats, the grid and the beats sit on the kicks."""
    p = tmp_path / "house.wav"
    kick_t, T = _house(p, kick_on_beat=kick_on_beat)
    r = audio._compute(str(p))[0]
    assert "half_beat_fix" in r
    ref = r["beat_grid"] if r.get("beat_grid") else r["beat_times"]
    assert _circ_dist(_phase(kick_t, ref, T)) < 0.12, r["half_beat_fix"]
    assert _circ_dist(_phase(kick_t, r["beat_times"], T)) < 0.12, r["half_beat_fix"]


def test_analysis_no_kick_unchanged(tmp_path):
    p = tmp_path / "hats.wav"
    _house(p, kicks=False)
    r = audio._compute(str(p))[0]
    assert r["half_beat_fix"] == [False, False]
