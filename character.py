"""
Optional on-screen dancing character.

A small, always-on-top, transparent companion that bobs and sways while music
plays and is themed to the *detected song's main artist*. Toggle it on/off from
the tray ("Dancing character"). It's draggable (click-drag to move) and you can
click it to make it jump.

WHY A PROCEDURAL AVATAR (and how to use real VTuber models)
-----------------------------------------------------------
The request was for high-quality models of specific VTuber groups (ReGLOSS,
V.W.P, hololive, …). Those models are **copyrighted** and are not freely
downloadable or redistributable, so the app cannot ship them. Instead this draws
a lightweight, artist-themed chibi that dances — and it's structured so a real
model can be dropped in:

  • Put a static image at  characters/<artist-slug>.png  (or .gif) and it is
    used instead of the drawn avatar, bobbing/sway/jump still applied.
  • For a fully rigged, bone-animated avatar, a VRM (3D) or Live2D model would
    need a real rendering engine (three-vrm / Live2D Cubism) in a webview — that
    is a much larger dependency and is intentionally left as a future path. See
    docs/RESEARCH.md.

The dance here is time-based (a steady, lively rhythm while the song plays) so it
never competes with Shazam for the audio device.
"""
from __future__ import annotations

import math
import re
import time
import tkinter as tk
from pathlib import Path

TRANSPARENT = "#0d0b14"          # same chroma key as the lyric overlay
_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    return _SLUG.sub("-", (name or "").lower()).strip("-")


def _theme(name: str):
    """Stable accent + skin colors derived from the artist name (so each artist
    gets a recognizable palette without shipping their art)."""
    h = 0
    for c in name or "?":
        h = (h * 131 + ord(c)) & 0xFFFFFFFF
    hue = (h % 360) / 360.0
    body = _hsl(hue, 0.62, 0.55)
    dark = _hsl(hue, 0.62, 0.42)
    hair = _hsl((hue + 0.5) % 1.0, 0.55, 0.62)
    return body, dark, hair


def _hsl(h, s, l):
    def f(n):
        k = (n + h * 12) % 12
        a = s * min(l, 1 - l)
        return l - a * max(-1, min(k - 3, 9 - k, 1))
    return "#%02x%02x%02x" % (round(f(0) * 255), round(f(8) * 255), round(f(4) * 255))


