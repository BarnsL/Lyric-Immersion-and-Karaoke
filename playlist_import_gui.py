"""
Import Playlist window — Tkinter Toplevel for importing Spotify or YouTube Music
playlists into the Desktop Karaoke library from the system tray menu.

Call `show_import_window(root)` from any thread — it marshals itself onto the
Tk thread automatically.

Three import modes (radio buttons):
  • Exportify CSV   — pick a CSV exported from exportify.net (no auth needed)
  • Spotify OAuth   — live fetch via Spotify Web API (needs a free Developer App)
  • YouTube Music   — playlist URL(s) via yt-dlp (close browser first)

Progress is shown live in a scrolling log. The import runs in a background
thread; Cancel stops it after the current track.
"""

from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, scrolledtext, ttk
from typing import Callable

from playlist_import import (
    ImportJob,
    import_from_csv,
    import_from_spotify,
    import_from_youtube,
)

# ── colours matching the overlay's dark-purple theme ──────────────────────────
_BG      = "#1a1625"
_FG      = "#e8e4f0"
_DIM     = "#887ea0"
_ACC     = "#7c3aed"
_BTN_BG  = "#2d2640"
_FONT    = ("Segoe UI", 10)
_MONO    = ("Consolas", 9)

# Module-level singleton so re-opening raises the existing window.
_state: dict = {"job": None, "win": None}


# ── public entry point ─────────────────────────────────────────────────────────

def show_import_window(root: tk.Misc) -> None:
    """Open (or raise) the Import Playlist window. Thread-safe."""
    root.after(0, lambda: _create_or_raise(root))


# ── internal ───────────────────────────────────────────────────────────────────

def _create_or_raise(root: tk.Misc) -> None:
    win = _state.get("win")
    if win:
        try:
            if win.winfo_exists():
                win.lift()
                win.focus_force()
                return
        except Exception:
            pass
    _build_window(root)


