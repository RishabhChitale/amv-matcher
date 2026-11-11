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


_CS = r"""
using System.Drawing;
using System.Windows.Forms;
public class AmvDarkTable : ProfessionalColorTable {
  Color bg, hi, pr, bd;
  public AmvDarkTable(Color bg, Color hi, Color pr, Color bd) { this.bg = bg; this.hi = hi; this.pr = pr; this.bd = bd; UseSystemColors = false; }
  public override Color MenuStripGradientBegin { get { return bg; } }
  public override Color MenuStripGradientEnd { get { return bg; } }
  public override Color ToolStripDropDownBackground { get { return bg; } }
  public override Color ImageMarginGradientBegin { get { return bg; } }
  public override Color ImageMarginGradientMiddle { get { return bg; } }
  public override Color ImageMarginGradientEnd { get { return bg; } }
  public override Color MenuItemSelected { get { return hi; } }
  public override Color MenuItemSelectedGradientBegin { get { return hi; } }
  public override Color MenuItemSelectedGradientEnd { get { return hi; } }
  public override Color MenuItemPressedGradientBegin { get { return pr; } }
  public override Color MenuItemPressedGradientMiddle { get { return pr; } }
  public override Color MenuItemPressedGradientEnd { get { return pr; } }
  public override Color MenuItemBorder { get { return bd; } }
  public override Color MenuBorder { get { return bd; } }
  public override Color ToolStripBorder { get { return bd; } }
  public override Color SeparatorDark { get { return bd; } }
  public override Color SeparatorLight { get { return bd; } }
  public override Color CheckBackground { get { return hi; } }
  public override Color CheckSelectedBackground { get { return hi; } }
  public override Color CheckPressedBackground { get { return pr; } }
}
public class AmvDarkRenderer : ToolStripProfessionalRenderer {
  Color fg, dim;
  public AmvDarkRenderer(ProfessionalColorTable t, Color fg, Color dim) : base(t) { this.fg = fg; this.dim = dim; RoundedEdges = false; }
  protected override void OnRenderItemText(ToolStripItemTextRenderEventArgs e) { e.TextColor = e.Item.Enabled ? fg : dim; base.OnRenderItemText(e); }
  protected override void OnRenderArrow(ToolStripArrowRenderEventArgs e) { e.ArrowColor = fg; base.OnRenderArrow(e); }
}
"""
_renderer_cache = {}


def _make_renderer():
    """Compile (once) a dark ProfessionalColorTable + renderer with the in-box C# compiler; pythonnet can't override
    virtual .NET members from Python. Raises on failure (caller logs)."""
    if "r" in _renderer_cache:
        return _renderer_cache["r"]
    import clr
    clr.AddReference("System")
    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")
    from System import Activator, Array, Object
    from System.Drawing import Color
    from Microsoft.CSharp import CSharpCodeProvider
    from System.CodeDom.Compiler import CompilerParameters
    cp = CompilerParameters()
    cp.GenerateInMemory = True
    for a in ("System.dll", "System.Windows.Forms.dll", "System.Drawing.dll"):
        cp.ReferencedAssemblies.Add(a)
    res = CSharpCodeProvider().CompileAssemblyFromSource(cp, _CS)
    if res.Errors.HasErrors:
        raise RuntimeError("C# compile: " + "; ".join(str(e) for e in res.Errors))
    asm = res.CompiledAssembly
    c = lambda rgb: Color.FromArgb(*rgb)
    table = Activator.CreateInstance(asm.GetType("AmvDarkTable"),
                                     Array[Object]([c(DARK_BG), c((58, 60, 70)), c((72, 75, 88)), c((70, 72, 82))]))
    r = Activator.CreateInstance(asm.GetType("AmvDarkRenderer"), Array[Object]([table, c(DARK_FG), c((130, 133, 142))]))
    _renderer_cache["r"] = r
    return r


def _find_strip(form):
    s = getattr(form, "MainMenuStrip", None) or getattr(form, "menu_strip", None)
    if s is not None:
        return s
    for c in form.Controls:
        if c.GetType().Name == "MenuStrip":
            return c
    return None


