"""Re-annotate cached Japanese lyrics with the current romanizer.

Older cache files were furigana/romaji-annotated by pykakasi, whose naive
longest-match segmentation produced errors such as 今生きて → 今生(こんじょう)
"konjou" instead of 今(いま)生き… "ima ikite". This rebuilds the furigana
(``jp``) and romaji (``rm``) for every ``lang == "ja"`` file from its base
text, using the fugashi + cutlet engine in ``fetch_lyrics``. Timestamps and the
English translation are left untouched.

    python reannotate.py            # rewrite all Japanese files in lyrics/
    python reannotate.py --dry      # report what would change, write nothing

Safe to re-run: it only rewrites ``jp``/``rm`` and skips files already produced
by the current engine when nothing changes.
"""
from __future__ import annotations

import json
import re
import sys

from fetch_lyrics import LYRICS_DIR, to_furigana, romanize, _jp_engine

# Strip an existing furigana reading "(かな)" to recover the base (kanji) text.
_READING = re.compile(r"[(（][ぁ-ゟ゛゜ー]+[)）]")
# Some old cache files left stray LRC timestamp tags inside the text itself.
_TS = re.compile(r"\[\d+:\d+(?:\.\d+)?\]|<\d+:\d+(?:\.\d+)?>")


def base_text(jp: str) -> str:
    return _TS.sub("", _READING.sub("", jp)).strip()


def reannotate_file(path, dry=False) -> bool:
    """Rewrite jp/rm per LINE by the line's own script (not the song's overall
    language), so Japanese lines inside a mixed / mis-detected song also get
    furigana + romaji instead of staying bare kanji. Returns True if changed."""
    from fetch_lyrics import detect_lang
    data = json.loads(path.read_text("utf-8"))
    lang = data.get("meta", {}).get("lang")
    changed = False
    for ln in data.get("lines", []):
        jp = ln.get("jp", "")
        base = base_text(jp)
        if not base.strip():
            continue
        ll = detect_lang(base)
        new_jp, new_rm = jp, ln.get("rm", "")
        if ll == "ja" or (ll == "zh" and lang != "zh"):     # read kanji-only as JP
            new_jp, new_rm = to_furigana(base), romanize(base, "ja")
        elif ll in ("zh", "ko"):
            new_jp, new_rm = base, romanize(base, ll)
        if new_jp != jp or new_rm != ln.get("rm", ""):
            changed = True
            if not dry:
                ln["jp"], ln["rm"] = new_jp, new_rm
    if changed and not dry:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(path)
    return changed


def main():
    dry = "--dry" in sys.argv
    if not _jp_engine():
        print("fugashi/cutlet not available — nothing to do (would fall back to "
              "the same pykakasi output).")
        return
    files = sorted(LYRICS_DIR.glob("*.json"))
    seen = changed = 0
    for p in files:
        try:
            data = json.loads(p.read_text("utf-8"))
        except Exception:
            continue
        # process any file that contains Japanese/CJK text, whatever its lang tag
        if not any(re.search(r"[぀-ヿ一-鿿]", ln.get("jp", ""))
                   for ln in data.get("lines", [])):
            continue
        seen += 1
        if reannotate_file(p, dry):
            changed += 1
            print(("would update " if dry else "updated ") + p.name)
    verb = "would change" if dry else "changed"
    print(f"\n{seen} files with CJK, {verb} {changed}.")


if __name__ == "__main__":
    main()
