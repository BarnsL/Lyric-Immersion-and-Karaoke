// Shape of the app's local API responses. See D:\Desktop-Karaoke\api.py.

export interface Health {
  ok: boolean;
  app: string;
  version: string;
  uptime_s: number;
}

/**
 * GET /status — field names verified line-by-line against `api.py._status`.
 *
 * TICKET-197: this interface used to invent its own names (`title`, `artist`,
 * `offset`, `now_line`, `source`, `subs_mode`, `live_arrangement`, `mv_mode`).
 * None of them exist on the wire. TypeScript cannot catch that — the payload is
 * cast from `await resp.json()`, so every one of them silently read `undefined`
 * and the Overview showed "Idle / No SMTC session detected" with a track playing.
 * If you add a field here, copy the name from api.py; do not guess it.
 */
export interface StatusPayload {
  ok: boolean;
  playing?: boolean;
  player_title?: string | null;        // NOT `title`
  player_artist?: string | null;       // NOT `artist`
  matched_title?: string | null;       // the loaded lyric body
  matched_artist?: string | null;
  lang?: string | null;
  position?: number;
  duration?: number | null;
  sync_offset?: number;                // NOT `offset`
  sync_drift?: number | null;
  sync_drift_age?: number | null;
  sync_pending?: number | null;
  verified?: boolean;
  verified_meta?: boolean;
  source_priority?: string;
  heard_by_sound?: string | null;
  live_mode?: boolean | null;
  boundary_detect?: boolean | null;
  window_titles_on?: boolean | null;
  perf?: string | null;
  fps_target?: number | null;
  render_fps?: number | null;
  frame_jitter_ms?: number;
  frame_worst_ms?: number;
  line_count?: number;
  current_line?: { t?: [number, number]; jp?: string; rm?: string; en?: string } | null;
  gpu?: Record<string, unknown> | null;
  decision_engine?: Record<string, unknown> | null;
  success_rate?: Record<string, unknown> | null;
}

export type TuneValue = number | boolean | string | null;
export interface TunePayload {
  ok: boolean;
  // GET /tune returns `{"ok": True, "tune": {...}}` — key is `tune`, not
  // `values` (verified against api.py:288). Adapters live in api.ts.
  tune: Record<string, TuneValue>;
}

// Rich diagnostics snapshot — mirrors main.py.get_diag() (~80 fields). Only
// the sub-objects we render on the Overview are named here; everything else
// arrives as `Record<string, unknown>` so a new backend field surfaces
// without a frontend change (Overview picks the ones it knows).
/**
 * The `sync` sub-object of GET /diag. Extracted into a named interface so
 * scripts/check_console_contract.py can diff it against the live endpoint —
 * while it was inline and unchecked, `live_mode` and `body_corroborated` were
 * rendered by Overview.tsx and never sent by the API (TICKET-197).
 */
export interface DiagSync {
  offset?: number;
  drift?: number | null;
  drift_age_s?: number | null;
  drift_integral?: number;
  pending_corr?: number | null;
  verified_meta?: boolean;
  sound_corroborated?: boolean;
  body_corroborated?: boolean;
  live_arrangement?: boolean;
  live_mode?: boolean;
  title_locked?: boolean;
  tier_interval_s?: number;
  tier_good_streak?: number;
  tier_miss_streak?: number;
  tier_listening?: boolean;
  fine_active?: boolean;
  showing_idx?: number;
  should_show_idx?: number;
  should_show_line?: string | null;
  in_sync?: boolean | null;
  scroll_mode?: boolean;
  smtc_paused_for_s?: number;
  verified_render_gate_remaining_s?: number;
  [k: string]: unknown;
}

export interface DiagPayload {
  ok: boolean;
  sync?: DiagSync;
  energy_align?: Record<string, unknown> | null;
  fps?: {
    target?: number | null;
    render?: number | null;
    frame_ms?: number;
    worst_ms?: number;
    jitter_ms?: number;
    recent_ms?: number[];
    perf_mode?: string;
    scroll_dir?: string;
    subtitle_mode?: boolean;
    subs_mode?: string;
    [k: string]: unknown;
  };
  lyrics?: Record<string, unknown>;
  pending_swap?: Record<string, unknown> | null;
  decision?: unknown;
  deciding?: boolean;
  [k: string]: unknown;
}

export type ViewKey =
  | "overview"
  | "finder"
  | "decisions"
  | "activity"
  | "diagram"
  | "parameters"
  | "autoresearch"
  | "resources";

