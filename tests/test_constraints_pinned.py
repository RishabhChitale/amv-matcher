import re
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _lines(name):
    return [l.strip() for l in (ROOT / name).read_text().splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


def _norm(n):
    return re.sub(r"[-_.]+", "-", n).lower()


@pytest.mark.parametrize("name", ["constraints-macos.txt", "constraints-stems-macos.txt"])
def test_all_pinned(name):
    for l in _lines(name):
        assert re.match(r"^[A-Za-z0-9._-]+(\[[^\]]+\])?==[^=\s]+$", l), l


def test_installed_matches_main_pins():
    for l in _lines("constraints-macos.txt"):
        pkg, ver = l.split("==")
        try:
            have = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            continue
        assert have == ver, f"{pkg}: installed {have} != pinned {ver}"


def test_requirements_covered():
    pins = {_norm(l.split("==")[0]) for l in _lines("constraints-macos.txt")}
    for l in _lines("requirements.txt"):
        if l.startswith("-"):
            continue
        name = re.split(r"[\[<>=!~; ]", l)[0]
        assert _norm(name) in pins, f"{name} missing from constraints-macos.txt"
