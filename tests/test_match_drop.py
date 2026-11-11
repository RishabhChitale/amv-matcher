"""match.js must not break drops when window.nativeDrop is undefined at load time (script-order independence)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "static" / "match.js"
NODE = shutil.which("node") or "/Users/Rishabh/.local/node/bin/node"

pytestmark = pytest.mark.skipif(not Path(NODE).exists(), reason="node not available")


def _hook_source():
    s = SRC.read_text()
    m = re.search(r"  function hookDrop\(\) \{.*?\n  hookDrop\(\);[^\n]*\n", s, re.S)
    assert m, "hookDrop block not found in match.js"
    return m.group(0)


def _run(scenario):
    js = """
const calls = []; const listeners = {};
const window = {addEventListener: (n, f) => { listeners[n] = f; }};
let dropKind = null; const setFromPaths = (k, p) => calls.push(['set', k, p]);
%s
const out = {};
%s
console.log(JSON.stringify({calls, out}));
""" % (_hook_source(), scenario)
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_no_native_drop_at_load_does_not_throw():
    o = _run("window.nativeDrop(['/a']); dropKind='video'; window.nativeDrop(['/b']);")
    assert o["calls"] == [["set", "video", ["/b"]]]


def test_native_drop_defined_late_is_still_reached():
    o = _run("window.nativeDrop = p => calls.push(['late', p]); listeners.load(); window.nativeDrop(['/c']);")
    assert o["calls"] == [["late", ["/c"]]]


def test_native_drop_defined_before_still_forwarded():
    r = subprocess.run([NODE, "-e", """
const calls=[]; const listeners={}; const window={addEventListener:(n,f)=>{listeners[n]=f;}};
let dropKind=null; const setFromPaths=(k,p)=>calls.push(['set',k,p]);
window.nativeDrop = p => calls.push(['base', p]);
%s
window.nativeDrop(['/d']); dropKind='dir'; window.nativeDrop(['/e']); listeners.load(); window.nativeDrop(['/f']);
console.log(JSON.stringify(calls));""" % _hook_source()], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [["base", ["/d"]], ["set", "dir", ["/e"]], ["base", ["/f"]]]


def test_old_pattern_would_fail():
    assert "origDrop(paths)" not in SRC.read_text()
