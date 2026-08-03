# Concert song detection — reading the on-screen song banner

## The problem
A 3D live / concert is **one long video** with many songs back-to-back. The media
title never changes (it stays "【#ReGLOSS3Dライブ】Reach the top!"), and **Shazam is
unreliable on live arrangements** (different mix, crowd, MC breaks). So the app
can't tell *which song is playing right now* from audio alone — it shows the wrong
song or none.

## The insight
hololive / idol concert videos **print the current song's name on screen** — a
banner in a top corner: `SUPER DUPER`, `泡沫メイビー`, `LAKI MODE`, `サクラミラージュ`. That
banner is the **single most reliable signal** for the current song. We read it.

> **Read this first: on a chaptered concert, none of the pipeline below runs.**
> OCR is fully suppressed whenever a setlist already exists. `main.py:5826-5829`
> fires `_concert_ocr_check()` only when **all** of these hold: `_live_mode` is on,
> `_concert_setlist` is empty, `_live_video_nonmusic` is false, and the
> `concert_ocr` setting is enabled, plus a 6.0s throttle between reads. Because
> chapters populate `_concert_setlist` (see
> [CONCERT_AUDIO_SYNC.md](CONCERT_AUDIO_SYNC.md)), and most 3D-live uploads have
> chapters, the OCR path described here never executes at all on a typical concert.
> It is the **chapterless fallback**, not the primary identity signal. Debug OCR
> behaviour by checking that gate before assuming the reader is broken.

## Pipeline — [`concert_ocr.py`](../concert_ocr.py)
1. **Capture** and crop. The grab is the **media window's own pixels**, not a
   desktop strip: `main.py:5984` calls `read_banner_lines(hwnd=...)` with the
   source window handle, and `concert_ocr.py` captures that window (falling back
   to a full-desktop `ImageGrab` only when the hwnd is unknown). Reading the
   window rather than the desktop is deliberate (v1.1.49): the old desktop grab
   OCR'd whatever sat top-left, which turned out to be VS Code.
   The crop is the **top-LEFT** region, not the whole top strip:
   `concert_ocr.py:105-106` takes the left 60% of the width and `_TOP_FRAC = 0.26`
   of the height, which skips the top-right hashtag and the far-right chat panel.
   On a browser window the crop starts 10% down instead of at `y=0`, because
   including the tab strip made another tab's title read as a song banner
   (v1.1.56).
2. **OCR** that strip with the **built-in Windows OCR engine** (`Windows.Media.Ocr`
   via `winsdk` — already a dependency, no new package). The in-memory conversion
   segfaults, so we save a temp PNG and OCR it via `StorageFile` (reliable).
3. **Match** every recognised line against a candidate pool with a normalised
   fuzzy match. **Which pool depends on a gate** (`main.py:5995-6000`): when this
   video has a description-derived candidate list (`_concert_candidates`) *and*
   the `ocr_setlist_gate` knob is 1 (the default), the pool is **this video's own
   candidate titles**. Only when there is no candidate list, or the gate is turned
   off, does it fall back to the **whole local library** (cached lyric titles).
   The gate exists because the capture is the whole media window, so it also
   contains the YouTube search box, sidebar ads and page copy. Ungated, leftover
   search-box text scored 0.87 against a library file and hijacked a concert for
   ten minutes, and an ad title was fetched as if it were a song (TICKET-189).
   A banner line that IS a title scores ~1.0; the concert hashtag, chat names, and
   the overlay's own lyric lines don't match any *title*, so the threshold ignores
   them.
4. Return `(title, score)`; the caller acts only on `score >= 0.85`, enforced at
   `main.py:6039`. (The `"accept_at": 0.85` at `main.py:6016` is only a diagnostic
   echo written into `_finder_ocr` for the dev console. Changing that line changes
   what the console reports, not what is accepted.)

### Languages
Windows ships **en-US** OCR by default — it already reads English banners
(SUPER DUPER, LAKI MODE, BANG, …). For **Japanese** banners (泡沫メイビー / サクラミラージュ),
install the pack **once** (admin), after which the engine auto-uses it:

```powershell
Add-WindowsCapability -Online -Name "Language.OCR~~~ja-JP~0.0.1.0"
```

`concert_ocr.ocr_langs()` reports what's active. Everything degrades gracefully:
no engine, no capture, or no confident match → returns nothing and the existing
sound/title detection stands.

## Integration — [`main.py`](../main.py)
- In **live/concert mode only, and only while no setlist is known**, the
  recalibration loop fires a **throttled (6.0 s), background**
  `_concert_ocr_check()`. The full gate is at `main.py:5826-5829`: `_live_mode`
  AND no `_concert_setlist` AND not `_live_video_nonmusic` AND the `concert_ocr`
  setting. A chaptered concert satisfies none of this, so OCR never fires there.
- It gathers candidate titles (this video's candidate list when
  `ocr_setlist_gate=1` and that list is populated, else the lyric cache), reads
  the banner, and matches.
- On a confident hit (`>= 0.85`) for a *different* song, `_apply_ocr_song()` loads
  that song's lyrics (from cache, else fetches) on the Tk thread, **title-locks**
  it (OCR is authoritative in a concert, so a Shazam mis-ID on the live take can't
  override it), zeroes the offset, and kicks a short sound-listen to lock timing.
- Toggle/persist via the `concert_ocr` setting (default on).

## How it feeds the confidence score
The banner is the **highest-weight** identity signal we have. The model is:

| Signal | Weight | Notes |
|---|---|---|
| **On-screen banner OCR** (this) | **highest** | exact, visible ground truth — used directly at `>=0.85` |
| Clean media title (Spotify / Topic) | high | authoritative metadata (TICKET-016) |
| Shazam heard == loaded | medium | noisy on live/niche; needs confirmation (TICKET-002) |
| Duration / artist match | low–medium | tie-breaker, same-title guard (TICKET-009) |
| Language match | low | sanity check |

The OCR hit short-circuits the rest when present; otherwise the lower signals
combine as before.

## Known limitations & next steps
- **Japanese banners** need the ja-JP OCR pack (above). English-named songs work now.
- **Candidates = cached songs** only when the setlist gate is off or this video
  has no candidate list (see pipeline step 3). In the gated default case the pool
  is the video's own candidates, so a song absent from that list cannot be matched
  at all. Wiring the master-tracks **library DB** (TICKET-009) would widen the
  ungated pool.
- **OCR does not run on chaptered concerts at all** (gate at `main.py:5826-5829`).
  The whole reader is a chapterless fallback, so improvements here do not affect
  the common case.
- **Intermissions / MC**: when no banner + no singing, the last song's lyrics linger.
  A follow-up should clear lyrics when neither OCR nor sound finds a song.
- **Banner region** is the **top-LEFT** area today (left 60% of the width, top 26%
  of the height, starting 10% down on browser windows), deliberately cropped to
  skip the top-right hashtag and the chat panel. Per-video region learning could
  narrow it further and cut both OCR cost and false text.
