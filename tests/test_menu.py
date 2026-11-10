import re
import sys
from pathlib import Path

import app_main
from app_main import MENU_ACCEL, menu_label

SHELL = Path(__file__).resolve().parent.parent / "static" / "shell.js"


def test_menu_label_mac(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert menu_label("Save", ("S", False)) == "Save\t⌘S"
    assert menu_label("Save As…", ("S", True)) == "Save As…\t⇧⌘S"
    assert menu_label("Help") == "Help"


def test_menu_label_win(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert menu_label("Open Project…", ("O", False)) == "Open Project…\tCtrl+O"
    assert menu_label("Redo", ("Z", True)) == "Redo\tCtrl+Shift+Z"


def shell_bindings():
    """Parse shell.js keydown branches -> {action: (KEY, shift)}."""
    out = {}  # action -> set of (KEY, shift)
    for line in SHELL.read_text().splitlines():
        m = re.search(r"k === '(\w)'", line)
        if not m:
            continue
        key = m.group(1).upper()
        t = re.search(r"e\.shiftKey \? '(\w+)' : '(\w+)'", line)
        if t:
            out.setdefault(t.group(1), set()).add((key, True))
            out.setdefault(t.group(2), set()).add((key, False))
        else:
            out.setdefault(re.search(r"a = '(\w+)'", line).group(1), set()).add((key, False))
    return out


def test_every_hint_has_a_binding():
    b = shell_bindings()
    for action, accel in MENU_ACCEL.items():
        assert accel in b.get(action, set()), action


def test_menu_uses_table(monkeypatch):
    import types
    labels = []
    mod = types.ModuleType("webview.menu")
    mod.Menu = lambda name, items: items
    mod.MenuAction = lambda label, fn: labels.append(label) or label
    mod.MenuSeparator = lambda: None
    monkeypatch.setitem(sys.modules, "webview", types.ModuleType("webview"))
    monkeypatch.setitem(sys.modules, "webview.menu", mod)
    monkeypatch.setattr(sys, "platform", "win32")
    app_main.build_menu(lambda a: None)
    assert "Save\tCtrl+S" in labels and "Help" in labels and "Play / Pause" in labels
