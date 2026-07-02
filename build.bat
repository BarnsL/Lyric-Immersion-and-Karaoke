@echo off
REM Build Lyric Immersion and Karaoke into a portable folder and, if Inno Setup is
REM    installed, a one-click Setup installer). Run this from the repo folder.
setlocal

echo [1/4] Installing build + app dependencies...
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
echo [2/4] Building Lyric-Immersion-and-Karaoke.exe ...
python -m PyInstaller --noconfirm DesktopKaraoke.spec || goto :err
echo     -> dist\DesktopKaraoke\Lyric-Immersion-and-Karaoke.exe

echo [3/4] Building the installer (optional) ...
where iscc >nul 2>nul
if %errorlevel%==0 (
    iscc installer.iss && echo     -> dist\Lyric-Immersion-and-Karaoke-Setup.exe
) else (
    echo     Inno Setup ^(iscc^) not found - skipping installer.
    echo     dist\DesktopKaraoke\Lyric-Immersion-and-Karaoke.exe is portable: double-click to run.
)

echo.
echo [4/4] Done.
echo.
echo  Outputs:
echo    dist\DesktopKaraoke\Lyric-Immersion-and-Karaoke.exe  (portable)
if exist dist\Lyric-Immersion-and-Karaoke-Setup.exe echo    dist\Lyric-Immersion-and-Karaoke-Setup.exe        (installer)
echo.
goto :eof

:err
echo.
echo Build failed - see the messages above.
exit /b 1
