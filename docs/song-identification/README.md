# Song Identification — *what is playing?*

Code: `main.py` (`MediaWatcher`, `clean_title`/`clean_artist`, `_on_track_change`,
`_consume_async`), `recognize.py`, `concert_ocr.py`, `confidence.py`.

The player's reported title is a **hint**, not the truth. Four methods each emit a
confidence signal; the app combines them so no single weak signal can pick a song.

| Method | File | Confidence output |
|---|---|---|
| Player metadata (SMTC) | [player-metadata.md](player-metadata.md) | `title_distinctiveness` 0–1 → may `_title_locked` |
| Sound fingerprint (Shazam) | [sound-fingerprint.md](sound-fingerprint.md) | a hit needs a **2nd agreeing read** to override a lock |
| Concert banner OCR | [concert-ocr.md](concert-ocr.md) | fuzzy match score, accept ≥ 0.85 |
| Title cleaning / cover extraction | [title-cleaning.md](title-cleaning.md) | routes covers to the original artist |

## How a verdict is reached (`_consume_async`)
1. **Heard vs. loaded.** Does the sound-ID'd song match the lyrics already on
   screen? (`_titles_match`, plus the romanized-title alias so 漢字 cache ↔ romaji
   Shazam title still count as the same song.)
2. **Match → calibrate**, don't switch. The heard song's timing tunes sync only
   when it IS the loaded song (applying a mis-ID'd mix's offset to the wrong lyrics
   was the old "wild offset" bug).
3. **Mismatch → demand a 2nd read.** A contradicting heard song is HELD in
   `_pending_switch`; only a 2nd corroborating read switches songs.
4. **Distinctive locked title wins ties.** `_title_locked` (distinctiveness ≥ 0.40,
   not generic, not MV, not stale) ignores a one-off Shazam mis-ID onto a
   same-artist track.
5. **Non-music suppressed.** `is_non_music_source` clears the overlay for bare
   site-name audio (Reels/Shorts/TikTok) with no artist.

See also [wrong-song-rejection](../wrong-song-rejection/) for the failure-mode guards.
