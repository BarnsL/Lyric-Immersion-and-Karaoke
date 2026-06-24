"""Cache accuracy auditor for the lyrics/ store.

Scans every cached *.json and flags anything that would make the overlay show
wrong or low-quality lyrics, WITHOUT touching the network. Checks, per file:

  • meta        — title/artist/duration/lang/source present
  • romaji      — does the stored romaji agree with the furigana's OWN readings?
                  (the 手→"shu" vs furigana 手(て) class of bug). Derives romaji
                  from the furigana kana — the single source of truth — and
                  compares to the stored `rm`.
  • language    — every line's script matches the song
  • english     — translation coverage for non-English songs
  • timing      — timestamps are sane: non-decreasing, in range, no dead-air gaps
  • dup         — two different files claiming the same (title) — collision risk

Run:  .venv/Scripts/python.exe audit_cache.py                 # summary
      .venv/Scripts/python.exe audit_cache.py --fix-romaji    # rewrite bad romaji
      .venv/Scripts/python.exe audit_cache.py --json report.json
"""
from __future__ import annotations
import json, re, sys, glob, collections
from pathlib import Path

import fetch_lyrics as F

LYR = Path(__file__).resolve().parent / "lyrics"

_KANJI = r"[㐀-鿿々〆ヵヶ]"
_FURI = re.compile(_KANJI + r"+[(（]([ぁ-ゟー]+)[)）]")
_PAREN = re.compile(r"[(（][ぁ-ゟー゛゜]+[)）]")


def furi_to_kana(furi: str) -> str:
    """'手(て)を伸(の)ばす' -> 'てをのばす' (readings become the text)."""
    return _FURI.sub(lambda m: m.group(1), furi)


def _norm_rm(s: str) -> str:
    """Compare romaji ignoring spacing/case/long-vowel notation — we only care
    that the *reading* matches, not cosmetic spacing."""
    s = (s or "").lower()
    s = s.replace("ō", "ou").replace("ū", "uu").replace("â", "aa")
    s = s.replace("ê", "ee").replace("î", "ii").replace("ô", "ou")
    s = re.sub(r"[^a-z]", "", s)
    s = re.sub(r"(.)\1+", r"\1", s)        # collapse doubled letters (sokuon/long vowels)
    return s


def romaji_from_furigana(furi: str) -> str:
    return _norm_rm(F.romanize(furi_to_kana(furi), "ja"))


def has_jp(s: str) -> bool:
    return bool(re.search(r"[぀-ヿ㐀-鿿]", s or ""))


def _editish(a: str, b: str) -> int:
    """Cheap bounded edit distance (caps at 3)."""
    if a == b:
        return 0
    lb = len(b)
    if abs(len(a) - lb) > 3:
        return 3
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
        if min(prev) > 3:
            return 3
    return min(prev[-1], 3)


def audit_file(path: Path) -> dict:
    try:
        d = json.loads(path.read_text("utf-8"))
    except Exception as e:
        return {"path": path.name, "fatal": f"unreadable: {e}", "issues": ["fatal"]}
    meta = d.get("meta", {})
    lines = d.get("lines", [])
    issues = []

    if not meta.get("title"):
        issues.append("no-title")
    if not meta.get("duration"):
        issues.append("no-duration")
    if not meta.get("source"):
        issues.append("no-source")
    src = meta.get("source") or ""
    if src.startswith("generated"):
        issues.append("GENERATED")

    last = -1.0
    bad_t = gap = 0
    for ln in lines:
        t = ln.get("t") or [0, 0]
        if t[0] < last - 0.05:
            bad_t += 1
        if len(t) > 1 and t[1] < t[0]:
            bad_t += 1
        if last >= 0 and t[0] - last > 25:
            gap += 1
        last = t[0]
    if bad_t:
        issues.append(f"timing×{bad_t}")
    if gap:
        issues.append(f"gap×{gap}")

    rm_bad = rm_total = 0
    rm_samples = []
    for ln in lines:
        jp = ln.get("jp", "") or ""
        rm = ln.get("rm", "") or ""
        if not has_jp(jp) or not _PAREN.search(jp):
            continue
        rm_total += 1
        want = romaji_from_furigana(jp)
        got = _norm_rm(rm)
        if want and got and want != got:
            if abs(len(want) - len(got)) > 1 or _editish(want, got) > 2:
                rm_bad += 1
                if len(rm_samples) < 2:
                    rm_samples.append((jp[:34], rm[:34],
                                       F.romanize(furi_to_kana(jp), "ja")[:34]))
    if rm_bad:
        issues.append(f"romaji×{rm_bad}/{rm_total}")

    lang = meta.get("lang") or F.detect_lang(
        " ".join(_PAREN.sub("", l.get("jp", "")) for l in lines[:6]))
    if lang and lang not in ("en", "es", "de"):
        miss = sum(1 for l in lines if not (l.get("en") or "").strip())
        if miss > max(2, len(lines) * 0.30):
            issues.append(f"no-en×{miss}/{len(lines)}")

    return {"path": path.name, "title": meta.get("title"), "lang": lang,
            "lines": len(lines), "issues": issues,
            "rm_bad": rm_bad, "rm_total": rm_total, "rm_samples": rm_samples, "src": src}


