/** Concerts.tsx — TICKET-218. Everything that drives concert / live behaviour.
 *
 * WHY THIS VIEW EXISTS
 * Concert handling is the most stateful thing the engine does, and until this
 * view almost none of it was observable. The mode verdict was a bare boolean
 * whose REASON lived only in a log line; the applause integrator counted in
 * silence and then reported its result as "~0.0s" because the value was reset
 * one line before it was logged (TICKET-214); the chapter list the console did
 * show was hard-wired to return empty (TICKET-215). So a concert that behaved
 * oddly gave the user a blank panel and no way to tell a real parse failure
 * from a bug.
 *
 * The organising principle here is: show the mechanism, its live state, and the
 * knobs that steer it, TOGETHER. A threshold is meaningless without the value
 * currently being measured against it, which is why the applause card draws the
 * accumulator as a meter against its arm point rather than printing two numbers.
 *
 * HONESTY RULES this view follows, because a diagnostics panel that guesses is
 * worse than no panel:
 *  - When a mechanism is not running, say WHICH condition is blocking it. Banner
 *    OCR in particular is silently and completely suppressed whenever chapters
 *    exist, which surprises everyone.
 *  - Distinguish "not detected" from "not detectable". MC and intermission
 *    segments are recognised by chapter TITLE only; there is no audio-based
 *    detector, so an unchaptered concert cannot report them at all.
 *  - Never present a derived guess as an engine reading.
 */
import { useCallback, useEffect, useState } from "react";
import {
  Activity, AlertTriangle, Clapperboard, Clock, Ear, Hand, ListMusic,
  Mic, ScanText, Sparkles, Timer,
} from "lucide-react";
import { getConcert, getTune, setTune, tuneError } from "../api";
import type { ConcertChapter, ConcertPayload, TuneValue } from "../models";

interface Props {
  online: boolean | null;
}

function secs(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  if (v < 60) return `${v.toFixed(v < 10 ? 1 : 0)}s`;
  const m = Math.floor(v / 60);
  return `${m}m ${Math.round(v - m * 60)}s`;
}

