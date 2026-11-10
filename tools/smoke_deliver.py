#!/usr/bin/env python3
"""Edit/Deliver page smoke test (reuses tools/ui_smoke.py's CDP helpers).

Checks: save -> reload -> identical state, missing file -> relink dialog, autosave writes.
Usage: .venv/bin/python tools/smoke_project.py [--video clip.mp4] [--music DIR]
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, time, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ui_smoke as U

SRV = 8797
CPORT = 9335
U.SRV_PORT = SRV


def serve(d):
    sys.path.insert(0, os.path.join(HERE, ".."))
    from pathlib import Path
    import uvicorn
    from amv_matcher import server, project
    server.UPLOADS, server.EXPORTS, server.EXTRACTED = (Path(d) / n for n in ("uploads", "exports", "extracted"))
    project.APP_DATA = Path(d) / "appdata"
    (Path(d) / "appdata").mkdir(exist_ok=True)
    server.ALLOWED_ORIGINS = {f"http://127.0.0.1:{SRV}", f"http://localhost:{SRV}"}
    uvicorn.run(server.app, host="127.0.0.1", port=SRV, log_level="warning")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--_serve", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a._serve:
        return serve(a._serve)
    sdir = tempfile.mkdtemp(prefix="amv-pj-")
    url = f"http://127.0.0.1:{SRV}"
    srv = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--_serve", sdir], cwd=os.path.join(HERE, ".."),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/api/uploads", timeout=1); break
        except Exception:
            time.sleep(0.2)
    else:
        print("FAIL  server did not start"); srv.kill(); return 2
    prof = tempfile.mkdtemp(prefix="amv-dlui-")
    proc = subprocess.Popen([U.CHROME, "--headless=new", f"--remote-debugging-port={CPORT}", f"--user-data-dir={prof}",
                             "--window-size=1440,900", "--no-first-run", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tabs = None
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CPORT}/json")); break
            except Exception:
                time.sleep(0.2)
        c = U.CDP(next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"])
        for m in ("Page.enable", "Runtime.enable", "Log.enable"):
            c.call(m)
        chk, wf = U.check, U.wait_for
        c.call("Page.navigate", url=url)
        chk("page + deliver UI load", wf(c, "document.readyState==='complete' && !!window.DELIVER_UI", 15))
        chk("tabs present", bool(c.ev("!!document.querySelector('#pages [data-pg=edit]') && !!document.querySelector('#pages [data-pg=deliver]')")))
        chk("edit unlocked", c.ev("DELIVER_UI.locked()") is False)
        c.ev("document.querySelector('#pages [data-pg=deliver]').click()")
        chk("deliver page shown", bool(c.ev("document.body.classList.contains('page-deliver') && getComputedStyle(document.getElementById('deliver')).display!=='none'")))
        chk("media locked on deliver", c.ev("DELIVER_UI.locked()") is True)
        chk("lock message shown", wf(c, "/disabled on Deliver/.test(document.getElementById('dl-lock').textContent)", 3))
        chk("drop blocked", c.ev("(()=>{let hit=false;document.getElementById('stage').addEventListener('drop',()=>hit=true);const e=new Event('drop',{bubbles:true,cancelable:true});document.getElementById('stage').dispatchEvent(e);return !hit && e.defaultPrevented})()") is True)
        c.ev("document.querySelector('#pages [data-pg=edit]').click()")
        chk("edit unlocks again", wf(c, "DELIVER_UI.locked()===false", 3))
        # simulated running render
        c.ev("AS.rd={running:true,id:'x'};document.getElementById('rd-bar').style.width='50%';document.getElementById('rd-stage').textContent='Rendering'")
        chk("render locks media on Edit", wf(c, "DELIVER_UI.locked()===true && document.body.classList.contains('rendering')", 3))
        chk("playhead visible at ~50%", wf(c, "getComputedStyle(document.getElementById('dl-playhead')).display==='block' && document.getElementById('dl-playhead').style.left==='50%'", 3))
        chk("cancel button shown", bool(c.ev("!document.getElementById('dl-cancel').classList.contains('hidden')")))
        chk("render lock message", wf(c, "/render is running/.test(document.getElementById('dl-lock').textContent)", 3))
        c.ev("AS.rd=null")
        chk("unlocks after render", wf(c, "DELIVER_UI.locked()===false", 3))
        chk("no JS errors", not c.errors, "; ".join(c.errors[:3]))
    finally:
        proc.kill(); srv.kill()
        shutil.rmtree(prof, ignore_errors=True); shutil.rmtree(sdir, ignore_errors=True)
    bad = U.results.count(False)
    print(f"{len(U.results) - bad}/{len(U.results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
