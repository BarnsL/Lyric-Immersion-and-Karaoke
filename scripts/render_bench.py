"""Standalone lyric-render micro-benchmark — faithfully replicates Desktop
Karaoke's CPU (Tk/PIL) render primitives (glyph atlas, line compose, sliver fill)
and tests the documented-but-unbuilt LP-005 lever #2 (flat render + alpha-composite
outline vs per-glyph stroke). Placeholder text only — no lyrics.

Measures, at font_scale 1.0 and 1.5, on a WORST-CASE dense JP block
(kanji + furigana + romaji + english = 4 rows):
  cold_atlas   : first-appearance — rasterise every glyph tile (per-glyph stroke) + compose
  warm_atlas   : re-entry — compose from cached tiles (alpha_composite only)
  cold_flat    : first-appearance via flat text + N-offset alpha-composite outline (the proposed win)
  fill_step    : one sliver-fill strip paste (the per-frame karaoke fill cost)
"""
import time, statistics
from PIL import Image, ImageDraw, ImageFont

INK = (0, 0, 0, 255)
BASE = (248, 250, 252, 255)   # #f8fafc
SUNG = (252, 211, 141, 255)   # #fcd34d
FURI = (125, 211, 252, 255)   # #7dd3fc

FONT_FILES = {"jp": ["YuGothB.ttc", "meiryob.ttc", "msgothic.ttc"],
              "furi": ["YuGothR.ttc", "meiryo.ttc", "msgothic.ttc"],
              "rm": ["seguisb.ttf", "segoeui.ttf"],
              "en": ["segoeui.ttf"]}

def load(kind, size):
    for f in FONT_FILES[kind]:
        try: return ImageFont.truetype(f, size)
        except Exception: continue
    return ImageFont.load_default()

# Placeholder dense content (NOT lyrics): repeated common kana/kanji + latin.
KANJI = "変身希望旅路光影夢幻空色時間"           # 14 CJK chars (main row)
FURI_READ = ["へん","しん","き","ぼう","たび","じ","ひかり","かげ"]  # 8 readings over kanji
ROMAJI = "henshin kibou tabiji hikari kage yume maboroshi"  # ~46 latin
ENGLISH = "transform hope journey light shadow dream illusion"  # ~48 latin

def build_spec(s):
    jp = load("jp", max(10, round(38*s))); furi = load("furi", max(7, round(17*s)))
    rm = load("rm", max(8, round(23*s)));  en = load("en", max(8, round(21*s)))
    # lay out rows with approximate x advances (matches _img_row: cumulative widths)
    def row(text, font, y):
        chars, cx = [], 0
        for ch in text:
            chars.append((ch, cx))
            cx += font.getlength(ch)
        return {"chars": chars, "font": font, "y": y, "w": cx}
    r_jp = row(KANJI, jp, round(60*s))
    r_rm = row(ROMAJI, rm, round(110*s))
    r_en = row(ENGLISH, en, round(150*s))
    # furigana positioned over first kanji columns
    furi_items, fx = [], 0
    for rd in FURI_READ:
        furi_items.append((rd, fx)); fx += jp.getlength("変") * 1.4
    w = int(max(r_jp["w"], r_rm["w"], r_en["w"]) + 20*s)
    h = int(180*s)
    return {"rows": [r_jp, r_rm, r_en], "furi": furi_items, "furi_font": furi,
            "furi_y": round(30*s), "w": w, "h": h}, (jp, furi, rm, en)

def atlas_tile(cache, text, font, color, sw, anchor):
    key = (text, id(font), color, sw, anchor)
    g = cache.get(key)
    if g is None:
        l, t, r, b = font.getbbox(text, stroke_width=sw, anchor=anchor)
        tile = Image.new("RGBA", (max(1, r-l), max(1, b-t)), (0,0,0,0))
        ImageDraw.Draw(tile).text((-l,-t), text, font=font, fill=color,
                                  anchor=anchor, stroke_width=sw, stroke_fill=INK)
        g = (tile, l, t); cache[key] = g
    return g

