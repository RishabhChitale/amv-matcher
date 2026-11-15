"""P1: an audio drop sits on the beat where the kick lands and the bass re-enters, not on the hit one beat
before it (a fill / pickup) nor on the off-beat (B-own: detected one beat early; E-own: on the 'and')."""
import numpy as np

from amv_matcher import boom as B

FPS = 100.0
T = 0.5


def _low_band(drop_t, dur=20.0):
    """|STFT|-like (freq x frames): a low bin with soft kicks on every beat before drop_t, loud kick + bass after."""
    freqs = np.array([60.0, 1000.0])
    n = int(dur * FPS)
    lo = np.full(n, 1e-3)
    for b in np.arange(0.0, dur, T):
        i = int(round(b * FPS))
        lo[i:i + 5] += 0.05 if b < drop_t - 1e-6 else 1.0
    lo[int(drop_t * FPS):] += 0.3                      # the bass re-enters on the drop
    L = np.vstack([lo, np.full(n, 1e-3)])
    return L, freqs, np.arange(0.0, dur, T)


def _boom(t, **kw):
    return dict({"t": t, "conf": 1.0}, **kw)


def test_boom_one_beat_early_moves_to_the_downbeat_and_main_follows():
    L, freqs, beats = _low_band(10.5)
    out = [_boom(10.0)]
    main = B._downbeat_snap(out, 10.0, L, freqs, FPS, beats)
    assert out[0]["t"] == 10.5 and main == 10.5 and out[0]["downbeat_shift"] == 0.5


def test_boom_on_the_off_beat_moves_to_the_downbeat():
    L, freqs, beats = _low_band(10.5)
    out = [_boom(10.75)]
    B._downbeat_snap(out, None, L, freqs, FPS, beats)
    assert out[0]["t"] == 10.5


def test_boom_already_on_the_downbeat_and_lead_in_booms_stay():
    L, freqs, beats = _low_band(10.5)
    out = [_boom(10.5), _boom(9.3, lead_in=0.7)]
    main = B._downbeat_snap(out, 10.5, L, freqs, FPS, beats)
    assert [d["t"] for d in out] == [9.3, 10.5] and main == 10.5
    assert not any("downbeat_shift" in d for d in out)


def test_no_beats_leaves_booms_alone():
    L, freqs, _ = _low_band(10.5)
    out = [_boom(10.0)]
    assert B._downbeat_snap(out, 10.0, L, freqs, FPS, np.array([1.0])) == 10.0 and out[0]["t"] == 10.0
