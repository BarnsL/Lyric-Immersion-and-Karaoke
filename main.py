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


def clean_title(title, source=""):
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
    is_cover = bool(re.search(r"歌ってみた|うたってみた|歌わせて|covered?\s+by"
                              r"|\(\s*cover\s*\)|[/／]\s*cover\b", t, re.I))
    if any(h in source for h in BROWSER_HINTS):
        t = re.sub(r"\s*[-–—|]\s*YouTube\s*$", "", t, flags=re.I)

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
    if song and song.strip():
        t = song.strip()

    t = re.sub(r"\s*[\[(【「『][^\])】」』]*[\])】」』]", "", t)       # leftover (Official MV) etc.
    # cover / "tried singing" credits → keep only the song title
    t = re.sub(r"\s*([/／]\s*)?\bcover(ed)?\s+by\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s*[/／]\s*cover\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s*[/／]?\s*(歌ってみた|歌わせて|アコギ|acoustic\s*ver).*$", "", t, flags=re.I)
    t = re.sub(
        r"\b(Official\s*(Music\s*)?(Video|Audio)|Official|Music\s*Video|MV|PV|"
        r"Lyric\s*Video|Audio|HD|4K|FULL|Full\s*Ver\.?)\b",
        "", t, flags=re.I,
    )
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
    # Cover titled "OriginalSong / Singer(s)": keep the song (before the first
    # slash), dropping the coverer names so the search hits the original's lyrics
    # ('ウェカピポ / 綺々羅々ヴィヴィ × 白銀ノエル' → 'ウェカピポ').
    if is_cover and re.search(r"\s[/／]\s", t):
        t = re.split(r"\s[/／]\s", t, 1)[0]
    return t.strip(" -–—|/　").strip()


