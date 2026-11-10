#!/usr/bin/env python3
"""Project save/load smoke test (reuses tools/ui_smoke.py's CDP helpers).

Checks: save -> reload -> identical state, missing file -> relink dialog, autosave writes.
Usage: .venv/bin/python tools/smoke_project.py [--video clip.mp4] [--music DIR]
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, time, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ui_smoke as U

SRV = 8798
CPORT = 9334
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


STATE = ("JSON.stringify({v:S.videoPath,r:(S.results||[]).map(x=>x.path).sort(),u:S.inUse&&S.inUse.path,"
         "o:+(S.offset||0).toFixed(3),sp:[S.sp.on,S.sp.max],fx:FX,g:AS.guard})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--_serve", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--video", default="/tmp/amvfx/clip.mp4")
    ap.add_argument("--music", default="/tmp/amvfx/music")
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
    prof = tempfile.mkdtemp(prefix="amv-pjui-")
    proc = subprocess.Popen([U.CHROME, "--headless=new", f"--remote-debugging-port={CPORT}", f"--user-data-dir={prof}",
                             "--autoplay-policy=no-user-gesture-required", "--window-size=1440,900", "--no-first-run",
                             "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

        def load():
            c.call("Page.navigate", url=url)
            return wf(c, "document.readyState==='complete' && !!window.PROJECT_UI", 15)

        chk("page + project UI load", load())
        chk("project bar present", bool(c.ev("!!document.getElementById('pj-save') && !!document.getElementById('pj-name')")))
        c.ev(f"document.getElementById('video-path').value={json.dumps(a.video)};document.getElementById('music-dir').value={json.dumps(a.music)};document.getElementById('scan').click();")
        chk("scan completes", wf(c, "document.querySelectorAll('.res').length>0 && !document.getElementById('scan').disabled", 90))
        c.ev("document.querySelector('.res').click()")
        c.pump(1.5)
        c.ev("(()=>{const e=document.getElementById('fx-boom'); if(e){e.value=0.5;e.dispatchEvent(new Event('input',{bubbles:true}));}})()")
        before = c.ev(STATE)
        proj = os.path.join(sdir, "t.amvproj")
        ok = c.ev(f"PROJECT_UI.save(false).then(()=>false)") if False else None
        c.ev(f"PROJECT.path={json.dumps(proj)};PROJECT.name='t'")
        chk("save returns true", c.ev("PROJECT_UI.save(false)"))
        chk("file written", os.path.exists(proj))
        chk("recent lists it", any(os.path.realpath(r["path"]) == os.path.realpath(proj) for r in json.load(urllib.request.urlopen(url + "/api/project/recent"))["recent"]))
        chk("dirty cleared after save", wf(c, "PROJECT.dirty===false", 5))

        # save -> reload -> identical state
        chk("reload", load())
        c.ev(f"PROJECT_UI.open({json.dumps(proj)})")
        wf(c, "!!S.inUse && !PROJECT.loading", 60)
        after = c.ev(STATE)
        chk("restored state identical", before == after, f"{(before or '')[:80]} vs {(after or '')[:80]}")

        # autosave
        c.ev("PROJECT.dirty=true;PROJECT.loading=false")
        chk("autosave call ok", c.ev("PROJECT_UI.autosave().then(()=>true)"))
        ad = os.path.join(sdir, "appdata", "autosave")
        chk("autosave file exists", os.path.isdir(ad) and any(f.endswith(".amvproj") for f in os.listdir(ad)))

        # missing file -> relink dialog
        d = json.load(open(proj))
        d["data"] = d.get("data", d)
        raw = json.load(open(proj))
        data = raw.get("data", raw)
        gone = os.path.join(sdir, "moved")
        os.makedirs(gone, exist_ok=True)
        moved = os.path.join(gone, "clip.mp4")
        shutil.copy(a.video, moved)
        if data.get("video"):
            data["video"]["path"] = os.path.join(sdir, "nowhere", "clip.mp4")
        json.dump(raw if "data" in raw else data, open(proj, "w"))
        chk("reload for relink", load())
        c.ev(f"void PROJECT_UI.open({json.dumps(proj)})")
        chk("relink dialog shown", wf(c, "!!document.querySelector('.pj-dlg [data-i]') && /Missing files/.test(document.body.innerText)", 15))
        chk("Find in folder button offered", bool(c.ev("!!document.querySelector('.pj-find')")))
        c.ev("document.getElementById('pj-cancel').click()")

        chk("no JS errors", not c.errors, "; ".join(c.errors[:3]))
    finally:
        proc.kill(); srv.kill()
        shutil.rmtree(prof, ignore_errors=True); shutil.rmtree(sdir, ignore_errors=True)
    bad = U.results.count(False)
    print(f"{len(U.results) - bad}/{len(U.results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
