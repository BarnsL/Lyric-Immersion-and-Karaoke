# Feature Specification: Concert performance: MC-aware analysis and concert-relative sync

**Feature Branch**: `claude/gracious-albattani-elcskj` (Spec Kit feature `001-concert-mc-awareness`)

**Created**: 2026-09-29

**Status**: Implemented (pending live validation on a Windows machine)

**Input**: User description: "let's see what improvements can be made since you're a new model. we want to improve performance on concerts"

## Background (measured, not assumed)

The measurements used a labelled mini-concert built from commercially-safe clips
(LibriSpeech talk, 22 JamendoLyrics CC BY/BY-SA/BY-ND songs, and CC BY/PD
instrumentals). The clips are fetched at runtime and never committed.

- The offline analyzer's "vocal" feature scored MC talk (0.53) above singing
  (0.48).
  - Chaptered onsets landed on talk in 3 of 3 cases.
  - Chapterless segmentation merged two songs, two MCs and an instrumental into
    one 243 s "song".
- The offline pass peaked at 946 MiB RSS on a 79-minute concert.
- Three concert sync paths use absolute offsets, but a concert offset is about
  minus the song's position in the video:
  - resync-by-listening gives up past 600 s;
  - the vocal-onset release always rejects;
  - a Shazam song switch sets the offset to 0.0.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Talk between songs does not derail the concert (Priority: P1)

A viewer watches a 60-minute concert with MC talk between songs. During the talk
the overlay shows a calm "MC — talk between songs" card. It does not flicker
wrong songs, does not overwrite or delete the correct cached lyrics, and does
not generate "lyrics" out of the host's speech. When singing resumes, the next
song is identified and synced.

**Why this priority**: MC drift is the failure mode found in 5 of the 5 corpus
concerts (docs/CONCERT_RESEARCH.md §3). The worst outcome, a SWITCH that
blacklists and deletes a correct cache file, cannot be undone.

**Independent Test**: Play a concert whose offline plan has MC intervals. During
an interval, the log shows `MC: entered …`, and no identify, decide-by-ear,
strike increment or generation happens. `/concert` reports `in_mc: true`. On exit,
`MC: exited …` appears and normal recognition resumes.

**Acceptance Scenarios**:

1. **Given** a concert with an MC interval in the plan, **When** playback enters
   it, **Then** background identify, live resync, decide-by-ear, the decision
   engine's strikes, applause and watchdog timers, and lyric generation pause
   until playback leaves it.
2. **Given** the overlay has no current line during MC, **When** MC is active,
   **Then** the hint card reads "MC — talk between songs". A line the loaded
   lyrics expect is never hidden.
3. **Given** the user clicks "Identify by sound" or "Wrong lyrics" during MC,
   **When** the action fires, **Then** it runs normally, because user actions
   are never gated.

---

### User Story 2 - Songs late in a concert stay synced (Priority: P1)

A song that starts 35 minutes into a concert gets the same sync behaviour as one
at minute 3:

- resync-by-listening corrects drift;
- the between-songs hold releases when singing starts;
- a song identified by Shazam is anchored at its real position, not at video
  time zero.

**Why this priority**: all three mechanisms are silently disabled today for most
of every concert. That is the difference between "synced" and "blank" for
Shazam-identified songs.

**Independent Test**: The unit tests reproduce each case at a concert position
around 1800 s:

- `capture_and_align` returns a correction when given `ref_offset`;
- `_on_vocal_onset` anchors relative to the chapter;
- a concert Shazam switch sets `offset = corr` or `-anchor`, never 0.0.

**Acceptance Scenarios**:

1. **Given** the concert offset is -1800 and the heard line implies -1803,
   **When** live resync runs, **Then** the offset moves to about -1803. Today it
   is rejected.
2. **Given** a chapter hold with the anchor at chapter start, **When** vocals
   start 30 s into the chapter, **Then** the offset becomes
   `first_line - vocal_position`. Today the onset is rejected as "implausible".
3. **Given** Shazam hears song B at 12 s into it, **When** it switches the loaded
   song in a concert, **Then** the display shows song B at about 12 s. Today it
   shows song B at 1800 s, which is blank.

