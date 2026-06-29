# -*- coding: utf-8 -*-
"""Per-RELEASE success / wobbler / fail telemetry for the lyric overlay.

Each SONG PLAY is one record. Outcomes (user spec, 2026-06-28):
  • SUCCESS — real lyrics found AND synced within 60s; final source is a real one
    (provider / bundle / caption / OCR, NOT generated); stayed stable (≤2 resyncs);
    never ended generated.
  • FAIL    — ended up generated, OR >10 resync events on one song (sat on a wrong
    song). For a CONCERT/compilation it's rate-based instead: ≥10 wrong-detections
    within any 5-minute window.
  • WOBBLER — anything in between, with a written note (time-to-sync, resync count,
    final source, and which rule made it a wobbler).

Counts are bucketed by ``version.__version__`` and persisted to
``data_dir()/metrics.json`` so they accumulate across restarts and are queryable via
the local API (GET /metrics). All classification lives here; main.py just calls the
lifecycle hooks. A metrics error must never break playback — callers wrap in try/except
and save() swallows disk errors.
"""
from __future__ import annotations

import json
import threading
import time

from appdata import data_dir

_RECORDS_CAP = 200

# Every kind of "needed re-syncing" event. The non-concert >10 fail rule and the ≤2
# success rule count ALL of these (a SWITCH is the app re-syncing → it counts).
_RESYNC_ALL = ("switch", "regen", "energy-align", "nudge", "report_wrong")
# At a concert, song-changes legitimately cause switches/regens; the FAILURE rate rule
# counts genuine WRONG-DETECTIONS only (manual nudge / benign auto-align excluded).
_WRONG_KINDS = ("switch", "regen", "report_wrong")

_REAL_SOURCES_PREFIX = ("bundled", "lrclib", "syncedlyrics", "netease")
_REAL_SOURCES_EXACT = ("youtube-captions", "ocr")

_SYNC_DEADLINE_S = 60.0
_SUCCESS_MAX_RESYNCS = 2          # ≤ this (non-concert) → success-eligible
_FAIL_RESYNCS = 10               # > this (non-concert) → fail
_CONCERT_WINDOW_S = 300.0        # 5-minute sliding window
_CONCERT_FAIL_RATE = 10          # ≥ this many wrong-detections in a window → fail
_CONCERT_SUCCESS_MAX = 3         # ≤ this many total → success; 4-9 → wobbler


class _Play:
    __slots__ = ("version", "title", "artist", "concert", "t0",
                 "synced_at", "resyncs", "final_source", "ended_generated", "seq")

    def __init__(self, version, title, artist, concert, t0, seq):
        self.version = version
        self.title = title or ""
        self.artist = artist or ""
        self.concert = bool(concert)
        self.t0 = float(t0)
        self.seq = seq
        self.synced_at = None        # wall-clock of FIRST verified sync (or None)
        self.resyncs = []            # list[(kind, ts)]
        self.final_source = ""
        self.ended_generated = False

    @property
    def time_to_sync_s(self):
        return None if self.synced_at is None else round(self.synced_at - self.t0, 2)

    @property
    def resync_count(self):
        return sum(1 for k, _ in self.resyncs if k in _RESYNC_ALL)


