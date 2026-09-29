# Concert Audio Sync (offline analysis + MC awareness)

*Introduced v1.1.57; MC (talk) awareness, concert-relative sync and the
low-memory pipeline added by spec [001-concert-mc-awareness](../specs/001-concert-mc-awareness/spec.md).*

**Files:**
- [`concert_audio.py`](../concert_audio.py), the offline analyser;
- [`main.py`](../main.py): `_analyze_concert_audio`, `_apply_concert_plan`,
  `_plan_for_pos` / `_plan_for_chapter`, `_concert_setlist_tick`, and the
  `_mc_*` runtime gate;
- [`align.py`](../align.py): `capture_and_align(ref_offset=…)`;
- `recognize.identify_pcm`, the offline fingerprint.

**Tests:** `tests/test_concert_audio.py` and `tests/test_concert_sync.py`.
**Evaluation:** `scripts/eval_concert_mc.py`.

## The problem

A multi-song VTuber 3D-live or concert is the hardest sync case:

1. **Live arrangement ≠ studio recording.** Shazam often misses a live take.
   A logged `drift = -748s` on an Offkai *Melt* read was long blamed on live
   timing. See "Concert-relative sync" below for the more likely cause.
2. **The live recognizer janks.** WASAPI capture plus fingerprinting is GIL-heavy,
   so the smoothness backoff can kill it on a busy frame.
3. **Applause, MC talk and intros.** The first 20-60 s of a song is often not
   singing, and between songs the host TALKS, sometimes for minutes.

MC talk turned out to be the missing piece. It was measured on a labelled
mini-concert (`scripts/eval_concert_mc.py`):

- The envelope's "vocal" feature (tonal energy in 200-3000 Hz) scored MC talk
  **higher than singing** (0.53 vs 0.48).
- **3 of 3** chapters that opened with talk anchored their lyrics ON the talk.
- A chapterless concert came back as **one 243 s "song"** (two songs, two MCs and
  an instrumental merged). The second song could never be identified.
- At runtime, talk after applause read as "vocals back", so the app re-identified
  on talk. Decide-by-ear scored talk transcripts against lyrics, and the decision
  engine could SWITCH, which blacklists and deletes the correct cached lyrics.
  Per-chapter generation could cache the host's words as "lyrics".

## The pipeline (`concert_audio.analyze_concert`)

With the whole concert downloaded once, the analysis can look ahead and behind.
It runs in a plain background thread (numpy; no live capture, so no jank).

1. **Download** the exact video's audio (audio-only, via
   `deep_transcribe._download_audio`). A local `audio_path` or decoded `pcm` can
   be passed instead (tests, evaluation).
2. **Decode** to mono 16 kHz **int16**, streaming, with PyAV (`_decode_pcm16`).
   - The output is bit-identical to faster-whisper's `decode_audio`, which remains
     the fallback.
   - The buffer is pre-sized from the container duration.
   - Every later stage works block by block and converts only small blocks to
     float, so no whole-concert float copy ever exists.
3. **Energy envelope.** For each 0.5 s frame: RMS plus a tonality-gated
   200-3000 Hz ratio. Numerically unchanged for float input.
4. **MC / talk detection** (`_mc_intervals`). See the next section.
5. **Segment.**
   - With YouTube chapters (≥ 2), they seed the song boundaries.
   - Without them, songs are runs of sustained tonal energy ≥
     `concert_audio_min_song_s`, and **MC talk is forced to be a gap**.
   - The vocal floor is keyed to the loud (p90) level of the **non-talk** frames.
6. **Vocal onset per segment.** The first point that stays above the floor for
   ≥ 1.2 s, skipping MC talk plus a margin of half the pulse window (3 s) around
   each talk block. The analysis windows blur talk edges by up to about 3 s, and
   the onset must never land on the host's first or last words.
7. **Partial delivery.** `on_partial` receives the segments (onsets, `mc_frac`)
   and the MC intervals before any fingerprint request, so the runtime can use
   them within seconds.
8. **Identify per segment** (`_identify_segments`).
   - Up to three probes: the onset, onset + 25 s, and the segment midpoint.
     Choruses fingerprint best.
   - A segment stops early once two probes agree.
   - Segments run in a thread pool (`concert_audio_id_workers`, default 3). The
     segment under the playhead goes first, then the ones after it.
   - Majority vote: 2+ agreeing probes → **0.85**; a lone hit → **0.60**;
     conflicting hits → **0.45**.
9. **Merge.** Adjacent ENERGY segments that fingerprint to the same song (≤ 90 s
   apart, both ≥ 0.60) are one song that a spoken-style passage split. They are
   joined, and the MC interval inside them is dropped.
