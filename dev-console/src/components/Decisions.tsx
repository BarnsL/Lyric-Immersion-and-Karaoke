import { Ban, Brain, CheckCircle2, CircleSlash, GitBranch, History, Radio, ShieldAlert, Timer, XCircle } from "lucide-react";
import { NowPane } from "./Now";
import type { GateSnapshot, InsightPayload } from "../models";

interface Props {
  insight: InsightPayload | null;
  online: boolean | null;
}

type Verdict = "pass" | "fail" | "block" | "skip";

interface Step {
  id: string;
  label: string;
  test: string;          // the condition, in the code's own terms
  actual: string;        // what the live numbers are
  verdict: Verdict;
  note?: string;         // why this gate exists / why it fired
}

/**
 * Replay the decide-by-ear ladder from the live snapshot.
 *
 * This mirrors main.py `_decide_by_ear` in order, so the console shows the gates
 * in the sequence the code actually evaluates them. Every threshold comes from the
 * payload (the server computes the EFFECTIVE values, including the title-lock bump)
 * rather than being duplicated here — a diagram that drifts from the code is worse
 * than no diagram.
 */
function ladder(g: GateSnapshot): Step[] {
  const steps: Step[] = [];
  const switched = g.outcome === "switch";

  steps.push({
    id: "heard",
    label: "Transcript length",
    test: "heard >= 20 chars",
    actual: `${g.heard_chars} chars`,
    verdict: g.short_transcript ? (g.short_decisive ? "pass" : "fail") : "pass",
    note: g.short_transcript
      ? (g.short_decisive
        ? "Short, but the scores are not close — a decisive read is allowed to act."
        : "Short AND the scores are close: a near-tie on few characters is the classic false switch.")
      : undefined,
  });
  if (g.short_transcript && !g.short_decisive) return steps;

  steps.push({
    id: "scores",
    label: "Candidate vs loaded",
    test: "best beats the loaded body",
    actual: `best ${g.best} · loaded ${g.loaded} (${g.best_key})`,
    verdict: g.best > g.loaded ? "pass" : "fail",
  });

  steps.push({
    id: "worthless",
    label: "Is the loaded body worth protecting?",
    test: "lines >= 8 or score > 8",
    actual: `${g.loaded_lines} lines, scores ${g.loaded}`,
    verdict: g.loaded_worthless ? "skip" : "pass",
    note: g.loaded_worthless
      ? "Stub body — the title-lock bump and cross-artist block are LIFTED. You cannot protect a right song you don't have."
      : "Real body, so the protections below apply.",
  });

  steps.push({
    id: "cross",
    label: "Cross-artist block",
    test: "candidate's artist agrees with the media session",
    actual: g.block_cross_artist ? "artists disagree on a title-locked song" : "no conflict",
    verdict: g.block_cross_artist ? "block" : "pass",
    note: g.block_cross_artist
      ? "A transcript-only match to a different artist is almost always a hallucination on a quiet section."
      : undefined,
  });

  steps.push({
    id: "min",
    label: "Absolute score bar",
    test: `best >= ${g.min_required}`,
    actual: `${g.best} vs ${g.min_required}${g.title_locked && !g.loaded_worthless ? "  (+15 title-lock)" : ""}`,
    verdict: g.best >= g.min_required ? "pass" : "fail",
    note: g.expanded ? "Library-wide search, so the bar is higher than a title-confined check." : undefined,
  });

  steps.push({
    id: "margin",
    label: "Margin over the loaded body",
    test: `best - loaded >= ${g.margin_required}`,
    actual: `${g.margin_actual} vs ${g.margin_required}`,
    verdict: g.margin_actual >= g.margin_required ? "pass" : "fail",
  });

  if (!(g.best >= g.min_required && g.margin_actual >= g.margin_required)) {
    steps.push({
      id: "lopsided",
      label: "Lopsided override",
      test: `loaded < 32 and margin >= 3x (${(g.margin_required * 3).toFixed(0)})`,
      actual: `${g.margin_actual} vs ${(g.margin_required * 3).toFixed(0)}`,
      verdict: g.lopsided ? "pass" : "fail",
      note: "Rescues a clear win that lost to an absolute threshold by a point or two.",
    });
  }

  steps.push({
    id: "outcome",
    label: switched ? "SWITCH" : "Hold the loaded body",
    test: "final",
    actual: g.outcome,
    verdict: switched ? "pass" : "fail",
  });
  return steps;
}


