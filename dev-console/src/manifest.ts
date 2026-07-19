import type { DiagramEdge, DiagramNode, KnobGroup, Resource } from "./models";

// ─── Runtime diagram (mirrors docs/REPO_ORGANIZATION.md flowchart) ──────────
// 5-column layered layout so an SVG grid can position without overlaps.
// Kept small on purpose — the goal is orientation, not exhaustive detail.
export const DIAGRAM_NODES: DiagramNode[] = [
  { id: "player",   label: "Media apps",         sub: "Spotify · YouTube · Games", col: 0, row: 0, kind: "input" },
  { id: "titles",   label: "Window titles",      sub: "Steam · Discord",           col: 0, row: 1, kind: "input" },
  { id: "discord",  label: "Discord RPC",        sub: "Spotify presence",          col: 0, row: 2, kind: "input" },

  { id: "smtc",     label: "audible_sessions",   sub: "SMTC position + meta",      col: 1, row: 0, kind: "reader" },
  { id: "audio",    label: "loopback audio",     sub: "WASAPI capture",             col: 1, row: 1, kind: "reader" },

  { id: "recog",    label: "recognize.py",       sub: "Shazam fingerprint",         col: 2, row: 0, kind: "analyzer" },
  { id: "sc",       label: "songchange.py",      sub: "boundary + vocal onset",     col: 2, row: 1, kind: "analyzer" },
  { id: "align",    label: "align + deep_transcribe", sub: "energy / Whisper sync", col: 2, row: 2, kind: "analyzer" },
  // TICKET-200: this step was missing from the map, and it is the one that decides
  // WHAT STRING the providers get asked for. It silently reduced an
  // "Artist / Song【MV】" title to just the artist and fetched a real body for the
  // wrong song. A map that goes straight from the player to the providers hides the
  // single most consequential transformation in the whole identification path.
  { id: "clean",    label: "clean_title()",      sub: "credits → search title",     col: 2, row: 3, kind: "analyzer" },

  { id: "overlay",  label: "main.py Overlay",    sub: "state + settings + clock",   col: 3, row: 0, kind: "decision" },
  { id: "decision", label: "confidence.py",      sub: "TRUST · CAUTION · SWITCH",   col: 3, row: 1, kind: "decision" },

  { id: "prov",     label: "fetch_lyrics",       sub: "LRCLIB · syncedlyrics",      col: 4, row: 0, kind: "source" },
  { id: "caps",     label: "deep_transcribe",    sub: "YouTube captions (yt-dlp)",  col: 4, row: 1, kind: "source" },
  { id: "ocr",      label: "concert_ocr",        sub: "burned-in title / lines",    col: 4, row: 2, kind: "source" },
  { id: "gen",      label: "generate by ear",    sub: "Whisper fallback",           col: 4, row: 3, kind: "source" },

  { id: "annot",    label: "annotate",           sub: "furigana · romaji · en",     col: 5, row: 1, kind: "annotate" },
  { id: "cache",    label: "lyrics/*.json",      sub: "cache",                      col: 5, row: 2, kind: "cache" },

  { id: "cpu",      label: "Tk canvas",          sub: "CPU renderer",               col: 6, row: 0, kind: "renderer" },
  { id: "api",      label: "api.py",             sub: "127.0.0.1:8765",             col: 6, row: 1, kind: "api" },
  { id: "tauri",    label: "lyric-overlay.exe",  sub: "Tauri GPU renderer",         col: 6, row: 2, kind: "renderer" },

  { id: "screen",   label: "Overlay",            sub: "click-through, per-pixel α", col: 7, row: 1, kind: "output" },
];

export const DIAGRAM_EDGES: DiagramEdge[] = [
  { from: "player",   to: "smtc" },
  { from: "player",   to: "audio" },
  { from: "titles",   to: "overlay" },
  { from: "discord",  to: "overlay" },

  { from: "smtc",     to: "overlay" },
  { from: "smtc",     to: "clean" },
  { from: "clean",    to: "prov" },
  { from: "audio",    to: "recog" },
  { from: "audio",    to: "sc" },
  { from: "audio",    to: "align" },

  { from: "recog",    to: "decision" },
  { from: "sc",       to: "decision" },
  { from: "align",    to: "decision" },
  { from: "overlay",  to: "decision" },

  { from: "decision", to: "prov" },
  { from: "decision", to: "caps" },
  { from: "decision", to: "ocr" },
  { from: "decision", to: "gen" },

  { from: "prov",     to: "annot" },
  { from: "caps",     to: "annot" },
  { from: "ocr",      to: "annot" },
  { from: "gen",      to: "annot" },

  { from: "annot",    to: "cache" },
  { from: "cache",    to: "overlay", dashed: true },

  { from: "overlay",  to: "cpu" },
  { from: "overlay",  to: "api" },
  { from: "api",      to: "tauri" },
  { from: "cpu",      to: "screen" },
  { from: "tauri",    to: "screen" },
];

