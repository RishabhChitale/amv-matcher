"""AMV Matcher desktop shell: the FastAPI app on a free 127.0.0.1 port inside a native window.

    python app_main.py              # open the window
    python app_main.py --selftest   # start the server, GET /, run one synthetic scan, exit 0/1 (no window)
"""
from __future__ import annotations

import multiprocessing
multiprocessing.freeze_support()   # FIRST statement: a frozen spawn child runs its worker and exits before any app import

import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

def ensure_std_streams():
    """Windowed exes (PyInstaller console=False, pythonw) start with sys.stdout/stderr = None: give them a
    harmless stream so any print/logging never crashes (W-16)."""
    for _n in ("stdout", "stderr"):
        if getattr(sys, _n) is None:
            setattr(sys, _n, open(os.devnull, "w", encoding="utf-8"))


ensure_std_streams()

if "--selftest" in sys.argv and not os.environ.get("AMV_DATA_DIR"):   # keep the selftest out of the user's data
    import tempfile
    os.environ["AMV_DATA_DIR"] = tempfile.mkdtemp(prefix="amv-selftest-data-")

from amv_matcher import nativeshell as ns
from amv_matcher import paths, winsafe
from amv_matcher import server as srv

TITLE = "AMV Matcher"
LOG = logging.getLogger("amv.app")


def setup_logging() -> Path:
    paths.DATA.mkdir(parents=True, exist_ok=True)
    f = paths.DATA / "app.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[logging.FileHandler(f, encoding="utf-8")])
    return f


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self):
        import uvicorn
        self.port = free_port()
        srv.allow_port(self.port)
        cfg = uvicorn.Config(srv.app, host="127.0.0.1", port=self.port, log_level="warning", log_config=None)
        self.srv = uvicorn.Server(cfg)
        self.t = threading.Thread(target=self.srv.run, daemon=True, name="uvicorn")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout=30.0):
        self.t.start()
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                urllib.request.urlopen(self.url + "/", timeout=1).read(1)
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def stop(self):
        self.srv.should_exit = True
        self.t.join(timeout=5)
        try:
            srv.shutdown_workers()
        except Exception:
            pass


def seed_rife_from_dev_cache() -> str:
    """Dev-only convenience: if this checkout's .cache holds a verified RIFE (marker tag/name/sha256 match the
    pinned asset and the binary + model are present) and the app-data tools dir has none, COPY it over instead of
    downloading ~436 MB. No-op when the source is missing/unverified or the target already has one."""
    try:
        from amv_matcher import render
        src = Path(os.environ.get("AMV_DEV_CACHE") or (Path.home() / "Claude" / "amv-matcher" / ".cache")) / "tools" / "rife"
        dst = render.rife_dir()
        if src.resolve() == dst.resolve() or render.rife_exe() is not None or not src.is_dir():
            return "skip"
        a = render._asset()
        m = json.loads((src / render._MARKER).read_text())
        if not (m.get("verified") and m.get("tag") == render.RIFE_TAG and m.get("name") == a["name"]
                and m.get("sha256") == a["sha256"]):
            return "skip: marker mismatch"
        if not any(f.is_file() and (f.parent / render.RIFE_MODEL).is_dir() for f in src.rglob(a["exe"])):
            return "skip: incomplete"
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
        LOG.info("RIFE seeded from dev cache %s -> %s", src, dst)
        return "copied"
    except Exception as e:  # noqa: BLE001
        LOG.info("RIFE seed skipped: %s", e)
        return "skip"


VIDEO_TYPES = ("Video files (*.mp4;*.mov;*.mkv;*.webm;*.avi;*.m4v)",)
AUDIO_TYPES = ("Audio files (*.mp3;*.wav;*.m4a;*.flac;*.ogg;*.aac)",)


