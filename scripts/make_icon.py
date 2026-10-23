"""
Generate the app icon (icon.ico) — a clean karaoke microphone with sound waves on
a purple gradient. Reproducible so the icon can be tweaked and rebuilt:

    python make_icon.py            # writes icon.ico (multi-size)
    python make_icon.py --preview  # also writes _icon_preview.png (a contact sheet)

Everything is drawn at high resolution and downscaled with LANCZOS for crisp
anti-aliasing. Small sizes (<=32px, the system tray) use a simplified, chunkier
master so the mic stays legible when it's only a few pixels tall.
"""
from __future__ import annotations

import sys
from PIL import Image, ImageDraw, ImageFilter

S = 1024                      # master canvas (supersampled)
RADIUS = int(S * 0.225)       # rounded-square corner radius

# Brand purples
TOP = (139, 96, 255)          # violet (top of gradient)
BOT = (84, 28, 178)           # deep purple (bottom)
MIC_TOP = (255, 255, 255)
MIC_BOT = (224, 222, 240)     # faint lavender so the white mic has form

# Microphone geometry as fractions of the canvas (shared by mask + grille so they
# never drift apart). A vocal mic = grille HEAD, a junction BAND, a tapered BODY.
HEAD = (0.380, 0.180, 0.620, 0.500)   # l, t, r, b  (semicircular domed top)
HEAD_R = 0.120                         # corner radius (= half width → full dome)
BAND = (0.402, 0.486, 0.598, 0.548)
BODY = (0.438, 0.540, 0.562, 0.840)   # longer handle so it reads as a mic
BODY_R = 0.058
GRILLE_FY = (0.250, 0.318, 0.386, 0.452)   # mesh lines within the head


def _rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1],
                                        radius=radius, fill=255)
    return m


def _vgradient(size, top, bot):
    """A vertical top→bottom gradient, RGB."""
    g = Image.new("RGB", (1, size))
    px = g.load()
    for y in range(size):
        t = y / (size - 1)
        px[0, y] = tuple(round(top[i] + (bot[i] - top[i]) * t) for i in range(3))
    return g.resize((size, size))


def _background(size):
    """Rounded-square purple gradient with a soft top sheen."""
    bg = _vgradient(size, TOP, BOT).convert("RGBA")
    # soft highlight near the top — a faint white radial for a glossy feel
    glow = Image.new("L", (size, size), 0)
    gd = ImageDraw.Draw(glow)
    gd.ellipse([size * 0.10, -size * 0.34, size * 0.90, size * 0.42], fill=70)
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.06))
    sheen = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    sheen.putalpha(glow)
    bg = Image.alpha_composite(bg, sheen)
    # clip to the rounded square
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(bg, (0, 0), _rounded_mask(size, RADIUS))
    return out


def _xy(size, scale, cy_shift):
    """Return X()/Y() that map geometry fractions → pixels, scaled about centre."""
    cx = size / 2
    def X(fx):
        return cx + (fx - 0.5) * size * scale
    def Y(fy):
        return (fy + cy_shift) * size * scale + (1 - scale) * size * 0.5
    return X, Y


def _mic_mask(size, scale=1.0, cy_shift=0.0):
    """A handheld vocal mic (grille head + band + tapered body) as an L mask,
    centred. `scale` grows the whole mic; `cy_shift` nudges it vertically."""
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    X, Y = _xy(size, scale, cy_shift)
    d.rounded_rectangle([X(HEAD[0]), Y(HEAD[1]), X(HEAD[2]), Y(HEAD[3])],
                        radius=size * HEAD_R * scale, fill=255)
    d.rounded_rectangle([X(BAND[0]), Y(BAND[1]), X(BAND[2]), Y(BAND[3])],
                        radius=size * 0.026 * scale, fill=255)
    d.rounded_rectangle([X(BODY[0]), Y(BODY[1]), X(BODY[2]), Y(BODY[3])],
                        radius=size * BODY_R * scale, fill=255)
    return m


