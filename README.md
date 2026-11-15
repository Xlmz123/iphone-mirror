# MirrorLink: see and control your iPhone from a Windows PC

MirrorLink shows your iPhone's screen in a window on your Windows PC, and lets you use
your mouse and keyboard on it. It works roughly like **iPhone Mirroring** on a Mac.

- **Video:** up to 60 fps with low lag, over a **USB cable** or your **Wi-Fi** (encrypted).
- **Control:** your PC acts as a **Bluetooth mouse and keyboard** for the phone. No extra
  hardware is needed, as long as your PC has Bluetooth.
- **Free.** No Mac needed, no jailbreak, and no paid Apple developer account.

It's a hobby project, not an Apple product. Read the [limitations](#limitations) first so you
know what to expect.

```
iPhone                                             Windows PC
MirrorLink app ── video (USB cable or Wi-Fi) ──▶   viewer window (pc/run.bat)
iPhone  ◀────────── Bluetooth mouse + keyboard ──  the same viewer
```

---

## What you need

| | |
|---|---|
| **iPhone** | iOS 27 or newer |
| **Windows PC** | Windows 10 or 11 with **Bluetooth**. The Bluetooth has to support being a keyboard/mouse; the check below tells you. |
| **A USB cable** | For setup and for pairing with Wi-Fi the first time |
| **A free Apple ID** | To install the app on your phone |
| **A free GitHub account** | GitHub builds the iPhone app for you in the cloud |

### Check your PC's Bluetooth (30 seconds)
1. Open **Start**, type **Windows PowerShell**, and open it. (Use PowerShell, not Command Prompt.)
2. Paste this and press **Enter**:
   ```powershell
   Add-Type -AssemblyName System.Runtime.WindowsRuntime
   $m = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
   [Windows.Devices.Bluetooth.BluetoothAdapter,Windows.Devices.Bluetooth,ContentType=WindowsRuntime] | Out-Null
   $t = $m.MakeGenericMethod([Windows.Devices.Bluetooth.BluetoothAdapter]).Invoke($null, @([Windows.Devices.Bluetooth.BluetoothAdapter]::GetDefaultAsync())); $t.Wait(-1) | Out-Null
   "Bluetooth LE peripheral supported: " + $t.Result.IsPeripheralRoleSupported
   ```
