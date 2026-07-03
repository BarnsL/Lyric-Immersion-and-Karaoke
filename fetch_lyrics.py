"""
Desktop Karaoke — lyric fetching, annotation, and verification.

═══════════════════════════════════════════════════════════════════════
SOURCES USED
  • LRCLIB        (https://lrclib.net)  — clean, open, returns track /
                  artist / duration metadata so matches can be VERIFIED.
                  Used first via /api/get (duration-exact) then /api/search.
  • syncedlyrics  (PyPI) — aggregates Musixmatch / NetEase / Megalobiz /
                  Genius. Great coverage for VTuber / anime / CJK songs
                  that LRCLIB lacks, but returns only an LRC string with
                  no metadata, so results MUST be verified heuristically.
                  NetEase in particular reliably carries the ORIGINAL kanji/
                  kana of Japanese songs — used to upgrade romaji-only uploads
                  (see _looks_romaji / _synced_cjk below).

PREFER ORIGINAL SCRIPT OVER ROMAJI
  Many LRCLIB uploads of Japanese songs are *romaji* (e.g. "sora kara maiorite"
  for 空から舞い降りて). Shown as-is that gives romaji with no kanji and no way to
  add furigana or a real translation. So fetch_lrc detects a romaji-only hit
  (_looks_romaji), stashes it, and tries to UPGRADE to the kanji/kana original
  (NetEase) first; only if no original-script version exists anywhere does it
  fall back to the romaji — and even then it still translates it to English.
  • fugashi + unidic-lite + cutlet — Japanese → furigana + romaji via a real
                  morphological analyzer. Segments correctly (今生きて →
                  今/生きて "ima ikite", not 今生 "konjou"), which is the single
                  biggest accuracy win for furigana/romaji. pykakasi is kept as
                  an automatic fallback when the analyzer isn't installed.
                  Katakana English is RECOVERED as English, not phoneticised:
                  cutlet's foreign-spelling mode plus gairaigo.py (a curated,
                  extensible katakana→English table) + _segment_katakana() split
                  run-together loanwords, so ベイビーアイラブユー → "baby I love
                  you" instead of "beibiiairabuyuu".
  • pypinyin      — Chinese → pinyin.
  • hangul-romanize — Korean → romaja.
  • deep-translator — line translation to English ('auto' source so it covers
                  ja / zh / ko / es / de / ru / fr / pt / it alike, plus
                  romanized-Japanese ('ja-romaji')). Uses the free Google
                  endpoint by default; if a DEEPL_API_KEY env var is set it
                  uses DeepL instead (noticeably better JP/CJK→EN). No key
                  required to run.
                  Lines are translated in CONTEXT WINDOWS (each block carries a
                  couple of neighbouring lines before/after) so a line is read in
                  the flow of the song, not in isolation. See _translate_lines.
  • Audio identification (recognize.py): soundcard (WASAPI loopback) +
                  shazamio (Shazam) — identifies the song by SOUND for covers
                  / mislabeled uploads. See recognize.py for details.

FUTURE / CANDIDATE SOURCES  (researched 2026-06; not yet wired — add here as
  providers for hard-to-find VTuber / indie / regional tracks). See
  docs/RESEARCH.md for the full investigation.
  • WORD-LEVEL (karaoke) timing. syncedlyrics accepts enhanced=True for
                  word-by-word <mm:ss.xx> tags, but the FREE providers
                  (LRCLIB/NetEase/Musixmatch-free) do NOT return it — tested on
                  JP + Western titles, all came back line-level. Real word-level
                  lives in QQ Music (qrc), Kugou (krc), NetEase (yrc) and Apple
                  Music, each needing a reverse-engineered/token endpoint. Wire
                  one of those to enable true per-word fill; until then the
                  renderer interpolates the fill across each line.
  • PetitLyrics (プチリリ) — large synced catalog for JP anime / VTuber /
                  doujin; best next addition for songs the aggregators miss.
  • animelyrics.com (via the `animelyrics` PyPI pkg) / Miraikyun — anime &
                  Vocaloid lyrics that ALREADY ship romaji + English, but only
                  PLAIN text (no per-line timing). Useful as a translation/romaji
                  cross-check, not for karaoke timing. NOTE: we don't actually
                  need these for romaji/EN — the analyzer + translator generate
                  them locally per line (see annotate / backfill_file), which
                  covers every song, not just charted anime.
  • QQ Music / Kugou — synced (incl. word-level) lyrics for Chinese + Asian pop.
  • Apple Music time-synced lyrics (needs an Apple Music API token).
  • BetterLyrics — TTML (word-level) provider seen in newer lyric tools.
  • Genius / AZLyrics / Uta-Net / J-Lyric — UNSYNCED only; usable as a
                  last-resort plain-text fallback (no karaoke timing).
  To add one: implement `def _provider(title, artist, duration) -> lrc|None`
  returning timed LRC, call it inside fetch_lrc() before returning None, and
  list it here.

PROBLEMS OVERCOME
  1. WRONG-SONG MATCHES. A bare title like "Lucky Star" matched a totally
     different song. Fix: prefer LRCLIB's duration-exact /api/get, score
     /api/search candidates on artist+title+duration, and for the opaque
     syncedlyrics fallback verify the result by song DURATION and LANGUAGE
     before accepting. Title-only queries are a last resort and still
     verified.
  2. WRONG-LANGUAGE / HALLUCINATED LYRICS. A Japanese song came back with
     Albanian text. Fix: detect_lang() on title + lyrics; if the title is
     CJK the lyrics must be the same script, else the result is rejected.
  3. CREDIT-LINE NOISE. Providers prefix "作词:" / "作曲:" etc. Filtered out.
  4. NetEase's public /api/search returns hot-charts garbage for foreign
     queries, so we do NOT call it directly — syncedlyrics handles it with
     its own signing, and we verify whatever it returns.
  5. NO PERSONAL DATA. Nothing here logs, stores, or transmits anything
     about the user, their account, or their machine — only public song
     title/artist strings are sent to public lyric APIs.
  6. WRONG FURIGANA / ROMAJI FROM NAIVE SEGMENTATION. pykakasi's longest-match
     read 今生きて as 今生(こんじょう) "konjou" instead of 今(いま)生き "ima ikite".
     Fix: use the fugashi + UniDic morphological analyzer (cutlet for romaji),
     place furigana only over the kanji, and nudge a few literary readings to
     their colloquial form (今日→きょう, 私→わたし). Older cache files are
     upgraded in place by `reannotate.py`.
═══════════════════════════════════════════════════════════════════════

Public API:
    fetch_and_save(title, artist, translate=False, duration=None) -> Path|None
    translate_file(path) -> bool
    validate_file(path, duration=None) -> (ok: bool, reason: str)
    detect_lang(text) -> 'ja'|'ko'|'zh'|'other'

CLI:
    python fetch_lyrics.py "Title" "Artist"
    python fetch_lyrics.py --lrc file.lrc "Title" "Artist"
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import confidence   # language_confidence — prefer the artist's usual language


# ── JP-act haystack regex (single source of truth = confidence._KNOWN_JA) ───
# fetch_lrc's ko/zh body rejection (jp_vagency) used to hand-code a short subset
# (regloss|hololive|holostars|kamitsubaki|神椿|v.w.p|花譜|理芽|ヰ世界情緒|harusaruhi|laplus)
# that drifted out of sync with confidence._KNOWN_JA — so a romanized credit like
# 'Hajime Todoroki' or 'Suisei' fetched a Korean body unrejected. Now compiled
# ONCE from _KNOWN_JA + the CJK-script forms the old regex carried (神椿/花譜/
# ヰ世界情緒/あんは) so the rejection covers every known JP act + future
# additions to _KNOWN_JA flow through automatically. Latin tokens word-boundaried
# so "rim" / "kaf" / "kobo" don't false-positive inside unrelated words.
_KNOWN_JA_CJK_EXTRAS = ("神椿", "花譜", "ヰ世界情緒", "あんは")

def _compile_jp_vagency_re():
    parts = []
    for k in (*getattr(confidence, "_KNOWN_JA", ()), *_KNOWN_JA_CJK_EXTRAS):
        esc = re.escape(k)
        if all(ord(c) < 128 for c in k):
            parts.append(rf"\b{esc}\b")    # Latin: require word boundary
        else:
            parts.append(esc)              # CJK: bare (Python \b is Latin-only)
    return re.compile("|".join(parts), re.I) if parts else None

_JP_VAGENCY_RE = _compile_jp_vagency_re()


def is_jp_vagency(title: str, artist: str, extras=None, *,
                  strict: bool = False) -> bool:
    """Public helper: True when title/artist (+ optional extras like split
    artists, yt_description vocals) tag this as a Japanese-language song.
    Independent signals (any one trips it):
      1. _JP_VAGENCY_RE (the _KNOWN_JA regex — Latin act names like ReGLOSS,
         hololive, Suisei, Hajime, Reol, Kanaria …).
      2. Kana anywhere in title/artist — uniquely Japanese (Korean is hangul,
         Chinese has neither kana nor hangul). Closes the Hajime/轟はじめ gap.
      3. (strict=False only) Kanji anywhere AND no hangul — Korean lyrics
         are written in hangul, so a kanji-only artist is unlikely to be a
         Korean song; signals JP for kanji-only acts like 音乃瀬奏 (Kanade).
         Pass strict=True when discriminating against a ZH body — pure kanji
         is ambiguous JP↔ZH, and signal 3 would wrongly tag legit Chinese
         songs like 孤勇者 / 陈奕迅.
    Hangul anywhere in artist suppresses (a genuine Korean entry by a JP-
    affiliated artist would carry hangul itself).

    Use strict=True for ZH rejection paths. Use the default for KO and
    European-language rejection paths (where signal 3 has no false positive)."""
    parts = [s for s in (title or "", artist or "", *(extras or [])) if s]
    # Hangul ANYWHERE in the context (title, artist, OR extras like the raw
    # player title) suppresses rejection — a song whose own metadata carries
    # hangul is Korean or bilingual (e.g. TAK "PPPP" feat 하츠네 미쿠, which is
    # JP + Korean), so its Korean lyrics are CORRECT and must not be rejected.
    if any(_HANGUL.search(p) for p in parts):
        return False
    hay = " ".join(parts)
    # Signal 1: known JP-act name.
    if _JP_VAGENCY_RE and _JP_VAGENCY_RE.search(hay):
        return True
    # Signal 2: kana anywhere (uniquely JP).
    if any(_KANA.search(p) for p in parts):
        return True
    # Signal 3: kanji + no hangul. Skipped in strict mode because kanji alone
    # is shared JP↔ZH (Chinese songs would otherwise be tagged JP).
    if not strict and any(_HAN.search(p) for p in parts):
        return True
    return False


# ── TICKET-113: lyric-body signature for the per-track blacklist ─────
# Defined ONCE at module top so the capture site (main.py) and the
# rejection site (the take() chokepoint in fetch_lrc) use byte-identical
# normalization — if they drift apart the blacklist silently stops working.
# Strips [..] timestamp tags and collapses whitespace so the same lyrics file
# from two providers (or the same provider with a tweaked sync offset)
# hashes identically and gets rejected consistently.
def _lrc_signature(lrc: str) -> str:
    """SHA-1 of the normalized lyric body. lrc may be a full LRC string OR
    plain-text lines (so callers without the original LRC — e.g. the App
    reading self.lines — can still produce the same signature)."""
    if not lrc:
        return ""
    body = re.sub(r"\[[^\]]*\]", "", lrc)          # drop [mm:ss.xx] timestamps
    body = re.sub(r"<\d+:\d+(?:\.\d+)?>", "", body)  # drop word-level <tags>
    body = re.sub(r"\s+", " ", body).strip()
    return hashlib.sha1(body.encode("utf-8")).hexdigest()

# syncedlyrics logs noisy provider warnings (e.g. Musixmatch 401) — quiet them
for _n in ("syncedlyrics", "syncedlyrics.providers"):
    logging.getLogger(_n).setLevel(logging.CRITICAL)

log = logging.getLogger(__name__)

# -- Musixmatch token-refresh resilience patch ------------------------------------
# The upstream syncedlyrics Musixmatch provider retries infinitely on 401 and
# doesn't auto-purge revoked tokens. This monkey-patch adds:
#  - A 3-retry cap on token.get (prevents infinite recursion)
#  - Auto-purge + single retry on 401 during track.search
def _patch_musixmatch_token_refresh():
    """Make Musixmatch auto-refresh its token when revoked server-side."""
    try:
        from syncedlyrics.providers.musixmatch import Musixmatch
        from syncedlyrics.utils import get_cache_path
        import json as _json, time as _time

        def _robust_get_token(self, _retries=0):
            token_path = get_cache_path('syncedlyrics', False) / 'musixmatch_token.json'
            current_time = int(_time.time())
            if token_path.exists():
                with open(token_path, 'r') as f:
                    cached = _json.load(f)
                if cached.get('token') and current_time < cached.get('expiration_time', 0):
                    self.token = cached['token']
                    return
            d = self._get('token.get', [('user_language', 'en')]).json()
            if d['message']['header']['status_code'] == 401:
                if _retries >= 2:
                    if token_path.exists():
                        token_path.unlink()
                    return
                _time.sleep(10)
                return _robust_get_token(self, _retries + 1)
            new_token = d['message']['body']['user_token']
            self.token = new_token
            token_path.parent.mkdir(parents=True, exist_ok=True)
            with open(token_path, 'w') as f:
                _json.dump({'token': new_token, 'expiration_time': current_time + 600}, f)

        def _robust_get_lrc(self, search_term, _refreshed=False):
            r = self._get('track.search', [('q', search_term), ('page_size', '5'), ('page', '1')])
            status_code = r.json()['message']['header']['status_code']
            if status_code == 401 and not _refreshed:
                token_path = get_cache_path('syncedlyrics', False) / 'musixmatch_token.json'
                if token_path.exists():
                    token_path.unlink()
                self.token = None
                self._get_token()
                return _robust_get_lrc(self, search_term, _refreshed=True)
            if status_code != 200:
                return None
            body = r.json()['message']['body']
            if not isinstance(body, dict):
                return None
            from syncedlyrics.utils import get_best_match
            tracks = body['track_list']
            cmp_key = lambda t: t['track']['track_name'] + ' ' + t['track']['artist_name']
            track = get_best_match(tracks, search_term, cmp_key)
            if not track:
                return None
            track_id = track['track']['track_id']
            if self.enhanced:
                lrc = self.get_lrc_word_by_word(track_id)
                if lrc and lrc.synced:
                    return lrc
            return self.get_lrc_by_id(track_id)

        Musixmatch._get_token = _robust_get_token
        Musixmatch.get_lrc = _robust_get_lrc
    except ImportError:
        pass  # syncedlyrics not installed

_patch_musixmatch_token_refresh()


if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Same writable location the overlay uses (next to the .exe when portable,
# %LOCALAPPDATA%\DesktopKaraoke when installed via MSIX). See appdata.py.
from appdata import data_dir
LYRICS_DIR = data_dir() / "lyrics"

# Script ranges
_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
_KANA   = re.compile(r"[぀-ゟ゠-ヿ]")
_HAN    = re.compile(r"[一-鿿㐀-䶿々]")
_CYRILLIC = re.compile(r"[а-яё]", re.I)
_GREEK  = re.compile(r"[Ͱ-Ͽἀ-ῼ]")
_KANJI  = r"[一-鿿㐀-䶿々]"
_JP_RE  = re.compile(r"[ぁ-んァ-ヶー一-鿿々]")
_CREDIT_RE = re.compile(
    r"^\s*(作词|作詞|作曲|编曲|編曲|制作|製作|制作人|製作人|监制|監製|混音|母带|母帶|"
    r"和声|和聲|录音|錄音|出品|发行|發行|策划|策劃|"
    r"Produced|Producer|Lyricist|Lyrics?|Composer|Arrang|Mixing|Mix|Master|"
    r"Vocal|Music|Words|Guitar|Bass|Drums)\b\s*[:：]",
    re.I,
)


_ES_DIA = re.compile(r"[ñáéíóúü¿¡]", re.I)
_ES_WORDS = {
    # function words (unaccented forms too — corridos rarely use accents)
    "que", "qué", "como", "cómo", "pero", "porque", "para", "por", "con", "sin",
    "una", "uno", "unos", "unas", "los", "las", "del", "este", "esta", "esto",
    "ese", "esa", "eso", "esos", "esas", "mas", "más", "muy", "tan", "donde",
    "dónde", "cuando", "cuándo", "quien", "aqui", "aquí", "alli", "allá", "asi",
    "así", "tambien", "también", "siempre", "nunca", "todo", "todos", "toda",
    "nada", "algo", "mucho", "poco", "bien", "mal", "ya", "aunque",
    # verbs / pronouns
    "soy", "eres", "está", "están", "estoy", "es", "son", "tengo", "tiene",
    "tienes", "tenemos", "quiero", "quieres", "vamos", "voy", "vas", "ven",
    "dame", "dime", "mira", "siento", "puedo", "hacer", "decir", "amar",
    "él", "ella", "ellos", "nosotros", "tú", "tu", "mi", "mis", "tus", "su",
    "sus", "me", "te", "le", "nos", "lo",
    # lyric nouns
    "corazón", "corazon", "vida", "amor", "mujer", "hombre", "noche", "día",
    "sol", "luna", "cielo", "tierra", "señor", "dios", "sangre", "fuego",
    "calle", "dinero", "plata", "amigo", "amiga", "hermano", "jefe", "patrón",
    "compa", "plebe", "morena", "morra", "morro", "carnal", "sancho", "cuentes",
}


def is_japanese(text: str) -> bool:
    return bool(_JP_RE.search(text))


def _is_spanish(text: str) -> bool:
    if _ES_DIA.search(text):
        return True
    words = set(re.findall(r"[a-zñáéíóúü]+", text.lower()))
    return len(words & _ES_WORDS) >= 2


# German: umlauts / ß are a strong signal; otherwise common function words.
_DE_DIA = re.compile(r"[äöüß]", re.I)
_DE_WORDS = {
    "und", "ich", "nicht", "das", "ist", "ein", "eine", "der", "die", "den",
    "dem", "du", "wir", "ihr", "sie", "mit", "auf", "für", "von", "zu", "im",
    "sich", "auch", "war", "sind", "haben", "wird", "werden", "kann", "mein",
    "dein", "dich", "mich", "uns", "wenn", "aber", "doch", "noch", "schon",
    "nur", "über", "ohne", "alles", "nichts", "immer", "wieder", "mehr", "ja",
    "nein", "herz", "liebe", "nacht", "leben", "welt", "feuer", "engel",
    "will", "wollen", "wollte", "kommt", "kommen", "geht", "gehen", "sehen",
    "weiter", "warum", "deine", "meine", "keine", "wie", "wo", "was", "wer",
    "hast", "habe", "bist", "weil", "dann", "hier", "sehr", "gut", "böse",
    "sonne", "regen", "wasser", "blut", "tod", "angst", "schmerz", "weiß",
}


def _is_german(text: str) -> bool:
    if _DE_DIA.search(text):
        return True
    words = set(re.findall(r"[a-zäöüß]+", text.lower()))
    return len(words & _DE_WORDS) >= 2


def detect_lang(text: str) -> str:
    """Coarse language of a string/lyric by dominant script / markers →
    ja|ko|zh|ru|el|es|de|other."""
    hang = len(_HANGUL.findall(text))
    kana = len(_KANA.findall(text))
    han = len(_HAN.findall(text))
    cyr = len(_CYRILLIC.findall(text))
    grk = len(_GREEK.findall(text))
    if grk and grk >= max(kana, han, hang, cyr):
        return "el"
    if cyr and cyr >= max(kana, han, hang):
        return "ru"
    if hang and hang >= kana:
        return "ko"
    if kana:
        return "ja"
    if han:
        return "zh"
    # Spanish vs German share short function words, so score both (diacritics
    # count double) and pick the stronger rather than first-match.
    if _is_spanish(text) or _is_german(text):
        words = set(re.findall(r"[a-zñáéíóúüäöüß]+", text.lower()))
        es = (2 if _ES_DIA.search(text) else 0) + len(words & _ES_WORDS)
        de = (2 if _DE_DIA.search(text) else 0) + len(words & _DE_WORDS)
        return "de" if de > es else "es"
    return "other"


# ── Romaji (romanized Japanese) detection ────────────────────────────
# Some providers (and many LRCLIB uploads) carry a *romaji* transliteration
# instead of the original kanji/kana — e.g. "sora kara maiorite" for 空から舞い降りて.
# We detect that so we can prefer the original-script version (which then gets
# proper furigana + romaji + translation) and never show romaji-only by mistake.
_EN_STOP = frozenset("""
    the a an and or but you your my me we is are be been to of in on for with this
    that it its all we love can will would could should about there here just like
    dont cant wont im ive lets what when where why how not no yes oh yeah baby
    starlight future tonight forever everything beautiful heart light dream world
    night sky time girl boy fly away into out down up never always
