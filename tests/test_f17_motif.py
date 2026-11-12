"""F17 motif rule: an auto-synced song never starts out of nowhere.

User (2026-11-11): "You can do whatever you like to the song to make it match, but it can't start out of nowhere
(unless the song file is just like that). Every song needs a motif."

A valid start is the file's own start, or a phrase boundary from which the motif (the repeated hook) is heard within
the first phrase. Synthetic songs only (no media).
"""
import numpy as np
import pytest

from amv_matcher import motif
from amv_matcher import score as S
from amv_matcher import autosync

HZ = 10
BPM = 120.0
BAR = 2.0
PH = 8 * BAR            # one phrase = 16 s


def make_song(layout=("intro", "verse", "hook", "verse", "hook", "outro"), energy=None):
    """Sections of one phrase each; 'hook' sections are the repeated, loudest group (the motif)."""
    energy = energy or {"intro": 0.2, "verse": 0.4, "hook": 0.95, "outro": 0.3}
    group = {"intro": "I", "verse": "V", "hook": "H", "outro": "O"}
    dur = len(layout) * PH
    bt = np.arange(0, dur, 60.0 / BPM)
    en = np.zeros(int(dur * HZ))
    sections, drops = [], []
    for k, lab in enumerate(layout):
        a, b = k * PH, (k + 1) * PH
        en[int(a * HZ):int(b * HZ)] = energy[lab]
        if lab == "hook":
            drops.append(a)
        sections.append({"start": a, "end": b, "label": lab, "energy": energy[lab], "repeat_group": group[lab]})
    return {"bpm": BPM, "beat_times": bt.tolist(), "beat_grid": bt.tolist(), "beat_strengths": [1.0] * len(bt),
            "duration": dur, "energy_curve": en.tolist(), "energy_hz": HZ, "drops": drops, "lulls": [],
            "sections": sections, "beats_per_bar": 4, "bar_phase": 0}


def make_video(duration=40.0, peaks=((10.0, 18.0),)):
    en = np.full(int(duration * HZ), 0.1)
    cuts = []
    for a, b in peaks:
        en[int(a * HZ):int(b * HZ)] = 0.95
        cuts += list(np.arange(a, b, 0.5))
    return {"duration": duration, "fps": 30, "energy_curve": en.tolist(), "energy_hz": HZ,
            "segments": [], "lulls": [], "peak_segments": [list(p) for p in peaks],
            "shots": [], "cut_times": [float(c) for c in cuts], "motion_curve": [],
            "motion_peaks": [float(c) for c in cuts], "motion_tempo_bpm": 120.0}


def test_gate_starts_on_intro_and_hook_phrases_never_mid_phrase():
    song = make_song()
    g = motif.gate(song)
    assert g is not None
    pts = g.points(0.0, song["duration"])
    # verse phrases lead into the hook (one phrase later), hooks are the motif; the outro leads nowhere
    assert pts == [PH, 2 * PH, 3 * PH, 4 * PH]
    assert g.label(0.0) == "intro" and g.label(2 * PH) == "hook" and g.label(4 * PH) == "hook"
    assert g.label(PH).startswith("phrase ")
    for o in (3.0, PH + 3.0, 2 * PH + 5.0, 2 * PH + BAR, 5 * PH, 5 * PH + 4.0):
        assert not g.ok(o), o                              # mid-phrase, weak bar, or a start that leads nowhere
        assert g.label(o) == "mid-phrase"
    assert g.ok(2 * PH - 0.1) and g.ok(2 * PH + 0.04)      # pickup / tiny trail around the downbeat
    assert g.ok(2 * PH - 1.5) and g.label(2 * PH - 1.5) == "hook"   # a pickup bar into the hook
    assert not g.ok(2 * PH - BAR - 0.3)                    # more than a bar early is the previous phrase
    assert g.ok(-1.5)                                      # silence then the file's own start


