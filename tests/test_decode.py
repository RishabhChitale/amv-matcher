"""Tests for amv_matcher.decode (fast: 320x180 synthetic clips)."""
import gc

import av
import numpy as np
import pytest

from amv_matcher import decode

W, H, FPS, N = 320, 180, 30, 60


def _make_clip(path, n=N, fps=FPS, pix_fmt="yuv420p", w=W, h=H):
    yy, xx = np.mgrid[0:h, 0:w]
    with av.open(str(path), "w") as c:
        s = c.add_stream("libx264", rate=fps)
        s.width, s.height, s.pix_fmt = w, h, pix_fmt
        s.options = {"preset": "ultrafast", "crf": "18", "g": "15"}
        for i in range(n):
            img = np.stack([(xx + i * 3) % 256, (yy + i * 2) % 256,
                            np.full_like(xx, (i * 4) % 256)], -1).astype(np.uint8)
            fr = av.VideoFrame.from_ndarray(img, format="rgb24")
            fr = fr.reformat(format=pix_fmt)
            for p in s.encode(fr):
                c.mux(p)
        for p in s.encode():
            c.mux(p)


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    p = tmp_path_factory.mktemp("dec") / "a.mp4"
    _make_clip(p)
    return p


def _all(path, **kw):
    with decode.open_frames(path, **kw) as fs:
        out = list(fs)
        return out, fs.info


def test_probe(clip):
    p = decode.probe_video(clip)
    assert (p["width"], p["height"]) == (W, H)
    assert abs(p["fps"] - FPS) < 0.01 and abs(p["duration"] - N / FPS) < 0.1
    assert p["codec"] == "h264" and p["frames"] == N
    assert isinstance(p["hw_capable"], bool)


def test_count_and_timestamps_vs_software_reference(clip):
    ref = []
    with av.open(str(clip)) as c:
        for f in c.decode(video=0):
            ref.append(float(f.time))
    out, info = _all(clip, hw="auto")
    assert abs(len(out) - len(ref)) <= 1
    for (t, _), r in zip(out, ref):
        assert abs(t - r) <= 1 / FPS
    assert info.frames_out == len(out)


def test_width_keeps_aspect_and_shape(clip):
    out, _ = _all(clip, width=160, hw="off")
    assert out[0][1].shape == (90, 160, 3) and out[0][1].dtype == np.uint8
    out, _ = _all(clip, width=100, height=50, hw="off")
    assert out[0][1].shape == (50, 100, 3)
    out, _ = _all(clip, hw="off")
    assert out[0][1].shape == (H, W, 3)


def test_gray_and_bgr_and_yuv(clip):
    g, _ = _all(clip, width=160, pix_fmt="gray", hw="off")
    assert g[0][1].shape == (90, 160)
    rgb, _ = _all(clip, width=160, pix_fmt="rgb24", hw="off")
    bgr, _ = _all(clip, width=160, pix_fmt="bgr24", hw="off")
    d = np.abs(rgb[5][1][..., ::-1].astype(int) - bgr[5][1].astype(int))
    assert d.mean() < 3     # same channel order up to swscale chroma-rounding
    y, _ = _all(clip, width=161, pix_fmt="yuv420p", hw="off")
    assert y[0][1].shape[1] % 2 == 0 and y[0][1].ndim == 2
    with pytest.raises(ValueError):
        decode.open_frames(clip, pix_fmt="nope")


def test_fps_resample(clip):
    out, _ = _all(clip, fps=10, hw="off")
    assert abs(len(out) - N / 3) <= 1
    ts = [t for t, _ in out]
    assert all(abs((b - a) - 0.1) < 1e-3 for a, b in zip(ts, ts[1:]))
    # no interpolation: every output is exactly a source frame
    full, _ = _all(clip, hw="off")
    src = [f for _, f in full]
    for t, img in out:
        k = int(round(t * FPS))
        assert any(np.array_equal(img, src[j]) for j in (k - 1, k, k + 1) if 0 <= j < len(src))
    # fps above source = passthrough
    up, _ = _all(clip, fps=120, hw="off")
    assert len(up) == len(full)