class Character:
    """A draggable, always-on-top dancing companion. One per app; created hidden
    and shown only when enabled."""

    W, H = 200, 280

    def __init__(self, root: tk.Tk, data_dir: Path):
        self.root = root
        self.data_dir = data_dir
        self.enabled = False
        self.playing = False
        self.artist = ""
        self._img = None            # optional PhotoImage for a user-supplied model
        self._t0 = time.time()
        self._jump = 0.0            # decays after a click
        self._drag = None

        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.configure(bg=TRANSPARENT)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", TRANSPARENT)
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        self.win.geometry(f"{self.W}x{self.H}+{sw - self.W - 40}+{sh - self.H - 120}")
        self.cv = tk.Canvas(self.win, width=self.W, height=self.H,
                            bg=TRANSPARENT, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        # interaction: drag to move, click to jump
        self.cv.bind("<Button-1>", self._press)
        self.cv.bind("<B1-Motion>", self._move)
        self.cv.bind("<ButtonRelease-1>", self._release)

    # ── public API ──
    def set_enabled(self, on: bool):
        """Show or hide the companion. When shown, starts its animation loop."""
        self.enabled = bool(on)
        if self.enabled:
            self.win.deiconify()
            self.win.attributes("-topmost", True)
            self._loop()
        else:
            self.win.withdraw()

    def set_artist(self, name: str):
        """Theme the companion to a new artist (re-derives colors and looks for a
        user-supplied characters/<artist>.png to use instead of the drawing)."""
        if name == self.artist:
            return
        self.artist = name or ""
        self._img = self._load_model(self.artist)

    def set_playing(self, playing: bool):
        """Tell the companion whether music is playing (drives the dance vs idle
        animation). Called every frame by the overlay."""
        self.playing = bool(playing)

    def destroy(self):
        """Tear down the companion window (ignored if already gone)."""
        try:
            self.win.destroy()
        except Exception:
            pass

    # ── interaction ──
    def _press(self, e):
        """Start a drag: record the pointer + window origin so _move can follow."""
        self._drag = (e.x_root, e.y_root, self.win.winfo_x(), self.win.winfo_y())
        self._moved = False

    def _move(self, e):
        """Drag the window with the pointer (and remember that it moved, so a
        release isn't treated as a click)."""
        if not self._drag:
            return
        dx, dy = e.x_root - self._drag[0], e.y_root - self._drag[1]
        if abs(dx) + abs(dy) > 3:
            self._moved = True
        self.win.geometry(f"+{self._drag[2] + dx}+{self._drag[3] + dy}")

    def _release(self, e):
        """End a drag; a release with no movement counts as a click → hop."""
        if self._drag and not self._moved:
            self._jump = 1.0            # a click (not a drag) makes it hop
        self._drag = None

    # ── optional user-supplied model image ──
    def _load_model(self, artist: str):
        """Return a PhotoImage for characters/<artist>.png|.gif if the user has
        supplied one, else None (fall back to the drawn avatar)."""
        folder = self.data_dir / "characters"
        for ext in (".png", ".gif"):
            p = folder / f"{_slug(artist)}{ext}"
            if p.exists():
                try:
                    from PIL import Image, ImageTk
                    im = Image.open(p).convert("RGBA")
                    im.thumbnail((self.W - 20, self.H - 60))
                    return ImageTk.PhotoImage(im)
                except Exception:
                    return None
        return None

    # ── animation ──
    def _loop(self):
        if not self.enabled:
            return
        self._draw()
        # ~30fps while dancing, ~10fps idle (saves CPU when paused/stopped)
        self.root.after(33 if self.playing else 100, self._loop)

    def _draw(self):
        cv = self.cv
        cv.delete("all")
        t = time.time() - self._t0
        body, dark, hair = _theme(self.artist or "Lyric Immersion and Karaoke")
        cx = self.W / 2

        # dance: lively bob + sway while playing; gentle idle otherwise
        bpm = 2.0 if self.playing else 0.7
        phase = t * bpm * 2 * math.pi
        amp = 16 if self.playing else 4
        self._jump = max(0.0, self._jump - 0.05)
        bob = -abs(math.sin(phase)) * amp - self._jump * 60
        sway = math.sin(phase / 2) * (10 if self.playing else 3)
        base_y = self.H - 70 + bob
        lean = sway * 0.4

        if self._img is not None:                      # user-supplied model
            cv.create_image(cx + sway, base_y - self._img.height() / 2 + 20,
                            image=self._img)
        else:
            self._draw_chibi(cx + sway, base_y, lean, phase, body, dark, hair)

        # artist name plate
        name = self.artist or "♪"
        cv.create_text(cx + 1, self.H - 22 + 1, text=name, fill="#000000",
                       font=("Segoe UI Semibold", 12), width=self.W - 8)
        cv.create_text(cx, self.H - 22, text=name, fill="#e2e8f0",
                       font=("Segoe UI Semibold", 12), width=self.W - 8)

    def _draw_chibi(self, cx, base_y, lean, phase, body, dark, hair):
        cv = self.cv
        # ── kawaii maneki-neko (招き猫) ──
        # big head : small body ratio for maximum cuteness
        WHITE = "#fefcf9"
        CREAM = "#fff5e6"
        OL = "#5c4033"
        RED = "#e63946"
        GOLD = "#f0c040"
        GOLD_D = "#b8860b"
        PINK = "#ffaec0"
        BLUSH = "#ffc0cb"
        S1, S2 = body, dark       # artist-themed calico spots

        # anchor: head center high, body below — head is 60% of figure
        hx = cx + lean * 0.25
        hy = base_y - 52

        # ── stubby tail (behind body) ──
        tw = math.sin(phase * 1.8) * (10 if self.playing else 3)
        cv.create_line(cx + 18, base_y + 8, cx + 30 + tw, base_y - 6,
                       cx + 26 + tw * 0.6, base_y - 18,
                       fill=S1, width=8, capstyle="round", smooth=True)

        # ── body (small, round, sits below the big head) ──
        cv.create_oval(cx - 24, base_y - 16, cx + 24, base_y + 26,
                       fill=WHITE, outline=OL, width=2)
        # calico spot on tummy
        cv.create_oval(cx - 14, base_y - 4, cx + 4, base_y + 16,
                       fill=S1, outline="")

        # ── little feet ──
        for sgn in (-1, 1):
            fx = cx + sgn * 14
            cv.create_oval(fx - 7, base_y + 18, fx + 7, base_y + 30,
                           fill=WHITE, outline=OL, width=1.5)
            cv.create_oval(fx - 3, base_y + 22, fx + 3, base_y + 27,
                           fill=PINK, outline="")

        # ── left paw (resting on tummy, holds koban) ──
        lx, ly = cx - 16, base_y + 2
        cv.create_oval(lx - 8, ly, lx + 8, ly + 12,
                       fill=WHITE, outline=OL, width=1.5)
        cv.create_oval(lx - 6, ly + 1, lx + 6, ly + 11,
                       fill=GOLD, outline=GOLD_D, width=1)
        cv.create_text(lx, ly + 6, text="福", fill=GOLD_D,
                       font=("Yu Gothic", 6, "bold"))

        # ── right paw (beckoning wave!) ──
        wave = math.sin(phase * (3.2 if self.playing else 1.0))
        rx = cx + 20
        # arm — simple rounded rectangle, no ugly triple-line hack
        ay_top = base_y - 10
        ay_end = base_y - 38 - (6 if self.playing else 0)
        cv.create_oval(rx - 7, ay_end - 4, rx + 9, ay_top + 4,
                       fill=WHITE, outline=OL, width=1.5)
        # paw circle at the end (tilts with wave)
        px = rx + 1 + wave * 5
        py = ay_end - 2
        cv.create_oval(px - 7, py - 7, px + 7, py + 7,
                       fill=WHITE, outline=OL, width=1.5)
        # paw pad
        cv.create_oval(px - 3, py - 2, px + 3, py + 3,
                       fill=PINK, outline="")

        # ── collar (red band across body-head junction) ──
        cv.create_arc(cx - 22, base_y - 22, cx + 22, base_y - 6,
                      start=200, extent=140, style="arc",
                      outline=RED, width=4)
        # bell
        cv.create_oval(cx - 5, base_y - 16, cx + 5, base_y - 6,
                       fill=GOLD, outline=GOLD_D, width=1)
        cv.create_line(cx, base_y - 12, cx, base_y - 7,
                       fill=GOLD_D, width=1)

        # ── head (BIG — the cute factor) ──
        cv.create_oval(hx - 36, hy - 32, hx + 36, hy + 30,
                       fill=WHITE, outline=OL, width=2)

        # calico spot on forehead
        cv.create_oval(hx + 4, hy - 26, hx + 24, hy - 10,
                       fill=S1, outline="")

        # ── ears (rounded triangles, big) ──
        for sgn in (-1, 1):
            ex = hx + sgn * 26
            cv.create_polygon(ex - 12, hy - 24, ex + 12, hy - 24,
                              ex + sgn * 3, hy - 52,
                              fill=WHITE, outline=OL, width=2)
            cv.create_polygon(ex - 7, hy - 26, ex + 7, hy - 26,
                              ex + sgn * 2, hy - 43,
                              fill=PINK, outline="")
        # calico spot on right ear
        cv.create_polygon(hx + 22, hy - 26, hx + 32, hy - 26,
                          hx + 29, hy - 40, fill=S2, outline="")

        # ── eyes (big happy crescents ◠◠) ──
        for sgn in (-1, 1):
            ex = hx + sgn * 14
            # thick happy arc — the signature kawaii expression
            cv.create_arc(ex - 8, hy - 6, ex + 8, hy + 10,
                          start=0, extent=180,
                          style="arc", outline=OL, width=3)

        # ── nose (tiny pink bean) ──
        cv.create_oval(hx - 3, hy + 10, hx + 3, hy + 15,
                       fill=PINK, outline="")

        # ── mouth (‿ smile) ──
        cv.create_arc(hx - 7, hy + 12, hx + 7, hy + 22,
                      start=200, extent=140,
                      style="arc", outline=OL, width=1.5)

        # ── whiskers (delicate) ──
        for sgn in (-1, 1):
            for dy in (-2, 3):
                cv.create_line(hx + sgn * 20, hy + 10 + dy,
                               hx + sgn * 40, hy + 8 + dy * 1.5,
                               fill=OL, width=0.8)

        # ── cheek blush (soft pink circles) ──
        for sgn in (-1, 1):
            bx = hx + sgn * 22
            cv.create_oval(bx - 7, hy + 6, bx + 7, hy + 16,
                           fill=BLUSH, outline="", stipple="gray50")

        # ── music notes while playing ──
        if self.playing:
            for k in range(2):
                nx = cx + 44 + 8 * math.sin(phase + k * 1.5)
                ny = hy - 20 - ((phase * 7 + k * 28) % 55)
                cv.create_text(nx, ny, text="♪", fill=GOLD,
                               font=("Segoe UI", 14, "bold"))
