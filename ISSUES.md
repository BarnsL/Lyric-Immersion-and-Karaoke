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

## TICKET-002 — Same-title collision: wrong song's lyrics get cached + reused 🔴
**Symptom:** "BANCHO / 轟はじめ" showed another song's lyrics. Identification was correct
(`heard BANCHO | loaded BANCHO | match=True`) — the cached *content* was a wrong
"BANCHO" fetched from a same-title collision.
**Research:** lyric finders stress **verifying the artist + not trusting the first
match** — "compare the artist… before you save it, the first match is not always the
right version." ([Musely](https://musely.ai/tools/lyrics-finder), [Chosic](https://www.chosic.com/find-song-by-lyrics/))
**Plan:** cross-check the fetched LRC's length against a **trusted duration** (the
master-tracks library DB, TICKET-009) and the artist; reject same-title/wrong-duration
hits instead of caching them. Deleted the bad BANCHO cache for now.

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

---

### Research summary (cross-cutting)
- **Matching:** verify artist + duration, don't trust the first hit (TICKET-002/009).
- **Sync:** forced alignment / vocal separation; auto sync-by-listening (TICKET-003/007).
- **Generation:** `condition_on_previous_text=False`, RMS-VAD, overlap chunks (TICKET-005).
- **Rendering:** full-coverage CJK font + fallback to kill tofu (TICKET-006).
- **Multi-monitor:** `screeninfo` + per-monitor Toplevels (TICKET-008).
