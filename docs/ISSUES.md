# Desktop Karaoke — Issue Tickets

Numbered tickets for matching / sync / rendering / performance / features.
Status: 🔴 open · 🟡 in-progress · 🟢 fixed (pushed) · 🔵 needs-repro

**Verification rule:** always compare the app's line to the **video's on-screen
lyrics** at the same playback position — not just `/status`.

---

## TICKET-079 — Concert SMTC wrapper defeats song-ID 🟡 (a+c landed; b+d open)
**Symptom (VESPERBELL 3rd ONE-MAN LIVE BEYOND):** the app was alive for the entire
6-minute window inside the concert (00:50:37 → 00:56:34) without showing a single lyric.
The logs say it all:
```
00:50:37 track change: 'VESPERBELL 3rd ONE' / 'VESPERBELL' (dur None)
00:50:37 no confident title-match for 'VESPERBELL 3rd ONE' (best 0); will use sound
00:50:57 decide-by-ear (track-start): listening among 5 title candidates   ← stale candidates from prior song; result NEVER logged
00:51:03 audio boundary detected → re-identifying by sound
00:51:32 audio boundary detected → re-identifying by sound
00:54:14 audio boundary detected → re-identifying by sound
00:54:54 audio boundary detected → re-identifying by sound
00:54:58 same song re-reported ('VESPERBELL 3rd ONE') — keeping sync, no reset
```
ZERO `heard '...'` lines for the whole window. Four piled-up failures:
- **(1) SMTC truncated** `【冒頭無料】VESPERBELL 3rd ONE-MAN LIVE BEYOND #VESP3rdONEMAN`
  to `VESPERBELL 3rd ONE` — `is_live_arrangement` (`_LIVE_VER_RE` requires `one[\s-]?man`)
  missed it → `live_arrangement=false` → no live-mode aggressive resync, no follow-the-offset.
- **(2) Shazam never returned a hit** — expected for MMD/live performances (TICKET-072
  already documents this).
- **(3) `_decide_by_ear` bailed on `not self.lines`** — no LRC was loaded because the
  wrapper title matched nothing (best 0). The "5 title candidates" line is misleading
  — those were stale candidates and the function didn't reach the whole-library scan
  for a freshly-empty `self.lines`.
- **(4) Boundary detections were no-ops** — `_on_boundary` fires Shazam (which fails on
  live cuts) and that's all. Inside a concert wrapper that means every real song change
  inside the container goes un-identified, while the "same song re-reported" SMTC gate
  suppresses any wrapper-level reset.

**Fix (v1.0.79) — (a) + (c) landed:**
- **(a) Truncation-tolerant `_LIVE_VER_RE`:** adds `\d+(?:st|nd|rd|th)\s+(?:one|live|tour|anniv(?:ersary)?)`
  so "3rd ONE" / "5th LIVE" / "10th Anniversary" / "3rd Tour" all classify as live
  arrangements regardless of where SMTC chops the title. Also adds `【冒頭無料】` / `【無料配信】`
  live-broadcast banners and hashtag tells `#…ONEMAN` / `#3rdLIVE`. Smoke-tested: VESPERBELL
  truncated form → LIVE, normal song titles (feelingradation / white balance / `KAF #128`) → std.
- **(c) Boundary in a concert wrapper fires whole-library decide-by-ear:** `_on_boundary`
  now schedules `_decide_by_ear(reason="boundary")` ~12 s after Shazam when
  `_live_arrangement or _live_mode`. The gate in `_decide_by_ear` is opened for concert
  contexts: it no longer bails on `not self.lines` when we're inside a concert wrapper or
  boundary-triggered — the whole-library scan path (loaded_score < wrong_floor) takes over
  and adopts the best library match for the song actually playing inside the container.

**Still open (deferred):**
- **(b) Decide-by-ear MIN tuning for concerts** — the current 70 library threshold may be
  too high for a 12 s vocal sample at concert audio quality. Watch real runs.
- **(d) Concert setlist mode** — pull setlist.fm (or accept paste-in `MM:SS — Title` CSV)
  and pre-seed candidates by time window inside the concert wrapper. Highest value, biggest
  surface area; do as its own pass.

---

## TICKET-078 — Defer auto-sync corrections to the next line boundary (no mid-line snap) 🟢
**Symptom:** every auto-sync correction (energy-align, tier-listen, align-by-ear) wrote
`self.offset = X` immediately + `self.idx = -1`, so any line on screen jump-cut to a
different line mid-display whenever the sync moved by even ~0.5 s. The eased display
offset hides this for the karaoke FILL but not for the LINE selection — pos = position
+ eased crossed line boundaries during the glide, swapping lines under the user's eyes.
**User's ask:** *"i want it to fade into the sync, allow the current line onscreen to
finish even if its wrong and start the next line it thinks it is after the last wrong
line for better user experience."*
**Fix (v1.0.78):**
- New `_smooth_offset(new_off, reason)` — queues `_pending_offset` instead of writing
  `self.offset` directly when a line is currently visible and the jump is ≤ 5 s.
- `_tick` commits the queued offset only when `cur_pos >= current_line.end` (the wrong
  line has finished), then clears `idx = -1` so the very next tick picks the right next
  line under the new offset. 8 s safety cap commits a stuck pending regardless.
- Big jumps (>5 s), continuous-scroll modes (`lr/rl/tb/bt`, no discrete lines), and
  corrections taken when no line is showing (`idx<0`) all bypass and snap as before —
  deferring those would be worse than snapping.
- Routed through `_smooth_offset`: `_apply_align`, `_tier_commit`, `_apply_energy_align`.
- Untouched (delicate or already-staggered): the in-tick Shazam follow/confirm path
  at `main.py:~2320` (live-follow has its own two-point hesitation), `_apply_decision`
  resets, force-sync probes, vocal-onset calibration (fires while `idx==-1` anyway),
  track-change reset to 0.
- `_pending_offset` is cleared on every track change so a queued correction from the
  previous song can't bleed across.

---

## TICKET-077 — Reject the song when sync-by-ear keeps failing (poisoned cache) 🟢
**Symptom:** "Deep Dive" / 轟はじめ (ReGLOSS) showed **Dunk's** lyrics the whole time.
```
22:17:06 title-match 'Deep Dive' -> deep_dive_轟はじめ.json (score 85)
22:17:25 heard 'Deep Dive' / 'Todoroki Hajime' | loaded 'Deep Dive/轟はじめ' | match=True   (every read)
```
Both checks PASS — because they check the **name**. But `deep_dive_轟はじめ.json` is a
mislabeled `syncedlyrics` LRC whose LINES are Dunk's ("Game on, hearts racing again",
"踏み鳴らすMotion"). Same poisoned-cache class as [[TICKET-075]] (kamone) — title-match +
Shazam confirm the title; **nothing verified the lyric CONTENT against the singing**, so a
provider that mislabels song B's LRC as song A is trusted forever.
**Fix (the user's ask — "reject once sync fails a few times"):** the periodic sync-by-ear
(Whisper) transcribes the vocals and tries to ANCHOR them to the loaded lines. For the right
lyrics it anchors; for the wrong lyrics it returns NO anchor *every* time. So count
consecutive no-anchor reads (`_sync_fail_streak`, reset on any real anchor in
`_apply_tier_listen`/`_tier_commit`) and after `sync_reject_strikes` (3) → reject:
`report_wrong()` (bin the cache + unlock + re-identify) and, for a browser video, pull the
video's OWN captions (authoritative real lyrics, now fetchable via the v1.0.76 anti-bot).
Capped at 2 rejects/track so it can't loop. This is the CONTENT verification the name-checks
lack — the centerpiece of the queued intelligence work.
**Note:** relies on the periodic Whisper sync check running (it escalates when the energy
correlator goes blind, which a wrong-lyrics song does). The poisoned `deep_dive_轟はじめ.json`
was also purged.

## TICKET-076 — Cover of a famous song not detected: "Black Sheep" (Suko, cover of Metric) 🔴
**Symptom:** A Suko cover of Metric's "Black Sheep" (same lyrics, close to the original)
generated by ear instead of matching the well-known original. "Adjust the weights so the
confidence is high enough to run it."
**Log evidence (Spotify source):**
```
21:05:30 track change: 'Black Sheep' / 'Suko' (dur 259.9)          # Spotify authoritative title/artist
21:05:30 no confident title-match for 'Black Sheep' (best 0)       # not cached
21:06:14 no lyrics after the grace window (lookup came up empty) -> generating by ear
21:06:18 deep: download failed 'Black Sheep Suko': HTTP 403        # Spotify has no video to fetch — path is moot
# (no 'heard …' Shazam line at all → the fingerprint never bridged the cover)
```
**Why it happened:**
1. **Not cover-tagged + searched under the COVER artist.** "Black Sheep / Suko" has no
   `cover` marker, so `extract_cover_original` never fired; the lyric fetch ran as
   **"Black Sheep" / "Suko"** and came up empty. The original — **"Black Sheep" / Metric**
   — is trivially available from providers, but nothing ever searched Metric.
2. **No title-only fallback.** When the artist-qualified fetch is empty, the app doesn't
   retry **title-only**, which would surface Metric's original (whose lyrics the cover
   matches almost exactly).
3. **A fingerprint can't bridge a cover.** Shazam fingerprints the *recording*; Suko's
   cover is a different recording, so it IDs (at best) Suko's own track, not Metric's —
   hence no `heard …` line. The signal that COULD bridge it, **decide-by-ear lyric
   matching**, had nothing to match against because the original's lyrics aren't cached.
4. **Weights never let a same-title / different-artist candidate "run."** Even if a
   title-only fetch found Metric's "Black Sheep", the current confidence model doesn't
   trust a different-artist match enough to commit — so it defaults to generating. This
   is the weight the user wants raised.
5. *(side)* The **deep-download path is moot for Spotify** (no video to pull) yet still
   runs and 403s.
**Proposed fix:**
- **Title-only fallback fetch:** when `title / artist` returns nothing, retry **title-only**
  and take the most popular provider hit (the original), **gated by decide-by-ear
  verification** — does the heard vocal match the fetched lyrics? A cover "close to the
  original" passes easily, and the ear-verify stops a wrong same-title song from locking.
- **Adjust the confidence weights** so a title-only / different-artist candidate that the
  EAR confirms clears the run threshold (let a strong by-ear match override the missing
  artist match). This is the user's "make the confidence high enough to run it."
- **Skip the deep video-download path for non-YouTube (Spotify) sources** — it can't
  download and just 403s. Optionally read richer metadata via the spotipy Web API.
