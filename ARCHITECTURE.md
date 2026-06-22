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
  - **audio**: `_start_identify(seconds, attempts)` (short captures re-sync
    fast, long ones detect reliably), `_recalibrate_loop` + `_arm_recal`
    (adaptive cadence — a 3-shot fast burst ~8s apart right after a song starts
    so the offset locks in ~25s, then relaxes to `recal_secs`; also catches
    concert song-changes), `_health_check`, `_suspect`. Correction snaps to a
    clearly-real offset (>2s, e.g. an MV intro) and otherwise eases 0.8× toward
    it, smoothing Shazam's sub-second noise.
  - rendering: `_render`, `_karaoke`, `_render_block`/`_ticker_update`
    (scroll-through ticker), `_animate_in`/`_anim_step`, `_hint`
  - **scroll layout**: `_relayout_song` sizes blocks + lane count to the rows
    the current song uses (a 1-row Latin song → short blocks → up to 4 lanes;
    a furigana+romaji+English song → tall blocks → fewer). `_compute_scroll_floor`
    picks a per-song minimum scroll speed so dense/fast songs don't overlap
    (same-lane lines sit `speed × Δtime` apart) while slow songs keep the
    user's comfortable pace.
  - **viewport safety**: layout sizes to the desktop **work area** (`_work_area`,
    screen minus taskbar) with a margin, and `_geom_y` anchors the window inside
    it, so the bottom lane can't slide under the taskbar. `_viewport_watchdog`
    (~2.5s) is a backstop: if anything renders past the window edge it trims a
    lane and re-asserts the window's place in the work area.
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
- **Romanization**: `to_furigana` + `romanize(text, lang)` use **fugashi +
  UniDic** (via **cutlet** for romaji) for Japanese — a real morphological
  analyzer that segments correctly (今生きて → 今(いま)生き), with **pykakasi** as
  an automatic fallback. Chinese uses `pypinyin`, Korean `hangul-romanize`.
  **Katakana English** is recovered as English: `_segment_katakana` splits
  run-together loanwords using **`gairaigo.py`** (an extensible katakana→English
  table) so ベイビーアイラブユー → "baby I love you", not "beibiiairabuyuu".
- **Translation**: `_translate_lines` translates in **context windows** — each
  block of lines is sent with a couple of neighbouring lines before/after, so a
  line is translated in the flow of the song. DeepL when `DEEPL_API_KEY` is set,
  else Google. `backfill_file` self-heals a cached song (romaji + translation)
  the first time it plays.
- **`split_artists`**, `parse_lrc_text` (strips stacked `[mm:ss]`/`<..>` tags &
  credit lines), `annotate`,
  `_translate_lines`/`translate_file`, **`fetch_and_save(...)`** (writes JSON
  with provenance: `lang/duration/source`), **`validate_file`**.

## recognize.py — identify by sound

- **`recognize_playing(seconds, attempts)`** → `(title, artist, offset, t_cap)`.
  Captures system audio (`soundcard` WASAPI loopback) and asks Shazam
  (`shazamio`). `offset` = seconds into the song; `t_cap` = capture timestamp,
  so the overlay can align its clock to the true position.

## Tools (run from a terminal)

- **`preload.py`** — bulk-fetch a curated `SONGS` list into the library.
- **`reannotate.py`** — rebuild furigana/romaji for cached Japanese files with
  the current analyzer (use after a romanizer change; `--dry` to preview).
- **`sync_playlists.py`** — Spotify OAuth-PKCE → all playlists → fetch.
- **`youtube_music.py`** — YouTube Music playlists via yt-dlp cookies → CSV + fetch.
- **`validate.py`** — scan the cache for bad/mismatched files (`--purge`).

## Lyrics JSON schema

```json
{ "meta": {"title","artist","lang","duration","source"},
  "lines": [ {"t":[start,end], "jp":"漢字(かな)…", "rm":"reading", "en":"english"} ] }
```

See **[AGENTS.md](AGENTS.md)** to add songs/sources, **[USAGE.md](USAGE.md)** for
the tray/settings reference, and **[RESEARCH.md](RESEARCH.md)** for the
subsystem-by-subsystem investigation behind recent changes (lyric sources,
word-level timing, sync, performance, translation).