class Api:
    """Exposed to the page as window.pywebview.api (native dialogs; real paths, no upload copy)."""
    _window = None   # PRIVATE: pywebview walks public attrs of js_api; a public window recurses into window.native forever

    def _dialog(self, kind, multi, types):
        import webview
        r = self._window.create_file_dialog(kind, allow_multiple=multi, file_types=types)
        return [str(p) for p in (r or [])]

    def pick_video(self):
        import webview
        return self._dialog(webview.OPEN_DIALOG, False, VIDEO_TYPES)

    def pick_songs(self):
        import webview
        return self._dialog(webview.OPEN_DIALOG, True, AUDIO_TYPES)

    def pick_folder(self):
        import webview
        return self._dialog(webview.FOLDER_DIALOG, False, ())

    def save_as(self, src):
        """Copy an export (inside the exports dir) to a place the user picks. Returns the saved path or ''."""
        import webview
        p = Path(str(src)).resolve()
        if not p.is_file() or not winsafe.is_relative_to_ci(p, paths.EXPORTS.resolve()):
            return ""
        d = paths.default_export_dir()
        r = self._window.create_file_dialog(webview.SAVE_DIALOG, directory=str(d), save_filename=p.name)
        if not r:
            return ""
        dest = Path(r if isinstance(r, str) else r[0])
        if dest.suffix.lower() != p.suffix.lower():
            dest = dest.with_suffix(p.suffix)
        shutil.copyfile(p, dest)
        LOG.info("native save: %s -> %s", p.name, dest)
        return str(dest)

    @staticmethod
    def _launch(cmd):
        winsafe.popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def open_path(self, src):
        """Open an export (file or folder inside the exports dir) with the OS default app. Returns True if launched."""
        p = paths.exports_member(src)
        if p is None:
            return False
        try:
            if sys.platform == "darwin":
                self._launch(["open", str(p)])
            elif sys.platform == "win32":
                os.startfile(str(p))   # noqa: S606
            else:
                self._launch(["xdg-open", str(p)])
        except OSError as e:
            LOG.info("open_path failed: %s", e)
            return False
        return True

    def reveal_path(self, src):
        """Show an export in the file manager (Finder `open -R`, Explorer `/select,`). Returns True if launched."""
        p = paths.exports_member(src)
        if p is None:
            return False
        try:
            if sys.platform == "darwin":
                self._launch(["open", "-R", str(p)])
            elif sys.platform == "win32":
                self._launch(ns.explorer_select_cmd(p))   # exit code ignored: Explorer returns 1 on success
            else:
                self._launch(["xdg-open", str(p if p.is_dir() else p.parent)])
        except OSError as e:
            LOG.info("reveal_path failed: %s", e)
            return False
        return True

    def log(self, msg):
        LOG.info("ui: %s", msg)

    def notify(self, title, body=""):
        """Native notification (macOS osascript, Windows toast; no-op elsewhere). Returns True if one was posted."""
        return ns.notify(title, body)

    def resolve_installed(self):
        return ns.resolve_app() is not None

    def open_in_resolve(self, src):
        """Open an export (inside the exports dir) in DaVinci Resolve when installed."""
        p = paths.exports_member(src)
        return bool(p) and ns.run(ns.resolve_cmd(p))

    def _pick_project(self, save, name=""):
        import webview
        if save:
            r = self._window.create_file_dialog(webview.SAVE_DIALOG, directory=str(paths.default_export_dir()),
                                               save_filename=ns.project_name(name))
        else:
            r = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                               file_types=("AMV project (*.amvproj)",))
        if not r:
            return ""
        p = Path(r if isinstance(r, str) else r[0])
        return str(p.with_suffix(".amvproj") if save and p.suffix.lower() != ".amvproj" else p)

    def pick_project_open(self):
        return self._pick_project(False)

    def pick_project_save(self, name=""):
        return self._pick_project(True, name)


def _screens():
    try:
        import webview
        return [(sc.x, sc.y, sc.width, sc.height) for sc in webview.screens]
    except Exception:
        return None


