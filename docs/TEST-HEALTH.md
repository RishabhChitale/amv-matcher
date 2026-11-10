# Test health run

- Date: 2026-11-10 (run started ~14:30 local, finished 14:49)
- Commit tested: 9f6ec9e (HEAD at the time of the re-runs) plus a dirty tree: other pawns had uncommitted edits in
  amv_matcher/{autosync,media,proxy,retime,server,video,workers}.py, tests/{test_autosync,test_retime,test_workers}.py,
  static/*, docs/LEFTOVERS.md. Untracked: stemsync.py, tests/test_stemsync.py, tests/test_winswitch.py (at start).
  Failures in files with others' uncommitted edits are marked "in-flight".
- Load average: 44 / 41 / 34 at start (uptime), 9 / 12 / 18 when the full run finished. The run waited on /tmp/amv-heavy.lock.
- Command: `lockf -k /tmp/amv-heavy.lock .venv/bin/python -m pytest -q --durations=15` (once), then the failed and >20 s tests twice more (3 runs total).

## Totals (full run)
2 failed, 328 passed, 6 skipped, 3 xfailed, 2 xpassed in 451.6 s (7m31s).

## Slowest 15 (full run)
| s | test |
|---|---|
| 33.86 | tests/test_render.py::test_rife_interpolates |
| 30.40 | tests/test_app_shell.py::test_app_main_selftest |
| 29.55 | tests/test_autosync.py::test_speed_jump_limit_all_fixtures |
| 24.42 | tests/test_server_workers.py::test_responsive_during_video_analysis |
| 14.75 | tests/test_autosync.py::test_progress_callback_exception_is_swallowed |
| 13.95 | tests/test_autosync.py::test_original_unlock_restores_old_behaviour |
| 10.90 | tests/test_workers.py::test_real_kinds_smoke |
| 10.74 | tests/test_upload.py::test_score_songs_matches_scan |
| 9.78 | tests/test_amv.py::test_api_scan_and_audio_guard |
| 9.75 | tests/test_autosync.py::test_cancel_mid_run_returns_valid_plan_at_least_baseline |
| 9.23 | tests/test_autosync.py::test_slow24_real_media_retime_tried_or_reported |
| 9.15 | tests/test_amv.py::test_click_track_120_bpm |
| 7.69 | tests/test_autosync.py::test_runtime_three_minute_pair |
| 7.60 | tests/test_autosync.py::test_budget_prunes_but_stays_valid |
| 7.14 | tests/test_autosync.py::test_steps_describe_plan[slow_drift] |

## Re-runs (runs 2 and 3: the 2 failures plus the 4 tests over 20 s)
- rife_interpolates 28.0 / 48.5 s, app_main_selftest 27.2 / 40.6 s, responsive_during_video_analysis 25.0 / 30.9 s,
  speed_jump_limit_all_fixtures 20.6 / 28.3 s. All passed every time; times track load (run 3 load ~11), so they are load-sensitive, not flaky.

## Flaky
- tests/test_stems_winswitch.py::test_pack_status_shapes: failed in run 1, passed in runs 2 and 3. The failing assert saw
  `half_installed: True, weights_cached: True, size_mb: 675.4` where the test expects an empty pack. It reads the real pack
  directory, so a concurrent pack install by another pawn (G-PACK work) made it fail. Environmental and in-flight; the test
  should point at a tmp pack dir.

## Consistent failures
- tests/test_upload.py::test_delete_clears_only_uploads (3/3 runs): the response has an extra `'skipped': []` key
  that the test's expected dict lacks. amv_matcher/server.py has uncommitted edits, so this is in-flight (response shape
  changed, test not updated yet).

## Observations
- The OpenCV "CvVideoWriter_Images ... filename_pattern.empty()" message is printed to stderr in the failing test's
  output but is not itself an assertion failure.
- tests/test_autosync.py:63 uses `@pytest.mark.slow`, which is not registered (PytestUnknownMarkWarning).

## Recommendations
- Register the `slow` marker in pytest config and mark the four >20 s tests (rife, app selftest, speed_jump_limit_all_fixtures,
  responsive_during_video_analysis) so quick runs can use `-m "not slow"`.
- Make test_pack_status_shapes use a tmp pack directory instead of the real one.
- Update test_delete_clears_only_uploads for the `skipped` key (owner of the server.py change).
- Keep runtime-bound tests out of runs started under load above ~10; they only pass with their bounds relaxed or when run via the heavy lock.
- Nothing was fixed in this task.
