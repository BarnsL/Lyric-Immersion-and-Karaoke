# Publishing Desktop Karaoke to the Microsoft Store

This is the one-click install path for non-technical users: they open the
Microsoft Store, click **Get**, and the app installs with **no SmartScreen or
Smart App Control warnings** (the Store signs it with a Microsoft-trusted
identity) and **auto-updates**.

Everything technical is already done — a valid, Store-ready package is produced
by [`packaging/build_msix.ps1`](packaging/build_msix.ps1). What's left needs
your Partner Center account, so it can't be scripted. Follow these steps.

---

## 0. One-time: get a Partner Center developer account

1. Go to <https://partner.microsoft.com/dashboard/registration> and register for
   the **Windows & Xbox** program.
2. Pay the **one-time fee** (~$19 individual / ~$99 company). Use the **BarnsL**
   identity to keep accounts isolated.
3. Pick a **publisher display name** (e.g. `BarnsL`) — users see this on the
   listing.

## 1. Reserve the app name

1. Partner Center → **Apps and games** → **New product** → **MSIX or PWA app**.
2. Reserve the name **`Desktop Karaoke`** (if free) — this also reserves the
   identity.
3. Open the product → **Product management** → **Product identity**. Copy these
   three values exactly:

   | Partner Center field | Goes into |
   |---|---|
   | **Package/Identity/Name** (e.g. `1234BarnsL.DesktopKaraoke`) | `-IdentityName` |
   | **Package/Identity/Publisher** (e.g. `CN=ABCDEF01-2345-...`) | `-Publisher` |
   | **Package/Properties/PublisherDisplayName** | `-PublisherDisplay` |

## 2. Build the Store package (unsigned — the Store signs it)

From the repo root, in PowerShell, paste **your** three values:

```powershell
.\packaging\build_msix.ps1 `
    -IdentityName     "1234BarnsL.DesktopKaraoke" `
    -Publisher        "CN=ABCDEF01-2345-6789-ABCD-EF0123456789" `
    -PublisherDisplay "BarnsL" `
    -Version          "1.0.0.0"
```

This produces **`dist\DesktopKaraoke.msix`**. Do **not** pass `-CertThumbprint`
for a Store build — the Store re-signs it, and a self-signed signature would be
rejected on upload.

> **Versioning:** every update needs a higher `-Version`, and the **last digit
> must stay `0`** (the Store reserves the revision field). e.g. `1.0.1.0`,
> `1.1.0.0`.

## 3. Create the submission

In the product, **Submissions → Start your submission**, then fill in:

- **Packages** — upload `dist\DesktopKaraoke.msix`. It must validate green; if it
  complains about identity, re-check the three values from step 1.
- **Properties → Category** — `Music` (or `Education`).
- **Age ratings** — complete the questionnaire (no objectionable content).
- **Store listing** — description (reuse the top of [README.md](README.md)),
  at least one **screenshot** (1366×768 or larger — grab the overlay over a song),
  and the **tile/Store logos are already in the package**.
- **Privacy policy URL** — **required**, because the app uses the network and
  sends a few seconds of audio to Shazam. Point it at a page describing this;
  the content of [SECURITY.md](SECURITY.md) is exactly what you need — host it
  (GitHub Pages, or link the raw file) and paste the URL.
- **"Why does this app need full-trust?"** — if asked about the `runFullTrust`
  restricted capability, answer plainly: *"Desktop overlay app built with Python;
  reads Windows media-session playback position and captures system audio
  loopback for song identification — both require a full-trust Win32 process."*
  Full-trust desktop apps are accepted on the Store.

## 4. Submit → certification → live

Submit. Certification typically takes a few hours to a couple of days. Once it
passes, the **Get** link goes live — that's your lay-person one-click install.

After it's live, drop the Store link into [README.md](README.md) (replace the
`<!-- STORE LINK -->` placeholder).

---

## Optional: test the package locally before submitting

The build can't be launched on this PC as-is because **Smart App Control is
enforced** (it blocks the self-signed test signature) and **Developer Mode is
off**. To do a real local install/launch test you'd need to, as **admin**:

1. Enable **Developer Mode** (Settings → System → For developers), **or** trust
   the dev cert: import `CN=Your Dev Cert` into
   `LocalMachine\TrustedPeople`.
2. Build a **signed** test package:
   `\.packaging\build_msix.ps1 -CertThumbprint <YOUR_CERT_THUMBPRINT>`
3. `Add-AppxPackage dist\DesktopKaraoke.msix`

Note: even then, Smart App Control may block the inner `.exe` at launch because
the dev cert isn't Microsoft-trusted. **This is expected and does NOT affect Store
users** — the Store signs with a trusted identity, so SAC allows it. The package
itself is already validated (manifest accepted, identity generated, all required
parts + signature present).

## What's in the package (already handled)

- **No Python required** — the PyInstaller bundle ships Python + every native
  dependency (winsdk, fugashi+UniDic, cutlet, shazamio, soundcard, audioop).
- **Read-only install handled** — an MSIX install dir is read-only, so the app
  now writes settings, the lyric cache, and the log to
  `%LOCALAPPDATA%\DesktopKaraoke` when packaged (see
  [`appdata.py`](appdata.py)); the portable `.exe` still keeps them next to
  itself.
- **Branded tiles/logos** — generated from `icon.ico` by
  [`make_assets.py`](make_assets.py).
