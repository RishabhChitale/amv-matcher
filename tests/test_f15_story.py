"""F15: beat-following lock, story gate cap, key visual moments (synthetic, no media)."""
import numpy as np
import pytest
from scipy.ndimage import uniform_filter1d

from amv_matcher import boom, score as sc


def _flat_video(events, vd, energy=None):
    n = int(vd * 10)
    return {"duration": vd, "cut_times": list(events), "motion_peaks": [],
            "energy_curve": list(energy) if energy is not None else [0.5] * n,
            "peak_segments": [], "lulls": [], "motion_tempo_bpm": 120.0}


# --- beat-following lock ---------------------------------------------------------------------

def _jumpy_beats(sd=60.0, T=0.5, jump_every=8.0):
    """Tracked beats of a spliced song: period T, phase flips by half a beat every `jump_every` s."""
    out, t, ph = [], 0.0, 0.0
    while t < sd:
        out.append(t + ph)
        t += T
        if t % jump_every < T - 1e-9:
            ph = T / 2 - ph
    return np.array(out)


def test_warp_lock_skips_grid_songs():
    beats = np.arange(0, 60, 0.5)
    song = {"bpm": 120.0, "beat_times": list(beats), "beat_grid": list(beats), "grid_bpm": 120.0,
            "grid_used": True}
    assert sc._uses_grid(song)
    assert sc._lock_warp(song, beats[:30], np.ones(30), 0.0, 0.0) is None
    song["grid_used"] = False
    assert not sc._uses_grid(song)
    assert sc._lock_warp(dict(song, beat_times=list(beats[:8])), beats[:8], np.ones(8), 0.0, 0.0) is None


@pytest.fixture
def f15_on(monkeypatch):
    monkeypatch.setattr(sc, "F15_ENABLED", True)


@pytest.fixture
def warp_on(monkeypatch):
    monkeypatch.setattr(sc, "WARPLOCK_ENABLED", True)


def _jumpy_case():
    beats = _jumpy_beats()
    song = {"duration": 60.0, "bpm": 120.0, "beat_times": list(beats), "grid_used": False,
            "energy_curve": [0.5] * 600, "drops": [], "lulls": []}
    ev = beats[(beats > 1) & (beats < 29)][::2] - sc._lead()
    return song, ev


def test_f15_off_by_default():
    import os
    if os.environ.get("AMV_F15", "") != "1":
        assert sc.F15_ENABLED is False


def test_warplock_on_by_default():
    import os
    assert sc.WARPLOCK_DEFAULT is True
    if os.environ.get("AMV_WARPLOCK", "").strip() in ("", "1"):
        assert sc.WARPLOCK_ENABLED is True


@pytest.mark.parametrize("env,want", [(None, True), ("", True), ("1", True), ("0", False), (" 0 ", False)])
def test_warplock_env_parsing(env, want):
    assert sc._warplock_flag(env) is want
    assert sc._warplock_flag(None, default=False) is False     # unset keeps whatever the default is


@pytest.mark.parametrize("f15", [False, True])
def test_warp_lock_used_by_default(monkeypatch, f15):
    monkeypatch.setattr(sc, "WARPLOCK_ENABLED", sc._warplock_flag(None))
    monkeypatch.setattr(sc, "F15_ENABLED", f15)
    song, ev = _jumpy_case()
    on = sc._sync_pct_at(song, ev, np.ones(len(ev)), 30.0, 0.0)
    monkeypatch.setattr(sc, "_lock_warp", lambda *a, **k: None)
    assert on > sc._sync_pct_at(song, ev, np.ones(len(ev)), 30.0, 0.0) + 20


