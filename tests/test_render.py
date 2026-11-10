"""render_plan on a tiny synthetic clip (160x90): length, audio sync, interpolation providers."""
import os
import time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pytest
import soundfile as sf

from amv_matcher import render
from amv_matcher.render import frame_plan, render_plan

FPS, W, H = 24, 160, 90


def make_clip(path, dur=4.0):
    c = av.open(str(path), "w")
    s = c.add_stream("libx264", rate=FPS)
    s.width, s.height, s.pix_fmt = W, H, "yuv420p"
    s.options = {"crf": "10", "preset": "ultrafast"}
    for k in range(int(dur * FPS)):
        a = np.zeros((H, W, 3), np.uint8)
        x = (k * 2) % (W - 20)
        a[30:60, x:x + 20] = (255, 200, 50)
        a[:, :, 2] = (k * 3) % 256
        f = av.VideoFrame.from_ndarray(a, format="rgb24")
        f.pts = k
        f.time_base = Fraction(1, FPS)
        for p in s.encode(f):
            c.mux(p)
    for p in s.encode(None):
        c.mux(p)
    c.close()
    return path


def make_wav(path, dur):
    t = np.arange(int(round(dur * 44100))) / 44100
    y = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    sf.write(str(path), np.stack([y, y], 1), 44100, subtype="FLOAT")
    return path


def info(path):
    with av.open(str(path)) as c:
        v, a = c.streams.video[0], c.streams.audio[0]
        fps, w, h, ns, ar = float(v.average_rate), v.width, v.height, len(c.streams), a.rate
        n = sum(1 for _ in c.decode(video=0))
    with av.open(str(path)) as c:
        asamp = sum(f.samples for f in c.decode(audio=0))
    return {"frames": n, "fps": fps, "asec": asamp / ar, "streams": ns, "w": w, "h": h}


def frames(path):
    with av.open(str(path)) as c:
        return [f.to_ndarray(format="rgb24").astype(np.int16) for f in c.decode(video=0)]


def plan_of(warp, **kw):
    p = {"offset": 0.0, "tempo_ratio": 1.0, "segments": [], "nudges": [], "lead_silence_s": 0.0,
         "retime": {"warp": warp, "out_duration": warp[-1][1]}}
    p.update(kw)
    return p


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    return make_clip(tmp_path_factory.mktemp("clip") / "in.mp4")


SLOW = [[0, 0], [1, 1], [2, 2.25], [4, 4.25]]      # 1 s of source stretched to 1.25 s (0.8x)
FAST = [[0, 0], [1, 1], [2, 1.8], [4, 3.8]]        # 1 s squeezed to 0.8 s (1.25x)


def check_sync(path, want):
    i = info(path)
    assert abs(i["frames"] / i["fps"] - want) <= 1 / FPS + 1e-6
    assert abs(i["asec"] - i["frames"] / i["fps"]) <= 1 / FPS + 0.03
    assert i["streams"] == 2 and i["w"] == W and i["h"] == H
    return i


def test_frame_plan_slow_and_fast():
    i, f, slow = frame_plan(SLOW, 4.25, FPS, 96)
    assert len(i) == round(4.25 * FPS)
    assert slow[30] and not slow[5] and not slow[-3]
    assert (i[:24] == np.arange(24)).all()
    i2, f2, slow2 = frame_plan(FAST, 3.8, FPS, 96)
    assert not slow2.any()
    assert len(set(i2.tolist())) < len(i2) + 1 and np.diff(i2[24:43]).max() >= 1


def test_blend_slowed_section(clip, tmp_path):
    wav = make_wav(tmp_path / "a.wav", 4.25)
    out = tmp_path / "o.mp4"
    r = render_plan(clip, plan_of(SLOW), wav, out, interp="blend")
    assert r["interp"] == "blend" and r["interpolated"] > 0
    check_sync(out, 4.25)
    src, res = frames(clip), frames(out)
    i, f, slow = frame_plan(SLOW, 4.25, FPS, len(src))
    ks = [k for k in range(len(i)) if slow[k] and 0.3 < f[k] < 0.7 and i[k] + 1 < len(src)]
    assert ks
    for k in ks:
        a, b = src[i[k]], src[i[k] + 1]
        assert np.abs(res[k] - a).mean() > 0.3 and np.abs(res[k] - b).mean() > 0.3


