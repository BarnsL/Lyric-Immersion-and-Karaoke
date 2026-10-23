import {
  AlertTriangle, CheckCircle2, CircleSlash, Loader2, Music, Pause, Play, Radio,
  Search, ShieldQuestion,
} from "lucide-react";
import type { InsightPayload } from "../models";

/**
 * The live strip that sits at the top of Song finder and Decisions (TICKET-194).
 *
 * WHY: every other panel in those views renders an EVENT — a banner-OCR pass, a
 * decide-by-ear gate. Both only happen on concerts and song boundaries, so with a
 * normal track playing the views were empty (or, worse, showed a half-hour-old
 * concert's OCR as if it were current) and the console read as broken while the
 * app was working perfectly. This block is never empty while a track is loaded,
 * and the playhead moving is itself the proof that the console is live.
 */

function clock(s: number) {
  if (!isFinite(s) || s < 0) return "0:00";
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

/**
 * The confidence badge, driven by EVIDENCE — not by title agreement (TICKET-200).
 *
 * It used to read `now.agree`: does the loaded body's title match the player's?
 * That is circular. A body fetched by title search is filed under the title it
 * was searched with, so it agrees with itself no matter which song it actually
 * contains. The console therefore showed a confident green "identified" on a body
 * for an entirely different song, while the one row that told the truth ("words
 * unchecked", three panels down) was outvoted by the big green tick.
 *
 * These labels say what was CHECKED, so an unverified body can never look proven.
 */
const EVIDENCE = {
  library: {
    cls: "pill ok",
    icon: <CheckCircle2 size={11} />,
    label: "library body",
    why: "A bundled/curated body from your own library — authoritative, no guessing involved.",
  },
  words: {
    cls: "pill ok",
    icon: <CheckCircle2 size={11} />,
    label: "words verified",
    why: "The words actually being sung were transcribed and matched against this body. This is the strongest proof the app can obtain.",
  },
  timing: {
    cls: "pill warn",
    icon: <ShieldQuestion size={11} />,
    label: "timing only",
    why: "An energy or caption lock aligned this body to the audio, which proves WHEN the lines land — not that they are the right lines. The words have not been checked.",
  },
  title: {
    cls: "pill warn",
    icon: <ShieldQuestion size={11} />,
    label: "title only",
    why: "Nothing backs this body except the title that was searched for. If that search title was wrong, these are confidently the wrong lyrics — compare 'searched' above against the video. Press Wrong lyrics, or let a listen pass verify it.",
  },
  none: {
    cls: "pill",
    icon: <CircleSlash size={11} />,
    label: "nothing loaded",
    why: "No lyric body is loaded for the current track yet.",
  },
} as const;

const MISMATCH = {
  cls: "pill err",
  icon: <AlertTriangle size={11} />,
  label: "title mismatch",
  why: "The loaded body names a DIFFERENT song from the one searched for. Either a switch is in flight, or the wrong lyrics are up — check the gate ladder below.",
} as const;

export function NowPane({ insight }: { insight: InsightPayload }) {
  const n = insight.now;
  if (!n) return null;
  const s = insight.sync;
  const pct = n.duration > 0 ? Math.min(100, (n.position / n.duration) * 100) : 0;
  const conf = EVIDENCE[n.evidence] ?? EVIDENCE.none;
  const showing = n.idx >= 0;
  // Show the reduction only when it CHANGED something. Every player title gets
  // cleaned, so displaying it unconditionally would be noise; displaying it when
  // it differs is exactly the diagnostic — "IA & ONE / てるみい【MUSIC VIDEO】"
  // reducing to "IA & ONE" is the whole bug, visible at a glance.
  const reduced = !!n.search_title && n.search_title !== n.player_title;

  return (
    <div className="card now-card">
      <div className="now-head">
        <div className="now-title">
          <h2 title={n.player_title ?? ""}>
            {n.player_title || <span className="empty-inline">nothing playing</span>}
          </h2>
          <div className="now-sub">
            {n.player_artist || "unknown artist"}
            {n.loaded_title && n.agree === "mismatch" && (
              <> · showing lyrics for <strong>{n.loaded_title}</strong></>
            )}
          </div>
          {/* The single most diagnostic line in the console: the title the engine
              REDUCED the video's title to, which is what the lyric providers were
              actually asked for. When the reduction eats the song name, every
              downstream panel looks healthy and only this row shows why. */}
          {reduced && (
            <div className="now-searched" title="clean_title() strips credits and decorations off the player's title before searching. If this is not the song's name, the lyrics that come back will be for whatever this DOES name.">
              <Search size={10} /> searched for <strong>{n.search_title}</strong>
            </div>
          )}
        </div>
        <div className="now-badges">
          {n.busy && (
            <span className="pill warn now-pulse" title="The engine is working on this track right now.">
              <Loader2 size={11} /> {n.busy}
            </span>
          )}
          {/* Evidence first — it is the one that says whether to trust any of this.
              A title mismatch is a separate, louder fact and rides alongside it. */}
          <span className={conf.cls} title={conf.why}>{conf.icon} {conf.label}</span>
          {n.agree === "mismatch" && (
            <span className={MISMATCH.cls} title={MISMATCH.why}>
              {MISMATCH.icon} {MISMATCH.label}
            </span>
          )}
          <span className="pill" title={n.playing ? "" : "The player is paused — the overlay holds position."}>
            {n.playing ? <Play size={11} /> : <Pause size={11} />} {n.playing ? "playing" : "paused"}
          </span>
          {s && (
            <span className={s.live ? "pill ok" : "pill"} title="Which threshold set is in force.">
              {s.live ? <Radio size={11} /> : <Music size={11} />} {s.profile ?? (s.live ? "live" : "studio")}
            </span>
          )}
        </div>
      </div>

      <div className="now-bar"><i style={{ width: `${pct}%` }} /></div>
      <div className="now-times">
        <span>{clock(n.position)}</span>
        <span>
          {showing
            ? `line ${n.idx + 1} of ${n.line_count}`
            : n.line_count ? `${n.line_count} lines loaded, none showing` : "no lyrics"}
        </span>
        <span>{clock(n.duration)}</span>
      </div>

      <div className="now-tiles">
        <div className={`now-tile ${n.line_count ? "good" : "bad"}`}>
          <strong>{n.line_count || "—"}</strong>
          <span>lines loaded</span>
        </div>
        <div className="now-tile">
          <strong title={n.loaded_source ?? ""}>{n.loaded_source ?? "—"}</strong>
          <span>lyric source</span>
        </div>
        <div className="now-tile">
          <strong>
            {n.loaded_lang ?? "—"}
            {n.has_romaji ? " +rm" : ""}{n.has_english ? " +en" : ""}
          </strong>
          <span>language</span>
        </div>
        <div className="now-tile">
          <strong>{s ? `${s.offset >= 0 ? "+" : ""}${s.offset.toFixed(2)}s` : "—"}</strong>
          <span>sync offset</span>
        </div>
        <div className={`now-tile ${s && s.drift != null && Math.abs(s.drift) > s.ok_drift ? "bad" : ""}`}>
          <strong>
            {s?.drift == null ? "—" : `${s.drift >= 0 ? "+" : ""}${s.drift.toFixed(2)}s`}
          </strong>
          <span>drift</span>
        </div>
        {/* With the GPU overlay on, the Tk canvas timer is idle and reports no fps.
            Show the active renderer rather than a misleading dash or a fake 0. */}
        <div className={`now-tile ${n.render_fps != null && n.render_fps < 30 ? "bad" : ""}`}>
          <strong title={n.render_fps == null ? "The GPU overlay renders in its own process; the app-side frame timer is idle." : ""}>
            {n.render_fps != null ? n.render_fps : n.renderer === "gpu" ? "GPU" : "—"}
          </strong>
          <span>{n.render_fps != null ? "render fps" : "renderer"}</span>
        </div>
        <div className={`now-tile ${n.overlay === "idle" ? "" : "good"}`}>
          <strong>{n.overlay}</strong>
          <span>overlay</span>
        </div>
      </div>
    </div>
  );
}
