"""Background analysis worker pool (process based, spawn context).

Heavy work (probe, analysis, thumbnails, proxies) never runs on the server
event loop: the server submits jobs here and polls ``events(since_seq)``.

Public API (see ``WorkerPool``):
    submit(kind, path, priority=1, **params) -> job_id
    cancel(job_id) -> bool          bump(job_id, priority) -> bool
    pause_prewarm() / resume_prewarm()
    status(job_id) -> dict | None   jobs(state=None) -> [dict]
    events(since_seq=0) -> [dict]   subscribe(cb) -> unsubscribe()
    stats() -> dict                 shutdown(wait=False)

Priorities: 0 = user's current video/song, 1 = visible Media items,
2 = idle pre-warming (pausable; held on battery power when detectable).

Job kinds are top-level pure functions ``fn(path, **params) -> result``
registered with ``register_kind``.  Worker processes are started with the
spawn context, so every kind must be importable there: kinds defined in
other modules are loaded in workers through ``WorkerPool(kind_modules=[...])``.
Frozen (PyInstaller) builds need ``multiprocessing.freeze_support()`` as the
first statement under ``if __name__ == "__main__":`` in app_main.py.

Job dict: id, kind, path, params, priority, state
(queued|running|done|error|cancelled), progress (0-100), result, error,
attempts, cached, created/started/finished (epoch seconds).
Event dict: seq (monotonic), ts, job_id, kind, path, state, progress,
priority, error (None unless state == 'error').
"""
from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing as mp
import os
import sys
import threading
import time
from collections import deque
from multiprocessing.connection import wait as _mp_wait

from . import winsafe

QUEUED, RUNNING, DONE, ERROR, CANCELLED = "queued", "running", "done", "error", "cancelled"
PRIORITIES = (0, 1, 2)
DEFAULT_TIMEOUT = 1800.0
_log = logging.getLogger(__name__)
DEFAULT_SPAWN_TIMEOUT = 120.0

# ---------------------------------------------------------------- registry
_KINDS = {}      # name -> fn(path, **params)
_CACHED = {}     # name -> fn(path, **params) -> result | None  (parent-side)
_progress_hook = None


def register_kind(name, fn, cached=None):
    """Register ``fn(path, **params) -> result``.  ``cached`` (optional) is
    ``cached(path, **params) -> result | None`` and lets a submit of an already
    cached input finish immediately without any worker."""
    _KINDS[name] = fn
    if cached is not None:
        _CACHED[name] = cached
    return fn


def report_progress(pct):
    """Called by kinds inside a worker to report 0-100 progress."""
    h = _progress_hook
    if h is not None:
        try:
            h(max(0, min(100, int(pct))))
        except Exception:
            pass


# ------------------------------------------------------------ builtin kinds
def _fresh_json(path, key):
    try:
        with open(path) as f:
            return json.load(f).get(key)
    except Exception:
        return None


def _songs_cache_path(cache_path=None):
    if cache_path:
        return cache_path
    from . import audio
    return audio.CACHE_PATH


def _song_cached(path, cache_path=None, **_):
    try:
        from . import audio
        hit = _fresh_json(_songs_cache_path(cache_path), audio._key(path))
        return hit if audio.is_current(hit) else None     # older entries: upgraded by analyze_song
    except Exception:
        return None


def _song_analysis(path, cache_path=None, **_):
    from . import audio
    if os.environ.get("AMV_TEST_CRASH_WORKER") == "1":    # test hook: die like a SIGSEGV (worker process only)
        os._exit(139)
    report_progress(5)
    r = audio.analyze_song(path, cache_path=_songs_cache_path(cache_path))
    report_progress(100)
    return r


def _video_cache_file(path, cache_dir=None):
    from . import paths
    st = os.stat(path)
    key = f"v1|{winsafe.norm_case_path(path)}|{int(st.st_mtime)}|{st.st_size}"
    d = cache_dir or (paths.CACHE / "videos")
    return os.path.join(str(d), hashlib.sha1(key.encode()).hexdigest() + ".json")


def _video_cached(path, cache_dir=None, **_):
    try:
        with open(_video_cache_file(path, cache_dir)) as f:
            hit = json.load(f)
        return hit if "camera_motion" in hit else None     # pre-F9 entries: re-analysed in place
    except Exception:
        return None


