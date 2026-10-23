"""Whisper in a CHILD PROCESS — the crash firewall (TICKET-184).

WHY THIS EXISTS
---------------
faster-whisper runs on CTranslate2, which is native C++. When a CUDA/cuDNN
operation fails (most often: not enough free VRAM, because a game or a browser
took it while we were mid-song) CTranslate2 throws a C++ exception **on one of
its own worker threads**. Nothing on that thread catches it, so the CRT calls
``std::terminate`` → ``abort()``. Observed twice in one evening as:

    Faulting module: KERNELBASE.dll   code 0xe06d7363   (C++ throw)
    Faulting module: ucrtbase.dll     code 0xc0000409   (abort / fastfail)

Python **cannot** catch that. No ``try/except`` anywhere in the app helps: the
exception never crosses back into the interpreter. The process simply dies, and
the user loses their lyrics mid-concert.

The only real defence is address-space isolation: run the model somewhere that
is allowed to die. This module is that somewhere. If it aborts, the parent sees
a dead socket / non-zero exit code, logs it, drops to CPU, and carries on.

SECOND BENEFIT (TICKET-135 rediscovered)
----------------------------------------
Whisper is GIL-heavy. Running it in-process stalls the Tk render thread, which
is *exactly* the "highlight sticks then jumps" symptom that moving identify-by-
sound into a child process fixed. Whisper was the remaining in-process offender.

TRANSPORT
---------
A localhost socket, NOT stdout: a windowed PyInstaller app has no reliable
stdout (``sys.stdout.flush()`` raises [Errno 22] and PyInstaller pops a crash
dialog — learned the hard way by the recognize child). The parent listens on
127.0.0.1:0, passes the port and a random token on argv, and this child connects
back and authenticates. Loopback + token, so no other local process can feed us
work or read results.

WIRE FORMAT
-----------
Both directions: 4-byte big-endian length, then that many bytes of UTF-8 JSON.

Request   {"op": "transcribe", "id": N, ...}
Response  {"id": N, "ok": true, "segments": [[start, end, text], ...],
           "language": "ja"}
          {"id": N, "ok": false, "error": "..."}

The child stays alive between requests so the model stays warm — reloading a
CUDA model per request would cost seconds and churn VRAM, which is what we are
trying to avoid in the first place.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import traceback

_HDR = struct.Struct(">I")
_MAX_MSG = 64 * 1024 * 1024          # sanity cap on a single frame


def _send(sock, obj) -> None:
    b = json.dumps(obj).encode("utf-8")
    sock.sendall(_HDR.pack(len(b)) + b)


def _recv_exactly(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None                  # parent closed → time to exit
        buf += chunk
    return buf


def _recv(sock):
    head = _recv_exactly(sock, _HDR.size)
    if head is None:
        return None
    (n,) = _HDR.unpack(head)
    if n <= 0 or n > _MAX_MSG:
        return None
    body = _recv_exactly(sock, n)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def _load_audio(req):
    """Audio arrives either as a path to decode, or as a raw float32 dump we
    wrote from the parent's numpy array (cheaper than re-encoding a wav)."""
    src = req.get("source") or {}
    if src.get("file"):
        return str(src["file"])          # faster-whisper decodes paths itself
    raw = src.get("raw")
    if raw:
        import numpy as np
        return np.fromfile(raw, dtype="float32")
    raise ValueError("request has no audio source")


def _handle_transcribe(req):
    import align
    align._WORKER_MODE = True            # we ARE the worker: never re-delegate

    audio = _load_audio(req)
    size = req.get("size") or align._MODEL
    kw = dict(
        language=req.get("lang"),
        beam_size=int(req.get("beam_size", 1)),
        vad_filter=bool(req.get("vad_filter", False)),
        condition_on_previous_text=bool(req.get("condition_on_previous_text", False)),
    )
    if req.get("no_speech_threshold") is not None:
        kw["no_speech_threshold"] = float(req["no_speech_threshold"])

    # _model_for applies the VRAM guard + one-CUDA-model-at-a-time eviction, and
    # keeps the model alive for the whole (lazy) drain.
    with align._model_for(size) as model:
        segments, info = model.transcribe(audio, **kw)
        out = [[float(s.start), float(s.end), (s.text or "")] for s in segments]
    return {"segments": out, "language": getattr(info, "language", None)}


def _handle(req):
    op = req.get("op")
    if op == "transcribe":
        return _handle_transcribe(req)
    if op == "ping":
        return {"pong": True, "pid": os.getpid()}
    if op == "device":
        import align
        align._WORKER_MODE = True
        dev, idx, ctype, reason = align._select_device(req.get("size"))
        return {"device": dev, "index": idx, "compute_type": ctype, "reason": reason}
    raise ValueError(f"unknown op {op!r}")


def serve(port: int, token: str) -> int:
    sock = socket.create_connection(("127.0.0.1", int(port)), timeout=30)
    sock.settimeout(None)                # requests can legitimately take minutes
    try:
        _send(sock, {"hello": token, "pid": os.getpid()})
        while True:
            req = _recv(sock)
            if req is None or req.get("op") == "shutdown":
                return 0
            rid = req.get("id")
            try:
                res = _handle(req)
                res.update(id=rid, ok=True)
            except Exception as e:
                res = {"id": rid, "ok": False,
                       "error": f"{type(e).__name__}: {e}",
                       "trace": traceback.format_exc()[-2000:]}
            _send(sock, res)
    finally:
        try:
            sock.close()
        except Exception:
            pass


def main(argv=None) -> int:
    a = list(argv if argv is not None else sys.argv[1:])

    def flag(name, default=None):
        try:
            i = a.index(name)
            return a[i + 1]
        except (ValueError, IndexError):
            return default

    port = flag("--port")
    token = flag("--token") or ""
    if not port:
        return 2
    try:
        return serve(int(port), token)
    except Exception:
        # A clean non-zero exit; the parent treats any death as "GPU is unsafe".
        return 1


if __name__ == "__main__":
    sys.exit(main())
