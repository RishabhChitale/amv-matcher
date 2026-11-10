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
     "best_offset_s": 20.0, "trim_start_s": 20.0, "trim_end_s": 30.0, "best_rate": 1.0}
  ],
  "too_short": [
    {"path": "/abs/music/short.wav", "name": "short.wav", "bpm": 100.0, "duration": 6.0,
     "eligible": false, "reason": "too_short", "song_duration": 6.0, "video_duration": 10.0,
     "total_pct": 0, "tempo_pct": 0.0, "sync_pct": 0.0, "arc_pct": 0.0,
     "best_offset_s": 0.0, "trim_start_s": 0.0, "trim_end_s": 10.0, "best_rate": 1.0}
  ],
  "skipped": [{"path": "/abs/music/broken.mp3", "name": "broken.mp3", "reason": "NoBackendError: ..."}]
}
```
Notes: `results` = eligible songs only, ranked by `total_pct` (ties: `sync_pct`). `segments[].label` is calm | build | peak. `energy_curve` is 0-1 at `energy_hz`. `segments`/`lulls`/`peak_segments`/`shots` times are seconds. Segments overlap (1 s smoothing window); draw by `label`. `total_pct = round(0.20*tempo + 0.40*sync + 0.40*arc)` (constants in score.py). Higher `arc_pct` also counts peak-on-drop (±0.5 s) and lull-overlap bonuses.

## POST /api/score  (manual offset)
Body: `{"video_path": str, "song_path": str, "offset": float}` (video must already be scanned; offset is clamped to `0..song_duration-video_duration`).
```json
{"eligible": true, "total_pct": 74, "tempo_pct": 100.0, "sync_pct": 80.2, "arc_pct": 51.0,
 "best_offset_s": 12.3, "trim_start_s": 12.3, "trim_end_s": 22.3, "best_rate": 1.0}
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
