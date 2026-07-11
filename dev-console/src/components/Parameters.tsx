import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, Cog, RefreshCw, Search, X } from "lucide-react";
import { getTune, setTune } from "../api";
import { KNOB_GROUPS } from "../manifest";
import type { TuneValue } from "../models";

interface Props {
  online: boolean | null;
}

interface Groups {
  title: string;
  hint: string;
  entries: [string, TuneValue][];
}

const OTHER: Groups = { title: "Other", hint: "Uncategorised knobs — add a matcher in manifest.ts to fold these into a group.", entries: [] };

// ─── Type inference & validation ────────────────────────────────────────────
type KnobType = "boolean" | "integer" | "float" | "string";

function knobTypeOf(v: TuneValue): KnobType {
  if (typeof v === "boolean") return "boolean";
  if (typeof v === "number") return Number.isInteger(v) ? "integer" : "float";
  return "string";
}

function validate(v: string, t: KnobType): string | null {
  const s = v.trim();
  if (t === "boolean") {
    if (!/^(0|1|true|false|on|off|yes|no)$/i.test(s)) return "must be 0/1 or true/false";
    return null;
  }
  if (t === "integer") {
    if (!/^-?\d+$/.test(s)) return "must be an integer";
    return null;
  }
  if (t === "float") {
    if (!/^-?\d+(\.\d+)?([eE][-+]?\d+)?$/.test(s)) return "must be a number";
    return null;
  }
  return null;
}

function coerce(v: string, t: KnobType): TuneValue {
  const s = v.trim();
  if (t === "boolean") return /^(1|true|on|yes)$/i.test(s);
  if (t === "integer") return parseInt(s, 10);
  if (t === "float") return parseFloat(s);
  return s;
}

function group(values: Record<string, TuneValue>): Groups[] {
  const groups: Groups[] = KNOB_GROUPS.map((g) => ({ title: g.title, hint: g.hint, entries: [] }));
  const other = { ...OTHER, entries: [] as [string, TuneValue][] };
  const entries = Object.entries(values).sort(([a], [b]) => a.localeCompare(b));
  for (const [k, v] of entries) {
    const idx = KNOB_GROUPS.findIndex((g) => g.match(k));
    if (idx >= 0) groups[idx].entries.push([k, v]);
    else other.entries.push([k, v]);
  }
  return [...groups.filter((g) => g.entries.length), ...(other.entries.length ? [other] : [])];
}

