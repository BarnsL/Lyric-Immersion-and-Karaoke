# Agent Handoff — Lyric Immersion and Karaoke

A live, click-through desktop overlay (Python/Tkinter) that shows synced lyrics with
furigana / romaji / pinyin / romaja / translation over whatever music is playing
(YouTube / Spotify / Niconico in a browser, or Spotify app). A language-learning +
karaoke tool. Read this first, then `ARCHITECTURE.md`.

## Where things live
- **Source repo:** `~/lyric-overlay` (git). Remote: **`BarnsL/Lyric-Immersion-and-Karaoke`**
  (renamed from Desktop-Karaoke). Work branch: **`claude/caption-sync-perf-fixes`**.
  `origin/master` is a *separate* fresh v1.1.1 history — NEVER push master (unrelated
  root, would need a force). Push to the branch only; it fast-forwards cleanly.
- **Deployed app:** `D:\DesktopKaraoke\` — `DesktopKaraoke.exe` + `_internal\` + runtime
  dirs `lyrics\` (LRC cache), `deps\`, `models\` (whisper), `settings.json`.
- **Local API:** `http://127.0.0.1:8765` (api.py) — agent control + diagnostics.

## Build + deploy (learned the hard way)
- Build: `cd ~/lyric-overlay; $env:PYTHONPATH=".deps"; python -m PyInstaller --noconfirm DesktopKaraoke.spec`
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

