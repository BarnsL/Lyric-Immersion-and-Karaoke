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
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger("karaoke")

_DEEP_SIZE = "medium"     # offline quality/speed balance: ~as accurate as large-v3
#                           on clear vocals, but small enough to fit GPU alongside
#                           the running app (large-v3 spilled to CPU → ~4min). Both
#                           are pre-cached; bump to "large-v3" for max accuracy.
_MAX_DUR = 900            # >15 min ⇒ almost certainly a concert/loop, not the song


def available() -> bool:
    """True if yt-dlp is importable — the only extra dependency beyond the
    faster-whisper stack the realtime generator already needs."""
    try:
        import yt_dlp  # noqa: F401
        return True
    except Exception:
        return False


def _download_audio(query: str, dest: Path) -> Path | None:
    """ytsearch the query and download the top hit's AUDIO-ONLY stream into
    ``dest``. Returns the downloaded file path, or None on any failure / an
    over-long match (a concert or "1 hour loop" is not the song we want)."""
    import shutil as _sh
    import yt_dlp
    out = str(dest / "src.%(ext)s")
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "default_search": "ytsearch1",
        "format": "bestaudio/best",          # audio-only ⇒ no ffmpeg merge needed
        "outtmpl": out, "retries": 3, "socket_timeout": 30,
        # reject an over-long top hit (concert / compilation / hour-long loop)
        "match_filter": yt_dlp.utils.match_filter_func(f"duration < {_MAX_DUR}"),
    }
    # YouTube now needs a JS runtime to mint un-throttled format URLs, else the
    # audio download 403s. yt-dlp only enables `deno` by default; opt in to
    # node when it's on PATH (it usually is — ships with VS Code/Electron). On a
    # machine with neither, the download may 403 and we degrade gracefully.
    if _sh.which("node"):
        opts["js_runtimes"] = {"node": {}}
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([f"ytsearch1:{query}"])
    except Exception as e:
        log.info("deep: download failed for %r: %s", query, str(e)[:140])
        return None
    files = glob.glob(str(dest / "src.*"))
    return Path(files[0]) if files else None


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


def deep_transcribe(title: str, artist: str = "", lang: str | None = None,
                    size: str = _DEEP_SIZE) -> tuple[list[dict], str | None] | None:
    """Full pipeline: search + download the source audio, transcribe the whole
    file with the large model, DELETE the audio, return ``(lines, lang)``.
    Returns None on any failure. The downloaded audio is ALWAYS removed."""
    if not available():
        return None
    query = f"{title} {artist}".strip()
    if not query:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="dk_deep_"))
    try:
        audio = _download_audio(query, tmp)
        if not audio:
            return None
        log.info("deep: transcribing %s (%d KB) with %s",
                 audio.name, audio.stat().st_size // 1024, size)
        lines, detected = transcribe_file(audio, lang=lang, size=size)
        if len(lines) < 4:
            log.info("deep: only %d lines — keeping best-effort", len(lines))
            return None
        log.info("deep: %d lines transcribed (lang=%s)", len(lines), detected)
        return lines, detected
    except Exception as e:
        log.info("deep: error: %s", str(e)[:160])
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)   # ALWAYS delete the source audio
