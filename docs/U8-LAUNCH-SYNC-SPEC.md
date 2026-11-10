# U8 autosync step: land video launches on song drops

Scoring half: `amv_matcher/buildup.py` (`build_stats`). This spec is the autosync half (owners: rufflet for ramps, gligar for retime/autosync).

## Inputs
- `buildup.video_hits(video)` -> `[{t, strength, kind}]`, video time. kind: `launch` (speed ramp ends in a peak), `onset` (calm -> build/peak block starts), `impact` (end of a peak block). Real AMVs hit a drop with either the rise or the release.
- `buildup.song_buildups(song, stems_feats=None)` -> `[{start, drop, strength}]`, song time. Candidates are detected drops plus chorus-section entries.
- `offset` (song time at video t=0) and the current step warp `[(src, out)]`.

## Function (proposed, `launchsync.plan_launch_anchors`)
`plan_launch_anchors(song, video, offset, step_warp) -> [{t_src, out_t, weight, feasible, speed_needed}]`
- For each build-up, pick the nearest video hit (any kind) within a search window of +-4 s of `drop - offset`.
- `out_t = drop - offset` (output time of the drop); `t_src` = hit time in the source video.
- `weight` = build strength * hit strength. Anchors are high weight, softer than hard cut constraints.
- `feasible` = required local speed `speed_needed` = d(src)/d(out) between neighbouring anchors stays inside the ramp limits (`docs/U11C-RAMP-FORMAT.md`); infeasible anchors are reported, not applied.

## Success
A launch is landed when `|hit_out_t - drop| <= 1 beat` (clip 0.25-0.8 s, the same tolerance as `build_stats`). Re-score with `build_stats` after the plan: `launch_pct` must not fall and total score must not fall.

## Known measurements (case ids)
Pre-fix (launch only) A own: hits 2-4 s before drops, launch match 0. See the commit for the post-fix table.
