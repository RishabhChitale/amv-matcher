"""F6e: precision/recall of detected booms vs bookmarks (timelinelog.evaluate, tools/boom_pr.py)."""
import json

from amv_matcher import timelinelog as TL


def _log():
    proj = {"offset": 0.0, "bookmarks": []}
    for tr, t, ty in [("audio", 10.0, "initial_boom"), ("audio", 30.0, "drop"), ("audio", 50.0, "encore"),
                      ("audio", 70.0, "perfect_sync_moment"), ("audio", 80.0, "other"),
                      ("video", 30.02, "visual_boom"), ("video", 90.0, "visual_boom")]:
        TL.add_bookmark(proj, tr, t, ty)
    song = {"booms": [{"t": 10.03, "class": "initial_boom"}, {"t": 30.01, "class": "drop"},
                      {"t": 50.2, "class": "encore"}, {"t": 70.04, "class": "boom"}, {"t": 99.0, "class": "drop"}],
            "main_boom": 30.01, "beat_times": []}
    video = {"duration": 120.0, "cut_times": [], "visual_booms": [{"t": 30.0, "kind": "flash", "conf": 1.0},
                                                                  {"t": 60.0, "kind": "flash", "conf": .5}]}
    return TL.build(proj, video=video, song=song, version="t", now="x")


def test_evaluate_strict():
    r = TL.evaluate(_log())
    a, v = r["audio"], r["video"]
    assert (a["gt"], a["detected"], a["hits"]) == (4, 5, 3)          # 'other' is not ground truth; 50.2 > 50 ms
    assert a["recall"] == 0.75 and a["precision"] == 0.6
    assert a["missed"] == [50.0] and a["main_hit"] is True
    assert (a["class_hits"], a["class_total"]) == (2, 2)
    assert (v["hits"], v["gt"], v["missed"]) == (1, 2, [90.0])


def test_evaluate_loose_class_confusion():
    r = TL.evaluate(_log(), audio_tol=0.25)
    assert r["audio"]["hits"] == 4 and r["audio"]["class_hits"] == 3


def test_one_to_one_matching():
    assert TL._match([1.0, 1.04], [1.02], 0.05) == [(0, 0, 0.02)]


def test_no_ground_truth_precision_none():
    lg = _log()
    lg["video"]["bookmarks"] = []
    assert TL.evaluate(lg)["video"]["precision"] is None


def test_cli_json_and_csv(tmp_path, capsys):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("boom_pr", Path(__file__).resolve().parents[1] / "tools" / "boom_pr.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    lg = _log()
    j = tmp_path / "log.json"
    j.write_text(json.dumps(lg))
    c = tmp_path / "log.csv"
    c.write_text(TL.to_csv(lg))
    assert m.main([str(j), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pooled"]["audio"]["hits"] == 3
    assert m.main([str(c), "--detected", str(j), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pooled"]["audio"]["hits"] == 3 and out["pooled"]["video"]["hits"] == 1