""".split())
# Tokens that are unmistakably romanized Japanese — they don't occur as words in
# Spanish/German/Italian/English, so a couple of them confirm romaji (vs. just
# "vowel-rich Latin", which Spanish also is).
_ROMAJI_MARK = frozenset("""
    wa wo youna desu masu kimi boku watashi anata kokoro yume tsunagu tsunaide
    hikari kaze namida koe sayonara arigatou yasashii kanashii ureshii itsumo
    itsuka doushite naze maiorite miseteku misete iroaseru egaite kakete moyou
    daisuki aishiteru suki naku naite naide yuku kimochi kanjiru sugiru darou
    deshou nano dakara kedo keredo zutto kitto sotto futari hitori
""".split())


def _romaji_word(w: str) -> bool:
    """Structural test: looks like a run of Japanese morae (open CV syllables,
    ends on a vowel or n, none of the letters rare in romaji)."""
    if not re.fullmatch(r"[a-z]+", w) or len(w) < 2:
        return False
    if re.search(r"[lqxcv]", w):                  # rare in Hepburn romaji
        return False
    if not (w[-1] in "aeiou" or w.endswith("n")):
        return False
    vowels = sum(c in "aeiou" for c in w)
    return vowels >= len(w) * 0.34


def _looks_romaji(text: str) -> bool:
    """True if Latin text is really romanized Japanese — so we should hunt for the
    original kanji/kana version. False for English / Spanish / German / actual CJK.
    Needs BOTH a high fraction of mora-shaped words AND a couple of unmistakably
    Japanese tokens, so vowel-rich Romance text isn't misread as romaji."""
    if detect_lang(text) != "other":
        return False                              # real CJK/Cyrillic/Spanish/German
    words = [w for w in re.findall(r"[a-z']+", text.lower()) if len(w) > 1]
    if len(words) < 6:
        return False
    eng = sum(1 for w in words if w in _EN_STOP)
    if eng > len(words) * 0.30:
        return False                              # mostly English → it's English
    structural = sum(1 for w in words if _romaji_word(w))
    marks = sum(1 for w in set(words) if w in _ROMAJI_MARK)
    return structural >= len(words) * 0.6 and marks >= 2