// ── GET /insight (TICKET-190) ────────────────────────────────────────────────
// Song-finder introspection. Every field here was previously log-only: the OCR
// strings and their verdicts were written to a rate-limited log line and then
// discarded, which is why "it matched the YouTube search box" and "the rescue
// lost to a MIN of 75" were both invisible from outside the process.

export interface OcrPass {
  t: number;
  lines: string[];              // EVERY line this pass read, not just the winner
  pool_kind: "setlist" | "library";
  pool_size: number;
  matched: { title: string; score: number } | null;
  accept_at: number;            // score a match must clear (0.85)
  plausible: string | null;     // best uncached candidate this pass
  pending_2nd: string | null;   // held awaiting a 2nd consistent read
}

export type OcrDropReason = "window-chrome" | "not-on-setlist" | "awaiting-2nd-read";

export interface OcrDrop {
  text: string;
  reason: OcrDropReason;
  n: number;                    // how many times this exact string was refused
  t: number;
}

/**
 * The ALWAYS-POPULATED live block (TICKET-194). Everything else in InsightPayload
 * describes an *event* — a banner pass, a by-ear gate — and those only fire on
 * concerts and song boundaries, so during ordinary playback the views had nothing
 * to render and looked broken while the app was working fine.
 */
export interface NowBlock {
  playing: boolean;
  position: number;
  duration: number;
  player_title: string | null;
  player_artist: string | null;
  /**
   * What the engine actually searched providers for, after clean_title() stripped
   * the credits off `player_title`. TICKET-200: this reduction turned an
   * "Artist / Song【MV】" title into just the artist and fetched a real body for
   * the WRONG song, and nothing outside the process could see it happen.
   */
  search_title: string | null;
  loaded_title: string | null;
  loaded_artist: string | null;
  loaded_source: string | null;
  loaded_lang: string | null;
  line_count: number;
  /**
   * Does the loaded body name the same song the player is playing? Compared
   * against `search_title`, not the raw player title, so 【MV】/【… Original
   * Song】 furniture no longer reads as a mismatch on a correct body.
   *
   * NOT a confidence signal — see `evidence`. A body fetched by title search is
   * filed under the title it was searched with, so "match" here is circular.
   */
  agree: "match" | "mismatch" | "none";
  /**
   * How well it is actually KNOWN that this body belongs to this song — the only
   * honest input to a confidence badge.
   *
   *   library — a bundled/baked body; authoritative
   *   words   — the sung words were matched against this body
   *   timing  — an energy or caption lock; proves WHEN, not WHAT
   *   title   — nothing but the title search backs it (the common case)
   *   none    — no body loaded
   */
  evidence: "library" | "words" | "timing" | "title" | "none";
  idx: number;                       // -1 when nothing is showing
  line_t: [number, number] | null;   // current line's [start, end]
  has_romaji: boolean;
  has_english: boolean;
  busy: string | null;               // "deciding by ear" | "aligning" | null
  frame_ms: number;
  /** Tk canvas fps. null when the GPU overlay is drawing — see `renderer`. */
  render_fps: number | null;
  renderer: "gpu" | "tk";
  perf: string | null;
  overlay: "lyrics" | "subtitles" | "idle";
}

/** The decide-by-ear gate arithmetic — the numbers that accept or refuse a switch. */
export interface GateSnapshot {
  t: number;
  /** True once the track changed under it — the snapshot describes a PREVIOUS song. */
  stale?: boolean;
  track?: string | null;
  expanded: boolean;            // library-wide search (higher bar) vs title-confined
  best: number;
  loaded: number;
  best_key: string;
  min_required: number;         // AFTER the title-lock bump
  margin_required: number;
  margin_actual: number;
  title_locked: boolean;
  block_cross_artist: boolean;
  loaded_worthless: boolean;    // stub body -> protections lift (TICKET-188)
  loaded_lines: number;
  lopsided: boolean;
  short_transcript: boolean;
  short_decisive: boolean;
  heard_chars: number;
  outcome: "switch" | "blocked-cross-artist" | "below-min" | "below-margin";
}

export interface SetlistSong {
  title: string;
  kind?: string;                // "original" | "cover" | …
  cached: boolean;              // lyrics already on disk
  file: string | null;
}

