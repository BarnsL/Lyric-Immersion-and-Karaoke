"""
Add lyrics for ANY song from a local .lrc file — for the niche/indie/VTuber
tracks that no online provider has (e.g. a B-side that isn't on LRCLIB,
Musixmatch, or NetEase).

Find or transcribe a timed .lrc anywhere (a fan wiki, the video description, or
make one with a tool like QuickLRC), then run this. It parses the timing,
detects the language, adds furigana + romaji + English the same way fetched
songs get them, and saves it to the library so the overlay uses it like any
other song.

    # one song
    python add_lrc.py "TIME TO LUV.lrc" --title "TIME TO LUV" --artist "ピーナッツくん"

    # or drop files named "Artist - Title.lrc" into a folder and import them all
    python add_lrc.py --folder manual

The .lrc must have [mm:ss.xx] timestamps (standard synced-lyrics format). Use
--no-translate to skip the English pass (faster / offline).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fetch_lyrics import (LYRICS_DIR, parse_lrc_text, annotate, _song_lang,
                          slugify)


def add_one(lrc_path: Path, title: str, artist: str, translate: bool) -> Path | None:
    """Annotate a single .lrc and save it to the library. Returns the path."""
    text = lrc_path.read_text("utf-8", errors="replace")
    lines = parse_lrc_text(text)
    if len(lines) < 2:
        print(f"  ✗ {lrc_path.name}: no timed [mm:ss] lines found")
        return None
    lang = _song_lang(lines)
    lines = annotate(lines, lang, translate=translate)
    LYRICS_DIR.mkdir(exist_ok=True)
    out = LYRICS_DIR / f"{slugify(title)}.json"
    out.write_text(json.dumps({
        "meta": {"title": title, "artist": artist, "lang": lang,
                 "duration": None, "source": "manual"},
        "lines": lines,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ {title} — {artist}  ({lang}, {len(lines)} lines) → {out.name}")
    return out


def main():
    ap = argparse.ArgumentParser(description="Add a song's lyrics from a .lrc file.")
    ap.add_argument("lrc", nargs="?", help="path to a .lrc file")
    ap.add_argument("--title", help="song title (required with a single file)")
    ap.add_argument("--artist", default="", help="artist (optional)")
    ap.add_argument("--folder", help="import every .lrc in this folder "
                                     "(filenames like 'Artist - Title.lrc')")
    ap.add_argument("--no-translate", action="store_true",
                    help="skip the English translation pass")
    args = ap.parse_args()
    translate = not args.no_translate

    if args.folder:
        n = 0
        for p in sorted(Path(args.folder).glob("*.lrc")):
            stem = p.stem
            if " - " in stem:
                artist, title = (s.strip() for s in stem.split(" - ", 1))
            else:
                artist, title = "", stem.strip()
            if add_one(p, title, artist, translate):
                n += 1
        print(f"\nImported {n} file(s) from {args.folder}.")
        return

    if not args.lrc or not args.title:
        ap.error("give a .lrc file and --title (or use --folder)")
    p = Path(args.lrc)
    if not p.exists():
        sys.exit(f"file not found: {p}")
    add_one(p, args.title, args.artist, translate)


if __name__ == "__main__":
    main()
