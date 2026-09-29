# Sync Engine

`display time = video position + offset`. Corrections go through `_smooth_offset`
(deferred to a line boundary, narrated to the console):

- Shazam sync reads ([[Shazam]]) — follow / confirm / chorus-trap guards
- energy correlator (vocal on/off mask vs lyric line activity)
- resync by listening ([[faster-whisper]]) — `align.capture_and_align`
- vocal / song onsets (intros), caption re-timing

In a concert the offset is minus the song's start in the video — see
[[Concert-Relative Sync]] for why that broke three of these paths.
