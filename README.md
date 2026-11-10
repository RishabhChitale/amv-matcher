# AMV Matcher

Local web app that ranks the songs in a folder by how well they fit a video edit. It reads only the files you point it at and serves them back to your own browser (127.0.0.1 only, no login, no cloud, no telemetry).

## Run
```
./run.sh        # creates .venv, installs deps, serves http://127.0.0.1:8765 and opens the browser
```
Enter a video path and a music folder, press Scan. Songs: .mp3 .wav .m4a .flac. Tests: `.venv/bin/python -m pytest`.

## How the score works (0-100%)
`total = 0.20 * tempo + 0.40 * sync + 0.40 * arc` (constants in `amv_matcher/score.py`).
- **Tempo**: song BPM vs the video's motion tempo (half/double time allowed).
- **Sync**: how many video cuts and motion peaks land on song beats at the best start offset.
- **Arc**: Pearson match of the video energy curve (motion + cut density) against the song energy curve (RMS + onsets + bass), plus bonuses for peaks landing on drops and calm parts on lulls.
- **Length rule**: a song shorter than the video is ineligible (shown under "Too short"); there is no coverage penalty. Each result shows a trim window ("Use 1:12 to 3:40"). Drag the song clip on A1 for a manual offset; the score updates live.
- **Boom** (Effects panel): the preview pulses the video on beats (drops hit 4x harder, with brightness flash and optional shake), driven by the song's clock. `GET /api/pump` returns the same pulses as keyframes (`amv_matcher/pump.py`).

## Known limits
- Browser preview depends on codecs: .mkv and similar may not play (scores still work).
- The pump is a preview plus keyframes; there is no export to an editor yet.
- Beat tracking is librosa-based; very ambient or rubato music scores poorly.
- First librosa import is slow (numba JIT); a 3-minute song takes ~11 s to analyze cold, then it is cached in `.cache/`.
- Test fixtures are MJPG .avi because OpenCV here can't open an mp4v writer.

## Roadmap
1. Auto-cut: use the cached `shots[]` (start, end, mean/peak energy, motion tempo) to trim shots so cuts land on beats.
2. Rearrange shots so high-energy shots meet drops and calm shots meet lulls.
3. Export the cut list and pump keyframes to a DaVinci Resolve timeline.
