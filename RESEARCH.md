# Research notes & improvement log

A review of every subsystem against current best practice and available
libraries (web research, June 2026), with what was **found**, what was **done**,
and what was **deferred** (and why). Source links at the bottom.

Each item is tagged: ✅ implemented · 📝 documented only · ⏭️ deferred.

---

## 0. Display correctness (the recurring "pushed down / boxes" bugs)

These were reported repeatedly, so the **root causes** are documented here.

- ✅ **Lyrics drifting down — ROOT CAUSE.** The overlay window was sized to the
  content and re-anchored each song (`H` changed in `_relayout_song`, position
  via `_geom_y`). Bottom-anchored, a shorter window sits lower; and the runtime
  **backfill adds romaji/English rows mid-song**, which grew the blocks and
  re-ran layout, so the window physically jumped **down** a few seconds in.
  Earlier patches (work-area sizing, a lane-trimming watchdog that *moved* the
  window) only reduced it. **Fix:** make the window a **fixed, full-work-area,
  click-through** surface (`WS_EX_TRANSPARENT`) that never moves or resizes;
  position content inside it with `_lane_y0` (bottom-anchored content stays
  pinned and grows upward). The watchdog no longer moves anything. Bottom-anchored
  lyrics also keep a `_bottom_clear` gap (~10% of height) so they sit **above** a
  media player's now-playing bar instead of hugging the screen edge.
- ✅ **Boxes (□) instead of letters — ROOT CAUSE.** Korean (Hangul) and some
  Chinese glyphs aren't in Yu Gothic, so they rendered as tofu. **Fix:**
  per-script fonts (`_script_of` → Malgun Gothic for Korean, Microsoft YaHei for
  Chinese, Yu Gothic for Japanese), chosen per line.
- ✅ **Japanese read as Chinese (pinyin) — ROOT CAUSE.** A kanji-only line
  detects as `zh`, and a kanji-heavy J-pop/VTuber song (花譜/KAF) could push the
  whole song to `zh` → pinyin instead of furigana. **Fix:** `_song_lang` treats
  **any kana anywhere** as decisive proof of Japanese (Chinese never uses kana).
- ✅ **Spanish not translated — ROOT CAUSE.** The Spanish detector only listed
  accented words (`cómo`), but corridos are written without accents (`como`), so
  songs fell through to `other` and skipped translation. **Fix:** expanded the
  Spanish word list with common unaccented forms; corridos now detect as `es`
  and get English.
- ✅ **Responsive to display size.** `_auto_scale` scales text to the work-area
  height so a big TV / 4K screen gets larger lyrics automatically.

## 0b. Performance (CPU / RAM) — kept every feature

A pass to lower idle and active cost without removing anything:

- ✅ **No PhotoImage churn in scroll mode.** The karaoke fill used to allocate a
  brand-new `ImageTk.PhotoImage` every time the sung-character count advanced
  (many times per line, per lane). Now `_paint_block_img` returns a PIL image
  and the ticker **pastes it into the existing PhotoImage** in place — no
  allocation, no GC pressure, and it drops the per-step `itemconfig` call too.
- ✅ **Cheaper media polling.** `MediaWatcher` polled GSMTC every 0.1 s; position
  is extrapolated between polls, so 0.15 s keeps timing accuracy while cutting
  that thread's CPU ~33%.
- ✅ **`measure_text` cache.** Width depends only on (text, font); the non-scroll
  renderer measured every character by creating/deleting a throwaway canvas
  item. Now cached.
- ✅ **Character idles cheap.** The companion animates at ~30 fps while dancing
  but drops to ~10 fps when paused/stopped.
- 📝 Already-good: image blocks scroll one bitmap (not 100s of items), repaint is
  throttled to fill changes, the GSMTC session manager is reused, per-song lane
  count is minimized, and the render fps is user-selectable (Smooth/Performance).

## 0c. English spaces squished — ROOT CAUSE

