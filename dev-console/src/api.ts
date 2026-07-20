import type {
  ComponentsPayload, ConcertPayload, DiagPayload, Health, InsightPayload,
  LyricCachePayload, StatusPayload, TunePayload, TuneValue,
} from "./models";

// The desktop app's local HTTP API. Localhost-only, no token when the app is
// running under its default settings — see D:\Desktop-Karaoke\api.py.
export const API_BASE = "http://127.0.0.1:8765";

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
  });
  if (!resp.ok) {
    throw new Error(`${resp.status} ${resp.statusText} on ${path}`);
  }
  return (await resp.json()) as T;
}

export const getHealth = () => j<Health>("/health");
export const getStatus = () => j<StatusPayload>("/status");
export const getTune = () => j<TunePayload>("/tune");
export const getDiag = () => j<DiagPayload>("/diag");
// TICKET-190 — song-finder introspection + live gate arithmetic.
export const getInsight = () => j<InsightPayload>("/insight");

// Update a single tune knob. api.py accepts POST /tune?key=…&value=… (query)
// OR a JSON body {k: v, …}. We stringify booleans/numbers so the server-side
// coercer (which lives in main.py.set_tune) receives a form the parser will
// accept for every scalar type.
//
// TICKET-220 — the declared return type used to be `{ok, msg?}`, which did not
// match the server on either field. api.py returns
//   {ok: bool, results: [{key, ok, msg}], tune: {…every knob…}}
// with NO top-level `msg`. Two consequences, both silent:
//   * callers reading `r.msg` for the failure reason always got `undefined`, so
//     the real per-key reason in `results[0].msg` was discarded;
//   * a REJECTED knob returns HTTP 200 with `ok: false`, so any caller that only
//     awaited the promise treated a refusal as a success and wrote the value
//     into local state anyway.
// The server already sends the full coerced `tune` back, so callers can also
// reconcile against what the engine actually stored rather than what was typed.
export interface SetTuneResult {
  ok: boolean;
  results?: { key: string; ok: boolean; msg?: string }[];
  tune?: Record<string, TuneValue>;
}

export async function setTune(key: string, value: TuneValue): Promise<SetTuneResult> {
  const s = typeof value === "boolean" ? (value ? "1" : "0") : String(value ?? "");
  return j(`/tune?key=${encodeURIComponent(key)}&value=${encodeURIComponent(s)}`, {
    method: "POST",
  });
}

// Pull the reason a knob write failed out of the per-key results array, falling
// back to a caller-supplied default. Centralised because every caller was
// reaching for the non-existent top-level `msg`.
export function tuneError(r: SetTuneResult, fallback: string): string | null {
  if (r.ok) return null;
  return r.results?.find((x) => !x.ok)?.msg || fallback;
}

// TICKET-204: mark the current lyrics wrong and trigger a re-identify + re-fetch.
export async function postWrong(): Promise<{ ok: boolean; action: string }> {
  return j("/wrong", { method: "POST" });
}

// TICKET-204: override the title the engine reduced the video title to. The
// user picked the correct string from what the engine saw; this forces a
// re-fetch under that title. The bad string + good string are logged engine-side.
export async function overrideTitle(title: string): Promise<{ ok: boolean; action: string }> {
  return j("/override_title", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
}

// TICKET-208: correct a wrongly-detected song language. This is not cosmetic —
// the language picks the romanisation (furigana+romaji vs pinyin), decides
// whether a translation lane is generated, chooses the overlay font, gates which
// lyric providers are tried, and drives a cache rejection that DELETES a body.
export async function overrideLanguage(lang: string): Promise<{ ok: boolean; action?: string; error?: string }> {
  return j("/override_language", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lang }),
  });
}

// TICKET-207: attach the user's own words to one narrated event.
// `matched` comes back false when the event has already aged out of the engine's
// ring — the note is still recorded, but the caller should say so rather than
// pretend it landed on the event.
export async function addEventNote(
  eventId: number, note: string,
): Promise<{ ok: boolean; matched?: boolean; id?: number; error?: string }> {
  return j("/event_note", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ event_id: eventId, note }),
  });
}

// TICKET-210: the cached lyric library, metadata only (never lyric text).
// Its own endpoint rather than a block on /insight because it grows with the
// library and /insight is polled every 2.5s.
export const getLyricCache = (limit = 500) => j<LyricCachePayload>(`/lyric_cache?limit=${limit}`);

// TICKET-210: wipe the whole cache so fresh-install behaviour can be tested.
// `confirm` is required engine-side; passing it explicitly here keeps the
// destructive intent visible at the call site rather than buried in a default.
export async function clearLyricCache(
  keepCurrent = false,
): Promise<{ ok: boolean; removed?: number; freed_bytes?: number; failed?: string[]; error?: string }> {
  return j(`/clear_cache?confirm=1${keepCurrent ? "&keep_current=1" : ""}`, { method: "POST" });
}

// TICKET-211: what is really installed vs what we are pretending is missing.
// Note this same object also rides along inside /insight, which is what the
// Library view consumes; this endpoint exists for callers that want it alone.
export const getComponents = () => j<ComponentsPayload>("/components");

// TICKET-218: the concert/live picture — mode verdict plus the rule behind it,
// the applause integrator, chapters with non-song segments marked, the offline
// onset plan, the between-songs hold, both watchdogs, and the steering knobs.
export const getConcert = () => j<ConcertPayload>("/concert");

// Open a URL or path via the Tauri opener plugin, degrading gracefully in a
// browser preview (window.open) so the same components work in `npm run dev`
// without a Tauri shell.
export async function openExternal(target: string) {
  try {
    // Dynamic import so Vite doesn't require the plugin during vite build in
    // a browser-only context.
    const { openUrl, openPath } = await import("@tauri-apps/plugin-opener");
    if (/^https?:\/\//i.test(target) || /^file:/i.test(target)) {
      await openUrl(target);
    } else {
      await openPath(target);
    }
  } catch {
    if (/^https?:\/\//i.test(target)) window.open(target, "_blank");
  }
}