10. **Emit** `{"segments", "mc", "stats", "partial": False}` and **delete the
    audio** in a `finally`. `analyze()` still returns just the segment list, for
    older callers.

## MC / talk detection

The envelope's "vocal" feature cannot tell speech from singing, and the Silero
VAD bundled with faster-whisper calls rap "speech". The detector therefore fuses
three cues. Each is computed per 0.5 s frame from a 32 ms / 15.6 ms STFT:

| Cue | Measures | Talk | Songs |
|---|---|---|---|
| `sp` | Silero speech probability (run only on candidate regions) | 0.6-0.9 | 0.0-0.2 (rap up to 0.67) |
| `pulse` | beat clarity: autocorrelation of the detrended onset envelope, 0.25-1.5 s lags, 6 s window | 0.10-0.20 | 0.3-0.7 (rap included) |
| `lefr` / `depth` | talk-like pauses: share of low-energy frames / dB spread over 3 s | ≥ 0.5 / ≥ 17 dB | 0.2-0.45 / 8-15 dB |

**Rule:** `sp ≥ concert_mc_speech_min` AND `pulse < concert_mc_pulse_max` AND
(`lefr ≥ 0.45` OR `depth ≥ 16 dB`). Then an 11-frame majority vote, a minimum run
of `concert_mc_min_s`, and talk runs closer than 4 s are joined (a cheer
mid-talk). Without the speech model, a strict acoustic rule is used: `pulse <
0.22`, `lefr ≥ 0.52`, `depth ≥ 18 dB`, `lowr ≤ 0.12`.

**Measured** with `scripts/eval_concert_mc.py --songs`, real recordings, nothing
committed:

