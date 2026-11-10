"""Windows semantics of media.py / proxy.py (W-06, W-09, W-15), simulated."""
import os
from pathlib import Path

import pytest

from amv_matcher import media, proxy, winsafe


def test_replace_retries_permission_error(monkeypatch, tmp_path):
    n = {"c": 0}
    real = os.replace

    def flaky(a, b):
        n["c"] += 1
        if n["c"] < 3:
            raise PermissionError("locked")
        return real(a, b)
    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(winsafe.time, "sleep", lambda s: None)
    src, dst = tmp_path / "a.tmp.mp4", tmp_path / "a.mp4"
    src.write_bytes(b"x")
    winsafe.replace_retry(src, dst)
    assert dst.exists() and n["c"] == 3


def test_proxy_encode_replace_retried_and_tmp_cleaned(monkeypatch, tmp_path):
    vp = tmp_path / "v.mkv"
    vp.write_bytes(b"v")
    monkeypatch.setattr(proxy.paths, "CACHE", tmp_path / "cache", raising=False)
    monkeypatch.setattr(proxy, "_encode", lambda v, t: Path(t).write_bytes(b"p"))
    calls = []
    monkeypatch.setattr(winsafe, "replace_retry", lambda a, b, *k, **kw: (calls.append((a, b)), os.replace(a, b)))
    out = proxy.build(vp)
    assert calls and out.is_file() and not list(out.parent.glob("*.tmp.mp4"))


@pytest.mark.parametrize("win", [True, False])
def test_cache_keys_case_insensitive_on_windows(monkeypatch, tmp_path, win):
    monkeypatch.setattr(os.path, "normcase", (lambda p: p.lower()) if win else (lambda p: p))
    if not win:
        # real Windows would otherwise canonicalise case via GetLongPathName; force the POSIX path
        monkeypatch.setattr(winsafe, "_is_win", lambda: False)
    f = tmp_path / "Clip.MP4"
    f.write_bytes(b"x")
    low = Path(str(f).lower())
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: self)
    k1, k2 = proxy._key(f), None
    # same stat for both spellings: patch stat via a copy of the same file content
    import shutil
    g = tmp_path / "clip.mp4"
    if not g.exists():
        shutil.copy2(f, g)
    os.utime(g, (f.stat().st_atime, f.stat().st_mtime))
    k2 = proxy._key(g)
    assert (k1 == k2) == win
    assert (media._extract_key(f) == media._extract_key(g)) == win
