import os

# Analysis runs inline in tests unless a test opts in (tests/test_server_workers.py sets AMV_WORKERS itself):
# a cold worker-process spawn would make unrelated API tests slow and timing-sensitive.
os.environ.setdefault("AMV_WORKERS", "0")
