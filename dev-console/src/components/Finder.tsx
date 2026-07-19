import { AlertTriangle, Eye, EyeOff, ListMusic, Radio, ScanLine, Target } from "lucide-react";
import { NowPane } from "./Now";
import type { InsightPayload, OcrDropReason } from "../models";

interface Props {
  insight: InsightPayload | null;
  online: boolean | null;
}

const DROP_COPY: Record<OcrDropReason, { label: string; why: string }> = {
  "window-chrome":    { label: "window chrome",  why: "matches an open window/tab title — it's the browser, not a banner" },
  "not-on-setlist":   { label: "not on setlist", why: "this video's setlist doesn't contain it — screen furniture (ads, search box, page copy)" },
  "awaiting-2nd-read":{ label: "needs 2nd read", why: "held until it reads identically again — a real banner persists, one-off junk doesn't" },
};

function ago(t?: number) {
  if (!t) return "—";
  const d = Date.now() / 1000 - t;
  if (d < 1) return "now";
  if (d < 60) return `${Math.round(d)}s ago`;
  return `${Math.round(d / 60)}m ago`;
}

/** What the banner reader literally saw this pass, and the verdict on each line. */
function OcrPane({ insight }: { insight: InsightPayload }) {
  const ocr = insight.ocr;
  const drops = insight.ocr_drops ?? [];
  // A dropped string may also appear in `lines`; index the verdicts so every raw
  // line can be labelled rather than shown twice.
  const verdict = new Map<string, OcrDropReason>();
  for (const d of drops) verdict.set(d.text, d.reason);

  return (
    <div className="card">
      <div className="card-heading">
        <ScanLine size={15} /> What the screen reader sees
        {ocr && <span className="pill">{ago(ocr.t)}</span>}
      </div>

      {!ocr ? (
        <p className="empty">
          Not running for this video — which is normal.
          <small>
            The banner reader only wakes up in live/concert mode, and stays off for ordinary
            tracks, non-music pages, and any concert that ships chapters (a parsed setlist is
            exact, so there is nothing to read). Identification is coming from the sources
            below instead. This panel is cleared whenever the track changes, so it never shows
            you a previous video's reads.
          </small>
        </p>
      ) : (
        <>
          <div className="stat-row">
            <span>Matched against</span>
            <span>
              <strong>{ocr.pool_kind === "setlist" ? "this video's setlist" : "the whole library"}</strong>
              {" "}({ocr.pool_size} title{ocr.pool_size === 1 ? "" : "s"})
              {ocr.pool_kind === "library" && (
                <span className="pill warn" title="With no setlist the pool is every song you own, so arbitrary page text can eventually score a match.">
                  wide pool
                </span>
              )}
            </span>
          </div>

          {ocr.matched ? (
            <div className="stat-row">
              <span>Accepted</span>
              <span>
                <strong>{ocr.matched.title}</strong>{" "}
                <span className={ocr.matched.score >= ocr.accept_at ? "pill ok" : "pill warn"}>
                  {ocr.matched.score.toFixed(2)} / {ocr.accept_at.toFixed(2)}
                </span>
              </span>
            </div>
          ) : (
            <div className="stat-row"><span>Accepted</span><span className="empty">nothing cleared {ocr.accept_at.toFixed(2)}</span></div>
          )}

          {ocr.pending_2nd && (
            <div className="stat-row">
              <span>Held for a 2nd read</span>
              <span><code>{ocr.pending_2nd}</code></span>
            </div>
          )}

          <div className="card-heading" style={{ marginTop: 14 }}>
            <Eye size={14} /> Every line read this pass ({ocr.lines.length})
          </div>
          {ocr.lines.length === 0 ? (
            <p className="empty">The capture came back empty.</p>
          ) : (
            <table className="diag-table">
              <tbody>
                {ocr.lines.map((ln, i) => {
                  const v = verdict.get(ln);
                  const isMatch = ocr.matched && ln.includes(ocr.matched.title);
                  return (
                    <tr key={i}>
                      <td style={{ width: "62%" }}><code>{ln}</code></td>
                      <td>
                        {isMatch ? <span className="pill ok"><Target size={11} /> used as the song</span>
                          : v ? <span className="pill warn" title={DROP_COPY[v].why}><EyeOff size={11} /> {DROP_COPY[v].label}</span>
                          : <span className="pill">seen, unused</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </>
      )}

      {drops.length > 0 && (
        <>
          <div className="card-heading" style={{ marginTop: 14 }}>
            <AlertTriangle size={14} /> Refused strings ({drops.length})
          </div>
          <p className="empty" style={{ marginTop: -6 }}>
            Text the reader saw and deliberately did not use. The log rate-limits these to once
            per 10 minutes; this is the live truth.
          </p>
          <table className="diag-table">
            <tbody>
              {drops.slice().reverse().map((d, i) => (
                <tr key={i}>
                  <td style={{ width: "52%" }}><code>{d.text}</code></td>
                  <td title={DROP_COPY[d.reason]?.why}>
                    <span className="pill warn">{DROP_COPY[d.reason]?.label ?? d.reason}</span>
                  </td>
                  <td>×{d.n}</td>
                  <td>{ago(d.t)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

/** The setlist: the candidate pool, and which songs are already on disk. */
function SetlistPane({ insight }: { insight: InsightPayload }) {
  const { candidates, chapters, gate_on } = insight.setlist;
  const cachedN = candidates.filter((c) => c.cached).length;
  return (
    <div className="card">
      <div className="card-heading">
        <ListMusic size={15} /> Setlist
        {gate_on
          ? <span className="pill ok" title="OCR matches only against these songs, so page text can't be mistaken for a title.">gate on</span>
          : <span className="pill warn" title="OCR matches against the whole library — arbitrary screen text can score a match.">gate off</span>}
      </div>
      {candidates.length === 0 && chapters.length === 0 ? (
        <p className="empty">
          No setlist parsed for this video (no chapters, nothing usable in the description).
          Identification falls back to the banner reader and by-ear listening.
        </p>
      ) : (
        <>
          {candidates.length > 0 && (
            <>
              <div className="stat-row">
                <span>Candidates</span>
                <span><strong>{cachedN}</strong> of {candidates.length} already cached</span>
              </div>
              <table className="diag-table">
                <tbody>
                  {candidates.map((c, i) => (
                    <tr key={i}>
                      <td style={{ width: "62%" }}>{c.title}</td>
                      <td>{c.kind && <span className="pill">{c.kind}</span>}</td>
                      <td>
                        {c.cached
                          ? <span className="pill ok" title={c.file ?? ""}>cached</span>
                          : <span className="pill">not fetched</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          {chapters.length > 0 && (
            <>
              <div className="card-heading" style={{ marginTop: 14 }}>Chapters ({chapters.length})</div>
              <table className="diag-table">
                <tbody>
                  {chapters.map((c, i) => (
                    <tr key={i} className={insight.setlist.idx === i ? "ok" : undefined}>
                      <td style={{ width: 80 }}><code>{Math.floor(c.start / 60)}:{String(Math.floor(c.start % 60)).padStart(2, "0")}</code></td>
                      <td>{c.title}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </>
      )}
    </div>
  );
}

/** Every other signal that can name the song, and whether it's currently trusted. */
function SourcesPane({ insight }: { insight: InsightPayload }) {
  const s = insight.sources;
  const row = (label: string, value: React.ReactNode, extra?: React.ReactNode) => (
    <div className="stat-row"><span>{label}</span><span>{value} {extra}</span></div>
  );
  return (
    <div className="card">
      <div className="card-heading"><Radio size={15} /> Identification sources</div>
      {row("Media session (SMTC)",
        s.smtc.title ? <strong>{s.smtc.title}</strong> : <span className="empty">none</span>,
        s.smtc.artist ? <span className="pill">{s.smtc.artist}</span> : null)}
      {row("Heard by sound (Shazam)",
        s.shazam.heard ? <strong>{s.shazam.heard}</strong> : <span className="empty">nothing yet</span>,
        s.shazam.corroborated ? <span className="pill ok">corroborated</span> : null)}
      {row("Loaded body",
        s.loaded.title ? <strong>{s.loaded.title}</strong> : <span className="empty">none</span>,
        <>
          {s.loaded.source && <span className="pill">{s.loaded.source}</span>}
          <span className={s.loaded.lines > 0 && s.loaded.lines < 8 ? "pill warn" : "pill"}
                title={s.loaded.lines < 8 ? "A body this thin is treated as a stub: the guards that normally protect the loaded song are lifted." : ""}>
            {s.loaded.lines} lines
          </span>
        </>)}
      {row("Locks",
        <>
          <span className={s.locks.title_locked ? "pill ok" : "pill"}>title{s.locks.title_locked ? " locked" : " open"}</span>{" "}
          <span className={s.locks.verified ? "pill ok" : "pill"}>{s.locks.verified ? "verified" : "unverified"}</span>{" "}
          <span className={s.locks.body_word_verified ? "pill ok" : "pill"}>{s.locks.body_word_verified ? "words verified" : "words unchecked"}</span>
        </>)}
      {row("Mode",
        <>
          <span className={s.mode.live ? "pill ok" : "pill"}>{s.mode.live ? "live / concert" : "single track"}</span>{" "}
          {s.mode.subtitles && <span className="pill">subtitles</span>}{" "}
          {s.mode.non_music_page && <span className="pill warn" title="Song identification is disabled on this page.">non-music page</span>}
        </>)}
    </div>
  );
}

export function Finder({ insight, online }: Props) {
  if (online === false) return <p className="empty">App offline.</p>;
  if (!insight) return <p className="empty">Waiting for /insight…</p>;
  return (
    <div className="page-grid">
      <div className="hero">
        <p className="eyebrow"><Eye size={13} /> SONG FINDER</p>
        <h1>What it can see</h1>
        <p>
          Every signal that could name the playing song, and — for the screen reader — every
          string it read plus why each was kept or thrown away.
        </p>
      </div>
      <NowPane insight={insight} />
      <div className="grid-2">
        <SourcesPane insight={insight} />
        <SetlistPane insight={insight} />
      </div>
      <OcrPane insight={insight} />
    </div>
  );
}
