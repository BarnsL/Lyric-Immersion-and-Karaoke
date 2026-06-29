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
    ``(wav_bytes, t_cap)`` where `t_cap` is the wall-clock instant recording
    actually began — or ``(None, None)`` if no loopback device.

    `t_cap` is stamped INSIDE the recorder, NOT before this call: device
    enumeration (`default_speaker` + `all_microphones` + opening the stream) can
    take 0.5–1 s, and stamping `t_cap` before that made the sync calibration think
    the song was ~1 s earlier than it was — the cause of the up-to-1-second sync
    error. Anchoring `t_cap` to the real recording start removes that bias."""
    import time
    import numpy as np
    import soundcard as sc

    spk = sc.default_speaker()
    mics = sc.all_microphones(include_loopback=True)
    loop = next((m for m in mics if getattr(m, "isloopback", False)
                 and spk.name in m.name), None)
    loop = loop or next((m for m in mics if getattr(m, "isloopback", False)), None)
    if loop is None:
        return None, None
    with loop.recorder(samplerate=_SR, channels=2) as rec:
        rec.record(numframes=256)       # prime the stream so the next read is live
        t_cap = time.time()             # clock anchor = real recording start
        data = rec.record(numframes=_SR * seconds)

    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(pcm.shape[1] if pcm.ndim > 1 else 1)
        w.setsampwidth(2)
        w.setframerate(_SR)
        w.writeframes(pcm.tobytes())
    return buf.getvalue(), t_cap


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
    for _ in range(max(1, attempts)):
        try:
            wav, t_cap = _capture(seconds)
            if not wav:
                return (None, None, None, None)
            t, a, off = asyncio.run(_shazam(wav))
            if t:
                return (t, a, off, t_cap)
        except Exception:
            pass
    return (None, None, None, None)


if __name__ == "__main__":
    import sys as _sys
    if "--child" in _sys.argv[1:]:
        # SUBPROCESS mode (TICKET-135): the parent runs identify in a child
        # PROCESS so the GIL-heavy capture+fingerprint can't stall its render
        # thread. Writes the result to --out (or stdout). Fully wrapped so the
        # child can NEVER raise an unhandled exception.
        try:
            import json as _json
            _a = _sys.argv[1:]
            _i = _a.index("--child")
            def _val(k):
                v = _a[_i + k] if _i + k < len(_a) else None
                return v if (v and not v.startswith("--")) else None
            def _flag(name):
                try:
                    j = _a.index(name)
                    return _a[j + 1] if j + 1 < len(_a) else None
                except ValueError:
                    return None
            _secs = float(_val(1) or _DUR)
            _atts = int(_val(2) or 1)
            _out = _flag("--out")
            try:
                t, a, off, tc = recognize_playing(_secs, _atts)
                _res = {"t": t, "a": a, "off": off, "tc": tc}
            except Exception as e:
                _res = {"t": None, "err": str(e)}
            try:
                if _out:
                    with open(_out, "w", encoding="utf-8") as _f:
                        _f.write(_json.dumps(_res))
                else:
                    _sys.stdout.write(_json.dumps(_res) + "\n")
                    _sys.stdout.flush()
            except Exception:
                pass
        except Exception:
            pass
    else:
        print("Listening to system audio…")
        t, a, off, _ = recognize_playing()
        print(f"Heard: {t!r} — {a!r}  (offset {off}s)" if t
              else "Could not identify the audio.")