// ─── Knob groups — the ~200 tune keys are organized by comment section ─────
// Match by prefix or substring so newly-added knobs land in the right bucket
// without an update here. First-match wins; unmatched go to "Other".
export const KNOB_GROUPS: KnobGroup[] = [
  { title: "Sync — deadband + windows",  hint: "Perceptual in-sync window; deadband; single-shot vs TPVR gates",
    match: (n) => /^(deadband|sync_win_|display_lead|agree|single_shot|reset_offset|drift_)/i.test(n) },
  { title: "TPVR — two-point verify",     hint: "Chorus-differentiation timing (hold + confirming listen)",
    match: (n) => /^(tpvr_|live_tpvr_|sync_confirm_)/i.test(n) },
  { title: "Auto re-sync / auto-align",   hint: "Cooldowns, minimum player position, Shazam-lock grace",
    match: (n) => /^(auto_align_|shazam_lock_|spread_reset)/i.test(n) },
  { title: "Concert / live arrangement",  hint: "Live resync cadence, live_song cap, applause detection",
    match: (n) => /^(live_|applause_|concert_)/i.test(n) },
  { title: "Concert setlist / candidates", hint: "candidate pool, chapter override, setlist skip filters",
    match: (n) => /^(concert_pool_|concert_setlist_|chapter_)/i.test(n) },
  { title: "OCR & sync",                  hint: "Concert OCR + burned-in line reading + OCR-assisted sync",
    match: (n) => /^(ocr_)/i.test(n) },
  { title: "Subtitles mode",              hint: "Deep-fetch caps + inline-translate cap for shows",
    match: (n) => /^(subs_)/i.test(n) },
  { title: "Discord RPC / window titles", hint: "Poll cadences + high/low tier for the auxiliary readers",
    match: (n) => /^(discord_rpc_|window_titles_)/i.test(n) },
  { title: "Auto game-focus mode",        hint: "Arm/release timings for D3D fullscreen swaps",
    match: (n) => /^(auto_game_)/i.test(n) },
  { title: "Display / multi-monitor",     hint: "Selector fallback timing, stick-to-selected",
    match: (n) => /^(display_)/i.test(n) },
  { title: "Translation / romanization",  hint: "Recheck cadence, retry cap; layer toggles gate the WORK",
    match: (n) => /^(translate_|captions_inline_translate)/i.test(n) },
  { title: "SMTC-paused takeover",        hint: "Shazam override when SMTC pauses but audio continues",
    match: (n) => /^(smtc_paused_|smtc_takeover_)/i.test(n) },
  { title: "YouTube description mining",  hint: "yt-dlp description fetch + title-alias album fallback",
    match: (n) => /^(yt_description_|title_alias_)/i.test(n) },
];

// ─── AutoResearch loop (mirror of D:\Lyric-Immersion-AR\AR_README.md) ──────
export const AUTORESEARCH = {
  worktreePath: "D:\\Lyric-Immersion-AR",
  branch: "autoresearch",
  installCmd: "npx skills add uditgoenka/autoresearch",
  restartHint: "Restart Claude Code after install so the /autoresearch slash command becomes visible.",
  loopTemplate: [
    "/autoresearch",
    "Goal: raise concerts.yaml aggregate score from baseline to ≥ 65",
    "Scope: main.py:5300-5900 main.py:11050-11110 concert_ocr.py concert_audio.py yt_description.py",
    "Guard: python -m py_compile main.py",
    "Metric: aggregate concerts score",
    "Verify: python -m auto concerts --corpus concerts.yaml --window 300 --print-score",
    "Iterations: 50",
  ].join("\n"),
  safety: [
    "Every AR iteration commits with an `experiment:` prefix — never merge this branch into master.",
    "Inspect winning experiments by hand, then port the diff onto a normal commit.",
    "`git worktree remove D:\\Lyric-Immersion-AR` when done — the branch itself stays.",
    ".ckignore at the worktree root extends the AR default block list.",
  ],
  envOverrides: [
    ["AR_DISABLE_SCOUT_BLOCK", "1  (if scout hook blocks a legit read)"],
    ["AR_DISABLE_PRIVACY_BLOCK", "1  (if privacy hook blocks a legit read)"],
  ],
};

