# U11-SMOOTH hand-off (from rufflet to porygon)

Owner from here: porygon (core pipeline). Code: `amv_matcher/autosync.py` (`_auto_sync_smooth`, `_plan_stats`),
`amv_matcher/server.py` (`smooth` request param), `docs/API.md`, tests in `tests/test_autosync.py`
(`test_smooth_mode_off_identical_and_on_forces_eased_ramps`) and `tests/test_autosync_api.py`.

## What is done
- Job param `smooth: bool` (top-level, default false), echoed in `options`. Off is byte-identical to before.
- On: auto-sync runs the plain search (for comparison) and a forced retime-only run, then re-plans it with
  `speedramp.plan_ramps(mode=RAMPS_MODE)` and rescored it on the eased warp. Deterministic.
- Final JSON shape (on):
  - `path = "smooth"`, `limits_hit` has `"smooth: forced retime-only eased ramps"`.
  - `plan.retime` IS the ramp plan, also copied to `plan.retime_ramps`:
    `{warp @20 Hz from [0,0], points: [{t_src, speed, ease_in_s, ease_out_s}], curve @4 Hz [[src,out,speed]], out_duration, ...}`
    (format: `docs/U11C-RAMP-FORMAT.md`). No `plan.retime.sections`. No `plan.tempo_ramps` / `tempo_ramp` key.
  - `final.*` are the smooth plan's scores. `final.ramps = {kept: true, forced: true, mode, total_step/total_after,
    sync_step/sync_after, tight_step/tight_after, arc_step/arc_after, max_jump_step/max_jump_ramp,
    smooth_step/smooth_ramp}`; `_step` = what plain auto-sync would have chosen, `_after`/`_ramp` = the smooth plan.
  - `result.smooth = {applied, reason, smooth: {total, sync, tight, arc, max_jump, max_accel}, best: {same}, best_path}`.
  - Not applicable (song shorter than video, looped, segmented plan, planner error): plain result,
    `smooth.applied = false` with `reason`, `limits_hit` has `"smooth: not applicable"`.
- `max_jump` for the smooth plan is the planner's own fine-grid figure (`plan_ramps()["smooth_ramp"]`, 100 Hz),
  <= 0.01. Measuring it on the returned 20 Hz warp reads ~accel*0.05 (0.018-0.028), which is sampling, not a speed step.
  The best/plain plan's `max_jump` is measured on its piecewise-linear retime warp (~0.13).

## Real-pair numbers (cases by id; smooth vs plain best; first run, max_jump on the 20 Hz warp)
| pair | smooth total/sync/tight/arc | best total/sync/tight/arc (path) | time |
|---|---|---|---|
| A+S1 | 93 / 97.6 / 75.0 / 72.1 | 96 / 98.0 / 83.2 / 86.0 (retime_first) | 68 s |
| A+B-audio | 91 / 98.0 / 69.5 / 62.4 | 95 / 98.5 / 76.8 / 80.6 (splice_first) | 96 s |
| B+S1 | 92 / 97.4 / 79.4 / 65.3 | 95 / 97.7 / 90.6 / 81.0 (retime_first) | 23 s |

Smooth costs 2-4 total points (mostly tightness and arc) for no instant speed steps; sync barely moves. All three
were deterministic (two runs equal). Plain step plans have max_jump ~0.13-0.14, accel ~1.2-1.3; smooth accel 0.2-0.5.
The max_jump column was re-measured on the fine grid afterwards only on the synthetic test (<= 0.01); a real-pair
re-run of `smooth_pairs` was queued behind the heavy lock and not seen by me.

## Open
- Tempo ramp on the song (`temporamp.py`) was NOT evaluated or adopted; consumers were told there is no `tempo_ramp` key.
- Real-pair re-run to confirm `max_jump <= 0.01` on A/B (expected: planner reports ~0.003-0.006).
- Smooth mode costs ~2x auto-sync time (plain + forced run). Could reuse the plain run's retime order if that matters.
- UI toggle in `static/autosync.js` must send `body.smooth` (yanma was told; it sent `body.ramps` in cf53051).
- Resolve/FCPXML export of ramps is still unverified in Resolve (see `docs/U11C-RAMP-FORMAT.md`).
