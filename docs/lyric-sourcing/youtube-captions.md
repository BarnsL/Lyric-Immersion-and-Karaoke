# Method: YouTube caption track (rung 1)

**Source:** `deep_transcribe.fetch_captions_only` (yt-dlp, subs only:
`skip_download`, `writeautomaticsub`), applied by `main.py:load_youtube_captions` /
`_apply_captions`.

For a video playing in the browser, the **video's own caption track** is the most
trustworthy lyric source: it's the exact words, already timed to THIS upload, so no
sync correction is needed.

## Why it's highest trust
- Words + timestamps come from the video itself → zero same-title-collision risk
  and zero offset error (skip the energy correlator entirely — [PERF-006]).
- Requested in the **song's own language** only, to avoid grabbing an
  auto-translated track.

## Confidence / guards
- Applied under `_track_seq` so a late fetch from a previous song can't paint over
  the current one ([TICKET-052/053]).
- **Wins over LRC:** `_apply_captions` clears `_fetch_key`/`_fetch_result` so a
  slower provider-LRC fetch can't overwrite captions.
- Single-flight (`_captions_fetching`) — one yt-dlp fetch at a time so a fast
  playlist can't pile up heavy node/yt-dlp processes ([PERF-002]).

**Gate before acting:** exact-URL or 11-char video id (or title search) resolves a
caption track in the right language → apply and lock; else fall to rung 2.
