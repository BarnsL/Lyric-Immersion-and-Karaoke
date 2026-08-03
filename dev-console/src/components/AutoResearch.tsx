import { AlertTriangle, CheckCircle2, Copy, ExternalLink, GitBranch, ShieldCheck } from "lucide-react";
import type { InsightPayload } from "../models";
import { AUTORESEARCH } from "../manifest";
import { openExternal } from "../api";
import { useState } from "react";

function CopyBtn({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="button quiet tiny"
      onClick={async () => {
        try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1200); } catch {}
      }}
      title="copy to clipboard"
    >
      <Copy size={11} /> {copied ? "copied" : "copy"}
    </button>
  );
}

/**
 * Live status, not documentation. This page previously described the loop as if it
 * were running; checked against the actual worktree it had ZERO experiment commits
 * and sat 0 commits ahead of master, and the skill it tells you to install was not
 * installed. A console that reports aspiration as working state is worse than one
 * that reports nothing.
 */
function LoopStatus({ insight }: { insight: InsightPayload | null }) {
  const ar = insight?.autoresearch;
  if (!ar) return null;
  const everRun = ar.experiments > 0;
  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="card-heading">
        {everRun ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}
        Loop status
        <span className={everRun ? "pill ok" : "pill warn"}>
          {everRun ? `${ar.experiments} experiment commits` : "has never run"}
        </span>
      </div>
      <div className="stat-row">
        <span>Worktree</span>
        <span>
          <code>{ar.worktree ?? "—"}</code>{" "}
          <span className={ar.exists ? "pill ok" : "pill warn"}>{ar.exists ? "present" : "missing"}</span>
        </span>
      </div>
      <div className="stat-row">
        <span>Skill installed</span>
        <span className={ar.skill_installed ? "pill ok" : "pill warn"}>
          {ar.skill_installed ? "yes" : "no — /autoresearch is unavailable"}
        </span>
      </div>
      <div className="stat-row">
        <span>Ahead of master</span>
        <span>
          <strong>{ar.ahead_of_master}</strong> commit{ar.ahead_of_master === 1 ? "" : "s"}
          {ar.ahead_of_master === 0 && (
            <span className="pill" title="Nothing on the branch that master doesn't already have — it is a stale snapshot, not work in progress.">
              stale snapshot
            </span>
          )}
        </span>
      </div>
      {ar.last_commit && (
        <div className="stat-row"><span>Last commit</span><span><code>{ar.last_commit}</code></span></div>
      )}
      {!everRun && (
        <p className="tree-note">
          The branch exists but contains no <code>experiment:</code> commits, so the loop
          described below has not actually been run.
          {!ar.skill_installed && <> Install the skill first — the <code>/autoresearch</code> command
          does not exist until then.</>}
        </p>
      )}
    </div>
  );
}

export function AutoResearch({ insight }: { insight?: InsightPayload | null }) {
  return (
    <>
      <div className="topbar">
        <div>
          <div className="eyebrow"><GitBranch size={11} /> uditgoenka/autoresearch</div>
          <h1>AutoResearch loop</h1>
          <p>Iterative <code>experiment:</code>-commit loop targeting concert scoring, isolated in its own worktree so it never touches <code>master</code>.</p>
        </div>
        <button className="button quiet" onClick={() => openExternal("https://github.com/uditgoenka/autoresearch")}>
          Skill on GitHub <ExternalLink size={13} />
        </button>
      </div>

      <LoopStatus insight={insight ?? null} />

      <div className="ar-hero">
        <div>
          <h2>One worktree per active research question</h2>
          <p>
            Every iteration commits <em>into</em> the isolated branch. You inspect winning diffs by hand, then port them onto
            <code> master</code> as a normal commit. Never merge <code>autoresearch</code> into <code>master</code> directly —
            the churn ratio is intentionally high.
          </p>
        </div>
        <div className="kv">
          <div>Worktree · <strong>{AUTORESEARCH.worktreePath}</strong></div>
          <div>Branch · <strong>{AUTORESEARCH.branch}</strong></div>
          <div>Install once · <strong>{AUTORESEARCH.installCmd}</strong></div>
          <div style={{ color: "#f0c782", fontSize: 11 }}>{AUTORESEARCH.restartHint}</div>
        </div>
      </div>

      <div className="grid-2" style={{ marginTop: 16 }}>
        <div className="card">
          <div className="card-heading">
            <div>
              <div className="eyebrow">phase 1</div>
              <h2>Prompt template</h2>
            </div>
            <CopyBtn text={AUTORESEARCH.loopTemplate} />
          </div>
          <div className="code-block">{AUTORESEARCH.loopTemplate}</div>
          <p style={{ marginTop: 10 }}>
            Paste this into an interactive Claude Code session opened at the worktree root. AutoResearch spins
            50 iterations, each with a small edit → guard → verify → commit cycle.
          </p>
        </div>

        <div className="card">
          <div className="card-heading">
            <div>
              <div className="eyebrow"><ShieldCheck size={11} /> phase 2</div>
              <h2>Safety notes</h2>
            </div>
          </div>
          <ul style={{ margin: 0, paddingLeft: 18, color: "#c4c2e9", fontSize: 13, lineHeight: 1.7 }}>
            {AUTORESEARCH.safety.map((s) => <li key={s}>{s}</li>)}
          </ul>
          <h3 style={{ marginTop: 16 }}>Env overrides (when a hook mis-blocks)</h3>
          <div className="code-block">
            {AUTORESEARCH.envOverrides.map(([k, v]) => `$env:${k} = ${v}`).join("\n")}
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-heading">
          <div>
            <div className="eyebrow">jump to</div>
            <h2>Worktree + reference material</h2>
          </div>
        </div>
        <div className="grid-3">
          <button className="button quiet" onClick={() => openExternal(AUTORESEARCH.worktreePath)}>
            Open worktree in Explorer <ExternalLink size={12} />
          </button>
          <button className="button quiet" onClick={() => openExternal("D:\\Lyric-Immersion-AR\\AR_README.md")}>
            AR_README.md <ExternalLink size={12} />
          </button>
          <button className="button quiet" onClick={() => openExternal("D:\\Desktop-Karaoke\\docs\\CONCERT_RESEARCH.md")}>
            CONCERT_RESEARCH.md §6 <ExternalLink size={12} />
          </button>
        </div>
      </div>
    </>
  );
}
