"""Generate assets/icon.ico (16-256 px) and assets/icon_<n>.png from assets/icon.png. Needs Pillow (dev only).
Usage: python tools/make_icons.py"""
from pathlib import Path

from PIL import Image

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SIZES = [16, 24, 32, 48, 64, 128, 256]
ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def main():
    src = Image.open(ASSETS / "icon.png").convert("RGBA")
    src.save(ASSETS / "icon.ico", sizes=[(s, s) for s in SIZES])
    out = ASSETS / "iconset"
    out.mkdir(exist_ok=True)
    for s in ICNS_SIZES:
        src.resize((s, s), Image.LANCZOS).save(out / f"icon_{s}.png")
    print("wrote", ASSETS / "icon.ico", "and", out)


if __name__ == "__main__":
    main()