- Shares the root with the cover-ID reasons list (translingual/same-title covers) and
  [[TICKET-075]] (don't generate when a confident source is reachable).

## TICKET-075 — XOVERLINE (cached ReGLOSS song) generated by ear instead of playing it 🔴
**Symptom:** XOVERLINE / ReGLOSS — a common, already-CACHED song — showed AI-generated
`***` lines for ~30 s+ before the real lyrics appeared. "It shouldn't be hard to find"
— and it wasn't: `xoverline.json` was already on disk. The app just didn't use it.
**Log evidence (21:00, before the v1.0.72 redeploy):**
```
20:59:48 title-match 'XOVERLINE' -> xoverline.json (score 65)     # cache HIT, but low score
20:59:49 audio boundary detected -> re-identifying by sound        # diverted off the cache @ +1s
21:00:20 no lyrics after the grace window (lookup came up empty) -> generating by ear
21:00:25 deep: download failed 'XOVERLINE ReGLOSS': HTTP 403 Forbidden
21:00:39 deep: 11 lines transcribed (lang=jw)                      # mis-detected JAVANESE → garbage
21:03:01 decide-by-ear[title]: best xoverline.json (56) vs loaded (56)   # finally re-confirmed the cache
```
**Why it happened (the chain):**
1. **The cache existed but wasn't trusted.** `xoverline.json` title-matched at only
   **65** — under the confident-lock bar — so the song counted as "not confidently
   known" and the app went looking (sound/fetch/generate) instead of just playing it.
   (Score 65 because "XOVERLINE"/"ReGLOSS" doesn't score clean against the cached
   entry's stored title/artist — a generic-ish title + verbose-channel penalty.)
2. **Immediate audio-boundary false trigger.** 1 s after load it fired "re-identifying
   by sound," diverting off the provisional cache onto the sound path.
3. **ReGLOSS provider gap.** The sound-path lookup "came up empty" — the same blind
   spot as feelingradation: XOVERLINE's real LRC isn't found under "ReGLOSS" / the
   "hololive DEV_IS ReGLOSS" channel string.
4. **Grace window shorter than the lookup (~31 s).** With no confident lyrics in time,
   it fell back to **generating by ear**, so the `***` placeholder showed *before* the
   cache was eventually re-confirmed by decide-by-ear (56 vs 56).
5. **Deep fallback also failed:** yt-dlp source download = **403 Forbidden** (YouTube
   block), and the by-ear deep transcribe **mis-detected the language as Javanese
   (`jw`)**, yielding 11 garbage lines.
**Proposed fix:**
- **Play a cached exact-title hit immediately**, even at a modest score — if
  `xoverline.json` is cached and the title matches, show it and verify-and-switch in
  the background. Never *generate* a song that's already cached.
- **Suppress audio-boundary re-ID for the first ~2 s** after a fresh title-match load.
- **Fix the ReGLOSS provider query** (search "ReGLOSS" + bare title, drop the verbose
  "hololive DEV_IS" prefix — the feelingradation lesson) and/or seed common ReGLOSS songs.
- **Pin transcription language from `language_confidence`** (ReGLOSS = JA) so deep
  transcribe can't land on Javanese.
- **Hold the grace window open** while a cache or provider hit is still in flight, so
  generation doesn't start prematurely. Relates to the cover-ID reasons list (#1, #3).

## TICKET-074 — Force Sync: try ranked match candidates, skip chorus traps 🟢
**Ask:** Force Sync wasn't working well — make it try several methods; if the
highest-probability match fails to KEEP matching, try the second-highest, to avoid
"chorus traps" where the lyrics lock onto a repeated phrase then run past the spot.
**Cause:** Force Sync committed to a SINGLE best anchor (`capture_and_align` →
`_best_anchor`). A chorus hook recurs at several timestamps, so the one chosen was
often the wrong occurrence; the old "two reads agree within 1s" check could even
*confirm* a wrong occurrence while the chorus repeated, or ping-pong forever.
**Fix (multi-hypothesis + forward verify):**
- `align._rank_anchors` (new) ranks EVERY (segment, line) pair, not just the best;
  `align.rank_offsets` (new) turns the top matches into a deduped, ranked list of
  candidate OFFSETs — a recurring chorus yields one per occurrence (verified by a
  unit test: hook at 45/120/200 s → offsets +0/−80/−155, incl. the true one).
- `_force_sync_apply` is now a small state machine: try the best candidate, then
  FORWARD-VERIFY it against each fresh read (does the offset still predict what's
  sung *now*?). A candidate that keeps matching across reads spanning ≥
  `force_sync_span_s` (16 s, so it can't lock inside one chorus pass) and ≥
  `force_sync_streak` (3) confirms → **locks**. One that stops lining up is
  **blacklisted** and the next-best candidate is tried (freshest read = the song's
  current spot). A single noisy read gets a 1-read grace before blacklisting.
- New tune knobs: `force_sync_span_s`, `force_sync_top_n`; `force_sync_streak`/
  `force_sync_agree_s` repurposed for the confirm machine. Tray label + `/forcesync`
  help updated. Background auto-sync (`capture_and_align`) is unchanged.
**Verified:** built v1.0.70, deployed, `/forcesync` engages with the new logic
(log: "try ranked matches until one holds 3× over 16s"); track-change cancels it;
unit test confirms multi-candidate generation incl. the true offset.

## TICKET-073 — Add waveform analysis to the sync + matching algorithms 🟢
**Ask:** use waveform analysis (not just the transcript) in syncing and song matching.
**Already there:** vocal-band energy (FFT) + **spectral flatness** + vocal-onset detection
+ baseline (`songchange.py`) already power the energy-correlation sync and the
song-change/applause detection.
**Added (fusion):** (1) **waveform-gated listening** — the periodic Whisper listens
(live-resync, the tier) only transcribe when the vocal-band energy says singing is
happening NOW (`_vocals_active_now`), so a clip is never an instrumental break (cleaner
transcript → better sync AND by-ear match). (2) **waveform-pinned offset** — after
`_decide_by_ear` identifies the song by its lyrics (the *what*), the energy correlation
pins the precise OFFSET (the *when*). **Scope note:** local audio FINGERPRINTING for
matching is NOT added — it needs reference audio we don't store, and a cover/MMD differs
from the original anyway; Shazam covers online fingerprinting, the lyric-match stays primary.

## TICKET-072 — Live/concert versions: resync by ear ~5×/min 🟢
**Ask:** a 【LIVE MV】 / ONE-MAN LIVE cut should expect resyncs + odd pauses; have live
versions lyric-match ~5×/min.
**Cause:** live cuts DO register (`is_live_arrangement`) but only polled Shazam, which
can't fingerprint the (usually MMD) performance — so tempo shifts + applause pauses
drifted the timing with no recovery.
**Fix:** `_live_resync_loop` — for a registered live arrangement/concert with lyrics
loaded, transcribe + match to the loaded lyrics ~5×/min (`live_resync_s=12s`), FOLLOWING
the measured live offset. Waveform-gated (only when vocals are active) so it doesn't
waste a transcription on an instrumental/applause gap.

## TICKET-071 — Smart song decision by ear (Whisper 'small' + rapidfuzz) 🟢
**Ask:** a small (~250 MB) model that makes smart decisions about WHICH song's lyrics to
show — the title/Shazam signals keep failing on MMD/cover/performance videos and
mislabeled provider LRCs.
**Researched:** Whisper does SOTA zero-shot lyric transcription with no fine-tuning
(LyricWhiz, ISMIR'23); rapidfuzz partial/token-ratio is the recommended transcript matcher.
Neural audio fingerprinting needs a reference DB of the exact tracks (useless for MMD
covers); CLAP embeddings are ~600 MB and match audio→text descriptions, not exact songs.
So: **faster-whisper 'small' (~250 MB int8, already bundled) + rapidfuzz**.
**Fix:** `align.transcribe_vocals` (small model, ~12 s) + `align.score_candidates`
(rapidfuzz `partial_ratio` char-level → works for Japanese, + `token_set_ratio`).
`_decide_by_ear` runs ~20 s into a track (and via `POST /decide`) in TWO stages: score the
loaded cache + title-similar caches first; **if the loaded song matches the singing below
`decide_wrong_floor`, identify against the WHOLE cached library** ("trained on everything we
have" — score the one transcript against every cached song, the right one self-matches ~100
vs ~30, must clear the higher `decide_library_min`). Switches to a clear winner, else
re-fetches by title (cover-qualified). Verified: 快晴 chunk → #1 of **833 songs** at 100 vs
~30; feelingradation → 100 vs 13-28. Skips baked/caption/live songs. `/diag.decision`.

## TICKET-070 — ReGLOSS songs always wrong (feelingradation, サクラミラージュ) → baked in 🟢
**Symptom:** "feelingradation" and "サクラミラージュ Performance Video" (hololive DEV_IS
ReGLOSS) were always wrong — feelingradation fell back to a poor Whisper transcription;
サクラミラージュ loaded a totally unrelated song ("Daybreak Frontline", then "Mumei").
**Cause (two layers):** (1) the app searches under the verbose channel "hololive DEV_IS
ReGLOSS", which every provider misses (the real LRCs are under "ReGLOSS"). (2) These
MMD/"Performance Video" cuts can't be Shazam-fingerprinted, so Shazam keeps mis-ID'ing
them as random other tracks and SOUND OVERRODE the title, loading the wrong lyrics.
**Fix:** (1) a `bundled_lyrics/` dir SHIPS with the app (PyInstaller datas); at startup
`_seed_bundled_lyrics()` copies it into the runtime cache over a weaker cache. Both songs
are baked in (`source: bundled`, full furigana/romaji/translation). (2) a baked cache is
now **authoritative**: the heard-handling ignores a contradicting Shazam read for a
`source: bundled` song (no switch, no strikes) so a mis-ID can't override ground truth.
Any always-failing song can be added the same way (generate via the providers' working
search term, drop the JSON in `bundled_lyrics/`).

## TICKET-069 — "Cinematic intro" shown while the song is already singing 🟢
**Symptom:** the "🎬 Cinematic intro — waiting for vocals…" card stuck on screen for a
cover (RIDE ON TIME) that clearly had vocals from early on.
**Cause:** `_vocals_active_now` was too strict (vocal-band ≥ baseline×1.5 across 60% of
the window) and missed real singing on backing-heavy mixes, and the backstop was 75 s —
long enough to hold most of a song. **Fix:** loosened the detector (×1.3 / 50% / min 2)
so present vocals release the hold, and dropped `mv_intro_timeout` 75 → 20 s so a false
hold can never sit through more than 20 s of vocals.

## TICKET-068 — Wrong song never recovers when title-locked (Deep Dive→Dunk) 🟢
**Symptom:** the overlay shows the WRONG song's lyrics and no amount of re-syncing fixes
it — e.g. playing "Dunk" lyrics over the "Deep Dive" video.
**Cause:** when `_title_locked`, the app IGNORED Shazam hearing a different song forever
(to resist same-artist mis-IDs), so a genuinely wrong title-lock never self-corrected.
**Fix (user's rule):** count strikes — hearing the SAME other song N× (`wrong_song_strikes`,
default **5**) means the loaded song is wrong, so BREAK the title-lock and switch to what
we hear (load cache or fetch). Strikes reset when the heard song matches the loaded one or
on track change; a different heard song restarts the count (spurious single mis-IDs can't trip it).

## TICKET-067 — Out-of-context single-line translation + Suisei mis-language 🟢
**Ask:** translate each line WITH its ±2 neighbours for context, not in isolation; and stop
getting Suisei songs in the wrong language.
**Fix:** translation already used ±2-line windows, but when the translator merged/split
lines the bare newline-join misaligned and dropped the WHOLE window to context-free
per-line translation. `_translate_window` now uses a **numbered protocol** ("1. …\n2. …")
that survives merges/splits/reorders, so context is preserved; a missed line retries in a
small numbered ±2 window before any isolated fallback. Confidence: `_ALWAYS_JA` (Suisei,
Hoshimachi, Hoshimatic) + known romanized JP acts now score **full Japanese (certainty
1.0)**, not a weak partial — a romanized JP channel still beats an English same-title collision.

## TICKET-066 — Stutter on Shazam-unconfirmable songs (MMD/cover/performance) 🟢
**Symptom:** on songs Shazam can't fingerprint (an MMD "Performance Video", a cover, a
live arrangement) the overlay stutters — `/diag.fps.worst_ms` spikes to 150-475 ms,
render drops to ~8 fps in bursts — and it never settles. Lyrics ARE loaded and in sync
(`drift ≈ 0`); the jank is purely the render hitching.
**Cause:** the recal loop treats the track as `unconfirmed` forever (Shazam never
matches the arrangement) and so polls `recognize_playing` every ~4 s indefinitely. Each
recognize stalls the Tk render thread via **GIL contention** (the fingerprint compute) —
caught live: `worst_ms` spikes track `identifying=True`, not the new Whisper tier.
**Fix:** an **anti-stutter back-off** in `_recalibrate_loop`. (1) A settled-but-
unconfirmable track (lyrics loaded, `|offset| ≤ 1 s`, ~45 s elapsed, no sound lock,
not `live_mode`) backs the Shazam poll off to `unconfirmed_backoff_s` (30 s) instead of
4 s. (2) A CONFIRMED + boundary-watched track relaxes its Shazam re-lock to
`confirmed_recal_s` (45 s, was 25 s) — drift is now re-locked by the adaptive sync tier
and song changes by the boundary detector, so the frequent recognize was redundant.
Measured: `identifying=True` went from near-constant (stall every ~4 s) to ~20% of
samples. Concerts (`live_mode`) keep polling. Remaining: the occasional ~25-45 s spike is
shazamio's LOCAL fingerprint holding the GIL during `recognize` — the deeper fix is to
run `recognize_playing` in a separate PROCESS (no shared GIL), deferred (risky in a
frozen PyInstaller build). This is the same escalation/de-escalation the user asked for,
applied to the Shazam loop.

## TICKET-065 — Adaptive escalation/de-escalation sync-verification tier 🟢
**Ask:** sample sound-matching more often while syncing — verify with TPVR ≥3×/min;
once a check succeeds drop to 1×/min; any failure resyncs and snaps back to 3×/min,
staying fast while failures continue.
**Fix:** `_periodic_auto_align` is now an **adaptive heartbeat**. `_sync_tier_interval`
starts at **20 s (3×/min)**; each check yields a verdict via `_note_sync_verdict`:
`insync` steps the cadence down (40 s → 60 s after two good checks), `corrected` snaps
back to 20 s and stays fast while misses continue, `inconclusive` holds. The cheap
energy correlation gives the verdict when it reads a clear peak; when it's **blind on a
song** (flat/ambiguous — the off-vocal ReGLOSS 'サクラミラージュ' case, `_energy_blind`)
the tier escalates to a short **Whisper listen** (`_tier_listen_now`, 6 s capture),
**two-point verified** for any large jump before it can move sync. A song we genuinely
**can't read** (Whisper keeps returning `inconclusive`) is NOT hammered at 3×/min — two
blind checks **back the cadence off** one notch toward 1×/min, so a futile check can't
cost a stutter (only a detected MISS keeps it fast). Whisper CPU is capped
(`cpu_threads=4` in `align.py`) so the transcribe can't stutter the overlay. Knobs:
`sync_tier_fast_s` / `_mid_s` / `_slow_s` / `_ok_drift` / `_listen_s`. Telemetry in
`/diag.sync`: `tier_interval_s`, `tier_good_streak`, `tier_miss_streak`,
`tier_energy_blind`, `tier_listening`.

## TICKET-064 — Cover videos (【Cover MV】) searched by the cover channel 🟢
**Symptom:** "【Cover MV】MAFIA / マフィア - Ouro Kronii" loaded the wrong/no lyrics and
took ages — the cover went undetected, so the search used the COVER CHANNEL ("Ouro
Kronii Ch. hololive-EN"), which has no lyrics listed for that song.
**Cause:** `_COVER_RE` matched `(cover)` / `[cover]` / `/cover` but **not** the
lenticular/fullwidth bracket tags VTuber covers use — `【Cover MV】`, `（Cover MV）`,
`［Cover］`. Also `extract_cover_original` was handed the *cleaned* artist
("hololive-EN"), so " - Ouro Kronii" looked like the ORIGINAL artist.
**Fix:** `_COVER_RE` now catches a `cover` tag after any common opening bracket;
`extract_cover_original` strips the bracketed tag, is passed the **raw** channel, and
keeps the song when the tail is the cover channel. On a cover with no parseable original
artist, `_on_track_change` **drops the channel** (`fetch_artist=""`) and searches by
title alone (the original's lyrics fit the cover) — never re-introducing the channel.
This also fixes the "taking too long to detect" complaint for covers (title-first
resolves instantly instead of failing through to sound).

## TICKET-063 — Long concert videos: weak song detection between songs 🟡
**Ask:** in a 1h+ concert, combine OCR (banner) + audio (Shazam) + the applause
detector + a 2-6 min duration heuristic to detect & switch songs and sync smoothly.
**Done:** in `live_mode`, an **applause gap is treated as a song BOUNDARY** —
`_check_applause_gap` re-identifies (Shazam + a forced OCR banner read) the NEXT song
instead of resyncing the old one. Plus a **2-6 min heuristic**: a concert song still
showing after ~6.5 min forces a re-identify (caught a missed transition).
**Open:** transcription-based song-ID (transcribe the singing → match against the whole
lyric LIBRARY to pick the song) as the last fallback when OCR + Shazam both miss.

## TICKET-062 — Language-confidence score (artist's usual language) 🟢
**Ask:** weight the artist's usual language so a Japanese act's English-titled song
isn't matched to an English same-title collision (Suisei's "GHOST" → English "Ghost";
ReGLOSS "feelingradation" must read Japanese). As a percentage with other factors.
**Fix:** `confidence.language_confidence(title, artist)` → {ja,en,zh,ko,certainty}.
Strong cue = the artist NAME script (kana→JA, hangul→KO) + a `_KNOWN_JA` reference for
romanized acts (hololive/ReGLOSS/V.W.P/Reol…). When certainty is high and CJK clearly
beats EN, `fetch_lrc.take()` rejects an English body as a collision; `_file_valid`
self-heals a cached English body for a kana-named artist. Neutral (certainty 0) for
plain romanized names so DEADPOOL/Suisei is unaffected. Measured: GHOST/星街すいせい
75% JA, feelingradation/ReGLOSS 71% JA, DEADPOOL/Suisei-Hoshimachi 0 certainty.

## TICKET-061 — Concert applause/cheering pause drifts the lyrics 🟢
**Symptom:** in a LIVE/concert cut the song pauses for applause & cheering; the player
clock keeps running, so the lyrics scroll ahead and stay desynced after the music
resumes.
**Fix:** `_check_applause_gap` (polled ~3×/s) watches the live audio in a live cut for
a sustained **loud-but-non-vocal** stretch (broadband cheering — high spectral
flatness, no tonal singing). When singing returns it fires a **Whisper
transcribe-and-match resync gated by TWO-POINT verification**: align by ear, HOLD the
offset, confirm with a 2nd listen ~2.5 s later, apply only if the two agree. Tunable
via `/tune applause_min_s`.

## TICKET-060 — Kanji song matched Korean lyrics (花譜 邂逅 → "Chance meeting") 🟢
**Fix:** kanji (Han) is JA/ZH, never modern Korean (hangul) — so a kanji title/artist
rejects a Korean lyric body at fetch AND on cache load (self-healing). Suppressed when
the title/artist itself carries hangul.

## TICKET-059 — Auto-captions = wrong/excess lyrics + [音楽] tags 🟢
**Symptom:** lyrics "close but wrong" (鉄後/ラッキラ mis-hearings), full of `[音楽]` /
`[ongaku]` tags, and an "excess" wall of duplicated text — on ReGLOSS MVs.
**Root cause:** `fetch_captions_only` had `writeautomaticsub: True`, so it used
YouTube's AUTO (ASR) caption track. ASR is inaccurate, inserts sound-event tags, and
ROLLS (each word repeats across overlapping cues) → duplicated lines. v1.0.25
(commit 616a70c) used MANUAL captions only.
**Fix:** back to **manual captions only** (`writeautomaticsub: False`); a song with no
manual track falls through to the provider LRC (cleaner) instead of bad ASR. Also
strip `[...]`/`【...】`/♪ sound tags in `_parse_vtt`. Purged the 48 cached
auto-caption songs so they re-fetch correctly.

## TICKET-058 — Karaoke fill (yellow highlight) jumped on sync correction 🟢
**Symptom:** the sung-word highlight was "a bit off" and SKIPPED to a new place when
sound-sync corrected the offset — poor UX.
**Fix:** render against an **eased display offset** (`_eased_offset`) that glides
toward the sound-sync target (Shazam offset + energy correlation) at ~20%/frame
(capped 0.10 s/frame) instead of snapping, so the highlight + scroll slide smoothly
into a correction. A major re-sync (>5 s, e.g. a song change) still snaps. The match
target is unchanged (still heard-audio driven) — only its APPLICATION is now smooth.

## TICKET-057 — MV intro hold never released → lyrics never started 🟢
**Symptom:** a music video (V.W.P 電脳) sat on "Instrumental intro — waiting for
vocals…" for the WHOLE song; lyrics never appeared even after singing clearly began.
**Root cause:** the MV intro hold only released on (a) the one-shot
`_fire_vocal_event` (a detector-thread band-energy-rise event that can simply never
fire), (b) Shazam setting `_sound_song` (fails when the song isn't fingerprinted),
or (c) a **100 s** hard timeout. With (a) and (b) both silent, the user faced a
~100 s stall — effectively "never started".
**Fix (this build):**
  - **Vocal-energy POLL** (`_vocals_active_now`) inside the hold: release the
    instant the live vocal-band ratio stays clearly above the learned instrumental
    baseline for ~1.2 s. Reuses the always-on vocal buffer the sync correlator keeps,
    so it doesn't depend on the flaky one-shot event. Calls `_on_vocal_onset` to
    calibrate the offset, then anchors.
  - Timeout backstop made tunable (`mv_intro_timeout`, default 75 s).
**Also (per request):** the MV hold card now reads **"🎬 Cinematic intro — waiting
for vocals…"** (a music video's lead-in is cinematic dead-space, often dialogue) —
distinct from a plain audio "Instrumental intro".

## TICKET-055 — Wrong song: Ludacris "The Potion" shown for Michiru Shisui's "Potion" 🟢
**Symptom:** Spotify playing **Potion — Michiru Shisui** (a Japanese Phase-Connect
VTuber's debut original). Overlay showed **English gangsta-rap** lines — "What up aye
shawty what it is", "Lil' buddy what you want? Some violent shit", "Tell yo' momma
I'm a ghet-to su-per-star". Those are **Ludacris — "The Potion"**, a different song.
**Diagnosis (via API):** `/lyricstate` meta = `source: "syncedlyrics/cover"`,
`lang: "es"` (Spanish tag on English text = garbage). `/diag` energy_align
`ambiguous, lift 0.02, rival 0.673 > best 0.592`; `offset_history` thrashing
0→-15→-27→0 — the words never line up with the audio.
**Root cause:** two compounding bugs.
  1. The two "Potion"s are **both 3:43** (223 s). That rare duration coincidence
     beat **every** duration gate (`verify_lrc`, `_strict_ok`, `validate_file`).
  2. The cache file `potion.json` was written by the **weak title-only "cover"
     fallback** in an older build. Today's code fetches this clean Spotify track
     with `cover=False, strict=True`, which already SKIPS both weak paths — but the
     app **trusted the stale cache forever** without re-checking its provenance.
The kana/hangul language guards don't fire (Latin title "Potion", romaji artist).
**Fix (this build):**
  - **Provenance guard** (`main.py:_file_valid`): a cache whose `meta.source` is
    `syncedlyrics/cover` or `syncedlyrics/title` is REJECTED for a clean,
    non-cover source → re-fetch under strict rules (returns the right song or
    nothing, never the wrong one). Duration-independent.
  - **Artist cross-check** (`fetch_lyrics._lrc_artist_conflict`): parse the LRC's
    own `[ar:]` tag; reject a cover-fallback hit whose tagged artist is a different
    script or shares no token with the requested artist (`[ar:Ludacris]` ≠ Michiru
    Shisui). Conservative — a missing/near-match tag never rejects.
  - Purged the stale `potion.json`.
**Defense principle:** when we have an AUTHORITATIVE artist (clean source, not a
cover), never trust a bare-title provider match — no matter how well the duration
matches. Sound/energy can't save a rap song (continuous vocals → flat energy mask).

## TICKET-056 — TPVR commits the wrong chorus on "All The Things She Said" 🟡
**Symptom:** two-point sound-verification "constantly gets the wrong chorus then
runs with it." A repeated chorus is acoustically identical each time, so read 1 and
the confirming read 2 can both match a chorus instance and report the SAME offset,
falsely "agreeing" → a wrong offset commits and sticks.
**Change (this build):** the hesitation-before-confirm and the confirming-listen
length are now **/tune knobs** (`sync_confirm_hold_ms` default 2600,
`sync_confirm_listen_s` default 5.0) so the timing can be dialled in live without a
rebuild. A LONGER hold separates the two reads by more song time, so two different
instances of one chorus are less likely to both read at the same offset.
**Open:** live-test a few songs (esp. "All The Things She Said") to find the
hold/listen pair that hits **≥80 %** correct-commit. Candidate further work: require
the two reads' implied song-position to be self-consistent with the player-clock
advance (a true lock advances by exactly Δt; a re-matched chorus does not).

## TICKET-001 — Dance/play covers generate instead of fetching real lyrics 🟢
**Symptom:** "Breaking Dimensions を踊ってみた", hololive covers, sit on "Generating…"
and never load real lyrics (slow, spotty, wrong language).
**Root cause:** `is_cover_title`/`clean_title` only knew 歌ってみた/(cover); they missed
踊ってみた (dance), 演奏してみた, 弾いてみた, 叩いてみた → no title-first fetch → fell to
generation.
**Fix (pushed 87a6de9):** added those markers to `_COVER_RE` + the `clean_title` strip
(handles the を particle). `fetch_lrc('Breaking Dimensions', cover=True)` → 70 lines.

## TICKET-002 — Same-title collision: wrong song's lyrics loaded for a common title 🟡
**Symptom (cover, FIXED):** 【歌ってみた】地球儀 covered by 花譜 — the video plays Kenshi
Yonezu's 地球儀 (僕が生まれた日の空は…, the *Boy and the Heron* theme) but the overlay
showed an **unrelated** 地球儀 (愛に飢えている / こんな夜に流されあっている / 揺られちまえよ).
Proved: those overlay lines are lines 6-8 of the *wrong* 地球儀, so it was a wrong-title
**fetch**, not generation.
**Root cause (cover):** the COVER fast-path queried by TITLE only, skipped the
`_strict_ok` guard, and ran **before** the artist-keyed queries — so for a super-common
title it short-circuited onto the first same-title hit. Yet `fetch_lrc('地球儀','花譜')`
artist-keyed *already* resolves Yonezu's 地球儀 (the song actually covered); the cover
path was overriding a correct result with a wrong one.
**Fix (pushed 0eeb696):** reordered `fetch_lrc` so the **artist-keyed queries run first**
and the title-only cover path is the **FALLBACK** — only for true 歌ってみた uploads where
the channel-as-artist genuinely derails search (TICKET-001). Verified: 地球儀/花譜 cover
now returns 僕が生まれた日の空は; Breaking Dimensions dance cover still fetches (no regression).
**Research:** lyric finders stress **verifying the artist + not trusting the first
match** — "compare the artist… before you save it, the first match is not always the
right version." ([Musely](https://musely.ai/tools/lyrics-finder), [Chosic](https://www.chosic.com/find-song-by-lyrics/))
**Still open (non-cover + no-artist edge):** the original "BANCHO / 轟はじめ" report was a
*non-cover* same-title collision (relies on `_strict_ok` in the title-only last resort);
and a bare-title cover with **no artist at all** can't disambiguate (returns the first
同名 song). Both want the **duration cross-check** against a trusted duration (player
duration arrives a few s late, or the master-tracks library DB, TICKET-009). → kept 🟡.

## TICKET-003 — Desync: correct lyrics, wrong timing 🔴
**Symptom:** "Deep Dive / 轟はじめ" matched the right lyrics but the displayed line was
far off the video's burned-in line. Likely behind several "wrong song" reports.
**Research:** the field uses **forced alignment** — separate vocals, recognize the
singing, align phonetic units (Viterbi/HMM). ([AutoLyrixAlign/MIREX](https://music-ir.org/mirex/wiki/2024:Lyrics-to-Audio_Alignment),
[lyrics-sync](https://github.com/mikezzb/lyrics-sync), [real-time chroma+phonetic](https://laurenceyoon.github.io/real-time-lyrics-alignment/))
**Plan:** the app's "Sync by listening" is forced-alignment-lite (Whisper transcribe →
fuzzy-match heard words to lyric lines → offset). Run it **automatically** right after a
fetch on MV/cover titles (where catalog offset ≠ video), and re-anchor on a confident
hit. Vocal separation (Demucs) is the heavier upgrade.

## TICKET-004 — Identification too slow ("identifying…" for a long time) 🔴
**Symptom:** long delay before lyrics appear, especially covers.
**Research:** faster-whisper chunk tuning; Shazam capture length. ([faster-whisper](https://github.com/SYSTRAN/faster-whisper))
**Plan:** TICKET-001 makes covers fetch by title immediately. Also: try the
master-tracks DB **locally first** (instant, no network), and shorten the first Shazam
capture. Overlaps TICKET-010.

## TICKET-005 — Spotty / intermittent generation ("pieces then blank") 🔴
**Symptom:** generated lyrics appear, go blank, reappear — big gaps.
**Research:** for streaming, **`condition_on_previous_text=False`** is recommended (True
"causes the model to condition on potentially incorrect previous hypotheses"). Smaller
chunks + overlap reduce latency/gaps; **RMS-VAD** segmentation cuts hallucinations
without dropping vocals. ([saytowords](https://www.saytowords.com/blogs/Real-Time-Streaming-with-Whisper/),
[arXiv ALT](https://arxiv.org/html/2506.15514v1))
**Plan:** test `condition_on_previous_text=False` for generation; add RMS-VAD; overlap
chunks so boundaries don't drop words. (Most of these songs should MATCH after
TICKET-001, avoiding generation entirely.)

## TICKET-006 — Box / "tofu" characters in the overlay 🔵
**Symptom:** some lines show □ boxes.
**Finding:** a scan of cached lyrics found **no** corrupt chars in the DATA → it's a
**font glyph-coverage** issue in the Tk renderer (missing glyphs → .notdef tofu).
**Research:** "tofu = font lacks a glyph and no fallback." Fix = a font with full CJK +
symbol coverage and a fallback chain. ([SimpleLocalize](https://simplelocalize.io/blog/posts/tofu-symbol/),
[SymbolFYI](https://symbolfyi.com/guides/tofu-missing-glyphs/))
**Plan:** set the overlay font to a verified full-coverage CJK face (Meiryo / Yu Gothic
UI / MS Gothic) and add a per-glyph fallback. Need the exact line that shows boxes to
confirm which glyphs are missing (symbols? half-width katakana? rare kanji?).

## TICKET-007 — Sync precision (general) 🔴
**Symptom:** request for "greater precision in lyric syncing."
**Research:** word-level "enhanced LRC" exists but free providers return line-level only;
real-time alignment uses chroma + phonetic features. ([EasyLRC enhanced LRC](https://easylrc.com/blog/enhanced-lrc-word-level-timing-guide-2026),
[real-time alignment](https://laurenceyoon.github.io/real-time-lyrics-alignment/))
**Plan:** tighten the Shazam offset recal cadence on unstable songs; interpolate
word-fill across each line (already partial); overlaps TICKET-003.

## TICKET-008 — Multi-monitor: move / scroll-across / mirror 🟡 (feature)
**Request:** move the overlay to a chosen display; **scroll lyrics continuously ACROSS
all** displays; **mirror** the same lyrics on every display.
**Done (built):** `_monitors()` via Win32 `EnumDisplayMonitors` (no new dep — `screeninfo`
would need PyInstaller bundling); tray **"Display"** submenu (each screen + "Scroll across
ALL screens"); `set_display('primary' | 'mon:N' | 'span')` repositions the
W-parameterized band (span = one band over the whole virtual desktop, so lyrics scroll
through every screen). The `primary` default is **unchanged** (verified safe on 1 display).
**Remaining:** **MIRROR** (same lyrics on every screen at once) needs one Toplevel+canvas
per monitor sharing the render — a render-target refactor. The menu is built once at
startup, so hot-plugging a display needs a restart to refresh.
**Validation:** all multi-screen modes need a **2nd display connected** to test (only 1
attached now: 1920×1080). Research: [wikiPython](https://www.wikipython.com/tkinter-ttk-tix/gui-demos/a-tkinter-multi-screen-strategy-demo/),
[PySimpleGUI](https://docs.pysimplegui.com/en/latest/cookbook/original/multi_monitor/).
**Status:** 🟡 move + scroll-across shipped; mirror + live validation pending a 2nd screen.

## TICKET-009 — Use the master-tracks library DB for matching/verification 🔴 (feature)
**Idea:** `Music-Migrator/data/master_tracks.json` (ISRC → track/artist/album/
duration_ms) is the user's real library. Fuzzy-match messy YouTube titles → clean
(artist, title, duration) for accurate fetch + same-title disambiguation (TICKET-002).
**Research:** duration + artist verification is the standard guard. ([Musely](https://musely.ai/tools/lyrics-finder))
**Plan:** load the DB once; normalized-title index; on track-change, fuzzy-match → if a
confident hit, use its (artist, title, duration) to fetch + verify. (CSV obtained.)

## TICKET-010 — Generate-vs-fetch race (covers still flash "Generating…") 🔴
**Plan:** the generate-defer (commit 71fdc2c) waits while a lookup is in flight; extend
so a cover/title fetch (TICKET-001) always pre-empts the 11s generate deadline, so
findable covers never flash AI text.

## TICKET-011 — Performance (render FPS / GPU / CPU) 🟡
**Status:** GPU now used for Whisper (cuBLAS/cuDNN fix, commit 142512a); render is
throttled (PIL repaint budget). FPS shows N/A in the browser HUD (that's the page, not
us). **Plan:** profile the Tk frame time (`/status` render_fps) on heavy songs.

## TICKET-012 — Generated-lyric language detection 🟢
**Was:** generation hard-forced Japanese → English covers became gibberish ("あかんぽう").
**Fix (pushed):** auto-detect the language **per chunk** (no first-chunk pin), flowing
into transcription / annotate / translate / saved meta.lang.

## TICKET-013 — Single-instance + "only the latest running" 🟢
**Request:** make sure only the latest version is installed/running; prevent >1 instance.
**Found:** repeated dev restarts had left **two** instance-pairs running (the venv
`pythonw` stub re-execs the real interpreter, so each instance = 2 processes; only the
newest owned port 8765). Desktop + Startup shortcuts both target the SOURCE
`pythonw main.py` (= latest). A stale `dist\DesktopKaraoke.exe` build exists but is
unlinked + not running.
**Fix (pushed):** `_is_only_instance()` — a process-lifetime named mutex
(`Local\\DesktopKaraoke.SingleInstance`); `main()` exits if it's already held. It runs
only in the real app (the venv stub doesn't), so it never blocks its own stub→child
pair. Killed the duplicates → one clean latest instance. **Tested:** a 2nd launch
self-exits, port owner unchanged.
**Note:** the stale dist exe is old code; rebuild it if you want the *packaged* version
current — the active path (shortcuts + running instance) is already the latest source.

## TICKET-014 — Common songs generate instead of fetching (title not cleaned) 🟢
**Symptom:** "ReGLOSS 'サクラミラージュ' Performance Video" generated wrong JP; "Clione feat.
轟はじめ (Live at PQ)" generated "me me me ***" — both HAVE real lyrics (サクラミラージュ 65
lines, Clione 27). Generation is meant to be a LAST RESORT.
**Root cause:** `clean_title` left the messy title so the fetch missed → fell to
generation: (a) the song wasn't extracted from `'…'`/`"…"` quotes (only 「」/『』);
(b) "Performance Video" not stripped; (c) "feat. X" not stripped; (d) a SINGLE live song
("X (Live at Y)") matched the bare word "live" in `_LIVE_RE` → forced sound-only mode (no
title fetch) → generated.
**Fix (pushed):** `clean_title` now also extracts a song from straight/smart quotes,
strips "Performance Video"/"Visualizer" and "feat./ft. X"; `_LIVE_RE` no longer trips on
bare "live" (keeps concert/tour/festival/medley/3D-live + the >12 min duration guard).
Verified: both titles clean to the song and fetch real lyrics; concerts ("Live Tour")
stay sound-driven; apostrophes ("Don't Stop Me Now") + covers unaffected. Bad generated
caches deleted.

## TICKET-015 — Auto re-sync by sound over-corrects a baseline that was already good 🟢
**Symptom:** "sometimes auto sync makes it more out of sync, so I reset to -0.0 and it's
good again." The periodic Shazam recal drifted a correctly-synced song OUT of sync.
**Root cause:** the recal eased the offset by `0.8*diff` on every reading where
`diff > 0.15s`. But Shazam's per-read timing is noisy (±~1s, worse on niche tracks) and
digital playback has **no clock drift** (the player's position is exact), so it kept
chasing that noise away from a good baseline.
**Research:** sync is a **constant offset, not drift**, for digital playback; the fix is
a **filtered/dead-banded** estimate, not per-read chasing. ([AudioEdit constant-vs-drift](https://audioedit.io/blog/how-to-fix-audio-out-of-sync),
[Acrovid drift correction](https://www.acrovid.com/audio_video_sync_drift_correction.htm))
**Fix (pushed fc6cabe):** move the offset ONLY when a correction is (a) outside a **0.8s
dead-band** (inside it the player clock is the better authority — leave it), AND (b)
**confirmed by a 2nd reading** agreeing within 2.5s. A real seek / long intro re-confirms
in seconds; random noise never does. Removed the 0.15s/0.8-gain easing. Verified in sim:
noise around 0 stays 0, single spikes ignored, real +30/+5 offsets still apply,
disagreeing spikes rejected. (On-demand "Sync by listening" already gates on a match
ratio, so it was left as-is.)

## TICKET-016 — Read music-source context: trust Spotify / YT-Topic, strict same-title fetch 🟢
**Request:** read the source context (esp. Spotify) so the app knows the ONE song actually
playing and doesn't grab a wrong same-title song, a cover's wrong version, or a whole
concert. Example: "Lucky Star" by Kaneko Lumi (VOID, 3:41 — Spotify/YT-Topic).
**Root cause:** Kaneko Lumi's "Lucky Star" isn't on the lyric providers, so the
artist-unconfirmed **title-only last resort** grabbed a DIFFERENT same-title song
("Twinkle Twinkle Lucky Star") — even with the artist + duration passed (the provider hit
carried no duration, so the guard couldn't reject it).
**Research:** verify against an **authoritative source** (the player's own clean metadata)
and don't trust a title-only hit; duration/album disambiguates same-title songs
(TICKET-002/009). The app already reads SMTC (`GlobalSystemMediaTransportControls`), which
carries Spotify/Topic title+artist+album+duration.
**Fix (pushed 19ebafa):** a `_clean_source()` signal — a real audio app (Spotify) or a
YT-Music "- Topic" channel is AUTHORITATIVE. When clean, `fetch_lrc(strict=True)` skips
the artist-unconfirmed title-only last resort: a generic title that misses the
artist-keyed queries returns nothing → **generate by ear from the real audio** instead of
showing the wrong song. Also: Topic-channel **duration is now trusted** (audio-only upload
= track length) and SMTC **album_title** is read, both for same-title disambiguation.
Covers (歌ってみた) + messy YouTube uploads are unaffected (not a clean source → loose path).
Verified: Lucky Star strict→None (no Twinkle Twinkle); 世惑い子/Lemon/Driver's License still
fetch real lyrics; 地球儀 cover still correct; source-classification unit-tested.

## TICKET-017 — Generated lyrics incomplete: add a deep OFFLINE transcription pass 🟢 (feature)
**Symptom:** for songs we MUST generate (no synced lyrics anywhere — e.g. Lucky Star /
Kaneko Lumi, Clione live), the realtime by-ear generation is **incomplete + rough**: it
transcribes short loopback chunks with a *small* model while racing the playhead.
**Request:** keep the realtime pass as instant best-effort, but ALSO download the source
audio and do a **proper full-file transcription**, cache that, and delete the audio.
**Fix (pushed):** new [`deep_transcribe.py`](deep_transcribe.py) — Tier 2:
(1) `yt-dlp` searches `ytsearch1:<title> <artist>` and downloads **audio-only** (`bestaudio`;
no ffmpeg — PyAV decodes the .webm/.m4a); (2) faster-whisper **`medium`** transcribes the
WHOLE file (`vad_filter=False` so sung vocals survive, `condition_on_previous_text=False`);
(3) lines are annotated + saved as `source: "generated-deep"`; (4) the audio is **deleted**
(`finally:`). Wired in `main.py`: `_begin_generation` also spawns `_begin_deep_generation`
→ `_apply_deep` (saves + upgrades the overlay live if still playing); `_deep_token` cancels
on track change; `_deep_tried` runs it **once per song**; an existing `generated-deep` cache
is never re-downloaded.
**Key findings:** YouTube now 403s audio downloads without a JS runtime — yt-dlp enables only
`deno` by default, so we opt in to **`node`** when on PATH (fixed the 403, 3.5 MB in ~3 s).
`large-v3` was exact-match accurate but spilled to CPU (~4 min) next to the running app, so
the default is **`medium`** (fits GPU, faster, near-identical on clear vocals). Verified on
Lucky Star: deep pass returned **48 complete lines** matching the video's burned-in lyrics
("And I'll be there for you, finding hope from a spark") vs the fragmentary best-effort.
Degrades gracefully (no yt-dlp / 403 / over-long match / <4 lines → best-effort stands).
Documented in [docs/GENERATION.md](docs/GENERATION.md).

## TICKET-018 — Overlay ate mouse clicks ("can't click anything in a game") 🟢
**Symptom:** with the overlay up, clicks didn't reach the game/app underneath — the
full-screen (fixed full-work-area) overlay was intercepting mouse input instead of
being click-through.
**Root cause:** click-through (`WS_EX_TRANSPARENT`) is applied **once at startup**.
It can be lost later (the overlay is a layered, `overrideredirect`, topmost window, and
various window operations re-touch the extended style) — and because the window covers
the **whole screen**, the moment that bit drops, the ENTIRE screen stops accepting clicks.
**Research/verify:** confirmed live — forcibly clearing `WS_EX_TRANSPARENT` on the running
overlay made it eat clicks; the new guard restored it automatically within ~0.5 s.
**Fix (pushed):** extracted `_click_through()` (NOACTIVATE|TOOLWINDOW|LAYERED|TRANSPARENT,
only writes when a bit is missing) and **re-assert it after every window-attribute change**
— init, `set_opacity`, `apply_preset` (the 45 %-opacity Gaming preset was a prime trigger),
`_place_window`, and `toggle()` (Show/Hide re-`deiconify`). Plus a **`_click_guard`** that
re-asserts every **500 ms** as a self-heal, so the overlay can *never* get stuck eating
clicks regardless of the trigger. Verified self-healing on the live window.

## TICKET-019 — "Song/Artist" MV titles generate instead of fetching (Dunk) 🟢
**Symptom:** "[Original] Dunk/Todoroki Hajime [Official MV]" (a *common* ReGLOSS song)
showed **generated** lyrics. Two stale caches existed: `dunk_轟はじめ.json` (good, 86 lines)
and `dunk_todoroki_hajime.json` (generated, 13 lines).
**Root cause:** `clean_title` stripped the brackets but left **`Dunk/Todoroki Hajime`**
(the slash-split only ran for *covers*). So the title-match exact-hit the *generated*
13-line file, and a live fetch of the messy title took **36.5 s** — far past the 11 s
generate deadline. `fetch_lrc("Dunk", "轟はじめ")` returns 87 real lines, and the good
`Dunk` cache was already there — it just wasn't being matched.
**Fix (pushed):** `clean_title` now treats an **Original/MV** upload like a cover for the
`Song/Artist` slash: "Dunk/Todoroki Hajime" → **"Dunk"** (so it instant-matches the good
cache). Guarded to a single slash with no `" - "` on either side, so bilingual
"Artist - JP / Artist - EN" uploads are left for `_title_variants`. Deleted the stale
generated cache. Verified: Dunk→"Dunk"; シンメトリー/アイドル/covers/bilingual all still correct.

## TICKET-020 — Sync-by-listening: reset to 0 when a big offset is low-confidence 🟢
**Request:** "return timing to 0 if significantly desynced after attempting sync — that
fixes it often." A big alignment offset on a song whose player clock is accurate is
usually a *mis-match* (the transcript matched the wrong repeated line).
**Fix (pushed):** `_apply_align` now snaps the offset back to **0** when the aligned
offset is large (>6 s) **and** the match ratio is low (<0.72) — the player position is
right far more often than a low-confidence big jump. High-confidence large offsets
(genuine long intros) still apply. Complements TICKET-015's dead-band on the Shazam recal.

## TICKET-027 — feelingradation showed SKAVLA again — my own clean_title fix broke the title-lock 🟢
**Symptom:** ReGLOSS "feelingradation" (bread-and-butter) showed SKAVLA's lyrics — the old
"feelingradation → SKAVLA" Shazam mis-ID, back again.
**Root cause (a regression I introduced):** the title-lock used an EXACT-string compare of
the player title vs the loaded cache's stored title. The TICKET-023 cleaning made the
player title `'ReGLOSS - feelingradation' → 'feelingradation'`, which no longer
string-equals a cache stored under the longer name (e.g. a stale generated
`regloss_feelingradation.json`) — so `_title_locked` went False and a Shazam mis-ID
(SKAVLA, a different ReGLOSS-adjacent song) overrode it.
**Fix (pushed, v1.0.17):** the lock now MATCHES by **containment** (player title == cache
title OR one contains the other) AND requires the title to be **distinctive**
(`confidence.title_distinctiveness >= 0.40`) — robust to cleaning, while common titles
(Awake/BANG) still defer to audio. So feelingradation (0.85) locks and SKAVLA can't take
over. Deleted the stale generated `regloss_feelingradation.json` (kept the real
`feelingradation.json`) and the old `dist/` build so the latest code runs.

## TICKET-035 — Use the video's OWN caption track (exact lyrics + perfect sync) 🟢
**Insight (from そして花になる):** even after TICKET-034 found the real provider lyrics, they were
~4.7s OUT OF SYNC — the lrclib LRC put line 1 at 0.7s but the video sings it at 5.4s. The official
MV ships a **manual Japanese caption track** (verified via yt-dlp: `subtitles: ['zh-TW','ja','ko']`)
which is the EXACT official lyrics WITH the video's own timing — strictly better than any provider
LRC on both counts, and it also confirmed "微かな人生の幸せを追う" was a Whisper mis-hearing (absent
from the official captions).
**Fix (v1.0.25):** the deep path already downloads the video with yt-dlp; it now also pulls the
**manual caption track** and uses it as the TOP-priority lyrics source (above provider lookup and
Whisper). New `_parse_vtt` / `_captions_from_dir` in `deep_transcribe.py` pick the ORIGINAL-language
track only (a 'ja' track is lyrics; a 'zh-TW'/'en' track is a translation, never shown as lyrics),
collapse rolling-caption dupes, and drop credit lines; saved as `source: youtube-captions`. So any
official MV with manual captions gets exact words AND zero-offset sync. (Fixed そして花になる /
kaf_27_and_become_a_flower in place with the caption track.)

## TICKET-034 — English player title → real lyrics generated by ear (花譜「そして花になる」) 🟢
**Symptom:** 花譜「そして花になる」showed by-ear AI lyrics (a mis-transcribed line "微かな人生の
幸せを追う" that isn't in the song) even though the real synced lyrics are cached AND on lrclib.
**Root cause:** YouTube's SMTC reported the **English** title "KAF #27 - And Become a Flower"
(KAF titles its videos in English). That missed the Japanese cache `そして花になる.json` and every
provider lookup, so the app fell back to deep-transcription — which saved a generated file under
the English slug `kaf_27_and_become_a_flower.json`.
**Fix (v1.0.24):** the deep-transcription path already downloads the video with yt-dlp, whose
metadata carries the REAL title (花譜「そして花になる」). It now **extracts the canonical title and
looks up real provider lyrics BEFORE transcribing by ear** — `そして花になる` → 66 real lines, so it
skips Whisper entirely and saves them as REAL (no AI "***" marker). `deep_transcribe()` returns
`(lines, lang, meta)`; `_apply_deep` saves with the real source. Bridges the whole class of
English/translated player titles for Japanese songs. (Also fixed the stale
`kaf_27_and_become_a_flower.json` in place.)

## TICKET-033 — Cover matched a WRONG-LANGUAGE same-title song (Beyond the Way → German) 🟡
**Symptom:** the cover 「Beyond the way」(音乃瀬奏＆Mori Calliope) was being generated, and the
title-only cover search was matching an unrelated **German** song also called "Beyond the Way".
**Root cause:** the cover fast-path does a TITLE-only lookup, and `verify_lrc`'s language gate
only fires for CJK *titles* — "Beyond the way" is Latin, so a German body passed. The real
Japanese cover lives on NetEase under the romanized artist "Kanade Otonose", which the app
can't derive (音乃瀬奏 romanizes to "oto no se sou", a literal kanji reading, not the name).
**Fix (v1.0.23):** `fetch_lrc` now gates on the ARTIST's script — a CJK-script artist's song is
CJK (or, for a cover, English), never German/Spanish/Russian/etc., so a European-language hit on
a Latin title is rejected as a same-title collision. This also stops the v1.0.22 romaji/generated
re-fetch from *replacing* a deep-transcription with the German words. English covers by JP artists
still pass (detect_lang→"other" is allowed).
**Status:** 🟡 the German collision is fixed, but this specific cover still falls back to
deep-transcription (its real synced lyrics are only findable via a romanized name we can't derive
from 音乃瀬奏). Tracked as a known limit; the transcription is at least the real audio.

## TICKET-032 — "Was fine then desynced": a spurious same-song track-change wiped the offset 🟢
**Symptom:** a song synced correctly, then suddenly jumped ~30s off (Shinigami Eyes, white
balance). Telemetry showed it: `CONFIRMED offset -29.89s → applied` (drift→0, synced), then a
few reads later `shown_off=+0.00` with `track change: 'Shinigami Eyes' / 'Grimes'` — the SAME
song re-fired as a track change.
**Root cause:** track changes fire on exact `(clean_artist, clean_title)` inequality, but YouTube's
SMTC re-reports the same song mid-play with slightly fluctuating metadata (a channel suffix
appears/disappears, the title reflows). Each flicker re-entered `_on_track_change`, which resets
`self.offset = 0.0` and re-identifies — wiping a confirmed sync.
**Fix (v1.0.22):** `_on_track_change` now bails early when the "new" track's title still matches
the currently-loaded song (`_titles_match`, and not live) — it keeps the sync and only refreshes
duration. The recal loop keeps listening, so a genuine same-title-different-song is still caught
by sound. Fixes the "was fine then desynced" class for every song.

## TICKET-031 — Romaji-only cover showed no Japanese / no English (Blue Bird) 🟢
**Symptom:** Raon Lee's "Blue Bird" cover displayed ONLY romaji ("aoi aoi ano sora") — no
kanji/kana, no English translation.
**Root cause:** the cached `naruto_shippuden_blue_bird.json` was `lang: ja-romaji` (a romanized
upload) with `en` = the romaji copied verbatim (romaji can't be furigana'd or translated). The
romaji→kanji upgrade (`_synced_cjk`, which DOES find the NetEase Japanese original) had failed
when first fetched — it searched the COVER channel ("Raon") as the artist — and the stale
romaji then stuck forever, because a cache hit never re-fetched (same trap as TICKET-028, but
for romaji). A stray `┃` (a truncated "| Cover by …") in the stored title also made the search
match a *Spanish* track.
**Fix (v1.0.21):**
- The runtime cache-hit upgrade (TICKET-028) now also fires for **romaji-only** hits
  (`lang endswith '-romaji'`), re-fetching **cover-style** (by TITLE) so it reaches the kanji
  original; `load()` supersedes the romaji the moment Japanese arrives. Romaji hits never lock.
- `clean_title` + `_title_variants` now strip box-drawing / fullwidth bars (`┃│｜／・‖`) so a
  truncated "Song┃" no longer poisons the search.
- `audit_cache.py --upgrade-generated` now also upgrades romaji-only files in place (only when a
  real CJK result is found). Verified: `fetch_lrc('Blue Bird', cover=True)` → 42 JP lines.

## TICKET-030 — Mode-aware sync: FOLLOW live/short arrangements, distrust repeated-chorus reads 🟢
**Symptoms (from live telemetry, TICKET-029's logs):**
- Studio サクラミラージュ "desynced multiple times" — log showed offsets oscillating
  `applied -10.36s` … `holding -70.36s`. The ~60 s jumps are the spacing between the song's
  **repeated choruses** (花桜/徒桜 ×3): Shazam matched the *wrong repetition*, and on a studio
  track the player clock is exact so chasing them is what desynced it.
- V.W.P `【LIVE MV】魔女(真) Short Ver.` — a **live/short arrangement** whose timing is wildly
  different from the studio LRC (massive real offset). It wasn't even detected as live
  (`is_live_or_compilation`=False) so it got studio handling and stranded.
**Insight:** studio and live pull in OPPOSITE directions — studio wants the offset *reset*
(exact clock, big reads = artifacts); live wants it *followed* (the offset is real and drifts
with tempo). So sync must be **mode-aware**.
**Fix (v1.0.20):**
- New `is_live_arrangement()` (`_LIVE_VER_RE`: LIVE/LIVE MV/Short Ver/Acoustic/`from "…"`/
  ライブ/弾き語り…) + a **duration-mismatch** test (playing length vs the LRC's span >25 s)
  classify each track as **studio** or **live** per read.
- **Live = FOLLOW:** apply a corroborated offset even when large (cap raised to the studio
  length), EWMA-smoothed (`0.6·new + 0.4·old`) to ride tempo drift, polling every ≤8 s.
- **Studio = distrust ambiguity:** track the spread of recent reads; if they diverge >15 s
  (repeated-chorus matches), RESET to 0 instead of chasing — plus all of TICKET-029's
  reset-first logic. Legit small studio offsets still apply.
- Telemetry now logs `mode=studio|live` and `spread=` per read. Verified by simulation across
  studio-repetitive, live-short, and studio-normal scenarios.

## TICKET-029 — Sync redesign: RESET is the first-line defense; add/drop time only on sonic confirmation 🟢
**Request:** "I just reset to get me back to proper place but that should happen
automatically. Make the reset the first line of defense against desync; only when sonic
markers indicate the lyrics are wrong should the system drop/add time. Improve logs so the
desync is visible."
**Insight:** digital playback has **no clock drift** — the player position is exact — so the
correct offset is almost always **0**, and a *chased* non-zero offset is the usual cause of
desync. Reset, not nudging, is the right default.
**Fix (v1.0.19, `_consume_async`):** the Shazam-read handler now:
1. **AUTO-RESETs to 0** the moment the audio implies ~no offset (`|corr|≤0.8`) while we're
   showing one — the manual "reset to 0 and it's fixed", made automatic and the first-line
   defense.
2. **Drops/adds time ONLY when corroborated** — two independent reads agree (`|corr−pending|
   <2.0`) before any non-zero offset is applied (sonic markers confirm a real mis-timing).
3. **Never disturbs a correct offset on noise** — absurd reads (≥ duration cap) and single
   uncorroborated reads are ignored/held, so a confirmed MV-intro offset survives Shazam's
   ±1–2 s jitter. Verified by a 7-scenario decision simulation.
4. **Re-verifies a live offset fast** — recal cadence drops to ≤12 s whenever `|offset|>0.8`,
   so a bad offset is reset within seconds, not a full slow cycle.
**Logging:** every read now emits `sync-read: drift=±Xs audio_off=… shown_off=… pos=… line#…`
so a developing desync is visible in `/logs`; `/status` gains `sync_drift`, `sync_drift_age`,
`sync_pending`. Supersedes the eager-correction behavior behind TICKET-015/026.

## TICKET-028 — Generated lyrics cached then served FOREVER (popular songs "keep generating") 🟢
**Symptom:** popular songs that providers DO have kept showing AI-generated lyrics on every
replay.
**Root cause:** a cache hit short-circuits the matcher — once a song got a `generated` file
(from a one-time transient fetch failure or a since-fixed cleaning bug), `LyricsIndex.match`
served it on every future play and **never re-fetched** the real lyrics. The audit found 66
such files.
**Fix (v1.0.19):** (a) **runtime upgrade** — a generated cache hit now shows instantly *and*
kicks off a background real-fetch; `load()` supersedes it the moment real lyrics arrive, and a
generated hit no longer title-locks. (b) **`audit_cache.py`** — a reusable cache accuracy
auditor (meta, romaji↔furigana, language, timing gaps, duplicates) with `--upgrade-generated`
to re-fetch the backlog **in place** (same filename, no slug duplicates). Audit of 492 files:
66 generated · 216 missing duration · 1 benign duplicate. (The romaji↔furigana check is
informational only — deriving romaji from furigana regresses 287 files via a compound-verb
doubling bug, so the stored cutlet romaji is kept.)

## TICKET-026 — Absurd Shazam offset desynced a song (シンメトリー +160s) 🟢
**Symptom:** "messing up on this ReGLOSS song again" (シンメトリー). The LYRICS were correct
(`heard 'シンメトリー' | loaded 'シンメトリー' | match=True`, 51-line cache); the SYNC was the
problem — `sync: holding +160.61s` in the log.
**Root cause:** Shazam matched a DIFFERENT recording/segment and returned a +160s offset.
The TICKET-015 cap was 180s, so +160s slipped through; the dead-band held it for ONE read,
but two consistent bad reads would "confirm" each other and apply +160s → the whole song
desyncs (clione live had the same shape).
**Fix (pushed, v1.0.15):** the re-sync cap is now duration-aware — reject any |corr| ≥
`min(120, max(45, 0.4×duration))` (a correction that's a big fraction of the song is a
Shazam mismatch, not a real seek) AND clear the pending value so two bad reads can't
confirm. Real seeks/intros (small) still apply via the dead-band + 2-read confirm.

## TICKET-024 — "Multiple sets of lyrics" + "some ended up generated too" 🟢
**Symptom:** lyrics looked like two overlapping sets; songs that eventually FETCHED real
lyrics sometimes still showed AI-generated lines.
**Root cause (a feature CONFLICT):** when a slow song generated and THEN the fetch
finally resolved, `load()` of the real lyrics cancelled the realtime generation
(`_gen_token`) but **NOT the background deep transcription** (`_deep_token`). The deep
pass would complete a bit later and `_apply_deep` would **overwrite the real fetched
lyrics** with its `generated-deep` version (and re-save the cache).
**Fix (pushed, v1.0.14):** real lyrics now supersede ALL generation — `load()` of a
non-`generated*` source bumps `_deep_token` too, clears `_gen_lines`, and stops the gen
loop; `_apply_deep` also bails if real lyrics are already loaded (no save, no display).
Plus the generate-vs-fetch defer was widened (~43s) so a slow-but-successful fetch wins
before generation even starts ("generated before finding it"); cleaner titles (TICKET-023)
already make most fetches resolve in <15s.

## TICKET-025 — Confidence score: generic titles must defer to the AUDIO ("Awake" rule) 🟢
**Request:** "Awake"/"BANG" are common names — the AUDIO should weigh more than the title;
document in source what contributes to the confidence score.
**Root cause:** `_is_generic_title` only caught tie-in *tags* ("OP Theme"), so a common but
real name like "Awake" / "BANG" / "Lucky Star" still got **title-locked** — and a wrong
same-title match couldn't be corrected by sound.
**Fix (pushed, v1.0.14):** new [confidence.py](confidence.py) documents EVERY signal that
contributes to song-match confidence (banner OCR > clean-source title > heard-by-sound >
title-exactness > duration > artist > language) and adds `title_distinctiveness()` /
`is_common_title()`. The title-lock now also requires the title to be DISTINCTIVE, so
Awake/BANG/Love/Lucky Star (distinctiveness 0.10–0.27) stay unlocked and let Shazam decide,
while feelingradation/シンメトリー/white balance (0.57–0.85) still lock. Logged for transparency.

## TICKET-023 — Popular JP/VTuber songs generate when the providers HAVE them 🟢
**Symptom:** very popular songs (KizunaAI "white balance" 2M views, "LOVESHII", 大神ミオ
"Howling") **generated** lyrics. The user assumed a database gap and asked for better
lyric libraries.
**Decisive finding (NOT a database gap):** the providers already carry them —
`fetch_lrc("white balance", "Kizuna AI")` → 32 lines, `fetch_lrc("LOVESHII", "Kizuna AI")`
→ 47. The bottleneck was the **title/artist cleaning algorithm**:
  - `clean_title` left the **"Artist - Song" hyphen prefix** ("KizunaAI - white balance",
    "Kizuna AI x KAF - LOVESHII", "Reol - Edge") — the same class as Dunk's "Song/Artist"
    slash, but with `-`.
  - `clean_artist` didn't strip a **dash-prefixed channel suffix** ("Kizuna AI -
    A.I.Channel" → must be "Kizuna AI"; the suffix made the search miss).
**Fix (pushed, v1.0.13):** the artist-aware reducer now also strips a leading artist
credit before the first ` - ` (only when that head matches the artist, so a real "A - B"
song title is left alone), and `clean_artist` strips `- …Channel`. Verified: white balance
→ 32 lines, LOVESHII → 47, Edge → 73, Dunk/LOAD/幻界 unaffected, bilingual untouched.
Deleted the stale generated caches so they re-fetch the real lyrics.
**Takeaway documented in RESEARCH.md:** for this catalog, *matching* (clean titles +
right artist) beats *adding databases* — Musixmatch + NetEase + LRCLIB already cover most
J-pop / anime / VTuber; the gaps left (genuinely niche covers, live takes) are handled by
OCR (TICKET-022) + generation, not a different fingerprinter.

## TICKET-022 — Concert song detection via on-screen banner OCR 🟡 (feature)
**Request:** in a long concert video (ReGLOSS 3D live) the app should play the CURRENT
song's lyrics (SUPER DUPER, 泡沫メイビー, …) and sync — Shazam alone fails on live takes.
Use the **song name shown on screen** as a high-confidence hint feeding the confidence score.
**Approach (new [concert_ocr.py](concert_ocr.py) + [docs/CONCERT_DETECTION.md](docs/CONCERT_DETECTION.md)):**
capture the screen → crop the top banner strip → OCR with the **built-in Windows OCR**
(`Windows.Media.Ocr` via winsdk — no new dep) → fuzzy-match to the song library → if
`score >= 0.85`, load that song (cache/fetch), title-lock it (OCR is authoritative in a
concert), and lock timing by sound. Runs throttled in **live mode** on a background thread.
**Research:** burned-in-text OCR is a proven approach (VideOCR/PaddleOCR ~99% JP). Combining
**on-screen text OCR + audio fingerprint** is the recommended design.
([VideOCR](https://www.fcportables.com/videocr-portable/), [meikipop JP OCR](https://github.com/rtr46/meikipop))
**Findings:** Windows OCR works (read "SUPER DUPER" cleanly); ships **en-US** only —
Japanese banners need the pack once: `Add-WindowsCapability -Online -Name
"Language.OCR~~~ja-JP~0.0.1.0"`. The in-memory bitmap path segfaults → use a temp PNG +
`StorageFile`. Matcher verified: real banners → 1.0, hashtag/chat noise → 0.36 (ignored).
**Status:** 🟡 module + matcher built & tested, wired into live mode (en-US live now);
needs the ja-JP pack for Japanese banners + live concert validation + intermission handling.
**v1.0.16 update:** the concert ("Departures") sat on "Listening to identify…" because OCR
only matched ALREADY-CACHED songs. Added `concert_ocr.plausible_title()` (extracts a clean
Latin banner name, filters hashtag/chat/UI noise; OCR cropped to the top-LEFT to skip the
right-side chat panel) + `_fetch_ocr_song()` so a confident banner we DON'T have is
**fetched cover-style** ('Departures' → 37 lines). Verified the matcher rejects "Top fans"/
"Top chat replay" noise. So concert detection is no longer limited to pre-cached songs.

## TICKET-021 — MV-intro onset-anchor double-shifted a fetched LRC (サクラミラージュ drift) 🟢
**Symptom:** サクラミラージュ's lyrics drifted ~11s late; resetting Sync→0 fixed it every
time. The watcher caught a persistent **-11s** offset on it.
**Root cause:** `_on_song_onset` anchored the MV intro with `offset = -vpos`, which
ASSUMES the lyrics start at time 0 (true for *generated* lyrics). But サクラミラージュ's
**fetched LRC already has the intro built in** (first line @18.9s, audio onset @~11s), so
its timestamps are **absolute video-time** — the right offset is **0**, and `-vpos`
double-shifted it by 11s.
**Fix (pushed, v1.0.11):** anchor only when the lyrics genuinely run AHEAD — if the first
line is already at/after the onset (`first_start >= vpos-2`), the LRC is absolute →
**offset 0** (no anchor); otherwise (generated ~0, or relative LRC) keep the `-vpos`
anchor. This makes the user's manual "reset to 0" automatic and correct. Verified across
fetched-with-intro / generated / relative / at-onset cases. Clione lyrics confirmed correct.

## TICKET-032 — Display persistence + mirror/cycle modes + 4s sample default 🟢
**Request:** chosen display falls back to primary too easily; need to mirror lyrics to ALL
screens or cycle through each; make 4-second sample for lyric sync the default (was 10s).
**Root cause (display fallback):** stored `display="mon:N"` was an INDEX into the current
enumeration. After a monitor sleep/wake/replug, indices renumber → the saved index pointed
nowhere → `_apply_display` silently fell back to primary.
**Fix (pushed, v1.0.26):** added monitor fingerprinting (`_mon_fingerprint` = "x,y,wxh")
saved alongside the index, with index fallback then primary fallback (each logged).
Watchdog (`_check_monitors`) re-enumerates every 3 s and re-applies the display if the
topology changed. Added "Mirror on ALL screens" (transparent Toplevel clones rendering
the current line via simplified `_create_mirrors` / `_update_mirrors`) and "Cycle through
screens" (rotates `_cycle_idx` on each `_render`). `recal_secs` default lowered from 10
to 4.

## TICKET-033 — Maneki-neko dancing character (cuteness rebuild) 🟢
**Request:** turn the dancing character into a maneki-neko — beckoning paw, red collar
with bell, gold koban; first attempt looked "kinda fucked up."
**Fix (pushed, v1.0.26):** complete redesign of `_draw_chibi` in `character.py` matching
classic kawaii proportions researched online (CLIP STUDIO chibi tutorial, DeviantArt
Maneki-Neko tutorial): big head (~60% of figure), small round body, raised right paw
that waves with `math.sin(phase * 3.2)`, calico patches themed to artist colors, koban
coin with 福 kanji in left paw, red collar arc + gold bell, happy closed-crescent eyes
(the signature kawaii expression), pink nose + ‿ smile, cheek blush with stipple, music
notes while playing. Clean tkinter primitives (oval/polygon/arc/line), no ugly triple-
line arm hack.

## TICKET-034 — Cover lyrics: search by ORIGINAL artist, not covering channel 🟢
**Symptom:** "[COVER] Coffee - Alka | Kaneko Lumi" loaded the wrong Lumi song; reporting
wrong-song never recovered. The covering channel (Lumi) was searched as the artist
instead of the original artist (Alka), so Shazam couldn't disambiguate either.
**Root cause:** `_COVER_RE` matched `(cover)` and 歌ってみた but NOT `[COVER]` (square
brackets). Generic bracket-strip in `clean_title` then ate the cover marker entirely, so
`_is_cover` was False — no title-first cover path, no original-artist extraction.
**Fix (pushed, v1.0.26):**
1. Added `\[\s*cover\s*\]` to `_COVER_RE` so square-bracket covers are recognized.
2. Added `extract_cover_original(raw_title, cover_channel)` that parses common cover
   patterns ("Song - OrigArtist | CoverChannel", "Song - OrigArtist / CoverChannel") and
   returns the original artist. Identifies which side is the cover channel by lowercase
   normalised-substring match against the cleaned artist.
3. `_on_track_change` uses `_cover_original_artist` as the `fetch_artist` for both index
   lookup and `_start_fetch` when set, falling back to the channel name otherwise.
4. `report_wrong` (user-driven correction) ALSO re-fetches by the original artist for
   covers before falling back to sound ID (which often fails on covers).
**Research:** lyric finders verify artist + don't trust the first match (TICKET-002).
This applies the same principle to the cover artist token.

## TICKET-035 — Long instrumental intros desync (Grimes "Genesis") 🟢
**Symptom:** "Grimes - Genesis" video is 5:32 but the song's first vocal is at ~1:10
(~70 s instrumental intro). Lyrics started showing at video time 0 and ran ahead of
singing until Shazam mid-song fingerprinted a vocal phrase to calibrate (often half the
song later).
**Root cause:** `_mv_mode` only triggered on titles containing MV/PV/Music Video/Official
markers — "Grimes - Genesis" has none. And the existing `_on_song_onset` only fires on a
quiet→music transition (a leading silent gap), which YouTube videos don't have — music
plays from frame 0.
**Fix (pushed, v1.0.26):**
1. Added **vocal-band onset detection** to `SongChangeDetector`: tracks the ratio of
   spectral energy in 200-3000 Hz (vocal range) to total energy via cheap real FFT on
   each 0.2 s block (~0.5 ms per check). Learns the instrumental baseline for the first
   ~5 s of music, then fires `on_vocal()` when ratio runs 1.4× baseline (or > 0.55
   absolute) sustained for 1 s.
2. New `_on_vocal_onset` handler in main.py calibrates `offset = first_line.start -
   player_position` when fired, jumping the displayed lyrics to line 1 the instant
   singing starts. Guarded: only when vpos > 8 s, first line < 8 s, new offset in
   (-120, 0).
3. `load()` auto-enables MV mode when LRC duration > 15 s shorter than the YouTube
   duration AND first line starts before 5 s — catches Grimes-class uploads with no
   MV markers in the title.
4. Bumped the MV intro-hold timeout from 50 s → 100 s (most MV intros are under 90 s)
   now that vocal-onset can release it precisely.
**Research:** Silero VAD and webrtcvad were rejected (both miss singing in polyphonic
music). HPSS + mid-band energy ratio is the lightweight robust approach
([MDPI: Singing Onset](https://www.mdpi.com/2076-3417/12/15/7391),
[Silero VAD #546](https://github.com/snakers4/silero-vad/discussions/546)).

## TICKET-054 — Paused tab hijacked playback (Coffee↔Mix flip-flop) 🟢🟢🟢
**Symptom:** on a YouTube Mix, the log showed the app flip-flopping between the
playing song and a PAUSED background tab every track: `track change: Coffee → Rumor
→ Coffee → Hug → Coffee …`. So the overlay kept loading the paused Coffee tab's
lyrics over the actually-playing song → "No lyrics found", wrong song, stale lyrics,
and a fresh fetch/caption churn on every flip. A huge part of the live "desync."
**Root cause:** `MediaWatcher._pick` returned `get_current_session()` when no session
was "playing". Between Mix tracks there's a brief gap where NOTHING is playing — so
`_pick` fell to the OS "current session", which was often the paused Coffee tab. Next
poll the Mix was playing again → back to it. Flip-flop.
**Fix (pushed, v1.0.48):** made `_pick` STICKY. It tracks the source_app it's
following and (1) keeps it while still playing, (2) else the first playing session,
(3) and when NOTHING is playing — a transition gap — KEEPS the followed session
instead of jumping to a different paused tab. A paused background tab can no longer
hijack the overlay.
**Also:** `_on_track_change` now clears `self.meta` so a new song with no lyrics yet
can't display the previous song's stale source (the "youtube-captions / 0 lines" bug).
**Verified live:** "【歌ってみた】One Last Kiss" held stable for 60 s with no Coffee
interleaving; got youtube-captions/62 lines, drift 0.0.

## TICKET-053 — Overlay FROZE on the old song (the real "hella bad") 🟢🟢🟢
**Symptom:** caught live — the SMTC title had changed to a new song
("【歌ってみた】林檎売りの泡沫少女") but the app was STUCK showing the previous song's
lyrics ("Break Into My Heart", 53 lines), its clock running 357 s past their end,
showing nothing. `/diag` confirmed `render_fps: None` — the render loop was DEAD.
**Root cause:** a `NameError` I introduced in v1.0.42. The auto-caption scheduling
block added to `_on_track_change` referenced a variable `src` that isn't in that
method's scope. So **every track change raised NameError**, which propagated out of
`_tick` — and since `_tick` is a self-rescheduling `root.after` loop, the exception
stopped it from rescheduling. The loop DIED while the OS media kept advancing, so the
overlay froze on whatever was last loaded. (Other timers — auto-align, monitors —
survived independently, which is why the app looked half-alive.) This single bug
explains a huge share of the "stuck / wrong song / no lyrics / hella bad" reports.
**Fix (pushed, v1.0.44-46):**
1. **Crash-proof the render loop (v1.0.44):** wrapped `_tick` so ANY frame exception
   is logged and the loop ALWAYS reschedules. One bad frame can never freeze the
   overlay again — it self-heals and logs the cause.
2. **Fixed the NameError (v1.0.45):** use the cached `self._last_src` (the source IS
   tracked in the tick loop) instead of the undefined `src`.
3. **Captions now actually apply (v1.0.46):** the caption fetch logged "76 lines
   fetched" but `src` stayed `lrclib` — `_apply_deep`'s `_deep_token` check discarded
   them because generation / a title re-report bumped that shared token during the
   ~20 s yt-dlp fetch. Added `_apply_captions` guarded by `_track_seq` (bumped ONLY on
   a real song change), so captions apply as long as the same song is still playing.
**Verified live end-to-end:** track changes no longer freeze; `西憂花『ふわふわhazy』` →
"captions: 64 ja lines" → "captions: applied 64 lines" → src `youtube-captions`,
drift 0.0, in_sync. The found-real-lyrics hint also no longer sticks (force re-render).

## TICKET-052 — YouTube caption track = accurate lyrics + perfect sync 🟢🟢
**The big one.** Watching live, the app showed WRONG TEXT for KizunaAI "white balance":
app LRC said "未来 未開 見たことない…" (mirai mikai mita koto nai) while the song actually
sings "未来 見たい 君の傍で…" (mirai mitai kimi no katawara de). syncedlyrics returned a
different/worse transcription than the video — and even with perfect timing, wrong WORDS
read as "hella bad." Provider LRCs also drift because their timing is for a different cut.
**Root insight:** a YouTube video's OWN caption track is the ground truth — correct words
AND timing locked to THIS exact video. (The user's other agent proved it: "pulled the
caption track, 35 lines, perfectly timestamped.")
**Fix (pushed, v1.0.42):**
1. **Bundled yt-dlp** into the build (it was never included → deep_transcribe AND captions
   silently no-op'd). Added `yt_dlp` to the spec's collect_all.
2. **`fetch_captions_only(query, lang)`** in deep_transcribe — a FAST subs-only yt-dlp
   pull (no audio download, no Whisper): manual subs first, then YouTube auto-captions
   (ASR), parsed to timed lines. Requests ONLY the song's language (asking all 5 CJK
   langs at once → YouTube 429) with `ignoreerrors` so one rate-limited lang can't abort.
3. **`Overlay.load_youtube_captions()`** annotates (furigana/romaji/translation) + saves
   as source `youtube-captions` (real → replaces a wrong LRC), upgrades the overlay live.
4. **Auto for browser videos:** `_on_track_change` schedules a background caption fetch
   ~4 s in for any browser (YouTube) source, throttled (≥8 s between yt-dlp calls,
   once per song) so a fast playlist can't 429. Tray toggle "Use YouTube captions" +
   "Get captions for this video now"; `POST /captions`; setting `captions` (default on).
**Verified end-to-end live:** `Reol - 'ミュータント' Music Video` → log "captions: 43 ja
lines from YouTube caption track" → saved → source `youtube-captions`, drift 0.0,
in_sync True, "✨ Found the real lyrics". This replaces the approximate
LRC+Shazam+correlation stack with the video's own ground-truth lyrics for YouTube.

## TICKET-047b — Scroll fill: layer-composite to kill per-fill glyph render 🟢
**After the timer fix (047), ~10% of frames still spiked 27-44 ms** — the karaoke fill
re-rendered every glyph WITH stroke outlines (8-9 draws/char) ~5×/s per singing line.
**Fix (pushed, v1.0.41):** render the block's base layer once at spawn and the fully-sung
layer LAZILY on first-sing; the per-fill step now just composites the two via a cheap
rectangle mask (no glyph render). Steady state went to a solid ~16 ms (60 fps). Splitting
the sung layer to first-sing (not spawn) avoids doubling the spawn cost into one big hitch.

## TICKET-051 — Game noise must not corrupt sync / recognition 🟢
**Concern:** the overlay is built to run WHILE gaming (there's a "Gaming" preset), but
the sync + recognition listen to the **system loopback**, which mixes the music with
GAME AUDIO — gunfire, explosions, UI clicks. Those dump energy into the 200-3000 Hz
vocal band, which would create false "vocal" blocks and corrupt the energy-correlation
sync (and could false-trigger vocal-onset detection).
**Fix (pushed, v1.0.40) — tonality gate on vocal detection:**
The discriminator between SINGING and game NOISE is **tonality**. A voice (and pitched
music) is harmonic — energy concentrated at a few frequencies → LOW spectral flatness.
Broadband SFX is noise-like → HIGH spectral flatness. `_vocal_ratio` now computes the
vocal band's spectral flatness (geometric/arithmetic mean of the power spectrum) and
scales the band-energy score by a tonality weight: full weight at flatness ≤0.35,
ramping to ZERO at ≥0.65. So a gunshot/explosion block contributes ~nothing to the
vocal mask, keeping the sync correlation clean while a game plays. Cheap (one extra
flatness ratio on the FFT already computed).
**Other layers already robust:** Shazam mis-IDs from noise can't switch songs without
a 2nd confirming read (TICKET-anti-churn); the energy correlator's small-shift prior +
uniqueness + lift floor (TICKET-049) make a noisy correlation DECLINE to act (player
clock carries sync) rather than jump.
**Diagnostics:** `/audio` now reports `band_flatness` and a `noise_like` flag, so a
game-noise period is visible in the listener view.
**Verified:** on the Marine song the vocal band's flatness stays low (vocals still
detected normally); a broadband-noise block reads high flatness and is gated out.

## TICKET-050 — Diagnostic views: source / audio listener / lyric-state analyzer 🟢
**Request:** add a video/music source view, an audio listener, and a lyric current-state
analyzer to the diagnostics API "and anything else that may help."
**Fix (pushed, v1.0.38) — three new GET endpoints:**
- **`/source`** — the RAW Windows SMTC data the app receives (title, artist, album,
  status, position, duration, rate, source_app) AND what it derived (clean_title,
  clean_artist, track_tuple, is_cover, cover_original_artist, trusted_duration,
  live_mode, mv_mode, intro_anchored) + media_error. Traces a desync to the SOURCE
  (wrong title leaking, stale position, paused) before blaming sync logic.
- **`/audio`** — live audio LISTENER from the loopback recorder: capturing flag +
  age, rms, loud_ema, is_silent, live vocal_ratio, vocal_detected_now, window on/off
  block counts + adaptive threshold, vocal_baseline, buffer_len, blocks_seen,
  music_for/silent_for. Plus a `recent_pattern` ASCII strip (█/· for the last ~6 s
  of vocal on/off). Confirms audio is flowing and vocals are being detected.
- **`/lyricstate`** — current/prev/next lines with timings, karaoke fill_fraction,
  between_lines flag, lrc_span vs video_duration, and structural anomaly checks
  (LRC past video end, low coverage, big gaps). Surfaces "lyrics don't fit the song"
  problems that masquerade as desync.
Implemented `SongChangeDetector.live_audio()` (latest rms/vocal_ratio/silence) and
`Overlay.get_source()/get_audio()/get_lyric_state()`.
**Already paid off:** `/source` revealed the Coffee cover's clean_title still carries
the "- A!ka | Kaneko Lumi" suffix (cover_original_artist extracts "A!ka" fine, but the
title isn't fully reduced) — a real title-cleaning gap to tighten. `/lyricstate`
confirmed that cover is healthily matched (span 180.6 vs video 186.2, no anomalies).

## TICKET-049 — Energy correlator chorus-repetition phantom (small-shift prior) 🟢
**Symptom:** intermittent MASSIVE desyncs (offset jumping ~15s). /diag caught the
mechanism live: on "Coffee - A!ka | Kaneko Lumi" the energy correlator persistently
reported `best_shift=-14.8, lift=0.262` while the song was actually in sync at
offset 0. That -14.8 s is a CHORUS-REPETITION match — the vocal on/off pattern one
chorus away looks identical, so the cross-correlation has a near-equal peak there.
Whenever Shazam couldn't provide a fresh anchor (its agreement-guard, TICKET-043,
needs a recent reading), the phantom could win and yank the offset 15 s → the
"massive desync."
**Research (score-following literature, ICASSP 2024 real-time lyrics alignment;
Dixon OLTW):** production systems use a TRANSITION PRIOR — the true position rarely
jumps far between updates — and reject ambiguous matches rather than treating the
global-best peak as truth.
**Fix (pushed, v1.0.37):**
1. **Small-shift prior:** `scored = agree − penalty·|shift|` (penalty 0.012/s,
   tunable). A distant shift must beat the no-change score by `penalty·|shift|` to
   win, so the correlator prefers KEEPING the current sync unless evidence is
   overwhelming. Directly kills the -14.8 phantom (it scored ~0.14 over no-change,
   under the 14.8×0.012≈0.18 it needed).
2. **Peak-uniqueness rejection:** mask ±2 s around the winner, find the next-best
   distant peak; if it's within `energy_peak_margin` (0.06) of the best, the match
   is ambiguous (chorus) → no change. Both knobs live-tunable via /tune.
3. **Adaptive vocal threshold:** the old absolute floor `max(0.50, baseline*1.25)`
   left the correlator BLIND on many songs ("insufficient vocal activity, 0 blocks")
   because the vocal-band ratio's absolute level varies hugely by genre. Replaced
   with a per-window split at `median + 0.5·(p75−median)` + a contrast gate
   (need ≥6 on AND ≥6 off blocks, spread ≥0.02). Result: 0 → 57 vocal blocks
   detected on the Marine cover, so the correlator can actually evaluate it (and
   then correctly reject the ambiguous chorus rival rather than jumping).
4. **Diagnostics:** /diag energy_align now reports `rival_shift`, `rival_score`,
   `ambiguous`; sync block adds `offset_history` (last 20 offset changes with
   timestamps) so a jump is visible after the fact.
**Live-verified:** Niconico Marine "Ahoy!!" cover now frame-matches the video's
burned-in karaoke ("ヨーソロー！ついておいで 共に Yo-Ho…"), drift 0.02, with the
correlator logging `best=0.0(0.38) rival=-15.0(0.38) ambiguous=True → no change`
— the exact phantom that used to cause the -15 s jump, now correctly rejected.
**Note:** the deeper fix the research points to (OLTW over chroma + a
constant-velocity Kalman filter with innovation gating, replacing the whole
ad-hoc reconciliation) is logged as future work — the prior+uniqueness is the
high-leverage 80% with far less risk.

## TICKET-047 — Scroll-through stutter: Windows timer granularity (+ GIL) 🟢
**Symptom:** scroll-through ("lr"/"rl") modes "very stuttery", "has been suffering."
**Diagnosis (via the new /diag, TICKET-048):** `recent_ms` frame history showed the
belt alternating between **16 ms and 30 ms even with ZERO render work** (spawns and
repaints disabled live via /tune). That pattern is the giveaway: Windows' default
system timer granularity is ~15.6 ms, so Tk's `after(16)` for a 60 fps loop fires at
EITHER ~15.6 ms OR ~31.2 ms unpredictably — an uneven cadence the eye reads as stutter.
**Root cause (primary):** system timer resolution. **(secondary):**
`_run_energy_correlation` ran a 151-iteration Python loop holding the GIL on a
background thread every 15 s, adding periodic hitches.
**Fix (pushed, v1.0.36):**
1. `ctypes.windll.winmm.timeBeginPeriod(1)` at startup → raises timer resolution to
   1 ms so `after(16)` is accurate. **Result: steady frames went 16/30 ms (jitter
   ~10 ms) → solid ~16 ms (jitter ~1 ms), render 48–60 fps.**
2. Vectorized the energy-correlation shift search — LRC mask built once on a 0.2 s
   grid, all 151 shifts evaluated with one numpy gather (`(151, nblocks)`, C-level,
   no GIL hold). Removes the periodic background hitch.
3. Time-budgeted the heavy ticker section (`heavy_budget_ms`, default 10) + made
   `fill_skip`/`spawn_budget`/`repaint_budget` live-tunable via /tune, so a slow PIL
   paste can't stall the belt and the knobs can be tuned without a rebuild.

## TICKET-048 — Deep diagnostics API (/diag) + FPS/frame-timing metrics 🟢
**Request:** "include those functions in the app so you can diagnose better" +
"include fps in diagnostics api." Iterating on sync/perf needed observability
without rebuilding.
**Fix (pushed, v1.0.36):**
- `GET /diag` returns the full sync state machine (offset, drift, drift_integral,
  pending_corr, last_audio_off + age, sound_song, sound_title_alias, title_locked,
  effective_song_time, showing_idx vs should_show_idx, in_sync flag), the last
  energy-correlation result (best_shift/score/median/lift, vocal-block counts), and
  FPS/frame-timing (target, render, frame_ms, worst_ms, jitter_ms, recent 60-frame
  history, perf_mode, scroll_dir).
- `_tick` now tracks frame jitter (EWMA of |frame − target|) and worst-frame ms
  over a 120-frame ring buffer — the stutter metrics.
- `/status` gained `frame_jitter_ms` + `frame_worst_ms` for at-a-glance checks.

## TICKET-046 — Cross-language load broke Shazam calibration → -23.7s drift 🟢
**Symptom:** after TICKET-045 made the Marine song load the correct Japanese cache,
a QA pass found the offset had silently drifted to **-23.7s**. The lyrics were right
but the timing was badly off.
**Root cause:** the loaded Japanese cache title ("Ahoy!! 我ら宝鐘海賊団☆") never
string-matches Shazam's romanized heard title ("Ahoy!! We are Houshou Pirates"), so
`loaded_ok` was always False → the Shazam timing-calibration block (`if loaded_ok:`)
never ran → `_last_audio_off` stayed stale → the energy correlator's chorus-guard
(TICKET-043, which needs a recent Shazam reading to compare against) couldn't fire,
and the correlator locked onto a chorus-repetition match (-23.7s). The CJK-preference
fix (045) fixed the lyrics but orphaned the calibration path.
**Fix (pushed, v1.0.35):** added `_sound_title_alias`. When the sound-correction
path loads a cache whose title doesn't match the romanized heard title, it records
that heard title as an alias. `loaded_ok` now also passes when the heard title
matches the alias — so Shazam calibrates the song's timing normally, keeping
`_last_audio_off` fresh and the chorus-guard armed. Reset on track change.
**QA-verified:** part of a full button/setting + all-tabs audit (see below).

## TICKET-045 — Romanized Shazam title loaded English lyrics for a JP song 🟢
**Symptom:** after TICKET-044 fixed identification, the Niconico Marine "Ahoy!!"
karaoke synced correctly (drift -0.01) but showed **English** lyrics ("A black sky
above / Die in the waves...") with `lang: "de"` — while the video is the Japanese
"On vocal" karaoke. The correct Japanese cache (`ahoy_我ら宝鐘海賊団.json`, 65 lines)
already existed from an earlier session.
**Root cause:** with the player title leaked (TICKET-044), the app trusts Shazam's
title — but Shazam returns the ROMANIZED/English title "Ahoy!! We are Houshou
Pirates". `index.match` then exact-matched a wrong English-lyrics LRC (and saved
it as a new cache), instead of the Japanese original whose title only shares the
leading token "Ahoy". Same class as the romaji-vs-kanji problem fetch_lrc already
solves for fetching — but it wasn't applied to the local cache match.
**Fix (pushed, v1.0.34):** added `Overlay._prefer_cjk_cache(artist, heard_title,
duration)`. When the heard title is pure-Latin (no CJK) but a cached entry by the
SAME artist HAS CJK script and shares the leading Latin token (e.g. "ahoy"
survives in "Ahoy!! 我ら宝鐘海賊団☆"), prefer that original-script cache. Wired into
the sound-correction path BEFORE `index.match`. Deleted the wrong English cache.
**Verified (live, title still leaking):** log shows the full chain —
`share no content → trusting Shazam` → `preferring original-script cache
ahoy_我ら宝鐘海賊団.json over romanized title` → `correcting -> cached`. App now
shows 「あん、神様ぁ、いつかこのマリンを本物の海賊に…」 with furigana + romaji + English,
matching the video's burned-in karaoke line frame-by-frame. drift -0.02.

## TICKET-044 — Niconico sidebar title leaks → wrong song fetched 🟢
**Symptom:** while playing the Marine "Ahoy!!" karaoke on Niconico, the app
reported `player_title: "Space Marine 2 プレイ動画 #35"` (a recommended video in
Niconico's sidebar) and `heard_by_sound: ["Space Marine 2 プレイ動画 #35",
"Houshou Marine"]`. Shazam correctly heard Houshou Marine (the artist) but the
fetch attempt used the wrong title and failed; no lyrics loaded.
**Root cause:** Niconico can populate its SMTC media session with the sidebar
"now-playing" preview metadata instead of the actual main video. The app's
CJK-preserve logic (`_has_cjk(g_title) and not _has_cjk(title)`) preserves
the player's CJK title even when it's completely unrelated to what Shazam
heard. Designed for legit cases like "Kira" (Shazam) vs "綺羅" (player) — the
same song, different scripts. Backfires when the player title is from a
completely different video.
**Fix (pushed, v1.0.33):** added `_titles_share_content(a, b)` static method.
Returns True when the two titles share ANY plausible content (4-char
normalised-substring overlap, or CJK 2-gram overlap). The CJK-preserve now
requires both `_has_cjk(g_title) and not _has_cjk(title)` AND
`_titles_share_content(g_title, title)` — so a totally-unrelated player
title falls through to "trust Shazam." Logs the override decision so the
behavior is auditable.
**Verified:** Marine "Ahoy!!" with Shazam reading the correct artist
(Houshou Marine) but stale sidebar title now uses Shazam's title for the
fetch, finding the real LRC. Existing partial-script cases (Kira ↔ 綺羅,
romanized JP) still preserve the player's CJK title via the n-gram overlap
check.

## TICKET-043 — Energy correlator picked chorus-repetition match → wrong offset 🟢
**Symptom (live observation):** On Grimes "Oblivion", offset jumped from -0.76s to
**-14.37s** in a single energy-align cycle, then took 4+ Shazam re-confirmations to
recover. Log evidence: `energy-align: offset -0.76s → -14.37s (α=0.91, score 0.255,
lift 0.221)` — the correlator's "sharp peak" was actually a chorus-repetition match,
not the true offset. Shazam was simultaneously reading `audio_off=-0.47` (the correct
value).
**Root cause:** songs with repeated patterns ("la la la" choruses, repetitive hooks)
produce sharp correlation peaks at MULTIPLE candidate shifts because the vocal-mask
pattern repeats periodically. `peak_lift > 0.10` alone can't distinguish "I found the
right alignment" from "I found a chorus that looks like the previous chorus." The
correlator picked the latter and the auto-sync chased a false offset.
**Fix (pushed, v1.0.32):**
1. Track Shazam's absolute implied offset (`_last_audio_off` + `_last_audio_off_t`)
   on every Shazam read — separate from `_last_drift` which is relative.
2. In `_run_energy_correlation`, before applying a candidate offset, sanity-check
   against the last Shazam reading (within 60s):
   - If `|new_off - _last_audio_off| > 4.0s` → reject the candidate as a probable
     chorus-repetition match.
3. Reset `_last_audio_off` on track change so it doesn't leak across songs.
**Why the band is 4s:** Shazam itself can read +/-2s due to chorus ambiguity, so
allowing 4s deviation absorbs that noise while catching the gross mismatches
(13.6s in this case). Tunable via `/tune` if needed.

## TICKET-042 — Karaoke version drift cap was too tight (Niconico Marine) 🟢
**Live test:** Watching the Niconico Ahoy!! karaoke video, drift hit +7.85s
(legitimate — karaoke version is offset ~7s vs studio). Auto-sync's fast-path
refused to apply because `drift_fastpath_cap` was 5.0s. The convergence still
worked via the 2-read confirmation path eventually, but slowly.
**Iteration via `/tune` (no rebuild):**
1. Raised `drift_fastpath_cap` 5.0 → 10.0 to allow karaoke-version corrections.
2. Lowered `drift_fastpath` 4.0 → 2.0 — was too aggressive (1-read momentary
   spikes would have applied).
3. After convergence to drift 0.02s, settled on `{drift_fastpath: 3.0,
   drift_fastpath_cap: 8.0}` — handles real karaoke-version offsets (up to
   8s) without applying single momentary chorus-ambiguity reads.
**Fix (pushed, v1.0.31):** promoted those tuned values to code defaults.
This is exactly what `/tune` is for — try values live, ship the winners.

## TICKET-041 — Live-tunable sync params via /tune API (no rebuild needed) 🟢
**Request:** allow adjusting sync tuning constants on the fly without rebuilding —
iterating on `DEADBAND`/`AGREE`/spread thresholds/drift integrals took 5-minute
rebuild cycles each.
**Fix (pushed, v1.0.30):**
1. Lifted all sync constants to a `self._tune` dict on Overlay (15 parameters
   covering Shazam confirmation gates, drift integral, energy correlation, and
   auto-align cadence). Defaults match what shipped.
2. Replaced every hardcoded literal in `_consume_async` + `_maybe_auto_align`
   + `_run_energy_correlation` + `_periodic_auto_align` with `self._tune[key]`
   lookups.
3. Added Overlay methods `get_tune()` (snapshot dict) and `set_tune(key, value)`
   (type-coercing setter with logging).
4. Added two API endpoints:
   - `GET /tune` → current state of every parameter
   - `POST /tune?key=X&value=Y` OR `POST /tune` with JSON body `{k: v, …}` →
     update one or many; returns per-key results + full new state
**Tunable keys** (defaults in parens):
- `deadband` (0.8), `agree` (2.0), `agree_live` (4.0) — Shazam confirmation gates
- `spread_reset` (20), `reset_offset_max` (5) — chorus-ambiguity reset thresholds
- `drift_fastpath` (4.0), `drift_align_trigger` (6.0), `drift_min_for_accum` (0.8),
  `drift_fastpath_cap` (5.0) — drift integral mechanics
- `auto_align_cooldown` (25), `auto_align_min_pos` (12), `shazam_lock_grace` (30) —
  auto-align gating
- `continuous_recal_ms` (15000) — background correlation cadence
- `energy_apply_min` (0.4), `energy_lift_floor` (0.10), `energy_max_offset` (60) —
  energy correlation acceptance thresholds
**Verified live:** `curl GET /tune` returns all values; `curl POST /tune?key=K&value=V`
updates one; `curl POST /tune -d '{"agree":2.5}'` updates many; new values apply
immediately on the next sync tick (no restart).

## TICKET-040 — Live-tested on Grimes "Oblivion": chorus reset + slow confirmation hurt sync 🟢
**Symptom (observed live):** Grimes "Oblivion" started ~15 s desynced (lyrics ahead).
Energy correlation pulled drift to ~2.4 s, but it stuck there — the offset never fully
locked. Watching `/status` showed Shazam reading the offset correctly but the 2-read
confirmation never fired (repeated choruses → Shazam reads varied widely).
**Root causes:**
1. **Chorus-ambiguity reset was too aggressive.** When recent Shazam reads spread
   > 15 s (chorus repetition), the code reset `self.offset = 0`. For a song needing a
   real -22 s offset (studio LRC vs album cut), this kept undoing the correction every
   time it came around to a chorus. The サクラミラージュ fix that motivated this logic
   was for a SMALL offset (-11 s); the same logic killed convergence for larger ones.
2. **2-read confirmation never converged on Grimes.** Shazam reads jumped between
   different choruses, so two reads within 2.0 s of each other rarely happened. The
   pending correction kept getting replaced rather than applied.
**Fix (pushed, v1.0.29):**
1. Spread threshold 15 → 20 s, AND only reset when `|offset| < 5 s`. A larger offset is
   doing real work and shouldn't be wiped on chorus ambiguity. Verified Grimes-class
   songs no longer revert mid-song.
2. **Drift-integral fast-path** in the sync ladder: when `_drift_integral > 4.0` AND
   `|diff| < 5 s`, apply the single-read correction immediately. The accumulated
   integral IS the agreement (consistent drift direction over multiple reads). Capped
   `|diff| < 5 s` so one wild Shazam read can't yank the offset.

## TICKET-038 — Algorithmic sync: continuous drift integral, confidence-weighted updates 🟢
**Request:** make the song-position detection algorithmic rather than rely on song-specific
counters (`_align_drift_strikes >= 3` was an arbitrary threshold).
**Fix (pushed, v1.0.28):**
1. **Drift integral** replaces the strike counter. Each Shazam read where the drift
   exceeds 0.8s contributes `|drift| × time_since_last_read` to `_drift_integral`; the
   integral decays by ×0.5 when drift drops into the deadband. When it crosses 6.0
   (e.g. 1.5s drift held for 4s, or 3s drift held for 2s), auto-align triggers. Cleanly
   proportional to "how wrong the sync actually is over time," not a hardcoded count of
   reads.
2. **Continuous correlation cadence** lowered from 45s → 15s. Energy correlation is now
   the primary continuous sync source; runs every 15s in the background. The 60s vocal
   buffer keeps building enough new signal between runs.
3. **Confidence-weighted application** in `_apply_energy_align`: alpha is computed from
   correlation peak lift (sharper peak → snap to measurement; marginal peak → blend
   conservatively via EMA). Avoids yanking the offset on noisy detections while still
   converging quickly when the signal is strong. `α = max(0.3, min(1.0, (lift-0.10)/0.20 + 0.3))`.
4. A successful energy-align zeros the drift integral (clean state for next round).

## TICKET-039 — Wide-range future fixes (researched, not yet implemented) 🔴
**Findings from web research (June 2026) — high-impact, low-risk additions for a future pass:**

- **YouTube CC track fast-path** via `youtube-transcript-api` (pure Python, no
  yt-dlp/ffmpeg). Many official lyric videos / K-pop / J-pop MVs ship MANUAL caption
  tracks containing the actual lyrics with millisecond timing — bypasses LRC providers
  entirely. Auto-captions on music are unreliable (often `[Music]`), but manual tracks
  are ground truth. Needs a URL-from-browser-tab extraction step.

- **QQ Music / KuGou** via `syncedlyrics_aio` (async fork of existing dep) — adds
  Tencent provider as a drop-in. KuGou via `ll-kugou-lyric-api` or direct endpoint
  closes the Mandopop gap. Together cover ~70% of Chinese market.

- **Phonetic matching with `jellyfish`** (pure-Rust wheel, no compiler) for Whisper
  alignment. Currently uses raw `difflib.SequenceMatcher`; switching to Double Metaphone
  + Jaro-Winkler would handle ASR noise (silent letters, homophones) far better. Big
  win when faster-whisper is bundled.

- **ytmdesktop Companion Server** at `localhost:9863/api/v1` with Socket.IO real-time
  state. Push-based sub-second `videoProgress` beats Windows Media Transport polling
  for YouTube Music Desktop users specifically. Optional listener, no harm if absent.

- **`silero-vad`** (ONNX, ~1MB) for vocal-section gating. Outperforms `webrtcvad` on
  music-mixed audio. Could improve Shazam capture hit rate by fingerprinting only
  during vocal-active windows.

- **Highlighted-line + word-wipe UI with 3-dot lookahead** (UltraStar Deluxe pattern).
  Tolerates more drift than continuous scrolling because the eye locks to the active
  word. MIREX-standard tolerance is ±300ms; current scrolling exposes drift at ~150ms.

Status: 🔴 documented as future work after the user asked for "wide-range" benefits.
None blocking, all additive.

## TICKET-037 — Niconico (and other video-site) tab suffix taken as song title 🟢
**Symptom:** Niconico karaoke video showed lyrics ~10 s out of sync no matter what.
`/status` showed `matched_title: "ニコニコ動画"` and `matched_artist: "Ahoy!! 我ら宝鐘海賊団☆"` —
the LRC fetched was the **wrong song entirely**, under "ニコニコ動画" as the title.
**Root cause:** `clean_title` only stripped `" - YouTube"` from browser tab titles. For
Niconico the tab is "Ahoy!! 我ら宝鐘海賊団☆ - ニコニコ動画". Unstripped, the empty-artist
split in `_on_track_change` (`if not artist and " - " in title:`) made
artist="Ahoy!!…" and title="ニコニコ動画" — then fetched whatever same-title hit existed.
Auto-sync had nothing right to lock onto.
**Fix (pushed, v1.0.27):** broaden the browser-suffix stripper to include
ニコニコ動画 / niconico / nicovideo / Vimeo / Bilibili / Dailymotion / Twitch / SoundCloud /
Bandcamp / TikTok alongside YouTube. Deleted the two wrong cached LRCs
(`ニコニコ動画.json`, `ahoy_我ら宝鐘海賊団_ニコニコ動画.json`).
**Verified:** post-rebuild `/status` shows `matched_title: "Ahoy!! 我ら宝鐘海賊団☆"`,
`matched_artist: "Houshou Marine"`, `heard_by_sound: [same]` (Shazam confirmed),
`sync_offset: -7.14`, `sync_drift: 0.05` — tight sync. The Whisper-free auto-sync
(TICKET-036) was working all along; it just needed real lyrics to sync against.

## TICKET-036 — "Always listening" continuous auto-sync 🟢
**Request:** Niconico karaoke video (`【ニコカラHD】 Ahoy!! 我ら宝鐘海賊団`) showed lyrics ~10 s
ahead with no auto-correction. User wants the app to "always be listening and trying to
sync lyric to place in song" — Shazam can't fingerprint an off-vocal karaoke cut, so the
existing pipeline never locks the offset.
**Root cause:** sync-by-listening (`align_by_listening`) was opt-in only — needed a
manual /align trigger. And the lean .exe ships without faster-whisper (1+ GB extra), so
even if it ran it would no-op.
**Fix (pushed, v1.0.26) — two-tier continuous auto-sync:**
1. **Background Whisper align** (when faster-whisper is available):
   - Auto-aligns ~25 s into each new track (after vocal-onset gap)
   - Background heartbeat every 45 s re-checks silently
   - Drift trigger: when Shazam reports persistent drift (>1.5 s, 3 consecutive reads),
     triggers immediately — catches karaoke / live / off-vocal cuts Shazam can't lock
   - Silent UI: "🎤 Auto-synced (+X.Xs)" only when a meaningful correction lands
2. **Whisper-free fallback via vocal-energy correlation** (the default lean build):
   - `SongChangeDetector` keeps a rolling 60 s buffer of `(t_wall, vocal_ratio)` per
     0.2 s block (already computed for vocal-onset detection in TICKET-035).
   - Main thread builds a binary "vocals on/off" mask from the buffer (ratio above
     1.25× learned baseline), and a matching LRC mask for each candidate offset in
     [-15, +15] s at 0.2 s precision.
   - Picks the offset with the highest agreement score. Requires the peak to lift
     ≥0.10 above the median of all candidates — sparse / flat masks fail this check
     and the offset isn't touched.
   - Same triggers as the Whisper path. Whisper preferred when available.
**Conservative gates** (no churn, no UI spam): skips if Shazam locked within 30 s, skips
within 25 s of last align, skips while paused / in live mode, requires ≥12 s of buffer,
≥4 vocal blocks captured, new offset within 60 s, change ≥0.4 s. State (`_last_align_t`,
`_last_sound_lock_t`, `_align_drift_strikes`) resets per track.

---

### Research summary (cross-cutting)
- **Matching:** verify artist + duration, don't trust the first hit (TICKET-002/009/034).
- **Sync:** forced alignment / vocal separation; auto sync-by-listening (TICKET-003/007).
  Whisper-free fallback via vocal-band energy cross-correlation (TICKET-036).
- **Intros:** vocal-onset via band-energy ratio (not VAD) for songs with long instrumental
  intros (TICKET-035).
- **Generation:** `condition_on_previous_text=False`, RMS-VAD, overlap chunks (TICKET-005).
- **Rendering:** full-coverage CJK font + fallback to kill tofu (TICKET-006).
- **Multi-monitor:** fingerprint-based persistence + topology watchdog (TICKET-032).
- **Covers:** original-artist extraction from title beats channel-as-artist (TICKET-034).
