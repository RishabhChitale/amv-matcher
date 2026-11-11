"""F6 (audio/visual booms) and F9 (camera motion from one dense optical-flow pass)."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from amv_matcher import audio as AU
from amv_matcher import autosync as A
from amv_matcher import boom as B
from amv_matcher import flow as F
from amv_matcher import score as S
from amv_matcher import video as V
from tests import synth
from tests.test_retime import make_song, make_video

W, H, FPS = 160, 120, 30.0


def _texture(w, h, seed=0):
    rng = np.random.default_rng(seed)
    img = rng.uniform(0, 255, (h, w)).astype(np.float32)
    img = cv2.GaussianBlur(img, (0, 0), 2.0)
    img = (img - img.min()) / (np.ptp(img) + 1e-9) * 255.0
    return img.astype(np.uint8)


def _run(frames, duration):
    fp = F.FlowPass(FPS)
    for g in frames:
        fp.feed(g)
    return F.summarize(fp.series(), [], duration)


# --- F9: robust similarity fit ---------------------------------------------------------------

def _pts():
    gy, gx = np.mgrid[-50:51:5, -70:71:5]
    return gx.ravel().astype(float), gy.ravel().astype(float)


def test_fit_similarity_translation():
    px, py = _pts()
    tx, ty, a, b, rm, inl = F.fit_similarity(px, py, np.full_like(px, 3.0), np.full_like(py, -1.5), 160.0)
    assert tx == pytest.approx(3.0, abs=1e-6) and ty == pytest.approx(-1.5, abs=1e-6)
    assert abs(a) < 1e-9 and abs(b) < 1e-9 and inl > 0.99


def test_fit_similarity_zoom_ignores_subject_outliers():
    px, py = _pts()
    dx, dy = 0.02 * px, 0.02 * py                      # 2 % zoom about the centre
    dx[:30] += 8.0                                    # a moving subject in one corner
    tx, ty, a, b, rm, inl = F.fit_similarity(px, py, dx, dy, 160.0)
    assert a == pytest.approx(0.02, abs=2e-3) and abs(b) < 2e-3
    assert abs(tx) < 0.3 and abs(ty) < 0.3
    assert 0.8 < inl < 1.0


# --- F9: flow pass on synthetic camera moves ---------------------------------------------------

def test_flow_pass_detects_pan():
    big = _texture(W + 300, H, seed=1)
    n = int(3 * FPS)
    frames = [big[:, 3 * i:3 * i + W] for i in range(n)]   # camera pans 3 px/frame
    out = _run(frames, n / FPS)
    cm = out["camera_motion"]
    assert cm["hz"] == F.CAM_HZ and len(cm["curve"]) == int(np.ceil(3 * F.CAM_HZ))
    assert float(np.mean(cm["curve"][2:-2])) > 0.4           # ~0.56 widths/s -> intensity ~0.65
    kinds = [e["kind"] for e in out["camera_events"]]
    assert "pan" in kinds and "whip" not in kinds and "zoom" not in kinds
    ev = next(e for e in out["camera_events"] if e["kind"] == "pan")
    assert ev["end"] - ev["start"] > 2.0 and ev["speed"] == pytest.approx(3 * FPS / W, rel=0.25)
    assert len(out["subject_curve"]) == int(np.ceil(3 * 10))


def test_flow_pass_detects_zoom():
    big = _texture(640, 480, seed=2)
    n = int(3 * FPS)
    frames = []
    for i in range(n):
        s = 1.01 ** i                                        # crop shrinks -> content grows: zoom in
        cw, ch = int(round(400 / s)), int(round(300 / s))
        x0, y0 = (640 - cw) // 2, (480 - ch) // 2
        frames.append(cv2.resize(big[y0:y0 + ch, x0:x0 + cw], (W, H), interpolation=cv2.INTER_AREA))
    out = _run(frames, n / FPS)
    zooms = [e for e in out["camera_events"] if e["kind"] == "zoom"]
    assert zooms and zooms[0]["direction"] == "in"
    assert not [e for e in out["camera_events"] if e["kind"] in ("pan", "tilt", "whip")]


def test_still_frames_have_no_camera_motion():
    g = _texture(W, H, seed=3)
    out = _run([g] * int(2 * FPS), 2.0)
    assert out["camera_events"] == []
    assert max(out["camera_motion"]["curve"]) < 0.1


def test_camera_events_whip_vs_fast_pan():
    rate = 15.0

    def ev(n_fast):
        n = 60
        t = np.arange(1, n + 1) / rate
        px = np.zeros(n)
        px[20:20 + n_fast] = 2.0                             # >= WHIP_MIN
        z, inl, ok = np.zeros(n), np.ones(n), np.ones(n, bool)
        return F.camera_events(t, px, np.zeros(n), z, inl, ok, rate)

    short = ev(6)                                            # 0.4 s burst
    assert [e["kind"] for e in short] == ["whip"] and short[0]["direction"] == "right"
    long_ = ev(30)                                           # 2 s at whip speed: a fast pan
    assert [e["kind"] for e in long_] == ["pan"]


def test_camera_events_need_reliable_fit():
    rate, n = 15.0, 45
    t = np.arange(1, n + 1) / rate
    px = np.full(n, 0.5)
    evs = F.camera_events(t, px, np.zeros(n), np.zeros(n), np.full(n, 0.1), np.ones(n, bool), rate)
    assert evs == []                                         # inliers below MIN_INLIERS: camera unknown


# --- F9: energy arc and sync -----------------------------------------------------------------

def test_energy_fields_camera_weight_zero_is_pre_f9():
    rng = np.random.default_rng(4)
    curve = rng.uniform(0, 1, int(10 * FPS))
    cuts = [2.0, 5.0, 7.5]
    cm = {"hz": 4.0, "curve": [0.8] * 40, "whip": [0] * 40}
    base = V.energy_fields(curve, FPS, cuts, 10.0)
    assert V.energy_fields(curve, FPS, cuts, 10.0, camera_motion=cm, cam_w=0.0) == \
        V.energy_fields(curve, FPS, cuts, 10.0, camera_motion=None)
    assert V.energy_fields(curve, FPS, cuts, 10.0, camera_motion=None) == base
    lop = {"hz": 4.0, "curve": [0.0] * 20 + [0.9] * 20, "whip": [0] * 40}
    withcam = V.energy_fields(curve, FPS, cuts, 10.0, camera_motion=lop, cam_w=0.35)
    assert withcam["energy_curve"] != base["energy_curve"]


def test_camera_energy_whip_boost():
    cm = {"hz": 4.0, "curve": [0.5] * 8, "whip": [0, 0, 0, 0, 1, 1, 0, 0]}
    e = V.camera_energy(cm, 20)
    assert e[3] == pytest.approx(0.25) and e[12] > 0.7
    assert not V.camera_energy(None, 20).any()


def test_camera_sync_events_off_by_default():
    v = make_video([1.0, 2.0], duration=5.0, tempo=120.0)
    v["camera_events"] = [{"start": 1.2, "end": 2.6, "kind": "pan"}, {"start": 3.0, "end": 3.3, "kind": "whip"}]
    t, w = S._camera_events(v, np.asarray([1.0, 2.0]))
    assert len(t) == 0 and len(w) == 0


# --- F6: booms --------------------------------------------------------------------------------

def test_audio_boom_found_at_drop(tmp_path):
    p = tmp_path / "drop.wav"
    synth.make_drop(str(p), bpm=120, seconds=12.0, drop_at=6.0)
    Sx, freqs, env = AU._boom_inputs(str(p))
    r = B.audio_booms(Sx, freqs, AU.SR, AU.HOP, env)
    assert r["source"] == "mix" and r["booms"]
    assert r["main"] == pytest.approx(6.0, abs=0.15)
    assert all(0.0 <= b["conf"] <= 1.0 for b in r["booms"])
    assert r == B.audio_booms(Sx, freqs, AU.SR, AU.HOP, env)     # deterministic


def test_audio_booms_too_short_is_empty():
    Sx = np.ones((1025, 20))
    r = B.audio_booms(Sx, np.linspace(0, 11025, 1025), 22050, 512, np.ones(20))
    assert r == {"booms": [], "main": None, "source": "mix"}


def _bsong(main=10.0):
    return {"booms": [{"t": main, "conf": 1.0}, {"t": 18.0, "conf": 0.5}], "main_boom": main,
            "duration": 30.0, "bpm": 120.0}


def test_boom_stats_hit_and_miss():
    v = {"duration": 20.0, "visual_booms": [{"t": 4.0, "conf": 1.0}]}
    hit = B.boom_stats(_bsong(), v, 6.0, 0.0)
    assert hit["boom"]["matched"] and hit["boom"]["main"] and hit["boom"]["err_ms"] == 0.0
    assert hit["n_booms"] == 2 and hit["n_matched"] == 1
    assert hit["boom_pct"] == pytest.approx(100.0 * 2.0 / 2.5, abs=0.1)   # main counts twice, other missed
    miss = B.boom_stats(_bsong(), v, 3.0, 0.0)
    assert not miss["boom"]["matched"] and miss["boom_pct"] < 1.0
    assert B.boom_stats({"booms": []}, v, 0.0) is None
    assert B.boom_stats(_bsong(), v, 25.0, 0.0) is None                     # no boom inside the video
    nov = B.boom_stats(_bsong(), {"duration": 20.0}, 6.0, 0.0)
    assert nov["boom_pct"] == 0.0 and nov["boom"]["vis_t"] is None


def test_blend_total_never_lowers():
    assert B.blend_total(80.0, None) == 80.0
    assert B.blend_total(80.0, {"boom_pct": 0.0}) == 80.0
    assert B.blend_total(80.0, {"boom_pct": 100.0}) == pytest.approx(82.0)


def test_map_and_splice_song_booms():
    s = _bsong()
    assert B.map_song_booms({"duration": 5.0}, lambda t: t) == {}
    m = B.map_song_booms(s, lambda t: t * 0.5)
    assert [b["t"] for b in m["booms"]] == [5.0, 9.0] and m["main_boom"] == 5.0
    segs = [{"src_start": 0.0, "src_end": 12.0, "dst_start": 0.0},
            {"src_start": 8.0, "src_end": 12.0, "dst_start": 12.0}]       # 18 s boom cut, 10 s one repeated
    sp = B.splice_song_booms(s, segs)
    assert [b["t"] for b in sp["booms"]] == [10.0, 14.0] and sp["main_boom"] == 10.0
    cut = B.splice_song_booms(s, [{"src_start": 12.0, "src_end": 30.0, "dst_start": 0.0}])
    assert cut["main_boom"] is None and [b["t"] for b in cut["booms"]] == [6.0]


# --- F6: guarded boom anchor in auto-sync ---------------------------------------------------------

def test_boom_offsets():
    v = {"duration": 20.0, "visual_booms": [{"t": 4.0, "conf": 1.0}, {"t": 30.0, "conf": 0.9}]}
    lead = S._lead(None)
    offs = A._boom_offsets(_bsong(), v, 0.0, None)
    assert offs and offs[0] == pytest.approx(round(10.0 - lead - 4.0, 3))
    assert all(S.MIN_OFFSET_S - 1e-9 <= o <= 10.0 + 1e-9 for o in offs)   # 30 s boom lands out of range
    assert offs == A._boom_offsets(_bsong(), v, 0.0, None)
    assert A._boom_offsets({"duration": 30.0, "bpm": 120.0}, v, 0.0) == []
    assert A._boom_offsets(_bsong(), {"duration": 20.0}, 0.0) == []


def test_auto_sync_without_booms_has_no_boom_step():
    cuts = [round(1.0 + 0.5 * i, 3) for i in range(30)]
    r = A.auto_sync(make_song(30.0, 120.0), make_video(cuts, duration=20.0, tempo=120.0), budget_s=5.0)
    assert not [s for s in r["steps"] if s["tool"] == "boom"]


def test_boom_ok_guard():
    old = {"tempo_pct": 90.0, "sync_pct": 90.0, "arc_pct": 70.0, "boom_pct": 10.0}
    assert A._boom_ok(dict(old, boom_pct=20.0), old)
    assert not A._boom_ok(dict(old, boom_pct=12.0), old)                     # gain below BOOM_MIN_GAIN
    assert not A._boom_ok(dict(old, boom_pct=40.0, sync_pct=89.0), old)      # sync dropped > 0.5