/**
 * The CONCERT identification path: how a live video decides which song is playing.
 * Distinct from decide-by-ear — a concert has no single track to verify against,
 * so it leans on the setlist and the on-screen banner first.
 */
function concertLadder(ins: InsightPayload): Step[] {
  const live = ins.sources.mode.live;
  const sl = ins.setlist;
  const ocr = ins.ocr;
  const steps: Step[] = [];

  steps.push({
    id: "mode", label: "Live / concert mode",
    test: "video is a concert or compilation",
    actual: live ? "live" : "single track",
    verdict: live ? "pass" : "skip",
    note: live ? undefined : "Not a concert — the single-track path runs instead.",
  });
  if (!live) return steps;

  steps.push({
    id: "setlist", label: "Setlist parsed",
    test: "chapters or description candidates",
    actual: sl.candidates.length
      ? `${sl.candidates.length} songs (${sl.candidates.filter((c) => c.cached).length} cached)`
      : sl.chapters.length ? `${sl.chapters.length} chapters` : "none",
    verdict: (sl.candidates.length || sl.chapters.length) ? "pass" : "fail",
    note: (sl.candidates.length || sl.chapters.length)
      ? "The setlist becomes the candidate pool AND the OCR whitelist."
      : "Without a setlist the app falls back to the banner reader and by-ear listening, matching against the WHOLE library — which is how page text can be mistaken for a song.",
  });

  steps.push({
    id: "gate", label: "OCR setlist gate",
    test: "banner matched only against the setlist",
    actual: sl.gate_on && sl.candidates.length ? "on" : "off — whole library",
    verdict: sl.gate_on && sl.candidates.length ? "pass" : "skip",
    note: sl.gate_on && sl.candidates.length
      ? undefined
      : "Wide pool: arbitrary screen text (search box, ads) can score a match.",
  });

  steps.push({
    id: "banner", label: "Banner read",
    test: `score >= ${ocr ? ocr.accept_at.toFixed(2) : "0.85"}`,
    actual: ocr
      ? (ocr.matched ? `${ocr.matched.title} @ ${ocr.matched.score.toFixed(2)}` : `nothing cleared it (${ocr.lines.length} lines seen)`)
      : "no pass yet",
    verdict: ocr?.matched ? "pass" : "fail",
  });

  const drops = ins.ocr_drops.length;
  if (drops) {
    steps.push({
      id: "drops", label: "Refused screen text",
      test: "chrome / off-setlist / unconfirmed",
      actual: `${drops} string${drops === 1 ? "" : "s"} rejected`,
      verdict: "skip",
      note: "See the Song finder view for exactly what was refused and why.",
    });
  }

  steps.push({
    id: "cid", label: ocr?.matched ? "Song identified" : "Falls through to sound",
    test: "final",
    actual: ocr?.matched ? "banner wins" : "Shazam / by-ear decides",
    verdict: ocr?.matched ? "pass" : "fail",
  });
  return steps;
}

