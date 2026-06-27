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
import threading as _threading

PLAYING = 4

# Active playlist import job (set when POST /import/csv is called).
_import_job = None
_import_lock = _threading.Lock()
_MAX_BODY = 64 * 1024          # cap POST bodies — we don't need a payload anyway
_START = time.time()

# {method: {path: description}} — returned by GET / so an agent can self-describe.
_ROUTES = {
    "GET": {
        "/health": "liveness + version + uptime",
        "/status": "now-playing + matched song + sync + current line",
        "/logs": "recent log lines (?n=200) — the matching decisions",
        "/lyrics": "the full loaded lyric lines",
        "/tune": "live sync-tuning parameters (drift_fastpath, agree, spread_reset, …)",
        "/diag": "deep diagnostics: full sync state machine, last energy-correlation, FPS/frame-timing, pending-swap (TICKET-111)",
        "/source": "video/music source view: raw SMTC data + what the app derived from it",
        "/audio": "audio listener: live loudness + vocal-band ratio + recent on/off pattern",
        "/lyricstate": "lyric current-state analyzer: current/prev/next lines, fill, structural checks",
        "/import/status": "current playlist import state: state, done, total, ok, skipped, failed_count",
    },
    "POST": {
        "/identify": "re-identify by sound now",
        "/wrong": "mark current lyrics wrong → re-identify + re-fetch",
        "/nudge": "shift sync by ?s=2.5 seconds (+ = lyrics later)",
        "/reset": "reset the sync offset to 0",
        "/align": "sync by listening — transcribe the audio + match to lyrics, one-shot (needs faster-whisper)",
        "/forcesync": "FORCE SYNC — reset to 0, then try RANKED match candidates (skip chorus traps), forward-verifying each until one holds; nuclear",
        "/decide": "smart song decision — transcribe vocals + pick which candidate's lyrics they match",
        "/captions": "pull THIS video's caption track (accurate text+timing); ?url=<exact video> beats a title search",
        "/nowplaying": "browser pushes the exact current video URL (?url=...) so auto-captions hit the right upload",
        "/tune": "set sync param: ?key=drift_fastpath&value=3.0 (one per call); or POST JSON {k:v,...}",
        "/reindex": "rescan the local library",
        "/import/csv": "start a playlist CSV import: ?path=C:\\path\\to\\file.csv [&translate=1] [&force=1]",
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


def _decision_engine_snapshot(app):
    """TICKET-109: surface the live decision-engine state. Returns None when
    the engine state hasn't been initialized (graceful legacy degrade)."""
    try:
        state = getattr(app, "_decision_state", None)
        if state is None:
            return None
        last_act = getattr(app, "_decision_last_action_t", 0) or 0
        return {
            "state":             state,
            "strikes":           getattr(app, "_decision_strikes", 0),
            "dim_scores":        dict(getattr(app, "_decision_dim_scores", {})),
            "dim_history":       {k: list(v)[-6:]
                                  for k, v in getattr(app, "_decision_dim_history", {}).items()},
            "audit":             list(getattr(app, "_decision_audit", []))[-5:],
            "last_action_age_s": (round(time.time() - last_act, 1) if last_act else None),
            "thresholds": {
                "caution": int(app._tune.get("decision_caution_strikes", 3)),
                "switch":  int(app._tune.get("decision_switch_strikes",  5)),
                "regen":   int(app._tune.get("decision_regen_strikes",   8)),
            },
        }
    except Exception:
        return None


def _gpu_snapshot():
    """TICKET-103: snapshot of the GPU device pick gpu_setup would make
    RIGHT NOW. Cheap (cuda_device_count + nvml utility queries are both
    memoized in gpu_setup). Returns None if align isn't importable, so a
    legacy /diag watcher just sees ``gpu: null`` instead of breaking."""
    try:
        import align
        dev, idx, reason, n = align.current_device_choice()
        return {"device": dev, "index": idx, "reason": reason, "gpu_count": n}
    except Exception:
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
        # last audio-vs-display drift (+ve ⇒ lyrics late) and its age in seconds —
        # lets a watcher see desync without parsing the log; None if never measured.
        "sync_drift": getattr(app, "_last_drift", None),
        "sync_drift_age": (round(time.time() - app._last_drift_t, 1)
                           if getattr(app, "_last_drift_t", 0) else None),
        "sync_pending": (round(app._pending_corr, 2)
                         if getattr(app, "_pending_corr", 1e9) < 1e8 else None),
        "matched_title": app.meta.get("title"),
        "matched_artist": app.meta.get("artist"),
        "lang": app.meta.get("lang"),
        # TICKET-099: `verified` now means "duration/title meta passes AND
        # Shazam has corroborated the loaded title at least once" — i.e.
        # SOUND has agreed. A duration-match alone is no longer enough; that
        # was the v1.0.88 bug where a paused SMTC tab with a stale title was
        # being reported as verified before any audio confirmation.
        # `verified_meta` exposes the old (duration/title) check for any
        # backward-compatible watcher that wants it.
        "verified": app._verified,
        "verified_meta": getattr(app, "_verified_meta", False),
        "source_priority": getattr(app, "_source_priority", "agree"),
        # TICKET-103: expose the current GPU policy choice. Cheap; lets a
        # diag watcher see Whisper falling to CPU during a fullscreen game
        # or because of the single-GPU safety floor.
        "gpu": _gpu_snapshot(),
        # TICKET-109: decision engine state + per-dim scores + recent audit
        # so a watcher (or the tray hint) can see TRUST -> CAUTION -> SWITCH
        # -> REGEN promotions live, including which dimension drove them.
        "decision_engine": _decision_engine_snapshot(app),
        "heard_by_sound": app._sound_song,
        "boundary_detect": getattr(app, "boundary_on", None),
        # TICKET-102: capability flags for the window-title scraper, mirrored
        # in /status the same way boundary_detect is. Lets a watcher distinguish
        # 'feature disabled' from 'feature on but slot empty'.
        "window_titles_on": getattr(app, "window_titles_on", None),
        "window_titles_generic_browsers_on":
            getattr(app, "window_titles_generic_browsers_on", None),
        "live_mode": getattr(app, "_live_mode", None),
        "perf": getattr(app, "perf", None),
        "fps_target": (round(1000.0 / app._fps) if getattr(app, "_fps", 0) else None),
        "render_fps": (round(1000.0 / app._frame_ms)
                       if getattr(app, "_frame_ms", 0) else None),
        "frame_jitter_ms": round(getattr(app, "_frame_jitter", 0.0), 1),
        "frame_worst_ms": round(getattr(app, "_frame_worst", 0.0), 1),
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
                    self._send(200, {"ok": True, "app": "Lyric Immersion and Karaoke",
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
                elif path == "/tune":
                    try:
                        self._send(200, {"ok": True, "tune": app.get_tune()})
                    except Exception as e:
                        self._err(500, f"{type(e).__name__}: {e}")
                elif path == "/diag":
                    try:
                        self._send(200, {"ok": True, **app.get_diag()})
                    except Exception as e:
                        self._err(500, f"{type(e).__name__}: {e}")
                elif path == "/source":
                    try:
                        self._send(200, {"ok": True, **app.get_source()})
                    except Exception as e:
                        self._err(500, f"{type(e).__name__}: {e}")
                elif path == "/audio":
                    try:
                        self._send(200, {"ok": True, **app.get_audio()})
                    except Exception as e:
                        self._err(500, f"{type(e).__name__}: {e}")
                elif path == "/lyricstate":
                    try:
                        self._send(200, {"ok": True, **app.get_lyric_state()})
                    except Exception as e:
                        self._err(500, f"{type(e).__name__}: {e}")
                elif path == "/import/status":
                    self._send(200, {"ok": True, **_import_status()})
                elif path == "/":
                    self._send(200, {"ok": True, "app": "Lyric Immersion and Karaoke",
                                     "version": API_VERSION, "routes": _ROUTES})
                else:
                    self._err(404, f"no GET {path}")
            except Exception as e:
                self._err(500, f"{type(e).__name__}: {e}")

        def do_POST(self):
            try:
                # Capture body for JSON-bearing endpoints BEFORE draining it.
                try:
                    n = int(self.headers.get("Content-Length", 0) or 0)
                except Exception:
                    n = 0
                body = b""
                if n > 0:
                    body = self.rfile.read(min(n, _MAX_BODY))
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
                elif path == "/align":
                    self._run(app.align_by_listening)
                    self._send(200, {"ok": True, "action": "syncing by listening (transcribe + match, one-shot)"})
                elif path == "/forcesync":
                    # FORCE SYNC: reset to 0, then transcribe+match (two-point) until 3 reads agree
                    self._run(app.force_sync)
                    self._send(200, {"ok": True, "action": "force sync — hammering transcribe+match until it locks"})
                elif path == "/decide":
                    # SMART song decision: transcribe the vocals + pick which candidate
                    # song's lyrics they match (corrects Shazam mis-IDs / mislabeled LRCs)
                    self._run(lambda: app._decide_by_ear(app._track_seq, reason="api"))
                    self._send(200, {"ok": True, "action": "deciding the song by ear (transcribe + lyric-match)"})
                elif path == "/captions":
                    u = q.get("url", [""])[0].strip() or None
                    self._run(lambda: app.load_youtube_captions(url=u))
                    self._send(200, {"ok": True, "action": "pulling the video's YouTube caption track",
                                     "url": u})
                elif path == "/nowplaying":
                    u = q.get("url", [""])[0].strip()
                    self._run(lambda: app.set_now_url(u))
                    self._send(200, {"ok": True, "action": "current video URL set", "url": u})
                elif path == "/tune":
                    # Accept either ?key=X&value=Y OR a JSON body {k1: v1, k2: v2}.
                    # Returns the resulting tune dict + any per-key errors.
                    updates = {}
                    if "key" in q:
                        updates[q.get("key", [""])[0]] = q.get("value", [""])[0]
                    if body:
                        try:
                            parsed = json.loads(body.decode("utf-8") or "{}")
                            if isinstance(parsed, dict):
                                updates.update(parsed)
                        except Exception as e:
                            return self._err(400, f"bad JSON body: {e}")
                    if not updates:
                        return self._err(400, "no updates: send ?key=X&value=Y or JSON body")
                    # Apply on the Tk thread (writing to the tune dict is fine
                    # without a lock — only the main loop reads it during the
                    # next sync tick).
                    results = []
                    for k, v in updates.items():
                        ok, msg = app.set_tune(k, v)
                        results.append({"key": k, "ok": ok, "msg": msg})
                    self._send(200, {"ok": all(r["ok"] for r in results),
                                     "results": results, "tune": app.get_tune()})
                elif path == "/reindex":
                    self._run(app.index.refresh)
                    self._send(200, {"ok": True, "action": "library rescanned"})
                elif path == "/import/csv":
                    csv_path = q.get("path", [""])[0].strip()
                    if not csv_path:
                        return self._err(400, "path query param required, e.g. ?path=C:\\my.csv")
                    translate = q.get("translate", ["0"])[0] in ("1", "true", "yes")
                    force     = q.get("force",     ["0"])[0] in ("1", "true", "yes")
                    result = _start_csv_import(csv_path, translate=translate, force=force)
                    self._send(200 if result["ok"] else 409, result)
                elif path == "/font":
                    # ?scale=1.0  — set the lyric font scale live (no settings edit/restart)
                    try:
                        v = float(q.get("scale", q.get("size", ["1.0"]))[0])
                    except Exception:
                        return self._err(400, "scale must be a number, e.g. ?scale=1.0")
                    self._run(lambda: app.set_font_scale(v))
                    self._send(200, {"ok": True, "action": f"font scale → {v}"})
                elif path == "/scroll":
                    # ?dir=rl  — none|left|right|lr|rl|tb|bt
                    d = q.get("dir", q.get("mode", [""]))[0].strip().lower()
                    if d not in ("none", "off", "stationary", "left", "right", "lr", "rl", "tb", "bt"):
                        return self._err(400, "dir must be none|left|right|lr|rl|tb|bt")
                    self._run(lambda: app.set_scroll(d))
                    self._send(200, {"ok": True, "action": f"scroll → {d}"})
                elif path == "/position":
                    # ?y=bottom&x=right  — set either/both axes (top|center|bottom / left|center|right)
                    y, x = q.get("y", [""])[0].strip().lower(), q.get("x", [""])[0].strip().lower()
                    if not y and not x:
                        return self._err(400, "send ?y=top|center|bottom and/or ?x=left|center|right")
                    if y:
                        self._run(lambda: app.set_pos("y", y))
                    if x:
                        self._run(lambda: app.set_pos("x", x))
                    self._send(200, {"ok": True, "action": f"position y={y or '-'} x={x or '-'}"})
                elif path == "/purgecache":
                    # clear bad cached lyrics at runtime: ?current=1, ?lang=ko, ?source=youtube-captions
                    cur = q.get("current", ["0"])[0] in ("1", "true", "yes")
                    lang = q.get("lang", [""])[0].strip() or None
                    source = q.get("source", [""])[0].strip() or None
                    if not (cur or lang or source):
                        return self._err(400, "send ?current=1 and/or ?lang=ko and/or ?source=...")
                    removed = app.purge_cache(lang=lang, source=source, current=cur)
                    self._send(200, {"ok": True, "removed": removed, "count": len(removed)})
                else:
                    self._err(404, f"no POST {path}")
            except Exception as e:
                self._err(500, f"{type(e).__name__}: {e}")

    return Handler


def _import_status() -> dict:
    """Return a serialisable summary of the current (or last) import job."""
    global _import_job
    job = _import_job
    if job is None:
        return {"state": "idle"}
    if job.cancelled:
        state = "cancelled"
    elif job.done >= job.total > 0:
        state = "done"
    else:
        state = "running"
    return {
        "state": state,
        "total": job.total,
        "done": job.done,
        "ok": job.ok,
        "skipped": job.skipped,
        "failed_count": len(job.failed),
        "pct": round(job.pct, 1),
        "last_track": list(job.last_track),
    }


def _start_csv_import(path: str, *, translate: bool = False, force: bool = False) -> dict:
    """Start a background CSV import. Returns {ok, action} or {ok, error}."""
    global _import_job
    with _import_lock:
        job = _import_job
        if job and not job.cancelled and job.done < job.total:
            return {"ok": False, "error": "import already running — cancel it first via /import/status"}
        from playlist_import import ImportJob, import_from_csv
        new_job = ImportJob()
        _import_job = new_job

    def _run():
        try:
            import_from_csv(path, new_job, translate=translate, force=force)
        except Exception:
            new_job.cancel()

    _threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "action": f"importing {path}"}


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
