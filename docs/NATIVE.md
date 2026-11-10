# Native shell APIs (app_main.py, amv_matcher/nativeshell.py, static/shell.js)

`window.pywebview.api` (desktop app only; all no-ops in a plain browser):

| Method | Does |
|---|---|
| `notify(title, body)` | Native notification (macOS `osascript`; Windows: no-op). Use `nativeNotify` so it only fires when the window is unfocused. |
| `resolve_installed()` | True if DaVinci Resolve is installed (`/Applications/DaVinci Resolve/...` or `C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe`). |
| `open_in_resolve(src)` | Opens an export (must be inside the exports dir) in Resolve. |
| `pick_project_open()` / `pick_project_save(name)` | Native dialogs for `.amvproj`; return a path or `''`. |
| `pick_video/pick_songs/pick_folder/save_as/open_path/reveal_path/log` | unchanged |

Window size and position persist to `<data>/window.json` (saved on resize, move and close; an off-screen saved position is dropped).

Native menu bar (File, Edit, View, Playback, Help) calls `window.nativeMenu(action)`, which dispatches
`document` event `amv-menu` with `detail.action` in: `new open save save_as add_folder export undo redo delete
toggle_media toggle_stems toggle_effects play_pause help`. Cmd/Ctrl+S, Shift+S, O, N, Z, Shift+Z fire the same event.
`window.nativeBeforeQuit` (if set to a function) is called when the window closes, for autosave.
`window.nativeProjectDialogs`, `window.nativeResolve`, `window.nativeNotify` wrap the API calls.
