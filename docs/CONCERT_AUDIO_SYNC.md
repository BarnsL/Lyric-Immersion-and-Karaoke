# Concert Audio Sync (offline analysis)

*Introduced v1.1.57.* Files: [`concert_audio.py`](../concert_audio.py),
integration in [`main.py`](../main.py) (`_analyze_concert_audio`,
`_apply_concert_plan`, `_plan_for_pos`, `_concert_setlist_tick`), and the
offline-Shazam helper `recognize.identify_pcm`.

## The problem

A multi-song VTuber 3D-live / concert is the hardest sync case, and real-time
recognition fails three ways at once:

1. **Live arrangement ≠ studio recording.** Shazam fingerprints the live
   performance against studio masters, so it either misses or returns a wildly
   wrong offset. A real log line from the Phase Connect *Offkai Expo Gen4*
   concert: matched `Melt` but `drift = -748s` — twelve minutes off — because the
   live arrangement's timing doesn't line up with the studio LRC.
2. **The live recognizer janks.** WASAPI capture + fingerprint is GIL-heavy, so
   on a busy concert frame the smoothness backoff kills the recognize child
   (`terminated recognize child (frame 3296ms)`, `delaying auto identify 60s`)
   and the concert goes unidentified for a minute at a time.
3. **Applause / MC / intros.** The first 20-60s of each song is not singing, so
   anchoring lyrics to the raw clock — or even the chapter start — shows them
   during the crowd noise.

There was also a category bug: VTuber concerts are tagged **Entertainment** or
**Film & Animation**, not Music, and the old setlist gate hard-disabled the
whole concert pipeline for any non-Music category. That alone is why *"not a
single song is getting identified"* on these uploads.

## The approach: analyse the whole thing offline

With the entire concert downloaded once, we can look ahead and behind — something
a live listener can't — and do the analysis in a plain background thread (numpy,
no live capture, so no jank). The pipeline (`concert_audio.analyze`):

1. **Download** the exact video's audio (audio-only, via
   `deep_transcribe._download_audio`, which carries the anti-bot resilience and a
   duration cap we lift for concert lengths).
2. **Decode** to mono 16 kHz PCM (`faster_whisper.audio.decode_audio`, PyAV — no
   ffmpeg binary needed).
3. **Energy envelope.** Per 0.5s frame: RMS loudness, and a **tonality-gated
   200-3000 Hz vocal-band ratio**. The tonality gate (spectral flatness) is what
   separates *singing* (tonal, high band ratio) from *applause/cheer* (broadband,
   high flatness → scored near zero). This mirrors the live detector in
   `songchange.py`, vectorised over the whole file in memory-bounded blocks.
4. **Segment.** If the video has YouTube **chapters**, they seed the song
   boundaries; otherwise songs are derived from the envelope directly (runs of
   sustained vocal energy ≥ `concert_audio_min_song_s`, separated by quiet/
   applause gaps). The vocal floor is keyed to the **loud level** (a fraction of
   the 90th-percentile vocal energy), *not* the median — in a concert the singing
   is the majority of the runtime, so a median floor would sit inside the song
   level and find nothing.
5. **Vocal onset per segment.** Walk the vocal frames from each segment start and
   take the first point that stays above the floor for ≥ ~1.2s. That's the lyric
   **anchor** — lyrics start on the first sung word, past the intro/applause.
6. **Identify per segment.** Fingerprint a ~12s slice at the onset (and a second
   probe ~25s later — a chorus is often a stronger match) with
   `recognize.identify_pcm`, the offline counterpart to the live recognizer. It
   accepts raw PCM, so no device enumeration and no jank.
7. **Emit a plan** — one entry per song: `{start, end, onset, title, artist,
   chapter, source, id_conf}` — and **delete the audio** (temp dir removed in a
   `finally`; only the small plan survives).

## How the engine uses the plan

`_analyze_concert_audio` runs the analysis in a background thread when a
live/concert video is detected (and it's not a non-music video). The result is
installed by `_apply_concert_plan`:

- **When the video had chapters**, the plan *refines* them: `_concert_setlist_tick`
  anchors each song to the plan's **vocal onset** (`offset = -onset`) instead of
  the raw chapter start, and a confident offline id **overrides a generic chapter
  label** (the `La La La → Melt` correction) while a *distinctive* human-authored
  label (`Face It`) is kept over a stray slice mis-ID. By-ear/decision-engine
  correction still has the final say either way — the plan is a strong default,
  not a lock, so it stays **correctable**.
- **When the video had no usable chapters**, the energy-derived segments *become*
  the setlist (`_apply_concert_plan` synthesises `_concert_setlist` from the
  plan), each labelled by its offline id, and the existing per-song tick drives
  loading.

Songs Shazam can't place (VTuber originals not in its DB) stay unlabelled and
fall through to the existing per-chapter by-ear generation — but now with the
correct **onset anchor**, so even a generated body lands in the right place.

## Validation (real concert)

`https://www.youtube.com/watch?v=Gl9B6D3ru7M` — Offkai Expo Gen4, 43 min,
category `Entertainment`, 11 chapters. Offline analysis (download + decode + 11
fingerprints) ran in ~60s and produced onsets that skip the crowd noise (Intro
13s, `Kton Boogie` @205 → onset 229, `Fuura` @2035 → onset 2056) and corrected
labels (`Kton Boogie → Konton Boogie`, and critically **`La La La → Melt`**, the
song live-Shazam kept missing at -748s).

## Tuning knobs (`/tune`)

| knob | default | meaning |
|---|---|---|
| `concert_audio_on` | 1 | master switch for the offline pass |
| `concert_audio_identify` | 1 | fingerprint each segment (offline Shazam) |
| `concert_audio_max_dur_s` | 4800 | reject videos longer than this (not one concert) |
| `concert_audio_min_song_s` | 45 | min sustained song length (energy/no-chapter mode) |
| `concert_audio_floor_frac` | 0.40 | vocal floor as a fraction of the loud (p90) level |
| `concert_audio_id_slice_s` | 12 | seconds fingerprinted per segment |

## Cost & privacy

One audio download per concert (~1 MB/min, deleted after analysis), a few
seconds of numpy, and one Shazam fingerprint per segment. Only raw audio is sent
to Shazam — never a title, account, or device id — identical to the live
recognizer. Nothing is written to disk except the in-memory plan; the audio file
never outlives the analysis.