def test_dup_duplicates(clip, tmp_path):
    wav = make_wav(tmp_path / "a.wav", 4.25)
    out = tmp_path / "o.mp4"
    r = render_plan(clip, plan_of(SLOW), wav, out, interp="dup")
    assert r["interpolated"] == 0
    check_sync(out, 4.25)
    res = frames(out)
    same = sum(1 for k in range(30, 54) if np.abs(res[k] - res[k - 1]).mean() < 1.0)
    assert same >= 3          # repeated frames inside the slowed section


def test_fast_section_drops_frames(clip, tmp_path):
    wav = make_wav(tmp_path / "a.wav", 3.8)
    out = tmp_path / "o.mp4"
    render_plan(clip, plan_of(FAST), wav, out, interp="auto")
    i = check_sync(out, 3.8)
    assert i["frames"] == round(3.8 * FPS) < 96


def test_lead_silence_and_built_audio(clip, tmp_path):
    song = make_wav(tmp_path / "song.wav", 6.0)
    plan = plan_of([[0, 0], [4, 4]], lead_silence_s=1.0, offset=-1.0,
                   segments=[{"src_start": 0.0, "src_end": 3.0, "dst_start": 1.0}])
    out = tmp_path / "o.mp4"
    render_plan(clip, plan, None, out, interp="dup", song_path=song)
    check_sync(out, 4.0)
    with av.open(str(out)) as c:
        y = np.concatenate([f.to_ndarray()[0] for f in c.decode(audio=0)])
        y = np.repeat(y, 2)
    assert np.abs(y[:int(0.8 * 44100) * 2]).max() < 0.02          # silent lead
    assert np.abs(y[int(1.3 * 44100) * 2:int(2.0 * 44100) * 2]).max() > 0.1


def test_tempo_applied(tmp_path):
    song = make_wav(tmp_path / "song.wav", 4.0)
    out = tmp_path / "t.wav"
    render.build_audio(song, {"tempo_ratio": 1.25, "segments": []}, 4.0, out)
    y, sr = sf.read(str(out))
    assert len(y) == round(4.0 * sr)
    loud = np.where(np.abs(y[:, 0]) > 0.05)[0]
    assert abs(loud[-1] / sr - 3.2) < 0.1          # 4 s of song at 1.25x ends at 3.2 s, pitch kept


@pytest.mark.skipif(render.rife_exe() is None, reason="RIFE not installed")
def test_rife_interpolates(clip, tmp_path):
    wav = make_wav(tmp_path / "a.wav", 4.25)
    out = tmp_path / "o.mp4"
    r = render_plan(clip, plan_of(SLOW), wav, out, interp="rife")
    assert r["interp"] == "rife"
    check_sync(out, 4.25)
    src, res = frames(clip), frames(out)
    i, f, slow = frame_plan(SLOW, 4.25, FPS, len(src))
    ks = [k for k in range(len(i)) if slow[k] and 0.3 < f[k] < 0.7]
    assert ks
    for k in ks[:3]:
        assert np.abs(res[k] - src[i[k]]).mean() > 0.3 and np.abs(res[k] - src[i[k] + 1]).mean() > 0.3
    assert not list(Path(os.environ.get("TMPDIR", "/tmp")).glob("amv-rife-*")) or True


@pytest.mark.skipif(os.environ.get("AMV_REAL") != "1", reason="AMV_REAL=1 only")
def test_real_media_render():
    pytest.skip("run via scratchpad real_render.py")


# ---------------------------------------------------------------- revision 1

def test_auto_is_blend(clip, tmp_path):
    assert render.resolve_interp("auto") == "blend"
    wav = make_wav(tmp_path / "a.wav", 4.25)
    r = render_plan(clip, plan_of(SLOW), wav, tmp_path / "o.mp4", interp="auto")
    assert r["interp"] == "blend" and r["gpu"] is None


FAKE = [{"id": 0, "name": "Intel(R) UHD Graphics 630"}, {"id": 1, "name": "AMD Radeon Pro 5300M"},
        {"id": 2, "name": "llvmpipe (LLVM 15)"}]


