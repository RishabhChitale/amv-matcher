"""Tiny job kinds for tests/test_workers.py; imported inside spawn workers."""
import os
import time

from amv_matcher.workers import register_kind, report_progress


def sleep_kind(path, secs=0.1, tag=None, **_):
    time.sleep(secs)
    return {"slept": secs, "tag": tag}


def crash_kind(path, code=1, **_):
    os._exit(code)


def echo_kind(path, marker=None, value=None, **_):
    if marker:
        with open(marker, "a") as f:
            f.write("x\n")
    return {"value": value}


def progress_kind(path, steps=4, delay=0.05, **_):
    for i in range(steps):
        time.sleep(delay)
        report_progress(100 * (i + 1) / steps)
    return {"steps": steps}


def rec_kind(path, log=None, name="", secs=0.02, **_):
    with open(log, "a") as f:
        f.write(name + "\n")
    time.sleep(secs)
    return name


def burn_kind(path, secs=1.0, tag=0, **_):
    end = time.time() + secs
    n = 0
    while time.time() < end:
        n += sum(i * i for i in range(2000))
    return n > 0


def fail_kind(path, **_):
    raise RuntimeError("boom")


register_kind("sleep", sleep_kind)
register_kind("crash", crash_kind)
register_kind("echo", echo_kind)
register_kind("progress", progress_kind)
register_kind("rec", rec_kind)
register_kind("burn", burn_kind)
register_kind("fail", fail_kind)
