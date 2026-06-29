"""
GPU lyric renderer — M1 (standalone process, no IPC yet).

Opens a borderless OpenGL overlay window with:
  - per-pixel alpha (DWM blur-behind with empty region — no UpdateLayeredWindow CPU readback)
  - click-through (WS_EX_TRANSPARENT)
  - topmost + no focus steal (WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
and renders a karaoke-style lyric block from a cached JSON file in
D:\\DesktopKaraoke\\lyrics\\. The window's per-pixel-alpha + click-through bet
was proven by spikes/gpu_overlay_autotest.py (PASSED on the 3080 ~109fps);
this file builds on that plumbing.

What M1 does (self-contained):
  * Load lyrics JSON ({meta:{title,artist,lang}, lines:[{t:[start,end], jp, rm, en}]}).
  * Build a glyph atlas per-line using pygame.freetype + Yu Gothic UI Medium
    (a CJK-capable TTF present on Win11). Each line becomes ONE pre-rasterized
    Surface, uploaded as one GL texture — keeps per-frame work to draw calls
    + a scissor uniform, no glyph layout in the hot loop.
  * Draw a 5-line vertical block centered on the current line. Above/below
    lines fade with distance. Active line gets a karaoke FILL: the texture is
    drawn twice — base color full width, then highlight color SCISSORED to
    [0, fill_frac × line_width].
  * Simulated playback: a wall-clock loop advances pos_raw from 0 to
    (last line end + 3 s) and quits.

Run it:
  python gpu_renderer.py D:\\DesktopKaraoke\\lyrics\\hour_time_yellow.json
Optional flags:
  --width / --height       window size (default 1280×320)
  --font-size              base font size (default 44)
  --field jp|rm|en         which line text to render (default jp)
  --start-at S             skip ahead S seconds into the song (default 0)
  --capture S OUT          grab one screenshot at S s, write OUT.png, quit ~1s later

NOT in M1 — those are M2/M3:
  * IPC with main.py (real player position + lyric state).
  * Tray toggle + GPU-pin/CPU override menu.
  * Replacing the Tk renderer.
"""
from __future__ import annotations
import argparse
import array
import ctypes
import json
import os
import re
import sys
import time
from ctypes import wintypes
from pathlib import Path

# Force UTF-8 on ALL three std streams. stdout/stderr so the diagnostic prints
# (which may include CJK or arrows) don't crash under Windows' default cp1252
# console encoding; stdin so the NDJSON the parent pipes in (UTF-8 lyric text)
# is decoded as UTF-8 and not the locale codepage — decoding the parent's
# UTF-8 bytes as cp1252 turns every CJK lyric into mojibake (JP garbled while
# ASCII romaji/English stay clean). _stdin_reader also reads binary + decodes
# UTF-8 explicitly as a second line of defense.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass

import moderngl
import pygame
from PIL import Image, ImageDraw, ImageFont

# ── win32 / dwmapi plumbing (mirrors spikes/gpu_overlay_spike.py) ────────────
user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
gdi32 = ctypes.windll.gdi32

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
DWM_BB_ENABLE = 0x01
DWM_BB_BLURREGION = 0x02
LWA_COLORKEY = 0x01
# The chroma key. The GL scene clears to this color (opaque) and DWM makes every
# pixel exactly this color fully transparent. Pure black: glyphs are bright tints
# (gold/white/sky/green/gray) so no glyph pixel is ever (0,0,0); the premultiplied
# fill leaves anti-aliased edges as tint×coverage (a subtle dark rim that reads as
# a readable outline over video), and only the true-black background is keyed out.
COLOR_KEY_RGB = 0x000000


class DWM_BLURBEHIND(ctypes.Structure):
    _fields_ = [("dwFlags", wintypes.DWORD), ("fEnable", wintypes.BOOL),
                ("hRgnBlur", wintypes.HRGN), ("fTransitionOnMaximized", wintypes.BOOL)]


def enable_per_pixel_alpha(hwnd):
    # LEGACY (kept for the --opaque-bg self-test only). DWM blur-behind with an
    # empty region was the original transparency bet, but on Win10/11 it
    # composites as a SOLID DARK backdrop on the live display (confirmed via a
    # real screen capture) — the "black background" the overlay showed over
    # video. The shipping path is set_colorkey() below, which is what the Tk
    # overlay uses and is immune to the "Transparency effects" setting + MPO
    # hardware-video planes.
    region = gdi32.CreateRectRgn(0, 0, -1, -1)
    bb = DWM_BLURBEHIND(DWM_BB_ENABLE | DWM_BB_BLURREGION, True, region, False)
    dwmapi.DwmEnableBlurBehindWindow(hwnd, ctypes.byref(bb))
    gdi32.DeleteObject(region)


def set_colorkey(hwnd, colorref: int = COLOR_KEY_RGB):
    """Make `colorref` (a Win32 0x00BBGGRR COLORREF) fully transparent on a
    LAYERED window via DWM color-keying. Reliable over hardware video and
    independent of the Windows transparency-effects toggle — unlike blur-behind.
    The window must already have WS_EX_LAYERED (set by set_exstyle)."""
    user32.SetLayeredWindowAttributes(hwnd, colorref, 0, LWA_COLORKEY)


