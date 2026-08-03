# Lyric Translation & Annotation

Code: `fetch_lyrics.py` (`annotate`, romanizers, `_song_lang`), `gairaigo.py`.

Once the right lyrics are sourced, each line is annotated for a non-native listener.
This subsystem is **language-routed**, not confidence-scored per line — the
confidence question here is *"which language is this?"*, answered once per song.

## Language routing (`_song_lang`)
Scans **all** lines (not just the first 40) so a song that opens kanji-only or
instrumental still classifies right. Decisive rule: **any kana anywhere ⇒ Japanese**
(Chinese never uses kana), so a kanji-heavy J-pop/VTuber song is never mistaken for
Chinese (which would wrongly give pinyin instead of furigana).

| Language | Annotation | Engine |
|---|---|---|
| Japanese | furigana + Hepburn romaji | fugashi + cutlet, with literary-reading fixes and katakana-English recovery (`gairaigo.py`) |
| Chinese | pinyin | per-character |
| Korean | romaja | per-syllable |
| Cyrillic | transliteration | table |
| any | English translation | deep-translator (DeepL when `DEEPL_API_KEY` set, else free Google) |

## Accuracy driver
Furigana quality hinges on the **morphological segmentation** (今生きて → 今 / 生きて,
not 今生 / きて). The katakana-English recovery turns ラブユー back into "love you" so
borrowed English reads naturally. A romanized-Japanese source (`ja-romaji`) can't be
furigana'd but is still translated, and is flagged stale for a kanji upgrade-fetch.

**Confidence tie-in:** the same kana/hangul/CJK script detection feeds the
[wrong-song language guards](../wrong-song-rejection/) — getting the language right
both annotates correctly AND rejects wrong-language matches.
