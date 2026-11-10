# AMV Matcher user guide

Short notes on each panel. Items marked *coming soon* are not finished yet.

## Media (left panel)
Your media pool. Press **Add folder** to add a folder; files stay where they are and are never copied.
- Switch between List and Grid, search by name, sort by name, date or duration.
- Double-click a file to link it into the project.
- Each file shows a thumbnail. Files that came from the internet carry a small globe badge with the source site; hover it for the full URL, or use Copy source URL.
- Folders are linked in place: the app reads the files where they are and keeps only small caches, so removing a folder from the pool never touches your files.

## Menu bar (desktop app)
The menu items do not show shortcut hints, but these keys work (Cmd on macOS, Ctrl on Windows).
| Menu | Items |
|---|---|
| File | New Project (Cmd/Ctrl+N), Open Project... (Cmd/Ctrl+O), Save (Cmd/Ctrl+S), Save As... (Cmd/Ctrl+Shift+S), Add Media Folder..., Export... |
| Edit | Undo (Cmd/Ctrl+Z), Redo (Cmd/Ctrl+Shift+Z, or Ctrl+Y), Delete (Delete/Backspace) |
| View | Toggle Media Panel, Toggle Stems, Toggle Effects |
| Playback | Play / Pause (Space) |
| Help | Help |
Project items (New/Open/Save/Save As) are *coming soon*: the menu entries exist, but the project UI behind them is not finished. The New match popup is also *coming soon* (see below).

## Native touches
- **Notifications:** a long job (like a render) sends a system notification when it finishes, only if the window is in the background. macOS only for now; Windows shows none.
- **Remembered window:** size and position are saved when you resize, move or close, and restored on the next launch. A position that is now off-screen is ignored.
- **Open in DaVinci Resolve:** after an export, if Resolve is installed, you can open the export in it directly. The Resolve export itself is untested (see docs/LEFTOVERS.md).
- **Reveal in Finder / Explorer:** shows an exported or source file in your file manager.

## Projects (*coming soon*)
Projects (`.amvproj`) will have Open, Save, Save As, Recent, autosave on close and Relink in the app. The project UI is not finished yet; today only the backend API exists (docs/API.md). This section will be updated when it lands.

## Player, transport and shortcuts
The video plays in the stage. The scrub bar under it seeks. The timecode shows hours:minutes:seconds:frames.
Transport buttons: start, frame back, play/pause, stop, frame forward, end, loop, and link. The VOL slider sets volume.
If the video can't be decoded for preview, the player says so. Scores still work.

## Timeline: V1, A1 and stems lanes
- **V1** shows the video's energy as a filled area, cuts as thin ticks, and calm and peak parts as tinted bands. Retimed parts show as bands.
- **A1** shows the song's waveform, beat ticks, drops as diamonds and a pump lane. Drag the song clip to set a manual offset; the score updates live. "Reset to best start" undoes it.
- **Linking:** a video and its own audio are linked. Select or delete one and both follow. Unlink to act on one.
- **Delete and undo:** Delete removes the selected clip from the editor view only. Undo (Cmd/Ctrl+Z) restores it.
- **Stems lanes:** open "A1 stems" under the timeline. Pick a method (Auto, Quick built-in, or Demucs best) and press **Separate stems**. Each stem gets a lane, and "play stems mix" plays them together. Demucs is an optional install (about 1 GB).

## Audio sources and Match results
- **New match** (Cmd/Ctrl+N; the new popup is *coming soon*, the current dialog works): pick a video and a music folder. Drag and drop works, or paste a path.
- **Match results** (a redesigned popup is *coming soon*) list every song with its fit %, BPM and trim window. Search, sort by "Fit now" or "After auto-sync", preview with Space, and press **Use this audio** (Enter) to load it.
- The **Audio sources** panel holds the same list. Songs shorter than the video are under "Too short". Unreadable files are under "Skipped".
- **Auto-sync all** runs auto-sync on the top 5 songs in turn.

## Auto-sync and Guardrails
Press **Auto-sync to 100%** after Use this audio. You see live steps, a progress ring and a Cancel button. The result shows before and after, and why it isn't 100 if it isn't. Switch between Original and Auto-synced to compare.
**Guardrails** limit how far it may go:
- **Cut lead:** how many ms before the beat a cut should land (default 60).
- **Max speed change:** how much a section's speed may change (default ±15%).
- **Max tempo stretch:** how much the song may be stretched (default 4%).
- **Max cuts:** how many cuts it may make in the song (default 2).
- **Allow loop:** repeat a song that is too short.
- **Unlock original:** let it retime or rearrange the video's own audio.

**Smart splice (advanced)** cuts and arranges the song by sections. Turn it on and set its max cuts.

## Export and Render quality
- **Export audio (.wav)** and **Export video with this audio (.mp4)** use the current song and offset.
- **Render video** applies the auto-sync plan. Choose quality:
  - **Fast (blend):** blends frames in slowed parts. Quick.
  - **High quality (RIFE):** generates new frames. Slow, and it asks you to install RIFE first. An estimate is shown before you start. Cancel any time.
- **Export to DaVinci Resolve** makes a bundle with a timeline, markers and an AMV Pump effect; open the folder from the result. It has not been tested in Resolve yet.
- Exports go to the app's exports folder; use Open or Show in folder.

## Effects
- **Boom (0-100):** pulse strength.
- **Decay (50-400 ms):** how fast it fades.
- **Drops only:** skip ordinary beats.
- **Shake:** jolt the picture on drops.
Effects show in the preview now. Baking them into the rendered video is *coming soon*.

## Keyboard shortcuts
| Key | Action |
|---|---|
| Space | Play / pause (in Match results: preview the highlighted song) |
| Left / Right | One frame back / forward |
| Shift + Left / Right | One second back / forward |
| J | Slower (half speed, down to 0.5x) |
| K | Pause |
| L | Faster (doubles, up to 4x) |
| Delete / Backspace | Remove the selected clip |
| Esc | Deselect, or close a dialog |
| Cmd/Ctrl + Z | Undo a removal |
| Cmd/Ctrl + Opt/Alt + L | Link / unlink video and audio |
| Cmd/Ctrl + N | New match |
| Up / Down | Move in Match results |
| Enter | Use this audio (Match results); Find best songs (New match) |

## Troubleshooting
- **Slow RIFE:** RIFE can take about an hour for a 60 s clip on an older Intel Mac. Use Fast (blend), or read the estimate and cancel if it is too long.
- **Demucs takes a while:** about 3 minutes for a 4-minute song on an Intel CPU. A progress bar and Cancel are shown. "Quick (built-in)" is instant but rougher.
- **Can't preview:** .mkv, .avi and similar formats get an automatic preview copy; wait a moment. If it still fails, scores and export still work.
- **Missing media:** if a file moved, the project marks it missing. Relink finds a file with the same name and size under the old folder. The Relink button in the app is *coming soon*; the project API does it today.
- **First run is slow:** the first analysis warms up the audio libraries. Results are cached after that.
- **App won't open:** see First launch in the README.
