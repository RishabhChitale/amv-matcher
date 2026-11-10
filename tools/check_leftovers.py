#!/usr/bin/env python3
"""Re-verify the fixed/done rows of docs/LEFTOVERS.md whose 'How we'll verify' cell is automatable.

Automatable = a backticked command, or a bare pytest node id (tests/x.py::name) in the cell. Only commands that
start with pytest / python -m pytest / .venv/bin/python tools/ are run (argv, no shell); others are 'unsafe: skipped'.
Every run is wrapped in /usr/bin/lockf -k /tmp/amv-heavy.lock. Report: docs/leftovers-verify-latest.md.
Usage: tools/check_leftovers.py [--only L12,L14] [--dry-run] [--list] [--timeout 300]
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEFTOVERS = ROOT / "docs" / "LEFTOVERS.md"
REPORT = ROOT / "docs" / "leftovers-verify-latest.md"
LOCK = ["/usr/bin/lockf", "-k", "/tmp/amv-heavy.lock"]
PY = ".venv/bin/python"
NODE = re.compile(r"\btests/[\w./-]+\.py(?:::[\w\[\]\-.]+)*")


def parse(text: str) -> list[dict]:
    """Table rows -> [{id, area, status, verify}] (only rows whose first cell looks like L<number>)."""
    rows = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6 or not re.fullmatch(r"L\d+", cells[0]):
            continue
        rows.append({"id": cells[0], "area": cells[1], "status": cells[4], "verify": "|".join(cells[5:])})
    return rows


def is_done(status: str) -> bool:
    return bool(re.match(r"(fixed|done)\b", status.strip(), re.I))


# Explicit alias table: prose keyword -> (argv, heavy). Nothing else is ever inferred from prose.
ALIASES = {
    "ui_smoke": ([PY, "tools/ui_smoke.py"], False),
    "real_suite": ([PY, "tools/real_suite.py"], True),
    "selftest": ([PY, "app_main.py", "--selftest"], False),
}
EXPECT = re.compile(r"\b(ui_smoke|real_suite)\s+(\d+)\s*/\s*(\d+)")


def infer(v: str):
    """Prose -> (argv, heavy, expect) using only ALIASES, test paths and 'pytest -k expr'; else None."""
    m = re.search(r"\bpytest\s+-k\s+(?:\"([^\"]+)\"|'([^']+)'|(\S+))", v)
    if m:
        return [PY, "-m", "pytest", "-q", "-k", m.group(1) or m.group(2) or m.group(3)], False, None
    paths = list(dict.fromkeys(NODE.findall(v)))
    if paths:
        return [PY, "-m", "pytest", "-q", *paths], False, None
    for key, (argv, heavy) in ALIASES.items():
        if re.search(rf"\b{key}\b", v):
            e = EXPECT.search(v)
            return list(argv), heavy, (int(e.group(2)), int(e.group(3))) if e and e.group(1) == key else None
    return None


def classify(row: dict) -> dict:
    """Adds kind = manual | automatable | unsafe | skip (not done), and argv for automatable rows."""
    row = dict(row)
    if not is_done(row["status"]):
        row["kind"] = "skip"
        return row
    v = row["verify"]
    spans = re.findall(r"`([^`]+)`", v)
    cmd = None
    if spans:
        cmd = spans[0].strip()
    if not cmd:
        inf = infer(v)
        if not inf:
            row["kind"] = "manual"
            return row
        row["kind"], row["argv"], row["heavy"], row["expect"], row["inferred"] = "automatable", *inf, True
        return row
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = []
    argv = None
    if parts[:1] in (["pytest"], [".venv/bin/pytest"]):
        argv = [PY, "-m", "pytest", "-q", *parts[1:]]
    elif parts[:3] in (["python", "-m", "pytest"], ["python3", "-m", "pytest"], [PY, "-m", "pytest"]):
        argv = [PY, "-m", "pytest", "-q", *parts[3:]]
    elif len(parts) >= 2 and parts[0] == PY and parts[1].startswith("tools/") and not parts[1].startswith("tools/.."):
        argv = parts
    if argv and not any(re.search(r"[;&|<>`$]", p) for p in argv):
        row["kind"], row["argv"] = "automatable", argv
    else:
        row["kind"], row["cmd"] = "unsafe", cmd
    return row


def run_row(row: dict, timeout: int) -> dict:
    if row.get("heavy") and not row.get("include_heavy"):
        return {**row, "result": "skipped", "secs": 0, "tail": "heavy: needs --include-heavy"}
    t0 = time.time()
    try:
        p = subprocess.run(LOCK + row["argv"], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + p.stderr).strip().splitlines()
        tail = out[-1][:160] if out else ""
        if row.get("expect"):
            ok = sum(l.startswith("PASS") for l in out)
            tot = ok + sum(l.startswith("FAIL") for l in out)
            if tot:
                tail = f"got {ok}/{tot}, expected {row['expect'][0]}/{row['expect'][1]}; " + tail
        if p.returncode == 0:
            status = "skipped" if re.search(r"\b\d+ skipped\b", tail) and "passed" not in tail else "pass"
        elif p.returncode == 5:
            status = "skipped"
        elif p.returncode == 1:
            status = "fail"
        else:
            status = "error"
    except subprocess.TimeoutExpired:
        status, tail = "error", f"timeout after {timeout}s"
    except OSError as e:
        status, tail = "error", str(e)[:160]
    return {**row, "result": status, "secs": round(time.time() - t0, 1), "tail": tail}


def git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    except OSError:
        return "?"


def write_report(results: list[dict], path: Path = REPORT) -> None:
    counts: dict[str, int] = {}
    for r in results:
        k = r.get("result") or r["kind"]
        counts[k] = counts.get(k, 0) + 1
    try:
        load = ", ".join(f"{x:.2f}" for x in os.getloadavg())
    except OSError:
        load = "n/a"
    lines = ["# Leftovers verification", "", f"- When: {datetime.now().isoformat(timespec='seconds')}",
             f"- Commit: {git_head()}", f"- Load average (1/5/15 min): {load}",
             "- Summary: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), "",
             "| ID | Result | Secs | Command | Output tail |", "|---|---|---|---|---|"]
    lines[-2:] = ["| ID | Result | Inferred | Heavy | Secs | Command | Output tail |", "|---|---|---|---|---|---|---|"]
    for r in results:
        if r["kind"] == "skip":
            continue
        res = r.get("result") or {"manual": "manual", "unsafe": "unsafe: skipped", "automatable": "not run"}[r["kind"]]
        cmd = " ".join(r.get("argv", [])) or r.get("cmd", "")
        lines.append(f"| {r['id']} | {res} | {'inferred' if r.get('inferred') else ''} | {'heavy' if r.get('heavy') else ''} | {r.get('secs', '')} | `{cmd}` | {r.get('tail', '').replace('|', '/')} |")
    path.write_text("\n".join(lines) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--include-heavy", action="store_true")
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args(argv)
    rows = [classify(r) for r in parse(LEFTOVERS.read_text())]
    if a.only:
        want = {x.strip().upper() for x in a.only.split(",") if x.strip()}
        rows = [r for r in rows if r["id"] in want]
    for r in rows:
        r["include_heavy"] = a.include_heavy
    n = {k: sum(r["kind"] == k for r in rows) for k in ("automatable", "manual", "unsafe", "skip")}
    n["inferred"] = sum(bool(r.get("inferred")) for r in rows)
    n["heavy"] = sum(bool(r.get("heavy")) for r in rows)
    print(f"rows parsed {len(rows)}: automatable {n['automatable']} (inferred {n['inferred']}, heavy {n['heavy']}), manual {n['manual']}, "
          f"unsafe {n['unsafe']}, not-done {n['skip']}")
    if a.list or a.dry_run:
        for r in rows:
            if r["kind"] != "skip":
                print(f"{r['id']:5} {r['kind']:11} {'inferred ' if r.get('inferred') else ''}{'heavy ' if r.get('heavy') else ''}{' '.join(r.get('argv', [])) or r.get('cmd', '')}")
        return 0
    results = [run_row(r, a.timeout) if r["kind"] == "automatable" else r for r in rows]
    write_report(results)
    bad = [r["id"] for r in results if r.get("result") in ("fail", "error")]
    print("report:", REPORT.relative_to(ROOT), "| failing:", ", ".join(bad) or "none")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
