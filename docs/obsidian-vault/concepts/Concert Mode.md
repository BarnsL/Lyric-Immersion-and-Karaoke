# Concert Mode

A long video / concert is one "track" to the player, so the app runs a concert profile
(`_live_mode`, verdict + rule from `explain_live_or_compilation`).

Signals, strongest first:
1. **Chapters / description setlist** → [[Chapter Setlist Tick]]
2. **[[Offline Concert Analysis]]** → onsets, fingerprint ids, [[MC Talk Detection]]
3. Banner OCR (chapterless only) · applause gaps · live Shazam · decide-by-ear

Runtime protection during talk: [[MC Gate]]. Timing: [[Concert-Relative Sync]].
Diagnostics: `/concert` + the Concerts panel in the [[Dev Console]].

Spec: `specs/001-concert-mc-awareness` ([[Spec Kit]]).