- ✅ English phrases lost their spaces ("Summer sun" → "Summersun"). Cause:
  `to_furigana` runs text through fugashi, which **drops whitespace** between
  tokens, and the squished line then poisoned the romaji derived from it. **Fix:**
  `to_furigana` now processes each whitespace-separated chunk and rejoins with
  the original spaces. Already-squished cache lines were re-spaced once with
  `wordninja` (a migration only — not a runtime dependency).

## 1. Lyric sources

**Current stack:** LRCLIB first (duration-exact `/api/get`, then scored
`/api/search`) → `syncedlyrics` (Musixmatch / NetEase / Megalobiz / Genius),
every result verified by duration + language. See `fetch_lyrics.py`.

- 📝 **LRCLIB is still the best free synced source** (~3M lyrics, open, returns
  metadata so matches can be verified). Keep it primary.
- 📝 **syncedlyrics provider set** (2026): Musixmatch, Lrclib, NetEase,
  Megalobiz, Genius (plain). Deezer + Lyricsify are currently broken upstream.
  Our verified-fetch wrapper already uses these correctly.
- ⏭️ **Word-by-word (enhanced / A2 “ELRC”) timing** — the headline finding.
  `syncedlyrics.search(..., enhanced=True)` *can* return `<mm:ss.xx>` per-word
  tags, but the **free providers do not** for our content. Tested live:
  `紅蓮華`, `アイドル`, `KICK BACK` (NetEase) and `Blinding Lights`,
  `Shape of You` (Musixmatch-free) **all returned line-level only**. Genuine
  word-level lyrics live in QQ Music (`qrc`), Kugou (`krc`), NetEase (`yrc`)
  and Apple Music — each behind a reverse-engineered or token endpoint. So a
  word-level karaoke pipeline would be **dead code today**; deferred until one
  of those providers is wired. The renderer interpolates the fill across each
  line in the meantime (looks good for the vast majority of songs).
- 📝 **Candidate providers** (now noted in `fetch_lyrics.py` header): PetitLyrics
  (JP), QQ/Kugou (CJK word-level), Apple Music (token), BetterLyrics (TTML),
  animelyrics.com / Miraikyun (anime/Vocaloid with ready-made romaji + English,
  but plain text only — no karaoke timing).
- ✅ **"Bare Japanese" fix.** Some songs displayed Japanese with no romaji /
  translation. Cause: `annotate()` only romanized when the *whole song's*
  language was `ja`, so a Japanese line inside a mostly-English (or
  mis-detected) song stayed bare. Fixed by romanizing **per line** by each
  line's own script. This is better than pulling a foreign romaji/translation
  source: the local analyzer + translator cover *every* song, not just charted
  anime, and stay consistent with the rest of the library. Three layers now
  guarantee it: `annotate()` (fetch), `reannotate.py` (cache — found 5 mixed
  files), and `backfill_file()` + `_maybe_translate` (runtime self-heal).
- ✅ **Romaji-upload upgrade (this pass) — ROOT CAUSE.** A Japanese song could
  show **romaji only, no kanji and no translation** (e.g. *Into Starlight* / IA).
  Cause: many LRCLIB uploads of Japanese tracks are a **romaji transliteration**
  ("sora kara maiorite" for 空から舞い降りて). The English title ("Into Starlight")
  meant the language gate didn't fire, the romaji LRCLIB hit matched first, and
  `fetch_lrc` returned it — so `lang` came out `other`: no furigana (it's already
  Latin), no translation (not a recognised language). Verified live that **NetEase
  carries the full kanji/kana original** while LRCLIB and Musixmatch only had
  romaji. **Fix:** `_looks_romaji()` detects romanized Japanese (mora-shaped words
  + unmistakable JP tokens, guarded against vowel-rich Romance text by
  `detect_lang`), and `fetch_lrc` now **stashes a romaji hit and upgrades to the
  kanji/kana original** (`_synced_cjk`, NetEase-first) before settling. Only if no
  original exists anywhere does it keep the romaji — and even then a new
  `ja-romaji` language tag routes it through translation, so it's at least
  romaji + English, never romaji alone. Re-fetch fixed both affected library
  files (*Into Starlight*, *unravel*) to full JP + romaji + English.

