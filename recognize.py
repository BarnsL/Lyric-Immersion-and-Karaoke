"""
Audio recognition — identify the playing song by SOUND, not by title.

Captures a few seconds of the system's audio output (WASAPI loopback) and
asks Shazam what it is. This is how covers, mislabeled YouTube uploads, and
DJ mixes get identified correctly when the window/title is wrong or missing.

    from recognize import recognize_playing
    title, artist = recognize_playing()   # (None, None) if not identified

Sources / notes:
  • soundcard — WASAPI loopback capture of the default output device.
  • shazamio  — unofficial Shazam client; fingerprints the clip locally and
                queries Shazam's public recognition endpoint.
  • Only raw audio is sent to Shazam — never any title, account, or device
    information.
"""

import asyncio
import io
import wave

_DUR = 8  # seconds to sample (longer = better recognition)
_SR = 44100


def _capture(seconds=_DUR):
    """Record `seconds` of the system's audio output (WASAPI loopback) and return
    it as in-memory WAV bytes, or None if no loopback device is found."""
    import numpy as np
    import soundcard as sc

    spk = sc.default_speaker()
    mics = sc.all_microphones(include_loopback=True)
    loop = next((m for m in mics if getattr(m, "isloopback", False)
                 and spk.name in m.name), None)
    loop = loop or next((m for m in mics if getattr(m, "isloopback", False)), None)
    if loop is None:
        return None
    with loop.recorder(samplerate=_SR, channels=2) as rec:
        data = rec.record(numframes=_SR * seconds)

    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(pcm.shape[1] if pcm.ndim > 1 else 1)
        w.setsampwidth(2)
        w.setframerate(_SR)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


async def _shazam(wav_bytes):
    """Fingerprint the WAV clip and ask Shazam to identify it. Returns
    (title, artist, offset) — offset = seconds into the matched song — or
    (None, None, None) if nothing matched."""
    from shazamio import Shazam
    out = await Shazam().recognize(wav_bytes)
    track = (out or {}).get("track") or {}
    title = track.get("title")
    artist = track.get("subtitle")
    matches = (out or {}).get("matches") or []
    offset = matches[0].get("offset") if matches else None  # secs into the song
    return (title, artist, offset) if title else (None, None, None)


def recognize_playing(seconds=_DUR, attempts=2):
    """Identify the playing audio. Returns (title, artist, offset, t_cap):
      • offset = seconds into the song where the captured clip starts
      • t_cap  = wall-clock time when that clip's capture began
    so the caller can align its playback clock to the real song position.
    (None, None, None, None) if not identified."""
    import time
    for _ in range(max(1, attempts)):
        try:
            t_cap = time.time()
            wav = _capture(seconds)
            if not wav:
                return (None, None, None, None)
            t, a, off = asyncio.run(_shazam(wav))
            if t:
                return (t, a, off, t_cap)
        except Exception:
            pass
    return (None, None, None, None)


if __name__ == "__main__":
    print("Listening to system audio…")
    t, a, off, _ = recognize_playing()
    print(f"Heard: {t!r} — {a!r}  (offset {off}s)" if t
          else "Could not identify the audio.")
