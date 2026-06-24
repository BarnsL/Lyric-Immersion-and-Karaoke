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
