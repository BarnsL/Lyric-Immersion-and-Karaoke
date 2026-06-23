"""
Local HTTP API — so an agent (or you) can see what Desktop Karaoke is doing and
drive it programmatically.

Designed to be reliable for agents: every request is wrapped so a bad call can
never crash the app; responses are always JSON with a consistent shape
(`{"ok": true|false, ...}`); errors return a clean message and the right status
code (never a stack trace). `GET /` returns the full machine-readable schema.

SECURITY
  • Binds to **127.0.0.1 only** — never reachable from the network.
  • If the `KARAOKE_API_TOKEN` environment variable is set, every request must
    present it (header `X-API-Token: <token>` or `?token=<token>`); otherwise the
    API trusts localhost. Mutating calls are marshalled onto the UI thread.
  • POST bodies are size-capped; nothing here reads or writes outside the app.

Endpoints (also at GET /):
  GET  /health      → liveness + version + uptime
  GET  /status      → now-playing, the matched song, sync offset, current line
  GET  /logs?n=200  → the last N log lines (every match/sound/swap decision)
  GET  /lyrics      → the full loaded, annotated lyric lines
  POST /identify    → re-identify the song by SOUND now
  POST /wrong       → mark the current lyrics wrong → re-identify + re-fetch
  POST /nudge?s=2.5 → shift sync by s seconds (+ = lyrics later)
  POST /reset       → reset the sync offset to 0
  POST /reindex     → rescan the local lyric library

Example:
  curl http://127.0.0.1:8765/status
  curl -X POST "http://127.0.0.1:8765/nudge?s=2.5"
"""
from __future__ import annotations

import json
import os
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from version import __version__ as API_VERSION
PLAYING = 4
_MAX_BODY = 64 * 1024          # cap POST bodies — we don't need a payload anyway
_START = time.time()

# {method: {path: description}} — returned by GET / so an agent can self-describe.
_ROUTES = {
    "GET": {
        "/health": "liveness + version + uptime",
        "/status": "now-playing + matched song + sync + current line",
        "/logs": "recent log lines (?n=200) — the matching decisions",
        "/lyrics": "the full loaded lyric lines",
    },
    "POST": {
        "/identify": "re-identify by sound now",
        "/wrong": "mark current lyrics wrong → re-identify + re-fetch",
        "/nudge": "shift sync by ?s=2.5 seconds (+ = lyrics later)",
        "/reset": "reset the sync offset to 0",
        "/reindex": "rescan the local library",
    },
}


def _current_line(app):
    """The lyric line at the current playback position (or None)."""
    try:
        st = app.media.get() or {}
        pos = (st.get("position") or 0) + app.offset
        for ln in app.lines:
            if ln.start <= pos < ln.end:
                return {"t": [ln.start, ln.end], "jp": ln.jp, "rm": ln.rm, "en": ln.en}
    except Exception:
        pass
    return None


def _status(app):
    st = app.media.get() or {}
    return {
        "playing": st.get("status") == PLAYING,
        "player_title": (app._track or (None, None))[1],
        "player_artist": (app._track or (None, None))[0],
        "position": round(st.get("position", 0.0), 2),
        "duration": st.get("duration"),
        "sync_offset": round(app.offset, 2),
        "matched_title": app.meta.get("title"),
        "matched_artist": app.meta.get("artist"),
        "lang": app.meta.get("lang"),
        "verified": app._verified,
        "heard_by_sound": app._sound_song,
        "boundary_detect": getattr(app, "boundary_on", None),
        "live_mode": getattr(app, "_live_mode", None),
        "perf": getattr(app, "perf", None),
        "fps_target": (round(1000.0 / app._fps) if getattr(app, "_fps", 0) else None),
        "render_fps": (round(1000.0 / app._frame_ms)
                       if getattr(app, "_frame_ms", 0) else None),
        "line_count": len(app.lines),
        "current_line": _current_line(app),
    }