def test_section_start_is_an_entrance_but_not_its_inner_phrases():
    """A long verse may open the song at its own start (video D's hand edit opens S2 on its verse), but the phrase in
    the middle of the verse, two phrases before the hook, is still mid-song nowhere; the outro start never counts."""
    song = make_song(layout=("hook", "verse", "verse", "verse", "hook", "outro"))
    secs = song["sections"]
    song["sections"] = [secs[0], dict(secs[1], end=secs[3]["end"]), secs[4], secs[5]]
    g = motif.gate(song)
    assert g.ok(PH) and g.label(PH) == "verse"
    assert not g.ok(2 * PH)                                 # inner verse phrase, hook two phrases away
    assert g.ok(3 * PH) and g.label(3 * PH).startswith("phrase ")   # the phrase leading into the hook
    assert not g.ok(5 * PH)                                 # outro


def test_abrupt_file_start_stays_allowed():
    """A file that itself starts on the hook (no intro) may start at 0: 'unless the song file is just like that'."""
    song = make_song(layout=("hook", "verse", "hook", "outro"))
    g = motif.gate(song)
    assert g.ok(0.0) and g.label(0.0) == "hook"
    assert not g.ok(5.0)


def test_no_sections_no_gate():
    song = make_song()
    song.pop("sections")
    assert motif.gate(song) is None and motif.starts_on(song, 3.0) is None


def test_gate_memo_follows_the_song():
    a, b = make_song(), make_song(layout=("hook", "verse", "hook", "outro"))
    assert motif.gate(a) is motif.gate(a)
    assert motif.gate(a).file_label == "intro" and motif.gate(b).file_label == "hook"


@pytest.mark.parametrize("peaks", [((10.0, 18.0),), ((3.0, 9.0), (25.0, 33.0)), ((21.0, 30.0),)])
def test_score_offset_is_a_valid_start(peaks):
    song, video = make_song(), make_video(peaks=peaks)
    r = S.score(song, video)
    g = motif.gate(song)
    assert g.ok(r["best_offset_s"]), r["best_offset_s"]
    assert r["starts_on"] == g.label(r["best_offset_s"]) != "mid-phrase"


def test_score_without_gate_unchanged(monkeypatch):
    """No sections / no bar grid: the scan is exactly the pre-F17 one."""
    song, video = make_song(), make_video()
    song.pop("sections")
    a = S.score(song, video)
    monkeypatch.setattr(motif, "gate", lambda s: None)
    b = S.score(song, video)
    assert a["best_offset_s"] == b["best_offset_s"] and a["total_pct"] == b["total_pct"]
    assert a["starts_on"] is None


def test_autosync_offset_helpers_respect_the_gate():
    song, video = make_song(), make_video(peaks=((3.0, 9.0), (25.0, 33.0)))
    g = motif.gate(song)
    o = autosync._best_offset(song, video)
    assert g.ok(o)
    o2 = autosync._best_offset(song, video, center=2 * PH + 5.0, half=1.0, step=0.05)
    assert g.ok(o2)                                         # a window with no valid start falls back to one
    for a in autosync._alt_offsets(song, video, [o]):
        assert g.ok(a)
    for b in autosync._boom_offsets(song, video, o):
        assert g.ok(b)


def test_splice_and_arrange_first_segment_is_a_valid_start():
    from amv_matcher.arrange import plan_arrangement
    from amv_matcher.splice import plan_splice
    song = make_song(layout=("intro", "verse", "hook", "verse", "hook", "verse", "hook", "outro"))
    video = make_video(duration=60.0, peaks=((4.0, 12.0), (30.0, 40.0)))
    g = motif.gate(song)
    sp = plan_splice(song, video, 3)
    if sp.get("segments"):
        assert g.ok(sp["segments"][0]["src_start"]), sp["segments"][0]
    ar = plan_arrangement(song, video)
    if ar.get("segments"):
        assert g.ok(ar["segments"][0]["src_start"]), ar["segments"][0]


def test_autosync_final_carries_starts_on():
    song, video = make_song(), make_video(peaks=((10.0, 18.0),))
    r = autosync.auto_sync(song, video)
    assert r["final"]["starts_on"] == autosync.plan_starts_on(song, r["plan"])
    assert r["final"]["starts_on"] is not None and r["final"]["starts_on"] != "mid-phrase"
