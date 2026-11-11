"""Background frame prefetch in video.analyze_video: same order, errors re-raised, clean stop (no media)."""
import threading
import time

import pytest

from amv_matcher import video


def test_prefetch_keeps_order():
    assert list(video._prefetch(iter(range(500)), depth=4)) == list(range(500))


def test_prefetch_reraises_decoder_error():
    def gen():
        yield 1
        yield 2
        raise RuntimeError("bad packet")
    out = []
    with pytest.raises(RuntimeError, match="bad packet"):
        for x in video._prefetch(gen(), depth=2):
            out.append(x)
    assert out == [1, 2]


def test_prefetch_close_stops_and_closes_source():
    closed = threading.Event()

    def gen():
        try:
            i = 0
            while True:
                yield i
                i += 1
        finally:
            closed.set()
    it = video._prefetch(gen(), depth=2)
    assert [next(it) for _ in range(5)] == [0, 1, 2, 3, 4]
    it.close()
    assert closed.wait(2.0)
    time.sleep(0.05)
    assert not any(t.name == "amv-video-decode" and t.is_alive() for t in threading.enumerate())


# --- threaded reformat (decode._converted) ----------------------------------------------------

def _synthetic_frames(n=40, w=320, h=180):
    import av
    import numpy as np
    rng = np.random.default_rng(3)
    out = []
    for i in range(n):
        img = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
        out.append((i / 30.0, av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuv420p")))
    return out


@pytest.mark.parametrize("threads", [2, 3])
def test_threaded_reformat_matches_serial(threads):
    from amv_matcher import decode
    src = _synthetic_frames()
    ser = list(decode._converted(iter(src), 160, 90, "bgr24", "AREA", 1))
    par = list(decode._converted(iter(src), 160, 90, "bgr24", "AREA", threads))
    assert [t for t, _ in par] == [t for t, _ in ser]
    assert all((a == b).all() for (_, a), (_, b) in zip(ser, par))


def test_threaded_reformat_close_early_stops_workers():
    from amv_matcher import decode
    it = decode._converted(iter(_synthetic_frames(60)), 160, 90, "bgr24", "AREA", 2)
    assert len([next(it) for _ in range(5)]) == 5
    it.close()
    assert not [t for t in threading.enumerate() if t.name.startswith("amv-reformat")]


def test_reformat_threads_env(monkeypatch):
    monkeypatch.delenv("AMV_REFORMAT_THREADS", raising=False)
    assert video._reformat_threads() == video._REFORMAT_THREADS == 1
    monkeypatch.setenv("AMV_REFORMAT_THREADS", "2")
    assert video._reformat_threads() == 2
    monkeypatch.setenv("AMV_REFORMAT_THREADS", "x")
    assert video._reformat_threads() == 1
