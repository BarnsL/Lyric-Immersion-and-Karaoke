"""
Desktop Karaoke — lyric fetching, annotation, and verification.

═══════════════════════════════════════════════════════════════════════
SOURCES USED
  • LRCLIB        (https://lrclib.net)  — clean, open, returns track /
                  artist / duration metadata so matches can be VERIFIED.
                  Used first via /api/get (duration-exact) then /api/search.
  • syncedlyrics  (PyPI) — aggregates Musixmatch / NetEase / Megalobiz /
                  Genius. Great coverage for VTuber / anime / CJK songs
                  that LRCLIB lacks, but returns only an LRC string with
                  no metadata, so results MUST be verified heuristically.
  • pykakasi      — Japanese → furigana + romaji (hepburn).
  • pypinyin      — Chinese → pinyin.
  • hangul-romanize — Korean → romaja.
  • deep-translator (Google) — line translation to English ('auto' source
                  so it covers ja / zh / ko / es alike).
  • Audio identification (recognize.py): soundcard (WASAPI loopback) +
                  shazamio (Shazam) — identifies the song by SOUND for covers
                  / mislabeled uploads. See recognize.py for details.

FUTURE / CANDIDATE SOURCES  (not yet wired — add here as providers for
  hard-to-find VTuber / indie / regional tracks)
  • PetitLyrics (プチリリ) — large synced catalog for JP anime / VTuber /
                  doujin; best next addition for songs the aggregators miss.
  • QQ Music / Kugou — synced lyrics for Chinese + much Asian pop.
  • Apple Music time-synced lyrics (needs an Apple Music API token).
  • Genius / AZLyrics / Uta-Net / J-Lyric — UNSYNCED only; usable as a
                  last-resort plain-text fallback (no karaoke timing).
  To add one: implement `def _provider(title, artist, duration) -> lrc|None`
  returning timed LRC, call it inside fetch_lrc() before returning None, and
  list it here.

PROBLEMS OVERCOME
  1. WRONG-SONG MATCHES. A bare title like "Lucky Star" matched a totally
     different song. Fix: prefer LRCLIB's duration-exact /api/get, score
     /api/search candidates on artist+title+duration, and for the opaque
     syncedlyrics fallback verify the result by song DURATION and LANGUAGE
     before accepting. Title-only queries are a last resort and still
     verified.
  2. WRONG-LANGUAGE / HALLUCINATED LYRICS. A Japanese song came back with
     Albanian text. Fix: detect_lang() on title + lyrics; if the title is
     CJK the lyrics must be the same script, else the result is rejected.
  3. CREDIT-LINE NOISE. Providers prefix "作词:" / "作曲:" etc. Filtered out.
  4. NetEase's public /api/search returns hot-charts garbage for foreign
     queries, so we do NOT call it directly — syncedlyrics handles it with
     its own signing, and we verify whatever it returns.
  5. NO PERSONAL DATA. Nothing here logs, stores, or transmits anything
     about the user, their account, or their machine — only public song
     title/artist strings are sent to public lyric APIs.
═══════════════════════════════════════════════════════════════════════

Public API:
    fetch_and_save(title, artist, translate=False, duration=None) -> Path|None
    translate_file(path) -> bool
    validate_file(path, duration=None) -> (ok: bool, reason: str)
    detect_lang(text) -> 'ja'|'ko'|'zh'|'other'

CLI:
    python fetch_lyrics.py "Title" "Artist"
    python fetch_lyrics.py --lrc file.lrc "Title" "Artist"
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
for _n in ("syncedlyrics", "syncedlyrics.providers"):
    logging.getLogger(_n).setLevel(logging.CRITICAL)

if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

LYRICS_DIR = Path(__file__).parent / "lyrics"

# Script ranges
_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
_KANA   = re.compile(r"[぀-ゟ゠-ヿ]")
_HAN    = re.compile(r"[一-鿿㐀-䶿々]")
_KANJI  = r"[一-鿿㐀-䶿々]"
_JP_RE  = re.compile(r"[ぁ-んァ-ヶー一-鿿々]")
_CREDIT_RE = re.compile(
    r"^\s*(作词|作詞|作曲|编曲|編曲|制作|製作|制作人|製作人|监制|監製|混音|母带|母帶|"
    r"和声|和聲|录音|錄音|出品|发行|發行|策划|策劃|"
    r"Produced|Producer|Lyricist|Lyrics?|Composer|Arrang|Mixing|Mix|Master|"
    r"Vocal|Music|Words|Guitar|Bass|Drums)\b\s*[:：]",
    re.I,
)


_ES_DIA = re.compile(r"[ñáéíóúü¿¡]", re.I)
_ES_WORDS = {
    "que", "qué", "más", "pero", "una", "por", "con", "los", "las", "está",
    "están", "cómo", "dónde", "corazón", "nada", "vida", "amor", "soy", "eres",
    "muy", "también", "aquí", "así", "mujer", "noche", "quiero", "él", "ella",
    "tú", "porque", "cuando", "siempre", "nunca", "todo", "todos", "mis", "tus",
    "señor", "tierra", "hombre", "dios", "compa", "plebe", "morena",
}


def is_japanese(text: str) -> bool:
    return bool(_JP_RE.search(text))


def _is_spanish(text: str) -> bool:
    if _ES_DIA.search(text):
        return True
    words = set(re.findall(r"[a-zñáéíóúü]+", text.lower()))
    return len(words & _ES_WORDS) >= 2


def detect_lang(text: str) -> str:
    """Coarse language of a string/lyric by dominant script / markers."""
    hang = len(_HANGUL.findall(text))
    kana = len(_KANA.findall(text))
    han = len(_HAN.findall(text))
    if hang and hang >= kana:
        return "ko"
    if kana:
        return "ja"
    if han:
        return "zh"
    if _is_spanish(text):
        return "es"
    return "other"


def slugify(title: str) -> str:
    return re.sub(r"[^\w぀-ヿ가-힣一-鿿]+", "_", title.lower()).strip("_")


def split_artists(artist: str) -> list[str]:
    """Break 'PeanutsKun, Ikuta Rira feat. X' → ['PeanutsKun','Ikuta Rira','X']."""
    parts = re.split(r"\s*[,/&、，]\s*|\s+(?:feat|ft|featuring|with|×|x)\.?\s+",
                     artist or "", flags=re.I)
    out, seen = [], set()
    for p in parts:
        p = p.strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


# ── Romanization ─────────────────────────────────────────────────────

_kks = None
_translit = None


def _kakasi():
    global _kks
    if _kks is None:
        import pykakasi
        _kks = pykakasi.kakasi()
    return _kks


def _korean():
    global _translit
    if _translit is None:
        from hangul_romanize import Transliter
        from hangul_romanize.rule import academic
        _translit = Transliter(academic)
    return _translit


def to_furigana(text: str) -> str:
    out = []
    for item in _kakasi().convert(text):
        orig, hira = item["orig"], item["hira"]
        if orig != hira and re.search(_KANJI, orig):
            out.append(f"{orig}({hira})")
        else:
            out.append(orig)
    return "".join(out)


def romanize(text: str, lang: str) -> str:
    try:
        if lang == "ja":
            return " ".join(it["hepburn"] for it in _kakasi().convert(text)).strip()
        if lang == "zh":
            from pypinyin import lazy_pinyin
            return " ".join(lazy_pinyin(text)).strip()
        if lang == "ko":
            return _korean().translit(text).replace("-", "").strip()
    except Exception:
        return ""
    return ""


# ── LRC parsing ──────────────────────────────────────────────────────

def parse_lrc_text(lrc: str) -> list[dict]:
    raw = []
    for line in lrc.splitlines():
        tags = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", line)
        if not tags:
            continue
        # strip ALL [mm:ss] line tags and <mm:ss> word tags from the text
        text = re.sub(r"\[\d+:\d+(?:\.\d+)?\]", "", line)
        text = re.sub(r"<\d+:\d+(?:\.\d+)?>", "", text).strip()
        for mm, ss in tags:                       # a line may repeat at several times
            raw.append({"time": round(int(mm) * 60 + float(ss), 2), "text": text})
    raw.sort(key=lambda x: x["time"])

    out = []
    for i, ln in enumerate(raw):
        if not ln["text"] or _CREDIT_RE.search(ln["text"]):
            continue
        end = raw[i + 1]["time"] if i + 1 < len(raw) else ln["time"] + 5.0
        out.append({"t": [ln["time"], round(end, 2)], "jp": ln["text"], "rm": "", "en": ""})
    return out


def _lrc_last_time(lrc: str) -> float:
    times = [int(m.group(1)) * 60 + float(m.group(2))
             for m in re.finditer(r"\[(\d+):(\d+(?:\.\d+)?)\]", lrc)]
    return max(times) if times else 0.0


# ── Verification (error detection) ───────────────────────────────────

def verify_lrc(lrc: str, title: str, duration: float | None) -> bool:
    """Reject lyrics that clearly don't belong to the requested song."""
    body = re.sub(r"\[[^\]]*\]", "", lrc)
    if len(body.strip()) < 10:
        return False
    # Language: if the title is CJK, the lyrics must share that script
    tl = detect_lang(title)
    if tl in ("ja", "ko", "zh"):
        ll = detect_lang(body)
        if ll != tl and not (tl == "zh" and ll == "ja"):
            return False
    # Duration: last timestamp should land within the song, not way past it
    if duration and duration > 30:
        last = _lrc_last_time(lrc)
        if last and (last < duration * 0.35 or last > duration + 45):
            return False
    return True


