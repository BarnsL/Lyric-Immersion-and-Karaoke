import { useEffect, useState } from "react";
import {
  Activity,
  Boxes,
  Cog,
  GitBranch,
  LayoutDashboard,
  Library,
  RefreshCw,
  Workflow,
  ExternalLink,
} from "lucide-react";
import { getDiag, getHealth, getStatus, openExternal } from "./api";
import { Overview } from "./components/Overview";
import { DiagramView } from "./components/Diagram";
import { Parameters } from "./components/Parameters";
import { AutoResearch } from "./components/AutoResearch";
import { Resources } from "./components/Resources";
import { RESOURCES } from "./manifest";
import type { DiagPayload, Health, StatusPayload, ViewKey } from "./models";

const NAV: { key: ViewKey; label: string; icon: JSX.Element }[] = [
  { key: "overview",     label: "Overview",      icon: <LayoutDashboard size={16} /> },
  { key: "diagram",      label: "Runtime map",   icon: <Workflow size={16} /> },
  { key: "parameters",   label: "Parameters",    icon: <Cog size={16} /> },
  { key: "autoresearch", label: "AutoResearch",  icon: <GitBranch size={16} /> },
  { key: "resources",    label: "Resources",     icon: <Library size={16} /> },
];

export function App() {
  const [view, setView] = useState<ViewKey>("overview");
  const [health, setHealth] = useState<Health | null>(null);
  const [status, setStatus] = useState<StatusPayload | null>(null);
  const [diag, setDiag] = useState<DiagPayload | null>(null);
  const [appOnline, setAppOnline] = useState<boolean | null>(null);
  const [ticker, setTicker] = useState(0);

  useEffect(() => {
    let cancelled = false;
    // Only pull /diag while the Overview tab is visible — no reason to burn
    // cycles polling the sync FSM while the user is on Resources.
    const pollDiag = view === "overview";
    async function tick() {
      try {
        const h = await getHealth();
        if (!cancelled) { setHealth(h); setAppOnline(true); }
      } catch { if (!cancelled) { setAppOnline(false); setHealth(null); setDiag(null); return; } }
      try {
        const s = await getStatus();
        if (!cancelled) setStatus(s);
      } catch { if (!cancelled) setStatus(null); }
      if (pollDiag) {
        try {
          const d = await getDiag();
          if (!cancelled) setDiag(d);
        } catch { if (!cancelled) setDiag(null); }
      }
    }
    tick();
    const id = window.setInterval(tick, 2500);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [ticker, view]);

  const dot =
    appOnline === true ? "live" :
    appOnline === false ? "off" : "";

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><Boxes size={17} /></div>
          <div>Lyric&nbsp;Immersion <em>·</em><br />Dev Console</div>
        </div>
        <nav>
          {NAV.map((n) => (
            <button
              key={n.key}
              className={view === n.key ? "active" : ""}
              onClick={() => setView(n.key)}
            >
              {n.icon}
              {n.label}
              {n.key === "resources" && (
                <span className="count">{RESOURCES.length}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <p>Runtime</p>
          <div className="kv">
            <span className={`status-dot ${dot}`} />
            <span>
              {appOnline === null ? "checking…" :
               appOnline ? <>online · <strong>v{health?.version ?? "?"}</strong></> :
               "app not running"}
            </span>
          </div>
          <div className="kv" style={{ marginTop: 6 }}>
            <Activity size={12} />
            <span>
              {status?.title
                ? <>now: <strong>{status.title}</strong></>
                : "no track"}
            </span>
          </div>
          <div className="kv" style={{ marginTop: 12 }}>
            <button className="button quiet tiny" onClick={() => setTicker(t => t + 1)} title="refresh">
              <RefreshCw size={11} /> refresh
            </button>
            <button className="button quiet tiny" onClick={() => openExternal("http://127.0.0.1:8765/")} title="open API root">
              <ExternalLink size={11} /> API
            </button>
          </div>
        </div>
      </aside>

      <main className="workspace">
        {view === "overview"     && <Overview health={health} status={status} diag={diag} online={appOnline} />}
        {view === "diagram"      && <DiagramView />}
        {view === "parameters"   && <Parameters online={appOnline} />}
        {view === "autoresearch" && <AutoResearch />}
        {view === "resources"    && <Resources />}
      </main>
    </div>
  );
}
