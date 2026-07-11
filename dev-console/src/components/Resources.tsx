import { useMemo, useState } from "react";
import { ExternalLink, FolderOpen, Library, Search } from "lucide-react";
import { openExternal } from "../api";
import { RESOURCES } from "../manifest";
import type { Resource } from "../models";

const KIND_LABEL: Record<Resource["kind"], string> = {
  worktree: "worktree",
  doc: "doc",
  corpus: "corpus",
  external: "tool",
  "external-doc": "web",
  "app-endpoint": "endpoint",
  sibling: "sibling",
};

const FILTERS: (Resource["kind"] | "all")[] = ["all", "worktree", "doc", "corpus", "app-endpoint", "sibling", "external", "external-doc"];

export function Resources() {
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<Resource["kind"] | "all">("all");

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return RESOURCES.filter((r) => {
      if (kind !== "all" && r.kind !== kind) return false;
      if (!needle) return true;
      return (
        r.title.toLowerCase().includes(needle) ||
        r.location.toLowerCase().includes(needle) ||
        r.detail.toLowerCase().includes(needle)
      );
    });
  }, [q, kind]);

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: RESOURCES.length };
    for (const r of RESOURCES) c[r.kind] = (c[r.kind] ?? 0) + 1;
    return c;
  }, []);

  return (
    <>
      <div className="topbar">
        <div>
          <div className="eyebrow"><Library size={11} /> resources</div>
          <h1>Everything the app depends on</h1>
          <p>Worktrees, docs, corpora, sibling projects, external tools, and localhost API endpoints — one click each.</p>
        </div>
      </div>

      <div className="params-toolbar">
        <div style={{ position: "relative" }}>
          <Search size={13} style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", color: "#82889c" }} />
          <input
            type="search"
            placeholder="filter…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            style={{ paddingLeft: 30 }}
          />
        </div>
        {FILTERS.map((k) => (
          <button
            key={k}
            className={`button tiny ${kind === k ? "primary" : "quiet"}`}
            onClick={() => setKind(k)}
          >
            {k === "all" ? "All" : KIND_LABEL[k as Resource["kind"]]}
            <span style={{ marginLeft: 4, opacity: .7 }}>{counts[k] ?? 0}</span>
          </button>
        ))}
      </div>

      <div className="resource-grid">
        {filtered.map((r) => (
          <div key={r.title + r.location} className="resource-card">
            <div className="row">
              <strong>{r.title}</strong>
              <span className="kind">{KIND_LABEL[r.kind]}</span>
            </div>
            <small>{r.location}</small>
            <p>{r.detail}</p>
            <div className="actions">
              {r.href && (
                <button className="button quiet tiny" onClick={() => openExternal(r.href!)}>
                  Open <ExternalLink size={11} />
                </button>
              )}
              {r.path && (
                <button className="button quiet tiny" onClick={() => openExternal(r.path!)}>
                  Explorer <FolderOpen size={11} />
                </button>
              )}
            </div>
          </div>
        ))}
        {filtered.length === 0 && (
          <div className="empty">No resources match that filter.</div>
        )}
      </div>
    </>
  );
}
