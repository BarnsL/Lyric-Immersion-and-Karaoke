"""
Song-boundary detector — catch the *instant* one song ends and the next begins
inside a single long video (a compilation, "openings 1-26", a DJ set, an album
upload). The player's title never changes across such a video, so the only way
to know a new song started is to listen.

Doing that with Shazam on a fixed timer is wasteful: a fingerprint+network call
every few seconds, all video long, just to notice the rare moment a track flips.
This module is the cheap front end to that. It watches the system audio's
*loudness* with a tiny RMS meter (numpy on short, low-rate blocks — a few
wake-ups a second, negligible CPU) and fires a callback only when it sees the
tell-tale shape of a track change: a brief dip toward silence after a stretch of
music, then sound returning. The overlay reacts by re-identifying *right then*
(fast, event-driven) instead of polling blindly — so switches are quicker AND
the costly recognizer can idle between songs.

    det = SongChangeDetector(on_change=lambda: ...)   # called from this thread
    det.start()
    ...
    det.stop()

Honest limits: a *crossfaded* compilation (no gap between tracks) won't trip the
silence gate — the overlay keeps a slow Shazam heartbeat as a backstop for that.
Only loudness is analysed; no audio is stored, fingerprinted, or sent anywhere.
"""
from __future__ import annotations

import threading
import time