def slugify(title: str) -> str:
    return re.sub(r"[^\w぀-ヿ가-힣一-鿿]+", "_", title.lower()).strip("_")


def split_artists(artist: str) -> list[str]:
    """Break 'PeanutsKun, Ikuta Rira feat. X' → ['PeanutsKun','Ikuta Rira','X']."""
    parts = re.split(r"\s*[,/&、，]\s*|\s+(?:feat|ft|featuring|with|×|x)\.?\s+",
                     artist or "", flags=re.I)
    out, seen = [], set()
    for p in parts:
        p = p.strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def _joined_artist_variants(artist: str) -> list[str]:
    """Provider-friendly no-space variants for stylized unit names.

    Some catalogs index acts like 'Re GLOSS' under the branded single token
    'ReGLOSS'. Keep the heuristic narrow so ordinary names ('Taylor Swift')
    don't double the query fan-out for no benefit.
    """
    a = re.sub(r"\s+", " ", (artist or "").strip())
    if " " not in a:
        return []
    toks = a.split()
    if not (2 <= len(toks) <= 4):
        return []
    if not all(re.fullmatch(r"[A-Za-z0-9]+", tok) for tok in toks):
        return []
    stylized = any(tok.isupper() for tok in toks) or any(len(tok) <= 2 for tok in toks)
    if not stylized:
        return []
    joined = "".join(toks)
    return [joined] if joined.lower() != a.lower() else []


# Agency / label PREFIXES that wrap the real performing UNIT. Lyric providers
# index the UNIT name ("ReGLOSS", "FLOW GLOW") — NOT the full agency credit
# ("hololive DEV_IS ReGLOSS") that SMTC delivers — so a fetch with the raw
# artist string finds NOTHING while the unit-only query hits instantly.
# VERIFIED LIVE: fetch_lrc('Flashpoint', 'hololive DEV_IS ReGLOSS') was EMPTY
# after 44s, but ('Flashpoint', 'ReGLOSS') returned the correct 53-line
# Japanese LRC in 8.5s. clean_artist() only strips an agency token that follows
# a DASH ('‐ ReGLOSS'); the space-separated PREFIX form is what leaks through.
# Ordered LONGEST-first so 'hololive DEV_IS' is matched before bare 'hololive'.
_AGENCY_PREFIXES = (
    "hololive dev_is", "hololive dev is", "hololive english -justice-",
    "hololive english", "hololive indonesia", "hololive justice",
    "hololive en", "hololive id", "hololive jp", "hololive",
    "holostars english", "holostars uprising", "holostars en", "holostars",
    "kamitsubaki studio", "kamitsubaki", "phase connect", "phase-connect",
    "phaseconnect", "nijisanji en", "nijisanji", "vshojo", "vspo en", "vspo",
    "ぶいすぽっ！", "ぶいすぽ",
)


def agency_unit_names(artist: str) -> list[str]:
    """Derive the performing UNIT name from an agency-prefixed credit:
    'hololive DEV_IS ReGLOSS' → ['ReGLOSS']; 'hololive DEV_IS FLOW GLOW' →
    ['FLOW GLOW']. Returns [] when the artist isn't agency-prefixed (a song
    genuinely credited to the whole label is left untouched). Longest prefix
    wins so the sub-label is stripped along with the parent."""
    a = (artist or "").strip()
    al = a.lower()
    for pre in _AGENCY_PREFIXES:
        if al.startswith(pre + " "):
            rest = a[len(pre):].strip(" -–—‐:·・|/").strip()
            if rest and rest.lower() != al:
                return [rest]
            break
    return []


def _artist_query_candidates(artist: str) -> list[str]:
    """Prioritized artist variants for provider lookups.

    Lead with the compact / unit form providers are most likely to index under,
    then fall back to the original credit. Example: 'Re GLOSS' →
    ['ReGLOSS', 'Re GLOSS']; 'hololive DEV_IS ReGLOSS' →
    ['ReGLOSS', 'hololive DEV_IS ReGLOSS'].
    """
    a = re.sub(r"\s+", " ", (artist or "").strip())
    out, seen = [], set()

    def add(name: str):
        name = re.sub(r"\s+", " ", (name or "").strip())
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)

    def add_preferred(name: str):
        for v in _joined_artist_variants(name):
            add(v)
        add(name)

    for unit in agency_unit_names(a):
        add_preferred(unit)
    for part in split_artists(a):
        add_preferred(part)
    add_preferred(a)
    return out


# ── Romanization ─────────────────────────────────────────────────────

_kks = None
_translit = None
_jp_tagger = None
_jp_katsu = None
_JP_READY = None       # tri-state: None=untried, True=analyzer up, False=fallback

# UniDic occasionally prefers a literary reading over the everyday one heard in
# song lyrics. Nudge the most frequent offenders back to the colloquial form so
# furigana and romaji agree and read naturally.
_READING_FIX = {
    "今日": "きょう", "私": "わたし", "明日": "あした", "昨日": "きのう",
    "貴方": "あなた", "何故": "なぜ", "一人": "ひとり", "二人": "ふたり",
}
_ROMAJI_FIX = {
    "今日": "kyou", "私": "watashi", "明日": "ashita", "昨日": "kinou",
    "貴方": "anata", "何故": "naze", "一人": "hitori", "二人": "futari",
}


def _is_hira(c: str) -> bool:
    return "ぁ" <= c <= "ゟ"


def _jp_engine():
    """Lazy-init fugashi (morphological analyzer) + cutlet (romaji). Returns
    True when the real analyzer is available; False means pykakasi fallback.
    The analyzer segments correctly (今生きて → 今/生きて, not 今生), which is
    the single biggest accuracy win for Japanese furigana + romaji."""
    global _jp_tagger, _jp_katsu, _JP_READY
    if _JP_READY is not None:
        return _JP_READY
    try:
        import fugashi
        import cutlet
        from gairaigo import KATAKANA_EN
        _jp_tagger = fugashi.Tagger()
        _jp_katsu = cutlet.Cutlet()
        # use_foreign_spelling=True renders known loanwords as English
        # (コンピューター→computer, スマイル→smile) instead of phonetic romaji.
        _jp_katsu.use_foreign_spelling = True
        # Our curated katakana→English overrides take priority over cutlet's
        # (which gets アイ→"eye", ミー→"Mi-", グッバイ→"Gubbai" wrong), plus the
        # everyday-reading fixes for kanji.
        for surf, rom in {**_ROMAJI_FIX, **KATAKANA_EN}.items():
            try:
                _jp_katsu.add_exception(surf, rom)
            except Exception:
                pass
        _JP_READY = True
    except Exception:
        _JP_READY = False
    return _JP_READY


# Longest katakana loanword first → greedy segmentation of run-together strings.
_KATA_RUN = re.compile(r"[ァ-ヶ]{2,}ー?|[ァ-ヶ][ァ-ヶー]+")


def _segment_katakana(text: str) -> str:
    """Insert spaces into run-together katakana English so the romanizer can
    resolve each loanword: ベイビーアイラブユー → 'ベイビー アイ ラブ ユー'.

    A run is split ONLY when it tiles ENTIRELY into known gairaigo words. That
    safety rule is essential: ノー ("no") is a known word, but ノート ("note")
    must NOT be broken into ノー+ト — and it isn't, because the leftover ト
    leaves no full tiling, so ノート is left intact for cutlet ("note"). Same
    for アイス (ice), アイドル (idol), etc."""
    from gairaigo import KATAKANA_EN, KATA_NORMALIZE
    for _styl, _std in KATA_NORMALIZE.items():
        if _styl in text:
            text = text.replace(_styl, _std)   # ラービュー -> ラブユー so the tiling recovers it
    keys = KATAKANA_EN

    def full_tiling(run: str):
        # DP: shortest sequence of dict words that covers the whole run, else None
        n = len(run)
        best = [None] * (n + 1)
        best[0] = []
        for i in range(n):
            if best[i] is None:
                continue
            for j in range(i + 2, min(n, i + 10) + 1):
                if run[i:j] in keys and (best[j] is None
                                         or len(best[j]) > len(best[i]) + 1):
                    best[j] = best[i] + [run[i:j]]
        return best[n]

    def repl(m):
        run = m.group(0)
        parts = full_tiling(run)
        return " ".join(parts) if parts else run

    return _KATA_RUN.sub(repl, text)


def _kakasi():
    global _kks
    if _kks is None:
        import pykakasi
        _kks = pykakasi.kakasi()
    return _kks


def _korean():
    global _translit
    if _translit is None:
        from hangul_romanize import Transliter
        from hangul_romanize.rule import academic
        _translit = Transliter(academic)
    return _translit


def _tok_reading(w) -> str:
    """Hiragana reading for a fugashi token (or '' when it has none)."""
    import jaconv
    f = w.feature
    for attr in ("kana", "pron"):
        v = getattr(f, attr, None)
        if v and v != "*":
            return jaconv.kata2hira(v)
    return ""


def _furi_pair(surf: str, kana: str) -> str:
    """Format one token as furigana, placing the reading only over the kanji by
    trimming shared kana on either side (e.g. 生き/いき → 生(い)き)."""
    if surf in _READING_FIX:
        kana = _READING_FIX[surf]
    head = tail = ""
    s, k = surf, kana
    while s and k and _is_hira(s[-1]) and s[-1] == k[-1]:
        tail, s, k = s[-1] + tail, s[:-1], k[:-1]
    while s and k and _is_hira(s[0]) and s[0] == k[0]:
        head, s, k = head + s[0], s[1:], k[1:]
    if s and k and re.search(_KANJI, s):
        return f"{head}{s}({k}){tail}"
    return surf


def to_furigana(text: str) -> str:
    """Annotate Japanese text with furigana as ``漢字(かな)`` (readings sit only
    over the kanji). Uses the fugashi analyzer when available, else pykakasi."""
    if _jp_engine():
        try:
            # fugashi DROPS whitespace between tokens, which squished English
            # phrases ("Summer sun" → "Summersun"). Process each whitespace-
            # separated chunk and rejoin with the original spaces preserved.
            out = []
            for chunk in re.split(r"(\s+)", text):
                if not chunk or chunk.isspace():
                    out.append(chunk)
                    continue
                for w in _jp_tagger(chunk):
                    surf = w.surface
                    out.append(_furi_pair(surf, _tok_reading(w))
                               if re.search(_KANJI, surf) else surf)
            return "".join(out)
        except Exception:
            pass
    out = []                                    # pykakasi fallback
    for item in _kakasi().convert(text):
        orig, hira = item["orig"], item["hira"]
        if orig != hira and re.search(_KANJI, orig):
            out.append(f"{orig}({hira})")
        else:
            out.append(orig)
    return "".join(out)


