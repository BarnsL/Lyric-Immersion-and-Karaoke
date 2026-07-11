# -*- coding: utf-8 -*-
"""TICKET-112: Extract structured credit metadata from a YouTube video
description.

WHY THIS EXISTS
---------------
SMTC titles like "Shooting Star" are massively ambiguous (dozens of songs
share the name). The existing provider chain in ``fetch_lyrics.fetch_lrc``
keys off ``(title, artist)`` only, so when the SMTC artist is the browser
or channel name we end up re-running the SAME wrong query on ``/wrong``
and getting the SAME wrong "Shooting Star" back.

A YouTube video's DESCRIPTION usually carries the ground-truth credits
(``作詞・作曲：kors k`` / ``歌唱：ReGLOSS``) right at the top. We pull those
out, in one cheap metadata-only ``yt_dlp`` call (no audio, no captions),
and hand the parsed fields to ``fetch_lrc`` as DISAMBIGUATORS — additional
artist candidates layered onto the existing query loop. The new signal
lets a re-fetch on ``/wrong`` actually try a different artist.

The module is intentionally tiny + standalone:

  - ``extract_video_metadata(url_or_id)`` -> dict | None
  - lazy imports yt_dlp (already a bundled dep — see DesktopKaraoke.spec)
  - reuses ``deep_transcribe._normalize_youtube_url`` + ``_yt_variants``
    so the same anti-bot cookie / client chain protects us from the
    PO-token 403s that broke the default web client
  - module-level LRU keyed by 11-char video id; second hit is <1 ms
  - hard ``socket_timeout=8`` so a cold network can't stall the tick
  - every failure is swallowed and returns ``None``
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any

log = logging.getLogger("karaoke")

# ──────────────────────────────────────────────────────────────────────
# Module-level cache. Keyed by canonical 11-char video id so all of
# {watch?v=VID, youtu.be/VID, music.youtube.com/watch?v=VID} share one
# entry. LRU-capped to keep a long session bounded.
# ──────────────────────────────────────────────────────────────────────
_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_CACHE_MAX = 64
_CACHE_LOCK = threading.Lock()

# Single-flight guard: at most one extraction in flight per video id at any
# time, globally. Mirrors the _captions_fetching guard in main.py — protects
# against a fast-playlist cache stampede that could 429 us off YouTube.
_INFLIGHT: set[str] = set()
_INFLIGHT_LOCK = threading.Lock()


def _video_id(url_or_id: str) -> str | None:
    """Pull the 11-char video id out of any common YouTube URL shape, or
    return a bare 11-char id unchanged. None when we can't recognise it."""
    q = (url_or_id or "").strip()
    if not q:
        return None
    if re.fullmatch(r"[\w-]{11}", q):
        return q
    # watch?v=, /v/, /embed/, /shorts/, youtu.be/<id>
    m = re.search(r"(?:v=|/v/|/embed/|/shorts/|youtu\.be/)([\w-]{11})", q)
    return m.group(1) if m else None


# ──────────────────────────────────────────────────────────────────────
# Label dispatch table.
#
# Each label maps to one of the public output fields. JP labels are
# matched WITHOUT requiring a trailing colon (Japanese description
# convention often uses whitespace alone — "作詞 kors k"). EN/KR labels
# require ':' or '：' to avoid false-firing on prose like "Listen to my
# Music!".
# ──────────────────────────────────────────────────────────────────────
_FIELD_COMPOSER = "composer"
_FIELD_LYRICIST = "lyricist"
_FIELD_ARRANGER = "arranger"
_FIELD_VOCALS = "vocals"
_FIELD_ORIGINAL = "original_artist"

