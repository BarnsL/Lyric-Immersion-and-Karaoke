<#
  build_msix.ps1 - Package Desktop Karaoke into a Microsoft Store .msix.

  Run from anywhere; paths are resolved relative to the repo.

  Local TEST package (signed with your dev cert so you can install it yourself):
      .\packaging\build_msix.ps1 -CertThumbprint C40DBB1204E80FA38A3CDDF9736551B7B211FD6A

  STORE package (identity comes from Partner Center; leave it UNSIGNED - the
  Store re-signs it on submission). See STORE_SUBMISSION.md for the values:
      .\packaging\build_msix.ps1 `
          -IdentityName    "12345BarnsL.DesktopKaraoke" `
          -Publisher       "CN=ABCDEF01-2345-6789-ABCD-EF0123456789" `
          -PublisherDisplay "Your registered seller name" `
          -Version         "1.0.0.0"
#>
param(
  [string]$IdentityName     = "BarnsL.DesktopKaraoke",
  [string]$Publisher        = "CN=BarnsL Website Console Dev",
  [string]$PublisherDisplay = "BarnsL",
  [string]$Version          = "1.0.0.0",
  [string]$CertThumbprint   = "",
  [switch]$SkipBuild
)
$ErrorActionPreference = "Stop"

$repo    = Split-Path -Parent $PSScriptRoot
$venvPy  = Join-Path $repo ".venv\Scripts\python.exe"
$dist    = Join-Path $repo "dist"
$bundle  = Join-Path $dist "DesktopKaraoke"
$layout  = Join-Path $dist "msix"
$assets  = Join-Path $layout "Assets"
$msix    = Join-Path $dist "DesktopKaraoke.msix"

function Find-SdkTool($name) {
  Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin\*\x64\$name" -ErrorAction SilentlyContinue |
    Sort-Object FullName | Select-Object -Last 1 -ExpandProperty FullName
}
$makeappx = Find-SdkTool "makeappx.exe"
$makepri  = Find-SdkTool "makepri.exe"
$signtool = Find-SdkTool "signtool.exe"
if (-not $makeappx) { throw "makeappx.exe not found - install the Windows 10/11 SDK." }
if (-not (Test-Path $venvPy)) { throw "venv python not found at $venvPy - create the .venv and pip install -r requirements.txt pyinstaller first." }

# 1) Build the frozen app (PyInstaller).
if ($SkipBuild -and (Test-Path "$bundle\Lyric-Immersion-and-Karaoke.exe")) {
  Write-Host "[1/5] Reusing existing bundle at $bundle" -ForegroundColor Cyan
} else {
  Write-Host "[1/5] Building app bundle (PyInstaller)..." -ForegroundColor Cyan
  & $venvPy -m PyInstaller --noconfirm --log-level WARN (Join-Path $repo "DesktopKaraoke.spec")
  if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
}

# 2) Stage the package layout (app files at the package root).
Write-Host "[2/5] Staging package layout..." -ForegroundColor Cyan
if (Test-Path $layout) { Remove-Item $layout -Recurse -Force }
New-Item -ItemType Directory -Path $layout | Out-Null
Copy-Item "$bundle\*" $layout -Recurse -Force

# 3) Generate the Store logo assets from the app icon.
Write-Host "[3/5] Generating Store logo assets..." -ForegroundColor Cyan
Push-Location $repo
& $venvPy "scripts/make_assets.py" $assets
$rc = $LASTEXITCODE
Pop-Location
if ($rc -ne 0) { throw "Asset generation failed." }

# 4) Write AppxManifest.xml from the template, then build the resource index.
Write-Host "[4/5] Writing AppxManifest.xml (Name=$IdentityName, Publisher=$Publisher)..." -ForegroundColor Cyan
$tpl = Get-Content (Join-Path $PSScriptRoot "AppxManifest.template.xml") -Raw
$tpl = $tpl.Replace("{{IDENTITY_NAME}}",    $IdentityName).
            Replace("{{PUBLISHER}}",        $Publisher).
            Replace("{{PUBLISHER_DISPLAY}}",$PublisherDisplay).
            Replace("{{VERSION}}",          $Version)
$manifestPath = Join-Path $layout "AppxManifest.xml"
[System.IO.File]::WriteAllText($manifestPath, $tpl, (New-Object System.Text.UTF8Encoding($false)))

if ($makepri) {
  try {
    $pricfg = Join-Path $dist "priconfig.xml"
    & $makepri createconfig /cf $pricfg /dq en-US /o 2>&1 | Out-Null
    & $makepri new /pr $layout /cf $pricfg /mn $manifestPath /of (Join-Path $layout "resources.pri") /o 2>&1 | Out-Null
    if (Test-Path (Join-Path $layout "resources.pri")) { Write-Host "      resources.pri built." -ForegroundColor DarkGray }
  } catch { Write-Host "      (makepri skipped: $($_.Exception.Message))" -ForegroundColor DarkYellow }
}

# 5) Pack into a single .msix (and optionally sign for local install testing).
Write-Host "[5/5] Packing -> $msix" -ForegroundColor Cyan
& $makeappx pack /o /d $layout /p $msix
if ($LASTEXITCODE -ne 0) { throw "makeappx pack failed." }

if ($CertThumbprint) {
  Write-Host "Signing with cert $CertThumbprint ..." -ForegroundColor Cyan
  & $signtool sign /sha1 $CertThumbprint /fd SHA256 /a $msix
  if ($LASTEXITCODE -ne 0) { throw "Signing failed - does the cert's subject (CN=...) exactly match the manifest Publisher '$Publisher'?" }
}

Write-Host ""
Write-Host ("Done -> {0}  ({1:N1} MB)" -f $msix, ((Get-Item $msix).Length/1MB)) -ForegroundColor Green
if (-not $CertThumbprint) {
  Write-Host "Unsigned: ready to UPLOAD to Partner Center (the Store signs it). For a local install test, re-run with -CertThumbprint." -ForegroundColor DarkGray
}
