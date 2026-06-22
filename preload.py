"""
Preload the Desktop Karaoke lyrics library.

Fetches synced lyrics (furigana + romaji, English on priority tracks) for a
curated list of ReGLOSS / hololive / VTuber / popular-JP songs and caches
them under lyrics/. Safe to re-run — existing songs are skipped.

    python preload.py                 # build / top up the library
    python preload.py --translate-all # also bake English into every song (slow)
    python preload.py --force         # re-fetch even if already cached
    python preload.py --no-en         # skip English entirely (fastest)
"""

import sys
import time
from pathlib import Path

from fetch_lyrics import fetch_and_save, slugify, LYRICS_DIR

# (title, artist, bake_english)  — English baked only on priority tracks by
# default so bulk loads stay fast and don't trip translation rate limits.
SONGS = [
    # ── ReGLOSS (hololive DEV_IS) — group ──
    ("瞬間ハートビート", "ReGLOSS", True),
    ("フィーリングラデーション", "ReGLOSS", True),
    ("アワータイムイエロー", "ReGLOSS", True),
    ("Lucky Loud", "ReGLOSS", True),
    ("サクラミラージュ", "ReGLOSS", True),
    ("泡沫メイビー", "ReGLOSS", True),
    ("夢路らぶ", "ReGLOSS", True),
    ("シー・ユー・スーン", "ReGLOSS", True),
    ("Flashpoint", "ReGLOSS", True),
    ("シンメトリー", "ReGLOSS", True),
    ("Midsummer Citrus", "ReGLOSS", False),

    # ── FLOW GLOW (hololive DEV_IS 2nd gen) — group ──
    ("MAKE IT, BREAK IT", "FLOW GLOW", True),
    ("FG ROADSTER", "FLOW GLOW", True),
    ("LOAD", "FLOW GLOW", True),
    ("24K GOLD", "FLOW GLOW", True),
    ("good enough", "FLOW GLOW", True),
    ("stay by my side", "FLOW GLOW", True),

    # ── ReGLOSS — members (solo) ──
    ("DEAD-END", "火威青", True),
    ("Bad Dance Holic", "火威青", False),
    ("レディメイド", "音乃瀬奏", False),
    ("EGO!!ST", "一条莉々華", False),
    ("ペルソナ", "儒烏風亭らでん", False),
    ("JUMP!!", "轟はじめ", False),

    # ── hololive — Hoshimachi Suisei ──
    ("Stellar Stellar", "Hoshimachi Suisei", False),
    ("ビビデバ", "Hoshimachi Suisei", False),
    ("Bibbidiba", "Hoshimachi Suisei", False),
    ("comet", "Hoshimachi Suisei", False),
    ("GHOST", "Hoshimachi Suisei", False),
    ("NEXT COLOR PLANET", "Hoshimachi Suisei", False),
    ("みちづれ", "Hoshimachi Suisei", False),

    # ── hololive — others ──
    ("Unison", "Houshou Marine", False),
    ("I'm Your Treasure Box", "Houshou Marine", False),
    ("美少女無罪♡パイレーツ", "宝鐘マリン", False),
    ("Journey", "IRyS", False),
    ("REFLECT", "Gawr Gura", False),
    ("Excuse my Rudeness, but Could You Please RIP?", "Mori Calliope", False),
    ("Off with Their Heads", "Mori Calliope", False),
    ("いのち", "AZKi", False),
    ("DAILY DIARY", "hololive", False),
    ("Shiny Smily Story", "hololive", False),
    ("キラメキライダー", "hololive", False),
    ("Capture the Moment", "hololive", False),

    # ── VTuber the user follows ──
    ("Hello, Morning", "Kizuna AI", False),
    ("AIAIAI", "Kizuna AI", False),
    ("future base", "Kizuna AI", False),
    ("Acid Rain", "Phase Invaders WISH", False),

    # ── Popular JP / anime (great for learning, guaranteed synced) ──
    ("アイドル", "YOASOBI", False),
    ("夜に駆ける", "YOASOBI", False),
    ("群青", "YOASOBI", False),
    ("怪物", "YOASOBI", False),
    ("勇者", "YOASOBI", False),
    ("ハルジオン", "YOASOBI", False),
    ("うっせぇわ", "Ado", False),
    ("新時代", "Ado", False),
    ("阿修羅ちゃん", "Ado", False),
    ("踊", "Ado", False),
    ("唱", "Ado", False),
    ("KICK BACK", "Kenshi Yonezu", False),
    ("Lemon", "Kenshi Yonezu", False),
    ("感電", "Kenshi Yonezu", False),
    ("M八七", "Kenshi Yonezu", False),
    ("廻廻奇譚", "Eve", False),
    ("心予報", "Eve", False),
    ("ドライフラワー", "Yuuri", False),
    ("ベテルギウス", "Yuuri", False),
    ("Subtitle", "Official HIGE DANdism", False),
    ("Pretender", "Official HIGE DANdism", False),
    ("ミックスナッツ", "Official HIGE DANdism", False),
    ("怪獣の花唄", "Vaundy", False),
    ("踊り子", "Vaundy", False),
    ("紅蓮華", "LiSA", False),
    ("炎", "LiSA", False),
    ("残響散歌", "Aimer", False),
    ("白日", "King Gnu", False),
    ("SPECIALZ", "King Gnu", False),
    ("夜咄ディセイブ", "じん", False),
    ("ロキ", "みきとP", False),

    # ── Korean (romaja support) ──
    ("Ditto", "NewJeans", False),
    ("Super Shy", "NewJeans", False),
    ("Hype Boy", "NewJeans", False),
    ("How Sweet", "NewJeans", False),
    ("LALALALA", "Stray Kids", False),
    ("God's Menu", "Stray Kids", False),
    ("How You Like That", "BLACKPINK", False),
    ("뱅뱅뱅", "BIGBANG", False),
    ("Spring Day", "BTS", False),
    ("Antifragile", "LE SSERAFIM", False),

    # ── Chinese / Mandarin (pinyin support) ──
    ("月亮代表我的心", "邓丽君", False),
    ("晴天", "周杰伦", False),
    ("七里香", "周杰伦", False),
    ("告白气球", "周杰伦", False),
    ("稻香", "周杰伦", False),
    ("起风了", "买辣椒也用券", False),
    ("孤勇者", "陈奕迅", False),

    # ── Corridos / Spanish (main line + English; no romanization needed) ──
    ("Ella Baila Sola", "Eslabon Armado", True),
    ("Lady Gaga", "Peso Pluma", True),
    ("La Bebe - Remix", "Yng Lvcas", True),
    ("PRC", "Peso Pluma", True),
    ("AMG", "Natanael Cano", True),
    ("Amor Tumbado", "Natanael Cano", True),
    ("El Azul", "Junior H", True),
    ("Bebe Dame", "Fuerza Regida", True),
    ("El Belicón", "Peso Pluma", True),
    ("Contrabando y Traición", "Los Tigres del Norte", True),
    ("Jefe de Jefes", "Los Tigres del Norte", True),
    ("La Puerta Negra", "Los Tigres del Norte", True),
    ("Nieves de Enero", "Chalino Sánchez", True),
    ("El Rey", "Vicente Fernández", True),

    # ── V.W.P (Virtual Witch Phenomenon / KAMITSUBAKI) — numbered MV set ──
    ("電脳", "V.W.P", False), ("輪廻", "V.W.P", False), ("変身", "V.W.P", False),
    ("言霊", "V.W.P", False), ("共鳴", "V.W.P", False), ("再会", "V.W.P", False),
    ("魔女", "V.W.P", False), ("定命", "V.W.P", False), ("玩具", "V.W.P", False),
    ("飛翔", "V.W.P", False), ("祭壇", "V.W.P", False), ("秘密", "V.W.P", False),
    ("感情", "V.W.P", False), ("切札", "V.W.P", False), ("同盟", "V.W.P", False),
    ("花束", "V.W.P", False), ("未遂", "V.W.P", False), ("強気", "V.W.P", False),
    ("暁光", "V.W.P", False), ("真偽", "V.W.P", False), ("神話", "V.W.P", False),
    ("愛詩", "V.W.P", False), ("追憶", "V.W.P", False), ("歌姫", "V.W.P", False),
    ("欲望", "V.W.P", False), ("照射", "V.W.P", False), ("幻界", "V.W.P", False),

    # ── Bakemonogatari / Monogatari series (OP + iconic ED) ──
    ("staple stable", "戦場ヶ原ひたぎ", False),
    ("帰り道", "八九寺真宵", False),
    ("ambivalent world", "神原駿河", False),
    ("恋愛サーキュレーション", "千石撫子", False),
    ("sugar sweet nightmare", "羽川翼", False),
    ("白金ディスコ", "阿良々木月火", False),
    ("君の知らない物語", "supercell", False),

    # ── kz (livetune) ──
    ("Tell Your World", "kz(livetune)", False),
    ("Packaged", "livetune", False),
    ("Last Night, Good Night", "livetune", False),
    ("ファインダー", "livetune", False),
    ("Redial", "livetune", False),
    ("Decorator", "livetune", False),

    # ── DECO*27 ──
    ("ヴァンパイア", "DECO*27", False),
    ("ゴーストルール", "DECO*27", False),
    ("アンドロイドガール", "DECO*27", False),
    ("乙女解剖", "DECO*27", False),
    ("モザイクロール", "DECO*27", False),
    ("妄想税", "DECO*27", False),
    ("ヒバナ", "DECO*27", False),
    ("愛言葉Ⅲ", "DECO*27", False),
    ("シンデレラ", "DECO*27", False),

    # ── Hoshimachi Suisei (星街すいせい) ──
    ("駆けろ", "Hoshimachi Suisei", False),
    ("自己紹介", "Hoshimachi Suisei", False),
    ("Bluerose", "Hoshimachi Suisei", False),
    ("Andromeda", "Hoshimachi Suisei", False),
    ("グラビティ", "Hoshimachi Suisei", False),
    ("夜永唄", "Hoshimachi Suisei", False),
    ("ノクターン", "Hoshimachi Suisei", False),
    ("ミチヲユケ", "Hoshimachi Suisei", False),
    ("七つの海よりキミの海", "Hoshimachi Suisei", False),
    ("新星マーチ", "Hoshimachi Suisei", False),
    ("王権神授説", "Hoshimachi Suisei", False),
    ("天球、彗星は夜を跨いで", "Hoshimachi Suisei", False),
    ("Awake", "Hoshimachi Suisei", False),
    ("TELL ME", "Hoshimachi Suisei", False),

    # ── Classic anime OPs / EDs ──
    ("残酷な天使のテーゼ", "高橋洋子", False),
    ("魂のルフラン", "高橋洋子", False),
    ("創聖のアクエリオン", "AKINO", False),
    ("God knows...", "平野綾", False),
    ("ハレ晴レユカイ", "平野綾", False),
    ("もってけ!セーラーふく", "泉こなた", False),
    ("only my railgun", "fripSide", False),
    ("そばかす", "JUDY AND MARY", False),
    ("secret base ～君がくれたもの～", "ZONE", False),
    ("again", "YUI", False),
    ("ライオン", "May'n", False),
    ("READY STEADY GO", "L'Arc-en-Ciel", False),
    ("メリッサ", "Porno Graffitti", False),
    ("1/3の純情な感情", "SIAM SHADE", False),
    ("Don't say \"lazy\"", "桜高軽音部", False),
    ("プラチナ", "坂本真綾", False),
    ("鳥の詩", "Lia", False),
    ("unravel", "TK from 凛として時雨", False),
    ("紅蓮の弓矢", "Linked Horizon", False),
]


def main():
    args = sys.argv[1:]
    force = "--force" in args
    no_en = "--no-en" in args
    translate_all = "--translate-all" in args

    LYRICS_DIR.mkdir(exist_ok=True)
    ok = miss = skip = 0
    total = len(SONGS)

    for i, (title, artist, prio) in enumerate(SONGS, 1):
        tag = f"[{i:>2}/{total}]"
        out = LYRICS_DIR / f"{slugify(title)}.json"
        if out.exists() and not force:
            print(f"{tag} skip {title} — {artist}")
            skip += 1
            continue

        translate = not no_en and (translate_all or prio)
        try:
            p = fetch_and_save(title, artist, translate=translate)
            if p:
                ok += 1
                en = " +en" if translate else ""
                print(f"{tag} OK   {title} — {artist}{en}")
            else:
                miss += 1
                print(f"{tag} MISS {title} — {artist}")
        except Exception as e:
            miss += 1
            print(f"{tag} ERR  {title} — {artist}: {e}")
        time.sleep(0.4)

    have = len(list(LYRICS_DIR.glob("*.json")))
    print(f"\nDone — {ok} fetched, {skip} already cached, {miss} missed.")
    print(f"Library now holds {have} songs.")


if __name__ == "__main__":
    main()
