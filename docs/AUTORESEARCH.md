# AutoResearch — offline knob research against a known playlist

**Status: 🟢 built** — `scripts/autoresearch.py`.

> **Scope correction.** An earlier revision of this document concluded the loop was
> not viable. That judged the wrong question: it assumed perturbation of *live* user
> playback. AutoResearch is for **offline, agent-driven runs on a predetermined
> playlist while nobody is at the machine**, which removes three of the four
> objections outright — perturbation is free, and a stated playlist supplies the
> ground truth that was missing. The section below is kept because the *measurement*
> traps it documents are still real and the runner is built to avoid them.

## How to run it

```
python scripts/autoresearch.py --write-samples      # templates in research/
python scripts/autoresearch.py --playlist research/playlist.json --arms research/arms.json --out research/results.json
```

`playlist.json` is `[{url, expect_title, dwell_s}]` — **`expect_title` is the ground
truth**, the whole reason this works. `arms.json` is `[{name, tune:{knob: value}}]`.

**What it scores**, entirely against the playlist's stated truth:

| field | meaning |
|---|---|
| `identified_pct` | matched the song that was actually playing |
| `wrong_locks` | confidently displayed a **different** song — weighted **1.5x** |
| `median_time_to_lyrics_s` | how fast anything appeared |
| `median_time_to_identified_s` | how fast the *correct* song appeared |
| `median_drift` | timing accuracy while it played |

`wrong_lock` dominates deliberately: showing the wrong song confidently is worse than
showing nothing, and an optimiser not told this will trade correctness for speed.

**Safety properties, each there for a reason found during the survey:**

- **Knob restore.** Every knob any arm touches is snapshotted and restored in a
  `finally` — including on Ctrl-C. An autotuner that silently leaves knobs moved is a trap.
- **Set-verification.** `set_tune` coerces to the existing value's *type*, so an int
  knob silently truncates a float. The runner reads back every knob and refuses to
  attribute results to an arm whose knobs did not actually apply.
- **Never persists.** No `?persist=1` — a crashed run cannot poison `settings.json`.
- **Honest ranking.** If the top two arms are within noise, or fewer than 8 tracks
  ran, it prints **"NOT a conclusion"** instead of declaring a winner.

## What it deliberately does NOT optimise

Objectives verified perverse — the optimum makes the app worse:

| rejected | degenerate optimum |
|---|---|
| `resync_count` | never correct — "sit on wrong lyrics silently" |
| `sync_in_window_pct` | its own window knobs are tunable: widen the ruler |
| `time_to_sync_s` | measures Shazam agreement, ~84% null, non-randomly |
| `/metrics` success | the concert rule requires no sync at all |
| tpvr latency alone | maximised at gap → 0, which deletes the feature |

## Real remaining limit

**Songs play in real time.** 6 tracks x 150s x 3 arms is ~45 min. Prefer few arms with
a large expected effect over a wide sweep, and remember between-song variance exceeds
most knob effects — hence the "NOT a conclusion" guard.

---

## Appendix: the measurement traps (why the scoring looks like it does)

**Original status: 🔴 not viable for LIVE tuning.** The console page describing an AutoResearch loop
documents something that has never run, and — more importantly — *could not produce
trustworthy results if it did*. This document records why, and what would have to be
built first.

Checked live (v1.1.85/86, 2026-07-18): the `D:/Lyric-Immersion-AR` worktree exists on
branch `autoresearch` with **0 `experiment:` commits**, **0 commits ahead of master**,
last touched 2026-07-04, and the skill it tells you to install (`npx skills add
uditgoenka/autoresearch`) is **not installed**. `/insight.autoresearch` now reports
this, and the console shows a "has never run" badge instead of presenting the loop as
operational.

---

## 1. The two findings that kill a naive optimiser

### 1a. Nothing attributes an outcome to a knob setting

`set_tune` (main.py) mutates a dict and writes one log line. It does **not** snapshot
the tune config into any record, does not open a new telemetry bucket, and does not
timestamp into `/metrics`. `metrics.py` buckets purely by `version.__version__`.

So: change `tpvr_gap_s` mid-session and the plays before and after land in the **same
bucket, indistinguishable**. An optimiser would be reading a signal that cannot see
its own actions.

### 1b. The objective for the flagship knob is thrown away

The two-point-verify outcome — *reads agreed* vs *reads disagreed* — is the direct,
physically correct objective for `tpvr_gap_s`. It exists **only** as `log.info` text
(main.py, tier path and applause path). It is not counted, not in `_stats`, not
emitted as a `_sync_event`. And `karaoke.log` rotates at 256 KB with
`backupCount=1`, so it is actively discarded.

