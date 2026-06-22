# AGENTS.md — instructions for AI agents extending Desktop Karaoke

This file tells an automated agent how to add songs and keep the lyric
library correct. Humans can follow it too.

## What this project is

A transparent karaoke overlay. `main.py` reads the real playback position
from Windows media controls and renders synced lyrics (furigana/romaji/pinyin/
romaja + English). `fetch_lyrics.py` fetches and **verifies** lyrics. Cached
lyrics live in `lyrics/*.json` (git-ignored — never commit them).

## Add one song

```bash
python fetch_lyrics.py "Song Title" "Artist Name"
```

This fetches the best **verified** synced lyrics, annotates them by language,
and writes `lyrics/<slug>.json`. If it prints "No verified lyrics found", the
song genuinely isn't on any provider — do not fake it.

## Add many songs

Append `(title, artist, bake_english)` tuples to the `SONGS` list in
`preload.py`, then:

```bash
python preload.py            # skips songs already cached
```

## Sync a user's Spotify playlists

```bash
python sync_playlists.py --client-id <id>   # see sync_playlists.py header
```

## Upgrade existing Japanese furigana/romaji

Japanese readings use the fugashi + UniDic morphological analyzer (cutlet for
romaji), which segments correctly where the old pykakasi path failed (今生きて →
今(いま)生き "ima ikite", not 今生 "konjou"). After changing the romanizer, or to
fix files annotated by an older version, rewrite the cache in place:

```bash
python reannotate.py          # rebuild jp/rm for every lang=="ja" file
python reannotate.py --dry    # preview, write nothing
```

It only touches `jp`/`rm`; timestamps and `en` are preserved. Idempotent.

## The lyrics JSON schema

```json
{
  "meta": { "title": "...", "artist": "...", "lang": "ja|ko|zh|other",
            "duration": 214.0, "source": "lrclib/get", "fetched": null },
  "lines": [
    { "t": [start_sec, end_sec], "jp": "漢字(かんじ)…", "rm": "romaji",
      "en": "english" }
  ]
}
```

- `jp` holds the **main line** in any language. For Japanese, kanji are
  annotated as `kanji(かな)` and rendered as furigana. For Chinese/Korean the
  raw text stays in `jp` and the reading (pinyin/romaja) goes in `rm`.
- `t` is `[start, end]` seconds. `rm` = reading, `en` = English translation.
- Always include `meta.duration` and `meta.lang` so the runtime can verify the
  file belongs to the playing song.

## Correctness rules (important)

1. **Verify, don't guess.** Use `fetch_lyrics.fetch_lrc(title, artist, duration)`
   — it checks artist, song duration, and language before accepting a match.
   Common titles ("Lucky Star", "Paradise") match many wrong songs without
   these checks.
2. **Language must match.** A CJK-titled song must have CJK lyrics. Run
   `python validate.py --purge` after bulk edits to catch mismatches.
3. **Never hardcode user/account/machine data.** Only public song
   title/artist strings may be sent to providers. No telemetry.
4. **Don't commit `lyrics/`, `spotify_config.json`, or `.spotify_cache`** —
   they're git-ignored (copyrighted content / local tokens).

## Adding a new language

Extend `detect_lang()` and `romanize()` in `fetch_lyrics.py` with the script
range and a romanizer, then `annotate()` will pick it up.

## Verify your changes

```bash
python -c "import ast;[ast.parse(open(f,encoding='utf-8').read()) for f in ('main.py','fetch_lyrics.py')]"
python validate.py
```
