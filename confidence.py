# -*- coding: utf-8 -*-
"""Song-match confidence — what makes us SURE the lyrics on screen are the RIGHT song.

The app fuses several INDEPENDENT signals when it decides to LOAD / KEEP / SWITCH a
song's lyrics. This module documents and computes that so the decision is principled
and tunable, not scattered ad-hoc checks. The pieces live where they're cheapest to
read, but THIS is the canonical list of what contributes and how much.

═══ SIGNALS (and what each contributes to confidence) ════════════════════════════
  1. ON-SCREEN BANNER OCR   (concert_ocr.py, TICKET-022) — concert videos print the
     current song's name on screen. HIGHEST weight when present: visible ground
     truth that Shazam can't beat on a live take. A ≥0.85 banner match is authoritative.
  2. CLEAN-SOURCE TITLE     (main.py `_clean_source`, TICKET-016) — Spotify / a
     YT-Music "- Topic" channel gave an AUTHORITATIVE title+artist. HIGH weight.
  3. HEARD-BY-SOUND MATCH   (Shazam: heard title == loaded title) — weight SCALES
     UP as the title gets more GENERIC (see below).
  4. TITLE EXACTNESS        (player clean title == loaded song title) — weight
     SCALES DOWN as the title gets more generic (mirror of #3).
  5. DURATION MATCH         (within ~12 s — TICKET-002/009) — rejects a same-title
     song of very different length; `_mark_verified`.
  6. ARTIST MATCH           (player artist == lyrics' artist).
  7. LANGUAGE MATCH         (detected language == lyrics' — a sanity check).

═══ THE "AWAKE" RULE (why audio must outweigh a generic title) ═══════════════════
The MORE GENERIC a title ("Awake", "BANG", "Love", "Lucky Star"), the MORE the heard
AUDIO and the BANNER weigh, and the LESS a bare title match weighs — because dozens of
DIFFERENT songs share that title, so the title alone proves almost nothing. A
DISTINCTIVE title ("feelingradation") is itself strong evidence and can lock out a
stray Shazam mis-ID; a generic one must NOT (it has to defer to sound). `_is_generic_title`
(main.py) only catches tie-in *tags* like "OP Theme"; THIS module adds the missing
"the title is a real name but a very COMMON one" axis via `title_distinctiveness`.
"""
from __future__ import annotations

import re

# Title tokens so common they identify almost nothing on their own — a title made
# only of these (or one short common word) must defer to the heard audio.
_GENERIC_WORDS = {
    "awake", "bang", "love", "lover", "again", "forever", "dream", "dreams", "star",
    "stars", "light", "lights", "night", "day", "days", "time", "life", "fire",
    "rain", "sky", "heart", "hello", "world", "you", "me", "us", "run", "fly", "go",
    "up", "down", "one", "home", "lucky", "music", "song", "theme", "alive", "free",
    "gravity", "paradise", "shine", "magic", "hero", "angel", "kiss", "baby",
    "愛", "夢", "光", "夜", "空", "君", "僕", "私", "花", "恋", "奇跡", "永遠", "明日",
}


def title_distinctiveness(title: str) -> float:
    """0.0 (totally generic — 'Awake', 'BANG') … 1.0 (distinctive — 'feelingradation').

    Short, single common-word, or all-generic-word titles score LOW (so the audio
    decides); long, multi-word, or rare titles score HIGH (so the title can lock).
    This is the single knob that shifts weight toward the audio for ambiguous names."""
    t = (title or "").strip().lower()
    words = [w for w in re.split(r"[^0-9a-z぀-ヿ一-鿿]+", t) if w]
    if not words:
        return 0.3
    if len(words) == 1:
        w = words[0]
        if w in _GENERIC_WORDS:
            return 0.1
        if len(w) <= 4:               # 'edge', 'load' — short, could collide
            return 0.45
        if len(w) >= 9:               # 'feelingradation' — rare, distinctive
            return 0.85
        return 0.6
    common = sum(1 for w in words if w in _GENERIC_WORDS)
    score = 0.45 + 0.12 * (len(words) - 1) - 0.30 * (common / len(words))
    return max(0.0, min(1.0, score))


def is_common_title(title: str, threshold: float = 0.35) -> bool:
    """True when the title is too COMMON to trust on its own → the heard audio
    (and any banner) should drive the song decision, not the title. Used to STOP
    locking the lyrics to a generic title (so a wrong same-title match can be
    corrected by sound — the 'Awake'/'BANG'/'Lucky Star' case)."""
    return title_distinctiveness(title) < threshold


