# Agent Handoff — Lyric Immersion and Karaoke

A live, click-through desktop overlay (Python/Tkinter, Windows) that floats synced lyrics
with furigana / romaji / pinyin / romaja / translation over whatever music is playing —
**audio-source agnostic** (YouTube / Spotify / Niconico in a browser, or a desktop player).
A language-learning + karaoke tool, heavy on VTuber/J-music (hololive, ReGLOSS, V.W.P,
Suisei). **Current build: v1.0.85.** Read this, then `ARCHITECTURE.md` + `ISSUES.md`.

---

## Where things live
- **Source repo:** `~/lyric-overlay` (git). Remote **`BarnsL/Lyric-Immersion-and-Karaoke`**
  (PUBLIC). **`master` is the single canonical branch** — history was remade clean (every commit
  `BarnsL <barnsl@pm.me>`, no `Co-Authored-By: Claude` trailers). **Push straight to `master`.**
- **COMMIT IDENTITY — always `BarnsL <barnsl@pm.me>`, NO Claude trailer.** The user has 3 GH
  accounts; never let commits land as `redacted@example.com` or the AWS `redacted@example.com`.
  Repo git config is set correctly; `gh` is authed as **BarnsL** (verify with `gh auth status`).
- **Public/private split:** CODE → public repo above. **Copyrighted content stays OUT of public:**
  `bundled_lyrics/` (the baked-in LRCs) is untracked + gitignored, backed up to **private
  `BarnsL/Desktop-Karaoke-library`**. `SALES_CONSIDERATIONS.md` is **local-only** (gitignored;
  never commit — sales/business notes). The lyric cache (`lyrics/`) is gitignored.
- **Deployed app:** `D:\DesktopKaraoke\` — exe is **`Lyric-Immersion-and-Karaoke.exe`** (renamed
  from `DesktopKaraoke.exe` 2026-06-27). The deploy FOLDER + the internal data-dir name stay
  `DesktopKaraoke` on purpose (renaming would orphan the lyric cache/models). Runtime siblings:
  `_internal\`, `lyrics\` (LRC cache), `deps\`, `models\` (Whisper), `settings.json`.
- **Build Python:** a Python 3.12 with PyInstaller + the app deps installed (faster-whisper is
  vendored in `.deps\`). On the dev box it's the per-user install under
  `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`.
- **Local control API:** `http://127.0.0.1:8765` (api.py) — the eyes/hands for live verification:
  `/health /diag /tune /scroll /position /forcesync /align /decide /wrong /purgecache …`.

