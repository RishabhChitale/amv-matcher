"""Frozen-bundle check (macOS): a THROWAWAY instance (own AMV_DATA_DIR) must open exactly one window while analysis
jobs run in worker processes, and a second launch against the same data dir must exit without a new window.

    python tools/frozen_windows_check.py "<dist>/AMV Matcher.app/Contents/MacOS/AMV Matcher" <scratch dir>
Never run it against the user's data dir; it always sets its own."""
import json, os, signal, subprocess, sys, time, urllib.request
import Quartz

exe, scratch = sys.argv[1], os.path.abspath(sys.argv[2])
os.makedirs(scratch, exist_ok=True)
env = dict(os.environ, AMV_DATA_DIR=os.path.join(scratch, "data"), TMPDIR=scratch)


def tree(pid):
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
    pids = {pid}
    for c in out:
        pids |= tree(int(c))
    return pids


def windows(pids):
    ws = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)
    return [w for w in ws if w["kCGWindowOwnerPID"] in pids and w.get("kCGWindowLayer") == 0
            and w["kCGWindowBounds"]["Width"] > 100]


def post(port, path, body):
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(),
                               {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
    return json.load(urllib.request.urlopen(r, timeout=20))


sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import synth
vid = os.path.join(scratch, "v.mp4")
synth.make_video(vid, seconds=12)
p = subprocess.Popen([exe], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
fail = []
try:
    time.sleep(10)
    port = int(subprocess.run(["lsof", "-nP", "-a", "-p", str(p.pid), "-iTCP", "-sTCP:LISTEN", "-Fn"],
                              capture_output=True, text=True).stdout.split(":")[-1].split()[0])
    t0 = time.time()                      # wait (up to ~30 s) for the window to exist before counting
    while time.time() - t0 < 30 and not windows(tree(p.pid)):
        time.sleep(0.5)
    base = len(windows(tree(p.pid)))
    post(port, "/api/media/roots", {"path": scratch})
    post(port, "/api/media/link", {"path": vid})
    for k in ("video_analysis", "thumbnails", "probe"):
        post(port, "/api/jobs", {"kind": k, "path": vid})
    time.sleep(8)
    kids = tree(p.pid) - {p.pid}
    after = len(windows(tree(p.pid)))
    print(f"windows before={base} after_jobs={after} worker/child processes={len(kids)}")
    if base < 1 or after != base:
        fail.append("window count changed during jobs")
    # second launch against the same data dir: must exit by itself, no new window, no new top-level instance
    s = subprocess.Popen([exe], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        rc = s.wait(20)
    except subprocess.TimeoutExpired:
        s.kill(); rc = None; fail.append("second launch did not exit")
    time.sleep(2)
    final = len(windows(tree(p.pid) | {s.pid}))
    print(f"second launch exit={rc} windows now={final}")
    if final != base:
        fail.append("second launch added a window")
finally:
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(15)
    except subprocess.TimeoutExpired:
        p.kill()
print("FAIL: " + "; ".join(fail) if fail else "frozen_windows_check ok")
sys.exit(1 if fail else 0)