# (label_regex, fields, requires_colon)
# A label with multiple fields (the combined 作詞・作曲 case) puts the same
# value into every listed field — composer AND lyricist for kors k.
_LABEL_TABLE: list[tuple[re.Pattern, list[str], bool]] = [
    # ── Japanese ── (colon optional — JP convention)
    (re.compile(r"^\s*作詞[・／/]作曲(?:[・／/]編曲)?\s*[:：]?\s*", re.I),
     [_FIELD_LYRICIST, _FIELD_COMPOSER], False),
    (re.compile(r"^\s*作詞作曲\s*[:：]?\s*", re.I),
     [_FIELD_LYRICIST, _FIELD_COMPOSER], False),
    (re.compile(r"^\s*作詞\s*[:：]?\s*", re.I), [_FIELD_LYRICIST], False),
    (re.compile(r"^\s*作曲\s*[:：]?\s*", re.I), [_FIELD_COMPOSER], False),
    (re.compile(r"^\s*編曲\s*[:：]?\s*", re.I), [_FIELD_ARRANGER], False),
    (re.compile(r"^\s*歌唱\s*[:：]?\s*", re.I), [_FIELD_VOCALS], False),
    (re.compile(r"^\s*(?:ボーカル|ヴォーカル|ボーカル|唄|歌)\s*[:：]?\s*", re.I),
     [_FIELD_VOCALS], False),
    (re.compile(r"^\s*Vo\.\s*[:：]?\s*", re.I), [_FIELD_VOCALS], False),
    (re.compile(r"^\s*演奏\s*[:：]?\s*", re.I), [_FIELD_VOCALS], False),
    (re.compile(r"^\s*(?:原曲|カバー元|Original\s*(?:by)?|Cover\s*of)\s*[:：]\s*", re.I),
     [_FIELD_ORIGINAL], True),
    # ── English ── (colon required — too generic without)
    (re.compile(r"^\s*Music\s*(?:\&|and)\s*Lyrics\s*[:：]\s*", re.I),
     [_FIELD_COMPOSER, _FIELD_LYRICIST], True),
    (re.compile(r"^\s*(?:Music|Composed\s*by|Composer)\s*[:：]\s*", re.I),
     [_FIELD_COMPOSER], True),
    (re.compile(r"^\s*(?:Lyrics|Lyricist|Written\s*by)\s*[:：]\s*", re.I),
     [_FIELD_LYRICIST], True),
    (re.compile(r"^\s*(?:Arrangement|Arranger|Arranged\s*by)\s*[:：]\s*", re.I),
     [_FIELD_ARRANGER], True),
    (re.compile(r"^\s*(?:Vocals?|Vocalist|Sung\s*by|Performed\s*by|Singer)\s*[:：]\s*", re.I),
     [_FIELD_VOCALS], True),
    # ── Korean ── (colon optional, KR follows JP convention too)
    (re.compile(r"^\s*작사[·/／]작곡\s*[:：]?\s*", re.I),
     [_FIELD_LYRICIST, _FIELD_COMPOSER], False),
    (re.compile(r"^\s*작사\s*[:：]?\s*", re.I), [_FIELD_LYRICIST], False),
    (re.compile(r"^\s*작곡\s*[:：]?\s*", re.I), [_FIELD_COMPOSER], False),
    (re.compile(r"^\s*편곡\s*[:：]?\s*", re.I), [_FIELD_ARRANGER], False),
    (re.compile(r"^\s*노래\s*[:：]?\s*", re.I), [_FIELD_VOCALS], False),
]

# Lines that LOOK like they have a label but are actually channel boilerplate.
# Caught by: contains URL, '@handle', '#hashtag', or markers like 'Subscribe',
# 'Listen on', 'Follow', 'Twitter', 'Spotify', 'Apple Music', 'Bandcamp'.
_BOILERPLATE = re.compile(
    r"(https?://|www\.|@\w+|(?<![-\w])#\w+|"
    r"\b(?:subscribe|follow|listen\s+on|stream|buy|download|"
    r"twitter|instagram|tiktok|spotify|apple\s*music|bandcamp|"
    r"discord|patreon|merch)\b)",
    re.I)

# Per-field length cap so a chatty paragraph after a colon can't poison
# the artist candidate list.
_VALUE_CAP = 200

# 'feat.' / 'ft.' inside a value: pull as additional vocalists.
_FEAT = re.compile(r"\bfeat\.?\s+|\bft\.?\s+|\bfeaturing\s+", re.I)

# Suffix tokens we must NOT mis-split on '・' / '/' (so "Hatsune Miku・Append"
# stays whole; only multi-name lists like "GUMI・KAITO" split).
_SUFFIX_NOSPLIT = {"append", "ver", "version", "mix", "edit", "remix", "cover",
                   "live", "acoustic", "instrumental"}

