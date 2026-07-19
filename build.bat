@echo off
REM Build Lyric Immersion and Karaoke into a portable folder and, if Inno Setup is
REM    installed, a one-click Setup installer). Run this from the repo folder.
setlocal

REM TICKET-196: pick the interpreter whose ABI matches ./.deps (cp312), NOT the
REM bare `python` on PATH. On the Windows build box that resolves to a 3.11 agent
REM venv, and building cp311 against cp312 .deps produces an app that starts fine
REM and has a silently dead whisper stack. The spec now refuses that outright, so
REM without this line every documented build command aborts.
set "PY=python"
py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
for /f "delims=" %%v in ('%PY% -c "import sys;print('%%d.%%d'%%sys.version_info[:2])" 2^>nul') do set "PYVER=%%v"
echo [0/5] Build interpreter: %PY%  (Python %PYVER%)
if not "%PYVER%"=="3.12" (
    echo     [!] WARNING: .deps is built for cp312. If this build fails in the spec
    echo         ABI check, install Python 3.12 or set PY to its python.exe.
)

echo [1/5] Installing build + app dependencies...
%PY% -m pip install --upgrade pip >nul
%PY% -m pip install pyinstaller -r requirements.txt || goto :err

echo.
echo ????????????????????????????????????????????????????????????
echo ?  Bundle faster-whisper into the portable build?          ?
echo ?  This adds ~150 MB but enables AI lyric generation and   ?
echo ?  sync-by-listening out of the box for end users.         ?
echo ????????????????????????????????????????????????????????????
choice /C YN /M "Bundle faster-whisper (recommended)?"
if %errorlevel%==1 (
    echo Installing faster-whisper for bundling...
    %PY% -m pip install faster-whisper>=1.0 || echo [!] Warning: faster-whisper install failed - build will continue without it.
)

echo.
echo [1b/5] Checking the AI stack (.deps vs build env) is version-consistent ...
REM TICKET-177: a version SKEW between the vendored .deps and the build env bundles
REM mismatched PyAV modules + FFmpeg DLLs and silently breaks faster-whisper import
REM at runtime (av._core) - which killed generate-by-ear, sync-by-listening AND the
REM wrong-lyrics reject path in every shipped build v1.1.74..v1.1.76 with no log.
REM This warns on a version diff (dist-info can lag the real module) and hard-fails
REM only on DUPLICATE dist-info (unambiguous corruption); the post-build --selftest
REM below is the definitive gate. Remediation is printed by the check.
%PY% scripts\check_build_deps.py || goto :err

echo.
echo [1c/5] Verifying PyAV's FFmpeg DLL imports resolve (the DIRECT skew check) ...
REM TICKET-176: check_build_deps.py above compares VERSION STRINGS, which is only a
REM proxy - dist-info can lag the real module files, and the failure is about DLL
REM FILE IDENTITY. This parses av/_core.pyd's PE import table and asserts every
REM FFmpeg DLL it imports is actually present in av.libs (the exact thing that
REM broke: a stale .deps _core.pyd + a newer av.libs => unresolved imports, so
REM `import av` dies at runtime with "DLL load failed while importing _core").
REM Stdlib-only, so it can't be silently skipped for a missing pefile.
%PY% scripts\check_av_dlls.py || goto :err

echo.
echo [2/5] Building Lyric-Immersion-and-Karaoke.exe ...
%PY% -m PyInstaller --noconfirm DesktopKaraoke.spec || goto :err
REM v1.1.62 BUGFIX: '->' in cmd is `-` + `>` (redirect), which OVERWROTE the
REM freshly-built exe with 7 bytes of "    -\r\n" — every build was silently
REM broken. Escape the `>` with `^>` so it stays literal text in the echo.
echo     -^> dist\DesktopKaraoke\Lyric-Immersion-and-Karaoke.exe

