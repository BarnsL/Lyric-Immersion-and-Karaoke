@echo off
REM ── Desktop Karaoke: Install Optional Features ──────────────────────────
REM Run this after "pip install -r requirements.txt" to enable extra features.
REM Each feature is offered as a choice — skip what you don't want.
setlocal

echo.
echo ╔══════════════════════════════════════════════════════════╗
echo ║   Desktop Karaoke — Optional Feature Installer          ║
echo ╚══════════════════════════════════════════════════════════╝
echo.
echo This script installs optional packages that unlock extra features.
echo The core app works fine without them — these add AI capabilities.
echo.

REM ── 1. faster-whisper (Generate lyrics by ear + Sync by listening) ──
echo ┌──────────────────────────────────────────────────────────┐
echo │  faster-whisper  (~150 MB download)                      │
echo │  Enables: ✦ Generate lyrics by ear (AI transcription)    │
echo │           ✦ Sync by listening (match lyrics to audio)    │
echo │  Without it: these features show a hint and skip.        │
echo └──────────────────────────────────────────────────────────┘
choice /C YN /M "Install faster-whisper?"
if %errorlevel%==1 (
    echo Installing faster-whisper...
    python -m pip install faster-whisper>=1.0 || (
        echo [!] Failed to install faster-whisper. Check your Python/pip.
        echo     You can retry manually: pip install faster-whisper
    )
    echo.
) else (
    echo Skipped.
    echo.
)

REM ── 2. yt-dlp (Deep transcription — download + transcribe full songs) ──
echo ┌──────────────────────────────────────────────────────────┐
echo │  yt-dlp  (~20 MB download)                               │
echo │  Enables: ✦ Deep transcription (download source audio    │
echo │             + transcribe the whole song for a clean,     │
echo │             complete AI-generated lyric)                  │
echo │  Requires: faster-whisper (above) + Node.js or Deno      │
echo │            on PATH for YouTube downloads.                 │
echo │  Without it: the instant by-ear mode still works.        │
echo └──────────────────────────────────────────────────────────┘
choice /C YN /M "Install yt-dlp?"
if %errorlevel%==1 (
    echo Installing yt-dlp...
    python -m pip install yt-dlp>=2024.0 || (
        echo [!] Failed to install yt-dlp.
        echo     You can retry manually: pip install yt-dlp
    )
    echo.
) else (
    echo Skipped.
    echo.
)

REM ── 3. GPU acceleration (NVIDIA only) ──
echo ┌──────────────────────────────────────────────────────────┐
echo │  GPU Acceleration  (NVIDIA only, ~1.5 GB download)       │
echo │  Enables: ✦ Much faster transcription on NVIDIA GPUs     │
echo │  Without it: everything runs on CPU (still works fine).  │
echo │  Note: You can also enable this later from the tray menu │
echo │        (⚡ Enable GPU acceleration).                      │
echo └──────────────────────────────────────────────────────────┘
choice /C YN /M "Download GPU libraries now?"
if %errorlevel%==1 (
    echo Running GPU setup...
    python gpu_setup.py || (
        echo [!] GPU setup failed. You can try again later from the tray menu.
    )
    echo.
) else (
    echo Skipped. (You can always enable later from the tray icon.)
    echo.
)

echo.
echo ══════════════════════════════════════════════════════════════
echo  Done! Start the overlay with:  pythonw main.py
echo ══════════════════════════════════════════════════════════════
echo.
pause
