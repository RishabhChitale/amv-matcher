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