export interface InsightPayload {
  ok: boolean;
  now: NowBlock | null;
  ocr: OcrPass | null;
  ocr_drops: OcrDrop[];
  gates: GateSnapshot | null;
  setlist: {
    candidates: SetlistSong[];
    chapters: { start: number; title: string }[];
    idx: number | null;
    gate_on: boolean;
  };
  sources: {
    smtc: { title: string | null; artist: string | null; source: string | null; playing: boolean };
    loaded: { title: string | null; artist: string | null; source: string | null; lang: string | null; lines: number };
    shazam: { heard: string | null; alias: string | null; corroborated: boolean };
    locks: { title_locked: boolean; verified: boolean; body_corroborated: boolean; body_word_verified: boolean };
    mode: { live: boolean; subtitles: boolean; non_music_page: boolean };
  };
  decision: { state: string | null; strikes: number; dims: Record<string, string> } | null;
  /** LIVE/concert sync posture — a different state machine from studio. */
  sync: {
    live: boolean;
    /** Which threshold set is in force — the three are genuinely different machines. */
    profile: "studio" | "live" | "concert";
    offset: number;
    drift: number | null;
    tier_interval_s: number;
    ok_drift: number;          // drift under this counts as "in sync"
    single_shot_max: number;   // drift that may commit on ONE read (live was 0)
    tpvr_gap_s: number;        // wait between the held read and its confirmation
    apply_min_s: number;       // correction deadband
    pending: number | null;
    held: boolean;             // a first read is held awaiting confirmation
    fine_active: boolean;
    caption_timed: boolean;    // body is video-locked; the correlator stands down
    fail_streak: number;
  } | null;
  autoresearch: {
    worktree: string | null;
    branch: string;
    exists: boolean;
    experiments: number;
    ahead_of_master: number;
    last_commit: string | null;
    skill_installed: boolean;
  } | null;
  events: Record<string, unknown>[];
  /**
   * TICKET-203 - the human-readable decision log. Each entry is a moment a human
   * would want narrated: the song changed, a body was rejected, a big correction
   * committed, lyrics finally arrived. Distinct from `events` (the raw sync
   * telemetry firehose) - the console renders this as prose and raises a
   * toast/notification per new entry.
   */
  notable: NotableEvent[];
  /**
   * TICKET-204 - the strings the title engine has seen for the current video,
   * so the "Wrong song" correct-text picker can offer every candidate. Titles
   * only - no lyric body text (keeps the repo clear of copyright material).
   */
  title_id: TitleIdentification | null;
}

/**
 * A single narrated decision moment. See main.py `_note_event`.
 *
 * `gap` is the number that makes a sync event legible: how far apart the video
 * position (`pos`) and the lyric line time (`lyric_t`) were when this happened.
 * In the hololive Unchained runaway it read pos=16.6, lyric_t=72.6 - a 56s
 * gap that was invisible until someone read the log by hand.
 */
export interface NotableEvent {
  t: number;
  kind: string;
  sev: "good" | "info" | "warn";
  detail: string;
  title: string;
  pos: number;
  lyric_t: number | null;
  gap: number | null;
  offset: number;
  source?: string;
  lines?: number;
  late_s?: number;
  line?: number;
  ratio?: number;
  reason?: string;
}

/**
 * Title identification diagnostics (TICKET-204). The full text the engine saw
 * and what it reduced it to, so the "Wrong song" picker can offer every string
 * the engine has seen as a candidate for the correct title.
 *
 * No lyric body text - only titles and metadata. Keeps the repo clear of
 * copyright material while still logging what the engine mistook for the song.
 */
export interface TitleIdentification {
  raw_title: string;
  clean_title: string;
  search_title: string;
  artist: string;
  overridden: boolean;
  override_title: string | null;
  seen_strings: string[];
}

export interface DiagramNode {
  id: string;
  label: string;
  sub?: string;
  col: number;
  row: number;
  kind: "input" | "reader" | "analyzer" | "decision" | "source" | "annotate" | "cache" | "renderer" | "api" | "output";
}

export interface DiagramEdge {
  from: string;
  to: string;
  dashed?: boolean;
}

export interface Resource {
  title: string;
  location: string;
  href?: string;               // http/https or file:// URI
  path?: string;               // filesystem path (Tauri opener)
  detail: string;
  kind: "worktree" | "doc" | "corpus" | "external" | "external-doc" | "app-endpoint" | "sibling";
}

export interface KnobGroup {
  title: string;
  hint: string;
  match: (name: string) => boolean;
}