# Separator strategy: split on these BETWEEN names. '・' is ambiguous (used
# in romanized JP names like 'kors k' — no, that's a space; but used as
# katakana middle dot inside Append etc.). We split only when each resulting
# token is >= 2 chars and not a known suffix word.
_SOFT_SEPS = ("/", "／", "、", ",", "・", "&")

# ── Description-embedded setlist parser ─────────────────────────────────
# Many concert uploads (indie / VTuber lives / EXPO self-produced shows
# like ZUTOMAYO's ずっと真夜中でいいのに。EXPO2025) don't set YouTube's
# native CHAPTERS metadata — they paste a plain "MM:SS Title" list into
# the description instead. The existing setlist path only reads
# info["chapters"], so it misses these entirely. This parser recovers
# that setlist so chapter-based recognition works on those videos too.
#
# Matched shapes (real-world YouTube conventions, all seen in the wild):
#   00:00 Song A
#   1:23:45 Song B                   ← HH:MM:SS
#   [00:00] Song C                   ← bracketed timestamp
#   (04:12) Song D                   ← parenthesized
#   00:00｜Song E                    ← full-width bar
#   00:00 - Song F  |  00:00 / Song G
#   00:00 曲名                       ← CJK title, no separator
#
# We deliberately require a SPACE or explicit separator between the
# timestamp and the title so a stray "at 04:12 today" prose line
# doesn't parse as a chapter.
_SETLIST_LINE_RE = re.compile(
    r"^[ \t　]*"
    r"[\[\(【（]?"                                   # optional open bracket
    r"(?P<t>(?:\d{1,2}:)?\d{1,2}:\d{2})"           # HH:MM:SS or MM:SS
    r"[\]\)】）]?"                                   # optional close bracket
    r"[ \t　\|｜／/・．\.\-–—~〜]{1,4}"          # separator (space or punct)
    r"(?P<title>[^\r\n]+?)"                         # title (non-greedy)
    r"[ \t　]*$",
    re.M,
)


def _hms_to_sec(hms: str) -> float:
    """'MM:SS' or 'HH:MM:SS' → seconds. Returns 0.0 on any parse failure."""
    try:
        parts = [int(p) for p in hms.split(":")]
    except ValueError:
        return 0.0
    if len(parts) == 2:
        return parts[0] * 60.0 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600.0 + parts[1] * 60.0 + parts[2]
    return 0.0


# Titles that look like a timestamp entry but are really header/section
# labels ("Setlist", "Tracklist", "セットリスト", "曲順") — reject so the
# monotonic filter below doesn't get seeded with garbage.
_SETLIST_HEADER_RE = re.compile(
    r"^(setlist|tracklist|songs?|track\s*list|"
    r"セットリスト|曲順|曲目|楽曲|プレイリスト)\s*[:：]?\s*$",
    re.I,
)