**Net:** the free stack is already near-optimal; the only real upgrade (word
timing) isn't available for free. No code change beyond documentation.

- 📝 **Coverage reality check (2026-06).** Re-tested when a niche VTuber B-side
  (ピーナッツくん — TIME TO LUV) returned nothing. Findings:
  - **Musixmatch is currently 401-ing for ALL queries** inside syncedlyrics
    (even 紅蓮華/LiSA) — an upstream token breakage that quietly removes one of
    the biggest providers. syncedlyrics 1.0.1 (latest) doesn't fix it. LRCLIB +
    NetEase still work (the ~300-song cache proves it), so coverage is still
    good, just not as good as it should be. Worth periodically retrying or
    pinning a syncedlyrics version where Musixmatch works.
  - Truly niche VTuber/indie songs (a B-side like TIME TO LUV) have **no
    lyrics — synced OR plain — on any accessible provider**. That's a content
    gap, not a bug.
  - ⏭️ **PetitLyrics** (best JP catalog) is a **commercial SyncPower API** with
    no open docs/keys — not a clean drop-in. QQ Music / Kugou need
    reverse-engineered, signed endpoints and are Chinese-catalog anyway.
  - 💡 **Best next step for "no lyrics" songs: a manual-LRC path** — let a user
    drop a `.lrc`/`.json` next to the app for any song the providers miss; the
    annotator already turns raw lyrics into furigana/romaji/translation. This
    solves the niche-song gap deterministically without a fragile new scraper.

## 2. Romanization (Japanese furigana + romaji)

- ✅ Already upgraded this session to **fugashi + UniDic + cutlet** (a real
  morphological analyzer) with pykakasi as fallback. This is the recommended
  modern approach and fixed the segmentation errors (今生きて → 今(いま)生き).
- ✅ **Katakana English → English, not phonetic.** A phonetic romanizer turns
  ベイビーアイラブユー into "beibiiairabuyuu". Fix: enable cutlet's
  foreign-spelling mode (コンピューター→computer) AND add `gairaigo.py` (a curated
  katakana→English table) + `_segment_katakana()` to split run-together
  loanwords and override cutlet's misses (it gives アイ→"eye", ミー→"Mi-",
  グッバイ→"Gubbai"). Result: ベイビーアイラブユー → "baby I love you". The table
  is plain data, so coverage grows by appending pairs — no code change.
- ✅ **Context-aware translation.** Translating line-by-line loses the subjects
  and pronouns that only make sense from neighbouring lines. `_translate_lines`
  now sends each block of lines together with a couple of context lines
  before/after, keeping only the focus lines' results.
- ⏭️ **pyopenjtalk** could add pitch-accent marks (a nice learning aid) but it's
  a heavy native dependency; deferred.

## 6. On-screen dancing character

- ✅ **Toggleable companion** (`character.py`, tray → "Dancing character"): a
  small, draggable, click-through avatar themed to the **detected song's
  artist** that bobs/sways while music plays and hops when clicked.
- 📝 **Why a procedural avatar, not real VTuber models.** The request was for
  high-quality models of specific groups (ReGLOSS, V.W.P, hololive…). Those are
  **copyrighted** and aren't freely downloadable or redistributable, so the app
  cannot ship them. The avatar is drawn procedurally and artist-themed instead.
  A drop-in path is provided: a user-supplied `characters/<artist>.png` is used
  if present. A fully rigged VRM/Live2D avatar would need a real 3D/Live2D engine
  in a webview — a large dependency, ⏭️ deferred and documented as the path.

## 3. Synchronization

**Current:** Windows `GlobalSystemMediaTransportControls` (GSMTC) gives the real
playback position; a Shazam offset corrects per-song bias (MV intros / LRC skew)
with a fast-lock burst after each song change.

- ✅ **PlaybackRate-aware clock.** GSMTC exposes `PlaybackRate`, which is ≠ 1.0
  whenever the user speeds up / slows down playback (extremely common on
  YouTube). Position extrapolation **and** the Shazam calibration now multiply
  elapsed wall-time by the rate, so the overlay stays in sync at 1.25× / 0.75×
  etc. (`MediaWatcher._loop` / `.get`, `_consume_async`).
