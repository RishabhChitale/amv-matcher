import builtins
import plistlib
import subprocess
import sys

import pytest

from amv_matcher import mediameta

mac = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")


def _setx(p, name, data):
    subprocess.run(["xattr", "-wx", name, data.hex(), str(p)], check=True)


@mac
def test_mac_parse(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    _setx(f, "com.apple.metadata:kMDItemWhereFroms",
          plistlib.dumps(["https://www.youtube.com/watch?v=1", "https://ref.example/"],
                         fmt=plistlib.FMT_BINARY))
    _setx(f, "com.apple.quarantine", b"0083;65000000;Google Chrome;ABCD")
    r = mediameta.download_source(str(f))
    assert r["source_url"] == "https://www.youtube.com/watch?v=1"
    assert r["referrer"] == "https://ref.example/"
    assert r["downloaded_by"] == "Google Chrome"
    assert r["downloaded_at"].startswith("2023-09-12T06:06:56")
    assert r["domain"] == "youtube.com"


@mac
def test_mac_none_and_malformed(tmp_path):
    f = tmp_path / "b.mp4"
    f.write_bytes(b"x")
    assert mediameta.download_source(str(f)) is None
    _setx(f, "com.apple.metadata:kMDItemWhereFroms", b"garbage")
    _setx(f, "com.apple.quarantine", b"junk")
    assert mediameta.download_source(str(f)) is None
    assert mediameta.download_source(str(tmp_path / "missing")) is None


def test_windows_parse(monkeypatch, tmp_path):
    import io
    monkeypatch.setattr(sys, "platform", "win32")
    real = builtins.open

    def fake(p, *a, **k):
        if str(p).endswith(":Zone.Identifier"):
            return io.StringIO("[ZoneTransfer]\nZoneId=3\nReferrerUrl=https://r.com/\n"
                               "HostUrl=https://www.site.org/v.mp4\n")
        return real(p, *a, **k)
    monkeypatch.setattr(builtins, "open", fake)
    r = mediameta.download_source(str(tmp_path / "v.mp4"))
    assert r == {"source_url": "https://www.site.org/v.mp4", "referrer": "https://r.com/",
                 "downloaded_by": None, "downloaded_at": None, "domain": "site.org"}


def test_windows_malformed(monkeypatch, tmp_path):
    import io
    monkeypatch.setattr(sys, "platform", "win32")
    real = builtins.open
    monkeypatch.setattr(builtins, "open", lambda p, *a, **k: io.StringIO("\x00not ini")
                        if str(p).endswith(":Zone.Identifier") else real(p, *a, **k))
    assert mediameta.download_source(str(tmp_path / "v.mp4")) is None