echo.
echo [2b/5] Verifying the BUNDLED PyAV FFmpeg DLLs resolve (av._core PE imports) ...
REM TICKET-176: the same check against the ACTUAL shipped bundle, which catches a
REM PyInstaller collection that pulled mismatched pieces even when the build env
REM was clean. Runs WITHOUT launching the exe, so it names the missing DLL instead
REM of the --selftest's opaque exit code - run both, this one first.
%PY% scripts\check_av_dlls.py --internal dist\DesktopKaraoke || goto :err

echo.
echo [2c/5] Smoke-testing the built app's AI stack (whisper actually imports) ...
REM TICKET-175 BACKSTOP: prove the FINISHED .exe can import av + faster-whisper and
REM that align.available() is True. --selftest runs before any GUI init and exits
REM 0/1 (writing a one-line verdict to the --out file), so a whisper-broken bundle
REM fails the build here even if the pre-build .deps check passed. -Wait -PassThru
REM reliably waits for the windowed exe and returns its exit code; no window appears.
powershell -NoProfile -Command ^
  "$exe='dist\DesktopKaraoke\Lyric-Immersion-and-Karaoke.exe'; $out='dist\selftest.txt';" ^
  "if (Test-Path $out) { Remove-Item $out -Force };" ^
  "$p=Start-Process -FilePath $exe -ArgumentList '--selftest','--out',$out -Wait -PassThru;" ^
  "if (Test-Path $out) { Write-Host ('    '+((Get-Content $out -Raw).Trim())) };" ^
  "exit $p.ExitCode"
if errorlevel 1 (
    echo [!] SELFTEST FAILED - refusing to package a build whose AI/whisper stack is broken.
    echo     See docs/BUILD.md ^(TICKET-175^): rebuild .deps to match the build env.
    goto :err
)

echo [3/5] Building the installer (optional) ...
where iscc >nul 2>nul
if %errorlevel%==0 (
    iscc installer.iss && echo     -^> dist\Lyric-Immersion-and-Karaoke-Setup.exe
) else (
    echo     Inno Setup ^(iscc^) not found - skipping installer.
    echo     dist\DesktopKaraoke\Lyric-Immersion-and-Karaoke.exe is portable: double-click to run.
)

echo.
echo [4/5] Packaging the portable zip + SHA-256 (the in-app updater's asset) ...
REM The in-app updater (updater.py) self-updates ONLY from a release .zip of the
REM onedir build, verified against a "<zip>.sha256" companion asset. Every
REM release MUST upload BOTH alongside the Setup.exe or deployed apps cannot
REM auto-update (they fall back to opening the Releases page in a browser).
powershell -NoProfile -Command ^
  "$v=(Select-String -Path version.py -Pattern '\"([0-9][0-9.]*)\"').Matches[0].Groups[1].Value;" ^
  "$zip='dist\Lyric-Immersion-and-Karaoke-'+$v+'.zip';" ^
  "Compress-Archive -Path 'dist\DesktopKaraoke\*' -DestinationPath $zip -Force;" ^
  "$h=(Get-FileHash $zip -Algorithm SHA256).Hash.ToLower();" ^
  "Set-Content -Path ($zip+'.sha256') -Value $h -Encoding ascii -NoNewline;" ^
  "Write-Host ('    -> '+$zip); Write-Host ('    -> '+$zip+'.sha256  ('+$h+')')" || echo     [!] zip packaging failed

echo.
echo [5/5] Done.
echo.
echo  Outputs:
echo    dist\DesktopKaraoke\Lyric-Immersion-and-Karaoke.exe  (portable)
if exist dist\Lyric-Immersion-and-Karaoke-Setup.exe echo    dist\Lyric-Immersion-and-Karaoke-Setup.exe        (installer)
echo.
echo  Release checklist: upload Setup.exe + the .zip + the .zip.sha256 together
echo  (gh release create vX.Y.Z dist\...-Setup.exe dist\...-X.Y.Z.zip dist\...-X.Y.Z.zip.sha256)
goto :eof

:err
echo.
echo Build failed - see the messages above.
exit /b 1
