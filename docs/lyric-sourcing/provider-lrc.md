# Method: Provider LRC (rung 2)

**Source:** `fetch_lyrics.fetch_lrc` over `syncedlyrics` (LRCLIB, Musixmatch,
NetEase, Megalobiz) plus direct LRCLIB duration-exact lookup.

Synced `[mm:ss.xx]` lyrics from the open lyric databases. Coverage is great for
charted / anime / VTuber songs. Risk: **same-title collisions** — a bare-title
query for "Potion" or "地球儀" returns the most popular same-title song.

## Search order (most → least confident)
1. **LRCLIB duration-exact** — artist+title+duration, the tightest match.
2. **Artist-keyed search** — `"{title} {artist}"` variants FIRST, even for covers
   (the artist disambiguates a common title). Source tag `syncedlyrics`.
3. **Cover fallback** (`cover=True` only) — title-only, trusting the cover marker.
   Source tag `syncedlyrics/cover`. Now also runs the **`[ar:]` artist
   cross-check** ([TICKET-055]).
4. **Title-only** (`strict=False` only) — last resort, guarded by `_strict_ok`.
   Source tag `syncedlyrics/title`. SKIPPED for clean sources (authoritative artist
   ⇒ a title-only hit is almost certainly the wrong same-title song).

## Confidence gates
- **`verify_lrc`** — line count, duration window, and **language-vs-title script**.
- **Language guards** — kana title ⇒ Japanese (reject zh/ko bodies); hangul title ⇒
  Korean (reject zh/ja); a CJK artist's lyrics must be CJK (or English, for a cover).
- **`_strict_ok`** — for Latin-title title-only hits, require a duration corroboration.
- **`_lrc_artist_conflict`** — reject a cover hit whose `[ar:]` tag is a different
  script / shares no token with the requested artist (duration-independent —
  catches the 3:43-vs-3:43 Ludacris coincidence).
- **romaji → kanji upgrade** — a romanized cache is re-fetched for original script.

**Gate before acting:** pass `verify_lrc` AND the path-specific guard above; a clean
non-cover source additionally refuses rungs 3–4.
