"""U11c: Resolve export of speed ramps (single timeMap clip) and tempo ramps (baked wav)."""
from __future__ import annotations

import hashlib
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from amv_matcher import resolve as R
from amv_matcher import resolve_ramps as RR
from amv_matcher import speedramp as SR

FPS = 30.0
VD = 30.0
SONG = {"beat_times": [i * 0.5 for i in range(80)], "drops": [10.0, 20.0], "duration": 40.0}
VIDEO = {"duration": VD, "fps": FPS, "cut_times": [3.0, 9.0, 15.0], "motion_peaks": []}
POINTS = [
    {"t_src": 0.0, "speed": 1.0, "ease_in_s": 0.0, "ease_out_s": 0.0},
    {"t_src": 8.0, "speed": 1.2, "ease_in_s": 0.5, "ease_out_s": 0.5},
    {"t_src": 16.0, "speed": 0.85, "ease_in_s": 0.5, "ease_out_s": 0.5},
    {"t_src": 24.0, "speed": 1.0, "ease_in_s": 0.5, "ease_out_s": 0.5},
]


def _ramp():
    w, _ = SR.warp_from_points(POINTS, VD)
    return {"warp": [[float(a), float(b)] for a, b in w], "points": POINTS, "out_duration": float(w[-1][1])}


def _xml(plan):
    xml = R.build_fcpxml(Path("/x/vid.mp4"), VD, FPS, 1920, 1080, plan, SONG, VIDEO, "audio/song.wav", 40.0)
    # The exporter correctly resolves the media path to an absolute file URI, which gains a drive
    # letter on Windows (file:///C:/x/vid.mp4). Normalise only that URI so the golden hashes below
    # stay byte-exact and identical across OSes.
    return xml.replace(Path("/x/vid.mp4").resolve().as_uri(), "file:///x/vid.mp4")


@pytest.mark.parametrize("key", ["retime", "retime_ramps"])
@pytest.mark.parametrize("mode", ["stepped", "smooth"])
def test_ramp_single_clip_valid_timemap(key, mode):
    plan = {key: _ramp(), "offset": 0.0}
    if mode == "smooth":
        plan[key]["resolve_mode"] = "smooth"
    root = ET.fromstring(_xml(plan))
    vids = root.find(".//spine").findall("asset-clip")
    assert len(vids) == 1 and vids[0].get("start") == "0s"
    tm = vids[0].find("timeMap")
    assert tm is not None
    assert RR.validate_timemap(ET.tostring(tm, encoding="unicode")) == []   # DTD checker
    pts = [(R_f(p.get("time")), R_f(p.get("value"))) for p in tm.findall("timept")]
    assert all(b[0] > a[0] for a, b in zip(pts, pts[1:]))      # strictly increasing
    assert all(b[1] >= a[1] for a, b in zip(pts, pts[1:]))
    assert abs(pts[-1][1] - VD) < 1.0 / FPS
    assert abs(R_f(vids[0].get("duration")) - _ramp()["out_duration"]) < 1.0 / FPS
    assert root.findall(".//marker") and root.findall(".//asset-clip[@lane]")


def R_f(s):
    from fractions import Fraction
    return Fraction(s[:-1]) if s != "0s" else Fraction(0)


def test_step_plan_unchanged_and_not_ramp():
    step = {"retime": {"warp": [[0, 0], [10, 10], [20, 18], [30, 28]]}, "offset": 0.0}
    plan, rt = R.ramp_plan(step)
    assert rt is None and plan is step
    h = hashlib.sha256(_xml(step).encode()).hexdigest()
    assert h == "38a3898a286d9ec93b65a274a132ad29a441f52ae19535d802f5d165e5e102b2"   # output of the pre-U11c code
    assert hashlib.sha256(_xml({"offset": 1.0}).encode()).hexdigest() == \
        "778b38a865ede68cb8796be1d07b61283e6850e6073a75341985a1feec0760b5"


def test_ramps_opt_in_prefers_retime_ramps():
    plan = {"retime": {"warp": [[0, 0], [30, 28]]}, "retime_ramps": _ramp()}
    _, rt = R.ramp_plan(plan)
    assert rt is plan["retime_ramps"]


def test_tempo_ramp_warp_key():
    assert R.tempo_ramp_warp({}) is None
    assert R.tempo_ramp_warp({"tempo_ramp": {"warp": [[0, 0], [10, 9.5]]}}) == [[0, 0], [10, 9.5]]


def test_both_anchor_conventions_same_when_start_zero(monkeypatch):
    a = _xml({"retime": _ramp()})
    monkeypatch.setattr(R, "RAMP_ANCHOR", "relative-values")
    assert _xml({"retime": _ramp()}) == a
