# Lyric Immersion and Karaoke — Architecture
<sub>*(repo: `BarnsL/Lyric-Immersion-and-Karaoke`; formerly Desktop Karaoke)*</sub>

The authoritative map of the app's parts. Each subsystem below lists its
**methods** and how each lands on a **confidence score** that decides whether the
app acts on it. This supersedes the old module-by-module listing and scattered
notes; per-function detail lives in source docstrings.

Doc map:
- **ARCHITECTURE.md** (this file) - the parts, methods, confidence model.
- **REPO_ORGANIZATION.md** - current runtime diagram, source layout, data stores.
- **SUBTITLES_MODEL_API.md** - subtitle preset/toggle behavior and model-facing API.
- **ISSUES.md** - numbered behaviour tickets (matching / sync / rendering).
- **PERFORMANCE.md** - rendering / CPU / audio-stutter tickets (PERF-###).
- **RESEARCH.md** - background research that informed the design.
- **DEPLOYMENT.md** - repo layout, the module map, build → deploy → run.
- **BUILD.md** - producing the packages and the build guards.
- **README.md** - the index of every doc in this folder.

The app is a **main Python process** (a Tkinter transparent overlay,
`main.py:Overlay`, plus background worker threads, with a local HTTP API in
`api.py` for inspection) that **spawns child processes** for the work that must
not be able to take the overlay down with it:

| Child process | Spawned by | When | Why it is separate |
|---|---|---|---|
| `overlay/lyric-overlay.exe` (Tauri GPU overlay) | `main.py` (`subprocess.Popen`, `_tauri_child`) | when the Tauri renderer is enabled | GPU rendering; polls `GET /overlay`. Tk stays visible until this child proves it is painting, and the watchdog restores Tk if it dies. |
| `recognize.py --child` (identify worker) | `main.py` (`_identify_proc`) | per sound-ID attempt | Loopback capture plus fingerprinting is GIL-heavy. Run at IDLE priority with no window, and killable on timeout without touching the overlay. |
| `whisper_worker.py` (`--whisper-worker`) | `align.py` (`start()`, over a loopback socket with a per-run token) | per Whisper listen | Crash firewall (TICKET-184). A CTranslate2 CUDA fault throws on a native worker thread and calls `abort()`, which Python cannot catch, so the model runs somewhere that is allowed to die. Pinned as a hidden import in `DesktopKaraoke.spec`. |
| dev-console exe (`dev-console/`) | `main.py` (`_devconsole_child`), from the tray | when the user opens the Developer Console | A separate Tauri desktop app that reads the localhost API. See **DEV_CONSOLE.md**. |

Only the main process is always present. The others come and go, and none of
them is required for the overlay to keep rendering.

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
| **Title cleaning** | clean_title/clean_artist, `extract_cover_original` | pulls the real song out of MV/cover/bilingual titles; covers route to the ORIGINAL artist (incl. "Song / Artist covered by X") |
| **Language confidence** | `confidence.language_confidence` (`_ALWAYS_JA`/`_KNOWN_JA`) | the artist's usual language (Suisei/ReGLOSS → full JA) rejects an English same-title collision |
| **Decide-by-ear / library match** | `align.transcribe_vocals`+`score_candidates`, `_decide_by_ear` | transcribe the live vocals (faster-whisper *small*) + rapidfuzz-match against the WHOLE cached library — identifies the song from what's SUNG when title+Shazam fail (MMD/cover cuts). Waveform-gated to vocal-active windows |

> **No lyrics ship with the app (TICKET-124).** This is a sellable product, so every
> lyric must be FOUND BY CODE at runtime (providers, YouTube captions, OCR, by-ear),
> never copyrighted text baked into the build. `bundled_lyrics/` was removed from the
> repo and `DesktopKaraoke.spec` ships nothing in its place. The `source: bundled`
> handling still exists in `main.py` (it treats such a cache as authoritative and
> exempt from the reject guards), but nothing seeds it in a shipped build, so it is
> unreachable in practice. Do not document it as a lyric source.

**Reconciliation:** in `_consume_async`, a heard song matching the loaded lyrics →
calibrate timing; a heard *different* song needs a 2nd confirming read to switch; a
`_title_locked` distinctive title ignores a one-off Shazam mis-ID of a same-artist
track, BUT hearing the same other song **5× breaks the lock** (`wrong_song_strikes`)
and switches. ~20 s in, `_decide_by_ear`
verifies the loaded lyrics actually match the singing and corrects from the library if
not. Reels/site audio (bare site name, no artist) → suppressed (`is_non_music_source`).

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
| **Energy correlation (waveform)** | cross-correlate audio vocal-band on/off mask vs LRC line intervals | small-shift prior, peak **uniqueness** (reject near-equal distant rival = chorus), lift-vs-median floor, agree-with-Shazam band. The cheap, always-on automatic method. |
| **Adaptive verify tier** | `_periodic_auto_align` runs the energy check ~3×/min while syncing, relaxes to 1×/min once confirmed, snaps back on a miss (`_note_sync_verdict`) | when energy is blind on a song it escalates to a short **two-point-verified Whisper listen** (`_tier_listen_now`); Whisper CPU capped (`cpu_threads=4`). |
| **Whisper align (waveform-gated)** | transcribe a clip, fuzzy-match lyric lines → offset | match-ratio floor scaled by jump size. Now used automatically (tier + the explicit button + live-resync), but **only when the waveform says vocals are active** (`_vocals_active_now`) so the clip isn't an instrumental break. |
| **Live-version resync** | `_live_resync_loop` ~5×/min for LIVE/concert cuts | follows the live offset through tempo shifts + applause pauses; the studio LRC timing won't hold, so it FOLLOWS the measured offset. |

**Waveform + transcript fusion:** the transcript answers *what* line is sung; the
vocal-energy waveform answers *exactly when*. After `_decide_by_ear` picks the song by
its lyrics, the energy correlation pins the precise offset for the new lyrics. Player
clock carries between corrections; captions are video-locked so the correlator is
skipped for them.

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
`character.py`, bundled `overlay/lyric-overlay.exe`; see **PERFORMANCE.md**.
Tkinter canvas is the guaranteed CPU fallback. The current GPU path is the Tauri
overlay polling `/overlay`; Python remains the timing/settings source of truth.
Scroll = `canvas.move` or Tauri belt positioning of pre-rendered/current line
state; karaoke fill uses the raw song clock while line position eases toward sync.

---

## 8. Diagnostics API (`api.py`, 127.0.0.1:8765)
`/status` `/diag` (sync state machine + FPS) `/source` `/audio` `/lyricstate`
`/tune` (live sync constants) `/display` `/subtitles` `/captions` `/align`
`/identify` `/wrong` `/nudge` `/reset` `/logs` `/overlay`.

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
