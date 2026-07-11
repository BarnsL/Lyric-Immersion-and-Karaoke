"""
Movie-site subtitle fetcher (TICKET-169).

Streaming aggregators (f2movies and siblings) play movies through embedded
players whose subtitle panels are fed by OpenSubtitles rips. yt-dlp can't see
inside those iframes, so Subtitles mode used to dead-end into whisper for a
110-minute film. This module gets the SAME subtitles the site's own panel
offers, from the source the panel itself rips from:

    1. Fetch the site's watch page (plain HTTP + a browser User-Agent — the
       page serves fine; only bot-fingerprinted clients get blocked).
    2. Extract the movie TITLE + YEAR (og:title / <title>) and, when present,
       the TMDB id from the embed iframe (kept for future providers).
    3. Query the legacy OpenSubtitles REST API by title
       (rest.opensubtitles.org — the Kodi-era endpoint; no API key) and rank
       English SRTs by download count with a year match bonus.
    4. Download the gzipped SRT, decode per the advertised encoding, parse to
       timed lines shaped exactly like deep_transcribe's caption output.

Self-contained: stdlib only (urllib/gzip/re/json). Fail-soft: any error
returns None and the caller falls back to the existing whisper/OCR tiers.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import time
import urllib.parse
import urllib.request

# Hosts whose watch pages this module understands. Substring-matched against
# the URL host. The f2movies family shares one page layout (Sflix clones);
# add siblings here as they're verified.
MOVIE_SUB_HOSTS = (
    "f2movies", "fmovies", "sflix", "solarmovie", "myflixer", "watchseries",
)

_UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
# The legacy REST endpoint authenticates by User-Agent string alone; this is
# the documented anonymous value (rate-limited but keyless).
_UA_OPENSUBS = "TemporaryUserAgent"

_TIMEOUT = 15


def is_movie_site(url: str) -> bool:
    """True when `url`'s host belongs to a supported movie-streaming site."""
    try:
        host = urllib.parse.urlsplit(url or "").netloc.lower()
    except Exception:
        return False
    return any(h in host for h in MOVIE_SUB_HOSTS)


def is_movie_window_title(title: str) -> bool:
    """True when a browser WINDOW/tab title carries a movie-site brand — the
    no-URL-pusher path: 'Watch X Movie… - F2movies - Brave'."""
    t = (title or "").lower()
    return any(h in t for h in MOVIE_SUB_HOSTS)


_BROWSER_SUFFIX = re.compile(
    r"(?i)\s*[-—–]\s*(brave|google chrome|microsoft.?\s*edge|mozilla firefox|"
    r"firefox|opera|vivaldi)\s*$")


def _clean_site_title(raw: str) -> str:
    """'Watch 3-D Sex and Zen: Extreme Ecstasy Movie… - F2movies - Brave'
    → '3-D Sex and Zen: Extreme Ecstasy'. Shared by the page and window-title
    paths."""
    t = _BROWSER_SUFFIX.sub("", raw or "")
    t = re.sub(r"(?i)^\s*watch\s+", "", t)
    t = re.sub(r"(?i)\s*[|\-–]\s*(f2movies|fmovies|sflix|solarmovie|myflixer|watchseries).*$", "", t)
    t = re.sub(r"(?i)\s+(movie|tv series|show)?\s*[….]*\s*$", "", t)
    t = re.sub(r"(?i)\s+(free|online|hd|full movie)\s*$", "", t).strip()
    return t