# Menu action -> shortcut (key, shift). Must mirror the keydown handler in static/shell.js (tests/test_menu.py checks it).
MENU_ACCEL = {"new": ("N", False), "open": ("O", False), "save": ("S", False), "save_as": ("S", True),
              "undo": ("Z", False), "redo": ("Z", True)}


def menu_label(label, accel=None):
    """Append the platform shortcut hint (tab-separated): mac '⇧⌘S', win/linux 'Ctrl+Shift+S'. accel = (key, shift)."""
    if not accel:
        return label
    key, shift = accel
    hint = ("⇧" if shift else "") + "⌘" + key if sys.platform == "darwin" else "Ctrl+" + ("Shift+" if shift else "") + key
    return label + "\t" + hint


def build_menu(fire):
    """Native menu bar. Every item calls fire('<action>'), which reaches the page as nativeMenu(action)."""
    from webview.menu import Menu, MenuAction, MenuSeparator
    A = lambda label, act: MenuAction(menu_label(label, MENU_ACCEL.get(act)), lambda: fire(act))
    return [
        Menu("File", [A("New Project", "new"), A("Open Project…", "open"), A("Save", "save"), A("Save As…", "save_as"),
                      MenuSeparator(), A("Add Media Folder…", "add_folder"), A("Export…", "export")]),
        Menu("Edit", [A("Undo", "undo"), A("Redo", "redo"), MenuSeparator(), A("Delete", "delete")]),
        Menu("View", [A("Toggle Media Panel", "toggle_media"), A("Toggle Stems", "toggle_stems"),
                      A("Toggle Effects", "toggle_effects")]),
        Menu("Playback", [A("Play / Pause", "play_pause")]),
        Menu("Help", [A("Help", "help")]),
    ]


