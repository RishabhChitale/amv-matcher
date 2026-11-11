#!/usr/bin/env python3
"""F19 check: does the player's audio sync loop stay quiet when audio and video are in sync, and does the original song
play through the plain song URL (never a planned/spliced render)?

Launches the app (frozen exe, or this checkout in source mode) on its own throwaway AMV_DATA_DIR/TMPDIR, registers a
synthetic video + song (tests/synth.py, nothing personal), loads them in the page, plays for --secs and samples
window.__aud (the F19 counters) once a second over the Chrome DevTools Protocol.

Two ways to reach the page:
    --mode webview2   attach to the frozen app's OWN WebView2 (WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port)
    --mode browser    open the app's local URL in a separate headless Chromium (--browser PATH: msedge / Google Chrome)

    python tools/f19_audio_check.py --exe "dist/AMV Matcher/AMV Matcher.exe" --mode webview2 --work W
    python tools/f19_audio_check.py --mode browser --browser "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --work W

Output (grep-able): f19_sample, f19_events, f19_summary, f19 PASS|FAIL <why>. Exit 0 ok, 1 a check failed, 2 setup failed.
Needs websocket-client (requirements-dev.txt) and what tests/synth.py needs (opencv, soundfile).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websocket

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "tests"))
import scan_timing as st  # noqa: E402

PORT = int(os.environ.get("AMV_F19_CDP_PORT", "9341"))


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.n = 0
        self.errors = []

    def _events(self, m):
        meth = m.get("method")
        if meth == "Runtime.exceptionThrown":
            d = m["params"]["exceptionDetails"]
            self.errors.append("exception: " + (d.get("exception", {}).get("description") or d.get("text", "")))
        elif meth == "Runtime.consoleAPICalled" and m["params"]["type"] == "error":
            self.errors.append("console.error: " + " ".join(str(a.get("value", a.get("description", ""))) for a in m["params"]["args"]))
        elif meth == "Log.entryAdded" and m["params"]["entry"]["level"] == "error":
            e = m["params"]["entry"]
            self.errors.append("log: %s %s" % (e.get("text"), e.get("url", "")))

    def call(self, method, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            self._events(m)
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError("%s: %s" % (method, m["error"]))
                return m.get("result", {})

    def pump(self, secs):
        end = time.time() + secs
        self.ws.settimeout(0.1)
        try:
            while time.time() < end:
                try:
                    self._events(json.loads(self.ws.recv()))
                except websocket.WebSocketTimeoutException:
                    pass
        finally:
            self.ws.settimeout(30)

    def ev(self, expr, gesture=False):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True, userGesture=gesture)
        if "exceptionDetails" in r:
            raise RuntimeError(str(r["exceptionDetails"])[:400])
        return r["result"].get("value")


def wait_for(c, expr, timeout, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if c.ev(expr):
                return True
        except RuntimeError:
            pass
        c.pump(step)
    return False


def find_page(cdp_port, url_part, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            tabs = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % cdp_port, timeout=2))
            for t in tabs:
                if t.get("type") == "page" and url_part in t.get("url", ""):
                    return t
        except Exception:
            pass
        time.sleep(0.5)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe")
    ap.add_argument("--mode", choices=("webview2", "browser"), default="browser")
    ap.add_argument("--browser")
    ap.add_argument("--work", required=True)
    ap.add_argument("--secs", type=float, default=30.0, help="seconds of playback to sample")
    ap.add_argument("--settle", type=float, default=3.0, help="seconds at the start of playback excluded from the in-sync rate")
    ap.add_argument("--max-rate-changes-per-s", type=float, default=0.1)
    ap.add_argument("--ref", default="local")
    a = ap.parse_args()
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    data, tmp, prof = work / "data", work / "tmp", work / "browser-profile"
    for d in (data, tmp, prof):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)

    try:
        import synth                                     # tests/synth.py: opencv + soundfile
    except ImportError:
        from tests import synth                          # tool run from a sparse checkout: tests/ is on PYTHONPATH's root
    video = work / "v.mp4"
    song = work / "song.wav"
    synth.make_video(video, seconds=max(60, a.secs + 20))
    synth.make_click_track(song, 120, seconds=max(60, a.secs + 20))

    # pywebview overrides WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS with its own CreationProperties, so the debug port can only
    # come from webview.settings['REMOTE_DEBUGGING_PORT']. The unmodified app has no hook for it: the CI workflow patches ONE
    # line into the runner's build copy of app_main.py (reading AMV_F19_CDP_PORT); the repo file is untouched.
    os.environ.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)
    if a.mode == "webview2":
        os.environ["AMV_F19_CDP_PORT"] = str(PORT)
    else:
        os.environ.pop("AMV_F19_CDP_PORT", None)
    exe = Path(a.exe).resolve() if a.exe else None
    proc, port, startup = st.launch(exe, work, "f19", None, data, tmp)
    print("f19_setup ref=%s mode=%s app_port=%d startup_s=%.1f" % (a.ref, a.mode, port, startup), flush=True)
    base = "http://127.0.0.1:%d" % port
    bproc = None
    fail = []
    summary = {}
    try:
        if a.mode == "webview2":
            page = find_page(PORT, "127.0.0.1:%d" % port)
        else:
            bproc = subprocess.Popen([a.browser, "--headless=new", "--remote-debugging-port=%d" % PORT, "--remote-allow-origins=*",
                                      "--user-data-dir=%s" % prof, "--autoplay-policy=no-user-gesture-required",
                                      "--window-size=1440,900", "--no-first-run", "--no-default-browser-check", "--disable-gpu",
                                      "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            page = find_page(PORT, "about:blank", 30)
        if not page:
            print("f19 SETUP-FAIL no DevTools page target (mode=%s)" % a.mode)
            return 2
        c = CDP(page["webSocketDebuggerUrl"])
        for m in ("Page.enable", "Runtime.enable", "Log.enable"):
            c.call(m)
        if a.mode == "browser":
            c.call("Page.navigate", url=base + "/")
        if not wait_for(c, "document.readyState==='complete' && typeof S!=='undefined' && !!window.__aud", 60):
            print("f19 SETUP-FAIL page did not load or window.__aud is missing (build without F19?)")
            return 2
        c.errors.clear()
        print("f19_env ua=%s" % c.ev("navigator.userAgent"), flush=True)
        c.ev("(async()=>{try{const r=await (await POST('/api/register-paths',{paths:%s})).json();"
             "const f=p=>({name:p.split(/[\\\\/]/).pop(),nativePath:p,path:p});"
             "enqueue(()=>doVideo(f(r.videos[0])));enqueue(()=>doSongs(r.songs.map(f)));window.__reg=JSON.stringify(r)}"
             "catch(e){window.__reg='ERR '+e}})()" % json.dumps([str(video), str(song)]))
        if not wait_for(c, "typeof S!=='undefined' && vid.duration>0", 120):
            print("f19 SETUP-FAIL video never became playable (vid.duration): reg=%s" % c.ev("window.__reg"))
            return 2
        c.pump(2)
        c.ev("selectSong({path:%s,best_offset_s:0,name:'song'})" % json.dumps(str(song)))
        if not wait_for(c, "!!S.sel && aud.readyState>=2 && aud.duration>0", 60):
            print("f19 SETUP-FAIL song audio never loaded: asrc=%s aerr=%s" % (c.ev("aud.currentSrc"), c.ev("aud.error&&aud.error.code")))
            return 2
        c.ev("S.offset=0;S.manual=true;seek(0)")
        c.pump(1)
        c.ev("window.__ev=[];['play','playing','pause','waiting','stalled','seeking','seeked','error'].forEach(n=>aud.addEventListener(n,()=>__ev.push([Math.round(performance.now()),n,+aud.currentTime.toFixed(2)])));"
             "window.__raf=0;(function f(){__raf++;requestAnimationFrame(f)})();window.__pe=[];const _p=aud.play.bind(aud);aud.play=()=>{const r=_p();r.catch(e=>__pe.push(String(e)));return r}")
        pre = c.ev("JSON.stringify({asrc:aud.currentSrc, spliceActive:spliceActive(), spUrl:S.sp.url||'', inUse:!!S.inUse, pv:vid.preservesPitch, pa:aud.preservesPitch})")
        pre = json.loads(pre)
        c.ev("document.getElementById('b-play').click()", gesture=True)   # counts as a user gesture (WebView2 has no autoplay flag)
        t0 = time.time()
        samples = []
        for i in range(int(a.secs)):
            c.pump(max(0.0, (t0 + (i + 1)) - time.time()))
            s = json.loads(c.ev("JSON.stringify({t:+vid.currentTime.toFixed(3), at:+aud.currentTime.toFixed(3), st:+songTime().toFixed(3), rate:aud.playbackRate, "
                                "rates:__aud.rates, seeks:__aud.seeks, nudging:__aud.nudging, playing:S.playing, apaused:aud.paused, vpaused:vid.paused, raf:__raf})"))
            s["i"] = i + 1
            s["drift_ms"] = round((s["at"] - s["st"]) * 1000)
            samples.append(s)
            print("f19_sample %s" % json.dumps(s), flush=True)
        post = json.loads(c.ev("JSON.stringify({asrc:aud.currentSrc, spliceActive:spliceActive(), spUrl:S.sp.url||'', ev:window.__ev, pe:window.__pe, "
                               "aerr:aud.error&&aud.error.code, verr:vid.error&&vid.error.code, log:__aud.log, vis:document.visibilityState})"))
        print("f19_events %s" % json.dumps({"ev": post["ev"][-40:], "pe": post["pe"], "log": post["log"][-40:]}), flush=True)

        settle = int(a.settle)
        window = [s for s in samples if s["i"] > settle]
        n_s = max(1, len(window))
        rate_changes = (window[-1]["rates"] - samples[settle - 1]["rates"]) if window and settle >= 1 else (samples[-1]["rates"] if samples else 0)
        seeks = (window[-1]["seeks"] - samples[settle - 1]["seeks"]) if window and settle >= 1 else (samples[-1]["seeks"] if samples else 0)
        per_s = rate_changes / float(n_s)
        max_drift = max((abs(s["drift_ms"]) for s in window), default=None)
        advanced = samples[-1]["t"] - samples[0]["t"] if samples else 0
        raf_per_s = (samples[-1]["raf"] / float(len(samples))) if samples else 0
        summary = {"ref": a.ref, "mode": a.mode, "secs": len(samples), "settle_s": settle, "rate_changes_after_settle": rate_changes,
                   "rate_changes_per_s": round(per_s, 3), "total_rate_changes": samples[-1]["rates"] if samples else None,
                   "seeks_after_settle": seeks, "max_abs_drift_ms": max_drift, "video_advanced_s": round(advanced, 2),
                   "raf_per_s": round(raf_per_s, 1), "visibility": post["vis"], "asrc_before": pre["asrc"], "asrc_after": post["asrc"],
                   "spliceActive": post["spliceActive"], "preservesPitch_vid": pre["pv"], "preservesPitch_aud": pre["pa"],
                   "js_errors": len(c.errors), "play_errors": post["pe"], "aud_error": post["aerr"], "vid_error": post["verr"]}
        print("f19_summary %s" % json.dumps(summary), flush=True)
        if c.errors:
            print("f19_js_errors %s" % json.dumps(c.errors[:10]), flush=True)
        if advanced < a.secs * 0.7:
            fail.append("video did not play (advanced %.1f s of %.0f)" % (advanced, a.secs))
        if per_s > a.max_rate_changes_per_s:
            fail.append("audio rate changed %.2f/s after settle (limit %.2f)" % (per_s, a.max_rate_changes_per_s))
        tail = [x["drift_ms"] for x in window[-10:]]
        if tail and abs(sorted(tail)[len(tail) // 2]) > 25:
            fail.append("steady audio/video drift %d ms after settle (limit 25)" % sorted(tail)[len(tail) // 2])
        if c.errors:
            fail.append("%d JS errors" % len(c.errors))
        if post["pe"] or post["aerr"] or post["verr"]:
            fail.append("media errors pe=%s aerr=%s verr=%s" % (post["pe"], post["aerr"], post["verr"]))
        if "/api/audio?path=" not in post["asrc"] or post["spliceActive"] or post["spUrl"]:
            fail.append("original audio is not on the plain song URL (src=%s spliceActive=%s)" % (post["asrc"], post["spliceActive"]))
        if not (pre["pv"] and pre["pa"]):
            fail.append("preservesPitch not true on both elements")
    except Exception as e:                                 # noqa: BLE001
        print("f19 SETUP-FAIL %s: %s" % (type(e).__name__, e), flush=True)
        return 2
    finally:
        if bproc:
            st.kill_tree(bproc)
        st.kill_tree(proc)
    print(("f19 FAIL " + "; ".join(fail)) if fail else "f19 PASS", flush=True)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
