/** Library.tsx — the lyric cache and the optional components.
 *
 * Both halves of this view exist to make a FRESH INSTALL testable without
 * actually being on a fresh install.
 *
 *  1. TICKET-210 — every cached lyric body, as metadata, with a clear button.
 *     The cache is what makes the second play of a song instant, and it is also
 *     what hides first-play bugs from a developer machine that has already
 *     cached everything.
 *
 *  2. TICKET-211 — the optional heavy pieces (faster-whisper, its model weights,
 *     the CUDA libraries, yt-dlp, a JS runtime). Availability is decided purely
 *     by "is it on disk", so exercising the without-it paths used to mean
 *     deleting ~4 GB and re-downloading it. The toggles here make the engine
 *     ACT as if a piece is missing.
 *
 * COPYRIGHT: the cache table shows title, artist, language, source, and a line
 * COUNT. It never shows lyric text. The engine's /lyric_cache endpoint does not
 * return any either — `lines[*].jp` (body), `.rm` (romanisation) and `.en`
 * (translation) are all third-party content and are deliberately excluded there
 * rather than merely hidden here.
 */
import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle, CheckCircle2, Database, HardDrive, RefreshCw, Trash2, XCircle,
} from "lucide-react";
import { clearLyricCache, getLyricCache, setTune, tuneError } from "../api";
import type { ComponentsPayload, InsightPayload, LyricCachePayload } from "../models";

interface Props {
  insight: InsightPayload | null;
  online: boolean | null;
}

