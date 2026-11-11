"""Windows dark theme for the native chrome (title bar + menu bar). No-op off Windows; every call is best-effort."""
import logging
import sys
import threading

LOG = logging.getLogger("amv.windark")
DARK_BG, DARK_FG = (32, 33, 38), (230, 232, 238)


def system_is_dark() -> bool:
    """True when Windows apps theme is dark (HKCU ...\\Personalize AppsUseLightTheme == 0)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


def set_title_bar(hwnd: int, dark: bool) -> bool:
    """DwmSetWindowAttribute(DWMWA_USE_IMMERSIVE_DARK_MODE); 20 on current Windows, 19 on early Win10 builds."""
    if sys.platform != "win32":
        return False
    import ctypes
    val = ctypes.c_int(1 if dark else 0)
    for attr in (20, 19):
        try:
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr, ctypes.byref(val), 4) == 0:
                return True
        except Exception:
            LOG.exception("DwmSetWindowAttribute")
            return False
    return False


def _paint_items(items, bg, fg):
    for it in items:
        try:
            it.BackColor, it.ForeColor = bg, fg
            if getattr(it, "DropDownItems", None) is not None:
                it.DropDown.BackColor, it.DropDown.ForeColor = bg, fg
                _paint_items(list(it.DropDownItems), bg, fg)
        except Exception:
            pass


def set_menu(form, dark: bool) -> None:
    """Colour the WinForms MenuStrip (and its drop-downs) dark, or back to system colours."""
    from System.Drawing import Color, SystemColors
    strip = getattr(form, "MainMenuStrip", None)
    if strip is None:
        return
    bg = Color.FromArgb(*DARK_BG) if dark else SystemColors.Control
    fg = Color.FromArgb(*DARK_FG) if dark else SystemColors.ControlText
    strip.BackColor, strip.ForeColor = bg, fg
    _paint_items(list(strip.Items), bg, fg)


def apply(form, dark: bool) -> None:
    try:
        set_title_bar(int(form.Handle.ToInt64()), dark)
        set_menu(form, dark)
    except Exception:
        LOG.exception("apply dark=%s", dark)


def follow_system(win, poll=2.0) -> None:
    """Apply now and re-apply whenever the system theme flips. win is the pywebview Window; Windows only."""
    if sys.platform != "win32":
        return

    def run():
        from webview.platforms.winforms import BrowserView
        from System import Action
        last = None
        stop = threading.Event()
        while not stop.wait(poll):
            try:
                dark = system_is_dark()
                bv = BrowserView.instances.get(win.uid)
                if bv is None:
                    continue
                if dark != last:
                    bv.Invoke(Action(lambda: apply(bv, dark)))
                    last = dark
            except Exception:
                LOG.exception("follow_system")
    threading.Thread(target=run, daemon=True, name="windark").start()