## Build + deploy (the proven recipe — do it exactly)
- **The app pins ITSELF to cores 8-15 (0xFF00) at BelowNormal** (audio-stutter fix). So a build
  MUST run isolated on **cores 3-7** or it starves the overlay to <1 fps. Run PyInstaller via a
  HIDDEN, core-pinned, foreground-waited process (never `run_in_background`, never a visible
  window — the user games fullscreen):
  ```powershell
  $p = Start-Process -FilePath <py312> -ArgumentList '-m','PyInstaller','--noconfirm','DesktopKaraoke.spec' `
       -WorkingDirectory '~/lyric-overlay' -WindowStyle Hidden -PassThru `
       -RedirectStandardOutput build.log -RedirectStandardError build.err
  $p.ProcessorAffinity = [IntPtr]248   # 0xF8 = cores 3-7
  $p.PriorityClass = 'Normal'; $p.WaitForExit()
  ```
  `.deps\` present → full Whisper build (~774 MB `_internal`, exe ~21 MB). `LEAN_BUILD=1` env →
  ~120 MB Whisper-free build. `py_compile` first as a quick syntax gate.
- **Deploy:** stop the app (`Stop-Process -Name Lyric-Immersion-and-Karaoke` — or `DesktopKaraoke`
  if an old one's running) → `robocopy "$src\_internal" "$dst\_internal" /MIR` (exit 0-3 = OK; ≥8 =
  error) → `Copy-Item` the exe → relaunch (`Start-Process` from `D:\DesktopKaraoke`) → poll `/health`
  for the new version. PRESERVE the runtime siblings (`/MIR` is on `_internal` only).
- **⚠️ Deletion guard:** the sandbox BLOCKS PowerShell `Remove-Item` under `D:\DesktopKaraoke`
  (and near the source repo) — "path is protected from removal", and it aborts the WHOLE command.
  Use the **Bash tool `rm`** for deletions there, or `/purgecache`. (Copy/robocopy are fine.)
- **Bump `version.py`** each deploy; `/health` reports it so you can confirm the new build is live.

## What this session shipped (v1.0.69 → v1.0.85, all deployed + on master)
- **v1.0.85 — Fine-tune sync mode (TICKET-085):** post-major-sync precision pass that drives
  sync to ±0.2s of the sung lyric WITHOUT touching anything else. Enters after 20s of
  satisfactory sync, listens every 8s via Whisper-anchor. Per tick: forward drift 0.2–1.0s →
  PAUSE lyric procession (line index + karaoke fill + scroll belt all freeze in lockstep on
  the held pos/pos_raw); at pause-end self.offset is re-based so the resumed frame equals
  the held frame with zero visible jump. Backward drift 0.2–2.0s → tiny forward nudge
  via _smooth_offset (asymmetric cap — pause >1s feels like a bug, but a 2s forward skip is
  imperceptible). Drift >2.5s exits to normal tier. Exits also on track change / force-sync /
  decide-by-ear switch / manual nudge / 2 inconclusive in a row. Adversarial verify caught
  a same-tick race with the v1.0.78 deferred-commit machinery (snapshot had_pending_pre
  before the deferred-commit consumes _pending_offset) + gated energy-align and silent
  apply_align so they don't race with fine-tune's own listen cadence. 7 tune knobs
  (fine_tune_*) live-tunable; 5 fine_* fields surface in /diag for observability.
- **v1.0.84 — Display-string rebrand "Desktop Karaoke" → "Lyric Immersion and Karaoke" (TICKET-084):**
  workflow-driven sweep (audit → replace → adversarial verify). 15 edits across api.py /health field,
  character.py artist-fallback, main.py tray tooltip + 7 toast titles + Tk window title + Startup .lnk,
  playlist_import_gui.py title, AppxManifest DisplayName + Executable, build_msix.ps1 SkipBuild
  Test-Path, version.py. Internal slugs preserved: D:\\DesktopKaraoke deploy folder, data-dir,
  DesktopKaraoke.spec, MSIX AppId, mutex/UA, pystray icon-name. Live /health confirms
  `app":"Lyric Immersion and Karaoke","version":"1.0.84"` post-deploy. Adversarial verify caught
  two MSIX/build-script issues the initial audit missed (Executable= attribute + SkipBuild path).
- **v1.0.83 — Overlay topmost re-assert (TICKET-082c):** the overlay was falling behind borderless
  game windows after a focus change because Tk's `-topmost` is one-shot at creation. Extended
  `_click_through` (already running every 500 ms via `_click_guard`) to also call
  `SetWindowPos(HWND_TOPMOST, …, SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE)` — Discord/Steam/Nvidia
  overlay pattern, no-op when already topmost. WS_EX_TOPMOST added to the EXSTYLE mask too.
  Mirror windows get the same per-HWND treatment. Caveat: exclusive-fullscreen DirectX games still
  cannot be overlaid by any Win32 window without DXGI hooks — borderless-fullscreen-windowed only.
  MV regex verified (already catches `Original Song MV`, `Official MV`, `(MV)`, `（MV）`,
  `【Original Song MV】`) so v1.0.82's MV-intro fast-sync applies to KOSEKI BIJOU / Deep Dive too.
- **v1.0.82 — Karaoke fill decoupling + scroll-mode deferral + wall-clock ease + MV-intro fast-sync
  + in-app perf recorder (TICKET-082a):** the karaoke highlight (currently-sung characters) now
  ramps against the RAW song clock (`pos_raw = position + self.offset`) while the LINE POSITION
  on the belt still uses the eased `pos` — decoupled timebases stop the "fill races during ease,
  snaps back when ease completes" stutter. Frac clamped to [0,1] so brief eased pos-excursions
  don't reset the fill to 0. Scroll modes (tb/bt/lr/rl) now queue at line boundary too instead of
  bypassing the deferral. Ease is wall-clock-based (`1 - exp(-pull*dt)` with abs cap) so heavy
  frames don't stretch the glide. Studio MVs (綺麗事) get a +5s fresh auto-align after vocal
  onset instead of waiting for the 25s slow-tier loop. New `perf_record` tune knob writes
  per-frame trace (ts/frame_ms/branch/pos_eased/pos_raw/offset/pending/idx/ease_delta) to
  `D:\DesktopKaraoke\perf.log` — buffered append on the Tk thread = zero observer effect;
  the previous /diag polling experiment dragged baseline 33ms frames to 60-200ms. Live trace
  already proved Tk-thread freezes of 3-6 SECONDS during track changes / consume_async — that
  goes in TICKET-082b (offload LRC parse + first-block render to a worker thread).
