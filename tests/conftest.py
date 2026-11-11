import os

# Analysis runs inline in tests unless a test opts in (tests/test_server_workers.py sets AMV_WORKERS itself):
# a cold worker-process spawn would make unrelated API tests slow and timing-sensitive.
os.environ.setdefault("AMV_WORKERS", "0")


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: load-sensitive tests (not deselected by default)")


def pytest_collection_modifyitems(config, items):
    """A node-based JS test must never be silently skipped: fail loudly when node cannot be found."""
    import shutil
    from pathlib import Path

    if shutil.which("node") or Path.home().joinpath(".local/node/bin/node").exists():
        return
    import pytest

    for it in items:
        for m in it.iter_markers("skipif"):
            if "node" in str(m.kwargs.get("reason", "")).lower():
                it.add_marker(pytest.mark.xfail(False, run=False))  # keep marker order stable
                it.own_markers = [x for x in it.own_markers if x is not m]
                it.add_marker(pytest.mark.usefixtures("_node_required"))


import pytest as _pytest


@_pytest.fixture
def _node_required():
    _pytest.fail("node not found (PATH or ~/.local/node/bin/node): JS tests must not be skipped")
