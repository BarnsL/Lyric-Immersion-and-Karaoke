# Concert-Relative Sync

In a concert `offset ≈ -(song start in the video)`. Paths that judged or wrote the
ABSOLUTE offset broke for most of every concert:

| | was | now |
|---|---|---|
| A resync by listening | rejected \|offset\| > 600 → dead after min 10 | guards on the correction (`ref_offset`) |
| B vocal-onset release | compared video time with song time → always rejected | chapter-relative |
| D song switch | `offset = 0.0` = video time → blank | Shazam timing / plan onset / chapter start |
| generation | lines in video time → never shown | display clock |

Knob: `concert_relative_sync` ([[Tune Knobs]]). Part of the [[Sync Engine]].
