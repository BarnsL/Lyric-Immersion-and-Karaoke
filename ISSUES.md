# Desktop Karaoke — Issue Tickets

Numbered tickets for matching / sync / rendering / performance / features.
Status: 🔴 open · 🟡 in-progress · 🟢 fixed (pushed) · 🔵 needs-repro

**Verification rule:** always compare the app's line to the **video's on-screen
lyrics** at the same playback position — not just `/status`.

---

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

---

### Research summary (cross-cutting)
- **Matching:** verify artist + duration, don't trust the first hit (TICKET-002/009).
- **Sync:** forced alignment / vocal separation; auto sync-by-listening (TICKET-003/007).
- **Generation:** `condition_on_previous_text=False`, RMS-VAD, overlap chunks (TICKET-005).
- **Rendering:** full-coverage CJK font + fallback to kill tofu (TICKET-006).
- **Multi-monitor:** `screeninfo` + per-monitor Toplevels (TICKET-008).
