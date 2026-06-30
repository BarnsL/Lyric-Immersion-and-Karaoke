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
import hashlib
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
from metrics import ReleaseMetrics
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

# ── success/failure telemetry ──────────────────────────────────────────
# Module-level so the title→cache matcher (a plain function) can record hits
# without an app handle; the app folds it into /diag.success_rate alongside its
# own per-instance counters.
_TITLE_STATS = {"hit": 0, "miss": 0}

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
# OCR burned-in-lyrics harvest tuning (TICKET-120): read the words off the VIDEO when no
# provider/caption/bundle lyrics exist (niche Vocaloid etc.), BEFORE falling to AI gen.
OCR_HARVEST_INTERVAL_S   = 1.6    # Tk-thread throttle between polls (encode hold lands on a frame)
OCR_HARVEST_HARD_CAP_S   = 45.0   # absolute wall cap from _track_t0
OCR_EMPTY_POLLS_GIVEUP   = 8      # consecutive empty/filtered polls (~13s) with 0 commits → generate
OCR_MIN_COMMITS_TO_TRUST = 3      # committed distinct lines before OCR supersedes generation
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

def _session_key(source_app, title):
    """TICKET-117: stable 16-hex composite id for an SMTC session, hashed off
    (lowercased source_app, lowercased title). Two Brave tabs collide under
    source_app alone — adding the title disambiguates. Artist is excluded
    because YouTube/SMTC occasionally updates artist a beat after title, which
    would flip the id mid-track."""
    base = f"{(source_app or '').lower()}||{(title or '').lower()}"
    return hashlib.sha1(base.encode("utf-8", "replace")).hexdigest()[:16]


class MediaWatcher:
    """Polls the OS media session in a background thread."""

    def __init__(self):
        self._state = None
        # TICKET-117: cached full session list — each entry is the same st-dict
        # shape as _state plus an "id" key. Updated every poll under _lock.
        self._all_sessions = []
        # TICKET-117: set-of-keys digest from the prior poll, so the menu
        # refresher only fires when the visible set actually changes.
        self._sessions_sig = ""
        # TICKET-117: pinned session id (16-hex). Empty string = Auto. Set
        # from the Overlay (set_pinned_session) after settings load; the
        # async loop reads it under _lock each poll so a pin from any thread
        # takes effect on the next _pick().
        self._pinned_id = ""
        # TICKET-117: source_app captured AT pin time, kept so the optional
        # auto-migrate (same-app sole survivor) can re-pin a single-tab title
        # change without dropping to Auto.
        self._pinned_app = ""
        # TICKET-117: optional callback the Overlay registers to refresh the
        # tray menu when the visible session set changes (debounced).
        self._on_sessions_changed = None
        self._last_change_notify = 0.0
        self._lock = threading.Lock()
        self._stop = False
        self.error = None
        self._pick_src = None       # source_app of the session we're following (sticky)
        # TICKET-118: audible-session preference. When ON, _pick uses Core
        # Audio peak meters to break ties between equally-eligible PLAYING
        # sessions (pick the LOUDEST process whose executable substring-
        # matches the session's source_app). Off / unavailable → fall through
        # to the existing sticky/first-playing behavior. Three pieces of
        # state, all read under _lock so the Tk-thread setter is safe:
        #   _audible_pref_on    -- runtime flag (1=on)
        #   _audible_threshold  -- peak below this is "silent" (~0.005 = -46 dBFS)
        #   _last_pick_reason   -- 'pinned' | 'audible-pref' | 'sticky' | 'first-playing' | 'fallback'
        #   _last_audible_scores -- last per-session score dict; surfaced via diag
        self._audible_pref_on = 1
        self._audible_threshold = 0.005
        self._last_pick_reason = "init"
        self._last_audible_scores: dict = {}
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
                # TICKET-117: enumerate ALL sessions once per poll so list_sessions()
                # and the tray menu builder don't have to hit WinRT from the Tk thread.
                # _pick() now consumes this same list (also gets pin awareness).
                try:
                    raw_sessions = list(mgr.get_sessions())
                except Exception:
                    raw_sessions = []
                all_states = []
                for rs in raw_sessions:
                    try:
                        rs_info = await rs.try_get_media_properties_async()
                        rs_tl = rs.get_timeline_properties()
                        rs_pb = rs.get_playback_info()
                        try:
                            rs_rate = float(rs_pb.playback_rate) if rs_pb.playback_rate else 1.0
                        except Exception:
                            rs_rate = 1.0
                        if rs_rate <= 0:
                            rs_rate = 1.0
                        rs_pos = rs_tl.position.total_seconds()
                        try:
                            rs_lu = rs_tl.last_updated_time
                            if rs_pb.playback_status == PLAYING and rs_lu.year > 1:
                                rs_pos += (datetime.now(timezone.utc) - rs_lu).total_seconds() * rs_rate
                        except Exception:
                            pass
                        rs_src = (rs.source_app_user_model_id or "").lower()
                        rs_title = rs_info.title or ""
                        all_states.append({
                            "id": _session_key(rs_src, rs_title),
                            "title": rs_title,
                            "artist": rs_info.artist or "",
                            "album": (getattr(rs_info, "album_title", "") or ""),
                            "status": rs_pb.playback_status,
                            "position": max(0.0, rs_pos),
                            "duration": rs_tl.end_time.total_seconds(),
                            "rate": rs_rate,
                            "source": rs_src,
                            "ts": time.time(),
                            "_sess": rs,            # for _pick(); stripped from snapshots
                        })
                    except Exception:
                        continue
                # Publish the cache (without the live winsdk handle) and detect
                # set-of-keys changes so the tray menu only rebuilds on real diff.
                snapshot = [{k: v for k, v in s.items() if k != "_sess"}
                            for s in all_states]
                sig = "|".join(sorted(f"{s['id']}:{int(s['status'])}" for s in snapshot))
                pinned_id, _pinned_app, on_changed = (
                    self._pinned_id, self._pinned_app, self._on_sessions_changed)
                with self._lock:
                    self._all_sessions = snapshot
                    sig_changed = (sig != self._sessions_sig)
                    self._sessions_sig = sig
                sess = self._pick(all_states, pinned_id)
                if sess:
                    st = {
                        "title": sess["title"], "artist": sess["artist"],
                        "album": sess["album"], "status": sess["status"],
                        "position": sess["position"], "duration": sess["duration"],
                        "rate": sess["rate"], "source": sess["source"],
                        "ts": sess["ts"],
                    }
                    with self._lock:
                        self._state = st
                else:
                    with self._lock:
                        self._state = None
                # Debounced menu refresh: at most once per 2s, only on real diff.
                if sig_changed and on_changed:
                    now = time.time()
                    if now - self._last_change_notify >= 2.0:
                        self._last_change_notify = now
                        try:
                            on_changed()
                        except Exception:
                            pass
            except Exception:
                mgr = None                      # drop a stale manager; re-request next poll
            await asyncio.sleep(0.15)   # position is extrapolated, so 0.15s polling
            #                              keeps accuracy while cutting CPU ~33%

    def _pick(self, sessions, pinned_id):
        """Pick the media session to follow.

        TICKET-117: if a pin is active, the pinned session wins absolutely —
        even when paused (the user's literal scenario is a MUTED video on a
        sibling tab that SMTC still reports as 'playing'; pin must beat the
        playing-priority, not be subordinate to it). When the pinned id is set
        but its session is gone, we deliberately return None so MediaWatcher.get()
        holds last state via the existing extrapolation path — falling back to
        any other session during a pin is the ping-pong bug the pin exists to fix.

        Without a pin: STICKY — prefer a PLAYING session, preferring the one we
        were already following; when nothing is playing (a transition gap) KEEP
        following the last session instead of jumping to a different paused tab.
        Same logic as before TICKET-117, just sourced from the pre-enumerated list.
        """
        if pinned_id:
            for s in sessions:
                if s["id"] == pinned_id:
                    return s
            self._last_pick_reason = "pinned-missing"
            return None

        def sid(s):
            return s["source"]

        def playing(s):
            try:
                return s["status"] == PLAYING
            except Exception:
                return False

        playing_now = [s for s in sessions if playing(s)]
        # TICKET-118: audible-session tiebreaker. When MULTIPLE sessions are
        # PLAYING (the user's literal scenario — Brave Tab A muted + Brave
        # Tab B audible BOTH report 'playing' to SMTC), score each by the
        # peak amplitude of any process whose executable basename appears as
        # a substring of the session's source_app. The loudest wins.
        # AUMIDs like 'app.brave.brave' → match against 'brave'; opaque UWP
        # ids score 0.0 and we fall through to sticky behavior.
        #
        # Pin (above) > audible-pref (here) > sticky (below). Done as a
        # PRE-FILTER before sticky so a muted-but-still-playing Tab A can't
        # hijack the lock just because we saw it first.
        if len(playing_now) >= 2 and self._audible_pref_on:
            try:
                levels = self._score_sessions_by_audio(playing_now)
            except Exception:
                levels = {}
            self._last_audible_scores = levels
            if levels:
                # Pick highest-scoring session whose score clears the floor.
                best_id, best_score = None, 0.0
                for s in playing_now:
                    score = levels.get(s["id"], 0.0)
                    if score > best_score:
                        best_id, best_score = s["id"], score
                if best_id is not None and best_score >= self._audible_threshold:
                    for s in playing_now:
                        if s["id"] == best_id:
                            self._pick_src = sid(s)
                            self._last_pick_reason = "audible-pref"
                            return s
            # else: no usable signal (pycaw unavailable / no matching process
            #       / all silent) — fall through to sticky behavior.
        else:
            self._last_audible_scores = {}
        # 1) keep following our session if it's still playing (stability)
        if self._pick_src:
            for s in playing_now:
                if sid(s) == self._pick_src:
                    self._last_pick_reason = "sticky"
                    return s
        # 2) otherwise the first playing session — and remember it
        if playing_now:
            self._pick_src = sid(playing_now[0])
            self._last_pick_reason = "first-playing"
            return playing_now[0]
        # 3) NOTHING is playing (likely a gap between Mix tracks). Do NOT jump to
        #    a paused tab — keep the session we were following if it still exists,
        #    so the overlay holds the current song through the gap.
        if self._pick_src:
            for s in sessions:
                if sid(s) == self._pick_src:
                    self._last_pick_reason = "sticky-paused"
                    return s
        # 4) Final fallback: ANY session (the original get_current_session()
        #    behavior, but sourced from our cache).
        if sessions:
            self._last_pick_reason = "fallback"
            return sessions[0]
        self._last_pick_reason = "none"
        return None

    def _score_sessions_by_audio(self, sessions):
        """TICKET-118: map {session_id: peak_amplitude} by substring-matching
        each session's source_app (e.g. 'app.brave.brave') against the list
        of audible process basenames (e.g. 'brave' → 0.42).

        Why substring: SMTC SourceAppUserModelId is NOT a stable mapping to a
        process — Brave reports 'app.brave.brave' for every window/tab while
        the executable is 'brave.exe'. Substring on the executable basename
        (no path, no '.exe') is the cheapest match that handles both
        traditional Win32 apps (Spotify → 'spotify') and the AUMID-as-id
        case. Caller already filtered to multi-session-playing case.

        Returns {} on any failure or when audible_sessions is unavailable.
        Caches nothing — audible_sessions.get_process_audio_levels has its
        own ~1s cache.
        """
        try:
            import audible_sessions
            levels = audible_sessions.get_process_audio_levels()
        except Exception:
            return {}
        if not levels:
            return {}
        out = {}
        for s in sessions:
            src = (s.get("source") or "").lower()
            if not src:
                continue
            # Highest peak across every process whose basename appears in src.
            best = 0.0
            for name, peak in levels.items():
                if name and name in src:
                    if peak > best:
                        best = peak
            out[s["id"]] = best
        return out

    def get(self):
        with self._lock:
            if not self._state:
                return None
            s = dict(self._state)
        if s["status"] == PLAYING:
            s["position"] += (time.time() - s["ts"]) * s.get("rate", 1.0)
        return s

    def list_sessions(self):
        """TICKET-117: snapshot of every SMTC session the watcher last saw.
        Each entry: {id, source, title, artist, album, status, position,
        duration, rate, ts}. Served from the cache, no WinRT call — safe to
        invoke from the Tk thread (tray menu builder)."""
        with self._lock:
            return [dict(s) for s in self._all_sessions]

    def get_for_id(self, session_id):
        """TICKET-117: live state for ONE specific session by id. Returns the
        same shape as get() (position extrapolated forward to wall-clock now),
        or None when the session is not present in the current cache."""
        if not session_id:
            return None
        with self._lock:
            for s in self._all_sessions:
                if s["id"] == session_id:
                    out = dict(s)
                    break
            else:
                return None
        if out["status"] == PLAYING:
            out["position"] += (time.time() - out["ts"]) * out.get("rate", 1.0)
        return out

    def set_pinned(self, session_id, source_app=""):
        """TICKET-117: install / clear the pinned-session filter. Pass '' to
        clear. source_app is captured at pin time for the auto-migrate guard."""
        with self._lock:
            self._pinned_id = (session_id or "").strip()
            self._pinned_app = (source_app or "").strip().lower()

    def get_pinned(self):
        """TICKET-117: (pinned_id, pinned_app) tuple under lock."""
        with self._lock:
            return self._pinned_id, self._pinned_app

    def set_audible_pref(self, on, threshold=None):
        """TICKET-118: enable/disable the audible-session tiebreaker. Called
        from the Overlay whenever the prefer_audible_session tune knob flips
        and once at startup to mirror the persisted value. Thread-safe."""
        with self._lock:
            self._audible_pref_on = 1 if on else 0
            if threshold is not None:
                try:
                    self._audible_threshold = max(0.0, float(threshold))
                except Exception:
                    pass

    def get_audible_pref_diag(self):
        """TICKET-118: snapshot for /diag.audible_pref. Returns
        {'enabled', 'threshold', 'last_reason', 'scores'} — `scores` is the
        per-session-id peak from the last _pick that had multiple playing
        sessions. Never raises."""
        with self._lock:
            return {
                "enabled": bool(self._audible_pref_on),
                "threshold": float(self._audible_threshold),
                "last_reason": self._last_pick_reason,
                "scores": dict(self._last_audible_scores or {}),
            }

    def set_sessions_changed_cb(self, cb):
        """TICKET-117: register a 0-arg callable invoked (debounced, max 1/2s)
        whenever the set of visible session ids changes. Used by the Overlay
        to icon.update_menu() so the Source submenu reflects the new tabs."""
        self._on_sessions_changed = cb

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
    r"|演奏してみた|弾いてみた|叩いてみた|\bcover(?:ed)?\s+by"
    r"|\(\s*cover\s*\)|\[\s*cover\s*\]|[/／]\s*cover\b"
    # "cover" as a TAG right after any common opening bracket — 【Cover MV】,
    # ［Cover］, （Cover MV）, (Cover MV). The lenticular / fullwidth styles VTuber
    # covers use that the ASCII-paren rules above miss. This is the bug that made
    # "【Cover MV】MAFIA / マフィア - Ouro Kronii" search by the COVER channel
    # (Ouro Kronii) — which has no lyrics for it — instead of title-first.
    r"|[【\[（(［]\s*covers?\b"
    # language-prefixed cover inside a bracket: "(English Cover by …)", "[EN cover]",
    # "（Spanish cover）". The leading bracket + \b on cover-by above keep this off
    # substrings like discover / recover / undercover / recovery / hangover.
    r"|[【\[（(［]\s*\w+\s+covers?\b", re.I)


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


# CROSS-LANGUAGE cover: "(English Cover by Limina)" / "Spanish cover" / "English Ver."
# names the LANGUAGE the cover is SUNG in. When that differs from any fetchable
# (original-language) lyrics, the body can never match the audio, so the decision
# engine REGENERATES by ear in this language (see _fire_decision_action). The map
# only fires when a language word sits right before a cover/version tag, so a normal
# title ("Englishman in New York", a song titled "English", "Spanish Sahara") is inert.
_COVER_LANG = {
    "english": "en", "eng": "en", "en": "en", "spanish": "es", "espanol": "es",
    "español": "es", "es": "es", "korean": "ko", "kor": "ko", "kr": "ko",
    "japanese": "ja", "jpn": "ja", "jp": "ja", "nihongo": "ja", "french": "fr",
    "francais": "fr", "français": "fr", "fr": "fr", "chinese": "zh", "mandarin": "zh",
    "cn": "zh", "zh": "zh", "german": "de", "deutsch": "de", "de": "de",
    "portuguese": "pt", "pt": "pt", "italian": "it", "it": "it", "russian": "ru",
    "ru": "ru", "thai": "th", "vietnamese": "vi", "indonesian": "id", "tagalog": "tl",
}
_COVER_LANG_RE = re.compile(
    r"\b(" + "|".join(sorted(map(re.escape, _COVER_LANG), key=len, reverse=True))
    + r")\s+(?:covers?|ver(?:s|sion)?\.?)\b", re.I)


def cover_language(title):
    """The language a cover is SUNG in (2-letter code), or None. Fires only when a
    language word directly precedes a cover/version tag ('English Cover', 'Spanish
    ver.'), so ordinary titles never trip it."""
    if not title:
        return None
    m = _COVER_LANG_RE.search(title)
    return _COVER_LANG.get(m.group(1).lower()) if m else None


def feat_artists_from_title(title):
    """Collaborator names from a '(feat. X)' / 'ft. X' credit inside a TITLE — they are
    the real artist(s), not the uploading channel (e.g. a game's '公式' channel for its
    theme song: '【NTE】…「Play On！」（feat. Reol）' → ['Reol']). clean_title strips the
    feat tag from the search title, so capture the names here and feed them in as artist
    candidates. Stops at a trailing version/bracket tag (スタジオ版 / '(Live at …)')."""
    from fetch_lyrics import split_artists
    out = []
    for m in re.finditer(r"\b(?:feat|ft|featuring)\.?\s+([^)）\]】」』]+)", title or "", re.I):
        seg = re.split(r"[(（\[【「『]|\bver\.?\b|版", m.group(1), flags=re.I)[0]
        for nm in split_artists(seg):
            if nm and nm not in out:
                out.append(nm)
    return out


# Curated cover→original-artist hints for frequently-covered songs whose ORIGINAL
# artist appears nowhere in the cover's title (so the title-only fallback can't
# find them, and a bare-title search risks a same-title WRONG-LANGUAGE hit). This
# is METADATA only — no lyrics — so it's compatible with the no-bundled-lyrics
# policy; the body still comes from providers/captions/OCR/by-ear. Keyed by the
# cleaned, lowercased song title. The '!!!' in 'bang!!!' is load-bearing: it is
# EGOIST's Japanese song, NOT the K-pop 'BANG' / IVE 'BANG BANG'. Extend as needed.
_KNOWN_COVER_ORIGINALS = {
    "bang!!!": "EGOIST",
}


def _known_cover_original(title: str):
    """Original artist for a known frequently-covered song title, or None."""
    return _KNOWN_COVER_ORIGINALS.get((title or "").strip().lower())


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


# A 「…」/『…』 that names a CONCERT/EVENT rather than the song — it sits right after a
# live-provenance lead-in like 'from … ONE-MAN LIVE「Event」'. When detected, the real
# song is OUTSIDE the bracket (at the head), so clean_title must NOT extract the bracket
# as the song. This is the ルフラン bug: 'ルフラン (from 2nd ONE-MAN LIVE「NEUROMANCE Ⅱ」)…'
# used to resolve to 'NEUROMANCE Ⅱ' (the concert) instead of 'ルフラン' (the song).
_EVENT_BRACKET_LEADIN_RE = re.compile(
    r"(?:\bfrom\b|one[\s-]?man(?:\s*live)?|live\s*tour|\btour\b|anniversary(?:\s*live)?|"
    r"\bconcert\b|\bfes(?:tival)?\b|\bsetlist\b|"
    r"ワンマン(?:ライブ)?|ライブ|ツアー|コンサート|フェス|周年|生放送)"
    r"\s*[:：「『]?\s*$",
    re.I,
)


def _is_event_bracket(text, m):
    """True if the 「…」/『…』 matched at `m` names a concert/event (introduced by a
    'from … LIVE/ONE-MAN/TOUR' lead-in in the ~48 chars before it), so it is NOT the
    song. Used by clean_title to skip the bracket and keep the head title as the song."""
    lead = text[max(0, m.start() - 48):m.start()]
    return bool(_EVENT_BRACKET_LEADIN_RE.search(lead))


_CREDIT_SUFFIX_RE = re.compile(r"\s*[\(（\[［【][^\)）\]］】]*[\)）\]］】]\s*$")


def _strip_title_credits(t):
    """Strip trailing parenthetical/bracket credits ('(feat. X)', '(Live)',
    '[Remix]', '（…）', '【…】') so VARIANT titles of the SAME song compare equal.
    Peels repeated/nested trailing brackets. Shared by the source-agreement
    scorer (so a feat. credit isn't a false mismatch → false decision SWITCH)
    and by concert title parsing."""
    t = (t or "").strip()
    prev = None
    while t and t != prev:
        prev = t
        t = _CREDIT_SUFFIX_RE.sub("", t).strip()
    return t


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
    # THE FIRST TAKE uploads are 'Artist - Song / THE FIRST TAKE' (a one-take live
    # show with a spoken preamble + live timing). Drop the show suffix and keep the
    # SONG so it fetches the studio lyrics; is_live_arrangement handles the timing.
    m_ft = re.search(r"^(.*?)\s*[/／｜|]\s*the\s*first\s*take\b.*$", t, re.I)
    if m_ft and m_ft.group(1).strip():
        head = m_ft.group(1).strip()
        dash_parts = re.split(r"\s*[-–—]\s*", head)
        t = (dash_parts[-1].strip() if len(dash_parts) >= 2 else head)
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
    if mq and _is_event_bracket(t, mq):
        # The 「…」 is a concert name ('(from 2nd ONE-MAN LIVE「NEUROMANCE Ⅱ」)'); the
        # real song is the head before the live aside. Cut at the opening paren that
        # introduced it ('ルフラン (from …' → 'ルフラン').
        head = re.split(r"\s*[\(（]", t[:mq.start()])[0]
        head = head.strip(" -–—|/　┃│｜／・「」『』").strip()
        if head and not _is_generic_title(head):
            song = head
        mq = None                                    # consumed: don't extract the event
    if not song:
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


def _live_cue_is_parenthetical_aside(t, m):
    """True if the live cue matched at `m` sits inside a (…)/（…） aside that follows a
    real song title — e.g. 'ルフラン (from 2nd ONE-MAN LIVE「NEUROMANCE Ⅱ」)'. That marks
    ONE song performed AT an event (a live ARRANGEMENT), NOT a whole-concert video to be
    driven by sound. The whole-concert form has the live words in the MAIN title with no
    enclosing paren (e.g. '歌姫 from V.W.P 4th ONE-MAN LIVE')."""
    open_idx = max(t.rfind("(", 0, m.start()), t.rfind("（", 0, m.start()))
    if open_idx < 0:
        return False
    closers = [i for i in (t.find(")", m.end()), t.find("）", m.end())) if i != -1]
    if not closers:
        return False
    head = t[:open_idx].strip(" -–—|/　┃│｜／・「」『』")
    return len(re.sub(r"\s", "", head)) >= 2     # a substantive song title precedes the aside


# A SINGLE song LOOPED / EXTENDED into a long video — "Seamless 30min Ver",
# "1 hour loop", "作業用 BGM", "2時間耐久", "10分ループ" — is ONE song, NOT a
# multi-song concert. It would otherwise trip the >10-min concert rule and get
# driven by sound + concert-OCR (which, with another window visible, has even
# OCR'd off-video text as "lyrics"). These load the song's lyrics and FOLLOW the
# offset like any long single track.
_LOOP_VER_RE = re.compile(
    r"seamless|\bloop(?:ed|ing)?\b|"
    r"\d+\s*(?:min(?:ute)?s?|h(?:ou)?rs?)\s*(?:ver(?:sion)?|loop|mix|edit|bgm)\b|"
    r"(?:extended|continuous)\s*(?:ver(?:sion)?|mix|edit|play)\b|"
    r"作業用|\d+\s*時間|\d+\s*分\s*(?:耐久|ループ)|耐久",
    re.I,
)


_FROM_EVENT_RE = re.compile(
    r"\bfrom\b.{0,40}?(?:one[\s-]?man|live|tour|公演|ワンマン|ライヴ|ライブ|フェス|festival)",
    re.I)


def _has_single_song_at_event(title):
    """True when the title is ONE song performed at a named event — a 'from … LIVE/
    ONE-MAN/TOUR' reference WITH a real song before it (a quoted 「…」/『…』 head, or
    non-generic text). Such a video is a single-song LIVE ARRANGEMENT (follow the
    offset, trust the title, fetch its lyrics/captions), NOT a multi-song concert.
    Multi-song events are caught by the >10-min length rule and have no single-song
    head. Fixes 'V.W.P 4th ONE-MAN LIVE「現象Ⅳ」「言葉」' being driven sound-only with
    captions skipped, when it is just 言葉 performed live."""
    t = title or ""
    m = _FROM_EVENT_RE.search(t)
    if not m:
        return False
    pre = t[:m.start()]
    if re.search(r"[「『][^」』]{1,40}[」』]", pre):     # quoted song before the 'from … LIVE'
        return True
    head = pre.strip(" 　【】[]()（）-–—|/・「」『』")
    return bool(head and not _is_generic_title(head))


def is_live_or_compilation(title, duration=None):
    """True for a long video, or one whose title says 'live / concert / festival /
    medley / 3D LIVE / メドレー' — almost always MANY songs under one title, where
    the title names the EVENT, not the song. Such videos must be driven by SOUND:
    title-matching them is what makes a whole concert show one (wrong) song's
    lyrics, with no way for Shazam to override a title that's a real song name."""
    t = title or ""
    # A single song LOOPED/EXTENDED into a long video is NOT a concert — load its
    # lyrics + follow the offset, even though it's long. (Stops the >10-min rule
    # misfiring on "Seamless 30min Ver" / "作業用" / "N時間耐久" → wrong song.)
    if _LOOP_VER_RE.search(t):
        return False
    if duration and duration > 10 * 60:      # >10 min ⇒ concert/compilation (multi-song) in practice
        return True
    m = _LIVE_RE.search(title or "")
    if not m:
        return False
    # A single song performed at a concert — '<song> (from … ONE-MAN LIVE「Event」)'
    # (parenthetical aside) OR '「言葉」from V.W.P 4th ONE-MAN LIVE「現象Ⅳ」' (the head song
    # + an event reference) — is NOT a multi-song event. Leave it to is_live_arrangement
    # (FOLLOW the offset, trust the title, fetch lyrics/captions), not sound-only.
    if _live_cue_is_parenthetical_aside(title or "", m) or _has_single_song_at_event(t):
        return False
    return True


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
    r"|the\s*first\s*take|ザ・?ファーストテイク"  # THE FIRST TAKE = one-take live show (preamble pause, live timing)
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


# Bare social-media / site names that a browser tab title delivers as a fake
# "track" — the PAGE, not a song. A poisoned instagram.json (literally titled
# "Instagram") was matching the tab title and showing junk rap lyrics over
# Instagram reels. Reject these as track titles when there's no real artist
# (a genuine song titled "Instagram" by a named artist still works).
_JUNK_SITE_NAMES = frozenset({
    "instagram", "facebook", "tiktok", "twitter", "x", "reddit", "threads",
    "snapchat", "linkedin", "pinterest", "tumblr", "whatsapp", "telegram",
    "messenger", "new tab", "new private tab", "新しいタブ", "home",
})