# Cyrillic → Latin (BGN/PCGN-ish) for a readable Russian transliteration.
_RU_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _translit_ru(text: str) -> str:
    out = []
    for ch in text:
        low = ch.lower()
        if low in _RU_MAP:
            r = _RU_MAP[low]
            out.append((r[0].upper() + r[1:]) if ch.isupper() and r else r)
        else:
            out.append(ch)
    return "".join(out)


# Greek → Latin (modern ELOT-ish) so Greek lyrics get a readable romanization,
# same guarantee as ja/zh/ko/ru: any non-Latin script line carries an `rm` row.
_EL_MAP = {
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i",
    "θ": "th", "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x",
    "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y",
    "φ": "f", "χ": "ch", "ψ": "ps", "ω": "o",
}


def _translit_el(text: str) -> str:
    import unicodedata
    # strip accents/breathings first (ά→α, ῆ→η) so the letter map covers everything
    text = "".join(c for c in unicodedata.normalize("NFD", text)
                   if not unicodedata.combining(c))
    text = text.replace("ου", "ou").replace("Ου", "Ou").replace("ΟΥ", "OU")
    out = []
    for ch in text:
        low = ch.lower()
        if low in _EL_MAP:
            r = _EL_MAP[low]
            out.append((r[0].upper() + r[1:]) if ch.isupper() and r else r)
        else:
            out.append(ch)
    return "".join(out)


# Cache for zh pinyin results, keyed by line text. Capped to avoid unbounded
# growth on long sessions; jieba.lcut + pypinyin per line is cheap but not free.
_ZH_PINYIN_CACHE: dict[str, str] = {}
_ZH_PINYIN_CACHE_MAX = 256


def _zh_pinyin(text: str) -> str:
    """Chinese pinyin with jieba word segmentation. Characters within a word are
    concatenated (no space), words are joined with single spaces. Falls back to
    per-character lazy_pinyin if jieba isn't installed."""
    cached = _ZH_PINYIN_CACHE.get(text)
    if cached is not None:
        return cached
    from pypinyin import lazy_pinyin, Style
    try:
        import jieba
        segments = jieba.lcut(text)
        parts = []
        for seg in segments:
            if not seg:
                continue
            parts.append("".join(lazy_pinyin(seg, style=Style.TONE)))
        result = " ".join(p for p in parts if p).strip()
    except ImportError:
        result = " ".join(lazy_pinyin(text, style=Style.TONE)).strip()
    # naive cap: drop oldest insertion when full (dict preserves insertion order)
    if len(_ZH_PINYIN_CACHE) >= _ZH_PINYIN_CACHE_MAX:
        try:
            _ZH_PINYIN_CACHE.pop(next(iter(_ZH_PINYIN_CACHE)))
        except StopIteration:
            pass
    _ZH_PINYIN_CACHE[text] = result
    return result


# ── Cantonese (jyutping) ────────────────────────────────────────────────────
# Cantonese and Mandarin share Han characters, so a script-only detector cannot
# tell them apart. Default to Mandarin PINYIN; switch a song to JYUTPING only on
# positive Cantonese evidence: >=2 distinct Cantonese-only colloquial characters
# in the body, an explicit 粵語/Cantonese tag, or a known Cantopop artist.
# Conservative on purpose so a stray marker can't flip a Mandarin song.
_CANTO_MARKERS = set("嘅唔喺咗佢冇睇嗰乜嘢啲哋嚟攞諗咁㗎喎嘞嗮閪咩嘥郁攰")
_CANTO_TAGS = ("粵語", "粤语", "廣東話", "广东话", "cantonese")
_CANTO_ARTISTS = ("beyond", "eason chan", "陳奕迅", "陈奕迅", "mirror", "容祖兒",
                  "容祖儿", "張國榮", "张国荣", "譚詠麟", "谭咏麟", "李克勤",
                  "古巨基", "謝霆鋒", "谢霆锋", "張學友", "张学友", "陳慧嫻",
                  "twins", "rubberband", "dear jane", "my little airport")
_ZH_JYUTPING_CACHE: dict[str, str] = {}


def _is_cantonese(body: str, artist: str = "", title: str = "") -> bool:
    meta = (artist or "") + "  " + (title or "")
    low = meta.lower()
    if any(t in meta or t in low for t in _CANTO_TAGS):
        return True
    if any(a in low for a in _CANTO_ARTISTS):
        return True
    return sum(1 for m in _CANTO_MARKERS if m in body) >= 2


def _zh_jyutping(text: str) -> str:
    """Cantonese jyutping via ToJyutping (pure-Python, own trie). Joins the
    non-None syllables with spaces — same shape as the pinyin path. Falls back to
    Mandarin pinyin if the library is missing so a build without it still shows a
    romanization rather than nothing."""
    cached = _ZH_JYUTPING_CACHE.get(text)
    if cached is not None:
        return cached
    try:
        import ToJyutping
        pairs = ToJyutping.get_jyutping_list(text)
        result = " ".join(jp for _ch, jp in pairs if jp).strip()
    except Exception:
        result = _zh_pinyin(text)
    if len(_ZH_JYUTPING_CACHE) >= _ZH_PINYIN_CACHE_MAX:
        try:
            _ZH_JYUTPING_CACHE.pop(next(iter(_ZH_JYUTPING_CACHE)))
        except StopIteration:
            pass
    _ZH_JYUTPING_CACHE[text] = result
    return result


def romanize(text: str, lang: str) -> str:
    """Romanize text for the given language: Japanese → Hepburn romaji (fugashi +
    cutlet, katakana English recovered as English), Chinese → pinyin, Korean →
    romaja, Russian → Latin transliteration. Returns '' on failure or an
    unsupported language (German/Spanish/English are already Latin)."""
    try:
        if lang == "ru":
            return _translit_ru(text)
        if lang == "el":
            return _translit_el(text)              # Greek → Latin reading
        if lang == "ja":
            if _jp_engine():
                try:
                    # split run-together katakana English first so the loanwords
                    # render as English (ベイビーアイラブユー → baby I love you)
                    r = _jp_katsu.romaji(_segment_katakana(text)).strip()
                    if r:
                        return r[0].lower() + r[1:]   # match the lowercase style
                except Exception:
                    pass
            return " ".join(it["hepburn"] for it in _kakasi().convert(text)).strip()
        if lang == "yue":
            return _zh_jyutping(text)               # Cantonese → jyutping
        if lang == "zh":
            return _zh_pinyin(text)                 # Mandarin → pinyin
        if lang == "ko":
            return _korean().translit(text).replace("-", "").strip()
    except Exception:
        return ""
    return ""


# ── LRC parsing ──────────────────────────────────────────────────────

def parse_lrc_text(lrc: str) -> list[dict]:
    raw = []
    for line in lrc.splitlines():
        tags = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", line)
        if not tags:
            continue
        # strip ALL [mm:ss] line tags and <mm:ss> word tags from the text
        text = re.sub(r"\[\d+:\d+(?:\.\d+)?\]", "", line)
        text = re.sub(r"<\d+:\d+(?:\.\d+)?>", "", text).strip()
        for mm, ss in tags:                       # a line may repeat at several times
            raw.append({"time": round(int(mm) * 60 + float(ss), 2), "text": text})
    raw.sort(key=lambda x: x["time"])

    out = []
    for i, ln in enumerate(raw):
        if not ln["text"] or _CREDIT_RE.search(ln["text"]):
            continue
        end = raw[i + 1]["time"] if i + 1 < len(raw) else ln["time"] + 5.0
        out.append({"t": [ln["time"], round(end, 2)], "jp": ln["text"], "rm": "", "en": ""})
    return out


def _lrc_last_time(lrc: str) -> float:
    times = [int(m.group(1)) * 60 + float(m.group(2))
             for m in re.finditer(r"\[(\d+):(\d+(?:\.\d+)?)\]", lrc)]
    return max(times) if times else 0.0


# ── Verification (error detection) ───────────────────────────────────

def verify_lrc(lrc: str, title: str, duration: float | None) -> bool:
    """Reject lyrics that clearly don't belong to the requested song."""
    body = re.sub(r"\[[^\]]*\]", "", lrc)
    if len(body.strip()) < 10:
        return False
    # Language: if the title is CJK, the lyrics must share that script
    tl = detect_lang(title)
    if tl in ("ja", "ko", "zh"):
        ll = detect_lang(body)
        if ll != tl and not (tl == "zh" and ll == "ja"):
            return False
    # Duration: last timestamp should land within the song, not way past it
    if duration and duration > 30:
        last = _lrc_last_time(lrc)
        if last and (last < duration * 0.35 or last > duration + 45):
            return False
    return True


# ── LRCLIB (verifiable) ──────────────────────────────────────────────

def _http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Desktop-Karaoke/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _lrclib_get(title, artist, duration):
    if not (artist and duration):
        return None
    q = urllib.parse.urlencode({
        "track_name": title, "artist_name": artist, "duration": int(duration),
    })
    try:
        d = _http_json(f"https://lrclib.net/api/get?{q}")
        if d.get("syncedLyrics"):
            return {"lrc": d["syncedLyrics"], "artist": d.get("artistName", artist),
                    "duration": d.get("duration", duration)}
    except Exception:
        pass
    return None


def _lrclib_candidates(title, artist, arts=None):
    cands, seen = [], set()
    urls = [f"https://lrclib.net/api/search?{urllib.parse.urlencode({'q': f'{title} {artist}'.strip()})}"]
    # When fetch_lrc supplies `arts`, it already carries the agency-UNIT name
    # ('ReGLOSS') at the front + YT-description vocalists — query THOSE so the
    # form providers actually index is searched, not just the full agency
    # credit ('hololive DEV_IS ReGLOSS') that returns nothing. (Verified: this is
    # what gets Flashpoint to lrclib in ~8s instead of the 27s syncedlyrics fallback.)
    cand_artists = arts if arts is not None else ([artist] + split_artists(artist))
    for ar in cand_artists[:4]:
        if ar:
            urls.append("https://lrclib.net/api/search?"
                        + urllib.parse.urlencode({"track_name": title, "artist_name": ar}))
    for u in urls:
        try:
            for r in _http_json(u):
                rid = r.get("id")
                if rid in seen or not r.get("syncedLyrics"):
                    continue
                seen.add(rid)
                cands.append(r)
        except Exception:
            continue
    return cands


def _norm(s):
    return re.sub(r"[^\w가-힣一-鿿ぁ-ヶ]+", "", (s or "").lower())


def _pick_lrclib(title, artist, duration, arts=None):
    best, best_score = None, 0
    nt = _norm(title)
    # Score against the agency-unit + YT-vocalist candidate list when supplied,
    # so a hit indexed under 'ReGLOSS' substring-matches and clears the 4-pt
    # threshold (the full 'hololive dev_is regloss' credit would not).
    _nas_src = arts if arts is not None else split_artists(artist)
    nas = [_norm(x) for x in _nas_src] or [_norm(artist)]
    for c in _lrclib_candidates(title, artist, arts=arts):
        ct, ca = _norm(c.get("trackName")), _norm(c.get("artistName"))
        score = 0
        if nt and (nt in ct or ct in nt):
            score += 2
        if any(na and (na in ca or ca in na) for na in nas):
            score += 3
        if duration and c.get("duration"):
            if abs(c["duration"] - duration) <= 8:
                score += 3
            elif abs(c["duration"] - duration) > 25:
                score -= 3
        if score > best_score:
            best, best_score = c, score
    if best and best_score >= 4:
        return {"lrc": best["syncedLyrics"], "artist": best.get("artistName", artist),
                "duration": best.get("duration")}
    return None


