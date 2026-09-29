# `scripts/` — standalone developer / maintenance scripts

These are **never imported by the app** (no module in the repo root imports from
`scripts/`). Most are run by hand from the repo root, e.g. `python
scripts/preload.py`. Two exceptions to "by hand": CI runs some of these
automatically, and `build.bat` runs the build guards. Both are marked below.

## Library / cache maintenance

| Script | What it does |
|--------|--------------|
| `preload.py` | Bulk-build the local lyric library from a curated ReGLOSS / hololive / V.W.P / J-pop / K-pop / C-pop list. `--translate-all` also bakes English into every song (slow). Skips songs already cached. |
| `add_lrc.py` | Add **any** song from a local `.lrc` file (for tracks no provider has). `--title`/`--artist`, or `--folder manual` to import a whole folder. |
| `reannotate.py` | Re-generate furigana / romaji for the whole cache after a romanizer change. `--dry` previews without writing. |
| `validate.py` | Scan the cache for bad / mismatched / mojibake files. `--purge` removes them. |
| `audit_cache.py` | Deeper cache audit / report (one-off diagnostics). |
| `_batch_fetch_originals.py` | One-off batch re-fetch of original-version lyrics for cached cover entries. |

## Build, deploy and release guards

| Script | What it does |
|--------|--------------|
| `check_build_deps.py` | Pre-build consistency guard for the vendored faster-whisper stack in `.deps` (TICKET-175). Fails the build on unambiguous corruption only: a foreign CPython ABI tag, or duplicate `dist-info` dirs. A plain version difference is a warning, because `dist-info` metadata can lag the real module files. Run from `build.bat`. |
| `check_av_dlls.py` | The direct detector for the TICKET-176 whisper breakage. Parses the PE import table of every `av/*.pyd` with stdlib `struct` and asserts every FFmpeg DLL it imports is actually present in `av.libs`. The mangled DLL names embed a per-build hash, so a mismatched pair means the frozen `import av` will die. Run pre- and post-build from `build.bat`. |
| `check_devconsole.py` | Verify the dev-console exe actually embeds its frontend. `cargo build --release` exits 0 but bakes in `devUrl` instead of `frontendDist`, producing a console that shows a connection-refused page (shipped once, v1.1.86). Checks for the hashed `index-*.js` asset names and the absence of the dev URL. |
| `deploy_local.py` | Deploy `dist\DesktopKaraoke` over the live install at `D:\DesktopKaraoke`. Mirrors only `_internal\` so a top-level `robocopy /MIR` cannot delete the user data the target holds (`lyrics\`, `models\`, `deps\`, `settings.json`, `metrics.json`, logs), refreshes the dev-console exe so a stale copy cannot be trusted (TICKET-198), stops running processes first, and reads robocopy's exit code as the bitmask it is. `--dry-run` available. |
| `make_assets.py` | Generate the app's image assets. Also called by `packaging/build_msix.ps1`. |
| `make_icon.py` | Rewrite `icon.ico` (+ a `_icon_preview.png` contact sheet) from the source art. `--preview`. Imported by `make_assets.py`. |

## Contract and regression probes

Most of these parse `main.py` with `ast` and exec just the function under test,
so `main.py` is never imported (importing it builds a Tk app). They exit non-zero
on failure, so they can gate a build.

| Script | What it does |
|--------|--------------|
| `ci_import_gate.py` | Every listed module must import without raising on every OS (PORTING.md Phase 0). Windows-only behavior may degrade behind its guards, but the import itself must always succeed, which is what keeps the Linux/macOS ports startable. **Run by CI** on all three runners (`.github/workflows/ci.yml`, under `xvfb-run` on Linux because pystray's X backend connects to `$DISPLAY` at import). |
| `test_mpris_provider.py` | Live test for `media_mpris.MprisWatcher`, Linux only, real D-Bus. Exports a mock MPRIS player and asserts the snapshot contract: CJK title, artist, status mapped to the app-level constant, position and duration in seconds. **Run by CI** on ubuntu-latest under `dbus-run-session`, which keeps it off any real desktop session. |
| `check_console_contract.py` | Compare the field names in `dev-console/src/models.ts` against a live response from the running app. `api.ts` casts responses to the model type, and a cast is not a check, so a wrong field name silently renders as a fallback. That shipped (TICKET-197): the console read `title`/`artist`/`offset` while the API sends `player_title`/`player_artist`/`sync_offset`, so the now-playing card showed "Idle" with a track playing. `--base` overrides the API URL. |
| `probe_tune_docs.py` | **Run by CI** on Linux. Enforce a 1:1 mapping between `Overlay._tune` and `tune_docs.TUNE_DOC` (TICKET-212). A knob with no doc renders as a bare key and a number; a doc with no knob is a lie nothing contradicts. Also checks the tooltip properties: ASCII only, and long enough to be a description rather than a restated key name. |
| `probe_sync_narration.py` | Prove every sync correction narrates its real outcome (TICKET-205). `_note_event` used to be called from the sync callers and only 3 of 18 `_smooth_offset` call sites did it, so most corrections moved the lyrics and told the console nothing. Checks behavior against a stub, and coverage: every `reason=` string has a `_SYNC_CAUSE` entry and every entry is still reachable. |
| `probe_clean_title.py` | Exercise `main.clean_title()` against real player titles. When it reduces a title wrongly the failure is invisible downstream: a correctly-timed lyric body comes back for the wrong song and every later check passes (TICKET-200). The slash tie-break is tuned against title conventions that pull in opposite directions, so the regression cases must all be re-run after any change. |
| `probe_concert.py` | Exercise `main.App.get_concert` (the `/concert` accessor and the Concerts panel) and the concert-verdict explainer against deliberately hostile stubs: missing attributes, legacy tuple-shaped chapters, a plan entry with a null onset. For a diagnostics accessor the failure that matters is "raised and took `/concert` down", so every case must survive `json.dumps`. Spec 001 made it repo-relative (it hard-coded a `D:\` path) and removed a fake `_s` helper it injected, which had hidden a real `NameError` in `get_concert`; `KARAOKE_MAIN=<path>` runs it against another checkout. **Run by CI** on Linux. |
| `probe_insight.py` | Exercise `main.App.get_insight` against a duck-typed stub. Verifies the TICKET-194 `now` block is present, JSON-serialisable and correctly shaped. |

## Offline experiments and benchmarks

| Script | What it does |
|--------|--------------|
| `autoresearch.py` | Score knob configurations against a known playlist, one arm at a time, using `research/playlist.json` and `research/arms.json`. Offline and agent-driven while nobody is watching, which is what makes perturbation free and gives real ground truth. Songs play in real time, so budget roughly (tracks x dwell) per arm and prefer few arms with a large expected effect. |
| `belt_sim.py` | Offline belt-motion simulation. Ports `_eased_offset` plus the belt delta so the per-frame velocity spikes ("lurch") can be measured under a realistic schedule of sync corrections and compared across configs. |
| `eval_concert_mc.py` | Reproducible evaluation of concert MC (talk) handling, spec 001. Builds labelled mini-concerts from permissively-licensed recordings (LibriSpeech CC BY 4.0 talk; CC BY / PD music from librosa's example data; with `--songs`, the 22 non-NC JamendoLyrics tracks incl. rap) plus synthetic applause, crowd murmur and arena reverb, and prints: MC flag rate per kind of audio, where chapter onsets land when a chapter opens with talk, chapterless segmentation, false MC seconds inside every real song, and MC recall per mixing condition. MC detection OFF reproduces the pre-spec-001 analyser, so every table is a before/after. Audio is downloaded at runtime into `--cache` (default: a temp dir, deleted) and never committed. Needs the analyser's optional PyAV / faster-whisper stack. |
| `render_bench.py` | Standalone CPU (Tk/PIL) lyric-render micro-benchmark: glyph atlas, line compose, sliver fill. Tests the LP-005 flat-render plus alpha-composite outline idea against per-glyph stroke, cold and warm, at two font scales. Placeholder text only. |

## Running the tests

The suite in `tests/` runs under `pytest` (install it with `pip install
pytest`; it is not in `requirements.txt`). CI runs exactly this on Linux:

```
pip install pytest -r requirements-deps.txt
python -m pytest tests -q          # Linux without a desktop: xvfb-run -a python -m pytest tests -q
```

`tests/test_concert_audio.py` needs only numpy. Its decoder tests need PyAV and
faster-whisper, from `requirements-deps.txt`, the optional audio stack. They
skip cleanly without it; CI installs it so they run. `tests/test_concert_sync.py` and
`tests/test_media_source_policy.py` import `main`, which needs Tk and, on
Linux, a display for pystray (hence `xvfb-run`). The older `unittest`-based
files also still run with `python -m unittest discover -s tests`; the spec-001
concert tests use pytest fixtures and need pytest.

> The app itself (the modules in the repo root) is **not** here — those are
> imported at runtime and bundled by `DesktopKaraoke.spec`. See
> [`docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md) for the module map and
> [`docs/REPO_ORGANIZATION.md`](../docs/REPO_ORGANIZATION.md) for the top-level
> inventory.