def test_start_end_window(clip):
    out, _ = _all(clip, start=0.5, end=1.5, hw="off")
    ts = [t for t, _ in out]
    assert ts and all(0.5 - 1e-6 <= t < 1.5 for t in ts)
    assert abs(ts[0] - 0.5) <= 1 / FPS
    assert abs(len(ts) - FPS) <= 1


def test_hw_off_reports_software(clip):
    out, info = _all(clip, hw="off")
    assert out and info.path == "software"


def test_auto_falls_back_when_device_factory_raises(clip, monkeypatch):
    ref, _ = _all(clip, width=160, hw="off")

    def boom(device):
        raise RuntimeError("no device")
    monkeypatch.setattr(decode, "_make_hwaccel", boom)
    monkeypatch.setattr(decode, "_auto_devices", lambda: ["videotoolbox"])
    monkeypatch.setattr(decode, "hwdevices_available", lambda: ["videotoolbox"])
    out, info = _all(clip, width=160, hw="auto")
    assert info.path == "software" and "no device" in (info.fallback_reason or "")
    assert len(out) == len(ref) and out[0][1].shape == ref[0][1].shape
    diff = np.mean([np.abs(a[1].astype(float) - b[1].astype(float)).mean() for a, b in zip(out, ref)])
    assert diff < 3
    # explicit unknown device also falls back
    out2, info2 = _all(clip, hw="d3d11va")
    assert info2.path == "software" and len(out2) == N


def test_real_hw_matches_software_if_available(clip):
    if "videotoolbox" not in decode.hwdevices_available():
        pytest.skip("no videotoolbox")
    hw, info = _all(clip, width=160, hw="videotoolbox")
    sw, _ = _all(clip, width=160, hw="off")
    assert len(hw) == len(sw)
    diff = np.mean([np.abs(a[1].astype(float) - b[1].astype(float)).mean() for a, b in zip(hw, sw)])
    assert diff < 6, diff          # hw colour conversion may differ slightly


def test_midstream_failure_fallback_each_frame_once(clip, monkeypatch):
    real = decode._raw_frames

    def flaky(path, device, start, last_t, info, fast, threaded, holder):
        n = 0
        for t, fr in real(path, None, start, last_t, info, fast, threaded, holder):
            if device and n == 23:
                raise RuntimeError("transfer failed")
            n += 1
            yield t, fr
    monkeypatch.setattr(decode, "_raw_frames", flaky)
    out, info = _all(clip, hw="videotoolbox")
    ts = [t for t, _ in out]
    assert info.fell_back_midstream and info.path == "software"
    assert len(ts) == N and len(set(round(t, 6) for t in ts)) == N
    assert ts == sorted(ts)
    ref, _ = _all(clip, hw="off")
    assert all(np.array_equal(a[1], b[1]) for a, b in zip(out, ref))


def test_10bit_clip_decodes_or_falls_back(tmp_path):
    p = tmp_path / "b10.mp4"
    try:
        _make_clip(p, n=20, pix_fmt="yuv420p10le")
    except Exception as e:  # encoder without 10-bit
        pytest.skip(f"no 10-bit encoder: {e}")
    out, info = _all(p, width=160, hw="auto")
    assert len(out) == 20 and out[0][1].shape == (90, 160, 3)
    # and with the hardware path forced to fail (mock), software still serves it
    import amv_matcher.decode as d
    orig = d._make_hwaccel
    d._make_hwaccel = lambda dev: (_ for _ in ()).throw(RuntimeError("codec unsupported"))
    try:
        out2, info2 = _all(p, width=160, hw="videotoolbox")
    finally:
        d._make_hwaccel = orig
    assert info2.path == "software" and len(out2) == 20


def test_close_releases_container_and_reopen(clip):
    fs = decode.open_frames(clip, hw="off")
    next(fs)
    assert fs._holder["c"] is not None
    fs.close()
    assert fs._holder["c"] is None
    with pytest.raises(StopIteration):
        next(fs)
    gen = decode.open_frames(clip, hw="off")
    for i, _ in enumerate(gen):
        if i == 3:
            break
    del gen
    gc.collect()
    out, _ = _all(clip, hw="off")
    assert len(out) == N


def test_fast_option_is_opt_in(clip):
    full, _ = _all(clip, hw="off")
    fast, _ = _all(clip, hw="off", fast=True)
    assert len(fast) <= len(full) and fast
