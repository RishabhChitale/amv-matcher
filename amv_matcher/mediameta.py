"""Download-source metadata (read-only): where a file was downloaded from."""
import configparser
import ctypes
import ctypes.util
import os
import plistlib
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

_WHEREFROMS = "com.apple.metadata:kMDItemWhereFroms"
_QUARANTINE = "com.apple.quarantine"


def _getxattr(path, name):
    """Return xattr bytes or None. Never raises."""
    try:
        if hasattr(os, "getxattr"):
            return os.getxattr(path, name)
    except Exception:
        return None
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
        fn = libc.getxattr
        fn.restype = ctypes.c_ssize_t
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p,
                       ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
        p, n = os.fsencode(path), name.encode()
        size = fn(p, n, None, 0, 0, 0)
        if size < 0:
            return None
        buf = ctypes.create_string_buffer(size)
        got = fn(p, n, buf, size, 0, 0)
        return buf.raw[:got] if got >= 0 else None
    except Exception:
        pass
    try:
        out = subprocess.run(["xattr", "-px", name, str(path)], capture_output=True,
                             text=True, timeout=5)
        if out.returncode == 0:
            return bytes.fromhex("".join(out.stdout.split()))
    except Exception:
        pass
    return None


def _domain(url):
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _mac(path):
    urls, agent, when = [], None, None
    raw = _getxattr(path, _WHEREFROMS)
    if raw:
        try:
            val = plistlib.loads(raw)
            if isinstance(val, list):
                urls = [u for u in val if isinstance(u, str)]
            elif isinstance(val, str):
                urls = [val]
        except Exception:
            urls = []
    q = _getxattr(path, _QUARANTINE)
    if q:
        try:
            parts = q.decode("utf-8", "replace").strip().split(";")
            if len(parts) > 2 and parts[2]:
                agent = parts[2]
            if len(parts) > 1 and parts[1]:
                ts = int(parts[1], 16)
                when = datetime.fromtimestamp(ts, timezone.utc).isoformat()
        except Exception:
            pass
    if not urls and not agent and not when:
        return None
    return {"source_url": urls[0] if urls else None,
            "referrer": urls[1] if len(urls) > 1 else None,
            "downloaded_by": agent, "downloaded_at": when}


def _win(path):
    try:
        with open(str(path) + ":Zone.Identifier", "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.optionxform = str
        cp.read_string(text)
        sec = cp["ZoneTransfer"]
        url, ref = sec.get("HostUrl"), sec.get("ReferrerUrl")
    except Exception:
        return None
    if not url and not ref:
        return None
    return {"source_url": url, "referrer": ref, "downloaded_by": None, "downloaded_at": None}


def download_source(path):
    """{source_url, referrer, downloaded_by, downloaded_at, domain} or None. Never raises."""
    try:
        res = _win(path) if sys.platform.startswith("win") else (
            _mac(path) if sys.platform == "darwin" else None)
        if not res:
            return None
        res["domain"] = _domain(res["source_url"]) if res["source_url"] else None
        return res
    except Exception:
        return None