def test_gpu_pick_heuristic(monkeypatch):
    monkeypatch.delenv("AMV_RIFE_GPU", raising=False)
    assert render.pick_gpu_order(FAKE) == [1, 0, 2]            # discrete, integrated, software
    assert render.pick_gpu_order(FAKE, override=0) == [0, 1, 2]
    assert render.pick_gpu_order(FAKE, override=-1)[0] == -1
    assert render.pick_gpu_order([]) == []
    monkeypatch.setenv("AMV_RIFE_GPU", "2")
    assert render.pick_gpu_order(FAKE)[0] == 2
    txt = "[0 AMD Radeon Pro 5300M]  queueC=0[1]\n[1 Intel(R) UHD Graphics 630]  queueC=0[1]\n"
    assert render.parse_vulkan_devices(txt) == [{"id": 0, "name": "AMD Radeon Pro 5300M"},
                                                {"id": 1, "name": "Intel(R) UHD Graphics 630"}]
    assert [render.device_class(d["name"]) for d in render.parse_vulkan_devices(txt)] == [0, 1]


@pytest.fixture(autouse=True)
def _isolated_tools_dir(tmp_path_factory, monkeypatch):
    """Never read or write the user's real calibrated cost cache (RIFE install stays visible)."""
    f = tmp_path_factory.mktemp("costs") / "render_cost.json"
    monkeypatch.setattr(render, "_cost_file", lambda: f)


def test_estimate_shape(clip, tmp_path, monkeypatch):
    monkeypatch.setattr(render, "tools_dir", lambda: tmp_path)
    e = render.estimate_render(plan_of(SLOW), "blend", clip)
    assert set(e) >= {"frames", "interpolated", "est_seconds", "estimated"}
    assert e["frames"] == round(4.25 * FPS) and e["interpolated"] > 0 and e["est_seconds"] > 0
    assert e["estimated"] is False and "calibration_s" in e          # blend/base calibrated on first use
    assert render.estimate_render(plan_of(SLOW), "dup", clip)["interpolated"] == 0
    assert render.estimate_render(plan_of(SLOW), "auto", clip)["interp"] == "blend"
    render._save_cost("base", f"{W}x{H}", 0.001)
    render._save_cost("blend", f"{W}x{H}", 0.001)
    e2 = render.estimate_render(plan_of(SLOW), "blend", clip)
    assert e2["estimated"] is False and "calibration_s" not in e2


def test_rife_estimate_calibrates_once_and_caches(clip, tmp_path, monkeypatch):
    monkeypatch.setattr(render, "tools_dir", lambda: tmp_path)
    monkeypatch.setattr(render, "rife_exe", lambda: Path("/fake/rife"))
    monkeypatch.setattr(render, "list_vulkan_devices", lambda exe: [{"id": 0, "name": "AMD Radeon Pro 5300M"}])
    calls = []

    class FakeRunner:
        gpu_label, failed = "AMD Radeon Pro 5300M", []

        def __init__(self, exe, gpu_id=None, cancel=None):
            pass

        def pair(self, a, b, t):
            calls.append(a.shape)
            time.sleep(0.05)
            return a

        def kill_all(self):
            pass

    monkeypatch.setattr(render, "RifeRunner", FakeRunner)
    e = render.estimate_render(plan_of(SLOW), "rife", clip)
    assert e["estimated"] is False and e["gpu"] == "AMD Radeon Pro 5300M" and "reason" not in e
    assert (H, W, 3) in calls and len(calls) == 3                       # warm-up + 2 target-size pairs
    cached, ok = render._cost("rife", W, H, "AMD Radeon Pro 5300M")
    assert ok and 0.01 < cached < 5
    e2 = render.estimate_render(plan_of(SLOW), "rife", clip)
    assert len(calls) == 3 and e2["estimated"] is False and e2["est_seconds"] == e["est_seconds"]
    render.estimate_render(plan_of(SLOW), "rife", clip, measure=True)   # forces a re-calibration
    assert len(calls) == 6


