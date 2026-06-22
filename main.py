"""
Desktop Karaoke — Transparent karaoke-style Japanese lyrics overlay.

Reads the REAL playback position from Windows Media Transport Controls
(works for Spotify, YouTube in any browser, etc.), then shows synced
lyrics with furigana over kanji, romaji, and English — with a karaoke
fill that sweeps across each line at singing speed.

Lyrics for unknown songs are auto-fetched from LRCLIB and annotated.

Usage:
    pythonw main.py                 Start (auto-detects whatever is playing)
    python  main.py --offset -1.5   Nudge sync (seconds) for video intros
"""

import asyncio
import ctypes
from ctypes import wintypes
import json
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(0)
except Exception:
    pass

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pystray", "Pillow"])
    import pystray
    from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).parent
LYRICS_DIR = BASE / "lyrics"

# ── Palette (21st.dev-inspired: clean, vivid, no muddy purple) ────────
TRANSPARENT = "#0d0b14"   # dark chroma key → anti-aliased edges fade to shadow
INK         = "#000000"   # outline / drop-shadow
WHITE       = "#f8fafc"   # unsung kanji
SUNG        = "#fcd34d"   # karaoke fill (warm amber) sweeping at singing speed
FURI_C      = "#7dd3fc"   # furigana (sky)
ROMAJI_C    = "#5eead4"   # romaji (teal)
EN_C        = "#e2e8f0"   # english (slate)
DIM         = "#9aa4b2"   # hints

JP_FONT     = ("Yu Gothic UI", 38, "bold")
FURI_FONT   = ("Yu Gothic UI", 17)
ROMAJI_FONT = ("Segoe UI Semibold", 23)
EN_FONT     = ("Segoe UI", 21)
HINT_FONT   = ("Segoe UI", 15)

_FURI_RE = re.compile(r"([一-鿿㐀-䶿々][一-鿿㐀-䶿々ぁ-ゖァ-ヺー]*)\(([ぁ-ゖァ-ヺー]+)\)")
PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.Playing

BROWSER_HINTS = ("youtube", "brave", "chrome", "msedge", "edge", "firefox", "opera", "mozilla")


# ── Real playback position via Windows Media Transport Controls ───────

class MediaWatcher:
    """Polls the OS media session in a background thread."""

    def __init__(self):
        self._state = None
        self._lock = threading.Lock()
        self._stop = False
        self.error = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            asyncio.run(self._loop())
        except Exception as e:
            self.error = str(e)

    async def _loop(self):
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as MM,
        )
        while not self._stop:
            try:
                mgr = await MM.request_async()
                sess = self._pick(mgr)
                if sess:
                    info = await sess.try_get_media_properties_async()
                    tl = sess.get_timeline_properties()
                    pb = sess.get_playback_info()
                    status = pb.playback_status
                    pos = tl.position.total_seconds()
                    try:
                        lu = tl.last_updated_time
                        if status == PLAYING and lu.year > 1:
                            pos += (datetime.now(timezone.utc) - lu).total_seconds()
                    except Exception:
                        pass
                    st = {
                        "title": info.title or "",
                        "artist": info.artist or "",
                        "status": status,
                        "position": max(0.0, pos),
                        "duration": tl.end_time.total_seconds(),
                        "source": (sess.source_app_user_model_id or "").lower(),
                        "ts": time.time(),
                    }
                    with self._lock:
                        self._state = st
                else:
                    with self._lock:
                        self._state = None
            except Exception:
                pass
            await asyncio.sleep(0.25)

    @staticmethod
    def _pick(mgr):
        try:
            sessions = list(mgr.get_sessions())
        except Exception:
            sessions = []
        cur = mgr.get_current_session()
        try:
            if cur and cur.get_playback_info().playback_status == PLAYING:
                return cur
        except Exception:
            pass
        for s in sessions:
            try:
                if s.get_playback_info().playback_status == PLAYING:
                    return s
            except Exception:
                continue
        return cur

    def get(self):
        with self._lock:
            if not self._state:
                return None
            s = dict(self._state)
        if s["status"] == PLAYING:
            s["position"] += time.time() - s["ts"]
        return s

    def stop(self):
        self._stop = True


