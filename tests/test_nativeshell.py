import json

from amv_matcher import nativeshell as ns
from amv_matcher import paths


def test_geometry_roundtrip_and_clamp(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA", tmp_path)
    assert ns.load_geometry() == ns.DEFAULT_GEOM
    assert ns.save_geometry(10, 20, 1600, 1000)
    assert ns.load_geometry() == {"width": 1600, "height": 1000, "x": 10, "y": 20}
    ns.save_geometry(0, 0, 100, 100)                       # tiny -> clamped to the minimum
    g = ns.load_geometry()
    assert (g["width"], g["height"]) == (ns.MIN_W, ns.MIN_H)


def test_geometry_offscreen_position_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA", tmp_path)
    ns.save_geometry(5000, 4000, 1200, 800)
    g = ns.load_geometry([(0, 0, 1920, 1080)])
    assert "x" not in g and g["width"] == 1200
    (tmp_path / "window.json").write_text("garbage")
    assert ns.load_geometry() == ns.DEFAULT_GEOM


def test_notify_escaping():
    cmd = ns.notify_cmd('Done "x"', 'a\\b\nc', platform="darwin")
    assert cmd[0] == "osascript" and '\\"x\\"' in cmd[2] and "\\\\" in cmd[2] and "\n" not in cmd[2]
    assert ns.notify_cmd("t", "b", platform="win32") is None


def test_resolve_cmd():
    assert ns.resolve_cmd("/x.mp4", "darwin", exists=lambda p: False) is None
    c = ns.resolve_cmd("/x.mp4", "darwin", exists=lambda p: True)
    assert c[:2] == ["open", "-a"] and c[-1] == "/x.mp4"
    w = ns.resolve_cmd("C:\\x.mp4", "win32", exists=lambda p: True)
    assert w[0].endswith("Resolve.exe")


def test_project_name():
    assert ns.project_name("my/proj:1") == "myproj1.amvproj"
    assert ns.project_name("") == "Untitled.amvproj"
    assert ns.project_name("a.amvproj") == "a.amvproj"
