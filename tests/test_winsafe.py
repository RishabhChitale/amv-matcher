import os
import subprocess
import sys
from pathlib import Path

import pytest

from amv_matcher import winsafe as W


def test_popen_adds_no_window_flag_on_windows(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: seen.update(kw) or "p")
    monkeypatch.setattr(W, "_is_win", lambda: True)
    assert W.popen(["x"], creationflags=1) == "p"
    assert seen["creationflags"] == 1 | W.CREATE_NO_WINDOW


def test_popen_untouched_on_posix(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: seen.update(kw) or "p")
    monkeypatch.setattr(W, "_is_win", lambda: False)
    W.popen(["x"])
    assert "creationflags" not in seen


def test_run_real():
    assert W.run([sys.executable, "-c", "pass"]).returncode == 0


def test_replace_retry_succeeds_after_permission_errors(monkeypatch, tmp_path):
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError
    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    W.replace_retry("a", "b")
    assert calls["n"] == 3


def test_replace_retry_gives_up(monkeypatch):
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    with pytest.raises(PermissionError):
        W.replace_retry("a", "b", tries=3)


def test_replace_retry_real(tmp_path):
    (tmp_path / "a").write_text("1")
    W.replace_retry(tmp_path / "a", tmp_path / "b")
    assert (tmp_path / "b").read_text() == "1"


def test_safe_unlink_respects_root(tmp_path):
    root = tmp_path / "r"; root.mkdir()
    f = root / "f"; f.write_text("x")
    out = tmp_path / "o"; out.write_text("x")
    assert W.safe_unlink(out, root) is False and out.exists()
    assert W.safe_unlink(f, root) is True and not f.exists()
    assert W.safe_unlink(f, root) is True  # already gone


def test_safe_unlink_retries(monkeypatch, tmp_path):
    f = tmp_path / "f"; f.write_text("x")
    n = {"i": 0}
    real = Path.unlink

    def flaky(self, missing_ok=False):
        n["i"] += 1
        if n["i"] < 3:
            raise PermissionError
        return real(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", flaky)
    monkeypatch.setattr(W.time, "sleep", lambda s: None)
    assert W.safe_unlink(f) is True and n["i"] == 3


def test_safe_rmtree_refuses_root_outside_and_traversal(tmp_path):
    root = tmp_path / "r"; (root / "d").mkdir(parents=True)
    (root / "d" / "x").write_text("1")
    assert W.safe_rmtree(root, root) is False
    assert W.safe_rmtree(tmp_path, root) is False
    assert W.safe_rmtree(root / "d" / ".." / "..", root) is False
    assert W.safe_rmtree(root / "d", root) is True and not (root / "d").exists()


def test_safe_rmtree_rejects_symlink_escape(tmp_path):
    root = tmp_path / "r"; root.mkdir()
    out = tmp_path / "o"; out.mkdir()
    (root / "link").symlink_to(out)
    assert W.safe_rmtree(root / "link", root) is False and out.exists()


def test_case_insensitive_on_windows(monkeypatch):
    import ntpath
    monkeypatch.setattr(os.path, "normcase", ntpath.normcase)
    monkeypatch.setattr(os.path, "abspath", lambda p: p)
    monkeypatch.setattr(os, "sep", "\\")
    assert W.norm_case_path(r"C:\Users\Me\A.MP4") == r"c:\users\me\a.mp4"
    assert W.is_relative_to_ci(r"c:\users\me\Videos\a.mp4", r"C:\Users\ME")
    assert W.is_relative_to_ci(r"C:\Users\Me", r"c:\users\me")
    assert not W.is_relative_to_ci(r"C:\Users\Menu", r"C:\Users\Me")


@pytest.mark.skipif(os.name == "nt", reason="posix case rules")
def test_case_sensitive_on_posix():
    assert not W.is_relative_to_ci("/a/B/c", "/a/b")
    assert W.is_relative_to_ci("/a/b/c", "/a/b")
    assert not W.is_relative_to_ci("/a/bc", "/a/b")


@pytest.mark.skipif(os.name == "nt", reason="posix kill_tree path")
def test_kill_tree_kills_grandchild(tmp_path):
    marker = tmp_path / "pid"
    code = ("import subprocess,sys,time;"
            "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            f"open({str(marker)!r},'w').write(str(c.pid));time.sleep(60)")
    p = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    for _ in range(100):
        if marker.exists() and marker.read_text():
            break
        import time; time.sleep(0.05)
    gpid = int(marker.read_text())
    W.kill_tree(p)
    p.wait(5)
    import time; time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(gpid, 0)


def test_kill_tree_windows_uses_taskkill(monkeypatch):
    calls = []

    class P:
        pid = 7
        def poll(self): return None
    monkeypatch.setattr(W, "_is_win", lambda: True)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    W.kill_tree(P())
    assert calls[0][0] == ["taskkill", "/PID", "7", "/T", "/F"]
    assert calls[0][1]["creationflags"] == W.CREATE_NO_WINDOW


def test_icon_ico_has_sizes():
    Image = pytest.importorskip("PIL.Image")
    ico = Image.open(Path(__file__).resolve().parents[1] / "assets" / "icon.ico")
    assert {16, 32, 256} <= {s[0] for s in ico.info["sizes"]}
