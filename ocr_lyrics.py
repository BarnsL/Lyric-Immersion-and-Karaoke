# -*- coding: utf-8 -*-
r"""Read BURNED-IN lyric lines off the video and feed them as a timed lyric source.

Some videos render the lyrics INTO the frame (concert subtitles, lyric-video
typography, fan-subbed karaoke) and publish NO provider LRC / YouTube CC that we
can fetch. For those the pixels on screen are the only copy of the words. This
module OCRs the lyric band of the *source* video, times each new line against the
track clock, de-dupes, and emits an LRC string usable as ``source="ocr"``.

THE ONE HARD PROBLEM — self-read feedback loop
==============================================
Our own overlay draws lyrics in the SAME bottom band as most burned-in lyrics. A
naive full-screen grab would OCR our overlay and feed it back to itself, locking
onto whatever (possibly wrong) text we already show. We avoid that STRUCTURALLY:

  * PRIMARY: capture the source window's own pixels via ``PrintWindow`` with
    ``PW_RENDERFULLCONTENT``. That renders ONLY that window to an off-screen DC —
    our overlay is a separate top-level window and never appears in it. No flicker,
    no exclusion math. The known failure mode is a hardware-accelerated Chromium
    VIDEO surface coming back BLACK; ``_looks_black`` detects that and we fall back.
  * FALLBACK: full-screen region grab, with a text-match guard that drops any OCR
    line fuzzy-equal to our CURRENT overlay text. This is only ever engaged when a
    song FAILED to match (so the overlay is blank or showing different words),
    which is exactly when the guard can't eat the real burned-in line.

Everything degrades to "return nothing": no OCR engine, no capture backend, a
black grab with no fallback bbox → ``[]`` and the existing source chain stands.

LANGUAGES: uses the Windows OCR engine (same as concert_ocr.py). en-US ships by
default; add ja-JP once (admin) for Japanese burned-in lyrics:
    Add-WindowsCapability -Online -Name "Language.OCR~~~ja-JP~0.0.1.0"

This module is intentionally STANDALONE and side-effect free (no daemon, no global
state) so it can be unit-/spike-tested without launching the app. See
spikes/ocr_lyrics_spike.py for the live capture+OCR probe that de-risks the
Chromium-black question before wiring this into main.py's source chain.
"""
from __future__ import annotations

import asyncio
import difflib
import os
import re
import tempfile
from typing import Optional


# ── Lyric band geometry ─────────────────────────────────────────────────────────
# Burned-in lyrics sit in the lower-middle band. We OCR a centred horizontal strip
# from `_BAND_TOP` to `_BAND_BOTTOM` of the captured frame height, trimming the far
# left/right (timestamps, watermarks, chat) via `_BAND_SIDE`.
# Burned-in lyrics sit in the lower third of the VIDEO. On a WINDOWED YouTube page the
# video fills the upper ~85% of the window and the title/channel/controls sit BELOW it,
# so the band must stop well short of the window bottom (0.82, not 0.97) or it OCRs the
# page title. These windowed-tuned defaults + the metadata filter keep chrome out.
_BAND_TOP = 0.60
_BAND_BOTTOM = 0.82
_BAND_SIDE = 0.08
_MIN_LEN = 2
_OCR_DOWNSCALE_H = 240   # downscale the band to this height before PNG/OCR (GIL-hold cut)

PW_RENDERFULLCONTENT = 0x00000002


# ── OCR engine (reuse concert_ocr's Windows.Media.Ocr plumbing) ─────────────────

def _engines():
    """Reuse concert_ocr's per-language OcrEngine cache so we don't double-create
    engines. Falls back to a local build if that import is unavailable."""
    try:
        from concert_ocr import _engines as ce
        return ce()
    except Exception:
        pass
    out = []
    try:
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.globalization import Language
        for lang in OcrEngine.available_recognizer_languages:
            eng = OcrEngine.try_create_from_language(Language(lang.language_tag))
            if eng:
                out.append(eng)
    except Exception:
        pass
    return out


def available() -> bool:
    """True if the Windows OCR engine AND a capture backend are usable."""
    try:
        from PIL import Image  # noqa: F401
        import ctypes  # noqa: F401
        from winsdk.windows.media.ocr import OcrEngine
        return OcrEngine.available_recognizer_languages.size > 0
    except Exception:
        return False


def ocr_langs() -> list:
    """Recognizer language tags currently usable (diagnostics)."""
    return [e.recognizer_language.language_tag for e in _engines()]


