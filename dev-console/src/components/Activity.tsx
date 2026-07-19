/** Activity.tsx — TICKET-203: the narrated event stream.
 *
 * Two things in one view:
 *  1. Animated toast notifications that slide in when a new decision event
 *     arrives (song changed, body rejected, sync jumped, lyrics loaded, etc.)
 *  2. A readable log panel: video name, what happened, video position vs lyric
 *     time (the gap), the offset in force — everything needed to understand
 *     a sync bug at a glance instead of reading karaoke.log by hand.
 */
import { useEffect, useRef, useState } from "react";
import {
  Bell, CheckCircle2, AlertTriangle, Info, Music, RefreshCw,
  ShieldAlert, ThumbsDown, ThumbsUp, Zap,
} from "lucide-react";
import { NowPane } from "./Now";
import { overrideTitle, postWrong } from "../api";
import type { InsightPayload, NotableEvent } from "../models";

interface Props {
  insight: InsightPayload | null;
  online: boolean | null;
}

// ─── helpers ───────────────────────────────────────────────────────────────

function clock(s: number | null | undefined): string {
  if (s == null || !isFinite(s) || s < 0) return "—";
  const sign = s < 0 ? "-" : "";
  const a = Math.abs(s);
  const m = Math.floor(a / 60);
  const sec = Math.floor(a % 60);
  return `${sign}${m}:${String(sec).padStart(2, "0")}`;
}

function ago(t: number): string {
  const d = Date.now() / 1000 - t;
  if (d < 2) return "just now";
  if (d < 60) return `${Math.round(d)}s ago`;
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  return `${Math.round(d / 3600)}h ago`;
}

const KIND_META: Record<string, { icon: typeof Info; label: string }> = {
  "song-change":     { icon: Music,         label: "Song change" },
  "lyrics-loaded":   { icon: CheckCircle2,  label: "Lyrics loaded" },
  "body-rejected":   { icon: ShieldAlert,   label: "Body rejected" },
  "cache-restored":  { icon: RefreshCw,     label: "Cache restored" },
  "sync-jump":       { icon: Zap,           label: "Sync jump" },
  "sync-nudge":      { icon: Zap,           label: "Sync nudge" },
  "sync-revert":     { icon: AlertTriangle, label: "Sync reverted" },
  "sync-rejected":   { icon: ThumbsDown,    label: "Sync refused" },
  "decision":        { icon: Bell,          label: "Decision" },
  "title-override":  { icon: ThumbsUp,      label: "Title overridden" },
};

function sevClass(sev: string): string {
  if (sev === "good") return "sev-good";
  if (sev === "warn") return "sev-warn";
  return "sev-info";
}

function kindIcon(kind: string) {
  const meta = KIND_META[kind] ?? KIND_META["decision"];
  return meta.icon;
}

// ─── toast (animated notification) ─────────────────────────────────────────

interface Toast extends NotableEvent {
  id: string;
}

