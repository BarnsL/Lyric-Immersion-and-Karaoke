@echo off
REM Build Lyric Immersion and Karaoke into a portable folder and, if Inno Setup is
REM    installed, a one-click Setup installer). Run this from the repo folder.
setlocal

echo [1/5] Installing build + app dependencies...
python -m pip install --upgrade pip >nul
python -m pip install pyinstaller -r requirements.txt || goto :err

echo.
echo ????????????????????????????????????????????????????????????
echo ?  Bundle faster-whisper into the portable build?          ?
echo ?  This adds ~150 MB but enables AI lyric generation and   ?
echo ?  sync-by-listening out of the box for end users.         ?
echo ????????????????????????????????????????????????????????????
choice /C YN /M "Bundle faster-whisper (recommended)?"
if %errorlevel%==1 (
    echo Installing faster-whisper for bundling...
    python -m pip install faster-whisper>=1.0 || echo [!] Warning: faster-whisper install failed - build will continue without it.
)

echo.
echo [2/5] Building Lyric-Immersion-and-Karaoke.exe ...
python -m PyInstaller --noconfirm DesktopKaraoke.spec || goto :err
REM v1.1.62 BUGFIX: '->' in cmd is `-` + `>` (redirect), which OVERWROTE the
REM freshly-built exe with 7 bytes of "    -\r\n" — every build was silently
REM broken. Escape the `>` with `^>` so it stays literal text in the echo.
echo     -^> dist\DesktopKaraoke\Lyric-Immersion-and-Karaoke.exe

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
