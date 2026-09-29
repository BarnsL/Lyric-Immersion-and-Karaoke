# Lyric Immersion and Karaoke — Constitution

This constitution records the rules every change to this repository must satisfy.
Its principles are drawn from `AGENTS.md`, `SECURITY.md` and the project's own
ticket history in `docs/ISSUES.md`. It is the checklist behind the
"Constitution Check" gate in every `specs/*/plan.md`.

## Core Principles

### I. No Copyrighted Content (NON-NEGOTIABLE)

This is a commercial, proprietary product. The repository and every build made
from it contain **zero** copyrighted third-party content:

- **No lyric text anywhere.** That covers code, docs, tests, fixtures, commit
  messages and example output. Song titles and artists are facts and are fine.
- `lyrics/` and `bundled_lyrics/` stay git-ignored. Lyrics are fetched and
  cached at runtime on the user's machine only.
- Test fixtures use invented placeholder text. Evaluation audio is fetched at
  runtime from permissively-licensed sources into a temporary directory and is
  never committed.

**Rationale:** anything that slips into a public remote survives in history,
and removing it means a history purge and a re-publish.

### II. Verify, Don't Guess

- Song identity needs corroborated evidence (duration, artist, language,
  fingerprint, or the video's own metadata) before the app acts on it.
- **Destructive actions** (blacklisting or deleting a cached lyric file,
  switching songs, generating lyrics by ear) demand the strongest evidence. They
  must not run on signals known to be unreliable, such as MC talk being
  transcribed as if it were singing.
- Precision beats recall wherever a false positive would hide correct lyrics or
  destroy a correct cache entry.

### III. Privacy by Default

- No telemetry. Only public song title/artist strings and raw audio (to the
  fingerprint service) may leave the machine.
- Never hardcode or commit user, account or machine data, tokens, or personal
  listening history (`metrics.json`, `settings.json`, `spotify_config.json`).
- Commits are made by a single contributor identity, with no third-party or
  tool trailers.

### IV. Tunable and Observable

- Every behavioural threshold is a live `/tune` knob, and each knob has exactly
  one `tune_docs.TUNE_DOC` entry. `scripts/probe_tune_docs.py` enforces this.
- Each decision is logged with its reason, so it can be diagnosed after the fact.
- Diagnostics accessors (`get_concert`, `get_insight`, `/diag`) must never
  raise. A diagnostics failure must not look like a plausible empty answer.

### V. Degrade Gracefully, Never Jank

- Optional dependencies may be absent: faster-whisper, PyAV, onnxruntime,
  yt-dlp, the fingerprint service. When one is missing, the feature using it
  no-ops. It never crashes the overlay.
- Heavy work (downloads, decoding, analysis, transcription) runs off the Tk
  render thread, with bounded memory, and results are marshalled back through
  `root.after`.

### VI. Tested Changes

- New logic ships with hermetic tests: no network, no live media session, no
  copyrighted fixtures. Put them in `tests/` (pytest) or in the ast-extraction
  probes under `scripts/probe_*.py`.
- CI runs those tests. A change is not done until they pass and its behaviour
  has been demonstrated.

## Additional Constraints

- **Platform:** Windows-first desktop overlay (Tk with an optional GPU renderer),
  with Linux (MPRIS) and macOS betas. The CI import gate runs on all three.
- **Performance:** the render loop targets 60 fps. Background analysis passes
  must keep peak memory bounded. The offline concert pass, for example, must not
  hold float64 copies of a whole concert.
- **Dependencies:** runtime numeric work uses NumPy only. The optional AI stack
  is pinned in `requirements-deps.txt` and bundled from `./.deps`.

## Development Workflow

1. Specify the feature under `specs/NNN-name/`: `spec.md`, then `plan.md`, then `tasks.md`.
2. Pass the Constitution Check in `plan.md` before implementing.
3. Implement behind knobs, with tests. Document new knobs in `tune_docs.py` and
   new behaviour in `docs/`, and add a ticket to `docs/ISSUES.md`.
4. Adversarially review the diff (correctness, sync behaviour, edge cases), then
   push and open a draft PR.

## Governance

This constitution supersedes ad-hoc practice. Amendments are made by pull
request, and the version number below is updated with each one. Every PR review
checks compliance with Principles I–VI.

**Version**: 1.0.0 | **Ratified**: 2026-09-29 | **Last Amended**: 2026-09-29
