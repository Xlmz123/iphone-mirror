# MirrorLink: your iPhone on your Windows PC

Shows your iPhone's screen on your PC with as little lag as possible, over a
**USB cable** or **Wi-Fi**. Clicking on the PC to control the phone comes next.

```
iPhone (iOS 27)                               Windows PC  (pc/mirror.py)
MirrorLink app
 ScreenCaptureKit (whole screen)       ┌─ USB ──────────────┐  decodes immediately,
 → hardware H.264, low-latency mode ───┤                    ├→ shows only the
 → USB: plain (private cable)          └─ Wi-Fi (encrypted)─┘  newest frame
 → Wi-Fi: paired + encrypted
```

---

## One-time setup

### 1. Install these on your PC
- **Python 3.12 or newer**: https://www.python.org/downloads/. On the first installer screen, tick **"Add python.exe to PATH"**.
- **iTunes and iCloud, the versions from the website, NOT the Microsoft Store.** Uninstall any Microsoft Store versions, and the Apple Devices app, first. Download links are on https://sideloadly.io. iTunes also installs Apple's USB service, which the viewer uses.
- **Sideloadly**: https://sideloadly.io

### 2. Build the iPhone app on GitHub (free, no Mac needed)
1. On github.com: **+ → New repository**, name it `iphone-mirror`, choose **Public**, tick nothing, and click **Create**.
2. Click **"uploading an existing file"**. Drag in **everything inside** this folder (`README.md`, `ios`, `pc`, `.github`), not the folder itself. Then click **Commit changes**.
3. If no `.github` folder shows up in the repo: **Add file → Create new file**, name it `.github/workflows/build-ios.yml`, paste in that file's contents, and commit.
4. **Actions** tab → "Build iPhone app" runs (about 5 minutes). Green ✓ → click the run → **Artifacts → MirrorLink-ipa**. Unzip it to get `MirrorLink-unsigned.ipa`.
   - Red ✗ → open "Show errors if the build failed" and send me the `error:` lines.

### 3. Put it on your iPhone
1. Plug the iPhone into the PC, unlock it, and tap **Trust**.
2. Open Sideloadly, drag in the `.ipa`, enter your Apple ID, click **Start**, and enter the 2-factor code.
3. On the iPhone:
   - **Settings → Privacy & Security → Developer Mode → On**. The phone restarts; tap **Turn On**.
   - **Settings → General → VPN & Device Management →** your Apple ID → **Trust**.
4. Open MirrorLink. When it asks to **find devices on your local network**, tap **Allow**. Wi-Fi mirroring needs this.

> With a free Apple ID, the app stops opening after **7 days**. Plug in and click Start in Sideloadly again (about 30 seconds).

### 4. Pair for Wi-Fi (happens automatically)
The first time the viewer connects over **USB**, it pairs with the phone and saves a secret key on this PC. After that, Wi-Fi works with no setup.

---

## Every time you use it
1. On the iPhone, open **MirrorLink**, tap **Start Mirroring**, and choose the **entire screen**.
2. On the PC, double-click **`pc/run.bat`**.
   - **Cable plugged in:** it uses USB, the fastest option.
   - **No cable:** it finds your phone on Wi-Fi by itself. Both need to be on the same network.
   - **On Wi-Fi and you plug the cable in:** it switches to USB within a few seconds.
3. You can leave the MirrorLink app on the phone. Mirroring keeps running in the background.

**Viewer keys:** `F1` stats overlay · `F11` fullscreen · `Esc` quit

**Away from home Wi-Fi?** Turn on the iPhone's **Personal Hotspot** and connect the PC to it. The mirroring stream stays on that local link and doesn't use mobile data. While connected, though, the PC's normal internet does go through your data plan.

**Tip:** while testing, set **Settings → Display & Brightness → Auto-Lock → Never**.

### Viewer options (`run.bat` passes these through)
```
run.bat --usb-only            never use Wi-Fi
run.bat --host 192.168.1.23   connect to this IP (shown in the app) if auto-discovery fails
run.bat --forget              delete this PC's pairing
```

---

## Security on Wi-Fi
- **Pairing only over the cable.** A PC gets the phone's key only through USB, after you've tapped "Trust This Computer."
- **Both sides prove it.** On every Wi-Fi connection, the phone and PC each prove they hold the key without ever sending it. A device without the key gets nothing and is disconnected within 5 seconds.
- **Everything is encrypted** (ChaCha20-Poly1305 with fresh keys each session). Changed, replayed, or reordered data is detected, and the connection drops and reconnects.
- **Reset pairing** in the app locks out every PC until it pairs over USB again. The **Allow Wi-Fi mirroring** switch turns Wi-Fi off completely.

## What the stats overlay means
- **video fps**: frames arriving. The phone only sends when something on screen changes, so a still screen shows a low number.
- **shown fps**: frames drawn. This should match video fps.
- **decode**: time the PC takes to decode one frame.
- **round trip**: a message going PC → phone → PC. A few milliseconds on USB; often 5–30 ms on Wi-Fi.

If Wi-Fi can't keep up, the phone **skips frames** instead of letting a delay build up. The picture might stutter for a moment, but it won't drift behind. In testing on a deliberately overloaded link, delay stayed around 0.4 seconds; without skipping, it grew to 8 seconds.

## Troubleshooting
| Message / problem | Fix |
|---|---|
| *Can't reach Apple Mobile Device Service* | Install iTunes from apple.com (not the Microsoft Store). |
| *No iPhone found … first connection must be over USB* | Not paired yet. Plug in with a data cable once. |
| *pairing is out of date* | Pairing was reset on the phone. Plug in with the cable once. |
| *iPhone not found on USB or Wi-Fi* | Open MirrorLink on the phone. Check both are on the same Wi-Fi and that **Allow Wi-Fi mirroring** is on. |
| App says *Allow Local Network…* | Settings → Privacy & Security → Local Network → MirrorLink → on. |
| Auto-discovery doesn't work (some routers block it) | Use `run.bat --host <IP shown in the app>`. |
| Works at home but not at school | Many school networks block devices from talking to each other. Use your Personal Hotspot. |
| Connected but black | Tap **Start Mirroring** on the phone, or move something on screen. |

---

## How it's built
- `ios/MirrorLink/` (Swift)
  - `CaptureEngine.swift`: ScreenCaptureKit capture (iOS 27 captures the whole screen in-app, no extension).
  - `H264Encoder.swift`: VideoToolbox low-latency H.264, no B-frames, lower bitrate on Wi-Fi.
  - `StreamServer.swift`: USB listener (loopback, port 47800) + Wi-Fi listener (port 47801, Bonjour `_mirrorlink._tcp`). Handles pairing and acknowledgement-based frame skipping.
  - `SecureChannel.swift`, `Pairing.swift`: Wi-Fi handshake and encryption; key stored in the Keychain.
  - `Protocol.swift`: the message format.
- `pc/`
  - `mirror.py`: viewer and connection logic (USB first, Wi-Fi fallback).
  - `usbmux.py`: talks to Apple's USB service.
  - `secure.py`: the PC side of the Wi-Fi handshake and encryption.
  - `discovery.py`: finds the phone on Wi-Fi.
  - `pairing.py`: saves the key in `%APPDATA%\MirrorLink`.
- `.github/workflows/build-ios.yml` + `ios/project.yml`: cloud build on GitHub's Xcode 27 Macs.

**Next (input):** clicking, typing, and scrolling on the PC window controls the phone.
