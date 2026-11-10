import json
import multiprocessing
import os
import subprocess
import sys
import threading

from amv_matcher import nativeshell as ns

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_worker_child_detection():
    assert ns.is_worker_child(["x", "--multiprocessing-fork", "tracker_fd=3"])
    assert ns.is_worker_child(["x", "-B", "-S", "-I", "-c", "from multiprocessing.resource_tracker import main;main(3)"])
    assert not ns.is_worker_child(["x"]) or multiprocessing.parent_process() is not None


def test_freeze_support_is_first_statement():
    src = open(os.path.join(ROOT, "app_main.py")).read().split("\n")
    code = [l for l in src[src.index("from __future__ import annotations"):] if l.strip() and not l.startswith("#")]
    assert code[1] == "import multiprocessing" and code[2].startswith("multiprocessing.freeze_support()")


def test_second_acquire_fails_and_hands_over(tmp_path):
    got, ev = [], threading.Event()
    a = ns.SingleInstance(tmp_path, lambda args: (got.append(args), ev.set()))
    assert a.acquire()
    # a second process (flock is per open-file, so use a real second process) cannot take the lock
    code = ("import sys; sys.path.insert(0, %r); from amv_matcher import nativeshell as ns; "
            "b = ns.SingleInstance(%r); print(b.acquire()); print(ns.SingleInstance.notify_existing(%r, ['/x/y.mp4']))"
            % (ROOT, str(tmp_path), str(tmp_path)))
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30).stdout.split()
    assert out == ["False", "True"]
    assert ev.wait(5) and got == [["/x/y.mp4"]]
    a.release()
    assert not (tmp_path / "instance.json").exists()
    b = ns.SingleInstance(tmp_path)
    assert b.acquire()
    b.release()


def test_bad_token_ignored(tmp_path):
    got = []
    a = ns.SingleInstance(tmp_path, got.append)
    assert a.acquire()
    info = json.loads((tmp_path / "instance.json").read_text())
    import socket
    with socket.create_connection(("127.0.0.1", info["port"])) as s:
        s.sendall(b'{"token": "nope", "args": ["z"]}\n')
    import time
    time.sleep(0.5)
    assert got == []
    a.release()


def test_notify_without_instance(tmp_path):
    assert ns.SingleInstance.notify_existing(tmp_path, []) is False