def _get(url: str, ua: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua,
                                               "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read()


def _extract_movie_identity(html: str, url: str):
    """(title, year, tmdb_id) from a watch page. Title falls back to the URL
    slug; year/tmdb may be None. f2movies layout: og:title carries the name,
    the embed iframe (or any server link) carries the TMDB id, and 'Released:'
    carries the date."""
    title = None
    m = re.search(r'property="og:title"\s+content="([^"]+)"', html)
    if not m:
        m = re.search(r"<title>([^<]+)</title>", html)
    if m:
        title = _clean_site_title(m.group(1))
    if not title:
        # /watch/movie-3-d-sex-and-zen-extreme-ecstasy-20yx0fbi → words minus id
        slug = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        slug = re.sub(r"^(movie|tv)-", "", slug)
        slug = re.sub(r"-[a-z0-9]{6,10}$", "", slug)      # trailing site id
        title = slug.replace("-", " ").strip()
    year = None
    m = re.search(r"Released:\s*</?\w*>?\s*(\d{4})", html) or \
        re.search(r"\b(19\d{2}|20\d{2})-\d{2}-\d{2}\b", html)
    if m:
        year = m.group(1)
    tmdb = None
    m = re.search(r"/embed/(?:movie|tv)/(\d{2,9})", html) or \
        re.search(r"[?&]video_id=(\d{2,9})\b", html)
    if m:
        tmdb = m.group(1)
    return title, year, tmdb


def _search_opensubtitles(title: str, year: str | None):
    """Ranked candidate list from the legacy REST API (English, SRT-first).
    The query matcher is picky about decorated titles ("3-D X: Y" finds
    nothing while "x y" finds 15), so retry with progressively simpler
    forms: full → without a leading number/format token → last 4+ words."""
    base = re.sub(r"[^\w\s]", " ", title)
    base = re.sub(r"\s+", " ", base).strip().lower()
    variants = [base]
    toks = base.split()
    if toks and re.fullmatch(r"\d+\s*d?|3d|4k", toks[0]):
        variants.append(" ".join(toks[1:]))            # drop "3 d" / "4k" prefix
    if len(toks) > 4:
        variants.append(" ".join(toks[-4:]))           # distinctive tail
    data = []
    for v in variants:
        if not v:
            continue
        q = urllib.parse.quote(v)
        url = f"https://rest.opensubtitles.org/search/query-{q}/sublanguageid-eng"
        try:
            data = json.loads(_get(url, _UA_OPENSUBS).decode("utf-8", "replace"))
        except Exception:
            data = []
        if isinstance(data, list) and data:
            break
    if not isinstance(data, list):
        return []

    def score(s):
        sc = 0.0
        try:
            sc += min(float(s.get("SubDownloadsCnt") or 0), 200000) / 1000.0
        except Exception:
            pass
        if (s.get("SubFormat") or "").lower() == "srt":
            sc += 50
        if year and str(s.get("MovieYear") or "") == str(year):
            sc += 120
        if (s.get("SubBad") or "0") not in ("0", 0):
            sc -= 100
        return sc

    return sorted((s for s in data if s.get("SubDownloadLink")),
                  key=score, reverse=True)


_SRT_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
_SRT_TAGS = re.compile(r"<[^>]+>|\{\\[^}]*\}")


def _parse_srt(text: str):
    """SRT → [{"t": start_s, "end": end_s, "jp": line}] (the caption-line shape
    the subtitles pipeline consumes; 'jp' is the body field regardless of
    language). Multi-line cues are joined; markup stripped; ad cues dropped."""
    lines = []
    for block in re.split(r"\r?\n\r?\n+", text):
        m = _SRT_TIME.search(block)
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(g) for g in m.groups())
        start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0
        end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0
        body = block[m.end():]
        rows = [ln.strip() for ln in body.splitlines() if ln.strip()]
        txt = _SRT_TAGS.sub("", " ".join(rows)).strip()
        if not txt:
            continue
        low = txt.lower()
        # skip the sub-scene ad cues OpenSubtitles injects
        if "opensubtitles" in low or "osdb.link" in low or "advertise your product" in low:
            continue
        # exact caption-line shape deep_transcribe._parse_vtt emits
        lines.append({"t": [round(start, 2), round(end, 2)],
                      "jp": txt, "rm": "", "en": ""})
    return lines


def _download_srt(link: str, encoding: str | None):
    raw = _get(link, _UA_OPENSUBS)
    try:
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    except Exception:
        pass                                    # some mirrors serve plain text
    for enc in (encoding, "utf-8-sig", "utf-8", "cp1252", "latin-1"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def _fetch_by_title(title: str, year: str | None, tmdb, log):
    """Shared core: title(+year) → best English SRT → parsed caption lines.
    Tries the top few candidates so one bad/ad-only file can't kill the fetch."""
    def _log(msg, *a):
        if log:
            log(msg, *a)
    try:
        cands = _search_opensubtitles(title, year)
    except Exception as e:
        _log("movie-subs: opensubtitles search failed (%s)", e)
        return None
    if not cands:
        _log("movie-subs: no English subtitles found for %r", title)
        return None
    for s in cands[:4]:
        try:
            text = _download_srt(s["SubDownloadLink"], s.get("SubEncoding"))
            lines = _parse_srt(text)
            if len(lines) >= 40:               # a real movie has hundreds
                info = {"title": title, "year": year, "tmdb": tmdb,
                        "sub_name": s.get("SubFileName", ""),
                        "downloads": s.get("SubDownloadsCnt")}
                _log("movie-subs: %d lines from %r (%s downloads)",
                     len(lines), info["sub_name"][:50], info["downloads"])
                return lines, "en", info
        except Exception as e:
            _log("movie-subs: candidate failed (%s) — trying next", e)
            time.sleep(0.5)
    _log("movie-subs: all candidates failed for %r", title)
    return None


def fetch_for_url(url: str, log=None):
    """Full pipeline for a movie-site watch URL → (lines, "en", info) or None."""
    def _log(msg, *a):
        if log:
            log(msg, *a)
    try:
        html = _get(url, _UA_BROWSER).decode("utf-8", "replace")
    except Exception as e:
        _log("movie-subs: page fetch failed (%s)", e)
        return None
    title, year, tmdb = _extract_movie_identity(html, url)
    if not title:
        _log("movie-subs: no title on page")
        return None
    _log("movie-subs: page says %r (%s) tmdb=%s", title, year or "?", tmdb)
    return _fetch_by_title(title, year, tmdb, log)


def fetch_for_window_title(raw_title: str, log=None):
    """No-URL path: a browser WINDOW title like
    'Watch 3-D Sex and Zen: Extreme Ecstasy Movie… - F2movies - Brave' carries
    the movie name — clean it and run the same search (no year available)."""
    def _log(msg, *a):
        if log:
            log(msg, *a)
    title = _clean_site_title(raw_title)
    if not title or len(title) < 3:
        _log("movie-subs: window title yielded no usable movie name")
        return None
    _log("movie-subs: window title says %r", title)
    return _fetch_by_title(title, None, None, log)


if __name__ == "__main__":                      # manual test:  python movie_subs.py <url>
    import sys
    res = fetch_for_url(sys.argv[1], log=lambda m, *a: print(m % a))
    if res:
        lines, lang, info = res
        print(f"\n{len(lines)} {lang} lines — first 5:")
        for ln in lines[:5]:
            print(f"  {ln['t'][0]:8.1f}s  {ln['jp'][:70]}")
