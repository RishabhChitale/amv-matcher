import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import check_leftovers as cl  # noqa: E402

MD = """# L
| ID | Area | Leftover | Owner | Status | How we'll verify |
|---|---|---|---|---|---|
| L1 | A | x | o | fixed in abc123 (note) | tests/test_a.py::test_one |
| L2 | A | x | o | done | `pytest tests/test_b.py -k foo` |
| L3 | A | x | o | open | tests/test_c.py::test_c |
| L4 | A | x | o | fixed in def | manual check in the native app |
| L5 | A | x | o | fixed | `rm -rf /tmp/x` |
| L6 | A | x | o | fixed | `.venv/bin/python tools/real_suite.py --quick` |
| L7 | A | x | o | fixed | `pytest tests/x.py; rm -rf /` |
"""


def rows():
    return {r["id"]: r for r in map(cl.classify, cl.parse(MD))}


def test_parse_and_classify():
    r = rows()
    assert len(r) == 7
    assert r["L1"]["kind"] == "automatable"
    assert r["L1"]["argv"] == [".venv/bin/python", "-m", "pytest", "-q", "tests/test_a.py::test_one"]
    assert r["L2"]["argv"][-3:] == ["tests/test_b.py", "-k", "foo"]
    assert r["L3"]["kind"] == "skip"
    assert r["L4"]["kind"] == "manual"
    assert r["L5"]["kind"] == "unsafe"
    assert r["L6"]["kind"] == "automatable"
    assert r["L7"]["kind"] == "unsafe"


def test_dry_run_executes_nothing(monkeypatch, tmp_path, capsys):
    f = tmp_path / "L.md"
    f.write_text(MD)
    monkeypatch.setattr(cl, "LEFTOVERS", f)
    monkeypatch.setattr(cl.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    assert cl.main(["--dry-run", "--only", "L1,L2,L5"]) == 0
    out = capsys.readouterr().out
    assert "automatable 2" in out and "unsafe 1" in out


def test_inferred_aliases():
    md = """| L1 | A | x | o | fixed | ui_smoke 106/106 |
| L2 | A | x | o | fixed | real_suite 6/6 pairs |
| L3 | A | x | o | fixed | selftest passes |
| L4 | A | x | o | fixed | see tests/test_z.py and tests/test_y.py::t |
| L5 | A | x | o | fixed | pytest -k "foo and bar" |
| L6 | A | x | o | fixed | looks fine to me |
"""
    r = {x["id"]: cl.classify(x) for x in cl.parse(md)}
    assert r["L1"]["argv"] == [".venv/bin/python", "tools/ui_smoke.py"] and r["L1"]["expect"] == (106, 106)
    assert r["L1"]["inferred"] and not r["L1"]["heavy"]
    assert r["L2"]["heavy"] and r["L2"]["argv"][-1] == "tools/real_suite.py"
    assert r["L3"]["argv"][-1] == "--selftest"
    assert r["L4"]["argv"][-2:] == ["tests/test_z.py", "tests/test_y.py::t"]
    assert r["L5"]["argv"][-2:] == ["-k", "foo and bar"]
    assert r["L6"]["kind"] == "manual"


def test_heavy_not_run_without_flag(monkeypatch):
    monkeypatch.setattr(cl.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    row = {"id": "L2", "argv": ["x"], "heavy": True, "include_heavy": False}
    assert cl.run_row(row, 5)["result"] == "skipped"
