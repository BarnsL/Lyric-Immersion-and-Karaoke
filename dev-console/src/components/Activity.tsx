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
  Bell, CheckCircle2, AlertTriangle, Globe, Info, MessageSquare, MinusCircle,
  Music, RefreshCw, ShieldAlert, ThumbsDown, ThumbsUp, Zap,
} from "lucide-react";
import { NowPane } from "./Now";
import { addEventNote, overrideLanguage, overrideTitle, postWrong } from "../api";
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
  // TICKET-205: a correction the engine WANTED but kept discarding under the
  // apply floor. Distinct from "refused" (which is an active judgement that the
  // evidence was bad) — this one means the lyrics stayed visibly wrong and
  // nothing in the engine was able to act on it.
  "sync-ignored":    { icon: MinusCircle,   label: "Sync held back" },
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

const signed = (n: number) => `${n > 0 ? "+" : ""}${n.toFixed(2)}s`;

/**
 * TICKET-205 — the offset either side of a sync correction, so the move can be
 * read at a glance instead of inferred: "moved +0.00s → +3.40s (Δ +3.40s)".
 *
 * Returns null for non-sync events, which simply do not carry these fields.
 * `sync-ignored` says "wanted" rather than "moved": nothing actually changed,
 * and labelling a discard as a move is the exact class of lie this ticket is
 * about.
 */
function SyncMove({ e }: { e: NotableEvent }) {
  if (e.delta == null || e.frm == null || e.to == null) return null;
  const held = e.kind === "sync-ignored";
  return (
    <span className="event-move" title={held ? "correction proposed but discarded"
                                             : "sync offset before → after"}>
      {held ? "wanted" : "moved"} <code>{signed(e.frm)}</code>
      {" → "}<code>{signed(e.to)}</code>
      <em className={Math.abs(e.delta) > 5 ? "gap-bad" : ""}>Δ {signed(e.delta)}</em>
    </span>
  );
}

// ─── toast (animated notification) ─────────────────────────────────────────

/**
 * A toast is an event plus a key unique to this ON-SCREEN card.
 *
 * TICKET-207 note: the field is `toastId`, not `id`. Events now carry their own
 * numeric `id` from the engine, and the two are different things — the event id
 * identifies the decision (and is what a user note is filed against), while this
 * one identifies one transient card, so the same event shown twice would need
 * two distinct values. Overloading `id` made them collide.
 */
interface Toast extends NotableEvent {
  toastId: string;
}

