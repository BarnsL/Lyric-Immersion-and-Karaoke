"""Verify the dev console's TypeScript models match the app's real API.

WHY THIS EXISTS
---------------
`api.ts` casts responses straight to the model type:

    return (await resp.json()) as T;

A cast is not a check. Every field the console reads is `foo?: string`, so a name
that does not exist on the wire reads `undefined` and renders as an em dash or a
fallback string. It compiles, it typechecks, it runs, and it is wrong.

That is exactly what happened (TICKET-197): `StatusPayload` declared `title`,
`artist`, `offset`, `now_line`, `source`, `subs_mode`, `live_arrangement` and
`mv_mode`. The API sends `player_title`, `player_artist`, `sync_offset` and
`current_line`, and never sent the rest at all. The Overview's now-playing card
therefore showed "Idle / No SMTC session detected" with a track playing — for
every build since it was written.

This compares the field names declared in dev-console/src/models.ts against a
LIVE response from the running app and fails on anything the console expects but
the API does not send.

    python scripts/check_console_contract.py [--base http://127.0.0.1:8765]

Exit 0 = the console's expectations are all satisfiable, 1 = drift, 2 = app not
reachable (start it, or pass --base).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "dev-console/src/models.ts"

# interface name in models.ts -> endpoint that fills it.
# Only the payloads whose fields are read positionally by the UI; DiagPayload is
# deliberately excluded (it carries an index signature and is rendered generically).
CHECKED = {
    "StatusPayload": "/status",
    "Health": "/health",
    "NowBlock": "/insight:now",
    # The nested DiagPayload.sync object. Excluding it was a real gap: the console
    # rendered "live" and "body corroborated" rows from `live_mode` and
    # `body_corroborated`, and get_diag()'s sync block sent neither — two more
    # permanent dashes nobody could see were bugs (TICKET-197).
    "DiagSync": "/diag:sync",
}


def declared_fields(src: str, iface: str) -> list[str]:
    """Field names at the TOP level of `interface <iface> { ... }`."""
    m = re.search(r"export interface %s\s*\{" % re.escape(iface), src)
    if not m:
        return []
    i, depth, body = m.end(), 1, []
    while i < len(src) and depth:
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if not depth:
                break
        if depth == 1:
            body.append(c)
        i += 1
    text = "".join(body)
    text = re.sub(r"//[^\n]*", "", text)                 # strip line comments
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)    # strip block comments
    # `name?: type;` / `name: type;` at this nesting level only
    return re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\??\s*:", text, flags=re.M)


def fetch(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=6) as r:
        return json.loads(r.read().decode("utf-8"))


# ── CSS section ──────────────────────────────────────────────────────────────
# The other silent failure mode. `.knob-row` is scoped to TABLE CELLS
# (`.knob-row td`) for the Parameters view; three components used it on a <div>,
# which matches nothing. No error, no warning — the rows just rendered as run-on
# text ("Matched againstthe whole library"). TICKET-194. A class that does not
# exist looks exactly like a class that does, until you see it on screen.

CSS = ROOT / "dev-console/src/styles.css"
TSX = ROOT / "dev-console/src"


def _class_exprs(src: str):
    """The raw expression text after each `className=`."""
    for m in re.finditer(r"className=", src):
        i = m.end()
        if i < len(src) and src[i] == '"':
            j = src.index('"', i + 1)
            yield src[i + 1:j]
        elif i < len(src) and src[i] == "{":
            depth, j = 1, i + 1
            while j < len(src) and depth:
                depth += {"{": 1, "}": -1}.get(src[j], 0)
                j += 1
            yield src[i + 1:j - 1]


def check_css() -> bool:
    if not CSS.is_file() or not TSX.is_dir():
        print("[skip] no stylesheet/components to check")
        return True
    css_src = CSS.read_text(encoding="utf-8")
    defined = set(re.findall(r"\.(-?[A-Za-z_][\w-]*)", css_src))

    # A class can be "present" in the stylesheet and still style nothing on the
    # element that carries it. `.knob-row td { … }` mentions `knob-row`, but only
    # styles its <td> DESCENDANTS — so `<div className="knob-row">` gets nothing.
    # A naive "is the name in the file" check therefore MISSES the exact bug that
    # motivated this. `self_styled` holds classes that style the element itself:
    # for each rule, split the selector on combinators and keep the classes in the
    # LAST compound, since that is the element the rule actually targets.
    self_styled: set[str] = set()
    for sel_blob in re.findall(r"([^{}]+)\{", re.sub(r"/\*.*?\*/", "", css_src, flags=re.S)):
        for sel in sel_blob.split(","):
            sel = sel.strip()
            if not sel or sel.startswith("@"):
                continue
            last = re.split(r"[\s>+~]+", sel)[-1]
            self_styled.update(re.findall(r"\.(-?[A-Za-z_][\w-]*)", last))

    used: dict[str, set[str]] = {}
    for f in sorted(TSX.rglob("*.tsx")):
        src = f.read_text(encoding="utf-8")
        for expr in _class_exprs(src):
            lits = re.findall(r'"([^"]*)"|\'([^\']*)\'|`([^`]*)`', expr) or [(expr, "", "")]
            for a, b, c in lits:
                # Drop `${...}` interpolations; what is left is literal class text.
                text = re.sub(r"\$\{[^}]*\}", " ", a or b or c)
                for tok in text.split():
                    if re.fullmatch(r"-?[A-Za-z_][\w-]*", tok):
                        used.setdefault(tok, set()).add(f.name)

    missing, descendant_only = {}, {}
    for k, v in used.items():
        if k in defined:
            if k not in self_styled:
                descendant_only[k] = v
            continue
        # `v-${verdict}` leaves the stub `v-`; it is satisfied if ANY defined
        # class starts with it (v-pass, v-fail, …). Only treat a trailing-dash
        # stub this way — a real missing class has no such excuse.
        if k.endswith("-") and any(d.startswith(k) for d in defined):
            continue
        missing[k] = v

    print(f"  styles.css     {len(defined)} classes defined, "
          f"{len(used)} used across {len(list(TSX.rglob('*.tsx')))} components")
    bad = False
    if missing:
        bad = True
        print("[FAIL] classes used by a component but absent from styles.css —")
        print("       these render with NO styling and no error:")
        for k, v in sorted(missing.items()):
            print(f"         .{k:20} <- {', '.join(sorted(v))}")
    if descendant_only:
        # WARNING, not a failure. Using a class purely as a scoping hook is a
        # legitimate pattern: Parameters.tsx puts `knob-row` on a <tr> and
        # `.knob-row td {…}` styles its cells, exactly as intended. What broke in
        # TICKET-194 was the same class on a <div> whose children were <span>s —
        # so the rule never applied and the row rendered as run-on text.
        # Distinguishing those needs the element type and its children, which is
        # more JSX parsing than this is worth; flagging the class for a human
        # glance catches the bug without inventing confident false failures.
        print("[warn] classes that only style DESCENDANTS (e.g. `.foo td {…}`).")
        print("       Fine as a scoping hook — but confirm the element you put it")
        print("       on actually has those children, or it styles nothing:")
        for k, v in sorted(descendant_only.items()):
            print(f"         .{k:20} <- {', '.join(sorted(v))}")
    return not bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    a = ap.parse_args(argv)

    if not MODELS.is_file():
        print(f"[FAIL] {MODELS} not found")
        return 1
    src = MODELS.read_text(encoding="utf-8")

    # CSS first: it needs no running app, so it still reports something useful
    # when the engine is down.
    ok = check_css()

    for iface, spec in CHECKED.items():
        path, _, sub = spec.partition(":")
        try:
            payload = fetch(a.base, path)
        except urllib.error.URLError as e:
            print(f"[skip] {path} unreachable ({e.reason}) — is the app running "
                  f"with Local API enabled?")
            # The CSS half already ran; report its verdict rather than throwing
            # the result away just because the engine is down.
            return 2 if ok else 1
        if sub:
            payload = payload.get(sub)
            if payload is None:
                print(f"[FAIL] {path} has no '{sub}' block — the app is older than "
                      f"the console expects (needs v1.1.87+).")
                ok = False
                continue

        want = declared_fields(src, iface)
        if not want:
            print(f"[FAIL] interface {iface} not found in models.ts")
            ok = False
            continue
        have = set(payload)
        missing = [f for f in want if f not in have]

        print(f"  {iface:14} <- {spec:16} {len(want)} declared, "
              f"{len(want) - len(missing)} present")
        if missing:
            ok = False
            print(f"[FAIL] the console reads fields {path} does not send: {missing}")
            print(f"       These read `undefined` at runtime and render as a dash or")
            print(f"       a fallback string — the UI looks broken, nothing throws.")
            print(f"       Fix the names in models.ts against api.py.")
            extra = sorted(have - set(want))
            if extra:
                print(f"       (available but unused: {extra[:12]})")
    print()
    print("[ok] console models match the live API." if ok else
          "[FAIL] console/API contract drift — see above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
