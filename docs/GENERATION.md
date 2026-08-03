# Lyric generation — two-tier "best effort, then do it properly"

Desktop Karaoke only **generates** lyrics as a last resort: a song reached
generation only after a real synced-lyric lookup (LRCLIB / syncedlyrics / NetEase,
across artist + title variants) and sound-ID both came up empty. For those songs
there is no "correct" file to fetch — the audio is the only source of truth — so
we transcribe it. That happens in **two tiers**:

| | Tier 1 — **best effort** (realtime) | Tier 2 — **deep** (offline) |
|---|---|---|
| Module | [`align.transcribe_for_generation`](../align.py) | [`deep_transcribe.py`](../deep_transcribe.py) |
| Audio | WASAPI loopback, captured in chunks **while it plays** | the **whole song**, downloaded as a file |
| Model | `small` (fast) | `large-v3` (accurate) |
| When | immediately, the first listen | in the background, right after Tier 1 starts |
| Result | incomplete + rough — it is racing the playhead | clean + complete — it is not racing anything |
| Marked | every line ends `***` (clearly AI, not official) | same `***` marker, but accurate |

Tier 1 gives you *something* instantly. Tier 2 then quietly replaces the rough
cache with a proper transcription, so the overlay upgrades live if the song is
still playing, and **the next play is clean and in sync**.

## How Tier 2 (deep) works — [`deep_transcribe.py`](../deep_transcribe.py)

`deep_transcribe(title, artist)` runs entirely in the background:

1. **Download the source audio.** `yt-dlp` searches `ytsearch1:<title> <artist>`
   and downloads the top hit's **audio-only** stream (`bestaudio` — a `.webm`/
   `.m4a`, so **no ffmpeg** is needed; faster-whisper decodes it via PyAV). An
   over-long hit (> 15 min — a concert or "1 hour loop") is rejected by a
   duration `match_filter`.
2. **Transcribe the whole file** with **`large-v3`** (already cached locally),
   `beam_size=5`, `vad_filter=False` (Silero VAD classifies *sung* vocals as
   non-speech and would drop them — see `align.py`), and
   `condition_on_previous_text=False` (so one mis-hear can't poison the rest).
3. **Hand the timed lines back** to `main.py`, which adds furigana / romaji /
   translation (`fetch_lyrics.annotate`) and saves the cache with
   `source: "generated-deep"`.
4. **Delete the downloaded audio.** Only the lyrics are kept — the audio file is
   removed in a `finally:` block, even on error, so nothing accumulates on disk.

### Wiring (automation) — [`main.py`](../main.py)
- `_begin_generation()` starts Tier 1 **and** spawns `_begin_deep_generation()`.
- `_begin_deep_generation(token, title, artist)` runs the pipeline in a thread and,
  on success, calls `_apply_deep()` on the Tk thread.
- `_apply_deep()` annotates, saves the `generated-deep` cache, and — if that song
  is still playing — loads it live and stops the Tier-1 loop.
- A `_deep_token` (bumped on every track change) cancels in-flight deep work when
  the song changes; a per-song guard (`_deep_tried`) makes it run **once** per song,
  and an existing `generated-deep` cache is never re-downloaded.

## Requirements & graceful degradation
- **yt-dlp** (in `requirements.txt`). Missing ⇒ `deep_transcribe.available()` is
  `False` and only Tier 1 runs.
- **A JS runtime for yt-dlp.** YouTube now needs one to mint un-throttled format
  URLs, or the audio download **403s**. yt-dlp enables only `deno` by default, so
  we opt in to **`node`** when it's on `PATH` (it usually is). With neither, the
  download may 403 and we fall back to Tier 1 — no crash.
- **faster-whisper + the `large-v3` model** (the realtime generator already needs
  faster-whisper; the model is pre-cached under `~/.cache/huggingface`).
- Every failure path (no yt-dlp, network error, 403, over-long match, < 4 lines
  transcribed) returns `None` and leaves the Tier-1 best effort in place.

## Known limitation — match quality
Tier 2 finds the audio by **searching the title + artist**, not by the exact video
you're playing (the app reads the OS media session, which doesn't expose the URL).
For the niche, no-lyrics-anywhere songs that reach generation, the top search hit is
almost always the right track. But a **live / cover** version can match the studio
upload (different arrangement), and a very generic title could mis-match. The
duration guard rejects concert-length hits; beyond that, this is best-effort by
design. (A future option: record the exact loopback audio to a file and transcribe
*that* — exact audio, no search — at the cost of waiting for the song to play through.)