## ⭐ NEXT WORK
0. **Split `main.py` (5600 lines) into logical modules** — user-requested, DEFERRED until
   they finish live-testing (a big regression-prone refactor; don't do it mid-iteration).
   Code is already heavily commented and md docs are already in `docs/`; this is purely the
   structural split (e.g. render / sync / matching / api-glue modules). Default branch is now
   `claude/caption-sync-perf-fixes` (the repo homepage renders it; `master` is still the old
   separate v1.1.1 history, untouched).
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

## ⭐ THE SONG-DECISION SYSTEM ("what lyrics do we show?") — current as of v1.0.66
The hardest problem in this app is picking the RIGHT song for MMD / cover / "Performance
Video" cuts (Shazam can't fingerprint them) and around MISLABELED provider LRCs (a
provider returned a *different* song's LRC for feelingradation). The decision is now
LAYERED, each layer a fallback for the last:
1. **Title match** (`LyricsIndex.match`) — exact/≥60 % title overlap; provisional instant load.
2. **Cover original artist** (`extract_cover_original`) — for 歌ってみた/covered-by titles,
   pull the ORIGINAL artist ("Rebellion / hololive English -Advent-" → search qualified, not
   the generic title) so a same-title collision isn't grabbed. Covers with no parseable
   original DROP the channel and search title-first.
3. **Language confidence** (`confidence.language_confidence`) — the artist's usual language
   (`_ALWAYS_JA` Suisei = full JA, `_KNOWN_JA` romanized acts = full JA) rejects an
   English-collision for a JP act. Western artists score 0 (unaffected).
4. **Shazam** (`recognize.py`) — sound ID; **5-strike override** (`wrong_song_strikes`):
   hearing the SAME other song 5× breaks a wrong title-lock and switches (Deep Dive→Dunk).
5. **Decide-by-ear** (`align.decide_song_by_lyrics` / `_decide_by_ear`, the "model") — ~20 s
   in (or `POST /decide`), transcribe ~12 s of vocals with **faster-whisper 'small' (~250 MB
   int8)** and **rapidfuzz**-match the transcript against candidate LYRICS. Two stages:
   title-similar pool first; if the loaded song matches the singing below `decide_wrong_floor`,
   it IDENTIFIES against the **WHOLE cached library** (the model "trained on everything we
   have" — 833 songs, the right one self-matches ~100 vs ~30 for wrong) and switches, or
   re-fetches if nothing cached fits. Skips baked + caption + live songs. `/diag.decision`.
6. **Bundled (baked) lyrics** (`bundled_lyrics/`, `_seed_bundled_lyrics`) — for songs that
   ALWAYS fail to fetch, ship a verified LRC; it's AUTHORITATIVE (sound mis-IDs can't override
   a `source: bundled` song). **LESSON: providers mislabel LRCs — verify a bake against the
   canonical Genius lyrics before trusting it** (feelingradation's first bake was a wrong song).
   Currently baked: feelingradation, サクラミラージュ.

## What this session shipped (all deployed + pushed) — current build **v1.0.68**
- **Waveform + transcript fusion** (TICKET-073) — Whisper listens are waveform-GATED to
  vocal-active windows (`_vocals_active_now`); after a by-ear match the energy correlation
  pins the precise offset. (No local audio fingerprinting — needs reference audio we lack.)
- **Live-version resync ~5×/min** (TICKET-072) — `_live_resync_loop` follows a live cut's
  drifting offset by ear; waveform-gated. `live_resync_s`.
- **Portable RELEASE** — `LEAN_BUILD=1` spec flag → ~120 MB Whisper-free portable zip;
  GitHub release per tag (`gh release create v#`). Whisper is a from-source extra (can't be
  pip'd into a frozen build). Default branch `claude/caption-sync-perf-fixes` IS the repo
  homepage. README/ARCHITECTURE/About renamed + de-stale'd.
- **Decide-by-ear / library-wide song ID** (TICKET-071) — see the decision system above.
- **Baked-lyrics mechanism + authoritative guard** (TICKET-070) — feelingradation + サクラミラージュ.
- **Cover original-artist for "Song / Artist covered by X"** — Rebellion case.
- **5-strike wrong-song recovery** (TICKET-068) — break a wrong title-lock by ear/sound.
- **Cinematic-intro false-positive** (TICKET-069) — looser vocal detect + 20 s backstop.
- **Translation context** (TICKET-067) — numbered-protocol window keeps ±2 lines of context.
- **Anti-stutter Shazam back-off** (TICKET-066) — settled/confirmed song slows the recal poll
  (the GIL-stall stutter on unconfirmable songs); `unconfirmed_backoff_s` / `confirmed_recal_s`.
- **Adaptive sync-verification tier** (TICKET-065) — `_periodic_auto_align` verifies
  ~3×/min while syncing, relaxes to 1×/min once confirmed, snaps back on any miss
  (`_note_sync_verdict` / `_sync_tier_interval`). Energy correlation gives the verdict;
  when it's blind on a song it escalates to a short two-point-verified Whisper listen
  (`_tier_listen_now`). Whisper CPU capped (`align.py cpu_threads=4`). `/diag.sync.tier_*`.
- **Cover detection fix** (TICKET-064) — `_COVER_RE` now catches `【Cover MV】` /
  `（Cover MV）` lenticular tags; covers with no parseable original artist DROP the cover
  channel and search title-first (the MAFIA / マフィア — Ouro Kronii case).
- **Language-confidence + known-acts** (TICKET-062) — `confidence.language_confidence`
  + `_KNOWN_JA` / `_ALWAYS_JA`; GHOST/星街すいせい → JA, feelingradation/ReGLOSS → JA.
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
- `ARCHITECTURE.md` (subsystems + confidence), `ISSUES.md` (TICKET-001..071),
  `PERFORMANCE.md` / `LYRIC_PERFORMANCE.md` (PERF/LP; LP-100/101 = PyGame-SDL2 / single-strip
  GPU paths if 60 fps isn't enough).
- **Song decision** (`main.py`): `_on_track_change`, `_consume_async` (Shazam + 5-strike +
  bundled-authoritative guard), `_decide_by_ear` / `_apply_decision`, `LyricsIndex.candidates`,
  `align.transcribe_vocals` / `score_candidates` / `decide_song_by_lyrics`, `confidence.py`,
  `extract_cover_original`, `_seed_bundled_lyrics`.
- **Sync** (`main.py`): `_periodic_auto_align` (adaptive tier), `_note_sync_verdict`,
  `_tier_listen_now`, `_check_applause_gap`, `_apply_align`, `_auto_align_by_energy`,
  `_eased_offset`, `align.py`, `songchange.py`.
- `/diag` is the eyes (fps + full sync + `.decision` + `.sync.tier_*`); `/tune` changes knobs
  live (incl. all `decide_*`, `sync_tier_*`, `wrong_song_strikes`).
- **PII / repo hygiene:** keep build scratch (`build_*.err/.log/.exit`, `*.bak`, `_batch_fetch*`)
  OUT of git — they embed `C:\Users\<name>\…` paths. `.gitignore` covers them; if a tracked one
  slips in, `git rm --cached` it. No PII in source/docs as of v1.0.66.
- User prefs (memory): keep work on D:\ ; always merge clean / never force-push (multi-machine);
  native minimised app, no terminal windows; minimise em/en dashes in prose.
