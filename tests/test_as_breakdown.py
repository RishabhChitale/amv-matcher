"""static/js/as-breakdown.js: F5 auto-sync result breakdown rows (before -> after)."""
import json, shutil, subprocess, pathlib
import pytest

NODE = shutil.which("node") or "/Users/Rishabh/.local/node/bin/node"
SRC = pathlib.Path(__file__).resolve().parent.parent / "static" / "js" / "as-breakdown.js"
pytestmark = pytest.mark.skipif(not pathlib.Path(NODE).exists() and not shutil.which("node"), reason="node missing")

HARNESS = r"""
const fs=require('fs');
const res=%s, sel=%s;
global.window={AS:{curRun:()=>({result:res})},S:{sel}};
global.document={getElementById:()=>null};
eval(fs.readFileSync(%s,'utf8'));
console.log(JSON.stringify(window.ASBreakdown.rows(res,sel)));
"""

def _rows(res, sel):
    r = subprocess.run([NODE, "-e", HARNESS % (json.dumps(res), json.dumps(sel), json.dumps(str(SRC)))], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-400:]
    return json.loads(r.stdout)

def test_before_after():
    res = {"final": {"tempo_pct": 90, "sync_pct": 80, "arc_pct": 70, "tight_pct": 85, "median_err_ms": 12.4,
                     "tightness_start": {"tight_pct": 40}}}
    o = {r["name"]: r for r in _rows(res, {"tempo_pct": 88, "sync_pct": 50, "arc_pct": 71})}
    assert o["Sync"]["before"] == 50 and o["Sync"]["after"] == 80
    assert o["Tightness"]["before"] == 40 and "12 ms" in o["Tightness"]["tip"]
    assert o["Energy"]["after"] == 70

def test_missing_and_ineligible():
    o = {r["name"]: r for r in _rows({"final": {"tempo_pct": 90}}, None)}
    assert list(o) == ["Tempo"] and o["Tempo"]["before"] is None
    assert _rows({"final": {"tempo_pct": 90}, "eligible": False}, None) is None
    assert _rows({}, None) is None
