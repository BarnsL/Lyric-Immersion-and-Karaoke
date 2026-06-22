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

_DUR = 6  # seconds to sample
_SR = 44100


def _capture(seconds=_DUR):
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
    from shazamio import Shazam
    out = await Shazam().recognize(wav_bytes)
    track = (out or {}).get("track") or {}
    title = track.get("title")
    artist = track.get("subtitle")
    return (title, artist) if title else (None, None)


def recognize_playing(seconds=_DUR):
    """Return (title, artist) of the currently playing audio, or (None, None)."""
    try:
        wav = _capture(seconds)
        if not wav:
            return (None, None)
        return asyncio.run(_shazam(wav))
    except Exception as e:
        return (None, None)


if __name__ == "__main__":
    print("Listening to system audio…")
    t, a = recognize_playing()
    print(f"Heard: {t!r} — {a!r}" if t else "Could not identify the audio.")
