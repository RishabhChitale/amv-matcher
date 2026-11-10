"""Windows-compat checks for pure path/platform logic, runnable on macOS (see docs/WINDOWS-AUDIT.md).
Known gaps are xfail(strict=False) with the audit id as reason."""
import ntpath
import sys
from pathlib import Path, PureWindowsPath

import pytest

from amv_matcher import decode, medialib, nativeshell, paths, stems


def test_notify_is_noop_on_windows():
    assert nativeshell.notify_cmd("t", "b", platform="win32") is None
    assert nativeshell.notify_cmd("t", "b", platform="darwin")[0] == "osascript"


def test_resolve_cmd_windows_vs_mac():
    win = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"
    cmd = nativeshell.resolve_cmd("x.xml", platform="win32", exists=lambda p: p == win)
    assert cmd == [win, "x.xml"]
    mac = nativeshell.resolve_cmd("x.xml", platform="darwin", exists=lambda p: p.endswith(".app"))
    assert mac[:2] == ["open", "-a"]
    assert nativeshell.resolve_cmd("x", platform="win32", exists=lambda p: False) is None


def test_default_export_dir_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert paths.default_export_dir().parts[-2:] == ("Videos", paths.APP_NAME)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert paths.default_export_dir().parts[-2:] == ("Movies", paths.APP_NAME)


def test_default_roots_use_videos_on_windows(monkeypatch, tmp_path):
    for n in ("Videos", "Movies", "Music"):
        (tmp_path / n).mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(sys, "platform", "win32")
    names = {Path(r).name for r in medialib.default_roots()}
    assert "Videos" in names and "Movies" not in names


def test_auto_devices_per_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert decode._auto_devices() == ["d3d11va", "dxva2"]
    monkeypatch.setattr(sys, "platform", "darwin")
    assert decode._auto_devices() == ["videotoolbox"]


def test_demucs_python_prefers_scripts_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(stems, "VENV_DIR", tmp_path)
    assert stems.demucs_python() == tmp_path / "bin" / "python"
    (tmp_path / "Scripts").mkdir()
    (tmp_path / "Scripts" / "python.exe").write_text("")
    assert stems.demucs_python() == tmp_path / "Scripts" / "python.exe"


def test_zip_member_filter_rejects_traversal():
    from amv_matcher import render
    assert render._wanted_member("pkg/rife-ncnn-vulkan.exe", "rife-ncnn-vulkan.exe")
    assert not render._wanted_member("pkg/other.exe", "rife-ncnn-vulkan.exe")


def test_sanitize_strips_backslash_dirs_and_trailing_dot():
    from amv_matcher.server import _sanitize_name
    assert _sanitize_name(r"C:\Users\me\clip.MP4") == "clip.mp4"
    assert not _sanitize_name("name. ").endswith((".", " "))


@pytest.mark.xfail(reason="W-07: reserved Windows device names (CON, NUL, COM1) not rejected", strict=False)
def test_sanitize_rejects_reserved_names():
    from amv_matcher.server import _sanitize_name
    assert _sanitize_name("NUL.mp4").split(".")[0].upper() != "NUL"


@pytest.mark.xfail(reason="W-04/05: allow-lists compare case-sensitively; Windows paths are case-insensitive", strict=False)
def test_allowlist_compare_is_case_insensitive_on_windows():
    a, b = r"C:\Users\Me\Videos\a.mp4", r"c:\users\me\videos\A.MP4"
    assert a == b or ntpath.normcase(a) == ntpath.normcase(b)
    assert PureWindowsPath(a).is_relative_to(PureWindowsPath(r"c:\users\me"))  # holds for pure paths
    assert a in {b}  # what server.py does today


@pytest.mark.xfail(reason="W-02: frozen build needs assets/icon.ico on Windows", strict=False)
def test_windows_icon_is_ico():
    spec = (Path(__file__).resolve().parents[1] / "amv_matcher.spec").read_text()
    assert "icon.ico" in spec


@pytest.mark.xfail(reason="W-12: app data should be local (roaming=False) on Windows", strict=False)
def test_data_dir_not_roaming():
    src = (Path(paths.__file__)).read_text()
    assert "roaming=True" not in src


@pytest.mark.xfail(reason="W-01: frozen venv bootstrap uses sys.executable (the app exe)", strict=False)
def test_stems_venv_bootstrap_not_sys_executable():
    src = (Path(paths.__file__).parent / "stems_api.py").read_text()
    assert '[sys.executable, "-m", "venv"' not in src
