"""
Katakana → English loanword data (gairaigo / 外来語).

Japanese lyrics are full of English written in katakana, often run together with
no spaces — e.g. ベイビーアイラブユー. A phonetic romanizer turns that into
"beibiiairabuyuu", which is useless to a learner. This table lets the romanizer:

  1. **segment** a run-together katakana string into the loanwords it contains
     (`ベイビーアイラブユー` → `ベイビー アイ ラブ ユー`), and
  2. **render** each part as the English it actually represents
     (`baby I love you`) — overriding the analyzer where it guesses wrong
     (cutlet alone gives アイ→"eye", ミー→"Mi-", グッバイ→"Gubbai").

It is deliberately a plain dict so it's trivial to extend — just add
`"カタカナ": "english"` pairs. Keep entries UNAMBIGUOUS where possible; for words
with two common readings the lyric-frequent sense is chosen (e.g. アイ→"I").

Used by `fetch_lyrics.romanize()` / `_segment_katakana()`. Adding data here
improves every future fetch and is picked up by `reannotate.py` for the cache.
"""

# Pronouns / people
_PRONOUNS = {
    "アイ": "I", "ミー": "me", "マイ": "my", "ユー": "you", "ユア": "your",
    "ウィー": "we", "アス": "us", "アワー": "our", "ヒー": "he", "シー": "she",
    "ゼイ": "they", "イット": "it", "ベイビー": "baby", "ベビー": "baby",
    "ハニー": "honey", "ダーリン": "darling", "ガール": "girl", "ボーイ": "boy",
    "フレンド": "friend", "マン": "man", "ピープル": "people",
}

# Love / feeling
_LOVE = {
    "ラブ": "love", "ラヴ": "love", "キス": "kiss", "ハグ": "hug",
    "ハート": "heart", "ドリーム": "dream", "ホープ": "hope", "ソウル": "soul",
    "スマイル": "smile", "クライ": "cry", "ティアーズ": "tears", "ペイン": "pain",
    "ジョイ": "joy", "ハッピー": "happy", "ハピネス": "happiness", "サッド": "sad",
    "ロンリー": "lonely", "アローン": "alone", "クレイジー": "crazy",
    "フィーリング": "feeling", "ミラクル": "miracle", "マジック": "magic",
}

# Time / nature / things
_THINGS = {
    "タイム": "time", "ナイト": "night", "デイ": "day", "モーニング": "morning",
    "トゥナイト": "tonight", "トゥデイ": "today", "ライフ": "life", "ワールド": "world",
    "スカイ": "sky", "スター": "star", "ムーン": "moon", "サン": "sun",
    "レイン": "rain", "スノー": "snow", "ウィンド": "wind", "ファイア": "fire",
    "ファイヤー": "fire", "ライト": "light", "ダーク": "dark", "シャドウ": "shadow",
    "カラー": "color", "レインボー": "rainbow", "オーシャン": "ocean", "シー": "sea",
    "ロード": "road", "ウェイ": "way", "ドア": "door", "ウィンドウ": "window",
    "ソング": "song", "ミュージック": "music", "メロディー": "melody",
    "ダンス": "dance", "ステージ": "stage", "ストーリー": "story", "ゲーム": "game",
}

# Verbs / actions / adjectives
_ACTIONS = {
    "ゴー": "go", "ストップ": "stop", "ラン": "run", "ジャンプ": "jump",
    "フライ": "fly", "コール": "call", "ホールド": "hold", "キャッチ": "catch",
    "ショー": "show", "プレイ": "play", "シング": "sing", "スマイリング": "smiling",
    "ビューティフル": "beautiful", "ワンダフル": "wonderful", "パーフェクト": "perfect",
    "スペシャル": "special", "フリー": "free", "トゥルー": "true", "リアル": "real",
    "フォーエバー": "forever", "トゥギャザー": "together", "アゲイン": "again",
    "オールウェイズ": "always", "ネバー": "never", "オンリー": "only",
    "エブリ": "every", "エブリデイ": "everyday", "エブリバディ": "everybody",
}

# Greetings / function words / numbers
_FUNCTION = {
    "ハロー": "hello", "ハーイ": "hi", "ハイ": "hi", "バイ": "bye",
    "バイバイ": "bye bye", "グッバイ": "goodbye", "プリーズ": "please",
    "サンキュー": "thank you", "ソーリー": "sorry", "オーケー": "okay",
    "オッケー": "okay", "イエス": "yes", "ノー": "no", "ドント": "don't",
    "キャント": "can't", "ウォント": "want", "ニード": "need", "レッツ": "let's",
    "カモン": "come on", "ヘイ": "hey", "オー": "oh", "イェー": "yeah",
    "ワン": "one", "ツー": "two", "スリー": "three", "フォー": "four",
    "ファイブ": "five", "アンド": "and", "オア": "or", "バット": "but",
    "ウィズ": "with", "フォー": "for", "オン": "on", "イン": "in", "アップ": "up",
    "ダウン": "down", "ナウ": "now", "ヒア": "here", "ゼア": "there",
}

# Merge into one lookup table.
KATAKANA_EN: dict[str, str] = {}
for _part in (_PRONOUNS, _LOVE, _THINGS, _ACTIONS, _FUNCTION):
    KATAKANA_EN.update(_part)