# ── LRCLIB (verifiable) ──────────────────────────────────────────────

def _http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Desktop-Karaoke/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _lrclib_get(title, artist, duration):
    if not (artist and duration):
        return None
    q = urllib.parse.urlencode({
        "track_name": title, "artist_name": artist, "duration": int(duration),
    })
    try:
        d = _http_json(f"https://lrclib.net/api/get?{q}")
        if d.get("syncedLyrics"):
            return {"lrc": d["syncedLyrics"], "artist": d.get("artistName", artist),
                    "duration": d.get("duration", duration)}
    except Exception:
        pass
    return None


def _lrclib_candidates(title, artist):
    cands, seen = [], set()
    urls = [f"https://lrclib.net/api/search?{urllib.parse.urlencode({'q': f'{title} {artist}'.strip()})}"]
    for ar in ([artist] + split_artists(artist))[:3]:
        if ar:
            urls.append("https://lrclib.net/api/search?"
                        + urllib.parse.urlencode({"track_name": title, "artist_name": ar}))
    for u in urls:
        try:
            for r in _http_json(u):
                rid = r.get("id")
                if rid in seen or not r.get("syncedLyrics"):
                    continue
                seen.add(rid)
                cands.append(r)
        except Exception:
            continue
    return cands


def _norm(s):
    return re.sub(r"[^\w가-힣一-鿿ぁ-ヶ]+", "", (s or "").lower())