def _apply_grille(img, size, scale=1.0, cy_shift=0.0):
    """Thin lavender mesh lines across the mic head (drawn on the composite,
    clipped to the head region)."""
    X, Y = _xy(size, scale, cy_shift)
    head_l, head_r = X(HEAD[0]), X(HEAD[2])
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    pad = (head_r - head_l) * 0.17
    for fy in GRILLE_FY:
        y = Y(fy)
        ld.line([head_l + pad, y, head_r - pad, y],
                fill=(150, 120, 220, 150), width=max(2, int(size * 0.013 * scale)))
    headmask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(headmask).rounded_rectangle(
        [head_l, Y(HEAD[1]), head_r, Y(HEAD[3])],
        radius=size * HEAD_R * scale, fill=255)
    img.paste(layer, (0, 0),
              Image.composite(layer.split()[3], Image.new("L", (size, size), 0), headmask))
    return img


def _sound_waves(img, size):
    """Two concentric arcs on each side of the mic head — 'voice / sound'."""
    d = ImageDraw.Draw(img)
    cx, cy = size / 2, size * 0.340
    for i, r in enumerate((size * 0.265, size * 0.345)):
        w = max(3, int(size * (0.020 - i * 0.004)))
        box = [cx - r, cy - r, cx + r, cy + r]
        col = (255, 255, 255, 235 - i * 70)
        d.arc(box, start=-38, end=38, fill=col, width=w)        # right side
        d.arc(box, start=142, end=218, fill=col, width=w)       # left side
    return img


def _compose(size, simplified=False):
    img = _background(size)
    scale = 1.27 if simplified else 1.0   # bolder mic for tiny tray sizes
    cy = -0.015 if simplified else 0.0
    mask = _mic_mask(size, scale=scale, cy_shift=cy)

    # drop shadow under the mic for depth
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow.putalpha(mask.point(lambda a: int(a * 0.45)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(size * 0.018))
    img = Image.alpha_composite(img, ImageChops_offset(shadow, int(size * 0.012), int(size * 0.02)))

    # the mic itself: a soft vertical white gradient through the mask
    micfill = _vgradient(size, MIC_TOP, MIC_BOT).convert("RGBA")
    img.paste(micfill, (0, 0), mask)
    img = _apply_grille(img, size, scale=scale, cy_shift=cy)
    if not simplified:
        img = _sound_waves(img, size)
    return img


def ImageChops_offset(im, dx, dy):
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(im, (dx, dy))
    return out


def build():
    full = _compose(S, simplified=False)
    small = _compose(S, simplified=True)
    sizes = [256, 128, 64, 48, 32, 24, 16]
    frames = {}
    for s in sizes:
        src = small if s <= 32 else full
        frames[s] = src.resize((s, s), Image.LANCZOS)
    frames[256].save("icon.ico", format="ICO",
                     sizes=[(s, s) for s in sizes],
                     append_images=[frames[s] for s in sizes if s != 256])
    return frames


def preview(frames):
    pad = 24
    bg = (40, 40, 48)
    order = [256, 64, 32, 16]
    w = sum(s for s in order) + pad * (len(order) + 1)
    h = 256 + pad * 2 + 28
    sheet = Image.new("RGB", (w, h), bg)
    x = pad
    dr = ImageDraw.Draw(sheet)
    for s in order:
        y = pad + (256 - s)
        sheet.paste(frames[s], (x, y), frames[s])
        dr.text((x, pad + 256 + 4), f"{s}px", fill=(200, 200, 210))
        x += s + pad
    sheet.save("_icon_preview.png")
    print("wrote _icon_preview.png")


if __name__ == "__main__":
    fr = build()
    print("wrote icon.ico", [f"{s}" for s in sorted(fr)])
    if "--preview" in sys.argv:
        preview(fr)
