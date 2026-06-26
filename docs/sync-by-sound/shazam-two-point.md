# Method: Shazam offset + two-point verification

**Source:** the sync ladder in `main.py:_consume_async`; `_schedule_sync_confirm`.

A Shazam hit returns `offset` = position into the song. Compared with the player's
reported position it implies a correction `corr`. **A single read is never
applied** — on a song with repeating choruses the first read can match a repeated
section and point to the wrong place.

## Two-point verification (the gate)
1. A non-zero `corr` is **held** in `_pending_corr` (not applied).
2. **Hesitate** `sync_confirm_hold_ms` (default 2600 ms), then take a **confirming
   listen** of `sync_confirm_listen_s` (default 5.0 s) — both /tune knobs
   ([TICKET-056]).
3. Commit only when the 2nd read agrees within `agree` (2.0 s studio / `agree_live`
   4.0 s) — the AGREE branch. Otherwise keep holding.

A longer hold separates the two reads by more song time, so two *different*
instances of one chorus are less likely to read at the same offset and falsely
agree. **Open tuning:** find the hold/listen pair giving ≥80 % correct commits on
"All The Things She Said" ([TICKET-056]).

## Mode-specific confidence
- **Studio** (exact clock): big reads distrusted; ambiguous spread > `spread_reset`
  (20 s) with a small current offset ⇒ **reset to player clock**, don't chase
  ([TICKET-040], サクラミラージュ chorus jumps). `corr ≈ 0` but showing an offset ⇒
  auto-reset to 0.
- **Live / alt arrangement** (duration mismatch > 25 s, or flagged live): the offset
  is REAL and may drift → **follow** it, smoothing `0.6·corr + 0.4·offset`.

## Drift integral fallback
When reads show non-trivial drift but never confirm, accumulate `|drift|·dt`; cross
`drift_align_trigger` (6.0) → escalate to energy correlation / Whisper. Proportional
to how wrong sync actually is, not a hardcoded strike count ([TICKET-038]).

**Gate before acting:** 2 agreeing reads (commit) · deadband (ignore) · spread
(reset) · mode (follow vs. reset).
