# Method: Whisper align (heavy, explicit only)

**Source:** `align.py` (faster-whisper). Transcribes a short clip of the live audio
and fuzzy-matches the transcript against the loaded lyric lines to find which line
is playing now → the offset.

This is the most *accurate* sync method (it reads the actual words) and the most
*expensive* (a ~1–2 s 100%-core CPU burst). So it is deliberately kept OFF the
automatic loop.

## When it runs
- The explicit **"Sync by listening"** tray button / `/align` API.
- A confirmed persistent drift escalated from the drift integral
  (`reason in {drift, drift-integral}`), not a periodic tick.
- Inside last-resort generation.

It is **never** the periodic/automatic corrector — that regression (render fell to
22 fps, audio stutter) is exactly why energy correlation took that role
([PERF-007]).

## Confidence gate
- A **match-ratio floor scaled by jump size**: the bigger the offset jump it wants
  to make, the higher the transcript-vs-lyrics fuzzy match must be to accept it. A
  small nudge is cheap to believe; a large jump must be strongly supported.

## Hardware
- `align._get_model` probes `cublas64_12.dll` before offering CUDA; if absent it
  runs **CPU-only** rather than crashing on a missing cuBLAS ([fixed]).

**Gate before acting:** explicit trigger OR confirmed drift, AND transcript match
ratio above the jump-scaled floor.
