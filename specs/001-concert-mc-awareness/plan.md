# Implementation Plan: Concert performance: MC-aware analysis and concert-relative sync

**Branch**: `claude/gracious-albattani-elcskj` | **Date**: 2026-09-29 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-concert-mc-awareness/spec.md`

## Summary

This plan does four things:

1. Teach the offline concert analyzer (`concert_audio.py`) to tell **MC talk** from
   **songs**, using three cues together: speech probability from the Silero VAD
   bundled with faster-whisper, no steady beat (onset-envelope pulse clarity), and
   talk-like pauses. The result feeds onsets, segmentation and a new list of MC
   intervals.
2. Make the analyzer **light**: streaming int16 decode, blockwise stages, parallel
   playhead-first fingerprinting, and progressive delivery.
3. Fix three **concert-relative sync bugs** in `align.py`/`main.py`, where offsets
   were treated as absolute, plus a chapter-skip bug and a plan-install bug.
4. Make the **runtime MC-aware**: pause listening machinery during talk, show a
   calm hint in blank moments, and never hide an expected lyric line.

## Technical Context

**Language/Version**: Python 3.11–3.13 (CI uses 3.12)

**Primary Dependencies**:
- Runtime: NumPy.
- Optional: PyAV (`av`), faster-whisper (decode fallback and Silero VAD asset), onnxruntime, yt-dlp, shazamio.

**Storage**: none new. The analysis plan lives in memory, and the audio is deleted in `finally`.

**Testing**:
- pytest (`tests/`), hermetic: synthetic signals, fake speech/identify functions.
- ast-extraction probes (`scripts/probe_concert.py`).

**Target Platform**: Windows desktop overlay (primary), Linux/macOS betas.

**Project Type**: desktop app (single project, flat module layout).

**Performance Goals**:
- The offline pass peaks at or under 300 MiB RSS for 80 minutes of audio.
- Silero runs only on acoustic candidate regions, at 7 s or less of CPU per audio-hour when run on everything.
- The render loop stays untouched: 60 fps, and no new early returns in `_tick_body`.

**Constraints**:
- No copyrighted content in the repo.
- Every behaviour change sits behind a `/tune` knob with a `TUNE_DOC` entry.
- Nothing may crash when an optional dependency is missing.

**Scale/Scope**: concerts up to `concert_audio_max_dur_s` (4800 s). About 20 knobs are touched or added.

## Constitution Check

| Principle | Status | How this plan complies |
|---|---|---|
| I. No copyrighted content | PASS | Tests use synthetic audio and placeholder text. The eval script downloads CC BY/BY-SA/BY-ND/PD clips into a temp dir at runtime. |
| II. Verify, don't guess | PASS | The MC gate blocks the destructive paths (SWITCH blacklist/unlink, by-ear generation) while evidence is known-bad (talk). The detector is precision-biased and never hides expected lines. |
| III. Privacy | PASS | Nothing new leaves the machine. Silero runs locally. Commits use a single identity. `metrics.json` is untracked. |
| IV. Tunable and observable | PASS | New knobs are documented in `tune_docs.py`. `MC: entered/exited` logs are added. `/concert` gains `in_mc` and `mc_audio`, and its `_s` NameError is fixed. |
| V. Degrade gracefully | PASS | PyAV missing falls back to `decode_audio`. Silero missing falls back to the strict acoustic rule. Analysis failures leave today's behaviour in place. |
| VI. Tested changes | PASS | `tests/test_concert_audio.py`, `tests/test_concert_sync.py` and the probe sections all run in CI. |

## Project Structure

### Documentation (this feature)

```text
specs/001-concert-mc-awareness/
├── spec.md    # what and why (user stories, FRs, SCs)
├── plan.md    # this file
└── tasks.md   # executable task list
```

### Source Code (repository root)

```text
concert_audio.py        # offline analyzer: decode, envelope, MC features, segmentation, IDs, delivery
align.py                # capture_and_align(ref_offset=…)
main.py                 # concert runtime: plan install, MC gates, sync fixes, /concert accessor
tune_docs.py            # TUNE_DOC entries for the new knobs
api.py                  # /identify passes user=True
tests/test_concert_audio.py
tests/test_concert_sync.py
scripts/probe_concert.py        # repo-relative path, no fake _s, MC sections
scripts/eval_concert_mc.py      # reproducible before/after evaluation (downloads CC clips at runtime)
docs/CONCERT_AUDIO_SYNC.md, docs/CONCERT_RESEARCH.md, docs/ISSUES.md
docs/obsidian-vault/            # concept notes + mind-map canvas
```

**Structure Decision**: keep the flat module layout. Put new pure logic in
`concert_audio.py`, where it can be tested in isolation, and keep `main.py`
changes to state, gates and anchoring at existing call sites.

## Design Notes

### MC detector (fused rule, validated offline)

The features are computed per 0.5 s frame, over a 32 ms / 15.625 ms STFT:

| Feature | Definition | Window |
|---|---|---|
| `lefr` | share of short frames whose 300–3400 Hz energy is below 0.5× the window mean | 3 s |
| `depth` | p90 − p10 dB of that energy | 3 s |
| `lowr` | 30–95 Hz energy share | 3 s |
| `pulse` | max normalised autocorrelation of the onset envelope at 0.25–1.5 s lags | 6 s |
| `sp` | Silero speech probability, run only on candidate regions | — |

- **Fused rule:** `sp ≥ 0.5 ∧ pulse < 0.30 ∧ (lefr ≥ 0.45 ∨ depth ≥ 16 dB)`, then an
  11-frame majority vote and a minimum run of 10 s. It measured 62 s false-MC in
  4743 s of real songs, with ~90% recall on talk.
- **Fallback when Silero is unavailable:** `pulse < 0.22 ∧ lefr ≥ 0.52 ∧ depth ≥ 18 ∧ lowr ≤ 0.12`.
  It measured 0 s false-MC, with ~60% recall on clean talk.

### Concert-relative offsets

A concert offset is approximately minus the anchor, i.e. the song's start in
video time. Every sanity guard that bounded `|offset|` therefore has to bound
`|offset − reference|` instead:

- align (`ref_offset`);
- `_apply_align`;
- the Shazam sync-read cap.

Every "fresh song" path that wrote `offset = 0.0` has to write `−anchor`, or the
Shazam-implied `corr`.

## Complexity Tracking

| Addition | Why needed | Simpler alternative rejected because |
|---|---|---|
| Silero VAD in the offline pass | Acoustic cues alone reach ~60% recall, and Silero alone false-flags rap for 498 s | Neither cue alone is both precise and sensitive |
| Per-caller MC gates rather than one gate in `_start_identify` | Callers do bookkeeping before they call (`_fast_calib`, health attempts, watchdog stamps) | A single gate would burn those budgets without identifying |
| Explicit `user=True` exemption | `_user_identify_pending` is sticky and not set by `/identify` or report-wrong | Reusing it would leak the exemption or block real user actions |
