# -*- coding: utf-8 -*-
"""High-quality OFFLINE lyric transcription — the "do it properly" pass.

WHY THIS EXISTS
---------------
The realtime generator (``align.transcribe_for_generation``) is a FAST,
best-effort pass: it transcribes short WASAPI-loopback chunks with a *small*
model WHILE the song plays, so it is necessarily incomplete and a bit rough — it
is racing the playhead and can only ever hear "so far". That is the right call
for the FIRST listen (instant, no waiting).

But for a song that has NO real synced lyrics anywhere — the only time we fall
back to generation at all — we can do much better in the background and replace
the rough cache with a clean, complete one:

  1. DOWNLOAD the source audio with yt-dlp (audio-only ``bestaudio`` — no ffmpeg
     needed; faster-whisper / PyAV decode the .webm/.m4a directly).
  2. TRANSCRIBE the WHOLE file with a LARGE model (faster-whisper ``large-v3``)
     and a wider beam — accurate and complete, because it is offline and not
     racing the playhead.
  3. Hand the timed lines back to the caller to annotate (furigana / romaji /
     translation) + cache, replacing the best-effort version.
  4. DELETE the downloaded audio — only the lyrics are kept (saves disk).

Everything degrades gracefully: missing yt-dlp, a network error, an over-long
("wrong video" / concert) match, or no speech → returns ``None`` and the
realtime best-effort generation simply stands. See ``docs/GENERATION.md``.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger("karaoke")

_DEEP_SIZE = "medium"     # offline quality/speed balance: ~as accurate as large-v3
#                           on clear vocals, but small enough to fit GPU alongside
#                           the running app (large-v3 spilled to CPU → ~4min). Both
#                           are pre-cached; bump to "large-v3" for max accuracy.
_MAX_DUR = 900            # >15 min ⇒ almost certainly a concert/loop, not the song

# ── yt-dlp anti-bot ──────────────────────────────────────────────────────────
# YouTube periodically 403s / rate-limits the audio + caption download (heavy use
# trips it). We add resilience that does NOT regress normal videos: a realistic
# User-Agent, more retries, and a polite per-request delay — then a 2nd attempt
# WITH browser cookies (authenticated requests are blocked less). We deliberately
# DON'T force player_client: yt-dlp's own client selection is smart, and forcing
# ios/tv mis-reported "DRM protected" on videos that download fine by default
# (observed live on the KAF MV). Browser cookies are OPT-IN (env DK_COOKIES_BROWSER):
# Chromium locks its cookie DB while running (yt-dlp #7271), so they only work when
# that browser is closed — off by default to avoid pointless errors.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_COOKIE_BROWSERS = ("brave", "chrome", "edge", "vivaldi", "opera", "chromium", "firefox")


def _cookie_browser():
    """Browser for cookies-from-browser — OPT-IN via env ``DK_COOKIES_BROWSER`` (e.g.
    'brave'). Off by default because Chromium locks its cookie DB while running
    (yt-dlp #7271), so cookies only work when that browser is CLOSED (or a non-running
    one). Returns the browser name, or None when unset/unknown."""
    env = os.environ.get("DK_COOKIES_BROWSER", "").strip().lower()
    return env if env in _COOKIE_BROWSERS else None


def _resilient(opts: dict) -> dict:
    """A copy of `opts` with anti-bot RESILIENCE that won't regress normal videos: a
    realistic User-Agent, more retries, and a polite per-request delay (rides out
    transient 403s / rate-limiting). Intentionally does NOT force player_client —
    forcing ios/tv mis-reports "DRM protected" on videos that work by default."""
    o = dict(opts)
    hdr = dict(o.get("http_headers") or {})
    hdr.setdefault("User-Agent", _UA)
    o["http_headers"] = hdr
    o["retries"] = max(int(o.get("retries", 3) or 3), 5)
    o["fragment_retries"] = 5
    o["extractor_retries"] = 3
    o["sleep_interval_requests"] = 1
    return o


def _yt_variants(base: dict):
    """yt-dlp opt sets to try IN ORDER until one works.

    v1.1.65 revision — 4-tier cascade:
      1. RESILIENT default clients (yt-dlp picks; UA + retries): works for
         the majority of songs / MV uploads.
      2. + browser cookies (if DK_COOKIES_BROWSER is set): authenticated
         requests get past the age gate and get less rate-limited.
      3. FORCED ios/tv/mweb clients: yt-dlp's web player client now returns
         a bare 'Video unavailable' without a PO token for many videos
         (fan compilations, game clips, older uploads). Forcing the mobile
         clients gets ASR captions when the web path was walled off.
      4. + browser cookies with forced clients: last-resort combo.

    The old note ("don't force player_client — mis-reported 'DRM protected'
    on the KAF MV") was for DOWNLOADS. Caption-only extraction is
    skip_download=True, so no DRM check runs — safe to force the mobile
    clients as a fallback. Ordering (default first, forced last) preserves
    the KAF-class videos: they succeed on the default before the forced
    variant is ever tried."""
    variants = [_resilient(base)]
    b = _cookie_browser()
    if b:
        v = _resilient(base)
        v["cookiesfrombrowser"] = (b,)
        variants.append(v)
    # Forced mobile-client variants — bypass the web PO-token wall.
    forced = _resilient(base)
    forced_ea = dict(forced.get("extractor_args") or {})
    _yt_ea = dict(forced_ea.get("youtube") or {})
    _yt_ea["player_client"] = ["ios", "tv", "mweb", "web"]
    forced_ea["youtube"] = _yt_ea
    forced["extractor_args"] = forced_ea
    variants.append(forced)
    if b:
        vf = dict(forced)
        vf["cookiesfrombrowser"] = (b,)
        variants.append(vf)
    return variants


def available() -> bool:
    """True if yt-dlp is importable — the only extra dependency beyond the
    faster-whisper stack the realtime generator already needs."""
    try:
        import yt_dlp  # noqa: F401
        return True
    except Exception:
        return False


def _download_audio(query: str, dest: Path,
                    max_dur: int | None = None) -> tuple[Path | None, str | None]:
    """ytsearch the query and download the top hit's AUDIO-ONLY stream into
    ``dest``. Returns (downloaded file path, the hit's CANONICAL title), or
    (None, None) on any failure / an over-long match (a concert or "1 hour loop"
    is not the song we want). The canonical title is the video's REAL name — often
    the Japanese original even when the player reported an English/translated one —
    so the caller can look up real lyrics before transcribing by ear.

    `max_dur` overrides the song-sized duration cap — SUBTITLES mode transcribes
    whole EPISODES (20-40 min), which the default would reject."""
    import shutil as _sh
    import yt_dlp
    out = str(dest / "src.%(ext)s")
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "default_search": "ytsearch1",
        "format": "bestaudio/best",          # audio-only ⇒ no ffmpeg merge needed
        "outtmpl": out, "retries": 3, "socket_timeout": 30,
        # reject an over-long top hit (concert / compilation / hour-long loop)
        "match_filter": yt_dlp.utils.match_filter_func(
            f"duration < {int(max_dur or _MAX_DUR)}"),
        # Also grab the MANUAL caption track (NOT auto-captions): on an official MV
        # this is the EXACT official lyrics WITH the video's own timing — strictly
        # better than a provider LRC (correct words AND perfect sync).
        "writesubtitles": True, "writeautomaticsub": False,
        "subtitleslangs": ["ja", "zh-Hans", "zh-Hant", "zh", "ko"],
        "subtitlesformat": "vtt",
    }
    # YouTube now needs a JS runtime to mint un-throttled format URLs, else the
    # audio download 403s. yt-dlp only enables `deno` by default; opt in to
    # node when it's on PATH (it usually is — ships with VS Code/Electron). On a
    # machine with neither, the download may 403 and we degrade gracefully.
    if _sh.which("node"):
        opts["js_runtimes"] = {"node": {}}
    # TICKET-086: normalize music.youtube.com → www.youtube.com so the rewrite
    # is centralized at every yt-dlp entry point. If the input is already a URL
    # we pass it straight in (else yt-dlp would prepend ytsearch1: to a URL,
    # which it tolerates but is wasteful).
    search_q = _normalize_youtube_url(query)
    target = search_q if re.match(r"https?://", search_q) else f"ytsearch1:{search_q}"
    info_title, last_err = None, None
    for vopts in _yt_variants(opts):                 # cookies+anti-bot → anti-bot → plain
        try:
            with yt_dlp.YoutubeDL(vopts) as y:
                info = y.extract_info(target, download=True)
                if isinstance(info, dict):
                    ents = info.get("entries") or [info]
                    if ents and isinstance(ents[0], dict):
                        info_title = ents[0].get("title")
            if glob.glob(str(dest / "src.*")):       # got the audio → stop trying variants
                break
        except Exception as e:
            last_err = e
            continue
    files = glob.glob(str(dest / "src.*"))
    if not files:
        if last_err is not None:
            log.info("deep: download failed for %r: %s", query, str(last_err)[:140])
        return None, None
    return (Path(files[0]) if files else None), info_title


def _real_from_canonical(canon_title: str | None, artist: str):
    """Try to find REAL provider lyrics using the video's CANONICAL title (from
    yt-dlp) rather than transcribing by ear. yt-dlp reports the Japanese name even
    when the player gave an English/translated one ('KAF #27 - And Become a Flower'
    vs the real 花譜「そして花になる」), which is why songs with perfectly good provider
    lyrics were being generated. Returns (annotated lines, lang, meta) or None."""
    if not canon_title:
        return None
    import fetch_lyrics as F
    cands = []
    m = re.search(r"「([^」]+)」|『([^』]+)』", canon_title)   # song is in 「」 for JP uploads
    if m:
        cands.append(m.group(1) or m.group(2))
    base = re.sub(r"【[^】]*】|\([^)]*\)|（[^）]*）", "", canon_title)   # drop 【MV】/(tags)
    base = re.sub(r"^\s*[^#「」]*#\s*\d+\s*[-‐–—]?\s*", "", base).strip(" 　-–—|｜/／")
    if base:
        cands.append(base)
    cands.append(canon_title)
    seen = set()
    for t in cands:
        t = (t or "").strip()
        if len(t) < 2 or t.lower() in seen:
            continue
        seen.add(t.lower())
        try:
            lrc, meta = F.fetch_lrc(t, artist, cover=True)
        except Exception:
            continue
        if lrc:
            lines = F.parse_lrc_text(lrc)
            if len(lines) >= 6:
                lang2 = F._song_lang(lines)
                F.annotate(lines, lang2, translate=True)
                log.info("deep: REAL lyrics via canonical title %r — %d %s lines (%s), "
                         "skipping by-ear transcription", t, len(lines), lang2,
                         (meta or {}).get("source"))
                return lines, lang2, (meta or {"source": "provider"})
    return None


def transcribe_file(path: str | Path, lang: str | None = None,
                    size: str = _DEEP_SIZE) -> tuple[list[dict], str | None]:
    """Transcribe a WHOLE audio file with the large model. Returns
    ``(lines, detected_lang)`` where each line is
    ``{"t": [start, end], "jp": text, "rm": "", "en": ""}`` (raw text only;
    the caller annotates). ``vad_filter`` stays OFF: Silero VAD classifies SUNG
    vocals as non-speech and would drop them (learned in align.py)."""
    import align
    model = align._get_model(size)
    segments, info = model.transcribe(
        str(path), language=lang, beam_size=5,
        vad_filter=False,
        condition_on_previous_text=False,   # one mis-hear must not poison the rest
        no_speech_threshold=0.6,
    )
    lines: list[dict] = []
    for s in segments:
        txt = (s.text or "").strip()
        if txt:
            lines.append({"t": [round(s.start, 2), round(s.end, 2)],
                          "jp": txt, "rm": "", "en": ""})
    detected = getattr(info, "language", None) or lang
    return lines, detected


_CAP_CREDIT = re.compile(
    r"作詞|作曲|編曲|translat|字幕|subtitle|lyrics?\s*by|sub(?:title)?\s*by|転載|©|"
    r"youtube\.com|^@", re.I)


def _vtt_ts(s: str) -> float:
    s = s.strip().replace(",", ".")
    p = s.split(":")
    if len(p) == 3:
        return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2])
    if len(p) == 2:
        return int(p[0]) * 60 + float(p[1])
    return float(p[0])


def _parse_vtt(path: Path) -> list[dict]:
    """Parse a .vtt caption file → timed line dicts (credits / blank cues dropped,
    rolling-caption duplicates collapsed into one timed line)."""
    out, cur, buf = [], None, []

    def flush():
        nonlocal cur, buf
        if cur and buf:
            txt = re.sub(r"<[^>]+>", "", " ".join(buf)).strip()
            # Drop sound-event annotations — [音楽]/[Music]/【拍手】/♪ etc. are never
            # lyrics; bracketed tags in a caption are always non-vocal markers.
            txt = re.sub(r"[\[【][^\]】]*[\]】]", "", txt).replace("♪", "")
            txt = re.sub(r"\s+", " ", txt).strip()
            if txt and not _CAP_CREDIT.search(txt):
                out.append({"t": [round(cur[0], 2), round(cur[1], 2)],
                            "jp": txt, "rm": "", "en": ""})
        buf = []

    for L in path.read_text("utf-8", errors="ignore").splitlines():
        L = L.rstrip()
        m = re.match(r"(\d+:[\d:]+[.,]\d+)\s*-->\s*(\d+:[\d:]+[.,]\d+)", L)
        if m:
            flush(); cur = [_vtt_ts(m.group(1)), _vtt_ts(m.group(2))]; continue
        if not L:
            flush(); cur = None; continue
        if L.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")) or L.isdigit():
            continue
        buf.append(L)
    flush()
    ded = []
    for ln in out:                       # collapse consecutive identical captions
        if ded and ded[-1]["jp"] == ln["jp"]:
            ded[-1]["t"][1] = ln["t"][1]
            continue
        ded.append(ln)
    return ded


def _captions_from_dir(dest: Path, lang: str | None, any_lang: bool = False):
    """Find + parse the best MANUAL caption track matching the song's ORIGINAL
    language → (lines, lang) or None. A Japanese song's 'ja' track is its lyrics; a
    'zh-TW'/'en' track is a TRANSLATION we must never show as the lyrics.

    ``any_lang=True`` (SUBTITLES mode): a show's own captions are useful in
    whatever language they exist — accept whatever track this video has (any
    of the many languages we support) and REPORT its actual language so the
    annotate/translate pass reads the body correctly.
    """
    # Explicit language HINT beats the fallbacks for MUSIC mode; for
    # SUBTITLES mode we still prefer the requested lang if it exists, but
    # gracefully take whatever else is on offer.
    preferred = [lang] if lang in ("ja", "zh", "ko") else []
    # Ranked fallback: CJK first (music-mode: only CJK carries the song's own
    # lyrics), then broadly for subtitles mode.
    music_fallback = ["ja", "zh-Hans", "zh", "zh-Hant", "ko"]
    # v1.1.65 — every language annotate/_translate_lines can handle (plus a
    # few common variants like pt-BR / zh-TW). Order = "most common video
    # caption languages worldwide" so a Japanese + English track picks JA
    # first for a Japanese source, but a Spanish-only track is still taken
    # instead of triggering Whisper generation.
    subs_fallback = [
        "ja", "en", "es", "pt", "pt-BR", "fr", "de", "it", "ru",
        "ko", "zh-Hans", "zh", "zh-Hant", "zh-TW", "vi", "id", "th",
        "tr", "pl", "uk", "ar", "hi", "el", "nl", "sv", "no", "da",
        "fi", "cs", "hu", "ro", "he", "ms", "tl",
    ]
    order = preferred + (subs_fallback if any_lang else music_fallback)
    files = glob.glob(str(dest / "*.vtt"))
    if not files:
        return None

    def rank(f):
        n = Path(f).name.lower()
        for i, w in enumerate(order):
            if f".{w.lower()}." in n:
                return i, w
        return 99, None

    files.sort(key=lambda f: rank(f)[0])
    r, w = rank(files[0])
    if r == 99:
        if not any_lang:
            return None                  # only translation tracks → don't use
        w = None                         # subtitles: take whatever track exists
    try:
        lines = _parse_vtt(Path(files[0]))
    except Exception:
        return None
    # Report the file's ACTUAL language so annotate() / translate() route
    # the body correctly. Filename form is "c.LANG.vtt" (e.g. c.es.vtt,
    # c.pt-BR.vtt) — read it back if we don't already know from `w`.
    if w is None:
        stem = Path(files[0]).stem.lower()
        parts = stem.split(".")
        if len(parts) >= 2:
            w = parts[-1]
    # Fold regional variants back to the base tag the annotate pipeline uses.
    lw = (w or "").lower()
    if lw.startswith("zh"):
        clang = "zh"
    elif lw.startswith("pt"):
        clang = "pt"
    elif lw.startswith("en"):
        clang = "en"
    elif lw in ("ja", "ko", "es", "fr", "de", "it", "ru", "el", "vi", "id",
                "th", "tr", "pl", "uk", "ar", "hi", "nl", "sv", "no", "da",
                "fi", "cs", "hu", "ro", "he", "ms", "tl"):
        clang = lw
    elif w is None and any_lang:
        clang = "en"                     # unknown label, subtitles: last resort
    else:
        clang = "ja"                     # music-mode CJK fallthrough
    return (lines, clang) if len(lines) >= 6 else None


def _normalize_youtube_url(q):
    """TICKET-086: rewrite ``music.youtube.com`` → ``www.youtube.com`` so every
    yt-dlp / video-id entry point sees the canonical host. The same 11-char video
    id resolves either way, but our regex fast-paths (and any future host check)
    are tuned for ``www.youtube.com``; a browser pushing a YouTube Music tab URL
    used to slip past them. NO-OP on non-http strings (title / 11-char id paths
    still work) and a lossless urlsplit/urlunsplit roundtrip on a normal URL."""
    q = (q or "").strip()
    if not re.match(r"https?://", q, re.I):
        return q
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(q)
        if parts.netloc.lower() in ("music.youtube.com", "m.music.youtube.com"):
            parts = parts._replace(netloc="www.youtube.com")
            return urlunsplit(parts)
    except Exception:
        pass
    return q


def fetch_captions_only(query: str, lang: str | None = None,
                        max_dur: int | None = None, any_lang: bool = False):
    """FAST path: download ONLY the caption track (no audio, no Whisper) for the
    top YouTube hit for `query`, parse it to timed lines → (lines, lang) or None.

    `query` may be a TITLE (→ ytsearch1, the top hit) OR an exact YouTube URL /
    11-char video id. The exact-video form is strictly better — a title search
    can land on a DIFFERENT upload (lyric video vs official MV) whose intro
    length differs, so its caption timing wouldn't match the playing video.

    For a YouTube video the caption track is strictly better than a provider LRC:
    it's THIS video's own text AND timing, so there's no wrong-transcription and
    no cross-version drift (the "white balance" case where syncedlyrics returned
    different words than the video sings). Grabs manual subs first, then YouTube
    auto-captions (ASR) as a fallback. Seconds, not the minute deep_transcribe
    takes (which downloads the whole audio and runs Whisper)."""
    if not available():
        return None
    import tempfile
    import shutil as _sh
    import yt_dlp
    tmp = Path(tempfile.mkdtemp(prefix="dk_caps_"))
    try:
        # Request ONLY the song's language (plus a close fallback). Asking for
        # all CJK langs at once fired 5× the sub requests → YouTube 429s, and one
        # lang's error aborted the whole fetch. ignoreerrors keeps a single
        # rate-limited lang from killing the others.
        if any_lang:
            # SUBTITLES mode (v1.1.69). MANUAL subs first — a short hot-list of
            # what a video actually publishes as a manual caption track. The
            # previous 34-language list triggered YouTube 429s (auto-cap fetch
            # slots are aggressively rate-limited) which killed the whole
            # batch. Auto-captions are handled in a SEPARATE retry below,
            # requesting only "en" (YouTube's auto-caps for a non-English
            # source are literally Google-Translated from the primary, so
            # asking for many translations wastes rate-limit budget for no
            # quality gain).
            langs = ["en", "ja", "ko", "zh-Hans", "zh", "es", "pt", "fr", "de", "it", "ru"]
        elif lang in ("ja", "ko"):
            langs = [lang]
        elif lang == "zh":
            langs = ["zh-Hans", "zh-Hant", "zh"]
        else:
            langs = ["ja", "ko", "en"]
        opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "default_search": "ytsearch1", "skip_download": True,
            "ignoreerrors": True,
            # keep it LIGHT — this runs while music plays. 1 retry, short timeout,
            # and cap extractor work so a slow/blocked fetch can't burn CPU for
            # long (a pile-up of these stuttered the audio).
            "outtmpl": str(tmp / "c.%(ext)s"), "retries": 1, "socket_timeout": 15,
            # MANUAL subs first, ASR fallback in SUBTITLES mode (v1.1.64):
            # ASR is close-but-wrong for MUSIC (misheard lyrics, [Music] tags,
            # rolling cues that parse as dupes) — hence manual-only for the
            # song path. But SUBTITLES mode covers general video (game clips,
            # vlogs, fan compilations, tutorials) where 90%+ have ONLY ASR
            # captions. Manual-only would return nothing for those. Toggle
            # ASR on when the caller signals subtitles via `any_lang=True`;
            # the song path (any_lang=False) keeps the strict manual-only
            # behavior.
            # v1.1.69 — MANUAL subs only on this pass. Auto-caps get a
            # dedicated retry with a single language so YouTube's per-language
            # rate limiter (~2 langs before HTTP 429) doesn't nuke the batch.
            "writesubtitles": True,
            "writeautomaticsub": False,
            "subtitleslangs": langs,
            "subtitlesformat": "vtt",
        }
        # Duration gate: MUSIC mode only. There it protects a TITLE SEARCH from
        # landing on a 2-hour compilation instead of the song. SUBTITLES mode
        # (any_lang=True) targets the EXACT video the user is watching and a
        # caption fetch downloads NO audio — so no length is too long. v1.1.71
        # live-caught: the old unconditional filter silently rejected every
        # >60-min video (subs_deep_max_dur_s=3600) — a 139-min talk show got
        # "no caption track" while its 'en' auto-caps existed the whole time.
        if not any_lang:
            opts["match_filter"] = yt_dlp.utils.match_filter_func(
                f"duration < {int(max_dur or _MAX_DUR)}")
        if _sh.which("node"):
            opts["js_runtimes"] = {"node": {}}
        # Anti-bot variants (alternate player clients + best-effort browser cookies)
        # are applied per-attempt via _yt_variants — the web client now 403s without
        # a PO token, so the ios/tv/mweb clients are what actually fetch the captions.
        # Exact URL / 11-char video id → fetch THAT video; else search by title.
        # TICKET-086: normalize music.youtube.com → www.youtube.com first so the
        # canonical host flows through every code path below.
        q = _normalize_youtube_url(query)
        if re.match(r"https?://", q):
            target = q
        elif re.fullmatch(r"[\w-]{11}", q):
            target = f"https://www.youtube.com/watch?v={q}"
        else:
            target = f"ytsearch1:{q}"
        last_err = None
        for vopts in _yt_variants(opts):
            try:
                with yt_dlp.YoutubeDL(vopts) as y:
                    y.extract_info(target, download=True)
                # v1.1.72: pass any_lang so a non-CJK manual track (en/es/fr —
                # the common talk-show case) is RECOGNIZED as success here; the
                # old check ranked it 99/reject and burned every remaining
                # client variant on captions that were already on disk.
                if _captions_from_dir(tmp, lang, any_lang=any_lang):
                    break
            except Exception as e:
                last_err = e
                continue
        res = _captions_from_dir(tmp, lang, any_lang=any_lang)
        # v1.1.69 — SUBTITLES-MODE AUTO-CAPTIONS RETRY. Most YouTube shows /
        # podcasts / lectures publish NO manual subs — only Google's ASR-
        # generated auto-captions. The primary attempt above deliberately asks
        # for manual only; if that comes up empty and the caller signalled
        # subs mode via any_lang, spin a SECOND yt-dlp call for auto-caps in
        # ONLY the primary language (usually "en"). Anything else (ja/ko/es…)
        # would just be YouTube's on-the-fly Google-Translate of the same
        # source, wasting a 429-prone request slot. `_captions_from_dir`
        # accepts both because auto-caps VTT files land under the exact same
        # `c.<lang>.vtt` filename as manual would.
        if not res and any_lang:
            # Clear leftover VTTs from the manual pass (review finding): a
            # too-short manual track (e.g. a 4-line c.ja.vtt) would outrank the
            # fresh c.en.vtt in _captions_from_dir's ja-first ordering and fail
            # the ≥6-line floor — returning None despite good auto-caps on disk.
            for _f in tmp.glob("*.vtt"):
                try:
                    _f.unlink()
                except Exception:
                    pass
            asr_opts = dict(opts)
            asr_opts["writesubtitles"] = False
            asr_opts["writeautomaticsub"] = True
            # v1.1.70 — SUBTITLES mode wants ENGLISH out ("translate non-English
            # to English"). Request ONLY "en": YouTube offers an 'en' auto-caption
            # for essentially EVERY video — it's the RAW ASR for English speech
            # (and, unlike auto-TRANSLATED tracks, downloads without a JS runtime)
            # and a server-side auto-translation for a foreign source. So one
            # 'en' request yields English for every video in a SINGLE call — no
            # 429-prone multi-language batch and no wrong-guess miss. (The old
            # code inherited the app's VTuber-default lang='ja' and fetched a
            # Japanese track for an English talking-head video → nothing usable.
            # Requesting ['ja','en'] is WORSE: both land and _captions_from_dir's
            # subs_fallback leads with 'ja', so it would pick the JA machine-
            # translation and then double-translate it back to English.)
            asr_opts["subtitleslangs"] = ["en"]
            log.info("captions: no manual track — retrying with YouTube auto-captions %s",
                     asr_opts["subtitleslangs"])
            for vopts in _yt_variants(asr_opts):
                try:
                    with yt_dlp.YoutubeDL(vopts) as y:
                        y.extract_info(target, download=True)
                    if _captions_from_dir(tmp, "en", any_lang=any_lang):
                        break
                except Exception as e:
                    last_err = e
                    continue
            res = _captions_from_dir(tmp, "en", any_lang=any_lang)
            if res:
                log.info("captions: %d %s lines from YouTube auto-captions for %r",
                         len(res[0]), res[1], query)
                return res
        if not res and last_err is not None:
            log.info("captions: fetch failed for %r: %s", target, str(last_err)[:140])
        if res:
            log.info("captions: %d %s lines from YouTube caption track for %r",
                     len(res[0]), res[1], query)
        return res
    finally:
        _sh.rmtree(tmp, ignore_errors=True)


def deep_transcribe(title: str, artist: str = "", lang: str | None = None,
                    size: str = _DEEP_SIZE, url: str | None = None,
                    max_dur: int | None = None, romanize_rows: bool = True,
                    translate_rows: bool = True, prefer_transcript: bool = False):
    """Full pipeline: search + download the source audio, then FIRST try REAL
    provider lyrics via the video's canonical (yt-dlp) title, and only transcribe
    by ear if that fails. DELETE the audio. Returns ``(lines, lang, meta)`` where
    ``meta`` is a provider dict for REAL lyrics or ``None`` for a by-ear
    transcription; ``None`` on total failure. The audio is ALWAYS removed.

    SUBTITLES mode (v1.1.56): pass ``url`` for the EXACT playing video (a title
    search could land on a different upload), ``max_dur`` to allow full-length
    episodes past the song-sized cap, and romanize_rows/translate_rows=False to
    skip a hidden layer's annotation work."""
    if not available():
        return None
    query = url or f"{title} {artist}".strip()
    if not query:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="dk_deep_"))
    try:
        audio, canon = _download_audio(query, tmp, max_dur=max_dur)
        if not audio:
            return None
        # 1) The video's OWN manual caption track — exact official lyrics WITH the
        #    video's exact timing (perfect sync, no offset). Strictly best.
        caps = _captions_from_dir(tmp, lang, any_lang=prefer_transcript)
        if caps:
            clines, clang = caps
            try:
                import fetch_lyrics as F
                F.annotate(clines, clang, translate=translate_rows,
                           romanize_rows=romanize_rows)
            except Exception:
                pass
            log.info("deep: using OFFICIAL caption track — %d %s lines (exact video timing)",
                     len(clines), clang)
            return clines, clang, {"source": "youtube-captions"}
        # 2) REAL provider lyrics reachable via the canonical title. SKIPPED in
        #    subtitles mode (prefer_transcript): an EPISODE title fuzzy-matching
        #    any provider LRC would return a random SONG body as "real lyrics"
        #    instead of the episode transcript.
        if not prefer_transcript:
            real = _real_from_canonical(canon, artist)
            if real:
                return real
        log.info("deep: transcribing %s (%d KB) with %s",
                 audio.name, audio.stat().st_size // 1024, size)
        lines, detected = transcribe_file(audio, lang=lang, size=size)
        if len(lines) < 4:
            log.info("deep: only %d lines — keeping best-effort", len(lines))
            return None
        log.info("deep: %d lines transcribed (lang=%s)", len(lines), detected)
        return lines, detected, None
    except Exception as e:
        log.info("deep: error: %s", str(e)[:160])
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)   # ALWAYS delete the source audio
