"""v0.9.7: alternatives UI wiring (static/js/alt-plans.js, #as-alts card)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "js" / "alt-plans.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_card_and_script_present_after_autosync():
    assert 'id="as-alts"' in HTML
    assert HTML.index("/static/autosync.js") < HTML.index("/static/js/alt-plans.js")


def test_switch_swaps_plan_final_path_in_place():
    assert "run.result = Object.assign({}, run.result, {plan: e.plan, final: e.final, path: e.path" in JS
    assert "result.alternatives" in JS and "applyAfterSplice()" in JS and "renderAudio(" in JS


def test_hidden_without_alternatives_and_labels():
    assert "!(res.alternatives && res.alternatives.length)" in JS
    assert "Match the video's own edit" in JS and "Best-score plan" in JS


def test_ui_hook_exported_from_autosync_iife():
    a = (ROOT / "static" / "autosync.js").read_text(encoding="utf-8")
    assert "window.asUi = ui" in a and "window.asAfterUi" in a
    assert "window.asAfterUi = render" in JS and "\n  const _ui = ui" not in JS


def test_choose_clamps_negative_src_start_and_surfaces_errors():
    js = (ROOT / "static" / "js" / "alt-plans.js").read_text()
    assert "src_start < 0" in js and "src_start: 0" in js
    assert "preview audio render failed" in js


def test_render_error_shown_in_card():
    js = (ROOT / "static" / "js" / "alt-plans.js").read_text()
    assert "e.err = true" in js and "Preview audio unavailable for this plan" in js
