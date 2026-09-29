# MC Talk Detection

Why: tonal vocal-band energy cannot tell speech from singing (talk scored higher).

Fused rule per 0.5 s frame:
- speech probability ≥ 0.5 ([[Silero VAD]], candidate regions only)
- **pulse clarity < 0.30** (no steady beat — keeps rap out)
- talk-like pauses (low-energy-frame ratio ≥ 0.45 or dB depth ≥ 16)

→ majority vote, ≥ 10 s runs, join gaps < 4 s. Fallback without the model: strict
acoustic rule (0 s false MC, ~60 % recall).

Measured: 92 % talk recall; 1.2 % false MC on 79 min of real songs; rap 0 s.
Evaluated by `scripts/eval_concert_mc.py` ([[Testing and Probes]]).