def _is_junk_track_title(title: str, artist: str) -> bool:
    """True when `title` is a bare social/site page name with no real artist —
    i.e. the browser reported the tab, not a song. Strips a leading unread
    count '(6) ' and a trailing ' • Messages' / ' • Reels' section marker."""
    if artist and artist.strip():
        return False                      # a named artist ⇒ treat as a real song
    t = (title or "").strip().lower()
    if not t:
        return False
    t = re.sub(r"^\(\d+\)\s*", "", t)     # '(6) Instagram' → 'Instagram'
    t = re.split(r"\s*[•|]\s*", t)[0].strip()   # 'Instagram • Messages' → 'Instagram'
    t = re.sub(r"\.(com|net|org)$", "", t)      # 'instagram.com' → 'instagram'
    return t in _JUNK_SITE_NAMES


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
    """Strip any stray inline LRC timestamp tags ([mm:ss], <mm:ss>) from text,
    and the OCR-style tofu glyphs (□ / ◢ / U+FFFD / exotic whitespace) that
    can also leak in from LRC providers. TICKET-128 originally limited
    _strip_tofu() to ocr_lyrics; the BEEP BEEP / Hoshimatic Project case
    ('Stop!□Oh woah woah woah □') showed syncedlyrics can deliver them too,
    so apply it on EVERY load — covers cached files immediately without a
    re-fetch."""
    s = _TS_RE.sub("", s or "").strip()
    try:
        from ocr_lyrics import _strip_tofu
        s = _strip_tofu(s)
    except Exception:
        pass
    return s


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


_CJK_TEXT_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힣ｦ-ﾟ]")


def _has_cjk(s: str) -> bool:
    """True if `s` contains any kana / kanji / hangul — i.e. names a CJK song."""
    return bool(_CJK_TEXT_RE.search(s or ""))