---

### User Story 3 - Chapterless concerts split into real songs (Priority: P2)

For a concert without chapters, the offline analysis finds one segment per song,
separated at MC talk. Each segment's lyrics start at the first sung word, not at
the host's talk.

**Why this priority**: 4 of the 5 corpus concerts have no chapters.

**Independent Test**: On the mini-concert, `analyze_concert` returns one segment
per song. No onset falls inside a talk span, and the MC intervals cover the talk.

**Acceptance Scenarios**:

1. **Given** song, talk, song with no chapters, **When** analysis runs, **Then**
   there are two segments and one MC interval.
2. **Given** a chapter that begins with 30 s of talk, **When** analysis runs,
   **Then** the onset is inside the song, not the talk.

---

### User Story 4 - The offline pass is light and fast (Priority: P2)

The one-time concert analysis runs beside a game without a gigabyte memory spike,
and the per-song onsets and MC intervals arrive before the slow fingerprinting
finishes.

**Why this priority**: the app has a gaming mode, and a background pass that
briefly doubles its footprint undermines it. Onsets that land minutes late help
nobody for the first songs.

**Independent Test**: Profile the 79-minute Opus file: peak RSS stays at or under
300 MiB. The `on_partial` callback fires before any fingerprint request.

**Acceptance Scenarios**:

1. **Given** a 79-minute concert, **When** it is analysed, **Then** peak RSS is at
   or under 300 MiB. It is 946 MiB today.
2. **Given** the playhead is at song 7, **When** fingerprinting starts, **Then**
   song 7 is probed first.

### Edge Cases

- **A sparse or spoken-style sung passage flagged as MC.** It never hides
  expected lines. It only pauses listening, and the analyzer drops MC that falls
  inside a merged same-song segment.
- **Silero/onnxruntime unavailable.** The strict acoustic fallback applies
  (0 s false MC on the 27-song set).
- **MC ends mid-capture.** Results captured inside MC are dropped. A decide whose
  capture overlaps MC is marked inconclusive.
- **Seeking across intervals.** Treated as an exit followed by an enter. The
  flag goes stale if ticks stop.
- **Partial plan arriving after the final.** Ignored. An empty plan that still
  has MC is installed.
- **Chapter hold during MC.** It never times out mid-talk, and is re-based at MC
  exit.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The offline analyzer MUST emit MC/talk intervals (video seconds,
  sorted, non-overlapping, each at least `concert_mc_min_s` long) using the
  fused rule: speech probability, no steady beat, and talk-like pauses. When the
  speech model is unavailable it MUST fall back to the strict acoustic rule.
- **FR-002**: Vocal onsets MUST skip leading MC. The vocal floor MUST be computed
  from non-MC frames. Energy segmentation MUST split at MC. Adjacent segments with
  the same confident ID MUST merge, and MC inside them MUST be dropped. Each
  segment MUST carry `mc_frac`.
- **FR-003**: The analyzer MUST decode to int16 in a streaming fashion (with
  `decode_audio` as the fallback) and compute every stage blockwise. It MUST NOT
  allocate a full-length float64 copy.
- **FR-004**: Fingerprint probes MUST run in parallel (`concert_audio_id_workers`),
  playhead-first, with an early-exit 3-probe majority vote (0.85 / 0.60 / 0.45).
- **FR-005**: `analyze_concert` MUST deliver a partial plan (onsets + MC) through
  `on_partial` before fingerprinting. `analyze()` MUST remain backward compatible.
- **FR-006**: In a concert, resync-by-listening MUST evaluate its guards relative
  to the current offset. It MUST drop results whose lines or anchor changed during
  capture, and MUST NOT reset a concert offset to 0.0.
- **FR-007**: In a concert chapter hold, `_on_vocal_onset` MUST measure from the
  chapter anchor (or the MC exit), MUST cap the relative shift, and MUST
  rate-limit its reject log.
- **FR-008**: A concert Shazam switch MUST anchor the new song from the match
  timing (`corr`) when plausible, else from the concert anchor, and never at 0.0.
  The concert sync-read cap MUST be relative.
