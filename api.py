"""
Local HTTP API — so an agent (or you) can see what Desktop Karaoke is doing and
drive it programmatically.

It binds to **127.0.0.1 only** (never the network) and needs no key — it's for
local automation. Start it from the overlay (on by default; toggle in the tray).
Default port 8765.

Endpoints
  GET  /            → this list
  GET  /status      → now-playing, the matched song, sync offset, current line
  GET  /logs?n=200  → the last N lines of karaoke.log (the matching decisions)
  GET  /lyrics      → the full loaded, annotated lyric lines (timestamps + text)
  POST /identify    → re-identify the song by SOUND now (force a sound re-match)
  POST /wrong       → mark the current lyrics wrong → re-identify + re-fetch
  POST /reindex     → rescan the local lyric library

Example:
  curl http://127.0.0.1:8765/status
  curl -X POST http://127.0.0.1:8765/identify
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PLAYING = 4
_ENDPOINTS = {
    "GET /status": "now-playing + matched song + sync + current line",
    "GET /logs?n=200": "recent log lines (matching decisions)",
    "GET /lyrics": "full loaded lyric lines",
    "POST /identify": "re-identify by sound now",
    "POST /wrong": "mark current lyrics wrong → re-identify + re-fetch",
    "POST /reindex": "rescan the local library",
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
        "line_count": len(app.lines),
        "current_line": _current_line(app),
    }


def make_handler(app, log_file):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):           # silence default stderr logging
            pass

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _run(self, fn):
            """Schedule a mutation on the Tk thread and ack."""
            app.root.after(0, fn)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/status":
                self._send(200, _status(app))
            elif path == "/lyrics":
                self._send(200, {"meta": app.meta,
                                 "lines": [{"t": [l.start, l.end], "jp": l.jp,
                                            "rm": l.rm, "en": l.en} for l in app.lines]})
            elif path == "/logs":
                n = 200
                if "n=" in self.path:
                    try:
                        n = int(self.path.split("n=", 1)[1].split("&")[0])
                    except Exception:
                        pass
                try:
                    lines = log_file.read_text("utf-8", errors="replace").splitlines()
                except Exception:
                    lines = []
                self._send(200, {"lines": lines[-n:]})
            elif path in ("/", ""):
                self._send(200, {"app": "Desktop Karaoke", "endpoints": _ENDPOINTS})
            else:
                self._send(404, {"error": "not found", "endpoints": _ENDPOINTS})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/identify":
                self._run(lambda: app._start_identify(seconds=6, attempts=2))
                self._send(200, {"ok": True, "action": "identifying by sound"})
            elif path == "/wrong":
                self._run(app.refetch)
                self._send(200, {"ok": True, "action": "re-identifying + re-fetching"})
            elif path == "/reindex":
                self._run(app.index.refresh)
                self._send(200, {"ok": True, "action": "library rescanned"})
            else:
                self._send(404, {"error": "not found", "endpoints": _ENDPOINTS})

    return Handler


def start_api(app, log_file, port=8765):
    """Start the local API server in a background thread. Returns the server, or
    None if the port is taken. Binds to localhost only."""
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, log_file))
    except Exception:
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