function ToastStack({ events }: { events: NotableEvent[] }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const seenRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    // Detect NEW events by comparing to what we have already shown. Prefer the
    // engine's stable id; fall back to the old composite for any event that
    // predates it (a ring populated by an older build).
    const fresh = events.filter((e) => {
      const key = e.id != null ? `#${e.id}` : `${e.t}-${e.kind}-${e.detail}`;
      if (seenRef.current.has(key)) return false;
      seenRef.current.add(key);
      return true;
    });
    if (fresh.length === 0) return;

    // Cap the toast ring at 5 so a burst does not pile up.
    const newToasts: Toast[] = fresh.slice(-5).map((e) => ({
      ...e,
      toastId: `${e.id ?? e.t}-${e.kind}-${Math.random().toString(36).slice(2, 6)}`,
    }));
    setToasts((prev) => [...prev, ...newToasts].slice(-5));

    // Auto-dismiss after 6s for info/good, 10s for warn.
    const timers = newToasts.map((t) =>
      window.setTimeout(() => {
        setToasts((prev) => prev.filter((x) => x.toastId !== t.toastId));
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
          <div key={t.toastId} className={`toast ${sevClass(t.sev)} toast-enter`}>
            <div className="toast-icon"><Icon size={16} /></div>
            <div className="toast-body">
              <div className="toast-head">
                <strong>{KIND_META[t.kind]?.label ?? t.kind}</strong>
                <span className="toast-ago">{ago(t.t)}</span>
              </div>
              <p>{t.detail}</p>
              {(t.gap != null || t.pos != null || t.delta != null) && (
                <div className="toast-meta">
                  {t.title && <span className="toast-title">{t.title}</span>}
                  {t.pos != null && <span>video {clock(t.pos)}</span>}
                  {t.gap != null && (
                    <span className={Math.abs(t.gap) > 10 ? "gap-bad" : ""}>
                      gap {t.gap > 0 ? "+" : ""}{t.gap.toFixed(1)}s
                    </span>
                  )}
                  <SyncMove e={t} />
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

/**
 * TICKET-207 — attach your own words to one event.
 *
 * The value is not the single note, it is the corpus. Each is appended to
 * `event_notes.jsonl` next to a snapshot of the event, so once enough incidents
 * are labelled a later pass can look for what the bad ones have in common,
 * rather than re-deriving each from the log. That is why the note is stored with
 * the event's full context and never overwrites a previous one.
 *
 * `matched: false` comes back when the event has already aged out of the
 * engine's 120-entry ring. The note is still recorded, and we say so, because
 * silently accepting a note that landed nowhere is exactly the class of quiet
 * failure this whole area of the console exists to remove.
 */
function EventNote({ e }: { e: NotableEvent }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [state, setState] = useState<"idle" | "saving" | "ok" | "stale" | "err">("idle");
  const [msg, setMsg] = useState<string | null>(null);

  if (e.id == null) return null;   // pre-TICKET-207 event, not addressable

  async function save() {
    const note = text.trim();
    if (!note) return;
    setState("saving"); setMsg(null);
    try {
      const r = await addEventNote(e.id!, note);
      if (!r.ok) { setState("err"); setMsg(r.error ?? "failed"); return; }
      setState(r.matched ? "ok" : "stale");
      setMsg(r.matched
        ? "Saved against this event."
        : "Saved to the diagnostics file, but this event has already aged out of the live ring.");
      setText(""); setOpen(false);
    } catch (err: unknown) {
      setState("err");
      setMsg(err instanceof Error ? err.message : String(err));
    }
  }

  const notes = e.notes ?? [];
  return (
    <div className="event-note">
      {notes.length > 0 && (
        <ul className="event-note-list">
          {notes.map((n, i) => (
            <li key={i}><MessageSquare size={11} /> <span>{n.note}</span></li>
          ))}
        </ul>
      )}
      {open ? (
        <div className="event-note-edit">
          <input
            autoFocus
            value={text}
            placeholder="What did you actually see? e.g. lyrics jumped a verse ahead here"
            onChange={(ev) => setText(ev.target.value)}
            onKeyDown={(ev) => {
              if (ev.key === "Enter") save();
              if (ev.key === "Escape") { setOpen(false); setText(""); }
            }}
          />
          <button className="button tiny primary" disabled={state === "saving" || !text.trim()}
                  onClick={save}>{state === "saving" ? "saving…" : "Save note"}</button>
          <button className="button tiny quiet" onClick={() => { setOpen(false); setText(""); }}>
            Cancel
          </button>
        </div>
      ) : (
        <button className="button tiny quiet event-note-add" onClick={() => setOpen(true)}
                title="Add a diagnostic note to this event. Notes are collected locally so patterns across many songs can be found later.">
          <MessageSquare size={11} /> {notes.length ? "Add another note" : "Add note"}
        </button>
      )}
      {msg && (
        <p className={`event-note-msg ${state === "err" ? "sev-warn" : state === "stale" ? "gap-warn" : "gap-ok"}`}>
          {msg}
        </p>
      )}
    </div>
  );
}

function EventLog({ events }: { events: NotableEvent[] }) {
  if (events.length === 0) {
    return (
      <div className="card">
        <div className="card-heading"><Bell size={15} /> Activity log</div>
        <p className="empty">
          No decisions have fired yet.
          <small>
            The log populates when the engine makes a notable decision: the song
            changes, a body is rejected or restored, lyrics arrive (especially
            late), or the decision engine escalates. Every sync correction is
            recorded too — whichever subsystem made it, and whether it applied,
            was deferred to the end of the line, or was held back under the apply
            floor. Each entry is stamped with the video position, the lyric line
            time, and the gap between them — the number that tells a sync bug
            from a correct run.
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
                  <SyncMove e={e} />
                  {/* TICKET-205: for a sync event SyncMove already shows the
                      before/after pair, so the standing offset would just be
                      a duplicate of `to`. */}
                  {e.delta == null && e.offset != null && e.offset !== 0 && (
                    <span>offset <code>{e.offset > 0 ? "+" : ""}{e.offset.toFixed(2)}s</code></span>
                  )}
                  {e.lines != null && <span>{e.lines} lines</span>}
                  {e.late_s != null && e.late_s >= 20 && (
                    <span className="gap-warn">loaded {e.late_s.toFixed(0)}s late</span>
                  )}
                  {e.ratio != null && <span>ratio {e.ratio.toFixed(2)}</span>}
                </div>
                <EventNote e={e} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Wrong Song + title picker ──────────────────────────────────────────────

/**
 * TICKET-208 — correct a wrongly-detected song language.
 *
 * Language is not a label on a card. It selects the romanisation (furigana plus
 * romaji for Japanese, pinyin for Chinese), decides whether a translation lane
 * is produced at all, picks the overlay font, orders which lyric providers are
 * tried, and drives a wrong-language check that DELETES a cached body. A wrong
 * verdict is therefore expensive, and until now there was no way to correct it:
 * every language control in the app was read-only.
 *
 * Corrections are appended to `language_corrections.jsonl` for the same reason
 * as the event notes: one correction fixes tonight's playback, a few hundred
 * show which kinds of song get misdetected.
 */
function LanguagePane({ insight }: { insight: InsightPayload }) {
  const lid = insight.language_id ?? null;
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  if (!lid) return null;

  async function pick(lang: string) {
    setBusy(lang); setErr(null); setMsg(null);
    try {
      const r = await overrideLanguage(lang);
      if (r.ok) setMsg(r.action ?? `Language set to ${lang}.`);
      else setErr(r.error ?? "failed");
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="card">
      <div className="card-heading">
        <div>
          <p className="eyebrow"><Globe size={13} /> Language</p>
          <h2>Detected as {lid.lang || "unknown"}</h2>
        </div>
        {lid.overridden && <span className="pill">corrected to {lid.override_lang}</span>}
      </div>
      <p>
        The language decides romanisation, whether a translation is generated, the
        overlay font, which lyric providers are tried, and a cache check that can
        delete a wrongly-matched body. If it is wrong, correct it here and the
        romaji and translation lanes are rebuilt immediately.
      </p>
      <div className="lang-grid">
        {lid.choices.map((c) => (
          <button
            key={c}
            className={`button tiny ${c === lid.lang ? "primary" : "quiet"}`}
            disabled={busy != null || c === lid.lang}
            onClick={() => pick(c)}
            title={c === lid.lang ? "This is the language currently in force"
                                  : `Tell the engine this song is ${c}`}
          >
            {busy === c ? "…" : c}
          </button>
        ))}
      </div>
      <div className="event-meta" style={{ marginTop: 10 }}>
        {lid.source && <span>body source <code>{lid.source}</code></span>}
        {lid.cover_lang && <span>cover lang <code>{lid.cover_lang}</code></span>}
        {lid.gen_lang && <span>by-ear lang <code>{lid.gen_lang}</code></span>}
      </div>
      {msg && <p className="gap-ok" style={{ marginTop: 8 }}>{msg}</p>}
      {err && <p className="sev-warn" style={{ marginTop: 8 }}>{err}</p>}
    </div>
  );
}

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
      <LanguagePane insight={insight} />

      <EventLog events={events} />
    </div>
  );
}