def _build_window(root: tk.Misc) -> None:
    win = tk.Toplevel(root)
    win.title("Import Playlist — Desktop Karaoke")
    win.configure(bg=_BG)
    win.resizable(False, False)
    win.geometry("560x560")
    win.attributes("-topmost", True)
    win.focus_force()
    _state["win"] = win

    # ── source selector ──────────────────────────────────────────────────
    src_var = tk.StringVar(value="csv")
    src_row = tk.Frame(win, bg=_BG)
    src_row.pack(fill="x", padx=16, pady=(14, 0))
    tk.Label(src_row, text="Import from:", bg=_BG, fg=_FG, font=_FONT).pack(side="left")
    for val, label in (("csv", "Exportify CSV"), ("spotify", "Spotify OAuth"),
                       ("youtube", "YouTube Music")):
        tk.Radiobutton(
            src_row, text=label, variable=src_var, value=val,
            bg=_BG, fg=_FG, selectcolor=_BTN_BG,
            activebackground=_BG, activeforeground=_FG, font=_FONT,
            command=lambda: _switch_panel(src_var.get(), panels),
        ).pack(side="left", padx=8)

    # ── dynamic source panels ────────────────────────────────────────────
    panel_host = tk.Frame(win, bg=_BG)
    panel_host.pack(fill="x", padx=16, pady=8)

    panels: dict[str, tuple[tk.Frame, Callable]] = {}

    # ── CSV panel ────────────────────────────────────────────────────────
    csv_frame = tk.Frame(panel_host, bg=_BG)
    csv_path_var = tk.StringVar()

    tk.Label(csv_frame, text="CSV file:", bg=_BG, fg=_FG, font=_FONT).grid(
        row=0, column=0, sticky="w")
    csv_entry = tk.Entry(
        csv_frame, textvariable=csv_path_var, width=40,
        bg=_BTN_BG, fg=_FG, insertbackground=_FG, font=_FONT, relief="flat")
    csv_entry.grid(row=0, column=1, padx=4)
    tk.Button(
        csv_frame, text="Browse…", bg=_ACC, fg="white", font=_FONT, relief="flat",
        command=lambda: csv_path_var.set(
            filedialog.askopenfilename(
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
                title="Select Exportify CSV")),
    ).grid(row=0, column=2)
    tk.Label(
        csv_frame,
        text="Export playlists from exportify.net → save CSVs → pick one here.",
        bg=_BG, fg=_DIM, font=("Segoe UI", 8),
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 0))

    def _csv_run(job: ImportJob) -> None:
        p = csv_path_var.get().strip()
        if not p:
            _log(log_box, "⚠  Pick a CSV file first.")
            return
        if not Path(p).exists():
            _log(log_box, f"⚠  File not found: {p}")
            return
        _start_thread(job, log_box, prog, btn_import, btn_cancel,
                      lambda j: import_from_csv(p, j,
                                                translate=tr_var.get(),
                                                force=force_var.get()))

    panels["csv"] = (csv_frame, _csv_run)

    # ── Spotify panel ────────────────────────────────────────────────────
    sp_frame = tk.Frame(panel_host, bg=_BG)
    sp_cid_var = tk.StringVar()
    # Pre-fill Client ID from saved config if present.
    try:
        cfg = Path(__file__).parent / "spotify_config.json"
        if cfg.exists():
            sp_cid_var.set(json.loads(cfg.read_text()).get("client_id", ""))
    except Exception:
        pass

    tk.Label(sp_frame, text="Client ID:", bg=_BG, fg=_FG, font=_FONT).grid(
        row=0, column=0, sticky="w")
    tk.Entry(
        sp_frame, textvariable=sp_cid_var, width=44,
        bg=_BTN_BG, fg=_FG, insertbackground=_FG, font=_FONT, relief="flat",
    ).grid(row=0, column=1, padx=4)
    liked_var = tk.BooleanVar()
    tk.Checkbutton(
        sp_frame, text="Include Liked Songs", variable=liked_var,
        bg=_BG, fg=_FG, selectcolor=_BTN_BG,
        activebackground=_BG, activeforeground=_FG, font=_FONT,
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
    tk.Label(
        sp_frame,
        text=("Create a free Spotify Developer App → Dashboard → Client ID.\n"
              "Set redirect URI to:  http://localhost:8888/callback"),
        bg=_BG, fg=_DIM, font=("Segoe UI", 8),
    ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(3, 0))

    def _sp_run(job: ImportJob) -> None:
        cid = sp_cid_var.get().strip() or None
        _start_thread(job, log_box, prog, btn_import, btn_cancel,
                      lambda j: import_from_spotify(cid, j,
                                                    include_liked=liked_var.get(),
                                                    translate=tr_var.get(),
                                                    force=force_var.get()))

    panels["spotify"] = (sp_frame, _sp_run)

    # ── YouTube Music panel ───────────────────────────────────────────────
    yt_frame = tk.Frame(panel_host, bg=_BG)
    yt_url_var   = tk.StringVar()
    browser_var  = tk.StringVar(value="brave")
    cookies_var  = tk.StringVar()

    tk.Label(yt_frame, text="Playlist URL:", bg=_BG, fg=_FG, font=_FONT).grid(
        row=0, column=0, sticky="w")
    tk.Entry(
        yt_frame, textvariable=yt_url_var, width=46,
        bg=_BTN_BG, fg=_FG, insertbackground=_FG, font=_FONT, relief="flat",
    ).grid(row=0, column=1, columnspan=3, padx=4)
    tk.Label(
        yt_frame,
        text="Type 'LM' for Liked Music. Separate multiple URLs with spaces.",
        bg=_BG, fg=_DIM, font=("Segoe UI", 8),
    ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(2, 4))

    tk.Label(yt_frame, text="Browser:", bg=_BG, fg=_FG, font=_FONT).grid(
        row=2, column=0, sticky="w")
    for col, br in enumerate(("brave", "chrome", "firefox", "edge"), start=1):
        tk.Radiobutton(
            yt_frame, text=br.capitalize(), variable=browser_var, value=br,
            bg=_BG, fg=_FG, selectcolor=_BTN_BG,
            activebackground=_BG, activeforeground=_FG, font=_FONT,
        ).grid(row=2, column=col, sticky="w")

    tk.Label(yt_frame, text="cookies.txt:", bg=_BG, fg=_FG, font=_FONT).grid(
        row=3, column=0, sticky="w", pady=(4, 0))
    tk.Entry(
        yt_frame, textvariable=cookies_var, width=36,
        bg=_BTN_BG, fg=_FG, insertbackground=_FG, font=_FONT, relief="flat",
    ).grid(row=3, column=1, columnspan=2, padx=4, sticky="w", pady=(4, 0))
    tk.Button(
        yt_frame, text="Browse…", bg=_BTN_BG, fg=_FG, font=_FONT, relief="flat",
        command=lambda: cookies_var.set(
            filedialog.askopenfilename(
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
                title="Select cookies.txt")),
    ).grid(row=3, column=3, pady=(4, 0))
    tk.Label(
        yt_frame,
        text="Close the browser before importing (it locks its cookie DB), or use cookies.txt.",
        bg=_BG, fg=_DIM, font=("Segoe UI", 8), wraplength=520, justify="left",
    ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(3, 0))

    def _yt_run(job: ImportJob) -> None:
        raw = yt_url_var.get().strip()
        if not raw:
            _log(log_box, "⚠  Enter a playlist URL or 'LM' for Liked Music.")
            return
        urls = raw.split()
        ck = cookies_var.get().strip() or None
        br = None if ck else browser_var.get()
        _start_thread(job, log_box, prog, btn_import, btn_cancel,
                      lambda j: import_from_youtube(urls, j, browser=br, cookies=ck,
                                                    translate=tr_var.get(),
                                                    force=force_var.get()))

    panels["youtube"] = (yt_frame, _yt_run)

    # ── options ──────────────────────────────────────────────────────────
    opts_row = tk.Frame(win, bg=_BG)
    opts_row.pack(fill="x", padx=16)
    tr_var    = tk.BooleanVar()
    force_var = tk.BooleanVar()
    for text, var in (("Translate to English (slow)", tr_var),
                      ("Force re-fetch (overwrite cache)", force_var)):
        tk.Checkbutton(
            opts_row, text=text, variable=var,
            bg=_BG, fg=_FG, selectcolor=_BTN_BG,
            activebackground=_BG, activeforeground=_FG, font=_FONT,
        ).pack(side="left", padx=(0, 16))

    # ── progress bar ─────────────────────────────────────────────────────
    prog_row = tk.Frame(win, bg=_BG)
    prog_row.pack(fill="x", padx=16, pady=(8, 0))
    style = ttk.Style(win)
    style.theme_use("clam")
    style.configure("Import.Horizontal.TProgressbar",
                    troughcolor=_BTN_BG, background=_ACC,
                    bordercolor=_BG, lightcolor=_ACC, darkcolor=_ACC)
    prog = ttk.Progressbar(
        prog_row, style="Import.Horizontal.TProgressbar",
        orient="horizontal", mode="determinate", maximum=100)
    prog.pack(fill="x")

    # ── log area ─────────────────────────────────────────────────────────
    log_box = scrolledtext.ScrolledText(
        win, height=12, bg=_BTN_BG, fg=_FG,
        insertbackground=_FG, font=_MONO, relief="flat", state="disabled")
    log_box.pack(fill="both", expand=True, padx=16, pady=(6, 0))

    # ── buttons ──────────────────────────────────────────────────────────
    btn_row = tk.Frame(win, bg=_BG)
    btn_row.pack(fill="x", padx=16, pady=10)

    btn_cancel = tk.Button(
        btn_row, text="Cancel", bg=_BTN_BG, fg="#888",
        font=_FONT, relief="flat", state="disabled",
        command=lambda: _do_cancel(log_box))
    btn_cancel.pack(side="right", padx=(4, 0))

    btn_import = tk.Button(
        btn_row, text="Import", bg=_ACC, fg="white",
        font=_FONT, relief="flat",
        command=lambda: _do_start(src_var, panels, prog, log_box,
                                  btn_import, btn_cancel, tr_var, force_var))
    btn_import.pack(side="right")

    # show the first panel
    _switch_panel("csv", panels)

    _log(log_box, "Ready — pick a source and click Import.")
    win.protocol("WM_DELETE_WINDOW", win.destroy)


# ── helpers ────────────────────────────────────────────────────────────────────

def _switch_panel(src: str, panels: dict) -> None:
    for frame, _ in panels.values():
        frame.pack_forget()
    panels[src][0].pack(fill="x")


def _do_start(src_var, panels, prog, log_box, btn_import, btn_cancel,
              tr_var, force_var) -> None:
    existing = _state.get("job")
    if existing and not existing.cancelled and existing.done < existing.total:
        _log(log_box, "⚠  Import already running — cancel it first.")
        return
    # Fresh job; the source panel's run function fills it.
    panels[src_var.get()][1](None)   # run fn creates the job itself


def _do_cancel(log_box) -> None:
    job = _state.get("job")
    if job:
        job.cancel()
    _log(log_box, "Cancelling after current track…")


def _start_thread(
    _job_unused,          # kept for signature compat; we create the job here
    log_box,
    prog,
    btn_import,
    btn_cancel,
    import_fn: Callable,
) -> None:
    """Create an ImportJob, wire its on_progress to the GUI, then start the import."""
    win = _state.get("win")

    def on_progress(job: ImportJob) -> None:
        if not (win and _win_alive(win)):
            return
        title, artist = job.last_track
        label = {"ok": "OK  ", "skip": "SKIP", "miss": "MISS", "err": "ERR "}.get(
            job.last_result, "    ")
        line = f"[{job.done}/{job.total}] {label} {title}"
        if artist:
            line += f" — {artist}"
        pct = job.pct
        try:
            win.after(0, lambda: _gui_tick(pct, line, prog, log_box))
        except Exception:
            pass

    job = ImportJob(on_progress=on_progress)
    _state["job"] = job

    def _run() -> None:
        if win and _win_alive(win):
            win.after(0, lambda: btn_import.config(state="disabled"))
            win.after(0, lambda: btn_cancel.config(state="normal", fg=_FG))
            win.after(0, lambda: _log(log_box, "Starting…"))
        try:
            import_fn(job)
        except Exception as exc:
            if win and _win_alive(win):
                win.after(0, lambda msg=str(exc): _log(log_box, f"Error: {msg}"))
        finally:
            if win and _win_alive(win):
                summary = job.summary
                win.after(0, lambda: btn_import.config(state="normal"))
                win.after(0, lambda: btn_cancel.config(state="disabled", fg="#888"))
                win.after(0, lambda s=summary: _log(log_box, f"\nDone — {s}"))

    threading.Thread(target=_run, daemon=True).start()


def _gui_tick(pct: float, line: str, prog, log_box) -> None:
    try:
        prog["value"] = pct
        _log(log_box, line)
    except Exception:
        pass


def _log(log_box: scrolledtext.ScrolledText, msg: str) -> None:
    try:
        log_box.config(state="normal")
        log_box.insert("end", msg + "\n")
        log_box.see("end")
        log_box.config(state="disabled")
    except Exception:
        pass


def _win_alive(win: tk.Toplevel) -> bool:
    try:
        return win.winfo_exists()
    except Exception:
        return False
