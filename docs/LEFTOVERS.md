# Leftovers

Honest-leftovers tracker: the goal is to perfect the app until none remain open.

| ID | Area | Leftover | Owner | Status | How we'll verify |
|---|---|---|---|---|---|
| L1 | Scorer | real AMVs scored too low (9/29 false cuts, tempo 136 vs 125.5, no negative offsets, beat-only sync) | krokorok | in progress: 66a67e6 fixed the cut detector (29->25), the grid, negative offsets and the reference, but the real scores didn't rise (own 46->43); redesigning as a ground-truth discriminative score | videoA own audio >= 75, songS1 >= 70 |
| L2 | Splice | ValueError when the best offset is negative | gligar | open | splice test with offset -0.63 |
| L3 | Splice | a repeated drop lands ~1 s early on the 2nd peak (bar-aligned cuts); 'tighter cuts' (beat-level) option not built | gligar | open | drop within 0.25 s of every peak in beat mode |
| L4 | Auto-sync | arc stuck at 50 without energy structure; the slow-section fixture reaches only 92-97 | gligar | in progress | fixture sync >= 95, arc improves on drop fixtures |
| L5 | Auto-sync | the tail keeps the last speed (out_duration drifts); runtime near the 10 s budget | gligar | open | out_duration report + budget test |
| L6 | Auto-sync | not yet run on real media (songS1 to videoA) | gligar | open | AMV_REAL=1 test passes |
| L7 | UI | ui_smoke 104/106: Boom checks fixed; 2 offset-drag checks fail because synthetic auto offsets now sit at ~0 (test fragility) | beedrill | open | ui_smoke 106/106 |
| L8 | UI | a real Finder folder drop is untested (only faked) | user + app shell | open | manual check in the native app |
| L9 | UI | .mkv and other formats can't preview in the browser | app shell | open | the app transcodes a preview proxy via PyAV |
| L10 | Platform | runs as a local server in a browser, not an app; Windows not supported | krokorok (next) | open | the .app and .exe from CI launch and pass smoke |
| L11 | Frame gen | slowed sections have no generated frames (no RIFE/blend render yet) | open | open | an exported retimed mp4 uses RIFE frames |
| L12 | Resolve | the export prototype isn't merged into the app; untested in Resolve | open | open | the user's Windows Resolve 20/21 checklist passes |
| L13 | Stems | no stem separation / stem player | beedrill | in progress | Demucs 4 stems + stem player smoke |
| L14 | Real media | only 1 real AMV + 1 song tested; 'any and all examples' needs a broader real set | user + commander | open | a real-media suite across several AMVs/songs passes |
| L15 | Tests | full pytest and ui_smoke must be green together on a clean tree | all | open | CI green |
| L16 | Scorer | the original-audio source got a non-zero best offset (-1.91) although it's intrinsically aligned | krokorok | in progress | own audio always offset 0 |
| L17 | Export | render/export/splice with negative offsets not verified | krokorok + gligar | open | export test at offset -0.63 |
