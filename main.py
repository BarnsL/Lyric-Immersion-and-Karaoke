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
            / "Start Menu" / "Programs" / "Startup" / "Desktop Karaoke.lnk")


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


def _title_forms(title):
    """Normalized forms a title may be matched by: the whole thing, and — for a
    JP-style 'Artist / Song' upload — the **song** part (the segment after the last
    '/'), plus a Cyrillic→Latin transliteration of each. Only the last segment is
    tried (the song; the leading parts are the artist), and segments shorter than
    4 chars are dropped, so the artist name can't cause a false match."""
    forms = set()
    for base in (title or "", _translit_cyr(title or "")):
        forms.add(_norm_title(base))
        segs = re.split(r"\s*[/／]\s*", base)
        if len(segs) > 1:
            nf = _norm_title(segs[-1])          # 'Artist / Song' → the Song
            if len(nf) >= 4:
                forms.add(nf)
    forms.discard("")
    return forms


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
    r"|\(\s*cover\s*\)|\[\s*cover\s*\]|[/／]\s*cover\b", re.I)


def is_cover_title(title):
    """True if a media title marks a 歌ってみた / cover. Drives a title-first lyric
    fetch — the original song's lyrics fit the cover (see fetch_lrc cover=)."""
    return bool(_COVER_RE.search(title or ""))


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
    t = raw_title.strip()
    # strip cover markers and brackets containing them
    t = re.sub(r"\[\s*cover\s*\]", "", t, flags=re.I).strip()
    t = re.sub(r"\(\s*cover\s*\)", "", t, flags=re.I).strip()
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
_LIVE_RE = re.compile(
    r"\b(?:concert|fes(?:tival)?|tour|setlist|set\s*list|medley|megamix|"
    r"mega\s*mix|non-?stop|dj\s*set|full\s*(?:album|live|concert|set)|"
    r"rock\s*japan|rising\s*sun|summer\s*sonic|fuji\s*rock|countdown|"
    r"anniversary\s*live)\b"
    r"|ライブ|ﾗｲﾌﾞ|生放送|コンサート|フェス|ツアー|メドレー|セットリスト|セトリ|"
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
    r"acoustic|unplugged|orchestral?|piano\s*ver|ballad\s*ver|spinning\s*ver)\b"
    r"|from\s+[\"'“”『「]"                       # 'from "<concert/album>"'
    r"|ライブ|ﾗｲﾌﾞ|生歌|ショート(?:バージョン|ver)|アコースティック|弾き語り",
    re.I,
)


def is_live_arrangement(title):
    """True for a single-song LIVE/short/alternate version whose timing won't match
    the studio LRC (so sync must FOLLOW the measured offset, not reset to 0)."""
    return bool(_LIVE_VER_RE.search(title or ""))


def clean_artist(artist):
    """Strip YouTube channel cruft so the artist matches lyric providers:
    'Kaneko Lumi - Topic' → 'Kaneko Lumi', 'LMFAOVEVO' → 'LMFAO'. Auto-generated
    '… - Topic' / VEVO / 'Official Artist Channel' uploads are real tracks; the
    suffix just blocks the provider/Shazam-name search."""
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
    return a.strip() or (artist or "")


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
            entries.append({
                "path": p,
                "title": lt,
                "core": core,
                "forms": {f for f in forms if f},
                "artist": m.get("artist") or "",
                "dur": m.get("duration"),
            })
        self.entries = entries

    def add(self, path):
        path = Path(path)
        self.entries = [e for e in self.entries if e["path"] != path]
        self.refresh()

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
        # whole title + each 'Artist / Song' segment + Cyrillic transliteration,
        # so a wrapped MV title ("IA & ОИЕ / Into Starlight -ver-") still matches.
        q_forms = _title_forms(title)
        best, best_score = None, 0
        for e in self.entries:
            ea = _norm_title(e["artist"])
            e_core = _norm_title(e["core"])
            score = 0
            for ct in e.get("forms") or {e_core}:
                # An ARTIST/GROUP-only SEGMENT (e.g. 'flowglow' from 'Song / FLOW
                # GLOW') is shared across that artist's songs, so on its own it must
                # not carry a match — require the SONG to match. Skip a *segment*
                # (never the whole title) that is (contained in) either artist name.
                if (ct and len(ct) >= 3 and ct != e_core
                        and ((ea and (ct == ea or ct in ea))
                             or (qa and (ct == qa or ct in qa)))):
                    continue
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
                    else:
                        s = 0
                    score = max(score, s)
            if not score:
                continue
            if duration and e["dur"]:
                score += 8 if abs(e["dur"] - duration) <= 12 else -40
            if qa and _norm_title(e["artist"]) == qa:
                score += 5
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


