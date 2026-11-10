# AMV Matcher

A desktop app for macOS and Windows. It ranks the songs in a folder by how well they fit an AMV video, then syncs the video to the song you pick.
Everything runs on your computer.

## Download and install
Builds are made by CI on every run and uploaded only as a password-protected 7z (see `docs/RELEASES.md`). Decrypt it with `7z x`, then move the app where you like.

### First launch
The app is unsigned (personal use), so the OS warns the first time:
- **macOS:** right-click `AMV Matcher.app` > Open, then confirm Open.
- **Windows:** SmartScreen > More info > Run anyway.

## The main workflow
1. **New match** (opens on first launch, or Cmd/Ctrl+N): pick a video and a music folder, then press Find best songs.
2. The **match results list** shows every song with a fit %. Press Space to preview one.
3. Press **Use this audio** on a song (or Enter). It loads on A1 at its best start.
4. Press **Auto-sync to 100%**. The app retimes, splices and arranges to raise the fit, within your guardrails.
5. **Export video** (.mp4) or **Export to Resolve** (a DaVinci Resolve bundle).

A step-by-step guide to every panel is in [docs/USER-GUIDE.md](docs/USER-GUIDE.md).

## Features
- **Media library:** your files are used in place, from folders you add; shows download sources and thumbnails.
- **Scoring:** tempo, beat sync and energy arc, 0-100 per song ([how it works](docs/HOW-SCORING-WORKS.md)).
- **Auto-sync:** tries tools in turn and keeps what helps; guardrails limit speed change, tempo stretch and cuts; a cut lead sets how early cuts land before the beat.
- **Smart splice and section arrangement:** cuts the song on bars and reorders its sections to fit the video.
- **Retime and frame generation:** slows or speeds sections of the video; fills slowed frames by blend (fast) or RIFE (high quality).
- **Stems and the stem player:** split the song into vocals, drums, bass and other; play and view them as lanes under A1.
- **Boom effects:** a pulse (and optional shake) on beats and drops, in the preview.
- **Projects:** `.amvproj` files with save, load, relink and autosave (file support is in; the in-app menu is coming soon).
- **Resolve export:** FCPXML timeline with retime clips, markers and an AMV Pump Fusion effect. Not yet tested in Resolve.

## Desktop touches
A native menu bar (File, Edit, View, Playback, Help) with the usual Cmd/Ctrl shortcuts, system notifications when a job finishes in the background, a remembered window size and position, Open in DaVinci Resolve, and Reveal in Finder/Explorer. Projects (Open, Save, Save As, Recent, autosave, Relink) and the redesigned New match and Match results popups are coming soon. See [docs/USER-GUIDE.md](docs/USER-GUIDE.md).

## Building from source
- **Dev:** run `./run.sh` (macOS/Linux) or `run.bat` (Windows). It creates `.venv` if missing, installs `requirements.txt`, then starts the desktop app with `.venv/bin/python app_main.py`.
- **Stems (optional):** Demucs needs its own venv, see `requirements-stems.txt`. The packaged app leaves it out; an in-app installer is coming soon.
- **Packaging:** `pip install -r requirements-dev.txt`, then `python -m PyInstaller --noconfirm amv_matcher.spec`. The result is in `dist/`.
- **CI:** `.github/workflows/build.yml` runs the tests, builds on macOS (Intel and Apple Silicon) and Windows, runs `--selftest` on the built app and zips the result. Pushing a `v*` tag attaches the zips to a release.
- Backend routes: [docs/API.md](docs/API.md). Open items: [docs/LEFTOVERS.md](docs/LEFTOVERS.md).

## Testing
- `.venv/bin/python -m pytest` runs the unit and API tests. Real-media and env-gated tests skip themselves.
- `.venv/bin/python tools/ui_smoke.py` drives the UI in headless Chrome and prints PASS/FAIL per check. It needs Google Chrome installed.
- `.venv/bin/python tools/real_suite.py` runs the scorer and auto-sync on real media. It reads a local, untracked `real_cases.json` that lists your videos and songs.

Licenses: see [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
Builds are private; no public binaries.

## Privacy
Everything is local. Nothing is uploaded. Your media is never copied: the app reads files where they are, and keeps only small caches (analysis, thumbnails, proxies) and exports in its own data folder. The one network use is the optional download of RIFE or Demucs, which you start yourself.
