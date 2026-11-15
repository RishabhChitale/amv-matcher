"""F24: own audio lines up with its own picture.

Two generic causes of a perfect own pair scoring ~4% sync:
1. the tracker beats sit on the off-beat ("and") for part of the song while the comb grid is on the kicks, so the
   grid is rejected (grid_fit_ms ~ half a beat) and everything falls back to the off-beat beats
   (audio.half_beat_fix now moves those beats back onto the kick grid);
2. a file whose audio stream starts later than its video: the decoded wav must start at the video's first frame
   (media.extract_audio pads the lead), else every own-audio time is early by that lag.
"""
from fractions import Fraction

import av
import numpy as np
import soundfile as sf

from amv_matcher import audio, media

FPS = audio.SR / audio.HOP


def _grid(t0, per, dur, fit_ms=5.0, used=True):
    return {"beat_grid": list(np.arange(t0, dur, per)), "grid_t0": t0, "grid_period": per,
            "grid_fit_ms": fit_ms, "grid_used": used, "grid_drift_ms": 0.0}


def _kenv(kick_t, dur):
    e = np.zeros(int(dur * FPS) + 1)
    e[np.round(np.asarray(kick_t) * FPS).astype(int)] = 1.0
    return e


def _phase_err(times, t0, T):
    u = ((np.asarray(times) - t0) / T) % 1.0
    return np.minimum(u, 1 - u)


def test_tracker_on_the_and_for_part_of_the_song_snaps_to_the_kick_grid():
    dur, T, t0 = 60.0, 0.5, 0.09
    kick_t = np.arange(t0, dur - 0.3, T)
    rng = np.random.default_rng(1)
    bt = kick_t + rng.normal(0, 0.01, len(kick_t))
    bt[len(bt) // 3:] += T / 2                       # tracker drifts onto the "and" for 2/3 of the song
    bt = bt[bt < dur - 0.1]
    g0 = _grid(t0, T, dur, fit_ms=330.0, used=False)
    g, nb, fg, fb = audio.half_beat_fix(g0, bt, _kenv(kick_t, dur), FPS)
    assert fb and not fg
    assert g["beat_grid"] == g0["beat_grid"]          # the grid (already on the kicks) stays
    assert np.percentile(_phase_err(nb, t0, T), 90) < 0.1
    assert g["grid_used"] and g["grid_fit_ms"] <= audio.GRID_MAX_FIT_MS


def test_tracker_and_grid_on_the_kick_are_untouched():
    dur, T, t0 = 30.0, 0.48, 0.5
    kick_t = np.arange(t0, dur - 0.3, T)
    g0 = _grid(t0, T, dur)
    g, nb, fg, fb = audio.half_beat_fix(g0, kick_t.copy(), _kenv(kick_t, dur), FPS)
    assert not fg and not fb and g is g0 and np.array_equal(nb, kick_t)


def _late_audio_file(path, lag_s, click_s=0.3, dur=3.0, rate=44100):
    """mkv: video from t=0, PCM audio whose stream starts lag_s later, one click click_s into the audio."""
    with av.open(str(path), "w") as c:
        vs = c.add_stream("mpeg4", rate=10)
        vs.width, vs.height, vs.pix_fmt = 64, 48, "yuv420p"
        a = c.add_stream("pcm_s16le", rate=rate)
        a.layout = "mono"
        a.time_base = Fraction(1, rate)
        y = np.zeros(int(rate * (dur - lag_s)), np.int16)
        i = int(click_s * rate)
        y[i:i + 200] = 20000
        for k in range(int(dur * 10)):
            fr = av.VideoFrame.from_ndarray(np.full((48, 64, 3), 128, np.uint8), format="rgb24")
            fr.pts = k
            for p in vs.encode(fr):
                c.mux(p)
        for p in vs.encode(None):
            c.mux(p)
        n0 = int(round(lag_s * rate))
        for s in range(0, len(y), 1024):
            fr = av.AudioFrame.from_ndarray(y[None, s:s + 1024], format="s16", layout="mono")
            fr.sample_rate, fr.pts, fr.time_base = rate, n0 + s, Fraction(1, rate)
            for p in a.encode(fr):
                c.mux(p)
        for p in a.encode(None):
            c.mux(p)


def _click_time(wav):
    y, sr = sf.read(str(wav))
    y = np.abs(y if y.ndim == 1 else y.mean(axis=1))
    return np.argmax(y > 0.3) / sr


def test_extract_audio_puts_a_late_audio_stream_at_its_video_time(tmp_path):
    src = tmp_path / "late.mkv"
    _late_audio_file(src, lag_s=0.5)
    wav = media.extract_audio(src, tmp_path / "x")
    assert abs(_click_time(wav) - 0.8) < 0.01       # 0.5 s lag + 0.3 s into the audio


def test_extract_audio_without_lag_is_unchanged(tmp_path):
    src = tmp_path / "nolag.mkv"
    _late_audio_file(src, lag_s=0.0)
    wav = media.extract_audio(src, tmp_path / "x")
    assert abs(_click_time(wav) - 0.3) < 0.01


def test_align_to_video_pads_and_trims():
    d = np.arange(10, dtype=np.int16).reshape(5, 2)
    assert media._align_to_video(d, 0.0) is d
    p = media._align_to_video(d, 2 / media.SR)
    assert p.shape == (7, 2) and not p[:2].any() and np.array_equal(p[2:], d)
    t = media._align_to_video(d, -1 / media.SR)
    assert np.array_equal(t, d[1:])
