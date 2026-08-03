"""Exercise main.clean_title() WITHOUT importing main (no GUI, no side effects).

Loads only the module-level `def`s and assignments from main.py via `ast`, so the
title-reduction pipeline can be run against real player titles in a fraction of a
second — no Tk, no rebuild, no running app.

WHY (TICKET-200): `clean_title()` decides what string the lyric providers are
searched for, and when it gets that wrong the failure is *invisible* downstream —
a real, correctly-timed body comes back for the wrong song, and every check after
that point passes. `IA & ONE / てるみい (石風呂)【MUSIC VIDEO】` reduced to the
performers, `IA & ONE`, and fetched another song by those performers.

The regression cases below are not decoration: the slash tie-break is tuned
against several title conventions that pull in opposite directions (`Song/Artist`
vs `Artist/Song`), so any change here needs all of them re-run.

    python scripts/probe_clean_title.py     # exits non-zero on any miss
"""
import ast, re, sys, unicodedata

# These cases are Japanese by nature, so the probe prints CJK. On Windows the
# default console codepage is cp1252 and `print` raises UnicodeEncodeError
# before a single assertion runs — the probe "fails" for a reason that has
# nothing to do with the code under test. Force UTF-8 on our own streams so the
# result does not depend on which shell happened to launch it.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SRC = open(r"D:\Desktop-Karaoke\main.py", encoding="utf-8-sig").read()
tree = ast.parse(SRC)

keep = []
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        keep.append(node)
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
        keep.append(node)   # anything that fails to exec is skipped below
    elif isinstance(node, ast.Import) and all(
            a.name in ("re", "os", "sys", "json", "time", "math", "unicodedata")
            for a in node.names):
        keep.append(node)

# Exec node-by-node: one unrelated module-level assignment that needs the app's
# data dir must not take the whole title pipeline down with it.
G = {"__name__": "mainsubset"}
skipped = 0
for node in keep:
    mod = ast.Module(body=[node], type_ignores=[])
    try:
        exec(compile(ast.fix_missing_locations(mod), "<main-subset>", "exec"), G)
    except Exception:
        skipped += 1
print(f"[loaded {len(keep) - skipped}/{len(keep)} module-level nodes]\n")

clean_title = G["clean_title"]

CASES = [
    # (player title, SMTC artist, what the song ACTUALLY is)
    # --- TICKET-200: the reported failure -------------------------------------
    ("IA & ONE / てるみい (石風呂)【MUSIC VIDEO】", "IA PROJECT", "てるみい"),
    # same shape, CJK performers, and a 3-way credit
    ("初音ミク×GUMI / 曲名【MV】", "初音ミク", "曲名"),
    ("IA & ONE & GUMI / 曲名【MUSIC VIDEO】", "IA PROJECT", "曲名"),
    # --- must NOT regress: the cases the tie-break was tuned for --------------
    ("Dunk/轟はじめ【MV】", "轟はじめ", "Dunk"),          # Song/Artist
    ("FLOW GLOW / LOAD【MV】", "FLOW GLOW", "LOAD"),      # Group/Song
    ("幻界/V.W.P #30【MV】", "V.W.P", "幻界"),            # neither → first
    # a genuine '&' SONG title must survive: no element is an artist token
    ("Sugar & Spice / Reol【MV】", "Reol", "Sugar & Spice"),
    ("Reol / Sugar & Spice【MV】", "Reol", "Sugar & Spice"),
    # unrelated channel name → no element matches → falls back to first (known
    # residual limitation, documented in TICKET-200)
    ("IA & ONE / てるみい (石風呂)【MUSIC VIDEO】", "Some Label", "IA & ONE"),
    # --- unrelated shapes still fine ------------------------------------------
    ("【MV】Unchained【hololive English -Advent- Original Song】",
     "hololive English", "Unchained"),

    # --- TICKET-206: the tie-in bracket beat the song bracket ------------------
    # The reported failure. The song is in 『』 at the head; the ANIME is in 「」
    # inside a trailing credit parenthetical. The old cascade searched 「」 first,
    # unconditionally and anywhere in the string, so bracket TYPE outranked
    # position and the app fetched lyrics for the anime's name.
    ("EGOIST 『名前のない怪物』 Music Video (TVアニメ「PSYCHO-PASS サイコパス」ep1-11 エンディングテーマ)",
     "EGOIST", "名前のない怪物"),
    ("EGOIST 『Namae No Nai Kaibutsu』 Music Video (TV anime 「PSYCHO-PASS」 ep1-11 Ending theme)",
     "EGOIST", "Namae No Nai Kaibutsu"),
    # same-bracket-type variant: both in 『』, tie-in one inside the parenthetical
    ("EGOIST 『Namae No Nai Kaibutsu』 Music Video (TV anime 『PSYCHO-PASS』 ep1-11 Ending theme)",
     "EGOIST", "Namae No Nai Kaibutsu"),
    # LEADING tie-in: the old code matched the first 『』, saw the tag, and gave
    # up entirely instead of looking at the next bracket.
    ("TVアニメ『PSYCHO-PASS』ED EGOIST 『名前のない怪物』", "EGOIST", "名前のない怪物"),
    # --- TICKET-206 must NOT regress: the song legitimately in 「」 ------------
    ("ヨルシカ 「花に亡霊」 Music Video", "ヨルシカ", "花に亡霊"),
    ("YOASOBI「アイドル」 Official Music Video", "YOASOBI", "アイドル"),
    # the ORIGINAL convention the old code assumed: song 「」, work 『』+tag
    ("「群青」/ YOASOBI 『アニメ』OPテーマ", "YOASOBI", "群青"),
]

fails = 0
for title, artist, want in CASES:
    got = clean_title(title, "", artist).strip()
    ok = got == want
    fails += not ok
    print(f"{'ok  ' if ok else 'MISS'} artist={artist!r}\n     {title!r}\n  -> {got!r}   (want {want!r})")
print(f"\n{len(CASES) - fails}/{len(CASES)} pass")
sys.exit(1 if fails else 0)
