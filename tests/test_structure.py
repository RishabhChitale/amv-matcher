"""Song structure: synthetic A B A B song, video struct segments, API exposure, gated real check."""
from __future__ import annotations

import os

import numpy as np
import pytest
import soundfile as sf

from amv_matcher import curves
from amv_matcher.audio import SR, analyze_song
from amv_matcher.structure import analyze_structure

BPM = 120
BAR = 4 * 60.0 / BPM


def _tone(freqs, n, amp):
    t = np.arange(n) / SR
    return amp * sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs)


def make_abab(path, bars=8, seed=0):
    """A: soft C-major pad + kick on the beat; B: loud F#-minor saw-ish pad + kick + off-beat hats."""
    rng = np.random.default_rng(seed)
    n_bar = int(BAR * SR)
    kick_n = int(0.08 * SR)
    kt = np.arange(kick_n) / SR
    kick = (np.sin(2 * np.pi * 60 * kt) * np.exp(-kt * 35)).astype(np.float32)
    hat_n = int(0.03 * SR)
    hat = (rng.standard_normal(hat_n) * np.exp(-np.arange(hat_n) / SR * 200)).astype(np.float32)
    out = []
    for sec in "ABAB":
        for _ in range(bars):
            if sec == "A":
                bar = _tone([261.6, 329.6, 392.0], n_bar, 0.12)
                kamp, hats = 0.5, False
            else:
                bar = _tone([185.0, 220.0, 277.2, 554.4, 1108.8], n_bar, 0.35)
                kamp, hats = 0.9, True
            bar = bar.astype(np.float32)
            for b in range(4):
                i = int(b * n_bar / 4)
                bar[i:i + kick_n] += kamp * kick[: n_bar - i][:kick_n]
                if hats:
                    j = int((b + 0.5) * n_bar / 4)
                    bar[j:j + hat_n] += 0.25 * hat[: n_bar - j][:hat_n]
            out.append(bar)
    y = np.concatenate(out)
    sf.write(str(path), y, SR)
    return y


def test_abab_structure(tmp_path):
    p = tmp_path / "abab.wav"
    make_abab(p)
    song = analyze_song(str(p), cache_path=tmp_path / "c.json")
    secs = song["sections"]
    assert len(secs) == 4, secs
    assert [s["repeat_group"] for s in secs] == ["A", "B", "A", "B"]
    want = [0, 8 * BAR, 16 * BAR, 24 * BAR]
    for s, w in zip(secs, want):
        assert abs(s["start"] - w) <= BAR / 2, (s, w)
    assert secs[0]["start"] == 0.0 and abs(secs[-1]["end"] - song["duration"]) < 0.01
    for a, b in zip(secs[:-1], secs[1:]):
        assert a["end"] == b["start"]
    assert secs[1]["energy"] > secs[0]["energy"]
    assert all(s["label"] in ("intro", "verse", "pre", "chorus", "bridge", "breakdown", "drop", "outro") for s in secs)
    assert secs[1]["label"] in ("chorus", "drop") and secs[3]["label"] in ("chorus", "drop")   # louder repeated group


def test_stems_refine_breakdown():
    from amv_matcher.structure import label_sections, _stem_refine
    secs = [{"start": 0, "end": 8, "energy": 0.6, "repeat_group": "A", "label": ""},
            {"start": 8, "end": 16, "energy": 0.15, "repeat_group": "B", "label": ""},
            {"start": 16, "end": 24, "energy": 0.9, "repeat_group": "A", "label": ""},
            {"start": 24, "end": 32, "energy": 0.9, "repeat_group": "A", "label": ""}]
    label_sections(secs)
    hz = int(curves.HZ)
    drums = np.concatenate([np.ones(8 * hz), np.zeros(8 * hz), np.ones(16 * hz)])
    _stem_refine(secs, {"drums": drums})
    assert secs[1]["label"] == "breakdown" and secs[1]["drums"] < 0.15


def test_video_segments_struct():
    from amv_matcher.video import struct_segments
    segs = [{"start": 0.0, "end": 1.2, "label": "calm"}, {"start": 1.2, "end": 1.8, "label": "calm"},
            {"start": 3.0, "end": 3.4, "label": "build"}, {"start": 3.4, "end": 4.0, "label": "build"},
            {"start": 4.0, "end": 9.0, "label": "peak"}]
    e = np.linspace(0, 1, 100)
    out = struct_segments(segs, 10.0, e)
    assert all(s["kind"] in ("calm", "build", "peak") and s["end"] - s["start"] >= 2.0 - 1e-6 for s in out)
    assert out[0]["start"] == 0.0 and out[-1]["end"] == 10.0
    assert [a["end"] for a in out[:-1]] == [b["start"] for b in out[1:]]
    assert "peak" in [s["kind"] for s in out]


_REAL = [p for p in [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".uploads", "song")]
         if os.path.isdir(p)]


@pytest.mark.skipif(os.environ.get("AMV_REAL") != "1", reason="AMV_REAL=1 only")
def test_real_songS1_sections():
    import glob
    f = glob.glob(os.path.join(_REAL[0], "*songS1*.mp3")) if _REAL else []
    if not f:
        pytest.skip("songS1 mp3 missing")
    song = analyze_song(f[0])
    print("\nbpm", song["bpm"], "bar_phase", song["bar_phase"], "dur", song["duration"])
    for s in song["sections"]:
        print(f'{s["start"]:7.2f}-{s["end"]:7.2f} {s["label"]:9s} grp {s["repeat_group"]} energy {s["energy"]}')
    assert len(song["sections"]) >= 3


def test_split_long_and_outro_labels():
    from amv_matcher.structure import label_sections, split_long
    nov = np.zeros(40)
    nov[8] = 1.0
    nov[24] = 0.9      # internal peak inside the 32-bar tail
    assert split_long(nov, [8], 40) == [8, 24]
    nov[24] = 0.1      # too weak (< 40% of reference)
    assert split_long(nov, [8], 40) == [8]
    secs = [{"start": 0, "end": 8, "energy": 0.7, "repeat_group": "A", "label": ""},
            {"start": 8, "end": 16, "energy": 0.5, "repeat_group": "B", "label": ""},
            {"start": 16, "end": 24, "energy": 0.8, "repeat_group": "A", "label": ""},
            {"start": 24, "end": 32, "energy": 0.5, "repeat_group": "C", "label": ""}]
    label_sections(secs)
    assert secs[0]["label"] == "intro (hook)" and secs[-1]["label"] == "outro"