# ── Multi-provider fetch with verification ───────────────────────────

def _strict_ok(lrc: str, title: str, duration: float | None) -> bool:
    """Extra guard for low-confidence (title-only) matches to cut false
    positives: only trust them when language gating or duration can confirm."""
    if detect_lang(title) in ("ja", "ko", "zh"):
        return True                        # language gate already applied
    if duration:
        last = _lrc_last_time(lrc)
        return bool(last and abs(last - duration) <= max(20, duration * 0.15))
    return False                           # Latin title, no duration → don't risk it


def _lrc_artist_conflict(lrc: str, want_artist: str) -> bool:
    """(TICKET-055) True when the LRC's own ``[ar:]`` metadata names an artist that clearly is
    NOT the one we asked for — a DURATION-INDEPENDENT wrong-song signal for the
    weak title-only / cover-fallback paths. A bare-title search for a common title
    can return a famous unrelated same-title song (Ludacris "The Potion" for a
    VTuber's "Potion"); when that file carries ``[ar:Ludacris]`` and we wanted
    "Michiru Shisui" we reject it even though the 3:43 durations coincided and beat
    every duration gate. Conservative: fires ONLY when an ``[ar:]`` tag is present
    AND the two artists are a different script OR share no token — a missing tag or
    a near-match never rejects a correct file."""
    m = re.search(r"\[ar:([^\]]+)\]", lrc, re.I)
    if not m or not want_artist:
        return False
    got = m.group(1).strip()
    if not got:
        return False
    def _norm(s):
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()
    ng, nw = _norm(got), _norm(want_artist)
    if not ng or not nw or ng == nw or ng in nw or nw in ng:
        return False
    cjk = lambda s: bool(_KANA.search(s) or _HAN.search(s) or _HANGUL.search(s))
    if cjk(got) != cjk(want_artist):
        return True                        # one CJK, one Latin ⇒ different artist
    return not (set(ng.split()) & set(nw.split()))   # same script: no shared token


def _synced_cjk(title, artist, duration):
    """Search syncedlyrics across providers that carry ORIGINAL-script lyrics and
    return (lrc, provider) for the first synced result that actually contains CJK
    and verifies — so a romanized upload gets upgraded to the real kanji/kana
    (NetEase in particular reliably has the Japanese original)."""
    try:
        import syncedlyrics
    except ImportError:
        return None
    arts = split_artists(artist)
    queries, seen = [], set()
    if title and artist:
        queries.append(f"{title} {artist}")
    for ar in arts:
        queries.append(f"{title} {ar}")
    if title:
        queries.append(title)
    for q in queries[:4]:
        k = q.lower().strip()
        if not k or k in seen:
            continue
        seen.add(k)
        for prov in ("NetEase", "Musixmatch", "Megalobiz"):
            try:
                lrc = syncedlyrics.search(q, synced_only=True, providers=[prov])
            except Exception:
                lrc = None
            if not lrc or "[" not in lrc:
                continue
            body = re.sub(r"\[[^\]]*\]", "", lrc)
            if (_KANA.search(body) or _HAN.search(body)) \
                    and verify_lrc(lrc, title, duration):
                return lrc, prov
    return None


def _fetch_netease_lyrics(title: str, artist: str) -> str | None:
    """NetEase Cloud Music (网易云音乐) direct provider — the canonical Chinese
    lyrics source, with synced LRC for almost every Chinese pop/rap/indie release
    that lrclib + syncedlyrics' aggregators miss. Used ONLY for lang=='zh' tracks
    (see the gated call in fetch_lrc), AFTER lrclib/syncedlyrics, BEFORE AI gen.

    Returns LRC text or None. Silently swallows network/parse errors so a
    NetEase outage never breaks the pipeline."""
    if not title:
        return None
    q = title if not artist else f"{title} {artist}"
    headers = {
        "User-Agent": "Mozilla/5.0",       # NetEase blocks empty UA
        "Referer": "https://music.163.com",
    }

    def _get(url: str) -> dict | None:
        last_exc = None
        for _attempt in range(2):           # single retry
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=5) as r:
                    return json.loads(r.read().decode("utf-8", errors="replace"))
            except Exception as e:
                last_exc = e
        return None

    # 1. SEARCH — type=1 is songs
    search_url = ("https://music.163.com/api/search/get/web?"
                  + urllib.parse.urlencode({"s": q, "type": 1,
                                            "offset": 0, "limit": 10}))
    try:
        data = _get(search_url)
        songs = (data or {}).get("result", {}).get("songs") or []
        if not songs:
            return None

        # Fuzzy match: score each candidate on title + artist token overlap
        # (reuses the local _norm helper — same scoring style as _pick_lrclib).
        nt = _norm(title)
        nas = [_norm(x) for x in split_artists(artist)] or [_norm(artist)]
        best, best_score = None, 0
        for s in songs:
            ct = _norm(s.get("name") or "")
            cas = [_norm((a or {}).get("name") or "") for a in (s.get("artists") or [])]
            score = 0
            if nt and ct and (nt in ct or ct in nt):
                score += 3
            if any(na and ca and (na in ca or ca in na)
                   for na in nas if na for ca in cas):
                score += 3
            if score > best_score:
                best, best_score = s, score
        if not best or best_score < 3:
            # No title overlap at all → don't risk a wrong-song match
            return None
        song_id = best.get("id")
        if not song_id:
            return None
    except Exception:
        return None

    # 2. LYRICS — lv=1 wants the synced LRC; kv/tv carry karaoke/translation
    lyric_url = ("https://music.163.com/api/song/lyric?"
                 + urllib.parse.urlencode({"id": song_id, "lv": 1, "kv": 1, "tv": -1}))
    try:
        data = _get(lyric_url)
        lrc = ((data or {}).get("lrc") or {}).get("lyric") or ""
        lrc = lrc.strip()
        if lrc and "[" in lrc:
            return lrc
    except Exception:
        return None
    return None


def _title_variants(title: str) -> list:
    """Clean song-title candidates pulled out of a messy / bilingual video title,
    so a niche song still gets found. E.g.
    '理芽 - おしえてかみさま / RIM - Divine Delays｜from 神椿' →
    ['理芽 - おしえてかみさま / RIM - Divine Delays', 'おしえてかみさま',
     'Divine Delays', …]. The full title stays first (most specific)."""
    out, seen = [], set()

    def add(s):
        s = (s or "").strip(" 　-–—|｜/／┃│‖・")
        if len(s) >= 2 and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)

    base = re.sub(r"\s*[|｜/／]\s*from\b.*$", "", title, flags=re.I)   # drop "｜from 神椿"
    add(base)
    add(title)
    for seg in re.split(r"\s*[/／]\s*", base):                         # bilingual "JP / EN"
        add(seg)
        if re.search(r"\s[-–—]\s", seg):
            dparts = re.split(r"\s+[-–—]\s+", seg)
            add(dparts[-1])      # song after "Artist - " ('米津玄師 - Lemon' → Lemon)
            add(dparts[0])       # JP before "- romaji" ('プリズムの魔法 - Prism no Mahou' → プリズムの魔法)
    return out[:6]


# Hepburn romaji → hiragana, longest-match, with sokuon (doubled consonant → っ)
# and moraic n (ん). Used to ALSO search a romanized Japanese title under its native
# kana: providers (NetEase / syncedlyrics aggregators) index 'かもね' under かもね, and
# the bare romaji query 'kamone' pulls a DIFFERENT same-romaji song. Returns the kana
# string, or None when the input is not cleanly romanized Japanese (English titles like
# 'white balance'/'deadpool' leave a consonant cluster and are rejected → no-op).
_HEPBURN_KANA = {
    "kya": "きゃ", "kyu": "きゅ", "kyo": "きょ", "sha": "しゃ", "shu": "しゅ", "sho": "しょ",
    "cha": "ちゃ", "chu": "ちゅ", "cho": "ちょ", "nya": "にゃ", "nyu": "にゅ", "nyo": "にょ",
    "hya": "ひゃ", "hyu": "ひゅ", "hyo": "ひょ", "mya": "みゃ", "myu": "みゅ", "myo": "みょ",
    "rya": "りゃ", "ryu": "りゅ", "ryo": "りょ", "gya": "ぎゃ", "gyu": "ぎゅ", "gyo": "ぎょ",
    "ja": "じゃ", "ju": "じゅ", "jo": "じょ", "bya": "びゃ", "byu": "びゅ", "byo": "びょ",
    "pya": "ぴゃ", "pyu": "ぴゅ", "pyo": "ぴょ", "shi": "し", "chi": "ち", "tsu": "つ",
    "fu": "ふ", "ji": "じ",
    "ka": "か", "ki": "き", "ku": "く", "ke": "け", "ko": "こ", "ga": "が", "gi": "ぎ",
    "gu": "ぐ", "ge": "げ", "go": "ご", "sa": "さ", "su": "す", "se": "せ", "so": "そ",
    "za": "ざ", "zu": "ず", "ze": "ぜ", "zo": "ぞ", "ta": "た", "te": "て", "to": "と",
    "da": "だ", "de": "で", "do": "ど", "na": "な", "ni": "に", "nu": "ぬ", "ne": "ね",
    "no": "の", "ha": "は", "hi": "ひ", "he": "へ", "ho": "ほ", "ba": "ば", "bi": "び",
    "bu": "ぶ", "be": "べ", "bo": "ぼ", "pa": "ぱ", "pi": "ぴ", "pu": "ぷ", "pe": "ぺ",
    "po": "ぽ", "ma": "ま", "mi": "み", "mu": "む", "me": "め", "mo": "も", "ya": "や",
    "yu": "ゆ", "yo": "よ", "ra": "ら", "ri": "り", "ru": "る", "re": "れ", "ro": "ろ",
    "wa": "わ", "wo": "を", "a": "あ", "i": "い", "u": "う", "e": "え", "o": "お",
}


def romaji_to_kana(s: str) -> str | None:
    """Romanized-Japanese title → hiragana, or None if it isn't cleanly romaji
    (so English titles are rejected, not mangled). Conservative on purpose: a
    leftover consonant cluster ⇒ not Japanese ⇒ None."""
    if not s or not re.fullmatch(r"[A-Za-z \-’'ー]+", s):
        return None
    out = []
    for word in re.split(r"[\s\-]+", s.strip().lower()):
        if not word:
            continue
        i = 0
        while i < len(word):
            c = word[i]
            # sokuon: a doubled consonant (not n) → small つ
            if c not in "aeioun" and i + 1 < len(word) and word[i + 1] == c:
                out.append("っ"); i += 1; continue
            # moraic n: 'n' not starting a な-row / -ya mora
            if c == "n" and (i + 1 >= len(word) or word[i + 1] not in "aeiouy"):
                out.append("ん"); i += 1; continue
            m = None
            for L in (3, 2, 1):
                if word[i:i + L] in _HEPBURN_KANA:
                    m = word[i:i + L]; break
            if not m:
                return None
            out.append(_HEPBURN_KANA[m]); i += len(m)
    return "".join(out) or None


def _hira_to_kata(h: str) -> str:
    """Hiragana → katakana (so a loanword title indexed in katakana also matches)."""
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in (h or ""))


