"""F17 motif rule (user, 2026-11-11): the edited song must not start out of nowhere.

"You can do whatever you like to the song to make it match, but it can't start out of nowhere (unless the song
file is just like that). Every song needs a motif."

A song start (offset o: song_time = video_time + o) is VALID when it is
  * the file's own start (o <= FILE_START_S; negative offsets play the file from its start after silence), or
  * a phrase boundary p (a section start, or a whole number of PHRASE_BARS bars after one, on a bar downbeat)
    from which the motif is heard within the first phrase:
      - p starts an intro-labelled section                                  -> "intro"
      - p lies in a motif section with at least MIN_HOOK_BARS bars of it left -> "hook"
      - a motif section starts within one phrase after p (a build into it)   -> "phrase N"
    and p is not inside a riser (between a build's start and its drop): a start there is a mid-build entry.
  The offset may sit up to PICKUP_BARS bars before p (the song opens on a pickup / fill into the phrase downbeat, as
  a hand edit does: video D's manual edit opens S2 about 1.4 beats before its first phrase) or POST_S after it.
  Sync is phase sensitive (the video's hits fix the offset to within a few tens of ms), so the pickup is what lets a
  motif start and an in-sync offset coexist; without it only videos whose hits happen to sit on bar multiples could
  start on a phrase.

The motif is the repeated section group (structure.py repeat_group) with the highest mean energy: the recurring
hook / riff. With no repeated group, the group of the highest-energy section. A song without a bar grid or
sections gets no gate (gate() returns None) and every start stays allowed, exactly as before.

Generic by construction: phrase length, the motif group and the riser spans come from each song's own analysis;
nothing is tuned to a particular pair. Light module (numpy + _memo only, no linalg) so the scan path can use it.
"""
from __future__ import annotations

import numpy as np

from ._memo import IdMemo

PHRASE_BARS = 8          # phrase / section boundaries are 8-bar multiples from a section start
MIN_HOOK_BARS = 4        # a start inside the hook needs at least this many bars of the hook after it
FILE_START_S = 0.05      # offsets up to this play the file from its own start
PICKUP_BARS = 1.0        # an offset may lead the phrase downbeat by up to one bar (a pickup / fill) ...
POST_S = 0.05            # ... or trail it by this much
SECTION_STARTS = True    # a section's own start (not the outro) is an entrance too: never mid-phrase. Video D's hand
                         # edit opens S2 on its verse, two phrases before the next hook, so the GT needs this.
RISER_IN_BARS = 0.5      # a start more than this many bars after a build's start ...
RISER_OUT_BARS = 1.0     # ... and more than this many bars before its drop is inside the riser (the last bar is a pickup)


def bar_edges(song: dict) -> np.ndarray:
    """Bar start times (same rule as structure._bar_edges, copied so this module stays light)."""
    per_bar = int(song.get("beats_per_bar", 4))
    grid = np.asarray(song.get("beat_grid") if song.get("beat_grid") is not None else [], dtype=float)
    if len(grid) >= 2 * per_bar:
        edges = grid[int(song.get("bar_phase", 0)) % per_bar::per_bar]
    else:
        bt = np.asarray(song.get("beat_times") if song.get("beat_times") is not None else [], dtype=float)
        edges = bt[::per_bar] if len(bt) >= 2 * per_bar else np.array([])
    if len(edges) < 2:
        return edges
    bar = float(np.median(np.diff(edges)))
    dur = float(song["duration"])
    pre = []
    t = edges[0] - bar
    while t > -0.25 * bar:
        pre.append(max(0.0, t))
        t -= bar
    edges = np.concatenate([pre[::-1], edges])
    if edges[-1] < dur - 0.5 * bar:
        edges = np.append(edges, edges[-1] + bar)
    return np.unique(np.round(edges, 4))


def motif_group(sections):
    groups = {}
    for s in sections:
        groups.setdefault(s.get("repeat_group"), []).append(float(s.get("energy") or 0.0))
    rep = {g: float(np.mean(e)) for g, e in groups.items() if len(e) >= 2}
    if rep:
        return max(sorted(rep, key=str), key=lambda g: rep[g])
    return max(sections, key=lambda s: float(s.get("energy") or 0.0)).get("repeat_group")


