# PyInstaller spec: one-folder, windowed desktop app.  Build: pyinstaller --noconfirm amv_matcher.spec
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

NAME = "AMV Matcher"
datas = [("static", "static"),
         ("amv_matcher/_demucs_runner.py", "amv_matcher")]   # run by path in the optional stems-pack venv
binaries = []
hidden = ["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
          "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on", "platformdirs",
          "multipart", "python_multipart", "fractions", "zipfile", "concurrent.futures"]
for pkg in ("librosa", "scipy", "av", "cv2", "soundfile", "webview", "numba", "sklearn", "resampy"):
    try:
        datas += collect_data_files(pkg)
        binaries += collect_dynamic_libs(pkg)
        hidden += collect_submodules(pkg)
    except Exception as e:  # optional package absent
        print("spec: skipping", pkg, e)
hidden += collect_submodules("amv_matcher")

a = Analysis(["app_main.py"], pathex=["."], binaries=binaries, datas=datas, hiddenimports=hidden,
             excludes=["torch", "torchaudio", "demucs", "tkinter", "matplotlib", "IPython", "pytest"],
             noarchive=False)
pyz = PYZ(a.pure)
icon = "assets/icon.icns" if sys.platform == "darwin" else "assets/icon.ico"
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, console=False, icon=icon)
coll = COLLECT(exe, a.binaries, a.datas, name=NAME)
if sys.platform == "darwin":
    app = BUNDLE(coll, name=NAME + ".app", icon="assets/icon.icns",
                 bundle_identifier="local.amv-matcher",
                 info_plist={"NSHighResolutionCapable": True, "CFBundleShortVersionString": "0.1.0"})
