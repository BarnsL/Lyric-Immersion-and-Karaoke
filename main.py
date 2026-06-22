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
import os
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
# When packaged as an .exe, keep the writable library/settings in the user's
# AppData (the exe's own folder may be read-only / a temp extract dir).
if getattr(sys, "frozen", False):
    _DATA = Path(os.environ.get("APPDATA", str(Path.home()))) / "Desktop Karaoke"
else:
    _DATA = BASE
_DATA.mkdir(parents=True, exist_ok=True)
LYRICS_DIR = _DATA / "lyrics"
SETTINGS = _DATA / "settings.json"


def _resource(name):
    """Path to a bundled read-only resource (icon), frozen or not."""
    return Path(getattr(sys, "_MEIPASS", BASE)) / name


def _load_settings():
    try:
        return json.loads(SETTINGS.read_text("utf-8"))
    except Exception:
        return {}


def _save_settings(data):
    try:
        SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Start-with-Windows (Startup-folder shortcut) ─────────────────────

def _startup_lnk():
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "Startup" / "Desktop Karaoke.lnk")


def startup_enabled():
    return _startup_lnk().exists()


def _psq(s):                       # quote a string for PowerShell
    return "'" + str(s).replace("'", "''") + "'"


def set_startup(enable):
    lnk = _startup_lnk()
    if not enable:
        try:
            lnk.unlink()
        except Exception:
            pass
        return
    if getattr(sys, "frozen", False):          # packaged .exe
        target, args = sys.executable, ""
    else:                                      # running from source
        target = str(Path(sys.executable).with_name("pythonw.exe"))
        args = f'"{BASE / "main.py"}"'
    ps = (f"$W=New-Object -ComObject WScript.Shell;"
          f"$S=$W.CreateShortcut({_psq(lnk)});"
          f"$S.TargetPath={_psq(target)};$S.Arguments={_psq(args)};"
          f"$S.WorkingDirectory={_psq(BASE)};$S.IconLocation={_psq(BASE / 'icon.ico')};"
          f"$S.Save()")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=15)
    except Exception:
        pass

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
SCROLL_SPEED = 220.0  # px/sec — constant, comfortable scroll-through pace

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
            await asyncio.sleep(0.1)

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


_TS_RE = re.compile(r"\[\d+:\d+(?:\.\d+)?\]|<\d+:\d+(?:\.\d+)?>")


def _clean(s):
    return _TS_RE.sub("", s).strip()


