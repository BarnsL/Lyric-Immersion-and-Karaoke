# Agent Handoff — Lyric Immersion and Karaoke

A live, click-through desktop overlay (Python/Tkinter) that shows synced lyrics with
furigana / romaji / pinyin / romaja / translation over whatever music is playing
(YouTube / Spotify / Niconico in a browser, or Spotify app). A language-learning +
karaoke tool. Read this first, then `ARCHITECTURE.md`.

## Where things live
- **Source repo:** `D:\Desktop-Karaoke` (git). Remote: **`BarnsL/Lyric-Immersion-and-Karaoke`**
  (renamed from Desktop-Karaoke). Work branch: **`claude/caption-sync-perf-fixes`**.
  `origin/master` is a *separate* fresh v1.1.1 history — NEVER push master (unrelated
  root, would need a force). Push to the branch only; it fast-forwards cleanly.
- **Deployed app:** `D:\DesktopKaraoke\` — `DesktopKaraoke.exe` + `_internal\` + runtime
  dirs `lyrics\` (LRC cache), `deps\`, `models\` (whisper), `settings.json`.
- **Local API:** `http://127.0.0.1:8765` (api.py) — agent control + diagnostics.

## Build + deploy (learned the hard way)
- Build: `cd D:\Desktop-Karaoke; $env:PYTHONPATH=".deps"; python -m PyInstaller --noconfirm DesktopKaraoke.spec`
  (faster-whisper bundled because `.deps\` exists → ~744 MB `_internal`). `py -m py_compile`
  first; a successful build + launch + `/health` verifies all imports resolve.
- **The app pins ITSELF to cores 8-15 (0xFF00) at BelowNormal** (audio-stutter fix). So a
  build must NOT compete: run it at **Idle**, or on **isolated cores 3-7 (affinity 0xF8) at
  Normal** (audio is on 0-2). A build at BelowNormal on overlapping cores starved the app to
  <1 fps — don't.
- Deploy (stop the app to unlock `_internal`): `Stop-Process DesktopKaraoke` →
  `robocopy "$src\_internal" "$dst\_internal" /MIR` → copy the `.exe` → relaunch. robocopy
  exit 1 = success. PRESERVE `deps/ models/ lyrics/ settings.json`.
- **Path guard:** deleting under `D:\DesktopKaraoke` can trip a protection error mid-script
  (aborts before relaunch — then the overlay is down, relaunch it). Prefer `/purgecache`.

## ⭐ NEXT WORK — the three requests from the last message (NOT yet implemented)
1. **Concert sync must transcribe in the SONG'S language.** `align.capture_and_align(lang=…)`
   gets `self.meta.get("lang","ja")`. For live/concert cuts ensure the lang matches the lyric
   track (JA/EN/ZH/KO) so the Whisper transcript actually matches the displayed lines; thread
   it through the applause resync (`_check_applause_gap` → `align_by_listening` →
   `capture_and_align`). The Grimes "Shinigami Eyes" (English) and V.W.P live cuts are the cases.
2. **Smooth sync correction — finish the current line, fade the next in corrected.** Today a
   found offset eases frame-by-frame (`_eased_offset`). The user wants: do NOT disturb the line
   reading as "current"; HOLD the correction, let that line finish, then at the NEXT line
   boundary jump to the corrected position and **fade in** the freshly-spawned correct line,
   continuing from there. Add `self._pending_offset` applied when `idx` advances + a fade-in
   alpha ramp on the new block. Touch: `_consume_async` (stash pending vs set `self.offset`),
   `_tick_body`/`_ticker_update` (apply at line change), `_render_img_block`/`_advance_fill` (fade).
3. **YouTube AUTO captions = sync HINT only, never displayed.** Display already uses MANUAL
   captions only (TICKET-059, `writeautomaticsub=False`). New ask: ALSO pull the AUTO/ASR
   captions into a SEPARATE buffer and use their word/line TIMING as a sync-position reference —
   fuzzy-match the ASR text to the displayed LRC/manual lines to compute an offset (a cheap,
   network-only alternative to Whisper), TPVR-gated. ASR text is NEVER shown. New path in
   `deep_transcribe.py` (fetch auto subs to a hint buffer) + a matcher feeding `_consume_async`.

## What this session shipped (all deployed + pushed)
- **Adaptive sync-verification tier** (TICKET-065) — `_periodic_auto_align` verifies
  ~3×/min while syncing, relaxes to 1×/min once confirmed, snaps back on any miss
  (`_note_sync_verdict` / `_sync_tier_interval`). Energy correlation gives the verdict;
  when it's blind on a song it escalates to a short two-point-verified Whisper listen
  (`_tier_listen_now`). Whisper CPU capped (`align.py cpu_threads=4`). `/diag.sync.tier_*`.
- **Cover detection fix** (TICKET-064) — `_COVER_RE` now catches `【Cover MV】` /
  `（Cover MV）` lenticular tags; covers with no parseable original artist DROP the cover
  channel and search title-first (the MAFIA / マフィア — Ouro Kronii case).
- **Language-confidence + known-acts** (TICKET-062) — `confidence.language_confidence`
  + `_KNOWN_JA`; GHOST/星街すいせい → JA, feelingradation/ReGLOSS → JA.
- **Long-concert** (TICKET-063) — applause gap = song boundary → re-identify; 2-6 min
  duration heuristic. (Open: transcription-based song-ID against the library.)
- **Glyph atlas** render — each (glyph,font,colour,stroke) rasterised once + pasted;
  pixel-identical, **8× faster**, 9-13 fps → **57 fps**. (Background prewarm REVERTED: Pillow
  text holds the GIL, a render thread stalls the Tk scroll.) See LYRIC_PERFORMANCE.md.
- **Sliver karaoke fill** + per-line **block cache** (LRU).
- **Eased display offset** (`_eased_offset`) — fill glides into a correction. (Request #2
  supersedes this with a per-line fade.)
- **Manual-captions-only** — auto/ASR was wrong/`[音楽]`-tagged/rolling-duplicated; reverted to
  v1.0.25 + strip `[..]/【..】/♪`. (TICKET-059)
- **Wrong-song / wrong-language guards:** provenance guard (Ludacris "The Potion" → Michiru
  Shisui, TICKET-055), `[ar:]` artist cross-check, kana→reject zh/ko, hangul→reject zh/ja,
  **Han(kanji)→reject ko** (花譜 邂逅 → Korean "Chance meeting", TICKET-060, self-heals on load).
- **MV/cinematic intro** vocal-poll release + "Cinematic intro" card (TICKET-057).
- **Concert applause-pause two-point resync** (TICKET-061) — detect loud-non-vocal gap, Whisper
  resync on vocal return, 2 agreeing reads. `/tune applause_min_s`.
- **X/Y position** — independent `pos_x` + `pos_y`, tray Vertical/Horizontal submenus, slid-in
  lines pin to the correct side.
- **Vertical scroll** (`tb`/`bt`) + 3-lane cap.
- **New API:** `POST /font?scale=` · `/scroll?dir=` · `/position?y=&x=` ·
  `/purgecache?current=1|lang=ko|source=…`.
- **Docs → `docs/`**; repo renamed.
- **Originals batch-fetch** (`_batch_fetch_originals.py`, gitignored) — V.W.P + ReGLOSS + Reol
  originals into `lyrics\`; **running in background at handoff** (`_batch_fetch.log` SUMMARY).
  hololive-wide out of scope (point at a playlist to add).

## Key references
- `ARCHITECTURE.md` (subsystems + confidence), `ISSUES.md` (TICKET-001..061),
  `PERFORMANCE.md` / `LYRIC_PERFORMANCE.md` (PERF/LP; LP-100/101 = PyGame-SDL2 / single-strip
  GPU paths if 60 fps isn't enough).
- Sync lives in `main.py`: `_consume_async`, `_schedule_sync_confirm`, `_eased_offset`,
  `_check_applause_gap`, `_apply_align`, `_auto_align_by_energy`, `align.py`, `songchange.py`.
- `/diag` is the eyes (fps + full sync state machine); `/tune` changes sync/render knobs live.
- User prefs (memory): keep work on D:\ ; always merge clean / never force-push (multi-machine);
  native minimised app, no terminal windows; minimise em/en dashes in prose.