// ─── Resources — everything the user might jump to ─────────────────────────
export const RESOURCES: Resource[] = [
  // Worktrees
  { kind: "worktree", title: "master (production)",  location: "D:\\Desktop-Karaoke",       path: "D:\\Desktop-Karaoke",       detail: "Live source. `master` branch. Builds to dist\\DesktopKaraoke and deploys to D:\\DesktopKaraoke." },
  { kind: "worktree", title: "auto-tune",            location: "D:\\Lyric-Immersion-Auto",  path: "D:\\Lyric-Immersion-Auto",  detail: "Optuna-driven param tuner + `python -m auto duel` two-instance A/B." },
  { kind: "worktree", title: "gpu-renderer",         location: "D:\\Lyric-Immersion-GPU",   path: "D:\\Lyric-Immersion-GPU",   detail: "Standalone GPU renderer process + JP-vagency caption guards." },
  { kind: "worktree", title: "autoresearch",         location: "D:\\Lyric-Immersion-AR",    path: "D:\\Lyric-Immersion-AR",    detail: "Landing zone for `uditgoenka/autoresearch` loops. Never merge into master." },

  // Runtime deploy
  { kind: "worktree", title: "deployed app",         location: "D:\\DesktopKaraoke",        path: "D:\\DesktopKaraoke",        detail: "Where the running v1.1.66 lives. `robocopy dist\\DesktopKaraoke` mirrors here." },

  // Docs
  { kind: "doc", title: "HANDOVER-2026-07-04.md",  location: "master · handover",        path: "D:\\Desktop-Karaoke\\HANDOVER-2026-07-04.md",     detail: "Baseline handover (v1.1.63); consult before sync / chapter / auto-game / gaming-preset / clean_title / build.bat work." },
  { kind: "doc", title: "docs/ARCHITECTURE.md",    location: "confidence model",         path: "D:\\Desktop-Karaoke\\docs\\ARCHITECTURE.md",      detail: "The parts, methods, and confidence gates that decide whether to act." },
  { kind: "doc", title: "docs/REPO_ORGANIZATION.md", location: "runtime diagram + layout", path: "D:\\Desktop-Karaoke\\docs\\REPO_ORGANIZATION.md", detail: "Mermaid runtime diagram (source of the Diagram tab) + source layout + data stores." },
  { kind: "doc", title: "docs/CONCERT_RESEARCH.md", location: "concert plan",            path: "D:\\Desktop-Karaoke\\docs\\CONCERT_RESEARCH.md",  detail: "8-section plan: corpus, failure-mode taxonomy, P0–P3 roadmap, AutoResearch patterns, open questions." },
  { kind: "doc", title: "docs/DEPLOYMENT.md",      location: "release process",         path: "D:\\Desktop-Karaoke\\docs\\DEPLOYMENT.md",        detail: "How releases are cut (version.py, installer.iss, build.bat, .zip + .sha256)." },
  { kind: "doc", title: "docs/ISSUES.md",          location: "behaviour tickets",       path: "D:\\Desktop-Karaoke\\docs\\ISSUES.md",            detail: "Numbered TICKET-### tracker for matching / sync / rendering behavior." },

  // Corpus
  { kind: "corpus", title: "docs/concerts.yaml",   location: "6 concerts (C1–C6)",       path: "D:\\Desktop-Karaoke\\docs\\concerts.yaml",       detail: "Machine-readable test corpus consumed by the auto-tune harness + `_load_concert_setlist`." },
  { kind: "corpus", title: "concert_marks.jsonl",  location: "<data>\\concert_marks.jsonl", detail: "Ground-truth boundary marks — written by the tray's 🔖 Mark song boundary item during concerts." },

  // App endpoints (localhost)
  { kind: "app-endpoint", title: "GET /health",   location: "http://127.0.0.1:8765/health",  href: "http://127.0.0.1:8765/health",  detail: "Liveness + version + uptime." },
  { kind: "app-endpoint", title: "GET /status",   location: "http://127.0.0.1:8765/status",  href: "http://127.0.0.1:8765/status",  detail: "Now-playing + matched song + sync offset + current line." },
  { kind: "app-endpoint", title: "GET /tune",     location: "http://127.0.0.1:8765/tune",    href: "http://127.0.0.1:8765/tune",    detail: "All live-tunable knobs + current values (drives the Parameters tab)." },
  { kind: "app-endpoint", title: "GET /diag",     location: "http://127.0.0.1:8765/diag",    href: "http://127.0.0.1:8765/diag",    detail: "Deep diagnostics: sync state machine, energy correlation, FPS." },
  { kind: "app-endpoint", title: "GET /metrics",  location: "http://127.0.0.1:8765/metrics", href: "http://127.0.0.1:8765/metrics", detail: "Per-release success / wobbler / fail telemetry counter." },
  { kind: "app-endpoint", title: "GET /overlay",  location: "http://127.0.0.1:8765/overlay", href: "http://127.0.0.1:8765/overlay", detail: "Render payload the Tauri lyric-overlay child polls." },
  { kind: "app-endpoint", title: "POST /nudge",   location: "?s=±2.5",                       detail: "Shift sync by S seconds (+ = lyrics later)." },

  // Sibling projects
  { kind: "sibling", title: "lyric-overlay-tauri", location: "D:\\projects\\lyric-overlay-tauri", path: "D:\\projects\\lyric-overlay-tauri", detail: "Transparent per-pixel-alpha WebView renderer, fed by /overlay." },
  { kind: "sibling", title: "odysseus-claude-squad", location: "D:\\projects\\odysseus-claude-squad", path: "D:\\projects\\odysseus-claude-squad", detail: "PEER-PROJECTS.md there points every Claude session at Lyric Immersion's docs." },

  // External docs
  { kind: "external-doc", title: "GitHub — repo",  location: "BarnsL/Lyric-Immersion-and-Karaoke",   href: "https://github.com/BarnsL/Lyric-Immersion-and-Karaoke", detail: "Public source, releases, issue tracker." },
  { kind: "external-doc", title: "GitHub — latest release", location: "gh releases",                 href: "https://github.com/BarnsL/Lyric-Immersion-and-Karaoke/releases", detail: "Setup.exe + portable .zip + .sha256." },

  // External tools
  { kind: "external", title: "uditgoenka/autoresearch", location: "GitHub — Claude Code skill",       href: "https://github.com/uditgoenka/autoresearch", detail: "Iterative research loop skill; landing zone is D:\\Lyric-Immersion-AR." },
  { kind: "external", title: "shazamio",              location: "audio fingerprinting",              href: "https://github.com/dotX12/ShazamIO", detail: "The Shazam client used by recognize.py." },
  { kind: "external", title: "faster-whisper",        location: "ctranslate2 whisper",               href: "https://github.com/SYSTRAN/faster-whisper", detail: "Deep transcribe / generation / align backend." },
  { kind: "external", title: "yt-dlp",                location: "captions + audio fetch",            href: "https://github.com/yt-dlp/yt-dlp", detail: "Fetches manual caption tracks + optional audio for whisper generation." },
  { kind: "external", title: "LRCLIB",                location: "provider — synced lyrics",          href: "https://lrclib.net/", detail: "Primary duration-exact provider, then scored fallback." },
];

// Docs the console links to. Absolute paths so the Tauri opener hands them to the
// OS regardless of the console's own working directory (it runs from _internal/
// in a frozen install, and from src-tauri/target/release/ in development).
// FORWARD slashes, deliberately. Windows accepts them everywhere, and a
// double-quoted JS string treats `\D` / `\d` as unknown escapes and silently
// DROPS the backslash: "D:\Desktop-Karaoke\docs\DEV_CONSOLE.md" evaluates to
// "D:Desktop-KaraokedocsDEV_CONSOLE.md", so the "How to use this console" button
// opened nothing at all. No error, no warning — the path was simply wrong.
export const DOCS = {
  devConsole: "D:/Desktop-Karaoke/docs/DEV_CONSOLE.md",
  autoResearch: "D:/Desktop-Karaoke/docs/AUTORESEARCH.md",
  issues: "D:/Desktop-Karaoke/docs/ISSUES.md",
};