def clean_title(title, source=""):
    t = title
    if any(h in source for h in BROWSER_HINTS):
        t = re.sub(r"\s*[-–—|]\s*YouTube\s*$", "", t, flags=re.I)
    t = re.sub(r"\s*[\[(【「『].*?[\])】」』]", "", t)
    t = re.sub(
        r"\b(Official\s*(Music\s*)?(Video|Audio)|Music\s*Video|MV|PV|"
        r"Lyric\s*Video|Audio|HD|4K|FULL|Full\s*Ver\.?)\b",
        "", t, flags=re.I,
    )
    return t.strip(" -–—|/　").strip()


# ── Lyrics data ──────────────────────────────────────────────────────

@dataclass
class Line:
    start: float
    end: float
    jp: str = ""
    rm: str = ""
    en: str = ""


def load_lyrics(path):
    data = json.loads(Path(path).read_text("utf-8"))
    meta = data.get("meta", {})
    lines = [
        Line(start=e["t"][0], end=e["t"][1],
             jp=e.get("jp", ""), rm=e.get("rm", ""), en=e.get("en", ""))
        for e in data.get("lines", [])
    ]
    return meta, lines


def split_furigana(text):
    parts, last = [], 0
    for m in _FURI_RE.finditer(text):
        if m.start() > last:
            parts.append((text[last:m.start()], ""))
        parts.append((m.group(1), m.group(2)))
        last = m.end()
    if last < len(text):
        parts.append((text[last:], ""))
    return parts


def find_lyrics_for_track(artist, title):
    LYRICS_DIR.mkdir(exist_ok=True)
    query = f"{artist} {title}".lower()
    best = None
    for path in LYRICS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text("utf-8"))
            meta = data.get("meta", {})
            lt = meta.get("title", "").lower()
            la = meta.get("artist", "").lower()
            core = re.sub(r"\s*[\(（].*?[\)）]", "", lt).strip()
            if lt and lt in query:
                return path
            if core and len(core) > 2 and core in query:
                return path
            if title and core and core in title.lower():
                best = best or path
            if la and la in query and any(
                w in query for w in re.findall(r"[\w぀-鿿]{2,}", core)
            ):
                best = best or path
        except Exception:
            continue
    return best


# ── Rendering ────────────────────────────────────────────────────────

def draw_text(cv, x, y, text, font, fill, anchor="center", tags="cur"):
    """Crisp text with a thin outline + soft drop shadow. Returns fill id."""
    for dx, dy in [(2, 3), (3, 2), (2, 2)]:                 # drop shadow
        cv.create_text(x + dx, y + dy, text=text, font=font,
                       fill=INK, anchor=anchor, tags=tags)
    for dx, dy in [(-1, -1), (1, -1), (-1, 1), (1, 1),      # 1px outline
                   (-1, 0), (1, 0), (0, -1), (0, 1)]:
        cv.create_text(x + dx, y + dy, text=text, font=font,
                       fill=INK, anchor=anchor, tags=tags)
    return cv.create_text(x, y, text=text, font=font, fill=fill,
                          anchor=anchor, tags=tags)


def measure_text(cv, text, font):
    tid = cv.create_text(-9999, -9999, text=text, font=font, anchor="nw")
    bbox = cv.bbox(tid)
    cv.delete(tid)
    return (bbox[2] - bbox[0]) if bbox else 0


# ── Overlay ──────────────────────────────────────────────────────────