- ✅ **Reuse the session manager.** The watcher was calling
  `MediaManager.request_async()` every 0.1 s; it now requests once and reuses
  it (re-requesting only after an error). Less overhead per poll.
- ✅ (earlier this session) **Fast-lock burst + short re-sync captures + tighter
  correction** — see the sync commit.
- ⏭️ **Event-driven** updates (`PlaybackInfoChanged` / `TimelinePropertiesChanged`)
  instead of a 0.1 s poll — marginally lower CPU, but the poll is already cheap
  and simpler/robust. Deferred.

### 3b. Seamless switching in compilations (this pass) — ROOT CAUSE + fix

**Problem.** In one long video with many songs back-to-back ("openings 1-26", an
album upload, a DJ set), the player's GSMTC **title never changes**, so the only
signal a new song started is the **audio**. The app caught these only via the
blind `_recalibrate_loop` Shazam poll, so a switch lagged ~one poll + a capture
(≈8-12 s of the *previous* song's lyrics over the new one), and polling Shazam
every few seconds for a whole compilation is wasteful (each call is a multi-second
capture + fingerprint + network round-trip).

**Fix — an energy-gated boundary detector (`songchange.py`).** A song boundary in
a compilation almost always shows up as a brief **near-silent gap** between
tracks. We watch the system-audio loudness with a tiny RMS meter (short, low-rate
blocks — a few wake-ups a second, trivial CPU) and detect the change-point shape
*music → gap → music*, then trigger an immediate re-identify. This is the
lightweight, real-time end of the **audio-segmentation / change-point-detection**
literature (energy-threshold activity detection à la `auditok`; change-point
detection à la `ruptures`) — we deliberately use the cheap energy-gate, not a
model, so it adds ~nothing to CPU. Conservative thresholds (absolute **and**
relative silence floor, a minimum gap length, a "was preceded by music" latch, a
post-fire debounce) keep a quiet musical passage from false-triggering.

- ✅ **Faster switches** — the swap now happens within a second or two of the
  real boundary (Shazam latency is the floor), not after the next blind poll.
- ✅ **Lower CPU/network** — because changes are now event-driven, once a song is
  confirmed the blind Shazam poll **relaxes to a slow safety heartbeat** instead
  of firing every few seconds for the length of the compilation.
- 📝 **Honest limit** — a *crossfaded* compilation with no inter-track gap won't
  trip the silence gate; the slow Shazam heartbeat remains the backstop. A pure
  spectral-novelty detector would catch those too but costs real CPU, so it's
  deferred. Tray-toggleable ("Fast song-change detect").

### 3c. "Artist / Song -ver-" MV titles didn't match cached lyrics — ROOT CAUSE + fix

**Symptom.** *IA & ОИЕ / Into Starlight -anniversary special ver.- (MUSIC VIDEO)*
showed **"No lyrics found"** even though the song was cached (correctly, with
kanji+romaji+English) and the title is on every provider.

**Root cause.** Two compounding things:
1. **Title wrapping.** JP music videos title themselves `Artist / Song -version-
   (TAGS)`. `clean_title` stripped `(TAGS)` but not the `Artist /` prefix or the
   `-anniversary special ver.-` subtitle, so the query normalised to
   `iaoieintostarlightanniversaryspecialver`. The cached `intostarlight` is only
   **34 %** of that string — below the matcher's deliberate **≥60 %** paranoia
   threshold (which exists to stop different-songs-same-artist false matches) — so
   it scored 0. Shazam couldn't fingerprint this "anniversary special ver." cut
   either (`heard_by_sound: null`), so there was no sound fallback.
2. **Corrupt landmine files.** Two cache files had **title == artist**
   (`yoasobi.json`/`YOASOBI`, `lyolite.json`/`Lyolite`) — bad earlier saves where a
   mangled YouTube title was the channel name. These false-match *every* video by
   that artist (the artist substring clears 60 %).

**Fix.**
- `clean_title` now also strips a trailing dash-delimited **version/edit subtitle**
  (`-… ver.-`, `-Remix-`, `-Acoustic-`, `-anniversary special ver.-`, …).
- `_title_forms()` adds, for an `Artist / Song` title, the **song part only** (the
  segment after the last `/`, ≥4 norm chars) as a match form — so the wrapped MV
  title matches the cached song. The leading artist segment is deliberately *not*
  tried (it would match an artist-named file), and `LyricsIndex.match` uses these
  forms. Result: *Into Starlight* → `into_starlight.json`; *YOASOBI / Idol* →
  `idol.json` (the song), not the artist.
- Deleted the 2 corrupt files and added a guard in `fetch_and_save`: never cache a
  song whose **title == artist** (it indexes garbage that false-matches everything).
- 📝 **Honest limit (sync).** This MV is 5:50 vs the single's 5:36; the cached LRC
  is timed to the single and Shazam can't ID the special-ver cut, so the lyrics
  show but the **timing needs a manual nudge** (tray *Sync timing* / `POST /nudge`).
  No automatic fix without a fingerprint of that exact cut.
- 📝 **Still-open formats (pre-existing, not regressions):** `Artist "Song"`
  (song in quotes) and `Artist MV「Song」` (song in 「」, which the bracket-strip
  removes) don't yet extract the song; candidates for a later `clean_title` pass.

## 4. Rendering & performance

**Current:** a transparent, click-through Tk canvas. Scroll-through renders each
line as **one `PhotoImage`** (image block) and moves the whole stream in a single
call; lanes + block height adapt per song.

- 📝 **Tk Canvas is inherently CPU-bound and O(n) per item** (no GPU, no batch
  rendering). Best-practice mitigations are exactly what we already do: reuse
  items, group with tags, batch moves, and rasterize many glyphs into one image.
  The per-song adaptive layout (this session) further cuts item count.
- ⏭️ **GPU backend.** A step-change in rendering performance would require
  leaving Tk for a hardware-accelerated surface — **PyQt/PySide `QGraphicsView`**
  or a layered **Direct2D** window. That's a substantial rewrite of the overlay
  shell, so it's deferred and recorded here as the future path if CPU ever
  becomes a problem. Today's frame budget is fine (16 ms loop, light per-frame
  work).