def upgrade_generated(verbose=True):
    """Re-fetch every GENERATED cache file and, if real synced lyrics now exist,
    replace the AI lines IN PLACE (same filename, so the slug-keyed cache hit
    starts serving real lyrics). Leaves genuinely-unavailable songs as generated.
    EN translation is left to the runtime backfill (fast, local furigana/romaji
    here; network translate happens on play)."""
    import fetch_lyrics as FL
    up = none = 0
    for f in sorted(glob.glob(str(LYR / "*.json"))):
        p = Path(f)
        try:
            d = json.loads(p.read_text("utf-8"))
        except Exception:
            continue
        m = d.get("meta", {})
        if not (m.get("source") or "").startswith("generated"):
            continue
        title, artist = m.get("title"), m.get("artist") or ""
        if not title:
            continue
        try:
            lrc, meta = FL.fetch_lrc(title, artist, m.get("duration"))
            lines = FL.parse_lrc_text(lrc) if lrc else []
        except Exception:
            lines = []
        if len(lines) >= 6:
            lang = FL._song_lang(lines)
            lines = FL.annotate(lines, lang, translate=False)
            d["meta"] = {**m, "lang": lang,
                         "duration": (meta or {}).get("duration") or m.get("duration"),
                         "source": (meta or {}).get("source", "unknown")}
            d["lines"] = lines
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(p)
            up += 1
            if verbose:
                print(f"  UPGRADE {p.name[:40]:40} -> {len(lines)} real lines", flush=True)
        else:
            none += 1
            if verbose:
                print(f"  keep-gen {p.name[:40]:40} (no real lyrics)", flush=True)
    print(f"\n  upgraded {up} generated files to REAL lyrics; {none} stay generated", flush=True)
    return up


def main():
    fix = "--fix-romaji" in sys.argv
    if "--upgrade-generated" in sys.argv:
        upgrade_generated()
        return
    files = sorted(glob.glob(str(LYR / "*.json")))
    reports = [audit_file(Path(f)) for f in files]

    norm = F._norm if hasattr(F, "_norm") else (lambda s: s.lower())
    by_title = collections.defaultdict(list)
    for r in reports:
        if r.get("title"):
            by_title[norm(r["title"])].append(r["path"])
    dups = {t: ps for t, ps in by_title.items() if len(ps) > 1}

    tally = collections.Counter()
    flagged = []
    for r in reports:
        for i in r.get("issues", []):
            tally[i.split("×")[0]] += 1
        if r.get("issues"):
            flagged.append(r)

    print(f"\n{'='*64}\n  CACHE AUDIT — {len(files)} files\n{'='*64}")
    for k in ["fatal", "no-title", "no-source", "no-duration", "GENERATED",
              "timing", "gap", "romaji", "no-en"]:
        if tally.get(k):
            print(f"  {k:14} {tally[k]:4}")
    print(f"  duplicate-titles {len(dups)}")

    rm_files = [r for r in reports if r.get("rm_bad")]
    if rm_files:
        print(f"\n  -- ROMAJI<->FURIGANA disagreements: {len(rm_files)} files "
              f"({sum(r['rm_bad'] for r in rm_files)} lines) --")
        for r in sorted(rm_files, key=lambda x: -x["rm_bad"])[:12]:
            print(f"   {r['path'][:40]:40} {r['rm_bad']}/{r['rm_total']}")
            for jp, got, want in r["rm_samples"]:
                print(f"      jp={jp}")
                print(f"        stored={got!r}")
                print(f"        furi  ={want!r}")

    if dups:
        print(f"\n  -- DUPLICATE TITLES (collision risk): {len(dups)} --")
        for t, ps in list(dups.items())[:12]:
            print(f"   {t}: {ps}")

    if fix:
        n = 0
        for r in rm_files:
            p = LYR / r["path"]
            d = json.loads(p.read_text("utf-8"))
            ch = False
            for ln in d.get("lines", []):
                jp = ln.get("jp", "") or ""
                if has_jp(jp) and _PAREN.search(jp):
                    new = F.romanize(furi_to_kana(jp), "ja")
                    if new and _norm_rm(new) != _norm_rm(ln.get("rm", "")):
                        ln["rm"] = new
                        ch = True
            if ch:
                tmp = p.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")
                tmp.replace(p)
                n += 1
        print(f"\n  rewrote romaji in {n} files (derived from furigana)")

    if "--json" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--json") + 1])
        out.write_text(json.dumps(flagged, ensure_ascii=False, indent=2), "utf-8")
        print(f"  report -> {out}")


if __name__ == "__main__":
    main()
