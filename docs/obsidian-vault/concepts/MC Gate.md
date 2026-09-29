# MC Gate (runtime)

`_mc_update` (every frame) → `_mc_gate_active()` (in talk ∧ PLAYING ∧ concert ∧ knob ∧ fresh).

While active: background identify, recal/health/boundary, applause + watchdogs, live
resync, decide-by-ear, [[Decision Engine]] strikes and generation all **pause**; late
results captured during talk are dropped. User actions pass (`user=True`).

Display: "MC — talk between songs" only when no line is due. On exit: hold backstop
re-based, stale timer reset, recal re-armed (nothing forced).

Knobs: `concert_mc_gate`, `concert_mc_edge_s`, `concert_mc_hint_margin_s` ([[Tune Knobs]]).