/** The LIVE sync path — a different machine from studio, and the one concerts use. */
function syncLadder(ins: InsightPayload): Step[] {
  const s = ins.sync;
  if (!s) return [];
  const steps: Step[] = [];
  const drift = s.drift;

  steps.push({
    id: "arr", label: "Arrangement",
    test: "live cut vs studio master",
    actual: s.live ? "live / concert" : "studio",
    verdict: "pass",
    note: s.live
      ? "Live uses its own thresholds: corrections are verified differently because a repeated chorus can be read at the wrong offset twice."
      : undefined,
  });

  if (s.caption_timed) {
    steps.push({
      id: "cap", label: "Video-locked timing",
      test: "body came from this video's captions",
      actual: "caption-timed",
      verdict: "skip",
      note: "The energy correlator stands down — the timing already comes from the video itself.",
    });
  }

  steps.push({
    id: "measure", label: "Drift measured",
    test: `|drift| <= ${s.ok_drift}s counts as in sync`,
    actual: drift === null || drift === undefined ? "no read yet" : `${drift.toFixed(2)}s`,
    verdict: drift === null || drift === undefined ? "skip"
      : Math.abs(drift) <= s.ok_drift ? "pass" : "fail",
    note: `Tier verifies every ${s.tier_interval_s}s.`,
  });

  steps.push({
    id: "deadband", label: "Correction deadband",
    test: `|correction| >= ${s.apply_min_s}s`,
    actual: drift === null || drift === undefined ? "—" : `${Math.abs(drift).toFixed(2)}s`,
    verdict: drift !== null && drift !== undefined && Math.abs(drift) >= s.apply_min_s ? "pass" : "skip",
    note: "Below this a correction is discarded as wobble.",
  });

  steps.push({
    id: "commit", label: "How it may commit",
    test: `single read allowed up to ${s.single_shot_max}s`,
    actual: s.single_shot_max === 0
      ? "NEVER single-shot — always needs 2 agreeing reads"
      : `<= ${s.single_shot_max}s on one read, bigger needs 2 (${s.tpvr_gap_s}s apart)`,
    verdict: s.single_shot_max === 0 ? "fail" : "pass",
    note: s.single_shot_max === 0
      ? "In a concert the song can change before a pair completes, so the track may never lock at all."
      : undefined,
  });

  if (s.held) {
    steps.push({
      id: "held", label: "First read held",
      test: "awaiting confirmation",
      actual: `confirming in ~${s.tpvr_gap_s}s`,
      verdict: "skip",
    });
  }

  steps.push({
    id: "state", label: "Offset",
    test: "applied",
    actual: `${s.offset >= 0 ? "+" : ""}${s.offset.toFixed(2)}s${s.pending !== null && s.pending !== undefined ? `  (pending ${s.pending.toFixed(2)}s)` : ""}`,
    verdict: "pass",
    note: s.fail_streak ? `${s.fail_streak} consecutive reads found no anchor — 3 rejects the body as the wrong song.` : undefined,
  });
  return steps;
}

const ICON: Record<Verdict, JSX.Element> = {
  pass:  <CheckCircle2 size={14} />,
  fail:  <XCircle size={14} />,
  block: <Ban size={14} />,
  skip:  <CircleSlash size={14} />,
};

