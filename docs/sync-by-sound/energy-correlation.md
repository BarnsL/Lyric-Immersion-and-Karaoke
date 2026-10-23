# Method: Energy correlation (the automatic workhorse)

**Source:** `main.py:_auto_align_by_energy`; vocal energy from `songchange.py`.
**Whisper-free, no network, cheap enough to run continuously** (cadence
`continuous_recal_ms`, 15 s).

## What "energy" means here
The **acoustic energy** of the audio in the **vocal band (200–3000 Hz)**, one FFT per
~0.2 s block. A block counts as "vocals on" when its vocal-band energy ratio is high
**and** its spectral **flatness** is low (tonal singing, not broadband game noise).
That yields a per-block **vocals on/off mask** — *when there is singing*.

## How it syncs
Slide that on/off mask against the LRC's **line-active intervals** (when the lyrics
say there should be singing). The shift of best overlap is the offset. No
transcription — just "make the singing line up with the lines."

## Confidence gates (all must pass to move the offset)
- **Small-shift prior** — `energy_shift_penalty` (0.012/s) biases toward small
  corrections over big jumps.
- **Peak uniqueness** — reject if a distant rival peak is within `energy_peak_margin`
  (0.06) of the best (a near-equal far peak = a repeated chorus). This is what stops
  chorus-repetition matches ([TICKET-043]).
- **Lift floor** — peak must clear the median by `energy_lift_floor` (0.10); a flat
  correlation = no real match.
- **Agree-with-Shazam band** — sanity-check against `_last_audio_off` so the
  correlator can't drift onto a chorus the fingerprint already ruled out.
- **Apply minimum** — only apply if `|new − old| ≥ energy_apply_min` (0.4 s).
- **Sanity cap** — `|new_off| < energy_max_offset` (60 s).

## Limitation
Rap / continuous-vocal songs have a nearly-always-on mask → little structure to
correlate → ambiguous result (low lift). That's expected and correctly produces NO
correction (it doesn't fabricate one). Such songs lean on captions / player clock.

**Gate before acting:** unique peak above the lift floor, agreeing with Shazam,
beyond the apply-minimum.