def fetch_lrc(title: str, artist: str = "", duration: float | None = None,
              cover: bool = False, strict: bool = False,
              reject_signatures: set | None = None,
              extra_artists: list | None = None,
              yt_composer: list | None = None,
              yt_original_artist: list | None = None,
              yt_lyrics_block: str | None = None):
    """Return (lrc_string, meta) of a VERIFIED match, or (None, None).
    Widens the search across artist variants while guarding false positives.
    Prefers ORIGINAL-script lyrics: a romaji-only result is stashed and used only
    if no kanji/kana version can be found, so a Japanese song shows real furigana
    + romaji + translation instead of a bare romaji upload.

    ``cover=True`` (a 歌ってみた / cover upload) means the supplied ``artist`` is the
    COVERING channel, not the song's artist — the lyrics are the ORIGINAL song's,
    so they're looked up by TITLE (trusting the cover marker) before the
    artist-keyed queries that would otherwise miss the original.

    ``strict=True`` means the ``artist`` is AUTHORITATIVE (a clean source — Spotify
    or a YT-Music "- Topic" channel — told us exactly who it is). Then the
    artist-unconfirmed title-only last resort is SKIPPED: for a generic title like
    "Lucky Star" that catalog has many different songs under, an artist-unconfirmed
    title hit is almost always the WRONG one ("Twinkle Twinkle"), so returning
    nothing (→ generate by ear from the real audio) beats showing a different song.

    TICKET-112: ``extra_artists`` / ``yt_composer`` / ``yt_original_artist`` /
    ``yt_lyrics_block`` are DISAMBIGUATORS pulled from the YouTube video's
    description (e.g. ``作詞・作曲：kors k`` / ``歌唱：ReGLOSS(...)``). They are
    NOT the primary query — instead, every extra artist name is layered onto
    the existing ``arts`` candidate list so the standard scoring + language
    + duration guards stay in charge of picking the winner. With them empty
    or None the behavior is identical to v1.0.93."""
    t, a = title.strip(), artist.strip()
    # Query artists in provider-friendly priority order: compact stylized-unit
    # forms first ('ReGLOSS'), then the original credit ('Re GLOSS' / full
    # agency channel). This keeps the fast hit in front of the slow miss.
    arts = _artist_query_candidates(a)
    # TICKET-112: prepend YT-description vocalists / original-artist /
    # composer / lyricist as additional artist candidates. Vocals come first
    # (most likely to BE the recording artist), then original_artist (for
    # cover videos), then composer/lyricist (less likely to be the singer
    # but still narrows aggregator search). De-duped against existing arts
    # so we don't re-query the same name twice.
    _extras_seen = {x.strip().lower() for x in arts if x}
    if a:
        _extras_seen.add(a.strip().lower())
    for _src in (extra_artists or []):
        if not _src:
            continue
        for _cand in (_artist_query_candidates(_src) or [_src.strip()]):
            _key = _cand.strip().lower()
            if _key and _key not in _extras_seen:
                arts.append(_cand.strip())
                _extras_seen.add(_key)
    romaji_fallback = [None]   # (lrc, meta) — used only if nothing original-script
    # A CJK-script artist's song is in a CJK language (or, for a cover, English) —
    # never German/Spanish/Russian. So a European-language hit on a Latin title is a
    # SAME-TITLE COLLISION (a German song also called "Beyond the Way" outranked the
    # Japanese cover, which lives on NetEase under a romanized name we can't derive).
    exp_cjk = bool(a and (_KANA.search(a) or _HAN.search(a) or _HANGUL.search(a)))
    # The TITLE's script pins the SONG's language even when the ARTIST is
    # romanized (Sakura Miko, Kizuna AI…). KANA (hiragana/katakana) is unique to
    # Japanese, HANGUL to Korean — so a kana title is a Japanese song and a
    # Chinese/Korean same-title hit is a COLLISION ("ファッションビート" by Sakura Miko
    # pulled a Chinese "Fashion Beat"). HAN-only is ambiguous (shared JA/ZH), so
    # it doesn't pin a language.
    title_ja = bool(_KANA.search(t))
    title_ko = bool(_HANGUL.search(t))
    # HAN (kanji) is used in Japanese & Chinese but NOT modern Korean lyrics (which
    # are hangul). So a kanji title/artist is a JA/ZH song — a KOREAN lyric body is a
    # same-title collision (花譜's kanji "邂逅" pulled a Korean "Chance meeting").
    # Suppressed when the title/artist ITSELF carries hangul (a real Korean entry).
    han_song = (bool(_HAN.search(t)) or bool(_HAN.search(a))) \
        and not (title_ko or bool(_HANGUL.search(a)))
    # A known JAPANESE VTuber act/agency (ReGLOSS / hololive / 神椿·V.W.P / Hajime /
    # Suisei / Reol / Isekaijoucho …) marks a JAPANESE song even when BOTH the title
    # and artist are romanized/English — e.g. 'Hajime Todoroki - Deep Dive': no
    # kana/kanji anywhere after clean_artist strips the channel ('Hajime Ch. 轟はじめ
    # ‐ ReGLOSS' → '轟はじめ' which DOES carry kanji, but a Spotify/desktop-player
    # SMTC field can deliver the bare romanized 'Hajime Todoroki' with no script
    # signal at all). These acts don't release Korean/Chinese-language songs, so
    # reject a ko/zh body for them. Single source of truth = confidence._KNOWN_JA
    # via _JP_VAGENCY_RE. Scan the title + primary artist + split-artist credits
    # (so an extra_artists entry from yt_description 'vocals: ["Hajime Todoroki"]'
    # or a feat. token still trips the guard). Suppressed when the title/artist
    # itself carries hangul — a genuine Korean entry.
    jp_vagency_hay = " ".join(s for s in (t, a, *arts) if s)
    jp_vagency = bool(_JP_VAGENCY_RE and _JP_VAGENCY_RE.search(jp_vagency_hay)) \
        and not (title_ko or bool(_HANGUL.search(a)))

    def take(lrc, meta):
        """Accept this match now — unless it's romanized Japanese (stash + keep
        looking for the original) or a same-title hit in the wrong language."""
        # TICKET-113: per-track blacklist gate. Every provider's hit routes
        # through this closure, so rejecting a known-wrong body HERE covers
        # lrclib/get, lrclib/search, syncedlyrics, syncedlyrics/cover,
        # syncedlyrics/title, and netease in one place.
        if reject_signatures and _lrc_signature(lrc) in reject_signatures:
            log.info("blacklist: rejected hit from %s (signature in reject_signatures)",
                     (meta or {}).get("source", "?"))
            return None
        body = re.sub(r"\[[^\]]*\]", "", lrc)
        if (exp_cjk or jp_vagency) and detect_lang(body) in ("de", "es", "ru", "fr", "it", "pt"):
            # CJK artist OR known JP act (Suisei / hololive / ReGLOSS / Hajime …)
            # + European-language lyrics → same-title collision OR a romaji body
            # detect_lang misclassifies as Spanish ("Ano tasogare mo oritatameba
            # shijima" — Suisei's Soirée). Either way, not the right body.
            return None
        body_lang = detect_lang(body)
        if title_ja and body_lang in ("zh", "ko"):
            return None   # kana title = Japanese song; zh/ko hit is a collision
        if title_ko and body_lang in ("zh", "ja"):
            return None   # hangul title = Korean song; zh/ja hit is a collision
        if han_song and body_lang == "ko":
            return None   # kanji (JA/ZH) song; a Korean body is a wrong-language collision
        if jp_vagency and body_lang == "ko":
            return None   # JP VTuber act + Korean body = collision (KO is unambiguously not JP)
        if body_lang == "zh" and is_jp_vagency(t, a, strict=True):
            return None   # ZH check uses strict — kanji-only artist alone is ambiguous JP↔ZH,
                          # so we only reject ZH when there's a stronger JP signal
                          # (known act name OR kana anywhere)
        # LANGUAGE-CONFIDENCE guard (TICKET-062): a kana/hangul-named artist almost
        # never IS an English same-title song — Suisei's 星街すいせい "GHOST" pulled an
        # English "Ghost". When confidence clearly favours a CJK language over English
        # AND rests on a strong NON-Latin signal (so a romanized name like "Suisei
        # Hoshimachi" can't misfire), reject the English body as a collision.
        if body_lang == "en":
            # A PURE CJK-script title (kana/hangul, NO Latin) whose body detects as
            # English is an English TRANSLATION mislabeled as the original — the
            # アイドル → idol.json bug, where 'Complete and perfect' / 'Dear miss genius
            # idol' was shown instead of 完璧で嘘つきな君は. Reject outright so the real
            # CJK body / a re-fetch wins. (A title carrying Latin — 'Idol English Ver.'
            # — falls through to the gentler confidence test so a genuine English
            # version isn't dropped.)
            if (title_ja or title_ko) and not re.search(r"[A-Za-z]", t):
                return None
            lc = confidence.language_confidence(t, a)
            cjk = lc["ja"] + lc["zh"] + lc["ko"]
            if lc.get("certainty", 0.0) >= 0.6 and cjk >= 0.6 and cjk > lc["en"] + 0.2:
                return None
        if _looks_romaji(body):
            if romaji_fallback[0] is None:
                romaji_fallback[0] = (lrc, {**meta, "romaji": True})
            return None
        return lrc, meta

    # 1. LRCLIB duration-exact, trying the full credit then each artist
    for ca in (arts if arts else ([a] if a else [])):
        hit = _lrclib_get(t, ca, duration)
        if hit and verify_lrc(hit["lrc"], t, duration):
            r = take(hit["lrc"], {"source": "lrclib/get", "artist": hit["artist"],
                                  "duration": hit.get("duration")})
            if r:
                return r

    # 2. LRCLIB scored search (artist/title/duration)
    hit = _pick_lrclib(t, a, duration, arts=arts)
    if hit and verify_lrc(hit["lrc"], t, duration):
        r = take(hit["lrc"], {"source": "lrclib/search", "artist": hit["artist"],
                              "duration": hit.get("duration")})
        if r:
            return r

    # 2b. LRCLIB only gave us romaji → try to UPGRADE to the kanji/kana original
    #     (NetEase etc. carry it) before settling for the romaji.
    if romaji_fallback[0] is not None:
        up = _synced_cjk(t, a, duration)
        if up:
            lrc, prov = up
            return lrc, {"source": f"syncedlyrics/{prov.lower()}",
                         "artist": a or None, "duration": duration}

    # 3. syncedlyrics — title+artist queries (high confidence) first
    try:
        import syncedlyrics
    except ImportError:
        return romaji_fallback[0] or (None, None)

    def _try(q):
        try:
            lrc = syncedlyrics.search(q, synced_only=True)
        except Exception:
            return None
        return lrc if (lrc and "[" in lrc) else None

    # Try each clean song-title candidate (full title first, then the song name
    # pulled out of a bilingual "Artist - JP / Artist - EN｜from X" video title) so
    # a niche song still resolves to its real lyrics instead of being generated.
    # ARTIST-KEYED queries run FIRST — even for a COVER, adding the (covering)
    # channel disambiguates a super-common title: "地球儀 花譜" ranks Kenshi
    # Yonezu's 地球儀 (the song actually being covered) first, whereas a bare
    # "地球儀" title query grabs an unrelated same-title song. The cover title-only
    # fast-path is the FALLBACK below — only for true 歌ってみた uploads where the
    # channel-as-artist genuinely derails the search (TICKET-001 vs TICKET-002).
    # NATIVE-KANA FIRST (guarded): a romanized-Japanese title ('kamone' → かもね) is
    # indexed by providers under its KANA; the bare romaji query pulls a DIFFERENT
    # same-romaji Japanese song that still passes the (script-only) language guard, so
    # it would WIN before any later kana variant is ever tried. Try the kana form FIRST,
    # ARTIST-QUALIFIED, with the [ar:]-conflict guard — a Latin title gets NO language
    # check in verify_lrc, so the artist-conflict guard is what stops a same-kana WRONG
    # song from being accepted. English / loanword titles → romaji_to_kana None → no-op.
    # Bounded to 4 queries (hiragana+katakana × 2 orders), all on the primary artist.
    kana = romaji_to_kana(t)
    if kana and a:
        kana_qs, kseen = [], set()
        for kf in (kana, _hira_to_kata(kana)):
            for q in (f"{kf} {a}", f"{a} {kf}"):
                kk = q.lower().strip()
                if kk not in kseen:
                    kseen.add(kk)
                    kana_qs.append(q)
        for q in kana_qs:
            lrc = _try(q)
            if (lrc and verify_lrc(lrc, t, duration)
                    and not _lrc_artist_conflict(lrc, a)):
                r = take(lrc, {"source": "syncedlyrics/kana", "artist": a or None,
                               "duration": duration})
                if r:
                    return r

    hi_q, seen = [], set()
    for tt in (_title_variants(t) or [t]):
        if arts:
            for ar in arts:
                hi_q += [f"{tt} {ar}", f"{ar} {tt}"]
        elif a:
            hi_q.append(f"{tt} {a}")
        else:
            hi_q.append(tt)
    for q in hi_q:
        k = q.lower().strip()
        if k in seen:
            continue
        seen.add(k)
        lrc = _try(q)
        if lrc and verify_lrc(lrc, t, duration):
            r = take(lrc, {"source": "syncedlyrics", "artist": a or None,
                           "duration": duration})
            if r:
                return r

    # COVER fast-path (FALLBACK). A 歌ってみた / cover's lyrics ARE the original
    # song's, but the "artist" we have is the COVERING channel. When the
    # artist-keyed queries above already resolved the original (the channel name
    # actually helped narrow it), we never get here. We only fall back to a
    # title-only query — trusting the cover marker, without the stricter
    # same-title guard (_strict_ok) — for true covers the artist token couldn't
    # resolve, so they still beat the ~11s generate-by-ear deadline. Running this
    # AFTER the artist-keyed pass is what stops a common title (地球儀) from
    # loading the wrong same-title song when the right one was reachable.
    if cover and t:
        for tt in (_title_variants(t) or [t]):
            lrc = _try(tt)
            if (lrc and verify_lrc(lrc, t, duration)
                    and not _lrc_artist_conflict(lrc, a)):
                r = take(lrc, {"source": "syncedlyrics/cover", "artist": a or None,
                               "duration": duration})
                if r:
                    return r

    # 4. title-only — last resort, guarded against same-title wrong songs. SKIPPED
    # when strict=True: a CLEAN source (Spotify / YT-Music "- Topic") gives an
    # authoritative artist, so an artist-unconfirmed title-only hit for a generic
    # title (the "Lucky Star" → "Twinkle Twinkle" trap) is almost certainly a
    # DIFFERENT same-title song — better to return nothing and let generation
    # transcribe the REAL audio than to display the wrong song.
    if t and not strict:
        lrc = _try(t)
        # _lrc_artist_conflict: a bare-title search for a generic title ("Rainy
        # Day", "Play On!") routinely returns a DIFFERENT same-title song; if its
        # [ar:] tag names an artist that clearly isn't the one playing, reject it
        # instead of mislabelling it with the player's artist (the "no beer in
        # this song" bug). Conservative — only fires on a clear cross-script /
        # no-shared-token artist mismatch.
        if (lrc and verify_lrc(lrc, t, duration) and _strict_ok(lrc, t, duration)
                and not _lrc_artist_conflict(lrc, a)):
            r = take(lrc, {"source": "syncedlyrics/title", "artist": a or None,
                           "duration": duration})
            if r:
                return r
        elif lrc and a and _lrc_artist_conflict(lrc, a):
            log.info("title-only: rejected %r — LRC artist conflicts with %r", t, a)

    # 5. NetEase Cloud Music (网易云音乐) — direct provider for CJK tracks.
    # v1.1.49: WIDENED from Chinese-only (lang=="zh") to also cover JAPANESE
    # (lang=="ja", i.e. any kana-bearing native title). NetEase is the canonical
    # source not just for Chinese but for VTuber originals, Vocaloid and JP covers,
    # which lrclib + syncedlyrics' Western aggregators routinely miss — and the old
    # `_synced_cjk` upgrade path only ran when lrclib ALREADY returned a romaji hit,
    # so a native-JP title with no lrclib hit at all never reached NetEase and fell
    # straight to AI-gen. Tried AFTER lrclib/syncedlyrics, BEFORE returning None
    # (which upstream takes as the cue to AI-generate by ear). Korean is excluded
    # (NetEase JP/CN catalog; Melon/Genie carry Korean). A kanji-only JP title is
    # already caught by detect_lang=="zh"; this adds the kana-bearing majority.
    if detect_lang(t) in ("zh", "ja"):
        lrc = _fetch_netease_lyrics(t, a)
        if lrc and verify_lrc(lrc, t, duration):
            r = take(lrc, {"source": "netease", "artist": a or None,
                           "duration": duration})
            if r:
                n_lines = sum(1 for ln in r[0].splitlines()
                              if re.search(r"\[\d+:\d+", ln))
                log.info("netease: lyrics found for %r — %d lines", t, n_lines)
                return r

    # Nothing original-script found → use the stashed romaji if we have one.
    # TICKET-113: re-check the stashed romaji against the blacklist at the
    # return site — a romaji whose signature is reject_signatures slips past
    # take() into the stash before rejection, so without this check a stashed
    # bad romaji can still be returned after all CJK upgrades are blacklisted.
    rf = romaji_fallback[0]
    if rf and reject_signatures and _lrc_signature(rf[0]) in reject_signatures:
        log.info("blacklist: rejected stashed romaji fallback")
        return (None, None)
    return rf or (None, None)