class Gate:
    """Valid song starts of one song: starts = [(p, label)] sorted by p (file start not included)."""

    def __init__(self, starts, file_label, bar):
        self.starts = starts
        self.file_label = file_label
        self.bar = bar
        self._p = np.array([p for p, _ in starts], dtype=float)
        self.pre = PICKUP_BARS * float(bar)

    def _hit(self, o: float) -> int:
        """Index of the phrase start o opens (the first p with p - pre <= o <= p + POST_S), or -1."""
        i = int(np.searchsorted(self._p, o - POST_S - 1e-9))
        return i if i < len(self._p) and o >= self._p[i] - self.pre - 1e-9 else -1

    def ok(self, o: float) -> bool:
        o = float(o)
        return o <= FILE_START_S + 1e-9 or self._hit(o) >= 0

    def mask(self, offs) -> np.ndarray:
        offs = np.asarray(offs, dtype=float)
        m = offs <= FILE_START_S + 1e-9
        for p in self._p:
            m |= (offs >= p - self.pre - 1e-9) & (offs <= p + POST_S + 1e-9)
        return m

    def points(self, lo: float, hi: float) -> list:
        """Valid starts p inside [lo, hi] (the downbeats themselves), ascending."""
        return [float(p) for p in self._p if lo - 1e-9 <= p <= hi + 1e-9]

    def label(self, o: float) -> str:
        o = float(o)
        if o <= FILE_START_S + 1e-9:
            return self.file_label
        i = self._hit(o)
        return self.starts[i][1] if i >= 0 else "mid-phrase"


def _build(song):
    secs = sorted(song.get("sections") or [], key=lambda s: float(s["start"]))
    edges = bar_edges(song)
    if len(edges) < 2 or not secs:
        return None
    bar = float(np.median(np.diff(edges)))
    if not bar > 0:
        return None
    mg = motif_group(secs)
    motif_starts = [float(s["start"]) for s in secs if s.get("repeat_group") == mg]
    try:
        from .buildup import _buildups_cached
        risers = [(float(b["start"]), float(b["drop"])) for b in _buildups_cached(song)]
    except Exception:
        risers = []

    def in_riser(p):
        return any(a + RISER_IN_BARS * bar < p < d - RISER_OUT_BARS * bar for a, d in risers)

    def snap(t):
        i = int(np.argmin(np.abs(edges - t)))
        return i, float(edges[i])

    found = {}
    for s in secs:
        s0, s1 = float(s["start"]), float(s["end"])
        lab = str(s.get("label", ""))
        b0, _ = snap(s0)
        k = 0
        while b0 + k * PHRASE_BARS < len(edges):
            bi = b0 + k * PHRASE_BARS
            p = float(edges[bi]) if k else s0     # a section start is a boundary even off the grid
            if p >= s1 - 0.5 * bar:
                break
            if p > FILE_START_S and not in_riser(p):
                name = None
                if k == 0 and lab.startswith("intro"):
                    name = "intro"
                elif s.get("repeat_group") == mg and (s1 - p) >= MIN_HOOK_BARS * bar - 1e-6:
                    name = "hook"
                elif any(p < m <= p + (PHRASE_BARS + 0.25) * bar for m in motif_starts):
                    name = f"phrase {bi // PHRASE_BARS + 1}"
                elif k == 0 and SECTION_STARTS and not lab.startswith("outro"):
                    name = lab.split(" (")[0] or "section"
                if name is not None:
                    found.setdefault(round(p, 4), name)
            k += 1
    first = secs[0]
    if str(first.get("label", "")).startswith("intro"):
        file_label = "intro"
    elif first.get("repeat_group") == mg:
        file_label = "hook"
    else:
        file_label = "phrase 1"
    return Gate(sorted(found.items()), file_label, bar)


_GMEMO = IdMemo(64)


def gate(song: dict):
    """The song's start gate (Gate), or None when the song has no bar grid / sections (no constraint)."""
    if not isinstance(song, dict) or not song.get("sections"):
        return None
    objs = (song.get("sections"), song.get("beat_grid"), song.get("beat_times"), song.get("drops"),
            song.get("energy_curve"))
    extra = (float(song.get("duration") or 0.0), int(song.get("beats_per_bar", 4)), int(song.get("bar_phase", 0)))
    key, hit = _GMEMO.get(objs, extra)
    if hit is not None:
        return hit[0]
    return _GMEMO.put(key, objs, (_build(song),))[0]


def starts_on(song: dict, offset: float):
    """'intro' / 'hook' / 'phrase N' / 'mid-phrase' for a song start, or None when the song has no gate."""
    g = gate(song)
    return None if g is None else g.label(offset)