def load_lyrics(path):
    data = json.loads(Path(path).read_text("utf-8"))
    meta = data.get("meta", {})
    lines = [
        Line(start=e["t"][0], end=e["t"][1],
             jp=_clean(e.get("jp", "")), rm=_clean(e.get("rm", "")),
             en=_clean(e.get("en", "")))
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


class LyricsIndex:
    """In-memory index of cached lyrics → millisecond matching, with
    duration-based rejection so a same-titled wrong file isn't used."""

    def __init__(self):
        self.entries = []
        self.refresh()

    def refresh(self):
        LYRICS_DIR.mkdir(exist_ok=True)
        entries = []
        for p in LYRICS_DIR.glob("*.json"):
            try:
                m = json.loads(p.read_text("utf-8")).get("meta", {})
            except Exception:
                continue
            lt = (m.get("title") or "").lower()
            entries.append({
                "path": p,
                "title": lt,
                "core": re.sub(r"\s*[\(（].*?[\)）]", "", lt).strip(),
                "dur": m.get("duration"),
            })
        self.entries = entries

    def add(self, path):
        path = Path(path)
        self.entries = [e for e in self.entries if e["path"] != path]
        self.refresh()

    def match(self, artist, title, duration=None):
        query = f"{artist} {title}".lower()
        tl = (title or "").lower()
        fallback = None
        for e in self.entries:
            lt, core = e["title"], e["core"]
            hit = (
                (lt and lt in query)
                or (core and len(core) > 2 and core in query)
                or (core and len(core) > 2 and core in tl)
            )
            if not hit:
                continue
            # duration guard: same title but clearly different length → skip
            if duration and e["dur"] and abs(e["dur"] - duration) > 12:
                fallback = fallback  # keep looking for a better-length match
                continue
            return e["path"]
        return fallback


# ── Rendering ────────────────────────────────────────────────────────

# Outline offsets per glyph — fewer = faster (less rasterization per frame).
_OUTLINE_FULL = ((-2, -2), (2, -2), (-2, 2), (2, 2))   # 5 items/char
_OUTLINE_LITE = ((2, 2),)                              # 2 items/char (perf mode)
_OUTLINE = _OUTLINE_FULL


def draw_text(cv, x, y, text, font, fill, anchor="center", tags="cur"):
    """Outlined text. Returns the fill item id. Outline weight follows the
    current performance mode (_OUTLINE)."""
    for dx, dy in _OUTLINE:
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
        self.W, self.H, self.sh = sw, 340, sh

        s = _load_settings()
        self.opacity = float(s.get("opacity", 1.0))
        self.position = s.get("position", "bottom")   # 'top' | 'bottom'
        self.scroll_dir = s.get("scroll", "left")      # 'none'|'left'|'right'|'lr'|'rl'
        self.scroll_speed = float(s.get("scroll_speed", SCROLL_SPEED))
        self.font_scale = float(s.get("font_scale", 1.0))  # 0.25 … 2.0
        self.perf = s.get("perf", "smooth")            # 'smooth' | 'fast'
        self._fps = 16
        self._last_pos = 0.0
        self._strm_rem = 0.0
        self._apply_perf()
        self._anim_id = None
        self._scroll_x = self._scroll_start = self._scroll_end = 0
        self._stream = []          # scroll-through ticker: live line blocks
        self._blk_seq = 0
        self._apply_scale()                            # sets fonts + layout + H

        self.root.overrideredirect(True)
        self.root.geometry(f"{self.W}x{self.H}+0+{self._geom_y()}")
        self.root.configure(bg=TRANSPARENT)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", TRANSPARENT)
        self.root.attributes("-alpha", self.opacity)
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
        self._cur_duration = None
        self._verified = False
        self._health_attempts = 0
        self._identifying = False
        self._identify_result = None
        self._identified = None      # track tuple we've already sound-checked

        self.index = LyricsIndex()
        self.media = MediaWatcher()
        self._hint("Waiting for music…")
        self.root.after(300, self._tick)
        self.root.after(7000, self._health_check)

    # ── per-track ──

    def _on_track_change(self, track, duration=None):
        artist, title = track
        if not artist and " - " in title:
            a, t = title.split(" - ", 1)
            artist, title = a.strip(), t.strip()
        self._cur_duration = duration
        self._health_attempts = 0

        # Provisional: show the title/artist match instantly (so there's no
        # dead air) — but AUDIO is primary and confirms/overrides it below.
        path = self.index.match(artist, title, duration)
        if path and self._file_valid(path, duration):
            if path != self._lyrics_path:
                self.load(path)
            self._maybe_translate()
        else:
            self.lines, self._lyrics_path, self.idx = [], None, -1
            self._kara = []
            self._verified = False
            self._hint(f"♪ {title} — identifying…")
            self._start_fetch(artist, title, duration)

        # PRIMARY signal: identify by sound and let it decide the real song.
        self._start_identify()

    def _trusted_duration(self, state):
        # YouTube/browser report the VIDEO length (intro/outro) which differs
        # from the audio track — using it to match/verify rejects correct
        # lyrics. Only trust duration from real audio players (Spotify, etc.).
        if any(h in state.get("source", "") for h in BROWSER_HINTS):
            return None
        return state.get("duration")

    def _mark_verified(self):
        md = self.meta.get("duration")
        if self._cur_duration:
            self._verified = bool(md and abs(md - self._cur_duration) <= 12)
        else:
            self._verified = True   # title+language match is the best signal here

    def _file_valid(self, path, duration):
        try:
            from fetch_lyrics import validate_file
            ok, _ = validate_file(path, duration)
            return ok
        except Exception:
            return True

    def _maybe_translate(self):
        # A cached non-English song with no translation yet (e.g. preloaded
        # with English off) gets it filled now and saved — so the local cache
        # is complete and the song is never fetched again.
        if not self.lines or not self._lyrics_path:
            return
        if self.meta.get("lang") not in ("ja", "zh", "ko", "es"):
            return
        have = sum(1 for ln in self.lines if ln.en.strip())
        need = sum(1 for ln in self.lines if ln.jp.strip())
        if need and have < need * 0.5:
            self._start_translate(self._lyrics_path)

    def _start_fetch(self, artist, title, duration=None):
        key = (artist, title)
        if self._fetch_key == key:
            return
        self._fetch_key = key

        def work():
            try:
                from fetch_lyrics import fetch_and_save
                p = fetch_and_save(title, artist, translate=False, duration=duration)
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

    # ── audio identification (detect by SOUND, not title) ──

    def _start_identify(self):
        if self._identifying or self._identified == self._track:
            return                       # one sound-check per track
        self._identifying = True
        self._identified = self._track

        def work():
            res = None
            try:
                from recognize import recognize_playing
                t, a = recognize_playing()
                if t:
                    res = (t, a or "")
            except Exception:
                res = None
            self._identify_result = ("done", res)

        threading.Thread(target=work, daemon=True).start()

    # ── periodic health check: notice a bad match mid-song and self-heal ──

    def _health_check(self):
        try:
            st = self.media.get()
            if (st and st.get("status") == PLAYING and self._track
                    and not self._identifying and self._health_attempts < 4):
                if self._suspect(st):
                    self._health_attempts += 1
                    # Identify by sound — the authoritative correction.
                    self._start_identify()
        finally:
            self.root.after(9000, self._health_check)

    def _suspect(self, st):
        """Signs the current lyrics don't belong to what's actually playing."""
        dur, pos = st.get("duration"), st.get("position", 0)
        if not self.lines:
            # browser/cover with no match yet, and we haven't sound-checked it
            return self._identified != self._track
        md = self.meta.get("duration")
        last_end = self.lines[-1].end if self.lines else 0
        if dur and md and abs(md - dur) > 12:
            return True                                   # wrong version/song
        if dur and last_end and last_end < dur * 0.6 and pos > last_end + 8 \
                and pos < dur - 5:
            return True                                   # lyrics don't cover song
        if not self._verified and self._identified != self._track:
            return True                                   # unverified → confirm by ear
        return False

    def _consume_async(self):
        if self._fetch_result:
            key, p = self._fetch_result
            self._fetch_result = None
            if key == self._fetch_key:
                if p:
                    self.index.add(p)
                    self.load(Path(p))
                    self._start_translate(Path(p))
                elif not self._identifying and self._identified != self._track:
                    # title/artist missed (e.g. name-variant) — Shazam returns
                    # the canonical name, which usually fetches fine.
                    self._hint("🎧 Finding the song by sound…")
                    self._start_identify()
                else:
                    self._hint("No lyrics found for this song")
        if self._translate_result:
            path, ok = self._translate_result
            self._translate_result = None
            if ok and Path(path) == Path(self._lyrics_path or ""):
                self.load(path, keep_idx=True)
        if self._identify_result:
            _, res = self._identify_result
            self._identify_result = None
            self._identifying = False
            if res:
                # AUDIO IS AUTHORITATIVE: fetch the heard song and swap it in,
                # overriding any (possibly wrong) title match. Doesn't re-key
                # self._track, so detection won't loop.
                title, artist = res
                cached = self.index.match(artist, title, self._cur_duration)
                if cached and self._file_valid(cached, self._cur_duration):
                    if cached != self._lyrics_path:
                        self.load(cached)
                    self._maybe_translate()
                else:
                    self._start_fetch(artist, title, self._cur_duration)

    def load(self, path, keep_idx=False):
        self.meta, self.lines = load_lyrics(path)
        self._lyrics_path = Path(path)
        self._mark_verified()
        if not keep_idx:
            self.idx = -1
            self._kara = []
            self._clear_stream()
            self.cv.delete("all")

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
            self._on_track_change(track, self._trusted_duration(state))

        if state["status"] != PLAYING or not self.lines:
            self.root.after(80, self._tick)   # frozen while paused — no advancing
            return

        pos = state["position"] + self.offset

        if self.scroll_dir in ("lr", "rl"):       # continuous scroll-through
            self._ticker_update(pos)
            self.root.after(self._fps, self._tick)
            return

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

        self.root.after(self._fps, self._tick)

    # ── drawing ──

    def _render(self, ln):
        self._cancel_anim()
        self.cv.delete("all")
        self._kara = []   # list of per-line "tracks", each swept in sync
        pad = self.pad

        if ln.jp:
            chars, cx = [], pad
            for base, reading in split_furigana(ln.jp):
                seg_start = cx
                for ch in base:
                    w = measure_text(self.cv, ch, self.JP_FONT)
                    if w <= 0:
                        continue
                    cxc = cx + w / 2
                    fid = draw_text(self.cv, cxc, self.main_y, ch, self.JP_FONT, WHITE)
                    chars.append({"cx": cxc, "fill": fid, "last": WHITE})
                    cx += w
                if reading:
                    draw_text(self.cv, (seg_start + cx) / 2, self.furi_y,
                              reading, self.FURI_FONT, FURI_C)
                cx += 6
            self._kara.append({"chars": chars, "left": pad, "right": cx,
                               "base": WHITE, "sung": SUNG})

        if ln.rm:
            self._kara.append(self._char_track(ln.rm, self.romaji_y, self.ROMAJI_FONT,
                                               ROMAJI_C, SUNG, pad))
        if ln.en:
            self._kara.append(self._char_track(ln.en, self.en_y, self.EN_FONT,
                                               EN_C, SUNG, pad))

        self._animate_in()

    def _char_track(self, text, y, font, base, sung, pad):
        chars, cx = [], pad
        sp = measure_text(self.cv, "n", font) * 0.5 or 6
        for ch in text:
            if ch == " ":
                cx += sp
                continue
            w = measure_text(self.cv, ch, font)
            if w <= 0:
                continue
            cxc = cx + w / 2
            fid = draw_text(self.cv, cxc, y, ch, font, base)
            chars.append({"cx": cxc, "fill": fid, "last": base})
            cx += w
        return {"chars": chars, "left": pad, "right": cx, "base": base, "sung": sung}

    # ── entrance animation (scroll-in from chosen corner) ──

    def _cancel_anim(self):
        if self._anim_id:
            try:
                self.root.after_cancel(self._anim_id)
            except Exception:
                pass
            self._anim_id = None

    def _animate_in(self):
        d = self.scroll_dir
        if d in ("lr", "rl", "none", "off", "stationary"):
            return                               # scroll handled by the ticker
        ox = 460 if d == "right" else -460       # slide in from right / left, once
        self.cv.move("cur", ox, 0)
        self._anim_step(ox, 0)

    # ── continuous scroll-through ticker (multiple lines on screen) ──

    def _render_block(self, i):
        """Draw line i's compact block at x-origin 0, offset into its vertical
        lane so staggered lines don't overlap. Returns the block."""
        ln = self.lines[i]
        self._blk_seq += 1
        tag = f"blk{self._blk_seq}"
        dy = (i % self._lanes) * self._lane_gap
        tracks, right = [], 0
        if ln.jp:
            chars, cx = [], 0
            for base, reading in split_furigana(ln.jp):
                seg = cx
                for ch in base:
                    w = measure_text(self.cv, ch, self.JP_FONT)
                    if w <= 0:
                        continue
                    fid = draw_text(self.cv, cx + w / 2, self.b_main + dy, ch,
                                    self.JP_FONT, WHITE, tags=(tag, "strm"))
                    chars.append({"fill": fid, "last": WHITE})
                    cx += w
                if reading:
                    draw_text(self.cv, (seg + cx) / 2, self.b_furi + dy, reading,
                              self.FURI_FONT, FURI_C, tags=(tag, "strm"))
                cx += 6
            tracks.append({"chars": chars, "base": WHITE, "sung": SUNG})
            right = max(right, cx)
        for text, y, font, col in ((ln.rm, self.b_rom + dy, self.ROMAJI_FONT, ROMAJI_C),
                                   (ln.en, self.b_en + dy, self.EN_FONT, EN_C)):
            if not text:
                continue
            chars, cx = [], 0
            sp = measure_text(self.cv, "n", font) * 0.5 or 6
            for ch in text:
                if ch == " ":
                    cx += sp
                    continue
                w = measure_text(self.cv, ch, font)
                if w <= 0:
                    continue
                fid = draw_text(self.cv, cx + w / 2, y, ch, font, col, tags=(tag, "strm"))
                chars.append({"fill": fid, "last": col})
                cx += w
            tracks.append({"chars": chars, "base": col, "sung": SUNG})
            right = max(right, cx)
        return {"idx": i, "tag": tag, "x": 0.0, "w": right, "tracks": tracks}

    def _highlight_block(self, b, frac):
        for tr in b["tracks"]:
            k = int(frac * len(tr["chars"]) + 0.5)
            for j, c in enumerate(tr["chars"]):
                col = tr["sung"] if j < k else tr["base"]
                if c["last"] != col:
                    self.cv.itemconfig(c["fill"], fill=col)
                    c["last"] = col

    def _ticker_update(self, pos):
        center, v = self.W / 2, self.scroll_speed
        d = 1 if self.scroll_dir == "rl" else -1

        # All blocks share the same per-frame motion, so move the whole stream
        # in ONE call (keeps a sub-pixel remainder so it doesn't drift).
        if self._stream:
            dx_f = -d * v * (pos - self._last_pos) + self._strm_rem
            dx = round(dx_f)
            self._strm_rem = dx_f - dx
            if dx:
                self.cv.move("strm", dx, 0)
        self._last_pos = pos

        want = {}
        for i, ln in enumerate(self.lines):
            cx = center + d * v * ((ln.start + ln.end) / 2 - pos)
            if -1200 < cx < self.W + 1200:
                want[i] = cx
        have = {b["idx"] for b in self._stream}
        for i, cx in want.items():
            if i not in have:                       # spawn at absolute target
                b = self._render_block(i)
                self.cv.move(b["tag"], (cx - b["w"] / 2) - b["x"], 0)
                self._stream.append(b)
        for b in self._stream[:]:
            if b["idx"] not in want:
                self.cv.delete(b["tag"])
                self._stream.remove(b)
                continue
            ln = self.lines[b["idx"]]
            dur = ln.end - ln.start
            if ln.start <= pos < ln.end and dur > 0:
                self._highlight_block(b, (pos - ln.start) / dur)   # current fills
            else:
                self._highlight_block(b, 0.0)

    def _clear_stream(self):
        for b in self._stream:
            self.cv.delete(b["tag"])
        self._stream = []

    def _anim_step(self, ox, step=0):
        steps = 20
        if step >= steps:
            self._anim_id = None
            return
        e0 = 1 - (1 - step / steps) ** 3
        e1 = 1 - (1 - (step + 1) / steps) ** 3
        self.cv.move("cur", -(e1 - e0) * ox, 0)
        self._anim_id = self.root.after(16, self._anim_step, ox, step + 1)

    def _karaoke(self, pos):
        if not self._kara:
            return
        ln = self.lines[self.idx]
        dur = ln.end - ln.start
        if dur <= 0:
            return
        frac = max(0.0, min(1.0, (pos - ln.start) / dur))
        for tr in self._kara:                       # JP, romaji, English in lockstep
            sweep = tr["left"] + frac * (tr["right"] - tr["left"])
            base, sung = tr["base"], tr["sung"]
            for k in tr["chars"]:
                col = sung if k["cx"] <= sweep else base
                if k["last"] != col:
                    self.cv.itemconfig(k["fill"], fill=col)
                    k["last"] = col

    def _hint(self, msg):
        self.cv.delete("all")
        self._kara = []
        self._clear_stream()
        draw_text(self.cv, self.pad, self.H // 2, msg, self.HINT_FONT, DIM, anchor="w")

    # ── tray hooks ──

    def nudge(self, d):
        self.offset += d

    def reset_offset(self):
        self.offset = 0.0

    # ── appearance (persisted) ──

    def _geom_y(self):
        return 40 if self.position == "top" else self.sh - self.H - 40

    def _persist(self):
        _save_settings({"opacity": self.opacity, "position": self.position,
                        "scroll": self.scroll_dir, "font_scale": self.font_scale,
                        "scroll_speed": self.scroll_speed, "perf": self.perf})

    def set_opacity(self, v):
        self.opacity = max(0.15, min(1.0, v))
        self.root.attributes("-alpha", self.opacity)
        self.root.update_idletasks()
        self._persist()

    def set_position(self, p):
        self.position = p
        self.root.geometry(f"{self.W}x{self.H}+0+{self._geom_y()}")
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()   # apply the move immediately
        self._persist()

    def set_scroll(self, d):
        self.scroll_dir = d
        self._apply_scale()                # scroll mode is a taller, laned window
        self.root.geometry(f"{self.W}x{self.H}+0+{self._geom_y()}")
        self.root.attributes("-topmost", True)
        self._cancel_anim()
        self._clear_stream()
        self.cv.delete("all")
        self._kara = []
        self.idx = -1                      # next tick repaints in the new mode
        self.root.update_idletasks()
        self._persist()

    def set_scroll_speed(self, v):
        self.scroll_speed = float(v)
        self._persist()

    def _apply_perf(self):
        global _OUTLINE
        if self.perf == "fast":
            _OUTLINE = _OUTLINE_LITE     # 2 items/char, 30fps → much less to draw
            self._fps = 33
        else:
            _OUTLINE = _OUTLINE_FULL     # 5 items/char, 60fps
            self._fps = 16

    def set_quality(self, mode):
        self.perf = mode
        self._apply_perf()
        self._clear_stream()
        self.cv.delete("all")
        self._kara = []
        self.idx = -1
        self._persist()

    def _apply_scale(self):
        s = self.font_scale
        self.JP_FONT     = ("Yu Gothic UI", max(10, round(38 * s)), "bold")
        self.FURI_FONT   = ("Yu Gothic UI", max(7,  round(17 * s)))
        self.ROMAJI_FONT = ("Segoe UI Semibold", max(8, round(23 * s)))
        self.EN_FONT     = ("Segoe UI", max(8, round(21 * s)))
        self.HINT_FONT   = ("Segoe UI", max(8, round(15 * s)))
        self.pad = 64
        self.furi_y   = round(52 * s)
        self.main_y   = round(102 * s)
        self.romaji_y = round(182 * s)
        self.en_y     = round(242 * s)
        # Scroll-through: staggered vertical lanes so consecutive lines sit at
        # different heights instead of piling up at one level. Compact block.
        self.b_furi = round(22 * s)
        self.b_main = round(64 * s)
        self.b_rom  = round(128 * s)
        self.b_en   = round(166 * s)
        self._lanes = 2
        self._lane_gap = round(200 * s)
        if self.scroll_dir in ("lr", "rl"):
            self.H = min(self.sh - 50, round(210 * s) + self._lane_gap * (self._lanes - 1))
        else:
            self.H = min(self.sh - 60, round(340 * s))

    def set_font_scale(self, v):
        self.font_scale = max(0.25, min(2.0, v))
        self._apply_scale()
        self.root.geometry(f"{self.W}x{self.H}+0+{self._geom_y()}")
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()
        if self.idx >= 0 and self.lines:        # re-render current line at new size
            self._render(self.lines[self.idx])
        self._persist()

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
            self._on_track_change(self._track, self._cur_duration)

    def report_wrong(self):
        """User-driven correction: bin the wrong lyrics and identify by SOUND."""
        if self._lyrics_path:
            try:
                Path(self._lyrics_path).unlink(missing_ok=True)
            except Exception:
                pass
            self.index.refresh()
        self._fetch_key = None
        self._lyrics_path = None
        self.lines, self.idx, self._kara = [], -1, []
        self._identified = None
        self._hint("🎧 Listening to identify the song…")
        self._start_identify()

    def identify_by_sound(self):
        self._identified = None
        self._hint("🎧 Listening to identify the song…")
        self._start_identify()

    def quit(self):
        self.media.stop()
        self.root.quit()

    def run(self):
        self.root.mainloop()


# ── Tray icon ────────────────────────────────────────────────────────

def make_icon():
    ico = _resource("icon.ico")
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

    def _reset(*_):   ov.root.after(0, ov.reset_offset)
    def _toggle(*_):  ov.root.after(0, ov.toggle)
    def _nudge(d):    return lambda *_: ov.root.after(0, lambda: ov.nudge(d))
    def _refetch(*_): ov.root.after(0, ov.refetch)
    def _wrong(*_):   ov.root.after(0, ov.report_wrong)
    def _ident(*_):   ov.root.after(0, ov.identify_by_sound)
    def _quit(icon, *_):
        icon.stop()
        ov.root.after(0, ov.quit)

    def _set_op(v):  return lambda *_: ov.root.after(0, lambda: ov.set_opacity(v))
    def _set_pos(p): return lambda *_: ov.root.after(0, lambda: ov.set_position(p))
    def _set_scr(d): return lambda *_: ov.root.after(0, lambda: ov.set_scroll(d))
    def _set_font(v): return lambda *_: ov.root.after(0, lambda: ov.set_font_scale(v))
    def _toggle_startup(*_): set_startup(not startup_enabled())

    def _op_item(label, v):
        return pystray.MenuItem(label, _set_op(v), radio=True,
                                checked=lambda i, v=v: abs(ov.opacity - v) < 0.02)

    def _pos_item(label, p):
        return pystray.MenuItem(label, _set_pos(p), radio=True,
                                checked=lambda i, p=p: ov.position == p)

    def _scr_item(label, d):
        return pystray.MenuItem(label, _set_scr(d), radio=True,
                                checked=lambda i, d=d: ov.scroll_dir == d)

    opacity_menu = pystray.Menu(
        _op_item("100%  (solid)", 1.0), _op_item("85%", 0.85),
        _op_item("70%", 0.70), _op_item("55%", 0.55),
        _op_item("40%  (faint — for games)", 0.40), _op_item("25%", 0.25),
    )
    position_menu = pystray.Menu(
        _pos_item("Top of screen", "top"),
        _pos_item("Bottom of screen", "bottom"),
    )
    scroll_menu = pystray.Menu(
        _scr_item("Stationary (appear in place)", "none"),
        _scr_item("Slide in from left", "left"),
        _scr_item("Slide in from right", "right"),
        pystray.Menu.SEPARATOR,
        _scr_item("Scroll through  →  (left to right)", "lr"),
        _scr_item("Scroll through  ←  (right to left)", "rl"),
    )

    def _spd_item(label, v):
        return pystray.MenuItem(label, lambda *_: ov.root.after(0, lambda: ov.set_scroll_speed(v)),
                                radio=True, checked=lambda i, v=v: abs(ov.scroll_speed - v) < 1)
    speed_menu = pystray.Menu(
        _spd_item("Slow", 130), _spd_item("Medium", 220),
        _spd_item("Fast", 340), _spd_item("Very fast", 480),
    )

    def _q_item(label, mode):
        return pystray.MenuItem(label, lambda *_: ov.root.after(0, lambda: ov.set_quality(mode)),
                                radio=True, checked=lambda i, mode=mode: ov.perf == mode)
    perf_menu = pystray.Menu(
        _q_item("Smooth  (best quality · 60fps)", "smooth"),
        _q_item("Performance  (lighter · 30fps)", "fast"),
    )

    def _font_item(pct):
        v = pct / 100
        return pystray.MenuItem(f"{pct}%", _set_font(v), radio=True,
                                checked=lambda i, v=v: abs(ov.font_scale - v) < 0.01)
    font_menu = pystray.Menu(*[_font_item(p) for p in range(25, 201, 25)])
    sync_menu = pystray.Menu(
        pystray.MenuItem("⏪  Lyrics earlier  +2.0s", _nudge(+2.0)),
        pystray.MenuItem("⏪  Lyrics earlier  +0.5s", _nudge(+0.5)),
        pystray.MenuItem("⏩  Lyrics later  −0.5s", _nudge(-0.5)),
        pystray.MenuItem("⏩  Lyrics later  −2.0s", _nudge(-2.0)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda i: f"Reset  (now {ov.offset:+.1f}s)", _reset),
    )

    menu = pystray.Menu(
        pystray.MenuItem("⚑  Wrong lyrics — fix this song", _wrong),
        pystray.MenuItem("🎧  Identify by sound", _ident),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda i: f"Sync timing  ({ov.offset:+.1f}s)", sync_menu),
        pystray.MenuItem("Opacity", opacity_menu),
        pystray.MenuItem("Font size", font_menu),
        pystray.MenuItem("Position", position_menu),
        pystray.MenuItem("Scroll-in", scroll_menu),
        pystray.MenuItem("Scroll-through speed", speed_menu),
        pystray.MenuItem("Performance", perf_menu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start with Windows", _toggle_startup,
                         checked=lambda i: startup_enabled()),
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