_MEASURE_CACHE = {}


def measure_text(cv, text, font):
    """Pixel width of `text` in `font`, cached. Width depends only on (text,
    font), so caching avoids creating/deleting a throwaway canvas item on every
    call (the non-scroll renderer measures every character per line)."""
    key = (text, font)
    w = _MEASURE_CACHE.get(key)
    if w is None:
        tid = cv.create_text(-9999, -9999, text=text, font=font, anchor="nw")
        bbox = cv.bbox(tid)
        cv.delete(tid)
        w = (bbox[2] - bbox[0]) if bbox else 0
        _MEASURE_CACHE[key] = w
    return w


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
        self.root.title("Desktop Karaoke")
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
        self.position = s.get("position", "bottom")   # 'top' | 'bottom'
        self.display = s.get("display", "primary")     # 'primary' | 'mon:N' | 'span'
        self._display_fp = s.get("display_fp")         # fingerprint for monitor identity
        self._mon_snapshot = ()                         # current monitor topology
        self._last_mon_check = 0.0                     # throttle for _check_monitors
        self.scroll_dir = s.get("scroll", "left")      # 'none'|'left'|'right'|'lr'|'rl'
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
        self.concert_ocr = bool(s.get("concert_ocr", True))  # read the on-screen song banner in concerts
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
        self._spawn_budget = 1       # max scroll blocks to PIL-render per heavy frame
                                     # (1: spawning a block allocates a new image +
                                     # PhotoImage — the priciest op; off-screen, so
                                     # spreading it across frames is invisible)
        self._repaint_budget = 2     # max karaoke-fill repaints per heavy frame
        self._fill_interval = 0.2    # min seconds between a block's fill repaints (~5fps)
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
        self._verified = False
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
            "spread_reset":         20.0,   # chorus-ambiguity spread threshold
            "reset_offset_max":      5.0,   # only reset when |offset| < this
            "drift_align_trigger":   6.0,   # integral → trigger auto-align
            "drift_min_for_accum":   0.8,   # |drift| > this contributes to integral
            "auto_align_cooldown":  25.0,   # min s between auto-aligns
            "auto_align_min_pos":   12.0,   # min player pos before auto-align
            "shazam_lock_grace":    30.0,   # auto-align skipped within N s of lock
            "continuous_recal_ms": 15000,   # background correlation cadence
            "energy_apply_min":      0.4,   # min |new-old| to apply correlation
            "energy_lift_floor":     0.10,  # min peak-vs-median lift to accept
            "energy_max_offset":    60.0,   # |new_off| < this for sanity
            "energy_shift_penalty":  0.012, # per-second penalty for large offset changes (small-shift prior)
            "energy_peak_margin":    0.06,  # reject if a distant rival peak is within this of the best
            # ── render perf knobs (scroll-through smoothness) ──
            # heavy_budget_ms caps the per-frame spawn/repaint work so a PIL
            # paste can't stall the scroll belt; 0 disables the cap.
            "heavy_budget_ms":      10.0,   # max ms of spawn+repaint work per heavy frame
            "repaint_budget":        4.0,   # max karaoke-fill SLIVER pastes per heavy frame (cheap now)
            "spawn_budget":          1.0,   # max block PIL-renders per heavy frame
            "fill_skip":             2.0,   # heavy work runs every Nth frame (fills are sliver-cheap)
            # PERF-102 — scroll bitmap-area controls (the dominant scroll cost):
            "scroll_max_lanes":      3,     # stacked scrolling lines (capped to what fits on screen)
            "scroll_spawn_margin": 1100,    # px off-screen a block is pre-rendered (avoid pop-in)
            # MV/cinematic intro hold backstop (primary release is the vocal poll):
            "mv_intro_timeout":     75.0,   # s before the intro card releases regardless
        }
        self._identify_result = None
        self._sound_song = None       # last (title, artist) heard by Shazam
        self._pending_corr = 1e9      # a large sound offset awaiting a 2nd confirming read
        self._sync_confirm_after = None  # pending 2s "confirm with a 2nd listen" timer
        self._recent_corr = []        # last few audio offsets — spot repeated-chorus ambiguity
        self._live_arrangement = False  # LIVE/short/alt version → FOLLOW the offset, don't reset
        self._last_drift = 0.0        # last audio-vs-display drift measured (sync telemetry)
        self._last_drift_t = 0.0      # when that drift was measured (time.time)
        self._pending_switch = None   # a contradicting heard song awaiting a 2nd confirming read
        self._fast_calib = 0          # remaining quick re-locks after a song change
        self._recal_after = None      # pending recalibrate timer id
        self._live_mode = False       # concert/compilation → sound-only, no title-match
        self._mv_mode = False         # MV/cinematic title → expect a dead-space intro
        self._intro_anchored = True   # have we anchored past this track's intro yet?
        self._track_t0 = 0.0          # wall-clock when the current track started

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
        self._pending_corr = 1e9   # drop any pending large-offset confirmation
        if self._sync_confirm_after is not None:   # cancel a pending confirm listen
            try:
                self.root.after_cancel(self._sync_confirm_after)
            except Exception:
                pass
            self._sync_confirm_after = None
        self._pending_switch = None  # drop any pending song-switch confirmation
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
        self._live_arrangement = is_live_arrangement(title)  # LIVE/short/alt → follow offset
        log.info("track change: %r / %r (dur %s)%s", title, artist, duration,
                 " [live-arrangement]" if self._live_arrangement else "")

        # For covers, use the original artist (extracted from the title) for
        # lyric search instead of the covering channel — "Coffee - Alka | Lumi"
        # should search "Coffee" by "Alka", not by "Lumi".
        fetch_artist = artist
        if self._is_cover and self._cover_original_artist:
            fetch_artist = self._cover_original_artist
            log.info("cover: using original artist %r instead of channel %r",
                     fetch_artist, artist)

        self._live_mode = is_live_or_compilation(title, duration)
        if self._live_mode:
            # A concert / live / festival / compilation: the title is the EVENT,
            # not a song. Title-matching it is what made a whole concert show one
            # song's lyrics — so refuse the title entirely and let SOUND drive.
            # The song-change detector + the fast re-ID loop pick up each track.
            log.info("live/compilation title → ignoring title, identifying by sound")
            self.lines, self._lyrics_path, self.idx = [], None, -1
            self._kara = []
            self._verified = False
            self._hint("🎤 Live set — listening for each song…")
        else:
            # Provisional: show the title/artist match instantly (so there's no
            # dead air) — but AUDIO is primary and confirms/overrides it below.
            # Try original artist first for covers, then fall back to channel.
            path = self.index.match(fetch_artist, title, duration)
            if not path and fetch_artist != artist:
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
            return True
        except Exception:
            return True

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
                        # The detector is listening for the next song, so blind
                        # polling is wasteful — just re-lock timing slowly. (In a
                        # live set songs often segue with no silent gap for the
                        # detector to catch, so live mode keeps polling instead.)
                        nxt = max(self.recal_secs, 25)
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
                self._verified = True
                self._title_locked = True          # OCR is authoritative in a concert
                self._sound_song = (title, artist)
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
                    self._pending_switch = None     # current song reconfirmed
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
                                    new = (corr if abs(self.offset) < DEADBAND
                                           else 0.6 * corr + 0.4 * self.offset)
                                    self.offset = round(new, 2)
                                    self._pending_corr = corr
                                    log.info("sync(live): following → offset %+.2fs (drift was %+.2f)",
                                             self.offset, diff)
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
                                    self.offset = 0.0
                            elif abs(corr) <= DEADBAND:
                                # audio says NO offset needed but we're showing one → drifted →
                                # reset to the exact player clock (manual "reset to 0", automatic).
                                self._pending_corr = 1e9
                                log.info("sync: audio_off≈0 but showing %+.2fs → AUTO-RESET to 0", self.offset)
                                self.offset = 0.0
                                self._last_sound_lock_t = time.time()
                                self._drift_integral = 0.0
                            elif abs(corr - self._pending_corr) < AGREE:
                                # real non-zero offset CORROBORATED by a 2nd agreeing read → apply.
                                self.offset = round(corr, 2)
                                self._pending_corr = 1e9
                                log.info("sync: CONFIRMED offset %+.2fs (two reads agree) → applied", corr)
                                self._last_sound_lock_t = time.time()
                                self._drift_integral = 0.0
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
                elif self._title_locked:
                    # The lyrics came from a confident EXACT match on a clean
                    # official title, but Shazam heard a DIFFERENT song — almost
                    # always a mis-ID of another track by the SAME artist
                    # (feelingradation heard as SKAVLA). Trust the title; ignore it.
                    log.info("ignoring sound %r — title-locked to %r",
                             f_title, self.meta.get("title", ""))
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
        lang = self._gen_lang

        def work():
            try:
                res = deep_transcribe.deep_transcribe(title, artist, lang=lang)
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
        # Measure the interval between consecutive RENDER frames (only) for the
        # /status render_fps readout — paused/no-music frames use a slower cadence
        # and would skew it, so they're excluded.
        now = time.time()
        if self._render_frame and self._last_tick_t is not None:
            dt = (now - self._last_tick_t) * 1000.0
            if 0.0 < dt < 500.0:
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
        self._check_monitors(now)
        state = self.media.get()

        if not state or not state["title"]:
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
            self._clean_artist_cache = clean_artist(rawa)
            # Only EXPLICIT covers (歌ってみた / "covered by" / [COVER]) take
            # the loose title-first path. Routing VTuber-channel uploads through
            # it too was WRONG: for a generic title like "Lucky Star" the
            # title-only search grabbed a same-titled DIFFERENT song.
            self._is_cover = is_cover_title(rawt)
            if self._is_cover:
                orig_a, song = extract_cover_original(rawt, self._clean_artist_cache)
                self._cover_original_artist = orig_a
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

        pos = state["position"] + self.offset

        if self.scroll_dir in ("lr", "rl"):       # continuous scroll-through
            self._ticker_update(pos)
            self._render_frame = True
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

        self._render_frame = True
        self.root.after(self._fps, self._tick)

    # ── drawing ──

    def _render(self, ln):
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

        # anchor the whole block: near the top, or near the bottom of the window
        # anchor within the FIXED window: near the top, or above the media bar
        dy = (self._win_margin if self.position == "top"
              else max(self._win_margin, self.work_h - cur_y - self._bottom_clear))
        self.cv.move("cur", 0, dy)
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
        if d in ("lr", "rl", "none", "off", "stationary"):
            return                               # scroll handled by the ticker
        ox = 460 if d == "right" else -460       # slide in from right / left, once
        self.cv.move("cur", ox, 0)
        self._anim_step(ox, 0)

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

    def _paint_one_layer(self, spec, color):
        """Render the block once, all glyphs in `color` (None = each row's own
        base color), plus furigana. One layer of the karaoke composite."""
        w, h = max(1, spec["w"]), max(1, spec["h"])
        sw = self._stroke_w()
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        for text, cx in spec["furi"]:
            d.text((cx, spec["furi_y"]), text, font=spec["furi_font"], fill=FURI_C,
                   anchor="mm", stroke_width=1, stroke_fill=INK)
        for row in spec["rows"]:
            col = color if color is not None else row["base"]
            for ch, cx in row["chars"]:
                d.text((cx, row["y"]), ch, font=row["font"], fill=col, anchor="lm",
                       stroke_width=sw, stroke_fill=INK)
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

    def _render_img_block(self, i, frac):
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
        lane_y = self._lane_y0 + (i % self._lanes) * self._lane_gap
        self.cv.create_image(0, lane_y, image=photo, anchor="nw", tags=(tag, "strm"))
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

    def _ticker_update(self, pos):
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
        if self._tick_n % int(self._tune.get("fill_skip", self._fill_skip) or 1):
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
        budget_ms = self._tune.get("heavy_budget_ms", 10.0)
        def _over_budget():
            return budget_ms > 0 and (time.perf_counter() - t_heavy) * 1000.0 > budget_ms
        # Spawn missing blocks, nearest-to-centre (most imminent) first.
        missing = sorted((i for i in want if i not in have),
                         key=lambda i: abs(want[i] - center))
        spawn_budget = int(self._tune.get("spawn_budget", self._spawn_budget))
        for i in missing[:spawn_budget]:
            if _over_budget():
                break
            cx = want[i]
            ln = self.lines[i]
            dur = ln.end - ln.start
            frac = (pos - ln.start) / dur if (ln.start <= pos < ln.end and dur > 0) else 0.0
            b = self._spawn_block(i, frac)
            self.cv.move(b["tag"], (cx - b["w"] / 2) - b["x"], 0)
            self._stream.append(b)
        # Despawn off-screen blocks, and advance karaoke fills — capped per pass.
        now = time.time()
        repaints = 0
        repaint_budget = int(self._tune.get("repaint_budget", self._repaint_budget))
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
            frac = (pos - ln.start) / dur if (ln.start <= pos < ln.end and dur > 0) else 0.0
            if b.get("img"):
                n = int(frac * b["nchars"] + 0.5)
                # Each fill repaint is a costly PhotoImage paste, so cap the
                # repaints per frame, each block's repaint rate, AND the total
                # heavy-frame time — a karaoke sweep at ~5fps reads fine.
                if (n != b["sung_n"] and repaints < repaint_budget
                        and now - b.get("paint_t", 0.0) >= self._fill_interval
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

    def _spawn_block(self, i, frac):
        """One image block (fast) if possible, else fall back to text items."""
        if self._use_img:
            try:
                return self._render_img_block(i, frac)
            except Exception:
                self._use_img = False     # disable images if rendering fails
        return self._render_block(i)

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
        self.offset += d

    def reset_offset(self):
        self.offset = 0.0

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
                "scroll_mode": self.scroll_dir in ("lr", "rl"),
                "offset_history": self._offset_hist[-20:],
            },
            "energy_align": self._last_energy,
            "fps": {
                "target": target_fps,
                "render": render_fps,
                "frame_ms": round(self._frame_ms, 1),
                "worst_ms": round(self._frame_worst, 1),
                "jitter_ms": round(self._frame_jitter, 1),
                "recent_ms": hist,
                "perf_mode": self.perf,
                "scroll_dir": self.scroll_dir,
            },
            "aligning": self._aligning,
            "identifying": self._identifying,
        }

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
                "cover_original_artist": self._cover_original_artist,
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

    def set_tune(self, key, value):
        """Set one live-tunable sync parameter. Returns (ok, message). Coerces
        the value to the existing type. Only known keys accepted — silent reject
        of unknowns is a footgun for tuning."""
        if key not in self._tune:
            return False, f"unknown tune key {key!r}"
        try:
            old = self._tune[key]
            new = type(old)(value)
        except Exception as e:
            return False, f"can't coerce {value!r} to {type(self._tune[key]).__name__}: {e}"
        self._tune[key] = new
        log.info("tune: %s %r → %r", key, old, new)
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
            self.opacity, self.position, self.scroll_dir = 0.45, "top", "left"
            self.font_scale, self.perf = 1.0, "fast"
        elif name == "karaoke":       # big, flowing lyrics for a room of people
            self.opacity, self.position, self.scroll_dir = 1.0, "bottom", "rl"
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
        if len(recent) < 4:
            return False
        # vocals = ratio meaningfully over the instrumental floor (relative when a
        # baseline was learned, else an absolute fallback), sustained across MOST
        # of the window so a single instrumental stab can't trip the release.
        thresh = max(0.20, base * 1.5) if base > 0 else 0.24
        above = sum(1 for r in recent if r >= thresh)
        return above >= max(3, int(0.6 * len(recent)))

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
        _save_settings({"opacity": self.opacity, "position": self.position,
                        "scroll": self.scroll_dir, "font_scale": self.font_scale,
                        "scroll_speed": self.scroll_speed, "perf": self.perf,
                        "recal_secs": self.recal_secs, "git_sync": self.git_sync,
                        "character": self.character_on, "api": self.api_on,
                        "boundary": self.boundary_on, "generate": self.generate_on,
                        "captions": self.captions_on,
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

    def set_now_url(self, url):
        """The browser tells us the EXACT video URL currently playing, so the
        caption fetch hits that exact upload (not a fuzzy title search). A new
        URL re-arms the per-song caption fetch."""
        url = (url or "").strip() or None
        if url and url != self._now_url:
            self._now_url = url
            self._caption_song = None        # re-fetch captions for the exact video
            if (self.captions_on and self._track and not self._live_mode
                    and (self.meta.get("source") or "") != "youtube-captions"):
                self.root.after(300,
                                lambda t=self._track_seq: self._maybe_fetch_captions(t))

    def _click_through(self):
        """(Re)assert the overlay's click-through window style.

        CRITICAL: this MUST be re-applied after every ``-alpha`` /
        ``-transparentcolor`` change. On Windows, tkinter resets the window's
        EXTENDED style when those are set, which silently DROPS our
        ``WS_EX_TRANSPARENT`` bit — turning the full-screen overlay into a window
        that EATS every click (you can't click your game/app underneath). That is
        the "can't click anything in game" bug: it appeared the moment the opacity
        changed (e.g. applying the 45%-opacity Gaming preset).

        WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW keep it from stealing focus or adding a
        taskbar button; WS_EX_LAYERED + WS_EX_TRANSPARENT make every pixel pass
        mouse input straight through to whatever is below."""
        try:
            u = ctypes.windll.user32
            hwnd = u.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
            GWL_EXSTYLE = -20
            WS_EX = 0x08000000 | 0x00000080 | 0x00080000 | 0x00000020  # NOACTIVATE|TOOLWINDOW|LAYERED|TRANSPARENT
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if (ex & WS_EX) != WS_EX:                  # only re-apply if a bit was lost
                u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX)
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

    def set_position(self, p):
        self.position = p
        self.root.geometry(f"{self.W}x{self.H}+{self.work_left}+{self._geom_y()}")
        self.root.attributes("-topmost", True)
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
            cy = (self._win_margin + 40 if self.position == "top"
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
            self._tune["fill_skip"] = float(self._fill_skip)

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
        # First-lane Y inside the fixed window: top-anchored or bottom-anchored.
        stack = self._block_h + self._lane_gap * (self._lanes - 1)
        if self.position == "top":
            self._lane_y0 = self._win_margin + self._lane_top
        else:
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

    def align_by_listening(self, silent=False):
        """On-demand: transcribe a few seconds of the live vocals and match them
        to the loaded lyrics to set the sync offset — fixes timing when Shazam
        can't identify the exact cut. Opt-in, runs once in a background thread,
        and no-ops gracefully if faster-whisper isn't installed.

        ``silent=True`` suppresses hints for the background auto-align that runs
        periodically — the user shouldn't see "Listening to sync…" every minute
        when nothing's wrong."""
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

        def work():
            res = None
            try:
                res = align.capture_and_align(lines, lang=lang, get_pos=self._align_pos)
            except Exception as e:
                log.info("align error: %s", e)
            self.root.after(0, lambda: self._apply_align(res))

        threading.Thread(target=work, daemon=True).start()

    def _track_start_auto_align(self, track_seq):
        """Fires once ~25 s into a new track, IF this is still that track. A
        quick early sync-by-ear catches cuts Shazam can't fingerprint right
        when the song settles in, instead of waiting for the periodic loop."""
        if track_seq != self._track_seq:
            return
        self._maybe_auto_align(reason="track-start")

    def _maybe_auto_align(self, reason="periodic"):
        """Background, automatic sync-by-listening. Runs only when conditions are
        right: lyrics loaded, song playing, no other alignment in flight, not in
        live mode (whole event), and Shazam hasn't locked the offset very
        recently. Cheap to call — no-ops when conditions aren't met."""
        if self._aligning or not self.lines or self._live_mode:
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
        # is more authoritative than re-checking by ear.
        if reason != "drift" and now - self._last_sound_lock_t < self._tune["shazam_lock_grace"]:
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
            return
        if ambiguous:
            log.info("energy-align: ambiguous — best %+.1fs (%.3f) vs rival %+.1fs (%.3f), "
                     "margin %.3f < %.3f → no change (chorus repetition)",
                     best_shift, best_score, rival_shift, rival_score,
                     best_score - rival_score, margin)
            return
        # best_shift is the offset to ADD to audio_t so the audio mask aligns
        # to the LRC. Since the displayed song-time = player_pos + self.offset,
        # the new offset becomes (current offset + best_shift).
        new_off = round(self.offset + best_shift, 2)
        if abs(new_off) > self._tune["energy_max_offset"]:
            log.info("energy-align: candidate offset %+.1fs out of range — skipped", new_off)
            return
        if abs(new_off - self.offset) < self._tune["energy_apply_min"]:
            log.info("energy-align: drift %+.2fs within tolerance — no change",
                     new_off - self.offset)
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
        blended = (1.0 - alpha) * prev + alpha * new_off
        self.offset = round(blended, 2)
        self.idx = -1
        # Update drift integral state — a successful correction zeros it.
        self._drift_integral = 0.0
        log.info("energy-align: offset %+.2fs → %+.2fs (α=%.2f, score %.3f, lift %.3f)",
                 prev, self.offset, alpha, score, lift)
        self._hint(f"🎤 Auto-synced ({self.offset:+.1f}s)")

    def _periodic_auto_align(self):
        """Continuous algorithmic sync heartbeat: every ~15 s, check the
        energy correlation against the LRC. Replaces the old strike-counter
        approach — every correlation reading is treated as a measurement and
        either applied (if the peak is sharp and the change is meaningful) or
        discarded silently. No song-specific thresholds.

        15 s cadence is a balance: tight enough to catch drift within a stanza,
        loose enough that Shazam-confirmed offsets don't get churned and the
        rolling vocal buffer (60 s) builds enough new signal between runs."""
        try:
            self._maybe_auto_align(reason="periodic")
        finally:
            self._auto_align_after = self.root.after(
                int(self._tune["continuous_recal_ms"]), self._periodic_auto_align)

    def _apply_align(self, res):
        self._aligning = False
        silent = getattr(self, "_auto_align_silent", False)
        self._auto_align_silent = False
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
        self.offset = round(offset, 2)
        log.info("aligned by listening: offset=%.2fs (match %.2f)%s",
                 offset, ratio, " [auto]" if silent else "")
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
    # threads to the LATER cores (upper half, never core 0) leaves the audio
    # path clean on a typical 4+ core machine; on 2-3 cores we just avoid core 0.
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
            for c in range(n // 2, n):       # upper half: cores N/2 … N-1
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
    def _align(*_):   ov.root.after(0, ov.align_by_listening)
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
    def _get_caps(*_):     ov.root.after(0, ov.load_youtube_captions)

    # ── Optional GPU acceleration ────────────────────────────────────────
    # Transcription runs on the CPU by default (fine — 16s clip in ~2s). On an
    # NVIDIA GPU the user can opt in to CUDA, which is a bit faster; the ~1.5 GB of
    # libraries are downloaded on demand (gpu_setup) instead of bloating everyone's
    # install. The item is hidden entirely on machines with no NVIDIA GPU.
    _gpu = {"busy": False}

    def _gpu_label(i=None):
        if _gpu["busy"]:
            return "⏳  Installing GPU acceleration…"
        return ("⚡  GPU acceleration: on" if gpu_setup.gpu_ready()
                else f"⚡  Enable GPU acceleration (~{gpu_setup.APPROX_MB} MB)")

    def _on_gpu(icon_, *_):
        if _gpu["busy"] or gpu_setup.gpu_ready() or not gpu_setup.nvidia_gpu_present():
            return
        _gpu["busy"] = True
        try: icon_.update_menu()
        except Exception: pass
        try: icon_.notify("Downloading CUDA libraries (~1.5 GB) in the background — "
                          "keep using the app; GPU kicks in when it's done.",
                          "Desktop Karaoke")
        except Exception: pass
        def _do():
            ok = gpu_setup.download_gpu_libs(log=log.info)
            _gpu["busy"] = False
            try: icon_.update_menu()
            except Exception: pass
            try: icon_.notify(
                "GPU acceleration enabled — used from the next song on." if ok
                else "Couldn't enable GPU acceleration; staying on CPU.",
                "Desktop Karaoke")
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
            try: icon_.notify(f"Updating to v{info['version']}…", "Desktop Karaoke")
            except Exception: pass
            def _do():
                if updater.stage_update(info, log=log.info):
                    ov.root.after(0, lambda: _quit(icon_))   # exit so the helper swaps + relaunches
            threading.Thread(target=_do, daemon=True).start()
            return
        try: icon_.notify("Checking for updates…", "Desktop Karaoke")    # manual check
        except Exception: pass
        def _check():
            got = updater.check()
            _upd["info"] = got
            try: icon_.update_menu()
            except Exception: pass
            try: icon_.notify(
                f"Update v{got['version']} available — open the tray menu to install." if got
                else f"You're up to date (v{updater.current_version()}).", "Desktop Karaoke")
            except Exception: pass
        threading.Thread(target=_check, daemon=True).start()

    def _on_update_found(info):                    # background check found one
        _upd["info"] = info
        try: icon.update_menu()
        except Exception: pass
        try: icon.notify(f"Desktop Karaoke v{info['version']} is available.", "Update available")
        except Exception: pass

    menu = pystray.Menu(
        pystray.MenuItem("Presets", preset_menu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("⚑  Wrong lyrics — fix this song", _wrong),
        pystray.MenuItem("🎧  Identify by sound", _ident),
        pystray.MenuItem("🎤  Sync by listening  (match lyrics to the audio)", _align),
        pystray.MenuItem("Fast song-change detect (compilations)", _toggle_bound,
                         checked=lambda i: ov.boundary_on),
        pystray.MenuItem("Use YouTube captions (accurate, for browser videos)", _toggle_caps,
                         checked=lambda i: ov.captions_on),
        pystray.MenuItem("⬇  Get captions for this video now", _get_caps),
        pystray.MenuItem("Generate lyrics by ear when none found (AI, ***)", _toggle_gen,
                         checked=lambda i: ov.generate_on),
        pystray.MenuItem(_gpu_label, _on_gpu,
                         enabled=lambda i: not _gpu["busy"] and not gpu_setup.gpu_ready(),
                         visible=lambda i: gpu_setup.nvidia_gpu_present()),
        pystray.MenuItem("Auto re-sync by sound", recal_menu),
        pystray.MenuItem("Library backup (Git)", git_menu),
        pystray.MenuItem("📥  Import playlist (Spotify / YouTube)", _open_import),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda i: f"Sync timing  ({ov.offset:+.1f}s)", sync_menu),
        pystray.MenuItem("Opacity", opacity_menu),
        pystray.MenuItem("Font size", font_menu),
        pystray.MenuItem("Position", position_menu),
        pystray.MenuItem("Display", display_menu),
        pystray.MenuItem("Scroll-in", scroll_menu),
        pystray.MenuItem("Scroll-through speed", speed_menu),
        pystray.MenuItem("Performance", perf_menu),
        pystray.MenuItem("Dancing character", _toggle_char,
                         checked=lambda i: ov.character_on),
        pystray.MenuItem("Local API (agent control)", _toggle_api,
                         checked=lambda i: ov.api_on),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start with Windows", _toggle_startup,
                         checked=lambda i: startup_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Re-fetch lyrics", _refetch),
        pystray.MenuItem("Show / Hide", _toggle),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(_upd_label, _on_updates),
        pystray.MenuItem(f"ℹ️  About  ·  v{version.__version__}", _about),
        pystray.MenuItem("Quit", _quit),
    )
    icon = pystray.Icon("desktop-karaoke", make_icon(), "Desktop Karaoke", menu)
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
                                   "Desktop Karaoke", menu)
            except Exception:
                pass
    threading.Thread(target=_tray_runner, daemon=True).start()
    ov.run()


if __name__ == "__main__":
    main()