def _pick_lrclib(title, artist, duration):
    best, best_score = None, 0
    nt = _norm(title)
    nas = [_norm(x) for x in split_artists(artist)] or [_norm(artist)]
    for c in _lrclib_candidates(title, artist):
        ct, ca = _norm(c.get("trackName")), _norm(c.get("artistName"))
        score = 0
        if nt and (nt in ct or ct in nt):
            score += 2
        if any(na and (na in ca or ca in na) for na in nas):
            score += 3
        if duration and c.get("duration"):
            if abs(c["duration"] - duration) <= 8:
                score += 3
            elif abs(c["duration"] - duration) > 25:
                score -= 3
        if score > best_score:
            best, best_score = c, score
    if best and best_score >= 4:
        return {"lrc": best["syncedLyrics"], "artist": best.get("artistName", artist),
                "duration": best.get("duration")}
    return None


# ── Multi-provider fetch with verification ───────────────────────────

def _strict_ok(lrc: str, title: str, duration: float | None) -> bool:
    """Extra guard for low-confidence (title-only) matches to cut false
    positives: only trust them when language gating or duration can confirm."""
    if detect_lang(title) in ("ja", "ko", "zh"):
        return True                        # language gate already applied
    if duration:
        last = _lrc_last_time(lrc)
        return bool(last and abs(last - duration) <= max(20, duration * 0.15))
    return False                           # Latin title, no duration → don't risk it


