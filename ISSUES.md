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
