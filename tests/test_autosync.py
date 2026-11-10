"""auto_sync tests (synthetic)."""
import time

import numpy as np

from amv_matcher.autosync import auto_sync
from tests.test_retime import make_song, make_video, slow_section_cuts


def test_slow_drift_fixed():
    cuts = np.arange(1, 56) * 0.52                      # 4% slow vs 120 BPM
    song, vid = make_song(90.0), make_video(cuts, duration=30.0, tempo=115.4)
    r = auto_sync(song, vid)
    assert r["final"]["sync_pct"] >= 95
    assert any(s["tool"] in ("tempo", "retime") and s["kept"] for s in r["steps"])


def test_slow_section_retimed():
    r = auto_sync(make_song(), make_video(slow_section_cuts()))
    assert r["final"]["sync_pct"] >= 95
    rt = r["plan"]["retime"]
    assert rt and any(abs(s["speed"] - 1.25) < 0.1 for s in rt["sections"])


def test_kept_steps_monotonic():
    r = auto_sync(make_song(90.0), make_video(np.arange(1, 56) * 0.52, tempo=115.4))
    kept = [s for s in r["steps"] if s["kept"]]
    scores = [s["score_after"] for s in kept]
    assert scores == sorted(scores)
    for s in kept:
        assert s["score_after"] >= s["score_before"]
    assert set(r) >= {"steps", "final", "plan", "limits_hit", "why_not_100"}


def test_runtime_three_minute_pair():
    song = make_song(240.0)
    vid = make_video(np.arange(1, 340) * 0.52, duration=180.0, tempo=115.4)
    t = time.perf_counter()
    r = auto_sync(song, vid)
    assert time.perf_counter() - t < 10.0
    assert r["final"]["total_pct"] >= r["steps"][0]["score_before"] - 1


def test_honest_when_unreachable():
    vid = make_video(np.arange(0.5, 28, 0.5), tempo=120.0)
    vid["energy_curve"] = np.linspace(0, 1, len(vid["energy_curve"])).tolist()   # song energy flat: arc stuck
    r = auto_sync(make_song(), vid)
    assert r["final"]["total_pct"] < 100 and "unreachable" in r["why_not_100"]


# ---- real media (numeric only, nothing committed; run after score/curves calibration) ----
import glob
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SONG = glob.glob(os.path.join(_ROOT, ".uploads", "song", "*songS1*.mp3"))
_VIDEO = glob.glob(os.path.join(_ROOT, ".uploads", "video", "*videoA*.mp4"))


@pytest.mark.slow
@pytest.mark.skipif(not (_SONG and _VIDEO), reason="real songS1/videoA files missing")
@pytest.mark.skipif(not os.environ.get("AMV_REAL"), reason="set AMV_REAL=1 (run after score/curves calibration)")
def test_real_songS1_videoA():
    from amv_matcher.audio import analyze_song
    from amv_matcher.video import analyze_video
    song, video = analyze_song(_SONG[0]), analyze_video(_VIDEO[0])
    t0 = time.time()
    r = auto_sync(song, video)
    print("\nelapsed", round(time.time() - t0, 1), "s")
    for s in r["steps"]:
        print(s["tool"], s.get("score_before"), "->", s.get("score_after"), "kept", s["kept"])
    print("segments", len(r["plan"]["segments"]), "final", r["final"])
    first = r["steps"][0]["score_before"] if r["steps"] else 0
    assert r["final"]["total_pct"] >= first        # never worse than the start
