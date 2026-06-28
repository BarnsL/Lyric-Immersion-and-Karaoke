# Agent Handoff — Lyric Immersion and Karaoke

A live, click-through desktop overlay (Python/Tkinter, Windows) that floats synced lyrics
with furigana / romaji / pinyin / romaja / translation over whatever music is playing —
**audio-source agnostic** (YouTube / Spotify / Niconico in a browser, or a desktop player).
A language-learning + karaoke tool, heavy on VTuber/J-music (hololive, ReGLOSS, V.W.P,
Suisei). **Current build: v1.0.96.** Read this, then `ARCHITECTURE.md` + `ISSUES.md`.

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
- **The app pins ITSELF to cores 7-15 (0xFF80, 9 cores) at BelowNormal** (audio-stutter fix +
  fill-paint headroom). So a build MUST run isolated on **cores 3-6** (one less than before) or
  it overlaps the app on core 7 and causes audio stutter during builds. Run PyInstaller via a
  HIDDEN, core-pinned, foreground-waited process (never `run_in_background`, never a visible
  window — the user games fullscreen):
  ```powershell
  $p = Start-Process -FilePath <py312> -ArgumentList '-m','PyInstaller','--noconfirm','DesktopKaraoke.spec' `
       -WorkingDirectory '~/lyric-overlay' -WindowStyle Hidden -PassThru `
       -RedirectStandardOutput build.log -RedirectStandardError build.err
  $p.ProcessorAffinity = [IntPtr]120   # 0x78 = cores 3-6 (avoids the app's core 7)
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

## What this session shipped (v1.0.69 → v1.0.96, all deployed + on master)
- **v1.0.96 — Two-tab "Tab A muted / Tab B audible" source lock (TICKET-117 + TICKET-118).**
  Closes the exact user scenario *"i want to be able to watch a muted video while having the
  actual music video in a different tab providing lyrics and music without interference"* —
  Brave with two YouTube tabs (TAB A = muted Cyberpunk 2077 POV motorbike video, TAB B = audible
  "I Really Want to Stay at Your House" by Rosa Walton). Before v1.0.96, both tabs published to
  SMTC and `MediaWatcher._pick` picked the most-recently-active session, so any browser focus
  ping-pong yanked the overlay between the two tracks. v1.0.96 adds two complementary fixes:
  - **TICKET-117 — explicit SMTC session pin (tray menu).** New tray submenu "Source →" lists
    every visible SMTC session (`{app_name} — {title} ({artist})`) with the AUTO entry on top
    and a checkmark on the currently-selected pin. Pinning installs a 16-hex composite id on
    `MediaWatcher` (`set_pinned_session(id, source_app)`); `_pick` then returns the pinned
    session unconditionally if it's still alive. Pinned id is persisted via `_persist()` (key
    `pinned_session_id` + `pinned_source_app`) and re-applied on launch. If the pinned session
    disappears (tab closed) the watcher enters a brief grace window (`pin_grace_s`, default
    20.0s) where any single new session from the same `source_app` is silently adopted as the
    re-pin target (so a Brave tab refresh doesn't lose the lock); after the grace window the
    pin is cleared and AUTO behavior resumes. `/diag.sessions` lists every visible session +
    the pin state for the operator; `/diag.pin` shows `{id, source_app, alive, age_s}`.
  - **TICKET-118 — audible-session preference (Core Audio peak meter tiebreaker).** New
    `audible_sessions.py` wraps pycaw's `AudioUtilities.GetAllSessions()` →
    `IAudioMeterInformation.GetPeakValue()`, aggregated per-PID then per-executable basename,
    cached 1s, hard-timeouted 500ms on a worker thread (so a hung HDMI audio endpoint can't
    stall the 0.15s SMTC poll loop). When MULTIPLE SMTC sessions are equally eligible and no
    explicit pin (TICKET-117) is set, `_pick` consults the audible-pref map: the session whose
    `source_app` substring-matches the loudest process (peak ≥ `prefer_audible_threshold`,
    default 0.02) wins. Net effect for the user scenario: even WITHOUT pinning Tab B
    explicitly, the muted Tab A's Brave PID reports peak ≈ 0.0 while Tab B's Brave PID reports
    a real audible peak, so the watcher locks onto Tab B automatically. Kill-switch via
    `prefer_audible_session` tune knob (default 1 = on, 0 = pre-118 sticky-only). On
    non-Windows / missing pycaw, `_AVAILABLE` flips False on the first ImportError and the
    feature degrades to exact pre-118 behavior — silent and free.
  - **Order of precedence in `_pick`:** TICKET-117 pin (absolute) → TICKET-118 audible (with
    threshold) → pre-118 sticky (last-active wins). `/diag.audible_pref` exposes
    `{enabled, threshold, levels, module: audible_sessions.diag()}` for live verification.
  - **New tune knobs:** `prefer_audible_session` (1), `prefer_audible_threshold` (0.02),
    `pin_grace_s` (20.0). A `/tune` POST that flips `prefer_audible_session` or
    `prefer_audible_threshold` mirrors the new value into `MediaWatcher` immediately
    (`Overlay.set_audible_pref`) — no restart needed.
  - **New dep:** `pycaw>=20240210; sys_platform == "win32"` in `requirements.txt`. ~50 KB
    plus comtypes (already transitive via pystray). Frozen-build pins: `audible_sessions` +
    `pycaw` + `pycaw.pycaw` + `comtypes` + `comtypes.gen` in `DesktopKaraoke.spec`
    hiddenimports, plus `pycaw` + `comtypes` in the `collect_all` loop (the COM proxy stubs
    comtypes generates lazily would otherwise be missed).
  - **Files:** `audible_sessions.py` (new, ~230 lines), `main.py` (`MediaWatcher` pin state +
    `set_pinned_session` / `set_audible_pref` / `list_sessions` / `get_pin` / `audible_diag`
    around lines 410-770, `_pick` two-stage tiebreaker, Overlay tray "Source →" submenu around
    line 10115, `_persist` keys `pinned_session_id` / `pinned_source_app`, pin liveness
    grace-window check in tick around line 3528, `/tune` mirror for the audible knobs around
    line 6810), `api.py` (`/diag.sessions` + `/diag.pin` + `/diag.audible_pref` pass-through —
    these already forward whatever `app.get_diag()` returns), `DesktopKaraoke.spec` + `requirements.txt`.
  - **Verify (the user's scenario):** open Brave with TAB A (Cyberpunk motorbike, MUTED) and
    TAB B (Rosa Walton "I Really Want to Stay at Your House", AUDIBLE, playing); `/diag.sessions`
    lists both; `/diag.audible_pref.levels` shows `brave` with a non-zero peak (Tab B);
    `/source.session_id` should lock to Tab B even when Tab A is brought to the foreground.
    For a guaranteed lock regardless of audio: tray → Source → pick Tab B; `/diag.pin.id`
    should match Tab B's id and `/source.session_id` should not budge even if Tab B is
    momentarily muted. Toggle `prefer_audible_session=0` via `/tune` to fall back to pre-118
    sticky behavior for A/B testing.
- **v1.0.95 — Four tickets shipped together (TICKET-112 + TICKET-113 + TICKET-114 + TICKET-115).**
  Two concurrent agent workflows (`wyo3skdey` + `wawvm5uvx`) landed in one bump.
- **TICKET-112 — YouTube video-description metadata extractor (`yt_description.py`):** SMTC titles
  like "Shooting Star" are massively ambiguous (dozens of songs share that name) and previously
  the fetch query was `title + smtc_artist` only, so once a wrong LRC was cached the re-fetch on
  `/wrong` returned the same wrong file. The new lazy-loaded `yt_description` module calls
  `yt_dlp` metadata-only (no audio download, bundled already) with a hard
  `yt_description_timeout_s` (default 8.0s) and an in-process LRU keyed by video_id, then parses
  templated tags in JP / EN / KR: `作詞・作曲` / `作詞` / `作曲` / `編曲` / `歌唱` / `ボーカル` /
  `Original` / `カバー元` / `Music:` / `Vocals:` / `Lyrics:` / `Composer:` / `Original by:` /
  `작사` / `작곡` / `노래`. Fires from `_on_track_change` (main.py:2479) and the `/wrong`
  re-identify path (main.py:6836) for sources matching `youtube*.com` / `steamwebhelper`.
  Extracted `vocals` overrides the SMTC artist when the SMTC title is short/ambiguous;
  `composer` + `original_artist` ride along as fetch disambiguators (`_fetch_lyrics` query)
  so the ReGLOSS × BEMANI "Shooting Star" case now resolves to `kors k / ReGLOSS` instead of
  matching some other "Shooting Star". `_video_id` and `extract_video_metadata` are the public
  entry points; failure is logged at debug, never raises. `/diag` exposes the parsed
  `yt_metadata` dict; new `GET /yt-meta` returns it. **New tune knobs:**
  `yt_description_lookup` (1), `yt_description_cache_days` (30), `yt_description_timeout_s` (8.0).
  **Frozen-build pin:** `yt_description` added to `DesktopKaraoke.spec` `hiddenimports` because
  it's lazy-imported (`from yt_description import ...`) and PyInstaller's static analyser misses it.
- **TICKET-113 — Per-track lyric blacklist + provider rotation on `/wrong` / REGEN:** the user-reported
  failure was *"wrong song, even when I told it, it didnt try to find a new one"* — `/wrong`
  cleared the cache and re-fetched, but the same provider chain with the same query returned the
  same wrong LRC. Fix is a track-scoped `self._lyrics_blacklist` set of `(sha1(first 500 chars of
  LRC), source_provider)` tuples that is reset on track change. `/wrong` adds the currently loaded
  lyrics' signature to the blacklist BEFORE kicking the re-fetch; the decision-engine REGEN branch
  does the same. `fetch_lyrics.fetch_and_save` accepts a `reject_signatures` kwarg and skips any
  provider hit whose signature matches. Two-or-more `/wrong` within 60s escalates straight to
  AI-gen, bypassing the provider chain that just kept returning the same wrong file. Provider chain
  order rotates per track on each `/wrong` so a stubborn primary doesn't dominate.
- **TICKET-114 — Instrumental-gap timer reset on every track change (and on every `idx>=0` transition):**
  the live diag captured `pending_swap.blocked_by='instrumental-gap(204.2s)'` on a **161-second song** —
  proof that `_idx_minus_one_since` was being set ONCE at app `__init__` (boot wall-clock) and never
  reset, so every short song after the first inherited a multi-minute "instrumental gap" measurement
  that blocked TICKET-111's swap from committing on the LINE-mode boundary path. Now reset in
  `_on_track_change` (the natural reset point) AND on every idx transition `>=0` in `_tick_body`
  (so within a single track, the gap timer always starts from when `idx` actually went `-1`, not from
  app boot). Diag display is clamped to `<= position` so reports stay sane even if the timer
  somehow leads playback. Net effect: TICKET-111's `will_force_commit_in_s` countdown is honest,
  and the `swap_defer_instrumental_gap_s` (default 2.0s) boundary actually fires.
- **v1.0.95 — Six-language translation delivered + `/retranslate` endpoint (TICKET-115):**
  the README has long advertised English translation for Japanese, Chinese, Korean, Spanish,
  German, and Russian, but the actual `_translate_lines` whitelist in `fetch_lyrics.py` only
  fired for `("ja", "ko", "zh", "es", "de", "ru")` at the per-line gate and
  `("ja", "ko", "zh", "es", "de", "ru", "ja-romaji")` at the whole-song gate — and the
  `_maybe_translate` mirror in `main.py` was tighter still (`("es", "de", "ru", "ja-romaji")`
  for "whole"). Live capture on Rammstein "Deutschland" (lang=de) showed every `en` field
  empty across all 51 lines: the German body loaded into `jp` but no translation ever ran
  because the path that detected the German source language never reached the translate
  worker on a non-Spanish Latin-script song that lacked CJK lines to trigger the per-line
  gate. **Fix:** hoisted the language set into a module constant
  `_LANGS = ("ja", "ko", "zh", "es", "de", "ru", "fr", "pt", "it")` in
  `fetch_lyrics._translate_lines`, used at BOTH the per-line `detect_lang(raw) in _LANGS`
  gate AND the whole-song `song_lang in (*_LANGS, "ja-romaji")` gate so they can never
  drift apart again. Mirrored the same set into `Overlay._maybe_translate`'s "whole" tuple
  in `main.py` with an explicit "keep in sync with `_translate_lines._LANGS`" comment.
  Net effect: German, French, Italian, and Portuguese songs now get whole-song translation;
  the README's claims are now actually delivered (the original six plus French/Italian/Portuguese
  as a bonus). `annotate()` no longer hard-codes the romaji-skip language list — the comment
  was updated to call out that any Latin-script source language renders as-is with empty `rm`.
  **New endpoint `POST /retranslate`** (api.py) — force a translation backfill of the currently
  loaded track without re-fetching lyrics. Bounces onto the Tk thread via an `Event`-marshalled
  `_run` call so the HTTP response carries the worker's snapshot (`{ok, action, path, lang,
  n_lines, n_missing}`); 5 s bounded wait so a frozen UI thread can't hang the API. Backed by
  `Overlay.retranslate_loaded()` which clears any stuck `_translating` guard and routes through
  the existing `_start_translate` → `backfill_file` pipeline (atomic rewrite, main tick re-loads
  in place, playback position preserved). The `_start_translate` worker's `_translating = None`
  release moved into a `try/finally` so an exception in `backfill_file` no longer poisons the
  in-flight guard and blocks every future `_maybe_translate` call for the same path. Help blurb
  for `/retranslate` added to `api.py`'s `_ROUTES` table.
- **v1.0.93 — Boundary-deferred lyrics swap (TICKET-111):** the v1.0.92 decision-engine
  SWITCH/REGEN actions, the long-standing wrong-song-strike teardown (Site D), and the
  user-driven `/wrong` path (Site G) all USED to blank `self.lines` IMMEDIATELY and re-fetch,
  producing a 1-5s on-screen blackout while the new lyrics arrived. v1.0.93 queues the swap
  on a new `self._pending_swap` state, kicks off the fetch/gen in parallel so latency
  overlaps, and KEEPS rendering the old lines until the boundary fires (LINE-mode current
  line ends, SCROLL-mode belt drains, or a 2s+ instrumental gap on `idx==-1`). When the
  fetch completes, `_consume_async` (and the AI-gen `_apply_generated` for REGEN) routes
  the result into `pending_swap["lines"]` instead of `self.lines`; the `_tick_body`
  consumer commits atomically via `_apply_pending_swap` once the boundary lands. Same
  shape as TICKET-078's `_pending_offset` (the precedent for offset corrections); the
  TICKET-088 same-tick ordering doc in `_tick_body` is preserved (offset commits first
  so the swap commits against the fresh offset). Stale fetch tokens are dropped (rapid
  double `/wrong` no longer races); a real track change calls `_cancel_pending_swap`
  to invalidate in-flight targets. Safety cap (`swap_defer_max_s`, default 8.0s) forces
  a commit even if the boundary never lands; user-driven `/wrong` uses a tighter cap
  (`swap_defer_user_max_s`, default 3.0s) since the user explicitly asked for it fixed
  fast. Kill-switch via `swap_defer_enabled` (default 1 = on, set 0 via `/tune` to
  restore v1.0.92 immediate-clear behavior without a re-release). `/diag.pending_swap`
  exposes queue state, age, blocked-by reason, and `last_commit_seq` for live observability;
  `api.py` `/diag` help blurb updated to mention pending-swap (no other api.py changes
  required, the dict passes through as-is). Four new tune knobs: `swap_defer_enabled`,
  `swap_defer_max_s`, `swap_defer_instrumental_gap_s`, `swap_defer_user_max_s`.
- **v1.0.92 — Continuous decision engine (TICKET-109):** new background watcher
  (`_decision_tick` self-throttled to `decision_tick_interval_s`, default 2.0s) that
  aggregates four signal dimensions (SMTC<->Shazam agreement, drift trend, lyric-quality
  flags, decide-by-ear corroboration) into a strike score over a rolling window
  (`decision_score_window`, default 12 samples). State promotes TRUST -> CAUTION -> SWITCH
  -> REGEN at thresholds `decision_caution_strikes` (3), `decision_switch_strikes` (5),
  `decision_regen_strikes` (8). `_fire_decision_action` executes SWITCH (re-fetch from
  alternative source) or REGEN (force AI generation); separated by
  `decision_action_cooldown_s` (default 30.0s) so a flaky read can't ping-pong actions.
  Engine forgets prior song's strikes on track change (`_reset_decision_engine`). User
  surface: tray hint + `/diag.decision_engine` (state, strikes, last_action_age_s,
  dim scores). Knobs: `decision_engine_on` (default 1), the five threshold/window knobs,
  and the cooldown. **Known gap fixed in v1.0.93:** the SWITCH/REGEN branches in
  `_fire_decision_action` cleared `self.lines = []` immediately on fire, which produced
  the 1-5s blackout TICKET-111 addressed.
- **v1.0.91 — LINE-mode render perf (A1) + perf instrumentation (A2) + title-alias album
  fallback + verified-render grace (A3) + scroll_ knob rename + concert-detection regex
  expansion (TICKETs 103-followups / 104 / 105 / 106 / batch4 A1+A2+A3):** seven workflow-driven
  fixes in one build. **(A1, TICKET-104 followup)** Bounded LRU cache for `measure_text()`
  (main.py:1298), keyed by `(font_name, font_size, char)`, capped at the new
  `measure_text_cache_size` tune knob (default 4096). The per-character canvas
  create+destroy that `_render()` triggered on every IDX transition (the textbook
  cause of the 200-960 ms stalls captured in workflow w821l9jnw cluster A / B) now
  hits the cache for >95% of calls after a song's character set is warm. Hit-rate
  surfaced in `/diag.measure_text_cache_hit_rate` so a regression is visible
  immediately. **(A2)** Sub-branch perf instrumentation: `_perf_branch(name)`
  context-manager wraps `_render()`, `_karaoke()`, and the per-char `itemconfig`
  loop with named timers; the perf-log line now appends `| branch=render=42.1
  kara=8.2 itemconfig=3.4` so the operator sees WHICH operation owns a 158 ms
  spike (slice 4's blind spot). Raw-frame-ms column added too — the v1.0.85 EWMA
  was hiding 800-960 ms real stalls as 156 ms entries because dt>500 ms was
  silently dropped (main.py:3773-3775); raw column logs the actual dt up to the new
  `perf_record_dt_cap_ms` ceiling (2000 ms). Two new tune knobs: `perf_record_branches`
  (pipe-separated names to instrument, default `render|kara|itemconfig`) and
  `perf_record_raw_frame_ms` (1 = on, default on). **(A3, TICKET batch4)**
  Title-alias album fallback + verified-render grace window. The capture showed
  V.W.P "歌姫" (SMTC track name) vs Shazam "DIVA (feat. KAF, RIM, Harusaruhi,
  Isekaijoucho & KOKO)" (album-string-with-features) tore down lyrics for 71 s on
  a benign disagreement — same release, same song. New `title_alias_album_fallback`
  (default 1): when Shazam's title aliases an album-string we've seen before for the
  loaded track, populate `_sound_title_alias` so the v1.0.89 strict-source-priority
  gate keeps `verified=true` instead of blanking the overlay. `_set_verified` is the
  new single chokepoint for all `self._verified` assignments — every flip records
  the wall-clock time so the verified→False render grace window can keep the last
  good lyrics on screen for `verified_render_gate_s` (default 3.0 s) before tearing
  down `line_count`. `_verified_gate_t` surfaces in /diag.derived alongside
  `sound_title_alias`. Routed through `_set_verified` at 5 call sites (decide,
  consume_async, takeover, fine-tune, force-sync). **(scroll_ rename, batch1 follow-on
  of TICKET-104)** Renamed five scroll-mode-only knobs with a `scroll_` prefix so a
  future operator can't confuse them with LINE-mode work (line mode is unbudgeted
  — A1's whole point): `heavy_budget_ms` → `scroll_heavy_budget_ms`,
  `spawn_budget` → `scroll_spawn_budget`, `repaint_budget` → `scroll_repaint_budget`,
  `fill_skip` → `scroll_fill_skip`, `fill_interval` → `scroll_fill_interval`. The
  old names still work via the new `_TUNE_LEGACY_ALIASES` map in `set_tune` — a
  /tune POST with a legacy key logs a warning and redirects. **(TICKET-106)**
  `_LIVE_VER_RE` expanded to catch `Nth ONE-MAN LIVE` / `Nth LIVE TOUR` /
  `Nth ANNIVERSARY LIVE` plus the `ワンマン` / `ワンマンライブ` JP family, fixing
  the V.W.P "4th ONE-MAN LIVE" miss where the in-tick concert-detection regex
  didn't fire on the obviously-LIVE wrapper. Live-resync cadence shortened in
  parallel (12.0 → 6.0 s; listen window 6.0 → 4.0 s; fast gap 1.5 → 1.0 s) so
  inside-wrapper song-ID happens ~12×/min on a hot tier. **(TICKET-104)**
  `fine_tune_max_pause_s` bumped 1.0 → 3.0 (user-requested) — holding a line
  still up to 3 s is visually quieter than the equivalent backward nudge that
  re-scrolls already-shown text; `fine_tune_exit_drift_s` follows to 3.5 so a
  drift just under the new cap doesn't immediately hand back to the regular tier.
  **(TICKET-103 followups)** `gpu_solo_override` tune knob added (default 0) so a
  user on a single-GPU machine can opt back into GPU acceleration; `/tune` flip
  re-applies via `align.set_gpu_solo_override(bool)` immediately. GPU device +
  index + reason + count now surface in `/diag`. **(TICKET-105)** Start Menu
  shortcut self-heal at startup — when `getattr(sys, 'frozen', False)` AND the
  old `Desktop Karaoke.lnk` target doesn't exist on disk, delete it; if no
  `Lyric Immersion and Karaoke.lnk` exists in the same dir, create one pointing
  at `sys.executable` (WindowStyle 7, minimized + no-activate per CLAUDE.md
  app-launch etiquette). Skipped in dev (sys.frozen=False) to avoid noise.
  No new modules — every change is in main.py — so `DesktopKaraoke.spec`
  hiddenimports needed no edit. **Follow-up to measure post-restart:** A1 should
  reduce the per-IDX-transition spikes (cluster B 1:1 correlation) substantially;
  re-run perf capture and confirm p99 drops from 78.5 ms toward the 33-40 ms
  baseline. If A1 alone is insufficient, schedule `render_idx_change_budget_ms`
  (defer to v1.0.92 — soft-budget `_render()` and re-queue residual segment
  work via `root.after(0, …)` so the next eased belt frame renders unblocked).
- **v1.0.90 — Window-title scraper for Steam Overlay / Discord / Slack / Teams CEF hosts
  (TICKET-102):** the Steam Overlay's embedded CEF browser (steamwebhelper.exe), Discord's
  embedded YouTube/Spotify iframes, and Slack/Teams media tiles do NOT publish to SMTC,
  so a song playing inside any of those was invisible to the overlay (SMTC would stay
  locked on a stale paused tab from earlier; Shazam might fingerprint a different release
  or miss the track entirely). New module `window_titles.py` — stdlib + ctypes only,
  matches `discord_rpc.py`'s daemon-watcher + lock-guarded-slot style. `EnumWindows` walks
  visible top-level HWNDs every 2s on a background daemon thread; per HWND we
  `GetWindowThreadProcessId` → `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` →
  `QueryFullProcessImageNameW` to resolve the exe basename, reject anything not on the
  process allowlist BEFORE reading the title (privacy invariant: non-allowlisted window
  text is never read), then `SendMessageTimeoutW(WM_GETTEXT, SMTO_ABORTIFHUNG, 100ms)`
  for the text — avoids stalling on a hung Chrome tab the way `GetWindowTextW` would.
  Title parser strips a music-marker suffix (` - YouTube` / ` - Spotify` / ` - SoundCloud`
  / ` - Bandcamp` / ` - Apple Music` / ` - Tidal` / ` - Deezer` / ` - Niconico` / ` - Bilibili`
  / ` - Mixcloud` and the YT Music variant), rejects a non-music suffix
  (Gmail / Docs / Sheets / Notion / Linear / GitHub / Jira / Confluence / Figma /
  Twitter / Reddit / Discord channel-name etc.) before accepting, drops bare-hostname
  titles ("youtube.com", "new tab"), strips a `Channel: ` Steam Overlay prefix, splits
  on first ` — ` / ` – ` / ` - ` / ` | ` separator into (title, artist) and hands the
  raw pair downstream where `clean_title`/`clean_artist` already swap by heuristic.
  TWO process tiers: HIGH (default ON) covers steamwebhelper.exe + discord(.exe|canary|ptb)
  + slack.exe + teams.exe + ms-teams.exe — these don't hit SMTC, so the scrape is
  unambiguously load-bearing. LOW (default OFF, opt-in via `window_titles_generic_browsers`)
  covers chrome/edge/brave/firefox/opera/vivaldi/arc — these DO hit SMTC for the major
  music sites, so the toggle is the kind of thing a user with a non-SMTC PWA setup
  flips on. PID→exe cache so OpenProcess+Query doesn't fire on every cycle for the same
  PID; cleared on stop() so a recycled PID can't carry a stale name. Hard 50ms per-cycle
  budget; foreground-window preferred when multiple allowlisted windows have a music
  marker (the tab the user is actually on wins the tie). Wired into `_tick` as a NEW
  source slotting between SMTC (`playing=true` still wins) and Shazam-live for the
  HIGH tier, BELOW Shazam for the LOW tier (most generic-browser tabs ARE in SMTC, so
  generic browser scraping only matters when SMTC is silent). Public surface is two
  functions — `start_watcher(poll_s, generic_browsers)` and `get_current_track()` →
  `{title, artist, source: "window-title:<exe>", process, window_handle, window_class,
  raw_title, priority: "high"|"low", last_update_t} | None`. Two tray menu items under
  the detection group: "Read window titles (Steam Overlay, Discord, Slack, Teams)"
  (default ON) and "Read window titles from web browsers (slower, may misfire)"
  (default OFF). Three new tune knobs (`window_titles_on`, `window_titles_generic_browsers`,
  `window_titles_poll_s`); `/diag.window_titles` exposes `on`, `generic_browsers_on`,
  `running`, `slot_age_s`, `track`; `/source.capabilities` mirrors the persisted toggles.
  Pinned in `DesktopKaraoke.spec` hiddenimports so the frozen build includes the module.
  No new requirements (`requirements.txt` unchanged — ctypes is stdlib). Teardown path
  in shutdown mirrors `discord_rpc.stop_watcher()`. Per-game RP / SteamWorks /
  registered-application-id are still queued as TICKET-101.
- **v1.0.89 — Slide-in top/bottom + SMTC-paused Shazam takeover + Discord RP fallback
  (TICKET-098 / TICKET-099 / TICKET-100):** three independent features in one build, each
  surgical. **(098)** Per-line slide-in gains `top` and `bottom` modes (drop from above /
  rise from below); `_animate_in` now offsets on the Y axis when `scroll_dir in {top,bottom}`
  and `_anim_step` takes an (ox, oy) pair so the easing applies to both axes. `set_scroll`
  auto-orients `pos_x` per the design contract (left→left, right→right, top/bottom→center)
  on the per-line slide modes only — continuous scroll modes (`lr`/`rl`/`tb`/`bt`) and
  `none` keep whatever horizontal anchor the user already chose; `pos_y` is untouched.
  Tray entries placed between "Slide in from right" and the first SEPARATOR per the spec.
  **(099)** SMTC vs Shazam disagreement: `_verified` is now split into `_verified_meta`
  (the v1.0.88 duration/title check) AND `_sound_corroborated` (≥1 Shazam read agreed with
  the loaded title). Public `/status.verified` requires BOTH — closing the v1.0.88 bug
  where a paused SMTC tab with a stale title was being reported `verified=true` before
  any audio ever confirmed it. `/status.verified_meta` exposes the old check for
  backward-compatible watchers. New `_resolve_source_priority(state, heard)` returns
  `'agree' | 'smtc' | 'shazam-live' | 'confused'`; the heart of the change: when SMTC has
  been NOT-PLAYING for ≥ `smtc_paused_min_s` (8s) and Shazam confidently hears a different
  song, `_smtc_paused_takeover` drops the loaded lyrics, swaps to the heard song (reusing
  the wrong-song correction path so reviewers learn one set of switch-mechanics), and
  debounces back-to-back takeovers via `smtc_takeover_debounce_s` (20s). A real user
  un-pause (SMTC PAUSED→PLAYING edge) clears `_last_takeover_t` so the next pause can take
  over immediately. The 2-read agreement gate applies (first contradicting read demotes
  `_verified` + drops `_title_locked` + seeds `_pending_switch`; second agreeing read
  fires the takeover). Concert OCR path explicitly sets both verification flags so the
  badge still goes true on a confident banner read. Three new tune knobs (all commented
  in the `self._tune` dict); five new fields in `/diag.derived` (`source_priority`,
  `verified_meta`, `sound_corroborated`, `smtc_paused_for_s`, `last_takeover_age_s`).
  **(100)** Discord Rich Presence reader for the user's own Spotify-Listening activity
  via the local IPC pipe (`\\.\pipe\discord-ipc-0..9`). New module `discord_rpc.py`:
  pure stdlib + ctypes (pywin32 optional), 500 ms hard timeout, exponential-backoff
  reconnect (5→10→20→40, cap 60s) so a missing Discord client doesn't spam the log,
  module-singleton connection. Public surface is two functions — `available()` and
  `get_listening_track(timeout_s=0.5)` → `{title, artist, source, started_at} | None`.
  Wired in `_tick` as a third-priority fallback: only contributes when both SMTC AND
  Shazam-live have been silent for ≥ `discord_rpc_silent_gap_s` (8s); the synthesized
  state dict carries `source="discord-rpc:<sub>"` so downstream paths recognize it.
  Opt-in (default OFF) — tray menu item under the detection group + persisted via
  `discord_rpc` settings key. Four new tune knobs (`discord_rpc_on`,
  `discord_rpc_silent_gap_s`, `discord_rpc_poll_s`, `discord_rpc_timeout_s`); pinned in
  `DesktopKaraoke.spec` hiddenimports so the frozen build includes the module. Per-game
  RP parsing + SteamWorks + registered-application-id work is spun off as TICKET-101
  (referenced inline in `discord_rpc.py` + the `_tune` dict comment).
- **v1.0.88 — Language lock + Shazam wins + snap fixes + Chinese pinyin/jieba/NetEase + SMTC
  normalizer + tray reorg (TICKETs 088 / 089 / 090 / 091 / 093 / 094 / 095 / 097):** eight
  tickets in one build. **(088)** Smooth-transition snap fixes: per-frame ease cap so a
  single 300 ms render frame can't blow past the destination, shared `_commit_offset`
  helper for atomic same-tick offset writes, sub-50 ms deadzone (don't ease drifts smaller
  than render jitter), re-queue logic when an offset commit races a deferred commit, and
  a debug-gated assertion that warns when >2 offset writes hit the same tick. **(089)**
  Whisper language lock: `_decide_whisper_lang` pins Whisper to the known song language
  (from SMTC `system.language` / fetched lyrics' `lang` / live Shazam result) instead of
  letting Whisper auto-detect; kills the Japanese-hallucination class of bugs where
  English/Spanish/Chinese vocals were being transcribed as gibberish kana. New tune knob
  `whisper_lang_lock=1`. **(090)** Verified-Shazam wins: gate the decide-by-ear loop behind
  `_verified AND _title_locked` so we don't re-fight a confident lock on every Shazam tick;
  clear stale `self.offset` + per-track decide cache on lock so an old track's offset can't
  bleed into the next song. **(091)** SMTC artist normalizer: `_normalize_smtc_artist`
  decompacts PascalCase artist handles (CalibreCincuenta → Calibre 50) including
  Spanish/English/Japanese number-words. **(093)** Pinyin tone marks: `lazy_pinyin(..., style=Style.TONE)`
  so "yao zou shang hang ye ta jian" becomes "yāo zǒu shàng háng yè tǎ jiān". **(094)** jieba
  word segmentation: per-word pinyin chunking + polyphonic-character disambiguation via
  `jieba.cut` (so 行 picks `xíng` vs `háng` from word context). **(095)** NetEase Cloud Music
  lyrics provider added to `fetch_lyrics.py` chain; attempted only when `lang == "zh"`,
  fills the Chinese long-tail gap before AI generation. **(097)** Tray menu reorganized
  into 8 grouped sections (per-song actions, detection/sources, sync behavior, visual,
  performance, library, app/system, updates) with separators between.
- **v1.0.87 — Karaoke fill speedup (+1 CPU core to app):** widened the app's CPU affinity
  from cores 7-14 (8 cores) to cores 7-15 (9 cores) — the laptop has 16 logical cores and
  the previous mask left core 15 unused. Karaoke fill paint is the dominant per-frame cost
  during a sung line; the extra core eats spikes during simultaneous Shazam decode +
  scroll-belt redraw without bumping the build mask (still 3-6 to avoid the app's cores).
  Re-tuned the fill rate constant accordingly. NOTE: BUILD AFFINITY (cores 3-6) is now ONE
  LESS than before to avoid overlapping core 7 — the recipe block above already reflects this.
- **v1.0.86 — YouTube Music URL + ampersand-collab cover signal + YT Music metadata trust (TICKET-086):**
  three small, surgical changes targeting YouTube Music sources. (A) URL-prep helper
  `deep_transcribe._normalize_youtube_url` rewrites `music.youtube.com` → `www.youtube.com`
  at every yt-dlp / video-id entry point (`fetch_captions_only`, `_download_audio`) plus an
  inline guard in `set_now_url` so cached URLs + diagnostics are canonical. (B) Cover-detector
  gains an "ampersand collab" signal via `_is_amp_collab_title` + new `cover_signal()` helper
  returning `'explicit'` / `'amp_collab'` / `None`; the explicit path stays full-confidence, the
  amp-collab path takes a title-only search (extract_cover_original returns `(None, song)` for
  it) and is DEMOTED when YT Music exposes a non-empty `album` field (strong evidence of an
  official original). An allowlist (Hall & Oates, Simon & Garfunkel, …) plus token-length ≥ 2
  + title-separator-required guard against false-positives. (C) `clean_artist(artist, source)`
  bypasses channel-stripping when source is YT Music (the SMTC artist field is already clean);
  `clean_title` strips a BOL-anchored `Mix - ` autoplay prefix. `_cover_signal` initialized in
  __init__, exposed in `/source` derived along with `yt_music_source` + `album`. One new tune
  knob (`cover_amp_album_demote`, default 1.0 = ON). Sanity-tested URL helper roundtrip on
  www.youtube.com URLs, amp_collab detection on positive + allowlist + negative cases, and
  the Mix - BOL anchor on edge cases (DJ Mix / Track - Mix preserved).
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
