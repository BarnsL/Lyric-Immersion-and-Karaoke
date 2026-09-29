# Offline Concert Analysis (`concert_audio.py`)

Download once ([[yt-dlp]]) → **int16 streaming decode** ([[PyAV]]) → blockwise energy
envelope → [[MC Talk Detection]] → segments (chapters, or energy split at talk) →
vocal onsets (skip talk ±3 s) → **partial plan** (onsets + MC, ~15 s) → parallel,
playhead-first, 3-probe voted fingerprints ([[Shazam]]) → final plan.

Numbers (79-min concert): peak RSS 946 → ~275 MiB. Audio deleted in `finally`.
Installed by `_apply_concert_plan` → used by [[Chapter Setlist Tick]] and [[MC Gate]].
