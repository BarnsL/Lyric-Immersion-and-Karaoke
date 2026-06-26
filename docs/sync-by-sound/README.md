# Sync by Sound — *line lyrics up to the audio*

Code: `main.py` (`_consume_async` sync ladder, `_schedule_sync_confirm`,
`_auto_align_by_energy`), `align.py` (Whisper), `songchange.py` (vocal energy).

The lyrics' timestamps assume a reference cut; the playing audio may be offset (long
intro, different master, live tempo). This subsystem finds the **offset** that lines
words to sound, continuously, while refusing to chase chorus-repetition false
matches.

| Method | File | Cost | Role |
|---|---|---|---|
| Shazam offset + two-point verify | [shazam-two-point.md](shazam-two-point.md) | medium | authoritative offset, gated by a 2nd read |
| Energy correlation | [energy-correlation.md](energy-correlation.md) | cheap | always-on automatic workhorse |
| Whisper align | [whisper-align.md](whisper-align.md) | heavy | explicit / last-resort only |

## The clock
Between corrections the **player clock** (SMTC position × rate) carries the
timeline, so sync doesn't depend on constant listening. Captions are video-locked,
so the correlator is skipped for them ([PERF-006]).

## Shared confidence ideas
- **Two-point / two-read agreement** — never move the offset on a single read; a
  repeated chorus can match the wrong place, so demand corroboration.
- **Deadband** — |drift| below `deadband` (0.8 s) is left alone; the exact player
  clock wins.
- **Studio vs. live mode** — a studio track has an exact clock (true offset ≈ 0, big
  reads are artifacts to distrust); a live/alt arrangement has a real, possibly
  large/drifting offset to FOLLOW.
- **Ambiguity spread** — wildly varying reads (repeated choruses) are detected as
  spread and reset rather than chased.

All thresholds are live-tunable via the `/tune` API ([TICKET-041]) — no rebuild.