def _paint_items(items, bg, fg, renderer, mode) -> int:
    n = 0
    for it in items:
        n += 1
        it.BackColor, it.ForeColor = bg, fg
        t = getattr(it, "Text", "") or ""
        if "\t" in t and hasattr(it, "ShortcutKeyDisplayString"):   # 'New Project<tab>Ctrl+N' -> aligned shortcut column
            it.Text, it.ShortcutKeyDisplayString = t.split("\t", 1)
            it.ShowShortcutKeys = True
        dd = getattr(it, "DropDown", None) if getattr(it, "HasDropDownItems", False) else None
        if dd is not None:
            dd.BackColor, dd.ForeColor = bg, fg
            if renderer is not None:
                dd.Renderer = renderer
            else:
                dd.RenderMode = mode
            n += _paint_items(list(it.DropDownItems), bg, fg, renderer, mode)
    return n


def set_menu(form, dark: bool) -> int:
    """Colour the WinForms MenuStrip, its drop-downs and hover/pressed states dark (custom renderer), or restore the
    system look. Returns the number of items painted; raises on failure (apply() logs it)."""
    from System.Drawing import Color, SystemColors
    from System.Windows.Forms import ToolStripRenderMode
    strip = _find_strip(form)
    if strip is None:
        raise RuntimeError("no MenuStrip on the form")
    if dark:
        bg, fg, r = Color.FromArgb(*DARK_BG), Color.FromArgb(*DARK_FG), _make_renderer()
        strip.Renderer = r
        mode = None
    else:
        bg, fg, r, mode = SystemColors.Control, SystemColors.ControlText, None, ToolStripRenderMode.ManagerRenderMode
        strip.RenderMode = mode
    strip.BackColor, strip.ForeColor = bg, fg
    n = _paint_items(list(strip.Items), bg, fg, r, mode)
    strip.Invalidate(True)
    return n


def apply(form, dark: bool) -> None:
    try:
        LOG.info("windark: title bar dark=%s ok=%s", dark, set_title_bar(int(form.Handle.ToInt64()), dark))
    except Exception:
        LOG.exception("windark: title bar FAILED dark=%s", dark)
    try:
        LOG.info("windark: menu recolour ok dark=%s items=%d", dark, set_menu(form, dark))
    except Exception:
        LOG.exception("windark: menu recolour FAILED dark=%s", dark)


def _signature(form):
    s = _find_strip(form)
    if s is None:
        return None
    return (s.GetHashCode(), s.Items.Count, tuple(i.GetHashCode() for i in s.Items))


def open_menu(win, index=0) -> bool:
    """Dev/CI aid: show drop-down <index> of the menu bar so a screenshot can check its colours."""
    if sys.platform != "win32":
        return False
    from webview.platforms.winforms import BrowserView
    from System import Action
    bv = BrowserView.instances.get(win.uid)
    if bv is None:
        return False
    ok = []

    def go():
        try:
            _find_strip(bv).Items[index].ShowDropDown()
            ok.append(1)
        except Exception:
            LOG.exception("windark: open_menu FAILED")
    bv.Invoke(Action(go))
    LOG.info("windark: open_menu index=%d ok=%s", index, bool(ok))
    return bool(ok)


def follow_system(win, poll=2.0) -> None:
    """Apply now and re-apply whenever the system theme flips. win is the pywebview Window; Windows only."""
    if sys.platform != "win32":
        return

    def run():
        from webview.platforms.winforms import BrowserView
        from System import Action
        last = lastsig = None
        stop = threading.Event()
        while not stop.wait(poll):
            try:
                dark = system_is_dark()
                bv = BrowserView.instances.get(win.uid)
                if bv is None:
                    continue
                sig = []
                bv.Invoke(Action(lambda: sig.append(_signature(bv))))
                if dark != last or (sig and sig[0] != lastsig):
                    bv.Invoke(Action(lambda: apply(bv, dark)))
                    last = dark
                    sig2 = []
                    bv.Invoke(Action(lambda: sig2.append(_signature(bv))))
                    lastsig = sig2[0] if sig2 else None
            except Exception:
                LOG.exception("follow_system")
    threading.Thread(target=run, daemon=True, name="windark").start()
