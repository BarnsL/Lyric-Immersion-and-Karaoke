"""TICKET-118: per-process Core Audio peak meters → audible-session preference.

The user's scenario: TWO Brave tabs publishing to SMTC, one muted (Tab A,
hyper-realistic motorbike POV video) and one audibly playing the lyric track
(Tab B, "I Really Want to Stay at Your House"). SMTC sees both, the most
recently active wins, and the overlay ping-pongs between them.

This module asks Core Audio "which processes are ACTUALLY making sound right
now?" and returns a `{process_name.lower(): peak_amplitude}` dict, which
MediaWatcher._pick uses as a tiebreaker between equally-eligible SMTC
sessions: pinned wins absolutely (TICKET-117), then audible-pref, then sticky.

Design constraints baked in here:

* **Lazy import** — pycaw/comtypes load on first use, so non-Windows dev boxes
  (and Windows boxes without the dep) just see an empty dict and the caller
  falls back to the existing sticky behavior. A single sentinel disables the
  feature after the first ImportError so we don't pay the cost on every poll.
* **Hard 500 ms timeout** — Core Audio enumeration normally returns in <5 ms,
  but a hung audio endpoint (HDMI display going to sleep is a known cause)
  can block for SECONDS. We do the enumeration on a short-lived worker thread
  and `.join(0.5)`; timeout = "unavailable for this cycle".
* **1 s peak cache** — pycaw.GetAllSessions has measurable COM overhead;
  GetMeterInformation.GetPeakValue is cheap but we still rate-limit to once
  per second to keep MediaWatcher's 0.15s poll loop snappy.
* **Per-process aggregation** — a browser publishes ONE audio session per
  playing tab; we collapse them per-PID (max peak across the PID's sessions)
  and then group by executable basename so the caller can substring-match
  AUMIDs like "app.brave.brave" against "brave" without caring about the
  multi-renderer-process detail.
* **Everything is best-effort** — every public call is wrapped to never raise.
  The fallback path is the EXACT pre-118 behavior.
"""

from __future__ import annotations

import threading
import time

# Sentinel that flips False on first ImportError / OSError so we never retry
# pycaw on a box where it'll never work (Linux dev box, broken Windows audio
# service, missing dep). Reset is a process restart — by design.
_AVAILABLE = True
_LAST_ERR = None

# Cache the per-process peak dict for 1 s so repeated calls within a single
# MediaWatcher tick (or the 0.15s poll cadence) hit the cache instead of
# re-enumerating COM sessions.
_CACHE_TTL_S = 1.0
_cache_lock = threading.Lock()
_cache_levels: dict[str, float] = {}
_cache_t = 0.0

# Hard wall-clock cap for the COM enumeration. Anything slower → treat as
# unavailable for this cycle (return cached or {}). 500 ms is well above the
# normal <5 ms enumeration time, with margin for a single slow endpoint.
_ENUM_TIMEOUT_S = 0.5