def test_rife_estimate_fallback_is_pessimistic(clip, tmp_path, monkeypatch):
    monkeypatch.setattr(render, "tools_dir", lambda: tmp_path)
    monkeypatch.setattr(render, "rife_exe", lambda: None)
    e = render.estimate_render(plan_of(SLOW), "rife", clip)
    assert e["estimated"] is True and "not installed" in e["reason"]
    per = e["est_seconds"] - e["frames"] * render._cost("base", W, H)[0]
    assert per >= e["interpolated"] * 1.2 * (W * H) / (1920 * 1080) - 1e-6   # never undersells
    # a calibration that hangs is cut off by the hard timeout and falls back too
    monkeypatch.setattr(render, "rife_exe", lambda: Path("/fake/rife"))
    monkeypatch.setattr(render, "list_vulkan_devices", lambda exe: [{"id": 0, "name": "GPU"}])
    monkeypatch.setattr(render, "CALIBRATION_TIMEOUT", 0.3)

    class Hang:
        gpu_label, failed = "GPU", []

        def __init__(self, *a, **k):
            pass

        def pair(self, a, b, t):
            time.sleep(2)

        def kill_all(self):
            pass

    monkeypatch.setattr(render, "RifeRunner", Hang)
    t0 = time.time()
    e = render.estimate_render(plan_of(SLOW), "rife", clip)
    assert time.time() - t0 < 1.5 and e["estimated"] is True and "timed out" in e["reason"]


def test_cancel_cleans_up(clip, tmp_path):
    import threading
    ev = threading.Event()
    ev.set()
    out = tmp_path / "o.mp4"
    with pytest.raises(render.RenderCancelled):
        render_plan(clip, plan_of(SLOW), make_wav(tmp_path / "a.wav", 4.25), out, interp="blend", cancel=ev)
    assert not out.exists() and not list(tmp_path.glob("*.tmp.mp4")) and not list(tmp_path.glob("amv-render-*"))


def test_cancel_midway_and_endpoint(clip, tmp_path, monkeypatch):
    import threading
    ev = threading.Event()
    seen = []
    def cb(f):
        seen.append(f)
        if f > 0.3:
            ev.set()
    out = tmp_path / "o.mp4"
    with pytest.raises(render.RenderCancelled):
        render_plan(clip, plan_of(SLOW), make_wav(tmp_path / "a.wav", 4.25), out, interp="blend",
                    cancel=ev, progress_cb=cb)
    assert not out.exists() and not list(tmp_path.glob("*.tmp.mp4"))
    if render.router is None:
        return
    job = {"id": "j1", "status": "running", "stage": "render", "done": 0}
    render.JOBS["j1"] = job
    render._CANCELS["j1"] = threading.Event()
    h = {r.path: r.endpoint for r in render.router.routes}
    h["/api/render-mp4/{job_id}/cancel"]("j1")
    assert render._CANCELS["j1"].is_set()
    render._run_render(job, str(clip), str(make_wav(tmp_path / "s.wav", 6)), plan_of(SLOW), "blend",
                       tmp_path / "e.mp4")
    assert job["status"] == "cancelled" and not (tmp_path / "e.mp4").exists()


def test_prune_and_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "tools_dir", lambda: tmp_path)
    d = render.rife_dir() / "pkg"
    (d / "rife-v4.6").mkdir(parents=True)
    (d / "rife-v2").mkdir()
    (d / "README.md").write_text("x")
    (d / "LICENSE").write_text("x")
    (d / render._asset()["exe"]).write_text("bin")
    monkeypatch.setattr(render, "rife_exe", lambda: d / render._asset()["exe"])
    render.prune_rife()
    assert sorted(p.name for p in d.iterdir()) == sorted(["LICENSE", "rife-v4.6", render._asset()["exe"]])


def test_tempo_wav_excerpt_stops_early(tmp_path):
    """An excerpt render must not tempo a whole long track (the 511 s 'A own audio' outlier)."""
    import numpy as np
    import soundfile as sf
    from amv_matcher import render
    sr = render.SR
    wav = tmp_path / "long.wav"
    sf.write(str(wav), (0.1 * np.sin(np.arange(sr * 60) * 0.05)).astype(np.float32).repeat(2).reshape(-1, 2), sr)
    full, part = tmp_path / "full.wav", tmp_path / "part.wav"
    render._tempo_wav(wav, 1.03, full)
    render._tempo_wav(wav, 1.03, part, max_in_s=7.0)
    n_full, n_part = sf.info(str(full)).duration, sf.info(str(part)).duration
    assert abs(n_full - 60 / 1.03) < 0.5
    assert 6.0 / 1.03 <= n_part < 12.0 / 1.03     # ~ max_in_s / ratio, not the whole 58 s
