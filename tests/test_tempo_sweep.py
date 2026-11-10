import numpy as np

from amv_matcher import score as S
from amv_matcher.tempo import RATIO_MAX, RATIO_MIN, best_tempo
from tests.test_retime import make_song, make_video


def test_sweep_finds_plus_4_percent():
    song = make_song(60.0, 120.0)
    cuts = np.arange(0.5, 28.0, 0.5 / 1.04)
    vid = make_video(cuts, duration=28.0, tempo=124.8)
    base = S.score(song, vid)
    bt = best_tempo(song, vid, base)
    assert abs(bt["best_tempo_ratio"] - 1.04) <= 0.011
    assert bt["best_tempo_total_pct"] >= base["total_pct"] and bt["tempo_gain_pct"] >= 0
    assert "+4% speed" in bt["best_tempo_label"] or "+3% speed" in bt["best_tempo_label"] or "+5% speed" in bt["best_tempo_label"]


def test_sweep_exact_fit_stays_at_1():
    song = make_song(60.0, 120.0)
    vid = make_video(np.arange(0.5, 28.0, 0.5), duration=28.0, tempo=120.0)
    bt = best_tempo(song, vid, S.score(song, vid))
    assert bt["best_tempo_ratio"] == 1.0 and bt["tempo_gain_pct"] == 0
    assert bt["best_tempo_label"] == "fits best at 1.00x"


def test_range_is_ten_percent():
    assert (RATIO_MIN, RATIO_MAX) == (0.90, 1.10)