def test_warp_lock_follows_half_beat_jumps(monkeypatch, warp_on):
    beats = _jumpy_beats()
    song = {"duration": 60.0, "bpm": 120.0, "beat_times": list(beats), "grid_used": False,
            "energy_curve": [0.5] * 600, "drops": [], "lulls": []}
    lead = sc._lead()
    ev = beats[(beats > 1) & (beats < 29)][::2] - lead      # events on the beats, editor's lead
    vd = 30.0
    z, err = sc._lock_warp(song, ev, np.ones(len(ev)), 0.0, lead)
    assert z > 3.0 and err < 0.03, (z, err)
    warped = sc._sync_pct_at(song, ev, np.ones(len(ev)), vd, 0.0)
    monkeypatch.setattr(sc, "_lock_warp", lambda *a, **k: None)
    const = sc._sync_pct_at(song, ev, np.ones(len(ev)), vd, 0.0)
    assert warped > const + 20, (warped, const)


@pytest.mark.parametrize("f15", [False, True])
def test_warp_lock_unused_when_warplock_off(monkeypatch, f15):
    monkeypatch.setattr(sc, "WARPLOCK_ENABLED", sc._warplock_flag("0"))   # AMV_WARPLOCK=0
    monkeypatch.setattr(sc, "F15_ENABLED", f15)          # the story gate flag does not turn the warp on
    song, ev = _jumpy_case()
    off = sc._sync_pct_at(song, ev, np.ones(len(ev)), 30.0, 0.0)
    monkeypatch.setattr(sc, "_lock_warp", lambda *a, **k: None)
    assert sc._sync_pct_at(song, ev, np.ones(len(ev)), 30.0, 0.0) == off


def test_warplock_alone_adds_no_story_fields(monkeypatch, warp_on):
    monkeypatch.setattr(sc, "F15_ENABLED", False)
    song, ev = _jumpy_case()
    video = _flat_video(list(ev), 30.0)
    r = sc.score_at(song, video, 0.0)
    assert not ({"story_z", "story_capped", "key_moments"} & set(r))


def test_warp_lock_needs_min_z(monkeypatch, warp_on):
    song, ev = _jumpy_case()
    monkeypatch.setattr(sc, "_lock_warp", lambda *a, **k: None)
    const = sc._sync_pct_at(song, ev, np.ones(len(ev)), 30.0, 0.0)
    monkeypatch.setattr(sc, "_lock_warp", lambda *a, **k: (sc.WARP_MIN_Z - 0.5, 0.01))
    assert sc._sync_pct_at(song, ev, np.ones(len(ev)), 30.0, 0.0) == const


# --- story gate ------------------------------------------------------------------------------

def test_story_cap_shape():
    assert sc.story_cap(None) == 100.0
    assert sc.story_cap(-3.0) == sc.STORY_CAP_LO == 80.0
    assert sc.story_cap(0.0) == 80.0
    assert abs(sc.story_cap(1.0) - 90.0) < 1e-9
    assert sc.story_cap(2.0) == 100.0 and sc.story_cap(5.0) == 100.0


def _profile(n, seed):
    rng = np.random.default_rng(seed)
    return np.clip(uniform_filter1d(rng.normal(0, 1, n), 25) * 3 + 1.0, 0.05, None)


def test_story_stats_none_when_flat_or_short():
    song = {"bpm": 120.0, "energy_curve": list(_profile(1500, 1)), "energy_hz": 10.0}
    flat = _flat_video([], 30.0)
    assert sc.story_stats(song, flat, 60.0)["story_z"] is None
    short = _flat_video([], 5.0, energy=_profile(50, 2))
    assert sc.story_stats(song, short, 60.0)["story_z"] is None
    assert sc.story_stats(dict(song, bpm=0), _flat_video([], 30.0, _profile(300, 3)), 60.0)["story_z"] is None


def test_story_stats_matching_story_beats_wrong_story():
    se = _profile(1500, 4)                                  # 150 s song
    song = {"bpm": 120.0, "energy_curve": list(se), "energy_hz": 10.0}
    right = _flat_video([], 30.0, energy=se[600:900])       # the video follows the song from 60 s
    wrong = _flat_video([], 30.0, energy=_profile(300, 5))
    zr = sc.story_stats(song, right, 60.0)["story_z"]
    zw = sc.story_stats(song, wrong, 60.0)["story_z"]
    assert zr is not None and zw is not None
    assert zr > 2.0, zr
    assert zw < zr - 1.0, (zr, zw)


