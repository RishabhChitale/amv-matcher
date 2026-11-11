"""F19: player audio must keep pitch and not churn playbackRate (static/app.js)."""
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")


def test_preserves_pitch_on_both_elements():
    for k in ("preservesPitch", "webkitPreservesPitch", "mozPreservesPitch"):
        assert f"m.{k} = true" in SRC


def test_steady_offset_fixed_by_one_seek_not_rate_nudge():
    assert "AUD.offSince" in SRC and "AUD.fixes" in SRC and "ad > 0.02" in SRC
    assert "0.97" not in SRC and "Math.sign(d) * 0.03" not in SRC
    assert "ad > 0.04 && ad <= 0.3" not in SRC


def test_rate_counter_exposed():
    assert "AUD.rates++" in SRC
