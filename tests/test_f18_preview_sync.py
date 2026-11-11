"""F18: the auto-sync preview must play what the export renders, on the beat.

Root cause found with the closed-loop tool: render == plan, but the in-app preview drove the video at the merged
retime *section* speeds (display bands) and drifted up to ~0.4 s from the audio, which was then rate-nudged and
seeked (off-beat, warbly). Tempo plans without a splice also previewed the raw song at 1.0x.
"""
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
AS_JS = (ROOT / "static" / "autosync.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- JS source contract

def _fn(src, head):
    i = src.index(head)
    j = src.index("\n};", i)
    return src[i:j]


def test_video_follows_exact_warp_slope_not_sections():
    assert "function warpSpeed(v)" in AS_JS and "planWarp(" in _fn(AS_JS, "function warpSpeed(v)")
    body = _fn(AS_JS, "window.asFrame = () => {")
    assert "warpSpeed(" in body and "sections()" not in body and ".speed" not in body
    assert "vid.playbackRate" in body and "aud.playbackRate" not in body   # trim goes on the muted video


def test_video_trim_is_bounded():
    body = AS_JS[AS_JS.index("function videoTrim()"):AS_JS.index("// Preview audio segments")]
    assert "VTRIM.cap" in body and "> 0.3" in body
    assert "cap: 0.05" in AS_JS


def test_preview_uses_the_warp_the_export_renders():
    body = _fn(AS_JS, "function planWarp(p) {")
    assert body.index("'retime_ramps'") < body.index("'retime'")        # same priority as render._clip_warp
    assert "planWarp(r.result.plan)" in _fn(AS_JS, "function warpSpeed(v) {")
    assert "planWarp(r.result.plan)" in AS_JS[AS_JS.index("function warp(v)"):AS_JS.index("function warpSpeed(v)")]


def test_playback_applies_no_perceptual_lead():
    """PERCEPTUAL_LEAD_S / the guard lead shapes the PLAN (cuts land slightly before beats); playback must not add it
    again, or the preview gets a constant audio/video offset."""
    parts = [_fn(AS_JS, "window.asFrame = () => {"), _fn(AS_JS, "function warpSpeed(v) {"),
             AS_JS[AS_JS.index("function videoTrim()"):AS_JS.index("// Preview audio segments")],
             _fn(AS_JS, "function previewSegs(p) {"),
             APP_JS[APP_JS.index("async function renderAudio(path, segs, tempo)"):][:600]]
    for body in parts:
        for bad in ("lead_s", "guard.lead", "g.lead", "PERCEPTUAL"):
            assert bad not in body, (bad, body[:60])


def test_song_clock_exposed_for_trim():
    assert "window.songTime = songTime;" in APP_JS


def test_tempo_plans_preview_from_a_rendered_file():
    body = _fn(AS_JS, "function previewSegs(p) {")
    assert "tempo_ratio" in body and "dst_start: lead" in body
    assert "renderAudio(r.path, segs, tempo)" in AS_JS
    ra = APP_JS[APP_JS.index("async function renderAudio(path, segs, tempo)"):]
    assert "body.tempo_ratio = tempo" in ra[:600]
    assert "segKey = (path, segs, tempo)" in APP_JS


# --------------------------------------------------------------------------- drift simulation (mirror of the JS)

def _warp_speed(w, v):
    if len(w) < 2 or v <= w[0][0] or v >= w[-1][0]:
        return 1.0
    i = int(np.searchsorted(w[:, 0], v, side="right")) - 1
    i = min(max(i, 0), len(w) - 2)
    dv, dout = w[i + 1, 0] - w[i, 0], w[i + 1, 1] - w[i, 1]
    return dv / dout if dv > 0 and dout > 1e-6 else 1.0


def _simulate(w, dur, trim=True, seed=0, signed=False):
    """Audio = master clock on the output timeline; video integrates playbackRate set once per (jittery) frame,
    applied a frame late. Returns |audio - song clock mapped from the video| over time (signed with signed=True)."""
    rng = np.random.default_rng(seed)
    t, v, rate, out = 0.0, 0.0, 1.0, []
    while t < dur:
        dt = float(rng.uniform(0.012, 0.022))
        t += dt
        v += rate * dt
        st = float(np.interp(v, w[:, 0], w[:, 1]))
        d = t - st
        k = 1.0
        if trim and 0.01 <= abs(d) <= 0.3:
            k = 1 + max(-0.05, min(0.05, d * 0.5))
        rate = min(16.0, max(0.0625, _warp_speed(w, v) * k))
        out.append(d if signed else abs(d))
    return np.array(out)


def _synthetic_warp(seed=1):
    rng = np.random.default_rng(seed)
    v = np.linspace(0, 60, 121)
    speed = np.clip(1 + 0.12 * np.sin(v / 3.0) + 0.04 * rng.standard_normal(len(v)), 0.75, 1.3)
    out = np.concatenate([[0.0], np.cumsum(np.diff(v) / speed[:-1])])
    return np.stack([v, out], 1)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_preview_drift_stays_tight(seed):
    w = _synthetic_warp(seed)
    for trim in (False, True):
        d = _simulate(w, float(w[-1, 1]) - 0.5, trim=trim, seed=seed)
        assert d.max() < 0.04, (trim, d.max())     # no audio nudge (0.15 s) or seek is ever needed
        assert np.mean(d > 0.02) < 0.05


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_preview_drift_has_no_constant_offset(seed):
    w = _synthetic_warp(seed)
    d = _simulate(w, float(w[-1, 1]) - 0.5, trim=True, seed=seed, signed=True)
    assert abs(float(np.mean(d))) < 0.005 and abs(float(np.median(d))) < 0.005


def test_render_applies_no_lead(tmp_path):
    """The planner's lead_s is not a render-time shift: the same segments render identically whatever lead_s says."""
    from amv_matcher import render
    song = _click(tmp_path / "c.wav")
    base = {"tempo_ratio": 1.0, "segments": [{"src_start": 1.0, "src_end": 5.0, "dst_start": 0.0}]}
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    render.build_audio(song, {**base, "lead_s": 0.06}, 4.0, a)
    render.build_audio(song, {**base, "lead_s": 0.0}, 4.0, b)
    assert np.array_equal(sf.read(str(a))[0], sf.read(str(b))[0])


# --------------------------------------------------------------------------- tempo preview == export audio

def _click(path, bpm=120.0, dur=12.0, sr=44100):
    y = np.zeros(int(sr * dur), dtype=np.float32)
    n = int(0.02 * sr)
    burst = (0.6 * np.sin(2 * np.pi * 1000 * np.arange(n) / sr) * np.hanning(n)).astype(np.float32)
    for k in np.arange(0, dur - 0.05, 60.0 / bpm):
        i = int(k * sr)
        y[i:i + n] += burst[:len(y) - i]
    sf.write(str(path), np.stack([y, y], 1), sr, subtype="FLOAT")
    return path


def _onsets(path):
    y, sr = sf.read(str(path))
    e = np.abs(y[:, 0])
    hop = int(0.005 * sr)
    env = np.array([e[i:i + hop].max() for i in range(0, len(e) - hop, hop)])
    on = np.where((env[1:] > 0.2) & (env[:-1] <= 0.2))[0] + 1
    return on * hop / sr


@pytest.mark.parametrize("plan", [
    {"tempo_ratio": 1.1, "offset": 1.0, "segments": []},
    {"tempo_ratio": 0.92, "offset": -0.5, "segments": []},
    {"tempo_ratio": 1.06, "segments": [{"src_start": 0.5, "src_end": 3.5, "dst_start": 0.0},
                                       {"src_start": 5.0, "src_end": 8.0, "dst_start": 3.0}]},
])
def test_tempo_preview_matches_export(tmp_path, plan):
    from amv_matcher import render
    song = _click(tmp_path / "c.wav")
    vd = 6.0
    exp = tmp_path / "export.wav"
    render.build_audio(song, plan, vd, exp)
    segs = plan["segments"]
    if not segs:                                    # what autosync.js previewSegs() sends
        off = plan["offset"]
        lead = max(0.0, -off)
        segs = [{"src_start": max(0.0, off), "src_end": max(0.0, off) + vd - lead, "dst_start": lead}]
    prev = tmp_path / "preview.wav"
    render.render_tempo_segments(song, plan["tempo_ratio"], segs, vd, prev)
    a, _ = sf.read(str(exp))
    b, _ = sf.read(str(prev))
    assert a.shape == b.shape
    assert np.max(np.abs(a - b)) < 1e-4
    on = _onsets(prev)
    gaps = np.diff(on)
    beat = 0.5 / plan["tempo_ratio"]
    ok = gaps[np.abs(gaps - beat) < 0.1]
    assert len(ok) >= 5 and abs(np.median(ok) - beat) < 0.012   # the preview plays at the plan's tempo


def test_tempo_one_is_plain_render(tmp_path):
    from amv_matcher import media, render
    song = _click(tmp_path / "c.wav")
    segs = [{"src_start": 1.0, "src_end": 5.0, "dst_start": 0.0}]
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    render.render_tempo_segments(song, 1.0, segs, 4.0, a)
    media.render_audio(song, segs, 4.0, b)
    assert np.array_equal(sf.read(str(a))[0], sf.read(str(b))[0])


# --------------------------------------------------------------------------- /api/render-audio tempo_ratio

def test_render_audio_endpoint_tempo(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from amv_matcher import paths, server
    from tests.synth import make_video
    monkeypatch.setattr(server, "UPLOADS", tmp_path / "uploads")
    monkeypatch.setattr(server, "EXTRACTED", tmp_path / "extracted")
    monkeypatch.setattr(server, "EXPORTS", tmp_path / "exports")
    monkeypatch.setattr(paths, "CACHE", tmp_path / "cache")
    c = TestClient(server.app)
    v = tmp_path / "v.avi"
    make_video(v, seconds=4.0)
    s = _click(tmp_path / "c.wav", dur=10.0)
    up = lambda kind, p: c.post("/api/upload", params={"kind": kind, "name": p.name}, content=p.read_bytes())
    uv, us = up("video", v).json()["path"], up("song", s).json()["path"]
    seg = [{"src_start": 1.0, "src_end": 5.0, "dst_start": 0.0}]
    base = {"video_path": uv, "song_path": us, "segments": seg}
    r1 = c.post("/api/render-audio", json=base)
    r2 = c.post("/api/render-audio", json={**base, "tempo_ratio": 1.2})
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert r1.json()["path"] != r2.json()["path"]                    # tempo is part of the cache key
    assert abs(sf.info(r2.json()["path"]).duration - 4.0) < 1 / 30 + 1e-6
    # 10 s song at 1.25x is 8 s long: a segment ending at 9 s is outside the tempo'd song
    bad = {**base, "segments": [{"src_start": 5.0, "src_end": 9.0, "dst_start": 0.0}], "tempo_ratio": 1.25}
    assert c.post("/api/render-audio", json=bad).status_code == 422
    assert c.post("/api/render-audio", json={**base, "tempo_ratio": 3.0}).status_code == 422