def _video_analysis(path, cache_dir=None, **_):
    from . import video
    report_progress(5)
    r = video.analyze_video(path)
    f = _video_cache_file(path, cache_dir)
    os.makedirs(os.path.dirname(f), exist_ok=True)
    tmp = f"{f}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(r, fh, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
    winsafe.replace_retry(tmp, f)
    report_progress(100)
    return r


def _probe(path, **_):
    import av
    with av.open(path) as c:
        out = {"duration": float(c.duration / 1e6) if c.duration else None,
               "format": c.format.name, "streams": []}
        for s in c.streams:
            d = {"type": s.type, "codec": s.codec_context.name}
            if s.type == "video":
                d.update(width=s.codec_context.width, height=s.codec_context.height,
                         fps=float(s.average_rate) if s.average_rate else None)
            elif s.type == "audio":
                d.update(sample_rate=s.codec_context.sample_rate,
                         channels=s.codec_context.channels)
            out["streams"].append(d)
    return out


def _thumb_dir(path, n, width, cache_dir=None):
    from . import paths
    st = os.stat(path)
    key = f"t1|{winsafe.norm_case_path(path)}|{int(st.st_mtime)}|{st.st_size}|{n}|{width}"
    root = cache_dir or (paths.CACHE / "thumbs")
    return os.path.join(str(root), hashlib.sha1(key.encode()).hexdigest())


def _thumbs_cached(path, n=1, width=160, cache_dir=None, **_):
    try:
        d = _thumb_dir(path, n, width, cache_dir)
        files = [os.path.join(d, f"{i}.jpg") for i in range(n)]
        return {"files": files} if all(os.path.exists(f) for f in files) else None
    except Exception:
        return None


def _thumbnails(path, n=1, width=160, cache_dir=None, **_):
    """n evenly spaced JPEG frames (n=1: poster; n>1: filmstrip)."""
    import cv2
    d = _thumb_dir(path, n, width, cache_dir)
    os.makedirs(d, exist_ok=True)
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ValueError("OpenCV could not open the video")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    files = []
    for i in range(n):
        if total > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int((i + 0.5) * total / n))
        ok, fr = cap.read()
        if not ok:
            raise ValueError("could not read frame %d" % i)
        h = max(1, int(fr.shape[0] * width / fr.shape[1]))
        fr = cv2.resize(fr, (width, h), interpolation=cv2.INTER_AREA)
        f = os.path.join(d, f"{i}.jpg")
        cv2.imwrite(f, fr)
        files.append(f)
        report_progress(100 * (i + 1) / n)
    cap.release()
    return {"files": files}


def _proxy(path, **_):
    from pathlib import Path
    from . import proxy
    report_progress(5)
    out = str(proxy.build(Path(path)))
    report_progress(100)
    return {"proxy": out}


register_kind("song_analysis", _song_analysis, _song_cached)
register_kind("video_analysis", _video_analysis, _video_cached)
register_kind("probe", _probe)
register_kind("thumbnails", _thumbnails, _thumbs_cached)

def _filmstrip(path, n=12, width=160, cache_dir=None, **_):
    return _thumbnails(path, n=n, width=width, cache_dir=cache_dir)


def _filmstrip_cached(path, n=12, width=160, cache_dir=None, **_):
    return _thumbs_cached(path, n=n, width=width, cache_dir=cache_dir)


register_kind("filmstrip", _filmstrip, _filmstrip_cached)
register_kind("proxy", _proxy)


# ------------------------------------------------------------ worker process
def _lower_priority():
    """Keep the UI/server responsive under CPU load. POSIX: os.nice(5); Windows (no os.nice):
    BELOW_NORMAL_PRIORITY_CLASS via ctypes. Never raises."""
    try:
        if hasattr(os, "nice") and not winsafe._is_win():
            os.nice(5)
        elif winsafe._is_win():
            import ctypes
            k = ctypes.windll.kernel32
            k.GetCurrentProcess.restype = ctypes.c_void_p
            k.SetPriorityClass(ctypes.c_void_p(k.GetCurrentProcess()), 0x00004000)
    except Exception:
        pass


