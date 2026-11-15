# MirrorLink: your iPhone on your Windows PC (step 1: video)

Step 1 shows your iPhone screen on your PC over a USB cable, with as little lag
as possible. Clicking to control the phone comes in step 2.

```
iPhone (iOS 27)                          Windows PC
MirrorLink app                           pc/mirror.py
 ScreenCaptureKit (whole screen)          receives over USB
 → hardware H.264, low-latency mode  ═USB═→ decodes immediately
 → tiny server on the phone's             → shows only the newest frame
   loopback (not on Wi-Fi)
```

---

## One-time setup

### 1. Install these on your PC
- **Python 3.12 or newer**: https://www.python.org/downloads/. On the first installer screen, tick **"Add python.exe to PATH"**.
- **Apple Devices**, from the Microsoft Store (or iTunes). This installs Apple's USB service, which the viewer talks to.
- **Sideloadly**: https://sideloadly.io. It installs the app onto your iPhone with your normal Apple ID.

### 2. Build the iPhone app on GitHub (free, no Mac needed)
1. Make a GitHub account if you don't have one, then create a **new repository**, for example `iphone-mirror`. Public or private both work.
2. Upload **everything in this folder**, including the `.github` folder. The easiest way is GitHub Desktop: clone the empty repo, copy these files in, then commit and push. You can also drag the files onto the repo page in your browser.
   - Check that `.github/workflows/build-ios.yml` shows up in the repo. If it doesn't, go to **Actions → "set up a workflow yourself"**, paste in that file's contents, and commit.
3. Open the **Actions** tab. "Build iPhone app" runs automatically and takes about 5 minutes.
4. When it shows a green ✓, click the run, scroll to **Artifacts**, and download **MirrorLink-ipa**. Unzip it to get `MirrorLink-unsigned.ipa`.
   - If the run shows a red ✗, open the "Show errors" step and send me the lines that say `error:`.

### 3. Put it on your iPhone
1. Plug the iPhone into the PC, unlock it, and tap **Trust**.
2. Open Sideloadly, drag in `MirrorLink-unsigned.ipa`, enter your Apple ID, and click **Start**.
3. On the iPhone:
   - **Settings → Privacy & Security → Developer Mode → On**. The phone restarts.
   - **Settings → General → VPN & Device Management →** your Apple ID → **Trust**.

> With a free Apple ID, the app stops opening after **7 days**. Re-run Sideloadly to refresh it (about 30 seconds).

---

## Every time you use it
1. Plug the iPhone into the PC with the cable.
2. On the iPhone, open **MirrorLink**, tap **Start Mirroring**, and choose the **entire screen**.
3. On the PC, double-click **`pc/run.bat`**. The first run takes a minute to set up.
4. You can leave the MirrorLink app on the phone. Mirroring keeps running in the background.

**Viewer keys:** `F1` stats overlay · `F11` fullscreen · `Esc` quit

**Tip:** while testing, set **Settings → Display & Brightness → Auto-Lock → Never** so the phone doesn't lock mid-session.

---

## What the stats overlay means
- **video fps**: frames arriving from the phone. The phone only sends a new frame when something on screen changes, so a still screen shows a low number. That's expected.
- **shown fps**: frames drawn on your PC. This should match video fps; it means nothing is piling up.
- **decode**: time the PC takes to decode each frame.
- **USB round trip**: a message going PC → phone → PC. This is the cable's share of the lag. Expect a few milliseconds.

## Troubleshooting
| Message | Fix |
|---|---|
| *Can't reach Apple Mobile Device Service* | Install or open the **Apple Devices** app. |
| *No iPhone found* | Use a data cable (not charge-only), unlock the phone, and tap Trust. |
| *Phone refused connection … code 3* | MirrorLink isn't running on the phone. Open it. |
| Connected but black | Tap **Start Mirroring** on the phone, or move something on screen. |

---

## How it's built (for later steps)
- `ios/MirrorLink/`: the iPhone app (Swift)
  - `CaptureEngine.swift`: ScreenCaptureKit capture. On iOS 27 the app captures the whole screen itself, with no extension.
  - `H264Encoder.swift`: VideoToolbox encoder: low-latency rate control, no B-frames, keyframe on demand.
  - `StreamServer.swift`: loopback-only TCP server on port 47800, reached through USB. Drops frames instead of queueing them when the link falls behind.
  - `Protocol.swift`: the message format, shared with the PC.
- `pc/mirror.py`: viewer. `pc/usbmux.py`: talks to Apple's USB service (no extra libraries).
- `.github/workflows/build-ios.yml` + `ios/project.yml`: cloud build on GitHub's Xcode 27 Macs.

**Next, step 2 (input):** clicking, typing, and scrolling on the PC window controls the phone.
