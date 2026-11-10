# AMV Matcher

Ranks the songs in a folder by how well they fit an AMV video: tempo, beat sync and energy arc.
Runs locally; it serves files only to your own browser (127.0.0.1, no login, no cloud).

## Quick start
```
./run.sh
```
This creates `.venv`, installs dependencies and starts the server. Open http://127.0.0.1:8765 (the script tries to open it for you). Paste the video path and the music folder path, then press Scan. Songs: .mp3 .wav .m4a .flac. Tests: `.venv/bin/python -m pytest`. Backend routes are in [docs/API.md](docs/API.md).

## Using the editor
- **Player**: the video plays with the selected song. Click a result to audition it.
- **Timeline**: video tracks plus the song on A1, with cuts, motion peaks and song beats/drops drawn on it.
- **Song offset**: drag the song clip on A1 to set a manual offset. The score and trim window update live.
- **Boom panel (Effects)**: pulses the video on beats. Boom sets strength, Decay the fall-off time, Drops only ignores ordinary beats, Shake adds a jolt on drops. Preview only for now; `GET /api/pump` returns the same pulses as keyframes.
- **Keys**: Space play/pause; Left/Right one frame; Shift+Left/Right one second; J slower, K pause, L faster.

## How the score works
`total = 0.20 * tempo + 0.40 * sync + 0.40 * arc`, each 0-100.
- **Tempo (20%)**: song BPM vs the video's motion tempo, half/double time allowed.
- **Sync (40%)**: how many video cuts and motion peaks land on song beats.
- **Arc (40%)**: how closely the video's energy curve follows the song's, plus bonuses for peaks on drops and calm parts on lulls.
- **Length rule**: a song shorter than the video is ineligible (listed under "Too short"). A longer song is trimmed: the best window is chosen and shown as the trim window.

Details and constants: [docs/HOW-SCORING-WORKS.md](docs/HOW-SCORING-WORKS.md).

## Known limits
- .mkv and some other formats may not preview in the browser; scores still work.
- No ffmpeg is used or required, so there is no transcoding or export of media.
- Analysis takes time: the first run is slow (librosa/numba start-up) and a 3-minute song takes about 11 s cold. Results are cached in `.cache/`.
- Beat tracking is librosa-based; very ambient or rubato music scores poorly.

## Roadmap
1. Auto-splice the video's shots to fit the song (cuts landing on beats, high-energy shots on drops).
2. DaVinci Resolve FREE export: an "AMV Pump" Fusion effect with baked keyframes, plus an FCPXML timeline with beat and drop markers.
