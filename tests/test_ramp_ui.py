"""static/js/ramp-ui.js: retime curve extraction + smooth-vs-best card rows (mocked result, per docs/U11C-RAMP-FORMAT.md)."""
import json, shutil, subprocess, pathlib
import pytest

NODE = shutil.which("node") or "/Users/Rishabh/.local/node/bin/node"
SRC = pathlib.Path(__file__).resolve().parent.parent / "static" / "js" / "ramp-ui.js"
pytestmark = pytest.mark.skipif(not pathlib.Path(NODE).exists() and not shutil.which("node"), reason="node missing")

HARNESS = r"""
const fs=require('fs');
const res=%s;
global.AS={mode:'synced',curRun:()=>({result:res})};
global.window={AS:global.AS,addEventListener(){}};
global.document={getElementById:()=>null};
eval(fs.readFileSync(%s,'utf8'));
console.log(JSON.stringify({c:window.ASRamp.curve(res),t:window.ASRamp.tempoCurve(res),r:window.ASRamp.cardRows(res.final)}));
"""

def _run(res):
    r = subprocess.run([NODE, "-e", HARNESS % (json.dumps(res), json.dumps(str(SRC)))], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-400:]
    return json.loads(r.stdout)

def test_curve_and_card():
    res = {"plan": {"retime_ramps": {"curve": [[0, 0, 1.0], [5, 5, 0.8], [10, 11, 1.2]]}},
           "final": {"ramps": {"total_step": 80, "total_after": 78, "sync_step": 70, "sync_after": 72, "smooth_step": 3.0, "smooth_ramp": 0.5}}}
    o = _run(res)
    assert [p["speed"] for p in o["c"]] == [1.0, 0.8, 1.2]
    rows = {r["name"]: r for r in o["r"]}
    assert rows["Total"]["better"] == "a" and rows["Sync"]["better"] == "b" and rows["Max jump"]["better"] == "b"

def test_step_fallback_and_none():
    o = _run({"plan": {"retime": {"sections": [{"v_start": 0, "v_end": 4, "speed": 0.9}]}}, "final": {}})
    assert [p["v"] for p in o["c"]] == [0, 4] and o["r"] is None
    assert _run({"plan": {}, "final": {}})["c"] is None