def set_exstyle(hwnd, click_through: bool, topmost: bool):
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    if click_through:
        ex |= WS_EX_TRANSPARENT
    else:
        ex &= ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
    user32.SetWindowPos(hwnd, HWND_TOPMOST if topmost else HWND_NOTOPMOST,
                        0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


# ── shaders — premultiplied alpha, color tint, optional horizontal scissor ──
# The scissor for the highlight pass is done via ctx.scissor (a real GL state),
# NOT in the shader, so the highlight color is drawn ONLY where the active
# line's fill has advanced to.
VERT = """
#version 330
in vec2 in_pos;        // clip-space [-1, 1]
in vec2 in_uv;         // texture [0, 1]
out vec2 v_uv;
void main() {
    v_uv = in_uv;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""
FRAG = """
#version 330
in vec2 v_uv;
out vec4 frag;
uniform sampler2D tex;
uniform vec4 tint;     // RGBA — RGB = glyph color, A = whole-line opacity
void main() {
    // Glyph mask is in the texture's alpha (we rasterize white-on-clear).
    // Premultiplied-alpha output: rgb = color × coverage × line-opacity, a = the same.
    float a = texture(tex, v_uv).a * tint.a;
    frag = vec4(tint.rgb * a, a);
}
"""


# ── furigana parsing (mirrors main.split_furigana) ─────────────────────────
# A kanji-led run followed by a kana reading in parens: 人生(じんせい) / 挑(いど).
_FURI_RE = re.compile(r"([一-鿿㐀-䶿々][一-鿿㐀-䶿々ぁ-ゖァ-ヺー]*)\(([ぁ-ゖァ-ヺー]+)\)")


def split_furigana(text: str):
    """Parse 'kanji(かな)' markup → [(base, reading), …]; plain runs come back
    as (text, ''). Identical to main.split_furigana so the GPU overlay shows
    the same ruby the Tk renderer did."""
    parts, last = [], 0
    for m in _FURI_RE.finditer(text or ""):
        if m.start() > last:
            parts.append((text[last:m.start()], ""))
        parts.append((m.group(1), m.group(2)))
        last = m.end()
    if last < len(text or ""):
        parts.append((text[last:], ""))
    return parts


# ── glyph-mask texture (one per text element) ──────────────────────────────
class GlyphTex:
    """A white-on-transparent text mask uploaded as a GL texture. The shader
    tints it at draw time, so ONE mask serves every color (base / fill / ruby /
    romaji / english). PIL RGBA bytes are top-down, matching moderngl's texture
    layout, so the quad uses UV (0,0)=top-left with no flip.

    `advance` is the font ADVANCE width (getlength) — used for laying ruby above
    the right base run; `w`/`h` are the INK bounds of the uploaded bitmap."""

    def __init__(self, ctx: moderngl.Context, img: Image.Image, advance: float):
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        self.w, self.h = img.size
        self.advance = advance
        self.tex = ctx.texture((self.w, self.h), 4, img.tobytes())
        self.tex.repeat_x = False
        self.tex.repeat_y = False
        self.tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

    def release(self):
        if self.tex is not None:
            self.tex.release()
            self.tex = None


def make_glyph_tex(ctx, text: str, font: ImageFont.FreeTypeFont,
                   pad: int = 3) -> "GlyphTex | None":
    """Rasterize `text` white-on-clear and upload it. None for empty input."""
    text = (text or "").strip()
    if not text:
        return None
    bbox = font.getbbox(text)
    w = max(1, bbox[2] - bbox[0]) + pad * 2
    h = max(1, bbox[3] - bbox[1]) + pad * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((-bbox[0] + pad, -bbox[1] + pad), text, font=font,
                             fill=(255, 255, 255, 255))
    try:
        advance = float(font.getlength(text))
    except Exception:
        advance = float(w)
    return GlyphTex(ctx, img, advance)


class RenderedLine:
    """All the GL textures + layout metrics for one lyric line: the jp base
    mask, the ruby masks with their x-centers (in base-advance pixels), and
    the romaji + english sub-line masks. Built once per line, cached per song."""

    __slots__ = ("base", "base_adv", "rubies", "rm", "en")

    def __init__(self, base, base_adv, rubies, rm, en):
        self.base = base            # GlyphTex | None
        self.base_adv = base_adv    # advance width of the full base text (px)
        self.rubies = rubies        # list[(GlyphTex, x_center_px)]
        self.rm = rm                # GlyphTex | None
        self.en = en                # GlyphTex | None

    def release(self):
        for t in (self.base, self.rm, self.en):
            if t:
                t.release()
        for t, _ in self.rubies:
            if t:
                t.release()


# ── renderer ───────────────────────────────────────────────────────────────
class LyricRenderer:
    """Owns the GL context, the line-atlas cache, and the draw loop.

    The PUBLIC surface is two methods + state:
      tick(pos_raw)   — given the current song time, update active line + fill.
      draw()          — one frame; clears, draws visible block, swaps.
    Plus `quit` set by the event loop. This shape will be the M2 IPC seam —
    a parent process feeds pos_raw + lines via stdin, and our loop calls
    tick()/draw() exactly like the self-test does."""

    # Colors as premultiply-friendly RGBA tints (A = line opacity).
    BASE_COLOR = (0.88, 0.88, 0.90, 1.00)  # active jp base (unsung) — near-white
    FILL_COLOR = (1.00, 0.78, 0.18, 1.00)  # karaoke fill on the sung portion — gold
    RUBY_COLOR = (0.49, 0.83, 0.99, 1.00)  # furigana above kanji — sky blue (#7dd3fc)
    RM_COLOR   = (0.55, 0.86, 0.66, 1.00)  # romaji — green
    EN_COLOR   = (0.82, 0.82, 0.85, 0.92)  # english — light gray
    CTX_COLOR  = (0.62, 0.62, 0.66, 1.00)  # context (non-active) lines — dim

    def __init__(self, lines, width: int, height: int,
                 font_path: str, font_size: int, field: str = "jp",
                 opaque_bg: bool = False):
        # `lines` may be empty when running under IPC — set_lines() repopulates
        # it on every {"type":"song"} message from the parent.
        self.lines = lines or []
        self.field = field
        self.W, self.H = width, height
        self.opaque_bg = opaque_bg                          # debug: solid dark fill
        self.quit = False

        # Window
        pygame.init()
        pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 3)
        pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 3)
        pygame.display.gl_set_attribute(pygame.GL_ALPHA_SIZE, 8)
        pygame.display.set_mode((self.W, self.H),
                                pygame.OPENGL | pygame.DOUBLEBUF | pygame.NOFRAME)
        pygame.display.set_caption("gpu_renderer (M1)")
        self.hwnd = pygame.display.get_wm_info()["window"]
        # Click-through ON from the start: the overlay is non-interactive.
        # set_exstyle MUST run before set_colorkey (color-key needs WS_EX_LAYERED).
        set_exstyle(self.hwnd, click_through=True, topmost=True)
        self._opaque_bg = opaque_bg
        if not opaque_bg:
            set_colorkey(self.hwnd)         # black → transparent (the real overlay)
        # SDL centers a NOFRAME window; pin it to (0,0) so a full-screen-sized
        # window actually covers the screen and the centered block lands mid-screen.
        # A window spawned from a DETACHED (CREATE_NO_WINDOW) child can come up
        # hidden, so explicitly SHOW it without stealing focus (SW_SHOWNOACTIVATE)
        # — this is the fix for "GPU child renders but nothing is visible".
        SW_SHOWNOACTIVATE = 4
        try:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, self.W, self.H,
                                SWP_NOACTIVATE)
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            # Re-assert the color key AFTER the show+resize (SetWindowLong/Pos
            # can clear the layered attributes on some drivers).
            if not opaque_bg:
                set_colorkey(self.hwnd)
        except Exception:
            pass

        # GL
        self.ctx = moderngl.create_context()
        self.prog = self.ctx.program(vertex_shader=VERT, fragment_shader=FRAG)
        self.ctx.enable(moderngl.BLEND)
        # premultiplied-alpha over destination; MAX on alpha keeps AA edges visible.
        self.ctx.blend_func = (moderngl.ONE, moderngl.ONE_MINUS_SRC_ALPHA,
                               moderngl.ONE, moderngl.ONE_MINUS_SRC_ALPHA)
        self.ctx.blend_equation = (moderngl.FUNC_ADD, moderngl.MAX)

        # One reusable VBO; we update its 4 vertices each draw call.
        self.vbo = self.ctx.buffer(reserve=4 * 4 * 4)   # 4 verts × (pos2 + uv2) × float32
        self.vao = self.ctx.vertex_array(self.prog, [(self.vbo, "2f 2f", "in_pos", "in_uv")])

        # Three font sizes: base (kanji/kana), ruby (furigana, ~42%), sub
        # (romaji + english, ~52%). PIL handles CJK + advance metrics cleanly.
        self._font_path = font_path
        self._base_font_size = font_size      # before font_scale (rescaled by set_opts)
        self.font = ImageFont.truetype(font_path, font_size)
        self.ruby_font = ImageFont.truetype(font_path, max(10, int(font_size * 0.42)))
        self.sub_font = ImageFont.truetype(font_path, max(12, int(font_size * 0.52)))
        self.base_h = font_size
        self.ruby_h = max(10, int(font_size * 0.42))
        self.sub_h = max(12, int(font_size * 0.52))
        self._line_cache: dict[str, RenderedLine] = {}      # jp text → RenderedLine

        # State driven by set_state()/tick().
        self._cur_idx = -1                                  # active line index, -1 = none
        self._fill_frac = 0.0                               # 0..1 across active line
        self._pos = 0.0                                     # song clock (pos_hi) from parent
        self._pos_recv = time.perf_counter()                # wall time the last _pos arrived
        self._playing = True                                # advance the belt only when playing
        self.scroll_dir = "none"                            # none|lr|rl (vertical block otherwise)
        self.scroll_speed = 200.0                           # px/s for the horizontal belt
        # SETTINGS PARITY with the Tk overlay (sent by the parent each tick).
        self.opacity = 1.0                                  # 0..1 — dims glyph RGB (color-key safe)
        self.pos_y = "center"                               # top|center|bottom
        self.pos_x = "center"                               # left|center|right (block mode)
        self.font_scale = 1.0                               # 0.25..2.0
        self._self_heal_t = 0.0                             # SDL ex-style re-assert clock
        # keep-last-line: when idx goes -1 (inter-line gap) hold the previous
        # line on screen for a short window so the overlay never flickers blank.
        self._held_idx = -1
        self._gap_since = None
        self._hold_gap_s = 0.7

        self.gl_renderer = self.ctx.info.get("GL_RENDERER", "?")

    # — line texture cache (jp text → RenderedLine) —
    def _line_for(self, i: int) -> "RenderedLine | None":
        if not (0 <= i < len(self.lines)):
            return None
        line = self.lines[i]
        key = (line.get("jp") or line.get("rm") or "") + f"#{i}"
        rl = self._line_cache.get(key)
        if rl is not None:
            return rl
        rl = self._build_line(line)
        self._line_cache[key] = rl
        if len(self._line_cache) > 300:                 # bound for long playlists
            k = next(iter(self._line_cache))
            self._line_cache.pop(k).release()
        return rl

    def _build_line(self, line: dict) -> "RenderedLine":
        jp = (line.get("jp") or "").strip()
        rm = (line.get("rm") or "").strip()
        en = (line.get("en") or "").strip()
        segs = split_furigana(jp)
        base_text = "".join(b for b, _ in segs)
        base = make_glyph_tex(self.ctx, base_text, self.font)
        base_adv = base.advance if base else 0.0
        rubies = []
        x = 0.0
        for b, r in segs:                               # lay ruby above each kanji run
            try:
                w = float(self.font.getlength(b))
            except Exception:
                w = 0.0
            if r:
                rt = make_glyph_tex(self.ctx, r, self.ruby_font)
                if rt:
                    rubies.append((rt, x + w / 2.0))
            x += w
        rm_t = make_glyph_tex(self.ctx, rm, self.sub_font) if rm else None
        en_t = make_glyph_tex(self.ctx, en, self.sub_font) if en else None
        return RenderedLine(base, base_adv, rubies, rm_t, en_t)

    # — frame state —
    def tick(self, pos_raw: float):
        """Update active line + karaoke fill from the current song time."""
        idx = -1
        for i, ln in enumerate(self.lines):
            start, end = ln["t"][0], ln["t"][1]
            if start <= pos_raw < end:
                idx = i
                break
        self._cur_idx = idx
        if idx >= 0:
            start, end = self.lines[idx]["t"][0], self.lines[idx]["t"][1]
            dur = max(0.001, end - start)
            self._fill_frac = max(0.0, min(1.0, (pos_raw - start) / dur))
        else:
            self._fill_frac = 0.0

    def set_state(self, pos_raw: float, idx: int | None = None,
                  fill_frac: float | None = None):
        """IPC variant of tick(): trust the parent's idx + fill_frac (computed
        from the same sync clock that drives the Tk overlay), so the two
        renderers stay byte-identical on which line is active. If the parent
        didn't supply them, fall back to local computation via tick()."""
        self._pos = pos_raw                  # song clock drives the scroll belt
        self._pos_recv = time.perf_counter() # anchor for between-message extrapolation
        if idx is None or fill_frac is None:
            self.tick(pos_raw)
        else:
            self._cur_idx = idx
            self._fill_frac = max(0.0, min(1.0, fill_frac))
        # keep-last-line: remember the last REAL line + when the gap started so
        # draw() can hold it briefly instead of flickering blank between lines.
        if self._cur_idx >= 0:
            self._held_idx = self._cur_idx
            self._gap_since = None
        elif self._gap_since is None:
            self._gap_since = time.perf_counter()

    def set_scroll(self, scroll_dir: str | None, scroll_speed: float | None = None):
        """Update the scroll mode/speed the parent (main.py) is using, so the GL
        overlay matches the Tk overlay's layout (horizontal belt vs centered
        block). Cheap no-op when unchanged."""
        if scroll_dir is not None:
            self.scroll_dir = scroll_dir
        if scroll_speed is not None and scroll_speed > 0:
            self.scroll_speed = float(scroll_speed)

    def set_opts(self, opacity=None, pos_y=None, pos_x=None, font_scale=None):
        """Mirror the Tk overlay's display SETTINGS so the GPU overlay honours the
        same opacity / position / font scale. font_scale changes rebuild the fonts
        + drop the glyph cache (rare, so the cost is fine)."""
        if opacity is not None:
            try:
                self.opacity = max(0.05, min(1.0, float(opacity)))
            except Exception:
                pass
        if pos_y:
            self.pos_y = pos_y
        if pos_x:
            self.pos_x = pos_x
        if font_scale is not None:
            try:
                fsx = max(0.25, min(2.5, float(font_scale)))
            except Exception:
                fsx = self.font_scale
            if abs(fsx - self.font_scale) > 1e-3:
                self.font_scale = fsx
                self._rebuild_fonts()

    def _rebuild_fonts(self):
        fs = max(10, int(self._base_font_size * self.font_scale))
        self.font = ImageFont.truetype(self._font_path, fs)
        self.ruby_font = ImageFont.truetype(self._font_path, max(10, int(fs * 0.42)))
        self.sub_font = ImageFont.truetype(self._font_path, max(12, int(fs * 0.52)))
        self.base_h = fs
        self.ruby_h = max(10, int(fs * 0.42))
        self.sub_h = max(12, int(fs * 0.52))
        for rl in self._line_cache.values():
            rl.release()
        self._line_cache.clear()

    def set_lines(self, lines, field: str | None = None):
        """Swap the loaded song. Frees the OLD line-texture cache. Idempotent:
        passing the same `lines` object is a cheap no-op."""
        if lines is self.lines:
            return
        for rl in self._line_cache.values():
            rl.release()
        self._line_cache.clear()
        self.lines = lines or []
        if field:
            self.field = field
        self._cur_idx = -1
        self._fill_frac = 0.0
        self._held_idx = -1
        self._gap_since = None

    def _draw_quad(self, x: float, y: float, w: float, h: float,
                   atlas: "GlyphTex", tint, scissor=None):
        """Draw `atlas` as a textured quad at (x,y) → (x+w, y+h) in PIXEL space
        (origin top-left). Optionally scissor to a sub-rect for the karaoke fill.

        The scissor box is in GL pixel coords (origin BOTTOM-left, +y up) — we
        convert from the top-left convention used elsewhere."""
        # convert pixel rect → clip-space; +x right, +y UP for GL clip.
        x0 = (x / self.W) * 2.0 - 1.0
        x1 = ((x + w) / self.W) * 2.0 - 1.0
        y0 = 1.0 - (y / self.H) * 2.0
        y1 = 1.0 - ((y + h) / self.H) * 2.0
        # Vertex layout: pos.x, pos.y, uv.x, uv.y — two triangles via triangle strip.
        # Texture data is top-down RGBA (PIL convention), so UV (0,0) = top-left
        # of the texture, (1,1) = bottom-right. Quad's top-left vertex maps to
        # UV (0,0); bottom-right to (1,1).
        data = array.array("f", [
            x0, y1, 0.0, 1.0,        # bottom-left  → texture (0, 1) bottom-left
            x1, y1, 1.0, 1.0,        # bottom-right → texture (1, 1) bottom-right
            x0, y0, 0.0, 0.0,        # top-left     → texture (0, 0) top-left
            x1, y0, 1.0, 0.0,        # top-right    → texture (1, 0) top-right
        ])
        self.vbo.write(data.tobytes())
        atlas.tex.use(0)
        self.prog["tex"].value = 0
        # OPACITY (color-key safe): scale the glyph RGB toward the keyed-out black
        # background. Per-pixel alpha can't carry opacity here (color-key uses RGB),
        # so a lower opacity dims the text and lets more of its edge key out.
        op = self.opacity
        self.prog["tint"].value = ((tint[0] * op, tint[1] * op, tint[2] * op, tint[3])
                                   if op < 0.999 else tint)
        if scissor is not None:
            sx, sy, sw, sh = scissor
            sy_gl = self.H - sy - sh
            self.ctx.scissor = (int(sx), int(sy_gl), max(1, int(sw)), max(1, int(sh)))
        else:
            self.ctx.scissor = None
        self.vao.render(mode=moderngl.TRIANGLE_STRIP)

    def _draw_line_block(self, i: int, base_cy: float, fill: float, is_active: bool,
                         cx_center: float | None = None, draw_sub: bool | None = None):
        """Render one lyric line with its base text horizontally centered on
        `cx_center` (screen centre by default) and vertically on `base_cy`.
        Active lines get the gold karaoke fill; `draw_sub` (defaults to
        is_active) forces romaji+english + bright ruby — the scroll belt passes
        draw_sub=True so every moving block carries its own translations."""
        rl = self._line_for(i)
        if rl is None or rl.base is None:
            return
        if cx_center is None:
            cx_center = self.W / 2.0
        if draw_sub is None:
            draw_sub = is_active
        base = rl.base
        # Horizontal origin = the advance-width centering point, so ruby x-centers
        # (advance-based) line up with the base ink.
        bx = cx_center - rl.base_adv / 2.0
        by = base_cy - base.h / 2.0
        # Only the ACTIVE line is bright; context lines are DIM. (Belt previously
        # tied brightness to draw_sub, so every scrolling line lit up bright =
        # the user's "permanent on highlights".) The gold karaoke fill is the
        # active line's sung portion; context lines never get it.
        base_tint = self.BASE_COLOR if is_active else self.CTX_COLOR
        self._draw_quad(bx, by, base.w, base.h, base, base_tint)
        if is_active and fill > 0:
            fw = max(1, int(base.w * fill))
            self._draw_quad(bx, by, base.w, base.h, base, self.FILL_COLOR,
                            scissor=(bx, by, fw, base.h))
        # ruby above the base run
        ruby_a = 1.0 if is_active else 0.55
        ruby_y = by - self.ruby_h - 1
        rcol = (self.RUBY_COLOR[0], self.RUBY_COLOR[1], self.RUBY_COLOR[2], ruby_a)
        for rt, cx in rl.rubies:
            self._draw_quad(bx + cx - rt.w / 2.0, ruby_y, rt.w, rt.h, rt, rcol)
        if not draw_sub:
            return
        # romaji + english stacked under the base (centered on the same cx)
        yb = by + base.h + 6
        if rl.rm:
            self._draw_quad(cx_center - rl.rm.w / 2.0, yb, rl.rm.w, rl.rm.h,
                            rl.rm, self.RM_COLOR)
            yb += rl.rm.h + 4
        if rl.en:
            self._draw_quad(cx_center - rl.en.w / 2.0, yb, rl.en.w, rl.en.h,
                            rl.en, self.EN_COLOR)

    def _row_cy(self):
        """Vertical centre of the lyric row, from the pos_y setting (parity with Tk)."""
        if self.pos_y == "top":
            return self.H * 0.24
        if self.pos_y == "bottom":
            return self.H * 0.76
        return self.H * 0.5

    def _eff_pos(self):
        """The belt's effective song position: the last value the parent sent plus
        the time elapsed since (capped), so the belt keeps gliding even when the
        parent thread STALLS (a ~4s Shazam capture, a lyric op) instead of freezing
        — the child renders at its own ~90fps and the parent re-anchors on resume."""
        if not self._playing:
            return self._pos
        ahead = time.perf_counter() - self._pos_recv
        return self._pos + max(0.0, min(ahead, 1.2))   # +1.2s cap: a dead parent can't run away

    def draw(self):
        # Self-heal the ex-style ~2×/sec so SDL events can't drop click-through.
        now = time.perf_counter()
        if now - self._self_heal_t > 0.5:
            self._self_heal_t = now
            set_exstyle(self.hwnd, click_through=True, topmost=True)
            if not self.opaque_bg:
                set_colorkey(self.hwnd)      # re-assert the chroma key too

        if self.opaque_bg:
            self.ctx.clear(0.06, 0.06, 0.09, 1.0)            # debug: opaque dark fill
        else:
            # OPAQUE black clear so DWM color-keying (black → transparent) works.
            # Premultiplied glyph blend over black leaves tint×coverage edges (a
            # subtle readable rim); only the true-black background is keyed out.
            self.ctx.clear(0.0, 0.0, 0.0, 1.0)

        # Effective active line: the real one, or — during a SHORT inter-line gap
        # — the held previous line (fill=full), so the overlay never flickers
        # blank between consecutive lines (the user's "disappear for a ms" bug).
        active, fill = self._cur_idx, self._fill_frac
        if active < 0 and self._held_idx >= 0 and self._gap_since is not None \
                and (now - self._gap_since) < self._hold_gap_s:
            active, fill = self._held_idx, 1.0

        # HORIZONTAL SCROLL-THROUGH (mirrors the Tk lr/rl belt). Drawn even during
        # an inter-line gap so the belt keeps moving; lines are positioned by time.
        if self.scroll_dir in ("lr", "rl") and self.lines:
            self._draw_scroll_belt(active, fill)
            return self._swap()

        if active < 0:
            return self._swap()

        # Vertical layout: active block centered; one context line above (clear of
        # the ruby) and one below (clear of the romaji+english).
        cy = self._row_cy()                    # honour the pos_y setting
        up_pitch = self.base_h + self.ruby_h + 34
        dn_pitch = self.base_h + self.sub_h * 2 + 42
        if active - 1 >= 0:
            self._draw_line_block(active - 1, cy - up_pitch, fill=0.0, is_active=False)
        if active + 1 < len(self.lines):
            self._draw_line_block(active + 1, cy + dn_pitch, fill=0.0, is_active=False)
        self._draw_line_block(active, cy, fill=fill, is_active=True)
        self._swap()

    def _draw_scroll_belt(self, active: int, fill: float):
        """Continuous horizontal belt: each line sits at
        cx = centre + dir·speed·(line_midtime − song_pos), so lines glide across
        and the one the parent flagged active carries the gold karaoke fill. The
        song clock (_pos) and scroll config come from the parent each tick, so
        the belt tracks the SAME free-running highlight clock as the Tk overlay."""
        pos = self._eff_pos()                  # extrapolated → smooth during parent stalls
        center = self.W / 2.0
        v = max(self.scroll_speed, 60.0)
        d = 1.0 if self.scroll_dir == "rl" else -1.0
        cy = self._row_cy()                    # honour the pos_y setting
        # Tighter window than before (1500 → ~half the screen each side) so only a
        # few lines are on the belt at once instead of the whole screen full.
        margin = self.W * 0.55
        for i, ln in enumerate(self.lines):
            t = ln.get("t") or (0.0, 0.0)
            mid = (t[0] + t[1]) / 2.0
            cx = center + d * v * (mid - pos)
            if -margin < cx < self.W + margin:
                is_act = (i == active)
                # Only the ACTIVE line carries the romaji+english (draw_sub); the
                # scrolling context lines are dim JP only — far less clutter.
                self._draw_line_block(i, cy, fill=(fill if is_act else 0.0),
                                      is_active=is_act, cx_center=cx, draw_sub=is_act)

    def _swap(self):
        pygame.display.flip()

    def pump_events(self):
        """Drain SDL events. ESC quits; toggle keys exist for manual experiments
        but the overlay is click-through, so SDL never receives mouse events."""
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                self.quit = True
            elif e.type == pygame.KEYDOWN and e.key == pygame.K_ESCAPE:
                self.quit = True

    def shutdown(self):
        for rl in self._line_cache.values():
            rl.release()
        self._line_cache.clear()
        pygame.quit()


