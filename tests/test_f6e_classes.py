"""F6e: audio boom classes (initial_boom / drop / encore / boom), main-drop choice, lead-in snap."""
from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from amv_matcher import audio as AU
from amv_matcher import boom as B

SR = AU.SR
CLASSES = {"initial_boom", "drop", "encore", "boom"}


def _song(path, sections, bpm=120, seconds=48.0, seed=0):
    """`sections` = [(start, end, click_gain, thump_gain)]; silence elsewhere (plus a little noise)."""
    rng = np.random.default_rng(seed)
    y = np.zeros(int(SR * seconds), dtype=np.float32)
    n, nt = int(0.03 * SR), int(0.25 * SR)
    t, tt = np.arange(n) / SR, np.arange(nt) / SR
    click = (np.sin(2 * np.pi * 1000 * t) * np.exp(-t * 150)).astype(np.float32)
    thump = (np.sin(2 * np.pi * 60 * tt) * np.exp(-tt * 8)).astype(np.float32)
    period = 60.0 / bpm
    for a, z, cg, tg in sections:
        b = a
        while b < z - 1e-6 and b < seconds - 0.3:
            i = int(b * SR)
            y[i:i + n] += cg * click
            if tg:
                y[i:i + nt] += tg * thump[: len(y) - i][:nt]
            b += period
    y += (0.002 * rng.standard_normal(len(y))).astype(np.float32)
    sf.write(str(path), np.clip(y, -1, 1), SR)


def _booms(path):
    Sx, freqs, env = AU._boom_inputs(str(path))
    return B.audio_booms(Sx, freqs, AU.SR, AU.HOP, env)


def _near(r, t, tol=0.2):
    return next((b for b in r["booms"] if abs(b["t"] - t) <= tol), None)


def test_breakdown_then_drop_is_main_and_beat_entry_is_initial(tmp_path):
    p = tmp_path / "s.wav"
    _song(p, [(0, 8, .08, 0), (8, 16, .5, .35),          # groove, then the beat comes in at 8 s
              (16, 24, .08, 0),                          # breakdown: no low end
              (24, 40, .9, .9)])                         # the drop
    r = _booms(p)
    assert r["booms"] and {b["class"] for b in r["booms"]} <= CLASSES
    d = _near(r, 24.0)
    assert d is not None and d["class"] == "drop"
    assert r["main"] == pytest.approx(24.0, abs=0.2)
    ib = _near(r, 8.0)
    assert ib is not None and ib["class"] == "initial_boom"
    assert sum(b["class"] == "initial_boom" for b in r["booms"]) == 1
    for b in r["booms"]:
        assert b["build"] is None and {"low_before", "low_after", "break_db"} <= set(b)
    assert r == _booms(p)                                  # deterministic


def test_at_most_one_encore_and_only_after_main(tmp_path):
    p = tmp_path / "s.wav"
    _song(p, [(0, 8, .08, 0), (8, 18, .9, .9),           # drop (no build-up needed)
              (19.5, 27.5, .9, .9), (29, 37, .9, .9), (38.5, 46, .9, .9)])   # gapped re-entries
    r = _booms(p)
    enc = [b for b in r["booms"] if b["class"] == "encore"]
    assert len(enc) <= 1
    assert all(b["t"] > r["main"] for b in enc)


def test_no_drop_falls_back_to_strongest():
    out = [{"t": 1.0, "conf": .5, "jump_db": 10.0, "_rank": .5},
           {"t": 5.0, "conf": .9, "jump_db": 20.0, "_rank": .9}]
    fps = SR / AU.HOP
    m = int(10 * fps)
    freqs = np.linspace(0, SR / 2, 1025)
    S = np.full((1025, m), 1e-3)
    main = B._classify(out, S, freqs, fps, S, np.ones(m), np.array([]))
    assert main == 5.0
    assert not any(d["class"] == "drop" for d in out)


def test_boom_version_gates_cached_analyses():
    assert "boom_version" in AU.BOOM_FIELDS