def fetch_lrc(title: str, artist: str = "", duration: float | None = None):
    """Return (lrc_string, meta) of a VERIFIED match, or (None, None).
    Widens the search across artist variants while guarding false positives."""
    t, a = title.strip(), artist.strip()
    arts = split_artists(a)

    # 1. LRCLIB duration-exact, trying the full credit then each artist
    for ca in ([a] + arts if a else []):
        hit = _lrclib_get(t, ca, duration)
        if hit and verify_lrc(hit["lrc"], t, duration):
            return hit["lrc"], {"source": "lrclib/get", "artist": hit["artist"],
                                "duration": hit.get("duration")}

    # 2. LRCLIB scored search (artist/title/duration)
    hit = _pick_lrclib(t, a, duration)
    if hit and verify_lrc(hit["lrc"], t, duration):
        return hit["lrc"], {"source": "lrclib/search", "artist": hit["artist"],
                            "duration": hit.get("duration")}

    # 3. syncedlyrics — title+artist queries (high confidence) first
    try:
        import syncedlyrics
    except ImportError:
        return None, None

    def _try(q):
        try:
            lrc = syncedlyrics.search(q, synced_only=True)
        except Exception:
            return None
        return lrc if (lrc and "[" in lrc) else None

    hi_q, seen = [], set()
    if t and arts:
        hi_q.append(f"{t} {a}")
        for ar in arts:
            hi_q += [f"{t} {ar}", f"{ar} {t}"]
    elif t and a:
        hi_q.append(f"{t} {a}")
    for q in hi_q:
        k = q.lower().strip()
        if k in seen:
            continue
        seen.add(k)
        lrc = _try(q)
        if lrc and verify_lrc(lrc, t, duration):
            return lrc, {"source": "syncedlyrics", "artist": a or None,
                         "duration": duration}

    # 4. title-only — last resort, strict guard against same-title wrong songs
    if t:
        lrc = _try(t)
        if lrc and verify_lrc(lrc, t, duration) and _strict_ok(lrc, t, duration):
            return lrc, {"source": "syncedlyrics/title", "artist": a or None,
                         "duration": duration}

    return None, None


# ── Annotation ───────────────────────────────────────────────────────

def _song_lang(lines: list[dict]) -> str:
    return detect_lang(" ".join(ln["jp"] for ln in lines[:40]))


def _translate_lines(lines: list[dict], song_lang: str | None = None) -> int:
    try:
        from deep_translator import GoogleTranslator
    except ImportError:
        return 0
    tr = GoogleTranslator(source="auto", target="en")
    whole = song_lang in ("ja", "ko", "zh", "es")

    def want(jp):
        raw = re.sub(r"\(.*?\)", "", jp)
        if not raw.strip():
            return False
        ll = detect_lang(raw)
        if ll in ("ja", "ko", "zh", "es"):
            return True
        return whole and ll == "other"     # Spanish line w/o accents, etc.

    idx = [i for i, ln in enumerate(lines) if want(ln["jp"])]
    raws = [re.sub(r"\(.*?\)", "", lines[i]["jp"]) for i in idx]
    done = 0
    for k in range(0, len(raws), 20):
        sl, chunk = idx[k:k + 20], raws[k:k + 20]
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


