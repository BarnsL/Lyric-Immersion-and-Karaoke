# Code-sign the app exe, the GPU overlay exe, and the Setup.exe.
#
# WHY: an UNSIGNED PyInstaller exe is flagged by Windows Defender / SmartScreen on
# clean machines (works locally only because the dev box has exclusions). A code
# signature is the one reliable fix. Version metadata (version_info.txt) lowers the
# false-positive rate but does NOT remove the "unknown publisher" wall — only a
# signature does.
#
# USAGE
#   Real certificate (public distribution — the correct fix):
#     .\sign.ps1 -PfxPath C:\path\to\codesign.pfx -PfxPassword 'secret'
#   Self-signed (ONLY makes it trusted on machines where you install the cert —
#   fine for your own / a few known test PCs, NOT for public release):
#     .\sign.ps1 -SelfSigned            # creates + reuses a cert in CurrentUser\My
#     .\sign.ps1 -SelfSigned -ExportCert publisher.cer   # export to install on the test PC
#
# On the OTHER computer, to trust a SELF-SIGNED build, run (as admin) once:
#     Import-Certificate -FilePath publisher.cer -CertStoreLocation Cert:\LocalMachine\Root
#     Import-Certificate -FilePath publisher.cer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher
param(
  [string]$PfxPath,
  [string]$PfxPassword,
  [switch]$SelfSigned,
  [string]$ExportCert,
  [string]$Subject = "CN=Purple Industries",
  [string]$TimestampUrl = "http://timestamp.digicert.com"
)
$ErrorActionPreference = "Stop"

$targets = @(
  "dist\DesktopKaraoke\Lyric-Immersion-and-Karaoke.exe",
  "dist\DesktopKaraoke\overlay\lyric-overlay.exe",
  "dist\Lyric-Immersion-and-Karaoke-Setup.exe"
) | Where-Object { Test-Path $_ }

if (-not $targets) { Write-Error "No build artifacts found under .\dist — build first."; return }

# ── resolve the signing certificate ──────────────────────────────────────────
if ($PfxPath) {
  if (-not (Test-Path $PfxPath)) { Write-Error "PFX not found: $PfxPath"; return }
  $sec = if ($PfxPassword) { ConvertTo-SecureString $PfxPassword -AsPlainText -Force } else { $null }
  $cert = if ($sec) { Get-PfxCertificate -FilePath $PfxPath -Password $sec }
          else       { Get-PfxCertificate -FilePath $PfxPath }
  Write-Host "Signing with certificate from $PfxPath"
}
elseif ($SelfSigned) {
  $cert = Get-ChildItem Cert:\CurrentUser\My | Where-Object { $_.Subject -eq $Subject -and $_.HasPrivateKey } |
          Sort-Object NotAfter -Descending | Select-Object -First 1
  if (-not $cert) {
    Write-Host "Creating a self-signed code-signing cert ($Subject) in CurrentUser\My..."
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $Subject `
              -CertStoreLocation Cert:\CurrentUser\My -KeyUsage DigitalSignature `
              -NotAfter (Get-Date).AddYears(5)
  }
  Write-Host "Self-signed cert thumbprint: $($cert.Thumbprint)"
  Write-Host "NOTE: other machines will NOT trust this until you install the .cer (see header)."
  if ($ExportCert) {
    Export-Certificate -Cert $cert -FilePath $ExportCert | Out-Null
    Write-Host "Exported public cert -> $ExportCert (install on the test PC's Trusted Root + Trusted Publisher)."
  }
}
else {
  Write-Error "Pass -PfxPath <file> for a real cert, or -SelfSigned for a test cert. See header for usage."
  return
}

# ── sign every artifact (with an RFC-3161 timestamp so it stays valid past cert expiry) ──
$fail = 0
foreach ($t in $targets) {
  try {
    $r = Set-AuthenticodeSignature -FilePath $t -Certificate $cert `
           -HashAlgorithm SHA256 -TimestampServer $TimestampUrl
    Write-Host ("  {0,-52} -> {1}" -f (Split-Path $t -Leaf), $r.Status)
    if ($r.Status -ne "Valid") { $fail++ }
  } catch {
    Write-Host ("  {0,-52} -> ERROR {1}" -f (Split-Path $t -Leaf), $_.Exception.Message)
    $fail++
  }
}
if ($fail) { Write-Error "$fail artifact(s) failed to sign." } else { Write-Host "`nAll artifacts signed." }