The single best objective for the knob the user asked about is currently deleted by
log rotation.

---

## 2. Every candidate objective is perverse

Independent adversarial review rejected **all** of them. The degenerate optimum for
each is not hypothetical — it is reachable by the optimiser:

| candidate | why it fails |
|---|---|
| `tpvr_gap_s` latency | every proposed metric is maximised at **gap → 0**, and gap → 0 *deletes the feature* |
| `resync_count` | **suppress-the-fix**: driven to zero by never correcting. Optimum = "sit on wrong lyrics silently". Two of its five kinds are *user* actions |
| `sync_in_window_pct` | **the ruler is inside the search space** — `sync_win_ahead_s`/`sync_win_behind_s` are themselves tunable, so the optimiser widens the window rather than improving sync. Cumulative-since-boot, so a mid-session change is invisible anyway |
| `time_to_sync_s` | measures **Shazam-agreement latency, not lyric correctness**; null in ~84% of plays, and missing *non-randomly* — a survivorship reward hack is available today with no code changes |
| `/metrics` success | the **concert rule does not require success**: ≤3 wrong-detections scores "success" with no display or sync precondition |
| `title_hit_pct` | measures the **user's music collection**, not any knob |

Add to that: no ground-truth corpus, no replay harness, ~68 confounded plays/day
across 400 distinct titles (only 23 played ≥5 times), and three-way profile splits
(studio / live / concert) that flip mid-session.

**And the corpus cannot legally be built.** Replay would require recording loopback
audio — third-party copyrighted content. It must stay local, gitignored, and never
reach a repo or build.

---

## 3. What was actually built instead

Nothing that pretends to be science:

- **`/insight.autoresearch`** — reports the loop's real state (worktree present,
  experiment count, commits ahead, skill installed). The console renders "has never
  run" rather than describing it as operational.
- **`/insight.gates`** — the decide-by-ear arithmetic that was previously
  function-local, so a *human* can see why a switch was refused.
- **`/insight.ocr` + `ocr_drops`** — every string the reader saw and why each was
  dropped, which the log rate-limited to once per 10 min.

Human-readable introspection, not an optimiser.

---

## 4. What would have to exist first

In order. Each is independently useful even if the loop is never built:

1. ✅ **DONE (TICKET-193).** Count the TPVR outcome — tallied by
   `_note_tpvr_outcome` and exposed at `/insight.tpvr` as
   `{"concert@1.20": {"agree": n, "disagree": n, "agree_pct": ..}}`.
   Tally reads-agreed vs reads-disagreed, keyed by the
   gap value in force. Self-attributing by construction — no config-bucket plumbing
   needed. This is the cheapest single change that makes one knob honestly tunable.
2. **Stamp a config fingerprint on every play record** in `metrics.py`, so plays can
   be attributed to the knob set that produced them.
3. **Fix the outcome classifier** — the concert rule scoring "success" with no sync
   precondition is a release bug regardless of any research loop.
4. **Freeze the ruler.** Any knob used to *score* a metric must be outside the search
   space, or the optimiser games the measurement.
5. **Persist the evidence.** `_sync_events`, `_stats` and the decision audit are all
   memory-only and die on restart.

Only after 1–5 does an optimisation loop have anything trustworthy to optimise
against — and even then, at ~68 plays/day with between-song variance far exceeding
any knob effect, **passive observation is defensible and active perturbation of live
playback is not**. Exploration here is asymmetrically expensive: a bad gap value does
not cost a data point, it shows the user wrong lyrics or none at all — most damagingly
in a concert, the exact scenario being tuned for.

---

## 5. Novel detectors — verdict

Proposed and rejected, each on verified code:

- **Deterministic TPVR simulator over a corpus** — the corpus does not exist and
  cannot legally be made.
- **Chroma self-similarity to disambiguate rival correlation peaks** — the comparison
  is not computable: the LRC side is *text*, there is no audio at the LRC region.
- **Spectral flux / onset envelope as a second correlation channel** — the claimed
  metric does not exist, and the physics does not work at the existing block rate.
- **Shazam `time_skew` / `frequency_skew`** — the fields are not on the live code
  path (`shazamio` does not return them from the call actually made).
- **Recording the full 151-shift correlation curve** — worth doing as *diagnostics*,
  but every reward derivable from it is maximised by lowering `energy_lift_floor`,
  which disables the wrong-lyrics safety net.

The recurring pattern: a metric that improves while the user's experience gets worse.
That is the thing to design against, and it is why this loop is documented rather than
shipped.