function GateLadder({ steps }: { steps: Step[] }) {
  // Everything up to and including the first non-pass is the path that actually
  // executed; later steps were never reached.
  const stopAt = steps.findIndex((s) => s.verdict === "fail" || s.verdict === "block");
  const liveTo = stopAt === -1 ? steps.length - 1 : stopAt;
  return (
    <div className="tree">
      {steps.map((s, i) => {
        const reached = i <= liveTo;
        return (
          <div key={s.id} className={`tree-step v-${s.verdict}${reached ? " live" : " unreached"}`}>
            {i > 0 && <div className={`tree-link${reached ? " live" : ""}`} />}
            <div className="tree-node">
              <div className="tree-node-head">
                <span className="tree-verdict">{ICON[s.verdict]}</span>
                <strong>{s.label}</strong>
                {!reached && <span className="pill">not reached</span>}
              </div>
              <div className="tree-node-body">
                <code>{s.test}</code>
                <span className="tree-actual">{s.actual}</span>
              </div>
              {s.note && reached && <p className="tree-note">{s.note}</p>}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function EnginePane({ insight }: { insight: InsightPayload }) {
  const d = insight.decision;
  if (!d || !d.state) return null;
  const dims = Object.entries(d.dims ?? {});
  const tone = d.state === "TRUST" ? "ok" : d.state === "REGEN" ? "warn" : "";
  return (
    <div className="card">
      <div className="card-heading"><Brain size={15} /> Decision engine</div>
      <div className="stat-row">
        <span>State</span>
        <span><span className={`pill ${tone}`}>{d.state}</span> <span className="pill">{d.strikes} strikes</span></span>
      </div>
      {dims.length > 0 && (
        <table className="diag-table">
          <tbody>
            {dims.map(([k, v]) => (
              <tr key={k}>
                <td style={{ width: "55%" }}>{k.replace(/_/g, " ")}</td>
                <td><span className={`pill ${v === "OK" ? "ok" : "warn"}`}>{v}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="tree-note">
        Strikes accumulate from the four dimensions above and escalate
        TRUST → CAUTION → SWITCH → REGEN. Many escalations are then suppressed
        (drift-only, captions, cover) — the suppression is why a SWITCH in the log
        often does not change the song.
      </p>
    </div>
  );
}

export function Decisions({ insight, online }: Props) {
  if (online === false) return <p className="empty">App offline.</p>;
  if (!insight) return <p className="empty">Waiting for /insight…</p>;
  const g = insight.gates;
  return (
    <div className="page-grid">
      <div className="hero">
        <p className="eyebrow"><GitBranch size={13} /> DECISIONS</p>
        <h1>Why it kept or changed the song</h1>
        <p>
          Each ladder is evaluated in the order the code evaluates it, with live numbers. The
          lit path is what actually ran; greyed steps were never reached.
        </p>
      </div>

      <NowPane insight={insight} />

      {/* Sync runs continuously, so it leads: it always has something to show. The
          gate ladder below only fires at a song boundary and would otherwise leave
          the top of this view blank during ordinary playback. */}
      <div className="grid-2">
        <div className="card">
          <div className="card-heading">
            <Timer size={15} /> Sync
            <span className={insight.sync?.live ? "pill ok" : "pill"}>
              {insight.sync?.profile ?? "studio"} thresholds
            </span>
          </div>
          {insight.sync
            ? <GateLadder steps={syncLadder(insight)} />
            : <p className="empty">No sync state.</p>}
        </div>
        <div className="card">
          <div className="card-heading">
            <Radio size={15} /> Concert identification
            {insight.sources.mode.live && <span className="pill ok">live</span>}
          </div>
          <GateLadder steps={concertLadder(insight)} />
        </div>
      </div>

      <div className={`card${g?.stale ? " stale-block" : ""}`}>
        <div className="card-heading">
          <ShieldAlert size={15} /> Gate ladder — decide by ear
          {g && <span className={`pill ${g.outcome === "switch" ? "ok" : "warn"}`}>{g.outcome}</span>}
          {g?.stale && (
            <span className="pill warn" title="The track changed after this decision — it describes an earlier song, not the one playing now.">
              <History size={11} /> previous track
            </span>
          )}
        </div>
        {!g ? (
          <p className="empty">
            Hasn't run yet this session — which is normal, and not a fault.
            <small>
              Deciding by ear means transcribing the audio with Whisper and scoring it against
              candidate bodies. That is expensive, so it is deliberately rare: it runs at a
              song boundary in a concert, on a late load, when the decision engine escalates,
              or when you press “Wrong lyrics”. Ordinary playback of a correctly identified
              track never needs it. When it does run, the full arithmetic appears here and
              stays until the next one.
            </small>
          </p>
        ) : (
          <>
            {g.stale && g.track && (
              <p className="tree-note" style={{ marginTop: 0 }}>
                Decided while <strong>{g.track}</strong> was playing. Kept because it usually
                explains what is loaded now.
              </p>
            )}
            <GateLadder steps={ladder(g)} />
          </>
        )}
      </div>

      <EnginePane insight={insight} />
    </div>
  );
}
