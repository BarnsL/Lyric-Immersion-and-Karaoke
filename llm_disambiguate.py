"""
Optional LLM song disambiguation — gated on an Anthropic API key.

When a key is present, this asks the newest Claude model to decide which
candidate song the LIVE VOCALS actually are, by matching a Whisper
transcription of the singing against candidate lyric bodies. It is far more
robust than char-level fuzzy matching on a short / noisy / ASR-mangled
transcript, and is the lever for the two worst failure modes in the success
scorecard: wrong-song (~31%) and title-search misses (~73%).

NO key is required to use the app. `available()` is False and `pick_best_match`
returns None whenever there is no key or anything goes wrong, and every caller
falls back to the existing rapidfuzz ranking. Mirrors the DeepL gating pattern
(`fetch_lyrics._make_translator`).

Key resolution order (first non-empty wins):
  1. ANTHROPIC_API_KEY environment variable
  2. the file named by ANTHROPIC_API_KEY_FILE, if that env var is set
  3. <data_dir>/anthropic-api-key.txt     (portable: next to the exe / settings)

Model: newest Claude by default; override with LYRIC_LLM_MODEL.

Implementation note: raw HTTPS via urllib — no SDK dependency to bundle, and the
call runs on the decide-by-ear WORKER thread, never the render thread.
"""

import hashlib
import json
import os
import urllib.request
from pathlib import Path

_ENDPOINT = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
# Strong + affordable for a per-song disambiguation call. Override via
# LYRIC_LLM_MODEL (e.g. claude-opus-4-8 for the hardest cases).
_DEFAULT_MODEL = "claude-sonnet-4-6"

_key_cache = None          # "" once resolved-empty, the key string once found
_verdict_cache = {}        # (heard+cands) signature -> verdict | None


def _env_key_file():
    """Optional: a key file path supplied via ANTHROPIC_API_KEY_FILE. No path is
    hardcoded so the repo carries no machine-specific filesystem reference."""
    p = (os.environ.get("ANTHROPIC_API_KEY_FILE") or "").strip()
    return Path(p) if p else None


def _data_key_file():
    try:
        from appdata import data_dir
        return data_dir() / "anthropic-api-key.txt"
    except Exception:
        return None


def _read_key():
    global _key_cache
    if _key_cache is not None:
        return _key_cache or None
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        files = []
        e = _env_key_file()
        if e is not None:
            files.append(e)
        d = _data_key_file()
        if d is not None:
            files.append(d)
        for f in files:
            try:
                if f and f.is_file():
                    k = f.read_text(encoding="utf-8").strip()
                    if k:
                        key = k
                        break
            except Exception:
                pass
    _key_cache = key or ""
    return key or None


def available():
    """True iff an API key is resolvable — so callers can skip all work when off."""
    return bool(_read_key())


def model():
    return (os.environ.get("LYRIC_LLM_MODEL") or "").strip() or _DEFAULT_MODEL


def _build_prompt(heard, candidates):
    blocks = []
    for i, c in enumerate(candidates):
        body = " ".join((c.get("body") or "").split())[:600]
        blocks.append(
            f"[{i}] title={c.get('title', '?')!r} artist={c.get('artist', '?')!r}\n"
            f"    lyrics: {body}"
        )
    return (
        "You identify which song some sung audio is. You are given an automatic "
        "transcription of the LIVE VOCALS (it may have ASR errors, wrong kanji, "
        "mixed scripts, or be only a short fragment) and several CANDIDATE songs "
        "with their lyrics. Decide which candidate the transcription is actually "
        "from. Match on meaning and lyric content, tolerating transcription "
        "noise; if none clearly fit, say so.\n\n"
        f"TRANSCRIPTION OF THE LIVE VOCALS:\n{heard[:800]}\n\n"
        "CANDIDATE SONGS:\n" + "\n".join(blocks) + "\n\n"
        "Reply with ONLY a compact JSON object and nothing else:\n"
        '{"best": <candidate index, or -1 if none clearly match>, '
        '"confidence": <0..1>, '
        '"matches_audio": <true only if the transcription is clearly that song>, '
        '"reason": "<short>"}'
    )


def _parse(text, candidates):
    try:
        s = text[text.index("{"): text.rindex("}") + 1]
        o = json.loads(s)
        bi = int(o.get("best", -1))
        if bi < 0 or bi >= len(candidates):
            return None
        return {
            "key": candidates[bi].get("key"),
            "index": bi,
            "confidence": max(0.0, min(1.0, float(o.get("confidence", 0.0)))),
            "matches_audio": bool(o.get("matches_audio", False)),
            "reason": str(o.get("reason", ""))[:200],
        }
    except Exception:
        return None


def pick_best_match(heard, candidates, timeout=8.0, max_tokens=200):
    """Ask Claude which candidate the vocals are.

    heard:      Whisper transcription of the live vocals (str)
    candidates: list of {"key", "title", "artist", "body"}

    Returns {"key","index","confidence","matches_audio","reason"} for the chosen
    candidate, or None when unavailable / on error / when nothing matched. Cached
    per (transcription, candidate-set) so a re-check doesn't re-bill."""
    key = _read_key()
    if not key or not heard or not candidates:
        return None
    sig = hashlib.sha1(
        (heard[:400] + "|" + "|".join(str(c.get("key", "")) for c in candidates))
        .encode("utf-8", "ignore")
    ).hexdigest()
    if sig in _verdict_cache:
        return _verdict_cache[sig]

    payload = json.dumps({
        "model": model(),
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": _build_prompt(heard, candidates)}],
    }).encode("utf-8")
    req = urllib.request.Request(
        _ENDPOINT, data=payload, method="POST",
        headers={
            "x-api-key": key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )
    verdict = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        verdict = _parse(text, candidates)
    except Exception:
        verdict = None
    _verdict_cache[sig] = verdict
    return verdict
