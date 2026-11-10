"""Kind module whose import is slow: simulates a cold worker (slow spawn/AV scan)."""
import os
import time

time.sleep(float(os.environ.get("AMV_TEST_SLOW_START", "0")))
