# Method: Player metadata (SMTC)

**Source:** Windows System Media Transport Controls via `winsdk`
(`GlobalSystemMediaTransportControlsSessionManager`), read by `MediaWatcher` in
`main.py`. Gives title, artist, album, position, duration, playback status, rate.

**Why it isn't trusted blindly:** browser tabs report messy titles ("Song (Official
MV) [4K]｜Channel"), stale paused-tab sessions, or a site name instead of a song.
A generic title ("Awake", "Lucky Star", "KING") collides with many songs.

## Confidence: `confidence.title_distinctiveness(title) → 0..1`
- **High (≥ 0.40):** long/specific/original-script title unlikely to collide →
  eligible for `_title_locked`, so sound won't second-guess it onto a same-artist
  track.
- **Low (< 0.40) / generic:** never locked; the title only seeds a provisional
  display while **sound decides** (`_is_generic_title` blocks locking outright).

## Session selection (`MediaWatcher._pick`)
Picks the actually-playing session with `_pick_src` **stickiness** so a paused tab
or a "… Mix" doesn't flip-flop the source (TICKET-054). Playback `status` +
`position`/`rate` give the player CLOCK that carries sync between sound reads.

**Gate before acting:** distinctiveness ≥ 0.40 AND not MV AND not stale AND not
generic → may lock. Otherwise hint-only.