def run_window() -> int:
    import webview
    from webview.dom import DOMEventHandler
    inst = ns.SingleInstance(paths.DATA)
    if not inst.acquire():   # already running: focus that window (passing any file args), never open a second one
        LOG.info("another instance is running; handing over and exiting")
        ns.SingleInstance.notify_existing(paths.DATA, [a for a in sys.argv[1:] if not a.startswith("-")])
        return 0
    seed_rife_from_dev_cache()
    SHUTDOWN = ns.ShutdownGuard(lambda: None, ns.kill_children, os._exit)
    SHUTDOWN.install()
    s = Server()
    s.start()
    LOG.info("server on %s (frozen=%s data=%s)", s.url, paths.FROZEN, paths.DATA)
    api = Api()
    geom = ns.load_geometry(_screens())
    win = webview.create_window(TITLE, s.url + "/", js_api=api, min_size=(ns.MIN_W, ns.MIN_H),
                                background_color="#0b0d12", **geom)
    api._window = win

    def on_activate(args):
        try:
            win.restore()
            win.show()
        except Exception:
            LOG.exception("activate window")
        if args:
            js("window.nativeDrop && window.nativeDrop(%s)" % json.dumps([ns.normalize_drop_path(a) for a in args]))
    inst.on_activate = on_activate

    def js(code):
        try:
            win.evaluate_js(code)
        except Exception:
            LOG.exception("evaluate_js")

    def save_geom(*_):
        try:
            ns.save_geometry(win.x, win.y, win.width, win.height)
        except Exception:
            LOG.exception("save geometry")

    on_closing = ns.CloseGuard(
        save_geom, lambda: js("window.nativeBeforeQuit && window.nativeBeforeQuit()"), lambda: win.destroy())

    SHUTDOWN.request_close = on_closing
    win.events.resized += lambda *a: save_geom()
    win.events.moved += lambda *a: save_geom()
    win.events.closing += on_closing
    MENU = build_menu(lambda action: js("window.nativeMenu && window.nativeMenu(%s)" % json.dumps(action)))

    def on_drop(e):
        try:
            files = e.get("dataTransfer", {}).get("files", []) or []
            ps = [ns.normalize_drop_path(f.get("pywebviewFullPath")) for f in files if f.get("pywebviewFullPath")]
            LOG.info("native drop: %s", ps)
            win.evaluate_js("window.nativeDrop && window.nativeDrop(%s)" % json.dumps(ps))
        except Exception:
            LOG.exception("drop handler")

    def snapshot(path):
        """Dev/CI aid (macOS): save the web view as a PNG. Used for docs/app-mac.png."""
        from PyObjCTools import AppHelper
        import AppKit

        def go():
            LOG.info("snapshot: requesting")
            def done(img, err):
                try:
                    rep = AppKit.NSBitmapImageRep.alloc().initWithData_(img.TIFFRepresentation())
                    rep.representationUsingType_properties_(AppKit.NSPNGFileType, None).writeToFile_atomically_(path, True)
                    LOG.info("snapshot saved: %s", path)
                except Exception:
                    LOG.exception("snapshot")
            try:
                from webview.platforms import cocoa
                view = cocoa.BrowserView.instances[win.uid].webview
                view.takeSnapshotWithConfiguration_completionHandler_(None, done)
            except Exception:
                LOG.exception("snapshot request")
        AppHelper.callAfter(go)

    def bind(_=None):
        win.dom.document.events.drop += DOMEventHandler(on_drop, True, True)   # stops the browser upload path
        # Dev aids (env only): feed paths through the same hand-off a native drop uses, then snapshot / exit.
        fake = os.environ.get("AMV_TEST_DROP")
        if fake:
            time.sleep(2.0)
            LOG.info("native drop (simulated via AMV_TEST_DROP): %s", fake.split(os.pathsep))
            win.evaluate_js("window.nativeDrop && window.nativeDrop(%s)" % json.dumps(fake.split(os.pathsep)))
        shot = os.environ.get("AMV_SNAPSHOT")
        if shot:
            time.sleep(float(os.environ.get("AMV_SNAPSHOT_DELAY", "4")))
            snapshot(shot)
            time.sleep(6.0)
            if os.environ.get("AMV_EXIT_AFTER_SNAPSHOT"):
                win.destroy()

    webview.start(bind, menu=MENU, debug=bool(os.environ.get("AMV_DEBUG")))
    LOG.info("window closed; shutting down")
    inst.release()
    s.stop()
    SHUTDOWN.cancel.set()
    return 0


def _rss_mb() -> float:
    """Resident memory of this process in MB (Windows: ctypes GetProcessMemoryInfo, no extra dependency)."""
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                           "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                           "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
    pmc = PMC()
    pmc.cb = ctypes.sizeof(pmc)
    h = ctypes.windll.kernel32.GetCurrentProcess()
    ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
    return pmc.WorkingSetSize / 1048576


def check_native_hooks(url: str, timeout: float = 45.0) -> dict:
    """Load url in a hidden pywebview window and return typeof window.nativeDrop / window.nativeMenu
    (polled until both are functions or timeout). Blocks on webview.start: call from the main thread."""
    import webview
    out: dict = {}
    api = Api()   # exactly like the real launch (js_api=Api()): a public non-callable attr on it makes pywebview recurse
    win = webview.create_window("selftest", url + "/", hidden=True, js_api=api)
    api._window = win
    oom: dict = {"errors": [], "peak_mb": 0.0}
    import logging

    class _H(logging.Handler):
        def emit(self, rec):
            if "Error while processing" in rec.getMessage():
                oom["errors"].append(rec.getMessage()[:120])
    logging.getLogger("pywebview").addHandler(_H())

    def probe():
        t0 = time.time()
        try:
            while time.time() - t0 < timeout:
                try:
                    r = win.evaluate_js("JSON.stringify({nativeDrop: typeof window.nativeDrop, nativeMenu: typeof window.nativeMenu})")
                    out.update(json.loads(r or "{}"))
                except Exception:  # noqa: BLE001  page not ready yet
                    pass
                if out.get("nativeDrop") == "function" and out.get("nativeMenu") == "function":
                    break
                time.sleep(0.5)
            if sys.platform == "win32":   # CI-verified only: the runaway walk took GBs within a minute
                t1 = time.time()
                while time.time() - t1 < 20:
                    oom["peak_mb"] = max(oom["peak_mb"], _rss_mb())
                    time.sleep(0.5)
        finally:
            out["oom"] = oom
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass
    webview.start(probe)
    return out