function clock(v: number): string {
  const m = Math.floor(v / 60);
  const s = Math.floor(v - m * 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

/* ── one editable knob, with its full documentation as a tooltip ───────────── */
function Knob({
  k, value, doc, onSaved,
}: {
  k: string; value: TuneValue; doc?: string; onSaved: () => void;
}) {
  const [edit, setEdit] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const commit = async () => {
    if (edit === null) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await setTune(k, edit);
      // A rejected knob comes back HTTP 200 with ok:false, so awaiting the call
      // is NOT enough to know it took — see TICKET-220 in api.ts.
      const msg = tuneError(r, `the engine refused ${k}`);
      if (msg) { setErr(msg); return; }
      setEdit(null);
      onSaved();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stat-row" title={doc || "No documentation registered for this knob."}>
      <span className="ck-name">
        {k}
        {doc ? <span className="knob-help" tabIndex={0} role="note">?</span> : null}
      </span>
      <span>
        {edit === null ? (
          <button className="linkish" onClick={() => setEdit(String(value))}>
            <strong>{String(value)}</strong>
          </button>
        ) : (
          <>
            <input
              className="ck-input"
              value={edit}
              autoFocus
              disabled={busy}
              onChange={(e) => setEdit(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void commit();
                if (e.key === "Escape") { setEdit(null); setErr(null); }
              }}
            />
            <button className="linkish" disabled={busy} onClick={() => void commit()}>save</button>
            <button className="linkish" disabled={busy} onClick={() => { setEdit(null); setErr(null); }}>
              cancel
            </button>
          </>
        )}
        {err ? <span className="pill err" style={{ marginLeft: 6 }}>{err}</span> : null}
      </span>
    </div>
  );
}

/* ── the chapter list, with non-song segments called out ───────────────────── */
function ChapterRow({ c }: { c: ConcertChapter }) {
  return (
    <div className={`stat-row${c.current ? " row-current" : ""}`}>
      <span>
        <span className="mono-dim">{clock(c.start)}</span>{" "}
        {c.skip ? <Mic size={12} style={{ verticalAlign: -1, opacity: .7 }} /> : null}{" "}
        {c.title}
      </span>
      <span>
        {c.current ? <span className="pill ok">playing now</span> : null}{" "}
        {c.skip ? <span className="pill warn">non-song</span> : null}
      </span>
    </div>
  );
}

export function Concerts({ online }: Props) {
  const [c, setC] = useState<ConcertPayload | null>(null);
  const [docs, setDocs] = useState<Record<string, string>>({});
  const [err, setErr] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  // Live poll. This view is only useful while something is playing, and the
  // whole point is watching the applause meter move, so it refreshes on the
  // same 2.5s cadence as the rest of the console rather than on a button.
  useEffect(() => {
    let cancelled = false;
    async function pull() {
      try {
        const r = await getConcert();
        if (!cancelled) { setC(r); setErr(null); }
      } catch (e) {
        if (!cancelled) setErr(String(e));
      }
    }
    void pull();
    const id = window.setInterval(pull, 2500);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [tick]);

  // Knob documentation is static for a given build, so it is fetched once
  // rather than on every poll.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const t = await getTune();
        if (!cancelled) setDocs((t.docs as Record<string, string>) ?? {});
      } catch { /* tooltips are a nicety; the view still works without them */ }
    })();
    return () => { cancelled = true; };
  }, []);

  const refresh = useCallback(() => setTick((n) => n + 1), []);

  if (online === false) {
    return <div className="card"><p className="empty">The app is not running, so there is no concert state to read.</p></div>;
  }
  if (err && !c) {
    return <div className="card"><p className="empty">Could not read concert state.<small>{err}</small></p></div>;
  }
  if (!c) {
    return <div className="card"><p className="empty">Reading concert state…</p></div>;
  }

  const a = c.applause;
  const isLive = c.live_mode || c.live_arrangement;

  return (
    <div className="page-grid">
      {/* ── 1. the verdict, and the rule that produced it ──────────────────── */}
      <section className="card">
        <div className="card-heading">
          <div>
            <p className="eyebrow"><Clapperboard size={13} /> Mode</p>
            <h2>What kind of video this is</h2>
          </div>
          <span className={`pill ${c.live_mode ? "ok" : c.live_arrangement ? "warn" : ""}`}>{c.mode}</span>
        </div>
        <p className="lede">
          {c.live_mode
            ? "Concert mode: the title names the EVENT, not a song, so the title is refused outright and songs are found by sound and by setlist."
            : c.live_arrangement
              ? "Live arrangement: the title is still trusted, but the timing differs from the studio recording, so the offset is FOLLOWED instead of being reset to zero."
              : c.nonmusic
                ? "This video was judged to be non-music, so the concert pipeline and banner OCR are both off."
                : "Studio mode. None of the concert machinery below is running."}
        </p>
        <div className="stat-row">
          <span>Verdict came from</span>
          <span><strong>{c.why?.by || "—"}</strong> · {c.why?.rule || "—"}</span>
        </div>
        <div className="stat-row">
          <span>Because</span>
          <span>{c.why?.detail || "—"}</span>
        </div>
        {c.live_arrangement && c.live_arrangement_why ? (
          <div className="stat-row">
            <span>Live arrangement because</span>
            <span>{c.live_arrangement_why}</span>
          </div>
        ) : null}
        <div className="stat-row">
          <span title="Three different sets of sync thresholds. The concert column is the loosest, because a live cut drifts the most.">
            Sync profile in force
          </span>
          <span><strong>{c.sync_profile}</strong></span>
        </div>
        <div className="stat-row">
          <span>Position</span>
          <span className="mono-dim">{clock(c.position_s)}</span>
        </div>
        {c.mv_mode ? (
          <div className="stat-row"><span>MV mode</span><span><span className="pill">expecting a dead-space intro</span></span></div>
        ) : null}
      </section>

      {/* ── 2. applause ────────────────────────────────────────────────────── */}
      <section className="card">
        <div className="card-heading">
          <div>
            <p className="eyebrow"><Ear size={13} /> Applause</p>
            <h2>Applause and cheering gaps</h2>
          </div>
          {a.armed ? <span className="pill warn">armed</span>
            : a.running ? <span className="pill ok">watching</span>
              : <span className="pill">idle</span>}
        </div>
        <p className="lede">
          A live cut pauses for applause while the player clock keeps running, so the
          lyrics drift ahead by the length of the pause. The detector looks for audio
          that is loud but NOT tonal singing (broadband cheering), and acts when the
          singing comes back.
        </p>
        {!a.running ? (
          <p className="empty">
            Not watching for gaps.
            <small>
              {!isLive ? "This is not a live or concert cut."
                : "Waiting for lyrics to be loaded, or an alignment is already in flight."}
            </small>
          </p>
        ) : (
          <>
            <div className="meter-row">
              <div className="meter" title={`${a.accumulating_s.toFixed(2)}s of ${a.arm_at_s.toFixed(2)}s needed to arm`}>
                <div
                  className={`meter-fill${a.armed ? " meter-fill-armed" : ""}`}
                  style={{ width: `${Math.round(a.progress * 100)}%` }}
                />
              </div>
              <span className="mono-dim">
                {a.accumulating_s.toFixed(2)}s / {a.arm_at_s.toFixed(2)}s
              </span>
            </div>
            <div className="stat-row">
              <span>When a gap completes</span>
              <span><strong>{a.on_gap}</strong></span>
            </div>
          </>
        )}
        <div className="stat-row">
          <span>Gaps caught this run</span>
          <span><strong>{a.gaps_this_run}</strong></span>
        </div>
        <div className="stat-row">
          <span title="Measured length of the last completed gap. Before TICKET-214 this was destroyed by a reset before it could be reported, so every log line said 0.0s.">
            Last gap
          </span>
          <span>
            {a.gaps_this_run
              ? <>{secs(a.last_gap_s)} <span className="mono-dim">· {a.last_action} · {secs(a.last_ago_s)} ago</span></>
              : <span className="empty-inline">none yet</span>}
          </span>
        </div>
        {a.tpvr_active ? (
          <div className="stat-row">
            <span title="A resync after applause is only applied if a SECOND listen agrees with the first, so one bad read cannot move the lyrics.">
              Two-point check
            </span>
            <span>
              <span className="pill warn">confirming</span>{" "}
              {a.tpvr_held_offset !== null
                ? <span className="mono-dim">holding {a.tpvr_held_offset > 0 ? "+" : ""}{a.tpvr_held_offset.toFixed(2)}s</span>
                : <span className="mono-dim">listening</span>}
              {a.tpvr_expires_in_s !== null ? <span className="mono-dim"> · expires in {secs(a.tpvr_expires_in_s)}</span> : null}
            </span>
          </div>
        ) : null}
      </section>

      {/* ── 3. setlist / chapters ──────────────────────────────────────────── */}
      <section className="card">
        <div className="card-heading">
          <div>
            <p className="eyebrow"><ListMusic size={13} /> Setlist</p>
            <h2>Setlist</h2>
          </div>
          <span className="pill">{c.chapters.length} chapters</span>
        </div>
        {c.chapters.length === 0 ? (
          <p className="empty">
            No chapter setlist for this video.
            <small>
              Chapters come from the video's own chapter marks, or from timestamps parsed
              out of its description. Without them, songs are found by banner OCR and by ear.
            </small>
          </p>
        ) : (
          <>
            <p className="lede">
              Chapters drive song changes deterministically: on entering a song chapter the
              engine fetches that title and anchors the offset to the chapter start, or to
              the measured vocal onset when the offline pass found one.
            </p>
            <div className="scroll-list">
              {c.chapters.map((ch) => <ChapterRow key={ch.i} c={ch} />)}
            </div>
          </>
        )}
        <div className="stat-row">
          <span>Candidate pool</span>
          <span>{c.candidates ? <><strong>{c.candidates}</strong> songs parsed from the description</> : <span className="empty-inline">empty</span>}</span>
        </div>
      </section>

      {/* ── 4. MC / intermissions ──────────────────────────────────────────── */}
      <section className="card">
        <div className="card-heading">
          <div>
            <p className="eyebrow"><Mic size={13} /> Non-song segments</p>
            <h2>MC, talk and intermissions</h2>
          </div>
          {c.mc_segments.length ? <span className="pill warn">{c.mc_segments.length} found</span> : null}
        </div>
        {c.mc_segments.length === 0 ? (
          <p className="empty">
            No non-song segments identified.
            <small>{c.mc_note}</small>
          </p>
        ) : (
          <>
            <p className="lede">
              These chapters are treated as non-song segments: the engine does not fetch
              lyrics for them, and the previous song's lyrics stay on screen until the
              next real song chapter begins.
            </p>
            <div className="scroll-list">
              {c.mc_segments.map((ch) => <ChapterRow key={ch.i} c={ch} />)}
            </div>
            <p className="note-inline"><AlertTriangle size={12} /> {c.mc_note}</p>
          </>
        )}
      </section>

      {/* ── 5. between songs ───────────────────────────────────────────────── */}
      <section className="card">
        <div className="card-heading">
          <div>
            <p className="eyebrow"><Hand size={13} /> Between songs</p>
            <h2>Between songs</h2>
          </div>
          {c.between_songs.holding ? <span className="pill warn">holding</span> : <span className="pill">not holding</span>}
        </div>
        <p className="lede">
          When a new chapter starts and the offline pass has no measured onset for it,
          the lyrics are held back until vocals are actually heard, so they do not run
          during the applause and introduction.
        </p>
        <div className="stat-row"><span>State</span><span>{c.between_songs.why}</span></div>
        <div className="stat-row">
          <span>Anchored past the intro</span>
          <span>{c.between_songs.anchored ? <span className="pill ok">yes</span> : <span className="pill warn">not yet</span>}</span>
        </div>
        {c.between_songs.holding ? (
          <div className="stat-row">
            <span>Held for</span>
            <span>{secs(c.between_songs.elapsed_s)} <span className="mono-dim">· releases regardless at {secs(c.between_songs.releases_at_s)}</span></span>
          </div>
        ) : null}
      </section>

      {/* ── 6. offline plan ────────────────────────────────────────────────── */}
      <section className="card">
        <div className="card-heading">
          <div>
            <p className="eyebrow"><Sparkles size={13} /> Offline analysis</p>
            <h2>Offline audio plan</h2>
          </div>
          {c.plan.length ? <span className="pill ok">{c.plan.length} segments</span> : null}
        </div>
        {c.plan.length === 0 ? (
          <p className="empty">
            No offline analysis for this video.
            <small>{c.plan_note}</small>
          </p>
        ) : (
          <>
            <p className="lede">{c.plan_note}</p>
            <div className="scroll-list">
              {c.plan.map((s, i) => (
                <div key={i} className={`stat-row${i === c.plan_current ? " row-current" : ""}`}>
                  <span>
                    <span className="mono-dim">{clock(s.start)}</span>{" "}
                    {s.title || <span className="empty-inline">unnamed</span>}
                  </span>
                  <span>
                    {s.onset !== null
                      ? <span className="pill ok" title="Measured first-vocal time. Lyrics anchor here, past the applause and intro.">onset {clock(s.onset)}</span>
                      : <span className="pill" title="No vocal onset measured for this segment; the chapter start is used instead.">no onset</span>}
                    {s.id_conf ? <span className="mono-dim"> · id {s.id_conf.toFixed(2)}</span> : null}
                  </span>
                </div>
              ))}
            </div>
          </>
        )}
      </section>

      {/* ── 7. watchdogs ───────────────────────────────────────────────────── */}
      <section className="card">
        <div className="card-heading">
          <div>
            <p className="eyebrow"><Timer size={13} /> Watchdogs</p>
            <h2>Watchdogs</h2>
          </div>
        </div>
        <p className="lede">
          Two backstops for a missed song boundary, which is the characteristic concert
          failure: the video moved on and the overlay did not.
        </p>
        <div className="stat-row">
          <span title="A song heard but not yet confirmed by a second read. Escalates faster when nothing is on screen, because a blank overlay is the worse state.">
            Unconfirmed switch
          </span>
          <span>
            {c.watchdogs.pending_switch
              ? <>{c.watchdogs.pending_switch} <span className="mono-dim">· {secs(c.watchdogs.pending_age_s)} of {secs(c.watchdogs.pending_escalates_at_s)}</span></>
              : <span className="empty-inline">none pending</span>}
          </span>
        </div>
        <div className="stat-row">
          <span title="A concert song still showing after 6.5 minutes almost certainly changed and the boundary was missed.">
            Same song showing for
          </span>
          <span>
            {c.watchdogs.same_song_for_s !== null
              ? <>{secs(c.watchdogs.same_song_for_s)} <span className="mono-dim">· forces a re-identify at {secs(c.watchdogs.stale_song_forces_reid_at_s)}</span></>
              : <span className="empty-inline">not timing</span>}
          </span>
        </div>
      </section>

      {/* ── 8. resync cadence ──────────────────────────────────────────────── */}
      <section className="card">
        <div className="card-heading">
          <div>
            <p className="eyebrow"><Activity size={13} /> Resync cadence</p>
            <h2>Live resync cadence</h2>
          </div>
          {c.cadence.inflight ? <span className="pill warn">listening</span> : null}
        </div>
        <p className="lede">
          A live arrangement drifts continuously, so the engine re-listens on a loop and
          backs off as it gains confidence: fast while it is still catching up, slow once
          it has been in sync for several passes in a row.
        </p>
        <div className="stat-row"><span>In-sync streak</span><span><strong>{c.cadence.in_sync_streak}</strong> <span className="mono-dim">· relaxes after {String(c.cadence.relax_after_n)}</span></span></div>
        <div className="stat-row"><span>Current gap between listens</span><span>{c.cadence.gap_s !== null ? secs(c.cadence.gap_s) : <span className="empty-inline">not running</span>}</span></div>
        <div className="stat-row">
          <span>Tiers</span>
          <span className="mono-dim">
            fast {String(c.cadence.tiers_s.fast)}s · mid {String(c.cadence.tiers_s.mid)}s · slow {String(c.cadence.tiers_s.slow)}s
          </span>
        </div>
      </section>

      {/* ── 9. banner OCR ──────────────────────────────────────────────────── */}
      <section className="card">
        <div className="card-heading">
          <div>
            <p className="eyebrow"><ScanText size={13} /> Banner OCR</p>
            <h2>Banner OCR</h2>
          </div>
          {c.ocr.running ? <span className="pill ok">running</span> : <span className="pill">not running</span>}
        </div>
        <p className="lede">
          Many concert streams caption the current song on screen. When there is no chapter
          setlist, the engine reads that banner as a song-identification source.
        </p>
        {c.ocr.blocked_because ? (
          <p className="note-inline"><AlertTriangle size={12} /> Not reading the banner: {c.ocr.blocked_because}.</p>
        ) : null}
        <div className="stat-row"><span>Enabled</span><span>{c.ocr.enabled ? <span className="pill ok">yes</span> : <span className="pill warn">no</span>}</span></div>
        <div className="stat-row"><span>Accepts a title at</span><span className="mono-dim">score ≥ {c.ocr.accept_at}</span></div>
        <div className="stat-row"><span>Last read</span><span>{c.ocr.last_read_ago_s !== null ? `${secs(c.ocr.last_read_ago_s)} ago` : <span className="empty-inline">never this track</span>}</span></div>
      </section>

      {/* ── 10. the knobs, grouped by what they steer ──────────────────────── */}
      <section className="card">
        <div className="card-heading">
          <div>
            <p className="eyebrow"><Clock size={13} /> Tuning</p>
            <h2>Knobs that steer concert behaviour</h2>
          </div>
          <span className="pill">
            {Object.values(c.knobs).reduce((n, g) => n + g.length, 0)} settings
          </span>
        </div>
        <p className="lede">
          Every value here applies immediately, with no restart. Hover any name for what it
          does and what moving it costs. Changes are runtime-only and revert when the app
          restarts, so an experiment cannot permanently break a working setup.
        </p>
        {Object.entries(c.knobs).map(([group, rows]) => (
          <div key={group} className="ck-group">
            <h4 className="ck-group-title">{group}</h4>
            {rows.map((r) => (
              <Knob key={r.key} k={r.key} value={r.value as TuneValue} doc={docs[r.key]} onSaved={refresh} />
            ))}
          </div>
        ))}
      </section>
    </div>
  );
}