def _enumerate_once() -> dict[str, float]:
    """Single Core-Audio enumeration → {executable_basename_lower: max_peak}.

    Aggregation order:
      1. For each audio session, take its PID and its IAudioMeterInformation
         peak. Skip sessions without a PID (system sounds).
      2. Group sessions by PID; take the MAX peak across the PID's sessions
         (a Chromium PID may host several audio streams; the loudest one is
         what 'this process is currently audible' means).
      3. Group PIDs by executable basename (lowercased, no path, no '.exe');
         take the MAX peak across the basename's PIDs. This is what AUMIDs
         like 'app.brave.brave' get matched against — substring search by the
         caller.

    Never raises; on any exception sets the module sentinel to disabled and
    returns {}.
    """
    global _AVAILABLE, _LAST_ERR
    try:
        # Lazy imports — pycaw + comtypes are Windows-only. The first attempt
        # on a non-Windows machine raises ImportError and we never try again.
        from pycaw.pycaw import AudioUtilities  # type: ignore
    except Exception as e:
        _AVAILABLE = False
        _LAST_ERR = f"import: {e!r}"
        return {}

    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception as e:
        # Stay AVAILABLE — a single failed enumeration may be transient
        # (audio service restart, sleeping HDMI endpoint). Caller falls back
        # to {} this cycle and will retry next cycle.
        _LAST_ERR = f"GetAllSessions: {e!r}"
        return {}

    # PID → max peak across all of that PID's sessions.
    pid_peak: dict[int, float] = {}
    # PID → executable basename (lowercased, no '.exe').
    pid_name: dict[int, str] = {}

    for sess in sessions:
        try:
            proc = getattr(sess, "Process", None)
            if proc is None:
                # System sounds session — no PID, can't map to SMTC.
                continue
            try:
                pid = int(proc.pid)
            except Exception:
                continue
            try:
                raw_name = proc.name() or ""
            except Exception:
                raw_name = ""
            name = raw_name.lower()
            if name.endswith(".exe"):
                name = name[:-4]
            if not name:
                continue
            try:
                meter = sess.SimpleAudioVolume  # ensure session is alive
            except Exception:
                meter = None
            try:
                # pycaw exposes the meter via the underlying COM object as
                # _ctl.QueryInterface(IAudioMeterInformation); the wrapper is
                # not on every version, so fall through to the helper.
                from pycaw.pycaw import IAudioMeterInformation  # type: ignore
                ami = sess._ctl.QueryInterface(IAudioMeterInformation)
                peak = float(ami.GetPeakValue())
            except Exception:
                peak = 0.0
            _ = meter  # keep ref alive briefly
            prev = pid_peak.get(pid, 0.0)
            if peak > prev:
                pid_peak[pid] = peak
            pid_name.setdefault(pid, name)
        except Exception:
            continue

    # Basename → max peak across all PIDs sharing that basename. The user's
    # scenario hits this case directly: Brave has many renderer PIDs (one per
    # site-isolated origin) and we want the loudest one represented under
    # 'brave'.
    by_name: dict[str, float] = {}
    for pid, peak in pid_peak.items():
        name = pid_name.get(pid)
        if not name:
            continue
        prev = by_name.get(name, 0.0)
        if peak > prev:
            by_name[name] = peak
    return by_name


def _enumerate_with_timeout(timeout_s: float) -> dict[str, float]:
    """Run _enumerate_once on a worker thread; join with hard timeout.

    Why a thread instead of asyncio: pycaw is sync-only COM and the caller
    (MediaWatcher._loop) is already in an asyncio task. A blocking COM call
    inside an async task would stall the SMTC poll loop; a worker thread
    isolates the failure mode to 'one wasted thread' if the COM call hangs.
    """
    result: dict[str, float] = {}
    err: list[BaseException] = []

    def _work():
        try:
            r = _enumerate_once()
            result.update(r)
        except BaseException as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=_work, daemon=True, name="audible-pyaw-enum")
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        # Worker is stuck (hung endpoint / driver). Abandon it as a daemon —
        # process exit will clean it up. Treat as unavailable this cycle.
        return {}
    if err:
        return {}
    return result


def get_process_audio_levels() -> dict[str, float]:
    """Public API → `{executable_basename_lower: max_peak_amplitude}`.

    Used by MediaWatcher._pick (TICKET-118) to break ties between equally-
    eligible SMTC sessions: the session whose source_app substring-matches
    the loudest process wins. Returns `{}` on:
      - non-Windows machines (pycaw not importable)
      - missing pycaw/comtypes
      - COM enumeration failure
      - 500 ms hard timeout (hung audio endpoint)

    Cached for ~1 s so a tight poll loop doesn't thrash COM. Thread-safe.
    """
    global _cache_levels, _cache_t
    if not _AVAILABLE:
        return {}
    now = time.monotonic()
    with _cache_lock:
        if (now - _cache_t) < _CACHE_TTL_S and _cache_levels is not None:
            return dict(_cache_levels)
    try:
        fresh = _enumerate_with_timeout(_ENUM_TIMEOUT_S)
    except Exception:
        fresh = {}
    with _cache_lock:
        _cache_levels = dict(fresh)
        _cache_t = now
        return dict(_cache_levels)


def diag() -> dict:
    """Lightweight introspection for /diag.audible_pref. Never raises."""
    try:
        return {
            "available": bool(_AVAILABLE),
            "last_error": _LAST_ERR,
            "cache_age_s": round(time.monotonic() - _cache_t, 2)
                if _cache_t else None,
            "cache_ttl_s": _CACHE_TTL_S,
            "timeout_s": _ENUM_TIMEOUT_S,
            "levels": dict(_cache_levels) if _cache_levels else {},
        }
    except Exception:
        return {"available": False, "last_error": "diag-exception"}
