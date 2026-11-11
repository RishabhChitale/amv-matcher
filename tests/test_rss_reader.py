"""The Windows selftest RSS watch must be real: a reader that returns 0 or fails makes the check FAIL."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app_main  # noqa: E402

MB = 1048576


def test_tree_sums_descendants_only():
    rss = {10: 100 * MB, 11: 50 * MB, 12: 25 * MB, 99: 900 * MB}
    pm = lambda: {10: 1, 11: 10, 12: 11, 99: 5}   # 99 is unrelated
    r = app_main.measure_tree_rss(10, rss.__getitem__, pm)
    assert r == {"own_mb": 100.0, "tree_mb": 175.0, "nproc": 3}


def test_unreadable_child_is_skipped_but_counted_out():
    def rd(pid):
        if pid == 11:
            raise OSError("denied")
        return 10 * MB
    r = app_main.measure_tree_rss(10, rd, lambda: {11: 10, 12: 10})
    assert r["nproc"] == 2 and r["tree_mb"] == 20.0


def test_zero_or_failing_root_read_raises():
    with pytest.raises(ValueError):
        app_main.measure_tree_rss(10, lambda p: 0, lambda: {})
    with pytest.raises(OSError):
        app_main.measure_tree_rss(10, lambda p: (_ for _ in ()).throw(OSError("x")), lambda: {})


def test_parent_cycle_terminates():
    r = app_main.measure_tree_rss(1, lambda p: MB, lambda: {1: 2, 2: 1})
    assert r["nproc"] == 2


@pytest.mark.parametrize("bad", [
    {"samples": 0, "peak_mb": 0.0, "peak_tree_mb": 0.0, "nproc": 0, "error": ""},
    {"samples": 0, "peak_mb": 0.0, "error": "OSError: boom"},
    {"samples": 5, "peak_mb": 0.0, "peak_tree_mb": 0.0, "nproc": 1, "error": ""},
    {"samples": 5, "peak_mb": 700.0, "peak_tree_mb": 800.0, "nproc": 1, "error": ""},
    {"samples": 5, "peak_mb": 100.0, "peak_tree_mb": 3000.0, "nproc": 4, "error": ""},
])
def test_vacuous_or_runaway_fails(bad):
    with pytest.raises(AssertionError):
        app_main.check_rss_result(bad)


def test_good_result_passes_and_reports():
    msg = app_main.check_rss_result({"samples": 40, "peak_mb": 180.0, "peak_tree_mb": 520.0, "nproc": 6, "error": ""})
    assert "180" in msg and "6 process" in msg


@pytest.mark.skipif(sys.platform != "win32", reason="Windows readers")
def test_real_reader_nonzero_on_windows():
    import os
    assert app_main.measure_tree_rss(os.getpid())["own_mb"] > 1