def compose_atlas(spec, color, sw, cache):
    img = Image.new("RGBA", (spec["w"], spec["h"]), (0,0,0,0))
    for text, cx in spec["furi"]:
        tile, l, t = atlas_tile(cache, text, spec["furi_font"], FURI, 1, "mm")
        img.alpha_composite(tile, (max(0,round(cx+l)), max(0,round(spec["furi_y"]+t))))
    for row in spec["rows"]:
        for ch, cx in row["chars"]:
            tile, l, t = atlas_tile(cache, ch, row["font"], color, sw, "lm")
            img.alpha_composite(tile, (max(0,round(cx+l)), max(0,round(row["y"]+t))))
    return img

def compose_flat_outline(spec, color, sw):
    """LP-005 lever #2: draw each row's text FLAT once (no per-glyph stroke), build
    the outline by compositing the flat black alpha at a ring of offsets. One
    text() call per row instead of N stroked-glyph rasterisations."""
    w, h = spec["w"], spec["h"]
    # 1) flat colored text layer
    flat = Image.new("RGBA", (w, h), (0,0,0,0)); fd = ImageDraw.Draw(flat)
    for text, cx in spec["furi"]:
        fd.text((round(cx), round(spec["furi_y"])), text, font=spec["furi_font"], fill=FURI, anchor="mm")
    for row in spec["rows"]:
        fd.text((0, round(row["y"])), "".join(c for c,_ in row["chars"]), font=row["font"], fill=color, anchor="lm")
    # 2) black outline from the flat alpha, ring of offsets radius sw
    alpha = flat.split()[3]
    black = Image.new("RGBA", (w, h), (0,0,0,0))
    solid = Image.new("RGBA", (w, h), INK)
    offs = [(dx,dy) for dx in range(-sw,sw+1) for dy in range(-sw,sw+1) if dx*dx+dy*dy<=sw*sw and (dx or dy)]
    for dx, dy in offs:
        shifted = Image.new("L", (w, h), 0); shifted.paste(alpha, (dx, dy))
        black.paste(solid, (0,0), shifted)
    black.alpha_composite(flat)      # colored text over its outline
    return black

def sliver_fill(base, sung, spec, sw):
    """Advance the fill one strip (crop the sung layer + paste over base)."""
    comp = base.copy()
    for row in spec["rows"]:
        chars = row["chars"]
        if not chars: continue
        x0 = int(chars[len(chars)//3][1]); x1 = int(chars[2*len(chars)//3][1])
        try: asc, desc = row["font"].getmetrics(); half = (asc+desc)/2+sw+3
        except Exception: half = 30
        y0, y1 = max(0,int(row["y"]-half)), min(spec["h"], int(row["y"]+half))
        crop = sung.crop((x0, y0, x1, y1)); comp.paste(crop, (x0, y0), crop)
    return comp

def bench(fn, k=25):
    ts = []
    for _ in range(k):
        t0 = time.perf_counter(); fn(); ts.append((time.perf_counter()-t0)*1000)
    return statistics.median(ts), min(ts), max(ts)

for s in (1.0, 1.5):
    spec, _ = build_spec(s); sw = 2
    warm_cache = {}; compose_atlas(spec, BASE, sw, warm_cache)   # prewarm
    base_img = compose_atlas(spec, BASE, sw, warm_cache)
    sung_img = compose_atlas(spec, SUNG, sw, warm_cache)
    n_glyphs = sum(len(r["chars"]) for r in spec["rows"]) + len(spec["furi"])
    print(f"\n===== font_scale {s}  (block {spec['w']}x{spec['h']}, {n_glyphs} glyphs) =====")
    for name, fn in [
        ("cold_atlas (first appearance, per-glyph stroke)", lambda: compose_atlas(spec, BASE, sw, {})),
        ("warm_atlas (re-entry, cached tiles)",             lambda: compose_atlas(spec, BASE, sw, dict(warm_cache))),
        ("cold_flat  (flat + alpha-composite outline)",     lambda: compose_flat_outline(spec, BASE, sw)),
        ("fill_step  (one sliver paste)",                   lambda: sliver_fill(base_img, sung_img, spec, sw)),
    ]:
        med, lo, hi = bench(fn)
        print(f"  {name:52s}  median={med:6.1f}ms  (min {lo:5.1f}, max {hi:5.1f})")
