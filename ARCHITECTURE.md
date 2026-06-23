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
- **`LyricsIndex`** — in-memory index of `lyrics/*.json`. `.match(artist, title,
  duration)` is **title-driven and paranoid**: a candidate is accepted only on an
  exact or ≥60%-overlap title match (never a loose substring), so a different
  song by the same artist is never grabbed; returns None when unsure → the caller
  identifies by **sound**. `.refresh()`, `.add()`.
- **Logging** — `log` (rotating `karaoke.log`) records every track change, the
  title-vs-sound match, corrections, and sync adjustments. Readable via the API's
  `/logs` so a human or agent can see *why* a song/lyric was chosen.
- **`load_lyrics` / `split_furigana` / `draw_text` / `measure_text`** — IO &
  rendering helpers. `draw_text` honours the perf mode's outline weight.
- **Fonts (`_PIL_FONTS`, `_TK_MAIN_FONT`, `_script_of`)** — per-script so text
  never renders as boxes (□): Yu Gothic has no Hangul, so Korean uses **Malgun
  Gothic** and Chinese uses **Microsoft YaHei**; bare kanji follows the song's
  language. The main row's font is chosen per line by its script.
- **`_work_area`** — desktop work area (screen minus taskbar). The overlay is a
  **fixed, full-work-area, click-through** window (added `WS_EX_TRANSPARENT`):
  it never moves or resizes, which is the **root fix for lyrics drifting down**
  (it used to resize per song and re-anchor to the bottom; backfill adding rows
  mid-song made it jump). Content is positioned *inside* via `_lane_y0`.
- **`Character`** (`character.py`) — optional tray-toggled dancing companion
  themed to the detected artist (see that file's header).
- **`api.py`** — optional localhost HTTP API (`127.0.0.1:8765`) for agents:
  `GET /health` · `/status` · `/logs` · `/lyrics`, `POST /identify` · `/wrong` ·
  `/nudge` · `/reset` · `/reindex`. Hardened for unattended driving — every
  handler is wrapped (a bad request returns clean JSON, never crashes the app),
  responses share an `{"ok": …}` shape, bodies are size-capped, mutations are
  marshalled onto the Tk thread, it binds 127.0.0.1 only, and an optional
  `KARAOKE_API_TOKEN` gates access.
- All `subprocess` calls (git, PowerShell, pip) run with `CREATE_NO_WINDOW` so no
  console window flashes.
- **`Overlay`** — the window. Notable methods:
  - lifecycle: `__init__`, `run`, `quit`, `_tick` (the ~60fps loop)
  - matching/fetch: `_on_track_change`, `_start_fetch`, `_consume_async`,
    `_file_valid`, `_maybe_translate`, `load`
  - **audio**: `_start_identify(seconds, attempts)` (short captures re-sync
    fast, long ones detect reliably), `_recalibrate_loop` + `_arm_recal`
    (adaptive cadence — a 3-shot fast burst ~8s apart right after a song starts
    so the offset locks in ~25s, then relaxes; once a song is **confirmed and
    the boundary detector is on**, the blind poll relaxes further to a slow
    safety heartbeat so a long compilation isn't Shazam-polled every few seconds),
    `_health_check`, `_suspect`. Correction snaps to a clearly-real offset (>2s,
    e.g. an MV intro) and otherwise eases 0.8× toward it, smoothing Shazam's
    sub-second noise.
  - **song-change detector**: `_start_boundary` spins up `songchange.py`'s
    `SongChangeDetector`; `_on_boundary` (marshalled to the Tk thread via
    `_fire_boundary`) fires when a track flip is heard inside one long video and
    pulls an immediate re-identify in — the seamless switcher for compilations.
    Throttled (ignores a boundary if one fired <4s ago or an identify is in
    flight); `set_boundary` toggles it.
  - rendering: `_render`, `_karaoke`, `_render_block`/`_ticker_update`
    (scroll-through ticker), `_animate_in`/`_anim_step`, `_hint`
  - **scroll layout**: `_relayout_song` sizes blocks + lane count to the rows
    the current song uses (a 1-row Latin song → short blocks → up to 4 lanes;
    a furigana+romaji+English song → tall blocks → fewer). `_compute_scroll_floor`
    picks a per-song minimum scroll speed so dense/fast songs don't overlap
    (same-lane lines sit `speed × Δtime` apart) while slow songs keep the
    user's comfortable pace.
  - **fixed window + placement**: `_relayout_song` no longer resizes the window
    — it computes `_lane_y0` (where the lane stack sits inside the fixed window:
    top-anchored, or bottom-anchored growing upward). `_viewport_watchdog` (~3s)
    just re-asserts the fixed geometry/topmost and, as a backstop, trims a lane
    if content ever overflows — **without moving the window**.
  - **responsive sizing**: `_auto_scale` (from the work-area height, ≈1.0 on
    1080p) multiplies the user's font %, so a 1440p/4K display or big TV gets
    proportionally larger lyrics automatically.
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

## songchange.py — detect a track flip inside one long video

- **`SongChangeDetector(on_change, …)`** — a daemon thread with a cheap RMS
  loudness meter on the WASAPI loopback (short, low-rate blocks → a few wake-ups
  a second, negligible CPU). It fires `on_change()` on the tell-tale shape of a
  track boundary: a stretch of music → a brief near-silent **gap** → music
  returning. Conservative by design (silence judged against both an absolute
  floor and a fraction of the recent loud level; the gap must persist `min_gap`
  and be preceded by real music; `debounce` after each fire) so a quiet musical
  passage doesn't false-trigger. `set_enabled(on)` / `stop()`. Only loudness is
  analysed — no audio is stored, fingerprinted, or sent anywhere. A *crossfaded*
  compilation (no gap) won't trip it; the overlay's slow Shazam heartbeat is the
  backstop for that.

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
