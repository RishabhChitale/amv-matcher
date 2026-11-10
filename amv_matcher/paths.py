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
        return Path(user_data_dir(APP_NAME, appauthor=False, roaming=True))
    return REPO_ROOT


_dot = "" if (FROZEN or os.environ.get("AMV_DATA_DIR")) else "."
DATA = data_dir()
CACHE = DATA / f"{_dot}cache"
UPLOADS = DATA / f"{_dot}uploads"
EXPORTS = DATA / f"{_dot}exports"
STEMS_PACK = DATA / "stems-pack"          # optional Demucs/torch venv installed by the user


def default_export_dir() -> Path:
    """Where the native Save dialog opens: Movies/AMV Matcher (mac), Videos\\AMV Matcher (Windows)."""
    home = Path.home()
    base = home / ("Videos" if sys.platform.startswith("win") else "Movies")
    return base / APP_NAME