class SongChangeDetector(threading.Thread):
    """Background thread that calls `on_change()` when the audio output looks
    like it crossed a song boundary (a short near-silent gap between tracks).

    Parameters are deliberately conservative to avoid false alarms on a quiet
    musical passage: silence is judged against both an absolute floor and a
    fraction of the recent loud level, must persist for `min_gap` seconds, and
    must be *preceded by music*; after firing, it stays quiet for `debounce`
    seconds (no song flips twice in a breath)."""

    def __init__(self, on_change, *, on_onset=None, on_vocal=None, block=0.2,
                 samplerate=16000, min_gap=0.30, min_music=1.2, debounce=5.0,
                 abs_silence=3.0e-3, rel_silence=0.10,
                 min_quiet=1.5, min_sustain=0.6):
        super().__init__(daemon=True)
        self.on_change = on_change
        # on_onset(): fired when sustained music BEGINS after a quiet stretch — the
        # tell-tale of a cinematic/MV dead-space intro ending and the song kicking
        # in. Unlike on_change (which needs music→silence→music), this needs only
        # quiet→music, so it catches a *leading* intro that no music preceded. The
        # overlay uses it to anchor lyric timing for songs Shazam can't ID.
        self.on_onset = on_onset
        # on_vocal(): fired when SINGING starts — band-energy in 200-3000 Hz
        # (the vocal range) rises and sustains. Catches "instrumental intro →
        # vocals start" transitions that on_onset misses (no silent gap before
        # the intro, music plays throughout). The overlay uses this to calibrate
        # offset for music videos with long instrumental intros (Grimes Genesis
        # has ~70s intro; without this the lyrics start showing at video time
        # 0:01 even though singing doesn't begin until 1:10).
        self.on_vocal = on_vocal
        self.block = block
        self.sr = int(samplerate)
        self.min_gap = min_gap
        self.min_music = min_music
        self.debounce = debounce
        self.abs_silence = abs_silence
        self.rel_silence = rel_silence
        self.min_quiet = min_quiet        # leading quiet (s) needed to call it an intro
        self.min_sustain = min_sustain    # music must hold this long (s) to be an onset

        self._enabled = True
        self._vocal_enabled = True        # turned off after firing once per track
        self._stop_evt = threading.Event()
        # rolling state
        self._loud_ema = 0.0       # EMA of RMS during music — the "loud" level
        self._music_for = 0.0      # seconds of continuous music seen
        self._silent_for = 0.0     # seconds of continuous near-silence
        self._had_music = False    # saw a real music run before the current gap
        self._in_gap = False       # currently inside a qualifying silent gap
        self._last_fire = 0.0
        self._pre_quiet = 0.0      # length of the quiet stretch the current music followed
        self._onset_fired = False  # onset already fired for the current music run
        # vocal-onset state
        self._vocal_for = 0.0      # seconds of sustained vocal-band dominance
        self._vocal_fired = False  # vocal onset already fired for the current track
        self._vocal_baseline = 0.0 # baseline vocal-band ratio from instrumental-only
        self._vocal_samples = 0    # samples seen for baseline establishment
        # rolling vocal-activity buffer: (wall_time, vocal_ratio) per block. The
        # main thread reads this to cross-correlate the audio's "vocals on/off"
        # pattern against the LRC's expected line-active intervals — a
        # Whisper-free auto-sync that catches drift even when faster-whisper
        # isn't bundled (the karaoke .exe is lean by default).
        self._vocal_buf = []       # list of (t_wall, ratio) — last ~60 s
        self._buf_lock = threading.Lock()
        self.active_device = None  # TICKET-168: loopback we're bound to (name)
        # latest per-block values, for the live audio-listener diagnostic
        self._live_rms = 0.0
        self._live_vr = None
        self._live_silent = True
        self._live_flatness = None    # spectral flatness of vocal band (noise indicator)
        self._live_t = 0.0
        self._blocks_seen = 0

    # ── control ──
    def set_enabled(self, on: bool):
        """Turn analysis on/off without tearing down the thread."""
        self._enabled = bool(on)
        if not on:
            self._reset()

    def stop(self):
        self._stop_evt.set()

    def _reset(self):
        self._music_for = self._silent_for = 0.0
        self._had_music = False
        self._in_gap = False
        self._pre_quiet = 0.0
        self._onset_fired = False
        self._vocal_for = 0.0
        self._vocal_baseline = 0.0
        self._vocal_samples = 0

    def reset_vocal(self):
        """Re-arm vocal-onset detection for a new track. The baseline rebuilds
        from the next instrumental block."""
        self._vocal_enabled = True
        self._vocal_fired = False
        self._vocal_for = 0.0
        self._vocal_baseline = 0.0
        self._vocal_samples = 0

    # ── the loop ──
    def run(self):
        # Imported lazily so the app still starts if audio capture is unavailable.
        try:
            import numpy as np
            import soundcard as sc
        except Exception:
            return

        backoff = 1.0
        probe_next = False        # TICKET-168: next open should HUNT for signal
        while not self._stop_evt.is_set():
            if not self._enabled:
                time.sleep(0.3)
                continue
            try:
                loop = None
                if probe_next:
                    # The last binding produced junk (silence or a flat mask):
                    # the default speaker is NOT where the music renders. Probe
                    # every loopback and bind the one actually carrying audio.
                    loop = self._probe_best_loopback(sc, np)
                    probe_next = False
                loop = loop or self._find_loopback(sc)
                if loop is None:
                    time.sleep(2.0)
                    continue
                # TICKET-167: remember WHICH default speaker this recorder is
                # bound to. WASAPI loopback keeps reading the ORIGINAL endpoint
                # after the user switches the default output device — it then
                # hears pure silence forever: no vocal mask (energy-align blind
                # for whole songs), no boundaries, no vocal onsets (MV-intro
                # anchor never fires). Live-caught on a YOASOBI live cut.
                bound_name = getattr(loop, "name", "") or ""
                self.active_device = bound_name    # for /diag + recognize child
                try:
                    _spk = sc.default_speaker()
                    bound_default = bool(_spk and _spk.name and _spk.name in bound_name)
                except Exception:
                    bound_default = False
                with loop.recorder(samplerate=self.sr, channels=1) as rec:
                    backoff = 1.0
                    self._reset()
                    n = max(1, int(self.sr * self.block))
                    blocks = 0
                    silence_run = 0.0
                    while not self._stop_evt.is_set() and self._enabled:
                        data = rec.record(numframes=n)
                        self._feed(np, data)
                        blocks += 1
                        # ── stale-device watchdog (TICKET-167) ──
                        # 1) Every ~5 s: if the DEFAULT speaker changed away
                        #    from the device we bound, reopen on the new one.
                        #    (Only when we bound the default — the any-loopback
                        #    fallback has no name relationship to check.)
                        if bound_default and blocks % 25 == 0:
                            try:
                                _spk = sc.default_speaker()
                                if _spk and _spk.name and _spk.name not in bound_name:
                                    break            # → reopen on new default
                            except Exception:
                                pass
                        # 2) TICKET-168 FLAT-MASK watchdog: 150+ recent ratio
                        #    samples with ~zero spread = the endpoint carries a
                        #    CONSTANT signal (junk), not music — the ZAWA MAKE IT
                        #    signature (on=150 off=0 spread=0.000, zero Shazam).
                        #    Explicit-silence zeros make this catch dead-silent
                        #    streams within ~30 s too.
                        if blocks % 25 == 0 and blocks >= 150:
                            with self._buf_lock:
                                tail = [r for (_, r) in self._vocal_buf[-150:]]
                            if len(tail) >= 150 and (max(tail) - min(tail)) < 1e-4:
                                probe_next = True
                                break            # → reopen via signal probe
                        # 3) 60 s of CONTINUOUS silence → reopen regardless.
                        #    Covers same-name re-routes and endpoint invalidation
                        #    the name check can't see. A genuinely paused system
                        #    just re-opens the same device once a minute (cheap).
                        if self._live_silent:
                            silence_run += self.block
                            if silence_run >= 60.0:
                                probe_next = True
                                break                # → reopen via signal probe
                        else:
                            silence_run = 0.0
            except Exception:
                # Device changed / busy / went away — back off and re-open.
                self._reset()
                time.sleep(min(backoff, 5.0))
                backoff = min(backoff * 2, 5.0)

    @staticmethod
    def _find_loopback(sc):
        """The WASAPI loopback mic for the current default speaker (so we hear
        exactly what's playing), falling back to any loopback device."""
        try:
            spk = sc.default_speaker()
            mics = sc.all_microphones(include_loopback=True)
        except Exception:
            return None
        loop = next((m for m in mics if getattr(m, "isloopback", False)
                     and spk and spk.name in m.name), None)
        return loop or next((m for m in mics
                             if getattr(m, "isloopback", False)), None)

    def _probe_best_loopback(self, sc, np, per_dev_s=0.5):
        """TICKET-168: find the loopback that is actually CARRYING audio.
        The default speaker is not always where the music renders (multi-
        device setups: HDMI displays, headsets, virtual devices) — binding
        the default fed a constant junk mask for entire songs (spread=0.000,
        zero Shazam IDs). Record a short block from every loopback and pick
        the strongest VARYING signal; None when everything is silent."""
        best = None
        try:
            mics = [m for m in sc.all_microphones(include_loopback=True)
                    if getattr(m, "isloopback", False)]
        except Exception:
            return None
        for m in mics:
            try:
                with m.recorder(samplerate=self.sr, channels=1) as rec:
                    rec.record(numframes=1024)              # prime the stream
                    data = rec.record(numframes=int(self.sr * per_dev_s))
                rms = float(np.sqrt(np.mean(np.square(data, dtype="float64")) + 1e-12))
                var = float(np.std(np.abs(data)))
                if rms > 1e-4 and (best is None or (rms + var) > best[0]):
                    best = (rms + var, m)
            except Exception:
                continue
        return best[1] if best else None

    # ── analysis of one block ──
    def _feed(self, np, data):
        if data is None or len(data) == 0:
            return
        rms = float(np.sqrt(np.mean(np.square(data, dtype="float64")) + 1e-12))
        dt = self.block

        # Track the recent "music" level (only when clearly above silence) so the
        # silence test adapts to quiet vs. loud songs.
        if rms > max(self.abs_silence * 2, 0.02):
            self._loud_ema = rms if self._loud_ema == 0 else \
                0.9 * self._loud_ema + 0.1 * rms

        floor = max(self.abs_silence, self.rel_silence * self._loud_ema)
        is_silent = rms < floor
        self._live_rms = rms
        self._live_silent = bool(is_silent)
        self._live_t = time.time()
        self._blocks_seen += 1

        if is_silent:
            self._silent_for += dt
            self._music_for = 0.0
            self._onset_fired = False       # arm the next quiet→music onset
            # TICKET-167: record EXPLICIT silence in the vocal mask. The
            # energy correlator aligns vocal-ON vs line-active intervals — the
            # OFF samples carry half the signal, and the ≥12 s history minimum
            # must stay reachable through the quiet passages of live cuts.
            self._live_vr = 0.0
            with self._buf_lock:
                self._vocal_buf.append((time.time(), 0.0))
                if len(self._vocal_buf) > 300:
                    del self._vocal_buf[: len(self._vocal_buf) - 300]
            # A qualifying gap = enough silence, and it followed real music.
            # `had_music` is latched while music played, so this still holds even
            # though music_for was just zeroed on the first silent block.
            if (not self._in_gap and self._silent_for >= self.min_gap
                    and self._had_music):
                self._in_gap = True
                self._had_music = False    # require fresh music for the NEXT gap
        else:
            # Sound returned. If it ended a qualifying gap, that's a boundary.
            if self._in_gap:
                self._fire()
            self._in_gap = False
            if self._silent_for > 0.0:
                self._pre_quiet = self._silent_for   # remember the quiet we just left
            self._silent_for = 0.0
            self._music_for += dt
            # Onset: sustained music after a real quiet stretch (a dead-space /
            # cinematic intro ending). Fires once per quiet→music transition; the
            # overlay only acts on the FIRST one of a track (the leading intro).
            if (self.on_onset and not self._onset_fired
                    and self._music_for >= self.min_sustain
                    and self._pre_quiet >= self.min_quiet):
                self._onset_fired = True
                self._fire_onset()
            if self._music_for >= self.min_music:
                self._had_music = True
            # ── VOCAL band-energy ratio (always computed) ──
            # Stored in the rolling buffer for auto-sync cross-correlation;
            # also used for one-shot vocal-onset detection during instrumental
            # intros (see Grimes "Genesis" notes above).
            vr = self._vocal_ratio(np, data)
            if vr is not None:
                tnow = time.time()
                self._live_vr = vr
                with self._buf_lock:
                    self._vocal_buf.append((tnow, vr))
                    # keep the last ~60 s (300 blocks @ 0.2 s)
                    if len(self._vocal_buf) > 300:
                        del self._vocal_buf[: len(self._vocal_buf) - 300]
                if self.on_vocal and self._vocal_enabled and not self._vocal_fired:
                    # Baseline from the first ~5 s (25 blocks @ 0.2 s) — assume
                    # instrumental-only at the very start of a track.
                    if self._vocal_samples < 25:
                        self._vocal_samples += 1
                        self._vocal_baseline = (
                            (self._vocal_baseline * (self._vocal_samples - 1) + vr)
                            / self._vocal_samples)
                    else:
                        thresh = max(0.55, self._vocal_baseline * 1.4)
                        if vr >= thresh:
                            self._vocal_for += dt
                            if self._vocal_for >= 1.0:
                                self._vocal_fired = True
                                self._fire_vocal()
                        else:
                            self._vocal_for = max(0.0, self._vocal_for - dt)

    def vocal_history(self, seconds=30.0):
        """Snapshot of the recent vocal-band ratio: list of (t_wall, ratio).
        The auto-sync correlator uses this to align audio's vocal-mask to
        the LRC's expected line-active intervals without Whisper."""
        now = time.time()
        with self._buf_lock:
            return [(t, r) for (t, r) in self._vocal_buf if now - t <= seconds]

    def vocal_baseline(self):
        """Instrumental-only baseline ratio learned at the start of the track.
        0.0 if not yet established."""
        return self._vocal_baseline

    def live_audio(self):
        """Live audio-listener snapshot: what the loopback is hearing RIGHT NOW.
        Lets the diagnostics show whether audio is even flowing, the current
        loudness, and the live vocal-band ratio + on/off classification — so a
        sync problem can be traced to 'no audio', 'all silence', or 'vocals not
        detected' rather than guessing."""
        now = time.time()
        with self._buf_lock:
            buf = self._vocal_buf[-30:]                # last ~6 s
            tail150 = [r for (_, r) in self._vocal_buf[-150:]]
        ratios = [r for (_, r) in buf]
        # adaptive on/off split, mirrors the correlator's logic
        on = off = 0
        thresh = None
        if len(ratios) >= 4:
            sr = sorted(ratios)
            med = sr[len(sr) // 2]
            p75 = sr[int(0.75 * (len(sr) - 1))]
            thresh = med + 0.5 * (p75 - med)
            on = sum(1 for r in ratios if r >= thresh)
            off = len(ratios) - on
        return {
            "capturing": (now - self._live_t) < 2.0 if self._live_t else False,
            "age_s": round(now - self._live_t, 2) if self._live_t else None,
            "rms": round(self._live_rms, 5),
            "loud_ema": round(self._loud_ema, 5),
            "is_silent": self._live_silent,
            "vocal_ratio": round(self._live_vr, 3) if self._live_vr is not None else None,
            "vocal_detected_now": (self._live_vr is not None and thresh is not None
                                   and self._live_vr >= thresh),
            "band_flatness": round(self._live_flatness, 3) if self._live_flatness is not None else None,
            "noise_like": (self._live_flatness is not None and self._live_flatness > 0.55),
            "window_on_blocks": on,
            "window_off_blocks": off,
            "window_thresh": round(thresh, 3) if thresh is not None else None,
            "vocal_baseline": round(self._vocal_baseline, 3),
            "buffer_len": len(self._vocal_buf),
            "blocks_seen": self._blocks_seen,
            "device": self.active_device,
            "flat_mask": (len(tail150) >= 150
                          and (max(tail150) - min(tail150)) < 1e-4),
            "music_for_s": round(self._music_for, 2),
            "silent_for_s": round(self._silent_for, 2),
        }

    def _vocal_ratio(self, np, data):
        """A NOISE-ROBUST "vocalness" score for the 200-3000 Hz band.

        Plain band-energy fraction can't tell SINGING from GAME NOISE — gunfire,
        explosions and UI clicks dump energy into the vocal band too, and on the
        system loopback (which hears the game AND the music) that would create
        false "vocal" blocks and corrupt the sync correlation.

        The discriminator is TONALITY: a sung/spoken voice (and pitched music)
        is harmonic — energy concentrated at a few frequencies → LOW spectral
        flatness. Broadband game SFX is noise-like → HIGH spectral flatness. So
        we scale the band-energy fraction down when the band looks broadband,
        keeping the vocal mask clean while a game is playing.

        Cheap: one real FFT + a flatness ratio (~0.6 ms on a 0.2 s @ 16 kHz block)."""
        try:
            flat = data.flatten() if hasattr(data, "flatten") else data
            n = len(flat)
            if n < 256:
                return None
            spec = np.abs(np.fft.rfft(flat * np.hanning(n)))
            total = spec.sum() + 1e-9
            freqs = np.fft.rfftfreq(n, d=1.0 / self.sr)
            bandmask = (freqs >= 200) & (freqs <= 3000)
            band_spec = spec[bandmask]
            ratio = float(band_spec.sum() / total)
            # spectral flatness of the vocal band: geometric/arithmetic mean of
            # the power spectrum. ~0 = pure tone, ~1 = white noise. Voice/music
            # sit low (~0.1-0.35); broadband SFX sits high (~0.5+).
            ps = (band_spec * band_spec) + 1e-12
            flatness = float(np.exp(np.mean(np.log(ps))) / np.mean(ps))
            self._live_flatness = flatness
            # Tonality gate: full weight while tonal, ramping to zero as the band
            # turns broadband (game noise). 1.0 at flatness≤0.35, 0 at ≥0.65.
            tonal = max(0.0, min(1.0, (0.65 - flatness) / 0.30))
            return ratio * tonal
        except Exception:
            return None

    def _fire(self):
        now = time.time()
        if now - self._last_fire < self.debounce:
            return
        self._last_fire = now
        try:
            self.on_change()
        except Exception:
            pass

    def _fire_onset(self):
        """Music just started after a quiet stretch — likely a cinematic/MV intro
        ending. Passes the length of that leading quiet so the overlay can tell a
        real leading intro from a brief mid-song breakdown. Best-effort."""
        try:
            self.on_onset(self._pre_quiet)
        except Exception:
            pass

    def _fire_vocal(self):
        """Vocals just started — sustained vocal-band energy rise. Best-effort."""
        try:
            self.on_vocal()
        except Exception:
            pass
