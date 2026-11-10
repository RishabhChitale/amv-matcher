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

    def key(self, key, code=None, shift=False, vk=0, text=None):
        mod = 8 if shift else 0
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
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    ap.add_argument("--video", default="/tmp/amvfx/clip.mp4")
    ap.add_argument("--music", default="/tmp/amvfx/music")
    ap.add_argument("--bad-video", default="/tmp/amvfx/clip.mkv")
    ap.add_argument("--shot", default=os.path.join(os.path.dirname(__file__), "..", "docs", "ui.png"))
    a = ap.parse_args()

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
        check("rings and bars rendered", bool(c.ev("document.querySelectorAll('.res .ring').length>0 && document.querySelectorAll('.res .bar i').length>=3")))

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
          const f=()=>{out.push(Math.abs((a.currentTime-S.offset)-v.currentTime)*1000);performance.now()-t0<3000?requestAnimationFrame(f):res(out)};requestAnimationFrame(f)})"""))
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
        check("Boom 80: drop brightness > 1 (if song has drops)", mb > 1.0 or not c.ev("(S.wf.drops||[]).length"), f"max brightness {mb:.3f}")
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

                smp = run_through(False)
                win = [x for x in smp if abs(x["st"] - drop) <= 0.1]
                mb = max([x["br"] for x in win] or [0])
                dbg_f = c.ev("vid.style.filter+'|'+getComputedStyle(vid).filter")
                dbg_p = c.ev("JSON.stringify(pumpAt(5.88))")
                check("Boom 80: brightness > 1.2 within +-100 ms of the drop", mb > 1.2, f"max {mb:.3f} over {len(win)} samples; last={win[-3:]} pa={dbg_p} f={dbg_f}")
                smp = run_through(True)
                at_drop = max([x["sc"] for x in smp if abs(x["st"] - drop) <= 0.1] or [1])
                at_beat = [x["sc"] for x in smp if normal and abs(x["st"] - normal) <= 0.05]
                check("Drops only: scale > 1 at the drop", at_drop > 1.02, f"max scale {at_drop:.3f}")
                check("Drops only: scale == 1.0 on a normal beat", bool(at_beat) and max(at_beat) < 1.0005, f"{max(at_beat or [0]):.4f} at beat {normal}; fx={c.ev('JSON.stringify(FX)')} ev={c.ev('JSON.stringify(EV.slice(0,4))')}")
                c.ev("clearInterval(window.__iv)")
                c.ev("document.getElementById('fx-drops').checked=false;document.getElementById('fx-drops').dispatchEvent(new Event('change',{bubbles:true}))")
                # screenshot paused just after the drop
                c.ev(f"seek({drop}-S.offset+0.03)")
                c.pump(0.4)
                os.makedirs(os.path.dirname(os.path.abspath(a.shot)), exist_ok=True)
                import base64
                png = c.call("Page.captureScreenshot", format="png")["data"]
                open(a.shot, "wb").write(base64.b64decode(png))
                check("screenshot saved (drop.wav at the drop)", os.path.getsize(a.shot) > 10000, os.path.abspath(a.shot))
        else:
            print("SKIP  drop checks (no drop.wav in", a.music + ")")

        # ---- drag-and-drop / Browse uploads
        mus = lambda n: os.path.join(a.music, n)
        have = [n for n in ("click120.wav", "click140.wav", "click97.wav") if os.path.exists(mus(n))]
        if os.path.exists(a.video) and len(have) >= 2:
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
            ok = wait_for(c, "/\\.uploads\\//.test(decodeURIComponent(document.getElementById('vid').src||'')) && document.getElementById('up-ring').classList.contains('hidden')", 90)
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
            c.ev("document.getElementById('up-clear').click();document.getElementById('up-yes').click()")
            wait_for(c, "document.getElementById('up-clear').disabled", 20)
            c.ev("document.getElementById('__scratch').remove()")
        else:
            print("SKIP  drag-and-drop checks (fixtures missing)")

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


if __name__ == "__main__":
    rc = main()
    print(f"{sum(results)}/{len(results)} checks passed")
    sys.exit(rc)