## 5. Translation (→ English)

- ✅ **Optional DeepL.** `deep-translator` still defaults to the free Google
  endpoint (no key needed), but if a `DEEPL_API_KEY` environment variable is
  present the fetcher uses **DeepL**, which is noticeably better for JP/CJK→EN.
  (`fetch_lyrics._make_translator`.)

## 7. Aligning lyrics to the HEARD audio (transcription vs sonic matching)

**The problem.** Sync today is GSMTC position + a Shazam **offset** that calibrates
when Shazam can identify the playing track. When it *can't* — a fan MV, a remix, an
"anniversary special ver." with a different intro length (e.g. *Into Starlight*,
5:50 vs the 5:36 single) — there's no anchor, so the cached LRC timestamps don't
line up and the only fix is a manual nudge. We want to align the lyrics to *what's
actually being heard*, not to a catalog match. Surveyed two families.

### Family A — transcription anchoring (ASR; **no reference audio needed**)
Capture a few seconds of the live vocals, **transcribe** them, then fuzzy-match the
transcript against the song's *already-cached* lyric lines to find which line is
playing now → `offset = cached_line_time − live_position`. The key insight: we
don't need an accurate transcript, only enough to **locate** the current line among
known lyrics (a forgiving fuzzy-match), so noisy sung-vocal ASR is acceptable.