# ── self-test driver ─────────────────────────────────────────────────────
def _resolve_font_path() -> str:
    """Pick a CJK-capable Windows font. Yu Gothic Medium first (Win11 default),
    then Meiryo, then MS Gothic — any of them covers JP + romaji + EN."""
    # NotoSansJP first: single-face TTF with full JP + Latin coverage. YuGothM.ttc
    # is a TrueType Collection — PIL's default face index might not be the JP face
    # under all builds, so prefer the simple TTF when available.
    for cand in ("NotoSansJP-VF.ttf", "YuGothM.ttc", "meiryo.ttc", "msgothic.ttc",
                 "BIZ-UDGothicR.ttc"):
        p = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / cand
        if p.exists():
            return str(p)
    raise FileNotFoundError("No CJK-capable font found in C:\\Windows\\Fonts")


def self_test(lyrics_path: Path, width: int, height: int,
              font_size: int, field: str, start_at: float,
              capture_at: float | None, capture_out: str | None,
              opaque_bg: bool = False):
    data = json.loads(lyrics_path.read_text(encoding="utf-8"))
    lines = data.get("lines") or []
    if not lines:
        raise SystemExit(f"no lines in {lyrics_path}")
    meta = data.get("meta") or {}
    print(f"loaded {len(lines)} lines: {meta.get('title')!r} by {meta.get('artist')!r}")

    font_path = _resolve_font_path()
    r = LyricRenderer(lines, width=width, height=height,
                      font_path=font_path, font_size=font_size, field=field,
                      opaque_bg=opaque_bg)
    print(f"GL_RENDERER: {r.gl_renderer}")

    last_t = lines[-1]["t"][1] + 3.0
    t0 = time.perf_counter() - start_at
    clock = pygame.time.Clock()
    frames = 0
    fps_t = time.perf_counter()
    captured = (capture_at is None)
    try:
        while not r.quit:
            now = time.perf_counter()
            song_t = now - t0
            if song_t > last_t:
                break
            r.pump_events()
            r.tick(song_t)
            r.draw()
            frames += 1
            if (not captured and capture_out is not None
                    and song_t >= float(capture_at)):
                captured = True
                _grab(r.hwnd, capture_out)
                print(f"captured at {song_t:.1f}s -> {capture_out}")
                # one extra second of frames so the user can see the capture state,
                # then break in the outer condition.
                t0 = now - last_t + 1.5
            if now - fps_t >= 1.0:
                print(f"  song_t={song_t:6.2f}s  cur_idx={r._cur_idx:3d}  "
                      f"fill={r._fill_frac:.2f}  fps={frames / (now - fps_t):5.1f}")
                frames = 0
                fps_t = now
            clock.tick(120)
    finally:
        r.shutdown()


