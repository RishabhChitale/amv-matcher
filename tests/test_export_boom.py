"""K1: plain /api/export-mp4 bakes Boom (boom>0 -> render_plan, boom=0 -> media.export_mp4)."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_render import FPS, frames, make_clip, make_wav  # noqa: E402

from amv_matcher import media, render, server  # noqa: E402


def _job(jid):
    server.JOBS[jid] = {"id": jid, "status": "running", "stage": "export", "done": 0}
    return server.JOBS[jid]


def test_boom_zero_uses_media_export(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(media, "export_mp4", lambda v, a, o, progress=None: (calls.append((v, a)), Path(o).write_bytes(b"x")))
    monkeypatch.setattr(render, "render_plan", lambda *a, **k: pytest.fail("render_plan must not run"))
    job = _job("k1a")
    server._run_export("k1a", tmp_path / "v.mp4", tmp_path / "a.wav", tmp_path / "o.mp4",
                       render.pump_params({"boom": 0}))
    assert calls and job["status"] == "done"


def test_boom_positive_routes_through_render_plan(tmp_path, monkeypatch):
    seen = {}

    def fake(video, plan, wav, out, interp="auto", progress_cb=None, **kw):
        seen.update(plan=plan, wav=wav, interp=interp, pump=kw.get("pump"))
        progress_cb(0.5)
        Path(out).write_bytes(b"x")
    monkeypatch.setattr(render, "render_plan", fake)
    monkeypatch.setattr(media, "export_mp4", lambda *a, **k: pytest.fail("plain export must not run"))
    monkeypatch.setattr(server, "_song", lambda p: {"beat_times": [1.0], "drops": [], "duration": 4.0})
    job = _job("k1b")
    pump = render.pump_params({"boom": 80, "decay": 200, "drops_only": True, "shake": True})
    server._run_export("k1b", tmp_path / "v.mp4", tmp_path / "a.wav", tmp_path / "o.mp4", pump)
    assert job["status"] == "done"
    assert seen["interp"] == "dup" and seen["plan"] == {}
    p = seen["pump"]
    assert (p["boom"], p["decay"], p["drops_only"], p["shake"]) == (80, 200, True, True)
    assert p["song"]["beat_times"] == [1.0]


def test_boom_error_shape(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise ValueError("nope")
    monkeypatch.setattr(render, "render_plan", boom)
    monkeypatch.setattr(server, "_song", lambda p: {"beat_times": [], "drops": []})
    job = _job("k1c")
    server._run_export("k1c", tmp_path / "v.mp4", tmp_path / "a.wav", tmp_path / "o.mp4",
                       render.pump_params({"boom": 50}))
    assert job["status"] == "error" and job["error"] == "ValueError: nope"


def test_real_boom_export_brighter_at_drop(tmp_path, monkeypatch):
    clip = make_clip(tmp_path / "c.mp4")
    wav = make_wav(tmp_path / "a.wav", 4.0)
    monkeypatch.setattr(server, "_song", lambda p: {"beat_times": [], "drops": [2.0], "duration": 4.0})
    base_out, boom_out = tmp_path / "base.mp4", tmp_path / "boom.mp4"
    _job("k1d"), _job("k1e")
    server._run_export("k1d", clip, wav, base_out, render.pump_params({"boom": 0}))
    server._run_export("k1e", clip, wav, boom_out, render.pump_params({"boom": 80, "decay": 180}))
    assert server.JOBS["k1d"]["status"] == "done", server.JOBS["k1d"]
    assert server.JOBS["k1e"]["status"] == "done", server.JOBS["k1e"]
    b, o = frames(base_out), frames(boom_out)
    k = 2 * FPS
    assert o[k].mean() > b[k].mean() * 1.15
    assert np.abs(o[10] - b[10]).mean() < 2.0      # non-beat frame ~untouched


def test_request_validation_clamps():
    m = server.ExportMp4Request(video_path="v", audio_path="a", boom=500, decay=1)
    p = render.pump_params({"boom": m.boom, "decay": m.decay})
    assert p["boom"] == 100 and p["decay"] == 50
    assert server.ExportMp4Request(video_path="v", audio_path="a").boom == 0
    r = server.RenderMp4Request(video_path="v", song_path="s", plan={}, boom=40, shake=True)
    assert r.boom == 40 and r.shake and r.drops_only is False


def test_render_mp4_passes_pump(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(server, "_safe_video", lambda p: tmp_path / "v.mp4")
    monkeypatch.setattr(server, "_safe_song", lambda p: tmp_path / "s.wav")
    got = {}
    monkeypatch.setattr(server._render._pool, "submit", lambda fn, *a: got.update(args=a))
    monkeypatch.setattr(server, "EXPORTS", tmp_path)
    r = TestClient(server.app).post("/api/render-mp4", json={
        "video_path": "v", "song_path": "s", "plan": {}, "interp": "dup",
        "boom": 70, "decay": 120, "drops_only": True, "shake": True})
    assert r.status_code == 200, r.text
    pump = got["args"][-1]
    assert (pump["boom"], pump["decay"], pump["drops_only"], pump["shake"]) == (70, 120, True, True)
    for j in list(server._render.JOBS):
        server._render.JOBS.pop(j, None)
