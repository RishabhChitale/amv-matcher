from fractions import Fraction

import pytest

from amv_matcher import resolve_ramps as RR
from amv_matcher import speedramp as SR

FPS = 24
VD = 20.0
POINTS = [
    {"t_src": 0.0, "speed": 1.0, "ease_in_s": 0.0, "ease_out_s": 0.0},
    {"t_src": 6.0, "speed": 2.0, "ease_in_s": 0.5, "ease_out_s": 0.5},
    {"t_src": 10.0, "speed": 0.5, "ease_in_s": 0.4, "ease_out_s": 0.4},
    {"t_src": 14.0, "speed": 1.0, "ease_in_s": 0.4, "ease_out_s": 0.4},
]


def _fr(s):
    return Fraction(s[:-1])


@pytest.mark.parametrize("mode", ["smooth", "stepped"])
def test_valid_and_anchored(mode):
    tps = RR.build_timemap(POINTS, FPS, mode, vd=VD)
    assert RR.validate_timemap(tps) == []
    assert tps[0]["time"] == "0s" and tps[0]["value"] == "0s"
    ts = [t["_t"] for t in tps]
    assert all(b > a for a, b in zip(ts, ts[1:]))
    w, _ = SR.warp_from_points(POINTS, VD)
    assert abs(tps[-1]["_t"] / FPS - w[-1][1]) < 1.0 / FPS
    assert abs(tps[-1]["_v"] / FPS - VD) < 1.0 / FPS


def test_interp_rules():
    sm = RR.build_timemap(POINTS, FPS, "smooth", vd=VD)
    assert {t["interp"] for t in sm} == {"smooth2"}
    assert any("inTime" in t or "outTime" in t for t in sm)
    st = RR.build_timemap(POINTS, FPS, "stepped", vd=VD)
    assert {t["interp"] for t in st} == {"linear"}
    assert not any("inTime" in t or "outTime" in t for t in st)


def test_stepped_speed_recovered():
    clips = RR.stepped_clips(POINTS, FPS, vd=VD, step_frames=4)
    # plateau of speed 2 starts after 6 s source, so output rate there is ~2 src frames per out frame
    p = [c for c in clips if c["src_f"] / FPS > 7.0 and c["src_f"] / FPS < 8.5]
    assert p and all(abs(c["speed"] - 2.0) < 0.2 for c in p)
    assert all(c["dur_f"] > 0 for c in clips)
    assert sum(c["dur_f"] for c in clips) == RR.build_timemap(POINTS, FPS, "stepped", vd=VD)[-1]["_t"]


def test_modes_same_duration():
    a = RR.build_timemap(POINTS, FPS, "smooth", vd=VD)[-1]["_t"]
    b = RR.build_timemap(POINTS, FPS, "stepped", vd=VD)[-1]["_t"]
    assert a == b


def test_warp_input():
    w, _ = SR.warp_from_points(POINTS, VD)
    for mode in ("smooth", "stepped"):
        tps = RR.build_timemap(w, FPS, mode)
        assert RR.validate_timemap(tps) == []
        assert len(tps) > 3


def test_xml_roundtrip_and_validator_catches():
    xml = RR.timemap_xml(RR.build_timemap(POINTS, FPS, "smooth", vd=VD))
    assert xml.startswith("<timeMap") and RR.validate_timemap(xml) == []
    bad = xml.replace("smooth2", "bogus", 1)
    assert RR.validate_timemap(bad)
    assert RR.validate_timemap("<timeMap><timept time='1s' value='0s'/></timeMap>")
    assert RR.validate_timemap("<nope")
    lin = '<timeMap><timept time="0s" value="0s" interp="linear" inTime="1s"/></timeMap>'
    assert any("inTime" in e for e in RR.validate_timemap(lin))


def test_ntsc_fps_and_errors():
    tps = RR.build_timemap(POINTS, 23.976, "stepped", vd=VD)
    assert RR.validate_timemap(tps) == []
    assert _fr(tps[1]["time"]) > 0
    with pytest.raises(ValueError):
        RR.build_timemap(POINTS, FPS, "wobbly", vd=VD)
    with pytest.raises(ValueError):
        RR.build_timemap(POINTS, FPS, "smooth")
    with pytest.raises(ValueError):
        RR.build_timemap([], FPS)
