# AMV Matcher backend API

Base URL `http://127.0.0.1:8765` (local only). All JSON. Errors: `{"detail": "<message>"}` with 400 (bad input), 403 (path not allowed, or an Origin header that is not this app), 404 (unknown job / video not scanned / unknown static file), 409 (a render or auto-sync for the same pair is already running, or Demucs missing; the body also carries `job_id` for the running ones), 422 (validation failed), 501 (stems pack missing in a frozen build), 503 (background workers are disabled), 507 (not enough free disk for stems).
Paths are absolute local paths. `/api/audio`, `/api/waveform`, `/api/pump`, `/api/score` only accept songs inside a music folder that was passed to a scan; `/api/video` only accepts scanned videos.

**Offset convention:** `offset` (seconds) is the song time that sits under video time 0: song time = video time + offset. On the timeline the song clip starts at `-offset`. `trim_start_s = offset`, `trim_end_s = offset + video_duration`.

## Route index

"Guarded" = Origin-checked (403 if an Origin header is present and is not 127.0.0.1/localhost on the app or shell port).

| Method | Path | What it does | Owner |
|---|---|---|---|
| GET | `/` | serves `static/index.html` | server.py |
| GET | `/static/{name}` | static asset, 404 outside `static/` | server.py |
| POST | `/api/scan` | start scan of songs against a video (guarded) | server.py |
| GET | `/api/scan/{job_id}` | poll a scan / export job | server.py |
| GET | `/api/audio` | stream an allowed song | server.py |
| GET | `/api/video` | stream a scanned video (proxy if needed) | server.py |
| GET | `/api/waveform` | peaks, beats, drops, sections of a song | server.py |
| POST | `/api/score` | score one song at a manual offset (guarded) | server.py |
| GET | `/api/pump` | pump keyframes for a song | server.py |
| POST | `/api/upload` | upload a video or song (guarded) | server.py |
| GET | `/api/uploads` | list uploads and cache stats | server.py |
| DELETE | `/api/uploads` | delete uploads only, never exports (guarded) | server.py |
| DELETE | `/api/exports` | delete exports (renders) only, never uploads (guarded) | server.py |
| POST | `/api/register-paths` | allow native-picked files/folders in place (guarded) | server.py |
| POST | `/api/analyze-video` | analyze an uploaded video (guarded) | server.py |
| POST | `/api/score-songs` | score a list of songs against a video (guarded) | server.py |
| POST | `/api/splice` | plan or build a spliced arrangement (guarded) | server.py |
| POST | `/api/render-audio` | render the spliced wav (guarded) | server.py |
| GET | `/api/export-file` | download a file from `.exports/` | server.py |
| POST | `/api/export-mp4` | mux video with exported wav (guarded) | server.py |
| POST | `/api/export-resolve` | DaVinci Resolve bundle (guarded) | server.py |
| POST | `/api/render-mp4` | render synced mp4, one at a time (guarded; shadows render.py's) | server.py |
| POST | `/api/render-estimate` | estimate render time (guarded; shadows render.py's) | server.py |
| GET | `/api/render-mp4/{job_id}` | render / RIFE install job status | render.py |
| POST | `/api/render-mp4/{job_id}/cancel` | cancel a render | render.py |
| GET | `/api/render-tools` | RIFE availability | render.py |
| POST | `/api/render-tools/rife` | download and install RIFE | render.py |
| POST | `/api/auto-sync` | start auto-sync job (guarded) | server.py |
| GET | `/api/auto-sync/{job_id}` | poll auto-sync | server.py |
| POST | `/api/auto-sync/{job_id}/cancel` | cancel auto-sync (guarded) | server.py |
| POST | `/api/jobs` | submit a worker job (guarded) | server.py |
| GET | `/api/jobs` | job events since a sequence number | server.py |
| GET | `/api/jobs/stats` | worker pool counts | server.py |
| GET | `/api/jobs/{job_id}` | one worker job | server.py |
| POST | `/api/jobs/{job_id}/cancel` | cancel a worker job (guarded) | server.py |
| POST | `/api/jobs/{job_id}/bump` | change a job's priority (guarded) | server.py |
| POST | `/api/prewarm/pause` | pause background prewarm (guarded) | server.py |
| POST | `/api/prewarm/resume` | resume background prewarm (guarded) | server.py |
| POST | `/api/stems` | start stem separation (guarded) | stems_api.py |
| POST | `/api/stems/cancel` | cancel running separations (guarded) | stems_api.py |
| GET | `/api/stems/status` | stems state for a song | stems_api.py |
| GET | `/api/stem-audio` | one stem as wav | stems_api.py |
| GET | `/api/stem-features` | per-stem features | stems_api.py |
| POST | `/api/stems/install` | install Demucs (guarded) | stems_api.py |
| GET | `/api/stems/install` | Demucs install status | stems_api.py |
| POST | `/api/project/save` | save `.amvproj` (guarded) | project_api.py |
| POST | `/api/project/load` | load `.amvproj` (guarded) | project_api.py |
| GET | `/api/project/recent` | recent projects (guarded) | project_api.py |
| POST | `/api/project/relink` | find moved media (guarded) | project_api.py |
| POST | `/api/project/autosave` | write an autosave (guarded) | project_api.py |
| GET | `/api/media/roots` | list media roots (guarded) | medialib_api.py |
| POST | `/api/media/roots` | add a root (guarded) | medialib_api.py |
| DELETE | `/api/media/roots` | remove a root (guarded) | medialib_api.py |
| GET | `/api/fs/list` | list a folder inside a root (guarded) | medialib_api.py |
| GET | `/api/media/probe` | media metadata (guarded) | medialib_api.py |
| POST | `/api/media/link` | allow a media file in place (guarded) | medialib_api.py |
| GET | `/api/thumb` | jpeg thumbnail (guarded) | medialib_api.py |
| GET | `/api/filmstrip` | thumbnail urls across a video (guarded) | medialib_api.py |

render.py's own `POST /api/render-mp4` and `POST /api/render-estimate` exist but are shadowed by the validated server.py versions; its other routes are included with the Origin guard.

## POST /api/scan
Body: `{"video_path": str, "music_dir": str, "recursive": bool=false}`. Video ext: .mp4 .mov .mkv .webm .avi .m4v; songs: .mp3 .wav .m4a .flac.
Response: `{"job_id": "ae64f08cef79"}`

## GET /api/scan/{job_id}  (poll every ~400 ms)
`status`: running | done | error. `stage`: starting | video | songs. `video` appears once the video is analyzed (before songs finish). `results`/`too_short`/`skipped` are filled when `status=done` (`results` is null before). While `status=running` and `stage=songs`, `partial` lists the songs scored so far (same per-song shape as `results`, scan order, unranked, ineligible ones included; `done`/`total` give progress). It only grows, is emptied at `status=done`, and the final `results` are identical to a scan without streaming (same ranking), so a UI may show `partial` early but must replace it with `results`. Same for `/api/score-songs` jobs.
```json
{
  "id": "ae64f08cef79", "status": "done", "stage": "songs", "done": 3, "total": 3,
  "message": "Done: 3 ranked, 0 skipped", "error": null,
  "video_path": "/abs/path/jump.avi",
  "video": {
    "duration": 10.0, "fps": 30.0, "motion_tempo_bpm": 119.92,
    "energy_hz": 10.0,
    "energy_curve": [0.0, 0.125, 0.4],
    "cut_times": [0.5, 1.0],
    "cut_count": 19, "peak_count": 19,
    "segments": [{"start": 0.0, "end": 2.6, "label": "build"}],
    "lulls": [[6.0, 8.5]],
    "peak_segments": [[2.6, 3.0], [4.6, 5.0]],
    "shots": [{"start": 0.0, "end": 0.5, "mean_energy": 0.2, "peak_energy": 0.375, "motion_tempo_bpm": 0.0}]
  },
  "results": [
    {"rank": 1, "path": "/abs/music/click120.mp3", "name": "click120.mp3", "bpm": 120.04, "duration": 30.0,
     "eligible": true, "total_pct": 81, "tempo_pct": 100.0, "sync_pct": 100.0, "arc_pct": 53.4,
     "best_offset_s": 20.0, "trim_start_s": 20.0, "trim_end_s": 30.0, "best_rate": 1.0,
     "tightness": {"tight_pct": 86.7, "median_err_ms": 12.4, "p90_err_ms": 38.0, "n_events": 24, "grid": "beat", "chance_pct": 17.0, "lead_ms": 60,
     "raw": {"tight_pct": 70.0, "median_err_ms": 25.0, "p90_err_ms": 80.0, "grid": "beat", "chance_pct": 17.0}}}
  ],
  "too_short": [
    {"path": "/abs/music/short.wav", "name": "short.wav", "bpm": 100.0, "duration": 6.0,
     "eligible": false, "reason": "too_short", "song_duration": 6.0, "video_duration": 10.0,
     "total_pct": 0, "tempo_pct": 0.0, "sync_pct": 0.0, "arc_pct": 0.0,
     "best_offset_s": 0.0, "trim_start_s": 0.0, "trim_end_s": 10.0, "best_rate": 1.0,
     "tightness": {"tight_pct": 0.0, "median_err_ms": null, "p90_err_ms": null, "n_events": 0, "grid": "beat", "chance_pct": 0.0, "lead_ms": 60,
     "raw": {"tight_pct": 0.0, "median_err_ms": null, "p90_err_ms": null, "grid": "beat", "chance_pct": 0.0}}}
  ],
  "skipped": [{"path": "/abs/music/broken.mp3", "name": "broken.mp3", "reason": "NoBackendError: ..."}]
}
```
Tempo sweep (U9): each song result (not `source: original`) also carries `best_tempo_ratio` (song speed 0.90-1.10, step 0.01; 1.0 = natural fit is best), `best_tempo_total_pct` (total at that speed), `best_tempo_label` (e.g. "fits best at +4% speed") and `tempo_gain_pct` (best minus natural, >= 0). The sweep scales the cached analysis (no re-analysis) and runs lazily, only for the top 5 ranked songs (`TEMPO_SWEEP_TOP`); lower-ranked songs carry no `best_tempo_*` fields. Ranking stays on `natural_total_pct` (1.00x); a UI may offer `best_tempo_total_pct` as a second column / toggled sort. Auto-sync's `max_tempo_pct` limit is now 10.0 (was 8.0).

Notes: `results` = eligible songs only, ranked by the NATURAL total, `natural_total_pct` (the plain trim at the best offset, no auto-sync edits; equals `total_pct` here), ties by `sync_pct`. Each result also carries `auto_sync_cached` whose `natural_total_pct` is the same key after auto-sync. Same for `/api/score-songs`. `segments[].label` is calm | build | peak. `energy_curve` is 0-1 at `energy_hz`. `segments`/`lulls`/`peak_segments`/`shots` times are seconds. Segments overlap (1 s smoothing window); draw by `label`. `total_pct = round(0.20*tempo + 0.60*sync + 0.20*arc)` (constants in score.py). Higher `arc_pct` also counts peak-on-drop (±0.5 s) and lull-overlap bonuses.

**`tightness`** (every score output, same shape in scan results, `/api/score`, splice/autosync scoring; pure and cheap, `score.tightness(song, video, offset)`): non-saturating alignment of the video's locked events (cuts + motion peaks, same cut/peak weights as the sync score) to the song's beat grid at the reported offset. Unlike `sync_pct` it does not saturate near the top, so use it to see fine improvements in auto-sync.
- Main numbers are measured against the lead-compensated grid `beat_time - PERCEPTUAL_LEAD_S` (`lead_ms`, 60 ms: the median amount human editors cut before the beat, measured on two ground-truth AMVs). `tightness.raw` has the same fields (`tight_pct`, errors, `grid`, `chance_pct`) against the bare beat grid.
- The same lead is applied everywhere events are compared with beats (sync phase/coherence, fine term, phrase term, tightness). `score()`, `score_at()` and `tightness()` take `lead_s: float = None` (None = `PERCEPTUAL_LEAD_S`; clamped 0-0.12 s); `lead_s=0` reproduces bare-beat scoring.
- `tight_pct`: weighted % of events within ±40 ms of the grid.
- `median_err_ms`, `p90_err_ms`: median / 90th percentile of the absolute distance (ms) from an event to the nearest grid line.
- `n_events`: number of events used. `null` errors and 0 when there are no events or no grid.
- `grid`: `"beat"`, or `"half_beat"` when the edit locks at 8ths (the events' 2nd-harmonic coherence beats the beat's), in which case beats plus midpoints are the grid.
- `chance_pct`: tight_pct a random edit would get (80 ms / grid period), so compare `tight_pct` against it (the half-beat grid has twice the chance level).

## POST /api/score  (manual offset)
Body: `{"video_path": str, "song_path": str, "offset": float}` (video must already be scanned; offset is clamped to `0..song_duration-video_duration`).
```json
{"eligible": true, "total_pct": 74, "tempo_pct": 100.0, "sync_pct": 80.2, "arc_pct": 51.0,
 "best_offset_s": 12.3, "trim_start_s": 12.3, "trim_end_s": 22.3, "best_rate": 1.0,
 "tightness": {"tight_pct": 86.7, "median_err_ms": 12.4, "p90_err_ms": 38.0, "n_events": 24, "grid": "beat", "chance_pct": 17.0, "lead_ms": 60,
     "raw": {"tight_pct": 70.0, "median_err_ms": 25.0, "p90_err_ms": 80.0, "grid": "beat", "chance_pct": 17.0}}}
```
(`best_offset_s` here echoes the clamped offset you sent.)

## GET /api/video?path=
Streams the file with HTTP Range (`Range: bytes=0-1000` -> 206 + `Content-Range`). Only for a scanned video; 403 otherwise.

## GET /api/audio?path=
Same, for a song (Range supported, 206).

## GET /api/waveform?path=
```json
{"peaks": [-0.12, 0.13, -0.4, 0.38],
 "duration": 30.0, "bpm": 120.04,
 "beat_times": [0.5, 1.0, 20.5, 21.0, 24.4], "beat_strengths": [0.9, 0.4, 0.8, 0.5, 1.0],
 "drops": [24.4], "lulls": [[4.0, 6.5]],
 "energy_curve": [0.1, 0.2], "energy_hz": 10,
 "sections": [{"start": 0.0, "end": 16.0, "label": "intro", "energy": 0.31, "repeat_group": "A"},
              {"start": 16.0, "end": 32.0, "label": "chorus", "energy": 0.8, "repeat_group": "B"}]}
```
`sections` is the song structure (empty list if it could not be computed): contiguous, covering 0..duration, boundaries snapped to bar starts (downbeats via `bar_phase`). `label` is one of `intro|intro (hook)|verse|pre|chorus|bridge|breakdown|drop|outro` (`intro (hook)` = the song opens with chorus material; `outro` = the last section when it follows the final chorus occurrence or its energy falls >= 20%); sections with the same `repeat_group` ("A", "B", ...) are repeats of each other (similar chroma + timbre). `energy` is the mean 0-1 energy curve over the section. Found from a bar-synchronous self-similarity matrix (chroma + MFCC), a checkerboard novelty curve, and average-linkage clustering; labels come from energy, position and repetition (most repeated high-energy group = chorus). Sections longer than 16 bars are split at their strongest internal novelty peak (recursively). If separated stems are already cached for the song (never separated on demand), `/api/waveform` refines labels with them and adds `drums` / `vocals` (0-1 mean activity) to each section. Songs with fewer than 8 bars get one `verse` section. `peaks` is a flat list `[min0, max0, min1, max1, ...]`, 4000 pairs (8000 numbers) spanning the whole song, values -1..1. No raw audio is ever returned. `beat_strengths[i]` (0-1) belongs to `beat_times[i]`. `beat_times`, `drops` (the drop times) and `lulls` are absolute song time in seconds.

## GET /api/pump?path=&boom=40&decay=180&drops_only=false&fps=30&shake=false&start=0&duration=
`boom` 0-100 (0 = off), `decay` ms 50-400, `start`/`duration` select the song-time window (omit `duration` = whole song). Keyframe `t` is WINDOW-RELATIVE seconds (`t = i/fps`, t=0 at song time `start`; song time = `start + t`), which is what a clip-relative Resolve export wants.
```json
// example: start=20, duration=10 -> t runs 0..10 (song time 20..30)
{"fps": 30.0, "keyframes": [
  {"t": 0.0, "scale": 1.0, "brightness": 1.0, "dx": 0.0, "dy": 0.0},
  {"t": 0.5, "scale": 1.0108, "brightness": 1.0, "dx": 0.0, "dy": 0.0},
  {"t": 4.4, "scale": 1.048, "brightness": 1.35, "dx": 3.1, "dy": -2.4}
]}
```

### Shared pump formula (pump.py and the JS preview must match)
```
beat i at time b_i, strength s_i in 0..1 (a drop, or a beat within 0.15 s of a drop, uses s = 1 and is a "drop")
amp_i   = 0.03 * (boom/100) * s_i * (4 if drop else 1)
k_i(t)  = exp(-(t - b_i) / (decay_ms/1000))   for t >= b_i, else 0
scale(t)      = 1 + max_i amp_i * k_i(t)
brightness(t) = 1 + 0.35 * max over drops of k_i(t)       (peak 1.35; 1 when boom = 0)
shake (drops, shake=true): dx, dy = random sign * (2..6 px) * k_i(t)   (deterministic per drop/frame in Python)
drops_only=true ignores ordinary beats; boom = 0 -> scale 1.0 everywhere.
```

## Uploads (drag-and-drop)
Browsers can't hand over file paths, so dropped files are uploaded to the local server and stored under `.uploads/<kind>/` (git-ignored). Uploaded files are accepted anywhere a `video_path` / `song_path` is expected (`/api/audio`, `/api/video`, `/api/waveform`, `/api/pump`, `/api/score`, `/api/score-songs`).

**Origin guard:** every POST/DELETE endpoint returns 403 if an `Origin` header is present and is not `http://127.0.0.1:8765` or `http://localhost:8765`. Requests with no Origin (curl) pass.

### POST /api/upload?kind=video|song&name=<original filename>
Body: the raw file bytes (`Content-Type` ignored; use `fetch(url, {method:"POST", body: file})`). Streamed to disk and hashed (sha256); stored as `.uploads/<kind>/<sha12>-<sanitized name>`. The name is reduced to its basename and characters outside `A-Za-z0-9._ ()-` become `_`.
Limits/types: video 8 GB (`.mp4 .mov .mkv .webm .avi .m4v`), song 300 MB (`.mp3 .wav .m4a .flac .ogg .aac`).
```json
{"path": "/abs/amv-matcher/.uploads/song/3f2a9c01b7de-click120.wav", "name": "click120.wav",
 "size": 5292044, "kind": "song", "deduped": false}
```
`deduped: true` means identical bytes were already stored (same `path` returned). Errors: 400 bad kind / extension / empty body, 413 over the limit, 403 bad Origin.

### POST /api/score-songs
Body: `{"video_path": str, "song_paths": [str, ...]}`. The video must be a scanned or uploaded video; songs must be in a scanned music folder or `.uploads/`. Results have exactly the same shape as `/api/scan` (`results` ranked, `too_short`, `skipped`, `video`).
- **One song: synchronous.** The response is the finished job plus `job_id`: `{"job_id": "...", "status": "done", "results": [...], "too_short": [], "skipped": [], "video": {...}, ...}`.
- **Two or more songs: job + progress.** Response `{"job_id": "a1b2c3d4e5f6", "status": "running"}`; poll `GET /api/scan/{job_id}` (same object as a scan).
A client can handle both: if `status == "done"` use it, else poll.
- **Zero songs: video analysis only.** `song_paths: []` is allowed and returns synchronously `{"job_id", "status": "done", "video": {...}, "results": [...], "too_short": [], "skipped": []}`; `results` is `[]` for a video without audio, otherwise it holds just the **original audio** entry (see below).

### Original audio (v2)
If a video has an embedded audio track it is decoded (PyAV, no system ffmpeg) to `.cache/extracted/<sha12>.wav` and offered as a scoring source. `video` gains `has_audio: bool`, `original_audio: {path, name: "Original audio (<video name>)"}` (absent when no audio) and, if decoding failed, `audio_error`. Every entry in `results` / `too_short` / `skipped` carries `source: "original" | "song"`. The original is already aligned to the video, so it is scored at the fixed offset 0 (`best_offset_s: 0`) when its duration >= video duration - 0.5 s, otherwise it lands in `too_short`. It is ranked alongside the songs. Its `path` works in `/api/audio`, `/api/waveform`, `/api/splice`, `/api/render-audio`.

### POST /api/analyze-video
Body: `{"video_path": str}`. The video must be under `.uploads/` or a previously scanned video (else 403; bad Origin 403; undecodable video 422). Returns `{"video_path": "<resolved>", "video": {...}}` where `video` is the same object as the `video` section of a scan (`duration`, `fps`, `motion_tempo_bpm`, `cut_times`, `cut_count`, `energy_curve`, `energy_hz`, `segments`, `segments_struct`, `lulls`, `peak_segments`, `shots`, `peak_count`). `segments_struct` is a coarser phrase-level list `[{start, end, kind: calm|build|peak, energy}]` covering 0..duration, every block >= 2 s (shorter fine segments are merged into the neighbour with the closest energy). Synchronous; the result is cached in memory per video, so repeat calls are instant.

### GET /api/uploads
Totals cover `.uploads/` plus `.exports/`:
```json
{"count": 3, "bytes": 5392050, "uploads_count": 2, "uploads_bytes": 5292050, "exports_count": 1, "exports_bytes": 100000}
```

### DELETE /api/uploads
Deletes everything inside `.uploads/` ONLY; `.exports/` is never touched. `{"freed": 5292050, "uploads_freed": 5292050, "skipped": []}` (bytes; `skipped` lists locked files). Bad Origin: 403.

### DELETE /api/exports
Deletes everything inside `.exports/` ONLY (the user's rendered files); `.uploads/` is never touched. `{"freed": 100000, "exports_freed": 100000, "skipped": []}`. Bad Origin: 403. The UI's "Clear exports" button calls this after a confirmation naming the file count and size.

## Splice, render and export (v2)
All POSTs are Origin-guarded (403). Paths follow the usual rules (`video_path` scanned/uploaded video, `song_path` in a scanned folder, `.uploads/` or the extracted original audio).

### POST /api/splice
Body `{"video_path", "song_path", "max_cuts": 0-3 (default 2), "crossfade_ms": 40}` (`max_cuts` outside 0-3 -> 422). Synchronous, cached per (song, video, max_cuts). Returns `plan_splice()` verbatim:
```json
{"segments": [{"src_start": 12.0, "src_end": 18.0, "dst_start": 0.0}],
 "cuts": [{"dst_time": 6.0, "src_from": 18.0, "src_to": 40.0}],
 "total_pct": 84, "tempo_pct": 100.0, "sync_pct": 90.0, "arc_pct": 70.0,
 "baseline": {"offset": 20.0, "total_pct": 78}, "improvement_pct": 6,
 "bar_phase": "...", "objective": "..."}
```
`baseline` is the best single trim. If the song is shorter than the video the response is still 200 with `{"eligible": false, "reason": "too_short", ...}` and no `segments`. `bar_phase` and `objective` come straight from splice.py (see its docstring for their exact meaning).

### POST /api/render-audio
Body `{"video_path", "song_path", "segments": [{src_start, src_end, dst_start}, ...] (1-8), "crossfade_ms": 40}`. `video_path` is needed for the output length and file name. Segments are laid at `dst_start` with an equal-power crossfade centred on each join and 10 ms fades at the ends; the wav is exactly `round(video duration * 44100)` samples, 44.1 kHz stereo 16-bit, written to `.exports/<video>__<song>__<hash8>.wav`. 422 if a segment is outside the song/video or the count is bad.
```json
{"path": "/abs/amv-matcher/.exports/clip__song__1a2b3c4d.wav", "url": "/api/export-file?path=%2Fabs%2F...", "duration": 10.0}
```

### Render to mp4 (render.py router, wired in server.py)
`POST /api/render-mp4` body `{"video_path", "song_path", "plan": {auto-sync plan}, "interp": "auto"|"rife"|"blend"|"dup"}`. Origin-guarded (403); paths go through the usual allowlist (403 / 400 for an unparsable path); bad `interp` is 400. Only one render at a time: while one runs a second POST returns **409** `{"detail", "job_id"}` with the running job's id. Returns `{"job_id"}`. Output goes to `.exports/<video>__<song>__<jobid>.mp4`.
`GET /api/render-mp4/{job_id}` -> `{id, status: running|done|error, stage, done: 0-100, interp, path, url, size, seconds, error}` (`url` works with `/api/export-file`).
`GET /api/render-tools` -> `{"rife": {available, installed, tag, size_mb}}`; `POST /api/render-tools/rife` (origin-guarded) downloads + verifies RIFE, returns `{"job_id"}`, polled with the render GET.
`POST /api/render-mp4/{job_id}/cancel` -> `{job_id, status}` (404 unknown job). `POST /api/render-estimate` body `{video_path, plan, interp, measure, gpu_id}` -> `{frames, interpolated, est_seconds, estimated, interp, per_frame_s, gpu}`; `video_path` is checked with the allowlist (403), bad `interp` is 400. Both are Origin-guarded (403).

### GET /api/export-file?path=
Serves a file from `.exports/` only (403 for anything else) as a download (`Content-Disposition: attachment`).

### POST /api/export-mp4
Body `{"video_path", "audio_path"}` where `audio_path` is a `.wav` inside `.exports/` (else 403). Returns `{"job_id", "status": "running"}`; poll `GET /api/scan/{job_id}` (`done` is 0-100 progress). When `status == "done"` the job has `path`, `url` (use with `/api/export-file`) and `size`; on failure `status == "error"` with `error`. The video's own audio is dropped and replaced by the wav (AAC 192k). The video stream is copied untouched when its codec is h264/hevc/mpeg4/av1/vp9 (falling back to a re-encode if the copy fails), otherwise re-encoded to H.264 (yuv420p, crf 20). Output: `.exports/<video>__<song>.mp4`.

### POST /api/export-resolve
DaVinci Resolve bundle (free 20/21 target; **not tested in Resolve**, only structurally validated). Body `{"video_path", "song_path", "plan": {...}, "boom": 40, "decay": 180, "drops_only": false, "shake": false}`; `plan` is the auto-sync plan (`offset`, `tempo_ratio`, `segments`, `retime`) or a simple `{"offset": s}`. Origin-guarded; paths must pass the usual allowlist (403). Returns `{"job_id", "status": "running"}`; poll `GET /api/scan/{job_id}`. On `done` the job has `path` (folder `.exports/<video>__<song>_resolve/`), `zip`, `url` (zip, via `/api/export-file`), `clips`, `markers`, `out_duration`.
Folder: `timeline.fcpxml` (FCPXML 1.9; original video as one asset-clip per constant-speed section, speed via `<timeMap>`; song as audio clips, one per splice segment on alternating lanes with fadeIn/fadeOut crossfades; markers `beat` (<= 1 per bar), `DROP`, `CUT`), `AMV_Pump.drfx`, `AMV_Pump_plan.setting` (pump keyframes baked for this plan), `audio/` (song as heard; tempo-stretched wav when `tempo_ratio != 1`; relative paths), `HOW-TO-IMPORT.txt`. The video is referenced by absolute file URI (never copied).

## Native app shell (app_main.py)

### POST /api/register-paths
Body `{"paths": [...]}` (Origin-guarded). Native drops/picks: files are allowed in place (no copy into uploads); folders are expanded recursively, skipping dot-folders. Returns `{"videos": [...], "songs": [...]}` (resolved paths, by extension).

### Preview proxy
Videos a `<video>` tag cannot decode (mkv, MJPG avi, ...) get `preview_proxy: true` in the video info and are served by `GET /api/video` from a cached 540p H.264 mp4 (`proxy.py`, built in the background). Scoring/export always use the original file. `GET /api/uploads` also reports `proxy_count`, `proxy_bytes`, `frozen`.

Env (dev aids): `AMV_DATA_DIR` data folder; `AMV_SNAPSHOT=<png>`, `AMV_TEST_DROP=<paths>`, `AMV_EXIT_AFTER_SNAPSHOT=1`. `python app_main.py --selftest` runs a headless end-to-end check.

## Section arrangement (Python API, `amv_matcher/arrange.py`)

`plan_arrangement(song, video, max_moves=3, lead_s=None)` rearranges whole song sections so the
energy shape follows the video's calm/build/peak structure. Returns `segments`
(`[{src_start, src_end, dst_start}]`, same shape as `splice.plan_splice`), `sections_used`,
`section_order`, `moves`, `arc_before/after`, `total_before/after`, `improvement_pct`.
Cuts are only at section boundaries (bar aligned); only the last section is trimmed, so the output
length equals the video duration. No more than 2 consecutive sections of one repeat group; starting
on intro/chorus/drop is preferred. The plain single trim is the baseline and is returned (empty
`segments` order, `moves` 0) unless an arrangement scores higher on the same objective as splice
(combined score + drop-on-peak / lull-on-calm bonus, minus a small penalty per move), so
`total_after >= total_before`.

## Auto-sync

`POST /api/auto-sync` (Origin-guarded; video/song paths must be allowed) starts a job running `autosync.auto_sync`.

Body: `{video_path, song_path, lead_s?, allow_loop? (false), guardrails?: {max_speed_dev, max_tempo_pct, max_cuts}, budget_s?, unlock_original? (false), prefer_energy? (true), ramps? (false), smooth? (false)}`.
`prefer_energy` (top-level bool, default **true**): the arrange step ranks by energy correlation and keeps the highest-arc arrangement that stays within the total / tight_pct / sync_pct guards of the baseline. Send `false` for the original objective. It is always echoed in `options` (a default call and an explicit `true` share one cache entry; `false` is cached separately). Measured on 4 pairs: no tight/sync cost; arc +11.5 on one pair, identical plans on the rest (`docs/ARC-RESEARCH.md` on branch `g-arc`).
`ramps` (top-level bool, default **false**): opt-in eased speed ramps (`speedramp.plan_ramps`, mode `anchored`) planned on top of the chosen plain-trim retime plan. Default off changes nothing. It is echoed in `options`. On gain (ramp total beats the step total) the result gets `plan.retime_ramps` (the ramp plan; `plan.retime` is untouched) and `final.ramps = {kept, mode, total_step, total_after, tight_step, tight_after, sync_step, sync_after, smooth_step, smooth_ramp}`; otherwise `limits_hit` has `"ramps: no gain"` (or `"ramps: not applicable"` for tempo/splice/nudge plans), and `final.ramps` is absent.
`smooth` (top-level bool, default **false**): the "Smooth speed changes" mode. Off changes nothing (byte-identical result). On, auto-sync FORCES a retime-only plain-trim plan with eased speed ramps (`speedramp` anchored mode: no instant speed steps, `max_jump <= ~0.01`) and applies it even when plain auto-sync would score higher: `plan.retime` IS the ramp plan (`{warp, points: [{t_src, speed, ease_in_s, ease_out_s}], curve, out_duration, ...}`, format in `docs/U11C-RAMP-FORMAT.md`; also copied to `plan.retime_ramps`), `final.ramps = {kept: true, forced: true, mode, ...}`, `final.*` are the smooth plan's scores, `path` is `"smooth"`. The result also carries `smooth = {applied, reason, smooth: {total, sync, tight, arc, max_jump, max_accel}, best: {same fields}, best_path}`: `smooth` is the plan applied, `best` is the plan plain auto-sync (smooth off) would have chosen (its `max_jump`/`max_accel` are measured on its retime warp only; null when it has no retime). Not applicable (song shorter than video, looped, segmented plan): the plain result is returned with `smooth.applied = false`, a `reason`, and `limits_hit` has `"smooth: not applicable"`. `smooth` is echoed in `options` (its own cache entry). Runs auto-sync twice (plain + forced), so it costs about 2x. Deterministic like the rest. Tempo ramps on the song are not part of it.
**Determinism.** Results depend only on the inputs and options, not on machine speed or load: the search budget is counted in work units (tempo 5, splice 4, arrange 6, retime 3, each alt-offset retime 1, micro 1; default 100, more than every order together costs, so by default nothing is pruned; `auto_sync(budget_units=...)` for tests/tools; `final.work_units = {used, budget}`). `budget_s` never prunes: it only scales an emergency wall-clock abort for a hang (`max(180 s, 10 x budget_s)`, far above a real pair even on a heavily loaded machine). If that cap ever trips, the result says so explicitly: `deterministic: false` (top level and in `final`), and `limits_hit` contains `"budget: time cap hit"` and `"budget: not reproducible (wall-clock safety cap hit)"` (shown in the UI's budget notes); otherwise `deterministic: true` and the result is byte-identical across runs, machines and load. Ties are broken by explicit sort keys.
Options are clamped to the engine limits: `lead_s` 0-0.12, `max_speed_dev` 0-0.25, `max_tempo_pct` 0-8, `max_cuts` 0-3, `budget_s` 1-60 (default 20).

Returns `{job_id, status: "running", options}` (the clamped options). A second request for the same (video, song) pair while one runs returns `409 {detail, job_id}`.
Poll `GET /api/auto-sync/{job_id}`; when `status` is `done`, `result` is the auto_sync result verbatim (`steps`, `final` incl. `effort`, `fit_after_effort`, `tightness`, `plan`, `why_not_100`).
`result.stem_targets` is always present: the soft sync targets the search used, `[{t, t_song, kind, stem, weight}]` sorted by `t`. `t` and `t_song` are the same value, seconds in the **source song** (before any retime/splice); `kind` is `detail | vocal | bass`, `stem` the stem they came from, `weight` 0-1. They lie only inside the song's low-energy sections (see `final.quiet_sync*`). It is `[]` when the song has no cached stems or no quiet section.

Progress: while running, the job also carries `steps_so_far` (list of the step payloads `auto_sync` reported through its `progress_cb`, oldest first) and `current_step` (a short name taken from the latest step, `""` when none / finished). Both stay `[]` / `""` if the installed `autosync.auto_sync` has no `progress_cb` parameter (checked with `inspect.signature`).

`POST /api/auto-sync/{job_id}/cancel` (Origin-guarded; 404 unknown job). Idempotent: returns `{job_id, status, cancelled}`. On a running job it sets the cancel flag that `auto_sync` polls through `should_cancel`; the job ends with `status: "cancelled"`, `result: null`, nothing is written to the cache, and the pair's 409 lock is released. If `auto_sync` has no `should_cancel` parameter the job is marked `cancelled` at once and the worker's eventual result is discarded. A job that already finished is returned unchanged (`cancelled` true only when its status is `cancelled`). `status` is therefore one of `running | done | error | cancelled`.

Each finished run caches its `final` on disk (`.cache/autosync.json`, keyed by a hash of video, song and options, plus a per-pair "latest" entry). `/api/scan` and `/api/score-songs` results carry `auto_sync_cached` per song: the latest `final` for that pair, or `null`, so a UI can rank by `fit_after_effort`.

## Native shell: open / reveal exports

Desktop shell only (`window.pywebview.api`; `static/native.js` wraps them as `nativeOpen(path)` / `nativeReveal(path)`, which resolve `false` in a browser). Both take a path and return `true` if the OS command was launched, `false` otherwise:

- `open_path(path)`: open the file or folder with the OS default app (`open` / `os.startfile` / `xdg-open`).
- `reveal_path(path)`: show it in the file manager (macOS `open -R`, Windows `explorer /select,<path>`, Linux opens the parent folder).

Validation (`paths.exports_member`): the path is `resolve()`d and must exist and be strictly inside the exports dir (`.exports/`), so `..` and symlinks leading out are rejected; the exports dir itself and anything else return `false` without running a command.


## Auto-sync additions
`POST /api/auto-sync` accepts `unlock_original: bool` (default false). When `song_path` is the video's own audio (the extracted `original_audio` path) the server passes `original=True` to `auto_sync`, which fixes offset 0 and only allows retime + micro unless `unlock_original` is true. `job.source` is `"original"` or `"song"`.
`GET /api/auto-sync/{id}` `steps_so_far` grows during the run (events `{step, status: start|end, score_before, score_after, kept, elapsed_s}`). After `POST .../cancel` the job ends `status: "cancelled"` with `result` = best-so-far (`limits_hit` contains `"cancelled"`, never cached), or `null` if none. Arrange is not interruptible (about 5 s worst case).

## Auto-sync all (PROPOSAL, F4; not implemented yet)
Batch version of `/api/auto-sync` for the scan list: one video, many songs, run through the worker pool, cancellable as a whole.
The UI (`static/js/autosync-all.js`) does NOT use this yet: it runs a client-side queue over the per-song `/api/auto-sync` job.

- `POST /api/auto-sync-batch` `{video_path, song_paths:[str], options?}` (Origin-guarded; same path validation and option clamping as `/api/auto-sync`)
  -> `{batch_id, total, status:"running"}`. Songs whose pair already has a running job are attached to it, not re-run; songs with a cached `final` for the same options finish immediately (`cached:true`).
- `GET /api/auto-sync-batch/{batch_id}` -> `{batch_id, status: running|done|cancelled|error, total, done, songs:[{path, status: queued|running|done|error|cancelled, job_id|null, progress: 0..1, natural_pct, auto_pct|null, error|null, cached?}]}`.
  `natural_pct` = the song's plain `total_pct`; `auto_pct` = `result.final.total_pct` once done. `progress` per song comes from the per-song job (`steps_so_far` / planned steps); overall progress is `done / total`. Same song order as the request.
- `POST /api/auto-sync-batch/{batch_id}/cancel` -> same body as GET. Queued songs become `cancelled`; running ones are cancelled through `/api/auto-sync/{job_id}/cancel` (best-so-far is kept, never cached). Idempotent.
- At most one batch at a time (409 with the running `batch_id` otherwise). Concurrency = a thin server-side loop over the existing per-song auto-sync job, bounded by the heavy-work pool, so no new engine code is needed.

## Booms, camera motion, shorter songs (F6 / F9 / F2)
Song analysis (`audio.py`, `boom.py`) adds `booms` `[{t, conf, jump_db, onset, cue, kind}]`, `main_boom` (the strongest, or `null`) and `boom_source` (`"mix"` or `"stems"`). Video analysis (`video.py`, `flow.py`: one shared dense optical-flow pass, rate `flow_hz`) adds:
- `camera_motion` `{hz: 4, curve, pan, zoom, whip, pan_x, pan_y, zoom_rate, speed, pan_speed}` (per-sample arrays from a similarity fit of the flow field; `null` if the pass failed),
- `camera_events` `[{start, end, kind: whip|pan|tilt|zoom, speed, peak, smooth, direction}]`,
- `subject_curve` / `subject_hz` (motion left after removing the camera fit) and `visual_booms` (impacts from subject motion and brightness flashes, plus camera whips).
Old video caches without `camera_motion` are re-analysed on the next hit (`workers._video_cached`).

Scoring: camera speed feeds the energy/arc curve with weight `CAM_ENERGY_W` 0.35. Camera events are not used as sync events by default (they lowered real-pair scores). Each result carries `boom_pct`, `boom` `{audio_t, video_t, vis_t, err_ms, matched, main}`, `n_booms` and `n_matched`. The boom is a bonus blended in with `W_BOOM` 0.10 and never lowers `total_pct`.

Auto-sync step `"boom"` tries offsets that put the song's main boom on a visual boom (`params`: `main_boom`, `candidates`, `offset`, `boom_before`, `boom_after`; work cost 1). A candidate is kept only if `boom_pct` gains at least `BOOM_MIN_GAIN` 5, the total does not drop, and sync/tight drop by at most 0.5. There is no boom step when either side has no booms.

F2: a song shorter than the video (but at least `PARTIAL_MIN_S` 4 s) is scored and auto-synced over the best-matching stretch of the video instead of being ineligible. The result has `partial_video: true` and `video_use` `{start_s, end_s, used_s, video_duration_s, coverage_pct, label}`, with a label like `"uses 0:08 of 0:20 video"`. Auto-sync puts it in `plan.video_window` and `final.video_use`. Offsets and times in such a result are clip times (relative to `start_s`). Render and the Resolve export trim the source to that stretch. Songs under 4 s stay ineligible ("song shorter than video"), and splice and arrangement stay ineligible for shorter songs.

## Analysis worker jobs (process pool; heavy work never runs on the event loop)
Analysis, probe, thumbnails and proxy builds run in a process pool (`amv_matcher/workers.py`, cores - 1;
`AMV_WORKERS=N` sets the size, `AMV_WORKERS=0` runs everything inline, used by the test suite).
Existing endpoints (`/api/analyze`, `/api/video`, auto-sync...) wait on the pool in their own sync thread, so
Range requests and polls stay fast during analysis. The non-blocking job API:

- `POST /api/jobs` `{kind, path, priority=1, params={}}` -> `{id, ...}`. Kinds: `song_analysis`, `video_analysis`,
  `probe`, `thumbnails`, `filmstrip`, `proxy`. Lower priority number runs first. `path` must be an allowed song/video
  (403 otherwise); `params` accepts only `n` and `width`. Origin-guarded.
- `GET /api/jobs/stats` -> queue/running/done counts and worker state.
- `GET /api/jobs?since=<seq>&state=<state>` -> job events since a sequence number (UI polling).
- `GET /api/jobs/{id}` -> state (`queued|running|done|error|cancelled`), `result`, `error`; 404 if unknown.
- `POST /api/jobs/{id}/cancel`, `POST /api/jobs/{id}/bump` `{priority}` (Origin-guarded).
- `POST /api/prewarm/pause`, `POST /api/prewarm/resume` (Origin-guarded).
A worker crash fails only its job; results land in the normal caches, keyed by (path, size, mtime).

## Projects (.amvproj)
Module `amv_matcher/project.py`, routes in `project_api.py` (Origin-guarded). A project is JSON (`version` 1) holding the video, music dirs, sources, `in_use`, `offset`, `splice`, `autosync`, `effects`, `export` and `ui`. Media is referenced by `{path,size,mtime}`, never copied. Paths must end with `.amvproj`.
- `POST /api/project/save` `{path, data}` writes atomically (tmp + replace) and adds it to Recent.
- `POST /api/project/load` `{path}` -> `{data, missing:[...]}`: defaults filled for missing keys; media that is gone or whose size/mtime differs gets `missing:true`. 400 for a bad file/version, 404 if absent.
- `GET /api/project/recent` -> `{recent:[{path,name,opened,missing}]}` (max 10, newest first).
- `POST /api/project/relink` `{entry, roots?}` -> `{candidates:[path]}`: same name + size found under `roots` (default: the entry's old folder); hidden dirs skipped, 2000 files per root.
- `POST /api/project/autosave` `{data}` writes `autosave/<name>.amvproj` under the app data dir.

## Media library (in place)

Roots persist in `media_roots.json` (defaults: Movies/Videos, Music, Downloads, Desktop). Files are never copied.

- `GET /api/media/roots` -> `{roots:[{path,name,exists}]}`; `POST` / `DELETE /api/media/roots {path}` add/remove (POST needs an existing dir).
- `GET /api/fs/list?path=` -> `{path,parent,folders,files:[{name,path,size,mtime,kind,ext}]}`; 403 outside the roots (symlinks resolved), hidden entries skipped.
- `GET /api/media/probe?path=` -> `{duration,width,height,fps,vcodec,acodec,has_audio,download}` (cached by path|size|mtime; `download` = download_source or null).
- `POST /api/media/link {path}` -> `{path,name,size,kind}`; allows the file via `server.allow_path`.
- `GET /api/thumb?path=&t=&w=` -> image/jpeg (cached); `GET /api/filmstrip?path=&n=12&w=160` -> `{frames:[{t,url}]}`.

## Static files
- `GET /` -> `static/index.html` (the single-page UI).
- `GET /static/{name}` -> the file under `static/` (nested paths allowed). 404 if it is missing or resolves outside `static/` (`..`, symlinks).

## Stems (stems_api.py)
Separation is one at a time (single worker). Providers: `demucs` (needs the Demucs install) and `hpss` (built in); `auto` picks the best available. Results are cached per song. Song paths go through the song allowlist (403).

- `POST /api/stems` (guarded) body `{"song_path", "provider": "auto"}` -> job: `{id, song_path, provider, status: running|done|error|cancelled, progress 0-1, message, error, stems: {name: wav path}|null, stem_names, started, cached?, seconds?}`. A running job for the same song and provider is returned instead of a new one; a cache hit returns `status: "done", cached: true`. 400 bad provider, 409 `Demucs is not installed`, 507 less than 3 GB free.
```json
{"id": "a1b2c3d4e5f6", "song_path": "/abs/music/song.mp3", "provider": "hpss", "status": "running", "progress": 0.0, "message": "Starting", "error": null, "stems": null, "stem_names": ["drums", "music"], "started": 1760000000.0}
```
- `POST /api/stems/cancel` (guarded) body `{"song_path", "provider"}` (provider ignored) -> `{"cancelled": <number of running jobs stopped>}`.
- `GET /api/stems/status?song_path=` -> `{capabilities: {demucs_installed, free_gb, install}, available: {provider: [stem names]}, state: "running"|"ready"|"none", running?, provider?, stems?, last?}`.
```json
{"capabilities": {"demucs_installed": false, "free_gb": 120.5, "install": {"status": "idle", "progress": 0.0, "message": "", "error": null}}, "available": {"hpss": ["drums", "music"]}, "state": "ready", "provider": "hpss", "stems": ["drums", "music"]}
```
- `GET /api/stem-audio?song_path=&stem=&provider=` -> `audio/wav`. Empty `provider` uses the first cached one (demucs, then hpss); 404 `No stems separated for this song yet` or `Unknown stem`.
- `GET /api/stem-features?song_path=&provider=` -> per-stem feature curves from `stems.features_for` (same provider rule and 404).
- `POST /api/stems/install` (guarded) -> install state `{status: idle|running|done|error, progress, message, error}`; starts a background install into the venv, or returns `done` if already installed. 501 in a frozen build with no stems pack and no system Python 3.9-3.12, 507 low disk.
- `GET /api/stems/install` -> the same state plus `installed: bool` (and `frozen: true` with the pack-missing message in a frozen build).

## Worker job details
`GET /api/jobs` returns `{"events": [...], "stats": {...}}`, plus `"jobs": [...]` when `state=` is given. `POST /api/jobs/{id}/cancel` -> `{"cancelled": bool}`. `POST /api/jobs/{id}/bump` body `{"priority": 0|1|2}` -> `{"bumped": bool}` (400 for any other priority). `POST /api/prewarm/pause|resume` -> `{"paused": true|false}`. Every `/api/jobs*` and prewarm route returns 503 when workers are disabled.
