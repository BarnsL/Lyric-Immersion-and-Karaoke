# Method: Sound fingerprint (Shazam)

**Source:** `recognize.py` → `shazamio`. Records a few seconds of the system audio
loopback and asks Shazam to identify it. Returns `(title, artist, offset)` where
**offset = how far into the song the captured audio was** (the basis of
[sync-by-sound](../sync-by-sound/)).

**This is the authority for identity** — it hears the actual audio, immune to messy
browser titles. But one read can mis-ID (a mix, a cover, a chorus-matched repeat),
so it is never trusted on a single read when it CONTRADICTS a locked title.

## Confidence model
- **Single hit:** provisional. Used immediately only to *calibrate* timing when the
  heard song already matches the loaded lyrics.
- **Override requires agreement:** to switch AWAY from a `_title_locked` song, a
  contradicting hit is held in `_pending_switch` and must be **corroborated by a
  2nd read** before the song changes. (Stops a one-off mis-ID from yanking lyrics.)
- **Same-artist containment:** a heard title contained in / containing the locked
  title is treated as the same song (handles "Ahoy!! We are Houshou Pirates" ↔
  "Ahoy!! 我ら宝鐘海賊団☆").

## Anti-abuse
- **Game noise:** `songchange.py` spectral-flatness gate keeps broadband SFX from
  being mistaken for vocals before a capture is even attempted ([TICKET-051]).
- **Reels/short audio:** suppressed upstream by `is_non_music_source`.

**Gate before acting:** identity-switch ⇒ 2 agreeing reads; timing-calibrate ⇒ the
heard song must equal the loaded song (see two-point logic in
[../sync-by-sound/shazam-two-point.md](../sync-by-sound/shazam-two-point.md)).
