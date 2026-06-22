"""
Fetch synced lyrics from LRCLIB, auto-generate furigana + romaji,
and (best-effort) English translation.

Usage:
    python fetch_lyrics.py "Title" "Artist"
    python fetch_lyrics.py --lrc path/to/file.lrc "Title" "Artist"
    python fetch_lyrics.py --no-en "Title" "Artist"     # skip translation

Importable:
    from fetch_lyrics import fetch_and_save
    path = fetch_and_save("フィーリングラデーション", "ReGLOSS")
"""

import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# syncedlyrics logs noisy provider warnings (e.g. Musixmatch 401) — quiet them
logging.getLogger("syncedlyrics").setLevel(logging.CRITICAL)
logging.getLogger("syncedlyrics.providers").setLevel(logging.CRITICAL)

if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

LYRICS_DIR = Path(__file__).parent / "lyrics"
_KANJI = r"[一-鿿㐀-䶿々]"
_JP_RE = re.compile(r"[ぁ-んァ-ヶー一-鿿々]")
_CREDIT_RE = re.compile(
    r"^\s*(作词|作詞|作曲|编曲|編曲|制作|製作|制作人|製作人|监制|監製|混音|母带|母帶|"
    r"和声|和聲|录音|錄音|出品|发行|發行|策划|策劃|"
    r"Produced|Producer|Lyricist|Lyrics?|Composer|Arrang|Mixing|Mix|Master|"
    r"Vocal|Music|Words|Guitar|Bass|Drums)\b\s*[:：]",
    re.I,
)

_kks = None


def is_japanese(text: str) -> bool:
    return bool(_JP_RE.search(text))


def _kakasi():
    global _kks
    if _kks is None:
        import pykakasi
        _kks = pykakasi.kakasi()
    return _kks


def slugify(title: str) -> str:
    return re.sub(r"[^\w぀-ヿ一-鿿]+", "_", title.lower()).strip("_")


# ── Multi-provider fetch (Musixmatch / NetEase / LRCLIB / …) ─────────

def fetch_lrc(title: str, artist: str = "") -> str | None:
    """Best synced LRC from any provider. syncedlyrics covers Musixmatch,
    NetEase, LRCLIB, Megalobiz, Genius — huge VTuber/anime/JP coverage."""
    queries = []
    t, a = title.strip(), artist.strip()
    if t and a:
        queries += [f"{t} {a}", f"{a} {t}"]
    if t:
        queries.append(t)

    try:
        import syncedlyrics
        for q in queries:
            try:
                lrc = syncedlyrics.search(q, synced_only=True)
            except Exception:
                lrc = None
            if lrc and "[" in lrc:
                return lrc
    except ImportError:
        pass

    # Fallback: LRCLIB direct
    c = search_lrclib(title, artist, interactive=False)
    return c.get("syncedLyrics") if c else None


# ── LRCLIB search ────────────────────────────────────────────────────

