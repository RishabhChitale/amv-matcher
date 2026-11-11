import pytest
import time
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


def test_close_guard_never_blocks_and_destroys_once():
    import threading
    calls, main = [], threading.get_ident()
    ran_on, gate = [], threading.Event()
    g = ns.CloseGuard(lambda: calls.append("geom"),
                      lambda: (gate.wait(2), calls.append("js"), ran_on.append(threading.get_ident())),
                      lambda: calls.append("destroy"), grace=0, js_timeout=2)
    assert g() is False and calls == ["geom"]        # cancels the close, JS not run inline
    assert g() is False                              # second request while pending: still cancelled, no double fire
    gate.set()
    assert g.finished.wait(3)
    assert calls == ["geom", "js", "destroy"] and ran_on[0] != main
    assert g() is None                               # after destroy the close is allowed


def test_shutdown_guard_graceful_and_fallback():
    import threading
    # graceful: request_close runs once off the caller, cancel set -> no kill/exit
    calls, exits = [], []
    g = ns.ShutdownGuard(lambda: calls.append("close"), lambda: calls.append("kill"), exits.append, fallback=2)
    assert g.trigger() is True and g.trigger() is False          # idempotent
    g.cancel.set()
    assert g.finished.wait(3) and exits == []
    for _ in range(50):
        if calls == ["close"]:
            break
        time.sleep(0.05)
    assert calls == ["close"]
    # fallback: window never closes -> children killed, exit(0)
    calls, exits = [], []
    g = ns.ShutdownGuard(lambda: calls.append("close"), lambda: calls.append("kill"), exits.append, fallback=0.2)
    g.trigger()
    assert g.finished.wait(3) and calls == ["close", "kill"] and exits == [0]
    # request_close that blocks for ever (evaluate_js on a wedged main thread) must not stop the fallback
    calls, exits = [], []
    hang = threading.Event()
    g = ns.ShutdownGuard(hang.wait, lambda: calls.append("kill"), exits.append, fallback=0.3)
    t0 = time.time()
    g.trigger()
    assert g.finished.wait(3) and calls == ["kill"] and exits == [0] and time.time() - t0 < 2
    hang.set()
    # a second signal skips the grace period
    calls, exits = [], []
    g = ns.ShutdownGuard(threading.Event().wait, lambda: calls.append("kill"), exits.append, fallback=30)
    g.trigger(); g.trigger()
    assert g.finished.wait(3) and exits == [0]
    # request_close raising must not prevent the fallback
    exits = []
    def boom():
        raise RuntimeError("x")
    g = ns.ShutdownGuard(boom, lambda: None, exits.append, fallback=0.1)
    g.trigger()
    assert g.finished.wait(3) and exits == [0]


def test_kill_children_kills_descendants_only():
    import os, subprocess, sys, time
    code = ("import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);time.sleep(60)")
    kids = [subprocess.Popen([sys.executable, "-c", code]) for _ in range(2)]
    time.sleep(1.5)
    deep = ns.descendants()
    assert os.getpid() not in deep and len(deep) >= 4
    ns.kill_children()
    for _ in range(100):
        if all(k.poll() is not None for k in kids):
            break
        time.sleep(0.1)
    assert all(k.poll() is not None for k in kids)
    assert os.getpid() > 0  # the test process survived


def test_sigterm_kills_process_with_blocked_main_thread_and_hung_close():
    """Real process: main thread parked in a blocking C call (stands in for the Cocoa loop, where Python-level
    signal handlers never run) and a request_close that never returns -> SIGTERM still ends it within ~fallback."""
    import os, signal, subprocess, sys, time
    if sys.platform.startswith("win"):
        pytest.skip("POSIX signal path")
    code = ("import sys,threading,time,os;sys.path.insert(0,%r)\n"
            "from amv_matcher import nativeshell as ns\n"
            "g=ns.ShutdownGuard(threading.Event().wait, lambda: None, os._exit, fallback=1.0)\n"
            "g.install();print('ready',flush=True)\n"
            "threading.Event().wait()\n") % os.path.dirname(os.path.dirname(os.path.abspath(ns.__file__)))
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    try:
        assert p.stdout.readline().strip() == b"ready"
        t0 = time.time()
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=15) == 0 and time.time() - t0 < 8
    finally:
        if p.poll() is None:
            p.kill()
