# AMV Matcher backend API

Base URL `http://127.0.0.1:8765` (local only). All JSON. Errors: `{"detail": "<message>"}` with 400 (bad input), 403 (path not allowed), 404 (unknown job / video not scanned).
Paths are absolute local paths. `/api/audio`, `/api/waveform`, `/api/pump`, `/api/score` only accept songs inside a music folder that was passed to a scan; `/api/video` only accepts scanned videos.

**Offset convention:** `offset` (seconds) is the song time that sits under video time 0: song time = video time + offset. On the timeline the song clip starts at `-offset`. `trim_start_s = offset`, `trim_end_s = offset + video_duration`.

## POST /api/scan
Body: `{"video_path": str, "music_dir": str, "recursive": bool=false}`. Video ext: .mp4 .mov .mkv .webm .avi .m4v; songs: .mp3 .wav .m4a .flac.
Response: `{"job_id": "ae64f08cef79"}`

## GET /api/scan/{job_id}  (poll every ~400 ms)
`status`: running | done | error. `stage`: starting | video | songs. `video` appears once the video is analyzed (before songs finish). `results`/`too_short`/`skipped` are filled when `status=done` (`results` is null before).
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
Notes: `results` = eligible songs only, ranked by `total_pct` (ties: `sync_pct`). `segments[].label` is calm | build | peak. `energy_curve` is 0-1 at `energy_hz`. `segments`/`lulls`/`peak_segments`/`shots` times are seconds. Segments overlap (1 s smoothing window); draw by `label`. `total_pct = round(0.20*tempo + 0.60*sync + 0.20*arc)` (constants in score.py). Higher `arc_pct` also counts peak-on-drop (±0.5 s) and lull-overlap bonuses.

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
 "energy_curve": [0.1, 0.2], "energy_hz": 10}
```
`peaks` is a flat list `[min0, max0, min1, max1, ...]`, 4000 pairs (8000 numbers) spanning the whole song, values -1..1. No raw audio is ever returned. `beat_strengths[i]` (0-1) belongs to `beat_times[i]`. `beat_times`, `drops` (the drop times) and `lulls` are absolute song time in seconds.

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
Body: `{"video_path": str}`. The video must be under `.uploads/` or a previously scanned video (else 403; bad Origin 403; undecodable video 422). Returns `{"video_path": "<resolved>", "video": {...}}` where `video` is the same object as the `video` section of a scan (`duration`, `fps`, `motion_tempo_bpm`, `cut_times`, `cut_count`, `energy_curve`, `energy_hz`, `segments`, `lulls`, `peak_segments`, `shots`, `peak_count`). Synchronous; the result is cached in memory per video, so repeat calls are instant.

### GET /api/uploads
Totals cover `.uploads/` plus `.exports/`:
```json
{"count": 3, "bytes": 5392050, "uploads_count": 2, "uploads_bytes": 5292050, "exports_count": 1, "exports_bytes": 100000}
```

### DELETE /api/uploads
Deletes everything inside `.uploads/` and `.exports/` only (nothing else is touched). `{"freed": 5392050, "uploads_freed": 5292050, "exports_freed": 100000}` (bytes).

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

### GET /api/export-file?path=
Serves a file from `.exports/` only (403 for anything else) as a download (`Content-Disposition: attachment`).

### POST /api/export-mp4
Body `{"video_path", "audio_path"}` where `audio_path` is a `.wav` inside `.exports/` (else 403). Returns `{"job_id", "status": "running"}`; poll `GET /api/scan/{job_id}` (`done` is 0-100 progress). When `status == "done"` the job has `path`, `url` (use with `/api/export-file`) and `size`; on failure `status == "error"` with `error`. The video's own audio is dropped and replaced by the wav (AAC 192k). The video stream is copied untouched when its codec is h264/hevc/mpeg4/av1/vp9 (falling back to a re-encode if the copy fails), otherwise re-encoded to H.264 (yuv420p, crf 20). Output: `.exports/<video>__<song>.mp4`.
