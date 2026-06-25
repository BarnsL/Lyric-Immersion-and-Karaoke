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
        while not self._stop_evt.is_set():
            if not self._enabled:
                time.sleep(0.3)
                continue
            try:
                loop = self._find_loopback(sc)
                if loop is None:
                    time.sleep(2.0)
                    continue
                with loop.recorder(samplerate=self.sr, channels=1) as rec:
                    backoff = 1.0
                    self._reset()
                    n = max(1, int(self.sr * self.block))
                    while not self._stop_evt.is_set() and self._enabled:
                        data = rec.record(numframes=n)
                        self._feed(np, data)
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

        if is_silent:
            self._silent_for += dt
            self._music_for = 0.0
            self._onset_fired = False       # arm the next quiet→music onset
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

    def _vocal_ratio(self, np, data):
        """Fraction of spectral energy in the vocal band (200-3000 Hz).
        Cheap real FFT (~0.5 ms on a 0.2 s @ 16 kHz block)."""
        try:
            flat = data.flatten() if hasattr(data, "flatten") else data
            n = len(flat)
            if n < 256:
                return None
            spec = np.abs(np.fft.rfft(flat * np.hanning(n)))
            total = spec.sum() + 1e-9
            freqs = np.fft.rfftfreq(n, d=1.0 / self.sr)
            band = spec[(freqs >= 200) & (freqs <= 3000)].sum()
            return float(band / total)
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