def _grab(hwnd, out_path: str):
    """One-shot composited-window screenshot, mirroring the autotest."""
    try:
        from PIL import ImageGrab
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        bbox = (rect.left, rect.top, rect.right, rect.bottom)
        img = ImageGrab.grab(bbox=bbox)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        img.save(out_path)
    except Exception as e:
        print(f"grab failed: {e}")


# ── M2: IPC child mode (parent = main.py) ──────────────────────────────────
# Parent writes newline-delimited JSON to our stdin and we apply each message
# to the renderer state. Reads run on a daemon thread + queue so the GL main
# loop is never blocked by a slow parent or a hung pipe. Message types:
#   {"type":"song",  "lines":[{"t":[s,e],"jp":"...","rm":"...","en":"..."},...],
#                    "meta":{"title":"...","artist":"...","lang":"ja", ...},
#                    "field":"jp"|"rm"|"en"}        (field is optional)
#   {"type":"state", "pos_raw":float, "offset":float, "idx":int, "fill_frac":float,
#                    "playing":bool}                 (sent ~60 Hz)
#   {"type":"window","width":W,"height":H}           (optional, future)
#   {"type":"quit"}                                  (clean shutdown)
# Robustness: malformed lines are logged + dropped. EOF on stdin ends the loop.
def _stdin_reader(q):
    import threading
    def _read():
        # Read stdin as BINARY and decode each line as UTF-8 explicitly. The
        # parent writes UTF-8 NDJSON; relying on text-mode sys.stdin would
        # decode with Windows' cp1252 locale and turn every CJK lyric into
        # mojibake (the JP-garbled / romaji-fine bug). Binary + explicit UTF-8
        # is immune to the active console codepage.
        stream = getattr(sys.stdin, "buffer", None) or sys.stdin
        for rawb in stream:
            if isinstance(rawb, bytes):
                raw = rawb.decode("utf-8", "replace").strip()
            else:
                raw = rawb.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception as e:
                print(f"gpu_renderer: bad IPC line: {e!s} :: {raw[:160]!r}",
                      file=sys.stderr)
                continue
            q.put(msg)
        q.put({"type": "quit", "reason": "stdin-eof"})
    t = threading.Thread(target=_read, daemon=True)
    t.start()
    return t


