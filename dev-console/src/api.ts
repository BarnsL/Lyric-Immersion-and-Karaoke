import type { DiagPayload, Health, StatusPayload, TunePayload, TuneValue } from "./models";

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

// Update a single tune knob. api.py accepts POST /tune?key=…&value=… (query)
// OR a JSON body {k: v, …}. We stringify booleans/numbers so the server-side
// coercer (which lives in main.py.set_tune) receives a form the parser will
// accept for every scalar type.
export async function setTune(key: string, value: TuneValue): Promise<{ ok: boolean; msg?: string }> {
  const s = typeof value === "boolean" ? (value ? "1" : "0") : String(value ?? "");
  return j(`/tune?key=${encodeURIComponent(key)}&value=${encodeURIComponent(s)}`, {
    method: "POST",
  });
}

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
