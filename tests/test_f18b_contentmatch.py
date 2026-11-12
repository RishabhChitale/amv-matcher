"""F18b content match: when the video's own audio is a hand splice of the song, auto-sync rebuilds that splice.

Synthetic signals only (no media): a song with non-repeating energy/onset curves, a reference made by cutting it.
"""
import json

import numpy as np
import pytest

from amv_matcher import autosync, contentmatch
from tests.test_retime import make_video

HZ = 100


def _smooth_noise(n, seed, k=25):
    rng = np.random.default_rng(seed)
    x = np.convolve(rng.random(n + k), np.ones(k) / k, mode="valid")[:n]
    return np.clip(x - x.min(), 0, None) + 0.05


def make_song(duration=120.0, seed=1, bpm=120.0):
    n = int(duration * HZ)
    en, on = _smooth_noise(n, seed), _smooth_noise(n, seed + 100, k=5)
    bt = np.arange(0, duration, 60.0 / bpm)
    return {"bpm": bpm, "beat_times": bt.tolist(), "beat_strengths": [1.0] * len(bt), "duration": duration,
            "energy_curve": en.tolist(), "energy_hz": HZ, "onset_env": on.tolist(), "onset_env_fps": HZ,
            "drops": [], "lulls": []}


def splice_ref(song, pieces):
    """The 'video's own audio': pieces [(src_start, src_end)] of the song laid end to end."""
    en, on = np.asarray(song["energy_curve"]), np.asarray(song["onset_env"])
    e = np.concatenate([en[int(a * HZ):int(b * HZ)] for a, b in pieces])
    o = np.concatenate([on[int(a * HZ):int(b * HZ)] for a, b in pieces])
    return {"duration": len(e) / HZ, "energy_curve": e.tolist(), "energy_hz": HZ,
            "onset_env": o.tolist(), "onset_env_fps": HZ}


def test_splice_is_rebuilt():
    song = make_song()
    ref = splice_ref(song, [(20.0, 80.0), (95.0, 115.0)])
    cm = contentmatch.match(song, ref)
    assert cm is not None and len(cm["segments"]) == 2
    a, b = cm["segments"]
    assert abs(a["src_start"] - 20.0) < 0.1 and abs(a["dst_start"]) < 1e-6
    assert abs(b["dst_start"] - 60.0) < 0.1 and abs(b["src_start"] - 95.0) < 0.1
    assert abs(b["src_end"] - 115.0) < 0.1
    assert [round(s["offset"], 1) for s in cm["spans"]] == [20.0, 35.0]


def test_single_offset_found():
    song = make_song()
    cm = contentmatch.match(song, splice_ref(song, [(30.0, 90.0)]))
    assert cm is not None and len(cm["segments"]) == 1
    assert abs(cm["segments"][0]["src_start"] - 30.0) < 0.05


def test_unrelated_audio_is_no_match():
    assert contentmatch.match(make_song(seed=1), splice_ref(make_song(seed=7), [(10.0, 70.0)])) is None


def _video(duration):
    return make_video(list(np.arange(0.5, duration - 0.5, 0.5)), duration=duration)


def test_auto_sync_offers_the_rebuilt_edit(monkeypatch):
    song = make_song()
    ref = splice_ref(song, [(20.0, 80.0), (95.0, 115.0)])
    vid = _video(ref["duration"])
    vid["original_audio"] = {"analysis": ref}
    monkeypatch.setattr(autosync, "CM_TOTAL_TOL", 1000)          # always keep it: check the plan it gives
    r = autosync.auto_sync(song, vid)
    cmi = r["plan"]["content_match"]
    assert cmi["kept"] and r["plan"]["content_matched"] and not r["plan"]["looped"]
    segs = r["plan"]["segments"]
    assert len(segs) == 2 and abs(segs[0]["src_start"] - 20.0) < 0.1 and abs(segs[1]["src_start"] - 95.0) < 0.1
    assert any("content match" in s for s in r["limits_hit"])


def test_auto_sync_kept_only_when_it_scores(monkeypatch):
    song = make_song()
    ref = splice_ref(song, [(20.0, 80.0), (95.0, 115.0)])
    vid = _video(ref["duration"])
    vid["original_audio"] = {"analysis": ref}
    monkeypatch.setattr(autosync, "CM_TOTAL_TOL", -1000)         # never good enough: the other paths stand
    r = autosync.auto_sync(song, vid)
    assert r["plan"]["content_match"]["kept"] is False and r["plan"]["content_matched"] is False
    del vid["original_audio"]
    plain = autosync.auto_sync(song, vid)
    assert r["plan"]["segments"] == plain["plan"]["segments"]
    assert r["final"]["total_pct"] == plain["final"]["total_pct"]


def _strip(r):
    return json.dumps(r, sort_keys=True, default=str)


@pytest.mark.parametrize("case", ["no_original", "unrelated", "is_original", "missing_file"])
def test_no_change_when_it_does_not_apply(case, monkeypatch):
    song = make_song()
    vid = _video(80.0)
    kw = {}
    if case == "unrelated":
        vid["original_audio"] = {"analysis": splice_ref(make_song(seed=7), [(10.0, 90.0)])}
    elif case == "is_original":
        vid["original_audio"] = {"analysis": splice_ref(song, [(20.0, 80.0), (95.0, 115.0)])}
        kw["original"] = True
    elif case == "missing_file":
        vid["original_audio"] = {"path": "/nonexistent/f18b-test.wav"}
    r = autosync.auto_sync(song, vid, **kw)
    assert "content_match" not in r["plan"]
    monkeypatch.setattr(autosync, "CONTENT_MATCH", False)
    off = autosync.auto_sync(song, vid, **kw)
    a, b = json.loads(_strip(r)), json.loads(_strip(off))
    for x in (a, b):
        x.pop("elapsed_s", None)
        x.get("final", {}).pop("elapsed_s", None)
    assert a == b