def _own_process_group():
    """POSIX: make the worker a process-group leader so kill_tree can reach its children
    (rife, ffmpeg). No-op on Windows (taskkill /T walks the tree)."""
    try:
        if not winsafe._is_win() and hasattr(os, "setpgrp"):
            os.setpgrp()
    except Exception:
        pass


class _ProcAdapter:
    """Popen-like view of a multiprocessing.Process for winsafe.kill_tree."""

    def __init__(self, p):
        self._p, self.pid = p, p.pid

    def poll(self):
        return None if self._p.is_alive() else 0

    def kill(self):
        self._p.kill()


def _worker_main(conn, kind_modules):
    """Child entry point (top-level, picklable under spawn)."""
    global _progress_hook
    import importlib
    _lower_priority()
    _own_process_group()
    try:
        for m in kind_modules or ():
            importlib.import_module(m)
    except BaseException:  # noqa: BLE001 - tell the parent why we cannot serve jobs
        import traceback
        try:
            conn.send(("initfail", None, traceback.format_exc()[-1500:]))
        except Exception:
            pass
        return
    send_lock = threading.Lock()

    def send(msg):
        with send_lock:
            conn.send(msg)

    cur = {"id": None, "last": -1}

    def hook(p):
        if p != cur["last"]:
            cur["last"] = p
            send(("progress", cur["id"], p))

    _progress_hook = hook
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg is None:
            return
        jid, kind, path, params = msg
        cur["id"], cur["last"] = jid, -1
        try:
            send(("started", jid, None))  # handshake: the job timeout clock starts here
            fn = _KINDS[kind]
            res = fn(path, **params)
            send(("done", jid, res))
        except BaseException as e:  # noqa: BLE001 - report everything
            try:
                send(("error", jid, f"{type(e).__name__}: {e}"))
            except Exception:
                return


# ------------------------------------------------------------ power detection
def on_battery():
    """True when running on battery, False on AC, None if unknown."""
    try:
        import psutil
        b = psutil.sensors_battery()
        if b is not None:
            return not b.power_plugged
    except Exception:
        pass
    try:
        if sys.platform == "darwin":
            import subprocess
            out = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                                 text=True, timeout=3).stdout
            if "Battery Power" in out:
                return True
            if "AC Power" in out:
                return False
        elif sys.platform.startswith("win"):
            import ctypes
            from ctypes import wintypes

            class SPS(ctypes.Structure):
                _fields_ = [("ACLineStatus", wintypes.BYTE), ("BatteryFlag", wintypes.BYTE),
                            ("BatteryLifePercent", wintypes.BYTE), ("SystemStatusFlag", wintypes.BYTE),
                            ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]
            s = SPS()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
                if s.ACLineStatus == 0:
                    return True
                if s.ACLineStatus == 1:
                    return False
    except Exception:
        pass
    return None


# ------------------------------------------------------------------- pool
class _Slot:
    __slots__ = ("proc", "conn", "job", "t0", "started", "init_err")

    def __init__(self):
        self.proc = self.conn = self.job = None
        self.t0 = 0.0       # dispatch time
        self.started = None  # worker's 'started' handshake time (None until then)
        self.init_err = None  # traceback tail reported by a child that failed to start