| | Result |
|---|---|
| Talk flagged, normal mixes (dry, hall with cheers, loud crowd, music bed at −20 / −14 dB) | **92-93 %** |
| Talk flagged, music bed at −8 dB / string bed at −14 dB / extreme arena reverb | 81 % / 70 % / 28 % (misses fall back to today's behaviour) |
| False MC inside 23 real songs (79 min, 3 rap tracks) | **59.5 s = 1.2 %**; rap **0 s** (Silero alone: 498 s) |
| The same songs back to back as one 79-min Opus file | 82.5 s = 1.7 % — the same two songs; the edges shift with the codec and framing |
| Strict acoustic fallback | **0 s** false MC, ~60 % recall on clean talk |
| Songs / applause / instrumentals in the mini-concert | **0 %** |

The residual false positives sit in two sparse, spoken-style songs. That is why
the runtime never lets MC hide a line the lyrics expect (below).

## Memory and time

Measured on a 79-minute Opus/WebM file (a YouTube-like format) on Linux:

| | Before | After |
|---|---|---|
| Peak RSS of the offline pass | **946 MiB** (decode 801 + a 586 MiB float64 copy) | **~295 MiB**, of which ~225 MiB is interpreter + speech model + the int16 PCM itself |
| Decode | 12 s | 9-10 s |
| Onsets and MC available to the runtime | only after fingerprinting | **~14 s** after the download (partial plan) |

## How the engine uses the plan

`_analyze_concert_audio` runs `analyze_concert` in a background thread.
`_apply_concert_plan(seq, plan, mc, partial)` installs its two deliveries.

**Partial first, then final.**
- A late partial never overrides the final.
- A plan with MC intervals but no song segments still installs.
- On the final plan of a chapterless video, the confidently named segments become
  the setlist. That needs `id_conf ≥ chapter_override_min_score` and length ≥
  `concert_plan_min_seg_s`. Both are knobs now; before spec 001 they were a
  hardcoded 0.70 and 8.0.

**Targeted re-anchor instead of a blind re-tick.**
- If the current chapter is in its between-songs hold with the crude
  chapter-start anchor, the measured onset is applied and the hold released.
- If nothing is loaded and nothing is in flight, the tick re-runs, so a confident
  offline id can name a generic chapter.
- Otherwise nothing happens. The old re-tick could release the hold onto a stale
  anchor, or wipe freshly fetched lyrics and refetch.

**Per chapter** (`_concert_setlist_tick`):
- The plan segment is found by chapter.
- It anchors the lyrics to the segment's vocal onset.
- A confident offline id overrides a generic chapter label.
- A chapter measured as **mostly talk** (`mc_frac ≥ concert_mc_chapter_skip_frac`)
  is treated as a non-song segment, whatever its title.

**Runtime MC gate** (`_mc_update` / `_mc_gate_active`, knob `concert_mc_gate`).
MC intervals are shrunk by `concert_mc_edge_s` on each side. While the playhead
is inside one (and the player is PLAYING in a concert), these pause:
- background identify (`_start_identify(user=False)`; user actions pass
  `user=True`);
- recal, health check and boundary re-identify;
- the applause integrator and both concert watchdogs;
- live resync by listening and decide-by-ear (except api / force / wrong);
- decision-engine strikes;
- per-chapter generation and generation chunks.

In-flight results whose capture sat inside MC are dropped. That covers identify
(judged from `t_cap`), align (the heard line's time) and decide (from the
capture window). On **exit** the hold backstop is re-based to the end of the
talk, the stale-song timer restarts, and the recal loop re-arms. Nothing is
forced, so nothing double-fires.

**The MC card.** "🎤 MC — talk between songs" replaces the held last line only
when no line is showing and none is due within `concert_mc_hint_margin_s`. It
never hides lyrics, so a false MC run inside a song is harmless. It is drawn on
the Tk canvas only; the GPU renderer has no hint channel.

## Concert-relative sync

In a concert the offset is about minus the song's start in the video, often
hundreds of seconds. Several mechanisms judged or wrote the ABSOLUTE offset. All
of the fixes below sit behind `concert_relative_sync` (default 1).

| | Before | After |
|---|---|---|
| **A** `align.capture_and_align` | rejected `abs(offset) > 600`, so **resync by listening died after minute 10**; needed a 0.72 match for any song after minute 1 | guards act on the correction `offset − ref_offset`; `_apply_align` also drops a result whose lyrics or anchor changed mid-capture, and never writes 0.0 in a concert |
| **B** `_on_vocal_onset` | compared the raw video position with song-relative limits, so it **always rejected** in a concert (logged every 90 ms); the hold ended only on its 20 s timer | during a chapter hold, the intro is measured from the anchor (or the MC exit); only "vocals later than expected" shifts apply; the reject log is rate-limited |
| **D** Shazam / OCR / decide-by-ear switch | wrote `offset = 0.0`, i.e. raw video time, so a correctly identified song was **blank**, and the absolute sync-read cap discarded every Shazam read that could have fixed it | `_set_switch_offset`: Shazam's own timing when plausible, else the plan onset or chapter start, else "starting now"; the cap is relative; a hit during a hold anchors in one read |
| **E** chapter tick | a chapter entered during the 90 s sound-lock hold was **skipped forever** | re-evaluated once the lock ages out, unless it is already the loaded song |
| Generation | stamped lines in VIDEO time while the display ran on the song clock, so **generated concert lyrics never showed** | lines stamped on the display clock (`position + offset`) |

**Hypothesis, not proven.** The `drift = -748s` *Melt* read in the Offkai
log is what Bug D produces: a song switched in at video ≈ 12:40 with offset
0.0 is "748 s off". It may never have been a live-arrangement timing problem.

## Tuning knobs (`/tune`)

| Knob | Default | Meaning |
|---|---|---|
| `concert_audio_on` | 1 | master switch for the offline pass |
| `concert_audio_identify` | 1 | fingerprint each segment (offline Shazam) |
| `concert_audio_max_dur_s` | 4800 | reject videos longer than this (not one concert) |
| `concert_audio_min_song_s` | 45 | min sustained song length (energy segmentation) |
| `concert_audio_floor_frac` | 0.40 | vocal floor as a fraction of the loud (p90) non-talk level |
| `concert_audio_id_slice_s` | 12 | seconds fingerprinted per probe |
| `concert_audio_id_workers` | 3 | segments fingerprinted concurrently |
| `chapter_override_min_score` | 0.70 | min offline-id confidence to override a generic chapter label **and** to build a chapterless setlist entry |
| `concert_plan_min_seg_s` | 8.0 | min segment length for a chapterless setlist entry |
| `concert_mc_detect` | 1 | offline MC / talk detection |
| `concert_mc_min_s` | 10.0 | shortest talk run that counts as MC |
| `concert_mc_speech_min` | 0.50 | speech probability needed |
| `concert_mc_pulse_max` | 0.30 | beat clarity above this means music (keeps rap out) |
| `concert_mc_gate` | 1 | runtime pause of the listening machinery during MC |
| `concert_mc_edge_s` | 2.0 | shrink each MC interval by this much per side |
| `concert_mc_hint_margin_s` | 3.0 | no MC card this close to an expected line |
| `concert_mc_chapter_skip_frac` | 0.80 | a chapter this much talk is a non-song segment |
| `concert_relative_sync` | 1 | concert-relative offsets (A / B / D / generation fixes) |

## Cost and privacy

The offline pass costs one audio download per concert (about 1 MB/min, deleted
after analysis), a few seconds of numpy, the local Silero model on the few
minutes that could be talk, and up to three fingerprint probes per segment.

What leaves the machine:
- Raw audio slices go to Shazam, never a title, account or device id. This is
  the same as the live recognizer.
- The speech model runs **locally**.
- Nothing is written to disk except the in-memory plan. The audio file never
  outlives the analysis.