def selftest(log_path: str = "") -> int:
    """Server up, GET /, one synthetic scan (tiny generated media), exit 0/1. No window."""
    import tempfile
    import numpy as np
    import soundfile as sf
    import av
    tmp = Path(tempfile.mkdtemp(prefix="amv-selftest-"))
    if log_path:
        Path(log_path).write_text("")

    def print(*a):   # noqa: A001  stdout AND the log file (a windowed exe has no console)
        line = " ".join(str(x) for x in a)
        sys.stdout.write(line + "\n")
        if log_path:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    try:
        s = Server()
        s.start()
        base = s.url
        html = urllib.request.urlopen(base + "/", timeout=10).read()
        assert b"AMV Matcher" in html, "index.html missing title"
        # synthetic 6 s video (flash every 0.5 s) + a 120 BPM click track
        vp = tmp / "v.mp4"
        with av.open(str(vp), "w", format="mp4") as out:
            vs = out.add_stream("libx264", rate=24)
            vs.width, vs.height, vs.pix_fmt = 160, 120, "yuv420p"
            for i in range(24 * 6):
                img = np.full((120, 160, 3), 255 if (i // 12) % 2 == 0 else 20, np.uint8)
                for pk in vs.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                    out.mux(pk)
            for pk in vs.encode(None):
                out.mux(pk)
        sr = 22050
        y = np.zeros(sr * 20, np.float32)
        for k in range(0, 40):
            i = int(k * 0.5 * sr)
            y[i:i + 400] = np.hanning(400) * 0.9
        sd = tmp / "songs"
        sd.mkdir()
        sf.write(str(sd / "click120.wav"), y, sr)

        def post(path, body):
            req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json", "Origin": base})
            return json.loads(urllib.request.urlopen(req, timeout=30).read())
        job = post("/api/scan", {"video_path": str(vp), "music_dir": str(sd)})
        jid = job["job_id"]
        t0 = time.time()
        while time.time() - t0 < 180:
            j = json.loads(urllib.request.urlopen(f"{base}/api/scan/{jid}", timeout=10).read())
            if j["status"] != "running":
                break
            time.sleep(0.5)
        assert j["status"] == "done", j
        assert j["results"], "no scored songs"
        print("selftest ok: top =", round(j["results"][0]["total_pct"], 1), "%")
        sp = str(sd / "click120.wav")          # scan registered both paths as allowed

        def get(path):
            return json.loads(urllib.request.urlopen(base + path, timeout=30).read())

        def wait(path, secs=240):
            t1 = time.time()
            while time.time() - t1 < secs:
                r = get(path)
                if r.get("status") != "running":
                    return r
                time.sleep(0.5)
            raise AssertionError("timeout waiting for " + path)

        # 1. auto-sync (tiny budget)
        r = wait("/api/auto-sync/" + post("/api/auto-sync", {"video_path": str(vp), "song_path": sp, "budget_s": 5})["job_id"])
        assert r["status"] == "done" and (r["result"] or {}).get("final"), r
        print("selftest ok: auto-sync final total =", r["result"]["final"].get("total_pct"))
        # 2. 1 s dup render (1 s of video, no interpolation tool needed)
        plan = {"offset": 0.0, "tempo_ratio": 1.0, "segments": [], "nudges": [], "lead_silence_s": 0.0,
                "retime": {"warp": [[0, 0], [1, 1]], "out_duration": 1.0}}
        r = wait("/api/render-mp4/" + post("/api/render-mp4", {"video_path": str(vp), "song_path": sp, "plan": plan, "interp": "dup"})["job_id"])
        assert r["status"] == "done", r
        with av.open(r["path"]) as c:
            assert c.streams.video and c.streams.audio and 0.8 < float(c.duration) / 1e6 < 1.3, c.duration
        print("selftest ok: render 1 s dup ->", Path(r["path"]).name)
        # 3. quick-split stems (hpss; the Demucs pack is optional)
        r = post("/api/stems", {"song_path": sp, "provider": "hpss"})
        t1 = time.time()
        while r.get("status") == "running" and time.time() - t1 < 240:
            time.sleep(0.5)
            r = get("/api/stems/status?song_path=" + urllib.parse.quote(sp)).get("running") or {"status": "done", "again": 1}
        st = get("/api/stems/status?song_path=" + urllib.parse.quote(sp))
        assert st["state"] == "ready" and {"drums", "music"} <= set(st["stems"]), st
        print("selftest ok: stems quick split:", ",".join(st["stems"]), "| demucs pack:", st["capabilities"]["demucs_installed"])
        # 4. DaVinci Resolve bundle
        r = wait("/api/scan/" + post("/api/export-resolve", {"video_path": str(vp), "song_path": sp, "plan": plan})["job_id"])
        assert r["status"] == "done" and Path(r["zip"]).is_file(), r
        import zipfile
        names = zipfile.ZipFile(r["zip"]).namelist()
        assert any(n.endswith(".fcpxml") for n in names), names
        print("selftest ok: resolve bundle:", len(names), "files")
        # 5. render-tools path logic: RIFE lives under the app-data cache
        rt = get("/api/render-tools")["rife"]
        from amv_matcher import render
        assert str(render.rife_dir()).startswith(str(paths.CACHE)), render.rife_dir()
        print("selftest ok: rife dir =", render.rife_dir(), "| installed:", rt["installed"], "| frozen:", paths.FROZEN)
        # 6. one real job through the worker pool: proves spawn + freeze_support + kind imports when frozen
        from amv_matcher.workers import WorkerPool   # own 1-worker pool: independent of AMV_WORKERS
        wp = WorkerPool(workers=1)
        try:
            t2 = time.time()
            wjid = wp.submit("probe", str(vp), 0)
            while True:
                st = wp.status(wjid)
                assert st is not None and st["state"] not in ("error", "cancelled"), st
                if st["state"] == "done":
                    break
                assert time.time() - t2 < 60, f"worker pool job not done in 60 s: {st}"
                time.sleep(0.2)
        finally:
            wp.shutdown()
        print("selftest ok: worker pool job (probe) -> done in", round(time.time() - t2, 1), "s")
        # the loaded page must expose the native hooks (a broken native.js/shell.js ships a dead drop/menu)
        found = check_native_hooks(s.url)
        oom = found.pop("oom", {"errors": [], "peak_mb": 0.0})
        assert found == {"nativeDrop": "function", "nativeMenu": "function"}, f"native hooks missing in the page: {found}"
        assert not oom["errors"], f"pywebview js_api walk errors (public attribute on Api?): {oom['errors'][:2]}"
        assert oom["peak_mb"] < 600, f"RSS {oom['peak_mb']:.0f} MB after page load (js_api walk runaway?)"
        print("selftest ok: page hooks window.nativeDrop + window.nativeMenu present | js_api clean, peak RSS",
              round(oom["peak_mb"]), "MB")
        s.stop()
        return 0
    except Exception as e:  # noqa: BLE001
        print("selftest FAILED:", type(e).__name__, e)
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv) -> int:
    if "--selftest" in argv:
        lp = argv[argv.index("--selftest-log") + 1] if "--selftest-log" in argv and argv.index("--selftest-log") + 1 < len(argv) else ""
        return selftest(lp)
    if ns.is_worker_child(argv):   # a worker/tracker child must never build a window (the "another app opens" bug)
        return 0
    setup_logging()
    return run_window()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
