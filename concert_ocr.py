# -*- coding: utf-8 -*-
"""Read the on-screen SONG-TITLE banner that idol / VTuber concert videos display.

hololive / ReGLOSS-style 3D lives put the CURRENT song's name in a top corner
("SUPER DUPER", "泡沫メイビー", "LAKI MODE", …). In a long concert video the media
title never changes and Shazam struggles with the live arrangement, so that banner
is the single most reliable hint for *which song is playing right now*. We read it
and feed it as a HIGH-confidence signal into the song-match confidence score
(see overlay._song_confidence / docs/CONCERT_DETECTION.md).

PIPELINE  (no heavy dependency — uses the OCR engine built into Windows):
  grab the screen → crop the top strip → save a PNG → OCR it with
  ``Windows.Media.Ocr`` (via winsdk, already a dependency) → fuzzy-match the text
  against the known song library → return (title, score).

LANGUAGES: Windows ships **en-US** OCR by default (reads English banners like
SUPER DUPER / LAKI MODE / BANG). For Japanese banners (泡沫メイビー / サクラミラージュ)
install the pack ONCE (admin), after which the engine auto-uses it:
    Add-WindowsCapability -Online -Name "Language.OCR~~~ja-JP~0.0.1.0"
Everything degrades gracefully: no OCR engine, no capture, or no confident match
→ returns None and the existing sound/title detection stands.
"""
from __future__ import annotations

import asyncio
import difflib
import os
import re
import tempfile

_TOP_FRAC = 0.26          # OCR the top 26% of the screen — where the banner sits
_MIN_LEN = 2


def available() -> bool:
    """True if the Windows OCR engine and a screen-grab backend are usable."""
    try:
        from PIL import ImageGrab  # noqa: F401
        from winsdk.windows.media.ocr import OcrEngine
        return OcrEngine.available_recognizer_languages.size > 0
    except Exception:
        return False


def _engines():
    """One OcrEngine per installed recognizer language (en-US always; ja-JP if the
    pack is added). Cached on the function."""
    if getattr(_engines, "_cache", None) is not None:
        return _engines._cache
    out = []
    try:
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.globalization import Language
        for lang in OcrEngine.available_recognizer_languages:
            eng = OcrEngine.try_create_from_language(Language(lang.language_tag))
            if eng:
                out.append(eng)
    except Exception:
        pass
    _engines._cache = out
    return out


def ocr_langs() -> list:
    """The recognizer language tags currently usable (for logging / diagnostics)."""
    return [e.recognizer_language.language_tag for e in _engines()]


async def _ocr_file(path, engine):
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage import StorageFile, FileAccessMode
    f = await StorageFile.get_file_from_path_async(os.path.abspath(path))
    stream = await f.open_async(FileAccessMode.READ)
    decoder = await BitmapDecoder.create_async(stream)
    bmp = await decoder.get_software_bitmap_async()
    res = await engine.recognize_async(bmp)
    return [ln.text.strip() for ln in res.lines if ln.text.strip()]


def read_banner_lines() -> list:
    """Grab the screen, OCR the top strip with every installed language, and return
    the recognised text lines (de-duplicated). [] on any failure."""
    engs = _engines()
    if not engs:
        return []
    try:
        from PIL import ImageGrab
        im = ImageGrab.grab()                       # COM grab OUTSIDE the asyncio loop
        strip = im.crop((0, 0, im.width, int(im.height * _TOP_FRAC)))
        fd, path = tempfile.mkstemp(prefix="dk_ocr_", suffix=".png")
        os.close(fd)
        strip.save(path)
    except Exception:
        return []
    lines, seen = [], set()
    try:
        for eng in engs:
            try:
                for t in asyncio.run(_ocr_file(path, eng)):
                    k = t.lower()
                    if len(t) >= _MIN_LEN and k not in seen:
                        seen.add(k)
                        lines.append(t)
            except Exception:
                continue
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    return lines


_NORM = re.compile(r"[^0-9a-z぀-ヿ一-鿿]")


def _norm(s: str) -> str:
    return _NORM.sub("", (s or "").lower())


def match_song(ocr_lines, candidates) -> tuple | None:
    """Best fuzzy match of any OCR line to any candidate song TITLE.

    ``candidates`` is an iterable of title strings (the local lyric cache + the
    library DB). Returns ``(title, score 0..1)`` for the best match, or None.
    A banner line that IS a song title scores high; the concert hashtag / chat /
    decorative text won't match any real title and is ignored by the threshold."""
    best_title, best = None, 0.0
    norm_cands = [(c, _norm(c)) for c in candidates if c and len(_norm(c)) >= 2]
    for raw in ocr_lines:
        nl = _norm(raw)
        if len(nl) < 2:
            continue
        for title, nc in norm_cands:
            if not nc:
                continue
            if nl == nc:
                score = 1.0
            elif nl in nc or nc in nl:                # banner often == the title exactly
                short, lng = sorted((nl, nc), key=len)
                score = 0.7 + 0.25 * (len(short) / max(1, len(lng)))
            else:
                score = difflib.SequenceMatcher(None, nl, nc).ratio()
            if score > best:
                best, best_title = score, title
    return (best_title, round(best, 3)) if best_title else None