def run_ipc_child(width: int | None = None, height: int | None = None,
                  font_size: int = 46, field: str = "jp", opaque_bg: bool = False):
    """Run as the GPU-renderer child of main.py. Blocks until the parent sends
    {"type":"quit"} or closes our stdin. Defaults to a FULL-SCREEN transparent
    click-through window so the centered lyric block sits mid-screen with room
    for the context lines above/below."""
    import queue
    if width is None:
        width = int(user32.GetSystemMetrics(0)) or 1920    # SM_CXSCREEN
    if height is None:
        height = int(user32.GetSystemMetrics(1)) or 1080   # SM_CYSCREEN
    q: "queue.Queue[dict]" = queue.Queue()
    _stdin_reader(q)

    font_path = _resolve_font_path()
    r = LyricRenderer([], width=width, height=height, font_path=font_path,
                      font_size=font_size, field=field, opaque_bg=opaque_bg)
    print(f"gpu_renderer: GL_RENDERER={r.gl_renderer}", file=sys.stderr)

    clock = pygame.time.Clock()
    pos_raw = 0.0
    idx = -1
    fill_frac = 0.0
    playing = False
    last_log_t = time.perf_counter()
    frames = 0
    cap_path = None                     # DEBUG: {"type":"capture","path":...}
    try:
        while not r.quit:
            # Drain ALL pending messages each frame so a burst of state updates
            # from the parent (60 Hz when caught up) doesn't accumulate latency.
            while True:
                try:
                    msg = q.get_nowait()
                except queue.Empty:
                    break
                t = msg.get("type")
                if t == "song":
                    r.set_lines(msg.get("lines") or [], field=msg.get("field"))
                    if "scroll_dir" in msg or "scroll_speed" in msg:
                        r.set_scroll(msg.get("scroll_dir"), msg.get("scroll_speed"))
                elif t == "state":
                    pos_raw = float(msg.get("pos_raw", pos_raw))
                    idx = int(msg.get("idx", -1))
                    fill_frac = float(msg.get("fill_frac", 0.0))
                    playing = bool(msg.get("playing", True))
                    r._playing = playing
                    if "scroll_dir" in msg or "scroll_speed" in msg:
                        r.set_scroll(msg.get("scroll_dir"), msg.get("scroll_speed"))
                    if any(k in msg for k in ("opacity", "pos_y", "pos_x", "font_scale")):
                        r.set_opts(opacity=msg.get("opacity"), pos_y=msg.get("pos_y"),
                                   pos_x=msg.get("pos_x"), font_scale=msg.get("font_scale"))
                elif t == "window":
                    # M2 keeps a fixed window; resize is M3 work.
                    pass
                elif t == "capture":
                    # DEBUG: grab a composited screenshot of the overlay window
                    # AFTER the next draw, so we can verify visibility through
                    # the exact spawned-child path (CREATE_NO_WINDOW etc.).
                    cap_path = msg.get("path")
                elif t == "quit":
                    r.quit = True
                    break

            r.pump_events()
            try:
                r.set_state(pos_raw, idx=idx, fill_frac=fill_frac)
                r.draw()
            except Exception as e:
                # NEVER let one bad frame kill the child — a dead child can leave a
                # black frame on screen and forces the CPU fallback. Log + continue.
                print(f"gpu_renderer: draw error (continuing): {e!s}",
                      file=sys.stderr, flush=True)
                try:
                    pygame.time.wait(8)
                except Exception:
                    pass
            if cap_path:
                _grab(r.hwnd, cap_path)
                print(f"gpu_renderer: captured -> {cap_path}", file=sys.stderr, flush=True)
                cap_path = None
            frames += 1
            now = time.perf_counter()
            if now - last_log_t >= 5.0:
                # Heartbeat to parent's stderr capture — helps confirm the
                # child is alive without spamming stdout.
                print(f"gpu_renderer: alive  pos_raw={pos_raw:.2f}  idx={idx}  "
                      f"fill={fill_frac:.2f}  playing={playing}  "
                      f"fps={frames / (now - last_log_t):.0f}",
                      file=sys.stderr, flush=True)
                last_log_t = now
                frames = 0
            clock.tick(120)
    finally:
        r.shutdown()


