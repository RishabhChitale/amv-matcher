# Leftovers

Honest-leftovers tracker: the goal is to perfect the app until none remain open.

| ID | Area | Leftover | Owner | Status | How we'll verify |
|---|---|---|---|---|---|
| L1 | Scorer | real AMVs scored too low (9/29 false cuts, tempo 136 vs 125.5, no negative offsets, beat-only sync) | krokorok | fixed in 052f7de (ground-truth discriminative sync: own pairs 81/97, songS1 84, cross 28/43) | videoA own audio >= 75, songS1 >= 70 |
| L2 | Splice | ValueError when the best offset is negative | gligar | fixed in a0eb580 (negative offsets in splice/retime/auto_sync, lead_silence_s) | splice test with offset -0.63 |
| L3 | Splice | a repeated drop lands ~1 s early on the 2nd peak (bar-aligned cuts); 'tighter cuts' (beat-level) option not built | gligar | open | drop within 0.25 s of every peak in beat mode |
| L4 | Auto-sync | arc stuck at 50 without energy structure; the slow-section fixture reaches only 92-97 | gligar | improved in 2075c9b/a0eb580 (drop fixture 78->95, peaks 51->93); slow_section still 78; recheck after the scorer redesign | fixture sync >= 95, arc improves on drop fixtures |
| L5 | Auto-sync | the tail keeps the last speed (out_duration drifts); runtime near the 10 s budget | gligar | partly fixed in a0eb580: the tail ramps to 1.0, out_duration reported, budget pruning; runtime bounds unconfirmed (failed under load avg ~24) | out_duration report + budget test |
| L6 | Auto-sync | not yet run on real media (songS1 to videoA) | gligar | open | AMV_REAL=1 test passes |
| L7 | UI | ui_smoke 104/106: Boom checks fixed; 2 offset-drag checks fail because synthetic auto offsets now sit at ~0 (test fragility) | beedrill | in progress (beedrill: set a known offset before the drag checks) | ui_smoke 106/106 |
| L8 | UI | a real Finder folder drop is untested (only faked) | user + app shell | open | manual check in the native app |
| L9 | UI | .mkv and other formats can't preview in the browser | app shell | open | the app transcodes a preview proxy via PyAV |
| L10 | Platform | runs as a local server in a browser, not an app; Windows not supported | krokorok (next) | open | the .app and .exe from CI launch and pass smoke |
| L11 | Frame gen | slowed sections have no generated frames (no RIFE/blend render yet) | open | open | an exported retimed mp4 uses RIFE frames |
| L12 | Resolve | the export prototype isn't merged into the app; untested in Resolve | open | open | the user's Windows Resolve 20/21 checklist passes |
| L13 | Stems | no stem separation / stem player | beedrill | in progress | Demucs 4 stems + stem player smoke |
| L14 | Real media | only 1 real AMV + 1 song tested; 'any and all examples' needs a broader real set | user + commander | open | a real-media suite across several AMVs/songs passes |
| L15 | Tests | full pytest and ui_smoke must be green together on a clean tree | all | open | CI green |
| L16 | Scorer | the original-audio source got a non-zero best offset (-1.91) although it's intrinsically aligned | krokorok | fixed in 052f7de (own audio scored at offset 0 only) | own audio always offset 0 |
| L17 | Export | render/export/splice with negative offsets not verified | krokorok + gligar | open | export test at offset -0.63 |
| L18 | Tests | runtime bounds (splice < 3 s, 3-min auto-sync < 10 s) unverified on a quiet machine | commander | open | re-run when the load avg < 4 |
| L19 | Stems | test_stems::test_quiet_detail_and_vocal_phrase_starts failing (work in progress) | beedrill | in progress | test_stems green |
