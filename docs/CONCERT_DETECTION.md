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

## Pipeline — [`concert_ocr.py`](../concert_ocr.py)
1. **Capture** the screen (PIL `ImageGrab`) and crop the **top ~26%** (where the
   banner sits).
2. **OCR** that strip with the **built-in Windows OCR engine** (`Windows.Media.Ocr`
   via `winsdk` — already a dependency, no new package). The in-memory conversion
   segfaults, so we save a temp PNG and OCR it via `StorageFile` (reliable).
3. **Match** every recognised line against the **known song library** (the local
   lyric cache today; the master-tracks DB later) with a normalised fuzzy match.
   A banner line that IS a title scores ~1.0; the concert hashtag, chat names, and
   the overlay's own lyric lines don't match any *title*, so the threshold ignores
   them.
4. Return `(title, score)`; the caller acts only on `score >= 0.85`.

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
- In **live/concert mode only**, the recalibration loop fires a **throttled (~6 s),
  background** `_concert_ocr_check()`.
- It gathers candidate titles from the lyric cache, reads the banner, and matches.
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
- **Candidates = cached songs** for now (so preload the concert's songs first, or
  via Import Playlist). Wiring the master-tracks **library DB** (TICKET-009) lets it
  match songs not yet cached.
- **Intermissions / MC**: when no banner + no singing, the last song's lyrics linger.
  A follow-up should clear lyrics when neither OCR nor sound finds a song.
- **Banner region** is the whole top strip today; per-video region learning could
  cut OCR cost and false text.
