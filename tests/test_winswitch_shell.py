"""Windows switch-over of the shell/server files (K4a), simulated: no real Windows, fast, tmp dirs."""
import os
import sys
from pathlib import Path

import pytest

import app_main
from amv_matcher import nativeshell as ns, paths, resolve, server, winsafe


def test_reserved_names_get_prefixed():
    for n in ("CON.mp4", "nul.wav", "Com1.txt", "LPT9.mkv", "aux"):
        assert server._sanitize_name(n).startswith("_"), n
    assert server._sanitize_name("console.mp4") == "console.mp4"
    assert server._sanitize_name("song.mp3") == "song.mp3"


def test_allowed_set_case_insensitive_on_win_exact_on_posix(tmp_path, monkeypatch):
    root = tmp_path / "Music"
    f = root / "A.mp3"
    swapped = str(f).swapcase()
    assert winsafe.is_relative_to_ci(f, root)
    assert not winsafe.is_relative_to_ci(swapped, root)          # POSIX: exact
    monkeypatch.setattr(os.path, "normcase", lambda s: s.lower())  # simulated Windows
    assert winsafe.is_relative_to_ci(swapped, root)
    assert winsafe.norm_case_path(f) == winsafe.norm_case_path(swapped)


def test_clear_dir_reports_locked_files_instead_of_raising(tmp_path, monkeypatch):
    (tmp_path / "ok.bin").write_bytes(b"1234")
    (tmp_path / "locked.bin").write_bytes(b"x")
    real = winsafe.safe_unlink

    def fake(path, root):
        if Path(path).name == "locked.bin":
            raise PermissionError("in use")
        return real(path, root)
    monkeypatch.setattr(winsafe, "safe_unlink", fake)
    skipped = []
    freed = server._clear_dir(tmp_path, skipped)
    assert skipped == ["locked.bin"]
    assert freed == 4
    assert (tmp_path / "locked.bin").exists() and not (tmp_path / "ok.bin").exists()


def test_roaming_migration_idempotent(tmp_path):
    old, new = tmp_path / "Roaming" / "AMV", tmp_path / "Local" / "AMV"
    old.mkdir(parents=True)
    (old / "proj.json").write_text("a")
    assert paths.migrate_dir(old, new) is True
    assert (new / "proj.json").read_text() == "a"
    (old / "late.json").write_text("b")
    assert paths.migrate_dir(old, new) is False                  # marker: runs once
    assert not (new / "late.json").exists()
    assert paths.migrate_dir(tmp_path / "missing", new) is False


def test_roaming_migration_never_overwrites(tmp_path):
    old, new = tmp_path / "o", tmp_path / "n"
    old.mkdir(); new.mkdir()
    (old / "x").write_text("old"); (new / "x").write_text("new")
    paths.migrate_dir(old, new)
    assert (new / "x").read_text() == "new"


def test_explorer_select_is_single_token():
    cmd = ns.explorer_select_cmd(Path("C:/Users/A B/My Videos/out.mp4"))
    assert len(cmd) == 2 and cmd[0] == "explorer" and cmd[1].startswith("/select,")


def test_known_videos_dir_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(paths, "known_videos_dir", lambda: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda c: tmp_path))
    assert paths.default_export_dir() == tmp_path / "Videos" / paths.APP_NAME
    monkeypatch.setattr(paths, "known_videos_dir", lambda: tmp_path / "OneDrive" / "Videos")
    assert paths.default_export_dir() == tmp_path / "OneDrive" / "Videos" / paths.APP_NAME


def test_known_videos_dir_none_off_windows():
    if not sys.platform.startswith("win"):
        assert paths.known_videos_dir() is None


def test_std_streams_guard(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    app_main.ensure_std_streams()
    print("still fine")
    sys.stderr.write("ok")


def test_resolve_setting_dir_expands_appdata(monkeypatch):
    monkeypatch.setenv("APPDATA", r"C:\Users\A\AppData\Roaming")
    if os.name == "nt":
        assert "%" not in resolve.setting_dir("win32")
    else:   # expandvars on POSIX does not know %VAR%: only check the mac branch expands ~
        assert "~" not in resolve.setting_dir("darwin")
    assert "AppData" in resolve.SETTING_DIR_WIN


def test_resolve_app_uses_program_files_env(monkeypatch):
    monkeypatch.setenv("ProgramFiles", r"D:\Apps")
    seen = []
    ns.resolve_app(platform="win32", exists=lambda p: seen.append(str(p)) or False)
    assert any(s.startswith("D:") and "Resolve.exe" in s for s in seen)


def test_normalize_drop_path_win_and_posix():
    assert ns.normalize_drop_path("c:/Users/a/x.mp4", platform="win32") == "C:\\Users\\a\\x.mp4"
    assert ns.normalize_drop_path("\\\\?\\c:\\Users\\x.mp4", platform="win32") == "C:\\Users\\x.mp4"
    assert ns.normalize_drop_path("/Users/a/x.mp4", platform="darwin") == "/Users/a/x.mp4"
