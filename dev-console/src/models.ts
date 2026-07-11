// Shape of the app's local API responses. See D:\Desktop-Karaoke\api.py.

export interface Health {
  ok: boolean;
  app: string;
  version: string;
  uptime_s: number;
}

export interface StatusPayload {
  ok: boolean;
  title?: string;
  artist?: string;
  source?: string;
  status?: number;
  position?: number;
  duration?: number;
  matched?: { title?: string; artist?: string; source?: string; lang?: string };
  live_mode?: boolean;
  live_arrangement?: boolean;
  mv_mode?: boolean;
  subs_mode?: string;
  offset?: number;
  now_line?: { jp?: string; rm?: string; en?: string } | null;
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
export interface DiagPayload {
  ok: boolean;
  sync?: {
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
  };
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
  | "diagram"
  | "parameters"
  | "autoresearch"
  | "resources";

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
