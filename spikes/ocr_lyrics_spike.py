r"""OCR-burned-in-lyrics spike — de-risks the ONE unknown before wiring ocr_lyrics.py
into main.py's source chain: can we capture the SOURCE video window's pixels
(PrintWindow) WITHOUT our own overlay bleeding in, and does Chromium come back with
real content or a BLACK frame?

RUN IT while a lyric/karaoke video with BURNED-IN lyrics is playing (Brave/Chrome
fullscreen-windowed or in a tab):

    python spikes/ocr_lyrics_spike.py
    python spikes/ocr_lyrics_spike.py "white balance"     # match a window by title substring
    python spikes/ocr_lyrics_spike.py 0x00123456          # target a specific HWND

WHAT IT PROVES (this is the whole bet):
  PASS — the saved band PNG shows the VIDEO's burned-in lyric line (NOT our overlay),
         and the OCR lines below it are that line's text. PrintWindow works → we can
         harvest lyrics with zero self-read risk.
  BLACK — PrintWindow returns an all-black frame (hardware-accelerated Chromium video
         surface). The spike says so and tries the full-screen FALLBACK; if the
         fallback band shows the video, we ship the fallback path + the overlay-text
         self-read guard (already in ocr_lyrics.filter_lyric_lines). NOT a blocker.

It saves PNGs to the scratchpad so you can EYEBALL exactly what was captured:
  dk_ocr_full.png   — the whole captured window/screen
  dk_ocr_band.png   — just the lyric band we OCR

Then it loops a few times to show the LyricOcrHarvester building a timed LRC from
the lines it reads. Console prints everything; no window is created by this script.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import ocr_lyrics as OL

SCRATCH = r"C:\Users\user\AppData\Local\Temp\claude\D--\1e3fdef9-0dbd-496e-8160-ecbb524749bf\scratchpad"

_BROWSER_PROCS = {"brave.exe", "chrome.exe", "msedge.exe", "firefox.exe",
                  "vivaldi.exe", "opera.exe", "arc.exe", "spotify.exe",
                  "steamwebhelper.exe"}


def _enum_target(title_sub: str | None):
    """Find the best source window: a visible browser/player window, preferring one
    whose title contains `title_sub` (case-insensitive) or a music marker, then the
    largest by area. Returns (hwnd, title, exe, (w,h)) or None."""
    user32 = ctypes.windll.user32
    try:
        from window_titles import _exe_basename_for_pid, _safe_window_text
    except Exception:
        _exe_basename_for_pid = lambda pid: None  # noqa: E731
        _safe_window_text = lambda h: ""           # noqa: E731

    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _l):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            exe = (_exe_basename_for_pid(int(pid.value)) or "").lower()
            if exe not in _BROWSER_PROCS:
                return True
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            w, h = r.right - r.left, r.bottom - r.top
            if w < 320 or h < 240:
                return True
            title = _safe_window_text(int(hwnd)) or ""
            found.append((int(hwnd), title, exe, w * h, (w, h)))
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    if not found:
        return None

    def _score(item):
        hwnd, title, exe, area, wh = item
        s = area
        tl = title.lower()
        if title_sub and title_sub.lower() in tl:
            s += 10 ** 12
        for mk in ("youtube", "spotify", "niconico", "- ", "｜", "music"):
            if mk in tl:
                s += 10 ** 9
                break
        return s

    hwnd, title, exe, area, wh = max(found, key=_score)
    return hwnd, title, exe, wh


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    hwnd = None
    title_sub = None
    if arg:
        a = arg.strip()
        if a.lower().startswith("0x") or a.isdigit():
            try:
                hwnd = int(a, 0)
            except Exception:
                hwnd = None
        else:
            title_sub = a

    print("ocr_lyrics.available():", OL.available())
    print("ocr langs:", OL.ocr_langs())
    if not OL.available():
        print("!! Windows OCR engine not available — install a Language.OCR pack.")
        return

    if hwnd is None:
        tgt = _enum_target(title_sub)
        if not tgt:
            print("!! No browser/player window found. Is the video playing in "
                  "Brave/Chrome? You can also pass a title substring or 0xHWND.")
            return
        hwnd, title, exe, wh = tgt
        print(f"target window: hwnd=0x{hwnd:08X}  exe={exe}  size={wh}  title={title!r}")
    else:
        title = ""
        print(f"target window: hwnd=0x{hwnd:08X} (from argv)")

    # derive the page's song/artist text from the window title (drop the site/app
    # suffix) so the meta filter can strip title/channel/up-next bleed-through.
    import re as _re
    meta_title = _re.sub(r"\s*[-–—|｜]\s*(?:YouTube|Brave|Chrome|ニコニコ動画|"
                         r"Niconico)\b.*$", "", title or "", flags=_re.I).strip()
    print(f"meta filter (title/artist hint): {meta_title!r}")

    fs = OL.is_fullscreen(hwnd)
    print(f"is_fullscreen: {fs}   "
          + ("(band is pure video — burned-in lyrics readable)" if fs else
             "(WINDOWED — band is YouTube UI; production would NOT harvest here)"))

    # ── 1) PrintWindow capture (the self-read-safe path) ──────────────────────
    img = OL.capture_source_window(hwnd)
    if img is None:
        print("PrintWindow: FAILED to capture (no image). Will try full-screen fallback.")
        black = True
    else:
        black = OL._looks_black(img)
        print(f"PrintWindow: captured {img.size}  black={black}")
        try:
            os.makedirs(SCRATCH, exist_ok=True)
            img.save(os.path.join(SCRATCH, "dk_ocr_full.png"))
            OL._crop_band(img).save(os.path.join(SCRATCH, "dk_ocr_band.png"))
            print(f"  saved dk_ocr_full.png + dk_ocr_band.png to scratchpad — EYEBALL these")
        except Exception as e:
            print("  (could not save preview PNGs:", e, ")")

    if img is not None and not black:
        raw = OL._ocr_image(OL._crop_band(img))
        lines = OL.filter_lyric_lines(raw, track_title=meta_title)
        print("  >> raw OCR (band):", raw or "(none)")
        print("  >> filtered lyric lines:", lines or "(none — correct if this video has NO burned-in lyrics)")
        print("  >> VERDICT: PrintWindow PATH WORKS — wire ocr_lyrics with hwnd capture, zero self-read.")
    else:
        print("  PrintWindow gave black/none → trying full-screen FALLBACK grab...")
        try:
            from PIL import ImageGrab
            full = ImageGrab.grab()
            full.save(os.path.join(SCRATCH, "dk_ocr_full.png"))
            band = OL._crop_band(full)
            band.save(os.path.join(SCRATCH, "dk_ocr_band.png"))
            lines = OL.filter_lyric_lines(OL._ocr_image(band), overlay_text=None)
            print("  >> Fallback OCR lyric lines:", lines or "(none)")
            print("  >> VERDICT: ship FALLBACK path (full-screen + overlay-text self-read guard).")
            print("     NOTE: this band MAY include our overlay — pass the live overlay text to")
            print("           filter_lyric_lines() in production so it's dropped.")
        except Exception as e:
            print("  fallback grab failed:", e)
            return

    # ── 2) demo the timed harvester over a few polls ──────────────────────────
    print("\nHarvesting ~6 polls @1.3s to demo the timed-LRC builder "
          "(let the lyrics advance)...")
    harv = OL.LyricOcrHarvester(stable_polls=2)
    t0 = time.perf_counter()
    for i in range(6):
        t = time.perf_counter() - t0
        lines = OL.read_lyric_lines(hwnd=hwnd, track_title=meta_title)
        committed = harv.observe(t, lines)
        tag = f"  committed: {committed!r}" if committed else ""
        print(f"  poll {i} @{t:4.1f}s: {lines or '(none)'}{tag}")
        time.sleep(1.3)
    print("\n--- harvested LRC ---")
    print(harv.to_lrc() or "(nothing committed — lyrics may not have advanced, or band is wrong)")


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("Windows-only (PrintWindow + Windows.Media.Ocr).")
    main()
