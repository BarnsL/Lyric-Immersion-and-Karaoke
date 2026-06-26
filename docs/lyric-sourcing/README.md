# Lyric Sourcing — *get the right words + timing*

Code: `fetch_lyrics.py` (providers + verify), `deep_transcribe.py` (captions +
by-ear), `main.py` (`load_youtube_captions`, `_begin_generation`, `_start_fetch`).

A **trust ladder**: try the most authoritative source first, fall to weaker ones,
and gate each so a weak source can't display the wrong song. Each rung is a
confidence gate.

| Rung | Method | File | Trust |
|---|---|---|---|
| 1 | YouTube caption track | [youtube-captions.md](youtube-captions.md) | **Highest** for a browser video — the video's own words+timing |
| 2 | Provider LRC (LRCLIB / Musixmatch / NetEase) | [provider-lrc.md](provider-lrc.md) | High when **artist-keyed**, low when title-only |
| 3 | Generation by ear (Whisper) | [generation-by-ear.md](generation-by-ear.md) | Last resort, marked `***`, heavy |

## Caching
Results cache to `lyrics/*.json` with `meta.source` recording WHICH rung produced
them. That provenance is itself a confidence signal: a `syncedlyrics/cover` or
`/title` cache is weak and gets **re-validated on load** for clean non-cover sources
([TICKET-055]) — a stale weak cache (Ludacris "The Potion") must not be trusted
forever just because it once matched.

## The provider order matters
**Artist-keyed queries run FIRST**, even for covers — adding the artist
disambiguates a super-common title. The bare-title fallbacks run only after, and
only when allowed by `strict`/`cover`. This ordering is what stops "地球儀" or
"Potion" from loading an unrelated same-title song.
