# Lyric Sourcing

Providers (synced LRC) → **verification** (artist, duration, language) → annotation
(furigana/romaji/pinyin/romaja + translation). Fallbacks: the video's own captions,
banner OCR, and **generation by ear** ([[faster-whisper]]).

Never commits lyrics — see [[Hard Rules]]. Wrong picks are caught by the
[[Decision Engine]].
