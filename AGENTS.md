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

It only touches `jp`/`rm`; timestamps and `en` are preserved. Idempotent. It
processes **any** file containing CJK — including songs whose overall language
was detected as English/other — so Japanese lines inside a mixed song still get
furigana + romaji (`annotate()` romanizes **per line** by each line's own
script, not the song's overall language).

## Why lyrics never come out as "bare Japanese"

Three layers make sure a Japanese line always gets furigana/romaji (and a
translation):

1. **`annotate()`** romanizes each line by its own script at fetch time.
2. **`reannotate.py`** fixes the existing cache.
3. **`backfill_file()`** + the overlay's `_maybe_translate` self-heal a song at
   runtime: the first time it plays, any CJK line missing romaji gets it, and
   any untranslated line gets English — then the file is re-saved.

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

## Improving katakana-English readings ("more data")

When a katakana English phrase romanizes phonetically instead of as English
(e.g. a new slang loanword), add the pair to **`gairaigo.py`**:

```python
"カタカナ": "english",   # e.g. "サンキュー": "thank you"
```

`_segment_katakana()` uses the keys to split run-together katakana and cutlet
exceptions render the English. After adding entries, run `python reannotate.py`
to refresh the cached romaji. No code changes needed — it's pure data.

## Verify your changes

```bash
python -c "import ast;[ast.parse(open(f,encoding='utf-8').read()) for f in ('main.py','fetch_lyrics.py')]"
python validate.py
```