def parse_setlist_timestamps(desc: str) -> list[dict]:
    """Pull a per-song setlist out of a video's DESCRIPTION.

    Returns a list of ``{"start": float, "title": str}`` compatible with
    the yt-dlp ``chapters`` shape, so callers can plug it into the same
    ``_concert_setlist_tick`` pipeline that consumes native chapters.

    Guards against false positives:
      - requires ≥2 entries (a single timestamp is just prose)
      - strips trailing ``(feat. …)`` / ``[…]`` credit blocks so they
        don't inflate the title-length check
      - drops entries with empty or over-long (>80 char) titles
      - rejects header lines ("Setlist" / "セットリスト") that happen
        to sit next to a timestamp
      - de-duplicates exact ``(start, title)`` repeats
      - enforces monotonic increasing starts (drops any backwards entry —
        common when a description lists times twice, or when the
        description mentions "back at 00:00" prose lower down)

    Returns ``[]`` on any failure or when the result would be shorter
    than 2 entries after filtering.
    """
    if not desc:
        return []
    hits: list[dict] = []
    for m in _SETLIST_LINE_RE.finditer(desc):
        t = _hms_to_sec(m.group("t"))
        title = (m.group("title") or "").strip()
        # Strip trailing credit / annotation brackets so a line like
        #  '00:00 Song A (music: kors k)' keeps just 'Song A'.
        title = re.sub(r"\s*[\(\[【（].*?[\)\]】）]\s*$", "", title).strip()
        # Strip a trailing separator artifact ('Song A -' / 'Song A |').
        title = re.sub(r"[ \t\|｜／/・．\.\-–—~〜]+$", "", title).strip()
        if not title or len(title) > 80:
            continue
        if _SETLIST_HEADER_RE.match(title):
            continue
        hits.append({"start": float(t), "title": title})
    if len(hits) < 2:
        return []
    # De-dup exact repeats first (some descriptions list the setlist
    # twice — once in JP, once in EN — with identical timestamps).
    seen: set[tuple[float, str]] = set()
    deduped: list[dict] = []
    for h in hits:
        key = (h["start"], h["title"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(h)
    # Enforce monotonic increasing starts. Prose like "back at 04:12"
    # lower in the description would otherwise re-anchor.
    mono = [deduped[0]]
    for h in deduped[1:]:
        if h["start"] > mono[-1]["start"]:
            mono.append(h)
    return mono if len(mono) >= 2 else []


def _strip_decoration(s: str) -> str:
    """Strip leading decorative chars (★☆♪・■▪︎▶◆◇♥♡✿◎) and whitespace."""
    return re.sub(r"^[\s★☆♪♫■□▪▶◆◇♥♡✿◎◯●→▼▽–—\-=*]+", "", s or "")


# ── Description-song candidate extractor ─────────────────────────────────
# Livestream concerts (VTuber birthday shows, anniversary streams) have NO
# native chapters and NO description-timestamp setlist — YouTube's chapter
# UI wasn't the right fit for a chatty 1-hour stream with songs interspersed.
# But the description ALWAYS carries the artist's own song list, marked with
# section headers like オリジナル曲宣伝 / カバー曲宣伝 / Original / Cover:
#
#   ✨オリジナル曲宣伝✨
#   💜【Original】Dunk/轟はじめ💜
#   💜【Original】BANZAI-轟はじめ💜
#   ✨COVER曲宣伝✨
#   BANDAGE-Ayumu Imazu /cover
#   夜咄ディセイブ/歌ってみた【轟はじめ/ReGLOSS】
#
# These are the songs the artist ACTUALLY performs — a scoped candidate pool
# far better than blind Shazam for the livestream case. Extract them so
# downstream code can fetch each candidate's lyrics ahead of time, then
# match live vocal transcripts against just this pool instead of against
# all of LRCLIB.

_CAND_SECTION_START_RE = re.compile(
    r"(?:オリジナル曲|カバー曲|COVER曲|カバー|オリジナル|"
    r"original\s*(?:song)?s?|cover\s*(?:song)?s?|"
    r"performed\s*songs?|setlist|track\s*list|楽曲一覧|セットリスト)",
    re.I,
)
# STRICT header — fullmatch a plain section header line (nothing after).
# Header + optional decorative suffix (宣伝 = "promotion/showcase" is the
# canonical VTuber convention), colon, or numbered marker.
_CAND_SECTION_HEADER_STRICT_RE = re.compile(
    r"^(?:オリジナル|カバー|COVER|Original|Cover)"
    r"\s*(?:曲|songs?|list)?"
    r"\s*(?:宣伝|一覧|selection|list)?"
    r"\s*(?:section)?\s*[:：]?\s*$",
    re.I,
)
# All emoji + decorative chars we want to STRIP from a candidate line before
# splitting on separators. Broad Unicode ranges: pictographic emoji + symbols
# + hearts + suits + decorative markers.
_CAND_STRIP_RE = re.compile(
    r"[\U0001F300-\U0001FAFF"
    r"\U0001F600-\U0001F64F"
    r"\U0001F680-\U0001F6FF"
    r"\U00002600-\U000027BF"
    r"★☆♪♫■□▪▶◆◇♥♡✿◎◯●→▼▽]+"
)
# Full-width punctuation we normalize to ASCII to keep the "NEW!!" prefix
# strip simple.
_CAND_FULLWIDTH_MAP = str.maketrans({"！": "!", "？": "?", "：": ":", "，": ","})
# Inline section markers we strip before title matching: 【Original】,
# 【Cover】, 【MV】, 【Official MV】, 【歌ってみた】, 【〜】 variants.
_CAND_INLINE_MARKER_RE = re.compile(
    r"【\s*(?:original|cover|カバー|オリジナル|mv|"
    r"official\s*mv|歌ってみた|cover\s*ver|off\s*vocal|"
    r"instrumental|acoustic)\s*】",
    re.I,
)
# Noise prefixes that sit before the title: NEW!!, ★NEW★, 【NEW】.
_CAND_PREFIX_NOISE_RE = re.compile(
    r"^\s*(?:new\s*!*\s*|【\s*new\s*】\s*|【\s*完成\s*】\s*|"
    r"latest\s*!*\s*|最新\s*)+",
    re.I,
)
# Numbered-list prefix ("1. ", "01) ", "M1  ", "① ").
_CAND_LIST_NUM_RE = re.compile(
    r"^\s*(?:\d{1,2}[\.\)\-））]|\d{1,2}\s+|"
    r"[①-⑳㈠-㈩]|M\d{1,2}\s+|"
    r"[\-–—•・◆▶]\s+)\s*",
    re.I,
)
# Titles that are obviously NOT songs (headers, boilerplate).
_CAND_NOISE_RE = re.compile(
    r"^(オリジナル曲宣伝|カバー曲宣伝|オリジナル曲|カバー曲|Original|Cover|"
    r"Music|Songs?|Setlist|Tracklist|楽曲一覧|セットリスト|"
    r"http|www\.|@|#|Special\s*Thanks|Credits?|"
    r"[\s\|\-＿_=]*$)",
    re.I,
)


def parse_song_candidates(desc: str, max_candidates: int = 24) -> list[dict]:
    """Extract a candidate pool of songs the artist performs, from the
    description's song-list sections.

    Returns a list of ``{"title": str, "kind": "original"|"cover"|"unknown"}``
    dicts, deduped, in order of first appearance. The ``kind`` field lets
    downstream ranking prefer originals (uniquely identifying) over covers
    (which collide with many other artists' recordings).

    Runs a simple state machine: walk lines top-to-bottom, activate a
    "collecting" mode when a section-header line is seen, then for each
    subsequent non-empty non-URL line try to extract a title until we hit
    a divider (======= / ______ / a new section header of a non-song kind).
    """
    if not desc:
        return []
    kind = None                       # 'original' | 'cover' | None
    out: list[dict] = []
    seen: set[str] = set()
    for raw in desc.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Section divider — reset kind.
        if re.fullmatch(r"[\s\-_=￣ー\|\.·・]{4,}", line):
            kind = None
            continue
        # Section header — activate collection mode. Only fire on a line
        # that is JUST the header (after decoration strip) — e.g.
        # "オリジナル曲宣伝" or "Cover:" or "Setlist". A line like
        # "【Original】BANZAI-轟はじめ" starts with "Original" but has song
        # content after it, so the STRICT fullmatch rejects it and the
        # candidate branch below picks up the song.
        _header_probe = _CAND_STRIP_RE.sub(" ", line).strip()
        _header_probe = re.sub(r"\s+", " ", _header_probe).strip(" 　【】[]")
        if _header_probe and _CAND_SECTION_HEADER_STRICT_RE.fullmatch(_header_probe):
            low = _header_probe.lower()
            if "cover" in low or "カバー" in _header_probe:
                kind = "cover"
            elif "オリジナル" in _header_probe or "original" in low:
                kind = "original"
            else:
                kind = "unknown"
            continue
        # Broader header pattern for "Setlist" / "楽曲一覧" / "セットリスト".
        if _header_probe and _CAND_SECTION_START_RE.fullmatch(_header_probe.rstrip(":： ")):
            low = _header_probe.lower()
            if "cover" in low or "カバー" in _header_probe:
                kind = "cover"
            else:
                kind = "unknown"
            continue
        if kind is None:
            continue
        # Reject obvious noise / boilerplate.
        if _CAND_NOISE_RE.match(line):
            continue
        if "http://" in line or "https://" in line or "www." in line:
            continue
        # Normalize:
        #  1. Full-width punct → ASCII so 'NEW！！' becomes 'NEW!!'.
        #  2. Strip all emoji / decorative glyphs anywhere in the line
        #     (VTuber descriptions sandwich the title in 💜 / 🏀 pairs).
        #  3. Strip inline section markers 【Original】 / 【Cover】 / 【MV】.
        #  4. Strip 'NEW!!' / '【NEW】' prefixes.
        norm = line.translate(_CAND_FULLWIDTH_MAP)
        norm = _CAND_STRIP_RE.sub(" ", norm)
        norm = _CAND_INLINE_MARKER_RE.sub(" ", norm)
        norm = _CAND_PREFIX_NOISE_RE.sub("", norm)
        norm = _CAND_LIST_NUM_RE.sub("", norm)
        norm = norm.strip()
        if not norm:
            continue
        # Now the title is the first non-empty segment before a
        # separator that introduces artist / cover-ver / MV etc:
        #   'Dunk/轟はじめ'   → 'Dunk'
        #   'BANZAI-轟はじめ' → 'BANZAI'
        #   '踊り子-Vaundy(Cover)/…' → '踊り子'
        # Split on the first of: /, ／, -, –, —, 【, [, (, （
        parts = re.split(r"[/／\-–—【\[\(（]", norm, maxsplit=1)
        title = parts[0].strip()
        # Some lines have artist-song reversed with '/' separator (rare in
        # song-list sections, but handle 'ReGLOSS/FeelingRadation' shape by
        # keeping the LEFT side — song-list sections nearly always put
        # song first).
        # Reject too-short / too-long titles or titles that are still
        # obvious noise.
        if len(title) < 2 or len(title) > 60:
            continue
        if _CAND_NOISE_RE.match(title):
            continue
        # Dedup on lowercased-normalized title so casing/spacing doesn't
        # create duplicates.
        key = re.sub(r"\s+", " ", title.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": title, "kind": kind})
        if len(out) >= max_candidates:
            break
    return out


def _split_names(val: str) -> list[str]:
    """Split a credit value into individual names.

    Conservative: only split on a separator when both resulting tokens are
    non-empty, >=2 chars, AND not a known suffix word ('Append', 'ver').
    'kors k' stays whole; 'GUMI・KAITO' becomes ['GUMI', 'KAITO']; parenthesized
    member lists 'ReGLOSS(音乃瀬奏/一条莉々華/儒烏風亭らでん/轟はじめ)' yield BOTH
    'ReGLOSS' and each member name."""
    val = (val or "").strip()
    if not val:
        return []
    out: list[str] = []

    # Pull a parenthesized member list out: keep the head AND every member.
    paren_match = re.match(r"\s*([^()（）]+?)\s*[（(]([^()（）]+)[)）]\s*$", val)
    if paren_match:
        head = paren_match.group(1).strip()
        members = paren_match.group(2).strip()
        if head:
            out.append(head[:_VALUE_CAP])
        out.extend(_split_names(members))
        return _dedupe_preserve_order(out)

    # 'feat.' / 'ft.' splits: 'X feat. Y' -> ['X', 'Y']
    if _FEAT.search(val):
        parts = [p.strip() for p in _FEAT.split(val) if p and p.strip()]
        for p in parts:
            out.extend(_split_names(p))
        return _dedupe_preserve_order(out)

    # Try each soft separator IN ORDER. First one that yields >=2 valid
    # tokens wins.
    for sep in _SOFT_SEPS:
        if sep not in val:
            continue
        parts = [p.strip() for p in val.split(sep)]
        good = [p for p in parts if len(p) >= 2 and p.lower() not in _SUFFIX_NOSPLIT]
        if len(good) >= 2:
            for p in good:
                out.extend(_split_names(p) if any(s in p for s in _SOFT_SEPS) else [p[:_VALUE_CAP]])
            return _dedupe_preserve_order(out)

    return [val[:_VALUE_CAP]]


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = (it or "").strip()
        if not key:
            continue
        k_low = key.lower()
        if k_low in seen:
            continue
        seen.add(k_low)
        out.append(key)
    return out


def _parse_description(desc: str) -> dict[str, list[str]]:
    """Line-by-line scan for credit labels. Returns a dict of field -> list
    of names (always lists, even single-credit fields). Empty values stay
    empty lists."""
    fields: dict[str, list[str]] = {
        _FIELD_COMPOSER: [],
        _FIELD_LYRICIST: [],
        _FIELD_ARRANGER: [],
        _FIELD_VOCALS: [],
        _FIELD_ORIGINAL: [],
    }
    if not desc or len(desc) < 30:
        return fields

    for raw_line in desc.splitlines():
        line = _strip_decoration(raw_line).rstrip()
        if not line:
            continue
        # Skip channel-boilerplate lines BEFORE trying field extraction —
        # otherwise 'Vocals: Subscribe!' would poison the vocals list.
        for pat, target_fields, requires_colon in _LABEL_TABLE:
            m = pat.match(line)
            if not m:
                continue
            value = line[m.end():].strip()
            if not value:
                continue
            if _BOILERPLATE.search(value):
                # The VALUE is boilerplate — skip. (The label-after-decoration
                # match is fine; only the right-hand-side content is suspect.)
                continue
            if len(value) > _VALUE_CAP:
                value = value[:_VALUE_CAP]
            names = _split_names(value)
            if not names:
                continue
            for f in target_fields:
                fields[f] = _dedupe_preserve_order(fields[f] + names)
            break  # don't double-match a line against a second label
    return fields


def _detect_language(desc: str) -> str | None:
    """Best-effort language hint from a description's script mix. Returns
    'ja', 'ko', 'zh', or None — used downstream as a soft prior, not a
    strict filter."""
    if not desc:
        return None
    has_kana = bool(re.search(r"[぀-ゟ゠-ヿ]", desc))
    has_hangul = bool(re.search(r"[가-힯]", desc))
    has_han = bool(re.search(r"[一-鿿]", desc))
    if has_kana:
        return "ja"
    if has_hangul:
        return "ko"
    if has_han:
        return "zh"
    return None


def _lyrics_block(desc: str) -> str | None:
    """Pull a LYRICS block out of the description if one is clearly marked
    ('歌詞', 'Lyrics:', '가사'). Returned VERBATIM (capped) so the AI-gen
    fallback can seed against it; NOT used as a fetch query (too noisy)."""
    if not desc:
        return None
    marker = re.search(r"(?:^|\n)\s*(?:歌詞|가사|Lyrics)\s*[:：]?\s*\n",
                       desc, re.I)
    if not marker:
        return None
    body = desc[marker.end():].strip()
    if len(body) < 40:
        return None
    return body[:4000]


def _extract_one(url: str, timeout: float) -> dict[str, Any] | None:
    """Single yt_dlp.extract_info call wrapped in the same anti-bot variant
    chain ``deep_transcribe.fetch_captions_only`` uses. Returns the raw info
    dict or None on any failure."""
    try:
        import deep_transcribe
        if not deep_transcribe.available():
            return None
        import yt_dlp
        from deep_transcribe import _yt_variants, _normalize_youtube_url
    except Exception:
        return None

    target = _normalize_youtube_url(url)
    if re.fullmatch(r"[\w-]{11}", target):
        target = f"https://www.youtube.com/watch?v={target}"

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "socket_timeout": max(2.0, float(timeout)),
        "extract_flat": False,
        # Metadata path is lighter than the audio path — keep retries TIGHT
        # so a cold/blocked fetch doesn't burn a worker thread for 30s.
        # _yt_variants → _resilient bumps retries to 5 (right for audio),
        # we override back down here.
        "retries": 1,
        "extractor_retries": 1,
    }

    last_err: Exception | None = None
    for vopts in _yt_variants(opts):
        # Re-clamp retries — _resilient bumped them.
        vopts = dict(vopts)
        vopts["retries"] = 1
        vopts["extractor_retries"] = 1
        try:
            with yt_dlp.YoutubeDL(vopts) as y:
                info = y.extract_info(target, download=False)
            if isinstance(info, dict):
                return info
        except Exception as e:
            last_err = e
            continue
    if last_err is not None:
        log.debug("yt_description: extract failed for %r: %s",
                  target, str(last_err)[:140])
    return None


def extract_video_metadata(url_or_id: str,
                           *, timeout: float = 8.0) -> dict[str, Any] | None:
    """Fetch + parse a YouTube video's description into structured credits.

    Returns a dict with these keys (lists are always present, possibly
    empty; scalars may be None):

      video_id        : canonical 11-char id
      title_raw       : the video's own title string
      channel         : YouTube channel name
      uploader        : yt_dlp uploader (channel fallback)
      description     : the raw description text (capped to 8 KB)
      composer        : list[str]  (e.g. ['kors k'])
      lyricist        : list[str]
      arranger        : list[str]
      vocals          : list[str]  (e.g. ['ReGLOSS', '音乃瀬奏', ...])
      original_artist : list[str]  (for cover/カバー元 lines)
      lyrics_block    : str | None (verbatim 歌詞/Lyrics body if present)
      language        : 'ja' | 'ko' | 'zh' | None
      tags            : list[str]  (yt_dlp tags array, capped)
      upload_date     : 'YYYYMMDD' | None
      fetched_at      : float  (epoch seconds — for cache TTL)
      from_cache      : bool   (True when served from the module LRU)

    Returns ``None`` on any failure (network, yt_dlp missing, parse error).
    """
    vid = _video_id(url_or_id)
    if not vid:
        return None

    # Cache hit — bump LRU and return a copy so callers can mutate freely.
    with _CACHE_LOCK:
        if vid in _CACHE:
            _CACHE.move_to_end(vid)
            cached = dict(_CACHE[vid])
            cached["from_cache"] = True
            return cached

    # Single-flight: if another thread is already fetching this id, just
    # bail. A re-watch (or a re-invoke after URL settles) will hit cache.
    with _INFLIGHT_LOCK:
        if vid in _INFLIGHT:
            return None
        _INFLIGHT.add(vid)

    try:
        info = _extract_one(url_or_id, timeout)
        if not info:
            return None

        desc = (info.get("description") or "")
        if len(desc) > 8192:
            desc = desc[:8192]

        parsed = _parse_description(desc)
        tags = info.get("tags") or []
        if isinstance(tags, list):
            tags = [str(t)[:80] for t in tags[:32]]
        else:
            tags = []

        result: dict[str, Any] = {
            "video_id": vid,
            "title_raw": info.get("title") or None,
            # Concert setlist: YouTube CHAPTERS carry per-song titles + exact
            # start times on most 3D-live uploads — deterministic recognition
            # (main.py _concert_setlist_tick consumes these in live mode).
            "chapters": [{"start": float(c.get("start_time") or 0.0),
                          "title": str(c.get("title") or "")[:120]}
                         for c in (info.get("chapters") or [])
                         if isinstance(c, dict)],
            "channel": info.get("channel") or None,
            "uploader": info.get("uploader") or None,
            "description": desc or None,
            _FIELD_COMPOSER: parsed[_FIELD_COMPOSER],
            _FIELD_LYRICIST: parsed[_FIELD_LYRICIST],
            _FIELD_ARRANGER: parsed[_FIELD_ARRANGER],
            _FIELD_VOCALS: parsed[_FIELD_VOCALS],
            _FIELD_ORIGINAL: parsed[_FIELD_ORIGINAL],
            "lyrics_block": _lyrics_block(desc),
            "language": _detect_language(desc),
            "tags": tags,
            "upload_date": info.get("upload_date") or None,
            # Category + true duration (SMTC often reports None) — used by the
            # concert-setlist category gate and available to any caller.
            "categories": [str(c)[:40] for c in (info.get("categories") or [])
                           if c][:8],
            "duration": (float(info.get("duration"))
                         if info.get("duration") else None),
            "fetched_at": time.time(),
            "from_cache": False,
        }

        with _CACHE_LOCK:
            _CACHE[vid] = dict(result)
            _CACHE.move_to_end(vid)
            while len(_CACHE) > _CACHE_MAX:
                _CACHE.popitem(last=False)

        log.info("yt_description: %s — composer=%s lyricist=%s vocals=%s original=%s",
                 vid,
                 result[_FIELD_COMPOSER][:2] or "-",
                 result[_FIELD_LYRICIST][:2] or "-",
                 result[_FIELD_VOCALS][:2] or "-",
                 result[_FIELD_ORIGINAL][:2] or "-")
        return result
    except Exception as e:
        log.debug("yt_description: unexpected failure for %r: %s",
                  url_or_id, str(e)[:140])
        return None
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(vid)


def cache_clear() -> None:
    """Drop the module LRU. Test helper; not wired to any runtime path."""
    with _CACHE_LOCK:
        _CACHE.clear()


def cache_size() -> int:
    with _CACHE_LOCK:
        return len(_CACHE)
