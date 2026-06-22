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
        self.enabled = bool(on)
        if self.enabled:
            self.win.deiconify()
            self.win.attributes("-topmost", True)
            self._loop()
        else:
            self.win.withdraw()

    def set_artist(self, name: str):
        if name == self.artist:
            return
        self.artist = name or ""
        self._img = self._load_model(self.artist)

    def set_playing(self, playing: bool):
        self.playing = bool(playing)

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass

    # ── interaction ──
    def _press(self, e):
        self._drag = (e.x_root, e.y_root, self.win.winfo_x(), self.win.winfo_y())
        self._moved = False

    def _move(self, e):
        if not self._drag:
            return
        dx, dy = e.x_root - self._drag[0], e.y_root - self._drag[1]
        if abs(dx) + abs(dy) > 3:
            self._moved = True
        self.win.geometry(f"+{self._drag[2] + dx}+{self._drag[3] + dy}")

    def _release(self, e):
        if self._drag and not self._moved:
            self._jump = 1.0            # a click (not a drag) makes it hop
        self._drag = None

    # ── optional user-supplied model image ──
    def _load_model(self, artist: str):
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
        self.root.after(33, self._loop)     # ~30 fps

    def _draw(self):
        cv = self.cv
        cv.delete("all")
        t = time.time() - self._t0
        body, dark, hair = _theme(self.artist or "Desktop Karaoke")
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
        # legs (alternating step)
        step = math.sin(phase) * (12 if self.playing else 2)
        for sgn in (-1, 1):
            cv.create_line(cx + sgn * 10, base_y, cx + sgn * 14 + sgn * step,
                           base_y + 46, fill=dark, width=10, capstyle="round")
        # body
        cv.create_oval(cx - 28, base_y - 50, cx + 28, base_y + 14,
                       fill=body, outline=dark, width=2)
        # arms (swing opposite to legs; raise while playing)
        raise_a = -26 if self.playing else -6
        for sgn in (-1, 1):
            ax = math.sin(phase + math.pi) * sgn * 14
            cv.create_line(cx + sgn * 22, base_y - 34,
                           cx + sgn * 40 + ax, base_y + raise_a + ax,
                           fill=body, width=9, capstyle="round")
        # head
        hy = base_y - 78 + lean * 0.2
        cv.create_oval(cx - 30 + lean, hy - 30, cx + 30 + lean, hy + 30,
                       fill=hair, outline=dark, width=2)
        cv.create_oval(cx - 24 + lean, hy - 18, cx + 24 + lean, hy + 26,
                       fill="#ffe8d6", outline="")
        # eyes
        for sgn in (-1, 1):
            cv.create_oval(cx + sgn * 11 - 4 + lean, hy - 2, cx + sgn * 11 + 4 + lean,
                           hy + 8, fill="#1f2937", outline="")
        # cheeks + smile
        cv.create_arc(cx - 10 + lean, hy + 4, cx + 10 + lean, hy + 20,
                      start=200, extent=140, style="arc", outline=dark, width=2)
        # floating music notes while playing
        if self.playing:
            for k in range(2):
                nx = cx + 44 + 10 * math.sin(phase + k)
                ny = hy - 10 - ((phase * 8 + k * 30) % 60)
                cv.create_text(nx, ny, text="♪", fill=body,
                               font=("Segoe UI", 16, "bold"))