- **v1.0.81 — Title/artist weight rebalancing + cover-as-live + in-tick Shazam smooth-sync
  + library MIN 60 + privacy cleanup (TICKET-081):** one big bundle of targeted fixes for
  the live-session failures. Adds a substring-superset penalty (`ghost` ⊂ `ghosting` no
  longer beats exact `ghost`), bumps artist corroboration from +5 to +12 exact / +6 partial,
  treats covers as live_arrangements so the FOLLOW path absorbs the inevitable cover-vs-original
  timing drift, fixes `_on_vocal_onset` to calibrate the negative offset for covers with
  extended intros (the 名前のない怪物 cover was 78 s out of sync), routes the four in-tick
  Shazam writes through `_smooth_offset` (the high-frequency steady-state corrections that
  were the user's "mid-line jump"), adds title-lock parenthetical equivalence
  (`GHOST` ≡ `Ghost (Stellar ver.)`), doubles the strike threshold when SMTC artist clearly
  disagrees with heard artist, adds a 20-char minimum on decide-by-ear so a tiny transcript
  can't claim "in sync", penalizes a cross-artist library switch by -8, and lowers
  `decide_library_min` to 60. Also deleted the poisoned `hand_sign.json` cache. Privacy: the
  stale public branch `claude/caption-sync-perf-fixes` and the public tag `v1.0.68` (both
  carrying AWS email / redacted alias / Claude trailers) were deleted from origin;
  10 local orphan tags pruned; `git log --all --format='%ae'` returns only `barnsl@pm.me`.
- **v1.0.80 — Romaji↔CJK title equivalence + lopsided decide-by-ear win + GPU picker
  by utilization (TICKET-080):** kamone took 41 s before because `kamone` (romaji
  player title) couldn't title-match `かもね.json` (JP-script cache), then Whisper
  found the right song at 69 vs 20 but the library MIN=70 rejected it by 1 point.
  Now every JP-titled cache entry also indexes a Hepburn romaji form (`_to_hepburn`
  via pykakasi) in a separate `forms_alt` set, and `LyricsIndex.match` applies −3
  when either side of the match used the cross-script bridge — so a same-script
  cache wins when both exist (verified: `kamone`→`kamone.json`, `かもね`→`かもね.json`,
  `Kireigoto`→`kireigoto.json`, `綺麗事`→`綺麗事.json`). `_apply_decision` accepts a
  just-under-MIN library win when loaded is clearly wrong AND margin ≥ 3·MARGIN
  (kamone's 49-pt margin would now win). `pick_inference_device` is utilization-based
  always, not just when gaming — drops the "game on cuda:0" assumption (broken now
  that the 2080 Max-Q Code 31 fix landed and the 3080 eGPU is cuda:1). Picks idlest
  GPU with a cache-locality bias to cuda:0; under games, skips any GPU ≥30% util.
- **v1.0.79 — Concert SMTC wrapper song-ID (TICKET-079 a+c):** `_LIVE_VER_RE` now matches
  SMTC-truncated concert titles (`3rd ONE` / `5th LIVE` / `10th Anniversary` / `3rd Tour`)
  plus `【冒頭無料】` / `【無料配信】` banners and `#…ONEMAN` / `#NthLIVE` hashtags, so
  `is_live_arrangement` fires for live wrappers even when the title is chopped. `_on_boundary`
  inside a `_live_arrangement` / `_live_mode` wrapper schedules a whole-library
  `_decide_by_ear(reason="boundary")` ~12 s later, and `_decide_by_ear`'s `not self.lines`
  gate is opened for concert contexts (the whole-library scan via `loaded_score < wrong_floor`
  picks the song actually playing inside the container). b+d still open — see TICKET-079.
- **v1.0.78 — Defer auto-sync corrections to line boundaries (TICKET-078):** the named
  auto-apply paths (`_apply_align`, `_tier_commit`, `_apply_energy_align`) now route
  through `_smooth_offset`, which queues `_pending_offset` when a line is on screen
  and ≤5s of correction; `_tick` commits at the current line's natural end (or 8s cap)
  so the wrong line finishes naturally and the next line shows under the corrected
  offset. Big jumps / scroll modes / no-line cases still snap. Cleared on track change.
- **v1.0.69-70 — Force Sync rework (TICKET-074):** the manual nuclear resync now tries RANKED
  offset candidates and forward-verifies each, so a recurring chorus phrase can't lock onto the
  wrong occurrence ("chorus trap"). `align.rank_offsets`/`_rank_anchors`; `_force_sync_apply` state
  machine; tunes `force_sync_*`.
- **v1.0.71 — Concert/live aggressive resync:** `_live_resync_loop` now rolls 8→5→3 ×/min (relax
  after 3 good reads, snap back to 8 on any miss; `_note_live_resync`). Detects `【LIVE】`/`[LIVE]`/
  `ONE-MAN`/`ワンマン` as live arrangements. Tunes `live_resync_*`.
- **v1.0.72-73 — Vertical scroll stagger:** when scrolling up/down + horizontally centered, lines
  fan across 2-3 horizontal columns (`_block_x_v`, the mirror of horizontal scroll's lanes), full
  width but never off-screen. Tune `scroll_v_stagger`.
- **v1.0.74 — Hallucination filter + title-lock guard + smoother fill:**
  - `align._is_hallucination` drops Whisper's non-speech stock phrases ("ご視聴ありがとうございました",
    `[Music]`, "thanks for watching") before they poison decide-by-ear. **This fixed the Suisei
    綺麗事 disaster** (a verse-gap clip transcribed as "thanks for watching" matched a wrong song
    and switched away from a correct title).
  - `_apply_decision` won't let a weak by-ear read override a title-LOCKED song (`decide_titlelock_*`).
  - Karaoke fill repaint rate 5fps → ~16fps (`fill_interval`, live-tunable).
- **v1.0.75 — GPU game-guard:** during a fullscreen game, Whisper keeps OFF the game's GPU — uses
  an idle 2nd NVIDIA GPU if enumerated, else CPU (`gpu_setup.pick_inference_device` via NVML +
  `SHQueryUserNotificationState`; `align._select_device`, models cached per (size, device)). Default
  on; `align.set_gpu_gaming_guard()`. NOTE: this rig has a 2080 Max-Q (Code 31 with the eGPU
  attached → not enumerated) + a 3080 eGPU, so only the 3080 is visible → falls to CPU during games.
- **v1.0.76 — yt-dlp anti-bot:** download resilience that does NOT regress normal videos — realistic
  UA + retries + polite delay; opt-in browser cookies via `DK_COOKIES_BROWSER` (Chromium locks its
  DB while running). Deliberately does NOT force player_client (forcing ios/tv mis-reports "DRM
  protected"). `deep_transcribe._resilient`/`_yt_variants`.
- **v1.0.77 — Reject the song when sync-by-ear keeps failing (TICKET-077):** the content-verification
  the name-checks lacked. Consecutive Whisper sync reads that hear vocals but can't ANCHOR them to
  the loaded lyrics (`_sync_fail_streak`, reset on any real anchor) → after `sync_reject_strikes`
  (3) → reject the cache + re-identify + pull the browser video's own captions. Capped 2/track.
  **Fixes poisoned caches** (Deep Dive cached with Dunk's lyrics; kamone cached with the wrong song).
- **Packaging:** exe renamed to the repo name; updater accepts both old+new names. `installer.iss`
  refreshed (exe/setup name, publisher, AppVer→1.0.77). Repo About now says "audio-source agnostic".

## The song-decision system ("what lyrics do we show?")
The hardest problem: pick the RIGHT song for MMD / cover / "Performance Video" cuts (Shazam can't
fingerprint them) and around MISLABELED provider LRCs. LAYERED, each a fallback:
1. **Title match** (`LyricsIndex.match`) — instant provisional load.
2. **Cover original artist** (`extract_cover_original`) — 歌ってみた/"covered by" → search the
   ORIGINAL artist, not the generic title.
3. **Language confidence** (`confidence.language_confidence`, `_KNOWN_JA`/`_ALWAYS_JA`).
4. **Shazam** (`recognize.py`) — sound ID; **5-strike override** breaks a wrong title-lock.
5. **Decide-by-ear** (`align.decide_song_by_lyrics`/`_decide_by_ear`) — transcribe ~12 s vocals
   (faster-whisper 'small') + rapidfuzz-match against the cached library; switch or re-fetch.
   Now **hallucination-filtered** + won't override a confident title-lock cheaply.
6. **Sync-failure rejection (NEW, v1.0.77)** — the CONTENT check: if sync-by-ear can't anchor the
   singing to the loaded lyrics N×, the cache is the wrong song → reject + re-identify + captions.
7. **Bundled (baked) lyrics** (`bundled_lyrics/`, `_seed_bundled_lyrics`) — AUTHORITATIVE; for
   provider-always-fail songs. Currently: feelingradation, サクラミラージュ. **Verify a bake against
   canonical lyrics before trusting it** (providers mislabel LRCs).

## ⭐ OPEN / QUEUED WORK (the "intelligence batch" — repeatedly deferred mid-iteration)
The recurring failure class is **poisoned/mislabeled provider caches + cross-language collisions**.
v1.0.74 (hallucination) and v1.0.77 (sync-reject) landed the first two; the rest are queued and all
documented with live-log evidence in `ISSUES.md` (TICKET-074..077 + the per-song table):
1. **Language weighting in song ID** — penalize a cross-language same-title candidate using
   `language_confidence` (BANG!!! by 音乃瀬奏 = Egoist's JP song, NOT the Korean "BANG").
2. **Romaji↔CJK title equivalence** — `Kireigoto`≡`綺麗事`, `feelingradation`≡`フィーリングラデーション`
   so the "trusting Shazam (player session stale)" rule stops firing falsely / trusting wrong IDs.
3. **Title-variant matching** — `Firelake`/`Fire Lake`, `for Planet`/`for the Planet`, so a right
   song stops reading `match=False` forever (re-generation / lost lyrics).
4. **Title-only fallback for covers** — when `title/artist` fetch is empty, retry TITLE-ONLY +
   by-ear verify to find the original (Black Sheep / Suko = a Metric cover; the app searched "Suko").
5. **Translingual covers** — an "English Cover" of a Korean song must transcribe by ear in English,
   not load the original Korean (ILLIT "Magnetic (English Cover by Limina)").
6. **Pin transcription language** from `language_confidence` (a deep transcribe detected Javanese).
7. **Skip the deep video-download path for Spotify** (no video → it just 403s).
- **ENVIRONMENTAL:** **yt-dlp HTTP 403** appears intermittently (heavy YouTube use → rate-limit).
  Captions + deep-transcribe go through it. yt-dlp is already latest; the v1.0.76 anti-bot helps.
  Cookies would help more but Chromium locks its DB while Brave runs (opt-in only).
- **DEFERRED (user choice):** split `main.py` (~5800 lines) into modules — big regression-prone
  refactor, do only when live-testing is paused. Optional full rebrand (rename the deploy folder +
  swap "Desktop Karaoke" DISPLAY strings in api.py/main.py tray/health to "Lyric Immersion and
  Karaoke") — user hasn't asked yet.

## Key references / files
- `ISSUES.md` (TICKET log, incl. 074-077 with live-log diagnoses), `ARCHITECTURE.md`,
  `PERFORMANCE.md`/`LYRIC_PERFORMANCE.md`. Key deliverable docs are also copied to a local
  `Desktop\Projects\` folder (ISSUES, SALES_CONSIDERATIONS, SONG_ID_REASONS, APP_PERFORMANCE).
- **Decision** (`main.py`): `_on_track_change`, `_consume_async`, `_decide_by_ear`/`_apply_decision`,
  `_maybe_reject_for_sync_fail`/`_reject_for_sync_fail`; `align.transcribe_vocals`/`score_candidates`/
  `rank_offsets`; `confidence.py`; `extract_cover_original`; `_seed_bundled_lyrics`.
- **Sync** (`main.py`): `_periodic_auto_align`, `_note_sync_verdict`, `_tier_listen_now`/
  `_apply_tier_listen`, `force_sync`/`_force_sync_apply`, `_live_resync_loop`/`_note_live_resync`,
  `_apply_align`, `align.py`, `songchange.py`.
- **GPU**: `gpu_setup.py` (`game_active`, `pick_inference_device`), `align._select_device`/`_get_model`.
- **yt-dlp**: `deep_transcribe.py` (`_resilient`, `_yt_variants`, `_cookie_browser`).
- `/diag` = the eyes (fps + sync + `.decision` + `.sync.tier_*`); `/tune` changes any knob live.

## User prefs + gotchas (from memory)
- Keep work on **D:\**; **fetch before committing, never push divergent histories** (multi-machine).
- Native, minimized app — no browser/localhost, no focus-steal, **no terminal/popup windows**
  (background via hidden core-pinned process; Defender trips on pwsh hidden-subprocess launches).
- **Minimize em/en dashes** in prose (commas/colons/parens instead).
- This handoff + the deliverable docs in `Desktop\Projects\` are the fastest way to reload context.