async def _ocr_file(path, engine):
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage import StorageFile, FileAccessMode
    f = await StorageFile.get_file_from_path_async(os.path.abspath(path))
    stream = await f.open_async(FileAccessMode.READ)
    decoder = await BitmapDecoder.create_async(stream)
    bmp = await decoder.get_software_bitmap_async()
    res = await engine.recognize_async(bmp)
    # keep reading order (top-to-bottom, left-to-right) so multi-line lyrics stay
    # in sequence; winsdk returns lines already in layout order.
    return [ln.text.strip() for ln in res.lines if ln.text.strip()]


# ── Source-window capture via PrintWindow (self-read-safe) ──────────────────────

def capture_source_window(hwnd: int):
    """Grab the CLIENT pixels of window `hwnd` via PrintWindow(PW_RENDERFULLCONTENT).

    Returns a PIL.Image (RGB) of the window's client area, or None on failure. The
    app's own overlay is a DIFFERENT top-level window and does NOT appear here —
    that is the whole point (no self-read). May come back near-black for a
    hardware-accelerated Chromium video surface; caller checks `_looks_black`."""
    try:
        import ctypes
        from ctypes import wintypes
        from PIL import Image
    except Exception:
        return None
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    try:
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w <= 8 or h <= 8 or w > 10000 or h > 10000:
            return None

        hdc = user32.GetWindowDC(hwnd)
        if not hdc:
            return None
        mem_dc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        old = gdi32.SelectObject(mem_dc, bmp)
        try:
            # PW_RENDERFULLCONTENT (0x2) is what makes Chromium/Electron render at
            # all; plain PrintWindow returns white/black for DWM-composited apps.
            ok = user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
            if not ok:
                return None

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
                ]

            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h          # top-down rows
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0      # BI_RGB
            buf_len = w * h * 4
            buf = (ctypes.c_char * buf_len)()
            DIB_RGB_COLORS = 0
            got = gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi),
                                  DIB_RGB_COLORS)
            if not got:
                return None
            img = Image.frombuffer("RGB", (w, h), bytes(buf), "raw", "BGRX", 0, 1)
            return img
        finally:
            gdi32.SelectObject(mem_dc, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(hwnd, hdc)
    except Exception:
        return None


def is_fullscreen(hwnd: int) -> bool:
    """True if `hwnd` covers its whole monitor (borderless-fullscreen video). This is
    the production GATE for OCR-harvesting: only when the player is fullscreen is the
    lyric band pure video — on a windowed watch page the band is YouTube UI (title,
    'Next:' card, channel) and OCR there reads chrome, not lyrics (the spike showed
    this). Tolerant by a few px for DWM borders. False on any failure."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False
    user32 = ctypes.windll.user32
    try:
        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        wr = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(wr)):
            return False
        MONITOR_DEFAULTTONEAREST = 2
        hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return False
        m = mi.rcMonitor
        tol = 3
        return (abs(wr.left - m.left) <= tol and abs(wr.top - m.top) <= tol
                and abs(wr.right - m.right) <= tol and abs(wr.bottom - m.bottom) <= tol)
    except Exception:
        return False


_BROWSER_PROCS = {"brave.exe", "chrome.exe", "msedge.exe", "firefox.exe",
                  "vivaldi.exe", "opera.exe", "arc.exe", "steamwebhelper.exe"}


def find_source_window(title_hint: Optional[str] = None) -> Optional[int]:
    """Best-effort: find the playing browser/player window's HWND for PrintWindow when
    the SMTC source doesn't carry one (a normal Brave/Chrome tab). Enumerates visible
    browser windows, prefers one whose title contains `title_hint` or a music marker,
    then the largest by area. Returns an int HWND or None. EnumWindows is sub-ms, so
    this is cheap and called ONCE per harvest (not per poll)."""
    try:
        import ctypes
        from ctypes import wintypes
        from window_titles import _exe_basename_for_pid, _safe_window_text
    except Exception:
        return None
    try:
        user32 = ctypes.windll.user32
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        found = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _l):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                exe = (_exe_basename_for_pid(int(pid.value)) or "").lower()
                if exe not in _BROWSER_PROCS:
                    return True
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                w, h = r.right - r.left, r.bottom - r.top
                if w < 480 or h < 360:
                    return True
                found.append((int(hwnd), _safe_window_text(int(hwnd)) or "", w * h))
            except Exception:
                pass
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        if not found:
            return None
        hint = (title_hint or "").lower()

        def _score(item):
            _h, title, area = item
            s = area
            tl = title.lower()
            if hint and len(hint) >= 3 and hint[:24] in tl:
                s += 10 ** 12
            elif any(mk in tl for mk in ("youtube", "niconico", "- youtube", "｜")):
                s += 10 ** 9
            return s

        return max(found, key=_score)[0]
    except Exception:
        return None


def _looks_black(img, thresh: int = 10, frac: float = 0.985) -> bool:
    """True if `img` is essentially all-black (PrintWindow failed on a GPU surface).
    Samples a grid so it stays O(1) regardless of resolution."""
    try:
        small = img.convert("L").resize((64, 64))
        px = small.getdata()
        dark = sum(1 for p in px if p <= thresh)
        return dark >= frac * len(px)
    except Exception:
        return False


def _crop_band(img):
    """Crop the centred lower lyric band out of a full-window/screen image."""
    w, h = img.size
    top = int(h * _BAND_TOP)
    bot = int(h * _BAND_BOTTOM)
    left = int(w * _BAND_SIDE)
    right = int(w * (1.0 - _BAND_SIDE))
    if bot <= top or right <= left:
        return img
    return img.crop((left, top, right, bot))


def _ocr_image(img) -> list:
    """OCR a PIL image with every installed language; returns de-duped text lines."""
    engs = _engines()
    if not engs:
        return []
    path = None
    try:
        # Downscale the band to a modest height before the PNG encode — the encode is
        # the dominant GIL hold per poll (a full-width ~1900px band is ~30-40ms; ~240px
        # is ~8-12ms ≈ one frame). WinRT OCR reads the smaller image fine.
        if _OCR_DOWNSCALE_H and img.height > _OCR_DOWNSCALE_H:
            from PIL import Image as _Im
            w = max(1, int(img.width * _OCR_DOWNSCALE_H / img.height))
            img = img.resize((w, _OCR_DOWNSCALE_H), _Im.BILINEAR)
        fd, path = tempfile.mkstemp(prefix="dk_lyrocr_", suffix=".png")
        os.close(fd)
        img.save(path)
    except Exception:
        return []
    lines, seen = [], set()
    try:
        for eng in engs:
            try:
                for t in asyncio.run(_ocr_file(path, eng)):
                    k = t.lower()
                    if len(t) >= _MIN_LEN and k not in seen:
                        seen.add(k)
                        lines.append(t)
            except Exception:
                continue
    finally:
        if path:
            try:
                os.remove(path)
            except Exception:
                pass
    return lines


# ── Noise / self-read filtering ─────────────────────────────────────────────────

# UI chrome that is NOT a lyric: player controls, hashtags, view counts, chat, and —
# the big one the spike surfaced — YouTube's video title / channel / up-next card text
# that bleeds into the band on a NON-fullscreen watch page.
_NOISE_RE = re.compile(
    r"^\s*(?:\d+:\d+|\d+[KMB]?\s*views?|subscribe|share|save|like|"
    r"settings|autoplay|live\s*chat|top\s*chat|replay|skip|mute|next|up\s*next|"
    r"ch\.|・|\(cover\)|\d+\s*videos?|playlist|mix\b|"
    r"再生|登録|チャンネル|高評価|共有|次の動画|広告)\s*$",
    re.I,
)
# Substrings that mark a YouTube UI element no matter where they sit in the line.
_UI_SUBSTR_RE = re.compile(r"next\s*:|up\s*next|·\s*\d|\bReGLOSS\b|\(cover\)|"
                           r"ch\.\s|subscrib|- youtube|｜.*youtube", re.I)
_TIMECODE_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_NORM = re.compile(r"[^0-9a-z぀-ヿ一-鿿]")
# Windows OCR sprinkles spaces BETWEEN CJK glyphs ('名 前 の な い'); collapse them so
# the text matches lyric providers and reads correctly. Only spaces flanked by CJK.
_CJK = r"぀-ヿ㐀-䶿一-鿿ｦ-ﾟ"
_CJK_SPACE_RE = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")

# OCR emits a "tofu" box (□ ■ ▯ ▢ … or U+FFFD �) where it failed to recognize a glyph
# or where the frame had a decorative separator / wide space it couldn't map. Those
# render as an ugly box in the overlay (seen: "S M T W T F S□Back to the beginning").
# Drop the known unknown-glyph code points + control chars, and normalize exotic
# spaces, then squeeze the whitespace they leave behind. Run BEFORE _collapse_cjk_spaces
# so a box sitting between two CJK glyphs collapses cleanly.
_OCR_TOFU_RE = re.compile(
    "["
    "\u25A0-\u25FF"          # geometric shapes (□ ■ ◢ ▯ …) - OCR's "unknown glyph"
    "\u2B1B\u2B1C"           # large black/white square
    "\uFFFC\uFFFD"           # object-replacement / replacement char
    "\uE000-\uF8FF"          # private-use area (stray font glyphs)
    "\x00-\x1F\x7F-\x9F"    # C0/C1 control chars
    "\uFFF9-\uFFFB"          # interlinear annotation marks
    "]"
)
_EXOTIC_SPACE_RE = re.compile(
    "[\u00A0\u2000-\u200B\u202F\u205F\u3000\uFEFF]")


def _strip_tofu(s: str) -> str:
    if not s:
        return s
    s = _OCR_TOFU_RE.sub(" ", s)
    s = _EXOTIC_SPACE_RE.sub(" ", s)
    return re.sub(r"\s{2,}", " ", s).strip()


def _collapse_cjk_spaces(s: str) -> str:
    return _CJK_SPACE_RE.sub("", s or "")


def _norm(s: str) -> str:
    return _NORM.sub("", (s or "").lower())


def _tokens(s: str) -> set:
    return {w for w in re.split(r"[^0-9a-z぀-ヿ一-鿿]+", (s or "").lower()) if len(w) >= 2}


def _is_lyric_like(line: str) -> bool:
    """Reject obvious non-lyric noise. Keeps anything with enough language content.
    Tightened (≥4 language chars, ≥60% of the line, ≥3 normalized chars) so stray
    typography / credits / single glyphs on a plain MV don't commit as a lyric."""
    s = (line or "").strip()
    if len(s) < _MIN_LEN or _NOISE_RE.match(s) or _UI_SUBSTR_RE.search(s):
        return False
    if _TIMECODE_RE.fullmatch(s):
        return False
    langish = sum(1 for c in s if c.isalpha() or "぀" <= c <= "鿿")
    return langish >= 4 and (langish / max(1, len(s))) >= 0.6 and len(_norm(s)) >= 3


def _matches_meta(raw_line: str, norm_line: str, meta_norm: str, meta_tokens: set) -> bool:
    """True if an OCR line is really the page's TITLE/ARTIST bleeding through (so NOT a
    lyric). The page title/channel text contains the song title and artist; real
    burned-in lyrics don't. Match on containment, high ratio, OR token overlap. Tokens
    come from the RAW (un-normalized) line so word boundaries survive."""
    if not norm_line or not meta_norm:
        return False
    if norm_line in meta_norm or meta_norm in norm_line:
        return True
    if difflib.SequenceMatcher(None, norm_line, meta_norm).ratio() >= 0.8:
        return True
    lt = _tokens(raw_line)
    if not lt or not meta_tokens:
        return False
    overlap = len(lt & meta_tokens)
    # a line sharing ≥2 distinct title/artist words (e.g. 'FLOW GLOW') is page chrome,
    # even when OCR garbles the rest — burned-in lyrics don't echo the title words.
    return overlap >= 2 or overlap >= max(1, len(lt) // 2)


def filter_lyric_lines(lines, overlay_text: Optional[str] = None,
                       track_title: Optional[str] = None,
                       track_artist: Optional[str] = None) -> list:
    """Keep only plausible burned-in LYRIC lines. Drops, in order:
      * UI chrome (player controls, view counts, 'Next:' up-next card, channel) — the
        spike showed these dominate the band on a non-fullscreen watch page;
      * lines that are really the page TITLE/ARTIST bleeding through (matched against
        `track_title`/`track_artist`) — the single most effective discriminator;
      * (fallback path only) lines fuzzy-matching our OWN overlay text — self-read guard.
    CJK glyph-spacing from the OCR engine is collapsed first. `overlay_text` should be
    None for PrintWindow captures (already overlay-free)."""
    ov = _norm(overlay_text) if overlay_text else ""
    meta = " ".join(x for x in (track_title, track_artist) if x)
    meta_norm = _norm(meta)
    meta_tokens = _tokens(meta)
    out = []
    for ln in lines:
        ln = _collapse_cjk_spaces(_strip_tofu((ln or "").strip()))
        if not _is_lyric_like(ln):
            continue
        nl = _norm(ln)
        if meta_norm and _matches_meta(ln, nl, meta_norm, meta_tokens):
            continue            # page title / channel / up-next, not a lyric
        if ov and nl and (nl == ov or nl in ov or ov in nl
                          or difflib.SequenceMatcher(None, nl, ov).ratio() >= 0.85):
            continue            # our own overlay bleeding back in
        out.append(ln)
    return out


# ── One-shot read ────────────────────────────────────────────────────────────────

def read_lyric_lines(hwnd: Optional[int] = None,
                     bbox: Optional[tuple] = None,
                     overlay_text: Optional[str] = None,
                     track_title: Optional[str] = None,
                     track_artist: Optional[str] = None,
                     allow_fallback: bool = True) -> list:
    """Read the burned-in lyric line(s) currently on screen, self-read-safe.

    PRIMARY path (hwnd given): PrintWindow the source window → crop band → OCR. The
    overlay is structurally absent, so `overlay_text` is ignored.
    FALLBACK path (PrintWindow black, or only bbox given): grab the screen region
    `bbox` (or full screen) → crop band → OCR → drop lines matching `overlay_text`.
    `track_title`/`track_artist` (when known) drop page-title/channel bleed-through —
    the strongest non-lyric filter. `allow_fallback=False` disables the screen-grab
    path entirely (caller sets this once an OCR LRC is already showing, so a black
    PrintWindow can never lock onto our own overlay). Returns [] on any failure."""
    img = None
    used_fallback = False
    if hwnd:
        img = capture_source_window(hwnd)
        if img is not None and _looks_black(img):
            img = None              # GPU surface came back black → fall back
    if img is None:
        if not allow_fallback:
            return []               # no self-read-safe capture available → read nothing
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab(bbox=bbox) if bbox else ImageGrab.grab()
            used_fallback = True
        except Exception:
            return []
    if img is None:
        return []
    raw = _ocr_image(_crop_band(img))
    # only apply the self-read guard on the fallback (composited) path
    return filter_lyric_lines(raw, overlay_text if used_fallback else None,
                              track_title=track_title, track_artist=track_artist)


# ── Timed harvester → LRC builder ────────────────────────────────────────────────

class LyricOcrHarvester:
    """Accumulate burned-in lyric lines into a timed LRC by polling `read_lyric_lines`
    against the track clock.

    Call ``observe(t_s, lines)`` every poll with the seconds-since-track-start and the
    OCR'd lyric lines. A line is COMMITTED to the LRC only after it has been seen on
    two consecutive polls (kills transient OCR garbage) and isn't a near-duplicate of
    the line we just committed (the same line lingers on screen for several polls).
    ``to_lrc()`` renders the standard ``[mm:ss.xx] text`` form the rest of the app
    already parses."""

    def __init__(self, stable_polls: int = 2, dup_ratio: float = 0.82):
        self._stable_polls = max(1, stable_polls)
        self._dup_ratio = dup_ratio
        self._committed = []                 # list[(t_s, text)]
        self._pending = None                 # (norm, text, first_t, count)

    def _is_dup_of_last(self, norm: str) -> bool:
        if not self._committed:
            return False
        last = _norm(self._committed[-1][1])
        if not last or not norm:
            return False
        return (norm == last or norm in last or last in norm
                or difflib.SequenceMatcher(None, norm, last).ratio() >= self._dup_ratio)

    def observe(self, t_s: float, lines) -> Optional[str]:
        """Feed one poll. Returns the text just committed (if any), else None."""
        # Join multi-line OCR into one logical lyric line (burned-in lyrics are often
        # wrapped); pick the longest language-ish run as the representative.
        cand = ""
        for ln in lines or []:
            if len(ln) > len(cand):
                cand = ln
        cand = cand.strip()
        if not cand:
            return None
        norm = _norm(cand)
        if not norm or self._is_dup_of_last(norm):
            return None
        if self._pending and (
                self._pending[0] == norm
                or difflib.SequenceMatcher(None, self._pending[0], norm).ratio() >= self._dup_ratio):
            n, txt, first_t, count = self._pending
            count += 1
            self._pending = (n, txt, first_t, count)
            if count >= self._stable_polls:
                self._committed.append((round(first_t, 2), txt))
                self._pending = None
                return txt
            return None
        # new pending candidate
        self._pending = (norm, cand, t_s, 1)
        if self._stable_polls == 1:
            self._committed.append((round(t_s, 2), cand))
            self._pending = None
            return cand
        return None

    def lines(self) -> list:
        return list(self._committed)

    def to_lrc(self) -> str:
        out = []
        for t, text in self._committed:
            mm = int(t // 60)
            ss = t - mm * 60
            out.append(f"[{mm:02d}:{ss:05.2f}] {text}")
        return "\n".join(out)


# ── Self-test ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print("available:", available())
    print("ocr langs:", ocr_langs())
    # tiny offline harvester check (no capture)
    h = LyricOcrHarvester(stable_polls=2)
    seq = [(1.0, ["君の声が"]), (2.2, ["君の声が"]), (3.4, ["遠く響く"]), (4.6, ["遠く響く"])]
    for t, ls in seq:
        c = h.observe(t, ls)
        if c:
            print(f"  committed @{t}s: {c}")
    print("LRC:\n" + h.to_lrc())