def search_lrclib(title: str, artist: str, interactive: bool = True) -> dict | None:
    queries = [
        f"{title} {artist}",
        title,
        f"{title} {artist} hololive",
        f"{title} hololive",
    ]
    candidates: list[dict] = []
    seen_ids: set = set()

    for q in queries:
        url = f"https://lrclib.net/api/search?q={urllib.parse.quote(q)}"
        req = urllib.request.Request(url, headers={"User-Agent": "nihongo-lyrics/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                results = json.loads(resp.read())
            for r in results:
                rid = r.get("id", id(r))
                if r.get("syncedLyrics") and rid not in seen_ids:
                    seen_ids.add(rid)
                    candidates.append(r)
        except Exception:
            continue

    if not candidates:
        return None

    # Prefer exact-ish title + artist match
    tl = title.lower()
    al = artist.lower()

    def score(c):
        ct = c.get("trackName", "").lower()
        ca = c.get("artistName", "").lower()
        s = 0
        if tl and (tl in ct or ct in tl):
            s += 3
        if al and (al in ca or ca in al):
            s += 2
        return s

    candidates.sort(key=score, reverse=True)
    if score(candidates[0]) >= 2:
        return candidates[0]

    if not interactive:
        return candidates[0]

    if len(candidates) == 1:
        return candidates[0]

    print(f"\n  Found {len(candidates)} results — pick one:\n")
    for i, c in enumerate(candidates[:8]):
        dur = c.get("duration", 0)
        m, s = divmod(int(dur), 60)
        print(f"    [{i + 1}] {c.get('trackName', '?')} — {c.get('artistName', '?')} ({m}:{s:02d})")
    print("    [0] None of these\n")
    try:
        choice = int(input("  Choice: "))
    except (ValueError, EOFError):
        choice = 0
    if 1 <= choice <= len(candidates):
        return candidates[choice - 1]
    return None


def parse_lrc_text(lrc: str) -> list[dict]:
    lines = []
    for m in re.finditer(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)", lrc):
        mins, secs, text = int(m.group(1)), float(m.group(2)), m.group(3).strip()
        lines.append({"time": round(mins * 60 + secs, 2), "text": text})
    lines.sort(key=lambda x: x["time"])

    result = []
    for i, ln in enumerate(lines):
        end = lines[i + 1]["time"] if i + 1 < len(lines) else ln["time"] + 5.0
        if not ln["text"] or _CREDIT_RE.search(ln["text"]):
            continue
        result.append({
            "t": [ln["time"], round(end, 2)],
            "jp": ln["text"], "rm": "", "en": "",
        })
    return result


# ── Annotation ───────────────────────────────────────────────────────

def to_furigana(text: str) -> str:
    """Wrap kanji words with their hiragana reading: 静けさ(しずけさ)."""
    out = []
    for item in _kakasi().convert(text):
        orig, hira = item["orig"], item["hira"]
        if orig != hira and re.search(_KANJI, orig):
            out.append(f"{orig}({hira})")
        else:
            out.append(orig)
    return "".join(out)


def to_romaji(text: str) -> str:
    return " ".join(it["hepburn"] for it in _kakasi().convert(text)).strip()


def _translate_lines(lines: list[dict]) -> int:
    """Fill the 'en' field of each Japanese line in place. Returns count."""
    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        print("  [info] deep-translator not installed — skipping English")
        return 0
    tr = GoogleTranslator(source="ja", target="en")
    # Only translate Japanese lines; leave non-JP (e.g. English songs) as-is
    idx = [i for i, ln in enumerate(lines)
           if is_japanese(re.sub(r"\(.*?\)", "", ln["jp"]))]
    raws = [re.sub(r"\(.*?\)", "", lines[i]["jp"]) for i in idx]
    done = 0
    for k in range(0, len(raws), 20):
        sl = idx[k:k + 20]
        chunk = raws[k:k + 20]
        joined = "\n".join(c if c.strip() else "　" for c in chunk)
        try:
            parts = tr.translate(joined).split("\n")
            if len(parts) == len(chunk):
                for j, gi in enumerate(sl):
                    lines[gi]["en"] = parts[j].strip()
                done += len(chunk)
                continue
        except Exception:
            pass
        for j, gi in enumerate(sl):
            try:
                lines[gi]["en"] = tr.translate(chunk[j]) if chunk[j].strip() else ""
                done += 1
            except Exception:
                pass
    return done


def add_annotations(lines: list[dict], translate: bool = True) -> list[dict]:
    for ln in lines:
        raw = ln["jp"]
        if is_japanese(raw):
            ln["jp"] = to_furigana(raw)
            ln["rm"] = to_romaji(raw)
        else:
            ln["jp"] = raw          # English/other: show as-is, no romaji
            ln["rm"] = ""
    print(f"  [ok] Furigana + romaji for {len(lines)} lines")
    if translate:
        n = _translate_lines(lines)
        if n:
            print(f"  [ok] English translation for {n} lines")
    return lines


def translate_file(path) -> bool:
    """Load a saved lyrics JSON, fill in English, save back."""
    path = Path(path)
    try:
        data = json.loads(path.read_text("utf-8"))
        n = _translate_lines(data["lines"])
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return n > 0
    except Exception:
        return False


# ── Public API ───────────────────────────────────────────────────────

def fetch_and_save(title: str, artist: str = "", translate: bool = True,
                   interactive: bool = False) -> Path | None:
    lrc = fetch_lrc(title, artist)
    if not lrc:
        return None
    lines = parse_lrc_text(lrc)
    if not lines:
        return None
    lines = add_annotations(lines, translate=translate)

    LYRICS_DIR.mkdir(exist_ok=True)
    out = LYRICS_DIR / f"{slugify(title)}.json"
    data = {
        "meta": {"title": title, "artist": artist},
        "lines": lines,
    }
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    lrc_path = None
    translate = True
    positional = []
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--lrc" and i + 1 < len(args):
            lrc_path = Path(args[i + 1]); i += 2
        elif args[i] == "--no-en":
            translate = False; i += 1
        elif not args[i].startswith("-"):
            positional.append(args[i]); i += 1
        else:
            i += 1

    if len(positional) < 1:
        print(__doc__.strip())
        sys.exit(1)

    title = positional[0]
    artist = positional[1] if len(positional) > 1 else ""

    if lrc_path:
        print(f"Importing LRC from {lrc_path} ...")
        lines = parse_lrc_text(lrc_path.read_text(encoding="utf-8"))
        print(f"  [ok] Parsed {len(lines)} timed lines")
        lines = add_annotations(lines, translate=translate)
        LYRICS_DIR.mkdir(exist_ok=True)
        out = LYRICS_DIR / f"{slugify(title)}.json"
        data = {"meta": {"title": title, "artist": artist}, "lines": lines}
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  [ok] Saved to {out}")
        return

    print(f"Searching LRCLIB for: {title}" + (f" by {artist}" if artist else "") + " ...")
    out = fetch_and_save(title, artist, translate=translate, interactive=True)
    if not out:
        print("\nNot found on LRCLIB. Try an .lrc import:")
        print(f'  python fetch_lyrics.py --lrc file.lrc "{title}" "{artist}"')
        sys.exit(1)
    print(f"  [ok] Saved to {out}")
    print("\nRun the overlay:  python main.py")


if __name__ == "__main__":
    main()