def test_pack_caps_total_by_story(monkeypatch, f15_on):
    beats = np.arange(0, 60, 0.5)
    song = {"duration": 60.0, "bpm": 120.0, "beat_times": list(beats), "energy_curve": [0.5] * 600,
            "drops": [], "lulls": []}
    video = _flat_video(list(beats[4:40] - 0.05), 30.0)
    monkeypatch.setattr(sc, "_combine", lambda *a: 95.0)
    monkeypatch.setattr(sc, "story_stats", lambda *a: {"story_z": -1.0, "story_r": 0.0, "n_null": 40})
    r = sc.score_at(song, video, 0.0)
    assert r["total_pct"] == 80 and r["story_capped"] is True and r["story_z"] == -1.0
    monkeypatch.setattr(sc, "story_stats", lambda *a: {"story_z": None, "story_r": None, "n_null": 0})
    r = sc.score_at(song, video, 0.0)
    assert r["total_pct"] >= 95 and r["story_capped"] is False
    assert isinstance(r["key_moments"], list)


def test_pack_without_f15_has_no_story_fields(monkeypatch):
    monkeypatch.setattr(sc, "F15_ENABLED", False)
    beats = np.arange(0, 60, 0.5)
    song = {"duration": 60.0, "bpm": 120.0, "beat_times": list(beats), "energy_curve": [0.5] * 600,
            "drops": [], "lulls": []}
    video = _flat_video(list(beats[4:40] - 0.05), 30.0)
    monkeypatch.setattr(sc, "_combine", lambda *a: 95.0)
    monkeypatch.setattr(sc, "story_stats", lambda *a: {"story_z": -1.0, "story_r": 0.0, "n_null": 40})
    r = sc.score_at(song, video, 0.0)
    assert r["total_pct"] >= 95
    assert not ({"story_z", "story_capped", "key_moments"} & set(r))


def test_splice_story_src_only_with_f15(monkeypatch):
    from amv_matcher.splice import _assemble
    beats = np.arange(0, 60, 0.5)
    song = {"duration": 60.0, "bpm": 120.0, "beat_times": list(beats), "beat_strengths": [1.0] * len(beats),
            "energy_curve": [0.5] * 600, "drops": [], "lulls": [], "onset_env": [0.1] * 600, "onset_fps": 10.0}
    seg = [{"src_start": 10.0, "src_end": 40.0, "dst_start": 0.0}]
    monkeypatch.setattr(sc, "F15_ENABLED", False)
    assert "story_src" not in _assemble(song, seg, 30.0)
    monkeypatch.setattr(sc, "F15_ENABLED", True)
    assert "story_src" in _assemble(song, seg, 30.0)


# --- key visual moments ----------------------------------------------------------------------

def test_key_moments_find_a_surge():
    fps, dur = 30.0, 60.0
    rng = np.random.default_rng(9)
    m = 0.2 + 0.05 * rng.random(int(fps * dur))
    t = np.arange(len(m)) / fps
    m[(t >= 20.0) & (t < 23.0)] = 1.5                      # calm, then a sustained surge at 0:20
    m[(t >= 44.0) & (t < 44.2)] = 1.5                      # a short spike scores lower
    km = boom.video_key_moments({"duration": dur, "fps": fps, "motion_curve": list(m)})
    assert boom.KM_MIN <= len(km) <= boom.KM_MAX
    best = max(km, key=lambda k: k["score"])
    assert -0.1 <= best["t"] - 20.0 <= boom.KM_GAP_S, km   # within the gap before the surge
    assert [k["t"] for k in km] == sorted(k["t"] for k in km)
    assert all(abs(a["t"] - b["t"]) >= boom.KM_SEP_S - 1e-6 for a, b in zip(km, km[1:]))


def test_key_moments_empty_without_motion():
    assert boom.video_key_moments({"duration": 60.0}) == []
    assert boom.video_key_moments({"duration": 1.0, "fps": 30.0, "motion_curve": [1.0] * 30}) == []
    assert boom.video_key_moments({"duration": 60.0, "fps": 30.0, "motion_curve": [0.5] * 1800}) == []