class Overlay:
    def __init__(self, offset=0.0):
        self.root = tk.Tk()
        self.root.title("Desktop Karaoke")
        self.offset = offset

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.W, self.H = sw, 340

        self.root.overrideredirect(True)
        self.root.geometry(f"{self.W}x{self.H}+0+{sh - self.H - 40}")
        self.root.configure(bg=TRANSPARENT)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", TRANSPARENT)
        self.root.update_idletasks()

        hwnd = ctypes.windll.user32.GetAncestor(self.root.winfo_id(), 2) \
            or self.root.winfo_id()
        ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        ex |= 0x08000000 | 0x00000080  # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, ex)

        self.cv = tk.Canvas(self.root, bg=TRANSPARENT, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)

        self.lines: list[Line] = []
        self.meta: dict = {}
        self.idx = -1
        self._track = None
        self._lyrics_path = None
        self._kara = []
        self._line_left = self._line_right = 0
        self._fetch_key = None
        self._fetch_result = None
        self._translate_result = None
        self._translating = None

        self.media = MediaWatcher()
        self._hint("Waiting for music…")
        self.root.after(300, self._tick)

    # ── per-track ──

    def _on_track_change(self, track):
        artist, title = track
        if not artist and " - " in title:
            a, t = title.split(" - ", 1)
            artist, title = a.strip(), t.strip()

        path = find_lyrics_for_track(artist, title)
        if path:
            if path != self._lyrics_path:
                self.load(path)
            return

        self.lines, self._lyrics_path, self.idx = [], None, -1
        self._kara = []
        self._hint(f"♪ {title} — fetching lyrics…")
        self._start_fetch(artist, title)

    def _start_fetch(self, artist, title):
        key = (artist, title)
        if self._fetch_key == key:
            return
        self._fetch_key = key

        def work():
            try:
                from fetch_lyrics import fetch_and_save
                p = fetch_and_save(title, artist, translate=False, interactive=False)
            except Exception:
                p = None
            self._fetch_result = (key, p)

        threading.Thread(target=work, daemon=True).start()

    def _start_translate(self, path):
        if self._translating == path:
            return
        self._translating = path

        def work():
            ok = False
            try:
                from fetch_lyrics import translate_file
                ok = translate_file(path)
            except Exception:
                ok = False
            self._translate_result = (path, ok)

        threading.Thread(target=work, daemon=True).start()

    def _consume_async(self):
        if self._fetch_result:
            key, p = self._fetch_result
            self._fetch_result = None
            if key == self._fetch_key:
                if p:
                    self.load(Path(p))
                    self._start_translate(Path(p))
                else:
                    self._hint("No synced lyrics found for this song")
        if self._translate_result:
            path, ok = self._translate_result
            self._translate_result = None
            if ok and Path(path) == Path(self._lyrics_path or ""):
                self.load(path, keep_idx=True)

    def load(self, path, keep_idx=False):
        self.meta, self.lines = load_lyrics(path)
        self._lyrics_path = Path(path)
        if not keep_idx:
            self.idx = -1
            self._kara = []

    # ── main loop ──

    def _tick(self):
        self._consume_async()
        state = self.media.get()

        if not state or not state["title"]:
            if self._track is not None:
                self._track = None
                self._hint("Waiting for music…")
            self.root.after(120, self._tick)
            return

        track = (state["artist"], clean_title(state["title"], state["source"]))
        if track != self._track:
            self._track = track
            self._on_track_change(track)

        if state["status"] != PLAYING or not self.lines:
            self.root.after(80, self._tick)   # frozen while paused — no advancing
            return

        pos = state["position"] + self.offset
        new = -1
        for i, ln in enumerate(self.lines):
            if ln.start <= pos < ln.end:
                new = i
                break

        if new != self.idx:
            self.idx = new
            if new >= 0:
                self._render(self.lines[new])
            else:
                self.cv.delete("all")
                self._kara = []
        elif new >= 0:
            self._karaoke(pos)

        self.root.after(33, self._tick)

    # ── drawing ──

    def _render(self, ln):
        self.cv.delete("all")
        self._kara = []
        pad = 64
        furi_y, main_y, romaji_y, en_y = 52, 102, 182, 242

        if ln.jp:
            cx = pad
            for base, reading in split_furigana(ln.jp):
                seg_start = cx
                for ch in base:
                    w = measure_text(self.cv, ch, JP_FONT)
                    if w <= 0:
                        continue
                    cxc = cx + w / 2
                    fid = draw_text(self.cv, cxc, main_y, ch, JP_FONT, WHITE)
                    self._kara.append({"cx": cxc, "fill": fid, "last": WHITE})
                    cx += w
                if reading:
                    draw_text(self.cv, (seg_start + cx) / 2, furi_y,
                              reading, FURI_FONT, FURI_C)
                cx += 6
            self._line_left, self._line_right = pad, cx

        if ln.rm:
            draw_text(self.cv, pad, romaji_y, ln.rm, ROMAJI_FONT, ROMAJI_C, anchor="w")
        if ln.en:
            draw_text(self.cv, pad, en_y, ln.en, EN_FONT, EN_C, anchor="w")

    def _karaoke(self, pos):
        if not self._kara:
            return
        ln = self.lines[self.idx]
        dur = ln.end - ln.start
        if dur <= 0:
            return
        frac = max(0.0, min(1.0, (pos - ln.start) / dur))
        sweep = self._line_left + frac * (self._line_right - self._line_left)
        for k in self._kara:
            col = SUNG if k["cx"] <= sweep else WHITE
            if k["last"] != col:
                self.cv.itemconfig(k["fill"], fill=col)
                k["last"] = col

    def _hint(self, msg):
        self.cv.delete("all")
        self._kara = []
        draw_text(self.cv, 64, self.H // 2, msg, HINT_FONT, DIM, anchor="w")

    # ── tray hooks ──

    def nudge(self, d):
        self.offset += d

    def reset_offset(self):
        self.offset = 0.0

    def toggle(self):
        if self.root.winfo_viewable():
            self.root.withdraw()
        else:
            self.root.deiconify()
            self.root.attributes("-topmost", True)

    def refetch(self):
        self._fetch_key = None
        self._lyrics_path = None
        if self._track:
            self._on_track_change(self._track)

    def quit(self):
        self.media.stop()
        self.root.quit()

    def run(self):
        self.root.mainloop()


# ── Tray icon ────────────────────────────────────────────────────────

def make_icon():
    ico = BASE / "icon.ico"
    if ico.exists():
        return Image.open(ico)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=14, fill="#7c3aed")
    try:
        f = ImageFont.truetype("segoeui.ttf", 22)
    except OSError:
        f = ImageFont.load_default()
    bbox = d.textbbox((0, 0), "あ", font=f)
    d.text(((64 - (bbox[2] - bbox[0])) // 2, 16), "あ", fill="white", font=f)
    return img


def main():
    offset = 0.0
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--offset" and i + 1 < len(args):
            try:
                offset = float(args[i + 1])
            except ValueError:
                pass

    LYRICS_DIR.mkdir(exist_ok=True)
    ov = Overlay(offset=offset)

    def _n_plus(*_):  ov.root.after(0, lambda: ov.nudge(+0.3))
    def _n_minus(*_): ov.root.after(0, lambda: ov.nudge(-0.3))
    def _reset(*_):   ov.root.after(0, ov.reset_offset)
    def _toggle(*_):  ov.root.after(0, ov.toggle)
    def _refetch(*_): ov.root.after(0, ov.refetch)
    def _quit(icon, *_):
        icon.stop()
        ov.root.after(0, ov.quit)

    menu = pystray.Menu(
        pystray.MenuItem("Sync  +0.3s  (lyrics earlier)", _n_plus),
        pystray.MenuItem("Sync  −0.3s  (lyrics later)", _n_minus),
        pystray.MenuItem("Reset sync", _reset),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Re-fetch lyrics", _refetch),
        pystray.MenuItem("Show / Hide", _toggle),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _quit),
    )
    icon = pystray.Icon("desktop-karaoke", make_icon(), "Desktop Karaoke", menu)
    threading.Thread(target=icon.run, daemon=True).start()
    ov.run()


if __name__ == "__main__":
    main()