export function Parameters({ online }: Props) {
  const [values, setValues] = useState<Record<string, TuneValue> | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [editing, setEditing] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setErr(null);
    getTune()
      .then((t) => { if (!cancelled) setValues(t.tune ?? {}); })
      .catch((e) => { if (!cancelled) setErr(String(e?.message ?? e)); });
    return () => { cancelled = true; };
  }, [tick]);

  const grouped = useMemo(() => (values ? group(values) : []), [values]);
  const total = values ? Object.keys(values).length : 0;
  const needle = q.trim().toLowerCase();
  const filter = (k: string) => !needle || k.toLowerCase().includes(needle);

  async function commit(k: string, valueOverride?: TuneValue) {
    let out: TuneValue;
    if (valueOverride !== undefined) {
      out = valueOverride;
    } else {
      const raw = editing[k];
      if (raw === undefined) return;
      const t = knobTypeOf(values?.[k] ?? "");
      const e = validate(raw, t);
      if (e) { setErr(`${k}: ${e}`); return; }
      out = coerce(raw, t);
    }
    setSaving(k);
    setErr(null);
    try {
      await setTune(k, out);
      // Optimistic update — the server refreshes on the next poll anyway.
      setValues((prev) => (prev ? { ...prev, [k]: out } : prev));
      const next = { ...editing };
      delete next[k];
      setEditing(next);
    } catch (e) {
      setErr(`Set ${k} failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSaving(null);
    }
  }

  return (
    <>
      <div className="topbar">
        <div>
          <div className="eyebrow"><Cog size={11} /> tune knobs</div>
          <h1>Parameters &amp; weights</h1>
          <p>Every live-tunable knob from <code>main.py:self._tune</code>. Values are read from <code>GET /tune</code>; edits post back via <code>POST /tune?key=&amp;value=</code>.</p>
        </div>
        <button className="button quiet" onClick={() => setTick((t) => t + 1)}>
          <RefreshCw size={13} /> refresh
        </button>
      </div>

      {online === false && (
        <div className="card" style={{ borderColor: "#7b3e50" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, color: "#ffc0ce" }}>
            <AlertTriangle size={16} />
            The app isn't running. Start Lyric Immersion, then refresh — the console reads <code>/tune</code> at runtime.
          </div>
        </div>
      )}

      {err && (
        <div className="card" style={{ borderColor: "#7b3e50" }}>
          <div style={{ color: "#ffc0ce", fontSize: 12 }}>
            <AlertTriangle size={14} style={{ verticalAlign: "middle", marginRight: 6 }} />
            {err}
          </div>
        </div>
      )}

      <div className="params-toolbar">
        <div style={{ position: "relative" }}>
          <Search size={13} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#82889c" }} />
          <input
            type="search"
            placeholder="filter by name (e.g. concert_)"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            style={{ paddingLeft: 30 }}
          />
        </div>
        <div className="count-pill">{total} knobs</div>
      </div>

      {values && grouped.map((g) => {
        const visible = g.entries.filter(([k]) => filter(k));
        if (visible.length === 0) return null;
        return (
          <div key={g.title} className="card knob-group">
            <h3>{g.title} <span style={{ color: "#82889c", fontSize: 11, marginLeft: 8 }}>({visible.length})</span></h3>
            <p className="knob-hint">{g.hint}</p>
            <table className="knob-table">
              <tbody>
                {visible.map(([k, v]) => {
                  const edited = editing[k];
                  const isEditing = edited !== undefined;
                  const t = knobTypeOf(v);
                  const err = isEditing ? validate(edited, t) : null;
                  // Booleans render as an immediate checkbox — no "click to edit" middle step
                  // and no free-text ambiguity. Everything else uses the click-to-edit pattern
                  // with a type-appropriate input.
                  if (t === "boolean") {
                    const on = v === true || v === 1 || v === "1" || v === "true";
                    const busy = saving === k;
                    return (
                      <tr key={k} className="knob-row">
                        <td>
                          <code>{k}</code>
                          <span className="knob-type">bool</span>
                        </td>
                        <td className="val">
                          <label className="switch" title={busy ? "saving…" : (on ? "on" : "off")}>
                            <input
                              type="checkbox"
                              checked={on}
                              disabled={busy}
                              onChange={(e) => commit(k, e.target.checked)}
                            />
                            <span className="slider" />
                          </label>
                        </td>
                      </tr>
                    );
                  }
                  return (
                    <tr key={k} className={`knob-row${isEditing ? " editable" : ""}`}>
                      <td>
                        <code>{k}</code>
                        <span className={`knob-type ${t}`}>{t}</span>
                      </td>
                      <td className="val">
                        {isEditing ? (
                          <>
                            <input
                              autoFocus
                              type={t === "string" ? "text" : "number"}
                              inputMode={t === "integer" ? "numeric" : t === "float" ? "decimal" : undefined}
                              step={t === "float" ? "any" : t === "integer" ? "1" : undefined}
                              value={edited}
                              className={err ? "invalid" : ""}
                              onChange={(e) => setEditing({ ...editing, [k]: e.target.value })}
                              onKeyDown={(e) => {
                                if (e.key === "Enter" && !err) commit(k);
                                if (e.key === "Escape") { const n = { ...editing }; delete n[k]; setEditing(n); }
                              }}
                            />
                            <button
                              className="button primary tiny"
                              disabled={!!err || saving === k}
                              onClick={() => commit(k)}
                              title={err ?? "save"}
                            >
                              {saving === k ? "…" : <><Check size={11} /> save</>}
                            </button>
                            <button
                              className="button quiet tiny"
                              onClick={() => { const n = { ...editing }; delete n[k]; setEditing(n); }}
                              title="cancel"
                            >
                              <X size={11} />
                            </button>
                            {err && <span className="knob-err">{err}</span>}
                          </>
                        ) : (
                          <button
                            className="button quiet tiny"
                            onClick={() => setEditing({ ...editing, [k]: String(v) })}
                            title={`edit (${t})`}
                          >
                            {String(v)}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        );
      })}

      {!values && !err && !online && (
        <div className="empty">
          Waiting for the app…
          <small>The Parameters tab reads <code>GET /tune</code> at runtime; it needs the app to be running.</small>
        </div>
      )}
    </>
  );
}
