@echo off
REM ── Build Desktop Karaoke into a single .exe (and, if Inno Setup is
REM    installed, a one-click Setup installer). Run this from the repo folder.
setlocal

echo [1/3] Installing build + app dependencies...
python -m pip install --upgrade pip >nul
python -m pip install pyinstaller -r requirements.txt || goto :err

echo [2/3] Building DesktopKaraoke.exe ...
python -m PyInstaller --noconfirm DesktopKaraoke.spec || goto :err
echo     -> dist\DesktopKaraoke.exe

echo [3/3] Building the installer (optional) ...
where iscc >nul 2>nul
if %errorlevel%==0 (
    iscc installer.iss && echo     -> dist\DesktopKaraoke-Setup.exe
) else (
    echo     Inno Setup ^(iscc^) not found - skipping installer.
    echo     dist\DesktopKaraoke.exe is portable: double-click to run.
)

echo.
echo Done.
goto :eof

:err
echo.
echo Build failed - see the messages above.
exit /b 1