def make_handler(app, log_file, token):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):            # silence default stderr logging
            pass

        # ── response helpers ──
        def _send(self, code, obj):
            try:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            except Exception:
                body = b'{"ok": false, "error": "serialization failed"}'
                code = 500
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "X-API-Token, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _err(self, code, msg):
            self._send(code, {"ok": False, "error": msg})

        def _authed(self):
            if not token:
                return True
            given = self.headers.get("X-API-Token") or \
                parse_qs(urlparse(self.path).query).get("token", [""])[0]
            return given == token

        def _drain_body(self):
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
            except Exception:
                n = 0
            if n > 0:
                self.rfile.read(min(n, _MAX_BODY))

        def _run(self, fn):
            """Marshal a mutation onto the Tk thread (so it's thread-safe)."""
            app.root.after(0, fn)

        # ── verbs ──
        def do_OPTIONS(self):                 # CORS preflight
            self._send(204, {})

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                if not self._authed():
                    return self._err(401, "missing or bad X-API-Token")
                path = urlparse(self.path).path.rstrip("/") or "/"
                q = parse_qs(urlparse(self.path).query)
                if path == "/health":
                    self._send(200, {"ok": True, "app": "Desktop Karaoke",
                                     "version": API_VERSION,
                                     "uptime_s": round(time.time() - _START, 1)})
                elif path == "/status":
                    self._send(200, {"ok": True, **_status(app)})
                elif path == "/lyrics":
                    self._send(200, {"ok": True, "meta": app.meta,
                                     "lines": [{"t": [l.start, l.end], "jp": l.jp,
                                                "rm": l.rm, "en": l.en}
                                               for l in app.lines]})
                elif path == "/logs":
                    try:
                        n = max(1, min(2000, int(q.get("n", ["200"])[0])))
                    except Exception:
                        n = 200
                    try:
                        lines = log_file.read_text("utf-8", "replace").splitlines()
                    except Exception:
                        lines = []
                    self._send(200, {"ok": True, "lines": lines[-n:]})
                elif path == "/":
                    self._send(200, {"ok": True, "app": "Desktop Karaoke",
                                     "version": API_VERSION, "routes": _ROUTES})
                else:
                    self._err(404, f"no GET {path}")
            except Exception as e:
                self._err(500, f"{type(e).__name__}: {e}")

        def do_POST(self):
            try:
                self._drain_body()
                if not self._authed():
                    return self._err(401, "missing or bad X-API-Token")
                path = urlparse(self.path).path.rstrip("/") or "/"
                q = parse_qs(urlparse(self.path).query)
                if path == "/identify":
                    self._run(lambda: app._start_identify(seconds=6, attempts=2))
                    self._send(200, {"ok": True, "action": "identifying by sound"})
                elif path == "/wrong":
                    self._run(app.refetch)
                    self._send(200, {"ok": True, "action": "re-identifying + re-fetching"})
                elif path == "/nudge":
                    try:
                        s = float(q.get("s", ["0"])[0])
                    except Exception:
                        return self._err(400, "s must be a number, e.g. ?s=2.5")
                    s = max(-180.0, min(180.0, s))
                    self._run(lambda: app.nudge(s))
                    self._send(200, {"ok": True, "action": f"sync nudged {s:+}s"})
                elif path == "/reset":
                    self._run(app.reset_offset)
                    self._send(200, {"ok": True, "action": "sync offset reset to 0"})
                elif path == "/reindex":
                    self._run(app.index.refresh)
                    self._send(200, {"ok": True, "action": "library rescanned"})
                else:
                    self._err(404, f"no POST {path}")
            except Exception as e:
                self._err(500, f"{type(e).__name__}: {e}")

    return Handler


def start_api(app, log_file, port=8765):
    """Start the local API server in a background thread (binds 127.0.0.1 only).
    Returns the server, or None if the port is taken. If KARAOKE_API_TOKEN is set
    in the environment, every request must present it."""
    token = os.environ.get("KARAOKE_API_TOKEN", "").strip()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, log_file, token))
    except Exception:
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
