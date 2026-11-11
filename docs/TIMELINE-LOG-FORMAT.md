# Timeline log format (F6d)

A **timeline log** is one JSON file (plus an optional readable CSV) describing one project's video
timeline and audio timeline: the user's **bookmarks** and the app's **detected events**. It is how
manual reviews travel between machines (e.g. Windows → Mac) and how detection is measured against
the user's ground truth (`tools/` P/R script, F6e).

**Privacy rule:** a log never contains file names, folder paths or the project name. Media are
identified only by duration, size and a sampled content hash.

Code: `amv_matcher/timelinelog.py` (schema, build / parse / validate, CSV writer + reader),
`amv_matcher/timelinelog_api.py` (endpoints). Tests: `tests/test_timelinelog.py`.

## Times

* Every `t` is **media time of its own track** in seconds (3 decimals): video time on the video
  track, song time on the audio track.
* Audio bookmarks also carry `timeline_t = t - offset` (the project's sync offset; song_t =
  video_t + offset), so both tracks can be read on one clock.
* Detected events are in media time too; the CSV adds `timeline_t` for every row.

## JSON (format_version 1)

```json
{
  "format": "amv-timeline-log",
  "format_version": 1,
  "app": {"name": "AMV Matcher", "version": "v0.9.0-12-gabc1234"},
  "exported_at": "2026-11-11T18:00:00+00:00",
  "project": {"perfect_sync_example": false, "offset": 0.0, "lead_s": null},
  "video": {
    "media": {"duration_s": 130.2, "size": 123456789, "hash": "<64 hex>",
              "hash_algo": "sha256-sample-3x1MiB", "fps": 23.976, "width": 1920, "height": 1080},
    "bookmarks": [
      {"id": "a1b2c3d4e5f6", "t": 15.56, "timeline_t": 15.56, "type": "visual_boom",
       "note": "first human movement", "created": "...", "modified": "..."}
    ],
    "detected": {
      "cuts": [1.042, 2.5],
      "visual_booms": [{"t": 15.56, "kind": "motion+flash", "conf": 0.81}],
      "camera_peaks": [{"t": 4.5, "start": 4.0, "end": 5.0, "kind": "pan", "speed": 0.4, "peak": 0.9}]
    }
  },
  "audio": {
    "media": {"duration_s": 130.2, "size": 22964268, "hash": "<64 hex>", "hash_algo": "sha256-sample-3x1MiB"},
    "role": "own",
    "bookmarks": [{"id": "...", "t": 90.0, "timeline_t": 90.0, "type": "drop", "note": "strong", "...": "..."}],
    "detected": {
      "booms": [{"t": 60.0, "class": "drop", "conf": 0.9, "build": null, "main": true}],
      "main_boom": 60.0,
      "energy_drops": [60.0, 99.4],
      "beats": {"bpm": 128.0, "count": 270, "first": 0.41, "last": 126.9, "median_interval": 0.4688}
    }
  }
}
```

Field notes:

| field | meaning |
|---|---|
| `app.version` | `AMV_APP_VERSION` env, else `git describe --tags --always --dirty`, else `unknown` |
| `project.perfect_sync_example` | the user marked this project as a perfect-sync example (training/eval positive) |
| `media.hash` | sha256 over the decimal size + three 1 MiB samples (start, middle, end); `hash_algo` names it |
| `media.size` / `hash` | `null` when the file was not reachable at export time |
| `audio.role` | `own` = the video's embedded audio, `song` = a separate song, `null` = no song in use |
| `detected` | `null` when that track has not been analysed yet (exports never start an analysis) |
| `booms[].class` | `drop`, `initial_boom` (groove/beat entry), `encore` (a drop again, may have no build-up), `boom` (unclassified) |
| `booms[].build` | detected build-up/riser info or `null`; a drop does **not** need one |
| `energy_drops` | the older energy-curve drop list (`curves.find_drops`), kept for comparison |

### Bookmarks

`{id, track, t, type, note, created, modified}` in the project file (`bookmarks` list, top level,
next to `perfect_sync_example`); in a log the `track` is implied by the section and `timeline_t`
is added.

* `track`: `video` | `audio`
* `type`: `drop` | `initial_boom` | `encore` | `visual_boom` | `perfect_sync_moment` | `other`
* `t` ≥ 0, finite; `note` ≤ 2000 characters (free text, may hold newlines); at most 5000 per project
* `id`: 12 hex characters, stable across export/import (merge matches on it)

## CSV

Readable, one row per event sorted by `timeline_t`. Leading `# key: value` lines hold the
identity (only at the top of the file):

```
# format: amv-timeline-log 1
# app_version: v0.9.0-12-gabc1234
# exported_at: 2026-11-11T18:00:00+00:00
# perfect_sync_example: false
# offset: 0.0
# video: duration_s=130.2 size=123456789 hash=<hex>
# audio: duration_s=130.2 size=22964268 hash=<hex>
track,source,type,t,timeline_t,mmss,conf,kind,end,id,note
video,detected,cut,1.042,1.042,0:01.042,,,,,
audio,bookmark,drop,90.0,90.0,1:30.000,,,,a1b2c3d4e5f6,strong
```

`source` is `bookmark` or `detected`; detected `type` is `cut`, `visual_boom`, `camera_peak`, or an
audio class (`drop`, `initial_boom`, `encore`, `boom`; `kind=main` marks the main boom).
Importing a CSV restores the bookmarks, the media identity and `perfect_sync_example`; detected rows
are informational and ignored on import.

## Endpoints

All behind the origin guard; `project` is an `.amvproj` path on this machine.

| method + path | body / query | result |
|---|---|---|
| `GET /api/timeline-log` | `?project=<path>&format=json\|csv` | the log (CSV as a download) |
| `POST /api/timeline-log` | `{data: <unsaved project>, format}` | same, for an unsaved project |
| `POST /api/timeline-log/import` | `{log: <object or JSON/CSV text>, format: json\|csv, project?: <path> \| data?: <project>, mode: replace\|merge}` | `{ok, bookmarks, perfect_sync_example, media_match: {video, audio}, data}`; with `project` the file is saved, with `data` the updated project comes back in `data` |
| `GET /api/project/bookmarks` | `?project=<path>[&track=video\|audio]` | `{bookmarks, types, perfect_sync_example}` |
| `POST /api/project/bookmarks` | `{project, track, t, type, note}` | `{ok, result: <bookmark>, bookmarks, perfect_sync_example}` |
| `PATCH /api/project/bookmarks/{id}` | `{project, track?, t?, type?, note?}` | same shape |
| `DELETE /api/project/bookmarks/{id}` | `?project=<path>` | same shape; 404 if unknown |
| `POST /api/project/perfect-sync` | `{project, value: bool}` | same shape |

`media_match.video/audio` is `true`/`false` when the log and the local file both have a hash, else
`null`: the UI should warn when a review was made on a different file. Errors: 400 for invalid
input (message says which field), 404 for a missing project file or bookmark.

## Versioning

Readers accept `format_version` ≤ their own and reject newer ones. New optional fields may be added
within version 1; renaming or changing a field's meaning bumps the version.
