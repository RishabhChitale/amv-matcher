"""JsonCache must survive several worker processes flushing the same file at once (scan prefetch runs song analyses in
parallel worker processes): no process may raise, the file stays valid JSON, and no process's entries are lost."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
CODE = """
import sys
from pathlib import Path
from amv_matcher.cache import JsonCache
path, tag, n = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
c = JsonCache(path)
for i in range(n):
    c.set(f"{tag}-{i}", {"v": i})
"""


def _run_procs(path, procs=4, n=40):
    env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
    ps = [subprocess.Popen([sys.executable, "-c", CODE, str(path), f"p{k}", str(n)], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for k in range(procs)]
    return [(p.wait(120), p.stderr.read()) for p in ps]


def test_concurrent_flushes_do_not_raise_or_lose_entries(tmp_path):
    path = tmp_path / "songs.json"
    for rc, err in _run_procs(path):
        assert rc == 0, err                          # old shared "songs.tmp" raised FileNotFoundError here
    data = json.loads(path.read_text())              # still valid JSON
    assert len(data) == 4 * 40                        # every process's entries survived the merge
    assert not list(tmp_path.glob("*.tmp"))          # no temp files left behind


def test_flush_merges_other_writers_entries(tmp_path):
    from amv_matcher.cache import JsonCache
    path = tmp_path / "c.json"
    a, b = JsonCache(path), JsonCache(path)
    a.set("x", 1)
    b.set("y", 2)                                    # b never saw "x"; its flush must not drop it
    assert json.loads(path.read_text()) == {"x": 1, "y": 2}


def test_stale_tmp_files_are_swept_but_fresh_ones_kept(tmp_path):
    from amv_matcher.cache import JsonCache
    path = tmp_path / "songs.json"
    stale = tmp_path / "songs.4242.1.tmp"            # left by a killed process
    fresh = tmp_path / "songs.4243.1.tmp"            # could be an in-flight write by a live process
    other = tmp_path / "other.4242.1.tmp"            # another cache's file: not ours to touch
    for f in (stale, fresh, other):
        f.write_text("{}")
    old = os.path.getmtime(stale) - 3600
    os.utime(stale, (old, old))
    os.utime(other, (old, old))
    JsonCache(path).set("k", 1)
    assert not stale.exists()
    assert fresh.exists() and other.exists()
    assert json.loads(path.read_text()) == {"k": 1}
