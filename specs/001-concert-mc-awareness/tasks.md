# Tasks: Concert performance: MC-aware analysis and concert-relative sync

**Input**: Design documents from `/specs/001-concert-mc-awareness/`

**Prerequisites**: plan.md, spec.md

**Tests**: Requested. The constitution (Principle VI) requires hermetic tests for new logic.

**Organization**: Tasks are grouped by user story (US1–US4 in spec.md).

## Format: `[ID] [P?] [Story] Description`

## Phase 1: Setup

- [x] T001 Set the repo-local single-contributor git identity, and disable environment commit signing
- [x] T002 Initialize Spec Kit (`.specify/`), write the constitution, and add `specs/001-concert-mc-awareness/`
- [x] T003 [P] Stop tracking personal `metrics.json` (it is already in `.gitignore`)

## Phase 2: Foundational (the offline analyzer core, which blocks US1/US3/US4)

- [x] T004 `concert_audio.py`: streaming int16 `_decode_pcm16`, with `decode_audio` as the fallback
- [x] T005 `concert_audio.py`: dtype-agnostic blockwise `_envelope` (no full-length float64 copy)
- [x] T006 `concert_audio.py`: `_mc_features` (lefr / depth / lowr / pulse), `_speech_prob` (Silero, candidate regions, injectable), and `_mc_intervals` (fused rule + strict fallback)

## Phase 3: User Story 3 — Chapterless concerts split into real songs (P2, but the analyzer comes first)

- [x] T007 [US3] MC-aware `_sustained_onset` (skip leading MC) and a non-MC vocal floor
- [x] T008 [US3] Energy segmentation splits at MC; merge same-ID neighbours; `mc_frac` per segment
- [x] T009 [P] [US3] Tests: onset skip, split, merge, and MC detection with the fallback (`tests/test_concert_audio.py`)

## Phase 4: User Story 4 — The offline pass is light and fast (P2)

- [x] T010 [US4] Parallel, playhead-first, early-exit 3-probe majority vote (`concert_audio_id_workers`)
- [x] T011 [US4] `analyze_concert(... on_partial, pos_now, speech_fn, identify_fn, audio_path)`, plus the `analyze()` wrapper
- [x] T012 [P] [US4] Tests: envelope parity / int16 / memory bound, voting, ordering, partial→final, legacy wrapper, decode parity
- [x] T013 [US4] Profile the 79-minute Opus file (peak RSS ≤ 300 MiB) — measured ~295 MiB

## Phase 5: User Story 2 — Songs late in a concert stay synced (P1)

- [x] T014 [US2] `align.capture_and_align(ref_offset=)`, plus `align_by_listening` / `_apply_align` relative checks and a lines-identity check (Bug A)
- [x] T015 [US2] `_on_vocal_onset`: chapter-relative anchoring in concerts, with a rate-limited reject log (Bug B)
- [x] T016 [US2] `_concert_anchor_for`: concert Shazam switches anchor to `corr` or `-anchor`, and the sync-read cap becomes relative (Bug D)
- [x] T017 [US2] `_concert_setlist_tick`: deferred re-evaluation after a sound-lock hold (Bug E); the generic branch anchors; stale `_pending_offset` is cleared
- [x] T018 [US2] `_apply_concert_plan(seq, plan, mc, partial)`: targeted re-anchor, partial/final ordering, and knobbed chapterless synthesis (Bug F)
- [x] T019 [P] [US2] Tests: `tests/test_concert_sync.py`, covering the align ref offset, vocal onset, switch anchor, deferral and plan install

## Phase 6: User Story 1 — Talk between songs does not derail the concert (P1)

- [x] T020 [US1] State, `_mc_normalize` / `_mc_find` / `_mc_overlap`, `_mc_update` in `_tick_body`, `_mc_gate_active`
- [x] T021 [US1] Gates: identify callers (with the `user=True` exemption), recal, health, boundary, applause/watchdogs, live resync, decide-by-ear, decision engine, setlist-gen, generation
- [x] T022 [US1] `_consume_async` drops non-user results captured inside MC; a decide overlapping MC is marked inconclusive
- [x] T023 [US1] MC hint in blank moments only; talk-dominated chapter skip (`concert_mc_chapter_skip_frac`)
- [x] T024 [P] [US1] Tests and probe sections for the gates and the hint rule

## Phase 7: Polish & Cross-Cutting

- [x] T025 `get_concert`: local `_s` (Bug C), plus an `mc` block and knob group; the probe gets a repo-relative path and no fake `_s`
- [x] T026 `tune_docs.py` entries for every new knob; `probe_tune_docs.py` passes
- [x] T027 [P] CI: a Linux `pytest tests` step (under xvfb) plus the probes
- [x] T028 [P] `scripts/eval_concert_mc.py` (a reproducible before/after table)
- [x] T029 [P] Docs: CONCERT_AUDIO_SYNC, CONCERT_RESEARCH (P1 landed, plus the −748 s hypothesis), ISSUES tickets, scripts README
- [x] T030 [P] Obsidian vault and mind-map canvas (`docs/obsidian-vault/`)
- [ ] T031 Adversarial review of the full diff; fix the findings
- [ ] T032 Commit (logical commits), push, open a draft PR, and watch CI

## Dependencies & Execution Order

- Setup (T001–T003) comes first.
- Foundational (T004–T006) blocks US3 and US4.
- US2 (T014–T019) is independent of the analyzer and can run in parallel with US3/US4.
- US1 (T020–T024) depends on T018, because MC intervals arrive through the plan install.
- Polish comes last. T031 comes before T032.
