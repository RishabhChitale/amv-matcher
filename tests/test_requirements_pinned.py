import re
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted(ROOT.glob("requirements*.txt"))
LINE = re.compile(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*==\s*([A-Za-z0-9_.!+\-]+)\s*(;[^#]*)?(#.*)?$")


def entries(path):
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-r", "-e", "-c")):
            continue
        yield line


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_requirement_pinned(path):
    bad = [l for l in entries(path) if not LINE.match(l)]
    assert not bad, f"unpinned in {path.name}: {bad}"


def test_installed_matches_pins():
    mismatches = []
    for line in entries(ROOT / "requirements.txt"):
        m = LINE.match(line)
        try:
            have = metadata.version(m.group(1))
        except metadata.PackageNotFoundError:
            continue  # not installed here: skip
        if have != m.group(3):
            mismatches.append(f"{m.group(1)}: pinned {m.group(3)}, installed {have}")
    assert not mismatches, mismatches
