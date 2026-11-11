"""F2b: no song is ever excluded, zeroed or hidden for being shorter than the video (synthetic, no media)."""
import pathlib
import re

import numpy as np

from amv_matcher.arrange import plan_arrangement
from amv_matcher.autosync import auto_sync
from amv_matcher.score import score
from amv_matcher.splice import plan_splice
from tests.test_retime import make_song, make_video

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _video_180():
    return make_video(np.arange(1, 360) * 0.5, duration=180.0)


def test_20s_song_on_3min_video_scored_and_synced():
    song, video = make_song(20.0), _video_180()
    r = score(song, video)
    assert r["eligible"] is True and r["total_pct"] > 0
    assert r["video_use"]["label"] == "uses 0:20 of 3:00 video"
    a = auto_sync(song, video)
    assert a["eligible"] is not False and a["final"]["total_pct"] > 0 and a["plan"]["video_window"]
    sp = plan_splice(song, video, 2)
    assert sp["eligible"] is not False and sp["video_window"]["end_s"] <= 180.0 + 1e-6
    ar = plan_arrangement(song, video)
    assert ar.get("eligible") is not False and ar["video_window"]["end_s"] <= 180.0 + 1e-6


def test_any_short_length_scores():
    video = _video_180()
    for d in (1.0, 2.5, 5.0, 60.0, 179.0):
        r = score(make_song(d), video)
        assert r["eligible"] is True and r["total_pct"] >= 0 and r.get("reason") is None, d


def test_only_unreadable_is_ineligible():
    s = make_song(20.0); s["duration"] = 0.0
    for r in (score(s, _video_180()), plan_splice(s, _video_180(), 2), plan_arrangement(s, _video_180())):
        assert r["eligible"] is False and r["reason"] == "unreadable"


def test_no_length_gate_left_in_code():
    banned = re.compile(r"PARTIAL_MIN_S|song shorter than video|too short to splice|shorter than the video, or unreadable"
                        r"|Too short for this video")
    hits = []
    for p in list((ROOT / "amv_matcher").rglob("*.py")) + list((ROOT / "amv_matcher" / "static").rglob("*.js")):
        for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
            if banned.search(line):
                hits.append("%s:%d" % (p.relative_to(ROOT), i))
    assert not hits, hits
