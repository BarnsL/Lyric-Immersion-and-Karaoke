# Desktop Karaoke — Architecture

The authoritative map of the app's parts. Each subsystem below lists its
**methods** and how each lands on a **confidence score** that decides whether the
app acts on it. This supersedes the old module-by-module listing and scattered
notes; per-function detail lives in source docstrings.

Doc map:
- **ARCHITECTURE.md** (this file) — the parts, methods, confidence model.
- **ISSUES.md** — numbered behaviour tickets (matching / sync / rendering).
- **PERFORMANCE.md** — rendering / CPU / audio-stutter tickets (PERF-###).
- **RESEARCH.md** — background research that informed the design.
- **docs/** — deep dives (CONCERT_DETECTION.md, GENERATION.md).

The app is ONE process: a Tkinter transparent overlay (`main.py:Overlay`) plus
background worker threads, with a local HTTP API (`api.py`) for inspection.

```
play audio ─▶ MediaWatcher (winsdk)  ─┐
              position/title/status    │
              recognize.py (Shazam) ───┼─▶ Overlay (tkinter, transparent)
              fetch_lyrics / captions ─┘     renders synced lyrics
                      └─▶ lyrics/*.json cache
```

---

## 1. Song Identification — "what is playing?"
**Modules:** `main.py` (MediaWatcher, clean_title/clean_artist, `_on_track_change`,
`_consume_async`), `recognize.py` (Shazam), `concert_ocr.py`, `confidence.py`.

The player title is a HINT; **sound is the authority**. Methods → confidence:

| Method | Source | Confidence signal |
|---|---|---|
| **Player metadata** (SMTC) | MediaWatcher (winsdk) | `confidence.title_distinctiveness()` 0–1: generic ("Awake","Lucky Star")→low→don't lock; distinctive→high→may `_title_locked` |
| **Sound fingerprint** | `recognize.py`→shazamio | a (title,artist,offset) hit is high, but one read needs the 2-read agreement gate before it overrides a locked title |
| **Concert banner OCR** | `concert_ocr.py` (Windows.Media.Ocr) | fuzzy match on-screen song→library; accept only `score ≥ 0.85` |
| **Title cleaning** | clean_title/clean_artist, extract_cover_original | pulls the real song out of MV/cover/bilingual titles; covers route to the original artist |

**Reconciliation:** in `_consume_async`, a heard song matching the loaded lyrics →
calibrate timing; a heard *different* song needs a 2nd confirming read to switch;
a `_title_locked` distinctive title ignores a one-off Shazam mis-ID of a
same-artist track. Reels/site audio (bare site name, no artist) → suppressed
(`is_non_music_source`).

---

## 2. Lyric Sourcing — "get the right words + timing"
**Modules:** `fetch_lyrics.py` (providers + verify), `deep_transcribe.py`
(captions + by-ear), `main.py` (`load_youtube_captions`, `_begin_generation`).

Ordered by trust; each step is a confidence gate:
1. **YouTube caption track** (`fetch_captions_only`, yt-dlp) — the video's OWN
   words + timing. **Highest trust for a browser video.** Auto-fetched per song,
   single-flight, preferred over any LRC (`_apply_captions`).
2. **Provider LRC** (`fetch_lrc`): LRCLIB duration-exact → LRCLIB scored search →
   syncedlyrics (artist-keyed → cover title-only → title-only last resort).
   Gates: `verify_lrc` (length, **language-vs-title script**, duration window),
   `_strict_ok` for generic titles, romaji→kanji upgrade.
3. **Generation by ear** (`deep_transcribe`/`_generate_loop`, Whisper) — LAST
   resort; marked `***` (AI); heavy CPU.

---

## 3. Lyric Translation & Annotation
**Modules:** `fetch_lyrics.py` (`annotate`, romanizers), `gairaigo.py`.

Per line by language: **furigana** (fugashi+cutlet, literary-reading fixes,
katakana-English recovery), **romaji** (Hepburn), **pinyin**, **romaja**,
Cyrillic **transliteration**, English **translation** (deep-translator/DeepL).
Accuracy hinges on the morphological analyzer's segmentation (今生きて→今/生きて).

---

## 4. Sync by Sound — "line up lyrics to the audio"
**Modules:** `main.py` (Shazam calibration in `_consume_async`, energy correlation,
`_schedule_sync_confirm`), `align.py` (Whisper), `songchange.py` (vocal energy).

| Method | How | Confidence gate before it moves the offset |
|---|---|---|
| **Shazam offset** | shazamio returns position-into-song | **Two-point verification**: a non-zero offset is HELD, then a confirming listen ~2 s later must AGREE before committing (anti-chorus). Deadband; studio-vs-live mode; ambiguity-spread reset. |
| **Energy correlation** | cross-correlate audio vocal-band on/off mask vs LRC line intervals | small-shift prior, peak **uniqueness** (reject near-equal distant rival = chorus), lift-vs-median floor, agree-with-Shazam band. The cheap, always-on automatic method. |
| **Whisper align** | transcribe a clip, fuzzy-match lyric lines | match-ratio floor scaled by jump size. HEAVY → **explicit "Sync by listening" button + last-resort generation only**, never the automatic loop. |

Player clock carries between corrections; captions are video-locked so the
correlator is skipped for them.

---

## 5. Wrong-Song Rejection (cross-cutting)
By failure mode:
- **Same-title, wrong language** → §2 language guards: **kana title = JA** (reject
  zh/ko), **hangul = KO** (reject zh/ja), **CJK artist ≠ European**.
- **Generic title** → `title_distinctiveness` + `_strict_ok` (don't lock "Lucky Star").
- **Same-artist Shazam mis-ID** → `_title_locked` containment match.
- **Wrong cut / stale generated cache** → captions override; `/wrong` deletes the
  cache + re-identifies; covers re-fetch by original artist.
- **Site/Reel audio** → `is_non_music_source`.

---

## 6. Music-Video & Concert Detection
**Modules:** `main.py` (`is_mv_version`, `is_live_or_compilation`, MV-intro hold,
`_on_vocal_onset`), `songchange.py`, `concert_ocr.py`.

- **MV intro** — title `is_mv_version` OR auto (LRC ≪ video, first line near 0) →
  hold lyrics through the instrumental intro until the **vocal-onset** detector
  (band-energy rise) or quiet→music onset fires.
- **Concert / compilation** — `is_live_or_compilation` → ignore the event title,
  drive each song by SOUND; song-change detector fires an immediate re-ID on a
  silent gap; **concert OCR** reads the on-screen banner.

---

## 7. Rendering & Performance
**Modules:** `main.py` (`_tick`/`_ticker_update`, layer-composite fill),
`character.py`; see **PERFORMANCE.md**. Tkinter canvas (CPU). Scroll = `canvas.move`
of pre-rendered PIL blocks; karaoke fill = composite a sung layer over base via a
full-glyph mask. Open: PERF-101 single-strip, PERF-100 moderngl GPU overlay.

---

## 8. Diagnostics API (`api.py`, 127.0.0.1:8765)
`/status` `/diag` (sync state machine + FPS) `/source` `/audio` `/lyricstate`
`/tune` (live sync constants) `/captions` `/align` `/identify` `/wrong` `/nudge`
`/reset` `/logs`.

---

## Appendix — what "energy" means here
"**Energy**" = the **acoustic loudness / spectral energy of the audio**, used two ways:
1. **Vocal-band energy ratio** (`songchange.py:_vocal_ratio`) — the fraction of an
   audio block's spectral energy in the **200–3000 Hz vocal band** (one FFT per
   0.2 s block). High ratio + low spectral **flatness** (tonal, not broadband game
   noise) = "vocals are sounding now" → a per-block **vocals on/off** mask.
2. **Energy correlation** (§4) — slide that on/off mask against the LRC's
   line-active intervals; the shift of best overlap is the sync offset. "Sync by
   energy" = aligning *when there's singing* to *when the lyrics say there should
   be* — no transcription, no network, cheap enough to run continuously. The
   Whisper-free workhorse of §4.