3. **True:** you're good. **False:** video still works, but for control you'd need a
   Bluetooth 5.0 USB adapter, or the [control helper](#other-way-to-control-the-control-helper)
   instead.

---

## Setup (once, about 30 minutes)

### Step 1: Install three things on your PC
1. **Python 3.12 or newer** from https://www.python.org/downloads/. On the first screen of the
   installer, tick **"Add python.exe to PATH"**.
2. **iTunes and iCloud, the versions from the website, NOT the Microsoft Store.** If you have the
   Microsoft Store versions or the Apple Devices app, uninstall them first. The download links are
   on https://sideloadly.io. iTunes installs Apple's USB driver, which MirrorLink uses.
3. **Sideloadly** from https://sideloadly.io. It installs apps on your iPhone with your Apple ID.

> If Windows blocks something it doesn't recognize (Smart App Control or SmartScreen), choose
> **More info → Run anyway**, or install from the official sites above.

### Step 2: Get your own copy and let GitHub build the iPhone app
1. Sign in to GitHub, open this repository, and click **Fork** (top right), then **Create fork**.
2. In **your fork**, open the **Actions** tab. If GitHub asks, click **"I understand my workflows,
   go ahead and enable them"**.
3. On the left, click **Build iPhone app → Run workflow → Run workflow**. It takes about 5 minutes.
4. When it shows a green ✓, click the run. Under **Artifacts**, download **MirrorLink-ipa** and
   unzip it. You get `MirrorLink-unsigned.ipa`.

### Step 3: Download the PC part
On your fork's main page click **Code → Download ZIP**. Unzip it somewhere you'll keep it, such
as `Documents\MirrorLink`. The **`pc`** folder inside is the PC app.

### Step 4: Install the app on your iPhone
1. Plug the iPhone into the PC, unlock it, and tap **Trust** when it asks.
2. Open **Sideloadly**, drag `MirrorLink-unsigned.ipa` onto it, type your Apple ID, click **Start**,
   and enter the code Apple sends you.
3. On the iPhone:
   1. **Settings → Privacy & Security → Developer Mode → On.** The phone restarts; tap **Turn On**.
   2. **Settings → General → VPN & Device Management →** tap your Apple ID → **Trust**.
4. Open **MirrorLink**. When it asks to find devices on your local network, tap **Allow**. Wi-Fi
   needs this.

> **The 7-day rule:** with a free Apple ID, apps you install yourself stop opening after 7 days.
> When that happens, plug in, open Sideloadly and click **Start** again. Nothing is lost.

### Step 5: Turn on AssistiveTouch (this is how iPhones use a mouse)
On the iPhone: **Settings → Accessibility → Touch → AssistiveTouch → On.**

Optional, once the PC is connected in step 6:
- In the same AssistiveTouch screen, turn **Always Show Menu** off so the floating button
  goes away.
- Leave **Tracking Speed** where it is. MirrorLink is tuned for the default.

### Step 6: First run and Bluetooth pairing
1. Open the `pc` folder and double-click **`run.bat`**. The first run sets itself up, which takes a
   minute or two. Later runs start in seconds.
2. On the iPhone, open **MirrorLink**, tap **Start Mirroring**, and choose the **entire screen**.
   The phone appears in the window on your PC.
3. **Pair Bluetooth (first time only):** on the iPhone go to **Settings → Bluetooth**. Under
   *Other Devices*, tap your **PC's name**. Accept the pairing on the iPhone, and if Windows
   shows a pop-up, accept it there too.
4. Press **F1** in the viewer. The status line should say
   **"Control (Bluetooth): connected to …"**. Click on the phone picture to try it.

From now on the phone reconnects by itself whenever the viewer is open.

### Step 7 (optional): A taskbar icon
Double-click **`create_shortcut.bat`** in the `pc` folder. It puts a **MirrorLink** shortcut
with its own icon on your Desktop and in the Start menu. To pin it, open **Start**, type
**MirrorLink**, right-click it and choose **Pin to taskbar**.

---

## Everyday use
1. On the iPhone, open **MirrorLink** and tap **Start Mirroring** (entire screen) if it isn't
   already running. You can leave the app afterwards; it keeps sending in the background.
2. On the PC, open **MirrorLink** (taskbar icon or `run.bat`).
   - **Cable plugged in:** it uses USB, the fastest option.
   - **No cable:** it finds the phone on Wi-Fi by itself. Both must be on the same network.
     The phone and PC pair automatically the first time you connect over USB.
   - If you plug the cable in while on Wi-Fi, it switches to USB within a few seconds.
3. When you're done, close the window. The phone gets its normal on-screen keyboard back
   within a few seconds. To stop sending video, tap the red dot at the top of the iPhone and
   choose **Stop**.

### Mouse and keyboard
| On the PC | On the iPhone |
|---|---|
| Click | Tap |
| Click and hold, or right-click | Long press (menus, Edit Home Screen) |
| Click and drag | Swipe or drag (it follows your mouse live) |
| Scroll wheel / trackpad | Scroll |
| Type | Types into the selected text box (Backspace, Enter, Tab, the arrow keys, and Esc all work) |
| Ctrl+V | Types what's on your PC's clipboard |
| Ctrl+A / C / X / Z | Select all / copy / cut / undo on the iPhone (Ctrl acts as the iPhone's ⌘) |
| **Ctrl+1** (or Ctrl+H, or click the bar at the bottom) | Home Screen |
| **Ctrl+2** | App Switcher |
| **Ctrl+3** | Search |
| **Ctrl+4** / **Ctrl+5** | Control Center / Notifications |
| Ctrl+↑ / Ctrl+↓ | Volume up / down |
| **Ctrl+K** | Show or hide the iPhone's on-screen keyboard |
| Hover over the top of the window | **Home** and **App Switcher** buttons |
| Ctrl+= / Ctrl+- / Ctrl+0 | Window bigger / smaller / normal |
| Ctrl+R | Rotate the view by hand (if a landscape app looks sideways) |
| F1 / F5 / F11 / Ctrl+Q | Stats / restart control / fullscreen / quit |

The window turns sideways when the phone or an app goes landscape.

You'll see the iPhone's round AssistiveTouch pointer follow your mouse; that's normal. Only
plain English (US) keyboard characters can be typed. Emoji and accented letters are skipped.

---

## Limitations
- **The red dot.** While mirroring, iOS shows a red recording dot at the top of the screen.
  Every app that captures the screen gets it, so a spy app can't hide. MirrorLink can't
  remove it.
- **The on-screen keyboard hides** while the PC is connected, because iOS thinks a real keyboard
  is attached. Press **Ctrl+K** in the viewer to bring it back, or close the viewer.
- **Swipes and scrolls are close, not pixel-exact.** A Bluetooth mouse moves by distance, so a drag
  can end a few points away from where you let go. If drags consistently go too far or not far
  enough, change `bt_drag_scale` (see below).
- **Re-install every 7 days** with a free Apple ID (Sideloadly, about 30 seconds).
- **Protected video** (Netflix and similar) shows black. iOS blocks it from being captured.
- **Unlike a Mac,** the phone stays unlocked and shows what you're doing.

---

## Settings: `pc/settings.json`
Open it in Notepad, change a number, and save. Video settings apply within a second; for
`control`, restart the viewer. Each setting is also explained inside the file.

| Setting | Default | What it does |
|---|---|---|
| `control` | `"bluetooth"` | `"bluetooth"`, or `"helper"` to use the [control helper](#other-way-to-control-the-control-helper) instead |
| `bt_drag_scale` | 1.0 | Drags and scrolls go too far → lower it (0.8). Too short → raise it (1.2). |
| `bt_wheel_step` | 0.12 | How far one mouse-wheel notch scrolls |
| `bt_scroll_speed` | 30 | Top scrolling speed |
| `bt_coordinates` | `"screen"` | Try `"portrait"` only if clicks land in the wrong spot in landscape |
| `max_height` | 1400 | Video height in pixels. Lower = less lag and less work for the phone. |
| `max_fps` | 60 | Frame rate cap |
| `bitrate_usb` / `bitrate_wifi` | 1.0 / 0.6 | Picture quality. Lower it if video stutters. |
| `max_queued_frames_usb` / `_wifi` | 4 / 6 | Frames allowed in flight before the phone skips ahead. Lower = snappier. |
| `keyframe_interval_s` | 3 | Seconds between full refresh frames |
| `jpeg_max_fps` / `jpeg_quality` / `jpeg_max_height` | 60 / 0.6 / 1400 | Used only if iOS stops the fast video encoder in the background |
| `orientation_map` | `{}` | Only if landscape shows sideways, e.g. `{"6": 270}`. The number appears in F1 stats. |

Command-line options for `run.bat`: `--usb-only` (never Wi-Fi), `--host 192.168.1.23` (the IP
shown in the app, if Wi-Fi discovery doesn't work), `--forget` (delete this PC's Wi-Fi pairing).

---

## Troubleshooting
| Problem | Fix |
|---|---|
| *Can't reach Apple Mobile Device Service* | Install iTunes from apple.com, not the Microsoft Store. |
| *No iPhone found … first connection must be over USB* | Plug in with a data cable once to pair. |
| *pairing is out of date* | Pairing was reset on the phone. Plug in with the cable once. |
| *iPhone not found on USB or Wi-Fi* | Open MirrorLink on the phone. Same Wi-Fi on both? **Allow Wi-Fi mirroring** on? |
| The app says *Allow Local Network…* | iPhone Settings → Privacy & Security → Local Network → MirrorLink → on. |
| Wi-Fi discovery doesn't work | Some routers block it. Use `run.bat --host <IP shown in the app>`. |
| Works at home but not at school or work | Those networks often block devices from talking to each other. Use your iPhone's Personal Hotspot. |
| Connected but the picture is black | Tap **Start Mirroring** on the phone, or move something on screen. |
| *Control (Bluetooth): waiting for the iPhone* | iPhone **Settings → Bluetooth**: tap your PC's name. If it's listed but won't connect, tap ⓘ → **Forget This Device**, then pair again. |
| *…can't act as a keyboard/mouse* | Your PC's Bluetooth can't do it. A Bluetooth 5.0 USB adapter fixes it, or use `"control": "helper"`. |
| *Bluetooth is off* | Windows Settings → Bluetooth & devices → turn Bluetooth on. |
| Clicks do nothing, but it says connected | Turn on **AssistiveTouch** (setup step 5). |
| Clicks land in the wrong place in landscape | Set `"bt_coordinates": "portrait"` in settings.json. |
| Something else with control | Press **F5**. Details are in `pc/bluetooth.log`. |

---

## Other way to control: the control helper
Before Bluetooth control, MirrorLink used **WebDriverAgent**, Appium's open-source helper built
on Apple's UI-testing tools. It's exact (taps land on the exact pixel) and needs no Bluetooth.
The catch: **while it runs, iOS covers the phone's screen with an "Automation Running" notice**,
so the phone is hard to use in your hand. To use it:
1. In your fork's **Actions** tab run **Build control helper (WebDriverAgent)**, download
   **WebDriverAgent-ipa**, and install it with Sideloadly. Trust it like in step 4.
2. Set `"control": "helper"` in `pc/settings.json` and restart the viewer **with the USB cable
   plugged in**. It starts the helper automatically. The first time takes a few minutes while it
   downloads Apple's developer tools for your iOS version.
3. To stop it (and the notice), hold both volume buttons on the iPhone.

---

## Security
- **Wi-Fi pairing only happens over the cable**, after you've tapped "Trust This Computer".
- **Every Wi-Fi connection is checked both ways:** each side proves it has the key without
  sending it. Anything else is disconnected within 5 seconds.
- **The Wi-Fi stream is encrypted** (ChaCha20-Poly1305, new keys each session). Changed or
  replayed data is detected and dropped.
- **Bluetooth control only listens to the viewer on your PC.** It uses a local connection with a
  secret token. Bluetooth pairing is done by you, in iPhone Settings, like any keyboard.
- In the app, **Reset pairing** locks out every PC until it pairs over USB again. The
  **Allow Wi-Fi mirroring** switch turns Wi-Fi off entirely.

## How it works
- **iPhone app** (`ios/MirrorLink`, Swift):
  - Captures the screen with ScreenCaptureKit.
  - Encodes it with the phone's hardware H.264 encoder in low-latency mode.
  - Sends it over the USB cable (Apple's usbmux) or over Wi-Fi (Bonjour plus its own encrypted
    channel).
  - The PC acknowledges each frame. If the link falls behind, the phone skips frames, so delay
    never builds up.
- **Viewer** (`pc/mirror.py`): decodes with FFmpeg (PyAV) and always shows only the newest frame.
- **Bluetooth control:**
  - `pc/blehid.py` publishes a standard *HID over GATT* keyboard + mouse through Windows' own
    Bluetooth stack. It runs on its own Python 3.13 through `uv`.
  - Clicks are sent as an absolute pointer position.
  - Drags are relative mouse moves.
  - Scrolls are drags that slow down at the end so the content doesn't fling.
  - `pc/btcontrol.py` connects it to the viewer.
- **Builds** (`.github/workflows`): GitHub's Mac machines build the app unsigned; Sideloadly signs
  it with your Apple ID when it installs.

## Credits
- Bluetooth control is based on the measurements and HID descriptor published by
  [phoneuse](https://github.com/lxe/phoneuse) (MIT License, © 2026 Aleksey Smolenchuk). They
  worked out how iOS reacts to a Bluetooth pointer.
- [WebDriverAgent](https://github.com/appium/appium-ios) (Apache 2.0) by the Appium project, for
  the optional control helper.
- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3), [PyAV](https://github.com/PyAV-Org/PyAV),
  [pygame-ce](https://github.com/pygame-community/pygame-ce), [PyWinRT](https://github.com/pywinrt/pywinrt),
  and [uv](https://github.com/astral-sh/uv).
