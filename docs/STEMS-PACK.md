# Stems pack: design

Status: **detection implemented, download/install of a prebuilt pack NOT implemented.** No downloads happen today.

## Problem
Stem separation (Demucs) needs Python + torch + demucs (~0.7 GB on macOS arm64 with CPU/MPS torch, 1.5-2.5 GB on Windows with CPU torch). The frozen app (PyInstaller) cannot import torch itself and cannot reuse `sys.executable` (that is the app exe). Today the frozen build either finds a system Python and builds a venv, or returns 501. A separate, versioned runtime ("stems pack") removes the need for a user-installed Python. The built-in HPSS separator stays as the always-available fallback.

## Pack contents
One archive per (os, arch, torch-flavour):

| target | python | torch | approx download | installed |
|---|---|---|---|---|
| macos-arm64 | python-build-standalone 3.11 | torch CPU/MPS | ~250 MB | ~0.7 GB |
| windows-x64 | embeddable CPython 3.11 + pip bootstrap (or python-build-standalone) | torch CPU | ~200 MB | ~1.5 GB |

Contents: interpreter, site-packages (torch, torchaudio, demucs, numpy, soundfile), `pack.json` (`{"version": "1.0.0", "demucs": "...", "torch": "...", "os": "...", "arch": "..."}`). Model weights (htdemucs, ~80 MB) are fetched on first use into `TORCH_HOME` as today, or optionally bundled.

## Distribution and verification
- A small manifest (`stems-pack-manifest.json`) lists, per target: `version`, `url`, `size`, `sha256`. The manifest is pinned to the app version (the app knows which pack versions it supports: `PACK_MIN..PACK_MAX`).
- Download to `<data>/stems/_dl/<name>.part`, stream with `Range` resume, verify sha256 before extracting. A mismatch deletes the file and fails the job with a clear error.
- Extract to `<data>/stems/pack-<version>.tmp/`, then atomic `winsafe.replace_retry` rename to `<data>/stems/pack-<version>/` and update the `current` pointer. Windows: long-path-safe extraction (same helper as render zips), AV scanners may lock files, so use `safe_rmtree` with leak reporting on cleanup.
- Old versions are deleted only after the new one passes a self-test (`python -c "import demucs, torch"` via `winsafe.run`).

## Detection (implemented)
`stems_api.pack_status()` returns `{installed, version, python_path, size_mb, source}`; `source` is `"pack"` (pack dir with `pack.json`), `"venv"` (app-built venv, dev or system-Python fallback) or `None`.

Resolution order for the demucs interpreter: installed pack, then app-built venv, then not installed.

System-Python fallback: `stems_api.find_system_python()` returns an argv prefix (e.g. `["py", "-3.11"]`), checking version in `PY_MIN..PY_MAX` (3.9-3.12) by actually running it:
- Windows: `py -3.12`, `py -3.11`, `py -3.10`, `py -3.9`, `py -3`, then `python` / `python3` on PATH.
- macOS/Linux: `python3.12` .. `python3.9`, `python3`, `python`, then `/usr/bin/python3`, Homebrew paths.
The venv is always built with that interpreter, never `sys.executable` (which is the app when frozen). Microsoft Store stubs fail the version check and are skipped.

## Install flow (implemented: system-Python pack)
`stems_api.py`; jobs poll like separation jobs.
- `POST /api/stems/install` -> job JSON `{id, status: running|done|error|cancelled, stage: starting|venv|downloading|installing|verifying|done, progress 0..1, message, error, package, started, seconds?, torch?, demucs?}`. 200 = started (or `{id:null,status:"done"}` if already installed), **409** one already running, **507** under 3 GB free, **501** frozen app without a system Python 3.9-3.12.
- `GET /api/stems/install` latest job + `installed` + `pack`; `GET /api/stems/install/{job}` that job (404 unknown); `POST /api/stems/install/{job}/cancel` -> `{cancelled, job}` (kills pip's process tree via `winsafe.kill_tree`, removes the half venv); `GET /api/stems/pack` -> `pack_status()`.
- Steps: `find_system_python()` (dev: the running interpreter) -> venv in the app-data `stems-pack` dir -> `pip install -r requirements-stems.txt` (+ `-c constraints-stems-macos.txt` on macOS Python 3.9). Progress is parsed from pip output (percent by package count). Then VERIFY: the venv python imports torch + demucs and prints versions.
- Resume-safe: `pack.json` (`{version, python, torch, demucs, installed_at}`) is written atomically LAST. A venv dir without it, or with another `PACK_VERSION`, is "half installed" and is deleted and rebuilt by the next install.
- `pack_status()` -> `{installed, version, python_path, size_mb, source, half_installed, weights_cached}`.
- Weights: Demucs downloads htdemucs weights on first separation into `TORCH_HOME` (app-data cache, `<cache>/torch`); the separation job reports stage `downloading_weights` (message "Downloading Demucs weights (first time only)") until the model is loaded, then `separating`.

## Failure modes
- No network / proxy: manifest fetch fails -> error text, HPSS still usable, retry button.
- Disk full: caught by preflight, and by `OSError` during extract (cleanup, report).
- sha256 mismatch / truncated download: delete, one automatic retry, then error.
- Antivirus locks or quarantines files on Windows: retry via `replace_retry`, then report leaked paths.
- Pack incompatible with app (version outside `PACK_MIN..PACK_MAX`): `pack_status()` reports `installed` but the UI offers update; do not run it.
- Self-test fails (missing VC++ runtime on Windows, wrong arch): keep old pack, show the error output tail.
- No supported system Python in `system` mode: message "No system Python 3.9-3.12 found; use the downloadable pack".
- Interrupted install: `.tmp` dirs and `_dl/*.part` are cleaned or resumed on next launch.

## Out of scope
Code signing/notarisation of the pack interpreter on macOS (needs ad-hoc signing check at extract time), GPU (CUDA) flavours, auto-update.
