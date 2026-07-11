import { DIAGRAM_EDGES, DIAGRAM_NODES } from "../manifest";
import { openExternal } from "../api";
import { ExternalLink, Workflow } from "lucide-react";

// ─── Layout constants ───────────────────────────────────────────────────────
// Column-driven layout: node X depends on its col, Y on its row within that
// column. Column widths and gaps are hand-tuned to match the mermaid graph in
// docs/REPO_ORGANIZATION.md without visual crowding.
const NODE_W = 158;
const NODE_H = 46;
const COL_GAP = 40;
const ROW_GAP = 22;
const PAD = 30;

const COLS = 8;                                        // 0..7 in the manifest

function nodePos(col: number, row: number) {
  const rowsInCol = Math.max(1, Math.max(...DIAGRAM_NODES.filter((n) => n.col === col).map((n) => n.row)) + 1);
  const maxRows = Math.max(...DIAGRAM_NODES.map((n) => n.row)) + 1;
  const totalHeight = maxRows * NODE_H + (maxRows - 1) * ROW_GAP;
  const colHeight = rowsInCol * NODE_H + (rowsInCol - 1) * ROW_GAP;
  const yOffset = (totalHeight - colHeight) / 2;       // vertically center each column
  return {
    x: PAD + col * (NODE_W + COL_GAP),
    y: PAD + yOffset + row * (NODE_H + ROW_GAP),
  };
}

export function DiagramView() {
  const maxRows = Math.max(...DIAGRAM_NODES.map((n) => n.row)) + 1;
  const width = PAD * 2 + COLS * NODE_W + (COLS - 1) * COL_GAP;
  const height = PAD * 2 + maxRows * NODE_H + (maxRows - 1) * ROW_GAP;

  const nodeById = new Map(DIAGRAM_NODES.map((n) => [n.id, n] as const));

  return (
    <>
      <div className="topbar">
        <div>
          <div className="eyebrow"><Workflow size={11} /> runtime map</div>
          <h1>How a track becomes an on-screen line</h1>
          <p>Mirrors the mermaid flow in <code>docs/REPO_ORGANIZATION.md</code>. Every column is a stage in the pipeline; each box is a real module.</p>
        </div>
        <button
          className="button quiet"
          onClick={() => openExternal("D:\\Desktop-Karaoke\\docs\\REPO_ORGANIZATION.md")}
        >
          Open source doc <ExternalLink size={13} />
        </button>
      </div>

      <div className="card">
        <div className="diagram-wrap">
          <svg
            className="diagram-svg"
            viewBox={`0 0 ${width} ${height}`}
            width={width}
            height={height}
            role="img"
            aria-label="Lyric Immersion runtime pipeline"
          >
            <defs>
              <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                <path d="M 0 0 L 10 5 L 0 10 z" fill="#5a6180" />
              </marker>
            </defs>

            {/* Edges */}
            {DIAGRAM_EDGES.map((e, i) => {
              const a = nodeById.get(e.from);
              const b = nodeById.get(e.to);
              if (!a || !b) return null;
              const p1 = nodePos(a.col, a.row);
              const p2 = nodePos(b.col, b.row);
              // Anchor on the right edge of `a` and the left edge of `b` for
              // forward flows; for the same-column feedback edges (cache→overlay)
              // we route with a horizontal bend at midpoint.
              const x1 = p1.x + NODE_W;
              const y1 = p1.y + NODE_H / 2;
              const x2 = p2.x;
              const y2 = p2.y + NODE_H / 2;
              const mid = (x1 + x2) / 2;
              const path = `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`;
              return (
                <path
                  key={i}
                  d={path}
                  className={`edge${e.dashed ? " dashed" : ""}`}
                  markerEnd="url(#arrow)"
                />
              );
            })}

            {/* Nodes */}
            {DIAGRAM_NODES.map((n) => {
              const { x, y } = nodePos(n.col, n.row);
              return (
                <g key={n.id} transform={`translate(${x} ${y})`}>
                  <rect className={`node-rect ${n.kind}`} width={NODE_W} height={NODE_H} />
                  <text className="node-label" x={NODE_W / 2} y={19} textAnchor="middle">
                    {n.label}
                  </text>
                  {n.sub && (
                    <text className="node-sub" x={NODE_W / 2} y={34} textAnchor="middle">
                      {n.sub}
                    </text>
                  )}
                </g>
              );
            })}
          </svg>
        </div>
      </div>

      <div className="grid-3" style={{ marginTop: 16 }}>
        <div className="card">
          <div className="eyebrow">reading order</div>
          <h3>Sound is the authority</h3>
          <p>The SMTC title is a hint. Sound (Shazam + energy + Whisper) confirms or overrides.
             `confidence.py` fuses the signals into a TRUST · CAUTION · SWITCH · REGEN state.</p>
        </div>
        <div className="card">
          <div className="eyebrow">source priority</div>
          <h3>Captions &gt; provider &gt; generate</h3>
          <p>YouTube captions ride the video's own timing (highest trust). Provider LRC is
             next, verified by <code>verify_lrc</code>. Whisper generation is the last resort.</p>
        </div>
        <div className="card">
          <div className="eyebrow">rendering</div>
          <h3>Tk fallback, Tauri GPU</h3>
          <p>The Tk canvas always renders. The Tauri overlay child polls <code>/overlay</code>
             for per-pixel-alpha click-through GPU rendering, and Tk hides once it's proven live.</p>
        </div>
      </div>
    </>
  );
}
