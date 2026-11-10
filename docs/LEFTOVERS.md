# Leftovers

Honest-leftovers tracker: the goal is to perfect the app until none remain open.

| ID | Area | Leftover | Owner | Status | How we'll verify |
|---|---|---|---|---|---|
| L1 | Scorer | real AMVs scored too low (9/29 false cuts, tempo 136 vs 125.5, no negative offsets, beat-only sync) | krokorok | fixed in 052f7de (ground-truth discriminative sync: own pairs 81/97, songS1 84, cross 28/43) | videoA own audio >= 75, songS1 >= 70 |
| L2 | Splice | ValueError when the best offset is negative | gligar | fixed in a0eb580 (negative offsets in splice/retime/auto_sync, lead_silence_s) | splice test with offset -0.63 |
| L3 | Splice | a repeated drop lands ~1 s early on the 2nd peak (bar-aligned cuts); 'tighter cuts' (beat-level) option not built | gligar | open | drop within 0.25 s of every peak in beat mode |
| L4 | Auto-sync | energy arc tops out at 73 on the real pair (the remaining gap to 100); the slow_section fixture is still weak | gligar | open: try a structure-level splice (sections, using the stem textures) | fixture sync >= 95, arc improves on drop fixtures |
| L5 | Auto-sync | the tail keeps the last speed (out_duration drifts); runtime near the 10 s budget | gligar | fixed in a0eb580 (tail + budget); runtime bounds tracked in L18 | out_duration report + budget test |
| L6 | Auto-sync | not yet run on real media (songS1 to videoA) | gligar | fixed in 1451ba1/d379c66: real songS1 + videoA auto-sync 84 -> 93 (tempo 100, sync 98, tight 84% within 40 ms, median 9 ms) at lead 0.06 | AMV_REAL=1 test passes |
| L7 | UI | ui_smoke 104/106: Boom checks fixed; 2 offset-drag checks fail because synthetic auto offsets now sit at ~0 (test fragility) | beedrill | in progress (beedrill: set a known offset before the drag checks) | ui_smoke 106/106 |
| L8 | UI | a real Finder folder drop is untested (only faked) | user + app shell | built, awaiting the user's real Finder drop: native drops register paths in place (`/api/register-paths`, folders expanded recursively, no upload copy); verified by a simulated hand-off (`AMV_TEST_DROP`), an OS-level drag cannot be simulated here | manual check in the native app |
| L9 | UI | .mkv and other formats can't preview in the browser | app shell | fixed (proxy.py: cached 540p H.264 preview via PyAV; scoring uses the original) | tests/test_app_shell.py::test_proxy_for_non_web_codec |
| L10 | Platform | runs as a local server in a browser, not an app; Windows not supported | app shell | macOS built locally (PyInstaller .app 479 MB, `--selftest` ok); Windows and CI build (.github/workflows/build.yml) untested until it runs on GitHub | the .app and .exe from CI launch and pass `--selftest` |
| L11 | Frame gen | slowed sections have no generated frames (no RIFE/blend render yet) | open | open | an exported retimed mp4 uses RIFE frames |
| L12 | Resolve | the export prototype isn't merged into the app; untested in Resolve | open | open | the user's Windows Resolve 20/21 checklist passes |
| L13 | Stems | no stem separation / stem player | beedrill | in progress | Demucs 4 stems + stem player smoke |
| L14 | Real media | only 1 real AMV + 1 song tested; 'any and all examples' needs a broader real set | user + commander | open | a real-media suite across several AMVs/songs passes |
| L15 | Tests | full pytest and ui_smoke must be green together on a clean tree | all | open | CI green |
| L16 | Scorer | the original-audio source got a non-zero best offset (-1.91) although it's intrinsically aligned | krokorok | fixed in 052f7de (own audio scored at offset 0 only) | own audio always offset 0 |
| L17 | Export | render/export/splice with negative offsets not verified | krokorok + gligar | open | export test at offset -0.63 |
| L18 | Tests | runtime bounds (splice < 3 s, 3-min auto-sync < 10 s) unverified on a quiet machine | commander | open | re-run when the load avg < 4 |
| L19 | Stems | test_stems::test_quiet_detail_and_vocal_phrase_starts failing (work in progress) | beedrill | in progress | test_stems green |
| L20 | Stems | the packaged app excludes torch/Demucs; stems need an optional pack installed into `STEMS_PACK` (design only, no installer yet) | app shell | open | an in-app 'install stems pack' flow that creates the venv and passes test_stems |
| L21 | Scorer | the perceptual lead (60 ms, from the 2 ground truths) is applied consistently to sync/phrase/tightness | krokorok | fixed in 7a865bb | the ground-truth table own 84/97, cross <= 43 |
| L22 | UI | no 'Cut lead' setting and no Auto-sync button/visuals in the UI yet | beedrill | open | ui_smoke covers the auto-sync flow |
