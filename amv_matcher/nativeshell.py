"""Pure helpers behind the desktop shell's native APIs (window state, notifications, Resolve launch).
No pywebview import here, so everything is unit-testable."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from . import paths

MIN_W, MIN_H = 1100, 700
DEFAULT_GEOM = {"width": 1440, "height": 900}


def _state_file() -> Path:
    return paths.DATA / "window.json"


def load_geometry(screens=None) -> dict:
    """Saved window geometry {width,height[,x,y]}; falls back to the default size. If `screens` ([(x,y,w,h)...])
    is given and the saved position is not on any screen, the position is dropped (the OS centres the window)."""
    g = dict(DEFAULT_GEOM)
    try:
        d = json.loads(_state_file().read_text())
        w, h = int(d["width"]), int(d["height"])
        g["width"], g["height"] = max(MIN_W, min(w, 10000)), max(MIN_H, min(h, 10000))
        if "x" in d and "y" in d:
            x, y = int(d["x"]), int(d["y"])
            if screens is None or any(sx <= x + 40 < sx + sw and sy <= y + 20 < sy + sh for sx, sy, sw, sh in screens):
                g["x"], g["y"] = x, y
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return g


def save_geometry(x, y, width, height) -> bool:
    try:
        vals = [int(v) for v in (x, y, width, height)]
        paths.DATA.mkdir(parents=True, exist_ok=True)
        tmp = _state_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(zip(("x", "y", "width", "height"), vals))))
        os.replace(tmp, _state_file())
        return True
    except (OSError, ValueError, TypeError):
        return False


def applescript_str(s) -> str:
    """Quote a string as an AppleScript literal (backslash and double quote escaped, newlines flattened)."""
    s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")
    return '"' + s[:300] + '"'


def notify_cmd(title, body, platform=None):
    """argv that shows a native notification, or None when unsupported (Windows is best-effort: no-op)."""
    if (platform or sys.platform) == "darwin":
        return ["osascript", "-e", f"display notification {applescript_str(body)} with title {applescript_str(title)}"]
    return None


def resolve_app(platform=None, exists=os.path.exists):
    """Path of the installed DaVinci Resolve, or None."""
    p = platform or sys.platform
    cands = {"darwin": ["/Applications/DaVinci Resolve/DaVinci Resolve.app"],
             "win32": [r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"]}.get(p, [])
    return next((c for c in cands if exists(c)), None)


def resolve_cmd(src, platform=None, exists=os.path.exists):
    """argv that opens `src` in Resolve, or None if Resolve is absent."""
    app = resolve_app(platform, exists)
    if not app:
        return None
    return ["open", "-a", app, str(src)] if (platform or sys.platform) == "darwin" else [app, str(src)]


def project_name(name) -> str:
    """Safe default file name for a project (.amvproj)."""
    stem = "".join(c for c in str(name or "") if c.isalnum() or c in " -_.")[:80].strip(" .") or "Untitled"
    return stem if stem.endswith(".amvproj") else stem + ".amvproj"


MENU_ACTIONS = ("new", "open", "save", "save_as", "add_folder", "export", "undo", "redo", "delete",
                "toggle_media", "toggle_stems", "toggle_effects", "play_pause", "help")


def run(cmd) -> bool:
    if not cmd:
        return False
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False