class ReleaseMetrics:
    """Owns the in-flight play, the per-version buckets, and disk persistence."""

    def __init__(self, version):
        self._version = str(version)
        self._lock = threading.RLock()   # hooks on the Tk thread, API reads on HTTP thread
        self._path = data_dir() / "metrics.json"
        self._cur = None                 # _Play | None
        self._buckets = {}               # version -> {plays,success,wobbler,fail,records[]}
        self.load()

    # ── persistence ──────────────────────────────────────────────────────────
    def load(self):
        with self._lock:
            try:
                b = json.loads(self._path.read_text("utf-8"))
                self._buckets = b if isinstance(b, dict) else {}
            except Exception:
                self._buckets = {}

    def save(self):
        # caller holds _lock; atomic temp+replace; never raise
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._buckets, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(self._path)
        except Exception:
            pass

    def _bucket(self, version):
        b = self._buckets.get(version)
        if b is None:
            b = {"plays": 0, "success": 0, "wobbler": 0, "fail": 0, "records": []}
            self._buckets[version] = b
        return b

    # ── lifecycle hooks (called from main.py) ────────────────────────────────
    def start_play(self, version, title, artist, concert, t0, seq=None):
        with self._lock:
            if self._cur is not None:          # orphaned prev play → finalize first
                self._finalize_locked()
            self._cur = _Play(version, title, artist, concert, t0, seq)

    def note_synced(self, t=None):
        with self._lock:
            p = self._cur
            if p is not None and p.synced_at is None:   # first sync only
                p.synced_at = float(t) if t else time.time()

    def note_source(self, source):
        with self._lock:
            p = self._cur
            if p is not None and source:
                p.final_source = source
                if source.startswith("generated"):
                    p.ended_generated = True

    def note_resync(self, kind):
        with self._lock:
            p = self._cur
            if p is not None:
                p.resyncs.append((kind, time.time()))

    def note_generated(self):
        with self._lock:
            p = self._cur
            if p is not None:
                p.ended_generated = True
                if not p.final_source:
                    p.final_source = "generated"

    def promote_concert(self):
        """One-way: a concert detected late (duration arrived after track start) →
        reclassify this play under the rate rule. Never demotes."""
        with self._lock:
            if self._cur is not None:
                self._cur.concert = True

    def finalize(self):
        with self._lock:
            return self._finalize_locked()

    # ── classification ───────────────────────────────────────────────────────
    @staticmethod
    def _max_in_window(sorted_ts, window):
        best, j = 0, 0
        for i in range(len(sorted_ts)):
            while sorted_ts[i] - sorted_ts[j] > window:
                j += 1
            best = max(best, i - j + 1)
        return best

    def _classify(self, p):
        src = (p.final_source or "")
        # ── CONCERT: rate-based on wrong-detections in any 5-min window ──
        if p.concert:
            wrong = sorted(ts for k, ts in p.resyncs if k in _WRONG_KINDS)
            peak = self._max_in_window(wrong, _CONCERT_WINDOW_S)
            if peak >= _CONCERT_FAIL_RATE:
                return "fail", (f"concert: {peak} wrong-detections in a 5min window "
                                f"(>= {_CONCERT_FAIL_RATE})")
            total = len(wrong)
            if total <= _CONCERT_SUCCESS_MAX:
                return "success", None
            return "wobbler", (f"concert: {total} wrong-detections total "
                               f"(peak {peak}/5min); 4-9 => wobbler")
        # ── NORMAL: generated or >10 resyncs = fail ──
        n = p.resync_count
        if src.startswith("generated") or p.ended_generated:
            return "fail", None
        if n > _FAIL_RESYNCS:
            return "fail", None
        synced = p.synced_at is not None
        in_time = synced and (p.synced_at - p.t0) <= _SYNC_DEADLINE_S
        real_src = src.startswith(_REAL_SOURCES_PREFIX) or src in _REAL_SOURCES_EXACT
        if synced and in_time and real_src and n <= _SUCCESS_MAX_RESYNCS:
            return "success", None
        # ── WOBBLER: explain why ──
        why = []
        if not synced:
            why.append("never synced")
        elif not in_time:
            why.append(f"synced late ({round(p.synced_at - p.t0, 1)}s > {int(_SYNC_DEADLINE_S)}s)")
        if synced and not real_src:
            why.append(f"source not real ({src or 'none'})")
        if n > _SUCCESS_MAX_RESYNCS:
            why.append(f"resync_count {n} > {_SUCCESS_MAX_RESYNCS}")
        note = (f"time_to_sync={p.time_to_sync_s}s resync_count={n} "
                f"final_source={src or 'none'} :: " + "; ".join(why or ["unclassified"]))
        return "wobbler", note

    def _finalize_locked(self):
        p = self._cur
        self._cur = None
        if p is None:
            return None
        outcome, note = self._classify(p)
        b = self._bucket(p.version)
        b["plays"] += 1
        b[outcome] = b.get(outcome, 0) + 1
        kinds = {}
        for k, _ in p.resyncs:
            kinds[k] = kinds.get(k, 0) + 1
        b["records"].append({
            "ts": round(time.time(), 1),
            "title": p.title, "artist": p.artist,
            "outcome": outcome,
            "time_to_sync_s": p.time_to_sync_s,
            "resync_count": p.resync_count,
            "resyncs_by_kind": kinds,
            "final_source": p.final_source or None,
            "concert": p.concert,
            "note": note,
        })
        if len(b["records"]) > _RECORDS_CAP:
            b["records"] = b["records"][-_RECORDS_CAP:]
        self.save()
        return outcome

    # ── API snapshot ─────────────────────────────────────────────────────────
    def as_dict(self):
        with self._lock:
            out = {"current_version": self._version, "versions": {}}
            for ver, b in self._buckets.items():
                plays = b.get("plays", 0) or 0
                succ = b.get("success", 0)
                out["versions"][ver] = {
                    "plays": plays,
                    "success": succ,
                    "wobbler": b.get("wobbler", 0),
                    "fail": b.get("fail", 0),
                    "success_rate": (round(100.0 * succ / plays, 1) if plays else None),
                    "records": list(b.get("records", [])[-50:]),
                }
            cur = self._cur
            out["in_flight"] = None if cur is None else {
                "title": cur.title, "artist": cur.artist, "concert": cur.concert,
                "synced": cur.synced_at is not None,
                "time_to_sync_s": cur.time_to_sync_s,
                "resync_count": cur.resync_count,
                "final_source": cur.final_source or None,
                "elapsed_s": round(time.time() - cur.t0, 1),
            }
            return out

    snapshot = as_dict


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    m = ReleaseMetrics("test-0")
    # success
    m.start_play("test-0", "Good Song", "Artist", False, time.time() - 30, 1)
    m.note_synced(time.time() - 30 + 20); m.note_source("syncedlyrics")
    print("success ->", m.finalize())
    # fail (generated)
    m.start_play("test-0", "Bad Song", "Artist", False, time.time() - 90, 2)
    m.note_generated()
    print("fail(gen) ->", m.finalize())
    # fail (>10 resyncs)
    m.start_play("test-0", "Stuck", "Artist", False, time.time() - 200, 3)
    m.note_synced(); m.note_source("syncedlyrics")
    for _ in range(12):
        m.note_resync("switch")
    print("fail(resync) ->", m.finalize())
    # wobbler (slow sync)
    m.start_play("test-0", "Slow", "Artist", False, time.time() - 120, 4)
    m.note_synced(time.time()); m.note_source("syncedlyrics")
    print("wobbler ->", m.finalize())
    print(json.dumps(m.as_dict()["versions"]["test-0"], ensure_ascii=False, indent=2)[:400])