def main():
    # M2 IPC child mode — handled BEFORE argparse so the standalone self-test
    # flags don't need to know about --ipc, and the parent (main.py) can stay
    # blissfully unaware of the self-test path.
    if "--ipc" in sys.argv[1:]:
        width = height = font_size = None
        field = "jp"
        opaque_bg = False
        # Tiny manual parser — argparse can't easily mix optional flags with
        # the self-test's required positional `lyrics`, and IPC mode has no
        # positional argument at all.
        argv = sys.argv[1:]
        i = 0
        while i < len(argv):
            a = argv[i]
            if a == "--ipc":
                pass
            elif a == "--width" and i + 1 < len(argv):
                width = int(argv[i + 1]); i += 1
            elif a == "--height" and i + 1 < len(argv):
                height = int(argv[i + 1]); i += 1
            elif a == "--font-size" and i + 1 < len(argv):
                font_size = int(argv[i + 1]); i += 1
            elif a == "--field" and i + 1 < len(argv):
                field = argv[i + 1]; i += 1
            elif a == "--opaque-bg":
                opaque_bg = True
            i += 1
        run_ipc_child(width=width or 1280, height=height or 320,
                      font_size=font_size or 44, field=field,
                      opaque_bg=opaque_bg)
        return

    ap = argparse.ArgumentParser(description="gpu_renderer M1 self-test")
    ap.add_argument("lyrics", type=Path, help="Path to a lyrics .json")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=320)
    ap.add_argument("--font-size", type=int, default=44)
    ap.add_argument("--field", choices=("jp", "rm", "en"), default="jp")
    ap.add_argument("--start-at", type=float, default=0.0,
                    help="Skip ahead S seconds into the song")
    ap.add_argument("--capture", nargs=2, metavar=("SECONDS", "OUT"),
                    help="Grab one window screenshot at SECONDS s, save as OUT.png")
    ap.add_argument("--opaque-bg", action="store_true",
                    help="Debug: fill the window with a solid dark color instead "
                         "of transparent, so the rendered text is unambiguous.")
    args = ap.parse_args()

    capture_at = float(args.capture[0]) if args.capture else None
    capture_out = args.capture[1] if args.capture else None
    self_test(args.lyrics, args.width, args.height, args.font_size, args.field,
              args.start_at, capture_at, capture_out, opaque_bg=args.opaque_bg)


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("Windows-only (DWM + win32 ex-styles).")
    main()