# ── Annotation ───────────────────────────────────────────────────────

def _song_lang(lines: list[dict]) -> str:
    """Whole-song language. Scans ALL lines (not just the first 40) so a song
    that opens with a kanji-only or instrumental section is still classified
    right. Crucially: ANY kana anywhere ⇒ Japanese — Chinese never uses kana, so
    a kanji-heavy J-pop/VTuber song (e.g. 花譜) is never mistaken for Chinese
    (which would give pinyin instead of furigana)."""
    body = " ".join(ln["jp"] for ln in lines)
    if _KANA.search(body):
        return "ja"
    if _looks_romaji(body):
        return "ja-romaji"      # romanized Japanese — can't furigana, but DO translate
    return detect_lang(body)


# Map our song language to an EXPLICIT translator source code. CRITICAL: Google's
# source="auto" silently FAILS on (Traditional) Chinese — it returns the input
# unchanged, which then got stored as the "translation" (the user's "no English
# for Chinese songs", en==original). An explicit zh source translates it
# correctly. Japanese/Korean auto-detect fine but explicit is harmless + safer.
_SRC_LANG = {"zh": "zh-CN", "yue": "zh-CN", "ja": "ja", "ko": "ko", "ru": "ru",
             "el": "el"}


def _make_translator(source_lang: str | None = None):
    """Prefer DeepL (noticeably better JP/CJK→EN) when a DEEPL_API_KEY is set in
    the environment; otherwise fall back to the free Google endpoint. Either way
    no key is required to use the app. `source_lang` is the SONG language — we map
    it to an explicit translator source so CJK doesn't fall through auto-detect."""
    src = _SRC_LANG.get(source_lang or "", "auto")
    key = os.environ.get("DEEPL_API_KEY")
    if key:
        try:
            from deep_translator import DeeplTranslator
            # DeepL wants the BCP-style code; zh-CN→ZH, else the auto path.
            dsrc = {"zh-CN": "zh", "ja": "ja", "ko": "ko", "ru": "ru"}.get(src, None)
            return DeeplTranslator(api_key=key, source=(dsrc or "auto"),
                                   target="en-us", use_free_api=True)
        except Exception:
            pass
    from deep_translator import GoogleTranslator
    return GoogleTranslator(source=src, target="en")


def _norm_echo(s: str) -> str:
    """Whitespace/case-normalized form for echo detection: the free translator
    sometimes returns the SOURCE text back with only spacing/case mangled
    ('какдела' for 'как дела'); a plain equality check missed those, so the
    original text got stored as the 'English translation'."""
    return re.sub(r"\s+", "", (s or "").strip().lower())


def _translate_window(tr, idxs: list[int], raw_fn) -> dict:
    """Translate a window of line-indices TOGETHER (so each line is read with its
    neighbours for context) and return {index: english} for as many as map back.

    Uses a NUMBERED protocol ("1. …\n2. …") rather than a bare newline-join: the
    translator routinely merges/splits/reorders lines, which made the plain join
    misalign and silently drop the WHOLE window to context-free per-line
    translation — the "out-of-context single line" symptom. Numbers survive
    translation, so we can map each result back to its source line even when the
    counts don't match exactly."""
    if not idxs:
        return {}
    numbered = "\n".join(f"{k + 1}. {(raw_fn(w).strip() or '　')}"
                         for k, w in enumerate(idxs))
    # RETRY: the free Google endpoint intermittently rate-limits / times out. A
    # single failure used to leave these lines with NO English forever (the
    # "Chinese/JP song shows romaji but no translation" bug — the local
    # romanizer always succeeds, so the gap looked language-specific). Retry a
    # couple of times with a short backoff before giving up on the window.
    out = ""
    for attempt in range(3):
        try:
            out = tr.translate(numbered) or ""
            if out.strip():
                break
        except Exception:
            out = ""
        if attempt < 2:
            time.sleep(0.6 * (attempt + 1))
    if not out.strip():
        return {}
    res = {}
    for line in out.split("\n"):
        m = re.match(r"\s*(\d+)\s*[.)．、:：]\s*(.*)", line)
        if not m:
            continue
        k = int(m.group(1)) - 1
        eng = m.group(2).strip()
        if 0 <= k < len(idxs) and eng:
            # REJECT a no-op "translation" that just echoes the source text (the
            # translator returned the input untranslated). Storing that produced
            # the "en shows the original Chinese" symptom. Leaving it unmapped
            # keeps the line eligible for re-translation instead. Normalized
            # compare: the echo often comes back with mangled spacing/case.
            if _norm_echo(eng) == _norm_echo(raw_fn(idxs[k])):
                continue
            res[idxs[k]] = eng
    return res