def _body_is_translation(lines) -> bool:
    """True when an LRC's 'original' (jp) text IS its English translation — i.e. a
    TRANSLATION mislabeled as the song body (the アイドル → idol.json bug, where every
    line had jp == en == 'Complete and perfect' instead of the Japanese). The honest
    tell: most non-empty lines have jp.strip() == en.strip() AND carry no CJK. A real
    Japanese song's jp ≠ en; only the corrupt translation-as-body trips this."""
    nonempty = [ln for ln in lines if (getattr(ln, "jp", "") or "").strip()]
    if len(nonempty) < 4:
        return False
    same = 0
    for ln in nonempty:
        jp = (ln.jp or "").strip()
        en = (ln.en or "").strip()
        if jp and en and jp == en and not _CJK_TEXT_RE.search(jp):
            same += 1
    return same >= 0.6 * len(nonempty)


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
            _TITLE_STATS["hit"] += 1
            return best
        _TITLE_STATS["miss"] += 1
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
        # CRASH-DIALOG SUPPRESSION: a TRANSIENT GDI/bitmap shortage — e.g. a 40-min
        # concert in live mode + OCR full-frame captures (~8 MB DIBs) + Whisper all
        # churning pixmaps at one instant — can make Tk's internal drawing fail with
        # "Tk_GetPixmap: Error from CreateDIBSection — not enough memory resources"
        # even with GBs of RAM free (it's the per-process GDI bitmap heap, not RAM).
        # By default Tk pops a focus-stealing MessageBox for that, which is the worst
        # possible thing while the user games fullscreen. Route BOTH Python-callback
        # errors AND Tcl background errors (the path Tk_GetPixmap surfaces on) to the
        # log and DROP the frame; the self-rescheduling render loop recovers on the
        # next tick. Never pop a dialog.
        def _silent_tk_error(exc, val, tb):
            try:
                log.info("tk callback error (suppressed): %s: %s",
                         getattr(exc, "__name__", exc), val)
            except Exception:
                pass
        self.root.report_callback_exception = _silent_tk_error
        try:
            self.root.tk.createcommand(
                "bgerror", lambda msg: log.info("tk bgerror (suppressed): %s", msg))
        except Exception:
            pass
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
        # GPU-driven lyric renderer — DEFAULT OFF. The CPU Tk renderer is the
        # reliable default (full furigana/romaji/EN layout, confirmed working).
        # The GPU child renders at 100+ fps internally, but its full-screen
        # transparent window did NOT composite visibly on this user's display
        # even with ShowWindow + re-asserted per-pixel-alpha — so it stays
        # OPT-IN via the tray "GPU renderer" toggle (the choice persists) until
        # its on-screen visibility can be debugged with eyes on the screen.
        self.gpu_renderer_on = bool(s.get("gpu_renderer", False))
        # v1.1.42: the GPU render path is now the standalone **Tauri overlay**
        # (the separate lyric-overlay-tauri project) — a transparent,
        # click-through, always-on-top WebView with per-pixel alpha + <ruby>, fed
        # over HTTP by the /overlay endpoint. It is an ADDITIVE second renderer
        # (it never hides the Tk overlay); the tray item toggles its child
        # process on/off and the choice persists. Default OFF (opt-in).
        self.tauri_overlay_on = bool(s.get("tauri_overlay_on", False))
        self._tauri_child = None
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
        # TICKET-117: pinned SMTC session (the "watch a muted video, follow a
        # different tab for lyrics" lock). Empty string = Auto (highest-priority
        # session wins as before). Composite id is `_session_key(source, title)`.
        # `pinned_session_app` is the source_app at pin time, kept for the
        # auto-migrate guard (a single same-app session changing title is
        # likely the same tab autoplaying the next song).
        self.pinned_session_id = (s.get("pinned_session_id") or "").strip()
        self.pinned_session_app = (s.get("pinned_session_app") or "").strip().lower()
        # Wall-time when the pinned session was last seen in the watcher's
        # snapshot. While the session is missing, _tick polls this against
        # pinned_grace_s before auto-unpinning so a tab navigation / quick
        # title flicker doesn't drop the pin.
        self._pinned_last_seen_t = time.time()
        # Set true once the watcher has produced any non-empty session list —
        # gates the cold-start auto-clear (a pin restored from settings whose
        # session never shows up in the FIRST enumeration silently reverts).
        self._pinned_cold_start = True
        self._pinned_cold_start_t = time.time()
        # Tray-icon handle (set in main() after pystray.Icon() is built) — the
        # set_pinned_session() helper calls icon.update_menu() to refresh the
        # Source submenu's radio dots. Stored as an attribute so api.py / tray
        # toggles can both reach it.
        self._icon = None
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
        # per-release success/wobbler/fail telemetry (TICKET-121); GET /metrics
        self.metrics = ReleaseMetrics(version.__version__)
        self._gen_lines = []          # accumulated generated line dicts
        # OCR burned-in-lyrics harvest state (TICKET-120)
        self._ocr_harvest_seq    = None   # _track_seq we've already tried OCR for (once per track)
        self._ocr_harvest_busy   = False  # in-flight guard (single harvest thread at a time)
        self._ocr_harvester      = None   # per-track LyricOcrHarvester (fresh each track)
        self._ocr_empty_polls    = 0      # consecutive empty/filtered polls → giveup
        self._ocr_committed_seen = 0      # distinct committed lines so far (min-commits trust gate)
        self._ocr_title = self._ocr_artist = ""
        self._gen_title = self._gen_artist = ""
        self._gen_lang = None         # language auto-detected for the current generation
        self._deep_token = 0          # bumped on track change → cancels in-flight deep transcription
        self._deep_tried = set()      # song slugs we've already attempted a deep upgrade for
        self._title_locked = False    # exact clean-title match → sound can't override
        self._title_locked_at = 0.0   # wall-clock when the lock was set (fast-escalation time-gate)
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
        self._cover_lang = None      # cross-language cover: language SUNG (en/es/ko…) or None
        self._title_feat_artists = []  # artist(s) pulled from a title '(feat. X)' credit
        self._cover_original_artist = None   # original artist extracted from a cover title
        self._last_caption_t = 0.0   # throttle YouTube caption fetches (rate-limit guard)
        self._caption_song = None    # (artist,title) we last attempted captions for
        self._captions_fetching = False  # single-flight: one yt-dlp caption fetch at a time
        self._now_url = None         # exact current video URL (browser-pushed, for captions)
        # TICKET-112: parsed YouTube description metadata for the current track
        # — composer/lyricist/vocals/original_artist as DISAMBIGUATORS for the
        # provider chain. _yt_metadata_video_id natural-dedupes so a re-invoke
        # (set_now_url firing again on the same id) no-ops. Both are cleared
        # on _on_track_change so a stale prior-track value can never bleed in.
        self._yt_metadata = None
        self._yt_metadata_video_id = None
        self._yt_metadata_fetching = False  # single-flight per track
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
        self._last_line_idx = -1        # previous real line, held during short gaps
        self._gap_start_t = None        # wall-time we entered an inter-line gap
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
        # kamone fix: corroboration of the BODY (LRC text vs the actual singing),
        # INDEPENDENT of title/meta verification. Set True ONLY by positive evidence the
        # loaded lyrics match the audio: a clean energy lock (vocal on/off pattern matched
        # the LRC line grid) or a healthy by-ear read. bundled/youtube-captions are
        # body-trusted by construction. Until True, a verified+title-LOCKED song is NOT
        # immune to a by-ear body check (right title, wrong body — the kamone case).
        self._body_corroborated = False
        self._body_probe_retried = False   # one bounded extra by-ear listen per track
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
            "display_lead_s":        0.0,   # v1.1.42: ZERO lead = true Jun-26 (~v1.0.74) parity, the build the user confirmed "perfect". The lead (0.3→0.12) only ever existed to mask the choppy-/slew-limited-clock lag; that clock is now retired (pos_hi = pos), so the simple eased clock needs no lead. Bump to ~0.1 if a residual systematic lag ever reappears (highlight sits just ahead of the vocal, within the ~170ms AV-binding window)
            # ── PERCEPTUAL in-sync window (asymmetric; ITU-R BT.1359 + AV binding window) ──
            # The old symmetric ±deadband(0.8s) called anything within ~800ms "in
            # sync" — 4-9x looser than a listener can perceive. Humans tolerate the
            # highlight running slightly AHEAD of the vocal far better than BEHIND
            # (sound-before-visual is the annoying direction). So: leave it alone
            # only inside [-ahead, +behind]; correct once it drifts outside.
            "sync_win_ahead_s":      0.17,  # highlight may lead the vocal by up to this (diff<0, forgiving direction)
            "sync_win_behind_s":     0.09,  # but only lag it by this before we correct (diff>0, "lyrics late")
            "agree":                 2.0,   # 2-read agreement window (s)
            "agree_live":            4.0,   # live-arrangement agreement window
            "live_max_jump_s":      45.0,   # reject a live-follow correction that jumps >this from a STABLE offset (a chorus-repeat mismatch, e.g. -170s on a 4-min song) — not real tempo drift
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
            # ── TICKET-112: YouTube description metadata extractor ──
            # The SMTC title "Shooting Star" is hugely ambiguous (dozens of
            # songs share the name). When the user reports /wrong we used to
            # re-run the SAME (title, artist) query and get the SAME wrong
            # match back — no new signal had entered the system. yt-dlp can
            # fetch the video's DESCRIPTION cheaply (no audio, no captions)
            # and the description usually carries ground-truth credits like
            # "作詞・作曲：kors k" / "歌唱：ReGLOSS(...)". Those become
            # additional artist candidates layered onto the existing
            # fetch_lrc loop so a re-fetch can actually try a different
            # artist. Set yt_description_lookup=0 to disable for diagnosis
            # (the feature degrades silently when off).
            "yt_description_lookup":        1,   # 1 = run description extraction on browser sources; 0 = off
            "yt_description_cache_days":   30,   # on-disk-ish TTL for the LRU (in-process for now); also caps memory
            "yt_description_timeout_s":   8.0,   # yt_dlp socket_timeout for the metadata-only call
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
            "wrong_song_uncorroborated_strikes": 3,  # faster escalation once an uncorroborated body is past the energy-align window (Play On!/kamone poisoned-cache fix)
            "uncorroborated_fast_after_s":   50.0,   # only apply the reduced threshold this many s AFTER title-lock (lets energy-align corroborate a correct body first)
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
            "decide_at_s":           12.0,  # run the by-ear decision this many s into a new track (20→12: faster wrong-body detection on ambiguous covers)
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
            # ── TICKET-111: boundary-deferred lyrics swap ──
            # When the decision engine (SWITCH/REGEN), the wrong-song strike, or
            # the user's /wrong has to replace the loaded lyrics, the v1.0.92 code
            # blanked self.lines IMMEDIATELY and re-fetched, producing a 1-5s
            # blackout on screen. Instead, QUEUE the swap on self._pending_swap,
            # start the fetch right now (so latency overlaps), and KEEP rendering
            # the old lines until either the current line ends (LINE mode), the
            # scroll belt drains (SCROLL mode), or the safety cap fires.
            # Mirrors the TICKET-078 _pending_offset state machine.
            "swap_defer_enabled":            1,   # 0 = legacy immediate clear
            "swap_defer_max_s":            8.0,   # safety cap: force commit after this
            "swap_defer_instrumental_gap_s": 2.0, # idx==-1 LINE-mode gap that counts as a boundary
            "swap_defer_user_max_s":       3.0,   # tighter cap for user-driven /wrong
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
            "energy_lift_floor":     0.045, # min peak-vs-median lift to accept (was 0.10; kamone's correct shift had lift≈0.049 and was being rejected, so the cache loaded right but sync never locked). Rival-peak margin + Shazam sanity + energy_apply_min still guard false alarms.
            "energy_max_offset":    60.0,   # |new_off| < this for sanity
            "energy_shift_penalty":  0.012, # per-second penalty for large offset changes (small-shift prior)
            "energy_peak_margin":    0.06,  # reject if a distant rival peak is within this of the best
            "keep_last_line_gap_s":  0.6,   # CPU renderer: hold the previous line on-canvas during inter-line gaps shorter than this, to kill the disappear/reappear flicker (GPU renderer holds independently)
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
            "fine_tune_max_pause_s":      5.0,  # TICKET-104: 1.0 -> 3.0 -> 5.0 per user; holding a line still up to 5 s is quieter than the equivalent backward nudge that re-scrolls already-shown text
            "fine_tune_max_move_ahead_s": 2.0,  # biggest backward-drift catch-up nudge (lyrics behind); higher cap because skipping forward is less perceptible than pausing
            "fine_tune_exit_drift_s":     5.5,  # TICKET-104: must be > fine_tune_max_pause_s + 0.5 buffer (now 5.0+0.5) so a drift just under the cap doesn't immediately hand back to the tier
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
            "ocr_when_gaming":              0,  # TICKET-125: 1 = allow OCR (capture+WinRT-OCR, GPU-backed) even while a game uses the GPU; 0 = back off so it can't hitch the game
            # ── TICKET-129 CPU policy ──
            # 1 (default) = "the last core drives the product": pin this process to
            # the LAST PHYSICAL core and run it ABOVE_NORMAL. Dedicating one core keeps
            # the overlay perfectly smooth while a game (using the other cores) is
            # barely touched. 0 = legacy spread (upper cores + BELOW_NORMAL), which is
            # better on a CPU-only box doing heavy lyric *generation* (it can then use
            # many cores). Hardware-agnostic: the mask is computed from the live CPU
            # topology, so it is correct on 2..64-thread machines, SMT or not.
            "cpu_dedicate_last_core":       1,
            # ── GPU-driven lyric renderer (M2) ──
            # 1 = spawn gpu_renderer.py as a child process, feed it state over
            # stdin NDJSON, hide the Tk overlay window. The child window does
            # the actual drawing on the idle GPU (3080 eGPU here) at 100+ FPS
            # with per-pixel-alpha + click-through + topmost. 0 (default) =
            # Tk renderer drives display as it always has. Flip via /tune
            # gpu_renderer_on=1 (live); a track change is not required.
            "gpu_renderer_on":              0,  # 1 = GPU child renderer, 0 = legacy Tk
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
            # ── TICKET-117: pinned-session lock (Tab-A-muted / Tab-B-lyrics) ──
            # Empty pinned_session_id = Auto (highest-priority session wins,
            # historical behavior). When set, only that session feeds lyrics —
            # even if other sessions are PLAYING. See set_pinned_session().
            "pinned_grace_s":             30.0,  # how long to hold a pin after its session goes missing before reverting to Auto
            "pinned_menu_refresh_s":       2.0,  # min wait between Source-submenu refreshes when the visible session set changes
            "pinned_auto_migrate_same_app": 1,  # 1 = if pin disappears + exactly one other session shares the pin's source_app, migrate to it (YouTube autoplay)
            # ── TICKET-118: audible-session preference ──
            # When MULTIPLE SMTC sessions are PLAYING (e.g. Brave Tab A muted +
            # Brave Tab B audible BOTH report 'playing'), prefer the session
            # whose process is actually making sound (Core Audio peak meter).
            # Pinned session (TICKET-117) wins absolutely if set; this knob is
            # only consulted to break ties between equally-eligible PLAYING
            # sessions. Default ON — degrades cleanly to pre-118 sticky
            # behavior when pycaw / Core Audio is unavailable (non-Windows
            # dev box, missing dep) or when no audible process matches any
            # session's source_app.
            "prefer_audible_session":       1,    # 1 = use Core Audio peak as a tiebreaker, 0 = pre-118 sticky-only
            "prefer_audible_threshold":     0.005,  # peak below this is 'silent' (~-46 dBFS)
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
        # TICKET-111: deferred WHOLE-LYRICS swap (SWITCH/REGEN/wrong-song/user-wrong).
        # Dict: kind, source_site, artist, title, cover, queued_t, hint,
        # fetch_token, lines, meta, lyrics_path, force_ai_gen, max_s, set_gate.
        # `lines/meta/lyrics_path` are populated by the fetch/gen completion
        # handler; the _tick consumer commits atomically once the boundary hits.
        self._pending_swap = None
        self._pending_swap_t = 0.0
        self._swap_fetch_token = 0       # monotonic; older fetch completions are dropped
        self._swap_commit_seq = 0        # /diag counter
        # ── success/failure telemetry (surfaced via /diag.success_rate) ──
        # Session counters; the success:failure ratio is a live readout instead
        # of something reverse-engineered from the log.
        self._stats = {
            "id_match": 0, "id_mismatch": 0,        # heard vs loaded (per Shazam read)
            "by_ear": 0, "track_loads": 0,          # generate/OCR fallback rate
            "sync_in_window": 0, "sync_reads": 0,   # perceptual-window adherence
            "regen": 0, "switch": 0,                # decision-engine actions
            "fetch_timeout": 0,                     # swap fetches that blew the hard cap
        }
        self._fetch_durations = []                  # seconds per applied swap fetch (P50/P95)
        # ── sync diagnostics ring buffer (surfaced via /syncdiag) ──
        # Bounded list of real sync EVENTS (snap/skip/commit/drift/caption/
        # decision/force-sync) — never appended per-frame, so it's cheap. Lets a
        # "highlights fucked" report be diagnosed by one curl. See _sync_event().
        self._sync_events = []
        self._drift_sign_hist = []                  # recent sign(drift) reads (monotonic-drift detect)
        self._drift_monotonic_since = 0.0           # wall-time a one-directional drift streak began (0=none)
        self._idx_minus_one_since = 0.0  # wall-time of the first tick idx hit -1 (for instrumental-gap boundary)
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
        # ── TICKET-113: per-track lyric blacklist + /wrong escalation ──
        # _lyrics_blacklist: {(source, sha1_signature)} of lyric bodies the user
        # (or REGEN) has rejected for THIS playback of THIS song. Memory-only,
        # cleared on _on_track_change — see the design doc: a blacklist entry's
        # meaning evaporates on track change / app restart.
        # _wrong_streak: consecutive /wrong presses inside the 60s window.
        # When the streak hits wrong_streak_force_ai_gen_threshold (default 2)
        # the next re-fetch jumps straight to AI-gen — every network provider
        # has been wrong, so burning more time on them wastes the listen.
        # _provider_order: rotation state. report_wrong rotates index 0 to the
        # end; reset to default on _on_track_change. 'ai-gen' is a SENTINEL — when
        # it rotates to position 0, _start_fetch skips fetch_and_save and sets
        # _force_ai_gen instead (generation lives in main.py, not fetch_lyrics).
        self._lyrics_blacklist: set        = set()
        self._wrong_streak: int            = 0
        self._wrong_streak_t: float        = 0.0
        self._provider_order: list         = ["lrclib", "syncedlyrics", "netease", "ai-gen"]
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

        # TICKET-124: NO hardbaked lyrics shipped — this is a sellable product, so every
        # lyric must be FOUND BY CODE (providers / YouTube captions / OCR of burned-in
        # lyrics / by-ear generation), never copyrighted text baked into the app. The old
        # _seed_bundled_lyrics() bake-in is removed; bundled_lyrics/ no longer ships.
        self.index = LyricsIndex()
        self.media = MediaWatcher()
        # TICKET-117: push the persisted pin into the watcher so the very first
        # _pick() respects it (no Auto blip on startup). The icon.update_menu
        # callback is registered later, AFTER pystray.Icon is built (in main()).
        if self.pinned_session_id:
            self.media.set_pinned(self.pinned_session_id, self.pinned_session_app)
        # TICKET-118: mirror the audible-pref tune knob into the watcher so
        # the very first _pick already respects the setting (instead of going
        # through one cycle of pre-118 sticky behavior). Safe even when pycaw
        # is unavailable — the watcher's score helper returns {} and _pick
        # falls through.
        try:
            self.media.set_audible_pref(
                int(self._tune.get("prefer_audible_session", 1) or 0),
                float(self._tune.get("prefer_audible_threshold", 0.005)),
            )
        except Exception:
            pass
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
        # Reject a bare social/site page title ('Instagram', '(6) Instagram',
        # 'TikTok' …) — that's the browser tab, not a song. Clear the overlay so
        # lyrics vanish when the user is just browsing a feed, and bail before
        # any fetch / title-match (a poisoned instagram.json used to load junk
        # rap lyrics over Instagram reels — the "stop it" bug).
        if _is_junk_track_title(title, artist):
            log.info("ignoring non-music page %r — not a song; clearing overlay", title)
            self._track = (artist, title)
            if self.lines or self._lyrics_path is not None:
                self.lines, self.meta, self._lyrics_path = [], {"source": ""}, None
                self.idx = -1
                self._cancel_pending_swap("non-music-page") if hasattr(self, "_cancel_pending_swap") else None
                try:
                    self.cv.delete("all")
                except Exception:
                    pass
                try:
                    self._gpu_send_song()      # tell the GL child to go blank too
                except Exception:
                    pass
            return
        # TICKET-114: re-anchor the instrumental-gap timer on EVERY SMTC track
        # event (including the same-song re-report below). Without this, the
        # boot-time stamp from __init__ (line 2042) is never cleared when the
        # initially-loaded lyrics never let idx reach >=0, so on subsequent
        # tracks the timer keeps accumulating as wall-time-since-boot — exactly
        # the "instrumental-gap(204.2s)" on an 11.7s-position 161s song seen in
        # the live diag. Placed ABOVE the same-song early-return so SMTC
        # re-reports of the current track also benefit (title-stability is not
        # the same as gap-state stability). The per-tick logic at ~line 4572
        # re-stamps on the very next tick where idx is still -1, so this
        # zero-then-restamp is safe for both branches.
        self._idx_minus_one_since = 0.0
        self._last_line_idx = -1        # new song → forget the prior song's held line
        self._gap_start_t = None        # …and its gap timer (keep-last-line state)
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
        self._body_corroborated = False    # new song → BODY not yet corroborated; re-earn per song
        self._body_probe_retried = False
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
        # TICKET-111: a real new track invalidates any in-flight swap target
        # (those lyrics are for the OLD song). Cancel; the new track's flow
        # below will run through the normal load/_start_fetch path.
        try:
            self._cancel_pending_swap("track-change")
        except Exception:
            pass
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
        # TICKET-121: finalize the PREVIOUS play (still the current _track_seq/_t0 here)
        # before the seq bumps. The same-song early-return above means a re-report of
        # the same song never reaches here, so it won't be double-counted.
        self._m(self.metrics.finalize)
        self._gen_token += 1       # cancel any in-flight lyric generation
        self._deep_token += 1      # cancel any in-flight deep (offline) transcription
        self._track_seq += 1
        self._stats_bump("track_loads")
        self._generating = False
        self._gen_defers = 0       # fresh defer budget for this track's fetch
        self._fetch_key = None     # let this track re-attempt a real fetch (upgrade path)
        # TICKET-113: per-track blacklist + /wrong escalation + provider rotation
        # all reset on a real song change — a blacklist entry from the previous
        # song must not silently suppress the new song's valid hits, and the
        # streak window must not bleed forward.
        self._lyrics_blacklist.clear()
        self._wrong_streak = 0
        self._wrong_streak_t = 0.0
        # TICKET-112: a real new track invalidates the previous track's YT
        # description (different video → different credits). Both fields
        # cleared together so set_now_url's "if vid != _yt_metadata_video_id"
        # triggers a fresh fetch even when the new URL hadn't pushed yet.
        self._yt_metadata = None
        self._yt_metadata_video_id = None
        self._yt_metadata_fetching = False
        self._provider_order = ["lrclib", "syncedlyrics", "netease", "ai-gen"]
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
        self._sync_event("mode_change", title=title, cover=getattr(self, "_is_cover", False),
                         live_arrangement=self._live_arrangement,
                         live_mode=getattr(self, "_live_mode", False))
        self._drift_sign_hist = []          # new song → fresh monotonic-drift detection
        self._drift_monotonic_since = 0.0
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
            # Curated original-artist hint (e.g. 'BANG!!!' → EGOIST) when the title
            # didn't name the original. Routes the fetch to the ORIGINAL's lyrics
            # instead of a same-title wrong-language hit — 'BANG!!!' is EGOIST's JP
            # song, never the K-pop 'BANG'. Checked before the title-only fallback.
            if not self._cover_original_artist:
                _hint = _known_cover_original(title)
                if _hint:
                    self._cover_original_artist = _hint
                    log.info("cover: curated original-artist hint %r for %r", _hint, title)
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

        # Live/concert DETECTION uses the RAW video length — a reliable concert
        # signal even on browser sources, where duration is NOT trusted for SYNC
        # (so the passed `duration` is usually None for a YouTube tab). A >10-min
        # video is a concert/compilation of many songs and must be driven by
        # SOUND, not its event title (理芽 - Singularity Live = 18.96 min, but the
        # title 'Singularity Live' isn't enough for _LIVE_RE alone). Per the user:
        # "over 10 minutes is generally a concert video with multiple songs."
        _live_dur = duration
        if not _live_dur:
            try:
                _live_dur = float((self.media.get() or {}).get("duration") or 0.0) or None
            except Exception:
                _live_dur = None
        self._live_mode = is_live_or_compilation(title, _live_dur)
        if self._live_mode and not (duration and duration > 600):
            log.info("live/concert mode via VIDEO LENGTH %.0fs (%.1f min) — %r",
                     _live_dur or 0.0, (_live_dur or 0.0) / 60.0, title)
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
            self._body_corroborated = False       # kamone fix: BODY re-earns corroboration per song
            self._body_probe_retried = False
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
        # TICKET-121: open a new telemetry record for this play (concert flag = live_mode,
        # set at ~2819 above). finalize() of the previous play already happened.
        self._m(self.metrics.start_play, version.__version__, title, artist,
                self._live_mode, self._track_t0, self._track_seq)
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
            # COVER / LIVE-ARRANGEMENT: a re-sung cover (or a live take) NEVER
            # matches the original studio LRC's timing, so fetch the video's OWN
            # caption track EAGERLY (it IS this performance's lyrics + timing)
            # before the mismatched LRC dominates the sync. Studio originals keep
            # the 4s delay (their LRC is fine; CC is just a nicety there).
            _cap_delay = (250 if (getattr(self, "_is_cover", False)
                                  or getattr(self, "_live_arrangement", False)) else 4000)
            self.root.after(_cap_delay,
                            lambda t=self._track_seq: self._maybe_fetch_captions(t))
        # TICKET-112: YT-description metadata extractor. Runs in PARALLEL to
        # captions (cheap metadata-only yt_dlp call, no audio/no subs) so a
        # browser source gets disambiguators ('作詞・作曲：kors k',
        # '歌唱：ReGLOSS(...)') layered onto the next provider query. Gated on
        # self._now_url being known — captions/description both need an exact
        # URL, and set_now_url also re-kicks this when the URL settles late
        # for a new track.
        if (not self._live_mode
                and any(h in (self._last_src or "") for h in BROWSER_HINTS)
                and int(self._tune.get("yt_description_lookup", 1) or 0)):
            self.root.after(300,
                            lambda t=self._track_seq: self._maybe_fetch_yt_description(t))

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

    # ── TICKET-112: YouTube description metadata extractor ─────────────
    def _maybe_fetch_yt_description(self, track_seq):
        """Background: pull this YouTube video's DESCRIPTION (metadata only,
        no audio + no captions) and parse out structured credit fields
        (作詞/作曲/歌唱 / Music/Lyrics/Vocals / 작사/작곡/노래) into
        ``self._yt_metadata``. Downstream readers:
          * ``_start_fetch`` — pass the extracted vocals as additional
            artist candidates to ``fetch_lrc`` (disambiguator only; the
            existing scoring + language guards stay in charge).
          * ``report_wrong`` — when /wrong fires AND we have description
            credits, prefer the new vocalist on the re-fetch.
          * ``get_diag`` — surfaces the parsed dict for /diag and /yt-meta.

        Single-flight per track via self._yt_metadata_fetching. Re-kicked
        from set_now_url when the browser pushes the URL late for the new
        track (without that, the gate `self._now_url is set` would miss
        the first invocation here)."""
        if track_seq != self._track_seq or self._live_mode:
            return
        if not int(self._tune.get("yt_description_lookup", 1) or 0):
            return
        url = self._now_url
        if not url:
            # URL hasn't been pushed by the browser yet — set_now_url will
            # re-kick this when it arrives. Don't busy-poll here.
            return
        if self._yt_metadata_fetching:
            return
        # Dedupe: if we already have parsed metadata for this exact video,
        # nothing to do (the LRU also catches this, but skipping here
        # avoids the lock + thread creation).
        try:
            from yt_description import _video_id, extract_video_metadata
        except Exception:
            return
        vid = _video_id(url)
        if vid and vid == self._yt_metadata_video_id and self._yt_metadata:
            return
        try:
            timeout = float(self._tune.get("yt_description_timeout_s", 8.0))
        except Exception:
            timeout = 8.0
        self._yt_metadata_fetching = True
        captured_seq = track_seq
        captured_url = url

        def work():
            meta = None
            try:
                meta = extract_video_metadata(captured_url, timeout=timeout)
            except Exception:
                meta = None
            try:
                # Stale-token guard: drop the result if the track changed
                # while we were fetching. Otherwise we'd stamp the prior
                # track's video credits onto the new song.
                if captured_seq != self._track_seq:
                    return
                if meta is None:
                    log.info("yt_description: no metadata for %s", captured_url[:80])
                    return
                self._yt_metadata = meta
                self._yt_metadata_video_id = meta.get("video_id")
                # Tray hint: prefer real credits; fall back gracefully when
                # only one side parsed. Mirrors the captions/identify hint
                # style and is muted (no symbol) when nothing parsed.
                vocals = meta.get("vocals") or []
                comp = meta.get("composer") or meta.get("lyricist") or []
                orig = meta.get("original_artist") or []
                primary_vocal = (vocals[0] if vocals
                                 else (orig[0] if orig
                                       else (meta.get("channel") or "")))
                primary_credit = comp[0] if comp else ""
                if primary_credit and primary_vocal:
                    self._hint(f"🎬 YT credits: {primary_credit} / {primary_vocal}")
                elif primary_vocal:
                    self._hint(f"🎬 YT credits: {primary_vocal}")
                elif primary_credit:
                    self._hint(f"🎬 YT credits: {primary_credit}")
            finally:
                self._yt_metadata_fetching = False

        threading.Thread(target=work, daemon=True).start()

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
            log.info("no lyrics after the grace window (%s) → OCR / generating by ear", why)
            self._stats_bump("by_ear")
            # TICKET-120: BEFORE generating by ear, try to READ the lyrics burned into
            # the video (niche Vocaloid / fan karaoke have no fetchable LRC but the words
            # are on screen). Browser source only, once per track. OCR success pre-empts
            # generation; OCR failure (no burned-in text) falls back to generation from
            # _apply_ocr's giveup branch.
            if (self.generate_on
                    and any(h in (self._last_src or "") for h in BROWSER_HINTS)
                    and self._track_seq != self._ocr_harvest_seq):
                self._ocr_harvest_seq = self._track_seq
                if self._begin_ocr_harvest(track_seq):
                    return
            # Shazam-heard-artist re-fetch (before generating). A fan LYRIC-VIDEO
            # uploads under the CHANNEL name (YohaNico / Yuan), so the fetch ran
            # 'Aishiteru Banzai!' by 'Yoha Nico' → empty → about to generate. But
            # Shazam already heard the REAL artist (µ's). Re-fetch with the heard
            # artist first — it's fingerprinted from the actual audio, so it's the
            # most reliable signal of who really performs the song. Once per track.
            heard = self._sound_song
            heard_artist = (heard[1] if heard else "") or ""
            if (heard_artist.strip()
                    and self._track_seq != getattr(self, "_sound_refetch_seq", None)
                    and heard_artist.strip().lower()
                        != (self._last_artist or "").strip().lower()):
                self._sound_refetch_seq = self._track_seq
                log.info("pre-generation: Shazam heard artist %r (channel was %r) → "
                         "re-fetching with the heard artist before generating",
                         heard_artist, self._last_artist)
                self._start_fetch(heard_artist, (heard[0] or "").strip() or None,
                                  self._cur_duration)
                # let the re-fetch run; the _fetching defer above re-fires this and
                # generates only if the heard-artist fetch ALSO comes up empty.
                self.root.after(4000, lambda t=track_seq: self._maybe_generate(t))
                return
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
        if (not prev) and new_v:
            # TICKET-121: first transition to verified = "found + synced". note_synced
            # records only the FIRST sync; note_source pins the real provider string so
            # the success-classifier can tell a real source from a generated one.
            self._m(self.metrics.note_synced)
            self._m(self.metrics.note_source, (self.meta.get("source") or ""))
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
        self._body_corroborated = False    # kamone fix: BODY re-earns corroboration per song
        self._body_probe_retried = False
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
        # TICKET-115: non-English Latin/Cyrillic songs (Spanish, German,
        # French, Italian, Portuguese, Russian, and romanized-Japanese) should
        # have every line translated; CJK songs only their CJK lines. Keep this
        # set in sync with fetch_lyrics._translate_lines' _LANGS.
        whole = self.meta.get("lang") in (
            "es", "de", "fr", "it", "pt", "ru", "ja-romaji"
        )
        want_en = (self.lines if whole else cjk)
        want_en = [ln for ln in want_en if ln.jp.strip()]
        have_en = sum(1 for ln in want_en if ln.en.strip())
        if need_rm or (want_en and have_en < len(want_en) * 0.5):
            self._start_translate(self._lyrics_path)

    def _start_fetch(self, artist, title, duration=None, cover=False, strict=False,
                     swap_token=None):
        key = (artist, title)
        # TICKET-113: snapshot the blacklist at queue time so the worker thread
        # never reads the live set (a second /wrong mid-fetch would otherwise
        # mutate the set under iteration in fetch_lrc's take() chokepoint).
        # frozenset(...) is the safe handoff — immutable, hashable, cheap.
        reject_sigs = frozenset(sig for (_src, sig) in self._lyrics_blacklist)
        # TICKET-112: snapshot the YT-description metadata at queue time so
        # the worker thread sees a stable view (description fetch can still
        # be in flight; if it lands AFTER us the NEXT _start_fetch — e.g.
        # report_wrong's re-fetch — picks it up). Build an ordered list of
        # extra artist candidates from vocals -> original_artist -> composer
        # -> lyricist so the most "artist-shaped" credits get tried first.
        ytm = self._yt_metadata or {}
        extra_artists = []
        # title '(feat. X)' artists FIRST — for a game / official-channel upload the
        # featured artist is the real one (NTE 公式 → Reol for 'Play On! (feat. Reol)').
        for name in getattr(self, "_title_feat_artists", []) or []:
            if name and name not in extra_artists:
                extra_artists.append(name)
        for k in ("vocals", "original_artist", "composer", "lyricist"):
            for name in (ytm.get(k) or []):
                if name and name not in extra_artists:
                    extra_artists.append(name)
        yt_composer = list(ytm.get("composer") or [])
        yt_original = list(ytm.get("original_artist") or [])
        yt_lyrics_block = ytm.get("lyrics_block")
        # TICKET-113: when reject_signatures is non-empty, bypass the
        # _fetch_key == key short-circuit — without this, /wrong's re-fetch
        # for the SAME (artist, title) silently no-ops and the blacklist filter
        # never runs. report_wrong / _fire_decision_action already clear
        # _fetch_key, but a future caller might forget.
        if self._fetch_key == key and not reject_sigs:
            return
        # TICKET-113: 'ai-gen' sentinel — when it has rotated (or escalated
        # via wrong-streak) to position 0, do not call fetch_and_save at all.
        # Generation needs the audio stream + Whisper; that lives in main.py
        # (_begin_generation), not fetch_lyrics. Set the flag the gen consumer
        # already checks and return — the existing tick / decide loop picks
        # it up and routes through _begin_generation.
        if (self._provider_order and self._provider_order[0] == "ai-gen"
                and not self._force_ai_gen):
            log.info("provider rotation: head is 'ai-gen' → forcing AI-gen, skipping fetch")
            self._force_ai_gen = True
        self._fetch_key = key
        self._fetching = True       # in flight → generation defers until this resolves
        # TICKET-111: capture swap_token at queue time so the completion can
        # route into self._pending_swap['lines'] instead of self.lines when the
        # token still matches the in-flight pending swap.
        captured_swap_token = swap_token

        def work():
            try:
                from fetch_lyrics import fetch_and_save
                # TICKET-112: feed YT-description disambiguators into the
                # provider chain as kwargs. fetch_and_save accepts unknown
                # kwargs gracefully (it forwards/ignores via **kw); when
                # they're empty the call is byte-identical to v1.0.93.
                p = fetch_and_save(title, artist, translate=False, duration=duration,
                                   cover=cover, strict=strict,
                                   reject_signatures=reject_sigs or None,
                                   extra_artists=extra_artists or None,
                                   yt_composer=yt_composer or None,
                                   yt_original_artist=yt_original or None,
                                   yt_lyrics_block=yt_lyrics_block)
            except TypeError:
                # Older fetch_and_save signature (no description kwargs) —
                # fall back to the legacy call so this stays robust if the
                # two files drift between deployments.
                try:
                    from fetch_lyrics import fetch_and_save as _fas
                    p = _fas(title, artist, translate=False, duration=duration,
                             cover=cover, strict=strict,
                             reject_signatures=reject_sigs or None)
                except Exception:
                    p = None
            except Exception:
                p = None
            # 3-tuple (key, path, swap_token); _consume_async tolerates legacy 2-tuples.
            self._fetch_result = (key, p, captured_swap_token)
            self._fetching = False

        threading.Thread(target=work, daemon=True).start()

    def _start_translate(self, path):
        if self._translating == path:
            return
        self._translating = path

        def work():
            ok = False
            try:
                try:
                    from fetch_lyrics import backfill_file   # romaji + translation
                    ok = backfill_file(path)
                except Exception:
                    ok = False
                self._translate_result = (path, ok)
            finally:
                # TICKET-115: ALWAYS release the in-flight guard, even on
                # exception — otherwise a poisoned _translating flag silently
                # blocks every future _maybe_translate call for this same path
                # (e.g. the user hits /retranslate to retry a failed run).
                self._translating = None

        threading.Thread(target=work, daemon=True).start()

    def retranslate_loaded(self) -> dict:
        """TICKET-115: force a translation backfill of the currently loaded
        track. Used by POST /retranslate (and any future "translate now" UI).
        Routes through the existing backfill_file pipeline so the file is
        rewritten atomically and the main tick re-loads it in place (keeping
        playback position). Returns a small status dict for the API.
        """
        path = self._lyrics_path
        if not path or not self.lines:
            return {"ok": False, "reason": "no loaded track"}
        want_en = [ln for ln in self.lines if ln.jp.strip()]
        missing_before = sum(1 for ln in want_en if not ln.en.strip())
        # Clear any stuck in-flight guard so a prior poisoned run can't block us.
        self._translating = None
        self._start_translate(path)
        return {
            "ok": True,
            "action": "backfilling translations",
            "path": str(path),
            "lang": self.meta.get("lang"),
            "n_lines": len(want_en),
            "n_missing": missing_before,
        }

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
            # TICKET-135: run identify in a SEPARATE PROCESS. The Shazam
            # capture+fingerprint is GIL-heavy (~4s) and, on a worker THREAD,
            # still stalled the render (150-475 ms frames) — the "highlight
            # sticks then jumps to the 2nd/3rd line", worst on unfingerprintable
            # songs that recal hammers. A child PROCESS can't touch our GIL, so
            # the render keeps running smoothly; this thread just waits + reads
            # one JSON line. t_cap is wall-clock (same system clock), so the
            # offset alignment is unaffected.
            res = None
            try:
                import subprocess as _sp, json as _json, sys as _sys, tempfile as _tf, os as _os
                outp = _os.path.join(_tf.gettempdir(),
                                     "li_recog_%d_%d.json" % (_os.getpid(), int(time.time() * 1000) % 100000))
                if getattr(_sys, "frozen", False):
                    cmd = [_sys.executable, "--recognize-child", str(seconds), str(attempts), "--out", outp]
                else:
                    cmd = [_sys.executable, str(Path(__file__).parent / "recognize.py"),
                           "--child", str(seconds), str(attempts), "--out", outp]
                try:
                    _sp.run(cmd, capture_output=True,
                            timeout=float(seconds) * max(1, attempts) + 30,
                            creationflags=0x08000000)   # CREATE_NO_WINDOW
                    with open(outp, "r", encoding="utf-8") as _f:
                        d = _json.load(_f)
                    if d.get("t"):
                        res = (d["t"], d.get("a") or "", d.get("off"), d.get("tc"))
                finally:
                    try:
                        _os.remove(outp)
                    except Exception:
                        pass
            except Exception:
                # Fall back to in-process recognize if the child can't spawn
                # (keeps identification working even if the subprocess path fails).
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
            # TICKET-117: pin liveness — if the pinned SMTC session has been
            # gone longer than pinned_grace_s, auto-migrate (same-app sole
            # survivor) or revert to Auto and notify. Cheaper than wiring
            # into the per-frame _tick (9s cadence is well under the 30s
            # default grace).
            try:
                self._pinned_tick()
            except Exception:
                pass
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
                    elif abs(self.offset) > 0.8 and confirmed:
                        # Only fast-re-sync a CONFIRMED song's drift. An UNCONFIRMED
                        # song's offset won't be fixed by recognize (it can't even ID
                        # it), so hammering it just freezes the render for nothing.
                        nxt = min(nxt, 12)
                    # ANTI-STUTTER BACK-OFF (de-escalation): a song Shazam simply
                    # CAN'T fingerprint (an MMD/cover/niche feat — e.g. NTE "Play On!"
                    # feat Reol) stays "unconfirmed" forever, so the unconfirmed branch
                    # polls recognize every ~4 s — and each recognize STALLS the render
                    # (GIL: ~150-475 ms frames = the highlight freezing every few sec).
                    # Once it has lyrics and has played a bit, STOP hammering regardless
                    # of the offset (recognize can't fix an unfingerprintable song's
                    # sync; the energy tier handles drift, the boundary detector handles
                    # song changes). live_mode (concert) is exempt — it polls to catch
                    # the next song.
                    if (self.lines and not self._live_mode
                            and time.time() - getattr(self, "_track_t0", 0.0)
                                > self._tune.get("unconfirmed_backoff_after_s", 25.0)
                            and ((not self._verified) or self._sound_song is None)):
                        nxt = max(nxt, self._tune.get("unconfirmed_backoff_s", 28.0))
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
            # TICKET-111: tolerate both legacy 2-tuple and new 3-tuple shape
            fr = self._fetch_result
            if len(fr) == 3:
                key, p, completion_swap_token = fr
            else:
                key, p = fr
                completion_swap_token = None
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
                        # TICKET-111: if a swap is pending and the token matches,
                        # route the loaded lines into the pending dict so the
                        # _tick consumer can commit atomically at the next
                        # boundary. Otherwise it's a normal fetch load now.
                        if (self._pending_swap is not None
                                and completion_swap_token is not None
                                and completion_swap_token
                                    == self._pending_swap.get("fetch_token")):
                            try:
                                meta_l, lines_l = load_lyrics(Path(p))
                                self._pending_swap["lines"] = lines_l
                                self._pending_swap["meta"]  = meta_l
                                self._pending_swap["lyrics_path"] = Path(p)
                                log.info("swap: target ready token=%d age=%.2fs "
                                         "src=fetch lines=%d",
                                         completion_swap_token,
                                         time.time() - self._pending_swap_t,
                                         len(lines_l))
                                # still kick off translation backfill against the cache file
                                self._start_translate(Path(p))
                                if self.git_sync:
                                    self.git_backup()
                            except Exception as e:
                                log.info("swap: pending-load failed (%s) "
                                         "falling back to direct load", e)
                                self.load(Path(p))
                                self._start_translate(Path(p))
                                if self.git_sync:
                                    self.git_backup()
                        else:
                            if completion_swap_token is not None:
                                log.info("swap: stale fetch completion token=%d "
                                         "current=%s dropping",
                                         completion_swap_token,
                                         (self._pending_swap or {}).get("fetch_token"))
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
                self._stats_bump("id_match" if loaded_ok else "id_mismatch")
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
                        self._title_locked_at = time.time()
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
                            self._sync_event("drift_read", drift=round(diff, 2), corr=round(corr, 2),
                                             offset=round(self.offset, 2), spread=round(spread, 1),
                                             cover=getattr(self, "_is_cover", False),
                                             live=getattr(self, "_live_arrangement", False))
                            # MONOTONIC-DRIFT detect (STUDIO only): N consecutive same-sign
                            # reads outside the deadband = the lyrics are steadily creeping
                            # ONE way (共鳴 "starts in sync then gets late"). Flag it so
                            # _maybe_auto_align re-locks on a SHORTER cooldown. Covers/live
                            # FOLLOW the offset by design and must not trip this.
                            _studio = not (getattr(self, "_is_cover", False)
                                           or getattr(self, "_live_arrangement", False)
                                           or getattr(self, "_force_sync_active", False))
                            _db = float(self._tune.get("deadband", 0.8))
                            _sgn = (1 if diff > _db else (-1 if diff < -_db else 0))
                            self._drift_sign_hist.append(_sgn)
                            if len(self._drift_sign_hist) > 6:
                                del self._drift_sign_hist[:len(self._drift_sign_hist) - 6]
                            _need = int(self._tune.get("drift_monotonic_reads_n", 3))
                            _recent = self._drift_sign_hist[-_need:]
                            if (_studio and _sgn != 0 and len(_recent) >= _need
                                    and all(s == _sgn for s in _recent)):
                                if not self._drift_monotonic_since:
                                    self._drift_monotonic_since = time.time()
                                    log.info("drift-recovery: monotonic %s drift (%d reads) → faster re-lock",
                                             "LATE" if _sgn > 0 else "EARLY", _need)
                                    self._sync_event("drift_monotonic",
                                                     dir=("late" if _sgn > 0 else "early"), n=_need)
                            elif _sgn == 0:
                                self._drift_monotonic_since = 0.0   # back inside the deadband
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
                            # success telemetry: was this read inside the perceptual window?
                            self._stats_bump("sync_reads")
                            if (-float(self._tune.get("sync_win_ahead_s", 0.17))
                                    <= diff <= float(self._tune.get("sync_win_behind_s", 0.09))):
                                self._stats_bump("sync_in_window")
                            if abs(corr) >= cap:
                                # matched a different recording/segment — no usable info; ignore.
                                self._pending_corr = 1e9
                            elif (-float(self._tune.get("sync_win_ahead_s", 0.17))
                                  <= diff <=
                                  float(self._tune.get("sync_win_behind_s", 0.09))):
                                # PERCEPTUALLY in sync (asymmetric window): the highlight
                                # may run a little AHEAD of the vocal (diff<0, forgiving)
                                # but is corrected quickly once it falls BEHIND (diff>0,
                                # "lyrics late" — the annoying direction). Replaces the
                                # old symmetric ±deadband(0.8s) that tolerated drift no
                                # listener would call "in sync".
                                self._pending_corr = 1e9      # already perceptibly in sync — leave it
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
                                    LIVE_MAX_JUMP = float(self._tune.get("live_max_jump_s", 45.0))
                                    if (abs(self.offset) > DEADBAND
                                            and abs(new - self.offset) > LIVE_MAX_JUMP):
                                        # Implausible jump from a STABLE offset = a chorus-repeat
                                        # mismatch (the -170s-on-a-4-min-song case), not real live
                                        # tempo drift. Reject it; an unlocked offset (~0) still
                                        # allows the large initial intro lock-in.
                                        log.info("sync(live): rejecting implausible jump %+.2fs→%+.2fs "
                                                 "(>%.0fs from stable offset)", self.offset, new, LIVE_MAX_JUMP)
                                        self._pending_corr = 1e9
                                    else:
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
                            elif (getattr(self, "_verified", False)
                                  and getattr(self, "_title_locked", False)
                                  and abs(corr) <= float(self._tune.get("fast_lock_max_s", 6.0))):
                                # FAST-LOCK (TICKET-146): on a VERIFIED + title-locked song
                                # (already known to be the right song) a MODEST first-read
                                # offset is real drift, not a chorus-repeat mismatch — commit
                                # it NOW instead of waiting ~8s for the two-point confirm
                                # (the studio "found sync at ~1 min" complaint). A LARGE first
                                # offset (chorus match or big MV intro) still falls through to
                                # the two-point verification below.
                                self._pending_corr = 1e9
                                log.info("sync: FAST-LOCK %+.2fs (verified+locked, modest) → applied", corr)
                                self._smooth_offset(round(corr, 2), "sync-fast-lock")
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
                    # A title-locked BODY that has NOT been corroborated should fail
                    # FAST when Shazam repeatedly disagrees (the Play On!/kamone
                    # poisoned-cache case) — but only AFTER the initial energy-align
                    # window (~50s) has had its chance to corroborate a correct-but-
                    # uncorroborated body, so 2 transient mis-reads can't tear down a
                    # correctly-synced fetched song in the lock→align gap.
                    if (not getattr(self, "_body_corroborated", False)
                            and getattr(self, "_title_locked_at", 0.0)
                            and time.time() - self._title_locked_at
                                > float(self._tune.get("uncorroborated_fast_after_s", 50.0))):
                        base_strikes = self._tune.get("wrong_song_uncorroborated_strikes", 3)
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
                        # TICKET-111: defer the actual swap to the next line/belt
                        # boundary so the user doesn't see a 1-5s blackout while
                        # the new lyrics arrive. The fetch (or cache load) starts
                        # NOW; old lines keep rendering until the boundary fires.
                        deferred = (int(self._tune.get("swap_defer_enabled", 1) or 0) == 1
                                    and bool(self.lines))
                        if deferred:
                            self._queue_swap(
                                kind="wrong-strike", source_site="D",
                                artist=f_artist, title=f_title, cover=False,
                                hint=f"🔄 Wrong song — switching to {f_title}…",
                                set_gate=True)
                            if cached and self._file_valid(cached, self._cur_duration):
                                if cached != self._lyrics_path:
                                    log.info("wrong-song correction → cached %s (deferred swap)", cached.name)
                                    try:
                                        meta_l, lines_l = load_lyrics(cached)
                                        self._pending_swap["lines"] = lines_l
                                        self._pending_swap["meta"]  = meta_l
                                        self._pending_swap["lyrics_path"] = Path(cached)
                                        log.info("swap: target ready token=%d age=0.00s src=cache lines=%d",
                                                 self._pending_swap["fetch_token"], len(lines_l))
                                    except Exception as e:
                                        log.info("swap: cache-preload failed (%s) — falling back to immediate", e)
                                        self.load(cached)
                                        self._cancel_pending_swap("cache-preload-failed")
                                self._maybe_translate()
                            else:
                                log.info("wrong-song correction → fetching %r / %r (deferred swap)", f_title, f_artist)
                                self._start_fetch(f_artist, f_title, self._cur_duration,
                                                  swap_token=self._pending_swap["fetch_token"])
                        else:
                            # legacy immediate-teardown path (kill-switch or no current lines)
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
        self._m(self.metrics.note_generated)                     # TICKET-121: ended generated = fail
        self._gen_token += 1
        self._gen_title, self._gen_artist = (title or "song"), (artist or "")
        self._gen_lines = []
        # cross-language cover hint: seed the sung language from the title (e.g. an
        # English cover → 'en') so the first chunk's annotation isn't the 'ja' default;
        # the per-chunk auto-detect (lang=None in _generate_loop) still refines it.
        self._gen_lang = getattr(self, "_cover_lang", None)
        # TICKET-111: if a REGEN swap is pending (force_ai_gen=True), do NOT
        # blow away self.lines yet the deferred-swap consumer will commit the
        # generated output atomically at the next boundary. Skip the verified-
        # flag wipe too, for the same reason; _apply_pending_swap handles it.
        regen_pending = (self._pending_swap is not None
                         and self._pending_swap.get("force_ai_gen"))
        if regen_pending:
            # Tag the pending dict so _apply_generated routes into it.
            self._pending_swap["gen_token"] = self._gen_token
        else:
            self.lines, self.idx, self._kara = [], -1, []
            self._lyrics_path = None
            self._verified = False
            self._verified_meta = False           # TICKET-099
            self._sound_corroborated = False      # TICKET-099
            self._body_corroborated = False       # kamone fix: BODY re-earns corroboration per song
            self._body_probe_retried = False
            self._verified_gate_t = 0.0           # TICKET batch4: explicit teardown, no gate
        self.meta = {"title": self._gen_title, "artist": self._gen_artist,
                     "lang": (getattr(self, "_cover_lang", None) or "ja"),
                     "duration": self._cur_duration, "source": "generated"}
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
        new_lines = [Line(start=d["t"][0], end=d["t"][1], jp=d.get("jp", ""),
                          rm=d.get("rm", ""), en=d.get("en", "")) for d in merged]
        # TICKET-111: if a REGEN swap queued this generation, write the result
        # into the pending dict instead of self.lines and let the boundary
        # consumer commit atomically. Otherwise behave as before.
        if (self._pending_swap is not None
                and self._pending_swap.get("force_ai_gen")
                and self._pending_swap.get("gen_token") == token):
            self._pending_swap["lines"] = new_lines
            self._pending_swap["meta"]  = dict(self.meta) if self.meta else {}
            # _lyrics_path is set further down in the save block; capture it then.
            log.info("swap: target ready token=%d gen_token=%d age=%.2fs src=ai-gen lines=%d",
                     self._pending_swap["fetch_token"], token,
                     time.time() - self._pending_swap_t, len(new_lines))
            self._save_generated_only(merged, into_pending=True)
            return
        self.lines = new_lines
        self._relayout_song()
        self._save_generated_only(merged, into_pending=False)

    def _save_generated_only(self, merged, into_pending=False):
        """TICKET-111: save the generated lyrics to disk + index.  When
        into_pending=True, write the resulting path into the pending swap
        dict's lyrics_path field instead of self._lyrics_path."""
        try:
            from fetch_lyrics import slugify
            out = LYRICS_DIR / f"{slugify(self._gen_title)}.json"
            data = {"meta": {"title": self._gen_title, "artist": self._gen_artist,
                             "lang": self._gen_lang or "ja", "duration": self._cur_duration,
                             "source": "generated"},
                    "lines": [{"t": d["t"], "jp": d.get("jp", ""), "rm": d.get("rm", ""),
                               "en": d.get("en", "")} for d in merged]}
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
            if into_pending and self._pending_swap is not None:
                self._pending_swap["lyrics_path"] = out
            else:
                self._lyrics_path = out
            self.index.add(out)
        except Exception as e:
            log.info("saving generated lyrics failed: %s", e)

    # ── OCR burned-in lyrics (TICKET-120) ────────────────────────────────────────
    # Some videos render the lyrics INTO the frame and publish no fetchable LRC /
    # caption (niche Vocaloid, fan karaoke). Read them off the source window with
    # PrintWindow (self-read-safe — never captures our own overlay) + Windows OCR, time
    # each new line against the player clock, and use them as source="ocr" BEFORE AI
    # generation. Mirrors the generation pattern: bg thread does the OCR, the Tk thread
    # runs the harvester + commits (single-writer), guarded by self._track_seq.
    def _ocr_gpu_safe(self) -> bool:
        """TICKET-125/127: decide whether OCR may run now. User policy: with TWO+ GPUs,
        OCR is ALLOWED during gaming (it runs alongside the game — the app is IDLE-
        priority while gaming so it yields, and the idle 2nd card has headroom). Only on
        a SINGLE-GPU machine do we back off while that lone card is busy with a game, so
        we never fight the game for the only GPU. `ocr_when_gaming=1` forces allow."""
        try:
            import gpu_setup
            if gpu_setup.cuda_device_count() >= 2:
                return True                       # idle 2nd GPU → run OCR even while gaming
            if int(self._tune.get("ocr_when_gaming", 0) or 0):
                return True
            if gpu_setup.game_active():
                return False                      # single GPU + game → don't fight for it
            utils = gpu_setup._gpu_utils()
            if utils and max(utils.values()) >= 45:
                return False
        except Exception:
            pass
        return True

    def _source_window_hwnd(self):
        """HWND of the playing browser/player window for PrintWindow. Prefer the handle
        the window-title watcher already published (CEF hosts); else enumerate browser
        windows once (a normal Brave/Chrome tab doesn't expose its HWND via SMTC)."""
        try:
            track = getattr(self, "_window_titles_last_track", None)
            if not track:
                import window_titles as _wt
                track = _wt.get_current_track()
            hwnd = (track or {}).get("window_handle") if track else None
            if hwnd:
                return int(hwnd)
        except Exception:
            pass
        try:
            import ocr_lyrics
            return ocr_lyrics.find_source_window(getattr(self, "_last_raw_title", "") or "")
        except Exception:
            return None

    def _begin_ocr_harvest(self, track_seq) -> bool:
        """Start the OCR burned-in-lyric harvest for the current track. Returns True if
        a harvest was started (caller then skips generation). One at a time, once/track."""
        if self._ocr_harvest_busy:
            return False
        if not self._ocr_gpu_safe():          # TICKET-125: don't hitch a running game
            log.info("OCR harvest skipped — a game is using the GPU")
            return False
        try:
            import ocr_lyrics
            if not ocr_lyrics.available():
                return False
        except Exception:
            return False
        hwnd = self._source_window_hwnd()      # may be None → screen-grab fallback inside read
        seq = self._track_seq
        # Use the CLEAN (artist, title) from self._track so the OCR cache is saved under
        # the SAME slug the title-matcher looks up — otherwise replay would re-match the
        # wrong cached body (e.g. the Sixth-Sense play_on.json) instead of the OCR result.
        _ta, _tt = (self._track or ("", ""))
        self._ocr_title = _tt or self._last_raw_title or (self.meta.get("title") or "")
        self._ocr_artist = _ta or self._last_artist or (self.meta.get("artist") or "")
        self._ocr_harvester = ocr_lyrics.LyricOcrHarvester(stable_polls=2)
        self._ocr_empty_polls = 0
        self._ocr_committed_seen = 0
        self._ocr_harvest_busy = True
        log.info("OCR harvest: reading burned-in lyrics for %r (hwnd=%s)",
                 self._ocr_title, hwnd)
        self._hint("👁️ Reading lyrics from the video…")
        threading.Thread(target=self._ocr_harvest_loop,
                         args=(seq, hwnd, self._ocr_title, self._ocr_artist),
                         daemon=True).start()
        return True

    def _ocr_harvest_loop(self, seq, hwnd, title, artist):
        """(bg thread) poll the source window's burned-in lyric band; OCR off the Tk
        thread, then marshal each result to _apply_ocr (which runs the harvester +
        commit, single-writer on the Tk thread). Self-cancels on track change."""
        import ocr_lyrics
        try:
            while seq == self._track_seq:
                # hard cap FIRST so a long pause can't keep this thread alive
                if time.time() - getattr(self, "_track_t0", 0.0) > OCR_HARVEST_HARD_CAP_S:
                    break
                st = self.media.get()
                if not (st and st.get("status") == PLAYING):
                    time.sleep(1.0)
                    continue
                pos = float(st.get("position") or 0.0)   # RAW position (generation convention)
                # once an OCR LRC is already showing, forbid the screen-grab fallback so a
                # black PrintWindow can never lock onto our own overlay (self-read guard).
                allow_fb = not ((self.meta.get("source") or "") == "ocr")
                try:
                    raw = ocr_lyrics.read_lyric_lines(
                        hwnd=hwnd, track_title=title, track_artist=artist,
                        allow_fallback=allow_fb)
                except Exception:
                    raw = []
                if seq != self._track_seq:
                    return
                self.root.after(0, lambda p=pos, ls=list(raw):
                                self._apply_ocr(seq, p, ls, title, artist))
                time.sleep(OCR_HARVEST_INTERVAL_S)
        finally:
            self._ocr_harvest_busy = False
            # Loop ended (hard cap, or a few lines committed but never reached the trust
            # threshold) on the SAME track with nothing showing → let generation take
            # over so the song isn't left blank. Skipped on a track change (seq stale)
            # or when OCR already committed real lyrics (self.lines set).
            if seq == self._track_seq:
                self.root.after(0, lambda: (
                    self._begin_generation()
                    if (not self.lines and not self._generating) else None))

    def _apply_ocr(self, seq, pos, lines, title, artist):
        """(Tk thread) run the harvester on this poll's OCR lines and, once OCR has
        proven itself (≥ OCR_MIN_COMMITS_TO_TRUST stable lines), commit them as the live
        lyrics with source='ocr'. If nothing burned-in appears, hand back to generation."""
        if seq != self._track_seq or self._ocr_harvester is None:
            return
        committed = self._ocr_harvester.observe(pos, lines)
        if committed is None:
            if not [l for l in (lines or []) if l.strip()]:
                self._ocr_empty_polls += 1
                if (self._ocr_committed_seen == 0
                        and self._ocr_empty_polls >= OCR_EMPTY_POLLS_GIVEUP):
                    log.info("OCR harvest: no burned-in lyrics after %d polls → generating",
                             self._ocr_empty_polls)
                    self._ocr_harvester = None
                    if not self.lines and not self._generating:
                        self._begin_generation()
            return
        self._ocr_empty_polls = 0
        self._ocr_committed_seen += 1
        rows = self._ocr_harvester.lines()             # list[(t_s, text)]
        new = []
        for i, (t_s, text) in enumerate(rows):
            end = rows[i + 1][0] if i + 1 < len(rows) else t_s + 4.0
            new.append({"t": [t_s, end], "jp": text, "rm": "", "en": ""})
        try:
            from fetch_lyrics import annotate
            annotate(new, "ja", translate=True)
        except Exception:
            pass
        if self._ocr_committed_seen < OCR_MIN_COMMITS_TO_TRUST:
            return                                     # accumulate quietly; don't pre-empt yet
        self.lines = [Line(start=d["t"][0], end=d["t"][1], jp=d["jp"],
                           rm=d.get("rm", ""), en=d.get("en", "")) for d in new]
        self.meta = {"title": title, "artist": artist, "lang": "ja",
                     "duration": self._cur_duration, "source": "ocr"}
        self._gen_token += 1
        self._deep_token += 1
        self._generating = False
        # TICKET-122: OCR is PROVISIONAL ground truth — it must EARN immunity by
        # corroborating against the audio (energy lock / by-ear), not get a free pass
        # (OCR can misread). Don't reset the flag here (a later poll mustn't wipe a
        # corroboration already earned); just don't seed it.
        self.idx = -1
        try:
            self._relayout_song()
        except Exception:
            pass
        try:
            from fetch_lyrics import slugify
            out = LYRICS_DIR / f"{slugify(title)}.json"
            data = {"meta": dict(self.meta),
                    "lines": [{"t": d["t"], "jp": d["jp"], "rm": d.get("rm", ""),
                               "en": d.get("en", "")} for d in new]}
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
            self.index.add(out)
            self._lyrics_path = out
        except Exception as e:
            log.info("OCR save failed: %s", e)

    def load(self, path, keep_idx=False):
        self.meta, self.lines = load_lyrics(path)
        # Guard (アイドル → idol.json): a provider LRC whose 'original' text EQUALS its
        # English translation is a TRANSLATION mislabeled as the body — English shown
        # where the Japanese + romaji should be. Catch it when the RAW video title is
        # CJK (a Mix can report a romanized SMTC title 'idol' that title-matches a
        # cached English LRC, but the browser tab still carries 「アイドル」). Bundled /
        # caption / generated bodies are trusted. Route to generation-by-ear (the real
        # language), at most once per track so it can't loop.
        _xsrc = (self.meta.get("source") or "")
        if (self.lines and not _xsrc.startswith(("generated", "bundled"))
                and _xsrc not in ("youtube-captions", "ocr")
                and self._track_seq != getattr(self, "_xlat_rejected_seq", None)
                and _has_cjk(getattr(self, "_last_raw_title", "") or "")
                and _body_is_translation(self.lines)):
            log.info("load: English-translation body rejected for CJK title %r "
                     "(src=%s) → generating by ear", self._last_raw_title, _xsrc)
            self._xlat_rejected_seq = self._track_seq
            self.lines, self.meta, self._lyrics_path = [], {"source": ""}, None
            self.root.after(50, self._begin_generation)
            return
        # Fast-switch language sanity check: a cached body's lang is incompatible
        # with the artist's known language (Kanade / 音乃瀬奏 cover of 怪獣の花唄
        # loaded a Spanish body with lang=es; without this the decision engine
        # took ~30 s of strikes to switch, eating most of a 3:47 song). Reject
        # the bad cache + re-fetch immediately — bypasses the strike accumulator
        # for the unambiguous "wrong-language-for-artist" case. Once-per-track
        # via _lang_rejected_seq so a stubborn provider can't loop.
        try:
            from fetch_lyrics import is_jp_vagency
            _BAD_LANGS_FOR_JP = ("ko", "zh", "es", "de", "ru", "fr", "it", "pt")
            _blang = (self.meta.get("lang") or "").lower()
            _btitle = self.meta.get("title") or ""
            _bartist = self.meta.get("artist") or ""
            # Include the RAW player title so hangul in it (e.g. TAK "PPPP" feat
            # 하츠네 미쿠 — a JP+Korean song) suppresses Korean rejection: those
            # Korean lyrics are correct, not a wrong-language body.
            try:
                _ptitle = (self.media.get() or {}).get("title") or ""
            except Exception:
                _ptitle = ""
            if (self.lines and _xsrc not in ("bundled",)
                    and self._track_seq != getattr(self, "_lang_rejected_seq", None)
                    and _blang in _BAD_LANGS_FOR_JP
                    and is_jp_vagency(_btitle, _bartist, extras=[_ptitle],
                                      strict=(_blang == "zh"))):
                log.info("load: rejected lang=%s body for JP-act %r / %r "
                         "(src=%s) → re-fetching", _blang, _btitle, _bartist, _xsrc)
                self._lang_rejected_seq = self._track_seq
                # Bin the stale file so the re-fetch doesn't immediately re-pick it.
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    pass
                self.lines, self.meta, self._lyrics_path = [], {"source": ""}, None
                # Kick a fresh fetch chain. report_wrong() is the existing
                # re-identify path — it clears decision state + re-runs the
                # full provider chain (now under v1.1.10's stricter guards).
                self.root.after(50, self.report_wrong)
                return
        except Exception as e:
            log.info("load: language sanity check raised %s — continuing", e)
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
        # kamone fix: a freshly loaded body is uncorroborated until evidence proves the LRC
        # text matches the singing (clean energy lock or healthy by-ear). bundled and
        # youtube-captions ARE the song's own body by construction → body-trusted on load.
        _bsrc = (self.meta.get("source") or "")
        # TICKET-122 — higher barrier: ONLY a hand-curated BUNDLE is trusted on load
        # without hearing the audio. youtube-captions / ocr are PROVISIONAL: they start
        # uncorroborated and must earn body-trust via a clean energy lock or by-ear read
        # (_note_energy_verdict / _score_ear_corrob) before they get switch/regen immunity.
        self._body_corroborated = bool(_bsrc.startswith("bundled"))
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

    def _apply_dynamic_priority(self):
        """TICKET-129 CPU policy: 'the last core drives the product'. By default this
        process is pinned to the LAST PHYSICAL core and run ABOVE_NORMAL — dedicating a
        single core keeps the overlay perfectly smooth while a game, which runs on the
        other cores, is barely affected (the core isolation is what protects the game
        now, replacing TICKET-127's IDLE-while-gaming downgrade). Override with tune
        knob `cpu_dedicate_last_core=0` for the legacy spread (upper cores +
        BELOW_NORMAL), which suits a CPU-only box doing heavy lyric generation.

        Hardware-agnostic: the mask comes from the live CPU topology, so it is correct
        on 2..64-thread machines, SMT or not. Idempotent — only touches the OS on a
        change, so this can be called every monitor tick and a live /tune flip lands
        within ~3s. Affinity is RE-asserted alongside priority so it self-heals."""
        try:
            import os as _os
            n = _os.cpu_count() or 1
            dedicate = int(self._tune.get("cpu_dedicate_last_core", 1) or 0)
            if dedicate and n >= 3:
                mask, prio, label = _dedicate_last_core_mask(n), _PRIO_ABOVE_NORMAL, "last-core/above-normal"
            else:
                mask, prio, label = _upper_cores_mask(n), _PRIO_BELOW_NORMAL, "upper-cores/below-normal"
            if (mask, prio) != getattr(self, "_cur_cpu_policy", None):
                ok, _ = _apply_affinity_priority(mask, prio)
                self._cur_cpu_policy = (mask, prio)
                log.info("cpu policy → %s (cpus=%d mask=0x%x set=%s)", label, n, mask, ok)
        except Exception:
            pass

    def _check_monitors(self, now):
        """Every ~3 seconds, re-enumerate monitors and re-apply the display
        setting if the topology changed (monitor plugged/unplugged, sleep/wake
        re-enumeration, resolution change).  Cheap: just compares fingerprints."""
        if now - getattr(self, "_last_mon_check", 0) < 3.0:
            return
        self._last_mon_check = now
        self._apply_dynamic_priority()        # TICKET-127: yield harder to a running game
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
        # combined writes for a single tick must stay ≤ 1 (one tier/deferred
        # commit at most). Anything else is a race that can produce a visible
        # snap.
        #   1. deferred-commit consumes self._pending_offset       (queued earlier)
        #   2. eased display offset is read for THIS frame's render
        # (The historical step 2 — fine-tune pause-end re-base — was retired
        # when the fine-tune PAUSE freeze was removed; corrections now route
        # through _smooth_offset and land in step 1's pending_offset queue.)
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
            # cross-language cover (sung in a different language than fetchable lyrics):
            # set independent of _is_cover so "English Ver." (not a 歌ってみた tag) still
            # routes to REGEN-in-cover-lang when the matched body's language differs.
            self._cover_lang = cover_language(rawt)
            # '(feat. X)' in the title names the real artist (not the uploading channel,
            # e.g. a game's 公式 channel). Captured here because clean_title strips it.
            self._title_feat_artists = feat_artists_from_title(rawt)
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
        # BEFORE we consume it. Historically used by the fine-tune pause-end
        # block (now retired); kept because TICKET-088's same-tick assertion
        # still reads it via _tick_offset_writes accounting.
        had_pending_pre = self._pending_offset is not None
        if self._pending_offset is not None and self.lines:
            cur_pos = state["position"] + self.offset
            cur_end = self.lines[self.idx].end if self.idx >= 0 else 0.0
            if (self.idx < 0 or cur_pos >= cur_end
                    or (time.time() - self._pending_offset_t)
                        > float(self._tune.get("offset_defer_cap_s", 3.0))):
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
        # ── TICKET-111: deferred whole-lyrics swap consumer ──────────────────
        # Placed RIGHT AFTER the _pending_offset consumer so TICKET-088 same-
        # tick ordering still holds (offset commits first; then the swap
        # commits against the fresh offset). Maintains the idx==-1 gap timer
        # used by _swap_ready for the LINE-mode instrumental-gap boundary.
        if self.idx == -1:
            if self._idx_minus_one_since == 0.0:
                self._idx_minus_one_since = time.time()
        else:
            self._idx_minus_one_since = 0.0
        try:
            self._try_apply_swap()
        except Exception as e:
            log.info("swap: _try_apply_swap raised %s", e)
        # (v1.0.85 fine-tune PAUSE expiry block removed — the freeze it managed
        # was retired in favor of _smooth_offset boundary-deferred rewinds. The
        # _fine_pause_* fields stay on the instance at their default 0.0/None for
        # /diag telemetry continuity but are never set anywhere.)
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
        # display_lead_s: lead the audio by a small constant to cancel the
        # SYSTEMATIC lag the user kept reporting ("highlights constantly behind").
        # The browser's SMTC position is reported slightly stale + there's a
        # render/tick latency, so the highlighted line trails the vocals by a
        # fixed amount on every song. Adding a small lead to BOTH timebases
        # shifts the line-index AND the karaoke fill earlier so they track the
        # singing. Tunable (default 0.3s); set 0 to disable.
        _lead = float(self._tune.get("display_lead_s", 0.0))
        pos = state["position"] + self._eased_offset() + _lead
        pos_raw = state["position"] + self.offset + _lead
        # ── v1.1.42 — HIGHLIGHT CLOCK REGRESSED TO THE JUN-26 BUILD ─────────
        # The user confirmed the highlight was PERFECT in the "Lyric Immersion
        # Test" video (uploaded 2026-06-26 22:11 UTC, build ~v1.0.74). That
        # build drove the active-line index + karaoke fill + scroll belt from
        # ONE simple timebase: the raw song position plus a smoothly-EASED sync
        # offset — exactly `pos` above. The v1.1.23→1.1.41 two-layer
        # slew-limited clock (_hi_pos) replaced it and was STARVING the fill —
        # "really slow and snap", sometimes "no highlights" — because its §3
        # output slew-limit caps forward motion to a few × realtime, so after
        # any internal clock step the visible fill crawls and then jumps to
        # catch up. Route every visible consumer back onto the Jun-26 clock.
        # Sync still corrects via self.offset, which _eased_offset glides in
        # over a few frames, so "only the lyrics follow sync; the highlight just
        # steadily fills." _hi_pos is retained below (now unused) for reference.
        # pos_raw stays the TRUE clock for perf logging + the deferred-commit
        # boundary test only.
        pos_hi = pos
        self._pos_hi = pos_hi          # honest live value for /syncdiag (the retired
        #                                _hi_clock/_hi_out are no longer written)
        # ── GPU-RENDERER FAST PATH (v1.1.15 — the performance fix) ──────────
        # When the GL child process is drawing, the MAIN process must do ZERO
        # Pillow/canvas rendering. That CPU text work holds the GIL on the Tk
        # thread and was the root cause of the THREE symptoms the user reported
        # together: audio stutter, the "lyrics disappear for a millisecond then
        # reappear" frame-drop flicker, and the highlight lagging ~2 lines (the
        # tick that computes the active line was being blocked by render work).
        # Here we ONLY compute the active-line index + push it to the child over
        # IPC, then reschedule and return — every _render/_karaoke/scroll-belt
        # path below is skipped. If the child dies, _gpu_active() flips False and
        # the normal CPU render resumes automatically next tick.
        #
        # v1.1.44: the **Tauri overlay** is also a "the GPU is the renderer now"
        # path. When it's on (Tk window is withdrawn), do the SAME: compute only
        # the active-line index so get_overlay_state()/`/overlay` stays fed, and
        # skip ALL Tk canvas work — so we never render two overlays at once and
        # the CPU isn't drawing lyrics the user can't see. The 2s watchdog clears
        # tauri_overlay_on + restores Tk if the overlay child dies.
        if self._gpu_active() or getattr(self, "tauri_overlay_on", False):
            new = -1
            for i, ln in enumerate(self.lines):     # highlight clock — same as the fill
                if ln.start <= pos_hi < ln.end:
                    new = i
                    break
            self._diag_idx_skip(new, pos_hi, state)
            self.idx = new
            try:
                self._gpu_send_state(pos_hi)        # no-op when there's no GL child
            except Exception:
                pass
            self.root.after(self._fps, self._tick)
            return
        # Highlight + line index + scroll belt all track pos_hi, which since
        # v1.1.42 IS the Jun-26 eased clock (pos = song position + eased offset
        # + lead) — NOT the retired slew-limited _hi_pos. Sync corrections go
        # through _smooth_offset (boundary-deferred) and are applied via
        # _eased_offset, so the offset commits at a line boundary and glides in;
        # the highlight itself just steadily fills.
        branch_tag = "line"

        # BELT MOTION + FILL both ride pos_hi (== pos, the eased song clock).
        # The belt's per-frame scroll is v·(pos − last_pos); smoothness comes
        # from _eased_offset (rate-capped) + the boundary-deferred offset, NOT a
        # slew limiter. The belt's own discontinuity guard (_ticker_update /
        # _ticker_update_v reseed when |Δpos| > belt_reseed_s) absorbs the
        # track-change position snap to ~0, so the belt can't lurch backward by
        # the whole song. Using the SAME clock for motion and fill keeps the
        # gold fill locked to each line's on-screen position (no divergence).
        if self.scroll_dir in ("lr", "rl"):       # continuous horizontal scroll-through
            self._ticker_update(pos_hi, pos_hi)
            self._render_frame = True
            self._perf_record(state, pos, pos_raw, "scroll-h")
            self.root.after(self._fps, self._tick)
            return
        if self.scroll_dir in ("tb", "bt"):       # continuous vertical scroll-through
            self._ticker_update_v(pos_hi, pos_hi)
            self._render_frame = True
            self._perf_record(state, pos, pos_raw, "scroll-v")
            self.root.after(self._fps, self._tick)
            return

        # LINE-INDEX + karaoke FILL both ride pos_hi (== the eased pos clock) —
        # ONE timebase, exactly as the Jun-26 build did. (The TICKET-082 split
        # that ran the index on eased pos and the fill on raw pos_raw is gone;
        # it diverged on a correction — "fill stops midway, then snaps full" —
        # and the unified clock fixes that.) self.offset only changes at line
        # boundaries (boundary-deferred via _smooth_offset), so the clock is
        # smooth mid-line and the highlight just runs.
        new = -1
        for i, ln in enumerate(self.lines):
            if ln.start <= pos_hi < ln.end:
                new = i
                break
        self._diag_idx_skip(new, pos_hi, state)
        if new != self.idx:
            self.idx = new
            if new >= 0:
                # Entering a real line — render it + arm the held-line state.
                self._last_line_idx = new
                self._gap_start_t = None
                self._render(self.lines[new])
            else:
                # Entering an inter-line gap (new == -1). Hold the previous line
                # on-canvas for gaps shorter than keep_last_line_gap_s so the
                # overlay doesn't flicker blank between consecutive lines; clear
                # only once the gap is genuinely long (instrumental) or there's
                # no prior line. _gap_start_t is reset on track change AND armed
                # here, so a stale value can't leak across songs.
                if self._gap_start_t is None:
                    self._gap_start_t = time.time()
                gap_dur = time.time() - self._gap_start_t
                gap_hold = float(self._tune.get("keep_last_line_gap_s", 0.6))
                hold_ok = (gap_dur < gap_hold
                           and 0 <= self._last_line_idx < len(self.lines))
                if not hold_ok:
                    self.cv.delete("all")
                    self._kara = []
        elif new >= 0:
            self._karaoke(pos_hi)

        self._render_frame = True
        self._perf_record(state, pos, pos_raw, branch_tag)
        # M2: feed the GPU renderer child (if active) one state message per
        # tick. The cost is one JSON-encode + pipe-write of ~80 bytes; cheap
        # against the existing per-tick render work. Skipped early via the
        # nullable _gpu_child check inside _gpu_send_state.
        try:
            self._gpu_send_state(pos_hi)
        except Exception:
            pass
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
            delta = pos - self._last_pos
            # BELT DISCONTINUITY GUARD: a smooth frame advances pos by < ~0.1s
            # (pos_hi is slew-limited). A LARGE jump means pos_hi was legitimately
            # cut — a track change (pos snaps to ~0 while _last_pos still holds the
            # old song's position), a real seek, or the first frame after a mode
            # switch. Moving the belt by v×(that jump) would lurch it across the
            # screen in one frame, so RESEED the baseline and skip the move; the
            # want-window respawns blocks at their correct absolute X within a
            # frame or two. This makes the belt immune to ANY pos_hi discontinuity.
            if abs(delta) > float(self._tune.get("belt_reseed_s", 0.5)):
                self._strm_rem = 0.0
            else:
                dx_f = -d * v * delta + self._strm_rem
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
            delta = pos - self._last_pos
            # BELT DISCONTINUITY GUARD (see _ticker_update): a large pos jump =
            # track change / seek / mode switch. Reseed instead of lurching the
            # whole column by v×jump in one frame; blocks respawn at correct Y.
            if abs(delta) > float(self._tune.get("belt_reseed_s", 0.5)):
                self._strm_rem = 0.0
            else:
                dy_f = -d * v * delta + self._strm_rem
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
        self._sync_event("offset_defer", reason=reason or "auto", to=round(new_off, 2),
                         frm=round(self.offset, 2), idx=self.idx)
        log.info("sync: deferring %+.2fs → %+.2fs until current line ends (%s)",
                 self.offset, new_off, reason or "auto")

    def _diag_idx_skip(self, new, pos_hi, state):
        """DIAG: log when the active line index SKIPS intermediate lines (jumps by
        >1), with the clock internals, so we can see WHY the highlight skips a
        couple lines instead of advancing one at a time."""
        try:
            if not int(self._tune.get("hi_diag", 1)):
                return
            prev = self.idx
            if new >= 0 and prev >= 0 and abs(new - prev) > 1:
                raw = state.get("position", 0.0) if isinstance(state, dict) else 0.0
                live = getattr(self, "_live_mode", False) or getattr(self, "_live_arrangement", False)
                # v1.1.42: pos_hi is now the eased pos clock (the slew-limited
                # _hi_clock/_hi_offset/_hi_corr are retired and no longer logged).
                log.info("hi-skip: idx %d→%d (Δ%d)  pos_hi=%.2f raw=%.2f off=%+.2f live=%s",
                         prev, new, new - prev, pos_hi, raw, self.offset, live)
                self._sync_event("idx_skip", frm=prev, to=new, delta=new - prev,
                                 pos_hi=round(pos_hi, 2), offset=round(self.offset, 2),
                                 drift=getattr(self, "_last_drift", None), live=live)
        except Exception:
            pass

    def _sync_event(self, kind, **fields):
        """Append a sync diagnostic event to the bounded ring buffer (for
        /syncdiag). Called only at real EVENTS (hi-snap, idx-skip, offset
        commit/defer, drift read, caption load, decision switch, energy-align,
        force-sync) — never per frame — so it's a few appends/sec at most. Never
        raises (diagnostics must not break playback)."""
        try:
            if not int(self._tune.get("sync_event_enabled", 1)):
                return
            fields["t"] = round(time.time(), 2)
            fields["kind"] = kind
            ev = self._sync_events
            ev.append(fields)
            cap = int(self._tune.get("sync_event_buffer_size", 200))
            if len(ev) > cap:
                del ev[:len(ev) - cap]
        except Exception:
            pass

    def get_sync_diag(self):
        """Ring buffer of recent sync events + a live snapshot of the highlight
        clock, offset, mode, and current line — so a 'fucked highlights' report
        is one `curl 127.0.0.1:8765/syncdiag` away from a full diagnosis."""
        def g(a):
            try:
                return round(float(getattr(self, a, 0.0) or 0.0), 3)
            except Exception:
                return None
        idx = self.idx if 0 <= self.idx < len(self.lines) else -1
        ln = self.lines[idx] if idx >= 0 else None
        st = self.media.get() or {}
        m = self.meta if isinstance(self.meta, dict) else {}
        po = getattr(self, "_pending_offset", None)
        return {
            "events": list(self._sync_events)[-120:],
            "n_events": len(self._sync_events),
            "clock": {
                # v1.1.42: pos_hi IS the live highlight clock (== eased pos). The
                # old slew-limited _hi_clock/_hi_out/_hi_offset/_hi_corr are
                # retired and no longer written — reporting pos_hi instead.
                "pos_hi": g("_pos_hi"), "clock_model": "eased-pos (v1.1.42)",
                "offset": g("offset"),
                "pending_offset": (round(po, 3) if po is not None else None),
                "last_drift": getattr(self, "_last_drift", None),
                "drift_integral": g("_drift_integral"),
                "drift_monotonic_since": round(getattr(self, "_drift_monotonic_since", 0.0), 1),
            },
            "line": ({"idx": idx, "start": round(ln.start, 2), "end": round(ln.end, 2),
                      "jp": ln.jp} if ln else {"idx": idx}),
            "mode": {"is_cover": getattr(self, "_is_cover", False),
                     "live_mode": getattr(self, "_live_mode", False),
                     "live_arrangement": getattr(self, "_live_arrangement", False),
                     "verified": getattr(self, "_verified", False),
                     "title_locked": getattr(self, "_title_locked", False),
                     "force_sync_active": getattr(self, "_force_sync_active", False)},
            "source": m.get("source"), "title": m.get("title"), "artist": m.get("artist"),
            "playing": (st.get("status", PLAYING) == PLAYING),
            "position": round(float(st.get("position", 0.0) or 0.0), 2),
            "frame_worst_ms": getattr(self, "_frame_worst", None),
            "offset_hist": list(getattr(self, "_offset_hist", []))[-12:],
        }

    def _hi_pos(self, state, lead):
        """The FREE-RUNNING highlight clock — the single timebase for the active
        line index + karaoke fill (CPU and GPU). It is

            (smooth song clock)  +  (frozen sync offset)  +  lead

        and it solves the user's "fill stops halfway, then jumps 2-3 lines fully
        filled" with TWO independent free-runs:

        1) SMOOTH SONG CLOCK. The browser's SMTC position is reported choppily —
           it FREEZES for a second or two then JUMPS to catch up. Reading it raw
           made the fill freeze then leap. Instead we advance our own clock at 1×
           every frame while playing and only RE-ANCHOR to the reported position:
           a real seek / big drift (> hi_seek_snap_s) snaps; small drift is gently
           pulled. In normal playback our 1× clock already matches the true
           position, so the reported value confirms it (gap ≈ 0) and the fill
           just glides — it never stops and never jumps.

        2) FROZEN SYNC OFFSET. Mid-song sync re-estimates (boundary-deferred
           commits, drift nudges, REGEN re-locks, energy corrections) do NOT yank
           the fill. The offset re-locks only at discrete moments: a new track,
           the initial lock-in, or a slow deadbanded pull for small drift; large
           mid-song deltas are ignored. ("let the highlights run on what they
           think they are; if they stop because of sync, ignore it.")

        A third layer (§3) slew-limits the VISIBLE output so the belt/index/fill
        can never lurch: the true clock may step (a stall credit, a correction),
        but the displayed position chases it at a bounded per-frame rate — zero
        lag in steady state, a quick smooth catch-up after any step, and a clean
        cut only on a real seek or track change. Sustained non-PLAYING freezes
        the clock; a brief blip does not (sticky playback).
        """
        raw_pos = state.get("position", 0.0) if isinstance(state, dict) else 0.0
        playing = True
        if isinstance(state, dict):
            playing = (state.get("status", PLAYING) == PLAYING)
        target = float(self.offset)
        now = time.time()
        seq = getattr(self, "_track_seq", 0)
        # (re)initialise on first use or a track change → lock to current values
        if getattr(self, "_hi_clock", None) is None or getattr(self, "_hi_seq", -1) != seq:
            self._hi_clock = raw_pos
            self._hi_offset = target
            self._hi_seq = seq
            self._hi_t = now
            self._hi_locked = False
            self._hi_last_raw = raw_pos        # last SMTC reading seen (change-detect)
            self._hi_corr = 0.0                # pending position correction, bled smoothly
            self._hi_play_since = now          # wall-time of the last PLAYING reading
            self._hi_out = self._hi_clock + self._hi_offset + lead   # slew-limited display
            return self._hi_out
        raw_dt = now - getattr(self, "_hi_t", now)
        self._hi_t = now
        nominal_dt = max(0.008, float(self._fps) / 1000.0)   # one frame's worth, in seconds

        # ── 1) smooth song clock (ground-truth position estimate) ──
        # Free-run at 1× while playing. CRITICAL: do NOT pull toward the reported
        # position every frame — the browser HOLDS a stale value between SMTC
        # updates, so a continuous pull drags the clock backward (it lags ~1.5s
        # and zig-zags). Only re-anchor when the reported value actually CHANGES
        # (a fresh reading = the true position): snap on a seek, else schedule a
        # small correction that bleeds in smoothly over the next few frames.
        #
        # STICKY PLAYBACK (the "fell 7.77s behind then snapped" bug): a brief
        # non-PLAYING reading (buffering, a tab/SMTC handoff, an app switch) must
        # NOT freeze the clock. If it does, the clock loses real playback time and
        # then SNAPS forward when status returns — lurching the belt 2-3 lines.
        # Keep crediting time through blips shorter than pause_debounce_s; only a
        # SUSTAINED non-PLAYING state is treated as a real pause.
        debounce = float(self._tune.get("hi_pause_debounce_s", 0.6))
        if playing:
            self._hi_play_since = now
            play_active = True
        else:
            play_active = (now - getattr(self, "_hi_play_since", now)) < debounce

        # Credit REAL elapsed time every active frame — EVEN across a Tk-thread
        # stall. The song advanced by raw_dt whether or not we rendered, so
        # advancing the clock by raw_dt keeps it matching true position with NO
        # jump. (The old code zeroed dt and hard-set _hi_clock=raw_pos on a stall;
        # that hard set was itself the visible skip.) Cap one credit at
        # max_credit_s so a sleep / lid-close / debugger-pause outlier can't
        # fast-forward minutes; that rare case reconciles via the bleed below.
        max_credit = float(self._tune.get("hi_max_credit_s", 5.0))
        dt = max(0.0, min(raw_dt, max_credit))
        if play_active:
            self._hi_clock += dt

        # Reconcile to the reported position — only on a FRESH reading, and only
        # via the bounded bleed (never a hard set) unless it's a genuine large
        # seek. This keeps drift from accumulating without ever yanking the clock.
        seek_snap = float(self._tune.get("hi_seek_snap_s", 4.0))
        snapped = False
        if abs(raw_pos - getattr(self, "_hi_last_raw", raw_pos)) > 1e-6:   # fresh reading
            self._hi_last_raw = raw_pos
            gap_p = raw_pos - self._hi_clock
            if abs(gap_p) > seek_snap:
                if int(self._tune.get("hi_diag", 1)):
                    log.info("hi-snap: clock %.2f→%.2f (Δ%+.2f) playing=%s",
                             self._hi_clock, raw_pos, gap_p, playing)
                self._sync_event("hi_snap", frm=round(self._hi_clock, 2), to=round(raw_pos, 2),
                                 delta=round(gap_p, 2), playing=playing)
                self._hi_clock = raw_pos                   # real seek / track scrub → cut
                self._hi_corr = 0.0
                snapped = True
            elif abs(gap_p) > 0.05:
                self._hi_corr = gap_p                      # schedule smooth catch-up
        corr = getattr(self, "_hi_corr", 0.0)              # bleed any pending correction
        if abs(corr) > 1e-4:
            cap = 2.0 * max(dt, nominal_dt)                # up to 2 s/s — fast but continuous
            step = max(-cap, min(cap, corr))
            self._hi_clock += step
            self._hi_corr = corr - step

        # ── 2) sync offset (frozen studio / fast-glide live) ──
        gap_o = target - self._hi_offset
        live = getattr(self, "_live_mode", False) or getattr(self, "_live_arrangement", False)
        if live:
            # LIVE / concert version: FOLLOW the measured offset, but only when it
            # actually COMMITS. self.offset (== target) is boundary-deferred via
            # _smooth_offset, so it changes at line boundaries, not mid-line.
            # Snapping _hi_offset to it (instead of the old per-frame glide) keeps
            # the gold fill STEADY within a line and re-anchors only at a boundary
            # — the user's "only the lyrics follow sync; the highlight just
            # proceeds". Following stays aggressive (a committed correction is
            # taken in full), and the §3 output slew-limit smooths the boundary
            # hand-off so it's never a teleport. The old continuous glide
            # (hi_live_pull_per_sec) was the main cause of concert fill jitter.
            self._hi_offset = target
            self._hi_locked = False           # re-freeze fresh if it later goes studio
        else:
            # STUDIO version: freeze the offset mid-song so a sync re-estimate
            # can't snap the fill across lines.
            settle_s  = float(self._tune.get("hi_settle_s", 22.0))
            pull_band = float(self._tune.get("hi_pull_band_s", 3.0))
            pull_rate = float(self._tune.get("hi_pull_per_sec", 0.6))
            dead      = float(self._tune.get("hi_deadzone_s", 0.12))
            if not getattr(self, "_hi_locked", False):
                self._hi_offset = target
                if abs(target) > 0.05 or self._hi_clock >= settle_s:
                    self._hi_locked = True
            elif abs(gap_o) <= dead:
                self._hi_offset = target
            elif abs(gap_o) <= pull_band:
                step = pull_rate * dt
                self._hi_offset += max(-step, min(step, gap_o))
            # else: |gap_o| > pull_band, locked → IGNORE (no snap from a re-estimate)

        # ── 3) OUTPUT SLEW LIMIT — the hard no-lurch guarantee ──
        # The scroll belt, the active-line index, the karaoke fill, AND the GPU
        # child all read this one return value, and the belt moves by
        # v·(pos − last_pos) per frame — so any single-frame jump in the output
        # lurches the belt by multiple lines. _hi_clock above is the true
        # position and may legitimately STEP (a full-rate elapsed credit after a
        # stall, a corr bleed, the offset settling). A real Tk-thread stall also
        # draws NO frames while it lasts, so the first frame afterward would move
        # the belt by the whole stall in one step. So the VISIBLE output chases
        # the true clock at a bounded PER-FRAME rate: in steady state it equals
        # the clock (zero lag — the budget exceeds one frame of real time), and
        # after any step it catches up over a few frames (a quick smooth scroll,
        # never a teleport). A genuine seek / track change bypasses the limit
        # (the belt SHOULD jump there).
        out_target = self._hi_clock + self._hi_offset + lead
        prev_out = getattr(self, "_hi_out", out_target)
        if snapped or abs(out_target - prev_out) > seek_snap:
            self._hi_out = out_target                      # seek / track change → cut through
        else:
            catchup = float(self._tune.get("hi_out_catchup", 4.0))
            fwd_cap = nominal_dt * (1.0 + catchup)         # forward: realtime + catch-up budget
            back_cap = nominal_dt * min(catchup, 1.5)      # backward: gentle (reverse-scroll is jarring)
            delta = max(-back_cap, min(fwd_cap, out_target - prev_out))
            self._hi_out = prev_out + delta
        return self._hi_out

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
          tail -f <install-dir>/perf.log                # watch live
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
        """Fill the active line's chars up to (pos − line.start)/dur. The param
        is named pos_raw for history, but since v1.1.42 _tick passes pos_hi
        (== the eased pos clock) — index + fill share ONE timebase, as the
        Jun-26 build did. (The TICKET-082 raw-vs-eased split is retired.)"""
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
        self._m(self.metrics.note_resync, "nudge")               # TICKET-121

    def reset_offset(self):
        self._fine_exit("manual-reset")            # user input wins; drop pause buffers
        # TICKET-088: route through _smooth_offset for a glided reset (snaps
        # automatically when there's no line on screen or when the current
        # offset is >5s, which covers most "reset is huge" cases).
        self._smooth_offset(0.0, "manual-reset")

    def get_tune(self):
        """Snapshot of the live-tunable sync parameters (for GET /tune)."""
        return dict(self._tune)

    def _m(self, fn, *args):
        """Crash-safe metrics hook wrapper — a telemetry bug must never break playback."""
        try:
            fn(*args)
        except Exception:
            pass

    def get_metrics(self) -> dict:
        """Snapshot for GET /metrics — per-release success/wobbler/fail counts."""
        return self.metrics.as_dict()

    def get_sync(self) -> dict:
        """Snapshot for GET /sync — everything about the current offset lock: the live
        offset, the last energy-correlation result, the last OCR-assisted alignment, and
        the verify cadence. Lets you see WHY a song is/ isn't locked and by how much."""
        st = self.media.get() or {}
        return {
            "offset_s": round(self.offset, 2),
            "player_pos_s": round(float(st.get("position") or 0.0), 2),
            "source": self.meta.get("source"),
            "verified": bool(getattr(self, "_verified", False)),
            "body_corroborated": bool(getattr(self, "_body_corroborated", False)),
            "live_mode": bool(self._live_mode),
            "verify_cadence_s": round(float(getattr(self, "_sync_tier_interval", 0.0)), 1),
            "last_energy_align": getattr(self, "_last_energy", None),
            "last_ocr_sync": getattr(self, "_last_ocr_sync", None),
            "fine_tuning": bool(getattr(self, "_fine_active", False)),
        }

    def get_measure_sync(self) -> dict:
        """Snapshot for GET /measure_sync — the SYNC ACCURACY METER. Quantifies the
        'highlight is N lines behind' symptom by comparing the line currently SHOWN
        against the line that SHOULD be active for the real audio position.

        shown_idx      = the line the overlay is highlighting right now (self.idx)
        should_idx     = the line whose [start,end] contains pos_raw
        lag_lines      = should_idx - shown_idx (positive = highlight is BEHIND)
        lag_s          = how far pos_raw is past the shown line's window (0 if inside)
        plus the offset, last energy best_shift/score, and the last heard transcription.
        """
        st = self.media.get() or {}
        player_pos = float(st.get("position") or 0.0)
        pos_raw = player_pos + self.offset
        lines = self.lines or []

        def _window(i):
            return [lines[i].start, lines[i].end] if 0 <= i < len(lines) else None

        should_idx = -1
        for i, ln in enumerate(lines):
            if ln.start <= pos_raw < ln.end:
                should_idx = i
                break
        shown_idx = self.idx if 0 <= self.idx < len(lines) else -1

        # seconds the audio is past the SHOWN line's end (the visible lag), or
        # negative if the shown line hasn't started yet.
        lag_s = None
        sw = _window(shown_idx)
        if sw is not None:
            if pos_raw > sw[1]:
                lag_s = round(pos_raw - sw[1], 2)      # audio past the shown line
            elif pos_raw < sw[0]:
                lag_s = round(pos_raw - sw[0], 2)      # audio before it (negative)
            else:
                lag_s = 0.0
        en = getattr(self, "_last_energy", None) or {}
        sound = getattr(self, "_sound_song", None)
        return {
            "player_pos_s": round(player_pos, 2),
            "offset_s": round(self.offset, 2),
            "pos_raw_s": round(pos_raw, 2),
            "shown_idx": shown_idx,
            "shown_window": sw,
            "shown_text": (lines[shown_idx].jp if 0 <= shown_idx < len(lines) else None),
            "should_idx": should_idx,
            "should_window": _window(should_idx),
            "should_text": (lines[should_idx].jp if 0 <= should_idx < len(lines) else None),
            "lag_lines": (should_idx - shown_idx) if (should_idx >= 0 and shown_idx >= 0) else None,
            "lag_s": lag_s,
            "n_lines": len(lines),
            "source": self.meta.get("source"),
            "gpu_renderer": bool(getattr(self, "gpu_renderer_on", False)),
            "energy_best_shift": en.get("best_shift"),
            "energy_best_score": en.get("best_score"),
            "energy_ambiguous": en.get("ambiguous"),
            "heard_song": list(sound) if sound else None,
            "in_sync": bool(should_idx == shown_idx),
        }

    def force_resync(self):
        """POST /resync — hammer every sync method at once: an energy re-align (which
        escalates to OCR on a weak peak) plus a direct OCR-assisted alignment. Spawns a
        worker so the HTTP handler returns immediately and the OCR/Whisper work is off
        the Tk thread."""
        try:
            self._last_ocr_sync_t = 0.0          # bypass the throttle for a manual force
            self.root.after(0, lambda: self._auto_align_by_energy("api-resync"))
            threading.Thread(target=lambda: self._ocr_assisted_sync("api-resync"),
                             daemon=True).start()
        except Exception as e:
            log.info("force_resync error: %s", e)

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
            # TICKET-111: deferred whole-lyrics swap state
            "pending_swap": self._diag_pending_swap(),
            # TICKET-113: per-track blacklist + /wrong escalation + provider rotation
            "lyrics": {
                "lyrics_blacklist_count": len(self._lyrics_blacklist),
                # Source names (not hex hashes) so the snapshot is human-readable
                "lyrics_blacklist_sources": sorted({src for (src, _sig)
                                                    in self._lyrics_blacklist}),
                "wrong_streak": self._wrong_streak,
                "wrong_streak_age_s": (round(time.time() - self._wrong_streak_t, 1)
                                       if self._wrong_streak_t else None),
                "provider_order": list(self._provider_order),
                "force_ai_gen": bool(self._force_ai_gen),
                # bool, not the dict — keeps /diag output bounded if a YT
                # description is several KB
                "yt_metadata_present": bool(getattr(self, "_yt_metadata", None)),
            },
            # TICKET-112: full parsed YT-description metadata for the
            # current track. The raw description and lyrics_block are
            # capped at the extractor; here we surface a SUMMARY (drop the
            # bulkiest fields) so /diag stays bounded. Full text is
            # available via the dedicated /yt-meta endpoint.
            "yt_metadata": self._diag_yt_metadata(),
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
            # TICKET-117: SMTC session pin (Tab-A-muted / Tab-B-lyrics).
            # `available` is every session the watcher last saw; `pinned_id`
            # is the composite hash currently locked, or null for Auto.
            "sessions": self._diag_sessions(),
            # TICKET-118: audible-session preference (Core Audio peak meter
            # tiebreaker when multiple SMTC sessions are PLAYING). Mirrors
            # the watcher's last decision so field reports can show WHY a
            # given tab was chosen without needing process-level access.
            "audible_pref": self._diag_audible_pref(),
            # concert banner OCR (live-mode song ID from the on-screen title) — lets a
            # field report confirm OCR is firing AND what it last read (e.g. 'ダリア').
            "ocr": self._diag_ocr(),
        }

    def _diag_ocr(self):
        """Concert banner OCR state for /diag. Never raises. OCR only runs in
        live_mode (a concert/medley), so `eligible` = enabled AND available AND
        live_mode; `last_song` is the title the banner OCR last confidently read."""
        try:
            try:
                import concert_ocr
                avail = bool(concert_ocr.available())
            except Exception:
                avail = False
            t = getattr(self, "_last_ocr_t", 0) or 0
            live = bool(getattr(self, "_live_mode", False))
            enabled = bool(getattr(self, "concert_ocr", False))
            return {
                "enabled": enabled,
                "available": avail,
                "live_mode": live,
                "eligible": bool(enabled and avail and live),
                "last_song": getattr(self, "_ocr_song", None),
                "last_read_age_s": (round(time.time() - t, 1) if t else None),
            }
        except Exception:
            return {"enabled": None, "available": None}

    def _diag_yt_metadata(self):
        """TICKET-112: SUMMARY snapshot of the YT-description metadata for
        /diag. Drops the bulkiest fields (`description`, `lyrics_block`) so
        /diag output stays bounded; the full dict is reachable via the
        dedicated /yt-meta endpoint. Returns None when no metadata yet.
        Never raises."""
        try:
            m = getattr(self, "_yt_metadata", None)
            if not m:
                return {
                    "present": False,
                    "fetching": bool(getattr(self, "_yt_metadata_fetching", False)),
                    "video_id": getattr(self, "_yt_metadata_video_id", None),
                }
            out = {
                "present": True,
                "fetching": bool(getattr(self, "_yt_metadata_fetching", False)),
                "video_id": m.get("video_id"),
                "title_raw": m.get("title_raw"),
                "channel": m.get("channel"),
                "uploader": m.get("uploader"),
                "composer": list(m.get("composer") or []),
                "lyricist": list(m.get("lyricist") or []),
                "arranger": list(m.get("arranger") or []),
                "vocals": list(m.get("vocals") or []),
                "original_artist": list(m.get("original_artist") or []),
                "language": m.get("language"),
                "tags": list(m.get("tags") or [])[:8],
                "upload_date": m.get("upload_date"),
                "fetched_at": m.get("fetched_at"),
                "from_cache": bool(m.get("from_cache")),
                "has_lyrics_block": bool(m.get("lyrics_block")),
                "description_chars": len(m.get("description") or ""),
            }
            return out
        except Exception:
            return None

    def get_yt_metadata(self):
        """TICKET-112: full parsed YT-description metadata for /yt-meta.
        Unlike _diag_yt_metadata this INCLUDES the raw description body and
        lyrics_block — invaluable when a description has unusual labels we
        don't yet parse. Returns None when no metadata is loaded yet."""
        m = getattr(self, "_yt_metadata", None)
        if not m:
            return {
                "present": False,
                "fetching": bool(getattr(self, "_yt_metadata_fetching", False)),
                "video_id": getattr(self, "_yt_metadata_video_id", None),
            }
        out = dict(m)
        out["present"] = True
        out["fetching"] = bool(getattr(self, "_yt_metadata_fetching", False))
        return out

    def _diag_sessions(self):
        """TICKET-117: snapshot of every visible SMTC session + the pin state
        for /diag. Status integers are decoded to human strings to match the
        existing /source convention. Never raises."""
        STATUS = {0: "closed", 1: "opened", 2: "changing", 3: "stopped",
                  4: "playing", 5: "paused"}
        out = {
            "pinned_id": self.pinned_session_id or None,
            "pinned_app": self.pinned_session_app or None,
            "pinned_grace_s": self._pinned_grace_s(),
            "pinned_grace_remaining_s": 0.0,
            "available": [],
        }
        try:
            sessions = self.media.list_sessions()
        except Exception:
            sessions = []
        ids_now = set()
        for s in sessions:
            sid = s.get("id") or ""
            ids_now.add(sid)
            out["available"].append({
                "id": sid,
                "source_app": s.get("source") or "",
                "title": s.get("title") or "",
                "artist": s.get("artist") or "",
                "status": STATUS.get(s.get("status"), s.get("status")),
                "is_pinned": bool(sid and sid == self.pinned_session_id),
            })
        if self.pinned_session_id and self.pinned_session_id not in ids_now:
            remaining = max(0.0, self._pinned_grace_s()
                                  - (time.time() - self._pinned_last_seen_t))
            out["pinned_grace_remaining_s"] = round(remaining, 2)
        return out

    def _diag_audible_pref(self):
        """TICKET-118: snapshot of the audible-session preference state for
        /diag. Combines the watcher's last-pick reason + per-session scores
        with the audible_sessions module's own diag (cache age, last error,
        the raw per-process levels that fed the score). Never raises.

        Scores are per-session-id, but for human-readable debugging we also
        attach the source_app so the reader doesn't have to cross-reference
        with /diag.sessions.available.
        """
        out = {
            "enabled": False,
            "threshold": 0.005,
            "last_reason": None,
            "scores": [],
            "module": None,
        }
        try:
            pref = self.media.get_audible_pref_diag()
        except Exception:
            pref = None
        try:
            sess_map = {s.get("id"): (s.get("source") or "")
                        for s in self.media.list_sessions()}
        except Exception:
            sess_map = {}
        if pref:
            out["enabled"] = bool(pref.get("enabled"))
            out["threshold"] = float(pref.get("threshold", 0.005))
            out["last_reason"] = pref.get("last_reason")
            scores = pref.get("scores") or {}
            for sid, peak in scores.items():
                out["scores"].append({
                    "session_id": sid,
                    "source_app": sess_map.get(sid, ""),
                    "peak": round(float(peak), 4),
                    "above_threshold": float(peak) >= out["threshold"],
                })
            out["scores"].sort(key=lambda r: r["peak"], reverse=True)
        try:
            import audible_sessions
            mod = audible_sessions.diag() or {}
            # Surface ONLY processes whose basename appears in some session's
            # source_app — keeps /diag bounded and avoids leaking unrelated
            # audible-app names to anyone reading diag output.
            full_levels = mod.get("levels") or {}
            srcs = " ".join(sess_map.values()).lower()
            mod["levels"] = {
                n: round(float(p), 4)
                for n, p in full_levels.items()
                if n and n in srcs
            }
            out["module"] = mod
        except Exception:
            out["module"] = {"available": False}
        return out

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
        # TICKET-118: a /tune POST that flips prefer_audible_session (or the
        # threshold) must propagate to MediaWatcher so the next _pick honors
        # it without an app restart.
        if key in ("prefer_audible_session", "prefer_audible_threshold"):
            try:
                self.media.set_audible_pref(
                    int(self._tune.get("prefer_audible_session", 1) or 0),
                    float(self._tune.get("prefer_audible_threshold", 0.005)),
                )
            except Exception:
                pass
        # M2: a /tune flip of gpu_renderer_on starts or stops the GL child
        # immediately. The Tk overlay window is hidden while the child is
        # alive so the user sees only the GPU-driven render.
        if key == "gpu_renderer_on":
            try:
                self._apply_gpu_renderer_toggle(bool(new))
            except Exception as e:
                log.info("gpu_renderer toggle failed: %s", e)
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
                        "gpu_renderer": self.gpu_renderer_on,
                        # NB: key MUST match the __init__ read s.get("tauri_overlay_on")
                        # or the choice silently never persists.
                        "tauri_overlay_on": getattr(self, "tauri_overlay_on", False),
                        "discord_rpc": self.discord_rpc_on,
                        "window_titles": self.window_titles_on,
                        "window_titles_generic_browsers":
                            self.window_titles_generic_browsers_on,
                        # TICKET-117: SMTC session pin (Tab-A-muted / Tab-B-lyrics).
                        # Two strings: the composite id (or '' for Auto) and the
                        # source_app captured at pin time (auto-migrate guard).
                        "pinned_session_id": self.pinned_session_id,
                        "pinned_session_app": self.pinned_session_app,
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

    def set_pinned_session(self, session_id, source_app=""):
        """TICKET-117: pin the lyric source to ONE specific SMTC session, or
        clear the pin (pass '' / None) to return to Auto. Persisted across
        restarts via _persist().

        User scenario: two browser tabs, one MUTED visual + one with the actual
        music — Auto ping-pongs because SMTC reports both as 'playing'. Pinning
        the music tab makes the overlay ignore the visual tab entirely.

        Also dropped into the MediaWatcher under its own lock so the SMTC poll
        thread sees the new pin on the NEXT tick (no race with the Tk thread).
        """
        new_id = (session_id or "").strip()
        new_app = (source_app or "").strip().lower()
        # Capture source_app from current session list if caller didn't pass one
        # — the menu builder always does, but /tune POSTs may not.
        if new_id and not new_app:
            try:
                for s in self.media.list_sessions():
                    if s.get("id") == new_id:
                        new_app = (s.get("source") or "").lower()
                        break
            except Exception:
                pass
        self.pinned_session_id = new_id
        self.pinned_session_app = new_app
        try:
            self.media.set_pinned(new_id, new_app)
        except Exception:
            pass
        # Reset grace-window state so a fresh pin gets a full grace period and
        # the cold-start auto-clear can't fire against a just-installed pin.
        self._pinned_last_seen_t = time.time()
        self._pinned_cold_start = False
        try:
            log.info("pinned session: id=%s app=%s", new_id or "(auto)", new_app or "-")
        except Exception:
            pass
        self._persist()
        # Refresh the tray menu so the radio dot moves to the new selection.
        try:
            if self._icon is not None:
                self._icon.update_menu()
        except Exception:
            pass

    def _pinned_grace_s(self):
        return float(self._tune.get("pinned_grace_s", 30.0))

    def _pinned_auto_migrate_check(self):
        """TICKET-117: while the pinned id is missing, look for a SINGLE same-
        source_app session — if exactly one is present AND no other session
        shares the pinned app, migrate the pin to that new id (the common case
        is a YouTube autoplay advancing to the next song; the title hash
        changes but it's the same tab). Returns True if a migration happened.

        The 'only one same-app session present' guard is the safety net for the
        user's two-Brave-tabs scenario: if both Tab A and Tab B are still
        Brave and Tab B's title changes, we can't disambiguate from app alone,
        so we DON'T migrate — we hold and let the grace timer eventually
        revert to Auto, prompting the user to re-pin."""
        if not (self.pinned_session_id and self.pinned_session_app):
            return False
        if not int(self._tune.get("pinned_auto_migrate_same_app", 1) or 0):
            return False
        try:
            sessions = self.media.list_sessions()
        except Exception:
            return False
        same_app = [s for s in sessions
                    if (s.get("source") or "").lower() == self.pinned_session_app
                    and s.get("id") != self.pinned_session_id]
        if len(same_app) != 1:
            return False
        new_id = same_app[0].get("id") or ""
        if not new_id:
            return False
        log.info("pin auto-migrate (%s sole survivor): %s → %s",
                 self.pinned_session_app, self.pinned_session_id, new_id)
        self.set_pinned_session(new_id, self.pinned_session_app)
        try:
            if self._icon is not None:
                self._icon.notify(
                    f"Pin followed: {same_app[0].get('title') or '(no title)'}",
                    "Lyric Immersion and Karaoke")
        except Exception:
            pass
        return True

    def _pinned_tick(self):
        """TICKET-117: per-tick pin-health check. Tracks _pinned_last_seen_t;
        when the pinned session is missing for longer than pinned_grace_s, try
        the same-app auto-migrate first, otherwise clear the pin and notify.

        Cold-start branch: on restart, if the pin's session is not in the FIRST
        non-empty enumeration (the tab was closed before restart), silently
        clear the pin without burning the full grace window — the user never
        meant 'wait 30s before resuming Auto' on a process restart."""
        if not self.pinned_session_id:
            return
        try:
            sessions = self.media.list_sessions()
        except Exception:
            return
        ids = {s.get("id") for s in sessions}
        now = time.time()
        if self.pinned_session_id in ids:
            self._pinned_last_seen_t = now
            self._pinned_cold_start = False
            return
        # Cold-start grace: 5s from boot to let the watcher's first poll land.
        if self._pinned_cold_start:
            if not sessions and (now - self._pinned_cold_start_t) < 5.0:
                return
            if sessions:
                log.info("pin cold-start: %s not present at boot → reverting to Auto",
                         self.pinned_session_id)
                self.set_pinned_session("", "")
                try:
                    if self._icon is not None:
                        self._icon.update_menu()
                except Exception:
                    pass
                return
        # Live grace: attempt auto-migrate, then time out to Auto.
        if (now - self._pinned_last_seen_t) < self._pinned_grace_s():
            return
        if self._pinned_auto_migrate_check():
            return
        old_id = self.pinned_session_id
        self.set_pinned_session("", "")
        log.info("pin grace expired (%s vanished) → Auto", old_id)
        try:
            if self._icon is not None:
                self._icon.notify("Pinned source ended — back to Auto",
                                  "Lyric Immersion and Karaoke")
                self._icon.update_menu()
        except Exception:
            pass

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
            # TICKET-112: a new URL may also mean a new video id — re-kick
            # the YT-description extractor so the disambiguator credits land
            # for THIS video. Cheap fast-path: skip when the video id hasn't
            # actually changed (same URL, different fragment / timestamp).
            try:
                from yt_description import _video_id as _ytd_vid
                new_vid = _ytd_vid(url)
            except Exception:
                new_vid = None
            if (new_vid and new_vid != self._yt_metadata_video_id
                    and self._track and not self._live_mode
                    and int(self._tune.get("yt_description_lookup", 1) or 0)):
                self.root.after(300,
                                lambda t=self._track_seq: self._maybe_fetch_yt_description(t))

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
        # M2: notify the GPU renderer child (if running) that a new song is
        # loaded. Hooked here because _relayout_song() is the canonical
        # "lyrics are now ready to render" entry point — load(), captions,
        # generation, OCR, /forcesync all flow through here.
        try:
            self._gpu_send_song()
        except Exception:
            pass
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

    # ─────────────────────── TICKET-113 helpers ───────────────────
    def _blacklist_current_lyrics(self, reason: str) -> None:
        """Add the currently-loaded lyric body to self._lyrics_blacklist BEFORE
        any caller unlinks the file / clears self.lines. Called from
        report_wrong, _fire_decision_action SWITCH, and _fire_decision_action
        REGEN — three sites, one helper, so the "capture before destroy"
        ordering is enforced in one place rather than three.

        Reads the on-disk LRC body via self._lyrics_path when available, else
        falls back to reconstructing a plain-text body from self.lines (used
        when the file was already unlinked or never written to disk). Both
        paths route through fetch_lyrics._lrc_signature so they hash identically
        to the rejection site."""
        try:
            from fetch_lyrics import _lrc_signature
        except Exception:
            return
        body = ""
        src = (self.meta or {}).get("source", "unknown")
        try:
            if self._lyrics_path and Path(self._lyrics_path).exists():
                # The cached JSON has parsed `lines` (jp/rm/en) not the raw LRC;
                # rebuild a plain-text body from `lines[*].jp` so the signature
                # matches what fetch_lrc's take() would have hashed for the same
                # content (timestamps stripped, whitespace collapsed).
                data = json.loads(Path(self._lyrics_path).read_text("utf-8"))
                body = "\n".join((ln.get("jp") or "") for ln in data.get("lines", []))
                src = (data.get("meta") or {}).get("source", src)
        except Exception:
            body = ""
        if not body and self.lines:
            body = "\n".join((getattr(ln, "jp", "") or "") for ln in self.lines)
        if not body:
            log.info("blacklist: skipped (no body to hash) reason=%s", reason)
            return
        sig = _lrc_signature(body)
        if not sig:
            return
        entry = (src, sig)
        if entry in self._lyrics_blacklist:
            log.info("blacklist: already present source=%s sig=%s reason=%s",
                     src, sig[:10], reason)
            return
        # FIFO cap (per design risks): keep the set bounded so a REGEN-storm
        # with the instrumental-gap-timer bug can't blow up memory or starve
        # every provider in a few cycles.
        cap = int(self._tune.get("lyrics_blacklist_max", 8))
        if len(self._lyrics_blacklist) >= cap:
            # set is unordered — drop an arbitrary entry to make room
            self._lyrics_blacklist.pop()
        self._lyrics_blacklist.add(entry)
        log.info("blacklist: added source=%s sig=%s reason=%s (now %d entries)",
                 src, sig[:10], reason, len(self._lyrics_blacklist))

    def _rotate_provider_order(self) -> None:
        """Move provider index 0 to the end. Called on /wrong so the next
        re-fetch reaches for a DIFFERENT provider first — demote-only doesn't
        help because the wrong provider's hit was 'valid' from its POV; we
        need a different one entirely."""
        if len(self._provider_order) >= 2:
            self._provider_order = self._provider_order[1:] + [self._provider_order[0]]
            log.info("provider rotation: %s", self._provider_order)

    def _bump_wrong_streak(self) -> bool:
        """Increment the /wrong streak (with a 60s soft expiry window) and
        return True iff the streak crossed the AI-gen-force threshold."""
        now = time.time()
        self._m(self.metrics.note_resync, "report_wrong")        # TICKET-121 (every /wrong press)
        window = float(self._tune.get("wrong_streak_window_s", 60.0))
        if now - self._wrong_streak_t < window:
            self._wrong_streak += 1
        else:
            self._wrong_streak = 1
        self._wrong_streak_t = now
        thr = int(self._tune.get("wrong_streak_force_ai_gen_threshold", 2))
        return self._wrong_streak >= thr

    def report_wrong(self):
        """User-driven correction: bin the wrong lyrics and identify by SOUND.
        For covers, also re-fetch using the original artist from the title."""
        # TICKET-112: if a YT-description fetch is in flight, wait briefly
        # for it to land — the user's exact failure ("re-fetch returned the
        # same wrong 'Shooting Star' because the query was unchanged") only
        # gets fixed when the new disambiguator signal is in
        # self._yt_metadata BEFORE _start_fetch reads it. Bounded short
        # wait so a hung yt-dlp can never block the user's correction.
        if (getattr(self, "_yt_metadata_fetching", False)
                and not getattr(self, "_yt_metadata", None)):
            try:
                wait_until = time.time() + 2.0  # at most 2s — design risk note
                while (self._yt_metadata_fetching
                       and not self._yt_metadata
                       and time.time() < wait_until):
                    time.sleep(0.1)
            except Exception:
                pass
        # TICKET-112: ground-truth override — when the video description names
        # an explicit Original / カバー元 artist, that beats whatever the title
        # regex inferred earlier. Logged on disagreement (silent overrides
        # historically masked real bugs in title parsing).
        ytm = getattr(self, "_yt_metadata", None) or {}
        yt_orig = (ytm.get("original_artist") or [None])[0]
        if yt_orig:
            if (self._cover_original_artist
                    and self._cover_original_artist != yt_orig):
                log.info("yt_description: original_artist %r overrides title-parsed %r",
                         yt_orig, self._cover_original_artist)
            self._cover_original_artist = yt_orig
            # Description-confirmed original → treat as a cover even when
            # title parsing didn't flag it (cover-without-marker case).
            self._is_cover = True
        # TICKET-113: capture the current lyric signature BEFORE the unlink
        # below destroys the file. Also rotate providers + bump the /wrong
        # streak — if the user hits /wrong twice within 60s, every network
        # provider has been wrong and we short-circuit to AI-gen (which uses
        # the audio stream, not titles, so it doesn't share the collision
        # failure mode).
        self._blacklist_current_lyrics(reason="report_wrong")
        self._rotate_provider_order()
        force_ai = self._bump_wrong_streak()
        if force_ai:
            log.info("wrong-streak hit AI-gen threshold (%d in %.0fs window) → forcing AI-gen",
                     self._wrong_streak, float(self._tune.get("wrong_streak_window_s", 60.0)))
            self._force_ai_gen = True
        if self._lyrics_path:
            try:
                Path(self._lyrics_path).unlink(missing_ok=True)
            except Exception:
                pass
            self.index.refresh()
        self._fetch_key = None
        # TICKET-111: defer the visible clear so the current line / belt finishes
        # before the new lyrics drop in. User-driven path uses a TIGHTER safety
        # cap (swap_defer_user_max_s, default 3.0s) when the user explicitly
        # says "wrong", they want it fixed fast, even at the cost of some snap.
        deferred = (int(self._tune.get("swap_defer_enabled", 1) or 0) == 1
                    and bool(self.lines))
        if deferred:
            if self._is_cover and self._cover_original_artist and self._track:
                _, t = self._track
                hint = f"🔄 Re-fetching as {self._cover_original_artist} — {t}…"
                q_artist, q_title, q_cover = self._cover_original_artist, t, True
            else:
                q_artist, q_title, q_cover = "", "", False
                hint = "🎧 Listening to identify the song…"
            self._queue_swap(
                kind="wrong-user", source_site="G",
                artist=q_artist, title=q_title, cover=q_cover,
                hint=hint, set_gate=False,
                max_s=float(self._tune.get("swap_defer_user_max_s", 3.0)))
            self._sound_song = None
            self._title_locked = False
            # Kick off the actual identification / re-fetch NOW so the boundary
            # has a real target by the time it hits.
            if self._is_cover and self._cover_original_artist and self._track:
                _, title = self._track
                log.info("wrong-song on cover: re-fetching %r by original artist %r (deferred swap)",
                         title, self._cover_original_artist)
                self._start_fetch(self._cover_original_artist, title,
                                  self._cur_duration, cover=True,
                                  swap_token=self._pending_swap["fetch_token"])
            self._start_identify()
            return
        # legacy immediate path
        self._lyrics_path = None
        self.lines, self.idx, self._kara = [], -1, []
        self._sound_song = None
        self._title_locked = False     # let sound override after a manual reject
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

    def _escalate_to_captions(self):
        """Wrong lyrics detected → re-attempt THIS video's caption track (its own
        accurate, timing-locked text) before settling for a provider re-fetch or AI
        generation. Re-arms the once-per-song guard and kicks an async fetch;
        _apply_captions makes captions win over whatever is showing. No-op when
        captions are off, in live_mode, already on captions, or no video is known."""
        try:
            if (not self.captions_on or self._live_mode
                    or (self.meta.get("source") or "") == "youtube-captions"):
                return
            if not (getattr(self, "_now_url", None) or getattr(self, "_last_raw_title", None)):
                return
            self._caption_song = None        # re-arm the per-song caption fetch
            self.root.after(0, lambda: self.load_youtube_captions(silent=True))
        except Exception:
            pass

    def _maybe_escalate_ocr(self):
        """Wrong lyrics detected AND the correct ones aren't fetchable (a niche song
        whose search returns a wrong same-artist hit — Play On! → Sixth Sense) → read
        the burned-in lyrics OFF the video. Once per track, browser source only. This
        is the wrong-lyrics counterpart to the no-lyrics OCR trigger in _maybe_generate;
        OCR commits source='ocr' (body-trusted) and supersedes the wrong body."""
        try:
            # NO generate_on gate here. OCR escalation for WRONG lyrics (decision-
            # engine SWITCH/REGEN) is independent of the AI-generation toggle —
            # reading burned-in lyrics off one frame is a screen-read, not audio
            # encoding. (generate_on still gates ONLY the no-lyrics deadline
            # fallback in _maybe_generate.) Per-track + _ocr_gpu_safe guards keep
            # this from spamming or hitching a game. Fixes Play On! → burned-in OCR.
            if (not self._live_mode
                    and any(h in (self._last_src or "") for h in BROWSER_HINTS)
                    and self._track_seq != self._ocr_harvest_seq):
                self._ocr_harvest_seq = self._track_seq
                self._begin_ocr_harvest(self._track_seq)
        except Exception:
            pass

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
        # JP-vagency language guard: a known JP act (ReGLOSS / hololive / Hajime /
        # Suisei / Kanade …) doesn't release Korean/Chinese/European-language
        # songs, so a ko/zh/es/de/ru/fr/it/pt captions track is a fan-translation
        # upload — applying it shows wrong-language lyrics over Japanese vocals
        # (the flashpoint.json Korean case + the 怪獣の花唄 Spanish case). Same
        # rejection set as fetch_lyrics.take()'s European-language guard. Single
        # source of truth is fetch_lyrics._JP_VAGENCY_RE (built from
        # confidence._KNOWN_JA).
        from fetch_lyrics import is_jp_vagency, slugify
        _BAD_CAPTIONS_LANGS = ("ko", "zh", "es", "de", "ru", "fr", "it", "pt")
        # ZH bodies are STRICT-checked (kanji-only doesn't count as JP signal)
        # so a legit Chinese-captions track for a kanji-titled Chinese song
        # (e.g. 孤勇者 / 陈奕迅) isn't wrongly rejected.
        if lang in _BAD_CAPTIONS_LANGS and is_jp_vagency(title, artist, strict=(lang == "zh")):
            log.info("captions: rejected %s track for JP-act %r / %r — wrong-language collision",
                     lang, title, artist)
            return
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
        """SMTC vs Shazam title/artist agreement. Cover-aware (skip on covers).

        A feat./remix/parenthetical VARIANT of the SAME song with an AGREEING
        artist is NOT a disagreement. The old code returned DEGRADED whenever one
        title was a substring of the other ("Tokyo Friday Night" ⊂ "Tokyo Friday
        Night (feat. Kana Hanazawa & Mori Calliope)"), which drove a false
        TRUST→CAUTION→SWITCH that BLACKLISTED the correct lyrics — the user's
        "lost lyrics completely then got them back". Closed captions are the
        video's own lyrics → ground truth, never a source mismatch."""
        if getattr(self, "_is_cover", False):
            return "OK"
        src = (self.meta.get("source") or "") if isinstance(self.meta, dict) else ""
        if src == "youtube-captions":
            return "OK"
        st = self.media.get() or {}
        raw_smtc_t = st.get("title") or ""
        smtc_t = _norm_title(raw_smtc_t)
        smtc_a = _norm_title(st.get("artist") or "")
        snd = getattr(self, "_sound_song", None)
        if not snd or not smtc_t:
            return "OK"
        s_title  = _norm_title(snd[0] or "")
        s_artist = _norm_title(snd[1] or "")
        if s_title == smtc_t and s_artist == smtc_a:
            return "OK"
        artist_ok = bool(smtc_a and s_artist and
                         (s_artist == smtc_a or s_artist in smtc_a or smtc_a in s_artist))
        # Same song minus a trailing (feat. …)/(Live)/[Remix] variant tag + artist agrees?
        bare_smtc  = _norm_title(_strip_title_credits(raw_smtc_t))
        bare_sound = _norm_title(_strip_title_credits(snd[0] or ""))
        if bare_smtc and bare_sound and bare_smtc == bare_sound and artist_ok:
            return "OK"
        if s_title and (s_title in smtc_t or smtc_t in s_title):
            return "OK" if artist_ok else "DEGRADED"   # title overlap: trust iff artist agrees
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
        # kamone fix: a by-ear run on an UNCORROBORATED title-locked body is a body PROBE,
        # not steady-state corroboration. Don't let one noisy probe transcript escalate
        # decision-engine strikes toward SWITCH/REGEN (that path blacklists+unlinks the LRC
        # with no bundled exemption and could thrash a CORRECT periodic body on a single
        # Whisper hallucination). Recovery for a genuinely wrong body happens inside
        # _decide_by_ear's own switch/refetch (MIN+titlelock-bump gated). Once the body is
        # corroborated this guard lifts and ear_corrob resumes normal behavior.
        if (getattr(self, "_verified", False) and getattr(self, "_title_locked", False)
                and not getattr(self, "_body_corroborated", False)):
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
        # Covers are collision-prone: a title-only search (the original artist isn't in
        # a '歌ってみた / covered by' title) lands on a wrong same-title song or a stub
        # generation. The user wants a bad COVER abandoned QUICKER, so trim the
        # escalation thresholds when the current track is an explicit cover. Non-covers
        # keep the conservative thresholds (cov=0). Floors keep it from over-firing.
        cov = 1 if getattr(self, "_is_cover", False) else 0
        regen   = max(4, int(self._tune.get("decision_regen_strikes",   8)) - 3 * cov)
        switch  = max(3, int(self._tune.get("decision_switch_strikes",  5)) - 2 * cov)
        caution = max(2, int(self._tune.get("decision_caution_strikes", 3)) - 1 * cov)
        if s >= regen:   return "REGEN"
        if s >= switch:  return "SWITCH"
        if s >= caution: return "CAUTION"
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

    # ── success/failure telemetry helpers ───────────────────────────────
    def _stats_bump(self, key, n=1):
        try:
            self._stats[key] = self._stats.get(key, 0) + n
        except Exception:
            pass

    def _stats_fetch_done(self, secs):
        try:
            self._fetch_durations.append(round(float(secs), 2))
            if len(self._fetch_durations) > 200:
                del self._fetch_durations[:len(self._fetch_durations) - 200]
        except Exception:
            pass

    def success_rate_snapshot(self):
        """ID-match %, title-hit %, fetch P50/P95, by-ear %, sync-in-window %,
        REGEN/SWITCH counts — measured against the perceptual + reliability
        targets so the success:failure ratio is a live /diag readout."""
        s = dict(getattr(self, "_stats", {}) or {})
        ts = dict(_TITLE_STATS)

        def pct(a, b):
            t = a + b
            return round(100.0 * a / t, 1) if t else None
        fd = sorted(getattr(self, "_fetch_durations", []) or [])

        def pctile(p):
            return fd[min(len(fd) - 1, int(p * (len(fd) - 1) + 0.5))] if fd else None
        loads = s.get("track_loads", 0)
        synt = s.get("sync_reads", 0)
        return {
            "id_match_pct":       pct(s.get("id_match", 0), s.get("id_mismatch", 0)),
            "id_checks":          s.get("id_match", 0) + s.get("id_mismatch", 0),
            "title_hit_pct":      pct(ts.get("hit", 0), ts.get("miss", 0)),
            "title_lookups":      ts.get("hit", 0) + ts.get("miss", 0),
            "by_ear_pct":         (round(100.0 * s.get("by_ear", 0) / loads, 1) if loads else None),
            "track_loads":        loads,
            "sync_in_window_pct": (round(100.0 * s.get("sync_in_window", 0) / synt, 1) if synt else None),
            "sync_reads":         synt,
            "fetch_p50_s":        pctile(0.50),
            "fetch_p95_s":        pctile(0.95),
            "fetch_timeouts":     s.get("fetch_timeout", 0),
            "regen":              s.get("regen", 0),
            "switch":             s.get("switch", 0),
            "llm":                self._llm_status(),
            "targets": {"id_match_pct": 90, "title_hit_pct": 70, "by_ear_pct_max": 20,
                        "sync_in_window_pct": 95, "fetch_p95_s_max": 6.0},
        }

    def _llm_status(self):
        """Whether the optional Claude disambiguator is armed (key present) + model."""
        try:
            import llm_disambiguate as _llm
            on = _llm.available()
            return {"available": on, "model": _llm.model() if on else None}
        except Exception:
            return {"available": False, "model": None}

    def get_overlay_state(self):
        """Compact render state for an EXTERNAL overlay client (the Tauri PoC):
        the CURRENT line (furigana `漢字(かな)` / romaji / translation), its song-time
        bounds, and the current display position — so the client renders the line
        and runs a STEADY LOCAL fill animation (frac = (pos − start)/(end − start),
        interpolated client-side between polls). Only a LINE CHANGE re-anchors the
        fill, which is the "only the lyrics follow sync; the highlight just
        proceeds" principle realized in the renderer itself. Read-only."""
        # HEARTBEAT for the CPU-fallback watchdog: the overlay polls this every
        # ~250ms WHILE it is actually running its render loop, so a fresh request
        # proves the GPU overlay is alive AND rendering. If these stop (the
        # overlay crashed, its window vanished, or its JS froze), the watchdog
        # restores the Tk (CPU) overlay — guaranteeing a CPU fallback.
        self._overlay_ping_t = time.time()
        st = self.media.get() or {}
        playing = (st.get("status", PLAYING) == PLAYING)
        lead = float(self._tune.get("display_lead_s", 0.12))
        pos = float(st.get("position", 0.0) or 0.0) + float(self.offset) + lead
        n = len(self.lines)

        def _ln(i):
            if 0 <= i < n:
                l = self.lines[i]
                return {"jp": l.jp, "rm": l.rm, "en": l.en,
                        "start": round(l.start, 3), "end": round(l.end, 3)}
            return None
        idx = self.idx if 0 <= self.idx < n else -1
        m = self.meta if isinstance(self.meta, dict) else {}
        return {
            "playing": playing,
            "position": round(pos, 3),
            "idx": idx,
            "line_count": n,
            "line": _ln(idx),
            "next": _ln(idx + 1) if idx >= 0 else None,
            "title": m.get("title"),
            "artist": m.get("artist"),
            "source": m.get("source"),
            "ts": time.time(),
        }

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
        # GROUND-TRUTH IMMUNITY (TICKET-122) — TWO-TIER BARRIER:
        #   • Tier 1 — a hand-curated BUNDLE is authoritative UNCONDITIONALLY (we verified
        #     it; trustworthy even before hearing the audio). An instrumental OUTRO makes
        #     sync_stable go BAD (no vocals to lock onto), which used to make a CORRECT
        #     bundled song (feelingradation) start generating near the end — never again.
        #   • Tier 2 — youtube-captions / OCR are PROVISIONAL: immune ONLY after they have
        #     CORROBORATED against the audio (_body_corroborated, earned via energy lock /
        #     by-ear). A mis-fetched caption or an OCR misread must stay re-checkable until
        #     it proves it matches the singing.
        #   • Everything else (provider LRC, generated) is always re-checkable.
        _gsrc = (self.meta.get("source") or "") if isinstance(self.meta, dict) else ""
        _authoritative = (_gsrc.startswith("bundled")
                          or (_gsrc in ("youtube-captions", "ocr")
                              and getattr(self, "_body_corroborated", False)))
        # Closed captions are the video's OWN lyrics — a SWITCH (blacklist + re-fetch)
        # is futile (the same video returns the same captions) and would blacklist the
        # only ground-truth source for this concert. Suppress SWITCH on captions; a
        # genuinely-wrong CC can still be replaced by REGEN (generate-by-ear) below.
        if state == "SWITCH" and _gsrc == "youtube-captions":
            log.info("decision: SWITCH suppressed on youtube-captions (re-fetch futile) — holding")
            self._decision_strikes = max(0, self._decision_strikes - 2)
            self._decision_state = "CAUTION" if self._decision_strikes >= int(
                self._tune.get("decision_caution_strikes", 3)) else "TRUST"
            return
        if state in ("SWITCH", "REGEN") and _authoritative:
            log.info("decision: %s suppressed — %r is authoritative ground truth (holding)",
                     state, _gsrc)
            self._decision_strikes = max(0, self._decision_strikes - 3)
            self._decision_state = "CAUTION" if self._decision_strikes >= int(
                self._tune.get("decision_caution_strikes", 3)) else "TRUST"
            return
        cooldown = float(self._tune.get("decision_action_cooldown_s", 30.0))
        if now - self._decision_last_action_t < cooldown:
            return
        self._decision_last_action_t = now
        track = getattr(self, "_track", None)
        # CROSS-LANGUAGE COVER routing: a SWITCH (blacklist + re-fetch) is futile when
        # the cover is SUNG in a different language than any fetchable lyrics — the
        # re-fetch only returns the original-language body again, which can never match
        # the audio. Redirect that SWITCH to REGEN-by-ear in the cover's language. Only
        # fires once the body has already failed through CAUTION (match-original-first is
        # preserved); same-language / unspecified-language covers (_cover_lang None or ==
        # body lang) are untouched and re-fetch normally.
        _cl = getattr(self, "_cover_lang", None)
        if (state == "SWITCH" and track and _cl
                and _cl != ((self.meta.get("lang") or "") if isinstance(self.meta, dict) else "")):
            log.info("decision: cross-language cover (cover_lang=%s, body_lang=%s) → "
                     "REGEN by ear in cover language", _cl,
                     self.meta.get("lang") if isinstance(self.meta, dict) else None)
            state = "REGEN"
        # success telemetry: a decision action is firing (past the cooldown gate)
        if state == "REGEN":
            self._stats_bump("regen")
        elif state == "SWITCH":
            self._stats_bump("switch")
        if state == "SWITCH" and track:
            artist, title = track
            hint = f"🔁 Switched to alternative lyric source for {title}…"
            # TICKET-113: capture before destroy — the unlink below would leave
            # nothing for the helper to hash.
            self._blacklist_current_lyrics(reason="decision-switch")
            self._m(self.metrics.note_resync, "switch")          # TICKET-121
            # Wrong lyrics → escalate to the VIDEO'S OWN caption track first (accurate,
            # video-locked). Captions win over any LRC/generation when they arrive
            # (_apply_captions), so this runs alongside the provider re-fetch and
            # supersedes it if the video has a real caption track.
            self._escalate_to_captions()
            # …and read the burned-in video lyrics — the right words for a niche song
            # whose provider search keeps returning a wrong same-artist hit (Play On! →
            # Sixth Sense). OCR supersedes the wrong body when the video carries lyrics.
            self._maybe_escalate_ocr()
            if getattr(self, "_lyrics_path", None):
                try: Path(self._lyrics_path).unlink(missing_ok=True)
                except Exception: pass
                try: self.index.refresh()
                except Exception: pass
            self._fetch_key = None
            deferred = (int(self._tune.get("swap_defer_enabled", 1) or 0) == 1
                        and bool(self.lines))
            if deferred:
                # TICKET-111: queue the swap; keep rendering current lyrics
                # while the new fetch runs in parallel.
                self._queue_swap(
                    kind="switch", source_site="H",
                    artist=artist, title=title,
                    cover=getattr(self, "_is_cover", False),
                    hint=hint, set_gate=False)
                self._start_fetch(artist, title, self._cur_duration,
                                  cover=getattr(self, "_is_cover", False),
                                  swap_token=self._pending_swap["fetch_token"])
            else:
                self._hint(hint)
                self._lyrics_path = None
                self.lines, self.idx, self._kara = [], -1, []
                self._start_fetch(artist, title, self._cur_duration,
                                  cover=getattr(self, "_is_cover", False))
            self._decision_strikes = max(0, self._decision_strikes - 3)
        elif state == "REGEN" and track:
            hint = "✨ Regenerating lyrics via AI…"
            # TICKET-113: capture before destroy — REGEN clears self.lines
            # below in the legacy path, so the helper must run first or the
            # signature comes back from an empty body.
            self._blacklist_current_lyrics(reason="decision-regen")
            self._m(self.metrics.note_resync, "regen")           # TICKET-121
            # 'before generation' (user directive): try the video's caption track AND the
            # burned-in video lyrics (OCR) first — AI generation is the genuine last
            # resort. Both supersede the regen if the video carries the words.
            self._escalate_to_captions()
            self._maybe_escalate_ocr()
            self._fetch_key = None
            deferred = (int(self._tune.get("swap_defer_enabled", 1) or 0) == 1
                        and bool(self.lines))
            if deferred:
                # TICKET-111: queue the regen swap. The AI-gen consumer (Site C
                # in _begin_generation / _apply_generated) checks this flag and
                # writes into pending['lines'] instead of self.lines.
                artist, title = track
                self._queue_swap(
                    kind="regen", source_site="I",
                    artist=artist, title=title, cover=False,
                    hint=hint, set_gate=False, force_ai_gen=True)
                self._force_ai_gen = True
            else:
                self._hint(hint)
                self._lyrics_path = None
                self.lines, self.idx, self._kara = [], -1, []
                self._force_ai_gen = True
            self._decision_strikes = 0

    # ─────────────────────── TICKET-111 deferred swap helpers ───────────────────
    def _queue_swap(self, kind, source_site, artist, title, cover=False,
                    hint="", set_gate=False, force_ai_gen=False, max_s=None):
        """Queue a deferred whole-lyrics swap. Caller is responsible for kicking
        off the fetch / gen with swap_token=self._pending_swap['fetch_token'].
        If a swap is already pending, this REPLACES it (supersedes) the old
        fetch's completion will fail the token check and be dropped."""
        if self._pending_swap is not None:
            old = self._pending_swap
            log.info("swap: superseded prior kind=%s site=%s token=%d age=%.2fs",
                     old.get("kind"), old.get("source_site"),
                     old.get("fetch_token"),
                     time.time() - self._pending_swap_t)
        self._swap_fetch_token += 1
        token = self._swap_fetch_token
        cap = float(max_s) if max_s is not None else float(
            self._tune.get("swap_defer_max_s", 8.0))
        self._pending_swap = {
            "kind": kind, "source_site": source_site,
            "artist": artist, "title": title, "cover": cover,
            "queued_t": time.time(),
            "hint": hint,
            "fetch_token": token,
            "lines": None, "meta": None, "lyrics_path": None,
            "force_ai_gen": bool(force_ai_gen),
            "set_gate": bool(set_gate),
            "max_s": cap,
            "cancelled": False,
            "gen_token": None,
        }
        self._pending_swap_t = time.time()
        if hint:
            try:
                self._hint(hint)
            except Exception:
                pass
        log.info("swap: queued kind=%s site=%s token=%d cap=%.1fs%s",
                 kind, source_site, token, cap,
                 " force_ai_gen" if force_ai_gen else "")
        return token

    def _cancel_pending_swap(self, reason):
        """Drop the pending swap (e.g. real track change, user cancel)."""
        if self._pending_swap is None:
            return
        p = self._pending_swap
        log.info("swap: cancelled kind=%s site=%s token=%d reason=%s age=%.2fs",
                 p.get("kind"), p.get("source_site"), p.get("fetch_token"),
                 reason, time.time() - self._pending_swap_t)
        self._pending_swap = None
        self._pending_swap_t = 0.0

    def _swap_ready(self):
        """Returns (ready: bool, reason: str). Reason surfaces in /diag.blocked_by.
        Boundary depends on render mode:
          LINE  (none/left/right/top/bottom) current line end OR instrumental gap
          SCROLL (lr/rl/tb/bt)              belt drained OR instrumental gap
        """
        if not self.lines:
            return True, "no-current-lines"
        mode = self.scroll_dir
        now = time.time()
        gap_thr = float(self._tune.get("swap_defer_instrumental_gap_s", 2.0))
        # TICKET-114: cosmetic clamp for the diag string only. The readiness
        # math (`>= gap_thr`) keeps raw wall-time, so a real 2s instrumental
        # gap still trips the boundary correctly. Without this clamp, a stale
        # anchor (e.g. from boot-time before TICKET-114 reset landed, or from
        # any future regression) can print absurd values like
        # "instrumental-gap(204.2s)" on a song only 11.7s into 161s of runtime.
        last_pos = getattr(self, "_last_pos", 0.0) or 0.0
        if mode in ("lr", "rl", "tb", "bt"):
            stream_n = len(getattr(self, "_stream", []) or [])
            if stream_n == 0:
                return True, "belt-drained"
            if (self.idx == -1 and self._idx_minus_one_since
                    and (now - self._idx_minus_one_since) >= gap_thr):
                gap_s = min(now - self._idx_minus_one_since, last_pos)
                return True, f"instrumental-gap({gap_s:.1f}s)"
            return False, f"belt={stream_n} idx={self.idx}"
        # LINE modes
        if getattr(self, "_anim_id", None) is not None:
            return False, "anim-in-progress"
        if self.idx == -1:
            if (self._idx_minus_one_since
                    and (now - self._idx_minus_one_since) >= gap_thr):
                gap_s = min(now - self._idx_minus_one_since, last_pos)
                return True, f"instrumental-gap({gap_s:.1f}s)"
            wait = (gap_thr - (now - self._idx_minus_one_since)
                    if self._idx_minus_one_since else gap_thr)
            wait = max(0.0, min(wait, last_pos)) if last_pos else max(0.0, wait)
            return False, f"gap-too-short(wait {wait:.1f}s)"
        # In-line must wait for line end. Fast path: post-last-line.
        try:
            ln = self.lines[self.idx]
        except Exception:
            return True, "idx-out-of-range"
        # last_pos hoisted to top of fn (TICKET-114 clamp); reuse here.
        ends_in = ln.end - last_pos
        if self.idx >= len(self.lines) - 1 and ends_in <= 0.0:
            return True, "post-last-line"
        return False, f"in-line idx={self.idx} ends-in={ends_in:.1f}s"

    def _try_apply_swap(self):
        """Called every _tick. If a pending swap has its target loaded AND the
        per-mode boundary is reached (or the safety cap expired), commit it
        atomically via _apply_pending_swap."""
        p = self._pending_swap
        if p is None:
            return
        # Kill-switch flipped off while a swap is in flight flush now if
        # there's a target, or cancel if not.
        if int(self._tune.get("swap_defer_enabled", 1) or 0) != 1:
            if p.get("lines") is not None:
                self._apply_pending_swap("disabled")
            else:
                self._cancel_pending_swap("disabled-no-target")
            return
        age = time.time() - self._pending_swap_t
        cap = float(p.get("max_s") or self._tune.get("swap_defer_max_s", 8.0))
        if p.get("lines") is None:
            # Fetch / gen still running. HARD CAP (TICKET-136): a correction/switch
            # fetch that never lands must not hang forever — observed 24-54s with
            # the WRONG/stale body frozen on screen while this logged EVERY tick
            # (1179×). Cap the wait, log only ONCE; if it was REPLACING wrong lyrics
            # (a REGEN), fall back to generate-by-ear so the screen isn't stuck on
            # the wrong song. The cap (default 30s) stays above the legit slow-fetch
            # window (niche/VTuber lookups can take 25-35s and still win).
            hard = float(self._tune.get("swap_fetch_hard_cap_s", 30.0))
            if age > hard:
                regen = bool(p.get("force_ai_gen"))
                log.info("swap: fetch TIMED OUT after %.1fs (hard cap %.1fs) token=%d kind=%s → abandon%s",
                         age, hard, p.get("fetch_token"), p.get("kind"),
                         " + by-ear" if regen else "")
                self._cancel_pending_swap("fetch-timeout")
                self._stats_bump("fetch_timeout")
                if regen and getattr(self, "generate_on", True):
                    self._lyrics_path = None
                    self.lines, self.idx, self._kara = [], -1, []
                    self._force_ai_gen = True
                    try:
                        self.root.after(0, lambda t=self._track_seq: self._maybe_generate(t))
                    except Exception:
                        pass
            elif age > 2 * cap and not p.get("_pending_logged"):
                p["_pending_logged"] = True      # once, not every tick (kills the spam)
                log.info("swap: fetch still pending after %.1fs (cap=%.1fs) token=%d",
                         age, cap, p.get("fetch_token"))
            return
        ready, reason = self._swap_ready()
        if not ready and age > cap:
            ready, reason = True, f"timeout({age:.1f}s)"
        if ready:
            self._apply_pending_swap(reason)

    def _apply_pending_swap(self, reason):
        """Atomic commit: in one tick, replace self.lines/meta/_lyrics_path
        with the pending target. No observable intermediate state."""
        p = self._pending_swap
        if p is None:
            return
        age = time.time() - self._pending_swap_t
        self._stats_fetch_done(age)              # success telemetry: swap fetch latency
        kind = p.get("kind"); site = p.get("source_site")
        token = p.get("fetch_token")
        # 1. Cancel any in-flight LINE-mode slide-in so it can't snap the new line.
        if getattr(self, "_anim_id", None) is not None:
            try:
                self.root.after_cancel(self._anim_id)
            except Exception:
                pass
            self._anim_id = None
        # 2. Clear scroll belt (cv.deletes per-block tags + resets list).
        try:
            self._clear_stream()
        except Exception:
            pass
        # 3. Wipe LINE-mode canvas. (For scroll mode _clear_stream already did it.)
        try:
            self.cv.delete("all")
        except Exception:
            pass
        # 4. Apply atomically.
        new_meta = p.get("meta") or {}
        new_lines = p.get("lines") or []
        new_path  = p.get("lyrics_path")
        self.meta = new_meta
        self.lines = new_lines
        self._lyrics_path = new_path
        self.idx = -1
        self._kara = []
        self._idx_minus_one_since = 0.0
        # 5. Drop the verified gate ONLY if this swap was the one that set it.
        if p.get("set_gate"):
            self._verified_gate_t = 0.0
        # 6. PERF-102 invalidation, mirroring load()'s post-load housekeeping.
        try:
            self._block_cache.clear()
            self._prewarm_token += 1
            self._block_cache_max = max(32, min(72, len(self.lines) + 2))
        except Exception:
            pass
        try:
            self._relayout_song()
        except Exception:
            pass
        # 7. Cancel a sibling _pending_offset write line timings just changed
        # under it, so its target is meaningless against the new lines.
        if self._pending_offset is not None:
            self._pending_offset = None
        # 8. Clear pending state BEFORE the log so /diag reflects committed.
        self._pending_swap = None
        self._pending_swap_t = 0.0
        self._swap_commit_seq += 1
        log.info("swap: committed kind=%s site=%s token=%s reason=%s "
                 "age=%.2fs lines=%d seq=%d",
                 kind, site, token, reason, age, len(self.lines),
                 self._swap_commit_seq)

    def _diag_pending_swap(self):
        p = self._pending_swap
        if p is None:
            return {
                "queued": False,
                "last_commit_seq": self._swap_commit_seq,
            }
        try:
            ready, blocked_by = self._swap_ready()
        except Exception as e:
            ready, blocked_by = False, f"ready-check-error: {e}"
        age = time.time() - self._pending_swap_t
        cap = float(p.get("max_s") or self._tune.get("swap_defer_max_s", 8.0))
        return {
            "queued": True,
            "kind": p.get("kind"),
            "source_site": p.get("source_site"),
            "artist": p.get("artist"),
            "title": p.get("title"),
            "queued_age_s": round(age, 2),
            "fetch_ready": bool(p.get("lines") is not None),
            "ready_for_swap": bool(ready),
            "blocked_by": blocked_by,
            "force_ai_gen": bool(p.get("force_ai_gen")),
            "fetch_token": p.get("fetch_token"),
            "will_force_commit_in_s": round(max(0.0, cap - age), 2),
            "set_gate": bool(p.get("set_gate")),
            "last_commit_seq": self._swap_commit_seq,
        }
    # ───────────────────── end TICKET-111 deferred swap helpers ─────────────────

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
        # A verified TITLE is not a verified BODY. Grant by-ear immunity only once the
        # BODY has actually been corroborated (clean energy lock or a prior healthy by-ear
        # pass). Title-locked-but-body-unconfirmed (kamone: right title, wrong body, blind
        # energy, Shazam silent) stays checkable so the wrong body is caught and re-fetched.
        # The title-lock MIN/MARGIN bump (below), short-transcript guard, and artist
        # penalty keep a noisy transcript from switching a CORRECT body away (Suisei 綺麗事).
        if (self._verified and self._title_locked and self._body_corroborated and not forced
                and not int(self._tune.get("decide_after_verified", 0))):
            log.info("decide-by-ear (%s): SKIPPED — verified + title-locked + body-corroborated", reason)
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
                    # OPTIONAL LLM disambiguation (gated on an Anthropic API key),
                    # on the HARD cases only — the loaded song looks wrong, the top
                    # fuzzy scores are close, or we expanded to the whole library.
                    # Claude matches a short/noisy transcript to the right lyrics far
                    # better than char-fuzzy; it is the lever for the wrong-song +
                    # title-miss failures. No key → no-op, rapidfuzz stands.
                    llm = None
                    try:
                        import llm_disambiguate as _llm
                        top = ranked[:6]
                        ambiguous = len(top) >= 2 and abs(top[0][0] - top[1][0]) < 8.0
                        if _llm.available() and top and (
                                loaded_score < wrong_floor or ambiguous or expanded):
                            bodymap = dict(pool)
                            if expanded:
                                bodymap.update(dict(libpool))
                            cands = [{
                                "key": k,
                                "title": (self.meta.get("title") or "?") if k == loaded_key
                                         else Path(k).stem,
                                "artist": self.meta.get("artist") or "?",
                                "body": bodymap.get(k, ""),
                            } for _, k in top]
                            llm = _llm.pick_best_match(heard, cands)
                            if llm:
                                _bk = llm.get("key")
                                log.info("decide-by-ear: LLM → best=%s conf=%.2f match=%s (%s)",
                                         (Path(_bk).name if _bk and _bk != loaded_key else _bk),
                                         llm.get("confidence", 0.0), llm.get("matches_audio"),
                                         (llm.get("reason") or "")[:80])
                    except Exception as _e:
                        log.info("decide-by-ear: LLM disambig skipped: %s", _e)
                    res = {"heard": heard, "ranked": ranked, "expanded": expanded, "llm": llm}
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
        # LLM-AUTHORITATIVE decision (gated on an API key): when the optional
        # disambiguator is confident the vocals ARE a specific candidate, trust it
        # over the fuzzy scores — it read the actual transcript + lyric bodies.
        # Same file-validity safety as the score path; if it confirms the LOADED
        # song, that corroborates the body. No key → res["llm"] is None, skipped.
        llm = res.get("llm")
        if llm and llm.get("matches_audio") and llm.get("confidence", 0.0) >= 0.7:
            lk = llm.get("key")
            if lk and lk not in ("loaded", loaded_key):
                try:
                    p = Path(lk)
                    if p.exists() and self._file_valid(p, self._cur_duration):
                        log.info("decide-by-ear: LLM-CONFIRMED switch to %s (conf %.2f) — %s",
                                 p.name, llm.get("confidence", 0.0), (llm.get("reason") or "")[:80])
                        self._fine_exit("song-switch")
                        self.load(p)
                        self._maybe_translate()
                        self._sound_title_alias = None
                        self.offset = 0.0
                        self.idx = -1
                        self._body_corroborated = True
                        self._hint("🎯 Corrected to the song being sung")
                        self.root.after(700, lambda: self._auto_align_by_energy("post-decide"))
                        return
                except Exception as e:
                    log.info("decide-by-ear: LLM switch failed: %s", e)
            elif lk in ("loaded", loaded_key):
                self._body_corroborated = True      # LLM confirms loaded IS playing
        # kamone fix: a healthy by-ear read of the LOADED body (real >=20-char transcript
        # scoring at the in-sync bar) is positive proof the body matches the singing —
        # corroborate so this verified+locked song earns by-ear immunity going forward.
        # Covers the energy-blind-but-CORRECT periodic case (Tori no Uta) after one clean
        # listen. A WRONG body (kamone, loaded_score well under decide_min_score) is NOT
        # corroborated and falls through to the switch / re-fetch paths below.
        if loaded_score >= float(self._tune.get("decide_min_score", 55.0)):
            self._body_corroborated = True
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
        # kamone fix: by-ear is scheduled exactly once per song. If this run neither
        # corroborated the body nor switched/re-fetched (e.g. vocals absent in the listen
        # window), give a body-unconfirmed title-locked song ONE more checked listen so a
        # wrong body isn't stranded until the next track. Capped via _body_probe_retried.
        if (self._verified and self._title_locked and not self._body_corroborated
                and not getattr(self, "_body_probe_retried", False)
                and track_seq == self._track_seq):
            self._body_probe_retried = True
            self.root.after(int(self._tune.get("decide_listen_s", 12.0) * 1000) + 8000,
                            lambda t=track_seq: self._decide_by_ear(t, reason="track-start"))

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
        # COVER on a mismatched LRC: the loaded lyrics are the ORIGINAL's timing,
        # which a re-sung cover never matches — correlating the cover's vocal
        # energy against that grid locks a confidently-WRONG offset (the "started
        # in sync then drifted way off" cover failure, reasons #4/#10). Don't
        # energy-align a cover; pull the video's own captions instead (THIS
        # performance's real timing). Once captions load, the guard above returns.
        if getattr(self, "_is_cover", False):
            try:
                self.root.after(0, lambda t=self._track_seq: self._maybe_fetch_captions(t))
            except Exception:
                pass
            self._sync_event("energy_align_skip", reason="cover-mismatched-lrc")
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
        # MONOTONIC drift active → re-lock on a SHORTER cooldown so the energy
        # correlator catches a steady one-directional creep (共鳴 "starts in sync
        # then gets late") instead of waiting the full ~14s. Studio-only flag.
        _cooldown = float(self._tune["auto_align_cooldown"])
        if getattr(self, "_drift_monotonic_since", 0.0):
            _cooldown = float(self._tune.get("drift_recovery_cooldown", 5.0))
        if now - self._last_align_t < _cooldown:
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

    def _ocr_assisted_sync(self, reason="energy-weak"):
        """TICKET-123 — sync 'try harder' for ambient/effect-heavy MVs where both energy
        correlation and Whisper struggle but the lyrics are BURNED INTO the video: OCR
        the on-screen line, fuzzy-match it to the loaded LRC, and set the offset so that
        line is active NOW (offset = matched_line.start − raw_player_pos). Far more
        precise than guessing from vocal energy. Browser source + loaded LRC only; safe
        to call from any non-Tk thread (OCR here, offset apply marshalled to Tk).
        Throttled to ~8s. Returns nothing; logs + stashes the result for GET /sync."""
        try:
            now = time.time()
            if now - getattr(self, "_last_ocr_sync_t", 0.0) < 8.0:
                return
            if not self._ocr_gpu_safe():          # TICKET-125: don't hitch a running game
                return
            if (self._live_mode or not self.lines
                    or (self.meta.get("source") or "").startswith("generated")
                    or not any(h in (self._last_src or "") for h in BROWSER_HINTS)):
                return
            import ocr_lyrics
            if not ocr_lyrics.available():
                return
            st = self.media.get()
            if not (st and st.get("status") == PLAYING):
                return
            self._last_ocr_sync_t = now
            raw_pos = float(st.get("position") or 0.0)
            hwnd = self._source_window_hwnd()
            ta, tt = (self._track or ("", ""))
            ocr_lines = ocr_lyrics.read_lyric_lines(hwnd=hwnd, track_title=tt, track_artist=ta)
            if not ocr_lines:
                return
            import difflib
            best = None                       # (ratio, lrc_idx)
            for ol in ocr_lines:
                no = ocr_lyrics._norm(ocr_lyrics._collapse_cjk_spaces(ol))
                if len(no) < 4:
                    continue
                for i, ln in enumerate(self.lines):
                    nl = ocr_lyrics._norm(getattr(ln, "jp", "") or "")
                    if len(nl) < 4:
                        continue
                    r = 0.95 if (no == nl or no in nl or nl in no) \
                        else difflib.SequenceMatcher(None, no, nl).ratio()
                    if best is None or r > best[0]:
                        best = (r, i)
            if not best or best[0] < 0.66:
                log.info("ocr-sync(%s): no confident LRC match (best %.2f)",
                         reason, best[0] if best else 0.0)
                self._last_ocr_sync = {"matched": False, "ratio": round(best[0], 2) if best else 0.0,
                                       "t": now, "reason": reason}
                return
            ratio, idx = best
            target = round(self.lines[idx].start - raw_pos, 2)
            if abs(target) > float(self._tune.get("energy_max_offset", 30.0)):
                log.info("ocr-sync(%s): line %d but offset %.1fs out of range — skip",
                         reason, idx, target)
                return
            self._last_ocr_sync = {"matched": True, "line": idx, "ratio": round(ratio, 2),
                                   "offset": target, "was": round(self.offset, 2),
                                   "t": now, "reason": reason}
            if abs(target - self.offset) < float(self._tune.get("deadband", 0.8)):
                log.info("ocr-sync(%s): line %d ratio %.2f — already within deadband (%.2fs)",
                         reason, idx, ratio, target - self.offset)
                return
            log.info("ocr-sync(%s): LRC line %d ratio %.2f → offset %.2fs (was %.2f)",
                     reason, idx, ratio, target, self.offset)
            self.root.after(0, lambda o=target: self._smooth_offset(o, "ocr-sync"))
        except Exception as e:
            log.info("ocr-sync error: %s", e)

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
            self._ocr_assisted_sync("energy-weak")   # TICKET-123: try the burned-in video lyrics
            return
        if ambiguous:
            log.info("energy-align: ambiguous — best %+.1fs (%.3f) vs rival %+.1fs (%.3f), "
                     "margin %.3f < %.3f → no change (chorus repetition)",
                     best_shift, best_score, rival_shift, rival_score,
                     best_score - rival_score, margin)
            verdict("inconclusive")
            self._ocr_assisted_sync("energy-ambiguous")   # TICKET-123
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
        # alpha=1.0 when lift≥0.30, falls linearly to 0.3 at the energy_lift_floor.
        # Anchored at the LIVE floor (not a hardcoded 0.10) so lowering the floor
        # for weak-but-clear peaks like kamone keeps the confidence gradient intact.
        _floor = float(self._tune.get("energy_lift_floor", 0.045))
        alpha = max(0.3, min(1.0, (lift - _floor) / 0.20 + 0.3))
        prev = self.offset
        blended = round((1.0 - alpha) * prev + alpha * new_off, 2)
        log.info("energy-align: offset %+.2fs → %+.2fs (α=%.2f, score %.3f, lift %.3f)",
                 prev, blended, alpha, score, lift)
        self._smooth_offset(blended, "energy-align")
        self._m(self.metrics.note_resync, "energy-align")        # TICKET-121 (applied realign only)
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
        # A clean ENERGY verdict ('insync' = sharp in-tolerance peak, 'corrected' = applied)
        # is direct proof the loaded BODY's line-timing grid lines up with the audio vocal
        # on/off pattern → corroborate the body. 'inconclusive' (weak/ambiguous — kamone
        # lift 0.049, and truly periodic-blind correct songs) does NOT corroborate, so a
        # blind-energy song stays body-checkable by ear. Corroborate ONLY here (energy),
        # never from the tier text-anchor path — a wrong JP body can false-anchor there.
        if verdict in ("insync", "corrected"):
            self._body_corroborated = True
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

        # (b) Lyrics ahead — boundary-deferred backward nudge (replaces v1.0.85
        # PAUSE which froze pos_raw for up to max_pause seconds). _smooth_offset
        # queues at the next line boundary so the current line plays out, then
        # the next line begins at the corrected offset; the highlight never
        # freezes. Symmetric with (c)'s catch-up nudge but in the opposite
        # direction. Clamped by fine_tune_max_pause_s (kept as the magnitude
        # cap so the knob remains meaningful in /tune).
        if drift > min_step:
            max_rewind = max_pause                     # reuse the existing cap knob
            step = min(drift, max_rewind)
            new_off = round(self.offset - step, 2)
            log.info("fine-tune rewind nudge: %+.2fs (drift %+.2fs, cap %.2f)",
                     -step, drift, max_rewind)
            self._smooth_offset(new_off, "fine-tune-rewind")
            self._fine_incon = 0
            self._fine_listen_after = self.root.after(interval_ms, self._fine_listen_tick)
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

    # ── M2: GPU-driven renderer (subprocess child) ─────────────────────────
    # The Tk overlay drives all the SMTC/sync/decision/lyric-fetch logic. When
    # gpu_renderer_on flips True we spawn gpu_renderer.py as a child process,
    # hide the Tk display window, and pipe state to the child over stdin:
    #   - {"type":"song", lines, meta, field}        on track change / load
    #   - {"type":"state", pos_raw, idx, fill_frac}  every Tk tick (~60 Hz)
    #   - {"type":"quit"}                            on toggle-off / app quit
    # If the child crashes / can't start, we fall back to the Tk renderer.
    def _gpu_child_cmd(self):
        """Build the argv to spawn the GPU renderer child. Frozen build → run
        the same EXE with the dispatch flag (main() in the child re-enters this
        file and routes to gpu_renderer.run_ipc_child()). Dev → run
        gpu_renderer.py with python directly."""
        import sys as _sys
        if getattr(_sys, "frozen", False):
            return [_sys.executable, "--gpu-renderer-child"]
        here = Path(__file__).parent / "gpu_renderer.py"
        return [_sys.executable, str(here), "--ipc"]

    def _start_gpu_renderer(self):
        """MOTHBALLED (v1.1.41). The in-process pygame/moderngl GPU CHILD renderer
        is retired: it spawned but drew NOTHING (color-keyed GL surface stayed
        blank) while HIDING the Tk overlay, so toggling "GPU renderer" left the
        user with no lyrics at all. The GPU render path is now the separate **Tauri
        overlay** (the separate lyric-overlay-tauri project) — a transparent,
        click-through WebView with per-pixel alpha + <ruby>, fed by the engine's
        /overlay endpoint.

        This stub keeps every call site (tray toggle, startup, /tune flip) HARMLESS:
        it never spawns a child and never withdraws the Tk window, so the WORKING
        Tk CPU renderer always stays on screen. `_apply_gpu_renderer_toggle` reads
        the False return and flips gpu_renderer_on back off. The old spawn code +
        gpu_renderer.py remain in git history for reference."""
        log.info("gpu_renderer: Python pygame/moderngl child is MOTHBALLED — the GPU "
                 "path is now the Tauri overlay; staying on the Tk CPU renderer")
        self._gpu_child = None
        return False

    def _stop_gpu_renderer(self, reason="off"):
        ch = getattr(self, "_gpu_child", None)
        if ch is not None:
            try:
                ch.stdin.write(b'{"type": "quit"}\n'); ch.stdin.flush()
            except Exception:
                pass
            try:
                ch.wait(timeout=1.5)
            except Exception:
                pass
            if ch.poll() is None:                 # didn't exit on its own → force it
                try:
                    ch.kill()
                except Exception:
                    pass
            self._gpu_child = None
            log.info("gpu_renderer: child stopped (%s)", reason)
        fh = getattr(self, "_gpu_log_fh", None)   # close the render-log file handle
        if fh not in (None, getattr(__import__("subprocess"), "DEVNULL", -3)):
            try:
                fh.close()
            except Exception:
                pass
        self._gpu_log_fh = None
        try:
            self.root.deiconify()             # restore Tk overlay
        except Exception:
            pass

    def _gpu_active(self):
        """True when the GPU child is alive and should be the renderer. _tick
        reads this to skip ALL CPU canvas work (the perf fix). If the child
        died, this flips False and the next tick resumes the Tk renderer; we
        also restore the (withdrawn) Tk window so the user never sees a blank
        screen after a child crash."""
        ch = getattr(self, "_gpu_child", None)
        if ch is None:
            return False
        if ch.poll() is not None:
            # Child exited unexpectedly — tear down cleanly + restore Tk.
            log.info("gpu_renderer: child exited (code %s) — restoring Tk renderer",
                     ch.returncode)
            self._gpu_child = None
            try:
                self.root.deiconify()
            except Exception:
                pass
            return False
        return True

    def _gpu_send(self, msg):
        ch = getattr(self, "_gpu_child", None)
        if ch is None or ch.poll() is not None:
            return
        try:
            ch.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            ch.stdin.flush()
            self._gpu_fail_streak = 0
        except Exception as e:
            # Tolerate a transient write hiccup — a single failed 60 Hz state
            # send must NOT orphan a healthy child (that was the silent-fallback
            # bug). Only give up after a sustained streak, and only if the
            # process is actually gone.
            self._gpu_fail_streak = getattr(self, "_gpu_fail_streak", 0) + 1
            if self._gpu_fail_streak >= 30 or ch.poll() is not None:
                log.info("gpu_renderer: send failing (%s, streak=%d) — stopping child",
                         e, self._gpu_fail_streak)
                self._stop_gpu_renderer(reason="pipe-error")

    def _gpu_send_song(self):
        ch = getattr(self, "_gpu_child", None)
        if ch is None or ch.poll() is not None:
            return
        self._gpu_send({
            "type": "song",
            "lines": [
                {"t": list(ln.t), "jp": ln.jp, "rm": ln.rm, "en": ln.en}
                for ln in (self.lines or [])
            ],
            "meta": dict(self.meta or {}),
            "field": "jp",
        })

    def _gpu_send_state(self, pos_raw):
        ch = getattr(self, "_gpu_child", None)
        if ch is None or ch.poll() is not None:
            return
        # Compute fill_frac the same way the active line is filled in Tk's
        # _karaoke() — using the active line's t-window. Keeps the GPU child
        # exactly in lockstep with the Tk renderer rather than re-deriving
        # idx independently (which could drift by ±1 line on close boundaries).
        idx = self.idx if 0 <= self.idx < len(self.lines) else -1
        fill = 0.0
        if idx >= 0:
            ln = self.lines[idx]
            dur = max(0.001, ln.end - ln.start)
            fill = max(0.0, min(1.0, (pos_raw - ln.start) / dur))
        try:
            playing = (self.media.get() or {}).get("status") == PLAYING
        except Exception:
            playing = True
        self._gpu_send({
            "type": "state",
            "pos_raw": round(float(pos_raw), 3),
            "offset": round(float(self.offset), 3),
            "idx": idx,
            "fill_frac": round(fill, 4),
            "playing": bool(playing),
            # scroll config so the GL overlay matches the Tk layout (horizontal
            # belt vs centered block). Cheap; the child treats it as idempotent.
            "scroll_dir": getattr(self, "scroll_dir", "none"),
            "scroll_speed": float(getattr(self, "scroll_speed", 200.0) or 200.0),
            # SETTINGS PARITY — opacity / position / font scale, same as the Tk
            # overlay, so toggling the renderer doesn't change the look.
            "opacity": round(float(getattr(self, "opacity", 1.0) or 1.0), 3),
            "pos_y": getattr(self, "pos_y", "center"),
            "pos_x": getattr(self, "pos_x", "center"),
            "font_scale": round(float(getattr(self, "font_scale", 1.0) or 1.0), 3),
        })

    def _apply_gpu_renderer_toggle(self, on: bool):
        """Live toggle hook from set_tune('gpu_renderer_on', …), the tray menu,
        and Overlay startup. Persists the choice so it survives a restart."""
        self.gpu_renderer_on = bool(on)
        if on:
            ok = self._start_gpu_renderer()
            if not ok:
                # Spawn failed — fall back to Tk and don't lie about being on.
                self.gpu_renderer_on = False
        else:
            self._stop_gpu_renderer(reason="toggle-off")
        try:
            self._persist()
        except Exception:
            pass

    # ── Tauri overlay (the GPU render path) ────────────────────────────────
    def _tauri_overlay_cmd(self):
        """Resolve the lyric-overlay.exe to launch. Order (first that exists):
        the `tauri_overlay_exe` tune override, the LYRIC_OVERLAY_EXE env var,
        then an `overlay/lyric-overlay.exe` (or `lyric-overlay.exe`) sitting next
        to the app — the frozen build dir, or this source file's dir in dev. No
        path is hardcoded, so the repo carries no machine-specific filesystem
        reference; on a dev box point LYRIC_OVERLAY_EXE (or the tune override) at
        your local Tauri build, or drop the exe in an `overlay/` folder beside
        the app. Returns the first path that exists, or None."""
        base = (Path(sys.executable).parent if getattr(sys, "frozen", False)
                else Path(__file__).parent)
        cands = []
        override = (self._tune.get("tauri_overlay_exe") or "").strip()
        if override:
            cands.append(Path(override))
        env = (os.environ.get("LYRIC_OVERLAY_EXE") or "").strip()
        if env:
            cands.append(Path(env))
        cands += [base / "overlay" / "lyric-overlay.exe", base / "lyric-overlay.exe"]
        for p in cands:
            try:
                if p.is_file():
                    return p
            except Exception:
                pass
        return None

    def _start_tauri_overlay(self):
        """Launch the standalone Tauri overlay child (windowless spawn; the
        overlay window itself is transparent + click-through + focus:false, so
        it never steals focus from a fullscreen game). It polls /overlay over
        HTTP and is fully decoupled from the Tk renderer — we do NOT hide Tk and
        do NOT pipe state to it. Returns True if a live child is running."""
        if self._tauri_active():
            return True
        exe = self._tauri_overlay_cmd()
        if exe is None:
            log.info("tauri overlay: lyric-overlay.exe not found — build it "
                     "(cargo build in lyric-overlay-tauri\\src-tauri) or set "
                     "tune tauri_overlay_exe")
            return False
        if not getattr(self, "api_on", True):
            log.info("tauri overlay: the Local API is OFF — the overlay will show "
                     "'waiting' until you enable it (it reads /overlay on :8765)")
        try:
            self._tauri_child = subprocess.Popen(
                [str(exe)], cwd=str(exe.parent),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
            log.info("tauri overlay: launched %s (pid %s)", exe, self._tauri_child.pid)
            return True
        except Exception as e:
            log.info("tauri overlay: launch failed: %s", e)
            self._tauri_child = None
            return False

    def _stop_tauri_overlay(self, reason="off"):
        ch = getattr(self, "_tauri_child", None)
        if ch is None:
            return
        try:
            if ch.poll() is None:
                ch.terminate()
                try:
                    ch.wait(timeout=2.0)
                except Exception:
                    pass
                if ch.poll() is None:
                    ch.kill()
        except Exception:
            pass
        self._tauri_child = None
        log.info("tauri overlay: stopped (%s)", reason)

    def _tauri_active(self):
        """True when the Tauri overlay child is alive. Self-heals the handle if
        the user closed the overlay window (so the tray checkmark stays honest)."""
        ch = getattr(self, "_tauri_child", None)
        if ch is None:
            return False
        if ch.poll() is not None:
            self._tauri_child = None
            return False
        return True

    def _apply_tauri_overlay_toggle(self, on: bool):
        """Tray-menu hook: the Tauri overlay BECOMES the renderer. Turning it on
        HIDES the Tk overlay (only one overlay on screen — the GPU one) and the
        _tick fast-path then skips all Tk canvas work; turning it off restores the
        Tk overlay. The choice persists. A 2s watchdog restores Tk if the overlay
        process dies, so a crash/close can never leave a blank screen."""
        on = bool(on)
        if on:
            ok = self._start_tauri_overlay()
            self.tauri_overlay_on = ok          # don't claim 'on' if it didn't launch
            if ok:
                try:
                    self.root.withdraw()         # GPU overlay is the renderer now
                except Exception:
                    pass
                self._arm_tauri_watchdog()
        else:
            self._stop_tauri_overlay(reason="toggle-off")
            self.tauri_overlay_on = False
            try:
                self.root.deiconify()            # restore the Tk overlay
            except Exception:
                pass
        try:
            self._persist()
        except Exception:
            pass

    def _arm_tauri_watchdog(self):
        """CPU-FALLBACK GUARANTEE. While the GPU overlay is the renderer (Tk
        withdrawn), poll every 2s ON THE TK THREAD and restore the Tk (CPU)
        overlay if the GPU overlay is not actually rendering. We detect that two
        ways: the child PROCESS died, OR the overlay stopped polling /overlay (a
        stale heartbeat) — which catches the harder 'process alive but blank /
        no window / JS frozen' failures the process check alone misses. Either
        way the user always ends up with a working overlay, never a blank screen."""
        self._overlay_ping_t = time.time()       # grace from launch for WebView init
        stale_s = float(self._tune.get("overlay_heartbeat_stale_s", 6.0))

        def _restore(reason):
            self._tauri_child = None
            self.tauri_overlay_on = False
            log.info("tauri overlay: %s — falling back to the Tk (CPU) overlay", reason)
            try:
                self._stop_tauri_overlay(reason=reason)
            except Exception:
                pass
            try:
                self.root.deiconify()
            except Exception:
                pass
            try:
                self._persist()
            except Exception:
                pass

        def _check():
            if not getattr(self, "tauri_overlay_on", False):
                return                           # toggled off elsewhere → stop polling
            ch = getattr(self, "_tauri_child", None)
            if ch is None or ch.poll() is not None:
                _restore("overlay process gone")
                return
            if (time.time() - getattr(self, "_overlay_ping_t", 0)) > stale_s:
                _restore("overlay not rendering (stale heartbeat)")
                return
            self.root.after(2000, _check)
        try:
            self.root.after(2000, _check)
        except Exception:
            pass

    def quit(self):
        self._stop_gpu_renderer(reason="app-quit")
        self._stop_tauri_overlay(reason="app-quit")
        self._m(self.metrics.finalize)      # TICKET-121: flush the last in-flight play
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


# ── CPU affinity / priority policy (hardware-agnostic) ───────────────────────────
# Windows PRIORITY_CLASS constants.
_PRIO_ABOVE_NORMAL = 0x00008000
_PRIO_NORMAL       = 0x00000020
_PRIO_BELOW_NORMAL = 0x00004000
_PRIO_IDLE         = 0x00000040


def _last_physical_core_mask():
    """Affinity mask covering ONLY the LAST physical core (including both SMT threads
    if the CPU is hyper-threaded), via GetLogicalProcessorInformation. Returns an int
    mask, or 0 if it can't be determined. Single processor-group only (≤64 logical
    CPUs) — that covers every consumer CPU; multi-group servers fall back to the
    caller's heuristic. SMT-aware and core-count-agnostic, so it is correct whether
    the box is a 4-thread laptop or a 32-thread desktop, Intel or AMD."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32

        class _SLPI(ctypes.Structure):   # SYSTEM_LOGICAL_PROCESSOR_INFORMATION
            _fields_ = [
                ("ProcessorMask", ctypes.c_size_t),
                ("Relationship", ctypes.c_int),
                ("_union", ctypes.c_ubyte * 16),   # largest union member (CACHE/NUMA)
            ]

        RelationProcessorCore = 0
        length = wintypes.DWORD(0)
        k32.GetLogicalProcessorInformation(None, ctypes.byref(length))
        if not length.value:
            return 0
        count = length.value // ctypes.sizeof(_SLPI)
        arr = (_SLPI * count)()
        if not k32.GetLogicalProcessorInformation(arr, ctypes.byref(length)):
            return 0
        best = 0
        for i in range(count):
            e = arr[i]
            if e.Relationship == RelationProcessorCore and int(e.ProcessorMask) > best:
                best = int(e.ProcessorMask)          # highest-indexed physical core
        return best
    except Exception:
        return 0


def _upper_cores_mask(n):
    """Legacy spread mask: upper-half-plus-one cores, never core 0 (Windows runs the
    audio engine + most DPC/ISR on core 0, so staying off it keeps audio clean).
    16-thread → 0xff80; 8-thread → 0xf8; 4-thread → 0xe."""
    if n >= 4:
        mask = 0
        for c in range(max(1, (n // 2) - 1), n):
            mask |= (1 << c)
        return mask
    if n >= 2:
        return ((1 << n) - 1) & ~1        # everything except core 0
    return 1


def _dedicate_last_core_mask(n):
    """Mask for 'the last core drives the product'. Prefers the exact last PHYSICAL
    core (SMT-aware, via the Win32 topology API); falls back to the last logical CPU
    (plus its likely SMT sibling on an even, ≥8-thread machine) if the API is
    unavailable. Always lands on the highest cores, off core 0."""
    m = _last_physical_core_mask()
    if m:
        return m
    if n >= 8 and n % 2 == 0:
        return (1 << (n - 1)) | (1 << (n - 2))      # assume one SMT pair
    return 1 << (n - 1)


def _apply_affinity_priority(mask, prio):
    """Best-effort SetProcessAffinityMask + SetPriorityClass on this process. Returns
    (affinity_ok, mask). Uses 64-bit-correct ctypes signatures — without them ctypes
    truncates the pseudo-handle (-1) and the DWORD_PTR mask to 32 bits and the call
    silently no-ops. Reversible by the user via Task Manager."""
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    k32.SetProcessAffinityMask.restype = ctypes.c_int
    k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    h = k32.GetCurrentProcess()
    ok = bool(k32.SetProcessAffinityMask(h, mask)) if mask else False
    if prio:
        k32.SetPriorityClass(h, prio)
    return ok, mask


def main():
    # M2 GPU-renderer child mode dispatch — runs BEFORE _is_only_instance() so
    # the parent (which already holds the named mutex) can spawn us without
    # being kicked out for "another instance is running". The child has no
    # SMTC / sync / decision pipeline; it just reads NDJSON from stdin and
    # paints. Done before everything else so the spawn doesn't drag along the
    # whole audio engine + tray icon for the child.
    if "--gpu-renderer-child" in sys.argv[1:]:
        try:
            import gpu_renderer
            gpu_renderer.run_ipc_child()
        except Exception as e:
            print(f"gpu_renderer child died: {e!s}", file=sys.stderr)
        return
    if "--recognize-child" in sys.argv[1:]:
        # TICKET-135: identify-by-sound in a SEPARATE PROCESS so the GIL-heavy
        # capture+fingerprint can't stall the parent's render thread (the
        # "highlight sticks then jumps" fix). Writes the result to the --out FILE
        # (a windowed PyInstaller app has no reliable stdout — sys.stdout.flush()
        # raised [Errno 22] and PyInstaller popped a crash dialog). EVERYTHING is
        # wrapped so this child can NEVER raise an unhandled exception.
        try:
            import json as _json
            _a = sys.argv[1:]
            def _flag(name):
                try:
                    j = _a.index(name)
                    return _a[j + 1] if j + 1 < len(_a) and not _a[j + 1].startswith("--") else None
                except ValueError:
                    return None
            _i = _a.index("--recognize-child")
            def _num(k, default):
                try:
                    v = _a[_i + k]
                    return v if (_i + k < len(_a) and not v.startswith("--")) else None
                except Exception:
                    return None
            _secs = float(_num(1, None) or 6.0)
            _atts = int(_num(2, None) or 1)
            _out = _flag("--out")
            _res = {"t": None}
            try:
                from recognize import recognize_playing
                t, ar, off, tc = recognize_playing(_secs, _atts)
                _res = {"t": t, "a": ar, "off": off, "tc": tc}
            except Exception as e:
                _res = {"t": None, "err": str(e)}
            try:
                if _out:
                    with open(_out, "w", encoding="utf-8") as _f:
                        _f.write(_json.dumps(_res))
                else:
                    sys.stdout.write(_json.dumps(_res) + "\n")
                    sys.stdout.flush()
            except Exception:
                pass
        except Exception:
            pass
        return
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
    # CPU AFFINITY + PRIORITY are applied via ov._apply_dynamic_priority() right after
    # the Overlay is built (TICKET-129) — a single code path that reads the override
    # knob, so 'last core @ ABOVE_NORMAL' (or the legacy spread) is honored immediately
    # and re-asserted on every monitor tick. Letting Overlay.__init__ run unpinned at
    # normal priority just makes startup a touch faster. Keeping the app OFF core 0
    # (where Windows runs the audio engine + DPC/ISR) is preserved by the masks.
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
    ov._apply_dynamic_priority()   # TICKET-129: pin to last core @ ABOVE_NORMAL (override: tune cpu_dedicate_last_core)
    # v1.1.15: the GPU renderer is the DEFAULT (persisted setting, default True).
    # Bring it up after Overlay.__init__ so tray/api/SMTC are wired and Tk has a
    # root window to withdraw. A spawn failure leaves the Tk window visible.
    if getattr(ov, "gpu_renderer_on", True):
        ov._apply_gpu_renderer_toggle(True)
    # v1.1.42: relaunch the Tauri overlay child if the user left it on. Mirror
    # the toggle path — reconcile the flag so a missing exe at launch clears it
    # instead of silently re-persisting "on". v1.1.44: when it launches, the
    # GPU overlay is the renderer → hide the Tk overlay + arm the watchdog.
    if getattr(ov, "tauri_overlay_on", False):
        ov.tauri_overlay_on = ov._start_tauri_overlay()
        if ov.tauri_overlay_on:
            try:
                ov.root.withdraw()
            except Exception:
                pass
            ov._arm_tauri_watchdog()

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
    def _toggle_tauri_overlay(*_):
        ov.root.after(0, lambda: ov._apply_tauri_overlay_toggle(not ov._tauri_active()))
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

    # ── TICKET-117: Source pin (which SMTC session feeds lyrics) ─────────
    # Two browser tabs both playing media (e.g. a muted visual + the actual
    # music) collide under Auto because both register as 'playing'; pinning
    # locks the overlay onto exactly one session. The submenu is rebuilt on
    # every open (callable Menu) so it reflects the live session list.
    _SOURCE_PRETTY = {
        "brave":   "Brave",
        "chrome":  "Chrome",
        "msedge":  "Edge",
        "edge":    "Edge",
        "firefox": "Firefox",
        "opera":   "Opera",
        "vivaldi": "Vivaldi",
        "arc":     "Arc",
        "spotify": "Spotify",
        "discord": "Discord",
        "slack":   "Slack",
        "teams":   "Teams",
        "music.youtube": "YouTube Music",
    }

    def _pretty_source(src):
        s = (src or "").lower()
        for needle, name in _SOURCE_PRETTY.items():
            if needle in s:
                return name
        # Strip the trailing .exe / AUMID suffix for readability.
        return (src or "(unknown)").split("!")[0].split(".")[0] or "(unknown)"

    def _set_pin(sid, app=""):
        def _do():
            ov.root.after(0, lambda: ov.set_pinned_session(sid, app))
        return _do

    def _source_menu_items():
        items = [
            pystray.MenuItem(
                "🔓  Auto  (highest-priority session wins)",
                _set_pin("", ""),
                radio=True,
                checked=lambda i: not ov.pinned_session_id),
            pystray.Menu.SEPARATOR,
        ]
        try:
            sessions = ov.media.list_sessions()
        except Exception:
            sessions = []
        seen_pinned = False
        # Sort: playing first, then by title for stability across rebuilds.
        sessions.sort(key=lambda s: (s.get("status") != PLAYING,
                                     (s.get("title") or "").lower()))
        for s in sessions:
            sid = s.get("id") or ""
            if not sid:
                continue
            if sid == ov.pinned_session_id:
                seen_pinned = True
            playing_glyph = "▶" if s.get("status") == PLAYING else "⏸"
            title = (s.get("title") or "(no title)").strip()
            if len(title) > 60:
                title = title[:57] + "…"
            label = f"{playing_glyph}  {title}  —  {_pretty_source(s.get('source'))}"
            items.append(pystray.MenuItem(
                label,
                _set_pin(sid, (s.get("source") or "").lower()),
                radio=True,
                checked=lambda i, sid=sid: ov.pinned_session_id == sid))
        # If the pinned session is missing from the live list (grace window),
        # show a disabled placeholder so the user can SEE the pin is still
        # active and not silently dropped.
        if ov.pinned_session_id and not seen_pinned:
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem(
                "⌛  (pinned session missing — holding lyrics)",
                None, enabled=lambda i: False))
        if len(items) <= 2:
            items.append(pystray.MenuItem(
                "(no sessions detected)", None, enabled=lambda i: False))
        return pystray.Menu(*items)

    def _refresh_source_menu():
        # Called from the MediaWatcher poll thread on session-set change.
        # The whole top-level menu is a single pystray.Menu object so we just
        # ask the icon to redraw — the callable-Menu in the Source item picks
        # up the fresh session list on the next open.
        try:
            if ov._icon is not None:
                ov._icon.update_menu()
        except Exception:
            pass

    ov.media.set_sessions_changed_cb(_refresh_source_menu)

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
        pystray.MenuItem("⏪  Lyrics earlier  +5.0s", _nudge(+5.0)),
        pystray.MenuItem("⏪  Lyrics earlier  +2.0s", _nudge(+2.0)),
        pystray.MenuItem("⏪  Lyrics earlier  +0.5s", _nudge(+0.5)),
        pystray.MenuItem("⏩  Lyrics later  −0.5s", _nudge(-0.5)),
        pystray.MenuItem("⏩  Lyrics later  −2.0s", _nudge(-2.0)),
        pystray.MenuItem("⏩  Lyrics later  −5.0s", _nudge(-5.0)),
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
        # TICKET-117: pin lyrics to ONE SMTC session. Solves the two-browser-
        # tabs case (one MUTED visual + one with the music) where Auto
        # ping-pongs because both register as 'playing'. Label updates live.
        pystray.MenuItem(
            lambda i: ("🎯  Source: pinned"
                       if ov.pinned_session_id else "🎯  Source  (Auto)"),
            _source_menu_items),
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
        # v1.1.42: the GPU render path is the standalone Tauri overlay — a
        # transparent, click-through, per-pixel-alpha WebView fed over HTTP by
        # /overlay. This toggles its child process on/off (additive: the Tk
        # overlay keeps running). The checkmark self-heals if the user closes
        # the overlay window. The old in-process pygame/moderngl child stays
        # MOTHBALLED (drew nothing); its toggle is gone.
        pystray.MenuItem("GPU overlay (Tauri · smooth, click-through)",
                         _toggle_tauri_overlay,
                         checked=lambda i: ov._tauri_active()),
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
    # TICKET-117: stash the icon handle on the Overlay so set_pinned_session()
    # and the auto-unpin tick can call icon.update_menu() / icon.notify().
    ov._icon = icon
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
                # TICKET-117: keep ov._icon in sync with the replacement icon
                # so set_pinned_session()/the auto-unpin tick stop poking the
                # dead handle and start refreshing the new one.
                ov._icon = cur
            except Exception:
                pass
    threading.Thread(target=_tray_runner, daemon=True).start()
    ov.run()


if __name__ == "__main__":
    main()