function ToastStack({ events }: { events: NotableEvent[] }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const seenRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    // Detect NEW events by comparing to what we have already shown.
    const fresh = events.filter((e) => {
      const id = `${e.t}-${e.kind}-${e.detail}`;
      if (seenRef.current.has(id)) return false;
      seenRef.current.add(id);
      return true;
    });
    if (fresh.length === 0) return;

    // Cap the toast ring at 5 so a burst does not pile up.
    const newToasts: Toast[] = fresh.slice(-5).map((e) => ({
      ...e,
      id: `${e.t}-${e.kind}-${Math.random().toString(36).slice(2, 6)}`,
    }));
    setToasts((prev) => [...prev, ...newToasts].slice(-5));

    // Auto-dismiss after 6s for info/good, 10s for warn.
    const timers = newToasts.map((t) =>
      window.setTimeout(() => {
        setToasts((prev) => prev.filter((x) => x.id !== t.id));
      }, t.sev === "warn" ? 10000 : 6000)
    );
    return () => timers.forEach((id) => window.clearTimeout(id));
  }, [events]);  // eslint-disable-line react-hooks/exhaustive-deps

  if (toasts.length === 0) return null;

  return (
    <div className="toast-stack">
      {toasts.map((t) => {
        const Icon = kindIcon(t.kind);
        return (
          <div key={t.id} className={`toast ${sevClass(t.sev)} toast-enter`}>
            <div className="toast-icon"><Icon size={16} /></div>
            <div className="toast-body">
              <div className="toast-head">
                <strong>{KIND_META[t.kind]?.label ?? t.kind}</strong>
                <span className="toast-ago">{ago(t.t)}</span>
              </div>
              <p>{t.detail}</p>
              {(t.gap != null || t.pos != null) && (
                <div className="toast-meta">
                  {t.title && <span className="toast-title">{t.title}</span>}
                  {t.pos != null && <span>video {clock(t.pos)}</span>}
                  {t.gap != null && (
                    <span className={Math.abs(t.gap) > 10 ? "gap-bad" : ""}>
                      gap {t.gap > 0 ? "+" : ""}{t.gap.toFixed(1)}s
                    </span>
                  )}
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ─── the log panel ──────────────────────────────────────────────────────────

function EventLog({ events }: { events: NotableEvent[] }) {
  if (events.length === 0) {
    return (
      <div className="card">
        <div className="card-heading"><Bell size={15} /> Activity log</div>
        <p className="empty">
          No decisions have fired yet.
          <small>
            The log populates when the engine makes a notable decision: the song
            changes, a body is rejected or restored, a sync correction commits or
            is refused, lyrics arrive (especially late), or the decision engine
            escalates. Each entry is stamped with the video position, the lyric
            line time, and the gap between them — the number that tells a sync
            bug from a correct run.
          </small>
        </p>
      </div>
    );
  }

  // Newest first
  const rows = [...events].reverse();

  return (
    <div className="card">
      <div className="card-heading">
        <Bell size={15} /> Activity log
        <span className="pill">{events.length} event{events.length === 1 ? "" : "s"}</span>
      </div>
      <div className="event-log">
        {rows.map((e, i) => {
          const Icon = kindIcon(e.kind);
          return (
            <div key={`${e.t}-${i}`} className={`event-row ${sevClass(e.sev)}`}>
              <div className="event-time">
                <span className="event-ago">{ago(e.t)}</span>
              </div>
              <div className="event-icon"><Icon size={13} /></div>
              <div className="event-content">
                <div className="event-kind">
                  {KIND_META[e.kind]?.label ?? e.kind}
                  {e.source && <span className="event-source">{e.source}</span>}
                </div>
                <p className="event-detail">{e.detail}</p>
                <div className="event-meta">
                  {e.title && <span className="event-title" title={e.title}>{e.title}</span>}
                  {e.pos != null && <span>video <code>{clock(e.pos)}</code></span>}
                  {e.lyric_t != null && <span>lyric <code>{clock(e.lyric_t)}</code></span>}
                  {e.gap != null && (
                    <span className={Math.abs(e.gap) > 10 ? "gap-bad" : Math.abs(e.gap) > 3 ? "gap-warn" : "gap-ok"}>
                      gap <code>{e.gap > 0 ? "+" : ""}{e.gap.toFixed(1)}s</code>
                    </span>
                  )}
                  {e.offset != null && e.offset !== 0 && (
                    <span>offset <code>{e.offset > 0 ? "+" : ""}{e.offset.toFixed(2)}s</code></span>
                  )}
                  {e.lines != null && <span>{e.lines} lines</span>}
                  {e.late_s != null && e.late_s >= 20 && (
                    <span className="gap-warn">loaded {e.late_s.toFixed(0)}s late</span>
                  )}
                  {e.ratio != null && <span>ratio {e.ratio.toFixed(2)}</span>}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Wrong Song + title picker ──────────────────────────────────────────────

function WrongSongPane({ insight }: { insight: InsightPayload }) {
  const tid = insight.title_id;
  const [picking, setPicking] = useState(false);
  const [custom, setCustom] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  async function doOverride(title: string) {
    if (!title.trim()) return;
    setBusy(true);
    setMsg(null);
    try {
      await overrideTitle(title.trim());
      setMsg(`Re-fetching as "${title.trim().slice(0, 50)}"…`);
      setPicking(false);
      setCustom("");
    } catch (e) {
      setMsg(`Failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  async function doWrong() {
    setBusy(true);
    setMsg(null);
    try {
      await postWrong();
      setMsg("Re-identifying by sound…");
    } catch (e) {
      setMsg(`Failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card wrong-song-card">
      <div className="card-heading">
        <ThumbsDown size={15} /> Wrong song?
        {tid?.overridden && <span className="pill warn">override active</span>}
      </div>
      <p className="tree-note" style={{ marginTop: 0 }}>
        If the lyrics are for the wrong song, pick the correct title from what
        the engine has seen — or type it. The bad string is logged alongside the
        good one for future diagnostics.
      </p>

      {tid && (
        <div className="title-id-grid">
          <div className="stat-row">
            <span>Raw title</span>
            <code title={tid.raw_title ?? ""}>{tid.raw_title || "—"}</code>
          </div>
          <div className="stat-row">
            <span>Engine reduced to</span>
            <code title={tid.clean_title ?? ""} className={tid.overridden ? "strikethrough" : ""}>
              {tid.clean_title || "—"}
            </code>
          </div>
          {tid.overridden && (
            <div className="stat-row">
              <span>Override</span>
              <code className="override-active">{tid.override_title}</code>
            </div>
          )}
        </div>
      )}

      {!picking ? (
        <div className="wrong-song-actions">
          <button className="button warn" disabled={busy} onClick={() => setPicking(true)}>
            <ThumbsDown size={13} /> Pick correct title
          </button>
          <button className="button quiet" disabled={busy} onClick={doWrong}>
            <RefreshCw size={13} /> Re-identify by sound
          </button>
        </div>
      ) : (
        <div className="title-picker">
          <p className="tree-note">Select the correct title from what the engine has seen:</p>
          <div className="seen-strings">
            {(tid?.seen_strings ?? []).map((s, i) => (
              <button
                key={i}
                className="button quiet seen-string-btn"
                disabled={busy}
                onClick={() => doOverride(s)}
                title={s}
              >
                {s.length > 70 ? s.slice(0, 67) + "…" : s}
              </button>
            ))}
          </div>
          <div className="custom-title">
            <input
              type="text"
              placeholder="or type the correct title…"
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && custom.trim()) doOverride(custom); }}
              disabled={busy}
            />
            <button className="button primary" disabled={busy || !custom.trim()} onClick={() => doOverride(custom)}>
              {busy ? "…" : "Apply"}
            </button>
            <button className="button quiet" disabled={busy} onClick={() => setPicking(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {msg && <p className="tree-note gap-warn">{msg}</p>}
    </div>
  );
}

// ─── main view ──────────────────────────────────────────────────────────────

export function Activity({ insight, online }: Props) {
  if (online === false) return <p className="empty">App offline.</p>;
  if (!insight) return <p className="empty">Waiting for /insight…</p>;

  const events = insight.notable ?? [];

  return (
    <div className="page-grid">
      <div className="hero">
        <p className="eyebrow"><Bell size={13} /> ACTIVITY</p>
        <h1>Decision stream</h1>
        <p>
          Every notable decision the engine makes, narrated as it happens. Toast
          notifications fire on new events; the log below stays for the session.
          Each row carries the video position, the lyric line time, and the gap
          between them — the number that tells a correct sync from a runaway.
        </p>
      </div>

      <NowPane insight={insight} />

      <ToastStack events={events} />

      <WrongSongPane insight={insight} />

      <EventLog events={events} />
    </div>
  );
}
