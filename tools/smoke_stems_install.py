#!/usr/bin/env python3
"""Smoke test for the stems-pack install card (static/js/stems-install.js) against a MOCKED backend.

No real Demucs install: a tiny local server serves static/ and fakes /api/stems/install*. Reuses tools/ui_smoke.py's CDP helpers.
Run under the heavy lock: /usr/bin/lockf -k /tmp/amv-heavy.lock .venv/bin/python tools/smoke_stems_install.py
"""
import json, mimetypes, os, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ui_smoke as U

STATIC = os.path.join(HERE, "..", "static")
SRV, CPORT = 8797, 9335
M = {"mode": "idle", "job_polls": 0, "idle_polls": 0, "installed": False, "cancelled": False, "stage_i": 0}
STAGES = ["venv", "downloading", "installing", "verifying"]


def job():
    i = M["stage_i"]
    if M["cancelled"]:
        return {"id": "j1", "status": "cancelled", "stage": "venv", "progress": 0.2, "message": "Cancelled", "error": None}
    if i >= len(STAGES):
        M["installed"] = True
        return {"id": "j1", "status": "done", "stage": "done", "progress": 1.0, "message": "Done", "error": None}
    return {"id": "j1", "status": "running", "stage": STAGES[i], "progress": (i + 1) / 5, "message": "working", "package": "torch" if i == 2 else None, "error": None}


def pack():
    return {"installed": M["installed"], "version": "4.0.1" if M["installed"] else None, "size_mb": 912 if M["installed"] else None,
            "source": "venv" if M["installed"] else None, "half_installed": False, "weights_cached": False}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/stems/install":
            M["idle_polls"] += 1
            return self.send(200, {"job": job() if M["stage_i"] or M["cancelled"] else None, "installed": M["installed"], "pack": pack()})
        if p == "/api/stems/install/j1":
            M["job_polls"] += 1
            if not M["cancelled"]:
                M["stage_i"] += 1
            return self.send(200, job())
        if p == "/api/stems/pack":
            return self.send(200, pack())
        if p.startswith("/api/"):
            return self.send(404, {"detail": "mock: not found"})
        f = os.path.join(STATIC, "index.html" if p == "/" else p[len("/static/"):] if p.startswith("/static/") else p.lstrip("/"))
        if not os.path.isfile(f):
            return self.send(404, {"detail": "nf"})
        data = open(f, "rb").read()
        if p == "/" and b"stems-install.js" not in data:
            data = data.replace(b"</body>", b'<script src="/static/js/stems-install.js"></script></body>')
        self.send_response(200); self.send_header("Content-Type", mimetypes.guess_type(f)[0] or "application/octet-stream"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n)
        p = self.path
        if p == "/api/stems/install":
            if M["mode"] == "507":
                return self.send(507, {"detail": "Not enough free disk space: 1.2 GB free, 3 GB needed"})
            if M["mode"] == "501":
                return self.send(501, {"detail": "no stems pack"})
            M["stage_i"] = 0; M["cancelled"] = False
            return self.send(200, job())
        if p == "/api/stems/install/j1/cancel":
            M["cancelled"] = True
            return self.send(200, {"cancelled": True, "job": "j1"})
        return self.send(404, {"detail": "nf"})


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", SRV), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    prof = tempfile.mkdtemp(prefix="amv-si-")
    proc = subprocess.Popen([U.CHROME, "--headless=new", f"--remote-debugging-port={CPORT}", f"--user-data-dir={prof}", "--window-size=1440,900", "--no-first-run", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        tabs = None
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CPORT}/json")); break
            except Exception:
                time.sleep(0.2)
        if not tabs:
            print("FAIL  Chrome did not start"); return 2
        c = U.CDP(next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"])
        for m in ("Page.enable", "Runtime.enable"):
            c.call(m)
        chk, wf = U.check, U.wait_for
        txt = lambda sel: c.ev(f"(document.querySelector({json.dumps(sel)})||{{}}).textContent||''")
        vis = lambda sel: bool(c.ev(f"(()=>{{const e=document.querySelector({json.dumps(sel)});return !!e&&!e.classList.contains('hidden')&&e.offsetParent!==null}})()"))

        def load(mode):
            M.update(mode=mode, job_polls=0, idle_polls=0, installed=False, cancelled=False, stage_i=0)
            c.call("Page.navigate", url=f"http://127.0.0.1:{SRV}/")
            wf(c, "document.readyState==='complete' && !!window.__stemsInstall", 15)
            c.ev("document.getElementById('stems-toggle').click()")
            return wf(c, "!document.getElementById('si-card').classList.contains('hidden')", 8)

        # idle -> running -> done
        chk("idle: install card shown when pack missing", load("idle"))
        chk("idle: Install button visible, Cancel hidden", vis("#si-go") and not vis("#si-cancel"))
        c.pump(3.0)
        chk("idle: no job polling", M["job_polls"] == 0, str(M["job_polls"]))
        chk("a11y: progressbar has role + aria-valuenow", bool(c.ev("(()=>{const b=document.querySelector('#si-card [role=progressbar]');return !!b&&b.hasAttribute('aria-valuenow')})()")))
        c.ev("document.getElementById('si-go').click()")
        seen = []
        end = time.time() + 15
        while time.time() < end and len(seen) < 4:
            cur = c.ev("(document.querySelector('.si-steps li.cur')||{}).dataset?.s||''")
            if cur and cur not in seen:
                seen.append(cur)
            c.pump(0.3)
        chk("running: stages seen venv->downloading->installing->verifying", seen == STAGES, str(seen))
        chk("done: 'Installed: Demucs 4.0.1, 912 MB' + weights note", wf(c, "document.querySelector('.si-title').textContent.indexOf('Installed: Demucs 4.0.1, 912 MB')===0", 10)
            and "first use" in txt(".si-note"))
        n = M["job_polls"]; c.pump(3.5)
        chk("done: polling stops", M["job_polls"] == n, f"{n}->{M['job_polls']}")

        # 507
        load("507"); c.ev("document.getElementById('si-go').click()")
        chk("507: exact GB needed + free shown, Retry visible", wf(c, "document.querySelector('.si-err').textContent.indexOf('3 GB needed')>=0", 6)
            and "1.2 GB free" in txt(".si-err") and vis("#si-retry"), txt(".si-err"))
        # 501
        load("501"); c.ev("document.getElementById('si-go').click()")
        e = ""
        wf(c, "document.querySelector('.si-err').textContent.length>0", 6); e = txt(".si-err")
        chk("501: names Python 3.9-3.12 + python.org, no Retry", "3.9" in e and "3.12" in e and "python.org" in e and not vis("#si-retry"), e)
        # cancel
        load("idle"); c.ev("document.getElementById('si-go').click()")
        wf(c, "!document.getElementById('si-cancel').classList.contains('hidden')", 6)
        c.ev("document.getElementById('si-cancel').click()")
        chk("cancel: back to Install button, no error", wf(c, "!document.getElementById('si-go').classList.contains('hidden')", 6) and not txt(".si-err"))
        n = M["job_polls"]; c.pump(3.0)
        chk("cancel: polling stops", M["job_polls"] == n)
    finally:
        proc.kill(); srv.shutdown()
    bad = len([r for r in U.results if not r])
    print("RESULT", "FAIL" if bad else "OK", f"({len(U.results)} checks, {bad} failed)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
