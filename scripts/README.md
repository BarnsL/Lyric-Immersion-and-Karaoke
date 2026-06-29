# `scripts/` — standalone developer / maintenance scripts

These are **run by hand**, never imported by the app. They build the local lyric
library, fix up the cache, and generate art. Run them from the repo root, e.g.
`python scripts/preload.py`.

| Script | What it does |
|--------|--------------|
| `preload.py` | Bulk-build the local lyric library from a curated ReGLOSS / hololive / V.W.P / J-pop / K-pop / C-pop list. `--translate-all` also bakes English into every song (slow). Skips songs already cached. |
| `add_lrc.py` | Add **any** song from a local `.lrc` file (for tracks no provider has). `--title`/`--artist`, or `--folder manual` to import a whole folder. |
| `reannotate.py` | Re-generate furigana / romaji for the whole cache after a romanizer change. `--dry` previews without writing. |
| `validate.py` | Scan the cache for bad / mismatched / mojibake files. `--purge` removes them. |
| `audit_cache.py` | Deeper cache audit / report (one-off diagnostics). |
| `_batch_fetch_originals.py` | One-off batch re-fetch of original-version lyrics for cached cover entries. |
| `make_assets.py` | Generate the app's image assets. Also called by `packaging/build_msix.ps1`. |
| `make_icon.py` | Rewrite `icon.ico` (+ a `_icon_preview.png` contact sheet) from the source art. `--preview`. Imported by `make_assets.py`. |

> The app itself (the 26 modules in the repo root) is **not** here — those are
> imported at runtime and bundled by `DesktopKaraoke.spec`. See
> [`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md) for the module map.
