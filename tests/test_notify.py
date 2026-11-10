import base64
import subprocess
import sys
import types

import pytest

from amv_matcher import nativeshell as ns


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    ns._last_notify.clear()


def fake_mod(monkeypatch, name, cls, calls):
    m = types.ModuleType(name)
    setattr(m, cls, lambda *a, **k: types.SimpleNamespace(show=lambda *a2, **k2: calls.append(name),
                                                         show_toast=lambda *a2, **k2: calls.append(name)))
    monkeypatch.setitem(sys.modules, name, m)


def block(monkeypatch, name):
    monkeypatch.setitem(sys.modules, name, None)  # import raises ImportError


def test_order_winotify_first(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    fake_mod(monkeypatch, "winotify", "Notification", calls)
    fake_mod(monkeypatch, "win10toast", "ToastNotifier", calls)
    assert ns.notify("t", "b") is True and calls == ["winotify"]


def test_order_win10toast_second(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    block(monkeypatch, "winotify")
    fake_mod(monkeypatch, "win10toast", "ToastNotifier", calls)
    assert ns.notify("t", "b") is True and calls == ["win10toast"]


def test_powershell_fallback(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    block(monkeypatch, "winotify")
    block(monkeypatch, "win10toast")
    seen = []
    monkeypatch.setattr(ns.winsafe, "run", lambda cmd, **kw: seen.append((cmd, kw)) or types.SimpleNamespace(returncode=0))
    assert ns.notify("t", "b") is True
    cmd, kw = seen[0]
    assert cmd[0] == "powershell.exe" and "-NoProfile" in cmd and "-WindowStyle" in cmd and "-EncodedCommand" in cmd
    assert kw["timeout"] and kw["stdin"] == subprocess.DEVNULL


def test_encoded_command_roundtrip():
    title = 'He said "hi" & <b> it\'s é日本'
    cmd = ns.powershell_cmd(title, "x")
    script = base64.b64decode(cmd[cmd.index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert "He said &quot;hi&quot; &amp; &lt;b&gt; it&apos;s é日本".replace("&apos;", "&apos;") in script
    assert "<b>" not in script.split("LoadXml")[1].split("<text>")[1]


def test_exceptions_swallowed(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    block(monkeypatch, "winotify")
    block(monkeypatch, "win10toast")
    def boom(*a, **k):
        raise OSError("no powershell")
    monkeypatch.setattr(ns.winsafe, "run", boom)
    assert ns.notify("t", "b") is False
    monkeypatch.setattr(ns, "notify_cmd", boom)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert ns.notify("t2", "b") is False


def test_rate_limit(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    fake_mod(monkeypatch, "winotify", "Notification", calls)
    assert ns.notify("t", "b") is True
    assert ns.notify("t", "b") is False
    assert ns.notify("t", "other") is True
    assert len(calls) == 2


def test_macos_unchanged(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    ran = []
    monkeypatch.setattr(ns, "run", lambda cmd: ran.append(cmd) or True)
    assert ns.notify("Done", "ok") is True
    assert ran == [["osascript", "-e", 'display notification "ok" with title "Done"']]


def test_linux_noop(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ns, "run", lambda cmd: pytest.fail("must not run"))
    assert ns.notify("t", "b") is False
