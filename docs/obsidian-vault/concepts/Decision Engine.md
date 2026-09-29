# Decision Engine

Every 2 s it scores four dimensions (source agreement, sync stability, lyric quality,
by-ear corroboration) into **strikes**: CAUTION → SWITCH → REGEN. SWITCH blacklists and
deletes the cached lyrics and refetches — irreversible, so the evidence must be good.

[[MC Gate]] freezes strikes while the concert host talks (talk transcripts are not
evidence about the song). Decide-by-ear results whose capture overlapped talk are
marked inconclusive.