function kb(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function when(t: number): string {
  if (!t) return "—";
  const d = (Date.now() / 1000) - t;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
}

// ─── optional components ────────────────────────────────────────────────────

const COMPONENT_HELP: Record<string, string> = {
  whisper: "The faster-whisper library. Without it: generate-by-ear, sync-by-listening and the wrong-lyrics reject path all decline with a hint. This is the single biggest difference between a lean install and a full one.",
  model: "The whisper model weights, about 2 GB. Without them the library downloads them on first use, so the effect of simulating absence is the first-run download message and a longer stall timeout, not a failure.",
  gpu: "The CUDA and cuBLAS libraries, about 1.9 GB. Without them everything still works on the CPU, just slower, and full-episode subtitle transcription is skipped entirely.",
  ytdlp: "yt-dlp. Without it, deep generation quietly does nothing and pulling a video's caption track shows a needs-yt-dlp hint.",
  node: "A JavaScript runtime (node or deno) on PATH. yt-dlp needs one to get past YouTube's anti-bot checks on audio downloads. Detected only, not simulatable.",
};

function ComponentRow({ name, real, effective, simulated, onToggle, busy }: {
  name: string; real: boolean; effective: boolean; simulated: boolean;
  onToggle: ((v: boolean) => void) | null; busy: boolean;
}) {
  const Icon = effective ? CheckCircle2 : XCircle;
  return (
    <tr className="knob-row">
      <td>
        <code>{name}</code>
        <span className="knob-help" title={COMPONENT_HELP[name] ?? name} tabIndex={0} role="note">?</span>
        {!real && <span className="knob-type" title="Not present on this machine">absent</span>}
        {simulated && <span className="knob-type float" title="Present, but the engine is being told to ignore it">simulated missing</span>}
      </td>
      <td className="val">
        <span className={effective ? "gap-ok" : "gap-bad"} style={{ marginRight: 10 }}>
          <Icon size={12} /> {effective ? "in use" : "not in use"}
        </span>
        {onToggle ? (
          <label className="switch"
                 title={simulated ? "Stop pretending this is missing" : "Pretend this is not installed"}>
            <input type="checkbox" checked={simulated} disabled={busy || !real}
                   onChange={(e) => onToggle(e.target.checked)} />
            <span className="slider" />
          </label>
        ) : (
          <span className="empty-inline">detected only</span>
        )}
      </td>
    </tr>
  );
}

function Components({ comp }: { comp: ComponentsPayload }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function toggle(which: string, on: boolean) {
    setBusy(which); setErr(null);
    try {
      const r = await setTune(`sim_missing_${which}`, on ? 1 : 0);
      // TICKET-220: this used to read `r.msg`, which the server never sends —
      // the real per-key reason lives in `results[]`, so every failure here
      // silently fell back to the generic message and threw away the engine's
      // actual explanation. Typing setTune honestly is what surfaced it.
      setErr(tuneError(r, `could not set sim_missing_${which}`));
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const rows: [string, boolean][] = [
    ["whisper", true], ["model", true], ["gpu", true], ["ytdlp", true], ["node", false],
  ];

  return (
    <div className="card">
      <div className="card-heading">
        <div>
          <p className="eyebrow"><HardDrive size={13} /> Optional components</p>
          <h2>What a fresh install would and would not have</h2>
        </div>
        {comp.any_simulated && (
          <span className="pill gap-warn" title="At least one component is being simulated as missing">
            simulating a lean install
          </span>
        )}
      </div>
      <p>
        These pieces are heavy and optional, and the app decides it has them purely
        by looking on disk. Toggle one to make the engine behave as though it is
        not installed, so the hints and fallbacks a new user sees can be checked
        without deleting several gigabytes. This simulates absence; it does not
        re-run the first-time download.
      </p>
      <table className="knob-table">
        <tbody>
          {rows.map(([name, canSim]) => (
            <ComponentRow
              key={name}
              name={name}
              real={!!comp.real[name]}
              effective={!!comp.effective[name]}
              simulated={!!comp.simulated_missing[name]}
              busy={busy === name}
              onToggle={canSim ? (v) => toggle(name, v) : null}
            />
          ))}
        </tbody>
      </table>
      <div className="event-meta" style={{ marginTop: 12 }}>
        <span>GPU <code>{comp.gpu_status}</code></span>
        {comp.whisper_error && (
          <span className="gap-bad" title={comp.whisper_error}>
            whisper error: {comp.whisper_error.slice(0, 90)}
          </span>
        )}
      </div>
      {err && <p className="sev-warn" style={{ marginTop: 8 }}><AlertTriangle size={12} /> {err}</p>}
    </div>
  );
}

// ─── the lyric cache ────────────────────────────────────────────────────────

function Cache() {
  const [data, setData] = useState<LyricCachePayload | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(() => {
    setErr(null);
    getLyricCache()
      .then(setData)
      .catch((e) => setErr(String(e?.message ?? e)));
  }, []);

  useEffect(load, [load]);

  async function doClear(keepCurrent: boolean) {
    setBusy(true); setErr(null); setMsg(null);
    try {
      const r = await clearLyricCache(keepCurrent);
      if (r.ok) {
        setMsg(`Cleared ${r.removed ?? 0} file(s), freed ${kb(r.freed_bytes ?? 0)}.`
               + (r.failed?.length ? ` ${r.failed.length} could not be deleted.` : ""));
        setConfirming(false);
        load();
      } else {
        setErr(r.error ?? "clear failed");
      }
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const needle = q.trim().toLowerCase();
  const rows = (data?.entries ?? []).filter(
    (e) => !needle || e.title.toLowerCase().includes(needle)
        || e.artist.toLowerCase().includes(needle)
        || e.file.toLowerCase().includes(needle));

  return (
    <div className="card">
      <div className="card-heading">
        <div>
          <p className="eyebrow"><Database size={13} /> Lyric cache</p>
          <h2>{data ? `${data.count} cached song${data.count === 1 ? "" : "s"}` : "Loading…"}</h2>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="button tiny quiet" onClick={load} title="Re-read the cache directory">
            <RefreshCw size={12} /> Refresh
          </button>
          {!confirming ? (
            <button className="button tiny quiet" disabled={!data?.count}
                    onClick={() => setConfirming(true)}
                    title="Delete every cached lyric so the app behaves like a fresh install">
              <Trash2 size={12} /> Clear cache
            </button>
          ) : (
            <>
              <button className="button tiny primary" disabled={busy} onClick={() => doClear(false)}>
                {busy ? "clearing…" : `Delete all ${data?.count ?? 0}`}
              </button>
              <button className="button tiny quiet" disabled={busy} onClick={() => doClear(true)}
                      title="Delete everything except the song currently playing">
                Keep current
              </button>
              <button className="button tiny quiet" disabled={busy} onClick={() => setConfirming(false)}>
                Cancel
              </button>
            </>
          )}
        </div>
      </div>

      <p>
        Every lyric body the app has saved, {data ? kb(data.bytes) : "—"} on disk. Clearing
        it makes the next play of each song go through the full find-and-fetch path,
        which is the only way to see what a new user sees. Titles and metadata only:
        the lyric text itself is never sent to this console.
      </p>

      {confirming && (
        <p className="gap-warn" style={{ marginTop: 4 }}>
          <AlertTriangle size={12} /> This deletes the files on disk. Cached lyrics are
          re-fetched automatically as songs play, but anything generated by ear will have
          to be regenerated by listening again.
        </p>
      )}
      {msg && <p className="gap-ok">{msg}</p>}
      {err && <p className="sev-warn"><AlertTriangle size={12} /> {err}</p>}

      {data && data.count > 0 && (
        <>
          <div className="params-toolbar" style={{ marginTop: 12 }}>
            <input value={q} onChange={(e) => setQ(e.target.value)}
                   placeholder="Filter by title, artist or filename…" />
            <span className="pill">{rows.length} shown</span>
          </div>
          <div className="event-log" style={{ maxHeight: 460 }}>
            <table className="knob-table">
              <tbody>
                {rows.map((e) => (
                  <tr key={e.file} className="knob-row">
                    <td>
                      <strong style={{ color: e.loaded ? "#cfcbff" : undefined }}>
                        {e.title || e.file}
                      </strong>
                      {e.loaded && <span className="knob-type" title="Currently loaded">playing</span>}
                      {e.subtitle && <span className="knob-type" title="A show transcript, not a song">subtitle</span>}
                      <div className="event-meta" style={{ marginTop: 3 }}>
                        {e.artist && <span>{e.artist}</span>}
                        {e.lang && <span>lang <code>{e.lang}</code></span>}
                        <span>source <code>{e.source || "unknown"}</code></span>
                        <span>{e.lines} lines</span>
                      </div>
                    </td>
                    <td className="val">
                      <span className="event-meta" style={{ justifyContent: "flex-end" }}>
                        <span>{kb(e.bytes)}</span>
                        <span>{when(e.mtime)}</span>
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {data && data.count === 0 && (
        <p className="empty">
          The cache is empty.
          <small>
            This is exactly the state a fresh install starts in. Play a song and it
            will be found, fetched, annotated and saved here.
          </small>
        </p>
      )}
    </div>
  );
}

export function Library({ insight, online }: Props) {
  return (
    <div className="page-grid">
      <div className="hero">
        <p className="eyebrow"><Database size={13} /> Library</p>
        <h1>Cache and components</h1>
        <p>
          What the app has already learned, and which optional parts it is running
          with. Both can be reset or simulated away so a fresh-install experience
          can be tested on a machine that is anything but fresh.
        </p>
        {online === false && (
          <p className="sev-warn"><AlertTriangle size={12} /> The app is not responding.</p>
        )}
      </div>
      <Cache />
      {insight?.components && <Components comp={insight.components} />}
    </div>
  );
}
