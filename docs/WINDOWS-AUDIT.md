# Windows compatibility audit

Read-only review of `amv_matcher/*.py`, `app_main.py`, `amv_matcher.spec`, `tools/`. Nothing has been run on Windows yet.
Line numbers are from commit `dfbeba5`. Executable checks are in `tests/test_windows_compat.py` (xfail = known gap).

Already fine: spawn multiprocessing + `freeze_support()`, no `shell=True`, no `fork`/`fcntl`/`os.kill`/signals, no hard-coded `/tmp`,
`.exe` selection for RIFE, `Scripts/python.exe` lookup, battery via `GetSystemPowerStatus`, Zone.Identifier for download source, `as_posix()` in zips.

## Blocker

| ID | Where | Risk | Fix |
|---|---|---|---|
| W-01 | `stems_api.py:157` | Frozen app: `sys.executable` is the app exe, so `-m venv` runs the app, not Python. Demucs install fails (both OSes, first hit on Windows). | Find system Python (`py -3` / PATH) or bundle an embeddable Python. |
| W-02 | `amv_matcher.spec:25` | Non-darwin icon is `assets/icon.png`; PyInstaller on Windows needs `.ico`. Build fails or icon dropped. | Add `assets/icon.ico`, pick by platform. |

## Major

| ID | Where | Risk | Fix |
|---|---|---|---|
| W-03 | `app_main.py` Popen/`nativeshell.run`, `render.py` RIFE Popen, `stems.py`, `stems_api.py` | `console=False` exe: every subprocess flashes a console window (one per RIFE frame pair). | `creationflags=subprocess.CREATE_NO_WINDOW` on win. |
| W-04 | `server.py:285,286,305,307,318,328,617,619` | `ALLOWED_VIDEOS/SONGS` compare `str(p) in set`; Windows paths differ by case, drive letter, 8.3 names, `\\?\`. Allowed media returns 403/404. | Normalise with `os.path.normcase(str(p.resolve()))` on both add and lookup. |
| W-05 | `server.py:65,72,78`, `paths.py:50`, `medialib.py:121`, `app_main.py:137` | `is_relative_to` on real `Path` is case-sensitive for different case only on posix; on win it is fine for pure compare but `resolve()` result case can differ from stored roots (same class as W-04). | Compare normcased resolved strings. |
| W-06 | `os.replace` at `nativeshell.py:44`, `project.py:151,226`, `medialib.py:73,217,256`, `media.py:72,123,146`, `proxy.py:63`, `workers.py:130`, `server.py:549`, `stems.py:172`, `render.py:886` | Windows raises `PermissionError` if target is open (AV scan, `<video>` streaming a proxy, other thread). | Helper `replace_retry()` with ~5 tries / 50-200 ms backoff. |
| W-08 | `stems.py:151,164`, `stems_api.py:99`, `render.py:345,362`, `workers.py:520-523` | `kill()`/`terminate()` = TerminateProcess, no grandchildren killed; demucs worker processes orphaned and hold files. | Kill process tree (`psutil`) or Windows job object. |
| W-09 | `server.py:569-581` `_clear_dir`, `stems.py:172-175`, `render.py:277,374`, `proxy.py:60`, `media.py:148-152` | `unlink`/`rmtree` of open files raises on Windows; `_clear_dir` is uncaught, so the API returns 500; rmtree with `ignore_errors` silently leaks. | try/except + retry; report skipped files. |
| W-12 | `paths.py:25` | `roaming=True` puts caches, RIFE (~436 MB), stems pack, uploads into `%APPDATA%` (roams/syncs). | `roaming=False` (LOCALAPPDATA). |

## Minor

| ID | Where | Risk | Fix |
|---|---|---|---|
| W-07 | `server.py:509-516`, `project.py:253` | Reserved names (CON, NUL, COM1..) and over-260 paths not handled. | Prefix `_` on reserved stems; enable long paths / `\\?\`. |
| W-10 | `render.py:210` | Zip member check only rejects leading `/` and `..`; misses `C:\`, backslash-rooted entries. | Reject `PureWindowsPath(m).anchor` / drive too. |
| W-11 | `nativeshell.py:58-76` | Notifications are a no-op on Windows; Resolve path hard-coded `C:\Program Files`. | Toast via `winotify`/PowerShell; use `%ProgramFiles%`. |
| W-13 | `app_main.py:161-185` | `explorer /select,` should be one arg `"/select,"+path`; Explorer returns nonzero on success. | Single arg, ignore exit code. |
| W-14 | `paths.py:37-41`, `medialib.py:50-54` | `~/Videos` etc. miss OneDrive-redirected known folders. | `SHGetKnownFolderPath` / `platformdirs.user_videos_dir`. |
| W-15 | `workers.py:110,157`, `media.py:26`, `proxy.py:29`, `stems.song_key` | Cache keys include the raw path string; case/separator differences cause cache misses (not crashes). | Key on normcased path. |
| W-16 | `app_main.py:20-23,345-347` | Windowed exe has `sys.stdout/stderr = None`; `print` in selftest could crash. | Verify the replacement stream is set before any print. |
| W-17 | `app_main.py:285` | `pywebviewFullPath` drops give backslash/drive-case paths (feeds W-04); unverified on WebView2. | Normalise on ingest; test on Windows. |
| W-18 | WebView2 | Needs the WebView2 runtime (absent on some Win10); window geometry restore may need DPI scaling; HEVC not playable (proxy list is conservative, fine); `menu=` support limited. Unverified. | Bootstrap runtime in installer; test DPI. |
| W-19 | `resolve.py:41-42` | `SETTING_DIR_WIN` uses a literal `%AppData%` string. | `os.path.expandvars` when used. |
| W-20 | `tools/ui_smoke.py:12`, `run.sh` | Dev tooling hard-codes the macOS Chrome path; `run.bat` exists now. | Choose Chrome path per platform. |
| W-21 | `app_main.py:89` | Seeds RIFE from `~/Claude/amv-matcher/.cache`; harmless no-op on Windows. | None. |

## Fix plan

Helpers are in `amv_matcher/winsafe.py` (tests: `tests/test_winsafe.py`); `assets/icon.ico` is generated by `tools/make_icons.py` (Pillow 11.3.0, dev only).
Call-site switch-over is a later wave; line numbers are from `dfbeba5` and will drift. Owner = file owner at switch time (assigned by commander-rishabh-2).

| ID | Helper | Call sites to switch | Owner |
|---|---|---|---|
| W-01 | none (design: find system Python / bundle one) | `stems_api.py:157` | stems pawn |
| W-02 | `assets/icon.ico` (done) | `amv_matcher.spec:25` -> `"assets/icon.ico"` when not darwin | build pawn |
| W-03 | `winsafe.popen` / `winsafe.run` | `render.py` RIFE Popen (`_once`), `stems.py` Popen, `stems_api.py:157` run, `nativeshell.run`, `app_main.py` `_launch` | render / stems / shell pawns |
| W-04 | `norm_case_path` | `server.py:285,286,305,307,318,328,617,619` (add and lookup both) | server pawn |
| W-05 | `is_relative_to_ci` | `server.py:65,72,78`, `paths.py:50`, `medialib.py:121`, `app_main.py:137` | server / paths pawn |
| W-06 | `replace_retry` | `nativeshell.py:44`, `project.py:151,226`, `medialib.py:73,217,256`, `media.py:72,123,146`, `proxy.py:63`, `workers.py:130`, `server.py:549`, `stems.py:172`, `render.py:886` | per-file owner |
| W-07 | none (sanitiser tweak) | `server.py:509-516`, `project.py:253` | server pawn |
| W-08 | `kill_tree` (start children with `start_new_session=True` on POSIX) | `stems.py:151,164`, `stems_api.py:99`, `render.py:345,362`, `workers.py:520-523` | stems / render pawns |
| W-09 | `safe_unlink` / `safe_rmtree` | `server.py:569-581` (`_clear_dir`), `stems.py:172-175`, `render.py:277,374`, `proxy.py:60`, `media.py:148-152` | server / stems pawns |
| W-12 | none | `paths.py:25` `roaming=False` (needs a one-time migration from `%APPDATA%`) | paths pawn |
| W-15 | `norm_case_path` | `workers.py:110,157`, `media.py:26`, `proxy.py:29`, `stems.song_key` (changes cache keys: one-time cache miss) | per-file owner |
| W-10, 11, 13, 14, 16-21 | not yet | see table above | unassigned |
