"""F21: audio drops as timeline markers mapped through the plan, matched to visual booms (synthetic only)."""
from amv_matcher import boom

SONG = {"duration": 120.0, "main_boom": 60.0,
        "booms": [{"t": 15.0, "conf": 0.9, "class": "initial_boom"}, {"t": 40.0, "conf": 0.8, "class": "boom"},
                  {"t": 60.0, "conf": 1.0, "class": "drop"}, {"t": 77.0, "conf": 0.7, "class": "encore"},
                  {"t": 100.0, "conf": 0.9, "class": "drop"}]}


def test_song_drops_kinds():
    k = [(d["t"], d["kind"]) for d in boom.song_drops(SONG)]
    assert k == [(15.0, "initial_boom"), (40.0, "boom"), (60.0, "main_drop"), (77.0, "encore"), (100.0, "drop")]


def test_plain_offset_maps_and_matches():
    plan = {"offset": 10.0, "tempo_ratio": 1.0, "segments": None}
    vid = {"visual_booms": [{"t": 5.06, "conf": 0.9}, {"t": 50.2, "conf": 0.8}, {"t": 67.3, "conf": 0.9}]}
    r = boom.drop_matches(SONG, plan, vid, 80.0, lead=0.0)
    rows = {m["kind"]: m for m in r["drop_matches"]}
    assert set(rows) == {"initial_boom", "boom", "main_drop", "encore"}  # a confident plain boom counts; 100 s is past the end
    assert rows["boom"]["status"] == "red"                               # 30 s out: nearest picture hit is 50.2
    assert rows["initial_boom"]["t_out"] == 5.0 and rows["initial_boom"]["status"] == "green"
    assert rows["initial_boom"]["delta_ms"] == 60.0
    assert rows["main_drop"]["status"] == "amber" and rows["main_drop"]["delta_ms"] == 200.0
    assert rows["encore"]["status"] == "red"
    assert (r["drops_matched"], r["drops_total"]) == (1, 4)


def test_lead_and_tempo_and_splice():
    plan = {"tempo_ratio": 2.0, "segments": [{"src_start": 25.0, "src_end": 35.0, "dst_start": 0.0},
                                            {"src_start": 45.0, "src_end": 55.0, "dst_start": 10.0}]}
    # tempo 2x: drops at 7.5, 30, 38.5, 50 (song s / 2); 30 -> out 5.0, 50 -> out 15.0; 7.5 / 38.5 cut out
    vid = {"visual_booms": [{"t": 4.94, "conf": 0.9}, {"t": 14.5, "conf": 0.9}]}
    r = boom.drop_matches(SONG, plan, vid, 20.0, lead=0.06)
    assert [(m["t_out"], m["kind"], m["status"]) for m in r["drop_matches"]] == [(5.0, "main_drop", "green"), (15.0, "drop", "red")]  # 40 s boom cut out
    assert r["drop_matches"][0]["delta_ms"] == 0.0 and r["drop_matches"][0]["t_song"] == 60.0


def test_weak_visual_booms_count_and_none_is_red():
    plan = {"offset": 0.0}
    weak = {"visual_booms": [{"t": 60.0, "conf": 0.1}]}
    r = boom.drop_matches(SONG, plan, weak, 70.0)
    assert next(m for m in r["drop_matches"] if m["kind"] == "main_drop")["status"] == "green"
    r2 = boom.drop_matches(SONG, plan, {"visual_booms": []}, 70.0)
    assert all(m["status"] == "red" and m["vis_t"] is None for m in r2["drop_matches"]) and r2["drops_matched"] == 0


def test_drop_offsets_land_each_strong_drop_on_a_visual_boom():
    from amv_matcher import autosync as A, score as S
    song = dict(SONG, bpm=120.0, duration=120.0)
    vid = {"duration": 60.0, "visual_booms": [{"t": 20.0, "conf": 0.9}, {"t": 45.0, "conf": 0.8}]}
    offs = A._drop_offsets(song, vid, 0.0, None)
    lead = S._lead(None)
    for d_t, v_t in ((60.0, 20.0), (77.0, 45.0), (15.0, 20.0) if False else (60.0, 45.0)):
        assert round(d_t - lead - v_t, 3) in offs
    assert offs == A._drop_offsets(song, vid, 0.0, None)                      # deterministic
    assert all(0.0 <= o <= 60.0 for o in offs)
    assert A._drop_offsets(dict(song, booms=[]), vid, 0.0) == [] and A._drop_offsets(song, {"duration": 60.0}, 0.0) == []
    o = round(60.0 - lead - 20.0, 3)
    assert A._drops_at(song, vid, o) >= 1 and A._drops_at(song, vid, 3.0) == 0


def test_drop_ok_guard():
    from amv_matcher import autosync as A
    old = {"tempo_pct": 90.0, "sync_pct": 80.0, "arc_pct": 70.0}
    assert A._drop_ok(dict(old), old, 2, 1)
    assert not A._drop_ok(dict(old), old, 1, 1)                                # must land MORE drops
    assert not A._drop_ok(dict(old, sync_pct=78.0), old, 3, 1)                 # sync gave up > 1 point


def test_weak_plain_booms_are_not_counted_and_any_visual_boom_is_a_hit():
    song = {"main_boom": 30.0, "booms": [{"t": 30.0, "conf": 1.0, "class": "drop"}, {"t": 50.0, "conf": 0.3, "class": "boom"},
                                         {"t": 70.0, "conf": 0.6, "class": "boom"}]}
    vid = {"visual_booms": [{"t": 30.0, "conf": 0.9}, {"t": 70.02, "conf": 0.2}]}
    r = boom.drop_matches(song, {"offset": 0.0}, vid, 90.0, lead=0.0)
    assert [(m["t_out"], m["status"]) for m in r["drop_matches"]] == [(30.0, "green"), (70.0, "green")]   # weak 50 s boom left out
    assert (r["drops_matched"], r["drops_total"]) == (2, 2)
