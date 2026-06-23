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
                  ja / zh / ko / es alike). Uses the free Google endpoint by
                  default; if a DEEPL_API_KEY env var is set it uses DeepL
                  instead (noticeably better JP/CJK→EN). No key required to run.
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

import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# syncedlyrics logs noisy provider warnings (e.g. Musixmatch 401) — quiet them
for _n in ("syncedlyrics", "syncedlyrics.providers"):
    logging.getLogger(_n).setLevel(logging.CRITICAL)

if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

if getattr(sys, "frozen", False):
    LYRICS_DIR = Path(sys.executable).parent / "lyrics"   # portable: next to the .exe
else:
    LYRICS_DIR = Path(__file__).parent / "lyrics"

# Script ranges
_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
_KANA   = re.compile(r"[぀-ゟ゠-ヿ]")
_HAN    = re.compile(r"[一-鿿㐀-䶿々]")
_CYRILLIC = re.compile(r"[а-яё]", re.I)
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
    ja|ko|zh|ru|es|de|other."""
    hang = len(_HANGUL.findall(text))
    kana = len(_KANA.findall(text))
    han = len(_HAN.findall(text))
    cyr = len(_CYRILLIC.findall(text))
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
    from gairaigo import KATAKANA_EN
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


def romanize(text: str, lang: str) -> str:
    """Romanize text for the given language: Japanese → Hepburn romaji (fugashi +
    cutlet, katakana English recovered as English), Chinese → pinyin, Korean →
    romaja, Russian → Latin transliteration. Returns '' on failure or an
    unsupported language (German/Spanish/English are already Latin)."""
    try:
        if lang == "ru":
            return _translit_ru(text)
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
        if lang == "zh":
            from pypinyin import lazy_pinyin
            return " ".join(lazy_pinyin(text)).strip()
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


def _lrclib_candidates(title, artist):
    cands, seen = [], set()
    urls = [f"https://lrclib.net/api/search?{urllib.parse.urlencode({'q': f'{title} {artist}'.strip()})}"]
    for ar in ([artist] + split_artists(artist))[:3]:
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


def _pick_lrclib(title, artist, duration):
    best, best_score = None, 0
    nt = _norm(title)
    nas = [_norm(x) for x in split_artists(artist)] or [_norm(artist)]
    for c in _lrclib_candidates(title, artist):
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


def fetch_lrc(title: str, artist: str = "", duration: float | None = None):
    """Return (lrc_string, meta) of a VERIFIED match, or (None, None).
    Widens the search across artist variants while guarding false positives.
    Prefers ORIGINAL-script lyrics: a romaji-only result is stashed and used only
    if no kanji/kana version can be found, so a Japanese song shows real furigana
    + romaji + translation instead of a bare romaji upload."""
    t, a = title.strip(), artist.strip()
    arts = split_artists(a)
    romaji_fallback = [None]   # (lrc, meta) — used only if nothing original-script

    def take(lrc, meta):
        """Accept this match now — unless it's romanized Japanese, in which case
        stash it and return None so the search keeps looking for the original."""
        body = re.sub(r"\[[^\]]*\]", "", lrc)
        if _looks_romaji(body):
            if romaji_fallback[0] is None:
                romaji_fallback[0] = (lrc, {**meta, "romaji": True})
            return None
        return lrc, meta

    # 1. LRCLIB duration-exact, trying the full credit then each artist
    for ca in ([a] + arts if a else []):
        hit = _lrclib_get(t, ca, duration)
        if hit and verify_lrc(hit["lrc"], t, duration):
            r = take(hit["lrc"], {"source": "lrclib/get", "artist": hit["artist"],
                                  "duration": hit.get("duration")})
            if r:
                return r

    # 2. LRCLIB scored search (artist/title/duration)
    hit = _pick_lrclib(t, a, duration)
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

    hi_q, seen = [], set()
    if t and arts:
        hi_q.append(f"{t} {a}")
        for ar in arts:
            hi_q += [f"{t} {ar}", f"{ar} {t}"]
    elif t and a:
        hi_q.append(f"{t} {a}")
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

    # 4. title-only — last resort, strict guard against same-title wrong songs
    if t:
        lrc = _try(t)
        if lrc and verify_lrc(lrc, t, duration) and _strict_ok(lrc, t, duration):
            r = take(lrc, {"source": "syncedlyrics/title", "artist": a or None,
                           "duration": duration})
            if r:
                return r

    # Nothing original-script found → use the stashed romaji if we have one.
    return romaji_fallback[0] or (None, None)


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


def _make_translator():
    """Prefer DeepL (noticeably better JP/CJK→EN) when a DEEPL_API_KEY is set in
    the environment; otherwise fall back to the free Google endpoint. Either way
    no key is required to use the app."""
    key = os.environ.get("DEEPL_API_KEY")
    if key:
        try:
            from deep_translator import DeeplTranslator
            return DeeplTranslator(api_key=key, source="auto", target="en",
                                   use_free_api=True)
        except Exception:
            pass
    from deep_translator import GoogleTranslator
    return GoogleTranslator(source="auto", target="en")


def _translate_lines(lines: list[dict], song_lang: str | None = None,
                     only_missing: bool = False) -> int:
    try:
        tr = _make_translator()
    except ImportError:
        return 0
    whole = song_lang in ("ja", "ko", "zh", "es", "de", "ru", "ja-romaji")

    def want(ln):
        if only_missing and ln.get("en", "").strip():
            return False                   # keep an existing translation
        raw = re.sub(r"\(.*?\)", "", ln["jp"])
        if not raw.strip():
            return False
        ll = detect_lang(raw)
        if ll in ("ja", "ko", "zh", "es", "de", "ru"):
            return True
        return whole and ll == "other"     # Spanish/German line w/o markers, etc.

    want_set = {i for i, ln in enumerate(lines) if want(ln)}
    if not want_set:
        return 0

    def raw(i):
        return re.sub(r"\(.*?\)", "", lines[i]["jp"])

    # Translate in windows that CARRY CONTEXT: each block of focus lines is sent
    # together with CTX neighbouring lines before and after, so a line is read in
    # the flow of the song (pronouns/subjects often only make sense from the
    # surrounding lines) instead of in isolation. Only the focus lines' results
    # are kept; the context lines just steer the translation.
    CTX, SIZE, n = 2, 24, len(lines)
    done = pos = 0
    while pos < n:
        end = min(n, pos + SIZE)
        focus = [i for i in range(pos, end) if i in want_set]
        if not focus:
            pos = end
            continue
        lo, hi = max(0, pos - CTX), min(n, end + CTX)
        window = list(range(lo, hi))
        joined = "\n".join(raw(w) if raw(w).strip() else "　" for w in window)
        parts = None
        try:
            parts = tr.translate(joined).split("\n")
        except Exception:
            parts = None
        if parts and len(parts) == len(window):           # aligned → keep focus
            m = dict(zip(window, parts))
            for i in focus:
                lines[i]["en"] = m[i].strip()
                done += 1
        else:                                              # fallback: per line
            for i in focus:
                try:
                    t = raw(i)
                    lines[i]["en"] = tr.translate(t) if t.strip() else ""
                    done += 1
                except Exception:
                    pass
        pos = end
    return done


def annotate(lines: list[dict], lang: str, translate: bool = False) -> list[dict]:
    """Add furigana + romaji to each line by ITS OWN script — not the song's
    overall language. This way a Japanese line inside a mostly-English song (or
    one whose language was mis-detected) still gets furigana/romaji instead of
    coming out as bare kanji. `lang` only disambiguates kanji-only lines
    (Japanese vs Chinese)."""
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
                ln["rm"] = romanize(raw, "zh")
            else:
                ln["jp"] = to_furigana(raw)
                ln["rm"] = romanize(raw, "ja")
        elif ll == "ru":
            ln["rm"] = romanize(raw, "ru")          # Cyrillic → Latin reading
        else:
            ln["rm"] = ""  # Spanish / German / English — shown as-is, no romaji
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
    lang = data.get("meta", {}).get("lang")
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
        elif ll in ("zh", "ko"):
            ln["rm"] = romanize(raw, ll)
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
                   duration: float | None = None, interactive: bool = False) -> Path | None:
    lrc, meta = fetch_lrc(title, artist, duration)
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
