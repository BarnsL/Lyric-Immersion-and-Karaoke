"""Generate the MSIX / Microsoft Store logo assets from the app's own branded
icon (the purple karaoke mic drawn by make_icon.py), so the tiles, taskbar
icon, and Store listing all match icon.ico.

    python make_assets.py <out_dir>      # default: ./Assets

Everything is rendered from the 1024px master and downscaled with LANCZOS for
crisp edges (the same approach make_icon.py uses for icon.ico). Tiny sizes use
make_icon's chunkier "simplified" master so the mic stays legible.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # find make_icon
from PIL import Image
import make_icon


def _save(img, out_dir, name):
    img.save(os.path.join(out_dir, name))


def render(out_dir="Assets"):
    os.makedirs(out_dir, exist_ok=True)
    S = make_icon.S
    full = make_icon._compose(S, simplified=False)
    small = make_icon._compose(S, simplified=True)   # bolder mic for tiny sizes

    def square(name, size):
        src = small if size <= 32 else full
        _save(src.resize((size, size), Image.LANCZOS), out_dir, name)

    # Base (scale-100) tiles + Store logo, each with a scale-200 for high-DPI.
    squares = {
        "Square44x44Logo.png": 44,   "Square44x44Logo.scale-200.png": 88,
        "Square71x71Logo.png": 71,   "Square71x71Logo.scale-200.png": 142,
        "Square150x150Logo.png": 150, "Square150x150Logo.scale-200.png": 300,
        "Square310x310Logo.png": 310, "Square310x310Logo.scale-200.png": 620,
        "StoreLogo.png": 50,         "StoreLogo.scale-200.png": 100,
    }
    for name, size in squares.items():
        square(name, size)

    # Taskbar / Start "target size" icons (plated + unplated), recommended set.
    for ts in (16, 24, 32, 48, 256):
        src = small if ts <= 32 else full
        ic = src.resize((ts, ts), Image.LANCZOS)
        _save(ic, out_dir, f"Square44x44Logo.targetsize-{ts}.png")
        _save(ic, out_dir, f"Square44x44Logo.targetsize-{ts}_altform-unplated.png")

    # Wide tile: the mic centred on the brand purple gradient.
    def wide(name, w, h, icon_frac=0.80):
        canvas = (make_icon._vgradient(max(w, h), make_icon.TOP, make_icon.BOT)
                  .convert("RGBA").resize((w, h)))
        isz = int(min(w, h) * icon_frac)
        canvas.alpha_composite(full.resize((isz, isz), Image.LANCZOS),
                               ((w - isz) // 2, (h - isz) // 2))
        _save(canvas, out_dir, name)

    wide("Wide310x150Logo.png", 310, 150)
    wide("Wide310x150Logo.scale-200.png", 620, 300)

    print(f"wrote {len(os.listdir(out_dir))} assets to {out_dir}")


if __name__ == "__main__":
    render(sys.argv[1] if len(sys.argv) > 1 else "Assets")
