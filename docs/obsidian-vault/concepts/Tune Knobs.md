# Tune Knobs (`/tune`)

~250 live parameters in `Overlay._tune`; each has exactly one entry in
`tune_docs.TUNE_DOC` (ASCII, 150-700 chars) — enforced by `scripts/probe_tune_docs.py`.
Editable in the [[Dev Console]] with the doc as a tooltip; persisted with `?persist=1`.

Concert knobs are grouped by mechanism on `/concert` (e.g. "MC talk awareness").
