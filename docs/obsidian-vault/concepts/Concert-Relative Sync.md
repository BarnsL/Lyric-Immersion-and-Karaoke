# Concert-Relative Sync

In a concert `offset ≈ -(song start in the video)`. Paths that judged or wrote the
ABSOLUTE offset broke for most of every concert:

| | was | now |
|---|---|---|
| A resync by listening | rejected \|offset\| > 600 → dead after min 10 | guards on the correction (`ref_offset`) |
| B vocal-onset release | compared video time with song time → always rejected | chapter-relative |
| D song switch | `offset = 0.0` = video time → blank | Shazam timing / plan onset / chapter start |
| generation | lines in video time → never shown | display clock |
| Force Sync | reset to 0.0, 600 s guard → never locked after min 10 | starts from the song anchor |
| OCR sync | 120 s absolute cap, revert to 0.0 | range and revert measured from the song anchor |
| energy auto-align | audio on the video clock → never corrected | song clock, correction capped, paused in talk |

The shared baseline is `_concert_baseline_offset()`: minus the plan onset or chapter
start. It is what studio code calls 0.

Knobs: `concert_relative_sync`, `concert_energy_align` ([[Tune Knobs]]). Part of the
[[Sync Engine]]; talk handling via [[MC Gate]].