- **Tooling.** [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper)
  (CTranslate2; ~2× faster than openai-whisper on CPU, int8 quant, most portable),
  [`whisper-timestamped`](https://github.com/linto-ai/whisper-timestamped)
  (DTW/cross-attention word timing — more robust than stable-ts's token-prob
  timestamps), [`WhisperX`](https://github.com/m-bain/whisperx) (adds VAD +
  wav2vec2 word alignment + optional **Demucs vocal isolation**, heavier).
- **Why it fits us.** It matches *content* (words) to *our* cached lyrics, so it
  works for any cut/intro/tempo and needs **no catalog** — exactly the case Shazam
  fails. The `tiny`/`base` model on CPU is feasible for an **occasional** anchor.
- **Costs / honest limits.** Adds a real dependency (faster-whisper + a ~40–150 MB
  model) and CPU; sung Japanese over backing music is hard for ASR (mitigated by
  the fuzzy anchor, and further by Demucs vocal isolation — but Demucs is heavy).
  Best run **on demand / at song start**, not continuously.

### Family B — sonic alignment (audio↔audio cross-correlation; **needs reference audio**)
[`audio-offset-finder`](https://github.com/bbc/audio-offset-finder) (MFCC
cross-correlation, ~0.01 s accuracy), [`audalign`](https://pypi.org/project/audalign/),
or chroma cross-correlation find the offset between a **reference** recording and
the live capture extremely accurately and cheaply. But we only cache LRC **text**,
not audio — and storing per-song reference audio is a storage + copyright problem.
This is essentially what Shazam already does; the technique isn't the gap, the
*missing reference for the specific cut* is. (Idea: when Shazam *does* confirm a
song, stash a short chroma "anchor" at a known time and cross-correlate against it
next time — but a different cut may not match it. Limited.)

### Real-time research systems
On-line audio-to-lyrics alignment exists ([Park et al. 2024 — chroma + phonetic for
classical/opera](https://laurenceyoon.github.io/real-time-lyrics-alignment/);
[on-line alignment vs a reference performance, arXiv:2107.14496](https://arxiv.org/pdf/2107.14496))
but targets isolated/classical voice with a reference, not mixed pop over loopback.

### Recommendation — phased, opt-in
1. **Phase 1 (best effort/ROI): on-demand Whisper anchor.** A tray action + API
   `POST /align` ("sync by listening"): capture ~8–10 s, `faster-whisper` (`base`,
   int8, CPU) → fuzzy-match (rapidfuzz) the transcript to cached lines → set the
   offset. One-shot, opt-in, no continuous CPU. Directly fixes unidentifiable cuts.
   Reuses the existing loopback capture (`recognize.py`).
2. **Phase 2 (quality): Demucs vocal isolation** before ASR, and optional periodic
   re-anchor to track drift. Much heavier — opt-in / GPU-friendly only.
3. **Phase 0 (cheap stop-gap): vocal-onset offset.** Reuse the RMS/VAD from
   `songchange.py` to estimate the first sustained vocal onset and align it to the
   first lyric line — catches the common "MV has a longer intro" case with no new
   deps, though it's only a rough start anchor.

**Verdict:** Family A / Phase 1 is the right first build — it's the only approach
that aligns to the heard sound *without a catalog or reference audio*, which is the
actual failure mode. Keep it **opt-in** (dependency + CPU weight) and **on-demand**.

✅ **Phase 1 BUILT** (`align.py`): tray **🎤 Sync by listening** + `POST /align`
capture ~9 s → `faster-whisper` (`base`, int8, CPU) → transcribe → fuzzy-match
(difflib) the transcript to the cached lines → set the offset. Validated: the
anchor+offset math is exact on a noisy synthetic transcript, and the ASR pipeline
loads + runs (~0.5 s on a short clip; model cached to `<data>/models`). Packaging:
a "lean EXE + loose-vendored `deps/`" approach **failed in the frozen app** (PyAV's
`av._core` can't find its FFmpeg DLLs from a non-bundled path), so the stack is
**bundled via PyInstaller's hooks** (`pathex=.deps` + `collect_all`), which place
the ctranslate2/av DLLs correctly. From source it auto-loads from `.deps`
(`_ensure_deps_path`); either way it degrades gracefully (`available()` False →
hint) when absent. Phase 0/2 remain future work.

✅ **Generate-by-ear (built, v1.0.2)** — the same Whisper stack, pushed further:
when **no provider has the song at all**, transcribe the audio itself into the
lyrics. `align.transcribe_for_generation` captures the song in ~16 s chunks and
runs the **bigger `small` model with `vad_filter=True`** (skip the instrumental
gaps → cleaner Japanese) and in-chunk context; `main._generate_loop` offsets the
segment times onto the song clock, annotates each chunk (furigana/romaji/
translate), appends **`***`** to every translation as an honesty marker, and
accumulates + **saves** the file so a replay is instant and perfectly synced.
Honest limits: sung-ASR is imperfect and the first pass lags ~20 s (chunked); it's
a genuine last resort, gated behind `generate_on` + faster-whisper. A future
quality lever is **Demucs vocal isolation** before ASR (heavy; deferred).

---

## What changed in code (this pass)

| Area | Change | Where |
|------|--------|-------|
| Sync | PlaybackRate-aware position + calibration | `main.py` `MediaWatcher`, `_consume_async` |
| Perf | Reuse GSMTC session manager across polls | `main.py` `MediaWatcher._loop` |
| Translation | Use DeepL when `DEEPL_API_KEY` is set | `fetch_lyrics.py` `_make_translator` |
| Coverage | Per-line furigana/romaji + runtime self-heal (no more bare Japanese) | `fetch_lyrics.py` `annotate`/`backfill_file`, `main.py` `_maybe_translate`, `reannotate.py` |
| Display | Fixed click-through window (no drift), per-script fonts (no boxes), kana⇒ja, Spanish detection, responsive scaling | `main.py` `_relayout_song`/`_script_of`/`_auto_scale`, `fetch_lyrics.py` `_song_lang`/`_ES_WORDS` |
| Companion | Optional tray-toggled dancing character themed to the artist | `character.py`, `main.py` |
| Spaces | `to_furigana` preserves whitespace (fugashi dropped it); cache re-spaced | `fetch_lyrics.py` `to_furigana` |
| Perf | PhotoImage paste-in-place, 0.15s poll, measure_text cache, idle char fps | `main.py`, `character.py` |
| Matching | Title match is strict + scored (no loose same-artist grabs); sound is the authority and re-checks every ~20s and self-corrects | `main.py` `LyricsIndex.match`, `_consume_async` |
| Matching | Handle `Artist / Song -ver-` MV titles: strip dash-version subtitles, match the song segment after the last `/`; drop + block corrupt `title==artist` cache files | `main.py` `clean_title`/`_title_forms`/`match`, `fetch_lyrics.py` `fetch_and_save` |
| Automation | Local HTTP API (hardened: total error-wrapping, `{ok}` shape, `/health`, auth token) + rolling `karaoke.log` of every decision | `api.py`, `main.py` |
| Switching | Energy-gated **song-change detector** for seamless switching in compilations; blind Shazam poll relaxes to a slow heartbeat once confirmed (lower CPU) | `songchange.py`, `main.py` `_on_boundary`/`_recalibrate_loop` |
| Sync | **Sync by listening** — opt-in faster-whisper transcribes live vocals + fuzzy-matches the cached lines to set the offset when Shazam can't ID the cut; not bundled, auto-loaded from `deps/` | `align.py`, `main.py` `align_by_listening`, `api.py` `/align` |
| Lyrics | **Generate by ear (last resort)** — when no provider has the song, transcribe the audio in chunks with Whisper `small` (VAD-filtered) → timed JP → furigana/romaji/translate, each line marked `***`; accumulates + saves so a replay is synced | `align.py` `transcribe_for_generation`, `main.py` `_begin_generation`/`_generate_loop`/`_apply_generated` |
| Languages | German + Russian (Cyrillic transliteration + translation); per-line CJK font on rm/en rows kills mixed-line □ boxes; whitespace-safe furigana | `fetch_lyrics.py`, `main.py` |
| Polish | All subprocess calls windowless (no flashing terminals) | `main.py` |
| Docs | Word-level finding, candidates, this file | `fetch_lyrics.py` header, `RESEARCH.md` |

Everything else researched (word-level providers, GPU backend, event-driven
GSMTC, pitch accent) is intentionally deferred for the reasons above.

## 8. "Popular JP/VTuber songs generate" — it was MATCHING, not the database (2026-06)

A recurring complaint was that popular songs (KizunaAI *white balance* — 2M views,
*LOVESHII*, 大神ミオ *Howling*) **generated** lyrics, with the assumption that we needed
a better / more Japanese lyric database. **Tested and disproven:** the existing providers
already carry them —

```
fetch_lrc("white balance", "Kizuna AI") → 32 lines
fetch_lrc("LOVESHII", "Kizuna AI")      → 47 lines
```

— they were only missed because the **title/artist we searched with was wrong**:
`"KizunaAI - white balance"` (Artist-Song hyphen) and artist `"Kizuna AI - A.I.Channel"`
(channel suffix). Fixing `clean_title` (strip a leading artist credit before ` - ` when it
matches the artist) and `clean_artist` (strip `- …Channel`) made them fetch. **Conclusion:
for this catalog, invest in MATCHING (clean title + correct artist), not in adding
databases.** Musixmatch + NetEase + LRCLIB already cover most J-pop / anime / VTuber.

### Audio fingerprinting (Shazam) — keep it; alternatives don't move the needle
Shazam (`shazamio`) is **global**, not US-only — its catalog has strong J-pop / anime / a
lot of VTuber. The cases it misses are **genuinely niche covers and LIVE arrangements**,
which **no fingerprinter solves** (they aren't in any catalog): ACRCloud / AudD are
commercial with the same catalog ceiling; AcoustID/Chromaprint is free but MusicBrainz has
thin VTuber coverage. The real answers to those gaps are the **on-screen banner OCR**
(TICKET-022, [docs/CONCERT_DETECTION.md](docs/CONCERT_DETECTION.md)) and **generation**,
not a different fingerprinter. Candidate *lyric* providers for truly-niche anime, if ever
needed: [animelyrics API](https://github.com/colorfusion/animelyrics), PetitLyrics
(commercial), uta-net (scrape).

## Sources

- [syncedlyrics (PyPI)](https://pypi.org/project/syncedlyrics/) ·
  [README / providers](https://github.com/moehmeni/syncedlyrics/blob/main/README.md)
- [LRCLIB API docs](https://lrclib.net/docs)
- [Enhanced LRC (A2 / word-level) format](https://en.wikipedia.org/wiki/LRC_(file_format))
- [GlobalSystemMediaTransportControlsSession (PlaybackRate, Timeline)](https://learn.microsoft.com/en-us/uwp/api/windows.media.control.globalsystemmediatransportcontrolssession)
- [Tk Canvas performance (Tcl wiki)](https://wiki.tcl-lang.org/page/Tk+Performance) ·
  [Tk Canvas limitations](https://www.ancisoft.com/blog/understanding-performance-limitations-of-the-tkinter-canvas/)
- [deep-translator (DeepL/Google)](https://pypi.org/project/deep-translator/)
- [auditok — energy-based audio activity detection / segmentation](https://github.com/amsehili/auditok) ·
  [ruptures — change-point detection (music segmentation example)](https://github.com/deepcharles/ruptures) — the approach behind `songchange.py`
- [animelyrics (PyPI)](https://pypi.org/project/animelyrics/) ·
  [Miraikyun](https://miraikyun.com/) — anime/Vocaloid romaji + English (plain text)
- **Lyric↔audio alignment (§7):**
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) ·
  [whisper-timestamped](https://github.com/linto-ai/whisper-timestamped) ·
  [WhisperX](https://github.com/m-bain/whisperx) (VAD + word align + Demucs) ·
  [audio-offset-finder](https://github.com/bbc/audio-offset-finder) ·
  [audalign](https://pypi.org/project/audalign/) ·
  [Real-time lyrics alignment (Park et al. 2024)](https://laurenceyoon.github.io/real-time-lyrics-alignment/) ·
  [On-line audio-to-lyrics alignment (arXiv:2107.14496)](https://arxiv.org/pdf/2107.14496)
