"""stem_targets dots in static/js/changes.js: source-song time is mapped through plan.segments."""
import json, shutil, subprocess, pathlib
import pytest

NODE = shutil.which("node") or "/Users/Rishabh/.local/node/bin/node"
SRC = pathlib.Path(__file__).resolve().parent.parent / "static" / "js" / "changes.js"
pytestmark = pytest.mark.skipif(not pathlib.Path(NODE).exists() and not shutil.which("node"), reason="node missing")

HARNESS = r"""
const fs=require('fs');
const run={result:{plan:%s,stem_targets:%s}};
global.AS={mode:'synced',curRun:()=>run};
global.window={AS:global.AS};
global.document={getElementById:()=>null,addEventListener(){}};
let src=fs.readFileSync(%s,'utf8');
try{ eval(src); }catch(e){}
const items=(window.ASChanges&&window.ASChanges.items)?window.ASChanges.items():[];
console.log(JSON.stringify(items.filter(i=>i.kind==='stem').map(i=>[i.v,i.label])));
"""

def _run(plan, targets):
    code = HARNESS % (json.dumps(plan), json.dumps(targets), json.dumps(str(SRC)))
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
    return r


def test_stem_targets_map_through_segments():
    plan = {"offset": 0, "segments": [
        {"src_start": 10, "src_end": 20, "dst_start": 0},
        {"src_start": 40, "src_end": 50, "dst_start": 10}]}
    t = [{"t": 12, "t_song": 12, "kind": "bass", "stem": "bass", "weight": 1},
         {"t": 45, "t_song": 45, "kind": "vocal", "stem": "vocals", "weight": 1},
         {"t": 30, "t_song": 30, "kind": "detail", "stem": "drums", "weight": 1}]
    r = _run(plan, t)
    assert r.returncode == 0, r.stderr[-400:]
    out = json.loads(r.stdout)
    assert [round(o[0], 3) for o in out] == [2.0, 15.0]   # t=30 is outside every used part
