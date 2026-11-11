# Timeline markers (F7)

Every marker the UI may draw on the video track (V1), the audio track (A1) and the results list. It says what
each marker means, where its data comes from, a suggested colour, and a tooltip. The legend and tooltips
(gligar) and the bookmark/timeline UI (yanma) read this file. The analysis side (porygon) owns the fields.

Rules for all markers:

* **Bookmarks are the user's; detected markers are the app's guess.** A bookmark and a detected marker of the
  same kind share a hue. Bookmarks are filled shapes (as `static/js/bookmarks.js` already draws them);
  detected markers are thin ticks or outlines, so the two never look alike.
* **Times.** Video markers are in video time, which is V1 time. Audio markers are in song time. On A1 a marker
  sits where its audio plays: with a plain offset that is `timeline_t = song_t - offset` (the timeline-log CSV
  already gives `timeline_t`). With an auto-sync plan, move the marker with the audio it sits in, using the
  same song-to-timeline mapping the A1 waveform uses. A spliced song already carries its `booms` and
  `main_boom` in output time (`splice_song_booms`). With `plan.video_window` (song shorter than the video,
  F2), times in the plan are clip times: add `video_window.start_s` before drawing on the full video.
* **Confidence** `conf` is 0-1. Show it as a whole percentage ("conf 82%"). It is how sure the detector is
  that something happened, not how good the moment is. Song boom `conf` saturates at 1.0 for strong booms.
* **Colour tokens.** The `--mk-*` names are new; add them to `static/style.css` `:root` with the values
  given (the bookmark hues already in `bookmarks.js` are reused so a bookmark and its detected marker match).
* **Tooltip pattern:** `<Label> · <m:ss.mmm> · <one detail>`, for example `Drop · 1:00.020 · conf 100%`.
  Use `timelinelog.mmss` formatting (`m:ss.mmm`). Never put file names in a tooltip that might be exported
  or screenshotted for a review.

## Where the data is today

| Source | Has |
|---|---|
| `GET /api/scan/{id}` `video` (`server._video_info`) | `cut_times`, `segments`, `segments_struct`, `lulls`, `peak_segments`, `shots`, `energy_curve` |
| `GET /api/waveform?path=` (song) | `beat_times`, `beat_strengths`, `drops`, `lulls`, `sections`, `energy_curve` |
| `GET`/`POST /api/timeline-log` `format=json` | `video.detected` `{cuts, visual_booms, camera_peaks}`, `audio.detected` `{booms, main_boom, energy_drops, beats}`, and both tracks' `bookmarks` |
| project file (`.amvproj`) | `bookmarks`, `perfect_sync_example` |
| auto-sync result `final` | `boom` `{audio_t, video_t, vis_t, err_ms, matched, main}`, `boom_pct`, `video_use`; `plan.video_window` |

`booms`, `main_boom`, `visual_booms` and `camera_events` are **not** in `/api/scan` or `/api/waveform` yet.
Until they are, the UI reads them from the timeline-log JSON (`POST /api/timeline-log` works on an unsaved
project). Proposal (server routing): add `visual_booms` and `camera_events` to `_video_info`, and `booms`
plus `main_boom` to `/api/waveform`, with the field shapes below.

## Audio (A1)

| Marker | Meaning (one sentence) | Field | Colour | Tooltip |
|---|---|---|---|---|
| Main drop | The song's single strongest boom: the moment the whole edit should land on. | `main_boom` (song s); also the `booms[]` entry with `main: true` in the log | `--mk-drop` `#22d3ee`, taller tick + small label "MAIN" | `Main drop · 1:00.020 · conf 100%` |
| Drop | Bass and beat slam back in hard after a build or a quieter stretch; no build-up is required. | `booms[]` with `class: "drop"` | `--mk-drop` `#22d3ee` | `Drop · 1:39.890 · conf 74%` |
| Initial boom | The first time the groove or full beat comes in, before the first drop. | `booms[]` with `class: "initial_boom"` | `--mk-initial` `#ff6b81` | `Initial boom · 0:14.930 · conf 61%` |
| Encore | A drop that comes again after a break (a second or third chorus drop); no build-up is needed. | `booms[]` with `class: "encore"` | `--mk-encore` `#a78bfa` | `Encore · 1:17.280 · conf 88%` |
| Beat | One beat of the song's tempo grid, the lines cuts are scored against. | `beat_times` (song s); `beat_strengths` 0-1 | `--mk-beat` `rgba(229,231,235,.25)`; every 4th beat (bar) brighter | none (too many); the A1 header shows `bpm` |
| Lull | A quiet low-energy stretch of the song. | song `lulls` `[[start, end]]` (s) | `--mk-lull` `rgba(139,92,246,.12)` band | `Quiet stretch · 0:42.0-0:51.5` |
| Section | A labelled part of the song: intro, verse, pre, chorus, bridge, breakdown, drop, outro. | `sections[]` `{start, end, label, ...}` | existing section bands | `<Label> · 0:30.0-0:45.2` |

