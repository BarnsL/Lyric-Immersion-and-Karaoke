# Architecture

How Desktop Karaoke is put together, module by module. Every public function is
listed so you can find your way around. Source files also carry docstrings and
inline notes (especially `fetch_lyrics.py`'s header: sources + problems solved).

```
play audio ──▶ MediaWatcher (winsdk)        ─┐
                 position / title / status    │
YouTube/Spotify                               ├─▶ Overlay (tkinter, transparent)
                 recognize.py (Shazam) ───────┘     renders synced lyrics
                 fetch_lyrics.py (providers) ──▶ lyrics/*.json  (cache)
```

## main.py — the overlay app

The whole UI/runtime. A transparent, click-through, always-on-top Tk window
plus a `pystray` tray menu.

- **`MediaWatcher`** — background thread polling Windows
  `GlobalSystemMediaTransportControls` for `{title, artist, status, position,
  duration, source}`; extrapolates position between polls. `.get()` / `.stop()`.
- **`clean_title(title, source)`** — strip "- YouTube", brackets, "Official MV"…
- **`LyricsIndex`** — in-memory index of `lyrics/*.json` for instant matching.
  `.match(artist, title, duration)` (duration-guarded), `.refresh()`, `.add()`.
- **`load_lyrics` / `split_furigana` / `draw_text` / `measure_text`** — IO &
  rendering helpers. `draw_text` honours the perf mode's outline weight.
- **`Overlay`** — the window. Notable methods:
  - lifecycle: `__init__`, `run`, `quit`, `_tick` (the ~60fps loop)
  - matching/fetch: `_on_track_change`, `_start_fetch`, `_consume_async`,
    `_file_valid`, `_maybe_translate`, `load`
  - **audio**: `_start_identify`, `_recalibrate_loop` (periodic listen → timing
    re-lock **and** concert song-change), `_health_check`, `_suspect`
  - rendering: `_render`, `_karaoke`, `_render_block`/`_ticker_update`
    (scroll-through ticker), `_animate_in`/`_anim_step`, `_hint`
  - settings (all persisted via `_persist`): `set_opacity`, `set_position`,
    `set_scroll`, `set_scroll_speed`, `set_font_scale`, `set_quality`,
    `set_recal`, `apply_preset`, `set_git_sync`, `git_backup`, `set_startup`
  - layout: `_apply_scale` (fonts + **font-aware lane count**), `_apply_perf`
- **module helpers**: `_load_settings`/`_save_settings`, `startup_enabled`/
  `set_startup`, `make_icon`, `main`.

Data is **portable**: `_DATA` = the folder next to the .exe (or the source
dir), holding `lyrics/` and `settings.json`.

## fetch_lyrics.py — get & annotate lyrics (see its header for sources)

- **`detect_lang(text)`** → `ja|ko|zh|es|other` (script + Spanish markers).
- **`fetch_lrc(title, artist, duration)`** → verified timed LRC. LRCLIB
  duration-exact first, then scored search, then `syncedlyrics` (Musixmatch/
  NetEase/…) with a guarded title-only last resort. `verify_lrc` rejects
  wrong-language / wrong-duration matches.
- **`split_artists`**, `parse_lrc_text` (strips stacked `[mm:ss]`/`<..>` tags &
  credit lines), `to_furigana`, `romanize(text, lang)`, `annotate`,
  `_translate_lines`/`translate_file`, **`fetch_and_save(...)`** (writes JSON
  with provenance: `lang/duration/source`), **`validate_file`**.

## recognize.py — identify by sound

- **`recognize_playing(seconds, attempts)`** → `(title, artist, offset, t_cap)`.
  Captures system audio (`soundcard` WASAPI loopback) and asks Shazam
  (`shazamio`). `offset` = seconds into the song; `t_cap` = capture timestamp,
  so the overlay can align its clock to the true position.

## Tools (run from a terminal)

- **`preload.py`** — bulk-fetch a curated `SONGS` list into the library.
- **`sync_playlists.py`** — Spotify OAuth-PKCE → all playlists → fetch.
- **`youtube_music.py`** — YouTube Music playlists via yt-dlp cookies → CSV + fetch.
- **`validate.py`** — scan the cache for bad/mismatched files (`--purge`).

## Lyrics JSON schema

```json
{ "meta": {"title","artist","lang","duration","source"},
  "lines": [ {"t":[start,end], "jp":"漢字(かな)…", "rm":"reading", "en":"english"} ] }
```

See **[AGENTS.md](AGENTS.md)** to add songs/sources and **[USAGE.md](USAGE.md)**
for the tray/settings reference.