def _translate_lines(lines: list[dict], song_lang: str | None = None,
                     only_missing: bool = False) -> int:
    try:
        tr = _make_translator(song_lang)
    except ImportError:
        return 0
    # GUARANTEE (user directive): every non-English lyric carries an English
    # translation. Per-line set = every language detect_lang() can name; the
    # whole-song fallback now covers ANY non-English song language (French,
    # Italian, Indonesian, … detect per-line as "other", so the song-level lang
    # is what routes them). Keep in lockstep with main.py's _maybe_translate.
    _LANGS = ("ja", "ko", "zh", "es", "de", "ru", "el", "fr", "pt", "it")
    whole = bool(song_lang) and not str(song_lang).startswith("en")

    def want(ln):
        raw = re.sub(r"\(.*?\)", "", ln["jp"])
        if not raw.strip():
            return False
        en = ln.get("en", "").strip()
        if only_missing and en:
            # Keep a REAL translation, but RE-translate a failed one where the
            # "translation" is just the original text (the translator echoed the
            # source — compare whitespace/case-normalized so a space-mangled
            # echo like 'какдела' for 'как дела' is also treated as missing).
            if _norm_echo(en) != _norm_echo(raw):
                return False
        ll = detect_lang(raw)
        if ll in _LANGS:
            return True
        return whole and ll == "other"     # Romance/Germanic line w/o markers, etc.

    want_set = {i for i, ln in enumerate(lines) if want(ln)}
    if not want_set:
        return 0

    def raw(i):
        return re.sub(r"\(.*?\)", "", lines[i]["jp"])

    # Group the wanted lines by their OWN language: with the translator source
    # pinned to the SONG language, a line in a different script inside a
    # mixed-language body (a Russian/Greek line in a Japanese song) came back
    # ECHOED — the original text was stored as the "English". Script lines get
    # a translator sourced from their own language; Latin/"other" lines ride
    # the song-language translator.
    groups: dict[str, set[int]] = {}
    for i in want_set:
        ll = detect_lang(raw(i))
        key = ll if ll in ("ja", "ko", "zh", "ru", "el") else "_song"
        groups.setdefault(key, set()).add(i)

    # Translate in windows that CARRY CONTEXT: each block of focus lines is sent
    # together with CTX neighbouring lines before and after (and the whole block is
    # numbered), so a line is read in the flow of the song (pronouns/subjects often
    # only make sense from the surrounding lines) instead of in isolation. Only the
    # focus lines' results are kept; the context lines just steer the translation.
    CTX, SIZE, n = 2, 24, len(lines)

    def _run(tr_run, wset):
        done = pos = 0
        while pos < n:
            end = min(n, pos + SIZE)
            focus = [i for i in range(pos, end) if i in wset]
            if not focus:
                pos = end
                continue
            lo, hi = max(0, pos - CTX), min(n, end + CTX)
            got = _translate_window(tr_run, list(range(lo, hi)), raw)
            for i in focus:
                if got.get(i):
                    lines[i]["en"] = got[i]
                    done += 1
                    continue
                # The block translator merged/dropped this one. Retry it in a SMALL
                # numbered window of just its ±CTX neighbours (still in context), and
                # only fall to a bare single-line translation if even that fails.
                sub = list(range(max(0, i - CTX), min(n, i + CTX + 1)))
                g2 = _translate_window(tr_run, sub, raw)
                if g2.get(i):
                    lines[i]["en"] = g2[i]
                    done += 1
                    continue
                try:
                    t = raw(i)
                    eng = (tr_run.translate(t) or "").strip() if t.strip() else ""
                    # reject a no-op echo of the source (normalized)
                    if eng and _norm_echo(eng) != _norm_echo(t):
                        lines[i]["en"] = eng
                        done += 1
                except Exception:
                    pass
            pos = end
        return done

    total = 0
    for key, idx_set in groups.items():
        if key == "_song" or key == (song_lang or ""):
            tr_g = tr
        else:
            try:
                tr_g = _make_translator(key)
            except Exception:
                tr_g = tr
        total += _run(tr_g, idx_set)
    return total


def annotate(lines: list[dict], lang: str, translate: bool = False) -> list[dict]:
    """Add furigana + romaji to each line by ITS OWN script — not the song's
    overall language. This way a Japanese line inside a mostly-English song (or
    one whose language was mis-detected) still gets furigana/romaji instead of
    coming out as bare kanji. `lang` only disambiguates kanji-only lines
    (Japanese vs Chinese)."""
    # Decide the Chinese romanization ONCE per song: jyutping for Cantonese,
    # else Mandarin pinyin (only relevant when the song lang is zh).
    zh_rom = "zh"
    if lang == "zh" and _is_cantonese(" ".join(l.get("jp", "") for l in lines)):
        zh_rom = "yue"
    for ln in lines:
        raw = ln["jp"]
        ll = detect_lang(raw)
        if ll == "ja":
            ln["jp"] = to_furigana(raw)
            ln["rm"] = romanize(raw, "ja")
        elif ll == "ko":
            ln["rm"] = romanize(raw, "ko")
        elif ll == "zh":
            # kanji-only: read as Japanese unless the whole song is Chinese
            if lang == "zh":
                ln["rm"] = romanize(raw, zh_rom)    # pinyin, or jyutping if Cantonese
            else:
                ln["jp"] = to_furigana(raw)
                ln["rm"] = romanize(raw, "ja")
        elif ll == "ru":
            ln["rm"] = romanize(raw, "ru")          # Cyrillic → Latin reading
        elif ll == "el":
            ln["rm"] = romanize(raw, "el")          # Greek → Latin reading
        else:
            # Latin-script source languages (Spanish, German, French, Italian,
            # Portuguese, English) — shown as-is; no romaji needed. Leaving 'rm'
            # empty here is intentional and keeps the downstream pipeline (incl.
            # backfill_file's `if ln.get('rm', '').strip(): continue` guard)
            # from re-processing these lines.
            ln["rm"] = ""
    if translate:
        _translate_lines(lines, lang)
    return lines


def backfill_file(path) -> bool:
    """Self-heal a cached file: add furigana/romaji to any Japanese/CJK line
    that's missing it, and translate any non-English line with no English yet.
    Returns True if anything changed. Used at runtime so a song that came out as
    bare Japanese gets fixed in place the first time it plays."""
    path = Path(path)
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception:
        return False
    lines = data.get("lines", [])
    meta = data.get("meta", {})
    lang = meta.get("lang")
    # Cantonese → jyutping (else Mandarin pinyin); body markers + artist/title.
    zh_rom = "zh"
    if lang == "zh" and _is_cantonese(
            " ".join(ln.get("jp", "") for ln in lines),
            meta.get("artist", ""), meta.get("title", "")):
        zh_rom = "yue"
    changed = False
    for ln in lines:
        raw = re.sub(r"[(（][ぁ-ゟ゛゜ー]+[)）]", "", ln.get("jp", ""))  # strip existing furigana
        if not raw.strip() or ln.get("rm", "").strip():
            continue
        ll = detect_lang(raw)
        if ll == "ja" or (ll == "zh" and lang != "zh"):
            ln["jp"] = to_furigana(raw)
            ln["rm"] = romanize(raw, "ja")
            changed = True
        elif ll in ("zh", "ko", "ru", "el"):
            # every non-Latin script self-heals its romanization, not just CJK —
            # ru/el used to be annotate-only, so a body that arrived without rm
            # never got its Latin reading backfilled.
            ln["rm"] = romanize(raw, zh_rom if ll == "zh" else ll)
            changed = True
    n = _translate_lines(lines, lang, only_missing=True)   # fills lines missing 'en'
    if n:
        changed = True
    if changed:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(path)
    return changed


def translate_file(path) -> bool:
    path = Path(path)
    try:
        data = json.loads(path.read_text("utf-8"))
        n = _translate_lines(data["lines"], data.get("meta", {}).get("lang"))
        if n:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return n > 0
    except Exception:
        return False


# ── Library validation (error detection over cached files) ───────────

def validate_file(path, duration: float | None = None) -> tuple[bool, str]:
    """True if the cached file looks like a real, correct match."""
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except Exception as e:
        return False, f"unreadable ({e})"
    lines = data.get("lines", [])
    if len(lines) < 4:
        return False, "too few lines"
    meta = data.get("meta", {})
    title = meta.get("title", "")
    body = " ".join(ln.get("jp", "") for ln in lines)
    tl, ll = detect_lang(title), detect_lang(body)
    if tl in ("ja", "ko", "zh") and ll != tl and not (tl == "zh" and ll == "ja"):
        return False, f"language mismatch (title {tl} / lyrics {ll})"
    md = meta.get("duration")
    if duration and md and abs(md - duration) > 12:
        return False, f"duration mismatch ({md}s vs {duration}s)"
    return True, "ok"


# ── Save ─────────────────────────────────────────────────────────────

def fetch_and_save(title: str, artist: str = "", translate: bool = False,
                   duration: float | None = None, interactive: bool = False,
                   cover: bool = False, strict: bool = False,
                   reject_signatures: set | None = None,
                   extra_artists: list | None = None,
                   yt_composer: list | None = None,
                   yt_original_artist: list | None = None,
                   yt_lyrics_block: str | None = None) -> Path | None:
    # Don't cache a song under a "title" that's just the artist/channel name
    # (e.g. a mangled YouTube title) — it indexes garbage that then false-matches
    # every other video by that artist. Sound ID will find the real song instead.
    if artist and _norm(title) and _norm(title) == _norm(artist):
        return None
    # TICKET-113: pass the per-track blacklist (a snapshot — see _start_fetch)
    # through to fetch_lrc, which routes the rejection into the take() closure
    # that gates every provider.
    # TICKET-112: thread YT-description disambiguators through to fetch_lrc.
    lrc, meta = fetch_lrc(title, artist, duration, cover=cover, strict=strict,
                          reject_signatures=reject_signatures,
                          extra_artists=extra_artists,
                          yt_composer=yt_composer,
                          yt_original_artist=yt_original_artist,
                          yt_lyrics_block=yt_lyrics_block)
    if not lrc:
        return None
    lines = parse_lrc_text(lrc)
    if len(lines) < 4:
        return None
    lang = _song_lang(lines)
    lines = annotate(lines, lang, translate=translate)

    LYRICS_DIR.mkdir(exist_ok=True)
    out = LYRICS_DIR / f"{slugify(title)}.json"
    data = {
        "meta": {
            "title": title,
            "artist": artist,
            "lang": lang,
            "duration": (meta or {}).get("duration") or (round(duration, 1) if duration else None),
            "source": (meta or {}).get("source", "unknown"),
        },
        "lines": lines,
    }
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    lrc_path, positional = None, []
    translate = "--no-en" not in sys.argv
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--lrc" and i + 1 < len(args):
            lrc_path = Path(args[i + 1]); i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            positional.append(args[i]); i += 1

    if not positional:
        print(__doc__.split("Public API:")[0])
        sys.exit(1)
    title = positional[0]
    artist = positional[1] if len(positional) > 1 else ""

    if lrc_path:
        lines = parse_lrc_text(lrc_path.read_text(encoding="utf-8"))
        lang = _song_lang(lines)
        print(f"Parsed {len(lines)} lines (lang={lang})")
        lines = annotate(lines, lang, translate=translate)
        LYRICS_DIR.mkdir(exist_ok=True)
        out = LYRICS_DIR / f"{slugify(title)}.json"
        out.write_text(json.dumps(
            {"meta": {"title": title, "artist": artist, "lang": lang,
                      "duration": None, "source": "lrc-import"}, "lines": lines},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved {out}")
        return

    print(f"Fetching: {title} — {artist}")
    out = fetch_and_save(title, artist, translate=translate)
    print(f"Saved {out}" if out else "No verified lyrics found.")


if __name__ == "__main__":
    main()
