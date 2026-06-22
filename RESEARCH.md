# Research notes & improvement log

A review of every subsystem against current best practice and available
libraries (web research, June 2026), with what was **found**, what was **done**,
and what was **deferred** (and why). Source links at the bottom.

Each item is tagged: ✅ implemented · 📝 documented only · ⏭️ deferred.

---

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

**Net:** the free stack is already near-optimal; the only real upgrade (word
timing) isn't available for free. No code change beyond documentation.

## 2. Romanization (Japanese furigana + romaji)

- ✅ Already upgraded this session to **fugashi + UniDic + cutlet** (a real
  morphological analyzer) with pykakasi as fallback. This is the recommended
  modern approach and fixed the segmentation errors (今生きて → 今(いま)生き).
- ⏭️ **pyopenjtalk** could add pitch-accent marks (a nice learning aid) but it's
  a heavy native dependency; deferred.

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

---

## What changed in code (this pass)

| Area | Change | Where |
|------|--------|-------|
| Sync | PlaybackRate-aware position + calibration | `main.py` `MediaWatcher`, `_consume_async` |
| Perf | Reuse GSMTC session manager across polls | `main.py` `MediaWatcher._loop` |
| Translation | Use DeepL when `DEEPL_API_KEY` is set | `fetch_lyrics.py` `_make_translator` |
| Coverage | Per-line furigana/romaji + runtime self-heal (no more bare Japanese) | `fetch_lyrics.py` `annotate`/`backfill_file`, `main.py` `_maybe_translate`, `reannotate.py` |
| Docs | Word-level finding, candidates, this file | `fetch_lyrics.py` header, `RESEARCH.md` |

Everything else researched (word-level providers, GPU backend, event-driven
GSMTC, pitch accent) is intentionally deferred for the reasons above.

## Sources

- [syncedlyrics (PyPI)](https://pypi.org/project/syncedlyrics/) ·
  [README / providers](https://github.com/moehmeni/syncedlyrics/blob/main/README.md)
- [LRCLIB API docs](https://lrclib.net/docs)
- [Enhanced LRC (A2 / word-level) format](https://en.wikipedia.org/wiki/LRC_(file_format))
- [GlobalSystemMediaTransportControlsSession (PlaybackRate, Timeline)](https://learn.microsoft.com/en-us/uwp/api/windows.media.control.globalsystemmediatransportcontrolssession)
- [Tk Canvas performance (Tcl wiki)](https://wiki.tcl-lang.org/page/Tk+Performance) ·
  [Tk Canvas limitations](https://www.ancisoft.com/blog/understanding-performance-limitations-of-the-tkinter-canvas/)
- [deep-translator (DeepL/Google)](https://pypi.org/project/deep-translator/)
- [animelyrics (PyPI)](https://pypi.org/project/animelyrics/) ·
  [Miraikyun](https://miraikyun.com/) — anime/Vocaloid romaji + English (plain text)