## Video (V1)

| Marker | Meaning (one sentence) | Field | Colour | Tooltip |
|---|---|---|---|---|
| Cut | A hard cut between shots. | `cut_times` (video s) | `--mk-cut` `rgba(229,231,235,.55)` thin tick | `Cut · 0:12.345` |
| Visual boom | A visual impact the song's booms should land on: a sudden burst of subject motion, a bright flash, a zoom punch or a whip pan. | `visual_booms[]` `{t, conf, kind, kinds?, strength}`; log: `{t, kind, conf}` | `--mk-visual` `#fbbf24` | `Visual boom · 1:47.500 · <kind> · conf 70%` |
| Camera whip | A very fast camera swing: it reads as a hit, so it can carry a drop. | `camera_events[]` with `kind: "whip"` `{start, end, speed, peak}`; log `camera_peaks[]` `kind: "whip"` | `--mk-camera` `#f97316`, short span from `start` to `end` | `Camera whip · 0:33.1-0:33.4 · speed 0.82` |
| Zoom punch | A sudden fast zoom in or out that acts as a hit. | `visual_booms[]` with `kind: "zoom_punch"` (or `"zoom_punch"` in `kinds`) | `--mk-visual` `#fbbf24` (it is a visual boom) | `Zoom punch · 0:58.200 · conf 64%` |
| Lull | A calm stretch of the video with little motion and few cuts. | video `lulls` `[[start, end]]` (s) | `--mk-lull` band | `Calm stretch · 2:01.0-2:09.5` |
| Peak | A short high-energy stretch of the video (fast motion or rapid cuts). | `peak_segments` `[[start, end]]` (s) | `--mk-peak` `rgba(34,211,238,.12)` band | `High energy · 1:00.0-1:04.6` |
| Build / calm / peak | The video's energy arc split into rising (build), calm and high (peak) parts. | `segments[]` `{start, end, label}` | existing energy bands | `Build · 0:40.0-0:59.8` |

Visual boom `kind` labels for the tooltip: `motion` "motion burst", `flash` "flash", `zoom_punch` "zoom punch",
`whip_pan` "whip pan". A merged boom lists all of them in `kinds`; show them joined with " + ".

## Results list and auto-sync

| Marker | Meaning (one sentence) | Field | Colour | Tooltip |
|---|---|---|---|---|
| Boom sync | Where the song's main drop lands on the video, and how far it is from the nearest visual boom. | result/`final` `boom` `{audio_t (song s), video_t (video s), vis_t, err_ms, matched}` | `--mk-drop` link line from A1 `audio_t` to V1 `video_t`; green when `matched` | `Main drop lands at 1:00.0 · 40 ms from a visual boom` (or `· no visual boom within 80 ms`; `boom.BOOM_MATCH_S` 0.08) |
| Video use (F2) | The song is shorter than the video and uses only this stretch of it. | `video_use` `{start_s, end_s, coverage_pct, label}` / `plan.video_window` | dim V1 outside `start_s`-`end_s` | the `label`, e.g. `uses 2:16 of 3:48 video` |

`boom_pct` is shown in the score breakdown only. It is **not** part of `total_pct` (`boom.BOOM_IN_TOTAL` is
`False`), so never present it as a reason a song ranks higher.

## Bookmarks (user, both tracks)

Types and shapes are fixed by `timelinelog.BOOKMARK_TYPES` and `static/js/bookmarks.js`:

| Type | Meaning | Colour / shape |
|---|---|---|
| `drop` | The user marks a drop (audio) or the main drop | `#22d3ee` diamond |
| `initial_boom` | The user marks the first groove/beat entry | `#ff6b81` burst |
| `encore` | The user marks a drop that returns after a break | `#a78bfa` triangle |
| `visual_boom` | The user marks a visual impact (video) | `#fbbf24` square |
| `perfect_sync_moment` | The user says sound and picture hit together perfectly here | `#4ade80` star |
| `other` | Anything else; the note says what | `#e5e7eb` ring |

Tooltip: `<Type label> (yours) · <m:ss.mmm> · <note, first 60 chars>`.

## Not shown (no clear meaning to a user)

* `booms[]` with `class: "boom"` (unclassified): not a drop, initial boom or encore. Draw only in a debug view.
* `drops` from `/api/waveform` and `energy_drops` in the log: the old energy-jump detector, superseded by
  `booms`. Showing both would draw two different "drops" for the same song.
* `camera_events` of kind `pan`, `tilt` and `zoom` (slow moves): they are spans, not hits, and the camera
  sync events are off in scoring (`CAM_EVENT_WEIGHT` 0). They may be a faint background band at most.
* `motion_peaks`: hundreds per video, mostly small; the cut/visual-boom markers carry the useful part.
* `onset_env`, `beat_grid` internals, `motion_tempo_candidates`, `shots`, `segments_struct`: analysis
  internals with no single point a user could check.
* Song build-ups (`buildup.song_buildups`): computed inside scoring and not in any API response yet. Add a
  marker only once a field exists.
