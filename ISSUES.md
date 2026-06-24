# Desktop Karaoke — Issues Tracker

Numbered log of reported problems + improvements, with status, root cause, and fix.
Status: 🔴 open · 🟡 in progress · 🟢 fixed (pushed) · 🔵 needs-info/repro

Always verify a fix by **comparing the app's lyrics to the video's on-screen lyrics**
(burned-in / official subs) at the same playback position, not just `/status`.

---

## #1 — Dance/play covers (踊ってみた etc.) generate instead of fetching real lyrics 🟢
**Symptom:** common songs (e.g. "Breaking Dimensions を踊ってみた", hololive) sit on
"Generating lyrics by ear…" and never load the real lyrics; slow + spotty.
**Root cause:** `is_cover_title` / `clean_title` only knew 歌ってみた/(cover); they MISSED
踊ってみた (dance), 演奏してみた, 弾いてみた, 叩いてみた. So those titles weren't flagged
covers → the title-first fetch never fired → fell through to generation.
**Fix:** added the dance/play cover markers to `_COVER_RE` + the `clean_title` strip
(handles the を particle: "Song を踊ってみた" → "Song"). Verified `fetch_lrc('Breaking
Dimensions', cover=True)` → 70 lines. Commit: (this change).

## #2 — False match: wrong song's lyrics shown 🔴
**Symptom:** "BANCHO【轟はじめ/ReGLOSS】" showed "Me and my girls / Turn it up now" — a
DIFFERENT song (possibly same artist 轟はじめ). Also seen earlier (feelingradation→skavla,
fixed; this is a new instance).
**Suspect:** sound-ID (Shazam) or a same-artist cached file matched the wrong song; the
match wasn't re-verified against the actual sung lyrics. Needs the master-tracks DB +
duration/lyltic cross-check to reject wrong-but-same-artist matches.
**Status:** open — needs investigation (which path picked the wrong file).

## #3 — Desync: correct lyrics, wrong timing 🔴
**Symptom:** "Deep Dive / 轟はじめ" matched the right lyrics (furigana+romaji+EN) but the
displayed line was far off the video's burned-in line ("massive desync").
**Suspect:** fetched LRC timeline vs the MV differ (intro length), and sound-sync didn't
correct it; or the offset drifted. Needs sync-precision work (see #7).
**Status:** open.

## #4 — Identification too slow 🔴
**Symptom:** long "♪ … — identifying…" before any lyrics appear, especially on covers.
**Tie-in:** #1 (covers now fetch immediately by title); plus the generate-deadline vs
fetch race. Needs: fire the cover/title fetch instantly on track-change and only
generate if it truly comes up empty.
**Status:** partly addressed by #1; revisit speed.

## #5 — Spotty / intermittent generation 🔴
**Symptom:** "pieces of lyrics then blank then pieces then blank" — generated lyrics have
big gaps. (VAD-off fix #cb8b7a3 helped but gaps remain on quiet/instrumental stretches.)
**Suspect:** chunk capture timing + no_speech_threshold dropping chunks. Research says
RMS-VAD segmentation reduces this. Most of these songs should MATCH (see #1), avoiding
generation entirely.
**Status:** open — mitigated by better matching; RMS-VAD is the real fix.

## #6 — Box / corrupt characters in the overlay 🔵
**Symptom:** boxes (□) / tofu glyphs in some rendered lyrics.
**Finding:** a scan of recent cached lyrics found NO replacement/box chars in the DATA —
so this is a FONT glyph-coverage issue in the Tk renderer (missing glyphs for some
kanji/symbols/half-width katakana), not corrupted lyrics.
**Status:** needs repro — capture the exact line that shows boxes, then pick a font with
full CJK + symbol coverage (or per-glyph fallback).

## #7 — Sync precision 🔴
**Symptom:** lyrics not tightly aligned to the audio (general request for "greater
precision in lyric syncing").
**Plan:** for matched LRC, tighten sound-sync (Shazam offset) + the recal cadence; for
generated, anchor to the audio onset. Overlaps #3.
**Status:** open.

## #8 — Multi-monitor 🔴 (feature)
**Request:** move the lyrics overlay to a CHOSEN display, and an option to MIRROR on ALL
connected displays at once.
**Plan:** enumerate monitors (ctypes), tray "Display" submenu (each monitor + "All"),
reposition the overlay, and spawn mirror windows for "All".
**Status:** open — not started.

## #9 — Use the master-tracks library DB for matching 🔴 (feature)
**Idea:** `BarnsL/Music-Migrator` `data/master_tracks.json` (ISRC → track/artist/album/
duration) is the user's real library. Fuzzy-match messy YouTube titles → clean (artist,
title, duration) → correct fetch + same-title disambiguation (#2). Helps library songs.
**Status:** CSV obtained locally; integration not started.
