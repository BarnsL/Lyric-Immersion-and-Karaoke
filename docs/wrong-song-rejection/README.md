# Wrong-Song Rejection (cross-cutting)

Not a single module — a set of guards spread across identification and sourcing,
organized here by **failure mode**. The governing principle:

> When we have an **authoritative artist** (clean source, not a cover), never trust
> a bare-title provider match — no matter how well the duration matches.

| Failure mode | Guard | Where |
|---|---|---|
| Same title, **wrong language** | kana title ⇒ JA (reject zh/ko); hangul ⇒ KO (reject zh/ja); CJK artist ⇒ CJK/English lyrics | `fetch_lyrics.fetch_lrc`, `verify_lrc` |
| Same title, **wrong artist** (durations coincide) | **`[ar:]` artist cross-check** (`_lrc_artist_conflict`) — different script / no shared token ⇒ reject | `fetch_lyrics` cover path |
| **Generic title** ("Lucky Star", "Awake") | `title_distinctiveness` + `_strict_ok` — don't lock, don't title-only-match | `confidence.py`, `fetch_lrc` |
| **Stale weak cache** trusted forever | **provenance guard** — reject `syncedlyrics/cover`·`/title` caches for clean non-cover sources, re-fetch | `main.py:_file_valid` |
| Same-artist Shazam **mis-ID** | `_title_locked` containment match needs 2 reads to override | `_consume_async` |
| Wrong **cut** / stale generated cache | captions override; `/wrong` purges + re-IDs; covers re-fetch by original artist | `main.py` |
| **Site / Reel** audio | `is_non_music_source` (bare site name, no artist) | `main.py` |

## Case study — TICKET-055 (the Potion bug)
Spotify "Potion" by Michiru Shisui (JA VTuber) showed **Ludacris "The Potion"**
(English rap). Both songs are **3:43**, so the duration coincidence beat every
duration gate; the Latin title + romaji artist meant no language guard fired; and
the wrong lyrics came from a **stale `syncedlyrics/cover` cache** the app trusted
without re-checking. Fixed by the **provenance guard** (duration-independent: a
clean non-cover source must not load a title-only cache) plus the **`[ar:]`
cross-check**. Energy/Shazam couldn't help — rap's continuous vocals give a flat,
ambiguous energy mask. The lesson: **provenance + artist beat duration** for
rejection.

See [`../../ISSUES.md`](../../ISSUES.md) TICKET-002, 012, 037, 055 for the history.
