"""AMV Matcher desktop shell: the FastAPI app on a free 127.0.0.1 port inside a native window.

    python app_main.py              # open the window
    python app_main.py --selftest   # start the server, GET /, run one synthetic scan, exit 0/1 (no window)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

if getattr(sys, "frozen", False):   # PyInstaller windowed apps have no stdout/stderr
    for _n in ("stdout", "stderr"):
        if getattr(sys, _n) is None:
            setattr(sys, _n, open(os.devnull, "w"))

if "--selftest" in sys.argv and not os.environ.get("AMV_DATA_DIR"):   # keep the selftest out of the user's data
    import tempfile
    os.environ["AMV_DATA_DIR"] = tempfile.mkdtemp(prefix="amv-selftest-data-")

from amv_matcher import paths
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


VIDEO_TYPES = ("Video files (*.mp4;*.mov;*.mkv;*.webm;*.avi;*.m4v)",)
AUDIO_TYPES = ("Audio files (*.mp3;*.wav;*.m4a;*.flac;*.ogg;*.aac)",)


class Api:
    """Exposed to the page as window.pywebview.api (native dialogs; real paths, no upload copy)."""
    window = None

    def _dialog(self, kind, multi, types):
        import webview
        r = self.window.create_file_dialog(kind, allow_multiple=multi, file_types=types)
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
        if not p.is_file() or not p.is_relative_to(paths.EXPORTS.resolve()):
            return ""
        d = paths.default_export_dir()
        r = self.window.create_file_dialog(webview.SAVE_DIALOG, directory=str(d), save_filename=p.name)
        if not r:
            return ""
        dest = Path(r if isinstance(r, str) else r[0])
        if dest.suffix.lower() != p.suffix.lower():
            dest = dest.with_suffix(p.suffix)
        shutil.copyfile(p, dest)
        LOG.info("native save: %s -> %s", p.name, dest)
        return str(dest)

    def log(self, msg):
        LOG.info("ui: %s", msg)


def run_window() -> int:
    import webview
    from webview.dom import DOMEventHandler
    s = Server()
    s.start()
    LOG.info("server on %s (frozen=%s data=%s)", s.url, paths.FROZEN, paths.DATA)
    api = Api()
    win = webview.create_window(TITLE, s.url + "/", js_api=api, width=1440, height=900, min_size=(1100, 700),
                                background_color="#0b0d12")
    api.window = win

    def on_drop(e):
        try:
            files = e.get("dataTransfer", {}).get("files", []) or []
            ps = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
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

    webview.start(bind, debug=bool(os.environ.get("AMV_DEBUG")))
    LOG.info("window closed; shutting down")
    s.stop()
    return 0


def selftest() -> int:
    """Server up, GET /, one synthetic scan (tiny generated media), exit 0/1. No window."""
    import tempfile
    import numpy as np
    import soundfile as sf
    import av
    tmp = Path(tempfile.mkdtemp(prefix="amv-selftest-"))
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
        s.stop()
        return 0
    except Exception as e:  # noqa: BLE001
        print("selftest FAILED:", type(e).__name__, e)
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv) -> int:
    if "--selftest" in argv:
        return selftest()
    setup_logging()
    return run_window()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