# Titles that name an EVENT (a whole concert / festival / medley), not a song.
_LIVE_RE = re.compile(
    r"\b(?:live|concert|fes(?:tival)?|tour|setlist|set\s*list|medley|megamix|"
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


def clean_artist(artist):
    """Strip YouTube channel cruft so the artist matches lyric providers:
    'Kaneko Lumi - Topic' → 'Kaneko Lumi', 'LMFAOVEVO' → 'LMFAO'. Auto-generated
    '… - Topic' / VEVO / 'Official Artist Channel' uploads are real tracks; the
    suffix just blocks the provider/Shazam-name search."""
    a = (artist or "").strip()
    a = re.sub(r"\s*[-–—]\s*Topic$", "", a, flags=re.I)
    a = re.sub(r"\s*[-–—]\s*Official(\s+(Artist|Music))?\s+Channel$", "", a, flags=re.I)
    a = re.sub(r"\s*VEVO$", "", a)
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
            score = 0
            for ct in e.get("forms") or {_norm_title(e["core"])}:
                for q in q_forms:
                    if not ct or not q:
                        continue
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
        self.scroll_dir = s.get("scroll", "left")      # 'none'|'left'|'right'|'lr'|'rl'
        self.scroll_speed = float(s.get("scroll_speed", SCROLL_SPEED))
        self.font_scale = float(s.get("font_scale", 1.0))  # 0.25 … 2.0
        self.perf = s.get("perf", "smooth")            # 'smooth' | 'fast'
        self.recal_secs = int(s.get("recal_secs", 10))  # re-check by sound often (0=off)
        self.git_sync = bool(s.get("git_sync", False))  # push new songs to git
        self.character_on = bool(s.get("character", False))  # dancing companion
        self.api_on = bool(s.get("api", True))         # local agent-control API
        self.boundary_on = bool(s.get("boundary", True))  # fast song-change detect
        self.generate_on = bool(s.get("generate", True))  # generate lyrics by ear
        self._generating = False      # Whisper lyric-generation in progress
        self._gen_token = 0           # bumped on track change to cancel generation
        self._gen_lines = []          # accumulated generated line dicts
        self._gen_title = self._gen_artist = ""
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
        self._frame_ms = 0.0         # EWMA of render-frame interval (ms) → /status render_fps
        self._last_tick_t = None
        self._render_frame = False
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

        hwnd = ctypes.windll.user32.GetAncestor(self.root.winfo_id(), 2) \
            or self.root.winfo_id()
        ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        # NOACTIVATE | TOOLWINDOW (no focus steal / no taskbar button) +
        # LAYERED | TRANSPARENT → click-through, so the full-screen overlay never
        # intercepts mouse input.
        ex |= 0x08000000 | 0x00000080 | 0x00080000 | 0x00000020
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
        self._aligning = False        # sync-by-listening (Whisper) in progress
        self._identify_result = None
        self._sound_song = None       # last (title, artist) heard by Shazam
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

    # ── per-track ──

    def _on_track_change(self, track, duration=None):
        artist, title = track
        if not artist and " - " in title:
            a, t = title.split(" - ", 1)
            artist, title = a.strip(), t.strip()
        self.character.set_artist(artist or title)   # spawn this song's artist
        self._cur_duration = duration
        self._health_attempts = 0
        self.offset = 0.0          # fresh baseline; sound calibration sets it
        self._sound_song = None    # new video → re-identify by ear
        self._gen_token += 1       # cancel any in-flight lyric generation
        self._generating = False
        log.info("track change: %r / %r (dur %s)", title, artist, duration)

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
            path = self.index.match(artist, title, duration)
            if path and self._file_valid(path, duration):
                if path != self._lyrics_path:
                    self.load(path)
                self._maybe_translate()
                # LOCK to this match if it's an EXACT title match on a clean,
                # official song name (not an MV/generic/live title): then a Shazam
                # mis-ID of a *different* song by the same artist can't override it
                # (the feelingradation → SKAVLA bug). Messy titles stay unlocked so
                # sound still corrects them.
                exact = _norm_title(self.meta.get("title", "")) == _norm_title(title)
                self._title_locked = bool(
                    exact and not is_mv_version(title)
                    and not _is_generic_title(title))
            else:
                self.lines, self._lyrics_path, self.idx = [], None, -1
                self._kara = []
                self._verified = False
                self._title_locked = False
                self._hint(f"♪ {title} — identifying…")
                self._start_fetch(artist, title, duration)

        # MV / cinematic dead-space intro: for an MV-titled video, hold the lyrics
        # through the leading intro (see _tick); for ANY unaligned track, anchor
        # lyric time 0 to the detected audio onset (see _on_song_onset). Shazam
        # overrides both the moment it can measure the real offset.
        self._mv_mode = is_mv_version(title) and not self._live_mode
        self._intro_anchored = False
        self._track_t0 = time.time()

        # PRIMARY signal: identify by sound and let it decide the real song.
        self._start_identify(seconds=6, attempts=2)
        # Lock the timing fast: a short burst of quick re-checks right after the
        # song starts, then the loop relaxes to the normal cadence.
        self._fast_calib = 3
        self._arm_recal(7)

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
                from fetch_lyrics import backfill_file   # romaji + translation
                ok = backfill_file(path)
            except Exception:
                ok = False
            self._translate_result = (path, ok)

        threading.Thread(target=work, daemon=True).start()

    # ── audio identification (detect by SOUND, not title) ──

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
        finally:
            self._arm_recal(nxt)

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
                # that original script for fetching/matching.
                g_artist, g_title = (self._track or ("", ""))
                if (_has_cjk(g_title) and not _has_cjk(title)
                        and not _is_generic_title(g_title) and not self._live_mode):
                    f_artist, f_title = (g_artist or artist), g_title
                else:
                    f_artist, f_title = artist, title
                heard = (f_title, f_artist)
                # Does the song we HEARD match the lyrics currently loaded?
                loaded_ok = bool(self.lines) and self._titles_match(
                    self.meta.get("title", ""), f_title)
                log.info("heard %r / %r | loaded %r | match=%s",
                         f_title, f_artist, self.meta.get("title", ""), loaded_ok)
                if loaded_ok:
                    # CALIBRATE timing ONLY when the heard song is the loaded one.
                    # (Applying a heard song's offset to *different* lyrics — e.g.
                    # a Shazam mis-ID on a mix — was what produced wild offsets.)
                    self._sound_song = heard
                    if offset is not None and t_cap is not None:
                        st = self.media.get()
                        if st and st.get("status") == PLAYING:
                            true_now = offset + (time.time() - t_cap) * st.get("rate", 1.0)
                            corr = true_now - st["position"]
                            diff = corr - self.offset
                            if abs(corr) < 180:
                                if abs(diff) > 2.0:
                                    self.offset = round(corr, 2)            # snap (intro / seek)
                                elif abs(diff) > 0.15:
                                    self.offset = round(self.offset + 0.8 * diff, 2)  # ease drift
                elif self._title_locked:
                    # The lyrics came from a confident EXACT match on a clean
                    # official title, but Shazam heard a DIFFERENT song — almost
                    # always a mis-ID of another track by the SAME artist
                    # (feelingradation heard as SKAVLA). Trust the title; ignore it.
                    log.info("ignoring sound %r — title-locked to %r",
                             f_title, self.meta.get("title", ""))
                else:
                    # heard a DIFFERENT song → switch to it; start its timing fresh
                    # rather than carrying over the previous song's offset.
                    self._sound_song = heard
                    self.offset = 0.0
                    self._fast_calib = max(self._fast_calib, 2)
                    self._arm_recal(7)
                    cached = self.index.match(f_artist, f_title, self._cur_duration)
                    if cached and self._file_valid(cached, self._cur_duration):
                        if cached != self._lyrics_path:
                            log.info("correcting -> cached %s", cached.name)
                            self.load(cached)
                        self._maybe_translate()
                    else:
                        log.info("correcting -> fetching %r / %r", f_title, f_artist)
                        self._start_fetch(f_artist, f_title, self._cur_duration)

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
        self.lines, self.idx, self._kara = [], -1, []
        self._lyrics_path = None
        self._verified = False
        self.meta = {"title": self._gen_title, "artist": self._gen_artist,
                     "lang": "ja", "duration": self._cur_duration, "source": "generated"}
        self._hint("✨ Generating lyrics by ear… (AI — marked ***)")
        log.info("generating lyrics by Whisper for %r", self._gen_title)
        threading.Thread(target=self._generate_loop,
                         args=(self._gen_token,), daemon=True).start()

    def _generate_loop(self, token):
        """Capture the song in chunks, transcribe each, annotate, accumulate.
        Cancels the moment the track changes (token bump)."""
        import align
        from fetch_lyrics import annotate
        CHUNK, last_end, idle = 16, 0.0, 0
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
            chunk = align.transcribe_for_generation(pos, lang="ja", seconds=CHUNK)
            if token != self._gen_token:
                return
            new = [d for d in chunk if d["t"][0] >= last_end - 1.0 and d["jp"].strip()]
            if new:
                try:
                    annotate(new, "ja", translate=True)   # furigana + romaji + EN
                except Exception:
                    pass
                for d in new:
                    if d.get("en", "").strip():
                        d["en"] = d["en"].strip() + " ***"   # mark as AI-generated
                    last_end = max(last_end, d["t"][1])
                self._gen_lines += new
                self.root.after(0, lambda t=token: self._apply_generated(t))
            if self._cur_duration and pos >= self._cur_duration - CHUNK:
                break
        if token == self._gen_token:
            self._generating = False

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
                             "lang": "ja", "duration": self._cur_duration,
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
        self._mark_verified()
        self._relayout_song()           # size lanes/blocks to this song's rows
        self.root.geometry(f"{self.W}x{self.H}+0+{self._geom_y()}")
        self.root.attributes("-topmost", True)
        if not keep_idx:
            self.idx = -1
            self._kara = []
            self._clear_stream()
            self.cv.delete("all")

    # ── main loop ──

    def _tick(self):
        # Measure the interval between consecutive RENDER frames (only) for the
        # /status render_fps readout — paused/no-music frames use a slower cadence
        # and would skew it, so they're excluded.
        now = time.time()
        if self._render_frame and self._last_tick_t is not None:
            dt = (now - self._last_tick_t) * 1000.0
            if 0.0 < dt < 500.0:
                self._frame_ms = (dt if self._frame_ms <= 0
                                  else 0.9 * self._frame_ms + 0.1 * dt)
        self._last_tick_t = now
        self._render_frame = False

        self._consume_async()
        state = self.media.get()

        if not state or not state["title"]:
            if self._track is not None:
                self._track = None
                self._hint("Waiting for music…")
            self.root.after(120, self._tick)
            return

        # clean_title() runs several regexes; the raw title rarely changes, so
        # only recompute it when it does (this loop runs every frame).
        rawt, src, rawa = state["title"], state["source"], state.get("artist", "")
        if (rawt != self._last_raw_title or src != self._last_src
                or rawa != self._last_artist):
            self._last_raw_title, self._last_src, self._last_artist = rawt, src, rawa
            self._clean_title_cache = clean_title(rawt, src)
            self._clean_artist_cache = clean_artist(rawa)
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
        # _on_song_onset clears _intro_anchored at the real onset; a ~50s timeout
        # releases it if no clear dead-space turns up (then lyrics run normally).
        if (self._mv_mode and not self._intro_anchored
                and self._sound_song is None):
            if state["position"] > 50.0 or (time.time() - self._track_t0) > 50.0:
                self._intro_anchored = True
            else:
                self._hint("🎬 Cinematic intro — waiting for the song…")
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
        Returns the PIL image (not a PhotoImage) so the caller can paste it into
        an existing PhotoImage — avoiding a fresh allocation every fill-step."""
        img = Image.new("RGBA", (max(1, spec["w"]), max(1, spec["h"])), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        sw = max(1, round(2 * self.font_scale * self._auto_scale))
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

    def _render_img_block(self, i, frac):
        self._blk_seq += 1
        tag = f"blk{self._blk_seq}"
        spec = self._block_spec(i)
        photo = ImageTk.PhotoImage(self._paint_block_img(spec, frac))
        lane_y = self._lane_y0 + (i % self._lanes) * self._lane_gap
        self.cv.create_image(0, lane_y, image=photo, anchor="nw", tags=(tag, "strm"))
        return {"idx": i, "tag": tag, "x": 0.0, "w": spec["w"], "img": True,
                "spec": spec, "photo": photo, "sung_n": -1,
                "nchars": max((len(r["chars"]) for r in spec["rows"]), default=0)}

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
        if self._tick_n % self._fill_skip:
            return

        want = {}
        for i, ln in enumerate(self.lines):
            cx = center + d * v * ((ln.start + ln.end) / 2 - pos)
            if -1200 < cx < self.W + 1200:
                want[i] = cx
        if want:
            self.cv.delete("hint")        # real lyrics showing → drop any stale hint
        have = {b["idx"] for b in self._stream}
        # Spawn missing blocks, but only a couple per pass — each spawn PIL-renders
        # an image (the costliest op), so creating several at once is what spiked
        # frame time. Blocks appear 1200px off-screen, so a frame or two of spawn
        # latency is invisible; render the nearest-to-centre (most imminent) first.
        missing = sorted((i for i in want if i not in have),
                         key=lambda i: abs(want[i] - center))
        for i in missing[:self._spawn_budget]:
            cx = want[i]
            ln = self.lines[i]
            dur = ln.end - ln.start
            frac = (pos - ln.start) / dur if (ln.start <= pos < ln.end and dur > 0) else 0.0
            b = self._spawn_block(i, frac)
            self.cv.move(b["tag"], (cx - b["w"] / 2) - b["x"], 0)
            self._stream.append(b)
        # Despawn off-screen blocks, and advance karaoke fills — but cap PIL-paste
        # repaints per pass (the other heavy op). Only blocks whose sung-count
        # actually changed need it, and usually just the one line currently singing.
        now = time.time()
        repaints = 0
        for b in self._stream[:]:
            if b["idx"] not in want:
                self.cv.delete(b["tag"])
                self._stream.remove(b)
                continue
            ln = self.lines[b["idx"]]
            dur = ln.end - ln.start
            frac = (pos - ln.start) / dur if (ln.start <= pos < ln.end and dur > 0) else 0.0
            if b.get("img"):
                n = int(frac * b["nchars"] + 0.5)
                # Each fill repaint is a ~50ms PhotoImage paste, so cap BOTH the
                # repaints per frame AND each block's repaint rate — a karaoke
                # sweep at ~5fps reads fine, while per-character pastes were what
                # stalled the belt. Idle (not-currently-sung) blocks never repaint.
                if (n != b["sung_n"] and repaints < self._repaint_budget
                        and now - b.get("paint_t", 0.0) >= self._fill_interval):
                    b["sung_n"] = n
                    b["paint_t"] = now
                    # paste into the existing PhotoImage (no new allocation) —
                    # the canvas reflects it without an itemconfig call
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

    # ── tray hooks ──

    def nudge(self, d):
        self.offset += d

    def reset_offset(self):
        self.offset = 0.0

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
        self.root.geometry(f"{self.W}x{self.H}+0+{self._geom_y()}")
        self.root.attributes("-topmost", True)
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
                                                on_onset=self._fire_onset_event)
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

    def _fire_onset_event(self):
        """Detector thread heard the song start after a quiet intro → Tk thread."""
        try:
            self.root.after(0, self._on_song_onset)
        except Exception:
            pass

    def _on_song_onset(self):
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
        # Only a *leading* intro: the onset must land in the first ~50s, and after a
        # real gap from t=0 (skip near-zero — that's a song that simply starts).
        if not (1.0 < vpos < 50.0):
            return
        self._intro_anchored = True
        self.offset = round(-vpos, 2)        # video time `vpos` → lyric time 0
        self.idx = -1
        log.info("MV intro dead-space ~%.1fs → anchored lyrics to the song onset", vpos)

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
                        "boundary": self.boundary_on, "generate": self.generate_on})

    def set_generate(self, on):
        """Toggle the last-resort Whisper lyric generation."""
        self.generate_on = bool(on)
        self._persist()

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
            self._fill_skip = 2          # heavy ticker work every 2nd frame (~15fps)
        else:
            _OUTLINE = _OUTLINE_FULL     # 5 items/char, 60fps
            self._fps = 16
            self._fill_skip = 3          # belt moves at 60fps; fills/spawns at ~20fps

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
        self._lanes = max(1, min(4, int(fit)))     # up to 4 when blocks are short
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
        self._sound_song = None
        self._title_locked = False     # let sound override after a manual reject
        self._hint("🎧 Listening to identify the song…")
        self._start_identify()

    def identify_by_sound(self):
        self._sound_song = None
        self._hint("🎧 Listening to identify the song…")
        self._start_identify()

    # ── sync by listening (align cached lyrics to the HEARD audio) ──
    def _align_pos(self):
        """The player's RAW position right now (no offset applied) — read at
        capture start so the alignment can derive an absolute offset."""
        st = self.media.get() or {}
        return float(st.get("position") or 0.0)

    def align_by_listening(self):
        """On-demand: transcribe a few seconds of the live vocals and match them
        to the loaded lyrics to set the sync offset — fixes timing when Shazam
        can't identify the exact cut. Opt-in, runs once in a background thread,
        and no-ops gracefully if faster-whisper isn't installed."""
        if self._aligning:
            return
        try:
            import align
            ok, err = align.available(), align._last_error
        except Exception as e:
            ok, err = False, str(e)
        if not ok:
            self._hint("Sync-by-listening needs faster-whisper — see the README")
            log.info("align requested but faster-whisper not available: %s", err)
            return
        if not self.lines:
            self._hint("Play a recognised song first, then sync by listening")
            return
        self._aligning = True
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

    def _apply_align(self, res):
        self._aligning = False
        if not res:
            self._hint("Couldn't hear the lyrics clearly — try again")
            return
        offset, ratio, _start = res
        self.offset = round(offset, 2)
        log.info("aligned by listening: offset=%.2fs (match %.2f)", offset, ratio)
        self._hint(f"Synced by ear ({offset:+.1f}s)")

    def quit(self):
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
    def _align(*_):   ov.root.after(0, ov.align_by_listening)
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
        pystray.MenuItem("Generate lyrics by ear when none found (AI, ***)", _toggle_gen,
                         checked=lambda i: ov.generate_on),
        pystray.MenuItem("Auto re-sync by sound", recal_menu),
        pystray.MenuItem("Library backup (Git)", git_menu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda i: f"Sync timing  ({ov.offset:+.1f}s)", sync_menu),
        pystray.MenuItem("Opacity", opacity_menu),
        pystray.MenuItem("Font size", font_menu),
        pystray.MenuItem("Position", position_menu),
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
        pystray.MenuItem("Quit", _quit),
    )
    icon = pystray.Icon("desktop-karaoke", make_icon(), "Desktop Karaoke", menu)
    updater.background_check(_on_update_found)   # notify if a newer release exists (portable build)
    threading.Thread(target=icon.run, daemon=True).start()
    ov.run()


if __name__ == "__main__":
    main()