class WorkerPool:
    def __init__(self, workers=None, timeout=DEFAULT_TIMEOUT, kind_modules=(),
                 spawn_timeout=DEFAULT_SPAWN_TIMEOUT, battery_detector=on_battery, battery_poll=15.0, max_events=5000):
        n = workers if workers else max(1, (os.cpu_count() or 2) - 2)   # keep 2 cores free for the UI/WebKit
        self._ctx = mp.get_context("spawn")
        self._kind_modules = list(kind_modules)
        self._timeout = timeout  # counts from the worker's 'started' handshake only
        self._spawn_timeout = spawn_timeout  # dispatch -> handshake (cold worker start)
        self._detector = battery_detector
        self._battery_poll = battery_poll
        self._battery = (False, 0.0)
        self._lock = threading.RLock()
        self._slots = [_Slot() for _ in range(n)]
        self._jobs = {}
        self._order = 0
        self._by_key = {}
        self._events = deque(maxlen=max_events)
        self._seq = 0
        self._subs = []
        self._paused = False
        self._closing = False
        self._stall_t0 = None  # watchdog: since when a dispatchable job sits queued with every slot idle
        self._nid = 0
        self._counts = {DONE: 0, ERROR: 0, CANCELLED: 0}
        self._thread = threading.Thread(target=self._loop, name="amv-workers", daemon=True)
        self._thread.start()

    # ---- helpers
    @staticmethod
    def _key(kind, path, params):
        try:
            st = os.stat(path)
            sig = (st.st_size, st.st_mtime_ns)
        except (OSError, TypeError, ValueError):
            sig = (None, None)
        ph = hashlib.sha1(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()
        try:
            kp = winsafe.norm_case_path(path)
        except (TypeError, ValueError):
            kp = str(path)
        return (kind, kp, sig, ph)

    def _emit(self, job, error=None):
        self._seq += 1
        ev = {"seq": self._seq, "ts": time.time(), "job_id": job["id"], "kind": job["kind"],
              "path": job["path"], "state": job["state"], "progress": job["progress"],
              "priority": job["priority"], "error": error}
        self._events.append(ev)
        for cb in list(self._subs):
            try:
                cb(dict(ev))
            except Exception:
                pass

    def _view(self, j):
        return {k: v for k, v in j.items() if not k.startswith("_")}

    # ---- public API
    def submit(self, kind, path, priority=1, **params):
        if kind not in _KINDS:
            raise ValueError(f"unknown job kind: {kind}")
        if priority not in PRIORITIES:
            raise ValueError("priority must be 0, 1 or 2")
        path = str(path) if path is not None else ""
        force = params.pop("force", False)
        key = self._key(kind, path, params)
        with self._lock:
            if self._closing:
                raise RuntimeError("pool is shut down")
            old = self._by_key.get(key)
            if old and not force:
                j = self._jobs[old]
                if j["state"] in (QUEUED, RUNNING, DONE):
                    if j["state"] == QUEUED and priority < j["priority"]:
                        j["priority"] = priority
                        self._emit(j)
                    return old
            self._nid += 1
            jid = f"j{self._nid}"
            self._order += 1
            job = {"id": jid, "kind": kind, "path": path, "params": params, "priority": priority,
                   "state": QUEUED, "progress": 0, "result": None, "error": None, "attempts": 0,
                   "cached": False, "created": time.time(), "started": None, "finished": None,
                   "_key": key, "_order": self._order}
            self._jobs[jid] = job
            self._by_key[key] = jid
            chk = _CACHED.get(kind)
            if chk is not None and not force:
                try:
                    hit = chk(path, **params)
                except Exception:
                    hit = None
                if hit is not None:
                    job.update(state=DONE, progress=100, result=hit, cached=True,
                               finished=time.time())
                    self._counts[DONE] += 1
                    self._emit(job)
                    return jid
            self._emit(job)
            return jid

    def cancel(self, job_id):
        with self._lock:
            j = self._jobs.get(job_id)
            if not j or j["state"] not in (QUEUED, RUNNING):
                return False
            if j["state"] == RUNNING:
                for s in self._slots:
                    if s.job == job_id:
                        self._kill(s)
            self._finish(j, CANCELLED)
            return True

    def bump(self, job_id, priority):
        if priority not in PRIORITIES:
            raise ValueError("priority must be 0, 1 or 2")
        with self._lock:
            j = self._jobs.get(job_id)
            if not j or j["state"] != QUEUED:
                return False
            if j["priority"] != priority:
                j["priority"] = priority
                self._emit(j)
            return True

    def pause_prewarm(self):
        with self._lock:
            self._paused = True

    def resume_prewarm(self):
        with self._lock:
            self._paused = False

    def status(self, job_id):
        with self._lock:
            j = self._jobs.get(job_id)
            return self._view(j) if j else None

    def jobs(self, state=None):
        with self._lock:
            return [self._view(j) for j in self._jobs.values() if state is None or j["state"] == state]

    def events(self, since_seq=0):
        with self._lock:
            return [dict(e) for e in self._events if e["seq"] > since_seq]

    def subscribe(self, cb):
        with self._lock:
            self._subs.append(cb)

        def unsub():
            with self._lock:
                if cb in self._subs:
                    self._subs.remove(cb)
        return unsub

    def stats(self):
        with self._lock:
            q = {p: 0 for p in PRIORITIES}
            for j in self._jobs.values():
                if j["state"] == QUEUED:
                    q[j["priority"]] += 1
            return {"queued": q, "workers": len(self._slots),
                    "busy": sum(1 for s in self._slots if s.job), "running": sum(1 for s in self._slots if s.job),
                    "done": self._counts[DONE], "error": self._counts[ERROR],
                    "cancelled": self._counts[CANCELLED], "prewarm_paused": self._paused,
                    "on_battery": self._battery[0], "seq": self._seq}

    def shutdown(self, wait=False):
        with self._lock:
            self._closing = True
            for j in self._jobs.values():
                if j["state"] == QUEUED:
                    self._finish(j, CANCELLED)
        if wait:
            end = time.time() + 600
            while time.time() < end and any(s.job for s in self._slots):
                time.sleep(0.02)
        self._thread.join(timeout=5)
        with self._lock:
            for s in self._slots:
                if s.job:
                    j = self._jobs.get(s.job)
                    if j and j["state"] == RUNNING:
                        self._finish(j, CANCELLED)
                self._kill(s)

    # ---- internals (lock held)
    def _finish(self, j, state, error=None, result=None):
        j["state"] = state
        j["finished"] = time.time()
        if state == DONE:
            j["progress"] = 100
            j["result"] = result
        j["error"] = error
        self._counts[state] += 1
        if state in (ERROR, CANCELLED) and self._by_key.get(j["_key"]) == j["id"]:
            del self._by_key[j["_key"]]
        self._emit(j, error)

    def _kill(self, s):
        p, c = s.proc, s.conn
        s.proc = s.conn = s.job = None
        s.init_err = None
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
        if p is not None:
            try:
                if p.is_alive():
                    winsafe.kill_tree(_ProcAdapter(p))   # worker + grandchildren (rife, ffmpeg) (W-08)
                    p.terminate()
                    p.join(2)
                    if p.is_alive():
                        p.kill()
                        p.join(2)
                else:
                    p.join(0)
            except Exception:
                pass

    def _ensure(self, s):
        if s.proc is None:
            parent, child = self._ctx.Pipe(duplex=True)
            p = self._ctx.Process(target=_worker_main, args=(child, self._kind_modules), daemon=True)
            p.start()
            child.close()
            s.proc, s.conn = p, parent

    def _battery_hold(self):
        now = time.time()
        if now - self._battery[1] >= self._battery_poll:
            try:
                v = bool(self._detector())
            except Exception:
                v = False
            self._battery = (v, now)
        return self._battery[0]

    def _next_job(self):
        hold2 = self._paused or self._battery_hold()
        best = None
        for j in self._jobs.values():
            if j["state"] != QUEUED or (j["priority"] == 2 and hold2):
                continue
            k = (j["priority"], j["_order"])
            if best is None or k < best[0]:
                best = (k, j)
        return best[1] if best else None

    def _dispatch(self):
        for s in self._slots:
            if s.job is not None:
                continue
            j = self._next_job()
            if j is None:
                return
            try:
                self._ensure(s)
                j["attempts"] += 1
                s.conn.send((j["id"], j["kind"], j["path"], j["params"]))
            except Exception as e:  # noqa: BLE001 - e.g. a Windows-only spawn/pipe failure
                self._kill(s)
                j["attempts"] -= 1
                j["_startfails"] = j.get("_startfails", 0) + 1
                if j["_startfails"] >= 3:
                    self._finish(j, ERROR, error=f"worker failed to start: {type(e).__name__}: {e}"[:500])
                continue
            s.job, s.t0, s.started = j["id"], time.time(), None
            j["state"], j["started"] = RUNNING, time.time()
            self._emit(j)

    def _crashed(self, s, why):
        jid = s.job
        if s.init_err and why == "worker crashed":
            why = "worker failed to start"
        if why == "worker failed to start":
            code = None
            try:
                code = s.proc.exitcode if s.proc is not None else None
            except Exception:
                pass
            why = f"{why} (exit code {code})"
            if s.init_err:
                why += ": " + s.init_err.strip().splitlines()[-1][:300]
        self._kill(s)
        j = self._jobs.get(jid)
        if j and j["state"] == RUNNING:
            if why == "worker crashed" and j["attempts"] < 2:
                j["state"], j["progress"] = QUEUED, 0
                self._emit(j)
            else:
                self._finish(j, ERROR, error=why)

    def _loop(self):
        """Crash-proof scheduler: an exception in one tick is logged, never kills the thread; repeated failures error the jobs."""
        fails = 0
        while True:
            try:
                if self._tick():
                    return
                fails = 0
            except Exception as e:  # noqa: BLE001
                fails += 1
                _log.exception("amv-workers scheduler tick failed (%d)", fails)
                if fails >= 3:
                    fails = 0
                    try:
                        self._fail_all(f"scheduler failed: {type(e).__name__}: {e}"[:500])
                    except Exception:  # noqa: BLE001
                        _log.exception("amv-workers could not fail jobs")
                if self._closing:
                    return
                time.sleep(0.05)

    def _fail_all(self, why):
        with self._lock:
            for s in self._slots:
                if s.job is not None or s.proc is not None:
                    self._kill(s)
            for j in list(self._jobs.values()):
                if j["state"] in (QUEUED, RUNNING):
                    self._finish(j, ERROR, error=why)

    def _watchdog(self):
        """A dispatchable job that stays queued with every slot idle for > spawn_timeout is errored, not left hanging."""
        if not self._spawn_timeout or self._closing:
            return
        if any(s.job is not None for s in self._slots):
            self._stall_t0 = None
            return
        j = self._next_job()
        if j is None:
            self._stall_t0 = None
            return
        now = time.time()
        if self._stall_t0 is None:
            self._stall_t0 = now
        elif now - self._stall_t0 > self._spawn_timeout:
            self._stall_t0 = None
            self._finish(j, ERROR, error=f"worker pool stalled: job stayed queued with no worker started for {self._spawn_timeout:g}s")

    def _tick(self):
        """One scheduler pass; True = exit the thread."""
        with self._lock:
            if self._closing and not any(s.job for s in self._slots):
                return True
            if not self._closing:
                self._dispatch()
                self._watchdog()
            conns = [s.conn for s in self._slots if s.job is not None and s.conn is not None]
        try:
            ready = _mp_wait(conns, timeout=0.02) if conns else []
        except (OSError, ValueError):
            # cancel()/_kill() closed a conn after we snapshotted it; on Windows wait() raises WinError 6 (handle invalid) instead of
            # ignoring it, which used to kill this thread silently and leave every later job 'queued' forever
            ready = []
            time.sleep(0.02)
        if not conns:
            time.sleep(0.02)
        with self._lock:
            for s in self._slots:
                if s.job is None:
                    continue
                if s.conn in ready:
                    try:
                        while s.conn.poll():
                            self._handle(s, s.conn.recv())
                            if s.job is None:
                                break
                    except (EOFError, OSError):
                        if s.job is not None:
                            self._crashed(s, "worker crashed")
                        continue
                if s.job is not None:
                    if s.proc is not None and not s.proc.is_alive() and not (s.conn and s.conn.poll()):
                        self._crashed(s, "worker crashed")
                    elif s.started is None:
                        if self._spawn_timeout and time.time() - s.t0 > self._spawn_timeout:
                            self._crashed(s, "worker failed to start")
                    elif self._timeout and time.time() - s.started > self._timeout:
                        self._crashed(s, f"timeout after {self._timeout:g}s")
        return self._closing

    def _handle(self, s, msg):
        tag, jid, val = msg
        if tag == "initfail":
            s.init_err = val
            return
        j = self._jobs.get(jid)
        if j is None or s.job != jid:
            return
        if tag == "started":
            s.started = time.time()
            j["started"] = s.started
        elif tag == "progress":
            if val != j["progress"]:
                j["progress"] = val
                self._emit(j)
        elif tag == "done":
            s.job = None
            self._finish(j, DONE, result=val)
        elif tag == "error":
            s.job = None
            self._finish(j, ERROR, error=val)
