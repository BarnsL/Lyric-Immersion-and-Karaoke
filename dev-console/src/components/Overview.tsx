import type { DiagPayload, Health, StatusPayload } from "../models";
import { RESOURCES } from "../manifest";
import { Activity, Clock, ExternalLink, Gauge, Music, Radar, Radio } from "lucide-react";
import { openExternal } from "../api";

interface Props {
  health: Health | null;
  status: StatusPayload | null;
  diag: DiagPayload | null;
  online: boolean | null;
}

function fmtDuration(s?: number) {
  if (s === undefined || s === null || Number.isNaN(s)) return "—";
  const t = Math.max(0, Math.floor(s));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const sec = t % 60;
  return h > 0 ? `${h}h ${m}m ${sec}s` : m > 0 ? `${m}m ${sec}s` : `${sec}s`;
}

function fmtNum(v: unknown, digits = 2): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return v.toFixed(digits);
  return String(v);
}

function DiagPane({ diag }: { diag: DiagPayload | null }) {
  if (!diag) return null;
  const s = diag.sync ?? {};
  const f = diag.fps ?? {};
  const e = (diag.energy_align ?? {}) as Record<string, unknown>;
  const eBest = typeof e.best_shift === "number" ? e.best_shift : null;
  const eScore = typeof e.score === "number" ? e.score : null;
  const inSync = s.in_sync;
  const inSyncPill =
    inSync === true  ? <span className="pill ok">in sync</span> :
    inSync === false ? <span className="pill warn">drift</span> :
    <span className="pill">unknown</span>;

  return (
    <div className="card">
      <div className="card-heading">
        <div>
          <div className="eyebrow"><Gauge size={11} /> live diagnostics</div>
          <h2>/diag snapshot</h2>
        </div>
        {inSyncPill}
      </div>

      <div className="grid-3">
        <div>
          <div className="eyebrow"><Radar size={10} /> sync FSM</div>
          <table className="diag-table">
            <tbody>
              <tr><td>offset</td><td>{fmtNum(s.offset)}s</td></tr>
              <tr><td>drift</td><td>{fmtNum(s.drift)}s {s.drift_age_s != null && <em>· {s.drift_age_s}s ago</em>}</td></tr>
              <tr><td>drift integral</td><td>{fmtNum(s.drift_integral)}</td></tr>
              <tr><td>tier interval</td><td>{fmtNum(s.tier_interval_s, 1)}s</td></tr>
              <tr><td>good / miss</td><td>{s.tier_good_streak ?? 0} / {s.tier_miss_streak ?? 0}</td></tr>
              <tr><td>verified meta</td><td>{s.verified_meta ? "✓" : "—"}</td></tr>
              <tr><td>body corroborated</td><td>{s.body_corroborated ? "✓" : "—"}</td></tr>
              <tr><td>title locked</td><td>{s.title_locked ? "✓" : "—"}</td></tr>
              <tr><td>fine tune</td><td>{s.fine_active ? "active" : "—"}</td></tr>
              <tr><td>listening</td><td>{s.tier_listening ? "yes" : "—"}</td></tr>
              <tr><td>live</td><td>{s.live_arrangement ? "arrangement" : s.live_mode ? "yes" : "—"}</td></tr>
              <tr><td>SMTC paused for</td><td>{s.smtc_paused_for_s ? `${s.smtc_paused_for_s}s` : "—"}</td></tr>
            </tbody>
          </table>
        </div>

        <div>
          <div className="eyebrow"><Radar size={10} /> energy correlation</div>
          {diag.energy_align ? (
            <table className="diag-table">
              <tbody>
                <tr><td>best_shift</td><td>{fmtNum(eBest)}s</td></tr>
                <tr><td>score</td><td>{fmtNum(eScore, 3)}</td></tr>
                {Object.entries(e).filter(([k]) => !["best_shift", "score"].includes(k)).slice(0, 6).map(([k, v]) => (
                  <tr key={k}><td>{k}</td><td>{typeof v === "number" ? fmtNum(v, 3) : String(v ?? "—")}</td></tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="empty" style={{ padding: 12 }}>No recent energy read.</div>
          )}
          {diag.pending_swap && (
            <div className="pill warn" style={{ marginTop: 10 }}>pending lyric swap</div>
          )}
        </div>

        <div>
          <div className="eyebrow"><Gauge size={10} /> FPS · frame timing</div>
          <table className="diag-table">
            <tbody>
              <tr><td>target</td><td>{f.target ?? "—"} fps</td></tr>
              <tr><td>render</td><td>{f.render ?? "—"} fps</td></tr>
              <tr><td>frame</td><td>{fmtNum(f.frame_ms, 1)} ms</td></tr>
              <tr><td>worst</td><td>{fmtNum(f.worst_ms, 1)} ms</td></tr>
              <tr><td>jitter</td><td>{fmtNum(f.jitter_ms, 1)} ms</td></tr>
              <tr><td>perf</td><td>{f.perf_mode ?? "—"}</td></tr>
              <tr><td>scroll dir</td><td>{f.scroll_dir ?? "—"}</td></tr>
              <tr><td>subs</td><td>{f.subs_mode ?? "off"}</td></tr>
            </tbody>
          </table>
          {Array.isArray(f.recent_ms) && f.recent_ms.length > 0 && (
            <svg viewBox={`0 0 100 26`} className="frame-spark" preserveAspectRatio="none">
              {(() => {
                const arr = f.recent_ms as number[];
                const max = Math.max(24, ...arr);
                const step = 100 / Math.max(1, arr.length - 1);
                const pts = arr.map((v, i) => `${(i * step).toFixed(2)},${(26 - (v / max) * 24).toFixed(2)}`).join(" ");
                return <polyline points={pts} fill="none" stroke="#7d72f1" strokeWidth="1.2" />;
              })()}
            </svg>
          )}
        </div>
      </div>
    </div>
  );
}

export function Overview({ health, status, diag, online }: Props) {
  const worktrees = RESOURCES.filter((r) => r.kind === "worktree");
  const docs      = RESOURCES.filter((r) => r.kind === "doc");
  const endpoints = RESOURCES.filter((r) => r.kind === "app-endpoint");

  return (
    <>
      <div className="topbar">
        <div>
          <div className="eyebrow"><Radio size={11} /> control plane</div>
          <h1>Lyric Immersion — Developer Console</h1>
          <p>Weights, parameters, the AutoResearch loop, and every resource the app depends on.</p>
        </div>
        <div>
          {online
            ? <span className="pill ok">app online · v{health?.version ?? "?"}</span>
            : <span className="pill err">app not running</span>}
        </div>
      </div>

      <div className="page-grid">
        <div className="hero">
          <div>
            <div className="eyebrow"><Music size={11} /> now playing</div>
            <h2>{status?.title ? `${status.title}` : "Idle"}</h2>
            <p>
              {status?.artist ? `${status.artist}` : "No SMTC session detected."}
              {status?.source ? ` · ${status.source}` : ""}
              {status?.subs_mode === "on" && " · subtitles ON"}
              {status?.live_arrangement && " · live arrangement"}
              {status?.live_mode && !status?.live_arrangement && " · live"}
              {status?.mv_mode && " · MV mode"}
            </p>
            <div className="grid-metrics" style={{ marginTop: 14 }}>
              <div className="metric"><strong>{status?.offset != null ? `${status.offset.toFixed(2)}s` : "—"}</strong><span>sync offset</span></div>
              <div className="metric"><strong>{status?.position != null ? fmtDuration(status.position) : "—"}</strong><span>position</span></div>
              <div className="metric"><strong>{status?.duration != null ? fmtDuration(status.duration) : "—"}</strong><span>duration</span></div>
              <div className="metric"><strong>{fmtDuration(health?.uptime_s)}</strong><span>uptime</span></div>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-heading">
            <div>
              <div className="eyebrow"><Activity size={11} /> current line</div>
              <h2>What's on screen</h2>
            </div>
          </div>
          {status?.now_line?.jp || status?.now_line?.rm || status?.now_line?.en ? (
            <div style={{ display: "grid", gap: 8 }}>
              {status?.now_line?.jp && <div style={{ color: "#f3f2ff", fontSize: 20, letterSpacing: "-.02em" }}>{status.now_line.jp}</div>}
              {status?.now_line?.rm && <div style={{ color: "#c4c2e9", fontSize: 14 }}>{status.now_line.rm}</div>}
              {status?.now_line?.en && <div style={{ color: "#9095a8", fontSize: 13, fontStyle: "italic" }}>{status.now_line.en}</div>}
            </div>
          ) : (
            <div className="empty">No line highlighted.<small>Toggle Show/Hide from the tray if the overlay is hidden.</small></div>
          )}
        </div>

        <DiagPane diag={diag} />

        <div className="grid-3">
          <div className="card">
            <div className="card-heading">
              <div>
                <div className="eyebrow"><Clock size={11} /> worktrees</div>
                <h2>{worktrees.length} branches</h2>
              </div>
            </div>
            <div style={{ display: "grid", gap: 6 }}>
              {worktrees.slice(0, 4).map((r) => (
                <div key={r.title} style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center", fontSize: 12 }}>
                  <strong style={{ color: "#e2e0f8" }}>{r.title}</strong>
                  <code style={{ color: "#82889c", fontSize: 11 }}>{r.location.replace(/^D:\\/, "")}</code>
                </div>
              ))}
            </div>
          </div>
          <div className="card">
            <div className="card-heading">
              <div>
                <div className="eyebrow"><Clock size={11} /> docs</div>
                <h2>{docs.length} living docs</h2>
              </div>
            </div>
            <div style={{ display: "grid", gap: 6 }}>
              {docs.slice(0, 5).map((r) => (
                <div key={r.title} style={{ fontSize: 12, color: "#c4c2e9" }}>{r.title}</div>
              ))}
            </div>
          </div>
          <div className="card">
            <div className="card-heading">
              <div>
                <div className="eyebrow"><Clock size={11} /> API</div>
                <h2>{endpoints.length} endpoints</h2>
              </div>
            </div>
            <div style={{ display: "grid", gap: 6 }}>
              {endpoints.slice(0, 5).map((r) => (
                <button
                  key={r.title}
                  onClick={() => r.href && openExternal(r.href)}
                  className="button quiet tiny"
                  style={{ justifyContent: "space-between", width: "100%" }}
                >
                  {r.title}
                  <ExternalLink size={10} />
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
