"""Regression guard (WIN-OOM): pywebview walks every PUBLIC non-callable attribute of js_api. A public
Api.window led it into window.native (WinForms, fresh objects per getter) until memory ran out."""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app_main  # noqa: E402


class _Endless:
    """Every public attribute access returns a NEW object, like pythonnet property getters."""
    __module__ = "fake"

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Endless()

    def __dir__(self):
        return ["AccessibilityObject", "Bounds", "Empty", "DefaultFont"]


def _walk(obj, base="", functions=None, depth=0):
    """pywebview 6.2.1 util.get_functions (same rules), plus a depth cap so a runaway fails instead of hanging."""
    assert depth < 25, f"runaway attribute walk at {base}"
    functions = {} if functions is None else functions
    for name in dir(obj):
        if name.startswith("_"):
            continue
        full = f"{base}.{name}" if base else name
        attr = getattr(obj, name)
        if inspect.ismethod(attr) or inspect.isfunction(attr):
            functions[full] = 1
        elif inspect.isclass(attr) or (not callable(attr) and hasattr(attr, "__module__")):
            _walk(attr, full, functions, depth + 1)
    return functions


def test_api_exposes_only_its_methods_and_walk_terminates():
    api = app_main.Api()
    api._window = type("W", (), {"native": _Endless(), "__module__": "fake"})()
    got = set(_walk(api))
    want = {n for n, m in inspect.getmembers(app_main.Api, inspect.isfunction) if not n.startswith("_")}
    assert got == want and got


def test_api_has_no_public_non_callable_attributes():
    api = app_main.Api()
    bad = [n for n in dir(api) if not n.startswith("_") and not callable(getattr(api, n))]
    assert bad == []


def test_guard_would_catch_the_old_bug():
    api = app_main.Api()
    api.window = type("W", (), {"native": _Endless(), "__module__": "fake"})()   # the old public attribute
    try:
        _walk(api)
    except AssertionError:
        return
    raise AssertionError("walker did not detect the runaway")