def annotate(lines: list[dict], lang: str, translate: bool = False) -> list[dict]:
    for ln in lines:
        raw = ln["jp"]
        ll = detect_lang(raw)
        if lang == "ja" and ll == "ja":
            ln["jp"] = to_furigana(raw)
            ln["rm"] = romanize(raw, "ja")
        elif lang in ("zh", "ko") and ll == lang:
            ln["rm"] = romanize(raw, lang)
        else:
            ln["rm"] = ""  # Spanish / English / other — shown as-is, no romaji
    if translate:
        _translate_lines(lines, lang)
    return lines


def translate_file(path) -> bool:
    path = Path(path)
    try:
        data = json.loads(path.read_text("utf-8"))
        n = _translate_lines(data["lines"], data.get("meta", {}).get("lang"))
        if n:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return n > 0
    except Exception:
        return False


# ── Library validation (error detection over cached files) ───────────

def validate_file(path, duration: float | None = None) -> tuple[bool, str]:
    """True if the cached file looks like a real, correct match."""
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except Exception as e:
        return False, f"unreadable ({e})"
    lines = data.get("lines", [])
    if len(lines) < 4:
        return False, "too few lines"
    meta = data.get("meta", {})
    title = meta.get("title", "")
    body = " ".join(ln.get("jp", "") for ln in lines)
    tl, ll = detect_lang(title), detect_lang(body)
    if tl in ("ja", "ko", "zh") and ll != tl and not (tl == "zh" and ll == "ja"):
        return False, f"language mismatch (title {tl} / lyrics {ll})"
    md = meta.get("duration")
    if duration and md and abs(md - duration) > 12:
        return False, f"duration mismatch ({md}s vs {duration}s)"
    return True, "ok"


# ── Save ─────────────────────────────────────────────────────────────

def fetch_and_save(title: str, artist: str = "", translate: bool = False,
                   duration: float | None = None, interactive: bool = False) -> Path | None:
    lrc, meta = fetch_lrc(title, artist, duration)
    if not lrc:
        return None
    lines = parse_lrc_text(lrc)
    if len(lines) < 4:
        return None
    lang = _song_lang(lines)
    lines = annotate(lines, lang, translate=translate)

    LYRICS_DIR.mkdir(exist_ok=True)
    out = LYRICS_DIR / f"{slugify(title)}.json"
    data = {
        "meta": {
            "title": title,
            "artist": artist,
            "lang": lang,
            "duration": (meta or {}).get("duration") or (round(duration, 1) if duration else None),
            "source": (meta or {}).get("source", "unknown"),
        },
        "lines": lines,
    }
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    lrc_path, positional = None, []
    translate = "--no-en" not in sys.argv
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--lrc" and i + 1 < len(args):
            lrc_path = Path(args[i + 1]); i += 2
        elif args[i].startswith("-"):
            i += 1
        else:
            positional.append(args[i]); i += 1

    if not positional:
        print(__doc__.split("Public API:")[0])
        sys.exit(1)
    title = positional[0]
    artist = positional[1] if len(positional) > 1 else ""

    if lrc_path:
        lines = parse_lrc_text(lrc_path.read_text(encoding="utf-8"))
        lang = _song_lang(lines)
        print(f"Parsed {len(lines)} lines (lang={lang})")
        lines = annotate(lines, lang, translate=translate)
        LYRICS_DIR.mkdir(exist_ok=True)
        out = LYRICS_DIR / f"{slugify(title)}.json"
        out.write_text(json.dumps(
            {"meta": {"title": title, "artist": artist, "lang": lang,
                      "duration": None, "source": "lrc-import"}, "lines": lines},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Saved {out}")
        return

    print(f"Fetching: {title} — {artist}")
    out = fetch_and_save(title, artist, translate=translate)
    print(f"Saved {out}" if out else "No verified lyrics found.")


if __name__ == "__main__":
    main()
