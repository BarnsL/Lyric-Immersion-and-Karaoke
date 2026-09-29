# Media Sources

Windows SMTC (`winsdk`), Linux MPRIS (`media_mpris.py`), window titles, Discord RPC.
`MediaWatcher._pick` ranks sessions (pinned > loudest > sticky > playing) after the
TICKET-226 **source-eligibility gate** (only real media apps, or ≥2 now-playing signals).

Position is extrapolated between reports; the frame loop reads it once per tick.
Feeds [[Lyric Sourcing]] (title/artist) and [[Concert Mode]] (long videos, URL).
