"""Where things live. Dev mode: repo-local dot-dirs. Frozen app (PyInstaller): per-user app-data dir."""
from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
APP_NAME = "AMV Matcher"
REPO_ROOT = Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    """Folder that holds static/ (the PyInstaller bundle dir when frozen, else the repo)."""
    return Path(getattr(sys, "_MEIPASS", REPO_ROOT)) if FROZEN else REPO_ROOT


def data_dir() -> Path:
    """Writable per-user root. AMV_DATA_DIR overrides (tests, selftest)."""
    env = os.environ.get("AMV_DATA_DIR")
    if env:
        return Path(env).expanduser()
    if FROZEN:
        from platformdirs import user_data_dir
        new = Path(user_data_dir(APP_NAME, appauthor=False, roaming=False))   # %LOCALAPPDATA% on Windows (W-12)
        if sys.platform.startswith("win"):
            old = Path(user_data_dir(APP_NAME, appauthor=False, roaming=True))
            migrate_dir(old, new)
        return new
    return REPO_ROOT


def migrate_dir(old: Path, new: Path) -> bool:
    """One-time, idempotent move of the old roaming data dir into `new`. Never overwrites existing files in
    `new`; leaves a marker so it runs once; never raises. Returns True if anything was moved."""
    import shutil
    try:
        if old == new or not old.is_dir():
            return False
        marker = new / ".migrated-from-roaming"
        if marker.exists():
            return False
        new.mkdir(parents=True, exist_ok=True)
        moved = False
        for child in list(old.iterdir()):
            dest = new / child.name
            if dest.exists():
                continue
            try:
                shutil.move(str(child), str(dest))
                moved = True
            except OSError:
                pass
        marker.write_text(str(old), encoding="utf-8")
        return moved
    except OSError:
        return False


_dot = "" if (FROZEN or os.environ.get("AMV_DATA_DIR")) else "."
DATA = data_dir()
CACHE = DATA / f"{_dot}cache"
UPLOADS = DATA / f"{_dot}uploads"
EXPORTS = DATA / f"{_dot}exports"
STEMS_PACK = DATA / "stems-pack"          # optional Demucs/torch venv installed by the user


def default_export_dir() -> Path:
    """Where the native Save dialog opens: Movies/AMV Matcher (mac), Videos\\AMV Matcher (Windows)."""
    home = Path.home()
    if sys.platform.startswith("win"):
        base = known_videos_dir() or home / "Videos"
    else:
        base = home / "Movies"
    return base / APP_NAME


def known_videos_dir():
    """The user's real Videos folder (may be redirected, e.g. OneDrive) via the shell known-folder API; None if
    unavailable (W-14)."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes
        import uuid
        guid = uuid.UUID("{18989B1D-99B5-455B-841C-AB7C74E4DDFC}")      # FOLDERID_Videos
        class _G(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]
        g = _G(guid.time_low, guid.time_mid, guid.time_hi_version, (ctypes.c_ubyte * 8)(*guid.bytes[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) != 0:
            return None
        try:
            return Path(out.value) if out.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:
        return None


def _winsafe():
    from amv_matcher import winsafe
    return winsafe


def exports_member(src):
    """Resolved Path of an existing file/folder inside the exports dir, else None (resolve() + is_relative_to,
    so `..` and symlinks out of the exports dir are rejected; the exports dir itself is not a member)."""
    try:
        p = Path(str(src)).expanduser().resolve()
        root = EXPORTS.resolve()
        if p != root and _winsafe().is_relative_to_ci(p, root) and p.exists():
            return p
    except (OSError, ValueError, RuntimeError):
        pass
    return None
