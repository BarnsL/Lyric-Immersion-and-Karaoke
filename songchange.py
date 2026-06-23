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

    def __init__(self, on_change, *, on_onset=None, block=0.2, samplerate=16000,
                 min_gap=0.30, min_music=1.2, debounce=5.0,
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
        ending. Best-effort; the overlay decides whether it matters."""
        try:
            self.on_onset()
        except Exception:
            pass