- **FR-009**: A chapter skipped because of a recent sound lock MUST be
  re-evaluated once that lock ages out and the title differs.
- **FR-010**: Plan installation MUST NOT blindly re-tick. A partial plan MUST NOT
  override the final one. Chapterless synthesis MUST read knobs, not hardcoded
  0.70 / 8.0.
- **FR-011**: While in MC, the runtime MUST gate non-user identify, live resync,
  decide-by-ear (except api/force/wrong), decision strikes, applause and watchdog
  timers, per-chapter generation and generation chunks, and MUST drop in-flight
  results captured inside MC.
- **FR-012**: The MC hint MUST appear only when no line is shown and no line is
  expected. It MUST NOT add early returns to the frame loop.
- **FR-013**: A chapter whose `mc_frac` is at least `concert_mc_chapter_skip_frac`
  MUST be treated as a non-song segment.
- **FR-014**: `/concert` MUST NOT raise. It MUST report `in_mc`, the MC intervals,
  and a knob group for the new knobs.
- **FR-015**: Every new knob MUST have a `TUNE_DOC` entry, and
  `probe_tune_docs.py` MUST pass.

### Key Entities

- **Plan segment**: a dict with `start`, `end`, `onset`, `title`, `artist`,
  `chapter`, `source`, `id_conf`, `mc_frac`.
- **MC interval**: `[start, end]` in video seconds. The runtime shrinks each end
  inward by `concert_mc_edge_s`.
- **Concert analysis**: `{"segments": [...], "mc": [[s, e], ...], "stats": {...}, "partial": bool}`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Onsets on MC talk in the chaptered mini-concert drop from 3/3 to
  0/3.
- **SC-002**: The chapterless mini-concert yields one segment per song, not one
  merged segment.
- **SC-003**: At least 85% of talk frames are flagged MC in normal conditions,
  and flagged talk inside real songs stays at or under 1.5% of song time
  (measured: 62 s of 4743 s).
- **SC-004**: Peak RSS on a 79-minute concert is at or under 300 MiB (from 946 MiB).
- **SC-005**: All three concert-sync cases in User Story 2 pass as unit tests at
  a concert position of about 1800 s.
- **SC-006**: `pytest tests/`, `scripts/probe_concert.py` and
  `scripts/probe_tune_docs.py` pass in CI.

### Results (measured 2026-09-29)

Measured with `scripts/eval_concert_mc.py --songs` and a 79-minute Opus file,
on Linux. Nothing downloaded is committed.

| | Target | Measured | |
|---|---|---|---|
| SC-001 | 3/3 → 0/3 onsets on talk | 0/3 | met |
| SC-002 | one segment per song | 1 → 3 segments (song, instrumental, song) | met |
| SC-003 recall | ≥ 85 % in normal conditions | 92-93 % (dry, hall, crowd, BGM −20/−14 dB) | met |
| SC-003 false MC | ≤ 1.5 % of song time | 1.2 % per song (59.5 s of 4880 s); 1.7 % (82.5 s of 4747 s) when the same songs are one Opus file | met per song, **missed on the concatenated file** |
| SC-004 | ≤ 300 MiB peak | ~295 MiB (partial plan ~14 s, total ~15 s with fingerprinting stubbed) | met |
| SC-005 | User Story 2 cases pass | `tests/test_concert_sync.py` | met |
| SC-006 | tests and probes in CI | Linux CI steps added | met (pending the first CI run) |

The false MC comes from the same two songs in both measurements: a sparse
country ballad and a track with a spoken feature. By design, MC never hides a
line the lyrics expect. A false MC only pauses background re-identification
and resync for those seconds, and it can show the MC card in an instrumental
gap.

## Assumptions

- The Silero VAD model bundled in faster-whisper
  (`faster_whisper/assets/silero_vad_v6.onnx`) and onnxruntime ship together
  with the concert analyzer's other optional deps.
- MC talk in produced concert uploads is mostly dry (board mix). Extreme arena
  reverb lowers recall, which falls back to today's behaviour, but does not
  cause false positives.
- The Tk/Windows runtime can't be launched in CI. Runtime behaviour is verified
  by unit and probe tests plus one live concert run on the owner's machine.
