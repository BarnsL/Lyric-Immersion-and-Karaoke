# Lyric Immersion — Developer Console

A Tauri 2 (React + Vite + TypeScript) developer GUI for Lyric Immersion and Karaoke. Reads the app's localhost API and gives you:

- **Overview** — health, now-playing, sync offset, uptime, quick jump list
- **Runtime map** — inline SVG of the pipeline (mirrors `docs/REPO_ORGANIZATION.md`)
- **Parameters** — every `self._tune` knob from `main.py`, grouped, searchable, editable via `POST /tune`
- **AutoResearch** — prompt template + safety notes for the `uditgoenka/autoresearch` loop targeting concerts
- **Resources** — worktrees, docs, corpora, sibling projects, external tools, and API endpoints

Style tokens intentionally track [Website Management Console](../../projects/Website-Management-Console/) so the two apps feel like a family.

## Dev

```pwsh
cd D:\Desktop-Karaoke\dev-console
npm install
npm run tauri:dev      # dev with HMR + Tauri shell
```

Vite runs on `http://127.0.0.1:1420` and the Tauri shell picks it up automatically.

## Build

```pwsh
npm run tauri:build
```

Outputs `src-tauri/target/release/lyric-immersion-dev-console.exe` (portable) + an NSIS installer under `src-tauri/target/release/bundle/nsis/`.

## Launched from the tray

The Lyric Immersion tray menu has a **🛠 Developer Console** item that spawns the built exe with a hidden console window (per the "no visible windows" etiquette). If the exe isn't built yet, the item shows a hint pointing back here.

## Talks to

- `GET http://127.0.0.1:8765/health`
- `GET http://127.0.0.1:8765/status`
- `GET http://127.0.0.1:8765/tune`
- `POST http://127.0.0.1:8765/tune?key=…&value=…`
- `GET http://127.0.0.1:8765/diag`

The `connect-src` CSP is scoped to just `http://127.0.0.1:8765` — the console can't reach anything else even accidentally.
