# AMV Matcher

Ranks the songs in a folder by how well they fit an AMV video: tempo, beat sync and energy arc.
Runs locally; it serves files only to your own browser (127.0.0.1, no login, no cloud).

## Quick start
```
./run.sh
```
This creates `.venv`, installs dependencies and starts the server. Open http://127.0.0.1:8765 (the script tries to open it for you). Paste the video path and the music folder path, then press Scan. Songs: .mp3 .wav .m4a .flac. Tests: `.venv/bin/python -m pytest`. Backend routes are in [docs/API.md](docs/API.md).

## Using the editor
- **Results list**: ranked songs with a score ring, BPM and trim window; the list scrolls. Songs too short for the video are under "Too short", unreadable files under "Skipped". Clicking a row loads that song onto A1 at its best offset. It does not autoplay: press Space or Play to hear it in sync with the video.
- **Timeline**: V1 shows the video's energy curve as a filled area, cuts as thin ticks, and calm/peak segments as tinted bands. A1 shows the selected song's waveform, beat ticks, drops as glowing diamonds, and a thin pump lane underneath.
- **Song offset**: drag the song clip on A1 to set a manual offset. The score updates live and the result's trim row shows the new window marked "(manual)".
- **Effects panel**: Boom (0-100, default 40) sets pulse strength; Decay (50-400 ms, default 180) sets how fast it fades; Drops only ignores ordinary beats; Shake jolts the picture on drops. Drops also flash the video brighter. Preview only for now; `GET /api/pump` returns the same pulses as keyframes. These settings are remembered in the browser between visits (Boom starts at 0 if your system asks for reduced motion).
- **Select, delete, undo**: click the V1 area (or the player) to select the video clip, or the A1 clip to select the audio; Esc or a click on empty space deselects. Backspace/Delete (or the x on the clip) removes the selection from the editor view only; songs stay in the sources list and in `.uploads/`. Removing audio leaves A1 empty and disables Fit; removing the video greys the scores ("needs a video") until you drop or browse a new video, which rescores everything. A toast with Undo (6 s) or Cmd/Ctrl+Z restores the clip, its offset, In use and scores.
- **Linking**: a video and its own original audio are linked by default (chain icons on V1/A1, toggle in the transport bar, Cmd+Opt+L): selecting or deleting either acts on both, and one Undo restores both. Unlink to remove just one; the original audio stays locked at offset 0. Regular songs are never linked.
- **Formats**: if the browser can't play the video (.mkv and some others), the player shows "This format can't preview in the browser; scores still work."
- **Keys**: Space play/pause; Left/Right one frame; Shift+Left/Right one second; J slower, K pause, L faster.

## Testing
- `.venv/bin/python -m pytest`
- `.venv/bin/python tools/ui_smoke.py` drives the UI in headless Chrome and prints PASS/FAIL per check. It needs Google Chrome installed and the server running (`./run.sh`).

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
