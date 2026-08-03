"""AutoResearch runner — score knob configurations against a KNOWN playlist.

WHY THIS DESIGN
---------------
An earlier survey concluded a tuning loop was not viable here, for good reasons:
no knob->outcome attribution, every internal metric perverse (optimised by doing
less), and perturbation degrading live playback. Three of those objections were
answers to the wrong question. This runner is for **offline, agent-driven runs on a
predetermined playlist while nobody is watching**, which removes them:

  * Perturbation is free.        Nobody is watching; a bad arm costs a run, not a song.
  * Ground truth exists.         The playlist states what SHOULD play, so "did it
                                 identify the right song" is measured against reality
                                 instead of against the app's own opinion.
  * Attribution is by construction. One arm at a time, tune applied before the arm
                                 and results recorded per arm. No config fingerprint
                                 plumbing needed inside the app.

The remaining objection is real and unfixable here: **songs play in real time.**
A 6-track playlist at 2 minutes' dwell is ~12 minutes per arm. Budget accordingly,
and prefer few arms with a big expected effect over a wide sweep.

SCORING — GROUND TRUTH ONLY
---------------------------
Everything scored here compares the app's conclusion against the playlist's stated
truth. Deliberately NOT used as objectives (all verified perverse — see
docs/AUTORESEARCH.md): resync_count (optimum: never correct), sync_in_window_pct
(its own window knobs are tunable, so the optimiser widens the ruler),
time_to_sync_s (measures Shazam agreement, ~84% null), /metrics success (the concert
rule requires no sync at all).

`wrong_lock` is weighted hardest on purpose: confidently showing the WRONG song is
worse than showing nothing, and an optimiser that is not told this will happily
trade correctness for speed.

USAGE
-----
    python scripts/autoresearch.py --playlist research/playlist.json \\
                                   --arms research/arms.json \\
                                   --out research/results.json

Both inputs are plain JSON; see --write-samples to generate templates.

NOTE ON PLAYBACK: this opens each URL in the default browser, so it WILL take focus.
That is intentional and safe here (the whole point is that nobody is at the machine),
but it is why this must never be run during normal use.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import unicodedata
import re
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

API = "http://127.0.0.1:8765"


# ── plumbing ────────────────────────────────────────────────────────────────
def _get(path: str, timeout: float = 8.0):
    with urllib.request.urlopen(f"{API}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path: str, timeout: float = 8.0):
    req = urllib.request.Request(f"{API}{path}", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def set_knob(key: str, value) -> bool:
    """POST /tune. NOT persisted: set_tune coerces to the existing value's TYPE, so an
    int knob silently truncates a float — the runner reads back and verifies."""
    try:
        _post(f"/tune?key={urllib.parse.quote(key)}&value={urllib.parse.quote(str(value))}")
        live = _get("/tune").get("tune", {})
        got = live.get(key)
        if got is None:
            print(f"    !! {key} is not a registered knob — ignored by the app")
            return False
        if abs(float(got) - float(value)) > 1e-6:
            print(f"    !! {key} set to {value} but reads back {got} "
                  f"(type coercion — the app stores this knob as {type(got).__name__})")
            return False
        return True
    except Exception as e:
        print(f"    !! failed to set {key}: {e}")
        return False


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[^a-z0-9぀-ヿ一-鿿]+", "", s)


def title_matches(got: str, expect: str) -> bool:
    a, b = _norm(got), _norm(expect)
    if not a or not b:
        return False
    return a == b or (len(a) > 3 and len(b) > 3 and (a in b or b in a))


# ── one track ───────────────────────────────────────────────────────────────
def run_track(entry: dict, dwell: float, poll: float = 2.0) -> dict:
    """Play one playlist entry and record what the app concluded, against truth."""
    url = entry["url"]
    expect = entry.get("expect_title") or ""
    t0 = time.time()
    print(f"    -> {expect or url}")
    webbrowser.open(url)

    first_lyrics = None
    first_correct = None
    wrong_locks = set()
    drifts: list[float] = []
    offsets: list[float] = []
    last = {}

    while time.time() - t0 < dwell:
        time.sleep(poll)
        try:
            st = _get("/status")
        except Exception:
            continue
        last = st
        el = time.time() - t0
        n = st.get("line_count") or 0
        if n > 0 and first_lyrics is None:
            first_lyrics = round(el, 1)
        matched = st.get("matched_title") or ""
        if matched:
            if title_matches(matched, expect):
                if first_correct is None:
                    first_correct = round(el, 1)
            else:
                # Confidently displaying a DIFFERENT song than the one playing.
                wrong_locks.add(matched[:60])
        d = st.get("sync_drift")
        if isinstance(d, (int, float)):
            drifts.append(abs(float(d)))
        o = st.get("sync_offset")
        if isinstance(o, (int, float)):
            offsets.append(float(o))

    identified = first_correct is not None
    return {
        "expect": expect,
        "url": url,
        "identified": identified,
        "time_to_lyrics_s": first_lyrics,
        "time_to_identified_s": first_correct,
        "wrong_locks": sorted(wrong_locks),
        "wrong_lock": bool(wrong_locks) and not identified,
        "lines": last.get("line_count") or 0,
        "drift_p50": round(statistics.median(drifts), 3) if drifts else None,
        "drift_max": round(max(drifts), 3) if drifts else None,
        "drift_samples": len(drifts),
        "final_offset": round(offsets[-1], 3) if offsets else None,
    }


def score_arm(tracks: list[dict]) -> dict:
    """Aggregate one arm. Ground truth only; wrong_lock dominates deliberately."""
    n = len(tracks) or 1
    ident = sum(1 for t in tracks if t["identified"])
    wrong = sum(1 for t in tracks if t["wrong_lock"])
    ttl = [t["time_to_lyrics_s"] for t in tracks if t["time_to_lyrics_s"] is not None]
    tti = [t["time_to_identified_s"] for t in tracks if t["time_to_identified_s"] is not None]
    dr = [t["drift_p50"] for t in tracks if t["drift_p50"] is not None]
    # A wrong lock costs more than a miss: showing the wrong song confidently is the
    # failure the user actually complains about. Speed only counts once correct.
    score = (ident / n) - 1.5 * (wrong / n)
    if tti:
        score -= min(0.25, (statistics.median(tti) / 600.0))
    return {
        "tracks": n,
        "identified": ident,
        "identified_pct": round(100.0 * ident / n, 1),
        "wrong_locks": wrong,
        "median_time_to_lyrics_s": round(statistics.median(ttl), 1) if ttl else None,
        "median_time_to_identified_s": round(statistics.median(tti), 1) if tti else None,
        "median_drift": round(statistics.median(dr), 3) if dr else None,
        "score": round(score, 4),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Score knob configs against a known playlist.")
    ap.add_argument("--playlist", help="JSON list of {url, expect_title, [dwell_s]}")
    ap.add_argument("--arms", help="JSON list of {name, tune:{knob:value}}")
    ap.add_argument("--out", default="research/results.json")
    ap.add_argument("--dwell", type=float, default=150.0, help="seconds per track")
    ap.add_argument("--settle", type=float, default=6.0, help="pause between tracks")
    ap.add_argument("--write-samples", action="store_true", help="write template inputs and exit")
    a = ap.parse_args(argv)

    if a.write_samples:
        Path("research").mkdir(exist_ok=True)
        Path("research/playlist.json").write_text(json.dumps([
            {"url": "https://www.youtube.com/watch?v=REPLACE_ME",
             "expect_title": "Song Title As The App Should Report It", "dwell_s": 150},
        ], indent=2), encoding="utf-8")
        Path("research/arms.json").write_text(json.dumps([
            {"name": "baseline", "tune": {}},
            {"name": "concert-fast-first-read",
             "tune": {"concert_first_read_max_s": 2.5}},
            {"name": "concert-strict",
             "tune": {"concert_first_read_max_s": 0.0}},
        ], indent=2), encoding="utf-8")
        print("wrote research/playlist.json and research/arms.json")
        return 0

    if not (a.playlist and a.arms):
        ap.error("--playlist and --arms are required (or use --write-samples)")

    playlist = json.loads(Path(a.playlist).read_text(encoding="utf-8"))
    arms = json.loads(Path(a.arms).read_text(encoding="utf-8"))

    try:
        h = _get("/health")
        print(f"app online: v{h.get('version')}")
    except Exception as e:
        print(f"app is not reachable on {API}: {e}")
        return 2

    # Snapshot every knob any arm touches, so the machine is left exactly as found
    # even if a run aborts. An autotuner that silently leaves knobs moved is a trap.
    touched = sorted({k for arm in arms for k in (arm.get("tune") or {})})
    original = {k: _get("/tune").get("tune", {}).get(k) for k in touched}
    print(f"snapshotted {len(original)} knob(s) for restore: {original}")

    est = len(arms) * len(playlist) * (a.dwell + a.settle) / 60.0
    print(f"\n{len(arms)} arms x {len(playlist)} tracks — estimated {est:.0f} min\n")

    results = {"started": time.strftime("%Y-%m-%d %H:%M:%S"),
               "app_version": h.get("version"), "dwell_s": a.dwell,
               "playlist": a.playlist, "arms": []}
    try:
        for arm in arms:
            name = arm.get("name") or "unnamed"
            print(f"[arm] {name}")
            applied, failed = {}, []
            for k, v in (arm.get("tune") or {}).items():
                (applied.setdefault(k, v) if set_knob(k, v) else failed.append(k))
            if failed:
                print(f"    !! {len(failed)} knob(s) did not apply — results are NOT "
                      f"attributable to this arm: {failed}")
            tracks = []
            for entry in playlist:
                tracks.append(run_track(entry, float(entry.get("dwell_s") or a.dwell)))
                time.sleep(a.settle)
            summary = score_arm(tracks)
            print(f"    = identified {summary['identified']}/{summary['tracks']}"
                  f"  wrong-locks {summary['wrong_locks']}"
                  f"  score {summary['score']}")
            results["arms"].append({"name": name, "tune": applied,
                                    "failed_knobs": failed,
                                    "summary": summary, "tracks": tracks})
    except KeyboardInterrupt:
        print("\ninterrupted — restoring knobs")
    finally:
        for k, v in original.items():
            if v is not None:
                set_knob(k, v)
        print(f"restored {len(original)} knob(s)")

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n=== ranking (higher is better) ===")
    ranked = sorted(results["arms"], key=lambda r: r["summary"]["score"], reverse=True)
    for r in ranked:
        s = r["summary"]
        print(f"  {s['score']:+.4f}  {r['name']:<28} "
              f"id {s['identified_pct']:>5.1f}%  wrong {s['wrong_locks']}  "
              f"lyrics@{s['median_time_to_lyrics_s']}s")
    if len(ranked) > 1:
        top, second = ranked[0]["summary"], ranked[1]["summary"]
        if abs(top["score"] - second["score"]) < 0.15 or top["tracks"] < 8:
            print("\n  NOT a conclusion: the gap is inside the noise for this sample "
                  "size. Between-song variance here exceeds most knob effects — "
                  "re-run with more tracks before believing a winner.")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
