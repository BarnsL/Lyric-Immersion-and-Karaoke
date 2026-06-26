# Music-Video & Concert Detection

Code: `main.py` (`is_mv_version`, `is_live_or_compilation`, MV-intro hold,
`_on_vocal_onset`), `songchange.py`, `concert_ocr.py`.

Videos aren't clean audio: MVs have long instrumental intros, concerts pack many
songs under one event title. This subsystem keeps lyrics from racing ahead of an
intro, and keeps a whole concert from showing one song's words.

## MV intro hold
- **Trigger:** title `is_mv_version`, OR auto-detect (the LRC span is far shorter
  than the video, and the first lyric line sits near 0) → the studio LRC would
  start during the instrumental intro.
- **Behaviour:** hold the lyrics at the top through the intro until the **vocal
  onset** fires — `_on_vocal_onset` watches the vocal-band energy rise
  (quiet→music→voice). Confidence = a sustained band-energy lift, not a single
  spike, so an intro stab doesn't release early.

## Concert / live / compilation
- **Trigger:** `is_live_or_compilation(title, duration)` — "LIVE", "concert",
  "setlist", festival names, or an unusually long duration.
- **Behaviour:** the title is the EVENT, not a song, so it is **ignored entirely**;
  each track is driven by **sound** ([sound fingerprint](../song-identification/sound-fingerprint.md)).
  The **song-change detector** (`songchange.py`) fires an immediate re-ID on a
  silent gap between songs, and **concert OCR** reads the on-screen song banner
  (accept ≥ 0.85). This fixes a whole concert showing one song's lyrics.

## Confidence tie-ins
- MV-mode and live-mode change the [sync](../sync-by-sound/) strategy: live mode
  FOLLOWS large offsets instead of resetting them (a real, drifting offset).
- Intro hold prevents a false early desync that the sync ladder would otherwise
  spend effort chasing.

Reported in `/source` as `mv_mode`, `live_mode`, `intro_anchored`.