_LC_KANA   = re.compile(r"[぀-ゟ゠-ヿ]")
_LC_HAN    = re.compile(r"[㐀-鿿豈-﫿]")
_LC_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ]")

# Known acts/labels whose ROMANIZED name carries no CJK script but who sing in a CJK
# language — so "ReGLOSS - feelingradation" (hololive DEV_IS) still scores Japanese
# even though both title and artist are Latin. Matched as a lowercase substring of the
# artist (the channel/label is the reliable cue). Expand as needed; this is the
# "ran against a database of known artists" cue the title/script alone can't give.
_KNOWN_JA = (
    "hololive", "regloss", "re gloss", "dev_is", "dev is", "holostars", "holo x",
    "flow glow", "kamitsubaki",
    "v.w.p", "vwp", "virtual witch", "neko hacker", "phase connect", "phase-connect",
    # VTuber acts / J-artists the user plays
    "reol", "kanaria", "suisei", "hoshimachi", "hoshimatic", "kanade", "otonose",
    "ririka", "ichijou", "raden", "juufuutei", "hajime", "todoroki", "hiodoshi",
    "ouro kronii", "kaf", "kanaeru", "kobo", "michiru shisui", "kaneko lumi",
    "harusaruhi", "isekaijoucho", "rim", "laplus", "la+", "laplus darkness",
    "kizuna ai", "kizunaai", "理芽", "幸祜",
)

# A subset of _KNOWN_JA that is UNAMBIGUOUS and consistent enough that the
# romanized name alone is a FULL Japanese signal (certainty 1.0) — the user's
# "Suisei is always Japanese, make her channel/name full JA" rule. Matched as a
# lowercase substring of the artist/channel.
_ALWAYS_JA = (
    "suisei", "hoshimachi", "hoshimatic", "星街すいせい", "すいせい",
)


def language_confidence(title: str, artist: str = "") -> dict:
    """Estimate the SONG's language as a percentage per language, fusing the script
    of the artist name (≈ the artist's USUAL language) and the title. Signal #7 of
    the confidence model, made concrete.

    Why it matters: the title alone is often English even for a Japanese song
    ("GHOST" by 星街すいせい), so a bare-title search pulls an English same-title
    collision. A kana/kanji or hangul ARTIST NAME is a strong cue to the act's usual
    language, so we can prefer the Japanese match. Returns {'ja','en','zh','ko'}
    summing to ~1, plus 'certainty' (0..1) — how much the verdict rests on a strong
    NON-Latin signal. When everything is romanized Latin (e.g. "Suisei Hoshimachi")
    certainty is LOW and the score must not be used to reject (it can't tell
    romanized-Japanese from a Western song).
    """
    t, a = title or "", artist or ""
    al = a.lower()
    v = {"ja": 0.0, "en": 0.0, "zh": 0.0, "ko": 0.0}
    strong = 0.0
    always_ja = any(k in al for k in _ALWAYS_JA)        # unambiguous JP act (Suisei…)
    known_ja = any(k in al for k in _KNOWN_JA)          # known romanized JP act/label
    # Artist name script = the act's usual language (the strongest cue).
    if always_ja:
        v["ja"] += 3.0; strong += 3.0          # Suisei etc.: romanized name = FULL JA
    elif _LC_KANA.search(a):
        v["ja"] += 3.0; strong += 3.0          # kana is uniquely Japanese
    elif _LC_HANGUL.search(a):
        v["ko"] += 3.0; strong += 3.0
    elif _LC_HAN.search(a):
        v["ja"] += 1.4; v["zh"] += 1.4; strong += 2.0   # kanji shared JA/ZH
    elif known_ja:
        # A KNOWN romanized JP act (ReGLOSS, hololive, Reol…) is as reliably
        # Japanese as a kana name — give it FULL weight (certainty 1.0), not a weak
        # partial. These are curated acts that always sing Japanese, so a romanized
        # channel name must still strongly prefer the JA match over an EN collision.
        v["ja"] += 3.0; strong += 3.0
    # Title script (weaker — titles are often English/romaji regardless).
    if _LC_KANA.search(t):
        v["ja"] += 2.0; strong += 2.0
    elif _LC_HANGUL.search(t):
        v["ko"] += 2.0; strong += 2.0
    elif _LC_HAN.search(t):
        v["ja"] += 0.8; v["zh"] += 0.8; strong += 1.0
    elif re.search(r"[A-Za-z]", t):
        v["en"] += 1.0                          # Latin title — could be EN or romaji
    total = sum(v.values()) or 1.0
    out = {k: round(val / total, 3) for k, val in v.items()}
    out["certainty"] = round(min(1.0, strong / 3.0), 3)
    return out
