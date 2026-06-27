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
import contextlib
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
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from character import Character
import updater
import version
import confidence
import gpu_setup

# Run every subprocess (git, PowerShell, pip) with NO console window — otherwise
# Windows flashes a black cmd window each time the app shells out.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(0)
except Exception:
    pass

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont, ImageTk
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pystray", "Pillow"],
                          creationflags=_NO_WINDOW)
    import pystray
    from PIL import Image, ImageDraw, ImageFont, ImageTk

# Windows font files for PIL block rendering (first that loads wins). Per-script
# so non-Japanese text doesn't render as boxes (□): Yu Gothic has no Hangul,
# so Korean needs Malgun Gothic; Chinese gets Microsoft YaHei for full coverage.
_PIL_FONTS = {
    "jp":    ("YuGothB.ttc", "meiryob.ttc", "msgothic.ttc", "yugothb.ttf"),
    "ko":    ("malgunbd.ttf", "malgun.ttf", "gulim.ttc", "batang.ttc"),
    "zh":    ("msyhbd.ttc", "msyh.ttc", "simhei.ttf", "simsun.ttc"),
    # Latin/Cyrillic main text: Segoe UI renders it tightly — Yu Gothic gives
    # Cyrillic/Latin huge full-width spacing, so Russian/German/English use this.
    "latin": ("segoeuib.ttf", "seguisb.ttf", "segoeui.ttf"),
    "furi":  ("YuGothR.ttc", "meiryo.ttc", "msgothic.ttc", "yugothr.ttf"),
    "rm":    ("seguisb.ttf", "segoeui.ttf"),
    "en":    ("segoeui.ttf",),
}
# tkinter font families per script (fallback path when PIL blocks are off).
_TK_MAIN_FONT = {"jp": "Yu Gothic UI", "ko": "Malgun Gothic",
                 "zh": "Microsoft YaHei", "latin": "Segoe UI"}

_HANGUL_RE = re.compile(r"[가-힣ㄱ-ㆎ]")
_KANA_RE = re.compile(r"[ぁ-ゖァ-ヺ]")
_HAN_RE = re.compile(r"[一-鿿㐀-䶿々]")


def _script_of(text, song_lang=None):
    """Pick the font for a line by its script. Korean (Hangul) and Japanese
    (kana) are unambiguous; bare kanji reads as Japanese unless the whole song
    is Chinese; anything else (Latin, Cyrillic) uses the tight Segoe UI 'latin'
    font rather than the wide-spaced Japanese font."""
    if _HANGUL_RE.search(text or ""):
        return "ko"
    if _KANA_RE.search(text or ""):
        return "jp"
    if _HAN_RE.search(text or ""):
        return "zh" if song_lang == "zh" else "jp"
    return "latin"

BASE = Path(__file__).parent
# Writable data (settings, lyric cache, log) lives next to the .exe for the
# portable build, but in %LOCALAPPDATA% when installed via MSIX (its install
# dir is read-only). appdata.data_dir() resolves that — see appdata.py.
from appdata import data_dir
_DATA = data_dir()
LYRICS_DIR = _DATA / "lyrics"
SETTINGS = _DATA / "settings.json"
LOG_FILE = _DATA / "karaoke.log"

# ── Logging ──────────────────────────────────────────────────────────
# A rolling log of what the app is doing — track changes, title vs. sound
# matches, swaps, sync corrections, errors — so a human OR an automated agent
# can see WHY a given song/lyric was chosen (read it via the API's /logs, or the
# file directly). Kept small (rotates at ~256 KB).
import logging
from logging.handlers import RotatingFileHandler

log = logging.getLogger("karaoke")
log.setLevel(logging.INFO)
try:
    _h = RotatingFileHandler(LOG_FILE, maxBytes=256_000, backupCount=1,
                             encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                      "%H:%M:%S"))
    log.addHandler(_h)
except Exception:
    pass


def _resource(name):
    """Path to a bundled read-only resource (icon), frozen or not."""
    return Path(getattr(sys, "_MEIPASS", BASE)) / name


def _seed_bundled_lyrics():
    """Copy lyrics SHIPPED with the app (bundled_lyrics/) into the runtime cache so
    songs whose providers ALWAYS fail are baked in and reliably available.

    feelingradation (ReGLOSS) is the case: the app searches under the verbose
    channel 'hololive DEV_IS ReGLOSS' and every provider misses, so it fell back to
    a poor Whisper transcription. The real synced LRC exists under 'ReGLOSS', so we
    ship a properly furigana'd / romaji'd / translated copy and seed it here. It
    OVERWRITES a weaker (generated/transcribed) cache of the same song; an identical
    already-seeded copy is left alone. Best-effort; any failure is ignored."""
    import shutil
    try:
        src_dir = _resource("bundled_lyrics")
        if not src_dir.is_dir():
            return
        LYRICS_DIR.mkdir(exist_ok=True)
        for src in src_dir.glob("*.json"):
            dst = LYRICS_DIR / src.name
            try:
                if dst.exists() and dst.read_bytes() == src.read_bytes():
                    continue                       # already seeded, identical
                shutil.copyfile(src, dst)
                log.info("seeded bundled lyrics: %s", src.name)
            except Exception:
                pass
    except Exception:
        pass


def _load_settings():
    """Read settings.json (returns {} if missing or unreadable)."""
    try:
        return json.loads(SETTINGS.read_text("utf-8"))
    except Exception:
        return {}


def _save_settings(data):
    """Write the settings dict to settings.json (best-effort)."""
    try:
        SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Start-with-Windows (Startup-folder shortcut) ─────────────────────

def _startup_lnk():
    """Path to the .lnk in the user's Startup folder used for 'Start with
    Windows'."""
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "Startup" / "Lyric Immersion and Karaoke.lnk")


def startup_enabled():
    """True if the Start-with-Windows shortcut currently exists."""
    return _startup_lnk().exists()


def _psq(s):
    """Quote a string as a single-quoted PowerShell literal (doubles any ')."""
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
                       capture_output=True, timeout=15, creationflags=_NO_WINDOW)
    except Exception:
        pass


# ── TICKET-105: Start Menu shortcut self-heal (rebrand migration) ────
# After the v1.0.84 rebrand from Desktop Karaoke -> Lyric Immersion and
# Karaoke (exe renamed too), users were left with a stale
# 'Desktop Karaoke.lnk' in the Start Menu pointing at a deleted exe.
# Clicking it did nothing, and searching the new name found nothing.
# Self-heal at startup: nuke the broken old shortcut, drop a fresh one
# under the new name pointing to sys.executable. Frozen builds only
# (dev runs are noisy enough already).
def _start_menu_dir() -> Path:
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs")


def _migrate_start_menu_shortcut():
    """One-shot per launch: clean up stale `Desktop Karaoke.lnk` and
    ensure a `Lyric Immersion and Karaoke.lnk` exists pointing to the
    current exe. Swallows all errors (best-effort, never blocks startup)."""
    if not getattr(sys, "frozen", False):
        return  # dev runs: leave the user's shortcuts alone
    try:
        smdir = _start_menu_dir()
        old = smdir / "Desktop Karaoke.lnk"
        new = smdir / "Lyric Immersion and Karaoke.lnk"
        # 1) Old shortcut: delete if its target is missing or it points
        # at the legacy DesktopKaraoke.exe (which no longer exists).
        if old.exists():
            try:
                ps_old = (f"$W=New-Object -ComObject WScript.Shell;"
                          f"$S=$W.CreateShortcut({_psq(old)});"
                          f"Write-Output $S.TargetPath")
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_old],
                    capture_output=True, timeout=5,
                    creationflags=_NO_WINDOW, text=True)
                tgt = (r.stdout or "").strip()
                if (not tgt) or (not Path(tgt).exists()) or \
                        tgt.lower().endswith("desktopkaraoke.exe"):
                    old.unlink()
            except Exception:
                try: old.unlink()
                except Exception: pass
        # 2) New shortcut: create if missing, pointing at the current exe.
        if not new.exists():
            target = sys.executable
            workdir = str(Path(target).parent)
            ps_new = (f"$W=New-Object -ComObject WScript.Shell;"
                      f"$S=$W.CreateShortcut({_psq(new)});"
                      f"$S.TargetPath={_psq(target)};"
                      f"$S.WorkingDirectory={_psq(workdir)};"
                      f"$S.IconLocation={_psq(target + ',0')};"
                      f"$S.Description='Transparent karaoke overlay with synced furigana, romaji, and English translation.';"
                      f"$S.WindowStyle=7;"  # minimized, no-activate per CLAUDE.md app etiquette
                      f"$S.Save()")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_new],
                           capture_output=True, timeout=10,
                           creationflags=_NO_WINDOW)
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
_CJK_RE = re.compile(r"[一-鿿㐀-䶿ぁ-んァ-ヶー가-힣]")

# Social / short-video platforms whose browser tab reports the SITE NAME as the
# SMTC "title" (with no artist) when you scroll Reels/Shorts/clips — that's NOT a
# song, so the karaoke overlay must not switch on and slap a same-named song's
# lyrics over it (an Instagram Reel matched the song "Instagram"). YouTube is
# deliberately ABSENT — its tabs report the real video title.
_NON_MUSIC_TITLES = {
    "instagram", "tiktok", "facebook", "x", "twitter", "reddit", "snapchat",
    "threads", "tumblr", "linkedin", "pinterest", "discord", "whatsapp",
    "telegram", "messenger", "twitch", "vimeo", "dailymotion", "bilibili",
    "新しいタブ", "new tab",
}


def is_non_music_source(title, artist):
    """True when the media title is just a social/short-video SITE NAME with no
    artist — a Reel/Short/clip, not a song. Then the overlay stays off."""
    t = re.sub(r"\s*[-–—|]\s*(reels?|shorts?|video)\s*$", "", (title or "").strip(), flags=re.I)
    t = t.strip().lower()
    return bool(t in _NON_MUSIC_TITLES and not (artist or "").strip())


def _has_cjk(s):
    """True if the string contains any CJK or Hangul character."""
    return bool(_CJK_RE.search(s or ""))


_NORM_RE = re.compile(r"[^0-9a-z぀-ヿ一-鿿가-힣]+")


def _norm_title(s):
    """Normalize a title/artist for comparison: lowercase, keep only letters,
    digits, and CJK/kana/Hangul (drops spaces, punctuation, feat. credits)."""
    return _NORM_RE.sub("", (s or "").lower())


# Cyrillic → Latin, so a romanized video title ("Nas Ne Dogonyat") can match a
# Cyrillic cached title ("Нас не догонят").
_CYR_LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
            "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
            "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
            "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
            "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}


def _translit_cyr(s):
    return "".join(_CYR_LAT.get(c.lower(), c) for c in (s or ""))


_kks_title = None


def _to_hepburn(s):
    """Romanize JP (kanji/hiragana/katakana) in `s` to Hepburn so a cached file
    titled 'かもね' is reachable from a player title 'kamone' (and vice versa).
    Empty/no-CJK → returns the input unchanged; pykakasi unavailable → ditto.
    Memoized: the kakasi analyzer is reused across calls (TICKET-080)."""
    if not s or not _has_cjk(s):
        return s
    global _kks_title
    if _kks_title is None:
        try:
            import pykakasi
            _kks_title = pykakasi.kakasi()
        except Exception:
            _kks_title = False
    if not _kks_title:
        return s
    try:
        return "".join(it.get("hepburn") or it.get("orig") or ""
                       for it in _kks_title.convert(s))
    except Exception:
        return s


def _title_forms(title):
    """Backwards-compat flat set of forms (native ∪ alt) used by older callers."""
    n, a = _title_forms_split(title)
    return n | a


def _title_forms_split(title):
    """Return (native_forms, alt_forms): the whole thing, each 'Artist / Song'
    segment, and Cyrillic→Latin go to NATIVE; the Hepburn romaji of a JP-script
    title goes to ALT so the matcher can apply a small penalty for cross-script
    bridge hits (TICKET-080). Only the last segment is tried (the song; the
    leading parts are the artist) and segments shorter than 4 chars are dropped
    so the artist name can't cause a false match."""
    def _expand(base, dest):
        dest.add(_norm_title(base))
        segs = re.split(r"\s*[/／]\s*", base)
        if len(segs) > 1:
            nf = _norm_title(segs[-1])
            if len(nf) >= 4:
                dest.add(nf)
    native, alt = set(), set()
    for base in (title or "", _translit_cyr(title or "")):
        _expand(base, native)
    hep = _to_hepburn(title or "")
    if hep and hep != (title or ""):
        _expand(hep, alt)
    native.discard("")
    alt.discard("")
    alt -= native                                   # don't double-count an alt that's also native
    return native, alt


# ── Real playback position via Windows Media Transport Controls ───────

class MediaWatcher:
    """Polls the OS media session in a background thread."""

    def __init__(self):
        self._state = None
        self._lock = threading.Lock()
        self._stop = False
        self.error = None
        self._pick_src = None       # source_app of the session we're following (sticky)
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
        mgr = None
        while not self._stop:
            try:
                if mgr is None:                 # reuse the manager across polls —
                    mgr = await MM.request_async()  # re-requesting it each 0.1s was wasteful
                sess = self._pick(mgr)
                if sess:
                    info = await sess.try_get_media_properties_async()
                    tl = sess.get_timeline_properties()
                    pb = sess.get_playback_info()
                    status = pb.playback_status
                    # Playback rate (≠1.0 when the user speeds up / slows down,
                    # very common on YouTube) — the clock must advance by it.
                    try:
                        rate = float(pb.playback_rate) if pb.playback_rate else 1.0
                    except Exception:
                        rate = 1.0
                    if rate <= 0:
                        rate = 1.0
                    pos = tl.position.total_seconds()
                    try:
                        lu = tl.last_updated_time
                        if status == PLAYING and lu.year > 1:
                            pos += (datetime.now(timezone.utc) - lu).total_seconds() * rate
                    except Exception:
                        pass
                    st = {
                        "title": info.title or "",
                        "artist": info.artist or "",
                        "album": (getattr(info, "album_title", "") or ""),
                        "status": status,
                        "position": max(0.0, pos),
                        "duration": tl.end_time.total_seconds(),
                        "rate": rate,
                        "source": (sess.source_app_user_model_id or "").lower(),
                        "ts": time.time(),
                    }
                    with self._lock:
                        self._state = st
                else:
                    with self._lock:
                        self._state = None
            except Exception:
                mgr = None                      # drop a stale manager; re-request next poll
            await asyncio.sleep(0.15)   # position is extrapolated, so 0.15s polling
            #                              keeps accuracy while cutting CPU ~33%

    def _pick(self, mgr):
        """Pick the media session to follow — STICKY, so a paused background tab
        can't hijack playback. The bug: with a paused tab (Coffee) AND a playing
        Mix, during the brief gap between Mix songs NO session is 'playing', and
        the old code fell back to `get_current_session()` (often the paused tab) —
        so the overlay flip-flopped Coffee↔Mix every track, loading the wrong
        song's lyrics. Now: prefer a PLAYING session, preferring the one we were
        already following; and when nothing is playing (a transition gap) KEEP
        following the last session instead of jumping to a different paused tab."""
        try:
            sessions = list(mgr.get_sessions())
        except Exception:
            sessions = []

        def sid(s):
            try:
                return s.source_app_user_model_id or ""
            except Exception:
                return ""

        def playing(s):
            try:
                return s.get_playback_info().playback_status == PLAYING
            except Exception:
                return False

        playing_now = [s for s in sessions if playing(s)]
        # 1) keep following our session if it's still playing (stability)
        if self._pick_src:
            for s in playing_now:
                if sid(s) == self._pick_src:
                    return s
        # 2) otherwise the first playing session — and remember it
        if playing_now:
            self._pick_src = sid(playing_now[0])
            return playing_now[0]
        # 3) NOTHING is playing (likely a gap between Mix tracks). Do NOT jump to
        #    a paused tab — keep the session we were following if it still exists,
        #    so the overlay holds the current song through the gap.
        if self._pick_src:
            for s in sessions:
                if sid(s) == self._pick_src:
                    return s
        try:
            return mgr.get_current_session()
        except Exception:
            return None

    def get(self):
        with self._lock:
            if not self._state:
                return None
            s = dict(self._state)
        if s["status"] == PLAYING:
            s["position"] += (time.time() - s["ts"]) * s.get("rate", 1.0)
        return s

    def stop(self):
        self._stop = True


# Tie-in descriptors that are NOT a song name — a title that cleans down to only
# these ('TVアニメOPテーマ', 'OP Theme') has no real song and must defer to sound.
_GENERIC_TITLE_RE = re.compile(
    r"^(?:tv)?(?:アニメ|anime)?"
    r"(?:op|ed|opening|ending|主題歌|オープニング|エンディング|テーマ|theme)+$",
    re.I,
)


def _is_generic_title(s):
    """True if `s` is only a tie-in tag like 'TVアニメOPテーマ' / 'OP Theme', with
    no actual song name (so it must never override a Shazam sound-ID)."""
    compact = re.sub(r"[\s　・:：\-–—|/／'\"]+", "", s or "")
    return bool(compact) and bool(_GENERIC_TITLE_RE.fullmatch(compact))


# A 歌ってみた / cover upload: its lyrics are the ORIGINAL song's, so the fetch
# looks the song up by TITLE and ignores the covering channel as the "artist".
_COVER_RE = re.compile(
    r"歌ってみた|うたってみた|歌わせて|踊ってみた|おどってみた"
    r"|演奏してみた|弾いてみた|叩いてみた|covered?\s+by"
    r"|\(\s*cover\s*\)|\[\s*cover\s*\]|[/／]\s*cover\b"
    # "cover" as a TAG right after any common opening bracket — 【Cover MV】,
    # ［Cover］, （Cover MV）, (Cover MV). The lenticular / fullwidth styles VTuber
    # covers use that the ASCII-paren rules above miss. This is the bug that made
    # "【Cover MV】MAFIA / マフィア - Ouro Kronii" search by the COVER channel
    # (Ouro Kronii) — which has no lyrics for it — instead of title-first.
    r"|[【\[（(［]\s*covers?\b", re.I)


# TICKET-086: bands whose canonical names include an ampersand or "and" — must
# NOT trip the ampersand-collab cover detector. Lowercased for compact compare.
_AMP_ARTIST_ALLOWLIST = (
    "hall & oates", "simon & garfunkel", "crosby, stills & nash",
    "crosby, stills, nash & young", "earth, wind & fire",
    "florence + the machine", "tegan and sara", "ike & tina turner",
    "ashford & simpson", "sonny & cher", "iggy & the stooges",
    "captain & tennille", "peaches & herb", "salt-n-pepa",
    "kool & the gang", "emerson, lake & palmer", "blood, sweat & tears",
)

# TICKET-086: "Song <sep> Artist1 & Artist2" on YouTube Music is almost always
# a collaboration cover. We separate the EXPLICIT cover-tag signal (high
# confidence) from this AMPERSAND collab signal (lower confidence) so the cover
# routing can be conservative — title-only search, no trust in the right-hand
# names as the "original artist".
# A title-separator is EITHER: a dash/slash/pipe/colon with WHITESPACE around it
# (so 'Counter-Strike & War' / 'T-Pain & Lil Wayne' / 'AC/DC & Friends' /
# 'k-os & Mike' do NOT match — those are single-artist names with embedded
# punctuation), OR an opening bracket character (always a delimiter on its own).
# Verify caught this — the original `[-–—/|:【「(\[]` matched ANY hyphen and
# wrongly fired on every hyphenated-artist title.
_AMP_COLLAB_SEPS = r"(?:\s[-–—/|:]\s|[【「(\[])"
_AMP_COLLAB_TAIL_RE = re.compile(
    # right side of the title-separator: two-or-more artist-like tokens joined
    # by & (with optional spaces) or ＆ — each token ≥ 2 chars of word /
    # CJK / a few in-name punctuators (' - ’ · ・). Anchored to end of string
    # after a light trim so a trailing bracket / pipe doesn't kill the match.
    r"^\s*([\w\-’'·・]{2,}(?:\s+[\w\-’'·・]{2,})*)"
    r"(?:\s*[&＆]\s*[\w\-’'·・]{2,}(?:\s+[\w\-’'·・]{2,})*)+\s*$",
    re.UNICODE,
)


def _is_amp_collab_title(title, cover_channel=""):
    """TICKET-086: True for a title like ``Song - Artist1 & Artist2`` where the
    ampersand sits between two DISTINCT artist tokens after a real title
    separator. Guards: HTML-unescape so ``&amp;`` decodes, refuse a single
    known-embedded-ampersand band name, and ignore the COVER channel as one of
    the tokens (so the channel landing on the right side doesn't itself fire
    the signal)."""
    import html as _html
    t = _html.unescape(title or "").strip()
    if not t:
        return False
    low = t.lower()
    for band in _AMP_ARTIST_ALLOWLIST:
        if band in low:
            return False
    # find the FIRST title separator and take what's on the right of it
    m = re.search(_AMP_COLLAB_SEPS, t)
    if not m:
        return False
    right = t[m.end():].strip(" -–—|/_:")
    # strip a trailing closing bracket so "Song - A & B)" still parses
    right = right.rstrip(")】］」』])」")
    if not right or "&" not in right and "＆" not in right:
        return False
    if not _AMP_COLLAB_TAIL_RE.match(right):
        return False
    # split on the ampersand and ensure both sides are distinct names of length
    # ≥ 2 and neither is just the cover channel itself
    parts = [p.strip() for p in re.split(r"\s*[&＆]\s*", right) if p.strip()]
    if len(parts) < 2:
        return False
    seen = set()
    for p in parts:
        pl = p.lower()
        if len(pl) < 2 or pl in seen:
            return False
        seen.add(pl)
    ch = (cover_channel or "").strip().lower()
    if ch and all(p.lower() == ch for p in parts):
        return False
    return True


def is_cover_title(title):
    """True if a media title marks a 歌ってみた / cover. Drives a title-first lyric
    fetch — the original song's lyrics fit the cover (see fetch_lrc cover=).
    TICKET-086: also fires on an ampersand-collab title (``Song - A & B``)
    which on YouTube Music is almost always a collaboration cover."""
    return bool(_COVER_RE.search(title or "")) or _is_amp_collab_title(title)


def cover_signal(title, cover_channel=""):
    """TICKET-086: WHICH cover signal fired — ``'explicit'`` for a real cover
    tag (歌ってみた / [COVER] / 'covered by' …), ``'amp_collab'`` for the weaker
    ampersand-collab heuristic, ``None`` for no cover signal. Lets callers vary
    confidence: an explicit tag is unambiguous; the ampersand signal only takes
    the title-only search path and can be DEMOTED by other evidence (e.g. a
    non-empty YouTube Music ``album`` field = official original)."""
    if _COVER_RE.search(title or ""):
        return "explicit"
    if _is_amp_collab_title(title, cover_channel):
        return "amp_collab"
    return None


def extract_cover_original(raw_title, cover_channel=""):
    """Parse a cover video title to find the ORIGINAL artist.

    Returns (original_artist, song_title) when it can identify the original
    artist from the title; (None, None) when it can't.

    Common patterns:
      [COVER] Coffee - Alka | Kaneko Lumi   → ("Alka", "Coffee")
      Song - OrigArtist / CoverArtist       → ("OrigArtist", "Song")
      Song (cover) / CoverArtist            → (None, "Song")
    """
    if not raw_title or not is_cover_title(raw_title):
        return None, None
    # TICKET-086: an AMP-COLLAB signal (Song - A & B) carries lower confidence
    # than an explicit cover tag — providers index covers under the ORIGINAL
    # artist, not the collab pair, so we deliberately return (None, song) and
    # let the caller take the title-only search path. Never trust the right-
    # hand "A & B" as the original artist.
    if cover_signal(raw_title, cover_channel) == "amp_collab":
        t0 = raw_title.strip()
        # take everything BEFORE the first title separator as the song
        m = re.search(_AMP_COLLAB_SEPS, t0)
        song = (t0[:m.start()] if m else t0).strip(" -–—|/_:")
        return None, (song or None)
    t = raw_title.strip()
    # strip cover markers and brackets containing them
    t = re.sub(r"\[\s*cover\s*\]", "", t, flags=re.I).strip()
    t = re.sub(r"\(\s*cover\s*\)", "", t, flags=re.I).strip()
    # a whole bracketed cover tag with extra words — 【Cover MV】, （Cover MV）, ［Cover］
    t = re.sub(r"[【\[（(［]\s*covers?\b[^】\]）)］]*[】\]）)］]", "", t, flags=re.I).strip()
    t = re.sub(r"\s*(?:[/／]|を)?\s*(?:歌ってみた|うたってみた|歌わせて|踊ってみた|おどってみた|"
               r"演奏してみた|弾いてみた|叩いてみた).*$", "", t, flags=re.I).strip()
    t = re.sub(r"\s*\bcovered?\s+by\b.*$", "", t, flags=re.I).strip()
    t = re.sub(r"\s*[/／]\s*cover\b.*$", "", t, flags=re.I).strip()
    if not t:
        return None, None
    # normalise the cover channel name for matching
    ch = re.sub(r"[^0-9a-z぀-鿿]", "", (cover_channel or "").lower())
    def _ch_match(part):
        pn = re.sub(r"[^0-9a-z぀-鿿]", "", part.lower())
        if not pn or not ch:
            return False
        return pn in ch or ch in pn
    # split by | (pipe — YouTube cover titles like "Song - Orig | CoverCh")
    if "|" in t:
        sides = [s.strip() for s in t.split("|", 1)]
        if len(sides) == 2 and sides[0] and sides[1]:
            if _ch_match(sides[1]):
                # right side is the cover channel → left has song + original artist
                left = sides[0]
            elif _ch_match(sides[0]):
                left = sides[1]
            else:
                left = sides[0]    # default: first side is song+artist
            # "Song - OrigArtist" → split by " - "
            if " - " in left:
                song, _, orig = left.partition(" - ")
                return orig.strip() or None, song.strip() or None
            return None, left.strip() or None
    # "Song - OrigArtist / CoverArtist" (slash separator)
    for sep in (r"\s*/\s*", r"\s*／\s*"):
        parts = re.split(sep, t, 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            if _ch_match(parts[1]):
                left = parts[0].strip()
                if " - " in left:
                    song, _, orig = left.partition(" - ")
                    return orig.strip() or None, song.strip() or None
                return None, left
            elif _ch_match(parts[0]):
                right = parts[1].strip()
                if " - " in right:
                    song, _, orig = right.partition(" - ")
                    return orig.strip() or None, song.strip() or None
                return None, right
    # plain "Song - OrigArtist" with no cover channel separator
    if " - " in t:
        parts = t.split(" - ", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            if not _ch_match(parts[1]):
                return parts[1].strip(), parts[0].strip()
            # the tail IS the cover channel ("MAFIA / マフィア - Ouro Kronii") → keep
            # the SONG, no original artist (search title-first, ignore the channel).
            return None, parts[0].strip()
    # EXPLICIT cover credit ("… covered by X" / "…歌ってみた") names the COVER artist
    # OUTSIDE the slash, so what's left is "OriginalSong / OriginalArtist" — even
    # though NEITHER side is the channel (the rules above missed it). Take the artist
    # side so the search is QUALIFIED: "Rebellion / hololive English -Advent-" must
    # search by "hololive English -Advent-" to find the right same-titled song
    # instead of a generic "Rebellion" collision. 歌ってみた titles are "Song / Artist".
    if re.search(r"covered?\s+by|歌ってみた|うたってみた|歌わせて", raw_title, re.I):
        for sep in ("/", "／"):
            if sep in t:
                left, _, right = t.partition(sep)
                left, right = left.strip(), right.strip()
                if left and right and len(right) >= 2 and not _ch_match(right):
                    return right, left            # (original artist, song)
    return None, None


def clean_title(title, source="", artist=""):
    """Reduce a media title to the actual SONG NAME so it matches lyric metadata.

    Japanese uploads name the song inside 「」 (and put the work/anime in 『』):
    'Daoko「COMIT COMET」(with BEATBROS.) - TVアニメ『よわよわ先生』OPテーマ' → the song
    is **COMIT COMET**, not 'TVアニメOPテーマ'. So we EXTRACT the bracketed song
    rather than stripping it — stripping 「」 used to leave only the generic
    descriptor and fetch a *different* song. Also drops 'Official MV / HD' tags
    and **cover credits** ('天誅 / covered by 幸祜' → '天誅'). `source` is the player
    app id (browser titles get extra cleanup)."""
    t = title or ""
    # TICKET-086: YouTube Music's autoplay-mix indicator leaks "Mix - <song>"
    # into the SMTC title and would mismatch every lyric provider (the song is
    # `<song>`, not `Mix - <song>`). Anchored to BOL so a song actually titled
    # 'DJ Mix - Track' (content BEFORE 'Mix') isn't touched.
    t = re.sub(r"^\s*Mix\s*[-–—]\s*", "", t, flags=re.I)
    # A 歌ってみた / cover is usually titled "OriginalSong / Singer(s)" and its
    # lyrics are the ORIGINAL song's. Detect the marker from the RAW title now —
    # the (cover) / 歌ってみた tags get stripped below — so we can keep just the
    # song part at the end.
    is_cover = is_cover_title(t)
    # An Original / MV upload titled "Song/Artist" (very common for VTuber MVs,
    # e.g. "[Original] Dunk/Todoroki Hajime [Official MV]") puts the SONG first —
    # used below to keep just the song so it matches + fetches by its real name.
    is_mv = bool(re.search(r"\b(?:Official|MV|PV|Music\s*Video|"
                           r"Performance\s*Video|Lyric\s*Video)\b", t, re.I))
    if any(h in source for h in BROWSER_HINTS):
        # Strip the video-site tab suffix: browsers append " - <SiteName>" to
        # tab titles, and an unstripped suffix makes the empty-artist split in
        # _on_track_change treat the suffix as the title (the real bug:
        # "Ahoy!! 我ら宝鐘海賊団☆ - ニコニコ動画" → artist="Ahoy!!…", title="ニコニコ動画",
        # fetched the wrong song under that title).
        t = re.sub(
            r"\s*[-–—|]\s*(?:YouTube|ニコニコ動画|niconico|nicovideo|"
            r"Vimeo|Bilibili|bilibili|Dailymotion|Twitch|SoundCloud|"
            r"Bandcamp|TikTok)\s*$",
            "", t, flags=re.I)

    # The song is the 「…」 content if present; otherwise a 『…』 that is NOT a
    # work-name tie-in (i.e. not '『Anime』OPテーマ') — that covers '『水星』' = the song.
    song = None
    mq = re.search(r"「([^」]+)」", t)
    if mq:
        song = mq.group(1)
    else:
        md = re.search(r"『([^』]+)』(\s*(?:tv|アニメ|anime|op|ed|opening|ending|主題歌|テーマ)\b)?",
                       t, flags=re.I)
        if md and not md.group(2) and not _is_generic_title(md.group(1)):
            song = md.group(1)
    # VTuber/idol uploads also wrap the song in straight/smart quotes, e.g.
    # "ReGLOSS 'サクラミラージュ' Performance Video" → サクラミラージュ. Require a real
    # PAIR around ≥2 chars so an apostrophe ("Don't") isn't taken for a quote.
    if not song:
        qm = re.search(r"['‘’]([^'‘’]{2,})['‘’]|[\"“”]([^\"“”]{2,})[\"“”]", t)
        if qm:
            cand = (qm.group(1) or qm.group(2) or "").strip()
            if cand and not _is_generic_title(cand):
                song = cand
    if song and song.strip():
        t = song.strip()

    # VTuber/idol uploads often title as "Song ✦ Artist" with a decorative star
    # separator (✦ ✧ ★ ☆ ◆ ❖ ♪ …). Keep the song (first part). Require spaces
    # around the mark so a stylised title like "★STARLIGHT★" isn't split.
    if not song:
        t = re.split(r"\s+[✦✧✩⭐★☆◆◇❖♪♫]\s+", t, 1)[0]

    t = re.sub(r"\s*[\[(（【「『][^\])）】」』]*[\])）】」』]", "", t)    # leftover (Official MV) / （cover） etc.
    # cover / "tried singing" credits → keep only the song title
    t = re.sub(r"\s*([/／]\s*)?\bcover(ed)?\s+by\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s*[/／]\s*cover\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s*(?:[/／]|を)?\s*(歌ってみた|歌わせて|踊ってみた|おどってみた|"
               r"演奏してみた|弾いてみた|叩いてみた|アコギ|acoustic\s*ver).*$", "", t, flags=re.I)
    t = re.sub(
        r"\b(Official\s*(Music\s*)?(Video|Audio)|Official|Music\s*Video|"
        r"Performance\s*Video|Visuali[sz]er|MV|PV|"
        r"Lyric\s*Video|Audio|HD|4K|FULL|Full\s*Ver\.?)\b",
        "", t, flags=re.I,
    )
    # "Song feat./ft. Artist" → the SONG is before feat (the collaborator is the
    # artist, not part of the lyric-search title): "Clione feat. 轟はじめ" → "Clione".
    t = re.sub(r"\s*\b(?:feat|ft|featuring)\.?\s+.*$", "", t, flags=re.I)
    # Trailing dash-delimited version/edit subtitle, common on JP MV uploads:
    # "Into Starlight -anniversary special ver.-", "曲名 -Remix-", "- Acoustic ver -".
    t = re.sub(
        r"\s*[-–—]\s*[^-–—]*\b(ver\.?|version|edit|remix|remaster(?:ed)?|acoustic|"
        r"instrumental|off\s*vocal|anniversary|special|tv\s*size|short\s*ver|"
        r"long\s*ver|self\s*cover|live)\b[^-–—]*[-–—]?\s*$",
        "", t, flags=re.I,
    )
    # a trailing anime tie-in with no song info ('… - TVアニメOPテーマ')
    t = re.sub(r"\s*[-–—/／]\s*(?:tv\s*)?(?:アニメ|anime)\s*.*?"
               r"(?:op|ed|主題歌|テーマ|opening|ending|theme).*$", "", t, flags=re.I)
    # ── Artist-aware reduction: pull the SONG out of a title that ALSO names the
    # artist, using the artist to decide which part is which. This rescues a huge
    # class of POPULAR songs that otherwise generate because the credit derails the
    # lyric search — the providers HAVE them, the messy title just hid them.
    a_low = (artist or "").lower()
    a_norm = re.sub(r"[^0-9a-z぀-ヿ一-鿿]", "", a_low)
    a_tok = {x for x in re.split(r"[^0-9a-z]+", a_low) if len(x) >= 4}

    def _artistish(p):
        pl = p.lower()
        pn = re.sub(r"[^0-9a-z぀-ヿ一-鿿]", "", pl)
        if pn and a_norm and (pn in a_norm or a_norm in pn):
            return True
        return bool({x for x in re.split(r"[^0-9a-z]+", pl) if len(x) >= 4} & a_tok)

    # "X / Y" (cover / MV uploads): EITHER order occurs — 'Dunk/Todoroki Hajime'
    # (Song/Artist → Dunk) vs 'FLOW GLOW / LOAD' (Group/Song → LOAD). Keep the side
    # that is NOT the artist; default to the FIRST when neither matches
    # ('幻界/V.W.P #30' → 幻界). Single slash, no ' - ' on either side (a bilingual
    # 'Artist - JP / Artist - EN' is left whole for _title_variants).
    if is_cover or is_mv:
        parts = re.split(r"\s*[/／]\s*", t)
        if (len(parts) == 2 and len(parts[0].strip()) >= 2
                and " - " not in parts[0] and " - " not in parts[1]):
            p0, p1 = parts[0].strip(), parts[1].strip()
            if _artistish(p1) and not _artistish(p0):
                t = p0
            elif _artistish(p0) and not _artistish(p1):
                t = p1
            else:
                t = p0

    # "Artist - Song" / "Artist × Artist - Song": drop a LEADING artist credit so the
    # song searches on its own — 'KizunaAI - white balance' → 'white balance',
    # 'Reol - Edge' → 'Edge', 'ReGLOSS - feelingradation' → 'feelingradation'. Only
    # when the part before the first ' - ' is artist-ish, so a genuine 'A - B' song
    # title is left intact. (These had NO lyrics found only because of the credit.)
    if a_norm and " - " in t:
        head, _, tail = t.partition(" - ")
        if len(tail.strip()) >= 2 and _artistish(head):
            t = tail.strip()
    # strip trailing/leading separators INCLUDING box-drawing / fullwidth bars
    # (┃│｜／・‖): a truncated "Song | Cover by X" arrives as "Song┃", and that
    # stray bar made the search match a different-language song (Blue Bird┃ → a
    # Spanish track instead of the Japanese original).
    return t.strip(" -–—|/　┃│｜／・‖").strip()


# Titles that name an EVENT (a whole concert / festival / medley), not a song.
# TICKET-106: added the "Nth ONE-MAN LIVE" / "Nth LIVE TOUR" / ワンマン family so
# titles like "歌姫 from V.W.P 4th ONE-MAN LIVE" promote to live_mode (drive by
# sound, ignore title-as-song) instead of just is_live_arrangement (FOLLOW offset,
# title still trusted). Generic bare "(Live)" / "[LIVE]" / "Live ver." stay OUT —
# those mark single-song live arrangements and are handled by _LIVE_VER_RE.
_LIVE_RE = re.compile(
    r"\b(?:concert|fes(?:tival)?|tour|setlist|set\s*list|medley|megamix|"
    r"mega\s*mix|non-?stop|dj\s*set|full\s*(?:album|live|concert|set)|"
    r"rock\s*japan|rising\s*sun|summer\s*sonic|fuji\s*rock|countdown|"
    r"anniversary\s*live|"
    r"one[\s-]?man\s*live|"                                   # TICKET-106: 'ONE-MAN LIVE' (JP solo-concert idiom)
    r"\d+(?:st|nd|rd|th)\s+one[\s-]?man(?:\s*live)?|"         # TICKET-106: '4th ONE-MAN LIVE', '5th ONE-MAN'
    r"\d+(?:st|nd|rd|th)\s+(?:live|tour|anniversary)\s+(?:tour|live|stage|fes(?:tival)?))\b"  # TICKET-106: '10th LIVE TOUR', '5th ANNIVERSARY LIVE'
    r"|ライブ|ﾗｲﾌﾞ|生放送|コンサート|フェス|ツアー|メドレー|セットリスト|セトリ|"
    r"ワンマン(?:ライブ)?|"                                    # TICKET-106: ワンマン / ワンマンライブ
    r"周年ライブ|[0-9]+\s*周年|[0-9]\s*d\s*live",
    re.I,
)


def is_live_or_compilation(title, duration=None):
    """True for a long video, or one whose title says 'live / concert / festival /
    medley / 3D LIVE / メドレー' — almost always MANY songs under one title, where
    the title names the EVENT, not the song. Such videos must be driven by SOUND:
    title-matching them is what makes a whole concert show one (wrong) song's
    lyrics, with no way for Shazam to override a title that's a real song name."""
    if duration and duration > 12 * 60:      # >12 min ⇒ multi-song in practice
        return True
    return bool(_LIVE_RE.search(title or ""))


# Music-video / MV uploads — these often open with a cinematic or instrumental
# "dead-space" intro BEFORE the song proper, so the lyrics' time 0 lands partway
# into the video. (See the MV-intro dead-space handling in the Overlay: hold the
# lyrics through the intro, then anchor them to the detected audio onset.)
_MV_RE = re.compile(
    r"\b(?:official\s*(?:music\s*)?video|music\s*video|m\s*/?\s*v|p\s*/?\s*v|"
    r"official\s*audio|lyric\s*video|visuali[sz]er)\b"
    r"|ミュージックビデオ|ＭＶ|【\s*mv\s*】|「\s*mv\s*」",
    re.I,
)


def is_mv_version(title):
    """True for an official-MV / music-video style upload. These frequently start
    with a cinematic/instrumental intro before the song — when Shazam can measure
    the real offset we use it; when it can't (niche tracks), the overlay holds the
    lyrics through that dead-space and anchors them to the detected audio onset."""
    return bool(_MV_RE.search(title or ""))


# A SINGLE-song LIVE / short / alternate ARRANGEMENT (not a multi-song event):
# 'LIVE MV', 'Short Ver.', 'Acoustic', 'from "<concert>"' … The lyrics still match
# the song, but the TIMING differs hugely from the studio LRC (different intro,
# tempo, edits), so a STUDIO reset-to-0 strategy strands them — these need the
# offset to be FOLLOWED, not reset. (Distinct from is_live_or_compilation, which is
# a multi-song event we drive by sound alone.)
_LIVE_VER_RE = re.compile(
    r"\b(?:live(?:\s*(?:mv|ver(?:sion)?|performance|stage|clip))?|short\s*ver(?:sion)?|"
    r"acoustic|unplugged|orchestral?|piano\s*ver|ballad\s*ver|spinning\s*ver|"
    r"one[\s-]?man(?:\s*live)?|"                 # 'ONE-MAN LIVE' = a solo concert (JP term)
    r"\d+(?:st|nd|rd|th)\s+(?:one|live|tour|anniv(?:ersary)?))\b"  # '3rd ONE' = SMTC-truncated ONE-MAN; '5th LIVE', '10th Anniversary', etc.
    r"|【\s*live\b|\[\s*live\b"                   # 【LIVE MV】 / [LIVE] bracket tags
    r"|【冒頭無料】|【\s*無料\s*配信"             # 【冒頭無料】 = JP "first portion free" live banner
    r"|#\w*one\s*man\b|#\w*\d+(?:st|nd|rd|th)\s*(?:one|live)\b"  # hashtag tells: #VESP3rdONEMAN, #3rdLIVE
    r"|from\s+[\"'“”『「]"                       # 'from "<concert/album>"'
    r"|ライブ|ﾗｲﾌﾞ|生歌|ワンマン|ショート(?:バージョン|ver)|アコースティック|弾き語り",
    re.I,
)


def is_live_arrangement(title):
    """True for a single-song LIVE/short/alternate version whose timing won't match
    the studio LRC (so sync must FOLLOW the measured offset, not reset to 0)."""
    return bool(_LIVE_VER_RE.search(title or ""))


# TICKET-091: number-word → digit map for SMTC handle normalization
# (`CalibreCincuenta` → `Calibre 50`). Only the LAST PascalCase-split token
# gets converted, so `One Direction` / `Trio Los Panchos` are preserved.
_SMTC_NUM_WORDS = {
    # English
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "twenty": "20", "thirty": "30",
    "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
    "eighty": "80", "ninety": "90", "hundred": "100",
    # Spanish (Calibre 50 / Banda Cuarenta y Cinco / etc.)
    "uno": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5",
    "seis": "6", "siete": "7", "ocho": "8", "nueve": "9", "diez": "10",
    "once": "11", "doce": "12", "veinte": "20", "treinta": "30",
    "cuarenta": "40", "cincuenta": "50", "sesenta": "60", "setenta": "70",
    "ochenta": "80", "noventa": "90", "cien": "100", "ciento": "100",
    # Japanese number-word handles (rare but cheap)
    "ichi": "1", "ni": "2", "san": "3", "shi": "4", "go": "5",
    "roku": "6", "shichi": "7", "hachi": "8", "kyuu": "9", "juu": "10",
}
_PASCAL_BREAK_RE = re.compile(r'(?<=[a-z])(?=[A-Z])')


def _normalize_smtc_artist(s: str) -> str:
    """TICKET-091: normalize SMTC artist strings that compact spaces and spell
    numbers as words. `CalibreCincuenta` → `Calibre 50`, `BandaMS` → `Banda MS`,
    `LosTigresDelNorte` → `Los Tigres Del Norte`, `MaroonFive` → `Maroon 5`.
    Idempotent: `Calibre 50` stays `Calibre 50`, `One Direction` stays
    `One Direction` (no PascalCase compaction → number-word not converted).
    Only the LAST PascalCase-split token converts, to avoid breaking names like
    `Trio Los Panchos`."""
    raw = (s or "").strip()
    if not raw:
        return raw
    if " " in raw:
        # already spaced — only convert if the LAST token is a number-word and
        # there are at least two tokens (avoid lone "Cinco" → "5")
        toks = raw.split()
        if len(toks) >= 2 and toks[-1].lower() in _SMTC_NUM_WORDS:
            toks[-1] = _SMTC_NUM_WORDS[toks[-1].lower()]
            return " ".join(toks)
        return raw
    # compacted PascalCase: split at lower→Upper boundaries
    parts = _PASCAL_BREAK_RE.split(raw)
    if len(parts) >= 2:
        if parts[-1].lower() in _SMTC_NUM_WORDS:
            parts[-1] = _SMTC_NUM_WORDS[parts[-1].lower()]
        return " ".join(parts)
    return raw


def clean_artist(artist, source=""):
    """Strip YouTube channel cruft so the artist matches lyric providers:
    'Kaneko Lumi - Topic' → 'Kaneko Lumi', 'LMFAOVEVO' → 'LMFAO'. Auto-generated
    '… - Topic' / VEVO / 'Official Artist Channel' uploads are real tracks; the
    suffix just blocks the provider/Shazam-name search.

    TICKET-086: ``source`` (the SMTC source-app id) lets the cleaner BYPASS the
    aggressive channel rules for YouTube Music (``music.youtube.*``), which
    delivers a clean artist name already (``轟はじめ`` alone, not the channel
    string). Default empty preserves all existing callers.

    TICKET-091: a final pass through ``_normalize_smtc_artist`` handles YT
    channel handles that compact spaces and spell numbers in words
    (`CalibreCincuenta` → `Calibre 50`)."""
    if source and "music.youtube" in (source or "").lower():
        return _normalize_smtc_artist((artist or "").strip())
    a = (artist or "").strip()
    D = r"[-–—‐]"          # include U+2010 ‐ used by hololive-style channel names
    a = re.sub(rf"\s*{D}\s*Topic$", "", a, flags=re.I)
    a = re.sub(rf"\s*{D}\s*Official(\s+(Artist|Music))?\s+Channel$", "", a, flags=re.I)
    # A dash-prefixed channel name with no space before "Channel": "Kizuna AI -
    # A.I.Channel" → "Kizuna AI" (that suffix made the search miss a song the
    # providers DO have — the #1 cause of popular songs generating).
    a = re.sub(rf"\s*{D}\s*[\w.]*Channel$", "", a, flags=re.I)
    a = re.sub(r"\s*VEVO$", "", a)
    # VTuber / idol-unit channels carry the channel, not the artist, in the media
    # "artist" field (e.g. "Hajime Ch. 轟はじめ ‐ ReGLOSS") — which broke the
    # provider/local lookup and slowed it to a crawl (a 60s fetch lost the race to
    # generate-by-ear). Reduce it to the performer: drop a trailing unit/group tag,
    # then a "<romaji> Ch." channel prefix, then a plain " Channel" suffix.
    # Trailing agency/group tag in brackets (【Phase Connect】, [hololive]) …
    a = re.sub(r"\s*[【\[][^】\]]*[】\]]\s*$", "", a)
    # … or after a dash (‐ ReGLOSS).
    a = re.sub(rf"\s*{D}\s*(ReGLOSS|hololive[\w-]*|holo\w*|NIJISANJI|Phase[\s-]?Connect"
               r"|VSPO!?|VShojo)\b.*$", "", a, flags=re.I)
    # "X Ch. Y" → the performer. Keep the name AFTER "Ch." when one remains
    # ("Hajime Ch. 轟はじめ" → "轟はじめ"); otherwise keep the name BEFORE it
    # ("Lumi Ch.【Phase Connect】" → "Lumi", the bracket tag already stripped). The
    # old rule blindly kept what followed "Ch." and so returned the agency.
    m = re.match(r"^(.+?)\s+Ch\.(?:\s+(.*))?$", a)
    if m:
        a = (m.group(2) or "").strip() or m.group(1).strip()
    a = re.sub(r"\s+Channel$", "", a, flags=re.I)  # "Suisei Channel" → "Suisei"
    a = _normalize_smtc_artist(a.strip())  # TICKET-091: CalibreCincuenta → Calibre 50
    return a or (artist or "")


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
    """Strip any stray inline LRC timestamp tags ([mm:ss], <mm:ss>) from text."""
    return _TS_RE.sub("", s).strip()


def load_lyrics(path):
    """Load a cached lyrics JSON file → (meta dict, list[Line]) with timestamps,
    furigana/main text, romaji, and English, tags stripped."""
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
    """Parse 'kanji(かな)' furigana markup into [(base, reading), …] segments;
    plain runs come back as (text, '')."""
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
            core = re.sub(r"\s*[\(（].*?[\)）]", "", lt).strip()
            cn = _norm_title(core) or _norm_title(lt)
            # also a Latin form of a Cyrillic title, so "Nas Ne Dogonyat" matches
            forms = {cn}
            if any("а" <= c <= "я" or c == "ё" for c in core.lower()):
                forms.add(_norm_title(_translit_cyr(core)))
            # TICKET-080: cross-script lookup — index 'かもね' under its Hepburn
            # romaji 'kamone' too so a romanized YouTube title finds it. Kept in
            # a SEPARATE set so the matcher can prefer a native-script match if
            # one also exists (avoids picking かもね.json over kamone.json when
            # the query is 'kamone' and both are cached).
            forms_alt = set()
            if _has_cjk(core):
                rj = _norm_title(_to_hepburn(core))
                if rj and rj != cn:
                    forms_alt.add(rj)
            entries.append({
                "path": p,
                "title": lt,
                "core": core,
                "forms": {f for f in forms if f},
                "forms_alt": {f for f in forms_alt if f},
                "artist": m.get("artist") or "",
                "dur": m.get("duration"),
            })
        self.entries = entries

    def add(self, path):
        path = Path(path)
        self.entries = [e for e in self.entries if e["path"] != path]
        self.refresh()

    def candidates(self, title, limit=6):
        """Cache PATHS whose title plausibly relates to `title` — the candidate
        pool for the by-ear song decision (`align.decide_song_by_lyrics`). Loose on
        purpose: the by-ear lyric match is the real filter, this only narrows the
        field so we transcribe-compare against a handful, not the whole library."""
        q = _norm_title(title)
        if not q:
            return []
        out = []
        for e in self.entries:
            ct = _norm_title(e["core"]) or _norm_title(e["title"])
            if not ct:
                continue
            short, lng = sorted((q, ct), key=len)
            if short and (short in lng or (len(short) >= 4 and any(
                    short[i:i + 4] in lng for i in range(len(short) - 3)))):
                out.append(e["path"])
                if len(out) >= limit:
                    break
        return out

    def match(self, artist, title, duration=None):
        """Find the cached song whose TITLE matches `title` confidently.

        Matching is **title-driven, not artist-driven**, and paranoid: a
        candidate is accepted only if its title equals the query, or one title
        contains ≥60% of the other. So a *different* song by the same artist is
        never grabbed (the bug where another ReGLOSS track matched). Duration
        breaks ties and rejects wrong-length versions; artist is only a mild
        tiebreaker. Returns None when nothing is confident — the caller then
        identifies by **sound**."""
        qt = _norm_title(title)
        qa = _norm_title(artist)
        if not qt:
            return None
        # whole title + each 'Artist / Song' segment + Cyrillic transliteration
        # (native); Hepburn romaji of a JP title goes to alt forms (penalized -3
        # on either side so a same-script cache beats a cross-script bridge).
        q_native, q_alt = _title_forms_split(title)
        best, best_score = None, 0
        for e in self.entries:
            ea = _norm_title(e["artist"])
            e_core = _norm_title(e["core"])
            score = 0
            def _score_form(ct, q_forms, alt_side):
                # An ARTIST/GROUP-only SEGMENT (e.g. 'flowglow' from 'Song / FLOW
                # GLOW') is shared across that artist's songs, so on its own it must
                # not carry a match — require the SONG to match. Skip a *segment*
                # (never the whole title) that is (contained in) either artist name.
                if (ct and len(ct) >= 3 and ct != e_core
                        and ((ea and (ct == ea or ct in ea))
                             or (qa and (ct == qa or ct in qa)))):
                    return 0
                best_s = 0
                for q in q_forms:
                    if not ct or not q:
                        continue
                    if qa and len(q) >= 3 and q != qt and (q == qa or q in qa):
                        continue   # query SEGMENT that's just the artist, not the song
                    if ct == q:
                        s = 100
                    elif ct in q or q in ct:
                        short, lng = sorted((ct, q), key=len)
                        cover = len(short) / max(1, len(lng))
                        s = 60 + int(30 * cover) if cover >= 0.6 else 0
                        # TICKET-081: penalize a CANDIDATE that's a strict superset
                        # of the QUERY ('ghost' ⊂ 'ghosting' shouldn't beat exact
                        # 'ghost' — that's the GHOST/Suisei "halloween thing" bug).
                        # Doesn't fire when the query is the superset (which is the
                        # legitimate "drop trailing 'feat.' / version tag" case).
                        if q == short and ct == lng and q != ct:
                            s -= 12
                    else:
                        s = 0
                    if s and alt_side:                 # cross-script (Hepburn) bridge
                        s -= 3                         # → prefer a same-script entry
                    best_s = max(best_s, s)
                return best_s
            # native↔native (full points); native↔alt or alt↔native or alt↔alt → -3.
            for ct in e.get("forms") or {e_core}:
                score = max(score, _score_form(ct, q_native, alt_side=False))
                score = max(score, _score_form(ct, q_alt, alt_side=True))
            for ct in e.get("forms_alt") or set():
                score = max(score, _score_form(ct, q_native | q_alt, alt_side=True))
            if not score:
                continue
            if duration and e["dur"]:
                score += 8 if abs(e["dur"] - duration) <= 12 else -40
            # TICKET-081: artist corroboration is the strongest non-title signal
            # for the failure modes we keep seeing (GHOST → ghosting.json, Suisei
            # → 'Suisei' as title). Bumped exact from +5 to +12; added a partial
            # +6 for 'Suisei' ⊂ 'Hoshimachi Suisei' / 'Suisei' ⊂ 'Suisei Channel'.
            if qa:
                ea_norm = _norm_title(e["artist"])
                if ea_norm and ea_norm == qa:
                    score += 12
                elif ea_norm and (qa in ea_norm or ea_norm in qa):
                    short_a, lng_a = sorted((qa, ea_norm), key=len)
                    if len(short_a) >= 3 and len(short_a) / max(1, len(lng_a)) >= 0.5:
                        score += 6
            if score > best_score:
                best, best_score = e["path"], score
        if best and best_score >= 60:
            log.info("title-match %r -> %s (score %d)", title, best.name, best_score)
            return best
        log.info("no confident title-match for %r (best %d); will use sound", title, best_score)
        return None


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


# Bounded LRU cache for measure_text widths. The previous implementation was an
# unbounded dict, which (a) drifted up forever when font_scale or the active
# script set changed mid-session, and (b) couldn't be inspected from /diag. The
# OrderedDict variant evicts oldest on overflow and exposes hit-rate telemetry.
# Cardinality target: ~3000-5000 entries per song (JP common-char set + Latin +
# 1-3 effective (size, weight) variants per font). 4096 covers this with
# headroom; raise via the 'measure_text_cache_size' tune knob (effective at
# next app restart since the cache is module-level).
_MEASURE_CACHE_MAX = 4096
_MEASURE_CACHE = OrderedDict()
# Lifetime counters for /diag (process-lifetime; no reset path today since the
# app has no hot-reload of main.py). Kept as a small list so we don't need a
# 'global' declaration in the hot path.
_MEASURE_CACHE_STATS = [0, 0]  # [hits, misses]


def measure_text(cv, text, font):
    """Pixel width of `text` in `font`, cached. Width depends only on (text,
    font), so caching avoids creating/deleting a throwaway canvas item on every
    call (the non-scroll renderer measures every character per line).

    Bounded LRU (cap = _MEASURE_CACHE_MAX). Tk-thread only (every caller is
    reached via root.after), so no lock is taken; if measure_text is ever moved
    off the Tk thread, revisit this and the create_text/bbox/delete trio below.
    """
    key = (text, font)
    cache = _MEASURE_CACHE
    w = cache.get(key)
    if w is not None:
        # Hit: refresh recency, bump counter, return cached width.
        cache.move_to_end(key)
        _MEASURE_CACHE_STATS[0] += 1
        return w
    # Miss: do the (expensive) Tk measurement, insert, evict oldest if over cap.
    tid = cv.create_text(-9999, -9999, text=text, font=font, anchor="nw")
    bbox = cv.bbox(tid)
    cv.delete(tid)
    w = (bbox[2] - bbox[0]) if bbox else 0
    cache[key] = w
    if len(cache) > _MEASURE_CACHE_MAX:
        cache.popitem(last=False)
    _MEASURE_CACHE_STATS[1] += 1
    return w


def _measure_text_cache_hit_rate():
    """hits / (hits + misses) over process lifetime, or None if no calls yet.
    Surfaced in /diag as 'measure_text_cache_hit_rate' to detect cache
    thrashing (e.g. font_scale spam) without rebuilding."""
    hits, misses = _MEASURE_CACHE_STATS
    total = hits + misses
    if total == 0:
        return None
    return round(hits / total, 4)


def _work_area():
    """The desktop work area (screen minus the taskbar) as (left, top, right,
    bottom). Sizing to THIS — not the raw screen — keeps the bottom scroll lane
    from sliding under the taskbar. Returns None if the query fails."""
    try:
        from ctypes import wintypes
        r = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0,
                                                       ctypes.byref(r), 0) \
                and r.bottom > r.top:
            return r.left, r.top, r.right, r.bottom
    except Exception:
        pass
    return None


def _click_through_hwnd(win):
    """Make any Toplevel window click-through (input passes to whatever is behind)."""
    try:
        u = ctypes.windll.user32
        hwnd = u.GetAncestor(win.winfo_id(), 2) or win.winfo_id()
        GWL_EXSTYLE = -20
        WS_EX = 0x08000000 | 0x00000080 | 0x00080000 | 0x00000020
        ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX)
    except Exception:
        pass


def _mon_fingerprint(m):
    """Stable identity for a monitor: position + resolution.  Survives
    enumeration-order changes across sleep/wake and GPU driver reloads."""
    return f"{m['x']},{m['y']},{m['w']}x{m['h']}"


def _monitors():
    """Every connected monitor as a list of dicts with FULL bounds (x,y,w,h) and the
    WORK area (wx,wy,ww,wh = bounds minus that monitor's taskbar) plus `primary`
    and a stable `fp` fingerprint.
    Tkinter can't enumerate monitors, so use Win32 EnumDisplayMonitors. Primary
    first, then left-to-right. Falls back to a single primary entry on failure."""
    mons = []
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        proc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                                  ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def _cb(hmon, hdc, lprc, lparam):
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                m, w = mi.rcMonitor, mi.rcWork
                mons.append({"x": m.left, "y": m.top, "w": m.right - m.left,
                             "h": m.bottom - m.top, "wx": w.left, "wy": w.top,
                             "ww": w.right - w.left, "wh": w.bottom - w.top,
                             "primary": bool(mi.dwFlags & 1)})
            return 1
        user32.EnumDisplayMonitors(0, 0, proc(_cb), 0)
    except Exception:
        mons = []
    if not mons:
        l, t, r, b = _work_area() or (0, 0, 1920, 1032)
        mons = [{"x": l, "y": t, "w": r - l, "h": b - t, "wx": l, "wy": t,
                 "ww": r - l, "wh": b - t, "primary": True}]
    mons.sort(key=lambda d: (not d["primary"], d["x"]))
    for m in mons:
        m["fp"] = _mon_fingerprint(m)
    return mons


# ── Overlay ──────────────────────────────────────────────────────────

class Overlay:
    def __init__(self, offset=0.0):
        self.root = tk.Tk()
        self.root.title("Lyric Immersion and Karaoke")
        self.offset = offset

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.W, self.H, self.sh = sw, 340, sh
        wa = _work_area()
        self.work_left, self.work_top, _wr, self.work_bottom = wa or (0, 0, sw, sh - 48)
        self.work_h = self.work_bottom - self.work_top
        self._win_margin = 28          # gap from the work-area edge (top)
        # Bottom-anchored lyrics sit this far above the work-area bottom so they
        # clear a media player's now-playing bar (YouTube ~60px, Spotify ~90px)
        # and stay readable instead of hugging the screen edge.
        self._bottom_clear = max(56, round(self.work_h * 0.10))
        self._lane_y0 = self._win_margin
        # Responsive sizing: scale text to the display so a big TV / 4K screen
        # gets proportionally larger lyrics automatically (≈1.0 on 1080p). The
        # tray font % is a multiplier ON TOP of this auto base.
        self._auto_scale = min(2.5, max(0.7, self.work_h / 1000.0))
        # The overlay window is FIXED to the whole work area and never moves or
        # resizes — content is positioned inside it. This is what stops lyrics
        # from drifting down. It's made click-through below so covering the
        # screen doesn't block anything.
        self.H = self.work_h

        s = _load_settings()
        self.opacity = float(s.get("opacity", 1.0))
        # Position is now TWO independent axes — a vertical AND a horizontal anchor,
        # chosen separately. Migrate the old single 'position' value.
        _op = s.get("position", "bottom")
        self.pos_y = s.get("pos_y", {"top": "top", "center": "center",
                                     "left": "center", "right": "center"}.get(_op, "bottom"))
        self.pos_x = s.get("pos_x", {"left": "left", "right": "right"}.get(_op, "center"))
        self.display = s.get("display", "primary")     # 'primary' | 'mon:N' | 'span'
        self._display_fp = s.get("display_fp")         # fingerprint for monitor identity
        self._mon_snapshot = ()                         # current monitor topology
        self._last_mon_check = 0.0                     # throttle for _check_monitors
        self.scroll_dir = s.get("scroll", "left")      # 'none'|'left'|'right'|'top'|'bottom'|'lr'|'rl'|'tb'|'bt'
        self.scroll_speed = float(s.get("scroll_speed", SCROLL_SPEED))
        self.font_scale = float(s.get("font_scale", 1.0))  # 0.25 … 2.0
        self.perf = s.get("perf", "smooth")            # 'smooth' | 'fast'
        self.recal_secs = int(s.get("recal_secs", 4))   # re-check by sound often (0=off)
        self.git_sync = bool(s.get("git_sync", False))  # push new songs to git
        self.character_on = bool(s.get("character", False))  # dancing companion
        self.api_on = bool(s.get("api", True))         # local agent-control API
        self.boundary_on = bool(s.get("boundary", True))  # fast song-change detect
        self.generate_on = bool(s.get("generate", True))  # generate lyrics by ear
        self.captions_on = bool(s.get("captions", True))   # prefer YouTube caption track for browser videos
        self.concert_ocr = bool(s.get("concert_ocr", True)) # banner-text fallback song-ID during concerts
        # TICKET-100: opt-in Discord Rich Presence reader; default OFF (mirrors
        # generate_on — both are "extra effort" features users opt into). Persists
        # under settings key 'discord_rpc'. Tray toggle and tune knob both write
        # the same runtime flag; whichever changes persists via _persist().
        self.discord_rpc_on = bool(s.get("discord_rpc", False))
        # TICKET-102: opt-OUT scrape of allowlisted-process window titles for
        # CEF/Electron hosts that don't publish SMTC (Steam Overlay, embedded
        # Discord/Slack/Teams players). Default ON because the user-observed
        # failure mode (steamwebhelper.exe playing ReGLOSS silent on SMTC) is
        # exactly what this fixes; the allowlist is narrow + music-purposed.
        # Generic standalone browsers are a separate, default-OFF tier — they
        # already feed SMTC and scraping them risks double-counting unrelated
        # tabs (Gmail, banking, chat).
        self.window_titles_on = bool(s.get("window_titles", True))
        self.window_titles_generic_browsers_on = bool(
            s.get("window_titles_generic_browsers", False))
        # Throttle state for the Discord RP poll (own clock, decoupled from the
        # tick rate so the GET_ACTIVITY round-trip can't run more than every
        # discord_rpc_poll_s seconds even if the Tk loop is ticking at 60 Hz).
        self._discord_last_poll_t = 0.0
        # Last (title, artist) seen on the Discord pipe — kept so a quiet poll
        # doesn't drop the loaded lyrics the moment the round-trip times out.
        self._discord_last_track = None
        # Last wall-time the SMTC OR Shazam source produced a usable track; the
        # Discord fallback only contributes after a continuous silent gap of
        # `discord_rpc_silent_gap_s` seconds (default 8.0). Bumped on the tick
        # loop whenever SMTC has a title OR Shazam's _sound_song is set.
        self._music_source_last_t = time.time()
        self._last_ocr_t = 0.0        # throttle the concert OCR check
        self._ocr_song = None         # last song the banner OCR confidently read
        self._generating = False      # Whisper lyric-generation in progress
        self._gen_token = 0           # bumped on track change / real-lyric load to stop generation
        self._track_seq = 0           # bumped per track change (gates the generation deadline)
        self._gen_lines = []          # accumulated generated line dicts
        self._gen_title = self._gen_artist = ""
        self._gen_lang = None         # language auto-detected for the current generation
        self._deep_token = 0          # bumped on track change → cancels in-flight deep transcription
        self._deep_tried = set()      # song slugs we've already attempted a deep upgrade for
        self._title_locked = False    # exact clean-title match → sound can't override
        self._api = None
        self._boundary = None         # song-change detector thread
        self._last_boundary = 0.0     # throttle: last boundary-triggered identify
        self._fps = 16
        self._last_pos = 0.0
        self._strm_rem = 0.0
        self._tick_n = 0           # frame counter for throttling the heavy ticker work
        self._fill_skip = 3        # spawn/despawn/fill every Nth frame (set by _apply_perf)
        self._last_raw_title = None  # cache so clean_title()/clean_artist() aren't
        self._last_src = None        # re-run every frame
        self._last_artist = None
        self._clean_title_cache = ""
        self._clean_artist_cache = ""
        self._is_cover = False       # current title is a 歌ってみた / cover (title-first fetch)
        self._cover_signal = None    # TICKET-086: 'explicit' / 'amp_collab' / None
        self._cover_original_artist = None   # original artist extracted from a cover title
        self._last_caption_t = 0.0   # throttle YouTube caption fetches (rate-limit guard)
        self._caption_song = None    # (artist,title) we last attempted captions for
        self._captions_fetching = False  # single-flight: one yt-dlp caption fetch at a time
        self._now_url = None         # exact current video URL (browser-pushed, for captions)
        self._frame_ms = 0.0         # EWMA of render-frame interval (ms) → /status render_fps
        self._last_tick_t = None
        self._render_frame = False
        self._frame_worst = 0.0      # worst (longest) render-frame interval in the window
        self._frame_jitter = 0.0     # EWMA of |frame - target| — stutter metric
        self._frame_hist = []        # ring buffer of recent frame intervals (ms)
        # TICKET-082: perf recorder — buffered append on the Tk thread so it
        # captures the truth of each frame without polling-induced contention.
        # Opens lazily on first frame when perf_record=1.
        self._perf_fh = None
        self._perf_path = None
        self._perf_last_offset = None
        self._perf_last_idx = None
        # A2 sub-branch timing: dict reset every _tick; populated by _perf_branch.
        # Values are "last completed call within this tick" (a cancelled _render
        # leaves the prior value stale, document in _perf_record).
        self._perf_branch_ms = {}
        # Cached parsed set so we don't re-split the pipe string every tick.
        self._perf_branches_raw = None
        self._perf_branches_set = set()
        # A2 raw frame dt (untrusted samples → None so the log shows '-').
        self._raw_dt_ms = None
        self._display_offset = None
        self._display_offset_t = 0.0
        self._spawn_budget = 1       # max scroll blocks to PIL-render per heavy frame
                                     # (1: spawning a block allocates a new image +
                                     # PhotoImage — the priciest op; off-screen, so
                                     # spreading it across frames is invisible)
        self._repaint_budget = 2     # max karaoke-fill repaints per heavy frame
        self._fill_interval = 0.06   # min seconds between a block's fill repaints (~16fps —
                                     # smoother sung-fill; the sliver paste is cheap, see _advance_fill)
        self._apply_perf()
        self._anim_id = None
        self._scroll_x = self._scroll_start = self._scroll_end = 0
        self._stream = []          # scroll-through ticker: live line blocks
        self._blk_seq = 0
        # PERF-102: cache each line's rendered base/sung BITMAP keyed by line idx.
        # Rendering a long 1.5×-scaled furigana block is ~150-450 ms (hundreds of
        # stroked-glyph draws); a line re-enters constantly (repeated choruses, edge
        # despawn/respawn, lane churn), so rendering it ONCE and reusing the bitmap
        # turns each repeat from a frame-killing spike into a cheap PhotoImage wrap.
        # Insertion-ordered dict used as an LRU; cleared on song load / scale change.
        self._block_cache = {}
        self._block_cache_max = 32
        self._cache_lock = threading.Lock()   # _block_cache shared with the prewarm thread
        self._prewarm_token = 0               # bumped on song load to cancel a stale prewarm
        # GLYPH ATLAS (PERF-102): render each (glyph, font, colour, stroke) ONCE into
        # a tiny cached tile; a line is then composed by PASTING tiles instead of
        # re-rasterising ~180 stroked glyphs. Measured 8× faster per line (30 → 3.5 ms)
        # — the per-line render stops being a scroll spike. Session-wide (glyphs reuse
        # across songs), LRU-capped.
        self._glyph_cache = {}
        self._glyph_cache_max = 4000
        self._pil_fonts = {}       # cache of PIL fonts for image blocks
        self._use_img = True       # image scroll blocks. Measured: a text-item block is
                                   # FAR worse here — cv.move of a full stream (~7k text
                                   # items) re-rasterizes every glyph at ~480ms/frame (2fps).
                                   # Images are pre-rasterized (cheap to move); the only cost
                                   # is the ~50ms PhotoImage create/paste, paid just on
                                   # spawn/fill-repaint and throttled below.
        self._v_floor = 0.0        # min scroll speed for THIS song (anti-overlap)
        self._apply_scale()                            # sets fonts + layout + H

        self.root.overrideredirect(True)
        self.root.geometry(f"{self.W}x{self.H}+{self.work_left}+{self.work_top}")
        self.root.configure(bg=TRANSPARENT)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", TRANSPARENT)
        self.root.attributes("-alpha", self.opacity)
        self.root.update_idletasks()

        self._click_through()   # make the overlay pass mouse input straight through
        self.root.after(1500, self._click_guard)   # self-heal if anything resets it

        self.cv = tk.Canvas(self.root, bg=TRANSPARENT, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)

        self._mirrors: list[tk.Toplevel] = []    # mirror-mode clone windows
        self._cycle_idx = 0                      # cycle-mode: current monitor index

        self.lines: list[Line] = []
        self.meta: dict = {}
        self.idx = -1
        self._track = None
        self._lyrics_path = None
        self._kara = []
        self._line_left = self._line_right = 0
        self._fetch_key = None
        self._fetch_result = None
        self._fetching = False        # a provider lookup is in flight (defer generate)
        self._gen_defers = 0          # times generation waited on the in-flight fetch
        self._translate_result = None
        self._translating = None
        self._cur_duration = None
        # TICKET-099: split verification into TENTATIVE (duration/title check at
        # load time — _verified_meta) and CONFIRMED (sound has corroborated the
        # loaded title at least once — _sound_corroborated). The public-facing
        # /status.verified is `_verified` (both true). Old offline-only paths
        # (title-lock gate, decide-by-ear gate) keep using `_verified_meta` so
        # an offline / Shazam-fails session doesn't regress.
        self._verified = False           # public api.verified: meta AND sound-corroborated
        self._verified_meta = False      # internal: duration/title match at load time
        self._sound_corroborated = False # internal: ≥1 Shazam read agreed with loaded title
        # TICKET batch4: wall-clock of the most recent True→False transition
        # of self._verified. Drives the verified_render_gate_s grace window
        # (lyrics on screen survive a transient disagreement instead of
        # tearing down immediately). 0 = not currently in a gate window.
        # Cleared on _on_track_change (real song change is not transient).
        self._verified_gate_t = 0.0
        # TICKET-099: paused-SMTC Shazam takeover tracking.
        # _smtc_paused_since: wall-clock when SMTC most recently EDGED PLAYING→not-PLAYING (0 = currently playing or unknown)
        # _last_takeover_t:   wall-clock of the last takeover swap (debounces back-to-back swaps)
        # _last_smtc_playing: edge detector for the pause-since timer
        self._smtc_paused_since = 0.0
        self._last_takeover_t = 0.0
        self._last_smtc_playing = None
        # _source_priority: telemetry for /diag — last decision from _resolve_source_priority()
        self._source_priority = "agree"
        self._health_attempts = 0
        self._identifying = False
        self._aligning = False        # sync-by-listening (Whisper) in progress
        self._last_align_t = 0.0      # wall-clock of last auto-align finish
        self._last_audio_off = None   # Shazam-implied absolute offset, for correlator sanity
        self._last_audio_off_t = 0.0  # wall-clock of last audio_off update
        self._sound_title_alias = None  # romanized heard-title we loaded a CJK cache for
        self._last_energy = None      # last energy-correlation result (for /diag)
        self._offset_hist = []        # ring buffer of (wall_t, offset) — catches jumps
        self._offset_hist_last = None # last offset recorded (only log on change)
        self._auto_align_after = None # pending auto-align timer id
        self._live_resync_after = None # pending live lyric-resync timer id
        self._live_resync_inflight = False  # the in-flight align IS a live resync (for cadence verdicts)
        self._live_sync_streak = 0     # consecutive GOOD live resyncs (drives the de-escalation)
        self._live_resync_gap = None   # current post-listen gap, seconds (None → fast/8×min)
        self._last_sound_lock_t = 0.0 # wall-clock of last confirmed Shazam offset
        # ── Algorithmic offset state (continuous, no strike counters) ──
        # `_drift_integral` accumulates |drift| × time between Shazam reads:
        # 1.5s drift for 4s = 6.0 integral. Triggers auto-align when it
        # crosses 6.0 (≈ a 1s drift held for 6s, or a 3s drift held for 2s).
        # No arbitrary count threshold — proportional to how wrong the sync
        # actually is over time.
        self._drift_integral = 0.0
        self._drift_integral_t = 0.0  # wall-clock when last updated
        # ── Live-tunable sync parameters (no rebuild needed) ──
        # Exposed via GET/POST /tune so the values can be adjusted at runtime
        # while watching live behavior. Defaults below are the values that
        # shipped — overrides reset to these on restart.
        self._tune = {
            "deadband":              0.8,   # |drift| below this is left alone
            "agree":                 2.0,   # 2-read agreement window (s)
            "agree_live":            4.0,   # live-arrangement agreement window
            # ── two-point sound-verification timing (TICKET-056) ──
            # hold a candidate offset, hesitate this long, then take a confirming
            # listen; the 2nd read must land within "agree" of the 1st to commit.
            # A LONGER hold separates the two reads by more song time, so two
            # different instances of a repeated chorus can't both be read at the
            # same offset and falsely "agree" ("All The Things She Said" symptom).
            "sync_confirm_hold_ms": 2600,   # hesitation before the confirming listen
            "sync_confirm_listen_s": 5.0,   # confirming-listen capture length
            "applause_min_s":        2.5,   # loud-non-vocal seconds = a concert applause gap
            "live_resync_s":         6.0,   # (legacy) LIVE/concert lyric-match RESYNC cadence (TICKET-106: 12.0 → 6.0)
            # Concert/live ARRANGEMENTS resync on a rolling, aggressive de-escalation:
            # start ~12×/min (TICKET-106; was ~8); after 3 good reads in a row relax to
            # ~6×/min; after 3 more to ~3×/min; ANY miss snaps straight back to fast so a
            # drifting concert gets hammered until it re-locks. (gap = pause AFTER a
            # listen; period≈listen+gap)
            "live_resync_listen_s":  4.0,   # TICKET-106: 6.0 → 4.0 (shorter capture, faster cycle)
            "live_resync_fast_gap_s": 1.0,  # TICKET-106: 1.5 → 1.0 (~12×/min while missing / fresh song)
            "live_resync_mid_gap_s":  6.0,  # ~6×/min  (after 3 good reads)
            "live_resync_slow_gap_s": 14.0, # ~3×/min  (after 6 good reads — locked in)
            "live_resync_relax_n":     3,   # good reads per de-escalation step
            "spread_reset":         20.0,   # chorus-ambiguity spread threshold
            "reset_offset_max":      5.0,   # only reset when |offset| < this
            "drift_align_trigger":   6.0,   # integral → trigger auto-align
            "drift_min_for_accum":   0.8,   # |drift| > this contributes to integral
            "auto_align_cooldown":  14.0,   # min s between auto-aligns (< fast tier so it doesn't throttle it)
            "auto_align_min_pos":   12.0,   # min player pos before auto-align
            "shazam_lock_grace":    30.0,   # auto-align skipped within N s of lock
            # ── TICKET-099: SMTC-paused Shazam takeover ──
            # When SMTC reports the session is NOT playing (paused / stopped) and
            # Shazam confidently hears a DIFFERENT song from what's loaded, the
            # paused SMTC tab cannot be what's actually audible in the room. After
            # smtc_paused_min_s seconds of continuous non-PLAYING SMTC AND two
            # Shazam reads that agree on the heard (title, artist), drop the
            # paused-SMTC lyrics and switch to what we hear. Debounced by
            # smtc_takeover_debounce_s so a fresh takeover gets time to settle
            # before another can fire; an SMTC PAUSED→PLAYING flip bypasses the
            # debounce (a real user un-pause is the authoritative cancel signal).
            # Set smtc_paused_shazam_takeover=0 to disable entirely (preserves
            # v1.0.88 behavior: paused SMTC stays loaded, decide-by-ear is the
            # only override).
            "smtc_paused_shazam_takeover": 1,    # 1 = enable takeover; 0 = disable
            "smtc_paused_min_s":           8.0,  # continuous non-PLAYING floor before takeover may fire
            "smtc_takeover_debounce_s":   20.0,  # min seconds between consecutive takeovers
            # ── TICKET batch4 A3: title-alias album fallback ──
            # When Shazam returns the ALBUM name instead of the track name (very
            # common with V.W.P style releases where Shazam responds with e.g.
            # 'DIVA (feat. KAF, RIM, Harusaruhi, Isekaijoucho & KOKO)' while
            # SMTC has the actual track '歌姫'), the existing wrong-song strike
            # flow used to spend 70+s tearing the lyrics down on a benign
            # disagreement. With this knob ON (default 1), structural markers
            # in the Shazam title ('(feat.', ' - EP', ' (Album)', ' Vol.', or
            # ALL-CAPS multi-word ASCII) make us accept SMTC as canonical and
            # set _sound_title_alias = Shazam_title so subsequent reads still
            # calibrate via the existing alias path. Set 0 to disable and fall
            # back to v1.0.90 strike behavior for diagnosis.
            "title_alias_album_fallback":   1,   # 1 = album-string fallback on; 0 = off
            # ── TICKET batch4: verified→False render grace window ──
            # A True→False flip of self._verified used to start the teardown of
            # lyrics immediately (the 71s outage in workflow w821l9jnw was a
            # benign Shazam disagreement that demoted verified, then the
            # downstream tear logic kicked in for the full window). Hold the
            # render-side teardown for this many seconds after the most recent
            # True→False transition so a re-confirming Shazam read (or the
            # album-alias path above) can bring verified back without ever
            # clearing self.lines. _on_track_change clears the gate immediately
            # — a real new song must wipe. /diag exposes
            # verified_render_gate_remaining_s so the live window is visible.
            "verified_render_gate_s":     3.0,
            "unconfirmed_backoff_s": 30.0,  # settled-but-unconfirmable song → slow the Shazam poll (anti-stutter)
            "confirmed_recal_s":     45.0,  # confirmed+watched song → slow Shazam re-lock (tier handles drift)
            "wrong_song_strikes":     5,    # heard the SAME other song this many × → loaded song is wrong, switch
            "sync_reject_strikes":    3,    # sync-by-ear heard vocals but couldn't ANCHOR them to the loaded
                                            # lyrics this many × in a row → the cache is the WRONG song (a
                                            # mislabeled/poisoned LRC that title+Shazam pass on NAME) → reject
            # ── SMART song decision by ear (Whisper 'small' + rapidfuzz, ~250 MB) ──
            "decide_min_score":      55.0,  # a title candidate must match the heard singing ≥ this to win
            "decide_library_min":    60.0,  # a WHOLE-LIBRARY identification must clear this higher bar
                                            # (TICKET-081: was 70 — kamone scored 69 vs loaded 20, a clear win
                                            #  rejected by 1 point of an absolute threshold. The lopsided
                                            #  override + sync-reject still catch real misfires.)
            # ── TICKET-082 perf instrumentation ──
            "perf_record":           0,     # 1 = log per-frame perf to perf.log (near-zero observer effect,
                                            #     writes on Tk thread to a buffered append). Off by default.
            "perf_record_path":      "",    # explicit path; if empty + perf_record=1 → <data>/perf.log
            "perf_record_cap_mb":    20.0,  # rotate the perf log when it grows beyond this
            # A2 sub-branch timers: pipe-separated set of branches to bracket with
            # time.perf_counter() in the hot path. Default covers the three known
            # offenders from workflow w821l9jnw (LINE-mode stutter root cause).
            # Drop entries (e.g. "render|kara") once a branch is ruled out to
            # cut the per-frame overhead further. Unknown branches are no-ops.
            "perf_record_branches":  "render|kara|itemconfig",
            # A2 raw frame dt: when 1, the perf log adds a `raw_dt_ms` column
            # right after `frame_ms` so a single stall stands out instead of
            # being smeared by the 0.9/0.1 EWMA. 0 = legacy format (EWMA only).
            "perf_record_raw_frame_ms": 1,
            "ease_slew_cap_s":       3.0,   # max seconds-per-second the eased offset can slew
            "ease_pull_per_sec":     3.5,   # exponential pull rate (higher = catches up faster)
            # TICKET-088: per-frame fraction cap so a single heavy frame (e.g. 300ms
            # stall) cannot consume more than this fraction of the remaining delta.
            # The exponential-pull formula step = delta * (1 - exp(-pull * dt)) with
            # dt=0.3, pull=3.5 yields ~65% of delta in ONE frame = visible snap. Cap
            # at 20% so even a quarter-second stall still glides over ~5 frames.
            "ease_max_step_frac":   0.20,   # max fraction of remaining delta per frame
            # TICKET-088: snap deadzone for sub-50ms drifts — below this, easing is
            # a waste (the visual change is below human perception) and the residual
            # ramp wastes work. Old hardcoded threshold was 10ms.
            "ease_deadzone_s":      0.05,   # |target - cur| below this snaps to target
            # TICKET-088: gate a debug assertion that warns when more than 2 offset
            # writes happen in a single _tick (a sign the same-tick ordering is
            # being violated). 0 = silent, 1 = log warnings.
            "assert_same_tick":        0,   # 1 = log when >2 offset writes per tick
            "decide_margin":         12.0,  # …and beat the loaded song's match by this much to switch
            "decide_wrong_floor":    32.0,  # loaded match below this = the lyrics are the wrong song → search the library
            "decide_listen_s":       12.0,  # seconds of vocals to transcribe for the decision
            "decide_at_s":           20.0,  # run the by-ear decision this many s into a new track
            # TICKET-090: a Shazam-VERIFIED song whose loaded title also matches the
            # heard title is ground truth — the by-ear decide loop has nothing useful
            # to add and a noisy/hallucinated transcription can only POISON it by
            # ranking the wrong song. Default OFF: verified+title-locked stops decide.
            # Set to 1 for paranoid mode (keep deciding even when locked) when
            # diagnosing a mis-lock.
            "decide_after_verified": 0,     # 0 = locked verified stops decide, 1 = keep deciding
            # ── TICKET-109 decision engine knobs ──
            # Watches SMTC<->Shazam agreement, drift trend, lyric-quality flags,
            # and decide-by-ear corroboration; aggregates dim scores into strikes;
            # promotes state TRUST -> CAUTION -> SWITCH -> REGEN at the thresholds
            # below. User-visible via tray hint + /diag.decision_engine.
            "decision_engine_on":           1,
            "decision_caution_strikes":     3,
            "decision_switch_strikes":      5,
            "decision_regen_strikes":       8,
            "decision_score_window":       12,
            "decision_tick_interval_s":   2.0,
            "decision_action_cooldown_s": 30.0,   # min seconds between SWITCH/REGEN actions
            "continuous_recal_ms": 15000,   # legacy fixed cadence (superseded by the adaptive tier)
            # ── ADAPTIVE sync-verification tier (escalation / de-escalation) ──
            # Verify sync ~3×/min while syncing or after a miss; once a check CONFIRMS
            # we're in sync, relax toward 1×/min; ANY miss snaps back to fast and
            # resyncs (two-point verified). Endpoints are the user's 3×/min ↔ 1×/min.
            "sync_tier_fast_s":     20.0,   # escalated verify cadence (~3×/min)
            "sync_tier_mid_s":      40.0,   # one hysteresis step (after 1 good check)
            "sync_tier_slow_s":     60.0,   # relaxed cadence (1×/min) once sync holds
            "sync_tier_ok_drift":    1.2,   # TICKET batch1: 0.8 → 1.2 — unblocks tier scheduler on periodic JP tracks
            "sync_tier_listen_s":    4.0,   # TICKET batch1: 6.0 → 4.0 — faster retry cadence
            # ── FORCE SYNC (manual nuclear resync) ──
            "force_sync_streak":       3,   # confirming reads in a row before a candidate locks
            "force_sync_listen_s":   8.0,   # transcribe capture length per Force-Sync read
            "force_sync_agree_s":    1.0,   # a fresh read within this of the tried offset = "still matches"
            "force_sync_span_s":    16.0,   # confirms must span this many player-seconds before locking
                                            # (so it can't lock inside ONE repeating chorus pass)
            "force_sync_top_n":        6,   # candidate offsets ranked per read (try best→next on failure)
            "energy_apply_min":      0.4,   # min |new-old| to apply correlation
            "energy_lift_floor":     0.10,  # min peak-vs-median lift to accept
            "energy_max_offset":    60.0,   # |new_off| < this for sanity
            "energy_shift_penalty":  0.012, # per-second penalty for large offset changes (small-shift prior)
            "energy_peak_margin":    0.06,  # reject if a distant rival peak is within this of the best
            # ── render perf knobs (SCROLL-MODE ONLY — scroll-through smoothness) ──
            # scroll_heavy_budget_ms caps the per-frame spawn/repaint work so a PIL
            # paste can't stall the scroll belt; 0 disables the cap. NONE of these
            # gate LINE-mode work (line mode's per-char measure_text + per-font
            # canvas ascent measurement is unbudgeted — see TICKET-104/105).
            "scroll_heavy_budget_ms": 14.0, # scroll-mode only: max ms of spawn+repaint work per heavy frame (40% more PIL slice with +1 core)
            "scroll_repaint_budget":   3.0, # scroll-mode only: max karaoke-fill SLIVER pastes per heavy frame (cheap now)
            "scroll_fill_interval":   0.04, # scroll-mode only: min seconds between a block's fill repaints (25 fps cap; was 0.06=16 fps)
            "scroll_spawn_budget":     1.0, # scroll-mode only: max block PIL-renders per heavy frame (alloc spikes still dangerous, keep low)
            "scroll_fill_skip":        2.0, # scroll-mode only: heavy work runs every Nth frame (fills are sliver-cheap)
            # PERF-102 — scroll bitmap-area controls (the dominant scroll cost):
            "scroll_max_lanes":      3,     # stacked scrolling lines (capped to what fits on screen)
            "scroll_v_stagger":    250,     # VERTICAL scroll: horizontal stagger (px@scale1) between
                                            # the 2-3 staggered columns so lines cascade diagonally
                                            # (centre mode isn't bound by the L/R pad — it fans wide)
            "scroll_spawn_margin": 1100,    # px off-screen a block is pre-rendered (avoid pop-in)
            # TICKET-104 A1 — bounded LRU cache for measure_text widths. Default
            # 4096 comfortably holds the ~3000-5000 (char, font) working set per
            # song with headroom for font_scale transitions. The cache is
            # MODULE-LEVEL (_MEASURE_CACHE) so this knob takes effect at next
            # app restart, not on live tune-knob edits. Bump if /diag shows
            # measure_text_cache_hit_rate dropping under steady-state playback.
            "measure_text_cache_size": 4096,
            # MV/cinematic intro hold backstop (primary release is the vocal poll).
            # Kept SHORT: a false "waiting for vocals" while the song is already
            # singing is worse than briefly running into a genuine long intro, so
            # the hold can never sit through more than this many seconds of vocals.
            "mv_intro_timeout":     20.0,   # s before the intro card releases regardless
            # ── FINE-TUNE sync mode (sub-second polish layer atop the normal tier) ──
            # After the regular tier has reported in-sync for `fine_tune_enter_after_s`
            # of wall-time, engage a fast 8s-cadence polishing loop that drives drift
            # toward `fine_tune_target_s` (0.2 s) using a VISUALLY GENTLE mechanism:
            # for lyrics-ahead drift it PAUSES the lyric procession by `drift` seconds
            # (line idx + karaoke fill freeze together) then re-bases self.offset by
            # the same amount, so the resumed frame matches the held frame — no snap.
            # For lyrics-behind drift it uses the existing _smooth_offset (boundary-
            # deferred) path. Anything outside the [-1.5, +1.5] s envelope exits to
            # the normal tier (which has the two-point verifier for big moves).
            "fine_tune_enter_after_s":   20.0,  # wall-time in good streak before entering
            "fine_tune_target_s":         0.2,  # |drift| at-or-below this = locked
            "fine_tune_min_step_s":       0.2,  # smallest pause; below this is in-target
            "fine_tune_max_pause_s":      3.0,  # TICKET-104: bumped 1.0 -> 3.0 per user; holding a line still up to 3 s is quieter than the equivalent backward nudge that re-scrolls already-shown text
            "fine_tune_max_move_ahead_s": 2.0,  # biggest backward-drift catch-up nudge (lyrics behind); higher cap because skipping forward is less perceptible than pausing
            "fine_tune_exit_drift_s":     3.5,  # TICKET-104: must be > fine_tune_max_pause_s + 0.5 buffer so a drift just under the cap doesn't immediately hand back to the tier
            "fine_tune_listen_interval_s": 8.0, # cadence between fine-tune Whisper listens
            "fine_tune_inconclusive_exit":   2, # consecutive unreadable listens → exit
            # ── TICKET-086 YouTube Music + ampersand-collab cover knobs ──
            # 1.0 = ON (default). Drop to 0 to disable the demote, e.g. for
            # diagnosis. Only affects the WEAKER amp_collab signal; an explicit
            # cover tag (歌ってみた / [COVER] / "covered by") is never demoted.
            "cover_amp_album_demote":     1.0,  # YT Music album → demote amp_collab
            # ── TICKET-103 GPU policy ──
            # 1 = override the single-GPU-stays-on-CPU safety floor (use the
            # one GPU anyway). 0 = honor the policy. Multi-GPU machines are
            # unaffected (they always get the idlest GPU when not gaming and
            # the non-game-card when gaming).
            "gpu_solo_override":            0,  # 1 = allow GPU on single-GPU machines, 0 = stay on CPU per policy
            # ── TICKET-089 Whisper language lock ──
            # 1 = pin Whisper to the song's known language for deep transcription
            # (the auto-detect default lets Whisper hallucinate Japanese on
            # Spanish/English/Korean audio — live diag saw Calibre 50 ES return
            # "てれこにでもなくすまさきまで…"). 0 = revert to auto-detect for A/B
            # testing. Only languages on the whitelist below are pinned; "ja" is
            # left on auto since the library is mostly Japanese already and the
            # current per-chunk detection is working there.
            "whisper_lang_lock":            1,  # 1=lock to self._gen_lang/meta lang, 0=auto-detect
            # ── TICKET-100: Discord Rich Presence reader (Spotify Listening) ──
            # Opt-in (default OFF) third-priority music-source. Only contributes
            # (title, artist) when BOTH SMTC and the live Shazam source are silent
            # for >= 8s; never overrides a real SMTC track and never supplies a
            # position/clock. Useful when audio plays on a device that does NOT
            # expose SMTC (e.g. iPhone Spotify → BT speaker, while the laptop's
            # Discord client shows "Listening to Spotify"). Tray toggle and tune
            # knob both write the same runtime flag (self.discord_rpc_on); when
            # either changes it persists to settings. Lazy: no thread, no pipe
            # probes, no module import until first poll while ON.
            #
            # NOTE TICKET-101 (out of scope for TICKET-100): per-game Rich
            # Presence parsing (rhythm games publishing 'now playing' in their
            # RP, e.g. Muse Dash, beatmania) would land behind a SEPARATE
            # 'discord_game_rpc' knob (default 0) plus a per-application_id
            # allowlist. Most games don't populate music info in RP and those
            # that do use ad-hoc string formats requiring per-game parsers, so
            # deferring keeps this ticket focused on the high-value Spotify case.
            "discord_rpc_on":               0,  # 1 = read Discord RP as a fallback source, 0 = off
            "discord_rpc_silent_gap_s":   8.0,  # SMTC+Shazam must be silent this long before Discord can speak
            "discord_rpc_poll_s":         5.0,  # min seconds between GET_ACTIVITY probes (matches Discord's RP min interval)
            "discord_rpc_timeout_s":      0.5,  # hard cap on a single IPC call — must never block the Tk loop
            # ── TICKET-102: window-title scraper (Steam Overlay / Discord) ──
            # The HIGH tier (steamwebhelper, discord, slack, teams) is default
            # ON: a narrow, music-purposed allowlist that fixes the SMTC-blind
            # CEF/Electron case the user reported. The LOW tier (chrome, edge,
            # firefox, opera, brave, vivaldi, arc) is default OFF because those
            # browsers already feed SMTC and scraping ALL their tabs risks
            # picking up unrelated content (Gmail, Twitter, podcasts).
            "window_titles_on":             1,  # 1 = scrape allowlisted CEF/Electron windows
            "window_titles_generic_browsers": 0,  # 1 = ALSO scrape chrome/edge/firefox/etc (opt-in)
            "window_titles_poll_s":       2.0,  # background poll cadence (s); EnumWindows is sub-ms so 2s is safe
        }
        # TICKET-104 A1: apply measure_text cache size ONCE at startup. The
        # cache is module-level (shared across Overlay/Mirror), so we set the
        # cap here rather than per-call (which would re-introduce the
        # dict-lookup overhead the LRU is meant to eliminate). Live /tune POST
        # changes to this key are ignored until next restart by design.
        try:
            global _MEASURE_CACHE_MAX
            _MEASURE_CACHE_MAX = int(self._tune.get("measure_text_cache_size",
                                                    _MEASURE_CACHE_MAX))
        except Exception:
            pass
        # TICKET-100: mirror the persisted toggle into the tune dict so a user
        # who flipped the tray menu ON in v1.0.89 boots back into the same
        # state at v1.0.90+ (the dict literal above defaults to 0; the persisted
        # bool is the source of truth at startup).
        try:
            self._tune["discord_rpc_on"] = 1 if self.discord_rpc_on else 0
        except Exception:
            pass
        # BUG-2/5/6: if the persisted toggle is ON at startup, kick off the
        # long-lived watcher daemon now so the first _tick poll already has
        # a fresh slot to read. Failures (e.g. discord_rpc import error) are
        # swallowed — the feature is best-effort.
        if self.discord_rpc_on:
            try:
                import discord_rpc as _drpc
                _drpc.start_watcher(
                    poll_s=float(self._tune.get("discord_rpc_poll_s", 5.0)))
            except Exception:
                pass
        # TICKET-102: mirror the persisted window-title toggles into the tune
        # dict so /tune queries reflect the live boot state.
        try:
            self._tune["window_titles_on"] = 1 if self.window_titles_on else 0
            self._tune["window_titles_generic_browsers"] = (
                1 if self.window_titles_generic_browsers_on else 0)
        except Exception:
            pass
        # Throttle state for the per-tick window-title fallback (own clock so
        # the slot read can't outpace the watcher's enum cadence).
        self._window_titles_last_t = 0.0
        self._window_titles_last_track = None
        if self.window_titles_on:
            try:
                import window_titles as _wt
                _wt.start_watcher(
                    poll_s=float(self._tune.get("window_titles_poll_s", 2.0)),
                    generic_browsers=self.window_titles_generic_browsers_on,
                )
            except Exception:
                pass
        self._identify_result = None
        self._sound_song = None       # last (title, artist) heard by Shazam
        # (_last_sound_lock_t is initialized earlier with the rest of the
        # sync-state; the Discord RP fallback in _tick also keys off it so
        # the "is Shazam ACTIVELY producing music?" gate decays cleanly.)
        self._pending_corr = 1e9      # a large sound offset awaiting a 2nd confirming read
        self._sync_confirm_after = None  # pending 2s "confirm with a 2nd listen" timer
        self._recent_corr = []        # last few audio offsets — spot repeated-chorus ambiguity
        # TICKET-078: defer auto-sync corrections to the next line boundary so the
        # CURRENT line (even if mis-synced) finishes naturally and the next line
        # appears under the new offset. Less jarring than a mid-line snap.
        self._pending_offset = None   # queued new offset; commits when current line ends
        self._pending_offset_t = 0.0  # when it was queued (capped so it can't stall forever)
        self._live_arrangement = False  # LIVE/short/alt version → FOLLOW the offset, don't reset
        # Concert applause/cheering-pause detection (TICKET-061): a live cut pauses
        # for applause while the player clock runs on, drifting the lyrics ahead.
        self._applause_for = 0.0      # accumulated loud-but-non-vocal (applause) time
        self._applause_t = 0.0        # last applause-check timestamp
        self._applause_armed = False  # a real gap was seen → resync when singing returns
        self._align_tpvr_active = False  # the pending align is an applause two-point resync
        self._align_tpvr = None       # 1st-read offset awaiting a 2nd confirming read
        self._align_tpvr_until = 0.0  # deadline for the 2nd confirming read
        # Adaptive sync-verification tier (escalation/de-escalation — user request):
        # verify ~3×/min while syncing, relax to 1×/min once confirmed, snap back to
        # fast on any miss. The cheap energy correlator gives the verdict when it can;
        # when it's blind on a song (flat/ambiguous peak) the tier escalates to a
        # short, two-point-verified Whisper listen.
        self._sync_tier_interval = 20.0  # current verify cadence (s): 20=fast(3×/min) … 60=relaxed(1×/min)
        self._sync_good_streak = 0    # consecutive in-sync confirmations (drives de-escalation)
        self._sync_miss_streak = 0    # consecutive out-of-sync checks (telemetry / stay-fast)
        self._sync_incon_streak = 0   # consecutive unreadable checks → back off (don't hammer)
        self._sync_fail_streak = 0    # consecutive Whisper reads that heard vocals but couldn't ANCHOR to the
        self._sync_reject_count = 0   # loaded lyrics → cache is the wrong song; reject after N (capped/track)
        self._energy_blind = 0        # consecutive energy checks with no usable peak → escalate to Whisper
        self._tier_tpvr = None        # held 1st Whisper-read offset for two-point confirm
        self._tier_tpvr_until = 0.0   # deadline for the confirming 2nd tier read
        self._tier_listen = False     # a tier Whisper listen is in flight
        # ── TICKET-109 decision engine state (exposed via /diag.decision_engine) ──
        self._decision_state            = "TRUST"   # TRUST | CAUTION | SWITCH | REGEN
        self._decision_strikes          = 0
        self._decision_last_t           = 0.0
        self._decision_last_action_t    = 0.0
        self._decision_dim_scores       = {
            "source_agree":  "OK",
            "sync_stable":   "OK",
            "lyric_quality": "OK",
            "ear_corrob":    "OK",
        }
        self._decision_dim_history = {
            k: deque(maxlen=int(self._tune.get("decision_score_window", 12)))
            for k in self._decision_dim_scores
        }
        self._decision_audit            = deque(maxlen=20)
        self._force_ai_gen              = False     # consumed by the AI-gen branch
        # ── FINE-TUNE mode state (sub-second polish, bolted onto the tier) ──
        # See the `fine_tune_*` block in self._tune for the knob semantics; these
        # are the runtime fields the entry-gate / listen tick / pause override use.
        self._fine_active = False         # fine-tune currently engaged
        self._fine_good_t0 = None         # wall-clock first 'insync' verdict of current good streak
        self._fine_incon = 0              # consecutive inconclusive fine-tune listens
        self._fine_listen_after = None    # pending root.after id for the next fine-tune listen
        self._fine_listen_pending = False # the in-flight tier-style listen is OURS — hand to _apply_fine_listen
        self._fine_pause_until = 0.0      # wall-clock pause expiry (0 = no pause active)
        self._fine_pause_pos_eased = None # held eased pos for the pause duration
        self._fine_pause_pos_raw = None   # held raw pos for the pause duration
        self._fine_pause_amount = 0.0     # how much to subtract from self.offset at pause-end
        self._last_drift = 0.0        # last audio-vs-display drift measured (sync telemetry)
        self._last_drift_t = 0.0      # when that drift was measured (time.time)
        self._pending_switch = None   # a contradicting heard song awaiting a 2nd confirming read
        self._sound_fail_streak = 0   # consecutive times the SAME other song was heard (wrong-song strikes)
        self._last_heard_contra = None  # the last contradicting heard song (for the strike streak)
        self._deciding = False        # a by-ear song decision (Whisper) is in flight
        self._force_sync_active = False  # FORCE SYNC nuclear resync running
        self._fs_current = None       # offset of the candidate currently being verified
        self._fs_confirms = 0         # consecutive fresh reads that still match _fs_current
        self._fs_line_lo = self._fs_line_hi = None  # player-pos span the confirms cover
        self._fs_blacklist = []       # offsets that FAILED to keep matching (chorus traps)
        self._fs_misses = 0           # consecutive fresh reads the current candidate missed
        self._fs_tries = 0; self._fs_empties = 0
        self._force_sync_after = None
        self._last_decision = None    # last decide_song_by_lyrics result (telemetry)
        self._decide_force_flag = False  # TICKET-090: caller forces decide past the verified+locked gate
        self._fast_calib = 0          # remaining quick re-locks after a song change
        self._recal_after = None      # pending recalibrate timer id
        self._live_mode = False       # concert/compilation → sound-only, no title-match
        self._mv_mode = False         # MV/cinematic title → expect a dead-space intro
        self._intro_anchored = True   # have we anchored past this track's intro yet?
        self._track_t0 = 0.0          # wall-clock when the current track started

        _seed_bundled_lyrics()        # bake-in songs providers always miss (feelingradation)
        self.index = LyricsIndex()
        self.media = MediaWatcher()
        self.character = Character(self.root, _DATA)
        if self.character_on:
            self.character.set_enabled(True)
        if self.api_on:
            try:
                from api import start_api
                self._api = start_api(self, LOG_FILE)
            except Exception as e:
                log.info("API failed to start: %s", e)
        if self.boundary_on:
            self._start_boundary()
        log.info("started (recal %ss, api %s, boundary %s)",
                 self.recal_secs, self.api_on, self.boundary_on)
        self._hint("Waiting for music…")
        self.root.after(300, self._tick)
        self.root.after(7000, self._health_check)
        self.root.after(4000, self._viewport_watchdog)
        self._arm_recal(max(4, self.recal_secs or 30))
        # Background sync-by-listening heartbeat: when faster-whisper is
        # available, re-checks alignment every ~45s in idle moments. Keeps
        # lyrics tight on cuts Shazam can't fingerprint (karaoke versions,
        # live arrangements, off-vocal mixes — Niconico karaoke videos).
        self._auto_align_after = self.root.after(45000, self._periodic_auto_align)
        self._live_resync_after = self.root.after(15000, self._live_resync_loop)

    # ── per-track ──

    def _on_track_change(self, track, duration=None):
        artist, title = track
        if not artist and " - " in title:
            a, t = title.split(" - ", 1)
            artist, title = a.strip(), t.strip()
        # SMTC re-reports the SAME song mid-playback (YouTube nudges its metadata —
        # a channel suffix appears/disappears, the title reflows), which flips the
        # cleaned (artist,title) tuple and used to re-enter here and WIPE a confirmed
        # sync offset back to 0 — a song that was perfectly synced suddenly jumped
        # ~30s off (Shinigami Eyes/white balance "was fine then desynced"). If this
        # "new" track is really the one already loaded, keep the sync and bail; the
        # recal loop keeps listening, so a genuine same-title-different-song is still
        # caught by sound.
        if (self.lines and not self._live_mode
                and self._titles_match(self.meta.get("title", ""), title)):
            if duration:
                self._cur_duration = duration
            log.info("same song re-reported (%r) — keeping sync, no reset", title)
            return
        self.character.set_artist(artist or title)   # spawn this song's artist
        self._cur_duration = duration
        self._health_attempts = 0
        self.offset = 0.0          # fresh baseline; sound calibration sets it
        self.meta = {}             # drop the PREVIOUS song's meta so a new song
        #                            with no lyrics yet can't show its stale source
        #                            (e.g. "youtube-captions / 0 lines")
        self._sound_song = None    # new video → re-identify by ear
        # TICKET-099: new SMTC track → reset confirmation flags + takeover
        # bookkeeping. Sound has not yet been heard for THIS song, and a fresh
        # SMTC PLAYING tab cancels any pending takeover state from the previous
        # track. The _last_takeover_t debounce intentionally stays — a song
        # change does not authorize an immediate second takeover.
        self._sound_corroborated = False
        self._verified_meta = False
        self._verified = False
        # TICKET batch4: a real SMTC track change must wipe lyrics immediately
        # (the render-side gate is for transient mid-track demotions only).
        # Clear the gate timestamp so /diag and any downstream gate check
        # see "no active grace window".
        self._verified_gate_t = 0.0
        self._smtc_paused_since = 0.0
        self._last_smtc_playing = None
        self._source_priority = "agree"
        self._pending_corr = 1e9   # drop any pending large-offset confirmation
        self._pending_offset = None  # drop any deferred sync-offset commit from prev track
        # FINE-TUNE: force-clear pause buffers AFTER the offset reset above so no
        # queued pause subtraction can fire against the fresh-song offset (the
        # subtraction-vs-reset order is intentional: clearing _fine_pause_amount=0
        # below guarantees pause-end is a no-op even if the old timer hasn't fired
        # yet). Cancel any pending fine-tune listen scheduled against the old
        # song's clock.
        if self._fine_listen_after is not None:
            try:
                self.root.after_cancel(self._fine_listen_after)
            except Exception:
                pass
            self._fine_listen_after = None
        self._fine_active = False
        self._fine_good_t0 = None
        self._fine_incon = 0
        self._fine_listen_pending = False
        self._fine_pause_until = 0.0
        self._fine_pause_pos_eased = None
        self._fine_pause_pos_raw = None
        self._fine_pause_amount = 0.0
        if self._sync_confirm_after is not None:   # cancel a pending confirm listen
            try:
                self.root.after_cancel(self._sync_confirm_after)
            except Exception:
                pass
            self._sync_confirm_after = None
        self._pending_switch = None  # drop any pending song-switch confirmation
        self._force_sync_active = False  # cancel any Force Sync from the previous track
        self._fs_current = None; self._fs_confirms = 0; self._fs_blacklist = []
        self._fs_misses = 0; self._fs_line_lo = self._fs_line_hi = None
        self._sound_fail_streak = 0  # fresh wrong-song strike count for the new track
        self._last_heard_contra = None
        # TICKET-090: clear per-track decide cache so a PREVIOUS track's heard /
        # ranked / scope can't bleed into the new track's diag (live diag saw
        # stale Japanese hallucination ranking from a previous song still in
        # decision.heard). The decide loop will repopulate from real audio on
        # the new track. Also drop the in-flight force flag.
        self._last_decision = None
        self._decide_force_flag = False
        self._gen_token += 1       # cancel any in-flight lyric generation
        self._deep_token += 1      # cancel any in-flight deep (offline) transcription
        self._track_seq += 1
        self._generating = False
        self._gen_defers = 0       # fresh defer budget for this track's fetch
        self._fetch_key = None     # let this track re-attempt a real fetch (upgrade path)
        self._recent_corr = []     # reset ambiguity history for the new song
        self._last_align_t = 0.0        # let auto-align run early on the new song
        self._last_sound_lock_t = 0.0   # no Shazam lock yet on the new song
        self._last_audio_off = None     # no Shazam read yet for new song
        self._last_audio_off_t = 0.0
        self._sound_title_alias = None  # clear cross-language alias for new song
        self._drift_integral = 0.0      # fresh drift accumulation for new song
        # New song → start in fast-verify (3×/min) and re-judge from scratch.
        self._sync_tier_interval = self._tune.get("sync_tier_fast_s", 20.0)
        self._sync_good_streak = self._sync_miss_streak = self._energy_blind = 0
        self._sync_incon_streak = 0
        self._sync_fail_streak = self._sync_reject_count = 0
        self._tier_tpvr = None
        self._tier_listen = False
        # TICKET-081: a COVER's timing (intro length, tempo, edits) almost never
        # matches the original artist's studio LRC we fetched for it, so a
        # studio reset-to-0 strategy strands the lyrics 30-90 s out of sync
        # for the whole song (the 名前のない怪物 / 快晴 by 音乃瀬奏 cases).
        # Treat covers like live-arrangements: FOLLOW the measured offset.
        self._live_arrangement = (is_live_arrangement(title)
                                  or bool(getattr(self, "_is_cover", False)))
        self._live_sync_streak = 0          # new song → resync aggressively again (~8×/min)
        self._live_resync_gap = None
        self._live_resync_inflight = False
        log.info("track change: %r / %r (dur %s)%s%s", title, artist, duration,
                 " [live-arrangement]" if self._live_arrangement else "",
                 " [cover]" if getattr(self, "_is_cover", False) else "")

        # For covers, use the original artist (extracted from the title) for
        # lyric search instead of the covering channel — "Coffee - Alka | Lumi"
        # should search "Coffee" by "Alka", not by "Lumi".
        fetch_artist = artist
        if self._is_cover:
            if self._cover_original_artist:
                fetch_artist = self._cover_original_artist
                log.info("cover: using original artist %r instead of channel %r",
                         fetch_artist, artist)
            else:
                # No original artist parseable from the title. The covering CHANNEL
                # ("Ouro Kronii Ch. hololive-EN") is NOT the song's artist and won't
                # have these lyrics listed under it — DROP it and search by title
                # alone (the original's lyrics fit the cover). Per the user's rule.
                fetch_artist = ""
                log.info("cover: no original artist in title → title-only search "
                         "(ignoring cover channel %r)", artist)

        self._live_mode = is_live_or_compilation(title, duration)
        # TICKET-109: new track => decision engine forgets the prior song's strikes
        self._reset_decision_engine()
        if self._live_mode:
            # A concert / live / festival / compilation: the title is the EVENT,
            # not a song. Title-matching it is what made a whole concert show one
            # song's lyrics — so refuse the title entirely and let SOUND drive.
            # The song-change detector + the fast re-ID loop pick up each track.
            log.info("live/compilation title → ignoring title, identifying by sound")
            self.lines, self._lyrics_path, self.idx = [], None, -1
            self._kara = []
            self._verified = False
            self._verified_meta = False           # TICKET-099
            self._sound_corroborated = False      # TICKET-099
            self._verified_gate_t = 0.0           # TICKET batch4: real track change, no gate
            self._hint("🎤 Live set — listening for each song…")
        else:
            # Provisional: show the title/artist match instantly (so there's no
            # dead air) — but AUDIO is primary and confirms/overrides it below.
            # Try original artist first for covers, then fall back to channel.
            path = self.index.match(fetch_artist, title, duration)
            # Fall back to the channel ONLY when we used a real original artist; for a
            # cover whose channel we deliberately dropped (fetch_artist=""), don't
            # re-introduce the channel — that's the wrong-artist match we're avoiding.
            if not path and fetch_artist and fetch_artist != artist:
                path = self.index.match(artist, title, duration)
            if path and self._file_valid(path, duration):
                if path != self._lyrics_path:
                    self.load(path)
                self._maybe_translate()
                gen = (self.meta.get("source") or "").startswith("generated")
                romaji_only = (self.meta.get("lang") or "").endswith("-romaji")
                stale = gen or romaji_only
                pt, ct = _norm_title(title), _norm_title(self.meta.get("title", ""))
                matched = bool(pt and ct and (pt == ct or pt in ct or ct in pt))
                distinct = confidence.title_distinctiveness(title)
                self._title_locked = bool(
                    matched and distinct >= 0.40 and not stale
                    and not is_mv_version(title) and not _is_generic_title(title))
                if matched and not self._title_locked:
                    log.info("title %r not locked (distinctiveness %.2f, stale=%s) → audio decides",
                             title, distinct, stale)
                if stale:
                    why = "GENERATED" if gen else "ROMAJI-only"
                    log.info("cache hit for %r is %s → background upgrade-fetch", title, why)
                    self._start_fetch(fetch_artist, title, duration,
                                      cover=(self._is_cover or romaji_only),
                                      strict=(self._clean_source() and not romaji_only))
            else:
                self.lines, self._lyrics_path, self.idx = [], None, -1
                self._kara = []
                self._verified = False
                self._verified_meta = False           # TICKET-099
                self._sound_corroborated = False      # TICKET-099
                self._verified_gate_t = 0.0           # TICKET batch4: real track change, no gate
                self._title_locked = False
                self._hint(f"♪ {title} — identifying…")
                self._start_fetch(fetch_artist, title, duration, cover=self._is_cover,
                                  strict=self._clean_source())

        # MV / cinematic dead-space intro: for an MV-titled video, hold the lyrics
        # through the leading intro (see _tick); for ANY unaligned track, anchor
        # lyric time 0 to the detected audio onset (see _on_song_onset). Shazam
        # overrides both the moment it can measure the real offset.
        self._mv_mode = is_mv_version(title) and not self._live_mode
        self._intro_anchored = False
        self._track_t0 = time.time()
        # Re-arm vocal-onset detection for the new track (lets us calibrate the
        # offset on songs with long instrumental intros — see _on_vocal_onset).
        if self._boundary:
            try:
                self._boundary.reset_vocal()
            except Exception:
                pass

        # PRIMARY signal: identify by sound and let it decide the real song.
        self._start_identify(seconds=6, attempts=2)
        # Lock the timing fast: a short burst of quick re-checks right after the
        # song starts, then the loop relaxes to the normal cadence.
        self._fast_calib = 3
        self._arm_recal(7)
        # Bound the "deliberation": if no real lyrics have loaded within ~11 s
        # (title fetch + sound-ID have had their chance), start generating by ear
        # — instead of waiting out the whole title→sound→re-fetch chain (~30 s).
        # If real lyrics arrive late, `load()` cancels the generation.
        if self.generate_on and not self._live_mode:
            self.root.after(11000,
                            lambda t=self._track_seq: self._maybe_generate(t))
        # Schedule an early auto-align ~25 s into the track — by then vocals
        # have had a chance to start (covers Grimes "Genesis"-class intros)
        # and Shazam has had a couple of attempts. If Shazam locked it, the
        # auto-align silently no-ops; if not, this catches off-vocal /
        # karaoke / live cuts Shazam can't fingerprint.
        if not self._live_mode:
            self.root.after(25000,
                            lambda t=self._track_seq: self._track_start_auto_align(t))
            # SMART song decision: a few seconds in (vocals present), transcribe and
            # confirm the lyrics on screen are the song actually being sung — switch
            # if Shazam mis-ID'd or the LRC is mislabeled. Skipped for baked songs.
            self.root.after(int(self._tune.get("decide_at_s", 20.0) * 1000),
                            lambda t=self._track_seq: self._decide_by_ear(t))
        # YOUTUBE CAPTIONS: for a browser video, the video's OWN caption track is
        # the most accurate lyric source — correct words AND timing locked to this
        # exact video (no wrong-transcription LRC, no cross-version drift). Fetch
        # it in the background a few seconds in (after the LRC has shown something)
        # and prefer it. Throttled + once-per-song so a fast playlist can't 429.
        # `src` isn't a param here — use the cached source from the tick loop.
        if (self.captions_on and not self._live_mode
                and any(h in (self._last_src or "") for h in BROWSER_HINTS)):
            self.root.after(4000,
                            lambda t=self._track_seq: self._maybe_fetch_captions(t))

    def _maybe_fetch_captions(self, track_seq):
        """Background: pull this YouTube video's caption track and prefer it over
        the provider LRC. Throttled (min gap between yt-dlp calls) and once per
        song, so a rapidly-advancing playlist can't rate-limit us."""
        if track_seq != self._track_seq or self._live_mode:
            return
        if self._track == self._caption_song:
            return                                   # already tried this song
        now = time.time()
        if now - self._last_caption_t < 8.0:
            # too soon after the last fetch — retry a bit later (still this track)
            self.root.after(4000,
                            lambda t=track_seq: self._maybe_fetch_captions(t))
            return
        # already on captions for this song? nothing to do.
        if (self.meta.get("source") or "") == "youtube-captions":
            return
        self._last_caption_t = now
        self._caption_song = self._track
        self.load_youtube_captions(silent=True)

    def _maybe_generate(self, track_seq):
        """Deadline fallback: still no lyrics for this track → generate by ear.

        Generation is a genuine LAST resort, so if a provider lookup is still in
        flight when the deadline fires we wait for it instead of pre-empting it —
        otherwise a slow-but-successful fetch (covers/niche titles resolve in
        ~20s) would flash AI lyrics that get replaced a moment later. Bounded so a
        hung fetch can't postpone generation forever."""
        if track_seq != self._track_seq or self.lines or self._generating:
            return
        # Niche/Vocaloid/VTuber lookups can take 25-35s (provider search is serial);
        # live watching showed real lyrics — which DO exist and always won — only
        # arriving after a brief AI flash. Wait out a realistic fetch (≈35s total
        # with the 11s deadline) before generating, so generation stays a genuine
        # last resort. Still bounded, so a hung fetch can't postpone it forever.
        # Only the defer extends while the fetch is STILL RUNNING (it might still
        # win); a no-lyrics song's fetch returns None fast and falls straight
        # through to generate, so this doesn't delay genuine generation. Cleaner
        # titles (TICKET-023) make most fetches resolve in <15s; this is the backstop
        # for the slow ones so they aren't pre-empted ("generated before finding it").
        if self._fetching and self._gen_defers < 8:      # ~32s extra; ~43s total
            self._gen_defers += 1
            self.root.after(4000, lambda t=track_seq: self._maybe_generate(t))
            return
        st = self.media.get()
        if st and st.get("status") == PLAYING:
            why = "lookup came up empty" if not self._fetching else "lookup still running"
            log.info("no lyrics after the grace window (%s) → generating by ear", why)
            self._begin_generation()

    def _trusted_duration(self, state):
        # YouTube/browser report the VIDEO length (intro/outro) which differs
        # from the audio track — using it to match/verify rejects correct
        # lyrics. Only trust duration from real audio players (Spotify, etc.).
        # EXCEPTION: a YT-Music "- Topic" channel is an audio-only upload, so its
        # length IS the track length — trust it (helps same-title disambiguation).
        if any(h in state.get("source", "") for h in BROWSER_HINTS):
            if (state.get("artist") or "").strip().lower().endswith("- topic"):
                return state.get("duration")
            return None
        return state.get("duration")

    def _clean_source(self):
        """True when the now-playing metadata is AUTHORITATIVE — a real audio app
        (Spotify, etc.) or a YT-Music "- Topic" channel (official audio upload).
        Then the supplied artist is trustworthy, so the lyric search runs STRICT:
        it skips artist-unconfirmed title-only matches that would grab a wrong
        same-title song ("Lucky Star" → a nursery-rhyme "Twinkle Twinkle")."""
        src = self._last_src or ""
        rawa = self._last_artist or ""
        if any(h in src for h in BROWSER_HINTS):
            return rawa.strip().lower().endswith("- topic")
        return bool(src)

    def _mark_verified(self, confirmed=False):
        """TICKET-099: split into TENTATIVE (meta — duration/title at load time)
        and CONFIRMED (sound has corroborated the loaded title at least once).

        Called WITHOUT confirmed at load time → updates _verified_meta only; the
        public-facing `_verified` flag stays False until a Shazam read agrees
        with the loaded title (see _consume_async loaded_ok branch which calls
        _mark_verified(confirmed=True)).

        This closes the v1.0.88 bug where _verified=True the moment a cache
        passed the duration check — even if Shazam never heard the song and
        SMTC was just a stale paused tab pointing at the wrong title.
        """
        md = self.meta.get("duration")
        if self._cur_duration:
            self._verified_meta = bool(md and abs(md - self._cur_duration) <= 12)
        else:
            self._verified_meta = True   # title+language match is the best signal here
        if confirmed:
            self._sound_corroborated = True
        # Public verified = meta AND sound-corroborated (TICKET-099). A duration
        # match alone is no longer enough — Shazam must agree at least once.
        new_v = bool(self._verified_meta and self._sound_corroborated)
        # TICKET batch4: route through _set_verified so the True↔False edge
        # bookkeeping (verified_render_gate_t) stays consistent across every
        # site that mutates the flag.
        self._set_verified(new_v, reason="mark_verified")

    def _set_verified(self, value, reason=""):
        """TICKET batch4: single chokepoint for self._verified assignments.

        Records wall-clock of any True→False transition into
        ``self._verified_gate_t`` so the verified_render_gate_s window can
        defer lyric teardown on transient disagreements (e.g. Shazam returned
        the album string while SMTC has the actual track). A False→True
        transition resets the gate (something — alias path or a re-confirming
        Shazam read — has restored verified, so no teardown is owed).

        Bare ``self._verified = False`` assignments mid-track should be
        replaced with this helper; the genuinely catastrophic sites
        (``_on_track_change`` and ``_smtc_paused_takeover``) explicitly clear
        ``_verified_gate_t = 0`` after the assignment, because a real song
        swap must wipe lyrics immediately rather than holding the gate window.
        """
        new_v = bool(value)
        prev = bool(getattr(self, "_verified", False))
        self._verified = new_v
        if prev and not new_v:
            self._verified_gate_t = time.time()
            if reason:
                log.info("verified True→False (%s) — render gate armed", reason)
        elif (not prev) and new_v:
            if self._verified_gate_t:
                log.info("verified False→True — render gate cleared")
            self._verified_gate_t = 0.0

    def _update_smtc_pause_state(self, st):
        """TICKET-099: edge-detect SMTC PLAYING/not-PLAYING transitions.

        Sets `_smtc_paused_since` to wall-clock on PLAYING→not-PLAYING and
        resets it (plus clears the takeover debounce) on not-PLAYING→PLAYING.
        Called from the tick loop after media.get(); kept tiny + idempotent so
        it can run every frame. `st` is the raw media-watcher dict (may be
        None / empty)."""
        try:
            playing = bool(st and st.get("status") == PLAYING)
        except Exception:
            playing = False
        # Edge detect: only act on a TRANSITION (not every frame at the same state).
        prev = self._last_smtc_playing
        if prev is None:
            # first observation — seed without firing an edge so we don't claim
            # a pause-since for a session that's been paused forever
            self._last_smtc_playing = playing
            if not playing:
                # treat the very first observed paused-state as starting now;
                # without this, an app launched into a paused tab would have
                # _smtc_paused_since=0 forever and never satisfy the floor.
                self._smtc_paused_since = time.time()
            return
        if prev and not playing:
            # PLAYING → not PLAYING: start the pause clock
            self._smtc_paused_since = time.time()
        elif (not prev) and playing:
            # not PLAYING → PLAYING: clear pause clock AND debounce (a real
            # user un-pause is the authoritative cancel signal — see TICKET-099
            # decision on the debounce bypass).
            self._smtc_paused_since = 0.0
            self._last_takeover_t = 0.0
        self._last_smtc_playing = playing

    def _resolve_source_priority(self, st, heard):
        """TICKET-099: decide who wins between SMTC and Shazam when Shazam
        delivers a new sound_song result that DISAGREES with what's loaded.

        Returns one of:
          'agree'        — heard matches loaded (existing loaded_ok branch
                           handles it; we just record the decision).
          'smtc'         — SMTC is currently PLAYING; the user is actively
                           streaming this audio, so SMTC stays primary and
                           the existing strike/pending-switch flow handles
                           any real disagreement.
          'shazam-live'  — SMTC has been NOT-PLAYING for at least
                           smtc_paused_min_s AND the takeover debounce has
                           expired (or hasn't fired yet). Caller may execute
                           a takeover swap to `heard`.
          'confused'     — neither side is authoritative right now (e.g.
                           SMTC paused but takeover gated by debounce / floor).
                           Caller should keep current state and log.

        Does NOT mutate any state apart from `self._source_priority` (telemetry).
        """
        try:
            playing = bool(st and st.get("status") == PLAYING)
        except Exception:
            playing = False
        loaded_t = self.meta.get("title", "") or ""
        heard_t = (heard or (None, None))[0]
        # Quick agree check (mirrors loaded_ok in _consume_async but cheaper —
        # no alias path, which the caller has already tried).
        if (heard_t and loaded_t and self._titles_match(loaded_t, heard_t)):
            self._source_priority = "agree"
            return "agree"
        # SMTC.playing=true → user IS streaming THIS audio; SMTC wins.
        if playing:
            self._source_priority = "smtc"
            return "smtc"
        # Master switch — feature off → never take over from a paused SMTC.
        if not int(self._tune.get("smtc_paused_shazam_takeover", 1) or 0):
            self._source_priority = "confused"
            return "confused"
        # Paused-SMTC: require a continuous paused-floor and respect debounce.
        floor = float(self._tune.get("smtc_paused_min_s", 8.0))
        debounce = float(self._tune.get("smtc_takeover_debounce_s", 20.0))
        now = time.time()
        paused_for = (now - self._smtc_paused_since) if self._smtc_paused_since else 0.0
        since_takeover = (now - self._last_takeover_t) if self._last_takeover_t else 1e9
        if paused_for >= floor and since_takeover >= debounce:
            self._source_priority = "shazam-live"
            return "shazam-live"
        self._source_priority = "confused"
        return "confused"

    def _smtc_paused_takeover(self, heard, f_title, f_artist):
        """TICKET-099: drop the (paused-SMTC) loaded lyrics and switch to the
        Shazam-heard song. Reuses the wrong-song correction path so reviewers
        only have to learn one set of switch-mechanics. Bumps _track_seq to
        cancel in-flight generation / translation / decide-by-ear for the
        previous (SMTC) title. Caller has already confirmed the 2-read agreement
        and the paused-floor / debounce gates (see _resolve_source_priority +
        the takeover branch in _consume_async)."""
        log.info("smtc-paused-takeover: dropping loaded %r — heard %r / %r "
                 "(paused %.1fs, debounce ok) — swapping",
                 self.meta.get("title", ""), f_title, f_artist,
                 (time.time() - self._smtc_paused_since)
                 if self._smtc_paused_since else 0.0)
        self._last_takeover_t = time.time()
        # Mirror the wrong-song correction path (line 2876-2894 in v1.0.88):
        self._title_locked = False
        self._sound_fail_streak = 0
        self._last_heard_contra = None
        self._pending_switch = None
        self._sound_song = heard
        self._last_sound_lock_t = time.time()
        # Public verified must drop the moment we admit the lyrics on screen
        # are not what we hear — even if meta still passes, sound_corroborated
        # is being repudiated by THIS very swap. Both flags reset; the new
        # song's verification re-earns through the normal loaded_ok flow.
        self._sound_corroborated = False
        self._verified_meta = False
        self._verified = False
        # TICKET batch4: takeover is an intentional swap to a different song;
        # the render gate exists for transient mid-track demotions, not real
        # song changes. Clear the gate so any deferred teardown can fire
        # immediately and the new lyrics replace cleanly.
        self._verified_gate_t = 0.0
        self.offset = 0.0
        self._fast_calib = max(self._fast_calib, 2)
        self._arm_recal(7)
        # Cancel in-flight per-track work for the OLD (SMTC) title so a slow
        # translate / generate / deep-transcribe can't land on the new lyrics.
        # Mirrors _on_track_change's discipline (line ~1842-1844 v1.0.88).
        self._track_seq += 1
        self._gen_token += 1
        self._deep_token += 1
        self._generating = False
        self._gen_defers = 0
        # Look up the heard song's cache and load / fetch — same as the
        # wrong-song correction's tail. _prefer_cjk_cache covers the
        # romanized-vs-CJK title case.
        cached = self._prefer_cjk_cache(f_artist, f_title, self._cur_duration) \
            or self.index.match(f_artist, f_title, self._cur_duration)
        if cached and self._file_valid(cached, self._cur_duration):
            if cached != self._lyrics_path:
                log.info("smtc-paused-takeover → cached %s", cached.name)
                self.load(cached)
            self._maybe_translate()
        else:
            log.info("smtc-paused-takeover → fetching %r / %r", f_title, f_artist)
            self._hint(f"🔄 Heard a different song — switching to {f_title}…")
            self._start_fetch(f_artist, f_title, self._cur_duration)

    def _file_valid(self, path, duration):
        try:
            from fetch_lyrics import validate_file
            ok, _ = validate_file(path, duration)
            if not ok:
                return False
            # PROVENANCE GUARD (wrong-song defense, TICKET-055). A cache produced
            # by a WEAK provider path — title-only or cover-fallback — is only
            # trustworthy for a song we'd still fetch that way. For a CLEAN source
            # (Spotify / "- Topic", authoritative artist) that is NOT a cover,
            # current rules FORBID those paths: a bare-title match grabs the most
            # popular same-title song (Ludacris "The Potion" for a VTuber's
            # "Potion" — durations coincided at 3:43 so every duration gate passed).
            # Such a cache is stale/low-confidence → reject so we re-fetch under
            # today's strict rules (which return the right song or nothing, not the
            # wrong one). Genuine artist-keyed caches ("syncedlyrics"/"lrclib"/…)
            # and generated/caption caches are untouched.
            if self._clean_source() and not self._is_cover:
                try:
                    src = (json.loads(Path(path).read_text("utf-8"))
                           .get("meta", {}).get("source") or "")
                except Exception:
                    src = ""
                if src in ("syncedlyrics/cover", "syncedlyrics/title"):
                    log.info("cache %s came from weak path %r but source is clean & "
                             "non-cover → distrust, re-fetch", Path(path).name, src)
                    return False
            # WRONG-LANGUAGE guard (TICKET-060): a cached KOREAN body for a song
            # whose title/artist is kanji (Han, no hangul) is a wrong-language
            # collision (花譜's 邂逅 → a Korean "Chance meeting"). Reject → re-fetch
            # under fetch_lrc's Han→reject-ko rule. Self-healing, no manual purge.
            try:
                lang = (json.loads(Path(path).read_text("utf-8"))
                        .get("meta", {}).get("lang") or "")
            except Exception:
                lang = ""
            if lang == "ko":
                ta = (self._clean_title_cache or "") + (self._clean_artist_cache or "")
                if re.search(r"[㐀-鿿]", ta) and not re.search(r"[가-힣]", ta):
                    log.info("cache %s is Korean but song is kanji (%r) → distrust, re-fetch",
                             Path(path).name, ta[:30])
                    return False
            # WRONG-LANGUAGE guard (TICKET-062): a cached ENGLISH body for a song whose
            # ARTIST name has KANA (uniquely Japanese — Suisei's 星街すいせい "GHOST"
            # pulled an English "Ghost") is a same-title collision. Re-fetch under
            # fetch_lrc's language-confidence guard. Gated on KANA only (not han, which
            # is ambiguous JA/ZH) so it can't misfire on Chinese / romanized names.
            if lang == "en" and re.search(r"[぀-ゟ゠-ヿ]", self._clean_artist_cache or ""):
                log.info("cache %s is English but artist is kana-Japanese (%r) → distrust, re-fetch",
                         Path(path).name, (self._clean_artist_cache or "")[:20])
                return False
            return True
        except Exception:
            return True

    def purge_cache(self, lang=None, source=None, current=False):
        """Delete cached lyric JSONs matching a language and/or source filter, or
        the CURRENT song's file, then re-fetch the current song if it was removed.
        Returns the removed filenames. Backs /purgecache — clearing a bad match at
        runtime instead of by hand."""
        removed = []
        cur = Path(self._lyrics_path) if self._lyrics_path else None
        for p in LYRICS_DIR.glob("*.json"):
            hit = bool(current and cur and p == cur)
            if not hit and (lang or source):
                try:
                    m = json.loads(p.read_text("utf-8")).get("meta", {})
                except Exception:
                    continue
                hit = ((not lang or (m.get("lang") or "") == lang)
                       and (not source or (m.get("source") or "").startswith(source)))
            if hit:
                try:
                    p.unlink()
                    removed.append(p.name)
                except Exception:
                    pass
        if removed:
            cur_removed = bool(current or (cur and cur.name in removed))
            def _after():                      # index + UI work on the Tk thread
                try:
                    self.index.refresh()
                except Exception:
                    pass
                if cur_removed:
                    self.refetch()
            try:
                self.root.after(0, _after)
            except Exception:
                pass
        return removed

    @staticmethod
    def _titles_match(a, b):
        """True if two titles refer to the same song (exact, or one contains
        ≥60% of the other after normalization). Used to check whether the loaded
        lyrics match what was heard by sound."""
        na, nb = _norm_title(a), _norm_title(b)
        if not na or not nb:
            return False
        if na == nb:
            return True
        short, lng = sorted((na, nb), key=len)
        return short in lng and len(short) / max(1, len(lng)) >= 0.6

    @staticmethod
    def _titles_share_content(a, b):
        """Looser overlap check: do two titles share ANY meaningful content?
        Returns False when the titles are completely unrelated (different
        songs/videos), True for any plausibly-related pair. Used to detect
        when the player's reported title is stale/wrong vs. Shazam's heard
        title — they MUST share something to be the same song.

        Two layers: (1) normalized-title contains substring of ≥4 chars,
        (2) shared CJK n-grams of ≥2 chars (handles ローマ字 vs. CJK mismatches).
        """
        na, nb = _norm_title(a), _norm_title(b)
        if not na or not nb:
            return False
        if na == nb or na in nb or nb in na:
            return True
        # 4-char substring overlap in normalised form
        short, lng = sorted((na, nb), key=len)
        if len(short) >= 4:
            for i in range(len(short) - 3):
                if short[i:i + 4] in lng:
                    return True
        # CJK n-gram overlap: any shared 2-char CJK sequence
        cjk_a = re.findall(r"[぀-ヿ一-鿿]{2,}", a or "")
        cjk_b = re.findall(r"[぀-ヿ一-鿿]{2,}", b or "")
        for ga in cjk_a:
            for gb in cjk_b:
                if len(ga) >= 2 and len(gb) >= 2:
                    if ga in gb or gb in ga:
                        return True
                    for i in range(len(ga) - 1):
                        if ga[i:i + 2] in gb:
                            return True
        return False

    def _prefer_cjk_cache(self, artist, heard_title, duration=None):
        """When Shazam returns a ROMANIZED/English title for a CJK song (e.g.
        "Ahoy!! We are Houshou Pirates" for 宝鐘マリン's "Ahoy!! 我ら宝鐘海賊団☆"),
        the English title fetches/loads English lyrics even though the video is
        the original Japanese. If a CJK-script cache by the SAME artist exists
        and shares the leading song token, prefer it — the same "original script
        wins" rule fetch_lrc uses for romaji vs kanji.

        Returns a Path to a better CJK cache, or None. Only fires when the heard
        title has no CJK itself (so a real English song is never redirected)."""
        if _has_cjk(heard_title):
            return None
        qa = _norm_title(artist)
        if not qa:
            return None
        # leading Latin token of the heard title (e.g. "ahoy" from
        # "Ahoy!! We are Houshou Pirates") — the shared anchor across languages.
        toks = [t for t in re.split(r"[^0-9a-z]+", (heard_title or "").lower()) if len(t) >= 3]
        if not toks:
            return None
        lead = toks[0]
        best, best_dur = None, None
        for e in self.index.entries:
            if not _has_cjk(e.get("title") or ""):
                continue
            if _norm_title(e.get("artist") or "") != qa:
                continue
            # the CJK title's Latin remnant must contain the same leading token
            # ("ahoy" survives in "Ahoy!! 我ら宝鐘海賊団☆")
            etoks = [t for t in re.split(r"[^0-9a-z]+", (e.get("title") or "").lower())
                     if len(t) >= 3]
            if lead not in etoks:
                continue
            # prefer a duration-consistent candidate when we know the duration
            if duration and e.get("dur") and abs(e["dur"] - duration) > 12:
                continue
            best, best_dur = e["path"], e.get("dur")
            break
        if best:
            log.info("preferring original-script cache %s over romanized title %r",
                     best.name, heard_title)
        return best

    def _maybe_translate(self):
        # Self-heal a loaded song in the background: add romaji to any
        # Japanese/CJK line missing it, and translate any line that should have
        # English but doesn't — whatever the song's overall detected language.
        # So a song that came out as bare Japanese (e.g. mixed-language, or a
        # mis-detected song) gets furigana/romaji + English filled and re-saved.
        if not self.lines or not self._lyrics_path:
            return
        cjk = [ln for ln in self.lines if ln.jp.strip() and _has_cjk(ln.jp)]
        need_rm = any(not ln.rm.strip() for ln in cjk)
        # non-English Latin/Cyrillic songs (Spanish, German, Russian, and
        # romanized-Japanese) should have every line translated; CJK songs only
        # their CJK lines.
        whole = self.meta.get("lang") in ("es", "de", "ru", "ja-romaji")
        want_en = (self.lines if whole else cjk)
        want_en = [ln for ln in want_en if ln.jp.strip()]
        have_en = sum(1 for ln in want_en if ln.en.strip())
        if need_rm or (want_en and have_en < len(want_en) * 0.5):
            self._start_translate(self._lyrics_path)

    def _start_fetch(self, artist, title, duration=None, cover=False, strict=False):
        key = (artist, title)
        if self._fetch_key == key:
            return
        self._fetch_key = key
        self._fetching = True       # in flight → generation defers until this resolves

        def work():
            try:
                from fetch_lyrics import fetch_and_save
                p = fetch_and_save(title, artist, translate=False, duration=duration,
                                   cover=cover, strict=strict)
            except Exception:
                p = None
            self._fetch_result = (key, p)
            self._fetching = False

        threading.Thread(target=work, daemon=True).start()

    def _start_translate(self, path):
        if self._translating == path:
            return
        self._translating = path

        def work():
            ok = False
            try:
                from fetch_lyrics import backfill_file   # romaji + translation
                ok = backfill_file(path)
            except Exception:
                ok = False
            self._translate_result = (path, ok)

        threading.Thread(target=work, daemon=True).start()

    # ── audio identification (detect by SOUND, not title) ──

    def _schedule_sync_confirm(self):
        """Two-point verification for sync-by-sound: after a CANDIDATE offset is
        held (one read), hesitate ~2 s and take a confirming listen, so a single
        chorus-matched read can't move the lyrics. The 2nd read, if it agrees,
        commits the offset via the AGREE branch in _consume_async. Scheduled at
        most once per pending; cancelled on track change / when sync settles."""
        if self._sync_confirm_after is not None:
            return                              # a confirm listen is already pending
        listen_s = float(self._tune.get("sync_confirm_listen_s", 5.0))
        def _confirm():
            self._sync_confirm_after = None
            # Only re-listen if a candidate is still waiting and we're not mid-ID.
            if self._pending_corr < 1e8 and not self._identifying:
                log.info("sync: hesitation up → confirming listen (%.1fs)", listen_s)
                self._start_identify(seconds=listen_s, attempts=1)
        try:
            hold_ms = int(self._tune.get("sync_confirm_hold_ms", 2600))
            self._sync_confirm_after = self.root.after(hold_ms, _confirm)
        except Exception:
            self._sync_confirm_after = None

    def _start_identify(self, seconds=6, attempts=2):
        """Listen and identify by sound. Short captures (re-sync of a known
        song) finish faster; longer ones (first detection) recognize more
        reliably."""
        if self._identifying:
            return
        self._identifying = True
        if self._live_mode:
            seconds = max(seconds, 8)   # live arrangements need more signal to ID

        def work():
            res = None
            try:
                from recognize import recognize_playing
                t, a, off, t_cap = recognize_playing(seconds, attempts)
                if t:
                    res = (t, a or "", off, t_cap)
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
                    self._start_identify(seconds=6, attempts=2)
        finally:
            self.root.after(9000, self._health_check)

    def _arm_recal(self, delay):
        """(Re)schedule the recalibrate loop, cancelling any pending fire so a
        song change can pull the next listen in close."""
        if self._recal_after:
            try:
                self.root.after_cancel(self._recal_after)
            except Exception:
                pass
        self._recal_after = self.root.after(int(max(1, delay) * 1000),
                                            self._recalibrate_loop)

    def _recalibrate_loop(self):
        """Listen again to re-lock timing AND catch a new song within one long
        video (compilation / concert / DJ set / livestream). The audio boundary
        detector (songchange.py) now fires an immediate re-identify the instant a
        track flips, so once a song is CONFIRMED by sound this loop relaxes to a
        slow safety heartbeat — far fewer Shazam calls over a long compilation
        (lower CPU + network) while still re-locking timing occasionally. Cadence
        stays fast right after a song starts (the 3-shot burst) and while the song
        is still unconfirmed. Re-syncs use a short 4s capture so each pass is quick."""
        nxt = max(4, self.recal_secs or 30)
        try:
            st = self.media.get()
            # CONCERT banner OCR: a long live video shows the CURRENT song's name on
            # screen — read it (a high-confidence hint that Shazam can't beat on a
            # live arrangement) and switch to the right lyrics. Throttled, on a
            # background thread. See concert_ocr.py / docs/CONCERT_DETECTION.md.
            if (st and st.get("status") == PLAYING and self._live_mode
                    and self.concert_ocr and time.time() - self._last_ocr_t > 6.0):
                self._last_ocr_t = time.time()
                threading.Thread(target=self._concert_ocr_check, daemon=True).start()
            if st and st.get("status") == PLAYING and not self._identifying:
                if self._fast_calib > 0:
                    self._fast_calib -= 1
                    self._start_identify(seconds=4, attempts=1)   # short clip = fast turnaround
                    nxt = 4
                elif self.recal_secs:
                    self._start_identify(seconds=4, attempts=1)
                    confirmed = (self._verified and self._sound_song is not None) \
                        or self._title_locked
                    watched = (confirmed and self._boundary is not None
                               and self.boundary_on and not self._live_mode)
                    if watched:
                        # The detector is listening for the next song AND the
                        # adaptive sync tier now re-locks drift, so a frequent Shazam
                        # re-lock is largely redundant — and each recognize stalls the
                        # render (GIL). Relax it well back; the tier handles drift, the
                        # boundary detector handles song changes. (Live sets segue with
                        # no silent gap, so live_mode keeps polling instead.)
                        nxt = max(self.recal_secs, self._tune.get("confirmed_recal_s", 45.0))
                    else:
                        # poll as fast as feasible while the song isn't confirmed by
                        # sound yet (each listen is ~4s of audio — the floor)
                        unconfirmed = (not self._verified) or self._sound_song is None
                        nxt = max(4, min(self.recal_secs, 4) if unconfirmed else self.recal_secs)
                    # A non-zero offset is the main desync risk (a bad correction that
                    # stuck). Re-verify SOON so the reset-first logic snaps it back within
                    # seconds. A LIVE arrangement is FOLLOWED continuously (its offset drifts
                    # with the live tempo), so poll fast regardless of the current offset.
                    if self._live_arrangement:
                        nxt = min(nxt, 8)
                    elif abs(self.offset) > 0.8:
                        nxt = min(nxt, 12)
                    # ANTI-STUTTER BACK-OFF (de-escalation): a song Shazam simply
                    # CAN'T fingerprint (an MMD/cover/performance arrangement) stays
                    # "unconfirmed" forever, so the unconfirmed branch above polls
                    # recognize every ~4 s indefinitely — and each recognize stalls
                    # the render (GIL contention: ~150-475 ms frames, the visible
                    # stutter). Once the track is clearly SETTLED — lyrics loaded, in
                    # sync (|offset|≤1 s), and ~45 s have passed with no sound lock —
                    # there's nothing left to confirm, so stop hammering: back the
                    # poll off to a slow heartbeat. Song changes are still caught by
                    # the boundary detector; live_mode (concert) is exempt (it needs
                    # continuous polling to catch the next song).
                    if (self.lines and abs(self.offset) <= 1.0 and not self._live_mode
                            and time.time() - getattr(self, "_track_t0", 0.0) > 45.0
                            and ((not self._verified) or self._sound_song is None)):
                        nxt = max(nxt, self._tune.get("unconfirmed_backoff_s", 22.0))
        finally:
            self._arm_recal(nxt)

    def _concert_ocr_check(self):
        """(background thread) Read the on-screen song-title banner and, if it
        confidently names a song we have, switch the overlay to that song's lyrics.
        Runs only in live/concert mode. Best-effort: any failure is ignored and the
        sound-driven detection stands. See concert_ocr.py / docs/CONCERT_DETECTION.md."""
        try:
            import concert_ocr
            if not concert_ocr.available():
                return
            lines = concert_ocr.read_banner_lines()
            cands = [e.get("title") for e in self.index.entries if e.get("title")]
            m = concert_ocr.match_song(lines, cands) if cands else None
            uncached = concert_ocr.plausible_title(lines)
        except Exception:
            return
        cur = self.meta.get("title", "")
        # 1) a CACHED song the banner names → load it (highest confidence).
        if m and m[1] >= 0.85:
            title = m[0]
            if self._titles_match(cur, title):
                self._ocr_song = title
            elif title != self._ocr_song:
                self._ocr_song = title
                self.root.after(0, lambda t=title, s=m[1]: self._apply_ocr_song(t, s))
            return
        # 2) a plausible banner title we DON'T have yet → fetch it ('Departures', …),
        #    so the concert detection isn't limited to pre-cached songs.
        if (uncached and not self._titles_match(cur, uncached)
                and uncached != self._ocr_song):
            self._ocr_song = uncached
            self.root.after(0, lambda t=uncached: self._fetch_ocr_song(t))

    def _apply_ocr_song(self, title, score):
        """(Tk thread) The concert banner named `title` — load it (from cache, else
        fetch) and let sound lock the timing. OCR is the authority in a concert, so
        we title-lock it: a Shazam mis-ID on the live arrangement can't override it."""
        artist = (self._track or ("", ""))[0]
        cached = self.index.match(artist, title, self._cur_duration)
        if cached and self._file_valid(cached, self._cur_duration):
            if cached != self._lyrics_path:
                log.info("concert OCR read %r (%.2f) → %s", title, score, cached.name)
                self.load(cached)
                self._maybe_translate()
                # TICKET-099: concert OCR is the authoritative source in a live
                # set — treat it as a corroborated source so the public verified
                # flag goes true even before Shazam fingerprints the live cut.
                self._verified_meta = True
                self._sound_corroborated = True
                # TICKET batch4: route through _set_verified so the False→True
                # transition clears any pending render gate window.
                self._set_verified(True, reason="concert-ocr")
                self._title_locked = True          # OCR is authoritative in a concert
                self._sound_song = (title, artist)
                self._last_sound_lock_t = time.time()
                self.offset = 0.0
                self._fast_calib = max(self._fast_calib, 2)
                self._arm_recal(5)
                self._start_identify(seconds=6, attempts=2)   # lock timing by sound
        else:
            log.info("concert OCR read %r (%.2f) → fetching", title, score)
            self._start_fetch(artist, title, self._cur_duration)

    def _fetch_ocr_song(self, title):
        """(Tk thread) the concert banner named a song we DON'T have cached — fetch its
        lyrics COVER-style (the banner gives the SONG; the concert group isn't its
        artist, so a title-first lookup finds the original — e.g. 'Departures'). The
        loaded result shows the moment the fetch resolves (_consume_async)."""
        log.info("concert OCR read uncached %r → fetching (cover-style)", title)
        self._title_locked = True               # OCR is authoritative in a concert
        self._hint(f"🎤 {title} — fetching…")
        self._start_fetch("", title, None, cover=True)

    def _viewport_watchdog(self):
        """Light keeper for the FIXED full-work-area window. The window never
        moves now, so this only (a) re-asserts the fixed geometry + topmost in
        case another app disturbed it, and (b) as a belt-and-braces guard, trims
        a lane if content somehow overflows the window — WITHOUT moving the
        window. Checked every ~3s."""
        try:
            if self.root.winfo_viewable():
                self.root.geometry(f"{self.W}x{self.H}+{self.work_left}+{self.work_top}")
                if self.lines and self.scroll_dir in ("lr", "rl"):
                    bb = self.cv.bbox("all")
                    if bb and bb[3] > self.work_h and self._lanes > 1:
                        self._lanes -= 1
                        self._relayout_song()      # recompute _lane_y0 (window stays put)
                        self._clear_stream()
        finally:
            self.root.after(3000, self._viewport_watchdog)

    def _suspect(self, st):
        """Signs the current lyrics don't belong to what's actually playing."""
        dur, pos = st.get("duration"), st.get("position", 0)
        if not self.lines:
            return self._sound_song is None    # no match yet, not sound-checked
        md = self.meta.get("duration")
        last_end = self.lines[-1].end if self.lines else 0
        if dur and md and abs(md - dur) > 12:
            return True                                   # wrong version/song
        if dur and last_end and last_end > dur + 15:
            return True              # lyrics run PAST the song's end = wrong/longer version
        if dur and last_end and last_end < dur * 0.6 and pos > last_end + 8 \
                and pos < dur - 5:
            return True                                   # lyrics don't cover song
        if not self._verified and self._sound_song is None:
            return True                                   # unverified → confirm by ear
        return False

    def _consume_async(self):
        if self._fetch_result:
            key, p = self._fetch_result
            self._fetch_result = None
            if key == self._fetch_key:
                if p:
                    # If captions (video-locked ground truth) already loaded for
                    # this song, a slower LRC fetch must NOT overwrite them.
                    if (self.meta.get("source") or "") == "youtube-captions":
                        log.info("keeping YouTube captions over LRC %s", Path(p).name)
                        self.index.add(p)
                    else:
                        self.index.add(p)
                        self.load(Path(p))
                        self._start_translate(Path(p))
                        if self.git_sync:           # back up the new song if opted in
                            self.git_backup()
                elif self._identifying:
                    pass                       # sound-ID running → wait for it
                elif self._sound_song is None:
                    # title/artist missed (e.g. name-variant) — Shazam returns
                    # the canonical name, which usually fetches fine. Try sound first.
                    self._hint("🎧 Finding the song by sound…")
                    self._start_identify()
                else:
                    # TRUE last resort: the title gave nothing, sound *did* identify
                    # the song, and even that has no lyrics anywhere → generate by ear.
                    self._begin_generation()
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
                title, artist, offset, t_cap = res
                self._intro_anchored = True   # Shazam can align this → drop the MV dead-space guess
                # SOUND IS THE AUTHORITY. Shazam often romanizes JP titles
                # ("Kira" for 綺羅), so when the player's own title is CJK keep
                # that original script for fetching/matching — UNLESS the player
                # title and Shazam's title share no content at all (Niconico's
                # sidebar / recommended-video session can leak a totally
                # unrelated CJK title like "Space Marine 2 プレイ動画 #35" while
                # the actual Marine song plays — trust Shazam in that case).
                g_artist, g_title = (self._track or ("", ""))
                same_song = self._titles_share_content(g_title, title)
                if (_has_cjk(g_title) and not _has_cjk(title)
                        and not _is_generic_title(g_title) and not self._live_mode
                        and same_song):
                    f_artist, f_title = (g_artist or artist), g_title
                else:
                    f_artist, f_title = artist, title
                    if _has_cjk(g_title) and not same_song:
                        log.info("player title %r and Shazam title %r share no content "
                                 "— trusting Shazam (player session likely stale)",
                                 g_title, title)
                heard = (f_title, f_artist)
                # TICKET-099: fetch SMTC state once at the top so the disagreement
                # branches below can route by source priority (paused-SMTC takeover
                # vs the normal SMTC-playing wrong-song flow). Cached locally so
                # we don't hit the cross-thread media getter twice per result.
                st_now = self.media.get() or {}
                # TICKET batch4 A3: album-string fallback. Some Shazam reads
                # respond with the ALBUM name plus a long "(feat. ...)" block
                # instead of the actual track name (V.W.P "DIVA (feat. KAF,
                # RIM, Harusaruhi, Isekaijoucho & KOKO)" vs SMTC's track
                # "歌姫"). The existing wrong-song strike flow then spent ~71s
                # tearing the lyrics down on what was actually the right song.
                # When the Shazam title carries clear album-context markers
                # AND it shares no content with the SMTC title (i.e. it's not
                # the romanized-CJK alias case the existing flow already
                # handles), accept SMTC as canonical and set the alias so
                # subsequent reads of the same album-string still calibrate
                # via the existing alias path. Heuristic is intentionally
                # conservative — structural markers, not just any disagreement.
                loaded_title = self.meta.get("title", "")
                if (int(self._tune.get("title_alias_album_fallback", 1) or 0) == 1
                        and bool(self.lines)
                        and loaded_title
                        and f_title
                        and not self._titles_match(loaded_title, f_title)
                        and (self._sound_title_alias is None
                             or not self._titles_match(self._sound_title_alias, f_title))
                        and not self._titles_share_content(loaded_title, f_title)
                        and not _is_generic_title(loaded_title)
                        and len(_norm_title(loaded_title)) >= 2):
                    markers = ("(feat.", " - EP", " - Single", " (Album)",
                               "(Album)", "(Deluxe", "(Remastered", " Vol.",
                               " vol.", " / ")
                    has_marker = any(m in f_title for m in markers)
                    # ASCII all-caps multi-word: scoped tight (ASCII-only,
                    # ≥2 space-separated tokens, total len ≥5) so JP/KR style
                    # caps titles like 'IDOL'/'KICK BACK' don't trip it. Even
                    # so we require a co-marker to fire — single-signal caps
                    # alone are too fragile across languages.
                    ascii_only = all(ord(c) < 128 for c in f_title)
                    parts = f_title.split()
                    caps_style = (ascii_only and len(parts) >= 2
                                  and len(f_title) >= 5
                                  and f_title.upper() == f_title
                                  and any(p.isalpha() for p in parts))
                    if has_marker or caps_style:
                        log.info("album-alias accepted: shazam-title %r treated as "
                                 "album for smtc-track %r (markers=%s caps=%s)",
                                 f_title, loaded_title, has_marker, caps_style)
                        self._sound_title_alias = f_title
                # Does the song we HEARD match the lyrics currently loaded?
                # Title match OR the romanized-title alias: when we loaded an
                # original-script (CJK) cache for a romanized heard title (e.g.
                # Japanese "Ahoy!! 我ら宝鐘海賊団☆" for Shazam's "Ahoy!! We are
                # Houshou Pirates"), the titles won't string-match — but it IS
                # the same song, so Shazam must still CALIBRATE its timing.
                # Without this, _last_audio_off stays stale and the energy
                # correlator drifts onto a chorus-repetition match (-23.7s bug).
                loaded_ok = bool(self.lines) and (
                    self._titles_match(self.meta.get("title", ""), f_title)
                    or (self._sound_title_alias is not None
                        and self._titles_match(self._sound_title_alias, f_title)))
                log.info("heard %r / %r | loaded %r | match=%s",
                         f_title, f_artist, self.meta.get("title", ""), loaded_ok)
                if loaded_ok:
                    # CALIBRATE timing ONLY when the heard song is the loaded one.
                    # (Applying a heard song's offset to *different* lyrics — e.g.
                    # a Shazam mis-ID on a mix — was what produced wild offsets.)
                    self._sound_song = heard
                    self._last_sound_lock_t = time.time()
                    self._pending_switch = None     # current song reconfirmed
                    self._sound_fail_streak = 0     # heard == loaded → sync works, clear strikes
                    self._last_heard_contra = None
                    # TICKET-099: Shazam has now corroborated the loaded title at
                    # least once → promote public `verified` from False (meta-only)
                    # to True (meta AND sound-corroborated). This is the gate
                    # that v1.0.88 was missing: a duration-match alone was being
                    # reported as verified before sound ever heard the song.
                    self._mark_verified(confirmed=True)
                    self._source_priority = "agree"
                    # TICKET-090: Verified-Shazam WINS. When the Shazam fingerprint
                    # matched (we're in the loaded_ok branch ⇒ heard == loaded) AND
                    # the song has been verified by duration/title (_mark_verified
                    # turns _verified on at load time), the decide-by-ear loop must
                    # not be allowed to override us with a hallucinated transcript.
                    # Promote the title-lock immediately. The fuzzy-match check is
                    # already loaded_ok (substring or _titles_match); harden it
                    # with an explicit _norm_title equality OR substring on title
                    # AND artist to avoid latching onto a same-title different-song.
                    loaded_title = self.meta.get("title", "") or ""
                    loaded_artist = self.meta.get("artist", "") or ""
                    nt_loaded, nt_heard = _norm_title(loaded_title), _norm_title(f_title)
                    na_loaded, na_heard = _norm_title(loaded_artist), _norm_title(f_artist or "")
                    title_fuzzy = bool(nt_loaded and nt_heard and (
                        nt_loaded == nt_heard or nt_loaded in nt_heard or nt_heard in nt_loaded))
                    artist_fuzzy = (not na_loaded) or (not na_heard) or (
                        na_loaded == na_heard or na_loaded in na_heard or na_heard in na_loaded)
                    if (self._verified and title_fuzzy and artist_fuzzy
                            and self.lines and not self._live_mode
                            and not self._title_locked):
                        # TRANSITION False → True: reset offset+drift state so the
                        # newly-locked timing starts from a clean baseline (any
                        # stale pending offset from before the lock could otherwise
                        # commit a wrong correction the moment we lock).
                        log.info("title-lock: verified Shazam + heard %r matches loaded %r "
                                 "→ LOCKING (resetting offset/drift state)",
                                 f_title, loaded_title)
                        self._title_locked = True
                        self.offset = 0.0                 # direct write, NOT _smooth_offset
                        self._pending_offset = None
                        self._pending_corr = 1e9
                        self._drift_integral = 0.0
                        self._drift_integral_t = 0.0
                        try:
                            self._offset_hist.append((round(time.time(), 1), 0.0))
                            self._offset_hist_last = 0.0
                            if len(self._offset_hist) > 40:
                                del self._offset_hist[:len(self._offset_hist) - 40]
                        except Exception:
                            pass
                    if offset is not None and t_cap is not None:
                        st = self.media.get()
                        if st and st.get("status") == PLAYING:
                            true_now = offset + (time.time() - t_cap) * st.get("rate", 1.0)
                            corr = true_now - st["position"]   # offset the AUDIO implies
                            diff = corr - self.offset          # how far the DISPLAY is off NOW
                            dur = self._cur_duration or st.get("duration") or 0
                            DEADBAND = self._tune["deadband"]      # within this the exact player clock wins
                            AGREE = self._tune["agree"]            # two reads this close ⇒ a corroborated offset
                            # MODE decides the whole strategy. A STUDIO track has an EXACT
                            # player clock, so its true offset is ~0 and big readings are
                            # artifacts (Shazam matching a repeated chorus) to distrust →
                            # reset-first. A LIVE/short/alternate ARRANGEMENT (by title, or a
                            # big duration mismatch vs the studio LRC) has a REAL, possibly
                            # large/drifting offset that must be FOLLOWED, not reset.
                            lrc_span = self.lines[-1].end if self.lines else 0.0
                            dur_mismatch = bool(lrc_span and dur and abs(dur - lrc_span) > 25)
                            dur_match = bool(lrc_span and dur and abs(dur - lrc_span) < 12)
                            live = dur_mismatch or (self._live_arrangement and not dur_match)
                            # live offset can land anywhere in the song → cap at the studio
                            # length; studio keeps the tight cap.
                            cap = (((lrc_span + 15) if lrc_span else 600.0) if live
                                   else (min(120.0, max(45.0, 0.4 * dur)) if dur else 75.0))
                            # AMBIGUITY: repeated choruses make Shazam return wildly varying
                            # offsets (e.g. -10s then -70s on サクラミラージュ). Track the spread
                            # of recent reads to detect it.
                            self._recent_corr.append(round(corr, 2))
                            self._recent_corr = self._recent_corr[-4:]
                            spread = (max(self._recent_corr) - min(self._recent_corr)
                                      if len(self._recent_corr) >= 2 else 0.0)
                            # SYNC TELEMETRY — log EVERY read so a developing desync is visible:
                            # drift = how far the shown lyrics are from where the audio says they
                            # should be (+ve ⇒ lyrics are LATE).
                            cur_t = (self.lines[self.idx].start
                                     if 0 <= self.idx < len(self.lines) else -1.0)
                            self._last_drift = round(diff, 2)
                            self._last_drift_t = time.time()
                            # Track the absolute Shazam-implied offset so the
                            # energy correlator can sanity-check its result
                            # against it (rejects chorus-repetition mismatches).
                            self._last_audio_off = corr
                            self._last_audio_off_t = self._last_drift_t
                            log.info("sync-read: drift=%+.2fs audio_off=%+.2f shown_off=%+.2f "
                                     "mode=%s spread=%.1f pos=%.1f line#%d@%.1f pend=%s",
                                     diff, corr, self.offset, "live" if live else "studio",
                                     spread, st["position"], self.idx, cur_t,
                                     ("%+.2f" % self._pending_corr) if self._pending_corr < 1e8 else "-")
                            if abs(corr) >= cap:
                                # matched a different recording/segment — no usable info; ignore.
                                self._pending_corr = 1e9
                            elif abs(diff) <= DEADBAND:
                                self._pending_corr = 1e9      # already in sync — leave it
                            elif live:
                                # FOLLOW: the offset is real. Apply a corroborated reading even
                                # when large, smoothing toward it to ride tempo drift without
                                # jitter, and keep following (pending stays set so each read
                                # nudges the offset).
                                AGREE_LIVE = self._tune["agree_live"]
                                if abs(corr - self._pending_corr) < AGREE_LIVE:
                                    new = round(
                                        (corr if abs(self.offset) < DEADBAND
                                         else 0.6 * corr + 0.4 * self.offset), 2)
                                    log.info("sync(live): following → offset %+.2fs (drift was %+.2f)",
                                             new, diff)
                                    self._smooth_offset(new, "sync(live)-follow")   # TICKET-081
                                    self._pending_corr = corr
                                else:
                                    self._pending_corr = corr
                                    log.info("sync(live): holding %+.2fs for a 2nd read", corr)
                            elif spread > self._tune["spread_reset"] and abs(self.offset) < self._tune["reset_offset_max"]:
                                # STUDIO with AMBIGUOUS reads (repeated choruses): the player
                                # clock is exact, so do NOT chase these contradictory offsets —
                                # reset (fixes サクラミラージュ's -10s/-70s chorus jumps).
                                # But ONLY when the current offset is small (< 5 s). A larger
                                # offset is doing real work (Grimes "Oblivion" needs ~-22 s for
                                # the studio LRC vs the album cut) and resetting it on chorus
                                # ambiguity made sync lurch back to wrong every chorus. Spread
                                # threshold raised 15 → 20 for the same reason.
                                self._pending_corr = 1e9
                                if abs(self.offset) > DEADBAND:
                                    log.info("sync: ambiguous reads (spread %.0fs, small offset "
                                             "%+.2fs) → backing off to 0", spread, self.offset)
                                    self._smooth_offset(0.0, "sync-ambiguous-reset")   # TICKET-081
                            elif abs(corr) <= DEADBAND:
                                # audio says NO offset needed but we're showing one → drifted →
                                # reset to the exact player clock (manual "reset to 0", automatic).
                                self._pending_corr = 1e9
                                log.info("sync: audio_off≈0 but showing %+.2fs → AUTO-RESET to 0", self.offset)
                                self._smooth_offset(0.0, "sync-audio0-reset")    # TICKET-081
                                self._last_sound_lock_t = time.time()
                            elif abs(corr - self._pending_corr) < AGREE:
                                # real non-zero offset CORROBORATED by a 2nd agreeing read → apply.
                                self._pending_corr = 1e9
                                log.info("sync: CONFIRMED offset %+.2fs (two reads agree) → applied", corr)
                                self._smooth_offset(round(corr, 2), "sync-confirmed")    # TICKET-081
                                self._last_sound_lock_t = time.time()
                            else:
                                # TWO-POINT VERIFICATION: a single read of a non-zero
                                # offset is NEVER applied — on a song with choruses the
                                # first read can match a repeated section and point to
                                # the wrong place. Hold it, then HESITATE ~2 s and take a
                                # confirming listen; only when the 2nd read agrees (the
                                # AGREE branch above) does the offset commit.
                                self._pending_corr = corr
                                log.info("sync: holding %+.2fs — confirming with a 2nd listen in 2s", corr)
                                self._schedule_sync_confirm()
                                # ── Continuous drift-integral fallback ──
                                # When Shazam reads a non-trivial drift but won't
                                # confirm, accumulate |drift|·dt over time. Trigger
                                # the energy correlator / Whisper align when the
                                # integral crosses 6.0 — proportional to how wrong
                                # the sync actually is, not an arbitrary count of
                                # reads. A 1.5s drift held for 4s ≈ integral 6;
                                # a 3s drift held for 2s ≈ integral 6; either way
                                # the correction fires when warranted, not on a
                                # hardcoded strike threshold.
                                now_t = time.time()
                                dt_last = (min(15.0, now_t - self._drift_integral_t)
                                           if self._drift_integral_t > 0 else 4.0)
                                self._drift_integral_t = now_t
                                if abs(diff) > self._tune["drift_min_for_accum"]:
                                    # cap the accumulator: if the offset never
                                    # confirms (repeated choruses → reads never
                                    # agree) it would otherwise grow without bound
                                    # and re-fire the aligner every read.
                                    self._drift_integral = min(
                                        12.0, self._drift_integral + abs(diff) * dt_last)
                                else:
                                    # decay quickly when drift drops back into the deadband
                                    self._drift_integral *= 0.5
                                if self._drift_integral > self._tune["drift_align_trigger"]:
                                    log.info("drift integral %.1f crossed threshold → auto-aligning by ear",
                                             self._drift_integral)
                                    self._drift_integral = 0.0
                                    self._maybe_auto_align(reason="drift-integral")
                elif (self.meta.get("source") or "").startswith("bundled") and self.lines:
                    # BAKED-IN authoritative lyrics. These songs (MMD / "Performance
                    # Video" cuts) can't be Shazam-fingerprinted, so Shazam keeps
                    # mis-ID'ing them as random other tracks (サクラミラージュ heard as
                    # "Daybreak Frontline" / "Mumei"). A baked cache is ground truth —
                    # NEVER let a heard mis-ID override it (no switch, no strikes).
                    log.info("ignoring sound %r — bundled (baked) lyrics are "
                             "authoritative for %r", f_title, self.meta.get("title", ""))
                elif (self.lines
                      and self._resolve_source_priority(st_now, heard) == "shazam-live"
                      and (heard == self._pending_switch
                           or heard == self._last_heard_contra)):
                    # TICKET-099: SMTC has been NOT-PLAYING for ≥ smtc_paused_min_s
                    # AND a 2nd Shazam read AGREES with a previously-held heard
                    # song (either pending_switch from the normal flow OR the
                    # last_heard_contra from the title-lock strike flow). A
                    # paused SMTC tab cannot be what's audible in the room, so
                    # SMTC's authority is forfeit and we take over with what we
                    # actually hear. Mirrors the wrong-song correction path but
                    # bypasses the 5-strike count (the paused-floor + 2-read
                    # agreement are the gates here). Debounced by
                    # smtc_takeover_debounce_s so a fresh swap gets time to
                    # settle. A SMTC PLAYING flip clears _smtc_paused_since and
                    # _last_takeover_t (see _update_smtc_pause_state) so a real
                    # user un-pause is heard immediately.
                    self._smtc_paused_takeover(heard, f_title, f_artist)
                elif (self.lines
                      and self._resolve_source_priority(st_now, heard) == "shazam-live"
                      and heard != self._pending_switch
                      and heard != self._last_heard_contra):
                    # TICKET-099: SMTC paused + Shazam disagrees, FIRST read. Hold
                    # for a 2nd agreeing read before we drop the loaded lyrics
                    # (Shazam mis-IDs niche audio — CS2 menu music reads as
                    # 'Crosshair Kings' one call and the original menu music the
                    # next; neither alone is enough). Mirror the normal pending_switch
                    # 2-read flow; the takeover above will fire on the agreeing 2nd read.
                    # Per design: even a SINGLE contradicting read from a paused
                    # SMTC source DEMOTES verified=true to false and DROPS the
                    # title-lock (lyrics stay on screen but no longer carry the
                    # verified badge), because a paused tab can't be the room
                    # audio so Shazam's reading is more trustworthy than SMTC.
                    if self._verified or self._sound_corroborated or self._title_locked:
                        log.info("smtc-paused-takeover: demoting verified/title-lock — "
                                 "SMTC paused + Shazam contradicts loaded %r with %r",
                                 self.meta.get("title", ""), f_title)
                    self._sound_corroborated = False
                    # TICKET batch4: route through _set_verified so a True→False
                    # transition arms the render gate (lyrics survive while we
                    # wait for the 2nd read or for the album-alias path to
                    # re-confirm). This is the demote path the gate exists for.
                    self._set_verified(False, reason="smtc-paused-demote")
                    self._title_locked = False
                    self._pending_switch = heard
                    # Also seed last_heard_contra so the title-lock strike flow
                    # would also count this; both paths funnel into the takeover
                    # branch on the next agreeing read.
                    if heard != self._last_heard_contra:
                        self._last_heard_contra = heard
                        self._sound_fail_streak = 1
                    log.info("smtc-paused-takeover: heard %r ≠ loaded %r (SMTC paused) "
                             "— awaiting 2nd agreeing read",
                             f_title, self.meta.get("title", ""))
                elif self._title_locked:
                    # The lyrics came from a confident EXACT match on a clean
                    # official title, but Shazam heard a DIFFERENT song — usually a
                    # mis-ID of another track by the SAME artist (feelingradation
                    # heard as SKAVLA). Normally we trust the title and ignore it.
                    # BUT if we keep hearing the SAME other song over and over, the
                    # title-lock is genuinely WRONG (we loaded "Dunk" for a "Deep
                    # Dive" video) and no amount of re-syncing will ever fix it — the
                    # song is wrong. After N strikes (user's rule: 5) BREAK the lock
                    # and switch to what we actually hear.
                    # TICKET-081: parenthetical equivalence — 'GHOST' and 'Ghost
                    # (Still Still Stellar ver.)' are the SAME song; don't strike.
                    # Strips '(…)' and '[…]' suffixes and compares titles. Same
                    # for the JP variants '（…）' '［…］'.
                    loaded_t = self.meta.get("title", "")
                    bare_heard = re.sub(r"\s*[\(（\[［][^\)）\]］]*[\)）\]］]\s*$", "", f_title).strip()
                    bare_loaded = re.sub(r"\s*[\(（\[［][^\)）\]］]*[\)）\]］]\s*$", "", loaded_t).strip()
                    if (bare_heard and bare_loaded
                            and self._titles_match(bare_heard, bare_loaded)):
                        log.info("title-lock: %r ≡ %r after stripping parenthetical "
                                 "suffix — not a strike", f_title, loaded_t)
                        self._last_heard_contra = None
                        self._sound_fail_streak = 0
                        self._sound_song = heard           # treat as same song confirmed
                        self._last_sound_lock_t = time.time()
                        return
                    if heard == self._last_heard_contra:
                        self._sound_fail_streak += 1
                    else:
                        self._last_heard_contra, self._sound_fail_streak = heard, 1
                    base_strikes = self._tune.get("wrong_song_strikes", 5)
                    # TICKET-081: artist disagreement → DOUBLE the bar before
                    # overriding (the GHOST/Suisei case where Shazam mis-IDs to a
                    # halloween track by some unrelated artist). When SMTC artist
                    # corroborates the heard artist OR the loaded artist, the
                    # mismatch is more likely a real same-artist mis-ID and the
                    # current 5 still applies.
                    smtc_artist = _norm_title(g_artist)
                    heard_artist_n = _norm_title(f_artist)
                    loaded_artist_n = _norm_title(self.meta.get("artist") or "")
                    artist_corroborates = bool(smtc_artist and heard_artist_n and (
                        smtc_artist == heard_artist_n
                        or smtc_artist in heard_artist_n
                        or heard_artist_n in smtc_artist))
                    if (smtc_artist and heard_artist_n and not artist_corroborates
                            and not (loaded_artist_n and loaded_artist_n == heard_artist_n)):
                        strikes = base_strikes * 2     # 10 strikes when artist disagrees
                    else:
                        strikes = base_strikes
                    if self._sound_fail_streak >= strikes:
                        # HIGH-FIX (verify lens 3-of-3): wire verified_render_gate
                        # at the mid-track teardown site. When the v1.0.89 strict
                        # verified gate just flipped to False (e.g., a transient
                        # album-vs-track Shazam disagreement like V.W.P 歌姫 /
                        # Shazam "DIVA (feat. ...)"), defer the wrong-song switch
                        # for verified_render_gate_s seconds. If title_alias_album_fallback
                        # accepts the alias and verified flips back to True, the
                        # gate clears and the strike resolves naturally. Real
                        # wrong-song persists and will retrigger after the window.
                        # Real track changes go through _on_track_change which
                        # already sets _verified_gate_t=0.0 so this gate never
                        # defers a legitimate track change.
                        gate_s = float(self._tune.get("verified_render_gate_s", 3.0))
                        gate_active = (self._verified_gate_t
                                       and time.time() - self._verified_gate_t < gate_s)
                        if gate_active:
                            remaining = gate_s - (time.time() - self._verified_gate_t)
                            log.info("wrong-song teardown DEFERRED by verified_render_gate "
                                     "(%.1fs remaining; heard=%r locked=%r streak=%d/%d)",
                                     remaining, f_title, self.meta.get("title", ""),
                                     self._sound_fail_streak, strikes)
                            # Decay the streak so a transient disagreement doesn't
                            # immediately retrigger after the gate window expires.
                            self._sound_fail_streak = max(0, self._sound_fail_streak - 1)
                            return
                        log.info("title-lock OVERRIDDEN: heard %r %d× ≠ locked %r → wrong "
                                 "song, switching", f_title, self._sound_fail_streak,
                                 self.meta.get("title", ""))
                        self._title_locked = False
                        self._sound_fail_streak = 0
                        self._last_heard_contra = None
                        self._pending_switch = None
                        self._sound_song = heard
                        self._last_sound_lock_t = time.time()
                        self.offset = 0.0
                        self._fast_calib = max(self._fast_calib, 2)
                        self._arm_recal(7)
                        cached = self._prefer_cjk_cache(f_artist, f_title, self._cur_duration) \
                            or self.index.match(f_artist, f_title, self._cur_duration)
                        if cached and self._file_valid(cached, self._cur_duration):
                            if cached != self._lyrics_path:
                                log.info("wrong-song correction → cached %s", cached.name)
                                self.load(cached)
                            self._maybe_translate()
                        else:
                            log.info("wrong-song correction → fetching %r / %r", f_title, f_artist)
                            self._hint(f"🔄 Wrong song — switching to {f_title}…")
                            self._start_fetch(f_artist, f_title, self._cur_duration)
                    else:
                        log.info("ignoring sound %r — title-locked to %r (strike %d/%d)",
                                 f_title, self.meta.get("title", ""),
                                 self._sound_fail_streak, strikes)
                elif heard == self._sound_song:
                    # Already switched to this heard song and its lyrics are still
                    # pending or simply don't exist (generation handles that). Don't
                    # re-reset the offset / re-fetch on every repeat hearing — that
                    # churned the sync and restarted generation. Just leave it be.
                    pass
                elif self.lines and heard != self._pending_switch:
                    # Heard a DIFFERENT song while we already have lyrics for the
                    # current one. A single contradicting reading is usually a
                    # spurious Shazam mis-ID on a niche track (Tombi briefly heard as
                    # a piano concerto), which used to reset the offset + re-fetch +
                    # re-generate. Require a SECOND reading of the same new song
                    # before switching; a real song change re-confirms in seconds.
                    self._pending_switch = heard
                    log.info("heard %r ≠ loaded %r — awaiting confirmation before switch",
                             f_title, self.meta.get("title", ""))
                else:
                    # A different song, confirmed (or nothing loaded yet) → switch to
                    # it; start its timing fresh rather than carrying the old offset.
                    self._pending_switch = None
                    self._sound_song = heard
                    self._last_sound_lock_t = time.time()
                    self.offset = 0.0
                    self._fast_calib = max(self._fast_calib, 2)
                    self._arm_recal(7)
                    # Prefer an original-script (CJK) cache over a romanized title
                    # — fixes Shazam's English title loading English lyrics for a
                    # Japanese song (Niconico Marine "Ahoy!!").
                    cached = self._prefer_cjk_cache(f_artist, f_title, self._cur_duration) \
                        or self.index.match(f_artist, f_title, self._cur_duration)
                    if cached and self._file_valid(cached, self._cur_duration):
                        if cached != self._lyrics_path:
                            log.info("correcting -> cached %s", cached.name)
                            self.load(cached)
                        # If the loaded cache title doesn't string-match the heard
                        # (romanized) title, remember the alias so future Shazam
                        # reads still calibrate this song's timing (see loaded_ok).
                        if not self._titles_match(self.meta.get("title", ""), f_title):
                            self._sound_title_alias = f_title
                            log.info("aliased heard title %r → loaded %r for calibration",
                                     f_title, self.meta.get("title", ""))
                        self._maybe_translate()
                    else:
                        log.info("correcting -> fetching %r / %r", f_title, f_artist)
                        self._start_fetch(f_artist, f_title, self._cur_duration,
                                          strict=self._clean_source())

    # ── last-resort lyric GENERATION (transcribe the audio) ──
    def _begin_generation(self):
        """No provider had this song — generate lyrics by ear: transcribe the live
        audio with Whisper into timed JP, add furigana + romaji + a *likely*
        translation (every line marked ``***`` so it's clearly AI-made, not
        official). Opt-in (`generate_on`), needs faster-whisper, runs in the
        background, accumulating + saving so the next play is instant and synced."""
        if not getattr(self, "generate_on", True):
            self._hint("No lyrics found for this song")
            return
        try:
            import align
            ok = align.available()
        except Exception:
            ok = False
        if not ok:
            self._hint("No lyrics found (install faster-whisper to auto-generate)")
            return
        st = self.media.get()
        if not (st and st.get("status") == PLAYING) or self._generating:
            if not self._generating:
                self._hint("No lyrics found for this song")
            return
        artist, title = (self._track or ("", ""))
        self._generating = True
        self._gen_token += 1
        self._gen_title, self._gen_artist = (title or "song"), (artist or "")
        self._gen_lines = []
        self._gen_lang = None          # first chunk auto-detects the sung language
        self.lines, self.idx, self._kara = [], -1, []
        self._lyrics_path = None
        self._verified = False
        self._verified_meta = False           # TICKET-099
        self._sound_corroborated = False      # TICKET-099
        self._verified_gate_t = 0.0           # TICKET batch4: explicit teardown, no gate
        self.meta = {"title": self._gen_title, "artist": self._gen_artist,
                     "lang": "ja", "duration": self._cur_duration, "source": "generated"}
        self._hint("✨ Generating lyrics by ear… (AI — marked ***)")
        log.info("generating lyrics by Whisper for %r", self._gen_title)
        threading.Thread(target=self._generate_loop,
                         args=(self._gen_token,), daemon=True).start()
        # Tier 2 (deep): in the BACKGROUND, download the source audio + transcribe
        # the WHOLE file with the large model, then replace this rough best-effort
        # cache with a clean, complete one. Runs once per song. See
        # deep_transcribe.py / docs/GENERATION.md.
        self._begin_deep_generation(self._deep_token, self._gen_title, self._gen_artist)

    def _whisper_lang_lock(self, lang):
        """TICKET-089: decide which language to pin Whisper to for deep
        transcription. Returns the language code to pass as ``language=`` (or
        ``None`` to let Whisper auto-detect).

        Whisper auto-detect hallucinates Japanese on Spanish/English/Korean
        audio (live diag: Calibre 50 ES → "てれこにでもなくすまさきまで…").
        When we already know the song's language — from per-chunk detection in
        the Tier-1 loop (``self._gen_lang``) or the loaded metadata
        (``self.meta["lang"]``) — pinning eliminates the wrong-script
        hallucination. Only the languages we have confidence in are pinned;
        "ja" stays on auto because the library is mostly Japanese already and
        any other auto-detection error there is preferable to forcing JA on a
        mis-tagged track. Gated by the ``whisper_lang_lock`` tune knob (set 0
        to revert to auto-detect for A/B testing)."""
        if int(self._tune.get("whisper_lang_lock", 1) or 0) != 1:
            return None
        # Whitelist of languages we trust the metadata/per-chunk detector for.
        # "ja" is deliberately EXCLUDED — auto-detect is already accurate on a
        # mostly-Japanese library, and forcing JA would mask mis-tagged tracks.
        ALLOW = {"es", "en", "de", "fr", "ko", "zh", "pt", "it", "ru"}
        lang = (lang or "").strip().lower()
        return lang if lang in ALLOW else None

    def _begin_deep_generation(self, token, title, artist):
        """Spawn the offline high-quality transcription (Tier 2). No-op if deep
        transcription isn't available (no yt-dlp), generation is off, or we've
        already tried this song. Cancels via `token` when the track changes."""
        try:
            import deep_transcribe
        except Exception:
            return
        if not (self.generate_on and deep_transcribe.available()):
            return
        from fetch_lyrics import slugify
        slug = slugify(title)
        if slug in self._deep_tried:
            return                      # one attempt per song (per run)
        # Already have a deep cache for this song? then nothing to redo.
        try:
            existing = LYRICS_DIR / f"{slug}.json"
            if existing.exists() and '"generated-deep"' in existing.read_text("utf-8"):
                return
        except Exception:
            pass
        self._deep_tried.add(slug)
        # TICKET-089: thread the known song language through to Whisper so it
        # doesn't auto-detect into a hallucinated script. Prefer the per-chunk
        # detector's result (Tier 1 already listened) over the metadata tag,
        # which can be the stale "ja" default from _start_generation. `pin_lang`
        # is what we hand to Whisper (None = auto-detect, only set for the
        # whitelisted non-JA languages); `raw_lang` is preserved as the
        # downstream annotation fallback so "ja" still drives furigana etc.
        raw_lang = self._gen_lang or self.meta.get("lang")
        pin_lang = self._whisper_lang_lock(raw_lang)
        lang = raw_lang

        def work():
            try:
                res = deep_transcribe.deep_transcribe(title, artist, lang=pin_lang)
            except Exception as e:
                log.info("deep gen error: %s", e)
                return
            if not res or token != self._deep_token:
                return
            lines, dl, real_meta = res
            dlang = dl or lang or "ja"
            if real_meta:
                # REAL provider lyrics, found via the video's canonical title
                # (deep_transcribe already annotated them) — the player gave an
                # English/translated title that missed the cache, but yt-dlp's real
                # title reached the lyrics. Save as REAL, no AI '***' marker.
                src = real_meta.get("source") or "provider"
            else:
                # By-ear transcription. Annotate (furigana / romaji + the NETWORK
                # translation) HERE, off the Tk thread — the round-trip must never
                # block the UI — and mark each line AI ('***').
                src = "generated-deep"
                try:
                    from fetch_lyrics import annotate
                    annotate(lines, dlang, translate=True)
                except Exception:
                    pass
                for d in lines:
                    if d.get("en", "").strip() and not d["en"].rstrip().endswith("***"):
                        d["en"] = d["en"].strip() + " ***"
            if token == self._deep_token:
                self.root.after(0, lambda: self._apply_deep(token, title, artist, lines, dlang, src))

        threading.Thread(target=work, daemon=True).start()

    def _apply_deep(self, token, title, artist, lines, lang, source="generated-deep"):
        """(Tk thread) save the already-annotated deep lines as the cache and
        upgrade the overlay live if this song still plays. `source` is
        `generated-deep` for a by-ear transcription, or a provider source when the
        canonical-title lookup found REAL lyrics."""
        if token != self._deep_token:
            return                      # track changed (or real lyrics loaded) → discard
        # REAL lyrics may have arrived (a slow fetch finally resolved) while we
        # transcribed — they WIN. Don't save or show a generated-deep version over
        # them (that was the "some ended up generated too" overwrite). A canonical
        # REAL result is itself real, so it may replace a generated stopgap.
        real = not source.startswith("generated")
        if self.lines and not (self.meta.get("source") or "").startswith("generated") and not real:
            return
        from fetch_lyrics import slugify
        try:
            out = LYRICS_DIR / f"{slugify(title)}.json"
            data = {"meta": {"title": title, "artist": artist, "lang": lang,
                             "duration": self._cur_duration, "source": source},
                    "lines": lines}
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
            self.index.add(out)
            log.info("deep %s: saved %d lines -> %s",
                     "real" if real else "gen", len(lines), out.name)
        except Exception as e:
            log.info("deep gen save failed: %s", e)
            return
        # If this is still the song playing, upgrade the overlay live.
        _, cur_t = (self._track or ("", ""))
        if self._titles_match(cur_t, title):
            self._gen_token += 1        # stop the Tier-1 best-effort loop
            self._generating = False
            self.load(out, keep_idx=True)
            self._hint("✨ Found the real lyrics" if real else "✨ Upgraded to full transcription")
            # The hint cleared the canvas, but load(keep_idx=True) left self.idx on
            # the current line — so the tick loop, seeing the index unchanged,
            # would NOT redraw the line and the hint would stay stuck with no
            # lyrics until the line changed. Force the next tick to re-render.
            self.idx = -1

    def _generate_loop(self, token):
        """Capture the song in chunks, transcribe each, annotate, accumulate.
        Cancels the moment the track changes (token bump)."""
        import align
        from fetch_lyrics import annotate
        CHUNK, last_end, idle, first = 16, 0.0, 0, True
        while token == self._gen_token:
            st = self.media.get()
            if not (st and st.get("status") == PLAYING):
                time.sleep(1.0)
                idle += 1
                if idle > 25:
                    break                                # gave up (paused/stopped)
                continue
            idle = 0
            pos = float(st.get("position") or 0.0)
            secs = 8 if first else CHUNK      # short FIRST chunk → lyrics appear sooner
            first = False
            # Auto-detect the sung language on EVERY chunk (lang=None). Pinning the
            # first chunk's guess mis-fired when the intro was instrumental/ambiguous
            # and locked e.g. an English cover into Japanese gibberish; per-chunk
            # detection self-corrects and even handles bilingual songs.
            chunk = align.transcribe_for_generation(pos, lang=None, seconds=secs)
            self._gen_lang = getattr(align, "_last_gen_lang", None) or self._gen_lang
            if token != self._gen_token:
                return
            new = [d for d in chunk if d["t"][0] >= last_end - 1.0 and d["jp"].strip()]
            if new:
                try:
                    annotate(new, self._gen_lang or "ja", translate=False)   # furigana/romaji NOW
                except Exception:
                    pass
                for d in new:
                    last_end = max(last_end, d["t"][1])
                self._gen_lines += new
                self.root.after(0, lambda t=token: self._apply_generated(t))
                # Translate OFF the capture loop: the network round-trip used to
                # block here (delaying the lyrics AND making the next capture miss
                # several seconds of audio). JP+romaji now show immediately; the
                # *** English fills in a moment later.
                threading.Thread(target=self._translate_generated,
                                 args=(token, list(new)), daemon=True).start()
            if self._cur_duration and pos >= self._cur_duration - CHUNK:
                break
        if token == self._gen_token:
            self._generating = False        # loop finished — clear the in-progress flag

    def _translate_generated(self, token, lines):
        """Fill the English (marked ***) for generated lines, off the capture loop.
        Runs once per chunk in its own thread; does NOT touch _generating (that's the
        capture loop's lifecycle, not the translation's)."""
        try:
            from fetch_lyrics import _translate_lines
            _translate_lines(lines, self._gen_lang or "ja")
        except Exception:
            return
        for d in lines:
            if d.get("en", "").strip() and not d["en"].rstrip().endswith("***"):
                d["en"] = d["en"].strip() + " ***"
        if token == self._gen_token:
            self.root.after(0, lambda t=token: self._apply_generated(t))

    def _apply_generated(self, token):
        """(Tk thread) sort/dedup the accumulated lines, show them, and save the
        generated file so a replay loads instantly and perfectly in sync."""
        if token != self._gen_token:
            return
        seen, merged = set(), []
        for d in sorted(self._gen_lines, key=lambda x: x["t"][0]):
            k = (round(d["t"][0], 1), d["jp"][:8])
            if k in seen:
                continue
            seen.add(k)
            merged.append(d)
        self.lines = [Line(start=d["t"][0], end=d["t"][1], jp=d.get("jp", ""),
                           rm=d.get("rm", ""), en=d.get("en", "")) for d in merged]
        self._relayout_song()
        try:
            from fetch_lyrics import slugify
            out = LYRICS_DIR / f"{slugify(self._gen_title)}.json"
            data = {"meta": {"title": self._gen_title, "artist": self._gen_artist,
                             "lang": self._gen_lang or "ja", "duration": self._cur_duration,
                             "source": "generated"},
                    "lines": [{"t": d["t"], "jp": d.get("jp", ""), "rm": d.get("rm", ""),
                               "en": d.get("en", "")} for d in merged]}
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
            self._lyrics_path = out
            self.index.add(out)
        except Exception as e:
            log.info("saving generated lyrics failed: %s", e)

    def load(self, path, keep_idx=False):
        self.meta, self.lines = load_lyrics(path)
        self._lyrics_path = Path(path)
        self._block_cache.clear()      # PERF-102: line idx → bitmap cache is per-song
        self._prewarm_token += 1       # cancel any prewarm still running for the old song
        if not (self.meta.get("source") or "").startswith("generated"):
            # REAL lyrics supersede ALL generation. Cancel BOTH the realtime
            # best-effort (_gen_token) AND the background deep transcription
            # (_deep_token), and drop any accumulated generated lines — otherwise a
            # late deep pass would overwrite the real lyrics, or two sets would show
            # at once ("multiple sets of lyrics" / "some ended up generated too").
            self._gen_token += 1
            self._deep_token += 1
            self._generating = False
            self._gen_lines = []
        self._mark_verified()
        self._relayout_song()           # size lanes/blocks to this song's rows
        # PERF-102: hold the whole song's bitmaps so a line renders at most ONCE
        # (repeats/choruses are free). NB: background prewarm was tried and reverted
        # — Pillow text holds the GIL, so a prewarm thread stalls the single Tk
        # scroll loop (LP-005). Each unique line still renders inline on first
        # appearance; the cost per render is what we minimise instead (cheap stroke).
        self._block_cache_max = max(32, min(72, len(self.lines) + 2))
        self.root.geometry(f"{self.W}x{self.H}+{self.work_left}+{self._geom_y()}")
        self.root.attributes("-topmost", True)
        if not keep_idx:
            self.idx = -1
            self._kara = []
            self._clear_stream()
            self.cv.delete("all")
        # Auto-enable MV mode when the LRC is much shorter than the video — the
        # video has an instrumental intro and/or outro the studio LRC doesn't
        # know about (Grimes "Genesis" video is 5:32; the song is ~4:20 with
        # ~70 s of intro). Without this signal, lyrics scroll ahead of the
        # actual singing until vocal-onset detection catches up. Skip for live
        # mode (whole-event title) and tracks already aligned by Shazam.
        try:
            st = self.media.get() or {}
            vdur = float(st.get("duration") or 0.0)
            lrc_end = self.lines[-1].end if self.lines else 0.0
            first_start = self.lines[0].start if self.lines else 0.0
            if (vdur > 0 and lrc_end > 0 and not self._live_mode
                    and vdur - lrc_end > 15.0 and first_start < 5.0
                    and self._sound_song is None):
                if not self._mv_mode:
                    log.info("LRC %.0fs < video %.0fs (1st line @%.1fs) → MV intro mode",
                             lrc_end, vdur, first_start)
                self._mv_mode = True
                self._intro_anchored = False
        except Exception:
            pass

    # ── monitor topology watchdog ──

    def _check_monitors(self, now):
        """Every ~3 seconds, re-enumerate monitors and re-apply the display
        setting if the topology changed (monitor plugged/unplugged, sleep/wake
        re-enumeration, resolution change).  Cheap: just compares fingerprints."""
        if now - getattr(self, "_last_mon_check", 0) < 3.0:
            return
        self._last_mon_check = now
        cur = tuple(m["fp"] for m in _monitors())
        if cur == self._mon_snapshot:
            return
        log.info("monitor topology changed: %s → %s; re-applying display=%s",
                 self._mon_snapshot, cur, self.display)
        try:
            self._apply_display()
        except Exception as e:
            log.info("display re-apply after topology change failed: %s", e)

    # ── main loop ──

    def _tick(self):
        # CRASH-PROOF render loop: the overlay's whole life is this self-
        # rescheduling loop, so ANY unhandled exception in a frame (a bad LRC, a
        # PIL edge case, a font glyph) used to kill it silently — the loop stopped
        # rescheduling while the OS media kept advancing, so the overlay FROZE on
        # the old song forever (the "stuck on the previous song / no lyrics" bug).
        # Wrap every frame: log the error and ALWAYS reschedule, so one bad frame
        # can never stop the loop.
        try:
            self._tick_body()
        except Exception as e:
            log.info("tick error (recovered): %s: %s", type(e).__name__, e)
            try:
                self.root.after(80, self._tick)
            except Exception:
                pass

    def _tick_body(self):
        # TICKET-088: CANONICAL same-tick offset-write ordering — every path that
        # writes self.offset within ONE _tick MUST run in this order, and the
        # combined writes for a single tick must stay ≤ 2 (one tier/deferred
        # commit + one pause-end re-base at most). Anything else is a race that
        # can produce a visible snap.
        #   1. deferred-commit consumes self._pending_offset       (queued earlier)
        #   2. fine-tune pause-end re-bases self.offset            (or re-queues)
        #   3. eased display offset is read for THIS frame's render
        # The race guard (`had_pending_pre`) below makes step 2 yield to step 1
        # when both fire on the same tick; step 2 then re-queues its delta into
        # _pending_offset (TICKET-088 item 4) rather than dropping it.
        # The _tick_offset_writes counter is bumped by _commit_offset; if more
        # than 2 writes land in one tick AND assert_same_tick is enabled, we
        # log a WARN so a regression is loud, not silent.
        self._tick_offset_writes = 0
        # Measure the interval between consecutive RENDER frames (only) for the
        # /status render_fps readout — paused/no-music frames use a slower cadence
        # and would skew it, so they're excluded.
        now = time.time()
        # A2 (workflow w821l9jnw): clear any prior tick's sub-branch timings, then
        # default raw dt to None so paused/no-music ticks (and rejected outliers)
        # render '-' in the perf log instead of a misleading value carried over.
        self._perf_branch_ms = {}
        self._raw_dt_ms = None
        if self._render_frame and self._last_tick_t is not None:
            dt = (now - self._last_tick_t) * 1000.0
            # A2: cap raised 500ms → 5000ms. The old 500ms drop was hiding the
            # 200-960ms stalls workflow w821l9jnw was hunting for. 5000ms still
            # rejects sleep/lid-close/debugger-pause outliers that would otherwise
            # drag the EWMA (published on /status) into nonsense for ~20 frames.
            if 0.0 < dt < 5000.0:
                self._raw_dt_ms = dt
                self._frame_ms = (dt if self._frame_ms <= 0
                                  else 0.9 * self._frame_ms + 0.1 * dt)
                # Stutter metrics: jitter = how far each frame strays from the
                # target interval (a smooth belt has near-zero jitter); worst =
                # the longest stall in the recent window. Both surface in /diag.
                jit = abs(dt - self._fps)
                self._frame_jitter = (jit if self._frame_jitter <= 0
                                      else 0.9 * self._frame_jitter + 0.1 * jit)
                self._frame_hist.append(round(dt, 1))
                if len(self._frame_hist) > 120:
                    del self._frame_hist[:len(self._frame_hist) - 120]
                self._frame_worst = max(self._frame_hist)
        self._last_tick_t = now
        self._render_frame = False

        # Record offset CHANGES (with a coarse source guess) so /diag can show
        # exactly when and how far the sync jumped — the key to diagnosing a
        # "massive desync" after the fact instead of having to catch it live.
        if self.offset != self._offset_hist_last:
            self._offset_hist.append((round(now, 1), round(self.offset, 2)))
            if len(self._offset_hist) > 40:
                del self._offset_hist[:len(self._offset_hist) - 40]
            self._offset_hist_last = self.offset

        self._consume_async()
        # TICKET-109: continuous decision engine tick. Self-throttled to
        # decision_tick_interval_s (default 2.0s) — cheap to invoke per tick.
        try:
            self._decision_engine_tick()
        except Exception as e:
            log.info("decision-engine tick: %s", e)
        # Watch for a concert applause/cheering pause (TICKET-061) — throttled, cheap.
        if now - getattr(self, "_applause_check_t", 0.0) > 0.3:
            self._applause_check_t = now
            try:
                self._check_applause_gap(now)
            except Exception:
                pass
        self._check_monitors(now)
        state = self.media.get()
        # TICKET-099: edge-detect SMTC PLAYING/not-PLAYING transitions for the
        # paused-Shazam takeover. Cheap (one bool compare + one timestamp);
        # idempotent every frame.
        self._update_smtc_pause_state(state)

        # ── TICKET-100: source-merge bookkeeping ────────────────────────────
        # Stamp wall-time the moment either authoritative source (SMTC or
        # Shazam-live) has spoken, so the Discord RP fallback can require a
        # CONTINUOUS silent gap before contributing. Cheap (one compare + one
        # timestamp). Runs every tick, BEFORE the silent-state early-return so
        # the timestamp is bumped on the same frame SMTC went silent — not on
        # the frame AFTER, which would shorten the gap by one tick.
        #
        # BUG-3 (v1.0.89): the old check was `_sound_song is not None`, but
        # _sound_song only clears on a NEW SMTC track. When SMTC goes silent
        # entirely (no published session), _sound_song retains its last value
        # forever, so this stamp re-fired every tick and silent_for stayed at
        # ~0 indefinitely → the Discord RP fallback could never activate.
        # Use _last_sound_lock_t (wall-time of the LAST Shazam lock) with a
        # 60s window so "Shazam ACTIVELY producing music" actually decays.
        now_src = time.time()
        sound_recent = (self._last_sound_lock_t
                        and (now_src - self._last_sound_lock_t) < 60.0)
        if (state and state.get("title")) or sound_recent:
            self._music_source_last_t = now_src

        if not state or not state["title"]:
            # ── TICKET-102: window-title fallback (Steam Overlay etc.) ──────
            # When SMTC is silent, peek at the WindowTitleWatcher's slot for
            # a music-bearing tab in an allowlisted CEF/Electron host. This
            # runs BEFORE the Discord RP probe because it's a LOCAL screen
            # signal (the user is literally on that tab) while RP is a 3rd-
            # party readout of someone else's Spotify session.
            #
            # Gating:
            #   * feature OFF (toggle or tune knob = 0)               → skip
            #   * candidate from LOW tier but generic-browser knob OFF → skip
            #   * candidate exe is itself an SMTC publisher whose ID is
            #     already in state['source'] (1-tick race during a tab
            #     switch) → suppress (belt-and-braces; the outer SMTC-first
            #     guard already covers this when SMTC has a title).
            win_state = None
            try:
                if (bool(self.window_titles_on)
                        or int(self._tune.get("window_titles_on", 0) or 0)):
                    poll_s = float(self._tune.get("window_titles_poll_s", 2.0))
                    generic_on = (
                        bool(self.window_titles_generic_browsers_on)
                        or int(self._tune.get(
                            "window_titles_generic_browsers", 0) or 0))
                    now_w = time.time()
                    try:
                        import window_titles as _wt
                    except Exception:
                        _wt = None
                    if _wt is not None:
                        # Ensure the watcher is up (covers the case where a
                        # /tune POST flipped the knob without going through
                        # set_window_titles).
                        try:
                            _wt.start_watcher(
                                poll_s=poll_s, generic_browsers=bool(generic_on))
                        except Exception:
                            pass
                        track = None
                        if (now_w - self._window_titles_last_t) >= poll_s:
                            self._window_titles_last_t = now_w
                            try:
                                track = _wt.get_current_track()
                            except Exception:
                                track = None
                            self._window_titles_last_track = track
                        else:
                            track = self._window_titles_last_track
                        if track and track.get("title"):
                            priority = (track.get("priority") or "high").lower()
                            # LOW tier (generic browsers) requires the user
                            # to have opted in. We already gated start_watcher
                            # on generic_on, but a stale slot from before the
                            # flip would still expose a LOW hit — drop it.
                            if priority == "low" and not generic_on:
                                track = None
                        if track and track.get("title"):
                            exe = (track.get("process") or "").lower()
                            win_state = {
                                "title": track["title"],
                                "artist": track.get("artist") or "",
                                "album": "",
                                "status": PLAYING,
                                "position": 0.0,
                                "duration": 0.0,
                                "rate": 1.0,
                                "source": track.get("source")
                                          or ("window-title:" + exe),
                                "ts": now_w,
                            }
            except Exception:
                win_state = None
            if win_state is not None:
                state = win_state
                # Mirror the Discord RP branch: bump source-last-spoken so
                # the next tick doesn't re-enter the fallback chain even
                # though SMTC is still silent. Side effect: the Discord RP
                # branch below sees silent_for=0 and skips on this tick.
                self._music_source_last_t = time.time()
            # ── TICKET-100: Discord Rich Presence fallback ──────────────────
            # When SMTC is silent (no session OR a session with no title), AND
            # Shazam-live has nothing, AND the toggle is on, AND the silent
            # gap has elapsed: try to pull the user's own Spotify Listening
            # activity from Discord and SYNTHESIZE a state dict that flows
            # through the rest of _tick exactly like an SMTC session would.
            # The synthesized dict carries source="discord-rpc:<sub>" so the
            # downstream code paths (clean_title source-aware bypass, diag,
            # boundary detector) can recognize it.
            #
            # Strict gating:
            #   - feature OFF (toggle or tune knob = 0)        → skip
            #   - silent gap < tune discord_rpc_silent_gap_s   → skip
            #   - poll throttle < discord_rpc_poll_s           → reuse cached
            #     last_track if recent enough (no IPC round-trip)
            # The IPC call itself has a hard 500 ms timeout — see discord_rpc.
            disc_state = None
            try:
                # Either control surface enables the feature (tray flag OR tune
                # knob). set_discord_rpc keeps them in sync, but a /tune POST
                # that flips the knob alone must still take effect.
                if (bool(self.discord_rpc_on)
                        or int(self._tune.get("discord_rpc_on", 0) or 0)):
                    gap_s = float(self._tune.get("discord_rpc_silent_gap_s", 8.0))
                    poll_s = float(self._tune.get("discord_rpc_poll_s", 5.0))
                    timeout_s = float(self._tune.get("discord_rpc_timeout_s", 0.5))
                    now_w = time.time()
                    silent_for = now_w - getattr(self, "_music_source_last_t", now_w)
                    if silent_for >= gap_s:
                        # Lazy import (zero cold-start cost when feature is off).
                        # Module-level state lives in discord_rpc.py.
                        try:
                            import discord_rpc as _drpc
                        except Exception:
                            _drpc = None
                        if _drpc is not None:
                            # BUG-2/5/6 (v1.0.89): get_listening_track() is now
                            # a non-blocking slot read served by the long-lived
                            # watcher daemon. The Tk thread NEVER spawns a
                            # worker or .join()s the pipe. If the toggle was
                            # flipped on via /tune alone (bypassing
                            # set_discord_rpc), make sure the watcher is up.
                            try:
                                _drpc.start_watcher(poll_s=poll_s)
                            except Exception:
                                pass
                            track = None
                            if (now_w - self._discord_last_poll_t) >= poll_s:
                                self._discord_last_poll_t = now_w
                                try:
                                    # timeout_s kept for API compat; ignored.
                                    track = _drpc.get_listening_track(timeout_s=timeout_s)
                                except Exception:
                                    track = None
                                self._discord_last_track = track
                            else:
                                # Throttle window: reuse the last track for
                                # continuity, so a quiet 5-second window
                                # between polls doesn't strobe the overlay.
                                track = self._discord_last_track
                            if track and track.get("title") and track.get("artist"):
                                sub = (track.get("source") or "other").lower()
                                # SMTC-shaped synthetic state. position/duration
                                # are 0 (Discord RP carries no real clock), and
                                # status=PLAYING so the rest of _tick treats it
                                # as a live source rather than a paused tab.
                                disc_state = {
                                    "title": track["title"],
                                    "artist": track["artist"],
                                    "album": "",
                                    "status": PLAYING,
                                    "position": 0.0,
                                    "duration": 0.0,
                                    "rate": 1.0,
                                    "source": "discord-rpc:" + sub,
                                    "ts": now_w,
                                }
            except Exception:
                disc_state = None
            if disc_state is not None:
                # Hand the synthesized state to the rest of the tick. Bump the
                # source-last-spoken timestamp so we don't re-enter this branch
                # on the next frame and re-probe Discord.
                state = disc_state
                self._music_source_last_t = time.time()
            # TICKET-102: a successful window-title synth already populated
            # `state` above; treat it the same as the Discord-RP synth so we
            # don't fall into the "waiting for music" no-source branch.
            if not state or not state.get("title"):
                if self._track is not None:
                    self._track = None
                    self._hint("Waiting for music…")
                self.root.after(120, self._tick)
                return

        # Scrolling Instagram/TikTok/etc. Reels: the tab reports the SITE NAME as
        # the title (no artist). That's not a song — keep the overlay OFF so it
        # doesn't match a same-named song and slap lyrics over the clip.
        if is_non_music_source(state["title"], state.get("artist", "")):
            if self._track is not None or self.lines:
                self._track = None
                self.lines, self.idx, self._kara = [], -1, []
                self._lyrics_path = None
                self.cv.delete("all")
                self._clear_stream()
                self._hint("")        # nothing — don't cover the reel
            self.root.after(200, self._tick)
            return

        # clean_title() runs several regexes; the raw title rarely changes, so
        # only recompute it when it does (this loop runs every frame).
        rawt, src, rawa = state["title"], state["source"], state.get("artist", "")
        if (rawt != self._last_raw_title or src != self._last_src
                or rawa != self._last_artist):
            self._last_raw_title, self._last_src, self._last_artist = rawt, src, rawa
            self._clean_title_cache = clean_title(rawt, src, rawa)
            # TICKET-086: source-aware bypass — YouTube Music delivers a clean
            # SMTC artist field already (e.g. '轟はじめ', not 'Hajime Ch. 轟はじめ
            # ‐ ReGLOSS'), so the channel-stripping rules tuned for the regular
            # YouTube tab can over-strip on YT Music. Trust the source there.
            self._clean_artist_cache = clean_artist(rawa, src)
            # Only EXPLICIT covers (歌ってみた / "covered by" / [COVER]) take
            # the loose title-first path. Routing VTuber-channel uploads through
            # it too was WRONG: for a generic title like "Lucky Star" the
            # title-only search grabbed a same-titled DIFFERENT song.
            self._is_cover = is_cover_title(rawt)
            self._cover_signal = cover_signal(rawt, rawa)
            # TICKET-086: a non-empty YouTube Music album field is strong
            # evidence of an OFFICIAL original (covers / UGC don't carry an
            # album). Demote the WEAKER ampersand-collab signal in that case;
            # never override an explicit cover tag (those are unambiguous).
            album = (state.get("album") or "").strip()
            yt_music_src = "music.youtube" in (src or "").lower()
            if (self._cover_signal == "amp_collab" and album and yt_music_src
                    and float(self._tune.get("cover_amp_album_demote", 1.0)) >= 0.5):
                log.info("cover: amp-collab demoted — YT Music album %r = official", album)
                self._cover_signal = None
                self._is_cover = False
            if self._is_cover:
                # Match the cover channel with the RAW artist, not clean_artist — the
                # cleaner strips the personal name ("Ouro Kronii Ch. hololive-EN" →
                # "hololive-EN"), which would make "… - Ouro Kronii" look like the
                # ORIGINAL artist and re-introduce the wrong-artist search.
                orig_a, song = extract_cover_original(rawt, rawa)
                self._cover_original_artist = orig_a
                if self._cover_signal == "amp_collab":
                    log.info("cover: amp-collab signal → title-only search "
                             "(ignoring right-hand A & B as original artist)")
                # If the cover parse found the bare song name ("Coffee" out of
                # "[COVER] Coffee - A!ka | Kaneko Lumi"), use it — clean_title's
                # generic rules leave the "- Artist | Channel" tail on, which
                # makes the lyric search messier than it needs to be.
                if song and len(song) >= 2 and len(song) < len(self._clean_title_cache):
                    self._clean_title_cache = song
            else:
                self._cover_original_artist = None
        track = (self._clean_artist_cache, self._clean_title_cache)
        if track != self._track:
            self._track = track
            self._on_track_change(track, self._trusted_duration(state))

        self.character.set_playing(state["status"] == PLAYING)   # dance when playing

        if state["status"] != PLAYING or not self.lines:
            self.root.after(80, self._tick)   # frozen while paused — no advancing
            return

        # MV / cinematic dead-space: for an MV-titled, not-yet-aligned song, hold
        # the lyrics through the leading intro so they don't run ahead of the song.
        # THREE release paths, fastest wins: (1) the live vocal-energy poll below
        # (_vocals_active_now — the reliable primary), (2) the one-shot
        # _on_vocal_onset / _on_song_onset events (band-energy rise / music after a
        # quiet stretch), (3) Shazam aligning (sets _sound_song). The tunable
        # `mv_intro_timeout` (default 75 s) is a last-ditch backstop for a very long
        # or oddly-mastered intro where none fire — Grimes "Genesis" has a ~70 s intro.
        if (self._mv_mode and not self._intro_anchored
                and self._sound_song is None):
            # RELEASE the moment singing actually starts. The one-shot vocal-onset
            # EVENT (_fire_vocal_event) can silently fail to fire, which left the
            # lyrics stuck on the intro card for the WHOLE song (the "lyrics never
            # started" bug). Poll the always-on vocal-band buffer here too — a
            # robust second path reusing the live energy the sync already tracks.
            if self._vocals_active_now():
                self._on_vocal_onset()           # calibrate the offset if applicable
                self._intro_anchored = True      # vocals are here → stop holding
            mv_to = float(self._tune.get("mv_intro_timeout", 75.0))
            if not self._intro_anchored:
                if state["position"] > mv_to or (time.time() - self._track_t0) > mv_to:
                    self._intro_anchored = True   # backstop: very long / oddly-mastered intro
                else:
                    # MUSIC VIDEO ⇒ "Cinematic" (visual dead-space, often dialogue);
                    # a plain audio instrumental lead-in keeps the "Instrumental" wording.
                    self._hint("🎬 Cinematic intro — waiting for vocals…")
                    self.idx = -1
                    self.root.after(90, self._tick)
                    return

        # TICKET-078: a deferred sync correction commits at the next line
        # boundary so the current (possibly mis-synced) line finishes naturally
        # before the next line is picked under the new offset. Capped at 8s in
        # case idx gets stuck so a pending correction can't strand forever.
        # SAME-TICK RACE GUARD: snapshot whether a deferred commit was pending
        # BEFORE we consume it, so the fine-tune pause-end below can tell the
        # difference between "no commit was ever queued" (safe to apply pause
        # subtraction) and "commit just fired this tick" (subtraction would
        # corrupt the freshly-set offset).
        had_pending_pre = self._pending_offset is not None
        if self._pending_offset is not None and self.lines:
            cur_pos = state["position"] + self.offset
            cur_end = self.lines[self.idx].end if self.idx >= 0 else 0.0
            if (self.idx < 0 or cur_pos >= cur_end
                    or (time.time() - self._pending_offset_t) > 8.0):
                new_off = self._pending_offset
                self._pending_offset = None
                log.info("sync: deferred commit %+.2fs → %+.2fs (line ended)",
                         self.offset, new_off)
                # TICKET-088: route through atomic _commit_offset helper. Use
                # reset_display=False so the eased display offset KEEPS gliding
                # toward the new target (matches pre-088 behavior) — the snap
                # fixes are in _eased_offset's per-frame fraction cap.
                self._commit_offset(new_off, reset_display=False)
                self.idx = -1
                self._drift_integral = 0.0
        # FINE-TUNE pause EXPIRY (placed ABOVE the pos computation so the offset
        # adjustment is reflected in THIS frame's pos / pos_raw — zero visible
        # discontinuity: the held frame becomes the resumed frame).
        # RACE GUARD: if a tier commit queued a `_pending_offset` (consumed THIS
        # tick OR still pending), that commit's offset is authoritative;
        # subtracting our pause delta on top would corrupt it. Just drop the
        # pause buffers and let the deferred commit drive the next frames.
        if self._fine_pause_until and time.time() >= self._fine_pause_until:
            amt = self._fine_pause_amount
            if not had_pending_pre and self._pending_offset is None:
                new_off = round(self.offset - amt, 2)
                # also slide the eased display offset in lockstep so _eased_offset
                # doesn't see a spurious target jump and re-ramp.
                cur_disp = getattr(self, "_display_offset", None)
                if cur_disp is not None:
                    self._display_offset = cur_disp - amt
                    self._display_offset_t = time.time()
                log.info("fine-tune pause end: applied %+.2fs (offset %+.2fs → %+.2fs)",
                         -amt, self.offset, new_off)
                # TICKET-088: route through atomic _commit_offset (reset_display
                # =False because we just slid display_offset in lockstep above).
                self._commit_offset(new_off, reset_display=False)
            else:
                # TICKET-088: instead of DROPPING the pause delta (the old code
                # silently lost the fine-tune correction whenever a tier commit
                # raced it), FOLD it into the pending offset so the deferred
                # commit picks it up at the next line boundary. Combines both
                # corrections in a single atomic write instead of either-or.
                existing = self._pending_offset if self._pending_offset is not None else self.offset
                merged = round(existing - amt, 2)
                self._pending_offset = merged
                self._pending_offset_t = time.time()
                log.info("fine-tune pause end: tier commit pending → re-queued pause delta "
                         "%+.2fs into pending offset (was %s, now %+.2fs)",
                         -amt,
                         "None" if not had_pending_pre else f"{existing:+.2f}",
                         merged)
            self._fine_pause_until = 0.0
            self._fine_pause_pos_eased = None
            self._fine_pause_pos_raw = None
            self._fine_pause_amount = 0.0
        # TICKET-088: same-tick offset-write assertion. _commit_offset bumps
        # _tick_offset_writes; the canonical max within one tick is 2
        # (deferred-commit + pause-end). More than that is a race regression.
        # Gated by `assert_same_tick` so it stays silent in production.
        try:
            if (int(self._tune.get("assert_same_tick", 0))
                    and self._tick_offset_writes > 2):
                log.info("WARN: _tick saw %d offset writes (expected ≤2). "
                         "Canonical order: deferred-commit then pause-end.",
                         self._tick_offset_writes)
        except Exception:
            pass
        # Render against an EASED display offset, not the raw sync offset, so a
        # sound-sync correction GLIDES the highlight/scroll into place instead of
        # snapping (the "jumpy karaoke fill"). See _eased_offset.
        # TICKET-082: split into TWO timebases —
        #   pos      = eased  → drives line POSITION on the belt + line-index
        #              selection (smooth visual transitions).
        #   pos_raw  = raw    → drives the karaoke FILL fraction so the sung-vs-
        #              unsung highlight tracks the ACTUAL song clock, not the
        #              easing ramp. Decoupling the two stops the "fill races
        #              ahead then snaps back" stutter the user kept seeing.
        pos = state["position"] + self._eased_offset()
        pos_raw = state["position"] + self.offset
        # FINE-TUNE pause OVERRIDE: if a pause is in flight, FREEZE both pos and
        # pos_raw to the values captured at pause entry. Scroll belt, line idx,
        # and karaoke fill all key off these — they freeze together with no work
        # done to them individually. The held frame remains on screen for the
        # pause duration; pause-end (above) then re-bases self.offset so the
        # resumed frame equals the held frame.
        branch_tag = "line"
        if self._fine_pause_until and time.time() < self._fine_pause_until:
            if self._fine_pause_pos_eased is not None:
                pos = self._fine_pause_pos_eased
            if self._fine_pause_pos_raw is not None:
                pos_raw = self._fine_pause_pos_raw
            branch_tag = "fine-pause"

        if self.scroll_dir in ("lr", "rl"):       # continuous horizontal scroll-through
            self._ticker_update(pos, pos_raw)
            self._render_frame = True
            self._perf_record(state, pos, pos_raw,
                              branch_tag if branch_tag == "fine-pause" else "scroll-h")
            self.root.after(self._fps, self._tick)
            return
        if self.scroll_dir in ("tb", "bt"):       # continuous vertical scroll-through
            self._ticker_update_v(pos, pos_raw)
            self._render_frame = True
            self._perf_record(state, pos, pos_raw,
                              branch_tag if branch_tag == "fine-pause" else "scroll-v")
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
            self._karaoke(pos_raw)

        self._render_frame = True
        self._perf_record(state, pos, pos_raw, branch_tag)
        self.root.after(self._fps, self._tick)

    # ── drawing ──

    def _render(self, ln):
        # A2: bracket the whole _render() body. Even on cancellation paths the
        # context-manager `finally` records the elapsed time, so a partial
        # render still shows up in the perf log (documented "last completed
        # call within this tick" semantics).
        with self._perf_branch("render"):
            self._cancel_anim()
            self.cv.delete("all")
            self._kara = []   # per-line "tracks" (index-based karaoke fill)
            pad = self.pad
            max_w = self.W - 2 * pad
            cur_y = 0.0

            # ── main line (furigana over kanji); wrap at segment boundaries ──
            if ln.jp:
                jpf = self._main_tk_font(ln)   # script-aware: Hangul→Malgun, etc.
                jp_h, furi_h = self._text_h(jpf), self._text_h(self.FURI_FONT)
                line_h = jp_h + furi_h + 10
                chars = []
                cur_y += furi_h + jp_h / 2 + 6
                cx = pad
                for base, reading in split_furigana(ln.jp):
                    seg_w = max(measure_text(self.cv, base, jpf),
                                measure_text(self.cv, reading, self.FURI_FONT) if reading else 0)
                    if cx + seg_w > pad + max_w and cx > pad:      # wrap underneath
                        cx, cur_y = pad, cur_y + line_h
                    seg_start = cx
                    for ch in base:
                        w = measure_text(self.cv, ch, jpf)
                        if w <= 0:
                            continue
                        fid = draw_text(self.cv, cx + w / 2, cur_y, ch, jpf, WHITE)
                        chars.append({"fill": fid, "last": WHITE})
                        cx += w
                    if reading:
                        draw_text(self.cv, (seg_start + cx) / 2,
                                  cur_y - jp_h / 2 - furi_h / 2 - 2,
                                  reading, self.FURI_FONT, FURI_C)
                    cx += 6
                self._kara.append({"chars": chars, "base": WHITE, "sung": SUNG})
                cur_y += jp_h / 2 + 14

            if ln.rm:
                chars, cur_y = self._wrap_row(ln.rm, cur_y, self.ROMAJI_FONT, ROMAJI_C, pad, max_w)
                self._kara.append({"chars": chars, "base": ROMAJI_C, "sung": SUNG})
            if ln.en:
                chars, cur_y = self._wrap_row(ln.en, cur_y, self.EN_FONT, EN_C, pad, max_w)
                self._kara.append({"chars": chars, "base": EN_C, "sung": SUNG})

            # Anchor the whole block within the FIXED window. VERTICAL: top edge,
            # bottom band, or centred (center/left/right all sit vertically centred).
            if self.pos_y == "top":
                dy = self._win_margin
            elif self.pos_y == "center":
                dy = max(self._win_margin, round((self.work_h - cur_y) / 2))
            else:   # bottom
                dy = max(self._win_margin, self.work_h - cur_y - self._bottom_clear)
            # HORIZONTAL: the block is laid out from the left margin; PIN it to the
            # right edge or centre it per pos_x (independent of the vertical anchor).
            bb = self.cv.bbox("cur")
            dx = 0
            if bb:
                if self.pos_x == "right":
                    dx = (self.W - pad) - bb[2]
                elif self.pos_x == "center":
                    dx = round((self.W - (bb[2] - bb[0])) / 2) - bb[0]
            self.cv.move("cur", dx, dy)
            self._animate_in()
            if self._mirrors:
                self._update_mirrors(ln)
            if self.display == "cycle" and len(_monitors()) > 1:
                self._cycle_idx = (self._cycle_idx + 1) % len(_monitors())
                try:
                    self._apply_display()
                except Exception:
                    pass

    def _wrap_row(self, text, y, font, color, pad, max_w):
        """Draw a text row, wrapping overflow onto lines underneath.
        Returns (chars, next_y)."""
        h = self._text_h(font)
        line_h = h + 12
        sp = measure_text(self.cv, " ", font) or h * 0.3
        cy, cx, chars = y + h / 2 + 6, pad, []
        latin = not _has_cjk(text)
        for unit in (text.split(" ") if latin else list(text)):
            uw = measure_text(self.cv, unit, font)
            if cx + uw > pad + max_w and cx > pad:
                cx, cy = pad, cy + line_h
            for ch in unit:
                w = measure_text(self.cv, ch, font)
                if w <= 0:
                    continue
                fid = draw_text(self.cv, cx + w / 2, cy, ch, font, color)
                chars.append({"fill": fid, "last": color})
                cx += w
            if latin:
                cx += sp
        return chars, cy + h / 2 + 14

    def _text_h(self, font):
        tid = self.cv.create_text(-9999, -9999, text="Aあ", font=font, anchor="nw")
        bb = self.cv.bbox(tid)
        self.cv.delete(tid)
        return (bb[3] - bb[1]) if bb else 28

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
        if d in ("lr", "rl", "tb", "bt", "none", "off", "stationary"):
            return                               # scroll handled by the ticker
        # Per-line slide-in: horizontal for left/right, vertical for top/bottom.
        # The line is offset BEFORE paint, then _anim_step interpolates it back
        # to the anchored position with an ease-out cubic.
        ox, oy = 0, 0
        if d == "right":
            ox = 460                             # start off-screen RIGHT, slide LEFT
        elif d == "left":
            ox = -460                            # start off-screen LEFT, slide RIGHT
        elif d == "top":
            # Slide DOWN from above. Use ~one block height for a snappy drop;
            # _block_h is set by _relayout_song, with a sane fallback if missing.
            oy = -max(80, round(getattr(self, "_block_h", 120) * 0.9))
        elif d == "bottom":
            oy = max(80, round(getattr(self, "_block_h", 120) * 0.9))   # slide UP from below
        else:
            return                               # unknown mode: no entrance animation
        self.cv.move("cur", ox, oy)
        self._anim_step(ox, oy, 0)

    # ── image-based scroll blocks (fast: scroll one bitmap, not 100+ items) ──

    def _pil_font(self, kind, size):
        key = (kind, size)
        f = self._pil_fonts.get(key)
        if f is None:
            for name in _PIL_FONTS[kind]:
                try:
                    f = ImageFont.truetype(name, size)
                    break
                except Exception:
                    continue
            f = f or ImageFont.load_default()
            self._pil_fonts[key] = f
        return f

    def _img_row(self, text, font, x0):
        """[(char, left_x), …] and the row's right edge, for a plain row."""
        chars, cx = [], x0
        latin = not _has_cjk(text)
        sp = font.getlength(" ") or font.size * 0.3
        for ch in text:
            if ch == " ":
                cx += sp
                continue
            chars.append((ch, cx))
            cx += font.getlength(ch)
        return chars, cx

    def _block_spec(self, i):
        """Lay out line i for image rendering (positions only, no drawing)."""
        ln = self.lines[i]
        s = self.font_scale * self._auto_scale
        fj = self._pil_font(self._main_pil_kind(ln), max(10, round(38 * s)))
        ff = self._pil_font("furi", max(7, round(17 * s)))
        fr = self._pil_font("rm", max(8, round(23 * s)))
        fe = self._pil_font("en", max(8, round(21 * s)))
        x0, right, rows, furi = 8, 8, [], []
        if ln.jp:
            chars, cx = [], x0
            for base, reading in split_furigana(ln.jp):
                seg = cx
                for ch in base:
                    chars.append((ch, cx))
                    cx += fj.getlength(ch)
                if reading:
                    furi.append((reading, (seg + cx) / 2))
                cx += 6 * s
            rows.append({"chars": chars, "y": self.b_main, "font": fj, "base": WHITE})
            right = max(right, cx)
        if ln.rm:
            # if a romaji/translation row contains CJK (a mixed-language line),
            # use a CJK-capable font so it doesn't render as □ boxes
            frow = (self._pil_font(_script_of(ln.rm, self.meta.get("lang")),
                                   max(8, round(23 * s))) if _has_cjk(ln.rm) else fr)
            rc, rx = self._img_row(ln.rm, frow, x0)
            rows.append({"chars": rc, "y": self.b_rom, "font": frow, "base": ROMAJI_C})
            right = max(right, rx)
        if ln.en:
            erow = (self._pil_font(_script_of(ln.en, self.meta.get("lang")),
                                   max(8, round(21 * s))) if _has_cjk(ln.en) else fe)
            ec, ex = self._img_row(ln.en, erow, x0)
            rows.append({"chars": ec, "y": self.b_en, "font": erow, "base": EN_C})
            right = max(right, ex)
        return {"rows": rows, "furi": furi, "furi_font": ff, "furi_y": self.b_furi,
                "w": int(right) + 8, "h": self._block_h}

    def _paint_block_img(self, spec, frac):
        """Render the block to a PIL image with the sung portion highlighted.
        Used by the non-scroll render path. Returns the PIL image (not a
        PhotoImage). The scroll path uses the cheaper layer composite below."""
        img = Image.new("RGBA", (max(1, spec["w"]), max(1, spec["h"])), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        sw = self._stroke_w()
        for text, cx in spec["furi"]:
            d.text((cx, spec["furi_y"]), text, font=spec["furi_font"], fill=FURI_C,
                   anchor="mm", stroke_width=1, stroke_fill=INK)
        for row in spec["rows"]:
            n = int(frac * len(row["chars"]) + 0.5)
            for idx, (ch, cx) in enumerate(row["chars"]):
                col = SUNG if idx < n else row["base"]
                d.text((cx, row["y"]), ch, font=row["font"], fill=col, anchor="lm",
                       stroke_width=sw, stroke_fill=INK)
        return img

    def _atlas_tile(self, text, font, color, sw, anchor):
        """A cached, outlined glyph (or short reading) tile + its (left, top) offset
        from the anchor point. Rendered ONCE per (text, font, colour, stroke, anchor)
        and reused everywhere — pasting these is ~8× faster than re-rasterising each
        stroked glyph per line (the per-line scroll spike). Pixel-equivalent to the
        old `d.text(..., anchor=anchor, stroke_width=sw)` since it uses the same
        anchor-relative bbox."""
        key = (text, getattr(font, "path", None) or id(font),
               getattr(font, "size", 0), color, sw, anchor)
        g = self._glyph_cache.get(key)
        if g is None:
            try:
                l, t, r, b = font.getbbox(text, stroke_width=sw, anchor=anchor)
            except Exception:
                l, t, r, b = font.getbbox(text, stroke_width=sw)
            tile = Image.new("RGBA", (max(1, r - l), max(1, b - t)), (0, 0, 0, 0))
            try:
                ImageDraw.Draw(tile).text((-l, -t), text, font=font, fill=color,
                                          anchor=anchor, stroke_width=sw, stroke_fill=INK)
            except Exception:
                ImageDraw.Draw(tile).text((-l, -t), text, font=font, fill=color,
                                          stroke_width=sw, stroke_fill=INK)
            g = (tile, l, t)
            self._glyph_cache[key] = g
            if len(self._glyph_cache) > self._glyph_cache_max:
                self._glyph_cache.pop(next(iter(self._glyph_cache)))   # LRU evict
        return g

    def _paint_one_layer(self, spec, color):
        """Compose the block from cached glyph tiles (GLYPH ATLAS, PERF-102): each
        glyph is rasterised once and PASTED, instead of re-rasterising ~180 stroked
        glyphs every render. `color=None` ⇒ each row's own base colour. One layer of
        the karaoke composite."""
        w, h = max(1, spec["w"]), max(1, spec["h"])
        sw = self._stroke_w()
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        fy = spec["furi_y"]
        for text, cx in spec["furi"]:
            tile, l, t = self._atlas_tile(text, spec["furi_font"], FURI_C, 1, "mm")
            img.alpha_composite(tile, (max(0, round(cx + l)), max(0, round(fy + t))))
        for row in spec["rows"]:
            col = color if color is not None else row["base"]
            font, ry = row["font"], row["y"]
            for ch, cx in row["chars"]:
                tile, l, t = self._atlas_tile(ch, font, col, sw, "lm")
                img.alpha_composite(tile, (max(0, round(cx + l)), max(0, round(ry + t))))
        return img

    def _sung_layer(self, b):
        """Lazily render (and cache) the fully-sung layer for a block — only when
        the line actually starts singing. Rendering base+sung TOGETHER at spawn
        doubled the spawn cost into a visible 100-150 ms hitch; deferring the
        sung layer to first-fill spreads the two renders so they never coincide
        (and a block that scrolls by without ever being the current line never
        pays for its sung layer at all)."""
        if b.get("sung") is None:
            sung = self._paint_one_layer(b["spec"], SUNG)   # render outside the lock
            b["sung"] = sung
            with self._cache_lock:
                ce = self._block_cache.get(b["idx"])         # cache so a repeated line reuses it
                if ce is not None and ce.get("sig") == self._block_sig():
                    ce["sung"] = sung
        return b["sung"]

    def _composite_fill(self, base, sung, spec, frac):
        """Cheap karaoke fill: build a mask that's opaque up to each row's sung
        boundary, then composite the sung layer over the base. No glyph render."""
        w, h = base.size
        mask = Image.new("L", (w, h), 0)
        md = ImageDraw.Draw(mask)
        sw = self._stroke_w()
        for row in spec["rows"]:
            chars = row["chars"]
            if not chars:
                continue
            n = int(frac * len(chars) + 0.5)
            if n <= 0:
                continue
            # Reveal the sung layer up to the boundary char + a hair past it so the
            # boundary glyph's left stroke isn't sliced.
            x_sung = w if n >= len(chars) else int(chars[n][1]) + sw
            # FULL-GLYPH vertical band: glyphs are drawn anchor="lm" (centred at
            # row["y"]), so cover ±(half text height) PLUS the stroke and a margin —
            # the old y+0.4·fs cut off descenders (g, y, p) and the lower outline,
            # leaving the bottom of letters un-highlighted.
            try:
                asc, desc = row["font"].getmetrics()
                half = (asc + desc) / 2.0 + sw + 3
            except Exception:
                half = getattr(row["font"], "size", 30) * 0.9
            md.rectangle([0, int(row["y"] - half), x_sung, int(row["y"] + half)],
                         fill=255)
        out = base.copy()
        out.paste(sung, (0, 0), mask)
        return out

    def _advance_fill(self, b, frac):
        """Karaoke fill without the whole-block recomposite. We keep a persistent
        composited surface `b["composited"]` and paste ONLY the newly-sung per-row
        strip into it (PIL Image.paste supports a box+mask — cheap), then do ONE
        upload to the PhotoImage. This drops the costly part of the old fill —
        `base.copy()` + a full-size mask + a full paste, ~85 ms for a long
        1.5×-scale block — leaving just the single blit. The fill grows
        monotonically within a line; a seek-back (frac < last) rebuilds in full so
        the highlight can recede correctly."""
        comp, spec, photo = b["composited"], b["spec"], b["photo"]
        w, h = comp.size
        old_frac = b.get("fill_frac", 0.0)
        if frac < old_frac - 1e-6:                        # seek-back → full rebuild
            comp = self._composite_fill(b["base"], self._sung_layer(b), spec, frac)
            b["composited"] = comp
            photo.paste(comp)
            b["fill_frac"] = frac
            return
        sung = self._sung_layer(b)
        sw = self._stroke_w()
        changed = False
        for row in spec["rows"]:
            chars = row["chars"]
            if not chars:
                continue
            on = int(old_frac * len(chars) + 0.5)
            nn = int(frac * len(chars) + 0.5)
            if nn <= on:
                continue                                  # this row gained no chars
            x0 = max(0, (w if on >= len(chars) else int(chars[on][1])) - sw)
            x1 = w if nn >= len(chars) else int(chars[nn][1]) + sw
            if x1 <= x0:
                continue
            try:
                asc, desc = row["font"].getmetrics()
                half = (asc + desc) / 2.0 + sw + 3
            except Exception:
                half = getattr(row["font"], "size", 30) * 0.9
            y0, y1 = max(0, int(row["y"] - half)), min(h, int(row["y"] + half))
            crop = sung.crop((x0, y0, x1, y1))
            comp.paste(crop, (x0, y0), crop)              # sung glyphs over base, in-strip
            changed = True
        if changed:
            photo.paste(comp)                             # single full blit
        b["fill_frac"] = frac

    def _block_sig(self):
        """Layout signature: anything that changes a line's rendered bitmap. A
        font/scale change flips this so cached blocks are re-rendered; line TEXT is
        fixed within a song (the cache is cleared on song load), so idx is enough."""
        return (round(self.font_scale, 3), round(self._auto_scale, 3), self._block_h)

    def _stroke_w(self):
        """Outline width for image-block glyphs. CAPPED at 2: Pillow's per-glyph
        stroke is the dominant block-render cost (python-pillow #6618), and a 3 px
        stroke at font_scale 1.5 nearly doubled the first-appearance render for
        little readability gain over 2 px. Must be identical at every call site so
        the sliver fill's x-offsets line up with the rendered glyphs."""
        return max(1, min(2, round(1.6 * self.font_scale * self._auto_scale)))

    def _render_img_block(self, i, frac, place=None):
        self._blk_seq += 1
        tag = f"blk{self._blk_seq}"
        # Reuse the cached layout + base bitmap when this line has been rendered
        # before at the current scale — the costly _block_spec measure pass and
        # _paint_one_layer glyph render are skipped entirely (PERF-102).
        sig = self._block_sig()
        with self._cache_lock:
            ce = self._block_cache.get(i)
            hit = ce is not None and ce["sig"] == sig
            if hit:
                self._block_cache[i] = self._block_cache.pop(i)        # mark recently used
        if not hit:
            # MISS: the background prewarm hasn't reached this line yet → render it
            # inline. The render runs OUTSIDE the lock so prewarm and the scroll
            # thread never block each other on the ~150 ms glyph pass.
            spec = self._block_spec(i)
            base = self._paint_one_layer(spec, None)     # base only; sung is lazy
            ce = {"sig": sig, "spec": spec, "base": base, "w": spec["w"], "sung": None,
                  "nchars": max((len(r["chars"]) for r in spec["rows"]), default=0)}
            with self._cache_lock:
                self._block_cache[i] = ce
                while len(self._block_cache) > self._block_cache_max:
                    self._block_cache.pop(next(iter(self._block_cache)))   # evict oldest (LRU)
        b = {"idx": i, "tag": tag, "x": 0.0, "w": ce["w"], "img": True,
             "spec": ce["spec"], "photo": None, "sung_n": -1,
             "base": ce["base"], "sung": ce["sung"], "nchars": ce["nchars"],
             "fill_frac": max(0.0, frac)}   # baseline for the sliver fill (_advance_fill)
        # only build the sung layer + composite if it's already singing at spawn
        img = ce["base"] if frac <= 0 else self._composite_fill(
            ce["base"], self._sung_layer(b), ce["spec"], frac)
        # persistent surface the sliver fill mutates — a COPY so the shared cached
        # base is never written to (frac>0 already returns a fresh composite).
        b["composited"] = ce["base"].copy() if frac <= 0 else img
        b["photo"] = photo = ImageTk.PhotoImage(img)
        if place is None:                                  # default: horizontal lane
            place = (0, self._lane_y0 + (i % self._lanes) * self._lane_gap)
        b["x"] = place[0]
        self.cv.create_image(place[0], place[1], image=photo, anchor="nw", tags=(tag, "strm"))
        return b

    # ── continuous scroll-through ticker (multiple lines on screen) ──

    def _render_block(self, i):
        """Draw line i's compact block at x-origin 0, offset into its vertical
        lane so staggered lines don't overlap. Returns the block."""
        ln = self.lines[i]
        self._blk_seq += 1
        tag = f"blk{self._blk_seq}"
        dy = self._lane_y0 + (i % self._lanes) * self._lane_gap
        jpf = self._main_tk_font(ln)
        tracks, right = [], 0
        if ln.jp:
            chars, cx = [], 0
            for base, reading in split_furigana(ln.jp):
                seg = cx
                for ch in base:
                    w = measure_text(self.cv, ch, jpf)
                    if w <= 0:
                        continue
                    fid = draw_text(self.cv, cx + w / 2, self.b_main + dy, ch,
                                    jpf, WHITE, tags=(tag, "strm"))
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

    def _compute_scroll_floor(self):
        """Pick the minimum scroll speed that keeps THIS song's lines from
        overlapping. Two blocks in the same lane sit a constant distance apart
        ( = speed × Δtimestamp ), and same-lane lines are `lanes` apart, so a
        dense/fast song needs a faster belt to space them out. Slow songs keep
        the user's comfortable pace; only crowded ones speed up, just enough.
        Computed once per song (and on font/lane change) — the ticker itself
        stays a single-move loop."""
        self._v_floor = 0.0
        lines = getattr(self, "lines", None) or []
        n, L = len(lines), max(1, self._lanes)
        if n <= L:
            return
        s = self.font_scale * self._auto_scale
        fj = self._pil_font("jp", max(10, round(38 * s)))
        fr = self._pil_font("rm", max(8, round(23 * s)))
        fe = self._pil_font("en", max(8, round(21 * s)))

        def width(ln):
            w = 0.0
            if ln.jp:
                base = "".join(b for b, _ in split_furigana(ln.jp))
                w = max(w, fj.getlength(base))
            if ln.rm:
                w = max(w, fr.getlength(ln.rm))
            if ln.en:
                w = max(w, fe.getlength(ln.en))
            return w

        mids = [(ln.start + ln.end) / 2 for ln in lines]
        ws = [width(ln) for ln in lines]
        margin = 46 * s
        reqs = []
        for i in range(n - L):                       # same-lane neighbour = i, i+L
            dt = mids[i + L] - mids[i]
            if dt > 0.05:
                reqs.append(((ws[i] + ws[i + L]) / 2 + margin) / dt)
        if reqs:
            reqs.sort()
            # 92nd percentile: cover all but the very tightest couplets (a brief
            # rapid-fire burst may still touch) without over-speeding the song.
            self._v_floor = min(700.0, max(0.0, reqs[int(0.92 * (len(reqs) - 1))]))

    def _ticker_update(self, pos, pos_raw=None):
        center, v = self.W / 2, max(self.scroll_speed, self._v_floor)
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

        # The belt move above (one cv.move) is cheap and runs EVERY frame for
        # smooth scrolling. Everything below — an O(lines) visibility scan,
        # spawning/despawning blocks, and the PIL karaoke-fill repaints — is the
        # expensive, variable-cost work that made scrolling stutter at 60fps. It
        # needn't run every frame: blocks spawn 1200px off-screen (so a frame or
        # two of spawn latency is invisible) and a fill sweeping at ~20fps looks
        # identical — so do it every Nth frame, keeping the belt at full rate.
        self._tick_n += 1
        if self._tick_n % int(self._tune.get("scroll_fill_skip", self._fill_skip) or 1):
            return

        # The spawn window must be wide enough that the widest block (a long
        # English line ≈ 1500 px) is fully ready before it scrolls on-screen, or
        # it pops in mid-frame. Live-tunable via /tune `scroll_spawn_margin`.
        spawn_margin = self._tune.get("scroll_spawn_margin", 1100)
        want = {}
        for i, ln in enumerate(self.lines):
            cx = center + d * v * ((ln.start + ln.end) / 2 - pos)
            if -spawn_margin < cx < self.W + spawn_margin:
                want[i] = cx
        if want:
            self.cv.delete("hint")        # real lyrics showing → drop any stale hint
        have = {b["idx"] for b in self._stream}
        # Time-budget the heavy work: a PIL spawn/paste can take tens of ms, and
        # several in one frame is what stalls the scroll belt (the "stutter").
        # Track elapsed and stop issuing more PIL ops once we've spent the budget
        # — deferred spawns appear a frame later (invisible, 1200px off-screen)
        # and deferred fills catch up next heavy frame. Belt stays smooth.
        t_heavy = time.perf_counter()
        budget_ms = self._tune.get("scroll_heavy_budget_ms", 10.0)
        def _over_budget():
            return budget_ms > 0 and (time.perf_counter() - t_heavy) * 1000.0 > budget_ms
        # Spawn missing blocks, nearest-to-centre (most imminent) first.
        missing = sorted((i for i in want if i not in have),
                         key=lambda i: abs(want[i] - center))
        spawn_budget = int(self._tune.get("scroll_spawn_budget", self._spawn_budget))
        # TICKET-082: fill is keyed off pos_raw (raw song clock), NOT pos (eased).
        # Defaults to pos for backwards-compat if a caller didn't pass pos_raw.
        pos_f = pos_raw if pos_raw is not None else pos
        for i in missing[:spawn_budget]:
            if _over_budget():
                break
            cx = want[i]
            ln = self.lines[i]
            dur = ln.end - ln.start
            if dur > 0:
                frac = max(0.0, min(1.0, (pos_f - ln.start) / dur))
            else:
                frac = 0.0
            b = self._spawn_block(i, frac)
            self.cv.move(b["tag"], (cx - b["w"] / 2) - b["x"], 0)
            self._stream.append(b)
        # Despawn off-screen blocks, and advance karaoke fills — capped per pass.
        now = time.time()
        repaints = 0
        repaint_budget = int(self._tune.get("scroll_repaint_budget", self._repaint_budget))
        for b in self._stream[:]:
            cxb = want.get(b["idx"])
            # Despawn when outside the want window OR fully past the EXIT edge by
            # the block's REAL width. The old centre-only ±margin kept a 1500px
            # block alive ~500px (≈0.5 s) after it had fully left the screen — pure
            # wasted re-composite. Cull direction-aware: rl exits left, lr exits right.
            if cxb is None:
                gone = True
            elif d == 1:
                gone = cxb + b["w"] / 2 < -40
            else:
                gone = cxb - b["w"] / 2 > self.W + 40
            if gone:
                self.cv.delete(b["tag"])
                self._stream.remove(b)
                continue
            ln = self.lines[b["idx"]]
            dur = ln.end - ln.start
            if dur > 0:
                frac = max(0.0, min(1.0, (pos_f - ln.start) / dur))   # TICKET-082: raw clock + clamp
            else:
                frac = 0.0
            if b.get("img"):
                n = int(frac * b["nchars"] + 0.5)
                # Each fill repaint is a costly PhotoImage paste, so cap the
                # repaints per frame, each block's repaint rate, AND the total
                # heavy-frame time — a karaoke sweep at ~5fps reads fine.
                if (n != b["sung_n"] and repaints < repaint_budget
                        and now - b.get("paint_t", 0.0) >= self._tune.get("scroll_fill_interval", self._fill_interval)
                        and not _over_budget()):
                    b["sung_n"] = n
                    b["paint_t"] = now
                    # SLIVER fill (PERF-102): paste ONLY the strip that newly
                    # became sung — O(changed pixels), not a whole-block
                    # recomposite. The fill only ever grows, so re-compositing the
                    # entire 1.5×-scale block every step was pure waste and the
                    # dominant remaining scroll spike (~85 ms each). _advance_fill
                    # falls back to a full composite on a seek-back.
                    if b.get("base") is not None:
                        self._advance_fill(b, frac)
                    else:
                        b["photo"].paste(self._paint_block_img(b["spec"], frac))
                    repaints += 1
            else:
                self._highlight_block(b, frac)

    def _block_x_v(self, w, i=None):
        """Horizontal X for a block in VERTICAL scroll, from the `position`
        setting: hug the left edge, the right edge, or centre the column.

        When CENTRED, consecutive lines are STAGGERED across `_lanes` horizontal
        columns (the vertical-scroll analogue of horizontal scroll's vertical
        lanes), so 2-3 lines cascade diagonally down instead of stacking in one
        rigid column. Left/right anchoring keeps the single column."""
        if self.pos_x == "left":
            return self.pad
        if self.pos_x == "right":
            return max(self.pad, self.W - w - self.pad)
        base = (self.W - w) / 2.0                          # centre each line first
        L = max(1, getattr(self, "_lanes", 1))
        step = getattr(self, "_v_stagger", 0)
        if i is None or L <= 1 or not step:
            return max(0, round(base))                     # single-column centre (unchanged)
        base += (i % L - (L - 1) / 2.0) * step             # offset by this line's lane
        # Centre mode is exclusive of the L/R anchors, so the stagger isn't bound by
        # their padding — lines fan out across the FULL width. Clamp only to the true
        # screen edges (0 … W-w) so a fanned line is never pushed partly off-screen.
        return max(0, min(round(base), max(0, self.W - w)))

    def _ticker_update_v(self, pos, pos_raw=None):
        """VERTICAL scroll: lines stacked in one column that scrolls up ('bt' —
        enter from the bottom, credits-style) or down ('tb' — enter from the top).
        Mirror of _ticker_update on the Y axis; column X comes from `position`
        (left/center/right). No lanes — lines stack by time × speed."""
        center, v = self.work_h / 2.0, max(self.scroll_speed, self._v_floor)
        d = 1 if self.scroll_dir == "bt" else -1          # bt: mid-pos (up); tb: pos-mid (down)
        if self._stream:
            dy_f = -d * v * (pos - self._last_pos) + self._strm_rem
            dy = round(dy_f)
            self._strm_rem = dy_f - dy
            if dy:
                self.cv.move("strm", 0, dy)
        self._last_pos = pos
        self._tick_n += 1
        if self._tick_n % int(self._tune.get("scroll_fill_skip", self._fill_skip) or 1):
            return
        bh = self._block_h
        margin = bh * 2 + 80                               # spawn a couple blocks off-screen
        want = {}
        for i, ln in enumerate(self.lines):
            cy = center + d * v * ((ln.start + ln.end) / 2 - pos)
            if -margin < cy < self.work_h + margin:
                want[i] = cy
        if want:
            self.cv.delete("hint")
        have = {b["idx"] for b in self._stream}
        t_heavy = time.perf_counter()
        budget_ms = self._tune.get("scroll_heavy_budget_ms", 10.0)
        def _over_budget():
            return budget_ms > 0 and (time.perf_counter() - t_heavy) * 1000.0 > budget_ms
        missing = sorted((i for i in want if i not in have),
                         key=lambda i: abs(want[i] - center))
        spawn_budget = int(self._tune.get("scroll_spawn_budget", self._spawn_budget))
        pos_f = pos_raw if pos_raw is not None else pos    # TICKET-082: raw clock for fill
        for i in missing[:spawn_budget]:
            if _over_budget():
                break
            cy = want[i]
            ln = self.lines[i]
            dur = ln.end - ln.start
            if dur > 0:
                frac = max(0.0, min(1.0, (pos_f - ln.start) / dur))
            else:
                frac = 0.0
            # spawn at the right Y; then shift X to the column (width known after spawn)
            b = self._spawn_block(i, frac, place=(0, round(cy - bh / 2)))
            x = self._block_x_v(b["w"], i)                  # lane-staggered when centred
            if x:
                self.cv.move(b["tag"], x, 0)
                b["x"] = x
            self._stream.append(b)
        now = time.time()
        repaints = 0
        repaint_budget = int(self._tune.get("scroll_repaint_budget", self._repaint_budget))
        for b in self._stream[:]:
            cyb = want.get(b["idx"])
            if cyb is None:                               # outside window OR past exit edge
                gone = True
            elif d == 1:
                gone = cyb + bh / 2 < -60                 # bt: exits the top
            else:
                gone = cyb - bh / 2 > self.work_h + 60    # tb: exits the bottom
            if gone:
                self.cv.delete(b["tag"])
                self._stream.remove(b)
                continue
            ln = self.lines[b["idx"]]
            dur = ln.end - ln.start
            if dur > 0:
                frac = max(0.0, min(1.0, (pos_f - ln.start) / dur))   # TICKET-082: raw clock + clamp
            else:
                frac = 0.0
            if b.get("img"):
                n = int(frac * b["nchars"] + 0.5)
                if (n != b["sung_n"] and repaints < repaint_budget
                        and now - b.get("paint_t", 0.0) >= self._tune.get("scroll_fill_interval", self._fill_interval)
                        and not _over_budget()):
                    b["sung_n"] = n
                    b["paint_t"] = now
                    if b.get("base") is not None:
                        self._advance_fill(b, frac)
                    else:
                        b["photo"].paste(self._paint_block_img(b["spec"], frac))
                    repaints += 1
            else:
                self._highlight_block(b, frac)

    def _spawn_block(self, i, frac, place=None):
        """One image block (fast) if possible, else fall back to text items."""
        if self._use_img:
            try:
                return self._render_img_block(i, frac, place=place)
            except Exception:
                self._use_img = False     # disable images if rendering fails
        return self._render_block(i)

    def _clear_stream(self):
        for b in self._stream:
            self.cv.delete(b["tag"])
        self._stream = []

    def _anim_step(self, ox, oy=0, step=0):
        steps = 20
        if step >= steps:
            self._anim_id = None
            return
        e0 = 1 - (1 - step / steps) ** 3
        e1 = 1 - (1 - (step + 1) / steps) ** 3
        dx = -(e1 - e0) * ox
        dy = -(e1 - e0) * oy
        self.cv.move("cur", dx, dy)
        self._anim_id = self.root.after(16, self._anim_step, ox, oy, step + 1)

    def _commit_offset(self, new_off, reset_display=True):
        """TICKET-088: ATOMIC offset commit used by the deferred-commit and
        pause-end paths. Both used to write self.offset (and sometimes
        self._display_offset) inline and could fire on the SAME tick, producing
        multiple offset writes per frame. Centralizing the write keeps the
        ordering canonical (write offset → mirror to display if asked → log)
        and lets _tick count the writes for the same-tick assertion.

        reset_display=True is the right choice when we WANT _eased_offset to
        treat the new value as already-displayed (no glide ramp from the old
        value). pause-end uses False because it slides the display offset by
        the same delta separately so the ramp continues smoothly.
        """
        self.offset = new_off
        if reset_display:
            self._display_offset = new_off
            self._display_offset_t = time.time()
        # Per-tick offset-write counter (consumed by _tick's same-tick warning
        # gated on the assert_same_tick tune knob — see _tick_body docstring).
        try:
            self._tick_offset_writes = getattr(self, "_tick_offset_writes", 0) + 1
        except Exception:
            pass

    def _smooth_offset(self, new_off, reason=""):
        """Queue an auto-sync correction for the NEXT line boundary instead of
        snapping it in mid-line (TICKET-078). User-perceived effect: whatever
        line is on screen finishes naturally, then the following line is picked
        under the corrected offset — no jarring jump-cut. Big jumps (>5s),
        continuous-scroll modes, and corrections taken when no line is showing
        all commit immediately, since deferring those would look worse than
        snapping. A safety cap (8s) commits a stale queued offset regardless,
        so a stuck idx can't strand a pending correction forever."""
        new_off = round(new_off, 2)
        if new_off == self.offset and self._pending_offset is None:
            return
        big_jump = abs(new_off - self.offset) > 5.0
        no_line = self.idx < 0 or not self.lines
        # TICKET-082: scroll mode used to snap here (the bypass). It now also
        # queues at the next line boundary — the karaoke fill is computed
        # against pos_raw separately, so the belt can glide while the fill
        # waits to commit at the boundary, no more mid-line jump-cuts in
        # scroll mode either. Big jumps and no-line still snap.
        if no_line or big_jump:
            self.offset = new_off
            self.idx = -1
            self._drift_integral = 0.0
            self._pending_offset = None
            return
        # TICKET-081: reset the timestamp on EVERY new queue so the 8s safety
        # cap measures "how long has this CURRENT correction been waiting,"
        # not "how long has SOME correction been waiting." Otherwise a stream
        # of fresh corrections inherits the first one's t0 and the cap fires
        # mid-line, defeating the deferral.
        self._pending_offset = new_off
        self._pending_offset_t = time.time()
        log.info("sync: deferring %+.2fs → %+.2fs until current line ends (%s)",
                 self.offset, new_off, reason or "auto")

    def _eased_offset(self):
        """The DISPLAY offset the lyrics + karaoke fill actually use. It EASES
        toward the sound-sync target (self.offset) instead of applying it instantly,
        so when sync hears a mismatch and corrects the offset the highlight/scroll
        GLIDE to the right place over a few frames rather than skipping (the jumpy
        fill the user flagged). The sync target itself already comes from heard
        audio (Shazam offset + energy correlation); this just makes APPLYING it
        smooth. A big re-sync (>5 s — a song change or a major Shazam jump) snaps,
        since gliding a huge gap looks worse than a clean cut."""
        target = self.offset
        cur = getattr(self, "_display_offset", None)
        now = time.time()
        last_t = getattr(self, "_display_offset_t", 0.0)
        if cur is None or abs(target - cur) > 5.0:
            self._display_offset = target            # first use / major re-sync → snap
            self._display_offset_t = now
            return self._display_offset
        # TICKET-088: deadzone widened from a hardcoded 10 ms to a tunable
        # ease_deadzone_s (default 50 ms). Sub-deadzone drifts snap rather than
        # crawl through the easing ramp — the residual ramp is below human
        # perception and just wastes per-frame work / log churn.
        deadzone = float(self._tune.get("ease_deadzone_s", 0.05))
        if abs(target - cur) <= deadzone:
            self._display_offset = target
            self._display_offset_t = now
            return self._display_offset
        # TICKET-082: WALL-CLOCK based ease instead of per-frame, so a heavy
        # frame doesn't slow the glide — same offset change always finishes
        # in ~the same wall-clock time regardless of FPS load.
        # Default: 3 s/s slew speed cap, 70%/sec exponential pull. A 1s drift
        # finishes in ~0.5s; a 4s drift in ~1.5s.
        dt = max(0.001, min(0.25, now - last_t)) if last_t > 0 else (self._fps / 1000.0)
        rate_per_sec = float(self._tune.get("ease_slew_cap_s", 3.0))
        pull = float(self._tune.get("ease_pull_per_sec", 3.5))
        delta = target - cur
        # exponential pull: approach target at rate proportional to remaining distance
        step = delta * (1.0 - 2.71828 ** (-pull * dt))
        # absolute cap so even a 5s glide doesn't move faster than rate_per_sec
        cap = rate_per_sec * dt
        step = max(-cap, min(cap, step))
        # TICKET-088: per-frame fraction cap — a single 300ms heavy frame
        # (browser tab stall, GC pause, antivirus scan) with the default pull
        # rate eats ~65% of the remaining delta in ONE step = a visible snap,
        # not a glide. Cap the step at ease_max_step_frac (default 20%) of the
        # remaining delta so even a quarter-second stall produces a ramp the
        # eye reads as smooth motion across the next several frames.
        max_frac = float(self._tune.get("ease_max_step_frac", 0.20))
        if max_frac > 0.0 and delta != 0.0:
            frac_cap = max_frac * abs(delta)
            sign = 1.0 if step >= 0.0 else -1.0
            step = sign * min(abs(step), frac_cap)
        self._display_offset = cur + step
        self._display_offset_t = now
        return self._display_offset

    @contextlib.contextmanager
    def _perf_branch(self, name):
        """A2 sub-branch timer (workflow w821l9jnw).

        Brackets a code region with time.perf_counter() ONLY when `name` is in
        the parsed `perf_record_branches` set, otherwise the manager is a
        ~50ns set-membership check + yield (the 'off' fast-path).

        Result lands in self._perf_branch_ms[name] and is emitted in the perf
        log's meta column as ` | branch=name1=ms,name2=ms,...`. The dict is
        reset at the top of every _tick, so a value is the LAST completed call
        within this tick (a cancelled _render leaves a stale entry from the
        prior render — acceptable for stutter hunting).

        DO NOT nest inside per-character loops — three pairs per frame is
        ~600ns, N pairs per frame is observably non-zero on Windows.
        """
        if name not in self._perf_branches_set:
            yield
            return
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self._perf_branch_ms[name] = (time.perf_counter() - t0) * 1000.0

    def _perf_record(self, state, pos, pos_raw, branch):
        """TICKET-082: append-only per-frame perf log (near-zero observer effect
        when perf_record=0; under ~100us per frame when on, even with all three
        A2 sub-branch timers enabled). Captures: ts, frame_ms, raw_dt_ms (A2),
        render-branch, pos (eased), pos_raw, offset, idx, pending_offset, ease
        delta, plus an optional ` | branch=...` segment with the A2 sub-branch
        timings. Only when the perf_record tune knob is on. Rotated when the
        file passes perf_record_cap_mb.

        Usage:
          POST /tune?key=perf_record&value=1            # turn on
          POST /tune?key=perf_record&value=0            # turn off
          tail -f D:/DesktopKaraoke/perf.log            # watch live
        """
        try:
            if not int(self._tune.get("perf_record", 0)):
                if self._perf_fh is not None:
                    try:
                        self._perf_fh.close()
                    except Exception:
                        pass
                    self._perf_fh = None
                return
            # A2: refresh the cached sub-branch set ONLY when the tune string
            # changes (a per-tick string-split would burn ~300ns we don't need).
            raw_branches = self._tune.get("perf_record_branches",
                                          "render|kara|itemconfig") or ""
            if raw_branches != self._perf_branches_raw:
                self._perf_branches_raw = raw_branches
                self._perf_branches_set = {
                    s.strip() for s in raw_branches.split("|") if s.strip()
                }
            raw_on = bool(int(self._tune.get("perf_record_raw_frame_ms", 1)))
            if self._perf_fh is None:
                path = (self._tune.get("perf_record_path") or "").strip()
                if not path:
                    try:
                        path = str(appdata.data_dir() / "perf.log")
                    except Exception:
                        path = "perf.log"
                # rotate if too big
                try:
                    cap = float(self._tune.get("perf_record_cap_mb", 20.0)) * 1024 * 1024
                    import os
                    if os.path.exists(path) and os.path.getsize(path) > cap:
                        os.replace(path, path + ".old")
                except Exception:
                    pass
                self._perf_fh = open(path, "a", buffering=1, encoding="utf-8")
                self._perf_path = path
                self._perf_fh.write(f"\n# perf session start {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                # A2: format v2 = adds raw_dt_ms column (when perf_record_raw_frame_ms=1)
                # and an optional ' | branch=...' suffix in the meta column.
                self._perf_fh.write("# format v2\n")
                if raw_on:
                    self._perf_fh.write(
                        "# ts  frame_ms  raw_dt_ms  branch    pos_eased  pos_raw  "
                        "offset  pending  idx  ease_delta  meta\n"
                    )
                else:
                    self._perf_fh.write(
                        "# ts  frame_ms  branch    pos_eased  pos_raw  "
                        "offset  pending  idx  ease_delta  meta\n"
                    )
            ts = time.strftime("%H:%M:%S") + f".{int((time.time() % 1) * 1000):03d}"
            disp = getattr(self, "_display_offset", None) or 0.0
            ease_delta = round(disp - self.offset, 3)
            pending = "-" if self._pending_offset is None else f"{self._pending_offset:+.2f}"
            meta = ""
            if self._perf_last_offset is not None and abs(self.offset - self._perf_last_offset) > 0.04:
                meta = f"OFFSET_JUMP {self.offset - self._perf_last_offset:+.2f}"
            elif self._perf_last_idx is not None and self.idx != self._perf_last_idx:
                meta = f"IDX {self._perf_last_idx}->{self.idx}"
            # A2: append sub-branch timings to the meta column via a ' | ' guard
            # so the legacy meta values (OFFSET_JUMP / IDX a->b) can coexist on
            # the same line. Branches dict was cleared at the top of _tick, so
            # only branches that ACTUALLY ran this tick appear.
            if self._perf_branch_ms:
                bparts = ",".join(
                    f"{n}={ms:.1f}" for n, ms in self._perf_branch_ms.items()
                )
                meta = f"{meta} | branch={bparts}" if meta else f"branch={bparts}"
            self._perf_last_offset = self.offset
            self._perf_last_idx = self.idx
            if raw_on:
                # '-' (right-justified to keep column width) when we have no
                # trusted dt sample this tick (paused/no-music or outlier).
                raw_col = (f"{self._raw_dt_ms:6.1f}"
                           if self._raw_dt_ms is not None else "     -")
                self._perf_fh.write(
                    f"{ts}  {self._frame_ms:5.1f}  {raw_col}  {branch:8s}  "
                    f"{pos:8.2f}  {pos_raw:8.2f}  {self.offset:+6.2f}  "
                    f"{pending:>7s}  {self.idx:4d}  {ease_delta:+6.3f}  {meta}\n"
                )
            else:
                self._perf_fh.write(
                    f"{ts}  {self._frame_ms:5.1f}  {branch:8s}  "
                    f"{pos:8.2f}  {pos_raw:8.2f}  {self.offset:+6.2f}  {pending:>7s}  {self.idx:4d}  "
                    f"{ease_delta:+6.3f}  {meta}\n"
                )
        except Exception:
            pass     # never let perf-logging break the tick

    def _karaoke(self, pos_raw):
        """TICKET-082: takes pos_raw (raw song clock) so the fill ramps at the
        actual song rate instead of the eased glide rate. Caller decides which
        timebase — _tick passes pos_raw, legacy callers (if any) pass pos."""
        # A2: bracket the whole _karaoke() body. The early returns inside the
        # `with` block still let the context manager record elapsed time on the
        # way out (the value will be ~0ms for the empty/zero-dur fast paths,
        # which is itself useful signal).
        with self._perf_branch("kara"):
            if not self._kara:
                return
            ln = self.lines[self.idx]
            dur = ln.end - ln.start
            if dur <= 0:
                return
            frac = max(0.0, min(1.0, (pos_raw - ln.start) / dur))
            # A2: wrap the per-char itemconfig loop ONCE (not per call). This
            # measures the aggregate cost of the hot loop identified in
            # workflow w821l9jnw without adding N time.perf_counter() calls per
            # frame. Per-call probing is deliberately out of scope (A3-shaped).
            with self._perf_branch("itemconfig"):
                for tr in self._kara:                       # JP, romaji, English in lockstep
                    n = int(frac * len(tr["chars"]) + 0.5)  # index-based: works across wraps
                    base, sung = tr["base"], tr["sung"]
                    for i, k in enumerate(tr["chars"]):
                        col = sung if i < n else base
                        if k["last"] != col:
                            self.cv.itemconfig(k["fill"], fill=col)
                            k["last"] = col

    def _hint(self, msg):
        self.cv.delete("all")
        self._kara = []
        self._clear_stream()
        draw_text(self.cv, self.pad, self.H // 2, msg, self.HINT_FONT, DIM,
                  anchor="w", tags="hint")
        if self._mirrors:
            self._update_mirrors(None)

    # ── tray hooks ──

    def nudge(self, d):
        self._fine_exit("manual-nudge")            # user input wins; drop pause buffers
        # TICKET-088: route manual nudges through _smooth_offset so the user
        # sees the SAME glide the auto-sync uses (a direct self.offset += d
        # would snap the highlight in mid-line — the very behavior we just
        # capped in _eased_offset). _smooth_offset still snaps big jumps and
        # no-line states.
        self._smooth_offset(round(self.offset + d, 2), "manual-nudge")

    def reset_offset(self):
        self._fine_exit("manual-reset")            # user input wins; drop pause buffers
        # TICKET-088: route through _smooth_offset for a glided reset (snaps
        # automatically when there's no line on screen or when the current
        # offset is >5s, which covers most "reset is huge" cases).
        self._smooth_offset(0.0, "manual-reset")

    def get_tune(self):
        """Snapshot of the live-tunable sync parameters (for GET /tune)."""
        return dict(self._tune)

    def get_diag(self):
        """Rich diagnostics snapshot for GET /diag — everything needed to
        understand a desync or a stutter without rebuilding: the full sync
        state machine, the last energy-correlation result, and frame-timing /
        FPS metrics. Read-only and cheap."""
        st = self.media.get() or {}
        pos = st.get("position")
        eff = (pos + self.offset) if isinstance(pos, (int, float)) else None
        # which line SHOULD be showing at the effective time, and which IS
        want_idx, want_line = -1, None
        if eff is not None:
            for i, ln in enumerate(self.lines):
                if ln.start <= eff < ln.end:
                    want_idx, want_line = i, ln.jp[:50]
                    break
        target_fps = round(1000.0 / self._fps) if self._fps else None
        render_fps = round(1000.0 / self._frame_ms) if self._frame_ms > 0 else None
        hist = self._frame_hist[-60:]
        return {
            "sync": {
                "offset": round(self.offset, 2),
                "drift": getattr(self, "_last_drift", None),
                "drift_age_s": (round(time.time() - self._last_drift_t, 1)
                                if getattr(self, "_last_drift_t", 0) else None),
                "drift_integral": round(getattr(self, "_drift_integral", 0.0), 2),
                "pending_corr": (round(self._pending_corr, 2)
                                 if getattr(self, "_pending_corr", 1e9) < 1e8 else None),
                "last_audio_off": (round(self._last_audio_off, 2)
                                   if self._last_audio_off is not None else None),
                "last_audio_off_age_s": (round(time.time() - self._last_audio_off_t, 1)
                                         if self._last_audio_off_t else None),
                "sound_song": self._sound_song,
                "sound_title_alias": self._sound_title_alias,
                "title_locked": self._title_locked,
                # TICKET-099: SMTC vs Shazam priority telemetry
                "source_priority": self._source_priority,
                "verified_meta": bool(self._verified_meta),
                "sound_corroborated": bool(self._sound_corroborated),
                # TICKET batch4: verified→False render grace remaining.
                # 0 when no gate is active (verified is True, or the gate
                # has already expired, or a real song change cleared it).
                # >0 means lyrics are being held on screen while we wait for
                # a re-confirming Shazam read / album-alias path / takeover.
                "verified_render_gate_remaining_s": (
                    max(0.0, round((self._verified_gate_t
                                    + float(self._tune.get(
                                        "verified_render_gate_s", 3.0)))
                                   - time.time(), 2))
                    if self._verified_gate_t else 0.0),
                "smtc_paused_for_s": (round(time.time() - self._smtc_paused_since, 1)
                                      if self._smtc_paused_since else 0.0),
                "last_takeover_age_s": (round(time.time() - self._last_takeover_t, 1)
                                        if self._last_takeover_t else None),
                "live_arrangement": self._live_arrangement,
                "effective_song_time": round(eff, 2) if eff is not None else None,
                "showing_idx": self.idx,
                "should_show_idx": want_idx,
                "should_show_line": want_line,
                # In scroll-through (lr/rl) the BELT position (driven by `pos`)
                # is the sync indicator, not `idx` (which stays -1) — so judge
                # sync by the measured drift there; in line mode use idx match.
                "in_sync": (abs(self._last_drift) < 1.0
                            if self.scroll_dir in ("lr", "rl")
                            and getattr(self, "_last_drift", None) is not None
                            else ((self.idx == want_idx) if want_idx >= 0 else None)),
                "scroll_mode": self.scroll_dir in ("lr", "rl", "tb", "bt"),
                "offset_history": self._offset_hist[-20:],
                # adaptive verify tier (escalation/de-escalation)
                "tier_interval_s": round(self._sync_tier_interval, 1),
                "tier_good_streak": self._sync_good_streak,
                "tier_miss_streak": self._sync_miss_streak,
                "tier_incon_streak": self._sync_incon_streak,
                "tier_energy_blind": self._energy_blind,
                "tier_listening": self._tier_listen,
                # fine-tune mode (TICKET-085): post-major-sync precision pass
                "fine_active": bool(getattr(self, "_fine_active", False)),
                "fine_good_streak_s": (round(time.time() - self._fine_good_t0, 1)
                                       if getattr(self, "_fine_good_t0", None) else None),
                "fine_incon": int(getattr(self, "_fine_incon", 0)),
                "fine_pause_remaining_s": (round(self._fine_pause_until - time.time(), 2)
                                           if getattr(self, "_fine_pause_until", 0) > time.time() else 0.0),
                "fine_pause_amount": round(float(getattr(self, "_fine_pause_amount", 0.0)), 2),
            },
            "energy_align": self._last_energy,
            "decision": self._last_decision,        # last by-ear song decision (Whisper+rapidfuzz)
            "deciding": self._deciding,
            "fps": {
                "target": target_fps,
                "render": render_fps,
                "frame_ms": round(self._frame_ms, 1),
                "worst_ms": round(self._frame_worst, 1),
                "jitter_ms": round(self._frame_jitter, 1),
                "recent_ms": hist,
                "perf_mode": self.perf,
                "scroll_dir": self.scroll_dir,
                # TICKET-104 A1: bounded LRU cache for measure_text widths.
                # Steady-state target is >0.95 once the per-song char set is
                # warm; a sustained drop signals thrashing (font_scale churn
                # or cap too low for the active script mix).
                "measure_text_cache_hit_rate": _measure_text_cache_hit_rate(),
                "measure_text_cache_size": len(_MEASURE_CACHE),
                "measure_text_cache_max": _MEASURE_CACHE_MAX,
            },
            "aligning": self._aligning,
            "identifying": self._identifying,
            # TICKET-102: window-title scraper telemetry. Surfaces the latest
            # raw title under the watcher's eye even when SMTC is the active
            # source — invaluable for "why did it pick THIS as a song" field
            # reports without forcing the user to reproduce the tab state.
            "window_titles": self._diag_window_titles(),
        }

    def _diag_window_titles(self):
        """TICKET-102: snapshot of the window-title watcher for /diag.
        Returns a small dict; never raises (best-effort)."""
        out = {
            "on": bool(getattr(self, "window_titles_on", False)),
            "generic_browsers_on": bool(
                getattr(self, "window_titles_generic_browsers_on", False)),
            "source": None,        # exe basename if a track is currently held
            "raw": None,           # unparsed window title text
            "age_s": None,         # seconds since the watcher last refreshed
            "priority": None,      # 'high' | 'low' for the cached track
        }
        try:
            import window_titles as _wt
            snap = _wt._current_snapshot()
            out["age_s"] = snap.get("slot_age_s")
            tr = snap.get("track") or None
            if tr:
                out["source"] = tr.get("process")
                out["raw"] = tr.get("raw_title")
                out["priority"] = tr.get("priority")
        except Exception:
            pass
        return out

    def get_source(self):
        """Video/music SOURCE view (GET /source): the RAW media-session data the
        app receives from Windows SMTC, and what it derived from it. Lets a sync
        problem be traced to the SOURCE (wrong title leaking, stale position,
        rate change, no session) before blaming the sync logic."""
        st = self.media.get() or {}
        STATUS = {0: "closed", 1: "opened", 2: "changing", 3: "stopped",
                  4: "playing", 5: "paused"}
        return {
            "raw": {
                "title": st.get("title"),
                "artist": st.get("artist"),
                "album": st.get("album"),
                "status": STATUS.get(st.get("status"), st.get("status")),
                "position": round(st.get("position", 0.0), 2) if st else None,
                "duration": st.get("duration"),
                "rate": st.get("rate"),
                "source_app": st.get("source"),
            },
            "derived": {
                "clean_title": self._clean_title_cache,
                "clean_artist": self._clean_artist_cache,
                "track_tuple": list(self._track) if self._track else None,
                "is_cover": self._is_cover,
                # TICKET-086: WHICH cover signal fired — 'explicit' is the
                # high-confidence tag; 'amp_collab' takes the lower-confidence
                # title-only path; None = no cover signal.
                "cover_signal": getattr(self, "_cover_signal", None),
                "cover_original_artist": self._cover_original_artist,
                # TICKET-086: source-aware fields so /source confirms YT Music
                # routing live (album field is mirrored from raw for diag clarity).
                "yt_music_source": ("music.youtube"
                                    in (st.get("source") or "").lower()),
                "album": st.get("album"),
                "trusted_duration": self._trusted_duration(st) if st else None,
                "live_mode": self._live_mode,
                "mv_mode": self._mv_mode,
                "intro_anchored": self._intro_anchored,
            },
            "media_error": getattr(self.media, "error", None),
        }

    def get_audio(self):
        """Audio LISTENER view (GET /audio): what the loopback recorder is
        hearing right now — loudness, the live vocal-band ratio + on/off
        classification, and a compact recent on/off pattern. Confirms audio is
        flowing and whether vocals are being detected (the correlator's input)."""
        live = {}
        try:
            if self._boundary:
                live = self._boundary.live_audio()
        except Exception as e:
            live = {"error": str(e)}
        # compact recent vocal on/off pattern (last ~6 s) as a string
        pattern = None
        try:
            hist = self._boundary.vocal_history(6.0) if self._boundary else []
            if hist:
                rs = [r for (_, r) in hist]
                srt = sorted(rs)
                med = srt[len(srt) // 2]
                p75 = srt[int(0.75 * (len(srt) - 1))]
                th = med + 0.5 * (p75 - med)
                pattern = "".join("█" if r >= th else "·" for r in rs[-40:])
        except Exception:
            pass
        return {"live": live, "recent_pattern": pattern,
                "boundary_on": self.boundary_on}

    def get_lyric_state(self):
        """Lyric CURRENT-STATE analyzer (GET /lyricstate): where we are in the
        loaded lyrics right now, the surrounding lines with their timings, the
        karaoke-fill fraction, and structural sanity checks (LRC span vs song
        duration, gaps, lines past the end). Surfaces 'lyrics don't fit the
        song' problems that look like desync."""
        st = self.media.get() or {}
        pos = st.get("position")
        eff = (pos + self.offset) if isinstance(pos, (int, float)) else None
        n = len(self.lines)
        cur = -1
        if eff is not None:
            for i, ln in enumerate(self.lines):
                if ln.start <= eff < ln.end:
                    cur = i
                    break
        def _line(i):
            if 0 <= i < n:
                ln = self.lines[i]
                return {"i": i, "start": round(ln.start, 2), "end": round(ln.end, 2),
                        "jp": ln.jp[:60], "rm": (ln.rm or "")[:50], "en": (ln.en or "")[:50]}
            return None
        # if between lines, find the next upcoming line
        nxt = cur + 1 if cur >= 0 else next(
            (i for i, ln in enumerate(self.lines) if eff is not None and ln.start > eff), -1)
        fill = None
        if cur >= 0 and eff is not None:
            ln = self.lines[cur]
            dur = ln.end - ln.start
            fill = round(max(0.0, min(1.0, (eff - ln.start) / dur)), 2) if dur > 0 else None
        span = self.lines[-1].end if self.lines else 0.0
        vdur = st.get("duration") or 0.0
        # structural checks
        anomalies = []
        if vdur and span and span > vdur + 15:
            anomalies.append(f"LRC ends {span - vdur:.0f}s PAST video end")
        if vdur and span and span < vdur * 0.6:
            anomalies.append(f"LRC covers only {span / vdur * 100:.0f}% of the video")
        gaps = sum(1 for a, b in zip(self.lines, self.lines[1:]) if b.start - a.end > 8)
        if gaps:
            anomalies.append(f"{gaps} gap(s) >8s between lines")
        return {
            "line_count": n,
            "effective_song_time": round(eff, 2) if eff is not None else None,
            "current": _line(cur),
            "prev": _line(cur - 1) if cur > 0 else None,
            "next": _line(nxt),
            "fill_fraction": fill,
            "between_lines": cur < 0 and eff is not None,
            "lrc_span": round(span, 1),
            "video_duration": round(vdur, 1) if vdur else None,
            "span_vs_video": round(span - vdur, 1) if vdur and span else None,
            "meta": {k: self.meta.get(k) for k in ("title", "artist", "lang", "source", "duration")},
            "anomalies": anomalies,
        }

    # TICKET-106 / batch1: legacy → new key map for /tune back-compat. The scroll
    # perf knobs were renamed with a scroll_ prefix so future operators don't
    # confuse them with LINE-mode work (line mode is unbudgeted; see TICKET-104).
    # Existing /tune scripts that POST the old name still work: we redirect to the
    # new key and log a warning.
    _TUNE_LEGACY_ALIASES = {
        "heavy_budget_ms":  "scroll_heavy_budget_ms",
        "spawn_budget":     "scroll_spawn_budget",
        "repaint_budget":   "scroll_repaint_budget",
        "fill_skip":        "scroll_fill_skip",
        "fill_interval":    "scroll_fill_interval",
    }

    def set_tune(self, key, value):
        """Set one live-tunable sync parameter. Returns (ok, message). Coerces
        the value to the existing type. Only known keys accepted — silent reject
        of unknowns is a footgun for tuning."""
        # Back-compat: old (pre-rename) keys redirect to the new scroll_* name.
        alias = self._TUNE_LEGACY_ALIASES.get(key)
        if alias is not None and alias in self._tune:
            log.warning("tune: legacy key %r → use %r (redirected)", key, alias)
            key = alias
        if key not in self._tune:
            return False, f"unknown tune key {key!r}"
        try:
            old = self._tune[key]
            new = type(old)(value)
        except Exception as e:
            return False, f"can't coerce {value!r} to {type(self._tune[key]).__name__}: {e}"
        self._tune[key] = new
        log.info("tune: %s %r → %r", key, old, new)
        # TICKET-103: a /tune POST that flips gpu_solo_override must take
        # effect immediately (not wait for the next app restart).
        if key == "gpu_solo_override":
            try:
                import align
                align.set_gpu_solo_override(bool(new))
            except Exception:
                pass
        return True, f"{key}: {old} → {new}"

    # ── appearance (persisted) ──

    def _geom_y(self):
        # Window is fixed to the work area; vertical placement of content is
        # handled by _lane_y0, not by moving the window.
        return self.work_top

    def set_recal(self, secs):
        self.recal_secs = int(secs)
        self._arm_recal(2 if self.recal_secs else 30)   # apply the new cadence now
        self._persist()

    def apply_preset(self, name):
        """One-click settings bundles for common use cases."""
        if name == "gaming":          # learn a language while gaming — subtle
            self.opacity, self.pos_y, self.pos_x, self.scroll_dir = 0.45, "top", "center", "left"
            self.font_scale, self.perf = 1.0, "fast"
        elif name == "karaoke":       # big, flowing lyrics for a room of people
            self.opacity, self.pos_y, self.pos_x, self.scroll_dir = 1.0, "bottom", "center", "rl"
            self.font_scale, self.perf, self.scroll_speed = 1.5, "smooth", 200.0
        self.root.attributes("-alpha", self.opacity)
        self._apply_perf()
        self._apply_scale()
        self.root.geometry(f"{self.W}x{self.H}+{self.work_left}+{self._geom_y()}")
        self.root.attributes("-topmost", True)
        self._click_through()      # preset changed -alpha → re-assert click-through
        self._cancel_anim(); self._clear_stream(); self.cv.delete("all")
        self._kara, self.idx = [], -1
        self.root.update_idletasks()
        self._persist()

    def set_git_sync(self, on):
        self.git_sync = bool(on)
        self._persist()

    def set_character(self, on):
        self.character_on = bool(on)
        self.character.set_enabled(self.character_on)
        self._persist()

    def set_boundary(self, on):
        """Turn the audio song-change detector on/off (the fast switcher for
        compilations). Starts the thread lazily the first time it's enabled."""
        self.boundary_on = bool(on)
        if self.boundary_on and self._boundary is None:
            self._start_boundary()
        elif self._boundary is not None:
            self._boundary.set_enabled(self.boundary_on)
        self._persist()

    def _start_boundary(self):
        """Spin up the song-boundary detector (cheap RMS listener). On a detected
        track change it pulls a re-identify in immediately — see _on_boundary."""
        try:
            from songchange import SongChangeDetector
            self._boundary = SongChangeDetector(self._fire_boundary,
                                                on_onset=self._fire_onset_event,
                                                on_vocal=self._fire_vocal_event)
            self._boundary.set_enabled(self.boundary_on)
            self._boundary.start()
            log.info("song-change detector on")
        except Exception as e:
            log.info("song-change detector failed to start: %s", e)

    def _fire_boundary(self):
        """Called from the detector thread → marshal onto the Tk thread."""
        try:
            self.root.after(0, self._on_boundary)
        except Exception:
            pass

    def _on_boundary(self):
        """A new song likely just started inside the same video (compilation /
        DJ set). Re-identify by sound RIGHT NOW so the swap is quick, instead of
        waiting for the slow blind poll. Cheap to call: short capture, throttled,
        and skipped if we're already listening or just did."""
        st = self.media.get()
        if not (st and st.get("status") == PLAYING):
            return
        now = time.time()
        if self._identifying or now - self._last_boundary < 4.0:
            return
        self._last_boundary = now
        log.info("audio boundary detected → re-identifying by sound")
        self._fast_calib = max(self._fast_calib, 2)
        self._start_identify(seconds=5, attempts=1)
        self._arm_recal(6)
        # TICKET-079: inside a concert wrapper (ONE-MAN / LIVE / compilation), the
        # SMTC track is the whole concert — Shazam usually can't fingerprint MMD/
        # live performances, so back it up with a whole-library decide-by-ear ~12s
        # after the boundary. Gives the new song a vocal sample to anchor against.
        if self._live_arrangement or self._live_mode:
            self.root.after(12000,
                            lambda t=self._track_seq: self._decide_by_ear(
                                t, reason="boundary"))

    def _fire_onset_event(self, pre_quiet=0.0):
        """Detector thread heard the song start after a quiet intro → Tk thread."""
        try:
            self.root.after(0, lambda q=pre_quiet: self._on_song_onset(q))
        except Exception:
            pass

    def _fire_vocal_event(self):
        """Detector thread heard vocals start (band-energy rise) → Tk thread."""
        try:
            self.root.after(0, self._on_vocal_onset)
        except Exception:
            pass

    def _vocals_active_now(self, min_secs=1.2):
        """True when the live vocal-band energy has stayed clearly above the
        learned instrumental baseline for ~min_secs — i.e. singing has really
        started. Polled during the MV intro hold as a ROBUST release signal: the
        one-shot _fire_vocal_event can miss, so this reuses the always-on vocal
        buffer the sync correlator already maintains, so the lyrics aren't left
        stranded on the intro card."""
        b = getattr(self, "_boundary", None)
        if not b:
            return False
        try:
            hist = b.vocal_history(min_secs + 0.8)
            base = b.vocal_baseline() or 0.0
        except Exception:
            return False
        recent = [r for (_, r) in hist]
        if len(recent) < 3:
            return False
        # vocals = ratio over the instrumental floor (relative when a baseline was
        # learned, else an absolute fallback), sustained across HALF the window.
        # Loosened (was base*1.5 / 60%): the old bar missed real singing on
        # backing-heavy mixes (covers), leaving the lyrics stranded on the
        # "cinematic intro — waiting for vocals" card while the song was clearly
        # singing. Releasing a touch early is far better than holding through vocals.
        thresh = max(0.16, base * 1.3) if base > 0 else 0.20
        above = sum(1 for r in recent if r >= thresh)
        return above >= max(2, int(0.5 * len(recent)))

    def _on_vocal_onset(self):
        """Vocals just started — singing rose above the instrumental floor. Used
        to calibrate offset for music videos with long instrumental intros where
        the LRC starts at ~0 s but vocals don't begin until 1:00+ into the video
        (Grimes "Genesis" being the canonical case). Shazam can't fingerprint an
        instrumental intro, so without this signal the lyrics start scrolling at
        video time 0 even though singing is 70 s away. By anchoring lyric time
        to the actual first vocal moment, the karaoke catches up the moment
        singing kicks in instead of waiting for Shazam to find a vocal phrase
        mid-song (which can take half the song).

        Only fires once per track and only when Shazam hasn't already calibrated.
        Conservative: needs lyrics loaded with a first line near 0 s AND a
        meaningful gap between player position and that first line — otherwise
        the song just started at 0 (no intro) and this is the wrong correction.
        """
        if self._sound_song is not None or not self.lines:
            return
        st = self.media.get()
        if not (st and st.get("status") == PLAYING):
            return
        vpos = float(st.get("position") or 0.0)
        first_start = self.lines[0].start if self.lines else 0.0
        # Need a real intro: vocal-onset must be at least 8 s in (a song that
        # starts immediately with vocals has nothing to calibrate), the LRC's
        # first vocal line must be near 0 (a relative/song-only LRC, NOT one
        # that already has the intro baked in), and we need a meaningful gap
        # between video position and lyric start.
        if vpos < 8.0:
            log.info("vocal onset @%.1fs — too early to be a long intro, ignored", vpos)
            return
        if first_start > 8.0:
            # TICKET-081: for COVERS / live-arrangements the LRC is the studio
            # original — its intro is bound to be shorter than the cover/live
            # rendition. When measured vocals arrive MUCH later than the LRC's
            # first line (gap > 15 s), don't bail; compute the negative offset
            # so lyrics align to the actual first sung word. The 名前のない怪物
            # cover sat 78s out of sync because this branch returned silently.
            cover_or_live = (self._is_cover or self._live_arrangement
                             or (self.meta.get("source") or "").endswith("/cover"))
            gap = vpos - first_start
            if cover_or_live and gap > 15.0:
                new_off = round(first_start - vpos, 2)
                if -300.0 < new_off < 0.0:
                    self._intro_anchored = True
                    log.info("vocal onset @%.1fs (1st line @%.1fs) cover/live extended "
                             "intro → calibrated offset %+.1fs", vpos, first_start, new_off)
                    self._smooth_offset(new_off, "vocal-onset-cover")
                    self._hint("🎤 Vocals — synced (cover intro)")
                    return
            log.info("vocal onset @%.1fs — LRC already has intro baked in (1st @%.1fs)",
                     vpos, first_start)
            self._intro_anchored = True
            return
        # Calibrate: lyrics' first vocal line should align to NOW (the moment
        # vocals were heard). offset shifts lyric time to player time.
        # current_displayed_lyric_time = player_pos + offset
        # We want first_start = vpos, so offset = first_start - vpos.
        new_off = round(first_start - vpos, 2)
        # Sanity cap: a YouTube video with an instrumental intro is typically
        # 5-90 s of intro; bigger and something's off.
        if -120.0 < new_off < 0.0:
            self._intro_anchored = True
            self.offset = new_off
            self.idx = -1
            log.info("vocal onset @%.1fs (1st line @%.1fs) → calibrated offset %+.1fs",
                     vpos, first_start, new_off)
            self._hint("🎤 Vocals — synced")
            # TICKET-082: MV-intro fast-sync — for a studio MV with a long
            # preamble (綺麗事 / Suisei was 33s of quiet before vocals), the
            # 25s track-start auto-align happens BEFORE vocals + can't tune
            # an offset against silence. Schedule a fresh align ~5s after the
            # onset so we lock the precise sync before the second verse.
            if self._mv_mode and not self._live_arrangement:
                _seq = self._track_seq
                self.root.after(5000, lambda t=_seq: (
                    self._maybe_auto_align(reason="mv-intro-onset")
                    if t == self._track_seq else None))

    def _on_song_onset(self, pre_quiet=0.0):
        """The audio just kicked in after a leading quiet stretch — the end of an
        MV's cinematic / instrumental DEAD-SPACE intro. When Shazam can't ID the
        song (so it can't supply the real offset), anchor the lyric clock to THIS
        moment: the song's start = lyric time 0, so the lyrics stop running ahead
        through the intro. Only for a not-yet-aligned track still near its start;
        Shazam overrides later if it succeeds (see _consume_async)."""
        if self._intro_anchored or self._sound_song is not None or not self.lines:
            return
        st = self.media.get()
        if not (st and st.get("status") == PLAYING):
            return
        vpos = st.get("position", 0.0)
        # Only a real LEADING dead-space intro — NOT a mid-song breakdown. The
        # run-up must have been quiet for MOST of the time before the onset
        # (pre_quiet ≈ vpos), and capped at ~25s. Without this, a brief quiet
        # passage 40s into the song was mistaken for the intro and anchored the
        # offset to ~-40s (a severe desync). pre_quiet is the leading-quiet length
        # the detector measured just before the music kicked in.
        if not (1.0 < vpos < 25.0 and pre_quiet >= max(3.0, vpos * 0.6)):
            log.info("onset at %.1fs (quiet %.1fs) — not a leading intro; ignored",
                     vpos, pre_quiet)
            return
        self._intro_anchored = True
        # Anchor ONLY when the lyrics genuinely run AHEAD of the song. The lyrics'
        # own first timestamp decides it:
        #  • If the first line is already at/after the onset (first_start >= vpos),
        #    the LRC has the dead-space BUILT IN — its timestamps are absolute video
        #    time — so the right offset is 0. Anchoring (-vpos) double-shifted it
        #    (サクラミラージュ: 1st line @18.9s, onset @11s → drifted to -11s; the user's
        #    reset-to-0 fixed it). Now it auto-stays at 0.
        #  • If the first line starts BEFORE the onset (generated lyrics ~0, or a
        #    relative LRC), the lyrics ARE ahead → anchor lyric-time 0 to the onset.
        first_start = self.lines[0].start if self.lines else 0.0
        if first_start >= vpos - 2.0:
            self.offset = 0.0
        else:
            self.offset = round(-vpos, 2)
        self.idx = -1
        log.info("onset @%.1fs (quiet %.1fs, 1st line @%.1fs) → offset %+.1fs",
                 vpos, pre_quiet, first_start, self.offset)

    def set_api(self, on):
        """Start/stop the local agent-control API (127.0.0.1:8765)."""
        self.api_on = bool(on)
        if self.api_on and not self._api:
            try:
                from api import start_api
                self._api = start_api(self, LOG_FILE)
                log.info("API on http://127.0.0.1:8765")
            except Exception as e:
                log.info("API failed to start: %s", e)
        elif not self.api_on and self._api:
            try:
                self._api.shutdown()
            except Exception:
                pass
            self._api = None
        self._persist()

    def git_backup(self):
        """Commit + push ONLY the lyrics library, if this folder is a git repo
        with a remote. Stages just lyrics/ (never code/settings), so in the
        source repo (where lyrics are git-ignored) it harmlessly no-ops.
        Runs in the background."""
        if not (_DATA / ".git").exists():
            return

        def work():
            # Commit ONLY the lyrics pathspec — `git commit -- lyrics` ignores
            # anything else that happens to be staged in the index, so a manual
            # `git add` of code can never be swept into an auto-backup commit.
            for args in (["add", "--", "lyrics"],
                         ["commit", "-m", "Update lyric library", "--", "lyrics"],
                         ["push"]):
                try:
                    subprocess.run(["git", "-C", str(_DATA), *args],
                                   capture_output=True, timeout=90,
                                   creationflags=_NO_WINDOW)
                except Exception:
                    return
        threading.Thread(target=work, daemon=True).start()

    def _persist(self):
        _save_settings({"opacity": self.opacity, "pos_y": self.pos_y, "pos_x": self.pos_x,
                        "scroll": self.scroll_dir, "font_scale": self.font_scale,
                        "scroll_speed": self.scroll_speed, "perf": self.perf,
                        "recal_secs": self.recal_secs, "git_sync": self.git_sync,
                        "character": self.character_on, "api": self.api_on,
                        "boundary": self.boundary_on, "generate": self.generate_on,
                        "captions": self.captions_on,
                        "discord_rpc": self.discord_rpc_on,
                        "window_titles": self.window_titles_on,
                        "window_titles_generic_browsers":
                            self.window_titles_generic_browsers_on,
                        "concert_ocr": self.concert_ocr,
                        "display": self.display,
                        "display_fp": getattr(self, "_display_fp", None)})

    def set_generate(self, on):
        """Toggle the last-resort Whisper lyric generation."""
        self.generate_on = bool(on)
        self._persist()

    def set_captions(self, on):
        """Toggle auto-using a YouTube video's caption track for browser videos."""
        self.captions_on = bool(on)
        self._persist()

    def set_discord_rpc(self, on):
        """TICKET-100: toggle the Discord Rich Presence reader (read the user's
        own Spotify Listening activity as a 3rd-priority lyric-name source).
        Mirrors set_captions: bool flag + _persist; the runtime poll in _tick
        reads self.discord_rpc_on every frame. Also mirror into the tune dict
        so /tune queries reflect the live state.

        BUG-2/5/6 (v1.0.89): start (or stop) the long-lived watcher daemon so
        the Tk-thread poll in _tick is a non-blocking slot read. The watcher
        is the ONLY thing that touches the IPC pipe; spawning it here keeps
        the per-frame cost in _tick to a single mutex acquire + dict copy."""
        self.discord_rpc_on = bool(on)
        try:
            self._tune["discord_rpc_on"] = 1 if self.discord_rpc_on else 0
        except Exception:
            pass
        try:
            import discord_rpc as _drpc
            if self.discord_rpc_on:
                poll_s = float(self._tune.get("discord_rpc_poll_s", 5.0))
                _drpc.start_watcher(poll_s=poll_s)
            else:
                _drpc.stop_watcher()
        except Exception:
            pass
        self._persist()

    def set_window_titles(self, on):
        """TICKET-102: toggle the window-title scraper (Steam Overlay etc.).
        Mirrors set_discord_rpc: bool flag + tune mirror + start/stop the
        long-lived daemon + _persist. Default ON, opt-out — see the tune
        knob docs for the high vs low tier rationale."""
        self.window_titles_on = bool(on)
        try:
            self._tune["window_titles_on"] = 1 if self.window_titles_on else 0
        except Exception:
            pass
        try:
            import window_titles as _wt
            if self.window_titles_on:
                poll_s = float(self._tune.get("window_titles_poll_s", 2.0))
                _wt.start_watcher(
                    poll_s=poll_s,
                    generic_browsers=self.window_titles_generic_browsers_on,
                )
            else:
                _wt.stop_watcher()
        except Exception:
            pass
        self._persist()

    def set_window_titles_generic_browsers(self, on):
        """TICKET-102: flip the LOW tier (chrome/edge/firefox/etc.) on/off.
        Sub-toggle of window_titles_on; if the parent is off this just
        persists the preference for when the parent is flipped back on."""
        self.window_titles_generic_browsers_on = bool(on)
        try:
            self._tune["window_titles_generic_browsers"] = (
                1 if self.window_titles_generic_browsers_on else 0)
        except Exception:
            pass
        try:
            import window_titles as _wt
            # Only push live if the parent is on; otherwise the daemon
            # isn't running and there's nothing to update.
            if self.window_titles_on:
                _wt.set_generic_browsers(
                    self.window_titles_generic_browsers_on)
        except Exception:
            pass
        self._persist()

    def set_now_url(self, url):
        """The browser tells us the EXACT video URL currently playing, so the
        caption fetch hits that exact upload (not a fuzzy title search). A new
        URL re-arms the per-song caption fetch."""
        url = (url or "").strip() or None
        # TICKET-086: a YouTube Music tab pushes a music.youtube.com URL — same
        # video id, different host. Canonicalize here so the cached _now_url and
        # the diagnostics (/source, /status) always carry the www.youtube.com
        # form (belt-and-braces with deep_transcribe._normalize_youtube_url).
        if url and "music.youtube.com" in url.lower():
            url = (url.replace("://music.youtube.com", "://www.youtube.com")
                      .replace("://m.music.youtube.com", "://www.youtube.com"))
        if url and url != self._now_url:
            self._now_url = url
            self._caption_song = None        # re-fetch captions for the exact video
            if (self.captions_on and self._track and not self._live_mode
                    and (self.meta.get("source") or "") != "youtube-captions"):
                self.root.after(300,
                                lambda t=self._track_seq: self._maybe_fetch_captions(t))

    def _click_through(self):
        """(Re)assert the overlay's click-through window style AND its z-order.

        CRITICAL: this MUST be re-applied after every ``-alpha`` /
        ``-transparentcolor`` change. On Windows, tkinter resets the window's
        EXTENDED style when those are set, which silently DROPS our
        ``WS_EX_TRANSPARENT`` bit — turning the full-screen overlay into a window
        that EATS every click (you can't click your game/app underneath). That is
        the "can't click anything in game" bug: it appeared the moment the opacity
        changed (e.g. applying the 45%-opacity Gaming preset).

        WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW keep it from stealing focus or adding a
        taskbar button; WS_EX_LAYERED + WS_EX_TRANSPARENT make every pixel pass
        mouse input straight through to whatever is below.

        TICKET-082c: also re-assert HWND_TOPMOST every guard tick. Tk's
        ``-topmost`` attribute is a one-shot at create time; games / fullscreen
        apps / focus changes can knock the window out of top z-order over time.
        ``SetWindowPos(HWND_TOPMOST, …, NOMOVE|NOSIZE|NOACTIVATE)`` is what
        Discord/Steam/Nvidia overlays use and is a no-op when already topmost,
        so this is safe to call from the 500 ms ``_click_guard`` loop. (Caveat:
        exclusive-fullscreen DirectX games cannot be overlaid by any Win32
        window without DXGI hooks — use borderless-fullscreen-windowed mode.)
        """
        try:
            u = ctypes.windll.user32
            hwnd = u.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
            GWL_EXSTYLE = -20
            WS_EX = (0x08000000 | 0x00000080 | 0x00080000 | 0x00000020
                     | 0x00000008)              # …| TOPMOST so EXSTYLE bit stays set
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if (ex & WS_EX) != WS_EX:                  # only re-apply if a bit was lost
                u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX)
            HWND_TOPMOST = -1
            SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
            u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                           SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            # Mirror windows too — each is its own HWND.
            for win in getattr(self, "_mirrors", []) or []:
                try:
                    mh = u.GetAncestor(win.winfo_id(), 2) or win.winfo_id()
                    u.SetWindowPos(mh, HWND_TOPMOST, 0, 0, 0, 0,
                                   SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                except Exception:
                    pass
        except Exception:
            pass

    def _click_guard(self):
        """Safety net: periodically re-assert click-through so the overlay can NEVER
        get stuck eating clicks, even if some future code path resets the window
        style. Cheap — `_click_through` only writes when a style bit is missing."""
        self._click_through()
        try:
            self.root.after(500, self._click_guard)
        except Exception:
            pass

    def set_opacity(self, v):
        self.opacity = max(0.15, min(1.0, v))
        self.root.attributes("-alpha", self.opacity)
        self._click_through()      # -alpha resets the exstyle → re-assert click-through
        self.root.update_idletasks()
        self._persist()

    def set_pos(self, axis, value):
        """Set ONE position axis independently — axis 'y' (top|center|bottom) or
        'x' (left|center|right). The two are chosen separately so the user can pick,
        e.g., bottom-right or top-left."""
        if axis == "y" and value in ("top", "center", "bottom"):
            self.pos_y = value
        elif axis == "x" and value in ("left", "center", "right"):
            self.pos_x = value
        else:
            return
        self._relayout_song()          # recompute the anchor for the new position
        self.root.geometry(f"{self.W}x{self.H}+{self.work_left}+{self._geom_y()}")
        self.root.attributes("-topmost", True)
        self._cancel_anim()
        self._clear_stream()           # scroll belt re-spawns at the new anchor/column
        self.cv.delete("all")
        self._kara = []
        self.idx = -1                  # next tick repaints in place
        self.root.update_idletasks()   # apply the move immediately
        self._persist()

    # ── mirror-mode clone windows ──

    def _destroy_mirrors(self):
        for w in self._mirrors:
            try:
                w.destroy()
            except Exception:
                pass
        self._mirrors = []

    def _create_mirrors(self, mons, active_fp):
        """Create transparent, click-through Toplevel clones on every monitor
        except the one the main overlay sits on."""
        self._destroy_mirrors()
        for m in mons:
            if m["fp"] == active_fp:
                continue
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.configure(bg=TRANSPARENT)
            win.attributes("-topmost", True)
            win.attributes("-transparentcolor", TRANSPARENT)
            win.attributes("-alpha", self.opacity)
            wh = m["wh"]
            win.geometry(f"{m['ww']}x{wh}+{m['wx']}+{m['wy']}")
            cv = tk.Canvas(win, bg=TRANSPARENT, highlightthickness=0)
            cv.pack(fill="both", expand=True)
            win._mirror_cv = cv
            win._mirror_mon = m
            _click_through_hwnd(win)
            self._mirrors.append(win)

    def _update_mirrors(self, ln):
        """Render the current line's text on each mirror canvas."""
        for win in self._mirrors:
            cv = win._mirror_cv
            cv.delete("all")
            if ln is None:
                continue
            m = win._mirror_mon
            cw, ch = m["ww"], m["wh"]
            cy = (self._win_margin + 40 if self.pos_y == "top"
                  else ch - self._bottom_clear - 40)
            if ln.jp:
                draw_text(cv, cw // 2, cy, ln.jp, self.JP_FONT, WHITE)
                cy += 36
            if ln.rm:
                draw_text(cv, cw // 2, cy, ln.rm, self.ROMAJI_FONT, ROMAJI_C)
                cy += 28
            if ln.en:
                draw_text(cv, cw // 2, cy, ln.en, self.EN_FONT, EN_C)

    def _place_window(self):
        """(Re)position the overlay band at its current monitor / span coordinates."""
        self.root.geometry(f"{self.W}x{self.H}+{self.work_left}+{self._geom_y()}")
        self.root.attributes("-topmost", True)
        self._click_through()      # a display move can reset the exstyle → re-assert

    def _resolve_monitor(self, mons):
        """Find the monitor matching ``self.display``, preferring fingerprint over
        index so the choice survives monitor re-enumeration after sleep/wake."""
        if self.display == "primary":
            return next((m for m in mons if m["primary"]), mons[0])
        if isinstance(self.display, str) and self.display.startswith("mon:"):
            fp = getattr(self, "_display_fp", None)
            if fp:
                hit = next((m for m in mons if m["fp"] == fp), None)
                if hit:
                    return hit
                log.info("saved monitor fp %s not found; trying index", fp)
            try:
                idx = int(self.display[4:])
            except ValueError:
                idx = 0
            if 0 <= idx < len(mons):
                return mons[idx]
            log.info("monitor index %s out of range (%d connected); falling back to primary", idx, len(mons))
        return next((m for m in mons if m["primary"]), mons[0])

    def _apply_display(self):
        """Place the overlay per ``self.display``: 'primary', 'mon:N', 'span',
        'mirror' (same lyrics on every screen), or 'cycle' (rotate screens per
        line). Recomputes the band's width / position / scale for the target."""
        mons = _monitors()
        self._mon_snapshot = tuple(m["fp"] for m in mons)
        self._destroy_mirrors()
        if self.display == "span" and len(mons) > 1:
            left = min(m["wx"] for m in mons)
            right = max(m["wx"] + m["ww"] for m in mons)
            prim = next((m for m in mons if m["primary"]), mons[0])
            self.work_left, self.W = left, right - left
            self.work_top, self.work_bottom = prim["wy"], prim["wy"] + prim["wh"]
        elif self.display == "mirror" and len(mons) > 1:
            m = next((m for m in mons if m["primary"]), mons[0])
            self._display_fp = m["fp"]
            self.work_left, self.W = m["wx"], m["ww"]
            self.work_top, self.work_bottom = m["wy"], m["wy"] + m["wh"]
            self._create_mirrors(mons, m["fp"])
        elif self.display == "cycle" and len(mons) > 1:
            self._cycle_idx = self._cycle_idx % len(mons)
            m = mons[self._cycle_idx]
            self._display_fp = m["fp"]
            self.work_left, self.W = m["wx"], m["ww"]
            self.work_top, self.work_bottom = m["wy"], m["wy"] + m["wh"]
        else:
            m = self._resolve_monitor(mons)
            self._display_fp = m["fp"]
            self.work_left, self.W = m["wx"], m["ww"]
            self.work_top, self.work_bottom = m["wy"], m["wy"] + m["wh"]
        self.work_h = self.work_bottom - self.work_top
        self.H = self.work_h
        self._auto_scale = min(2.5, max(0.7, self.work_h / 1000.0))
        self._bottom_clear = max(56, round(self.work_h * 0.10))
        self._apply_scale()          # re-font + re-layout for the new width/height
        self._place_window()
        self.root.update_idletasks()

    def set_display(self, d):
        """Tray 'Display' submenu → move the overlay to a monitor, or span all."""
        self.display = d
        try:
            self._apply_display()
        except Exception as e:
            log.info("display switch failed: %s", e)
        log.info("display set to %s (fp=%s)", d, getattr(self, "_display_fp", None))
        if getattr(self, "idx", -1) >= 0 and getattr(self, "lines", None):
            self._render(self.lines[self.idx])
        self._persist()

    def set_scroll(self, d):
        self.scroll_dir = d
        # Auto-orient pos_x for the per-line slide modes ONLY. The direction the
        # line slides FROM implies a natural horizontal anchor:
        #   left  -> anchor LEFT   (slides in from the left edge)
        #   right -> anchor RIGHT  (slides in from the right edge)
        #   top   -> anchor CENTER (drops straight down)
        #   bottom-> anchor CENTER (rises straight up)
        # Continuous scroll modes ('lr','rl','tb','bt') and 'none' keep whatever
        # pos_x the user already chose — auto-orient is a slide-in-only contract.
        # pos_y is left alone in all cases so the user's vertical anchor stays
        # independent of the slide axis.
        _orient = {"left": "left", "right": "right", "top": "center", "bottom": "center"}
        if d in _orient:
            self.pos_x = _orient[d]
        self._apply_scale()                # scroll mode is a taller, laned window
        self.root.geometry(f"{self.W}x{self.H}+{self.work_left}+{self._geom_y()}")
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
            self._fill_skip = 2          # heavy ticker work every 2nd frame (~15fps)
        else:
            _OUTLINE = _OUTLINE_FULL     # 5 items/char, 60fps
            self._fps = 16
            self._fill_skip = 3          # belt moves at 60fps; fills/spawns at ~20fps
        # mirror into the live-tune dict so /tune reflects the mode's defaults
        if hasattr(self, "_tune"):
            self._tune["scroll_fill_skip"] = float(self._fill_skip)

    def set_quality(self, mode):
        self.perf = mode
        self._apply_perf()
        self._clear_stream()
        self.cv.delete("all")
        self._kara = []
        self.idx = -1
        self._persist()

    def _main_tk_font(self, ln):
        """Main-row tkinter font chosen by the line's script (so Korean/Chinese
        don't render as boxes in Yu Gothic)."""
        fam = _TK_MAIN_FONT[_script_of(ln.jp, self.meta.get("lang"))]
        return (fam, max(10, round(38 * self.font_scale * self._auto_scale)), "bold")

    def _main_pil_kind(self, ln):
        return _script_of(ln.jp, self.meta.get("lang"))

    def _apply_scale(self):
        s = self.font_scale * self._auto_scale
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
        # different heights instead of piling up at one level. Per-row baselines
        # within a block; the block's *height* adapts to which rows the current
        # song actually uses (English-only songs are one row → shorter blocks →
        # more lanes → far less overlap), set in _relayout_song().
        self.b_furi = round(26 * s)
        self.b_main = round(70 * s)
        self.b_rom  = round(132 * s)
        self.b_en   = round(170 * s)
        self._lane_top = round(8 * s)
        self._relayout_song()              # sets _block_h, _lane_gap, _lanes, H

    def _relayout_song(self):
        """Size blocks + lanes to the CURRENT song's rows, and place the lane
        stack WITHIN the fixed full-work-area window. The window itself never
        moves or resizes (that was the cause of lyrics drifting down: it used to
        shrink/grow per song and re-anchor to the bottom). Here only the content
        offset `_lane_y0` changes, so bottom-anchored lines stay pinned to the
        bottom and simply grow upward when there are more rows."""
        s = self.font_scale * self._auto_scale
        has_rm = has_en = False
        for ln in getattr(self, "lines", None) or []:
            has_rm = has_rm or bool(ln.rm.strip())
            has_en = has_en or bool(ln.en.strip())
            if has_rm and has_en:
                break
        if has_en:
            self._block_h = self.b_en + round(36 * s)     # ≈ full 4-row height
        elif has_rm:
            self._block_h = self.b_rom + round(34 * s)
        else:
            self._block_h = self.b_main + round(46 * s)    # single main row
        self._lane_gap = self._block_h + round(14 * s)
        usable = self.work_h - 2 * self._win_margin
        fit = 1 + max(0, (usable - self._lane_top - self._block_h) // self._lane_gap)
        # PERF: each lane is another full row of wide furigana image-blocks that
        # Tkinter re-composites EVERY frame (the dominant scroll cost — see
        # PERF-102). Cap the lanes hard: more lanes = more simultaneous big
        # bitmaps = fewer fps. Default 2 (current + next line) instead of 4 —
        # halves the per-frame bitmap area for tall 4-row blocks. Live-tunable via
        # /tune `scroll_max_lanes` so it can be dialled per machine without a rebuild.
        cap = int(getattr(self, "_tune", {}).get("scroll_max_lanes", 2) or 2)
        self._lanes = max(1, min(cap, int(fit)))
        # VERTICAL scroll: horizontal stagger step so consecutive lines cascade
        # across `_lanes` columns (the mirror of horizontal scroll's vertical
        # lanes). Scaled with the font; live-tunable via /tune scroll_v_stagger.
        self._v_stagger = round(float(getattr(self, "_tune", {})
                                      .get("scroll_v_stagger", 170)) * s)
        # First-lane Y inside the fixed window: top-anchored or bottom-anchored.
        stack = self._block_h + self._lane_gap * (self._lanes - 1)
        if self.pos_y == "top":
            self._lane_y0 = self._win_margin + self._lane_top
        elif self.pos_y == "center":
            self._lane_y0 = max(self._win_margin, round((self.work_h - stack) / 2))
        else:   # bottom
            self._lane_y0 = max(self._win_margin,
                                self.work_h - self._bottom_clear - stack)
        self._compute_scroll_floor()

    def set_font_scale(self, v):
        self.font_scale = max(0.25, min(2.0, v))
        self._apply_scale()
        self.root.geometry(f"{self.W}x{self.H}+{self.work_left}+{self._geom_y()}")
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
            self.root.overrideredirect(True)   # re-assert borderless...
            self.root.attributes("-topmost", True)
            self._click_through()              # ...and click-through after re-showing

    def refetch(self):
        self._fetch_key = None
        self._lyrics_path = None
        # TICKET-090: user-driven correction → drop the title-lock so the decide
        # loop re-engages on demand (the lock would otherwise still gate decide
        # for a fraction of a second before _on_track_change re-evaluates it).
        self._title_locked = False
        if self._track:
            self._on_track_change(self._track, self._cur_duration)

    def report_wrong(self):
        """User-driven correction: bin the wrong lyrics and identify by SOUND.
        For covers, also re-fetch using the original artist from the title."""
        if self._lyrics_path:
            try:
                Path(self._lyrics_path).unlink(missing_ok=True)
            except Exception:
                pass
            self.index.refresh()
        self._fetch_key = None
        self._lyrics_path = None
        self.lines, self.idx, self._kara = [], -1, []
        self._sound_song = None
        self._title_locked = False     # let sound override after a manual reject
        # For covers, try re-fetching with the original artist from the title
        # before falling back to sound (which often fails on covers)
        if self._is_cover and self._cover_original_artist and self._track:
            artist, title = self._track
            log.info("wrong-song on cover: re-fetching %r by original artist %r",
                     title, self._cover_original_artist)
            self._hint(f"🔄 Re-fetching as {self._cover_original_artist} — {title}…")
            self._start_fetch(self._cover_original_artist, title,
                              self._cur_duration, cover=True)
        else:
            self._hint("🎧 Listening to identify the song…")
        self._start_identify()

    def identify_by_sound(self):
        self._sound_song = None
        self._hint("🎧 Listening to identify the song…")
        self._start_identify()

    def load_youtube_captions(self, silent=False, url=None):
        """Pull THIS video's YouTube caption track (accurate text + perfect
        timing, locked to the video) and use it INSTEAD of the provider LRC.
        For a browser video this is the most accurate source — it fixes both
        wrong-transcription LRCs (the "white balance" case, where syncedlyrics
        returned different words than the video sings) and cross-version timing
        drift. Runs in the background; no-ops if yt-dlp isn't available or the
        video has no caption track.

        `url` (or the browser-pushed `self._now_url`) fetches the EXACT playing
        video — strictly better than a title search, which can land on a
        different upload whose intro length (and thus caption timing) differs."""
        try:
            import deep_transcribe
            if not deep_transcribe.available():
                if not silent:
                    self._hint("YouTube captions need yt-dlp — see the README")
                return
        except Exception:
            return
        if not self._track:
            return
        # SINGLE-FLIGHT: only ONE yt-dlp caption fetch at a time. A fast playlist
        # would otherwise spawn a new (heavy: network + node JS runtime) fetch
        # every track while the previous ones still ran — they pile up and
        # SATURATE THE CPU, which stutters the audio AND the overlay. yt-dlp runs
        # to completion in its thread even when the result will be discarded, so
        # the guard is essential, not just an optimization.
        if getattr(self, "_captions_fetching", False):
            return
        artist, title = self._track
        # exact video URL (param > browser-pushed) beats a fuzzy title search
        query = url or self._now_url or self._last_raw_title or title
        lang = self.meta.get("lang", "ja")
        # Guard by _track_seq (bumped ONLY on a real song change), NOT _deep_token
        # — generation / a title re-report bump _deep_token mid-fetch and would
        # wrongly discard the captions even though the SAME song is still playing
        # (the "76 lines fetched but not applied" bug). Captions are ground truth,
        # so they apply as long as the song hasn't actually changed.
        seq = self._track_seq
        self._captions_fetching = True
        if not silent:
            self._hint("📥 Pulling the video's caption track…")

        def work():
            res = None
            try:
                res = deep_transcribe.fetch_captions_only(query, lang=lang)
            except Exception as e:
                log.info("captions error: %s", e)
            finally:
                self._captions_fetching = False
            if not res:
                if not silent:
                    self.root.after(0, lambda: self._hint("No caption track found for this video"))
                return
            if seq != self._track_seq:
                return                              # the song actually changed
            lines, clang = res
            try:
                from fetch_lyrics import annotate
                annotate(lines, clang or lang, translate=True)
            except Exception:
                pass
            if seq == self._track_seq:
                self.root.after(0, lambda: self._apply_captions(
                    seq, title, artist, lines, clang or lang))

        threading.Thread(target=work, daemon=True).start()

    def _apply_captions(self, seq, title, artist, lines, lang):
        """(Tk thread) save + load a YouTube caption track as the lyrics. Guarded
        by _track_seq (the song), so generation / deep-token churn can't discard
        it. Captions are ground truth → they replace whatever LRC was showing."""
        if seq != self._track_seq or not lines:
            return
        from fetch_lyrics import slugify
        try:
            out = LYRICS_DIR / f"{slugify(title)}.json"
            data = {"meta": {"title": title, "artist": artist, "lang": lang,
                             "duration": self._cur_duration, "source": "youtube-captions"},
                    "lines": lines}
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
            self.index.add(out)
            log.info("captions: applied %d lines -> %s", len(lines), out.name)
        except Exception as e:
            log.info("captions save failed: %s", e)
            return
        # captions win over any LRC / generation for this song
        self._gen_token += 1
        self._deep_token += 1
        self._fetch_key = None      # cancel a still-pending LRC fetch result so it
        self._fetch_result = None   # can't overwrite the captions a moment later
        self._generating = False
        self.load(out, keep_idx=True)
        self.idx = -1                               # force re-render over the hint
        self._hint("✨ Synced to the video's captions")

    # ── sync by listening (align cached lyrics to the HEARD audio) ──
    def _align_pos(self):
        """The player's RAW position right now (no offset applied) — read at
        capture start so the alignment can derive an absolute offset."""
        st = self.media.get() or {}
        return float(st.get("position") or 0.0)

    def _check_applause_gap(self, now):
        """In a LIVE/concert cut a pause for applause & cheering keeps the player
        clock running while no one sings, so the lyrics drift ahead by the pause
        length. Detect the gap — loud but NON-vocal (broadband cheering, not tonal
        singing) — and, when singing returns, kick off a Whisper transcribe-and-match
        resync GATED BY TWO-POINT verification (TICKET-061). Cheap poll; the caller
        throttles it."""
        if not (self._live_arrangement or self._live_mode) or not self.lines or self._aligning:
            self._applause_for, self._applause_armed, self._applause_t = 0.0, False, now
            return
        b = getattr(self, "_boundary", None)
        if not b:
            return
        # 2-6 min heuristic (TICKET-063): a CONCERT song still showing after ~6.5 min
        # almost certainly changed and we missed the boundary → force a re-identify.
        if self._live_mode and not self._identifying:
            if self._lyrics_path != getattr(self, "_concert_song_path", None):
                self._concert_song_path, self._concert_song_t = self._lyrics_path, now
            elif now - getattr(self, "_concert_song_t", now) > 390.0 \
                    and now - getattr(self, "_last_align_t", 0.0) > 30.0:
                log.info("concert song shown > 6.5 min → forced re-identify (missed a change?)")
                self._concert_song_t, self._last_ocr_t = now, 0.0
                self._start_identify(seconds=5, attempts=2)
                return
        dt = min(2.0, now - self._applause_t) if self._applause_t else 0.0
        self._applause_t = now
        try:
            lv = b.live_audio()
        except Exception:
            return
        # applause/cheering = loud, broadband (high spectral flatness), NOT tonal singing
        applause = (not lv.get("is_silent") and lv.get("noise_like")
                    and not lv.get("vocal_detected_now"))
        if applause:
            self._applause_for += dt
            if self._applause_for >= self._tune.get("applause_min_s", 2.5):
                self._applause_armed = True
        elif self._applause_armed and lv.get("vocal_detected_now"):
            self._applause_for, self._applause_armed = 0.0, False
            if self._live_mode:
                # CONCERT compilation: an applause gap is almost always a SONG BOUNDARY,
                # not a mid-song pause → RE-IDENTIFY the next song (sound + a forced
                # OCR-banner read), don't resync the old one (TICKET-063).
                log.info("applause gap (~%.1fs) in a concert → re-identify the next song",
                         self._applause_for)
                self._last_ocr_t = 0.0                 # force an immediate banner read
                self._fast_calib = max(self._fast_calib, 2)
                self._start_identify(seconds=5, attempts=2)
            else:
                # single LIVE arrangement: mid-song applause → two-point resync (TICKET-061)
                log.info("applause gap (~%.1fs) ended, vocals back → two-point resync by ear",
                         self._applause_for)
                self._align_tpvr, self._align_tpvr_active = None, True
                self._align_tpvr_until = now + 14.0
                self.align_by_listening(silent=True)
        else:
            self._applause_for = max(0.0, self._applause_for - dt)

    def align_by_listening(self, silent=False, seconds=None):
        """On-demand: transcribe a few seconds of the live vocals and match them
        to the loaded lyrics to set the sync offset — fixes timing when Shazam
        can't identify the exact cut. Opt-in, runs once in a background thread,
        and no-ops gracefully if faster-whisper isn't installed.

        ``silent=True`` suppresses hints for the background auto-align that runs
        periodically — the user shouldn't see "Listening to sync…" every minute
        when nothing's wrong. ``seconds`` overrides the capture length (the live
        concert resync uses a shorter clip to cycle faster)."""
        if self._aligning:
            return
        try:
            import align
            ok, err = align.available(), align._last_error
        except Exception as e:
            ok, err = False, str(e)
        if not ok:
            if not silent:
                self._hint("Sync-by-listening needs faster-whisper — see the README")
            log.info("align requested but faster-whisper not available: %s", err)
            return
        if not self.lines:
            if not silent:
                self._hint("Play a recognised song first, then sync by listening")
            return
        self._aligning = True
        self._auto_align_silent = silent
        if not silent:
            self._hint("🎧 Listening to sync the lyrics…")
        lines, lang = self.lines, self.meta.get("lang", "ja")
        cap_s = seconds

        def work():
            res = None
            try:
                kw = {"get_pos": self._align_pos}
                if cap_s:
                    kw["seconds"] = cap_s
                res = align.capture_and_align(lines, lang=lang, **kw)
            except Exception as e:
                log.info("align error: %s", e)
            self.root.after(0, lambda: self._apply_align(res))

        threading.Thread(target=work, daemon=True).start()

    # ── FORCE SYNC — the manual "nuclear" resync ──────────────────────────────
    def force_sync(self):
        """FORCE SYNC — the manual "nuclear" resync for the "right song, stubborn
        timing" case the background auto-sync won't catch.

        It resets the offset to 0 as a clean baseline, then repeatedly transcribes
        the live vocals and matches them to the loaded lyrics. Crucially it does NOT
        trust one match: each read yields a RANKED list of candidate offsets (a
        recurring chorus hook legitimately matches several timestamps), and it tries
        them best→next. A candidate has to keep matching FRESH reads — spanning
        enough of the song to clear a whole chorus pass — before it locks. One that
        stops lining up (a "chorus trap" the lyrics then run past) is blacklisted and
        the next candidate is tried. Manual, opt-in; needs the AI add-on."""
        if not self.lines:
            self._hint("Play a recognised song first, then Force Sync")
            return
        try:
            import align
            if not align.available():
                self._hint("Force Sync needs the AI add-on (faster-whisper) — see the README")
                return
        except Exception:
            self._hint("Force Sync needs the AI add-on (faster-whisper)")
            return
        self._fine_exit("force-sync")              # force-sync owns the offset cleanly
        log.info("FORCE SYNC engaged: offset → 0, then try ranked matches until one holds %d× over %.0fs",
                 int(self._tune.get("force_sync_streak", 3)),
                 float(self._tune.get("force_sync_span_s", 16.0)))
        self.offset = 0.0                       # set sync timing to 0 as the first resort
        # TICKET-088: snap the EASED display offset in parallel so _eased_offset
        # doesn't try to glide from the previous offset to 0 — the user just
        # asked for a nuclear resync, the glide here would look like a snap
        # anyway (huge delta) and the per-frame fraction cap would draw the
        # ramp out over many frames. Set both to the same value atomically.
        self._display_offset = 0.0
        self._display_offset_t = time.time()
        self.idx = -1
        # TICKET-090: a manual Force Sync means the user is overruling our current
        # belief — drop the title-lock so the decide loop can re-engage and the
        # sound-driven correction paths aren't blocked by a stale ground-truth claim.
        self._title_locked = False
        self._force_sync_active = True
        self._fs_current = None
        self._fs_confirms = 0
        self._fs_misses = 0
        self._fs_line_lo = self._fs_line_hi = None
        self._fs_blacklist = []
        self._fs_tries = 0
        self._fs_empties = 0
        self._hint("🚀 Force Sync — listening for the right timing…")
        self._force_sync_tick()

    def _force_sync_tick(self):
        if not self._force_sync_active or not self.lines:
            self._force_sync_active = False
            return
        st = self.media.get()
        if not (st and st.get("status") == PLAYING) or self._aligning:
            self._force_sync_after = self.root.after(1200, self._force_sync_tick)  # paused/busy → wait
            return
        self._aligning = True
        self._fs_tries += 1
        lines, lang = self.lines, self.meta.get("lang", "ja")
        secs = float(self._tune.get("force_sync_listen_s", 8.0))
        top_n = int(self._tune.get("force_sync_top_n", 6))
        pos0 = self._align_pos()                 # capture-start position (drives the confirm span)

        def work():
            ranked = []
            try:
                import align
                ranked = align.rank_offsets(lines, lang=lang, get_pos=self._align_pos,
                                            seconds=secs, top_n=top_n) or []
            except Exception as e:
                log.info("force-sync listen error: %s", e)
            self.root.after(0, lambda: self._force_sync_apply(ranked, pos0))

        threading.Thread(target=work, daemon=True).start()

    def _force_sync_apply(self, ranked, pos0):
        self._aligning = False
        if not self._force_sync_active:
            return
        need   = int(self._tune.get("force_sync_streak", 3))
        agree  = float(self._tune.get("force_sync_agree_s", 1.0))
        span_s = float(self._tune.get("force_sync_span_s", 16.0))
        # Drop candidates sitting on a known chorus-trap offset.
        cands = [(o, r, ls) for (o, r, ls) in (ranked or [])
                 if not any(abs(o - b) <= agree for b in self._fs_blacklist)]

        if not cands:                            # silence / instrumental / no confident match
            self._fs_empties += 1
            tag = f"{self._fs_current:+.1f}s" if self._fs_current is not None else f"{self.offset:+.1f}s"
            log.info("force-sync: no usable match (try %d, empty %d) — holding %s",
                     self._fs_tries, self._fs_empties, tag)
            self._hint(f"🚀 Force Sync — listening… ({tag})")
            self._force_sync_after = self.root.after(1300, self._force_sync_tick)
            return
        self._fs_empties = 0

        if self._fs_current is None:             # ── pick the first (highest-probability) candidate
            off, ratio, _ls = cands[0]
            self._fs_current = round(off, 2)
            self._fs_confirms = 1
            self._fs_misses = 0
            self._fs_line_lo = self._fs_line_hi = pos0
            self.offset = self._fs_current; self.idx = -1
            log.info("force-sync: try #1 %+.2fs (match %.2f, %d cands)", off, ratio, len(cands))
            self._hint(f"🚀 Force Sync — trying {off:+.1f}s…")
            self._force_sync_after = self.root.after(1000, self._force_sync_tick)
            return

        # ── verify the current candidate against this FRESH read ──
        match = next(((o, r) for (o, r, _ls) in cands
                      if abs(o - self._fs_current) <= agree), None)
        if match:                                # it STILL lines up here → confirm
            self._fs_current = round(match[0], 2)   # follow small drift
            self._fs_confirms += 1
            self._fs_misses = 0
            self._fs_line_lo = min(self._fs_line_lo, pos0)
            self._fs_line_hi = max(self._fs_line_hi, pos0)
            self.offset = self._fs_current; self.idx = -1
            span = max(0.0, self._fs_line_hi - self._fs_line_lo)
            log.info("force-sync: %+.2fs still matches — confirm %d/%d, span %.0f/%.0fs",
                     self._fs_current, self._fs_confirms, need, span, span_s)
            if self._fs_confirms >= need and span >= span_s:
                self._force_sync_active = False
                log.info("FORCE SYNC LOCKED at %+.2fs (%d confirms over %.0fs)",
                         self.offset, self._fs_confirms, span)
                self._hint(f"✅ Force Sync locked ({self.offset:+.1f}s)")
                return
            self._hint(f"🚀 Force Sync {self._fs_confirms}/{need} ({self.offset:+.1f}s)…")
            self._force_sync_after = self.root.after(1000, self._force_sync_tick)
            return

        # ── current candidate did NOT line up here ──
        self._fs_misses += 1
        if self._fs_misses < 2:                  # one noisy read → hold, give it another chance
            log.info("force-sync: %+.2fs missed once — holding (grace), confirms stay %d",
                     self._fs_current, self._fs_confirms)
            self._hint(f"🚀 Force Sync {self._fs_confirms}/{need} ({self.offset:+.1f}s)…")
            self._force_sync_after = self.root.after(1000, self._force_sync_tick)
            return
        # missed twice → the lyrics have run past it: it was a trap. Blacklist & advance.
        bad = self._fs_current
        self._fs_blacklist.append(bad)
        nxt = cands[0]                           # freshest match at the song's CURRENT spot
        self._fs_current = round(nxt[0], 2)
        self._fs_confirms = 1
        self._fs_misses = 0
        self._fs_line_lo = self._fs_line_hi = pos0
        self.offset = self._fs_current; self.idx = -1
        log.info("force-sync: %+.2fs ran past (chorus trap) — blacklisted; now trying %+.2fs (match %.2f)",
                 bad, nxt[0], nxt[1])
        self._hint(f"🚀 Force Sync — {bad:+.1f}s missed, trying {self._fs_current:+.1f}s…")
        self._force_sync_after = self.root.after(1000, self._force_sync_tick)

    def _track_start_auto_align(self, track_seq):
        """Fires once ~25 s into a new track, IF this is still that track. A
        quick early sync-by-ear catches cuts Shazam can't fingerprint right
        when the song settles in, instead of waiting for the periodic loop."""
        if track_seq != self._track_seq:
            return
        self._maybe_auto_align(reason="track-start")

    def _live_resync_loop(self):
        """LIVE / concert arrangements drift — applause pauses, tempo shifts and live
        edits mean the studio LRC timing won't hold and Shazam can't fingerprint the
        (often MMD) cut. So for a registered live arrangement we RESYNC BY EAR on a
        ROLLING, aggressive cadence: ~8×/min while the song is fresh or a read just
        MISSED, relaxing to ~5×/min after 3 good reads in a row and ~3×/min after 6 —
        any miss snaps straight back to ~8×/min (`_note_live_resync`). Each listen
        FOLLOWS the measured live offset instead of trusting the studio timing, and is
        waveform-gated so it never lands on an instrumental / applause gap. No-op
        unless a live version has lyrics loaded and nothing else is listening."""
        try:
            if ((self._live_arrangement or self._live_mode) and self.lines
                    and not self._aligning and not self._deciding
                    and (self.meta.get("source") or "") != "youtube-captions"):
                st = self.media.get()
                # WAVEFORM GATE: only spend a transcription when the audio waveform
                # shows VOCALS are actually active right now — transcribing an
                # instrumental break / applause gap just yields an empty or garbage
                # transcript that can't resync. The vocal-band energy analysis tells
                # us when singing is happening, so the listen lands on real lyrics.
                if (st and st.get("status") == PLAYING and float(st.get("position") or 0.0) > 8
                        and self._vocals_active_now()):
                    try:
                        import align
                        if align.available():
                            log.info("live resync: vocals active → following live timing (good-streak %d)",
                                     self._live_sync_streak)
                            self._live_resync_inflight = True
                            self.align_by_listening(
                                silent=True,
                                seconds=float(self._tune.get("live_resync_listen_s", 6.0)))
                    except Exception:
                        self._live_resync_inflight = False
        finally:
            gap = self._live_resync_gap
            if gap is None:
                gap = float(self._tune.get("live_resync_fast_gap_s", 1.5))
            self._live_resync_after = self.root.after(int(max(1.0, gap) * 1000),
                                                      self._live_resync_loop)

    def _note_live_resync(self, ok):
        """Verdict from a live/concert resync → roll the cadence. ``live_resync_relax_n``
        (3) good reads in a row relax one step (8×/min → 5×/min → 3×/min); ANY miss
        resets the streak and snaps back to 8×/min, so a drifting concert is hammered
        until it re-locks, then backs off once it's holding."""
        fast = float(self._tune.get("live_resync_fast_gap_s", 1.5))
        mid  = float(self._tune.get("live_resync_mid_gap_s", 6.0))
        slow = float(self._tune.get("live_resync_slow_gap_s", 14.0))
        step = max(1, int(self._tune.get("live_resync_relax_n", 3)))
        if ok:
            self._live_sync_streak += 1
            self._live_resync_gap = (slow if self._live_sync_streak >= 2 * step
                                     else mid if self._live_sync_streak >= step
                                     else fast)
        else:
            self._live_sync_streak = 0
            self._live_resync_gap = fast
        listen = float(self._tune.get("live_resync_listen_s", 6.0))
        log.info("live resync %s → good-streak %d, next gap %.1fs (~%.0f×/min)",
                 "OK" if ok else "miss", self._live_sync_streak, self._live_resync_gap,
                 60.0 / max(0.1, self._live_resync_gap + listen))

    # ── SMART song decision by ear (Whisper 'small' + rapidfuzz) ──────────────
    #
    # The robust answer to "what song should we show lyrics to?" when the usual
    # signals fail — an MMD / cover / "Performance Video" Shazam can't fingerprint,
    # or a MISLABELED provider LRC (feelingradation got a different song's LRC). It
    # transcribes a few seconds of the actual vocals and picks the candidate whose
    # LYRICS best match what's being sung, switching if the loaded song is wrong.

    @staticmethod
    def _lyric_text(lines):
        """Flatten a song's lines to one plain string for lyric-match scoring."""
        out = []
        for ln in lines or []:
            t = getattr(ln, "jp", None) if not isinstance(ln, dict) else ln.get("jp")
            if t:
                out.append(t)
        return " ".join(out)

    # ───────────────────────── TICKET-109 decision engine ─────────────────────────
    # Continuous wrong-lyric / desync detector. Every decision_tick_interval_s,
    # score 4 dimensions OK/DEGRADED/BAD (source-agreement, sync-stability,
    # lyric-quality, ear-corroboration), accumulate strikes, and promote state
    # TRUST -> CAUTION -> SWITCH -> REGEN at the threshold knobs. Each state has
    # a concrete action (do nothing / verify nudge / drop+refetch / drop+AI gen).
    # User asked for this: "tell me what happened and make a plan. this needs to
    # work algorithmically and without lyrics in repo."
    def _score_source_agree(self):
        """SMTC vs Shazam title/artist agreement. Cover-aware (skip on covers)."""
        if getattr(self, "_is_cover", False):
            return "OK"
        st = self.media.get() or {}
        smtc_t = _norm_title(st.get("title") or "")
        smtc_a = _norm_title(st.get("artist") or "")
        snd = getattr(self, "_sound_song", None)
        if not snd or not smtc_t:
            return "OK"
        s_title  = _norm_title(snd[0] or "")
        s_artist = _norm_title(snd[1] or "")
        if s_title == smtc_t and s_artist == smtc_a:
            return "OK"
        if s_title and (s_title in smtc_t or smtc_t in s_title):
            return "DEGRADED"
        return "BAD"

    def _score_sync_stable(self):
        drift = getattr(self, "_last_drift", None)
        if drift is None:
            return "OK"
        integral = abs(getattr(self, "_drift_integral", 0.0))
        if abs(drift) < 1.0 and integral < 4.0:  return "OK"
        if abs(drift) < 3.0 and integral < 12.0: return "DEGRADED"
        return "BAD"

    def _score_lyric_quality(self):
        if not self.lines:
            return "OK"
        bad = 0
        src = (self.meta.get("source") or "") if isinstance(self.meta, dict) else ""
        for ln in self.lines:
            t = getattr(ln, "jp", "") or ""
            if "�" in t or "□□" in t:
                bad += 1
            elif t.startswith("***") and src not in ("generated", "generated-deep", "ai-gen"):
                bad += 1
        ratio = bad / max(1, len(self.lines))
        if ratio >= 0.20: return "BAD"
        if ratio >= 0.05: return "DEGRADED"
        return "OK"

    def _score_ear_corrob(self):
        dec = getattr(self, "_last_decision", None)
        if not dec or (time.time() - dec.get("t", 0)) > 90:
            return "OK"
        ranked = dec.get("ranked") or []
        if not ranked:
            return "OK"
        loaded_path = getattr(self, "_lyrics_path", None)
        if not loaded_path:
            return "OK"
        loaded_name = Path(str(loaded_path)).name
        loaded_score = 0.0
        for entry in ranked:
            try:
                s, k = entry
            except Exception:
                continue
            if k == str(loaded_path) or Path(str(k)).name == loaded_name:
                loaded_score = float(s)
                break
        wrong_floor = float(self._tune.get("decide_wrong_floor", 32.0))
        if loaded_score >= 55.0:        return "OK"
        if loaded_score >= wrong_floor: return "DEGRADED"
        return "BAD"

    def _decide_state_from_strikes(self):
        s = self._decision_strikes
        if s >= int(self._tune.get("decision_regen_strikes",   8)): return "REGEN"
        if s >= int(self._tune.get("decision_switch_strikes",  5)): return "SWITCH"
        if s >= int(self._tune.get("decision_caution_strikes", 3)): return "CAUTION"
        return "TRUST"

    def _decision_engine_tick(self):
        if not int(self._tune.get("decision_engine_on", 1)):
            return
        now = time.time()
        if now - self._decision_last_t < float(
                self._tune.get("decision_tick_interval_s", 2.0)):
            return
        self._decision_last_t = now
        if not self.lines and not getattr(self, "_track", None):
            return
        dims = {
            "source_agree":  self._score_source_agree(),
            "sync_stable":   self._score_sync_stable(),
            "lyric_quality": self._score_lyric_quality(),
            "ear_corrob":    self._score_ear_corrob(),
        }
        for k, v in dims.items():
            self._decision_dim_scores[k] = v
            self._decision_dim_history[k].append(v)
        delta = sum(2 if v == "BAD" else 1 if v == "DEGRADED" else 0
                    for v in dims.values())
        if delta == 0:
            self._decision_strikes = max(0, self._decision_strikes - 2)
        else:
            self._decision_strikes += delta
        prev = self._decision_state
        new = self._decide_state_from_strikes()
        if new != prev:
            self._decision_audit.append({
                "t": now, "from": prev, "to": new,
                "dims": dict(dims), "strikes": self._decision_strikes,
            })
            self._decision_state = new
            self._fire_decision_action(new, dims)

    def _fire_decision_action(self, state, dims):
        now = time.time()
        log.info("decision-engine: %s -> %s (strikes=%d dims=%s)",
                 self._decision_audit[-1].get("from") if self._decision_audit else "?",
                 state, self._decision_strikes, dims)
        if state == "TRUST":
            return
        if state == "CAUTION":
            self._hint("🔎 Verifying current song…")
            try:
                self._set_verified(False, reason="decision-caution")
            except Exception:
                pass
            return
        cooldown = float(self._tune.get("decision_action_cooldown_s", 30.0))
        if now - self._decision_last_action_t < cooldown:
            return
        self._decision_last_action_t = now
        track = getattr(self, "_track", None)
        if state == "SWITCH" and track:
            artist, title = track
            self._hint(f"🔁 Switched to alternative lyric source for {title}…")
            if getattr(self, "_lyrics_path", None):
                try: Path(self._lyrics_path).unlink(missing_ok=True)
                except Exception: pass
                try: self.index.refresh()
                except Exception: pass
            self._fetch_key = None
            self._lyrics_path = None
            self.lines, self.idx, self._kara = [], -1, []
            self._start_fetch(artist, title, self._cur_duration,
                              cover=getattr(self, "_is_cover", False))
            self._decision_strikes = max(0, self._decision_strikes - 3)
        elif state == "REGEN" and track:
            self._hint("✨ Regenerating lyrics via AI…")
            self._fetch_key = None
            self._lyrics_path = None
            self.lines, self.idx, self._kara = [], -1, []
            self._force_ai_gen = True
            self._decision_strikes = 0

    def _reset_decision_engine(self):
        """TICKET-109: new track => engine forgets everything from the prior song."""
        self._decision_state = "TRUST"
        self._decision_strikes = 0
        self._decision_last_action_t = 0.0
        for dq in self._decision_dim_history.values():
            dq.clear()
        for k in self._decision_dim_scores:
            self._decision_dim_scores[k] = "OK"
        self._force_ai_gen = False
    # ─────────────────────── end TICKET-109 decision engine ───────────────────────

    def _decide_by_ear(self, track_seq, reason="track-start"):
        """Transcribe the live vocals and pick which candidate song's lyrics they
        match; switch if the loaded lyrics are the wrong song. Gated + one in
        flight; skipped for baked (authoritative) songs. For a concert wrapper
        (live_arrangement / live_mode / boundary-triggered) we proceed even with
        NO lyrics loaded — the SMTC track is the whole-concert container, so the
        whole-library scan inside is the only way to ID the song currently playing
        (TICKET-079)."""
        if track_seq != self._track_seq or self._deciding or self._aligning:
            return
        # TICKET-090: a Shazam-VERIFIED + title-LOCKED song is ground truth — the
        # decide loop can only hurt (a noisy/hallucinated transcript ranks the
        # wrong song and overrides good lyrics). Skip it unless the tune knob
        # asks for paranoia, or a caller forces it (e.g. the /decide API endpoint
        # the user invoked deliberately, or a /wrong/forcesync that just unlocked).
        forced = bool(getattr(self, "_decide_force_flag", False)
                      or reason in ("api", "force", "wrong", "boundary"))
        if (self._verified and self._title_locked and not forced
                and not int(self._tune.get("decide_after_verified", 0))):
            log.info("decide-by-ear (%s): SKIPPED — verified + title-locked (ground truth)", reason)
            return
        in_concert = (self._live_arrangement or self._live_mode
                      or reason == "boundary")
        if not in_concert and (self._live_mode or not self.lines):
            return
        if (self.meta.get("source") or "").startswith("bundled"):
            return                                    # baked = ground truth already
        if (self.meta.get("source") or "") == "youtube-captions":
            return                                    # caption lyrics are the video's own
        st = self.media.get()
        if not (st and st.get("status") == PLAYING):
            return
        if float(st.get("position") or 0.0) < self._tune.get("decide_at_s", 20.0) - 2:
            return
        try:
            import align
            if not align.available():
                return
        except Exception:
            return
        # candidate pool: the loaded song + title-similar library caches + the
        # Shazam-heard song (each as (path-or-key, plain lyric text)).
        pool, seen = [], set()
        loaded_key = str(self._lyrics_path) if self._lyrics_path else "loaded"
        pool.append((loaded_key, self._lyric_text(self.lines)))
        seen.add(loaded_key)
        try:
            for p in self.index.candidates(self._clean_title_cache, limit=5):
                k = str(p)
                if k in seen:
                    continue
                try:
                    d = json.loads(Path(p).read_text("utf-8"))
                except Exception:
                    continue
                pool.append((k, self._lyric_text(d.get("lines"))))
                seen.add(k)
        except Exception:
            pass
        # (a pool of just the loaded song is fine — a clearly-wrong loaded song is
        # then identified against the WHOLE library below.)
        self._deciding = True
        lang = self.meta.get("lang", "ja")
        secs = float(self._tune.get("decide_listen_s", 12.0))
        wrong_floor = float(self._tune.get("decide_wrong_floor", 32.0))
        lib_paths = [str(e["path"]) for e in self.index.entries][:600]
        log.info("decide-by-ear (%s): listening among %d title candidates "
                 "(whole library of %d ready if the loaded song is wrong)",
                 reason, len(pool), len(lib_paths))

        def work():
            res = None
            try:
                import align
                heard = align.transcribe_vocals(lang, seconds=secs)
                if heard:
                    ranked = align.score_candidates(heard, pool)
                    loaded_score = next((s for s, k in ranked if k == loaded_key), 0.0)
                    expanded = False
                    if loaded_score < wrong_floor:
                        # The loaded lyrics DON'T match the singing → identify against
                        # the WHOLE cached library (the model "trained on everything we
                        # have"): score the SAME transcript against every cached song.
                        libpool = []
                        for k in lib_paths:
                            if k == loaded_key:
                                continue
                            try:
                                d = json.loads(Path(k).read_text("utf-8"))
                            except Exception:
                                continue
                            libpool.append((k, self._lyric_text(d.get("lines"))))
                        if libpool:
                            ranked = sorted(align.score_candidates(heard, libpool)
                                            + [(loaded_score, loaded_key)], reverse=True)
                            expanded = True
                    res = {"heard": heard, "ranked": ranked, "expanded": expanded}
            except Exception as e:
                log.info("decide-by-ear error: %s", e)
            self.root.after(0, lambda: self._apply_decision(res, track_seq, loaded_key))

        threading.Thread(target=work, daemon=True).start()

    def _apply_decision(self, res, track_seq, loaded_key):
        self._deciding = False
        if not res or not res.get("ranked"):
            return
        expanded = bool(res.get("expanded"))
        self._last_decision = {
            "heard": res["heard"][:60],
            "scope": "library" if expanded else "title",
            "ranked": [(s, Path(k).name if k not in ("loaded",) else k)
                       for s, k in res["ranked"][:4]],
            "t": time.time(),
        }
        if track_seq != self._track_seq:
            return                                    # track changed mid-transcribe
        ranked = res["ranked"]
        best_score, best_key = ranked[0]
        loaded_score = next((s for s, k in ranked if k == loaded_key), 0.0)
        log.info("decide-by-ear[%s]: heard %r → best %s (%.0f) vs loaded (%.0f)",
                 "library" if expanded else "title", res["heard"][:40],
                 Path(best_key).name if best_key not in ("loaded",) else best_key,
                 best_score, loaded_score)
        # TICKET-081: a very short transcript (e.g. 11 chars in the 名前のない怪物
        # cover case) can produce a deceptive tie at low score that the matcher
        # logs as "in sync" while the song is actually way off. Treat short
        # transcripts as inconclusive — don't switch, don't claim alignment.
        heard_text = res.get("heard") or ""
        if len(heard_text.strip()) < 20:
            log.info("decide-by-ear: only %d chars heard — inconclusive, no action",
                     len(heard_text.strip()))
            return
        # A LIBRARY-WIDE identification (broad search over every cached song) must
        # clear a HIGHER bar than a title-confined check, so a short transcript can't
        # latch onto a stray song among hundreds.
        MIN = (self._tune.get("decide_library_min", 60.0) if expanded
               else self._tune.get("decide_min_score", 55.0))
        MARGIN = self._tune.get("decide_margin", 12.0)
        # TICKET-081: when SMTC artist is known and the best library candidate's
        # artist clearly disagrees with it, penalize best_score by -8 BEFORE the
        # MIN/MARGIN compare — a transcript-only switch should hesitate when the
        # candidate is by the wrong artist. Skipped for covers (a cover's original
        # artist is DIFFERENT by design).
        if expanded and best_key not in ("loaded", loaded_key) and not getattr(self, "_is_cover", False):
            smtc_artist_n = _norm_title(getattr(self, "_clean_artist_cache", "") or "")
            best_artist_n = ""
            try:
                for ent in self.index.entries:
                    if str(ent["path"]) == best_key:
                        best_artist_n = _norm_title(ent.get("artist") or "")
                        break
            except Exception:
                pass
            if smtc_artist_n and best_artist_n and smtc_artist_n != best_artist_n:
                if not (smtc_artist_n in best_artist_n or best_artist_n in smtc_artist_n):
                    log.info("decide-by-ear: best candidate's artist %r ≠ SMTC artist %r "
                             "— penalizing best_score by 8", best_artist_n, smtc_artist_n)
                    best_score -= 8.0
        # GUARD: a title-LOCKED song came from a confident title match — strong ground
        # truth. Require an OVERWHELMING library match before a by-ear read may override
        # it, so one noisy/short clip can't switch away from the right song (the Suisei
        # 綺麗事 → Tip Taps Tip case — the hallucination filter is the primary fix; this
        # is defense-in-depth for any garbage transcript that still slips through).
        if getattr(self, "_title_locked", False) and loaded_key not in ("loaded",):
            MIN += self._tune.get("decide_titlelock_bump", 15.0)
            MARGIN = max(MARGIN, self._tune.get("decide_titlelock_margin", 28.0))
        # TICKET-080: "lopsided win" override — when the loaded lyrics are clearly
        # wrong (score < wrong_floor) AND the best library candidate has a wildly
        # bigger margin (>= 3× the usual), accept it even if it's just under MIN.
        # The kamone case (69 vs 20, library MIN=70) lost the right song by 1 point
        # of an absolute threshold while the margin was 49 — a clear net win that
        # the strict gate was throwing away in favor of a slow re-fetch.
        wrong = self._tune.get("decide_wrong_floor", 32.0)
        lopsided = (expanded and loaded_score < wrong
                    and best_score >= max(50.0, MIN - 10.0)
                    and best_score - loaded_score >= 3.0 * MARGIN)
        if (best_key not in ("loaded", loaded_key)
                and ((best_score >= MIN and best_score - loaded_score >= MARGIN)
                     or lopsided)):
            try:
                p = Path(best_key)
                if p.exists() and self._file_valid(p, self._cur_duration):
                    log.info("decide-by-ear: SWITCHING to %s — its lyrics match the "
                             "singing far better (%.0f vs %.0f)", p.name, best_score, loaded_score)
                    self._fine_exit("song-switch")   # fresh lyrics have no streak yet
                    self.load(p)
                    self._maybe_translate()
                    self._sound_title_alias = None
                    self.offset = 0.0
                    self.idx = -1
                    self._hint("🎯 Corrected to the song being sung")
                    # FUSE waveform + transcript: the transcript picked the SONG; now
                    # let the vocal-energy WAVEFORM correlation pin the precise OFFSET
                    # for the freshly-loaded lyrics (the transcript gives WHAT, the
                    # waveform gives exactly WHEN). Runs shortly after the load settles.
                    self.root.after(700, lambda: self._auto_align_by_energy("post-decide"))
                    return
            except Exception as e:
                log.info("decide-by-ear: switch failed: %s", e)
        if loaded_score < self._tune.get("decide_wrong_floor", 32.0) and best_score < MIN:
            # loaded matches the singing POORLY and NOTHING cached fits → the right
            # song isn't in the library yet. Fetch fresh (qualified by the cover's
            # original artist when it's a cover, so a generic title finds the right one).
            log.info("decide-by-ear: loaded doesn't match the singing (%.0f) and no library "
                     "song fits → re-fetching %r", loaded_score, self._clean_title_cache)
            if self._track:
                art = (self._cover_original_artist if self._is_cover
                       else self._clean_artist_cache) or ""
                self._hint("🎯 Wrong lyrics — re-identifying…")
                self._start_fetch(art, self._clean_title_cache, self._cur_duration,
                                  cover=self._is_cover)

    def _maybe_auto_align(self, reason="periodic"):
        """Background, automatic sync-by-listening. Runs only when conditions are
        right: lyrics loaded, song playing, no other alignment in flight, not in
        live mode (whole event), and Shazam hasn't locked the offset very
        recently. Cheap to call — no-ops when conditions aren't met."""
        if self._aligning or not self.lines or self._live_mode:
            return
        # Fine-tune mode owns the sync cadence and applies sub-second pauses
        # instead of nudging the offset. Letting the energy correlator nudge
        # `_smooth_offset` in parallel races with the pause-end subtraction.
        # The MV-intro fast-sync still gets through (reason='mv-intro-onset').
        if getattr(self, "_fine_active", False) and reason not in ("mv-intro-onset",):
            return
        # YouTube captions are already locked to the video's own timing — running
        # the energy correlator against them is wasted CPU (and risks nudging a
        # perfect sync). Skip it for caption-sourced lyrics.
        if (self.meta.get("source") or "") == "youtube-captions":
            return
        st = self.media.get()
        if not (st and st.get("status") == PLAYING):
            return
        now = time.time()
        # Don't run if Shazam locked the offset recently — its confirmation
        # is more authoritative than re-checking by ear. A fresh authoritative
        # lock IS an in-sync confirmation, so let the adaptive tier de-escalate.
        if reason != "drift" and now - self._last_sound_lock_t < self._tune["shazam_lock_grace"]:
            self._note_sync_verdict("insync")
            return
        # Don't run within the cooldown of the previous auto-align (CPU budget).
        if now - self._last_align_t < self._tune["auto_align_cooldown"]:
            return
        # Need to be reasonably into the track so there are vocals to match.
        vpos = float(st.get("position") or 0.0)
        if vpos < self._tune["auto_align_min_pos"]:
            return
        self._last_align_t = now
        # AUTOMATIC sync ALWAYS uses the cheap vocal-energy correlation. Whisper
        # transcription is HEAVY (~1-2 s of 100% on a core) and running it on ANY
        # automatic path — periodic OR drift-triggered — stuttered the scroll AND
        # the audio (render fell to ~22 fps, worst-frame 492 ms). Whisper stays
        # reserved for the EXPLICIT "Sync by listening" button (align_by_listening,
        # which doesn't go through here) and the last-resort deep transcription.
        log.info("auto-align (%s) — checking sync by energy correlation", reason)
        self._auto_align_by_energy(reason)

    def _auto_align_by_energy(self, reason):
        """Whisper-free sync correction: cross-correlate the recent audio's
        vocal-band energy mask against the LRC's expected line-active intervals.
        The offset that maximizes overlap is the drift correction.

        Works on karaoke / off-vocal / live cuts that Shazam can't fingerprint
        AND when faster-whisper isn't installed. Conservative: requires a clear
        correlation peak (best score > 1.5x the runner-up's neighborhood mean)
        before applying, so noisy or sparse vocals don't yank the offset."""
        if not self._boundary or not self.lines:
            return
        st = self.media.get()
        if not (st and st.get("status") == PLAYING):
            return
        try:
            history = self._boundary.vocal_history(30.0)
        except Exception:
            return
        if len(history) < 60:    # need at least 12 s of buffer
            log.info("auto-align: not enough vocal-mask history (%d blocks)", len(history))
            return

        self._energy_reason = reason
        def work():
            try:
                self._aligning = True
                self._run_energy_correlation(history, st)
            except Exception as e:
                log.info("energy-align error: %s", e)
            finally:
                self._aligning = False

        threading.Thread(target=work, daemon=True).start()

    def _run_energy_correlation(self, history, st_snap):
        """The actual correlation work (runs in a background thread)."""
        import numpy as np
        # Report this check's verdict to the adaptive tier (on the Tk thread). A
        # clear in-tolerance peak = "insync"; a flat/ambiguous/rejected read =
        # "inconclusive" (which, while syncing, escalates the tier to a Whisper
        # listen). The applied-correction path reports "corrected" from _apply_*.
        def verdict(v):
            self.root.after(0, lambda: self._note_energy_verdict(v))
        # The history is a list of (t_wall, vocal_ratio). Each entry is one
        # 0.2 s block. Map wall-time → song-time using the player position
        # at the time of the latest entry: that entry corresponds to NOW, and
        # NOW's song-time is st_snap["position"] (plus current offset, since
        # we're aligning the displayed lyric time, not the absolute clock).
        now_wall = history[-1][0]
        now_song = float(st_snap.get("position") or 0.0)
        # Build the audio mask: per-block "are vocals active right now?".
        # ADAPTIVE threshold (per-window), not an absolute floor: the vocal-band
        # ratio's absolute level varies hugely by song (bass-heavy electronic vs
        # piano ballad), so a fixed 0.50 floor either missed quiet vocals or
        # flagged everything. Instead split at the window's own median + half the
        # upper spread — vocals (higher band ratio) rise above it, instrumental
        # stays below — so the on/off PATTERN is captured on any song. (The
        # absolute baseline still guards against an all-instrumental window.)
        block_dt = 0.2
        ratios = np.array([r for (_, r) in history])
        med = float(np.median(ratios))
        hi = float(np.percentile(ratios, 75))
        spread = hi - med
        thresh = med + 0.5 * spread
        audio_t = np.array([now_song - (now_wall - t) for (t, _) in history])
        audio_v = (ratios >= thresh).astype(float)
        # Need genuine on/off CONTRAST: enough vocal blocks AND enough silent
        # blocks. A near-constant mask (all-vocal or all-silent) has no pattern
        # to align and would match any shift equally (flat agreement → no info).
        n_on = int(audio_v.sum())
        n_off = int(len(audio_v) - n_on)
        if spread < 0.02 or n_on < 6 or n_off < 6:
            log.info("energy-align: low vocal contrast (on=%d off=%d spread=%.3f) — no change",
                     n_on, n_off, spread)
            verdict("inconclusive")
            return
        # Build the LRC active-mask on a fixed 0.2 s grid spanning the audio
        # window ±16 s (room for the ±15 s shift search), then evaluate ALL
        # candidate shifts at once with a single vectorized gather. The old
        # 151-iteration Python loop (searchsorted per shift) held the GIL long
        # enough on a background thread to STUTTER the scroll belt — this
        # numpy-only version runs the whole search in C in microseconds.
        lines = self.lines
        starts = np.array([ln.start for ln in lines])
        ends = np.array([max(ln.end, ln.start + 1.0) for ln in lines])
        g0 = float(np.floor(audio_t.min() - 16.0))
        g1 = float(np.ceil(audio_t.max() + 16.0))
        ngrid = int(round((g1 - g0) / block_dt)) + 1
        if ngrid < 8 or ngrid > 100000:
            log.info("energy-align: grid size %d out of range — skipped", ngrid)
            verdict("inconclusive")
            return
        grid_t = g0 + np.arange(ngrid) * block_dt
        li = np.searchsorted(starts, grid_t, side="right") - 1
        lrc_grid = np.zeros(ngrid)
        gv = (li >= 0) & (li < len(lines))
        lrc_grid[gv] = (grid_t[gv] <= ends[li[gv]]).astype(float)
        # grid index of each audio block at shift 0
        base = np.round((audio_t - g0) / block_dt).astype(int)
        S = np.arange(-75, 76)                       # ±15 s in 0.2 s steps
        idx = base[None, :] + S[:, None]             # (151, nblocks)
        inrange = (idx >= 0) & (idx < ngrid)
        lrc_vals = np.where(inrange, lrc_grid[np.clip(idx, 0, ngrid - 1)], 0.0)
        # agreement per shift = fraction of blocks where audio mask == LRC mask
        agree = (audio_v[None, :] == lrc_vals).sum(axis=1) / float(len(audio_v))
        shifts_s = S * block_dt                          # candidate offset CHANGE (s)
        # SMALL-SHIFT PRIOR: a repetitive song (la-la-la chorus) produces an
        # equally-tall agreement peak one chorus away — the bug where the offset
        # jumped -14.8 s onto a chorus. The TRUE offset rarely jumps far between
        # checks (continuity), so penalize large offset changes: a far shift must
        # beat the no-change score by penalty·|shift| to win. This makes the
        # correlator prefer "keep the current sync" unless the evidence is
        # overwhelming. (Score-following literature calls this a transition prior.)
        penalty = self._tune.get("energy_shift_penalty", 0.012)
        scored = agree - penalty * np.abs(shifts_s)
        best_i = int(np.argmax(scored))
        best_shift = float(shifts_s[best_i])
        best_score = float(agree[best_i])
        median = float(np.median(agree))
        peak_lift = best_score - median
        # PEAK UNIQUENESS: mask a ±2 s window around the winner and find the next
        # best raw-agreement peak elsewhere. If a DISTANT shift scores almost as
        # high, the match is ambiguous (chorus repetition) → don't trust it.
        win = 10                                         # ±2 s in 0.2 s steps
        lo, hi = max(0, best_i - win), min(len(S), best_i + win + 1)
        masked = agree.copy()
        masked[lo:hi] = -1.0
        rival_i = int(np.argmax(masked))
        rival_score = float(masked[rival_i])
        rival_shift = float(shifts_s[rival_i]) if rival_score >= 0 else None
        margin = self._tune.get("energy_peak_margin", 0.06)
        ambiguous = rival_score >= 0 and (best_score - rival_score) < margin
        self._last_energy = {
            "best_shift": round(best_shift, 2), "best_score": round(best_score, 3),
            "median": round(median, 3), "lift": round(peak_lift, 3),
            "rival_shift": round(rival_shift, 2) if rival_shift is not None else None,
            "rival_score": round(rival_score, 3) if rival_score >= 0 else None,
            "ambiguous": bool(ambiguous),
            "blocks": int(len(audio_v)), "vocal_blocks": int(audio_v.sum()),
            "reason": getattr(self, "_energy_reason", "?"),
            "t": time.time(),
        }
        if peak_lift < self._tune["energy_lift_floor"]:
            log.info("energy-align: weak peak (best %.3f, median %.3f, lift %.3f) — no change",
                     best_score, median, peak_lift)
            verdict("inconclusive")
            return
        if ambiguous:
            log.info("energy-align: ambiguous — best %+.1fs (%.3f) vs rival %+.1fs (%.3f), "
                     "margin %.3f < %.3f → no change (chorus repetition)",
                     best_shift, best_score, rival_shift, rival_score,
                     best_score - rival_score, margin)
            verdict("inconclusive")
            return
        # best_shift is the offset to ADD to audio_t so the audio mask aligns
        # to the LRC. Since the displayed song-time = player_pos + self.offset,
        # the new offset becomes (current offset + best_shift).
        new_off = round(self.offset + best_shift, 2)
        if abs(new_off) > self._tune["energy_max_offset"]:
            log.info("energy-align: candidate offset %+.1fs out of range — skipped", new_off)
            verdict("inconclusive")
            return
        if abs(new_off - self.offset) < self._tune["energy_apply_min"]:
            log.info("energy-align: drift %+.2fs within tolerance — no change",
                     new_off - self.offset)
            verdict("insync")          # a clear peak AT the current offset = confirmed in sync
            return
        # SANITY CHECK against Shazam: songs with repeated patterns (la-la-la
        # choruses, repetitive hooks) produce sharp correlation peaks at MULTIPLE
        # shift candidates — picking a chorus-repetition match instead of the
        # true offset is what made Oblivion's offset jump to -14.37 in one
        # go. If Shazam has read the offset recently, reject correlation
        # candidates that differ from it by more than this band.
        if (self._last_audio_off is not None
                and time.time() - self._last_audio_off_t < 60.0):
            disagreement = abs(new_off - self._last_audio_off)
            if disagreement > 4.0:
                log.info("energy-align: candidate %+.2fs disagrees with Shazam %+.2fs "
                         "(Δ=%.1fs) — likely chorus-repetition match, rejected",
                         new_off, self._last_audio_off, disagreement)
                verdict("inconclusive")
                return
        self.root.after(0, lambda: self._apply_energy_align(new_off, best_score, peak_lift))

    def _apply_energy_align(self, new_off, score, lift):
        # Confidence-weighted update: a sharp correlation peak (high `lift`)
        # snaps to the measurement; a marginal peak blends conservatively
        # toward it via EMA. Avoids yanking the offset on noisy detections
        # while still converging quickly when the signal is strong.
        # alpha=1.0 when lift≥0.30, falls linearly to 0.3 at the 0.10 floor.
        alpha = max(0.3, min(1.0, (lift - 0.10) / 0.20 + 0.3))
        prev = self.offset
        blended = round((1.0 - alpha) * prev + alpha * new_off, 2)
        log.info("energy-align: offset %+.2fs → %+.2fs (α=%.2f, score %.3f, lift %.3f)",
                 prev, blended, alpha, score, lift)
        self._smooth_offset(blended, "energy-align")
        self._hint(f"🎤 Auto-synced ({blended:+.1f}s)")
        self._note_energy_verdict("corrected")

    # ── ADAPTIVE sync-verification tier (escalation / de-escalation) ──────────
    #
    # The user's model: while syncing, verify by ear at least 3×/min; once a check
    # CONFIRMS we're in sync, relax to 1×/min; ANY miss resyncs and snaps back to
    # 3×/min, staying fast while misses continue. Each correction is two-point
    # verified so a chorus-matched mis-read can't yank the lyrics. The cheap energy
    # correlator gives the verdict when it reads a clear peak; only when it goes
    # blind on a song (flat/ambiguous — the off-vocal ReGLOSS 'サクラミラージュ'
    # case) does the tier escalate to a short Whisper listen for the verdict.

    def _note_sync_verdict(self, verdict):
        """Drive the adaptive verify cadence from one check's outcome.
        verdict ∈ {"insync", "corrected", "inconclusive"}:
          • insync       → success: step the cadence DOWN toward 1×/min.
          • corrected    → a miss we fixed: snap to 3×/min and stay fast while missing.
          • inconclusive → no reliable reading: leave the cadence unchanged."""
        fast = self._tune.get("sync_tier_fast_s", 20.0)
        mid  = self._tune.get("sync_tier_mid_s", 40.0)
        slow = self._tune.get("sync_tier_slow_s", 60.0)
        # FINE-TUNE: when active, OWN the cadence. The 8 s fine-tune listen IS the
        # check; letting the tier also re-arm a 40-60 s Whisper read on top would
        # race for the _tier_listen lock. We still track verdict-driven entry-gate
        # accumulation below.
        fine_owns = self._fine_active
        if verdict == "insync":
            self._sync_good_streak += 1
            self._sync_miss_streak = self._sync_incon_streak = 0
            if not fine_owns:
                # one good check halves the rate; a second relaxes fully to 1×/min
                self._sync_tier_interval = mid if self._sync_good_streak < 2 else slow
            # FINE-TUNE entry-gate accumulation: track wall-time spent in good streak.
            if self._fine_good_t0 is None:
                self._fine_good_t0 = time.time()
            self._maybe_enter_fine_tune()
        elif verdict == "corrected":
            self._sync_miss_streak += 1
            self._sync_good_streak = self._sync_incon_streak = 0
            self._fine_good_t0 = None              # any miss resets the entry clock
            if not fine_owns:
                self._sync_tier_interval = fast    # escalate; keep fast while missing
        else:
            # inconclusive = couldn't get a reading (NOT a detected desync). A song we
            # can't verify must NOT be hammered at 3×/min — that only costs CPU (and
            # risks a stutter) for nothing. Back the cadence off one notch after a
            # blind check (TICKET batch1: threshold 2 → 1 — the periodic-JP-track
            # tier-deadlock case sat in fast tier for 90 s on streak=1, so we no
            # longer wait for a 2nd inconclusive before relaxing).
            self._fine_good_t0 = None              # blind = streak interrupted; restart clock on next insync
            self._sync_incon_streak += 1
            if self._sync_incon_streak < 1:
                return                              # (dead branch after threshold lowering — kept for shape)
            self._sync_incon_streak = 0
            if not fine_owns:
                self._sync_tier_interval = mid if self._sync_tier_interval <= fast else slow
        log.info("sync-tier: %s → verify every %.0fs (good=%d miss=%d)%s",
                 verdict, self._sync_tier_interval, self._sync_good_streak,
                 self._sync_miss_streak,
                 " [fine-tune owns]" if fine_owns else "")

    def _note_energy_verdict(self, verdict):
        """Energy-correlator verdict: tracks the consecutive-blind streak (so the
        tier knows when to escalate to Whisper) then feeds the shared tier logic."""
        self._energy_blind = self._energy_blind + 1 if verdict == "inconclusive" else 0
        self._note_sync_verdict(verdict)

    def _periodic_auto_align(self):
        """Adaptive sync-verification heartbeat (the escalation/de-escalation tier).

        Reschedules itself at the CURRENT tier cadence (fast ≈3×/min while syncing,
        relaxing to 1×/min once sync is confirmed). Each tick runs the cheap energy
        correlation for a verdict; if energy has gone blind on this song while we
        still need to confirm sync (fast tier), it escalates to a short Whisper
        listen instead, so the heavy transcription runs only when it's the only
        thing that can judge sync."""
        try:
            mid = self._tune.get("sync_tier_mid_s", 40.0)
            fast_tier = self._sync_tier_interval <= mid
            if self._force_sync_active:
                pass                                # Force Sync owns the offset — don't fight it
            elif (self._energy_blind >= 1 and fast_tier and not self._live_mode
                    and not self._aligning and not self._tier_listen):
                self._tier_listen_now()             # energy can't read this song → use ears
            else:
                self._maybe_auto_align(reason="periodic")
        finally:
            nxt = int(max(8.0, self._sync_tier_interval) * 1000)
            self._auto_align_after = self.root.after(nxt, self._periodic_auto_align)

    def _tier_listen_now(self):
        """Whisper-based tier verification, used when the energy correlator is blind.
        Transcribes a few seconds and matches to the LRC; the result is judged (and,
        for a real miss, two-point verified) in _apply_tier_listen. Short capture,
        gated so it runs only when it's needed and safe."""
        # FINE-TUNE owns the cadence while active — its 8 s listen IS the verification.
        # Letting the relaxed tier ALSO fire here would race for _tier_listen and risk
        # two Whisper reads in flight at once on the same song.
        if self._fine_active:
            return
        if (self._aligning or self._tier_listen or not self.lines
                or self._live_mode or not self.media):
            return
        if (self.meta.get("source") or "") == "youtube-captions":
            return                                  # caption timing is already exact
        try:
            import align
            if not align.available():
                return
        except Exception:
            return
        st = self.media.get()
        if not (st and st.get("status") == PLAYING):
            return
        if float(st.get("position") or 0.0) < self._tune.get("auto_align_min_pos", 12.0):
            return
        # WAVEFORM GATE: only transcribe when the vocal-band energy says singing is
        # happening now — an instrumental/quiet window yields a useless transcript.
        if not self._vocals_active_now():
            return
        self._tier_listen = True
        self._aligning = True
        lines, lang = self.lines, self.meta.get("lang", "ja")
        secs = float(self._tune.get("sync_tier_listen_s", 6.0))

        def work():
            res = None
            try:
                res = align.capture_and_align(lines, lang=lang,
                                              get_pos=self._align_pos, seconds=secs)
            except Exception as e:
                log.info("tier listen error: %s", e)
            self.root.after(0, lambda: self._apply_tier_listen(res))

        threading.Thread(target=work, daemon=True).start()

    def _apply_tier_listen(self, res):
        """Judge a tier Whisper read and update the adaptive cadence. A small drift
        applies on a single read; only a LARGE proposed jump (chorus-mismatch
        territory) is held for a confirming second read before it can move sync."""
        # FINE-TUNE: if this listen was kicked off by the fine-tune scheduler,
        # hand the result to the fine-tune classifier instead of the tier logic.
        # The capture path is shared (same align.capture_and_align via _tier_listen_now),
        # but the verdict semantics diverge — fine-tune uses pause/nudge, not the
        # two-point verifier.
        if self._fine_active and self._fine_listen_pending:
            self._fine_listen_pending = False
            self._aligning = False
            self._tier_listen = False
            return self._apply_fine_listen(res)
        self._aligning = False
        self._tier_listen = False
        if not res:
            # Heard the audio but NO loaded line matched it. For the RIGHT lyrics this is
            # just an instrumental gap; for the WRONG lyrics (a mislabeled/poisoned cache
            # that title+Shazam pass on NAME) it happens EVERY time — so count it, and
            # after enough in a row, reject the cache and re-identify.
            self._sync_fail_streak += 1
            self._note_sync_verdict("inconclusive")
            self._maybe_reject_for_sync_fail()
            return
        offset, ratio, _start = res
        now = time.time()
        pending = self._tier_tpvr is not None and now <= self._tier_tpvr_until
        if not pending:
            self._tier_tpvr = None
            drift = offset - self.offset
            if abs(drift) <= self._tune.get("sync_tier_ok_drift", 0.8):
                log.info("sync-tier: confirmed in sync (drift %+.2fs) — relaxing", drift)
                self._sync_fail_streak = 0     # a real anchor → the lyrics DO match this song
                self._note_sync_verdict("insync")
                return
            # a modest drift is low-risk → apply on this single read
            if abs(drift) <= 2.0 and not (abs(offset) > 6.0 and ratio < 0.72):
                self._tier_commit(offset, ratio, "drift %+.2fs" % drift)
                return
            # a big jump → HOLD and confirm with a 2nd listen (two-point). The
            # window must outlast the 2.5 s gap + the next capture (~6 s) + margin.
            self._tier_tpvr, self._tier_tpvr_until = offset, now + 14.0
            log.info("sync-tier: holding %+.2fs — confirming with a 2nd listen", offset)
            self.root.after(2500, self._tier_listen_now)
            return
        # confirming (second) read of a held big jump
        first = self._tier_tpvr
        self._tier_tpvr = None
        if abs(offset - first) > 1.2:
            log.info("sync-tier: reads disagree (%.2f vs %.2f) → no change", first, offset)
            self._note_sync_verdict("inconclusive")
            return
        measured = 0.5 * (first + offset)
        if abs(measured) > 6.0 and ratio < 0.72:
            self._note_sync_verdict("inconclusive")
            return
        self._tier_commit(measured, ratio, "two reads agree %.2f≈%.2f" % (first, offset))

    def _tier_commit(self, offset, ratio, why):
        self._sync_fail_streak = 0          # got a real anchor → content matches; not a wrong song
        prev = self.offset
        target = round(offset, 2)
        log.info("sync-tier: resync %+.2fs → %+.2fs (%s, match %.2f)",
                 prev, target, why, ratio)
        self._smooth_offset(target, "sync-tier")
        self._hint(f"🎤 Re-synced ({target:+.1f}s)")
        self._note_sync_verdict("corrected")

    # ── FINE-TUNE mode ────────────────────────────────────────────────────
    # Engaged after the regular tier holds in-sync for fine_tune_enter_after_s
    # of wall-time. Re-uses _tier_listen_now's threaded capture but routes the
    # result here. For lyrics-ahead drift it PAUSES the lyric procession so the
    # line/fill freeze, then re-bases self.offset by the same amount at pause
    # expiry (see _tick) — the resumed frame matches the held frame, zero snap.
    # For lyrics-behind drift it uses the existing _smooth_offset (boundary-
    # deferred). Anything outside [-fine_tune_exit_drift_s, +exit] hands back to
    # the normal tier via _tier_commit.

    def _maybe_enter_fine_tune(self):
        """Gate + activation. Called from _note_sync_verdict on every 'insync'."""
        if self._fine_active:
            return
        if self._force_sync_active or self._live_mode or self._aligning or self._tier_listen:
            return
        if not self.lines:
            return
        if (self.meta.get("source") or "") == "youtube-captions":
            return                                   # caption timing is already exact
        try:
            import align
            if not align.available():
                return                               # no Whisper → nothing to fine-tune with
        except Exception:
            return
        need = float(self._tune.get("fine_tune_enter_after_s", 20.0))
        if self._fine_good_t0 is None:
            return
        if (time.time() - self._fine_good_t0) < need:
            return
        self._fine_active = True
        self._fine_incon = 0
        self._fine_listen_pending = False
        interval_ms = int(float(self._tune.get("fine_tune_listen_interval_s", 8.0)) * 1000)
        self._fine_listen_after = self.root.after(interval_ms, self._fine_listen_tick)
        log.info("fine-tune: ENTER (good streak %.1fs, drift target ≤%.2fs, cadence %.1fs)",
                 time.time() - self._fine_good_t0,
                 float(self._tune.get("fine_tune_target_s", 0.2)),
                 float(self._tune.get("fine_tune_listen_interval_s", 8.0)))
        self._fine_good_t0 = None                    # consumed

    def _fine_exit(self, reason):
        """Idempotent teardown. Cancels the pending listen and clears pause buffers
        WITHOUT re-applying the pause delta (the caller's reason — track change,
        force-sync, manual override — has its own offset semantics)."""
        was_active = self._fine_active
        self._fine_active = False
        if self._fine_listen_after is not None:
            try:
                self.root.after_cancel(self._fine_listen_after)
            except Exception:
                pass
            self._fine_listen_after = None
        self._fine_listen_pending = False
        self._fine_incon = 0
        self._fine_good_t0 = None
        # Force-clear pause without subtracting amount — the caller owns the offset.
        if self._fine_pause_until:
            log.info("fine-tune: clearing in-flight pause (no delta applied) — %s", reason)
        self._fine_pause_until = 0.0
        self._fine_pause_pos_eased = None
        self._fine_pause_pos_raw = None
        self._fine_pause_amount = 0.0
        if was_active:
            log.info("fine-tune: EXIT (%s)", reason)

    def _fine_listen_tick(self):
        """Schedule + dispatch the next fine-tune Whisper listen. Mirrors
        _tier_listen_now's gating so we don't fire when it's unsafe (paused,
        no vocals, song too early), and sets _fine_listen_pending so the
        shared _apply_tier_listen routes the result back here."""
        self._fine_listen_after = None
        if not self._fine_active:
            return
        # If a tier listen is mid-flight, defer one cadence and try again — don't
        # fight it for the _tier_listen lock.
        interval_ms = int(float(self._tune.get("fine_tune_listen_interval_s", 8.0)) * 1000)
        if self._tier_listen or self._aligning:
            self._fine_listen_after = self.root.after(interval_ms, self._fine_listen_tick)
            return
        # SHARED gating: copied from _tier_listen_now so behavior matches.
        if (not self.lines or self._live_mode or not self.media
                or self._force_sync_active):
            self._fine_exit("guard-fail")
            return
        if (self.meta.get("source") or "") == "youtube-captions":
            self._fine_exit("captions-source")
            return
        try:
            import align
            if not align.available():
                self._fine_exit("align-unavailable")
                return
        except Exception:
            self._fine_exit("align-import-error")
            return
        st = self.media.get()
        if not (st and st.get("status") == PLAYING):
            # paused/stopped — reschedule, don't exit; resume should pick up.
            self._fine_listen_after = self.root.after(interval_ms, self._fine_listen_tick)
            return
        if float(st.get("position") or 0.0) < self._tune.get("auto_align_min_pos", 12.0):
            self._fine_listen_after = self.root.after(interval_ms, self._fine_listen_tick)
            return
        if not self._vocals_active_now():
            # instrumental window — don't waste a Whisper read, try again next tick.
            self._fine_listen_after = self.root.after(interval_ms, self._fine_listen_tick)
            return
        # Mark the in-flight listen as ours and dispatch via the shared infrastructure.
        self._fine_listen_pending = True
        self._tier_listen = True
        self._aligning = True
        lines, lang = self.lines, self.meta.get("lang", "ja")
        secs = float(self._tune.get("sync_tier_listen_s", 6.0))

        def work():
            res = None
            try:
                res = align.capture_and_align(lines, lang=lang,
                                              get_pos=self._align_pos, seconds=secs)
            except Exception as e:
                log.info("fine-tune listen error: %s", e)
            self.root.after(0, lambda: self._apply_tier_listen(res))

        threading.Thread(target=work, daemon=True).start()

    def _fine_pause(self, drift):
        """Pause the lyric procession by `drift` seconds (capped by max_pause and
        80% of the current line's remaining time so a pause can't cross a line
        boundary). Captures pos/pos_raw ONCE so the held frame is stable during
        the pause. The pause-end block in _tick re-bases self.offset by the same
        amount so the resumed frame equals the held frame."""
        min_step = float(self._tune.get("fine_tune_min_step_s", 0.2))
        max_pause = float(self._tune.get("fine_tune_max_pause_s", 1.0))
        amount = max(min_step, min(drift, max_pause))
        # BOUND by line-remaining (80%) so the pause can't strand the next line.
        st = self.media.get()
        if not st:
            log.info("fine-tune: media state unavailable — skipping pause (drift %+.2fs)", drift)
            return False
        cur_pos = float(st.get("position") or 0.0)
        pos_raw_now = cur_pos + self.offset
        if 0 <= self.idx < len(self.lines):
            remaining = self.lines[self.idx].end - pos_raw_now
            if remaining > 0:
                amount = min(amount, remaining * 0.8)
        if amount < min_step:
            # not enough time left on this line — skip; next listen will catch it.
            log.info("fine-tune: drift %+.2fs but only %.2fs left on line — skip pause",
                     drift, amount / 0.8 if amount > 0 else 0.0)
            return False
        self._fine_pause_pos_eased = cur_pos + self._eased_offset()
        self._fine_pause_pos_raw = pos_raw_now
        self._fine_pause_amount = amount
        self._fine_pause_until = time.time() + amount
        log.info("fine-tune pause %.2fs (lyrics ahead by %+.2fs)", amount, drift)
        return True

    def _apply_fine_listen(self, res):
        """Classify the fine-tune Whisper read into one of:
          (a) |drift| ≤ target  → in sync, reset incon, reschedule.
          (b) +min_step < drift ≤ +max_pause → pause to let vocals catch up.
          (c) -max_pause ≤ drift < -min_step → tiny forward nudge via _smooth_offset.
          (d) |drift| > exit_drift          → exit + hand off to _tier_commit.
          (e) res is None (inconclusive)    → bump incon, exit after N consecutive."""
        interval_ms = int(float(self._tune.get("fine_tune_listen_interval_s", 8.0)) * 1000)
        target = float(self._tune.get("fine_tune_target_s", 0.2))
        min_step = float(self._tune.get("fine_tune_min_step_s", 0.2))
        max_pause = float(self._tune.get("fine_tune_max_pause_s", 1.0))
        exit_drift = float(self._tune.get("fine_tune_exit_drift_s", 1.5))
        incon_limit = int(self._tune.get("fine_tune_inconclusive_exit", 2))

        if res is None:
            self._fine_incon += 1
            log.info("fine-tune: inconclusive (%d/%d)", self._fine_incon, incon_limit)
            if self._fine_incon >= incon_limit:
                self._fine_exit("inconclusive-streak")
                return
            self._fine_listen_after = self.root.after(interval_ms, self._fine_listen_tick)
            return

        offset, ratio, _start = res
        drift = offset - self.offset

        # (d) Big drift — escalate back to the normal tier's verifier.
        if abs(drift) > exit_drift:
            log.info("fine-tune: drift %+.2fs > exit %.2f → hand back to tier", drift, exit_drift)
            self._fine_exit("big-drift")
            self._tier_commit(offset, ratio, "fine→tier handoff %+.2f" % drift)
            return

        # (a) In target.
        if abs(drift) <= target:
            self._fine_incon = 0
            log.info("fine-tune: locked (drift %+.2fs ≤ %.2fs)", drift, target)
            self._fine_listen_after = self.root.after(interval_ms, self._fine_listen_tick)
            return

        # (b) Lyrics ahead — pause to let vocals catch up.
        if drift > min_step:
            ok = self._fine_pause(drift)
            self._fine_incon = 0
            # Schedule next listen AFTER the pause expires + one cadence (so the
            # subsequent measurement sees the rebased offset, not the in-flight pause).
            if ok:
                wait_ms = int((self._fine_pause_amount * 1000) + interval_ms)
            else:
                wait_ms = interval_ms
            self._fine_listen_after = self.root.after(wait_ms, self._fine_listen_tick)
            return

        # (c) Lyrics behind — tiny forward nudge via the boundary-deferred path.
        if drift < -min_step:
            # Backward drift = lyrics behind sung vocals. A pause would make
            # this worse — only option is a small forward nudge. Uses the
            # SEPARATE max-move-ahead cap (default 2.0s) since a small forward
            # skip is less perceptible than holding lyrics frozen.
            max_move_ahead = float(self._tune.get("fine_tune_max_move_ahead_s", 2.0))
            step = min(abs(drift), max_move_ahead)
            new_off = round(self.offset + step, 2)
            log.info("fine-tune catch-up nudge: %+.2fs (drift %+.2fs, cap %.2f)",
                     step, drift, max_move_ahead)
            self._smooth_offset(new_off, "fine-tune-catchup")
            self._fine_incon = 0
            self._fine_listen_after = self.root.after(interval_ms, self._fine_listen_tick)
            return

        # |drift| ≤ min_step but > target — within step-noise floor; treat as locked.
        self._fine_incon = 0
        self._fine_listen_after = self.root.after(interval_ms, self._fine_listen_tick)

    def _maybe_reject_for_sync_fail(self):
        """After `sync_reject_strikes` sync-by-ear reads in a row that heard vocals but
        couldn't ANCHOR them to the loaded lyrics, the cache is the WRONG song — a
        mislabeled / poisoned LRC that title-match and Shazam both pass on the NAME
        while the LYRIC CONTENT is someone else's (Deep Dive cached with Dunk's words).
        Reject it and re-identify. Capped per track so it can't loop."""
        if self._sync_fail_streak < int(self._tune.get("sync_reject_strikes", 3)):
            return
        src = (self.meta.get("source") or "")
        if (not self.lines or self._live_mode or self._force_sync_active or self._deciding
                or src.startswith("bundled") or src == "youtube-captions"
                or self._sync_reject_count >= 2):
            return                              # nothing to reject / authoritative / already tried twice
        self._reject_for_sync_fail()

    def _reject_for_sync_fail(self):
        self._sync_fail_streak = 0
        self._sync_reject_count += 1
        log.info("sync kept failing on %r (%d× no anchor) — lyrics don't match the singing "
                 "→ rejecting the cache and re-identifying", self.meta.get("title", ""),
                 int(self._tune.get("sync_reject_strikes", 3)))
        self._hint("🎯 Lyrics don't match the song — re-identifying…")
        # report_wrong bins the bad cache, unlocks the title, and re-identifies. For a
        # browser video the video's OWN captions are the authoritative real lyrics (now
        # fetchable again), so prefer those right after — they override a re-fetched
        # provider LRC and aren't themselves rejectable (source 'youtube-captions').
        self.report_wrong()
        if getattr(self, "captions_on", False) and not self._live_mode:
            self.root.after(2000, lambda: self.load_youtube_captions(silent=True))

    def _apply_align(self, res):
        self._aligning = False
        silent = getattr(self, "_auto_align_silent", False)
        self._auto_align_silent = False
        # Silent (background) aligns must yield to fine-tune mode — the fine
        # cadence is already listening at 8s. A manual /align (silent=False)
        # is a user override and still runs (it'll exit fine-tune via _apply_align's
        # existing _smooth_offset call → _fine_exit gets triggered separately
        # by the offset write through _smooth_offset side effects).
        if silent and getattr(self, "_fine_active", False):
            return
        # If this listen was a live/concert resync, score it for the rolling cadence
        # (a confident match = good read → relax a step; nothing heard = miss → hammer).
        if getattr(self, "_live_resync_inflight", False):
            self._live_resync_inflight = False
            self._note_live_resync(res is not None)
        # APPLAUSE two-point resync (TICKET-061): HOLD the 1st read, confirm with a
        # 2nd ~2.5 s later, and apply only if they agree — a chorus-matched mis-read
        # on resume can't jump the sync. Falls through to the normal apply when the
        # two reads agree.
        if self._align_tpvr_active:
            if not res or time.time() > self._align_tpvr_until:
                self._align_tpvr_active, self._align_tpvr = False, None
                log.info("applause resync aborted (no capture / expired)")
                return
            offset = res[0]
            if self._align_tpvr is None:
                self._align_tpvr = offset
                log.info("applause resync: holding %+.2fs — confirming with a 2nd listen", offset)
                self.root.after(2500, lambda: self.align_by_listening(silent=True))
                return
            first = self._align_tpvr
            self._align_tpvr, self._align_tpvr_active = None, False
            if abs(offset - first) > 1.2:
                log.info("applause resync: reads disagree (%.2f vs %.2f) → discard", first, offset)
                return
            log.info("applause resync: two reads agree (%.2f≈%.2f) → applying", first, offset)
        if not res:
            if not silent:
                self._hint("Couldn't hear the lyrics clearly — try again")
            return
        offset, ratio, _start = res
        # A BIG alignment offset on a song whose player clock is already accurate is
        # usually a MIS-match (the transcript matched the wrong repeated line) — and
        # the user's observed fix is "reset to 0". So only trust a large offset when
        # the match is strong; otherwise snap back to 0 (the player position), which
        # is right far more often than a low-confidence big jump.
        if abs(offset) > 6.0 and ratio < 0.72:
            if not silent:
                self.offset = 0.0
                log.info("align: large offset %.1fs at low match %.2f → reset to 0",
                         offset, ratio)
                self._hint("Couldn't sync confidently — reset to 0")
            return
        # Background auto-align: only apply when the new offset DIFFERS meaningfully
        # from the current one. A tiny correction is noise — don't churn the offset.
        if silent and abs(offset - self.offset) < 0.6:
            log.info("auto-align: drift %+.2fs within tolerance — no change", offset - self.offset)
            return
        log.info("aligned by listening: offset=%.2fs (match %.2f)%s",
                 offset, ratio, " [auto]" if silent else "")
        if not silent:
            self._fine_exit("manual-align")        # user-driven align → restart the 20 s clock
        self._smooth_offset(offset, "align-by-ear")
        if not silent:
            self._hint(f"Synced by ear ({offset:+.1f}s)")
        else:
            self._hint(f"🎤 Auto-synced ({offset:+.1f}s)")

    def quit(self):
        self._destroy_mirrors()
        if self._boundary is not None:
            try:
                self._boundary.stop()
            except Exception:
                pass
        # BUG-2/5/6: shut down the long-lived Discord RP watcher so its
        # blocking _recv_one doesn't survive process exit (the daemon flag
        # tears it down regardless, but closing the pipe handle here lets
        # the worker exit promptly rather than waiting on an indefinite read).
        try:
            import discord_rpc as _drpc
            _drpc.stop_watcher()
        except Exception:
            pass
        # TICKET-102: tear down the window-title watcher too. Like the Discord
        # watcher this is daemon=True so process exit would kill it anyway,
        # but signalling stop here lets the worker's wait wake up cleanly.
        try:
            import window_titles as _wt
            _wt.stop_watcher()
        except Exception:
            pass
        self.media.stop()
        self.root.quit()

    def run(self):
        self.root.mainloop()


# ── Tray icon ────────────────────────────────────────────────────────

def make_icon():
    """Load the tray icon image (bundled icon.ico), falling back to a simple drawn
    microphone (matching the real icon) if the file can't be read."""
    ico = _resource("icon.ico")
    if ico.exists():
        return Image.open(ico)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=14, fill="#6d28d9")
    # minimal microphone: capsule head + band + handle
    d.rounded_rectangle([25, 13, 39, 39], radius=7, fill="white")     # head
    d.rounded_rectangle([27, 38, 37, 43], radius=2, fill="white")     # band
    d.rounded_rectangle([29, 42, 35, 53], radius=3, fill="white")     # handle
    return img


_SINGLE_INSTANCE_MUTEX = None


def _is_only_instance():
    """Single-instance guard: hold a process-lifetime named mutex so a SECOND Desktop
    Karaoke launch exits instead of opening a duplicate overlay. The venv `pythonw`
    stub that re-execs the real interpreter does NOT run this (only the real app
    process does), so an instance never blocks its own stub→child pair."""
    global _SINGLE_INSTANCE_MUTEX
    try:
        k32 = ctypes.windll.kernel32
        h = k32.CreateMutexW(None, False, "Local\\DesktopKaraoke.SingleInstance")
        if not h:
            return True
        _SINGLE_INSTANCE_MUTEX = h         # keep alive for the run (releases on exit)
        return k32.GetLastError() != 183   # 183 = ERROR_ALREADY_EXISTS
    except Exception:
        return True


def main():
    if not _is_only_instance():
        return                 # another Desktop Karaoke is already running
    # TICKET-105: heal up the rebrand-stale Start Menu shortcut before
    # anything else (frozen builds only; no-op in dev). Best-effort and
    # never blocks startup.
    _migrate_start_menu_shortcut()
    # Windows' default system timer granularity is ~15.6 ms, so Tk's after(16)
    # for a 60 fps loop actually fires at either ~15.6 ms or ~31.2 ms — that
    # uneven cadence is exactly the scroll "stutter" (diagnosed via /diag:
    # frame intervals alternating 16/30 ms even with zero render work).
    # Raising the timer resolution to 1 ms makes after() accurate and the belt
    # smooth. Costs a little power; fine for a foreground media app.
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass
    # CPU AFFINITY: keep this app OFF the first core(s). Windows runs the audio
    # engine and most device interrupts (DPC/ISR) on core 0, so a CPU-busy app
    # sharing core 0 shows up as a STATIC STUTTER in playing audio. Pinning our
    # threads to the LATER cores (upper half + 1, never core 0) leaves the audio
    # path clean on a typical 4+ core machine; on 2-3 cores we just avoid core 0.
    # On a 16-core box this is cores 7-15 (mask 0xff80, 9 cores) — the extra core
    # below the upper half gives the fill-paint loop more PIL headroom for the
    # newly raised scroll_heavy_budget_ms / scroll_repaint_budget without crowding audio.
    # Best-effort and reversible by the user via Task Manager.
    try:
        import os as _os
        k32 = ctypes.windll.kernel32
        # 64-bit-correct signatures — without these ctypes truncates the pseudo
        # handle (-1) and the DWORD_PTR mask to 32 bits and the call no-ops.
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        k32.SetProcessAffinityMask.restype = ctypes.c_int
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        n = _os.cpu_count() or 1
        if n >= 4:
            mask = 0
            # upper half PLUS one extra core below it: e.g. 16-core → cores 7-15
            # (mask 0xff80); 8-core → cores 3-7 (mask 0xf8); 4-core → cores 1-3.
            start = max(1, (n // 2) - 1)     # never include core 0
            for c in range(start, n):
                mask |= (1 << c)
        elif n >= 2:
            mask = ((1 << n) - 1) & ~1        # all cores except core 0
        else:
            mask = 1
        h = k32.GetCurrentProcess()
        ok = k32.SetProcessAffinityMask(h, mask)
        # Below-normal priority so we yield to the foreground game/player.
        k32.SetPriorityClass(h, 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
        log.info("cpu affinity: %d cores → mask 0x%x (set=%s), priority below-normal",
                 n, mask, bool(ok))
    except Exception as e:
        try:
            log.info("cpu affinity set failed: %s", e)
        except Exception:
            pass
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
    def _align(*_):   ov.root.after(0, ov.force_sync)
    def _about(*_):
        import webbrowser
        webbrowser.open("https://github.com/BarnsL/Desktop-Karaoke#readme")

    def _open_import(*_):
        from playlist_import_gui import show_import_window
        show_import_window(ov.root)
    def _quit(icon, *_):
        ov._tray_quit = True       # tell the self-healing runner to stop
        try:
            icon.stop()
        except Exception:
            pass
        ov.root.after(0, ov.quit)

    def _set_op(v):  return lambda *_: ov.root.after(0, lambda: ov.set_opacity(v))
    def _set_pos(axis, v): return lambda *_: ov.root.after(0, lambda: ov.set_pos(axis, v))
    def _set_scr(d): return lambda *_: ov.root.after(0, lambda: ov.set_scroll(d))
    def _set_font(v): return lambda *_: ov.root.after(0, lambda: ov.set_font_scale(v))
    def _toggle_startup(*_): set_startup(not startup_enabled())

    def _op_item(label, v):
        return pystray.MenuItem(label, _set_op(v), radio=True,
                                checked=lambda i, v=v: abs(ov.opacity - v) < 0.02)

    def _pos_item(label, axis, v):
        return pystray.MenuItem(label, _set_pos(axis, v), radio=True,
                                checked=lambda i, axis=axis, v=v:
                                    (ov.pos_y if axis == "y" else ov.pos_x) == v)

    def _scr_item(label, d):
        return pystray.MenuItem(label, _set_scr(d), radio=True,
                                checked=lambda i, d=d: ov.scroll_dir == d)

    opacity_menu = pystray.Menu(
        _op_item("100%  (solid)", 1.0), _op_item("85%", 0.85),
        _op_item("70%", 0.70), _op_item("55%", 0.55),
        _op_item("40%  (faint — for games)", 0.40), _op_item("25%", 0.25),
    )
    # Position is two INDEPENDENT axes now — pick a vertical AND a horizontal anchor.
    position_menu = pystray.Menu(
        pystray.MenuItem("Vertical", pystray.Menu(
            _pos_item("Top", "y", "top"),
            _pos_item("Center", "y", "center"),
            _pos_item("Bottom", "y", "bottom"),
        )),
        pystray.MenuItem("Horizontal", pystray.Menu(
            _pos_item("Left", "x", "left"),
            _pos_item("Center", "x", "center"),
            _pos_item("Right", "x", "right"),
        )),
    )

    # ── Display (multi-monitor) ──────────────────────────────────────────
    def _disp_item(label, key):
        return pystray.MenuItem(
            label, lambda *_: ov.root.after(0, lambda: ov.set_display(key)),
            radio=True, checked=lambda i, key=key: ov.display == key)
    _disp = []
    for _i, _m in enumerate(_monitors()):
        _key = "primary" if _i == 0 else f"mon:{_i}"
        _tag = "  ·  primary" if _m["primary"] else ""
        _disp.append(_disp_item(f"Screen {_i + 1}  ({_m['w']}×{_m['h']}){_tag}", _key))
    if len(_disp) > 1:
        _disp.append(pystray.Menu.SEPARATOR)
        _disp.append(_disp_item("Mirror on ALL screens", "mirror"))
        _disp.append(_disp_item("Cycle through screens  (rotate per line)", "cycle"))
        _disp.append(_disp_item("Scroll across ALL screens  (one continuous band)", "span"))
    display_menu = pystray.Menu(*_disp)
    scroll_menu = pystray.Menu(
        _scr_item("Stationary (appear in place)", "none"),
        _scr_item("Slide in from left", "left"),
        _scr_item("Slide in from right", "right"),
        _scr_item("Slide in from top  ↓  (centered)", "top"),
        _scr_item("Slide in from bottom  ↑  (centered)", "bottom"),
        pystray.Menu.SEPARATOR,
        _scr_item("Scroll through  →  (left to right)", "lr"),
        _scr_item("Scroll through  ←  (right to left)", "rl"),
        pystray.Menu.SEPARATOR,
        _scr_item("Scroll in from top  ↓", "tb"),
        _scr_item("Scroll in from bottom  ↑", "bt"),
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

    def _recal_item(label, secs):
        return pystray.MenuItem(label, lambda *_: ov.root.after(0, lambda: ov.set_recal(secs)),
                                radio=True, checked=lambda i, secs=secs: ov.recal_secs == secs)
    recal_menu = pystray.Menu(
        _recal_item("Off", 0), _recal_item("Every 4s  (max — ~4s is the floor)", 4),
        _recal_item("Every 8s", 8), _recal_item("Every 10s", 10),
        _recal_item("Every 15s", 15), _recal_item("Every 20s", 20),
    )

    def _preset(name):
        return lambda *_: ov.root.after(0, lambda: ov.apply_preset(name))
    preset_menu = pystray.Menu(
        pystray.MenuItem("🎮  Gaming  (subtle, learn while you play)", _preset("gaming")),
        pystray.MenuItem("🎤  Karaoke  (big, scrolling, for a room)", _preset("karaoke")),
    )
    def _toggle_git(*_):   ov.root.after(0, lambda: ov.set_git_sync(not ov.git_sync))
    def _backup_now(*_):   ov.root.after(0, ov.git_backup)
    def _toggle_char(*_):  ov.root.after(0, lambda: ov.set_character(not ov.character_on))
    def _toggle_api(*_):   ov.root.after(0, lambda: ov.set_api(not ov.api_on))
    def _toggle_bound(*_): ov.root.after(0, lambda: ov.set_boundary(not ov.boundary_on))
    def _toggle_gen(*_):   ov.root.after(0, lambda: ov.set_generate(not ov.generate_on))
    def _toggle_caps(*_):  ov.root.after(0, lambda: ov.set_captions(not ov.captions_on))
    # TICKET-100: Discord Rich Presence reader toggle (default OFF, opt-in).
    def _toggle_discord_rpc(*_):
        ov.root.after(0, lambda: ov.set_discord_rpc(not ov.discord_rpc_on))
    # TICKET-102: window-title scraper toggles (HIGH default ON, LOW default OFF).
    def _toggle_window_titles(*_):
        ov.root.after(0, lambda: ov.set_window_titles(not ov.window_titles_on))
    def _toggle_window_titles_generic(*_):
        ov.root.after(0, lambda: ov.set_window_titles_generic_browsers(
            not ov.window_titles_generic_browsers_on))
    def _get_caps(*_):     ov.root.after(0, ov.load_youtube_captions)

    # ── Optional GPU acceleration ────────────────────────────────────────
    # Transcription runs on the CPU by default (fine — 16s clip in ~2s). On an
    # NVIDIA GPU the user can opt in to CUDA, which is a bit faster; the ~1.5 GB of
    # libraries are downloaded on demand (gpu_setup) instead of bloating everyone's
    # install. The item is hidden entirely on machines with no NVIDIA GPU.
    _gpu = {"busy": False}

    def _gpu_label(i=None):
        # TICKET-103: when the GPU libs are installed, show WHERE Whisper
        # will actually run right now (live policy: which CUDA device or
        # 'CPU' with the reason, e.g. 'game running', 'single-GPU policy').
        if _gpu["busy"]:
            return "⏳  Installing GPU acceleration…"
        if not gpu_setup.gpu_ready():
            return f"⚡  Enable GPU acceleration (~{gpu_setup.APPROX_MB} MB)"
        try:
            import align
            dev, idx, reason, _n = align.current_device_choice()
            if dev == "cuda":
                return f"⚡  GPU acceleration: cuda:{idx}  ·  {reason}"
            return f"⚡  GPU acceleration: CPU  ·  {reason}"
        except Exception:
            return "⚡  GPU acceleration: on"

    def _on_gpu(icon_, *_):
        if _gpu["busy"] or gpu_setup.gpu_ready() or not gpu_setup.nvidia_gpu_present():
            return
        _gpu["busy"] = True
        try: icon_.update_menu()
        except Exception: pass
        try: icon_.notify("Downloading CUDA libraries (~1.5 GB) in the background — "
                          "keep using the app; GPU kicks in when it's done.",
                          "Lyric Immersion and Karaoke")
        except Exception: pass
        def _do():
            ok = gpu_setup.download_gpu_libs(log=log.info)
            _gpu["busy"] = False
            try: icon_.update_menu()
            except Exception: pass
            try: icon_.notify(
                "GPU acceleration enabled — used from the next song on." if ok
                else "Couldn't enable GPU acceleration; staying on CPU.",
                "Lyric Immersion and Karaoke")
            except Exception: pass
        threading.Thread(target=_do, daemon=True).start()
    git_menu = pystray.Menu(
        pystray.MenuItem("Auto-push new songs", _toggle_git,
                         checked=lambda i: ov.git_sync),
        pystray.MenuItem("Back up now", _backup_now),
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

    # ── Updates ──────────────────────────────────────────────────────
    # Store (MSIX) installs auto-update via the Microsoft Store; the portable
    # .exe self-updates from GitHub Releases. updater.py handles the difference.
    _upd = {"info": None}

    def _upd_label(i=None):
        info = _upd["info"]
        return f"⬆  Install update v{info['version']}" if info else "Check for updates"

    def _on_updates(icon_, *_):
        info = _upd["info"]
        if info:                                   # an update is known → apply it
            try: icon_.notify(f"Updating to v{info['version']}…", "Lyric Immersion and Karaoke")
            except Exception: pass
            def _do():
                if updater.stage_update(info, log=log.info):
                    ov.root.after(0, lambda: _quit(icon_))   # exit so the helper swaps + relaunches
            threading.Thread(target=_do, daemon=True).start()
            return
        try: icon_.notify("Checking for updates…", "Lyric Immersion and Karaoke")    # manual check
        except Exception: pass
        def _check():
            got = updater.check()
            _upd["info"] = got
            try: icon_.update_menu()
            except Exception: pass
            try: icon_.notify(
                f"Update v{got['version']} available — open the tray menu to install." if got
                else f"You're up to date (v{updater.current_version()}).", "Lyric Immersion and Karaoke")
            except Exception: pass
        threading.Thread(target=_check, daemon=True).start()

    def _on_update_found(info):                    # background check found one
        _upd["info"] = info
        try: icon.update_menu()
        except Exception: pass
        try: icon.notify(f"Lyric Immersion and Karaoke v{info['version']} is available.", "Update available")
        except Exception: pass

    # TICKET-097: menu organized in logical sections separated by SEPARATOR.
    # 1. PER-SONG ACTIONS (act on the current track right now)
    # 2. DETECTION / LYRIC SOURCES (toggles for how we find lyrics)
    # 3. SYNC BEHAVIOR (timing controls)
    # 4. VISUAL / DISPLAY (overlay appearance)
    # 5. PERFORMANCE (renderer + GPU)
    # 6. LIBRARY / CONTENT (presets, import, backup)
    # 7. APP / SYSTEM (api, autostart)
    # 8. UPDATES / ABOUT / QUIT
    menu = pystray.Menu(
        # 1. PER-SONG ACTIONS ─────────────────────────────────────────────
        pystray.MenuItem("⚑  Wrong lyrics — fix this song", _wrong),
        pystray.MenuItem("🎧  Identify by sound", _ident),
        pystray.MenuItem("🚀  Force Sync  (try ranked matches, skip chorus traps, until it locks)", _align),
        pystray.MenuItem("Re-fetch lyrics", _refetch),
        pystray.MenuItem("⬇  Get captions for this video now", _get_caps),
        pystray.Menu.SEPARATOR,
        # 2. DETECTION / LYRIC SOURCES ────────────────────────────────────
        pystray.MenuItem("Fast song-change detect (compilations)", _toggle_bound,
                         checked=lambda i: ov.boundary_on),
        pystray.MenuItem("Use YouTube captions (accurate, for browser videos)", _toggle_caps,
                         checked=lambda i: ov.captions_on),
        pystray.MenuItem("Generate lyrics by ear when none found (AI, ***)", _toggle_gen,
                         checked=lambda i: ov.generate_on),
        # TICKET-100: Discord RP reader. Only contributes when SMTC + Shazam are
        # both silent for >= 8s, so it never fights an actual local player.
        pystray.MenuItem("Read Discord Rich Presence (Spotify, when no other source)",
                         _toggle_discord_rpc,
                         checked=lambda i: ov.discord_rpc_on),
        # TICKET-102: window-title scraper — picks up Steam Overlay's embedded
        # CEF browser, Discord/Slack/Teams embedded players (the SMTC-blind
        # cases). Default ON because the allowlist is narrow + music-purposed.
        pystray.MenuItem("Read window titles (Steam Overlay, Discord, Slack, Teams)",
                         _toggle_window_titles,
                         checked=lambda i: ov.window_titles_on),
        # Sub-toggle: ALSO scrape standalone browsers (slower, may misfire on
        # podcast / vlog titles). Hidden when the parent is off.
        pystray.MenuItem("Read window titles from web browsers (slower, may misfire)",
                         _toggle_window_titles_generic,
                         checked=lambda i: ov.window_titles_generic_browsers_on,
                         visible=lambda i: ov.window_titles_on),
        pystray.Menu.SEPARATOR,
        # 3. SYNC BEHAVIOR ────────────────────────────────────────────────
        pystray.MenuItem(lambda i: f"Sync timing  ({ov.offset:+.1f}s)", sync_menu),
        pystray.MenuItem("Auto re-sync by sound", recal_menu),
        pystray.Menu.SEPARATOR,
        # 4. VISUAL / DISPLAY ─────────────────────────────────────────────
        pystray.MenuItem("Show / Hide", _toggle),
        pystray.MenuItem("Position", position_menu),
        pystray.MenuItem("Display", display_menu),
        pystray.MenuItem("Opacity", opacity_menu),
        pystray.MenuItem("Font size", font_menu),
        pystray.MenuItem("Scroll-in", scroll_menu),
        pystray.MenuItem("Scroll-through speed", speed_menu),
        pystray.MenuItem("Dancing character", _toggle_char,
                         checked=lambda i: ov.character_on),
        pystray.Menu.SEPARATOR,
        # 5. PERFORMANCE ──────────────────────────────────────────────────
        pystray.MenuItem("Performance", perf_menu),
        pystray.MenuItem(_gpu_label, _on_gpu,
                         enabled=lambda i: not _gpu["busy"] and not gpu_setup.gpu_ready(),
                         visible=lambda i: gpu_setup.nvidia_gpu_present()),
        pystray.Menu.SEPARATOR,
        # 6. LIBRARY / CONTENT ────────────────────────────────────────────
        pystray.MenuItem("Presets", preset_menu),
        pystray.MenuItem("📥  Import playlist (Spotify / YouTube)", _open_import),
        pystray.MenuItem("Library backup (Git)", git_menu),
        pystray.Menu.SEPARATOR,
        # 7. APP / SYSTEM ─────────────────────────────────────────────────
        pystray.MenuItem("Local API (agent control)", _toggle_api,
                         checked=lambda i: ov.api_on),
        pystray.MenuItem("Start with Windows", _toggle_startup,
                         checked=lambda i: startup_enabled()),
        pystray.Menu.SEPARATOR,
        # 8. UPDATES / ABOUT / QUIT ───────────────────────────────────────
        pystray.MenuItem(_upd_label, _on_updates),
        pystray.MenuItem(f"ℹ️  About  ·  v{version.__version__}", _about),
        pystray.MenuItem("Quit", _quit),
    )
    icon = pystray.Icon("desktop-karaoke", make_icon(), "Lyric Immersion and Karaoke", menu)
    updater.background_check(_on_update_found)   # notify if a newer release exists (portable build)
    # SELF-HEALING TRAY ICON: the icon is the ONLY way to reach the menu (Quit,
    # toggles), so it must be present whenever the app runs. pystray's run() can
    # die on a Windows shell race (Explorer restart, icon-registration timing) and
    # then the overlay keeps playing with NO icon — unkillable except via Task
    # Manager. Run it in a loop that re-creates and re-shows the icon if it ever
    # exits or throws, until the user actually picks Quit.
    ov._tray_quit = False

    def _tray_runner():
        cur = icon
        while not getattr(ov, "_tray_quit", False):
            try:
                cur.run()                       # blocks until stop() or failure
            except Exception as e:
                log.info("tray icon crashed: %s — re-creating", e)
            if getattr(ov, "_tray_quit", False):
                break
            log.info("tray icon vanished — restoring it")
            time.sleep(2)
            try:
                cur = pystray.Icon("desktop-karaoke", make_icon(),
                                   "Lyric Immersion and Karaoke", menu)
            except Exception:
                pass
    threading.Thread(target=_tray_runner, daemon=True).start()
    ov.run()


if __name__ == "__main__":
    main()
