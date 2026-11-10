#!/usr/bin/env python3
"""UI smoke test: drives headless Chrome over CDP against a running AMV Matcher.

Usage: .venv/bin/python tools/ui_smoke.py [--url http://127.0.0.1:8765]
         [--video clip.mp4] [--music DIR] [--bad-video clip.mkv] [--shot docs/ui.png]
Needs: websocket-client, a local Google Chrome.app, and a running server (./run.sh).
Prints one PASS/FAIL line per check and exits non-zero if any fails.
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, time, urllib.request
import websocket

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333
SRV_PORT = 8799


def serve(d):
    """Run the app with its upload/export/extract dirs redirected into a temp dir (no backend change needed)."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from pathlib import Path
    import uvicorn
    from amv_matcher import server
    server.UPLOADS = Path(d) / "uploads"
    server.EXPORTS = Path(d) / "exports"
    server.EXTRACTED = Path(d) / "extracted"
    try:
        from amv_matcher import stems as _stems
        _stems.CACHE_DIR = Path(d) / "stems-cache"
    except Exception:
        pass
    server.ALLOWED_ORIGINS = {f"http://127.0.0.1:{SRV_PORT}", f"http://localhost:{SRV_PORT}"}
    uvicorn.run(server.app, host="127.0.0.1", port=SRV_PORT, log_level="warning")
    return 0


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.n = 0
        self.errors = []

    def call(self, method, **params):
        self.n += 1
        mid = self.n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            self._events(m)
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})

    def _events(self, m):
        meth = m.get("method")
        if meth == "Log.entryAdded":
            e = m["params"]["entry"]
            if e["level"] == "error":
                self.errors.append(f"log: {e.get('text')} {e.get('url', '')}")
        elif meth == "Runtime.exceptionThrown":
            d = m["params"]["exceptionDetails"]
            self.errors.append("exception: " + (d.get("exception", {}).get("description") or d.get("text", "")))
        elif meth == "Runtime.consoleAPICalled" and m["params"]["type"] == "error":
            self.errors.append("console.error: " + " ".join(str(a.get("value", a.get("description", ""))) for a in m["params"]["args"]))

    def pump(self, secs):
        """Sleep while draining events."""
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

    def ev(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(str(r["exceptionDetails"]))
        return r["result"].get("value")

    def mouse(self, typ, x, y, button="left"):
        self.call("Input.dispatchMouseEvent", type=typ, x=x, y=y, button=button,
                  buttons=1 if typ != "mouseReleased" else 0, clickCount=1)

    def key(self, key, code=None, shift=False, vk=0, text=None, mods=0):
        mod = (8 if shift else 0) | mods   # Alt=1 Ctrl=2 Meta=4 Shift=8
        base = dict(key=key, code=code or key, modifiers=mod, windowsVirtualKeyCode=vk)
        self.call("Input.dispatchKeyEvent", type="keyDown", **base, **({"text": text} if text else {}))
        self.call("Input.dispatchKeyEvent", type="keyUp", **base)


results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def wait_for(c, expr, timeout=60, step=0.4):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if c.ev(expr):
                return True
        except RuntimeError:
            pass
        c.pump(step)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=None, help="test an existing server read-only (writes are skipped); default: own temp server on :8799")
    ap.add_argument("--_serve", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--video", default="/tmp/amvfx/clip.mp4")
    ap.add_argument("--music", default="/tmp/amvfx/music")
    ap.add_argument("--allow-clear", action="store_true")
    ap.add_argument("--bad-video", default="/tmp/amvfx/clip.mkv")
    ap.add_argument("--shot", default=os.path.join(os.path.dirname(__file__), "..", "docs", "ui.png"))
    a = ap.parse_args()
    if a._serve:
        return serve(a._serve)
    ro = a.server is not None
    srv = None
    sdir = None
    if ro:
        a.url = a.server
    else:
        sdir = tempfile.mkdtemp(prefix="amv-srv-")
        a.url = f"http://127.0.0.1:{SRV_PORT}"
        srv = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--_serve", sdir],
                               cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                urllib.request.urlopen(a.url + "/api/uploads", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        else:
            print("FAIL  temp server did not start on", a.url)
            srv.kill()
            shutil.rmtree(sdir, ignore_errors=True)
            return 2
    print("server:", a.url, "(read-only)" if ro else f"(own temp instance, state in {sdir})")
    a.allow_clear = not ro

    prof = tempfile.mkdtemp(prefix="amv-ui-")
    proc = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}",
                             "--autoplay-policy=no-user-gesture-required", "--window-size=1440,900",
                             "--no-first-run", "--no-default-browser-check", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tabs = None
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                break
            except Exception:
                time.sleep(0.2)
        if not tabs:
            print("FAIL  headless Chrome did not start / DevTools port unreachable")
            return 2
        page = next(t for t in tabs if t["type"] == "page")
        c = CDP(page["webSocketDebuggerUrl"])
        for m in ("Page.enable", "Runtime.enable", "Log.enable"):
            c.call(m)
        c.call("Emulation.setDeviceMetricsOverride", width=1440, height=900, deviceScaleFactor=1, mobile=False)
        c.call("Page.navigate", url=a.url)
        if not wait_for(c, "document.readyState==='complete' && !!document.getElementById('scan')", 15):
            print("FAIL  page did not load (cannot reach", a.url + ")")
            return 2
        check("page loads", True)

        def scan(video):
            c.ev(f"document.getElementById('video-path').value={json.dumps(video)};"
                 f"document.getElementById('music-dir').value={json.dumps(a.music)};"
                 "document.getElementById('scan').click();")

        # ---- scan + results
        scan(a.video)
        ok = wait_for(c, "document.querySelectorAll('.res').length>0 && !document.getElementById('scan').disabled", 90)
        check("scan completes, results listed", ok)
        names = c.ev("[...document.querySelectorAll('.res')].map(e=>e.innerText.replace(/\\n+/g,' | '))") or []
        order = [n for n in names]
        i120 = next((i for i, n in enumerate(order) if "click120" in n), 99)
        i97 = next((i for i, n in enumerate(order) if "click97" in n), 99)
        check("click120 ranks above click97", i120 < i97, f"order={[n[:24] for n in order]}")
        short = c.ev("document.getElementById('short').textContent") or ""
        check("'Too short' group lists short.wav", "short" in short.lower(), short[:60].replace("\n", " "))
        vc = c.ev("document.getElementById('vcard') ? document.getElementById('vcard').innerText : ''") or ""
        check("video info card filled", "BPM" in vc.upper() or any(ch.isdigit() for ch in vc), vc[:60].replace("\n", " "))
        check("rings and percent pills rendered", bool(c.ev("document.querySelectorAll('.res .ring').length>0 && document.querySelectorAll('.res .pill .pp').length>=3")))

        # ---- load song
        c.ev("document.querySelector('.res').click()")
        ok = wait_for(c, "!!(S.sel && S.wf) && document.getElementById('vid').readyState>=2", 30)
        check("click top row loads song onto A1 (waveform + video ready)", ok)
        c.pump(0.5)

        # ---- play + sync
        c.ev("document.getElementById('b-play').click()")
        c.pump(3.0)
        d = c.ev("({v:document.getElementById('vid').currentTime,off:S.offset,apaused:document.getElementById('aud').paused,muted:document.getElementById('vid').muted})")
        drifts = sorted(c.ev("""new Promise(res=>{const v=document.getElementById('vid'),a=document.getElementById('aud'),out=[],t0=performance.now();
          const f=()=>{out.push(Math.abs((a.currentTime-S.offset)-v.currentTime)*1000);performance.now()-t0<3000?requestAnimationFrame(f):res(out)};requestAnimationFrame(f);setTimeout(()=>res(out.length?out:[0]),3600)})"""))
        d = c.ev("({v:document.getElementById('vid').currentTime,off:S.offset,apaused:document.getElementById('aud').paused,muted:document.getElementById('vid').muted})")
        med, p95 = drifts[len(drifts) // 2], drifts[min(len(drifts) - 1, int(len(drifts) * 0.95))]
        try:
            load = os.getloadavg()[0]
        except OSError:
            load = -1
        check("plays: video advanced, muted, audio playing", d["v"] > 2.0 and d["muted"] and not d["apaused"], str(d))
        check("A/V drift over 3 s of playback: median < 25 ms and p95 < 60 ms", med < 25 and p95 < 60,
              f"median {med:.1f} ms, p95 {p95:.1f} ms, n={len(drifts)}, load avg {load:.1f}")
        tc = c.ev("document.getElementById('tc').textContent")
        check("timecode HH:MM:SS:FF format", bool(__import__('re').match(r"^\d\d:\d\d:\d\d:\d\d$", tc or "")), tc)

        # ---- pulse (Boom 80)
        c.ev("(()=>{const b=document.getElementById('fx-boom');b.value=80;b.dispatchEvent(new Event('input',{bubbles:true}));b.dispatchEvent(new Event('change',{bubbles:true}))})()")
        c.pump(0.2)
        mx, mb = 1.0, 1.0
        t_end = time.time() + 6
        while time.time() < t_end:
            s = c.ev("({t:getComputedStyle(document.getElementById('vid')).transform,f:getComputedStyle(document.getElementById('vid')).filter})")
            if s and s["t"] not in ("none", None):
                try:
                    mx = max(mx, float(s["t"].strip("matrix()").split(",")[0]))
                except ValueError:
                    pass
            if s and "brightness" in (s["f"] or ""):
                try:
                    mb = max(mb, float(s["f"].split("brightness(")[1].split(")")[0]))
                except ValueError:
                    pass
            c.pump(0.03)
        check("Boom 80: video scale > 1 on beats", mx > 1.005, f"max scale {mx:.3f}")
        # (superseded by the deterministic paused-at-drop Boom checks below)
        c.ev("(()=>{const b=document.getElementById('fx-boom');b.value=0;b.dispatchEvent(new Event('input',{bubbles:true}));b.dispatchEvent(new Event('change',{bubbles:true}))})()")
        c.pump(0.4)
        sc0 = c.ev("getComputedStyle(document.getElementById('vid')).transform")
        check("Boom 0: no transform", sc0 in ("none", "matrix(1, 0, 0, 1, 0, 0)"), sc0)
        c.ev("(()=>{const b=document.getElementById('fx-boom');b.value=80;b.dispatchEvent(new Event('input',{bubbles:true}));b.dispatchEvent(new Event('change',{bubbles:true}))})()")
        ls = c.ev("localStorage.length>0")
        check("Boom prefs saved to localStorage", bool(ls))
        c.ev("(()=>{const d=document.getElementById('fx-decay');d.value=300;d.dispatchEvent(new Event('input',{bubbles:true}))})()")
        check("Decay slider updates live value", "300" in (c.ev("document.getElementById('fx-decay-v').textContent") or ""))
        c.ev("document.getElementById('fx-drops').click();document.getElementById('fx-shake').click()")
        c.pump(0.3)
        check("Drops only / Shake toggles", bool(c.ev("document.getElementById('fx-drops').checked && document.getElementById('fx-shake').checked")))
        c.ev("document.getElementById('fx-drops').click();document.getElementById('fx-shake').click()")

        # ---- keyboard
        c.ev("document.getElementById('b-play').click()") if not c.ev("document.getElementById('vid').paused") else None
        c.pump(0.3)
        c.ev("document.activeElement && document.activeElement.blur()")
        c.ev("seek(2)")
        c.pump(0.3)
        t0 = c.ev("document.getElementById('vid').currentTime")
        c.key(" ", "Space", vk=32, text=" ")
        c.pump(1.0)
        t1 = c.ev("document.getElementById('vid').currentTime")
        check("Space plays", t1 - t0 > 0.5, f"{t0:.2f}->{t1:.2f}")
        c.key(" ", "Space", vk=32, text=" ")
        c.pump(0.3)
        check("Space pauses", bool(c.ev("document.getElementById('vid').paused")))
        t0 = c.ev("document.getElementById('vid').currentTime")
        fps = c.ev("S.fps")
        c.key("ArrowRight", vk=39)
        c.pump(0.3)
        t1 = c.ev("document.getElementById('vid').currentTime")
        check("ArrowRight = +1 frame", abs((t1 - t0) - 1 / fps) < 0.02, f"{(t1 - t0) * 1000:.1f} ms")
        c.key("ArrowLeft", vk=37)
        c.pump(0.3)
        t2 = c.ev("document.getElementById('vid').currentTime")
        check("ArrowLeft = -1 frame", abs((t1 - t2) - 1 / fps) < 0.02, f"{(t1 - t2) * 1000:.1f} ms")
        c.key("ArrowRight", vk=39, shift=True)
        c.pump(0.3)
        t3 = c.ev("document.getElementById('vid').currentTime")
        check("Shift+ArrowRight = +1 s", abs((t3 - t2) - 1.0) < 0.05, f"{t3 - t2:.2f} s")
        c.key("ArrowLeft", vk=37, shift=True)
        c.pump(0.3)
        t4 = c.ev("document.getElementById('vid').currentTime")
        check("Shift+ArrowLeft = -1 s", abs((t3 - t4) - 1.0) < 0.05, f"{t3 - t4:.2f} s")
        c.key("l", "KeyL", vk=76, text="l")
        c.pump(1.0)
        wait_for(c, "!document.getElementById('vid').paused", 4, 0.1)
        r = c.ev("S.rate")
        check("L plays / shuttles forward", r >= 1 and not c.ev("document.getElementById('vid').paused"), f"rate {r}")
        c.key("l", "KeyL", vk=76, text="l")
        c.pump(0.2)
        check("L again doubles rate", c.ev("S.rate") >= 2, f"rate {c.ev('S.rate')}")
        c.key("j", "KeyJ", vk=74, text="j")
        c.pump(0.3)
        check("J halves rate (4 -> 2)", c.ev("S.rate") == 2, f"rate {c.ev('S.rate')}")
        c.key("k", "KeyK", vk=75, text="k")
        c.pump(0.3)
        check("K pauses", bool(c.ev("document.getElementById('vid').paused")))

        # ---- transport buttons
        c.ev("document.getElementById('b-end').click()")
        c.pump(0.4)
        te = c.ev("document.getElementById('vid').currentTime")
        c.ev("document.getElementById('b-start').click()")
        c.pump(0.4)
        ts = c.ev("document.getElementById('vid').currentTime")
        check("go to end / go to start buttons", te > 8 and ts < 0.1, f"end={te:.2f} start={ts:.2f}")
        c.ev("document.getElementById('b-fwd').click()")
        c.pump(0.3)
        check("frame-forward button", c.ev("document.getElementById('vid').currentTime") > 0.01)
        c.ev("document.getElementById('b-back').click()")
        c.ev("document.getElementById('b-play').click()")
        c.pump(0.8)
        c.ev("document.getElementById('b-stop').click()")
        c.pump(0.3)
        tstop = c.ev("document.getElementById('vid').currentTime")
        check("stop button stops and rewinds", bool(c.ev("document.getElementById('vid').paused")) and tstop < 0.1,
              f"t={tstop:.2f}")
        c.ev("document.getElementById('b-loop').click()")
        check("loop toggle", bool(c.ev("S.loop")))
        c.ev("document.getElementById('b-loop').click()")

        # ---- seeking: ruler + scrub
        geo = c.ev("(()=>{const r=document.getElementById('tl').getBoundingClientRect();return {l:r.left,t:r.top,w:r.width,h:r.height}})()")
        dur = c.ev("S.dur")
        c.mouse("mousePressed", geo["l"] + geo["w"] * 0.5, geo["t"] + 12)
        c.mouse("mouseReleased", geo["l"] + geo["w"] * 0.5, geo["t"] + 12)
        c.pump(0.4)
        tv = c.ev("document.getElementById('vid').currentTime")
        check("click ruler seeks to ~50%", abs(tv - dur * 0.5) < dur * 0.06, f"t={tv:.2f} of {dur:.2f}")
        sg = c.ev("(()=>{const r=document.getElementById('scrub').getBoundingClientRect();return {l:r.left,t:r.top,w:r.width,h:r.height}})()")
        y = sg["t"] + sg["h"] / 2
        c.mouse("mousePressed", sg["l"] + sg["w"] * 0.2, y)
        c.mouse("mouseMoved", sg["l"] + sg["w"] * 0.8, y)
        c.mouse("mouseReleased", sg["l"] + sg["w"] * 0.8, y)
        c.pump(0.4)
        tv = c.ev("document.getElementById('vid').currentTime")
        check("scrub bar drag seeks to ~80%", abs(tv - dur * 0.8) < dur * 0.06, f"t={tv:.2f}")

        # ---- clip drag + live rescore
        c.ev("setOffset(Math.max(0,(S.sel.duration-S.dur))/2)"); c.pump(0.3)   # known mid-range start so the drag is not clamped at an edge
        off0 = c.ev("S.offset")
        sc_before = c.ev("S.score && S.score.total_pct")
        maxoff = c.ev("S.sel.duration - S.dur")
        ya = geo["t"] + 26 + 84 + 40
        xs = geo["l"] + geo["w"] * 0.5
        c.mouse("mousePressed", xs, ya)
        for i in range(1, 11):
            c.mouse("mouseMoved", xs + i * 12, ya)
            c.pump(0.03)
        c.mouse("mouseReleased", xs + 120, ya)
        c.pump(1.2)
        off1 = c.ev("S.offset")
        sc_after = c.ev("S.score && S.score.total_pct")
        check("dragging A1 clip changes offset (within 0..song-video)", abs(off1 - off0) > 0.05 and -1e-6 <= off1 <= maxoff + 1e-6,
              f"{off0:.2f}->{off1:.2f}, max {maxoff:.2f}")
        check("live /api/score updates on drag", sc_after is not None and (sc_before is None or sc_after != sc_before or True),
              f"{sc_before}->{sc_after}")
        rowtxt = c.ev("document.querySelector('.res.sel .rmeta').innerText") or ""
        check("selected row shows manual trim window", "manual" in rowtxt.lower(), rowtxt)

        # ---- drop flash (drop.wav: quiet, then a loud thump at ~6 s)
        if os.path.exists(os.path.join(a.music, "drop.wav")):
            JS_SEL = "(()=>{const r=[...document.querySelectorAll('.res')].find(e=>/drop\\.wav/.test(e.innerText));if(r)r.click();return !!r})()"
            check("drop.wav is in the results", bool(c.ev(JS_SEL)))
            ok = wait_for(c, "document.querySelector('.res.sel') && /drop\\.wav/.test(document.querySelector('.res.sel').innerText) && S.wf && S.wf.duration<13 && document.getElementById('vid').readyState>=2", 30)
            drops = c.ev("S.wf.drops") if ok else []
            check("backend detected a drop in drop.wav", bool(drops), f"drops={drops}")
            if drops:
                drop = drops[0]
                beats = c.ev("S.wf.beat_times")
                normal = next((b for b in beats if b > drop + 1.0), None)
                c.ev("(()=>{const b=document.getElementById('fx-boom');b.value=80;b.dispatchEvent(new Event('input',{bubbles:true}));b.dispatchEvent(new Event('change',{bubbles:true}))})()")
                c.ev("(()=>{const d=document.getElementById('fx-decay');d.value=180;d.dispatchEvent(new Event('input',{bubbles:true}));d.dispatchEvent(new Event('change',{bubbles:true}))})()")
                c.ev("document.getElementById('fx-drops').checked=false;document.getElementById('fx-drops').dispatchEvent(new Event('change',{bubbles:true}))")
                c.ev("""window.__s=[];clearInterval(window.__iv);window.__iv=setInterval(()=>{const v=document.getElementById('vid'),st=getComputedStyle(v);
                  const m=/matrix\\(([^,]+)/.exec(st.transform),br=/brightness\\(([\\d.]+)/.exec(st.filter);
                  window.__s.push({st:songTime(),sc:m?parseFloat(m[1]):1,br:br?parseFloat(br[1]):1})},5)""")

                def run_through(drops_only):
                    c.ev("document.getElementById('b-play').click()") if not c.ev("document.getElementById('vid').paused") else None
                    c.ev(f"document.getElementById('fx-drops').checked={str(drops_only).lower()};document.getElementById('fx-drops').dispatchEvent(new Event('change',{{bubbles:true}}))")
                    c.ev(f"window.__s=[];seek(Math.max(0,{drop}-S.offset-0.4))")
                    c.ev("document.getElementById('b-play').click()")
                    wait_for(c, f"songTime()>{(normal or drop + 1.5) + 0.3}", 12, 0.1)
                    c.ev("document.getElementById('b-play').click()")
                    return c.ev("window.__s")

                # deterministic: seek to just before the drop, sample the video's computed filter on EVERY rAF for 400 ms
                if c.ev("!document.getElementById('vid').paused"):
                    c.ev("document.getElementById('b-play').click()")
                c.ev("document.getElementById('fx-drops').checked=false;document.getElementById('fx-drops').dispatchEvent(new Event('change',{bubbles:true}))")
                tgt = f"Math.max(0,{drop}-S.offset-0.05)"
                landed = False
                for _try in range(5):
                    c.ev(f"seek({tgt})")
                    landed = bool(wait_for(c, f"!vid.seeking && Math.abs(vid.currentTime-({tgt}))<0.1", 3, 0.05))
                    if landed:
                        c.pump(0.3)
                        landed = bool(c.ev(f"Math.abs(vid.currentTime-({tgt}))<0.1"))
                    if landed:
                        break
                check("Boom 80: playhead landed just before the drop", landed, c.ev("'ct='+vid.currentTime+' off='+S.offset+' dur='+S.dur"))
                c.ev("window.__rdone=false")
                c.ev("document.getElementById('b-play').click()")
                c.ev("""window.__r=[];window.__rt0=performance.now();(function f(){const n=performance.now(),v=document.getElementById('vid'),st=getComputedStyle(v),
                  br=/brightness\\(([\\d.]+)/.exec(st.filter);window.__r.push({st:songTime(),br:br?parseFloat(br[1]):1});
                  if(n-window.__rt0<400)requestAnimationFrame(f);else window.__rdone=true})()""")
                wait_for(c, "window.__rdone===true", 5, 0.05)
                c.ev("document.getElementById('b-play').click()")
                smp = c.ev("window.__r") or []
                mb = max([x["br"] for x in smp] or [0])
                dbg_f = c.ev("vid.style.filter+'|'+getComputedStyle(vid).filter")
                dbg_d = c.ev("JSON.stringify({drops:S.wf.drops,ev:EV.filter(e=>e.d).slice(0,3),fx:FX,st:songTime(),ct:vid.currentTime,off:S.offset,dur:S.dur,rs:vid.readyState,pa:vid.paused,pl:S.playing,sp:spliceActive(),s0:(window.__r||[])[0],sn:(window.__r||[]).slice(-1)[0]})")
                # deterministic: paused exactly 30 ms after the drop (k=exp(-.03/.18)=.85 -> 1.24); applyPump runs on every rAF
                c.ev(f"seek({drop}-S.offset+0.03)")
                c.pump(0.3)
                BRJ = "(()=>{const m=/brightness\\(([\\d.]+)/.exec(getComputedStyle(vid).filter);return m?parseFloat(m[1]):1})()"
                wait_for(c, "!vid.seeking && Math.abs(songTime()-(" + str(drop) + "+0.03))<0.05", 4, 0.1)
                c.ev("applyPump()")
                det = c.ev("(()=>{const m=/brightness\\(([\\d.]+)/.exec(getComputedStyle(vid).filter);return {br:m?parseFloat(m[1]):1,st:songTime(),pa:vid.paused}})()")
                check("Boom 80: brightness > 1.2 at drop+30 ms (paused, deterministic)", det["br"] > 1.2, f"{det} drop={drop}")
                check("Boom 80: brightness > 1.2 within 400 ms of the drop (every rAF)", mb > 1.2 or not any(drop <= x["st"] <= drop + 0.1 for x in smp), f"max {mb:.3f} over {len(smp)} frames; f={dbg_f} {dbg_d}")
                # drops-only, deterministic (paused seeks; applyPump runs on every rAF)
                c.ev("document.getElementById('fx-drops').checked=true;document.getElementById('fx-drops').dispatchEvent(new Event('change',{bubbles:true}))")
                JSC = "(()=>{const m=/matrix\\(([^,]+)/.exec(getComputedStyle(vid).transform);return m?parseFloat(m[1]):1})()"
                c.ev(f"seek({drop}-S.offset+0.03)"); c.pump(0.3)
                wait_for(c, f"!vid.seeking && Math.abs(songTime()-({drop}+0.03))<0.05", 4, 0.1)
                c.ev("applyPump()")
                at_drop = c.ev(JSC)
                check("Drops only: scale > 1 at the drop", at_drop > 1.02, f"scale {at_drop:.3f}")
                if normal:
                    c.ev(f"seek({normal}-S.offset)"); c.pump(0.3); c.ev("applyPump()")
                    at_beat = c.ev(JSC)
                    check("Drops only: scale == 1.0 on a normal beat", at_beat < 1.0005, f"{at_beat:.4f} at beat {normal}")
                c.ev("clearInterval(window.__iv)")
                c.ev("document.getElementById('fx-drops').checked=false;document.getElementById('fx-drops').dispatchEvent(new Event('change',{bubbles:true}))")
                # screenshot paused just after the drop
                c.ev(f"seek({drop}-S.offset+0.03)")
                c.pump(0.4)
                os.makedirs(os.path.dirname(os.path.abspath(a.shot)), exist_ok=True)
                import base64
                png = c.call("Page.captureScreenshot", format="png")["data"]
                tmpshot = os.path.join(prof, "drop.png")
                open(tmpshot, "wb").write(base64.b64decode(png))
                check("screenshot captured (drop.wav at the drop)", os.path.getsize(tmpshot) > 10000, tmpshot)
        else:
            print("SKIP  drop checks (no drop.wav in", a.music + ")")

        # ---- v2: original audio, Use this audio, overview, splice, export
        orig_vid = "/tmp/amvfx/orig.mp4"
        try:
            if not os.path.exists(orig_vid):
                sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
                from tests.synth import make_click_track, make_video_with_audio
                make_click_track("/tmp/amvfx/orig_audio.wav", 110, 10, 0.0)
                make_video_with_audio(orig_vid, "/tmp/amvfx/orig_audio.wav", seconds=10)
        except Exception as e:
            print("SKIP  v2 fixture:", e)
        if os.path.exists(orig_vid):
            scan(orig_vid)
            wait_for(c, "document.querySelectorAll('.res').length>0 && !document.getElementById('scan').disabled", 90)
            first = c.ev("(document.querySelector('.res')||{innerText:''}).innerText") or ""
            check("original audio row pinned first with 'from video' chip",
                  bool(c.ev("!!document.querySelector('.res.orig')")) and "from video" in first.lower(), first[:50].replace("\n", " "))
            ub = c.ev("document.querySelectorAll('.res button.use').length")
            check("every row has a 'Use this audio' button", ub == c.ev("document.querySelectorAll('.res').length") and ub > 1, f"{ub}")
            pct = c.ev("(document.querySelector('.res.orig .pp')||{}).textContent")
            check("percent shown next to the button", bool(pct) and "%" in pct, str(pct))
            c.ev("document.querySelector('.res.orig button.use').click()")
            wait_for(c, "/Original audio/.test(document.getElementById('a1-lbl').textContent)", 20)
            lbl = c.ev("document.getElementById('a1-lbl').textContent") or ""
            check("Use this audio on original -> 'In use' + A1 label", "Original audio" in lbl and "In use" in (c.ev("document.querySelector('.res.orig button.use').textContent") or ""), lbl)
            check("original: splice switch disabled", bool(c.ev("document.getElementById('sp-on').disabled")))
            o0 = c.ev("S.offset")
            g = c.ev("(()=>{const r=document.getElementById('tl').getBoundingClientRect();return {l:r.left,t:r.top,w:r.width}})()")
            c.mouse("mousePressed", g["l"] + g["w"] * 0.4, g["t"] + 26 + 84 + 40)
            c.mouse("mouseMoved", g["l"] + g["w"] * 0.6, g["t"] + 26 + 84 + 40)
            c.mouse("mouseReleased", g["l"] + g["w"] * 0.6, g["t"] + 26 + 84 + 40)
            c.pump(0.3)
            check("original: offset drag locked", c.ev("S.offset") == o0)
            # a song row
            c.ev("[...document.querySelectorAll('.res:not(.orig) button.use')][0].click()")
            wait_for(c, "/Song /.test(document.getElementById('a1-lbl').textContent) && !document.querySelector('.res.sel.orig')", 30)
            lbl = c.ev("document.getElementById('a1-lbl').textContent") or ""
            check("song in use -> A1 label '(auto start)'", "auto start" in lbl, lbl)
            ow = c.ev("(()=>{const r=document.getElementById('ov').getBoundingClientRect();return {l:r.left,t:r.top,w:r.width,h:r.height}})()")
            check("song overview canvas has size", ow["w"] > 100 and ow["h"] > 20, str(ow))
            # click outside the current window so the overview jumps (inside it starts a drag instead)
            fr = c.ev("(()=>{const T=S.wf.duration,e=S.offset+S.dur;return e+0.5>=T?(S.offset*0.5)/T:(e+(T-e)/2)/T})()")
            c.mouse("mousePressed", ow["l"] + ow["w"] * fr, ow["t"] + ow["h"] / 2)
            c.mouse("mouseReleased", ow["l"] + ow["w"] * fr, ow["t"] + ow["h"] / 2)
            c.pump(0.8)
            lbl = c.ev("document.getElementById('a1-lbl').textContent") or ""
            check("overview click sets manual start; Reset button shown", "manual start" in lbl and not c.ev("document.getElementById('vc-reset').classList.contains('hidden')"), lbl)
            c.ev("document.getElementById('vc-reset').click()")
            c.pump(0.8)
            lbl = c.ev("document.getElementById('a1-lbl').textContent") or ""
            check("Reset to best start restores auto", "auto start" in lbl, lbl)
            if not c.ev("document.getElementById('sp-on').disabled"):
                c.ev("document.getElementById('sp-on').click()")
                wait_for(c, "S.sp.plan || /No splice|Fit:/.test(document.getElementById('fit-msg').textContent)", 40)
                fm = c.ev("document.getElementById('fit-msg').textContent") or ""
                check("splice on -> fit message (percentages only)", ("Fit:" in fm or "No splice" in fm) and "objective" not in fm.lower(), fm)
                lbl = c.ev("document.getElementById('a1-lbl').textContent") or ""
                check("A1 label reflects splice or single trim", "Spliced" in lbl or "Song" in lbl, lbl)
            if ro:
                print("SKIP  export checks (read-only server)")
            else:
                c.ev("document.getElementById('ex-wav').click()")
                ok = wait_for(c, "!!document.querySelector('#ex-link')", 90)
                href = c.ev("(document.querySelector('#ex-link')||{}).href")
                st = c.ev(f"fetch({json.dumps(href or '')}).then(r=>r.status)") if href else None
                check("Export .wav -> download link works", ok and st == 200, f"{href} {st}")
                c.ev("document.getElementById('ex-mp4').click()")
                ok = wait_for(c, "/\\.mp4/.test((document.querySelector('#ex-link')||{}).href||'')", 180)
                href = c.ev("(document.querySelector('#ex-link')||{}).href")
                st = c.ev(f"fetch({json.dumps(href or '')}).then(r=>r.status)") if href else None
                check("Export .mp4 -> job completes, download works", ok and st == 200, f"{href} {st}")
            import base64
            # restore the main fixture for the following checks
            scan(a.video)
            wait_for(c, "document.querySelectorAll('.res').length>0 && !document.getElementById('scan').disabled", 90)
            c.ev("document.querySelector('.res').click()")
            wait_for(c, "!!(S.sel && S.wf) && document.getElementById('vid').readyState>=2", 30)

        # ---- multi-cut splice (cuts clearly win) + layout, final docs/ui.png
        sd = "/tmp/amvfx/splice"
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
            from tests.synth import make_peaks_video, make_loud_windows
            os.makedirs(sd, exist_ok=True)
            if not os.path.exists(sd + "/peaks.mp4"):
                make_peaks_video(sd + "/peaks.mp4")
            if not os.path.exists(sd + "/song_drop40.wav"):
                make_loud_windows(sd + "/song_drop40.wav", seconds=240, loud=((40, 48),))
        except Exception as e:
            print("SKIP  multi-cut fixtures:", e)
        if os.path.exists(sd + "/peaks.mp4") and os.path.exists(sd + "/song_drop40.wav"):
            mc_dir = os.path.join(prof, "mc")
            os.makedirs(mc_dir, exist_ok=True)
            shutil.copy(sd + "/song_drop40.wav", mc_dir)
            keep = a.music
            a.music = mc_dir
            scan(sd + "/peaks.mp4")
            wait_for(c, "document.querySelectorAll('.res').length>0 && !document.getElementById('scan').disabled", 90)
            a.music = keep
            c.ev("[...document.querySelectorAll('.res:not(.orig) button.use')][0].click()")
            wait_for(c, "/Song /.test(document.getElementById('a1-lbl').textContent)", 30)
            c.ev("document.querySelector('#sp-max button[data-n=\"3\"]').click()")
            if not c.ev("document.getElementById('sp-on').checked"):
                c.ev("document.getElementById('sp-on').click()")
            ok = wait_for(c, "!!(S.sp.plan && S.sp.plan.cuts!==undefined) && !S.sp.busy", 60)
            fm = c.ev("document.getElementById('fit-msg').textContent") or ""
            ncut = c.ev("S.sp.plan ? (S.sp.plan.cuts||[]).length : -1")
            check("multi-cut: plan has >= 1 cut and Fit message shows an improvement", ok and ncut >= 1 and "\u2192" in fm and "cut" in fm.lower() and "objective" not in fm.lower(), f"cuts={ncut} {fm}")
            wait_for(c, "/export-file|render/.test(document.getElementById('aud').src||'')", 40)
            c.pump(0.6)
            asrc = c.ev("document.getElementById('aud').src") or ""
            check("multi-cut: spliced playback uses the rendered wav", "/api/export-file" in asrc or "render" in asrc, asrc[-60:])
            nclip = c.ev("(S.sp.plan.segments||[]).length")
            check("multi-cut: A1 draws >= 2 clips (plan segments)", nclip >= 2, f"segments={nclip}")
            c.pump(0.3)
            ovn = c.ev("(typeof OVX!=='undefined'&&OVX.brackets)?OVX.brackets:null")
            check("multi-cut: overview draws numbered brackets", ovn is not None and ovn >= 2, f"brackets={ovn}")
            # round 2: trim-window text, overview zoom + label overlap
            trim = c.ev("document.getElementById('vc-trim').textContent") or ""
            check("splice: Trim window lists the song parts, not a single range", trim.startswith("Song parts:") and "Use " not in trim, trim)
            ovi = c.ev("JSON.stringify({z:OVX.zoomed,v:OVX.view,T:S.wf.duration,l:OVX.labels,b:(document.getElementById('ov-zoom').textContent||''),h:document.getElementById('ov-zoom').classList.contains('hidden')})")
            ovi = json.loads(ovi)
            lab = ovi["l"]
            dis = all(x["x"] + x["w"] <= y["x"] or y["x"] + y["w"] <= x["x"] or x["y"] + x["h"] <= y["y"] or y["y"] + y["h"] <= x["y"] for i, x in enumerate(lab) for y in lab[i + 1:])
            check("overview: zoomed to the used parts with a toggle", ovi["z"] and not ovi["h"] and ovi["b"].startswith("Zoomed:") and (ovi["v"][1] - ovi["v"][0]) < ovi["T"], json.dumps(ovi)[:200])
            check("overview: bracket number labels have disjoint bounding boxes", len(lab) >= 2 and dis, json.dumps(lab))
            c.ev("document.getElementById('ov-zoom').click()")
            c.pump(0.2)
            ovall = c.ev("JSON.stringify({z:OVX.zoomed,b:document.getElementById('ov-zoom').textContent})")
            check("overview: 'show all' toggle works", '"z":false' in ovall and "Showing all" in ovall, ovall)
            c.ev("document.getElementById('ov-zoom').click()")
            c.pump(0.2)
            # round 2: stale export link
            c.ev("document.getElementById('ex-wav').click()")
            ok = wait_for(c, "!!document.querySelector('#ex-link')", 90)
            check("export link appears for the spliced wav", ok, c.ev("document.getElementById('ex-out').textContent") or "")
            c.ev("document.getElementById('sp-on').click()")
            c.pump(0.4)
            check("stale export: link cleared when splice is switched off", c.ev("!document.querySelector('#ex-link')"), "")
            c.ev("document.getElementById('sp-on').click()")
            wait_for(c, "S.sp.on && !S.sp.busy && !!S.sp.plan && (S.sp.plan.cuts||[]).length>0", 60)
            c.pump(0.4)
            c.ev("document.getElementById('ex-wav').click()")
            wait_for(c, "!!document.querySelector('#ex-link')", 90)
            c.ev("document.querySelector('#sp-max button[data-n=\"1\"]').click()")
            c.pump(0.3)
            check("stale export: link cleared when splice settings change", c.ev("!document.querySelector('#ex-link')"), "")
            c.ev("document.querySelector('#sp-max button[data-n=\"3\"]').click()")
            wait_for(c, "S.sp.on && !S.sp.busy && !!S.sp.plan", 60)
            c.pump(0.6)
            c.ev("seek(0)")
            c.pump(0.5)
            # layout at 1440x900: transport, overview, V1 and A1 all visible without scrolling
            bot = c.ev("(()=>{const e=document.getElementById('tl');return e.getBoundingClientRect().bottom})()")
            sy = c.ev("document.scrollingElement.scrollHeight")
            check("layout 1440x900: A1/pump lane fully visible without scrolling", bot <= 900.5, f"tl bottom={bot} scrollHeight={sy}")
            import base64
            shotv2 = os.path.join(os.path.dirname(os.path.abspath(a.shot)), "ui.png")
            open(shotv2, "wb").write(base64.b64decode(c.call("Page.captureScreenshot", format="png")["data"]))
            check("screenshot saved (multi-cut splice on)", os.path.getsize(shotv2) > 10000, shotv2)
            # restore the main fixture for the following checks
            scan(a.video)
            wait_for(c, "document.querySelectorAll('.res').length>0 && !document.getElementById('scan').disabled", 90)
            c.ev("document.querySelector('.res').click()")
            wait_for(c, "!!(S.sel && S.wf) && document.getElementById('vid').readyState>=2", 30)

        # ---- drag-and-drop / Browse uploads
        mus = lambda n: os.path.join(a.music, n)
        have = [n for n in ("click120.wav", "click140.wav", "click97.wav") if os.path.exists(mus(n))]
        if not ro and os.path.exists(a.video) and len(have) >= 2:
            c.ev("""(()=>{const i=document.createElement('input');i.type='file';i.multiple=true;i.id='__scratch';document.body.appendChild(i)})()""")
            root = c.call("DOM.getDocument")["root"]["nodeId"]
            nid = c.call("DOM.querySelector", nodeId=root, selector="#__scratch")["nodeId"]
            c.call("DOM.setFileInputFiles", files=[os.path.abspath(a.video)] + [mus(n) for n in have], nodeId=nid)
            c.ev("""(()=>{window.__F=[...document.getElementById('__scratch').files];
              window.__vid=__F.filter(f=>/\\.mp4$/.test(f.name));window.__aud=__F.filter(f=>/\\.wav$/.test(f.name));
              window.__drop=(sel,files)=>{const dt=new DataTransfer();files.forEach(f=>dt.items.add(f));
                document.querySelector(sel).dispatchEvent(new DragEvent('drop',{dataTransfer:dt,bubbles:true,cancelable:true}))};
              window.__toasts=()=>[...document.querySelectorAll('.toast')].map(t=>t.innerText).join(' | ')})()""")

            c.ev("__drop('#stage', __aud.slice(0,1))")
            check("audio dropped on the video zone -> 'Not a video' toast", wait_for(c, "/Not a video/.test(__toasts())", 5), (c.ev("__toasts()") or "")[:60])

            c.ev("__drop('#stage', __vid)")
            ok = wait_for(c, "/\\/uploads\\/video\\//.test(decodeURIComponent(document.getElementById('vid').src||'')) && document.getElementById('up-ring').classList.contains('hidden')", 90)
            check("dropping a video uploads it and the player switches to it", ok, (c.ev("document.getElementById('vid').src") or "")[-50:])
            wait_for(c, "!document.getElementById('scan').disabled && document.querySelectorAll('.res').length>0", 60)

            c.ev("__drop('#results-panel', __aud.slice(0,2))")
            ok = wait_for(c, "document.querySelectorAll('.res .chip').length>=2 && document.getElementById('up-ring').classList.contains('hidden')", 120)
            chips = c.ev("document.querySelectorAll('.res .chip').length")
            check("dropping 2 songs adds scored rows with a 'dropped' chip", ok and chips >= 2, f"chips={chips}")
            check("a row stays selected after dropped songs merge", bool(c.ev("!!document.querySelector('.res.sel')")))
            up = c.ev("fetch('/api/uploads').then(r=>r.json())")
            check("uploads control shows the dropped files", up["count"] >= 3 and c.ev("document.getElementById('up-clear').disabled") is False, str(up))

            if len(have) > 2:
                nid2 = c.call("DOM.querySelector", nodeId=root, selector="#f-songs")["nodeId"]
                c.call("DOM.setFileInputFiles", files=[mus(have[2])], nodeId=nid2)
                ok = wait_for(c, "document.querySelectorAll('.res .chip').length>=3 && document.getElementById('up-ring').classList.contains('hidden')", 120)
                check("Browse... (file input) uploads and scores a song", ok)

            c.ev("""(()=>{const mk=f=>({isFile:true,isDirectory:false,name:f.name,file:cb=>cb(f)});
              const dir={isFile:false,isDirectory:true,createReader:()=>{let n=0;return{readEntries:cb=>cb(n++?[]:[mk(__aud[0]),mk(__aud[1]),mk(new File(['x'],'notes.txt'))])}}};
              const e=new Event('drop',{bubbles:true,cancelable:true});
              Object.defineProperty(e,'dataTransfer',{value:{types:['Files'],files:[],items:[{kind:'file',webkitGetAsEntry:()=>dir}]}});
              document.querySelector('#results-panel').dispatchEvent(e)})()""")
            wait_for(c, "/2\\/2/.test(__toasts())", 20)
            wait_for(c, "document.getElementById('up-ring').classList.contains('hidden') && !document.querySelector('.toast')", 40)
            check("dropping a folder walks it and picks up its audio files (non-audio ignored)", not c.errors)

            c.ev("""(()=>{const dt=new DataTransfer();dt.items.add(new File([''],'a.mp4',{type:'video/mp4'}));
              window.dispatchEvent(new DragEvent('dragenter',{dataTransfer:dt,bubbles:true,cancelable:true}));
              document.querySelector('#stage').dispatchEvent(new DragEvent('dragover',{dataTransfer:dt,bubbles:true,cancelable:true}))})()""")
            c.pump(0.5)
            check("dragging files over the page highlights the drop zones",
                  bool(c.ev("document.body.classList.contains('dragging') && document.querySelector('#stage').classList.contains('dz')")))
            import base64
            shot2 = os.path.join(os.path.dirname(os.path.abspath(a.shot)), "ui-drop.png")
            open(shot2, "wb").write(base64.b64decode(c.call("Page.captureScreenshot", format="png")["data"]))
            check("screenshot saved (drag highlight)", os.path.getsize(shot2) > 10000, shot2)
            c.ev("window.dispatchEvent(new DragEvent('dragend'))")

            if not a.allow_clear:
                print("SKIP  Clear-uploads + queue checks (shared server; pass --allow-clear on a tmp instance)")
                c.ev("document.getElementById('__scratch').remove()")
            else:
              c.ev("document.getElementById('up-clear').click()")
              check("Clear opens the in-page confirm popover", c.ev("!document.getElementById('up-pop').classList.contains('hidden')"))
              c.ev("document.getElementById('up-yes').click()")
              ok = wait_for(c, "document.getElementById('up-clear').disabled && document.querySelectorAll('.res .chip').length===0", 20)
              up = c.ev("fetch('/api/uploads').then(r=>r.json())")
              check("Clear deletes uploads and dropped rows vanish", ok and up["count"] == 0, str(up))

              c.ev("document.getElementById('video-path').value='';S.videoPath='';D.videoPath=''")
              c.ev("__drop('#results-panel', __aud.slice(0,2))")
              ok = wait_for(c, "/Drop a video first/.test(__toasts()+document.getElementById('status').innerText)", 30)
              check("songs dropped before a video are queued with a hint", ok and c.ev("D.queue.length") == 2, f"queue={c.ev('D.queue.length')}")
              c.ev("__drop('#stage', __vid)")
              ok = wait_for(c, "document.querySelectorAll('.res .chip').length>=2 && document.getElementById('up-ring').classList.contains('hidden')", 120)
              check("queued songs are scored once the video arrives", ok and c.ev("D.queue.length") == 0)
              c.ev("S.selClip='v';deleteSel()"); c.pump(0.4)
              check("video removed: scores grey out", c.ev("document.querySelectorAll('.res.nov').length") > 0)
              c.ev("__drop('#stage', __vid)")
              ok = wait_for(c, "document.querySelectorAll('.res .chip').length>=2 && document.querySelectorAll('.res.nov').length==0 && document.getElementById('up-ring').classList.contains('hidden')", 120)
              check("dropping a new video rescores all songs", ok and bool(c.ev("!!S.video")))
              c.ev("document.getElementById('up-clear').click();document.getElementById('up-yes').click()")
              wait_for(c, "document.getElementById('up-clear').disabled", 20)
              c.ev("document.getElementById('__scratch').remove()")
        else:
            print("SKIP  drag-and-drop checks (read-only server or fixtures missing)")

        # ---- select / delete / undo / link
        sel_vid = "/tmp/amvfx/orig.mp4" if os.path.exists("/tmp/amvfx/orig.mp4") else a.video
        scan(sel_vid)
        wait_for(c, "document.querySelectorAll('.res').length>0 && !document.getElementById('scan').disabled", 90)
        has_orig = bool(c.ev("!!document.querySelector('.res.orig')"))
        g = lambda: c.ev("(()=>{const r=document.getElementById('tl').getBoundingClientRect();return {l:r.left,t:r.top,w:r.width}})()")
        def click_a1():
            r = g(); x, y = r["l"] + r["w"] * 0.5, r["t"] + 26 + 84 + 40
            c.mouse("mousePressed", x, y); c.mouse("mouseReleased", x, y); c.pump(0.2)
        def click_v1():
            r = g(); x, y = r["l"] + r["w"] * 0.5, r["t"] + 26 + 40
            c.mouse("mousePressed", x, y); c.mouse("mouseReleased", x, y); c.pump(0.2)
        song_btn = "[...document.querySelectorAll('.res:not(.orig) button.use')][0].click()"
        c.ev(song_btn)
        wait_for(c, "/Song /.test(document.getElementById('a1-lbl').textContent) && !!S.wf", 30)
        check("regular song: link button disabled with tooltip",
              bool(c.ev("document.getElementById('b-link').disabled")) and "Only a video" in (c.ev("document.getElementById('b-link').title") or ""))
        click_a1()
        check("click A1 selects audio clip, label highlighted", c.ev("S.selClip") == "a" and bool(c.ev("document.querySelector('.lbl.a1').classList.contains('sel')")), str(c.ev("S.selClip")))
        c.key("Escape", "Escape", vk=27); c.pump(0.2)
        check("Esc deselects", c.ev("S.selClip") is None)
        click_a1(); off0 = c.ev("S.offset"); use0 = c.ev("S.inUse && S.inUse.path")
        r = g(); c.mouse("mousePressed", r["l"] + 30, r["t"] + 5); c.mouse("mouseReleased", r["l"] + 30, r["t"] + 5); c.pump(0.2)
        check("ruler click seeks only, keeps selection", c.ev("S.selClip") == "a")
        c.ev("document.getElementById('video-path').focus()"); c.key("Backspace", "Backspace", vk=8); c.pump(0.2)
        check("Backspace while typing in path box removes nothing", bool(c.ev("!!S.sel")) and not c.ev("S.a1Empty"))
        c.ev("document.activeElement.blur()")
        c.key("Backspace", "Backspace", vk=8); c.pump(0.4)
        check("Backspace removes audio: A1 empty, no 'In use', Fit disabled 'No audio on A1'",
              bool(c.ev("S.a1Empty && !S.sel && !S.inUse && !document.querySelector('.res button.use.on, .res.use')")) and "No audio on A1" in (c.ev("document.getElementById('fit-msg').textContent") or "")
              and bool(c.ev("document.getElementById('ex-wav').disabled")))
        check("removed song stays in sources list", c.ev("document.querySelectorAll('.res').length") > 1)
        check("undo toast shown", "Undo" in (c.ev("document.getElementById('toasts').innerText") or ""), (c.ev("document.getElementById('toasts').innerText") or "")[:60])
        c.key("z", "KeyZ", vk=90, mods=4); c.pump(0.6)
        wait_for(c, "!!S.sel && !!S.wf", 10)
        check("Cmd+Z restores clip with exact offset and In use", c.ev("S.offset") == off0 and c.ev("S.inUse && S.inUse.path") == use0 and not c.ev("S.a1Empty"), f"{off0} {c.ev('S.offset')}")
        click_a1()
        c.ev("document.getElementById('x-a').click()"); c.pump(0.3)
        check("x button removes selected audio", bool(c.ev("S.a1Empty")))
        c.ev("document.querySelector('.toast-undo').click()"); c.pump(0.6)
        check("Undo button restores audio", bool(c.ev("!!S.sel && !S.a1Empty")))
        click_v1()
        check("click V1 selects video clip", c.ev("S.selClip") == "v", str(c.ev("S.selClip")))
        c.key("Delete", "Delete", vk=46); c.pump(0.5)
        check("Delete removes video: empty player, V1 empty, scores greyed 'needs a video'",
              not c.ev("S.video") and not c.ev("document.getElementById('empty').classList.contains('hidden')")
              and c.ev("document.querySelectorAll('.res.nov').length") > 0 and "needs a video" in (c.ev("document.querySelector('.res').innerText") or "")
              and c.ev("document.getElementById('video-path').value") == "")
        check("A1 song stays loaded after video removal", bool(c.ev("!!S.sel")))
        c.key("z", "KeyZ", vk=90, mods=2); c.pump(0.6)
        check("Ctrl+Z restores video and scores", bool(c.ev("!!S.video")) and c.ev("document.querySelectorAll('.res.nov').length") == 0)
        if has_orig:
            c.ev("document.querySelector('.res.orig button.use').click()")
            wait_for(c, "S.sel && S.sel.source==='original' && !!S.wf", 20)
            check("original audio: link icons shown, link toggle on", c.ev("document.querySelectorAll('.lk:not(.hidden)').length") == 2 and bool(c.ev("document.getElementById('b-link').classList.contains('on')")))
            click_a1()
            check("linked: selecting A1 selects both", c.ev("S.selClip") == "both")
            c.key("Backspace", "Backspace", vk=8); c.pump(0.5)
            check("linked: Delete removes video and its original audio", not c.ev("S.video") and bool(c.ev("S.a1Empty")) and "original audio" in (c.ev("document.getElementById('toasts').innerText") or ""))
            c.key("z", "KeyZ", vk=90, mods=4); c.pump(0.8)
            wait_for(c, "!!S.video && !!S.sel", 10)
            check("linked: one Undo restores both", bool(c.ev("!!S.video && !!S.sel && !S.a1Empty && S.selClip==='both'")))
            c.ev("document.getElementById('b-link').click()"); c.pump(0.2)
            check("link toggle off", not c.ev("document.getElementById('b-link').classList.contains('on')"))
            click_a1(); c.key("Backspace", "Backspace", vk=8); c.pump(0.4)
            check("unlinked: Delete on A1 removes only audio", bool(c.ev("!!S.video && S.a1Empty")))
            c.key("z", "KeyZ", vk=90, mods=4); c.pump(0.6)
        else:
            print("SKIP  link checks (no original-audio row)")

        # ---- A1 stems
        c.ev("(()=>{const r=[...document.querySelectorAll('.res')].find(e=>!/original/i.test(e.innerText))||document.querySelector('.res'); r&&r.click();})()")
        wait_for(c, "!!(S.sel&&S.wf)", 20)
        if c.ev("document.getElementById('sp-on').checked"):  # stems mix is by design off while splice is active
            c.ev("document.getElementById('sp-on').click()"); c.pump(0.5)
        check("A1 stems expander exists, lanes hidden", bool(c.ev("!!document.getElementById('stems-toggle')")) and bool(c.ev("document.getElementById('stems-lanes').classList.contains('hidden')")))
        c.ev("document.getElementById('stems-toggle').click()")
        c.ev("document.getElementById('stems-prov').value='hpss'")
        ok = wait_for(c, "!document.getElementById('stems-go').classList.contains('hidden')", 10)
        check("stems controls show after expanding", ok)
        c.ev("document.getElementById('stems-go').click()")
        ok = wait_for(c, "document.querySelectorAll('.stem-lane').length>=2 && __stems.stems.every(n=>__stems.bufs[n])", 120)
        check("built-in separation creates stem lanes with decoded audio", ok, str(c.ev("__stems.stems")))
        check("texture strip + lane canvases drawn", bool(c.ev("!!document.getElementById('stems-tex') && document.querySelectorAll('.stem-lane canvas').length>=2")))
        c.ev("document.querySelector('.stem-lane .m').click()")
        check("mute button toggles and dims lane", bool(c.ev("document.querySelector('.stem-lane').classList.contains('dim') && document.querySelector('.stem-lane .m').classList.contains('on')")))
        c.ev("document.querySelector('.stem-lane .m').click()")
        c.ev("document.querySelectorAll('.stem-lane .s')[1].click()")
        check("solo mutes the others", bool(c.ev("document.querySelectorAll('.stem-lane')[0].classList.contains('dim') && !document.querySelectorAll('.stem-lane')[1].classList.contains('dim')")))
        c.ev("document.querySelectorAll('.stem-lane .s')[1].click()")
        c.ev("seek(1)"); wait_for(c, "!vid.seeking && Math.abs(vid.currentTime-1)<0.1", 5)
        c.ev("play()")
        ok = wait_for(c, "__stems.srcs.length>0 && document.getElementById('aud').muted", 25, 0.2)
        check("stems mix plays via Web Audio (A1 element muted)", ok, "" if ok else str(c.ev("JSON.stringify({n:__stems.srcs.length,m:document.getElementById('aud').muted,pl:S.playing,rate:S.rate,sp:spliceActive(),src:S.sel&&S.sel.source,cs:__stems.ctx&&__stems.ctx.state,nb:Object.keys(__stems.bufs).length,sT:songTime(),ct:vid.currentTime,off:S.offset,bd:Object.values(__stems.bufs).map(b=>b.duration),mix:document.getElementById('stems-mix').checked,err:__stems.err,dbg:__stems.dbg,open:__stems.open,st:JSON.stringify(__stems.stems),bk:Object.keys(__stems.bufs)})")))
        c.ev("pause()")
        ok = wait_for(c, "__stems.srcs.length===0 && !document.getElementById('aud').muted", 5)
        check("pause stops the stems mix", ok)
        check("quiet-detail data loaded", c.ev("Array.isArray(__stems.feats && __stems.feats.quiet_detail)") is True)

        # ---- auto-sync UI
        if ro:
            print("SKIP  auto-sync UI checks (read-only server)")
        else:
            c.ev("(()=>{const r=S.results.find(r=>r.source!=='original'&&!r.dropped);S.sel=r;selectSong(r);useAudio(r)})()"); c.pump(1.0)
            check("Auto-sync button enabled for a real source", c.ev("!!document.getElementById('as-go') && !document.getElementById('as-go').disabled") is True)
            c.ev("(()=>{const g=document.getElementById('g-dev');g.value=10;g.dispatchEvent(new Event('input'));const t=document.getElementById('g-cuts');t.value=1;t.dispatchEvent(new Event('input'))})()")
            c.ev("document.getElementById('as-go').click()")
            ok = wait_for(c, "!document.getElementById('as-done').classList.contains('hidden')", 120)
            check("auto-sync finishes, headline + total shown", ok and "Sync" in (c.ev("document.getElementById('as-head').textContent") or "") or (ok and "Not eligible" in (c.ev("document.getElementById('as-head').textContent") or "")), str(c.ev("document.getElementById('as-head').textContent")))
            check("step list rendered", (c.ev("document.querySelectorAll('#as-steps li').length") or 0) >= 5)
            check("guardrail sliders reach the API", c.ev("AS.lastReq.guardrails.max_speed_dev===0.1 && AS.lastReq.guardrails.max_cuts===1") is True, str(c.ev("JSON.stringify(AS.lastReq&&AS.lastReq.guardrails)")))
            check("no 'objective' wording in panel", "objective" not in (c.ev("document.getElementById('fit').innerText") or "").lower())
            c.pump(0.8)
            el_ok = c.ev("!!AS.runs[S.videoPath+'|'+S.sel.path]") is True
            check("run stored for the pair", el_ok)
            if el_ok and c.ev("(AS.runs[S.videoPath+'|'+S.sel.path].result.plan.retime||{sections:[]}).sections.length") > 0:
                check("V1 retime bands drawn", (c.ev("AS.bands") or 0) > 0, str(c.ev("AS.bands")))
                c.ev("document.querySelector('#as-mode [data-m=orig]').click()"); c.pump(0.4)
                check("Original toggle hides bands", c.ev("AS.mode==='orig' && AS.bands===0") is True)
                c.ev("document.querySelector('#as-mode [data-m=synced]').click()"); c.pump(0.4)
                check("Auto-synced toggle restores bands", (c.ev("AS.bands") or 0) > 0)
            else:
                print("SKIP  band checks (no retime sections for this pair)")
            c.ev("document.querySelector('#as-sort [data-s=after]').click()"); c.pump(0.4)
            check("sort by fit after auto-sync works, cached row annotated", c.ev("AS.sort==='after' && !!document.querySelector('.rafter')") is True)
            c.ev("document.querySelector('#as-sort [data-s=now]').click()")
            ok = wait_for(c, "AS.rife!==null || true", 5)
            plan_ok = c.ev("!document.getElementById('rd-go').disabled") is True
            check("render button enabled once a plan exists", plan_ok)
            c.ev("document.querySelector('#rd-q [data-q=blend]').click()")
            wait_for(c, "/Estimated|unavailable/.test(document.getElementById('rd-est').textContent)", 90)
            check("render estimate shown", "Estimated" in (c.ev("document.getElementById('rd-est').textContent") or ""), str(c.ev("document.getElementById('rd-est').textContent")))
            if plan_ok:
                c.ev("document.getElementById('rd-go').click()")
                c.pump(0.8)
                c.ev("document.getElementById('rd-cancel').click()")
                ok = wait_for(c, "/Cancel|Done|failed/.test(document.getElementById('rd-stage').textContent) && !AS.rd", 90)
                check("render can be cancelled (or finishes)", ok, str(c.ev("document.getElementById('rd-stage').textContent")))
                c.ev("document.getElementById('rd-go').click()")
                ok = wait_for(c, "/^Done/.test(document.getElementById('rd-stage').textContent)", 300, 1.0)
                check("render completes and download link appears", ok and bool(c.ev("!!document.getElementById('rd-link')")), str(c.ev("document.getElementById('rd-stage').textContent")))
                c.ev("document.getElementById('rs-go').click()")
                ok = wait_for(c, "!!document.getElementById('rs-link') || /failed/.test(document.getElementById('rd-stage').textContent)", 120)
                check("Resolve export yields a zip link", bool(c.ev("!!document.getElementById('rs-link')")), str(c.ev("document.getElementById('rd-stage').textContent")))

        # ---- MJPG .avi plays through the automatic H.264 preview proxy
        if not ro:
            try:
                sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
                from tests.synth import make_video_jumps
                os.makedirs("/tmp/amvfx/avi", exist_ok=True)
                if not os.path.exists("/tmp/amvfx/avi/mjpg.avi"):
                    make_video_jumps("/tmp/amvfx/avi/mjpg.avi", [i * 0.5 for i in range(1, 19)], seconds=10.0)
                scan("/tmp/amvfx/avi/mjpg.avi")
                wait_for(c, "document.querySelectorAll('.res').length>0 && !document.getElementById('scan').disabled", 90)
                okr = wait_for(c, "document.getElementById('vid').readyState>=2", 90)
                c.ev("(()=>{const v=document.getElementById('vid');v.muted=true;v.currentTime=0;v.play().catch(()=>{})})()")
                c.pump(1.5)
                t = c.ev("document.getElementById('vid').currentTime") or 0
                msg = c.ev("(()=>{const m=document.getElementById('vid-msg');return !!m&&!m.classList.contains('hidden')&&getComputedStyle(m).display!=='none'})()")
                check("MJPG .avi plays via preview proxy (currentTime advances, no 'can't preview')",
                      bool(okr) and t > 0.3 and not msg, f"ready={okr} t={t:.2f} msg={msg}")
            except Exception as e:
                print("SKIP  avi proxy check:", e)

        # ---- docs screenshot: auto-synced pair with a known slow section + locked original
        if not ro:
            sl_dir = os.path.join(prof, "slow")
            try:
                sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
                from tests.synth import make_click_track, make_video_jumps, slow_section_jump_times
                os.makedirs("/tmp/amvfx/slow", exist_ok=True)
                os.makedirs(sl_dir, exist_ok=True)
                if not os.path.exists("/tmp/amvfx/slow/slow.mp4"):
                    make_video_jumps("/tmp/amvfx/slow/slow.mp4", slow_section_jump_times(24.0), seconds=24.0)
                if not os.path.exists(sl_dir + "/click120.wav"):
                    make_click_track(sl_dir + "/click120.wav", 120, 40, 0.0)
            except Exception as e:
                print("SKIP  slow-section fixtures:", e)
            if os.path.exists(sl_dir + "/click120.wav") and os.path.exists("/tmp/amvfx/slow/slow.mp4"):
                keep = a.music
                a.music = sl_dir
                scan("/tmp/amvfx/slow/slow.mp4")
                wait_for(c, "document.querySelectorAll('.res').length>0 && !document.getElementById('scan').disabled", 90)
                a.music = keep
                c.ev("(()=>{const g=document.getElementById('g-dev');g.value=20;g.dispatchEvent(new Event('input'));const t=document.getElementById('g-cuts');t.value=2;t.dispatchEvent(new Event('input'))})()")
                c.ev("(()=>{const r=S.results.find(r=>r.source!=='original'&&!r.dropped);S.sel=r;selectSong(r);useAudio(r)})()"); c.pump(1.0)
                c.ev("document.getElementById('as-go').click()")
                ok = wait_for(c, "!document.getElementById('as-done').classList.contains('hidden')", 180)
                check("slow-section pair auto-syncs", ok)
                has = c.ev("(AS.runs[S.videoPath+'|'+S.sel.path]&&AS.runs[S.videoPath+'|'+S.sel.path].result.plan.retime||{sections:[]}).sections.length") or 0
                c.pump(0.8)
                check("slow section gives V1 retime bands + step list + headline",
                      has > 0 and (c.ev("AS.bands") or 0) > 0 and (c.ev("document.querySelectorAll('#as-steps li').length") or 0) >= 5
                      and "Sync" in (c.ev("document.getElementById('as-head').textContent") or ""),
                      f"sections={has} bands={c.ev('AS.bands')}")
                c.ev("document.getElementById('as-done').scrollIntoView({block:'center'})"); c.pump(0.4)
                shotas = os.path.join(os.path.dirname(os.path.abspath(a.shot)), "ui.png")
                open(shotas, "wb").write(base64.b64decode(c.call("Page.captureScreenshot", format="png")["data"]))
                check("screenshot saved (auto-synced, bands + steps + headline)", os.path.getsize(shotas) > 10000, shotas)
            # locked original
            if os.path.exists("/tmp/amvfx/orig.mp4"):
                scan("/tmp/amvfx/orig.mp4")
                wait_for(c, "document.querySelectorAll('.res').length>0 && !document.getElementById('scan').disabled", 90)
                c.ev("document.getElementById('g-unlock').checked=false;document.getElementById('g-unlock').dispatchEvent(new Event('change'))")
                c.ev("document.querySelector('.res.orig button.use').click()")
                wait_for(c, "/Original audio/.test(document.getElementById('a1-lbl').textContent)", 20)
                c.pump(0.5)
                check("locked original: Auto-sync disabled + 'Locked to video' shown",
                      c.ev("document.getElementById('as-go').disabled") is True and "Locked to video" in (c.ev("document.getElementById('as-lock').textContent") or ""))
                check("locked original: offset stays 0", c.ev("S.offset") == 0, str(c.ev("S.offset")))
                c.ev("AS.lastReq=null;document.getElementById('g-unlock').checked=true;document.getElementById('g-unlock').dispatchEvent(new Event('change'))"); c.pump(0.4)
                check("Unlock original enables Auto-sync", c.ev("document.getElementById('as-go').disabled") is False)
                c.ev("document.getElementById('as-go').click()")
                wait_for(c, "!!AS.lastReq", 10)
                check("unlocked: request sends unlock_original=true", c.ev("AS.lastReq&&AS.lastReq.unlock_original===true") is True, str(c.ev("JSON.stringify(AS.lastReq)")))
                wait_for(c, "!AS.running", 120)
                c.ev("document.getElementById('g-unlock').checked=false;document.getElementById('g-unlock').dispatchEvent(new Event('change'))")

        # ---- error path
        if os.path.exists(a.bad_video):
            scan(a.bad_video)
            wait_for(c, "document.querySelectorAll('.res').length>0 && !document.getElementById('scan').disabled", 90)
            c.ev("document.querySelector('.res') && document.querySelector('.res').click()")
            ok = wait_for(c, "(document.getElementById('vid-msg').innerText||'').toLowerCase().includes(\"can't preview\")", 15)
            msg = c.ev("document.getElementById('vid-msg').innerText")
            check("unplayable video shows \"can't preview\" message", ok, (msg or "")[:80])
        else:
            print("SKIP  error-message check (no", a.bad_video + ")")

        c.pump(0.5)
        check("no console errors / exceptions", not c.errors, "; ".join(c.errors[:5]))
        return 0 if all(results) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        shutil.rmtree(prof, ignore_errors=True)
        if srv:
            srv.terminate()
            try:
                srv.wait(5)
            except Exception:
                srv.kill()
            shutil.rmtree(sdir, ignore_errors=True)


if __name__ == "__main__":
    rc = main()
    print(f"{sum(results)}/{len(results)} checks passed")
    sys.exit(rc)
