"""MirrorLink Bluetooth control: this PC becomes a Bluetooth LE mouse + keyboard for the iPhone.

Why: the WebDriverAgent helper makes iOS show "Automation Running" over the phone for as
long as it runs. A Bluetooth mouse and keyboard are ordinary accessories: no overlay,
nothing extra on the phone, no Developer Mode. iOS drives the mouse through AssistiveTouch.

mirror.py starts this as its own process on Python 3.13 through uv, because the Windows
Runtime bindings (winrt) aren't built for newer Pythons yet. Only the viewer talks to it,
over 127.0.0.1 with a token. It stops by itself a few seconds after the viewer closes, so
the iPhone gets its on-screen keyboard back.

Credits: the HID report descriptor, the GATT layout and the iOS behaviour this relies on
were measured and published by phoneuse (https://github.com/lxe/phoneuse), MIT License,
Copyright (c) 2026 Aleksey Smolenchuk. What they found:
  * An absolute pointer report lands the pointer exactly; presses count only from
    absolute reports.
  * While the button is held, an absolute move ends the touch (it reads as a click), and
    relative moves drag it. So a drag = absolute press, relative motion, absolute release.
  * Relative motion is accelerated by report size; DRAG_TABLE says how far each size goes.
  * iOS ignores the mouse wheel, so scrolling is a drag that slows down before it lifts
    (otherwise the content flings).
  * After relative moves, iOS ignores an absolute report at the same spot as the previous
    absolute report (Nudge works around it).

Commands, one per line after "auth <token>". Positions are fractions of the screen (0..1)
as the viewer shows it:
  screen W H             the phone's screen in points (same orientation as the positions)
  tune {json}            drag_scale, wheel_max
  hover X Y              the phone's pointer follows the mouse (no touch)
  down X Y | to X Y | up a live touch: press, follow, lift
  hold X Y SECONDS       long press
  swipe X0 Y0 X1 Y1 SECONDS HOLD   a one-finger swipe (app switcher, Control Center, ...)
  scroll X Y DX DY       scroll: move a finger DX, DY points, starting at X, Y
  key CODE MODS          press and release a key (HID usage, modifier bits)
  kdown CODE MODS | kup CODE   hold / release a key (iOS repeats held keys itself)
  text "json string"     type text (US keyboard layout)
  consumer NAME          home, search, eject (shows/hides the on-screen keyboard),
                         volup, voldown, playpause, mute
  restart | bye          start over | the viewer is closing
The service answers with "status {json}" lines (on change, and every few seconds).

Run by hand for testing:  python blehid.py --fake   (no Bluetooth: logs the reports)
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import collections
import json
import math
import os
import secrets
import sys
import time
import uuid
from pathlib import Path

PORT = 47830
FAKE_LOG = os.environ.get("MIRRORLINK_FAKE_HID_LOG")


def log(*a) -> None:
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------------------------------
# Token shared with the viewer (so no other program on this PC can type on the phone)
# ---------------------------------------------------------------------------------------
def _home() -> Path:
    if os.environ.get("MIRRORLINK_HOME"):
        return Path(os.environ["MIRRORLINK_HOME"])
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "MirrorLink"
    return Path.home() / ".config" / "mirrorlink"


def load_token() -> str:
    p = _home() / "bt_token"
    try:
        t = p.read_text(encoding="utf-8").strip()
        if len(t) >= 32:
            return t
    except OSError:
        pass
    p.parent.mkdir(parents=True, exist_ok=True)
    t = secrets.token_hex(24)
    p.write_text(t, encoding="utf-8")
    return t


# ---------------------------------------------------------------------------------------
# What the iPhone sees (HID over GATT). Don't change the report map casually: iOS reads it
# when it pairs, and a different one can need "Forget This Device" + pairing again.
# ---------------------------------------------------------------------------------------
REPORT_MAP = bytes.fromhex(
    # Report 1: keyboard (modifiers, reserved, 6 keys).
    "05010906a1018501050719e029e71500250175019508810295017508810395067508150025650507190029658100c0"
    # Report 2: relative mouse (buttons, x, y, wheel).
    "05010902a10185020901a1000509190129031500250195037501810295017505810305010930093109381581257f750895038106c0c0"
    # Report 3: absolute pointer (buttons, X and Y 0..32767): lands the pointer exactly.
    "05010902a10185030901a1000509190129031500250195037501810295017505810305010930093115002"
    "6ff7f751095028102c0c0"
    # Report 4: consumer keys, one bit each (CONSUMER_BITS order), 1 bit padding.
    "050c0901a10185041500250175019501"
    "09b88102"     # Eject: shows / hides the on-screen keyboard
    "09408102"     # Menu: Home
    "0a21028102"   # AC Search: Spotlight
    "09e98102"     # Volume Up
    "09ea8102"     # Volume Down
    "09cd8102"     # Play/Pause
    "09e28102"     # Mute
    "95018103c0"
)
REPORT_SIZES = {1: 8, 2: 4, 3: 5, 4: 1}
CONSUMER_BITS = {"eject": 0, "home": 1, "search": 2, "volup": 3, "voldown": 4, "playpause": 5, "mute": 6}

# US keyboard layout: character -> (usage, modifiers)
CHARS: dict[str, tuple[int, int]] = {}
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    CHARS[_c] = (4 + _i, 0)
    CHARS[_c.upper()] = (4 + _i, 0x02)
for _i, _c in enumerate("1234567890"):
    CHARS[_c] = (30 + _i, 0)
for _i, _c in enumerate("!@#$%^&*()"):
    CHARS[_c] = (30 + _i, 0x02)
for _plain, _shifted, _code in [("-", "_", 45), ("=", "+", 46), ("[", "{", 47), ("]", "}", 48),
                                ("\\", "|", 49), (";", ":", 51), ("'", '"', 52), ("`", "~", 53),
                                (",", "<", 54), (".", ">", 55), ("/", "?", 56)]:
    CHARS[_plain] = (_code, 0)
    CHARS[_shifted] = (_code, 0x02)
CHARS[" "] = (44, 0)
CHARS["\n"] = (40, 0)
CHARS["\t"] = (43, 0)
CHARS["\b"] = (42, 0)
# Typographic characters people paste a lot: type the plain version.
for _fancy, _plain in {"‘": "'", "’": "'", "“": '"', "”": '"',
                       "–": "-", "—": "-", " ": " ", "…": "..."}.items():
    CHARS.setdefault(_fancy, CHARS[_plain[0]])


def absolute(fx: float, fy: float, buttons: int) -> list[int]:
    x, y = (max(0, min(32767, round(float(v) * 32767))) for v in (fx, fy))
    return [buttons & 0x07, x & 0xFF, x >> 8, y & 0xFF, y >> 8]


class Nudge:
    """After relative motion, iOS ignores an absolute report at the previous absolute
    report's position; send one a hair off first in that case."""

    def __init__(self) -> None:
        self.last: list[int] | None = None
        self.moved = False

    def before(self, report_id: int, payload: list[int]) -> list[list[int]]:
        if report_id == 2:
            self.moved = self.moved or bool(payload[1] or payload[2])
            return []
        if report_id != 3:
            return []
        extra = []
        if self.moved and self.last is not None and payload[1:] == self.last[1:]:
            x = payload[1] | payload[2] << 8
            x = x - 8 if x >= 8 else x + 8
            extra.append([self.last[0], x & 0xFF, x >> 8, payload[3], payload[4]])
        self.last, self.moved = list(payload), False
        return extra


# Points the touch moves for one relative report of N units, one report per ~15 ms
# connection interval, default Tracking Speed (phoneuse, measured over Bluetooth LE on an
# iPhone 14 Pro Max with iOS 26.5; an iPhone 17 Pro Max was within ~10%).
DRAG_TABLE = [(1, 0.3), (2, 0.6), (3, 1.1), (4, 1.6), (5, 2.3), (6, 3.0), (8, 4.7), (10, 6.9),
              (12, 9.3), (16, 15.4), (20, 23.0), (28, 42.7), (40, 84.0)]


# ---------------------------------------------------------------------------------------
# Bluetooth: publish the HID service through the Windows Runtime and send reports.
# Adapted from phoneuse/windows/hid.py.
# ---------------------------------------------------------------------------------------
class WinHid:
    RECONNECT_WAIT = 2.0

    def __init__(self, loop: asyncio.AbstractEventLoop, changed) -> None:
        from winrt.windows.devices.bluetooth import (  # noqa: F401  (imported here: Windows only)
            BluetoothAdapter, BluetoothConnectionStatus, BluetoothError, BluetoothLEDevice,
            BluetoothLEPreferredConnectionParameters)
        import winrt.windows.devices.bluetooth.genericattributeprofile as gatt
        from winrt.windows.storage.streams import DataWriter
        self.w = dict(BluetoothAdapter=BluetoothAdapter, BluetoothConnectionStatus=BluetoothConnectionStatus,
                      BluetoothError=BluetoothError, BluetoothLEDevice=BluetoothLEDevice,
                      Params=BluetoothLEPreferredConnectionParameters, DataWriter=DataWriter)
        self.g = gatt
        self.loop = loop
        self.changed = changed            # call when connection state changes
        self.providers: list = []
        self.reports: dict[int, object] = {}
        self.clients: dict[str, dict] = {}   # device id -> {"name", "device", "params", "interval"}
        self.known: dict[str, dict] = {}
        self.lock = asyncio.Lock()
        self.last_sent = 0.0
        self.published = False
        self.problem: str | None = None
        self.generation = 0
        self.readvertising = False
        self.connected_event = asyncio.Event()

    # ---- state for the viewer
    @property
    def connected(self) -> bool:
        return bool(self.clients)

    @property
    def phone(self) -> str | None:
        names = [c.get("name") for c in self.clients.values() if c.get("name")]
        return names[0] if names else ("iPhone" if self.clients else None)

    @property
    def interval_ms(self) -> float | None:
        v = [c["interval"] for c in self.clients.values() if c.get("interval")]
        return v[0] if v else None

    def _uuid(self, n: int) -> uuid.UUID:
        return uuid.UUID(f"0000{n:04x}-0000-1000-8000-00805f9b34fb")

    def _buffer(self, data) -> object:
        w = self.w["DataWriter"]()
        w.write_bytes(bytes(data))
        return w.detach_buffer()

    # ---- publishing
    async def start(self) -> None:
        while True:
            try:
                await self._publish()
                self.problem = None
                self.published = True
                self.changed()
                return
            except Exception as e:  # Bluetooth off, adapter busy, ...
                self.problem = str(e)
                self.published = False
                self.changed()
                self.teardown()
                log("can't publish yet:", e, "(retrying in 5 s)")
                await asyncio.sleep(5)

    def teardown(self) -> None:
        self.generation += 1
        for _, p in self.providers:
            try:
                p.stop_advertising()
            except OSError:
                pass
        self.providers, self.reports, self.clients = [], {}, {}
        self.connected_event.clear()

    async def _publish(self) -> None:
        g, W = self.g, self.w
        adapter = await W["BluetoothAdapter"].get_default_async()
        if adapter is None:
            raise RuntimeError("this PC has no Bluetooth adapter (or it's turned off in Device Manager)")
        if not adapter.is_low_energy_supported or not adapter.is_peripheral_role_supported:
            raise RuntimeError("this PC's Bluetooth can't act as a keyboard/mouse (no LE peripheral role)")
        try:
            from winrt.windows.devices.radios import RadioState
            radio = await adapter.get_radio_async()
            if radio is not None and radio.state != RadioState.ON:
                raise RuntimeError("Bluetooth is off — turn it on in Windows Settings → Bluetooth & devices")
        except ImportError:
            pass

        self.generation += 1
        gen = self.generation
        enc = g.GattProtectionLevel.ENCRYPTION_REQUIRED
        P = g.GattCharacteristicProperties

        hid = await self._provider(0x1812)
        self.providers = [("HID", hid)]
        s = hid.service
        await self._char(s, 0x2A4A, P.READ, value=bytes([0x11, 0x01, 0x00, 0x02]))   # HID information
        await self._char(s, 0x2A4B, P.READ, value=REPORT_MAP)                       # report map
        cp = await self._char(s, 0x2A4C, P.WRITE_WITHOUT_RESPONSE, encrypted=False)  # control point
        cp.add_write_requested(self._on_write)
        for rid, size in REPORT_SIZES.items():
            c = await self._char(s, 0x2A4D, P.READ | P.NOTIFY, value=bytes(size))
            d = g.GattLocalDescriptorParameters()
            d.static_value = self._buffer([rid, 1])          # report reference: id, input
            d.read_protection_level = enc
            r = await c.create_descriptor_async(self._uuid(0x2908), d)
            if r.error != W["BluetoothError"].SUCCESS:
                raise RuntimeError(f"report reference: {W['BluetoothError'](r.error).name}")
            c.add_subscribed_clients_changed(lambda ch, a: self.loop.call_soon_threadsafe(self.refresh))
            self.reports[rid] = c

        bat = await self._provider(0x180F)
        self.providers.append(("battery", bat))
        await self._char(bat.service, 0x2A19, P.READ | P.NOTIFY, value=bytes([100]), encrypted=False)

        for _, p in self.providers:
            p.start_advertising(self._adv_params())
        await asyncio.sleep(1.0)        # Windows sometimes reports ABORTED on the way up, then starts
        for name, p in self.providers:
            if not self._advertising(p):
                st = g.GattServiceProviderAdvertisementStatus(p.advertisement_status).name
                raise RuntimeError(f"Windows didn't start advertising the {name} service ({st})")
            p.add_advertisement_status_changed(
                lambda prov, a, name=name: self.loop.call_soon_threadsafe(self._on_adv, gen, name, prov))
        log(f"advertising as a Bluetooth keyboard and mouse ({os.environ.get('COMPUTERNAME', 'this PC')})")
        self.refresh()

    def _adv_params(self):
        p = self.g.GattServiceProviderAdvertisingParameters()
        p.is_connectable = True
        p.is_discoverable = True      # puts the HID UUID in the advertisement: the iPhone lists us
        return p

    def _advertising(self, p) -> bool:
        S = self.g.GattServiceProviderAdvertisementStatus
        return p.advertisement_status in (S.STARTED, S.STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA)

    async def _provider(self, n: int):
        r = await self.g.GattServiceProvider.create_async(self._uuid(n))
        if r.error != self.w["BluetoothError"].SUCCESS:
            raise RuntimeError(f"service {n:#06x}: {self.w['BluetoothError'](r.error).name}")
        return r.service_provider

    async def _char(self, service, n: int, props, value=None, encrypted: bool = True):
        p = self.g.GattLocalCharacteristicParameters()
        p.characteristic_properties = props
        if encrypted:          # reading these makes the iPhone pair (bond) first, as HID requires
            p.read_protection_level = self.g.GattProtectionLevel.ENCRYPTION_REQUIRED
            p.write_protection_level = self.g.GattProtectionLevel.ENCRYPTION_REQUIRED
        if value is not None:
            p.static_value = self._buffer(value)
        r = await service.create_characteristic_async(self._uuid(n), p)
        if r.error != self.w["BluetoothError"].SUCCESS:
            raise RuntimeError(f"characteristic {n:#06x}: {self.w['BluetoothError'](r.error).name}")
        return r.characteristic

    def _on_write(self, characteristic, args) -> None:
        self.loop.call_soon_threadsafe(log, "the iPhone wrote the HID control point (it's using the keyboard/mouse)")
        deferral = args.get_deferral()

        async def take():
            try:
                req = await args.get_request_async()
                if req is not None and req.option == self.g.GattWriteOption.WRITE_WITH_RESPONSE:
                    req.respond()
            finally:
                deferral.complete()
        asyncio.run_coroutine_threadsafe(take(), self.loop)

    def _on_adv(self, gen: int, name: str, provider) -> None:
        if gen != self.generation or self._advertising(provider) or self.readvertising:
            return
        log(f"advertising the {name} service stopped; starting it again")
        self.readvertising = True
        asyncio.ensure_future(self._readvertise(gen, provider))

    async def _readvertise(self, gen: int, provider) -> None:
        try:
            await asyncio.sleep(2)
            if gen != self.generation or self._advertising(provider):
                return
            try:
                provider.start_advertising(self._adv_params())
                await asyncio.sleep(1.5)
            except OSError:
                pass
            if self._advertising(provider):
                return
            log("advertising won't restart; publishing everything again")
            self.published = False
            self.teardown()
            self.changed()
            await asyncio.sleep(2)
            await self.start()
        finally:
            self.readvertising = False

    # ---- connected iPhones
    async def watch(self) -> None:
        while True:          # Windows doesn't always raise an event when a phone comes back
            await asyncio.sleep(1.0)
            if self.published:
                try:
                    self.refresh()
                except Exception as e:
                    log("checking connected iPhones failed:", e)

    def refresh(self) -> None:
        g = self.g
        seen = {}
        summary = []
        for rid, c in self.reports.items():
            subs = []
            for sub in c.subscribed_clients:
                try:
                    session = sub.session
                    st = session.session_status
                    subs.append(f"{session.device_id.id[-17:]}:{g.GattSessionStatus(st).name}")
                    if st == g.GattSessionStatus.ACTIVE:
                        seen[session.device_id.id] = session
                except OSError as e:
                    subs.append(f"?({e})")
                    continue
            summary.append(f"report {rid}: " + (", ".join(subs) if subs else "nobody"))
        text = " | ".join(summary)
        if text != getattr(self, "_last_summary", None):
            self._last_summary = text
            log("subscriptions:", text)
        before = set(self.clients)
        for dev in list(self.clients):
            if dev not in seen:
                log("disconnected:", self.clients.pop(dev).get("name") or dev)
        for dev in seen:
            if dev not in self.clients:
                cl = self.clients[dev] = self.known.setdefault(dev, {"id": dev})
                if cl.get("device") is None:
                    if not cl.get("tuning"):
                        cl["tuning"] = True
                        asyncio.ensure_future(self._tune(cl))
                else:
                    self._ask_fast(cl)
                    log("connected again:", cl.get("name"))
        if self.clients:
            self.connected_event.set()
        else:
            self.connected_event.clear()
        if set(self.clients) != before:
            self.changed()

    async def _tune(self, cl: dict) -> None:
        try:
            device = await self.w["BluetoothLEDevice"].from_id_async(cl["id"])
            if device is None:
                return
            cl["device"], cl["name"] = device, device.name
            self._ask_fast(cl)
            self._read_interval(cl)
            device.add_connection_parameters_changed(
                lambda d, a: self.loop.call_soon_threadsafe(self._read_interval, cl))
            device.add_connection_status_changed(
                lambda d, a: self.loop.call_soon_threadsafe(self.refresh))
            log(f"connected: {cl['name']} (interval {cl.get('interval')} ms)")
            self.changed()
        except Exception as e:
            log("connected:", cl["id"], f"(details unavailable: {e})")
        finally:
            cl["tuning"] = False

    def _ask_fast(self, cl: dict) -> None:
        """Ask for short connection intervals (iOS otherwise often sits at 30 ms)."""
        old = cl.get("params")
        if old is not None:
            try:
                old.close()
            except (AttributeError, OSError):
                pass
        try:
            cl["params"] = cl["device"].request_preferred_connection_parameters(self.w["Params"].throughput_optimized)
        except (AttributeError, OSError) as e:
            cl["params"] = None
            log("can't ask for faster connection parameters:", e)

    def _read_interval(self, cl: dict) -> None:
        try:
            p = cl["device"].get_connection_parameters()
            v = round(p.connection_interval * 1.25, 2)
            if v:
                cl["interval"] = v
        except (AttributeError, OSError, KeyError):
            pass

    # ---- reports
    def gap(self) -> float:
        """One report per connection interval: two in one event reach iOS together, and
        iOS drops pointer motion that comes too fast."""
        iv = [c["interval"] for c in self.clients.values() if c.get("interval")]
        return max(iv or [15.0]) / 1000

    async def send(self, rid: int, payload: list[int], wait: float) -> bool:
        """Send one report. If no phone is connected, wait up to `wait` seconds for it."""
        deadline = time.monotonic() + wait
        while True:
            if not self.clients:
                if wait <= 0:
                    return False
                try:
                    await asyncio.wait_for(self.connected_event.wait(), max(0.0, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    return False
            if await self._send(rid, payload):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.2)
            self.refresh()

    async def _send(self, rid: int, payload: list[int]) -> bool:
        c = self.reports.get(rid)
        if c is None:
            return False
        buf = self._buffer(payload)
        async with self.lock:
            delay = self.last_sent + self.gap() - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            self.last_sent = time.perf_counter()
            ok = False
            for sub in list(c.subscribed_clients):
                try:
                    if sub.session.session_status != self.g.GattSessionStatus.ACTIVE:
                        continue
                    r = await c.notify_value_async(buf, sub)
                    ok = ok or r.status == 0            # GattCommunicationStatus.SUCCESS
                except OSError as e:
                    log("notify failed:", e)
            return ok


class FakeHid:
    """No Bluetooth: records reports (for tests on any OS)."""

    def __init__(self, loop, changed) -> None:
        self.changed = changed
        self.published = True
        self.problem = None
        self.connected = os.environ.get("MIRRORLINK_FAKE_PHONE", "1") == "1"
        self.phone = "Test iPhone" if self.connected else None
        self.interval_ms = 15.0
        self.sent: list = []
        self.last = 0.0
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        self.changed()

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(3600)

    def teardown(self) -> None:
        pass

    def gap(self) -> float:
        return 0.015

    async def send(self, rid: int, payload: list[int], wait: float) -> bool:
        if not self.connected:
            return False
        async with self.lock:
            delay = self.last + self.gap() - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            self.last = time.perf_counter()
            self.sent.append((round(time.time(), 4), rid, list(payload)))
            if FAKE_LOG:
                with open(FAKE_LOG, "a") as f:
                    f.write(json.dumps([time.time(), rid, list(payload)]) + "\n")
        return True


# ---------------------------------------------------------------------------------------
# Turning the viewer's mouse and keyboard into reports
# ---------------------------------------------------------------------------------------
def _clamp(v: float) -> float:
    return min(max(float(v), 0.0), 1.0)


class Engine:
    SETTLE = 0.1          # s between moving the pointer and pressing (else iOS starts the touch on the way)
    UP_TIMEOUT = 0.6      # s a lift waits for the touch to catch up with the mouse
    # scrolling
    MIN_GRIP = 15.0       # pt: shorter scrolls would read as taps
    WHEEL_FORGET = 0.3    # s: a remainder below MIN_GRIP is dropped after this much quiet
    SCROLL_STILL = 0.12   # s of quiet wheel before the scrolling finger lifts
    SCROLL_TAIL = 60.0    # pt: the last points go slowly so the content doesn't fling
    SCROLL_SHARE = 0.12   # before the tail: this share of what's left per report...
    SCROLL_SLOP = 8.0     # pt: content starts following a finger only after it moved this far
    EDGE = 0.06           # regrip before the finger comes this close to an edge

    def __init__(self, hid) -> None:
        self.hid = hid
        self.events: collections.deque = collections.deque()
        self.wake = asyncio.Event()
        self.screen = (402.0, 874.0)       # points; the viewer sends the real size
        self.drag_scale = 1.0
        self.wheel_max = 30.0
        self.pos: tuple[float, float] | None = None
        self.target: tuple[float, float] | None = None
        self.down = False
        self.want_up: float | None = None
        self.hover: tuple[float, float] | None = None
        self.backlog = [0.0, 0.0]
        self.scroll_at = (0.5, 0.5)
        self.scroll_t = 0.0
        self.mods = 0
        self.held: list[int] = []
        self.nudge = Nudge()
        self.skipped_chars = 0
        self._table_pts = [p for _, p in DRAG_TABLE]

    # ---- from the viewer (never awaits: the driver does the work)
    def push(self, ev: tuple) -> None:
        if ev[0] == "to" and self.events and self.events[-1][0] == "to":
            self.events[-1] = ev           # only the latest position matters
        else:
            self.events.append(ev)
        self.wake.set()

    def set_hover(self, fx: float, fy: float) -> None:
        self.hover = (_clamp(fx), _clamp(fy))
        self.wake.set()

    def add_scroll(self, fx: float, fy: float, dx: float, dy: float) -> None:
        if self.backlog == [0.0, 0.0]:
            self.scroll_at = (_clamp(fx), _clamp(fy))
        # a change of direction drops what's left of the other direction
        if dx * self.backlog[0] < 0:
            self.backlog[0] = 0.0
        if dy * self.backlog[1] < 0:
            self.backlog[1] = 0.0
        self.backlog[0] += dx
        self.backlog[1] += dy
        self.scroll_t = time.monotonic()
        self.wake.set()

    # ---- drag arithmetic
    def units_for(self, points: float) -> float:
        points /= self.drag_scale
        if points <= 0:
            return 0.0
        pts = self._table_pts
        i = bisect.bisect_left(pts, points)
        if i == 0:
            return points / DRAG_TABLE[0][1] * DRAG_TABLE[0][0]
        if i >= len(DRAG_TABLE):
            return DRAG_TABLE[-1][0]
        (u0, p0), (u1, p1) = DRAG_TABLE[i - 1], DRAG_TABLE[i]
        return u0 + (u1 - u0) * (points - p0) / (p1 - p0)

    def points_for(self, units: float) -> float:
        if units <= 0:
            return 0.0
        us = [u for u, _ in DRAG_TABLE]
        i = bisect.bisect_left(us, units)
        if i == 0:
            p = units * DRAG_TABLE[0][1] / DRAG_TABLE[0][0]
        elif i >= len(DRAG_TABLE):
            p = DRAG_TABLE[-1][1] * units / DRAG_TABLE[-1][0]
        else:
            (u0, p0), (u1, p1) = DRAG_TABLE[i - 1], DRAG_TABLE[i]
            p = p0 + (p1 - p0) * (units - u0) / (u1 - u0)
        return p * self.drag_scale

    def whole_units(self, px: float, py: float) -> tuple[int, int]:
        """A report that moves at most (px, py) points (rounding down, so the touch never
        overshoots and has to come back)."""
        dist = math.hypot(px, py)
        if dist < self.points_for(1):
            return 0, 0
        u = self.units_for(dist)
        ux, uy = int(u * px / dist), int(u * py / dist)
        if ux == 0 and uy == 0:
            ux = (1 if px > 0 else -1) if abs(px) >= abs(py) else 0
            uy = (1 if py > 0 else -1) if abs(py) > abs(px) else 0
        return ux, uy

    def remaining(self) -> float:
        if self.pos is None or self.target is None:
            return 0.0
        W, H = self.screen
        return math.hypot((self.target[0] - self.pos[0]) * W, (self.target[1] - self.pos[1]) * H)

    # ---- reports
    async def _abs(self, fx: float, fy: float, buttons: int, wait: float = 1.0) -> bool:
        p = absolute(fx, fy, buttons)
        for extra in self.nudge.before(3, p):
            await self.hid.send(3, extra, wait)
        return await self.hid.send(3, p, wait)

    async def _rel(self, ux: int, uy: int) -> bool:
        p = [0, ux & 0xFF, uy & 0xFF, 0]          # button 0 here: the press is the absolute report's
        self.nudge.before(2, p)
        return await self.hid.send(2, p, 0.5)

    async def press(self, fx: float, fy: float) -> None:
        fx, fy = _clamp(fx), _clamp(fy)
        if self.pos is None or abs(self.pos[0] - fx) > 0.002 or abs(self.pos[1] - fy) > 0.002:
            known = self.pos is not None
            await self._abs(fx, fy, 0)
            await asyncio.sleep(self.SETTLE if known else 0.2)
        ok = await self._abs(fx, fy, 1)
        self.pos = self.target = (fx, fy)
        self.down = ok
        self.want_up = None

    async def step(self) -> bool:
        """One relative report toward the target. Returns whether one was sent."""
        if not self.down or self.pos is None or self.target is None:
            return False
        W, H = self.screen
        ex = (self.target[0] - self.pos[0]) * W
        ey = (self.target[1] - self.pos[1]) * H
        if math.hypot(ex, ey) < 1.0:
            return False
        ux, uy = self.whole_units(ex, ey)
        if ux == 0 and uy == 0:
            return False
        mag = math.hypot(ux, uy)
        moved = self.points_for(mag)
        await self._rel(ux, uy)
        self.pos = (_clamp(self.pos[0] + moved * ux / mag / W), _clamp(self.pos[1] + moved * uy / mag / H))
        return True

    async def release(self) -> None:
        if not self.down:
            return
        fx, fy = self.target or self.pos or (0.5, 0.5)
        ok = await self._abs(fx, fy, 0, wait=2.0)     # the lift must arrive
        if not ok:
            await self._abs(fx, fy, 0, wait=2.0)
        self.down = False
        self.want_up = None
        self.pos = (fx, fy)

    async def key(self, code: int, mods: int = 0) -> None:
        await self.hid.send(1, [mods | self.mods, 0, code, 0, 0, 0, 0, 0], 1.0)
        await self.hid.send(1, self._keyboard_state(), 1.0)

    def _keyboard_state(self) -> list[int]:
        keys = self.held[-6:]
        return [self.mods, 0] + keys + [0] * (6 - len(keys))

    # ---- gestures
    async def swipe(self, x0, y0, x1, y1, secs, hold) -> None:
        await self.press(x0, y0)
        if not self.down:
            return
        t0 = time.monotonic()
        secs = max(0.05, secs)
        while True:
            k = min(1.0, (time.monotonic() - t0) / secs)
            self.target = (_clamp(x0 + (x1 - x0) * k), _clamp(y0 + (y1 - y0) * k))
            if not await self.step():
                await asyncio.sleep(0.008)
            if k >= 1.0:
                break
        end = time.monotonic() + 0.5
        while self.remaining() >= 1.0 and time.monotonic() < end:
            if not await self.step():
                break
        if hold > 0:
            await asyncio.sleep(hold)
        await self.release()

    def scroll_pending(self) -> bool:
        mag = math.hypot(*self.backlog)
        if mag >= self.MIN_GRIP:
            return True
        if mag > 0 and time.monotonic() - self.scroll_t > self.WHEEL_FORGET:
            self.backlog = [0.0, 0.0]
        return False

    def _scroll_step(self, remaining: float) -> float:
        tail = max(self.points_for(1), 150.0 * self.hid.gap())     # ~150 pt/s at the end
        if remaining <= self.SCROLL_TAIL:
            return min(remaining, tail)
        return min(max(remaining * self.SCROLL_SHARE, tail), self.wheel_max)

    def _room(self, ux: float, uy: float) -> float:
        """Points the finger can move in direction (ux, uy) before the edge margin."""
        W, H = self.screen
        x, y = self.pos
        room = float("inf")
        if ux > 0:
            room = min(room, (1 - self.EDGE - x) * W / ux)
        elif ux < 0:
            room = min(room, (x - self.EDGE) * W / -ux)
        if uy > 0:
            room = min(room, (1 - self.EDGE - y) * H / uy)
        elif uy < 0:
            room = min(room, (y - self.EDGE) * H / -uy)
        return max(0.0, room)

    async def scroll(self) -> None:
        """Scroll the content by the backlog: grip, drag (slowing down at the end), lift.
        Comes back for more (regrips) if the wheel keeps turning or the finger hit an edge."""
        bx, by = self.backlog
        mag = math.hypot(bx, by)
        gx, gy = self.scroll_at
        gx, gy = min(max(gx, 0.12), 0.88), min(max(gy, 0.12), 0.88)
        # leave room to move: at least ~40% of the screen in the scroll direction
        if by > 0:
            gy = min(gy, 0.55)
        elif by < 0:
            gy = max(gy, 0.45)
        if bx > 0:
            gx = min(gx, 0.55)
        elif bx < 0:
            gx = max(gx, 0.45)
        # the finger goes a bit further than the content will (touch slop)
        k = (mag + self.SCROLL_SLOP) / mag
        self.backlog = [bx * k, by * k]
        await self.press(gx, gy)
        if not self.down:
            self.backlog = [0.0, 0.0]
            return
        await asyncio.sleep(0.02)
        W, H = self.screen
        while True:
            if any(e[0] in ("down", "hold", "swipe") for e in self.events):
                self.backlog = [0.0, 0.0]          # the user grabbed the screen: stop scrolling
                break
            bx, by = self.backlog
            rem = math.hypot(bx, by)
            if rem < 0.5:
                if time.monotonic() - self.scroll_t > self.SCROLL_STILL:
                    break
                await asyncio.sleep(0.01)      # the wheel may still be turning
                continue
            ux, uy = bx / rem, by / rem
            room = self._room(ux, uy)
            if room < 1.0:
                break                           # at the edge: lift, then grip again
            step = self._scroll_step(min(rem, room))
            before = self.pos
            self.target = (_clamp(self.pos[0] + ux * step / W), _clamp(self.pos[1] + uy * step / H))
            if not await self.step():
                # less than one report's worth: count it as done so we don't spin
                self.backlog = [bx - ux * min(step, rem), by - uy * min(step, rem)]
                await asyncio.sleep(0.008)
                continue
            mx, my = (self.pos[0] - before[0]) * W, (self.pos[1] - before[1]) * H
            nbx, nby = bx - mx, by - my
            self.backlog = [nbx if nbx * bx > 0 else 0.0, nby if nby * by > 0 else 0.0]
        self.target = self.pos
        await asyncio.sleep(0.15)               # rest before lifting: no fling
        await self.release()

    # ---- the driver: one thing at a time, in order
    async def run(self) -> None:
        while True:
            try:
                if self.events:
                    await self._handle(self.events.popleft())
                    continue
                if self.down:
                    rem = self.remaining()
                    if self.want_up is not None and (rem < 1.0 or time.monotonic() - self.want_up > self.UP_TIMEOUT):
                        await self.release()
                        continue
                    if rem >= 1.0 and await self.step():
                        continue
                elif self.scroll_pending():
                    await self.scroll()
                    continue
                elif self.hover is not None:
                    h, self.hover = self.hover, None
                    if self.hid.connected and (self.pos is None or abs(h[0] - self.pos[0]) + abs(h[1] - self.pos[1]) > 0.0015):
                        if await self._abs(h[0], h[1], 0, wait=0):
                            self.pos = h
                    continue
                self.wake.clear()
                if self.down and self.want_up is not None:
                    await asyncio.sleep(0.01)     # waiting for the touch to catch up / time out
                elif math.hypot(*self.backlog) > 0:
                    try:                          # a small scroll waiting for more wheel (or to be dropped)
                        await asyncio.wait_for(self.wake.wait(), self.WHEEL_FORGET)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await self.wake.wait()
            except Exception as e:
                log("input error:", repr(e))
                self.down = False
                self.want_up = None
                await asyncio.sleep(0.05)

    async def _handle(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "down":
            if self.down:
                await self.release()
            await self.press(ev[1], ev[2])
        elif kind == "to":
            if self.down:
                self.target = (_clamp(ev[1]), _clamp(ev[2]))
        elif kind == "up":
            if self.down:
                self.want_up = time.monotonic()
        elif kind == "hold":
            if self.down:
                await self.release()
            await self.press(ev[1], ev[2])
            if self.down:
                await asyncio.sleep(max(0.0, ev[3]))
                await self.release()
        elif kind == "swipe":
            if self.down:
                await self.release()
            await self.swipe(*ev[1:])
        elif kind == "key":
            await self.key(ev[1], ev[2])
        elif kind == "kdown":
            code, mods = ev[1], ev[2]
            self.mods = mods
            if code and code not in self.held:
                self.held.append(code)
            await self.hid.send(1, self._keyboard_state(), 1.0)
        elif kind == "kup":
            if ev[1] in self.held:
                self.held.remove(ev[1])
            if not self.held:
                self.mods = 0
            await self.hid.send(1, self._keyboard_state(), 1.0)
        elif kind == "text":
            for ch in ev[1]:
                code = CHARS.get(ch)
                if code is None:
                    self.skipped_chars += 1
                    continue
                await self.key(*code)
        elif kind == "consumer":
            bit = CONSUMER_BITS.get(ev[1])
            if bit is not None:
                await self.hid.send(4, [1 << bit], 1.0)
                await asyncio.sleep(0.06)
                await self.hid.send(4, [0], 1.0)
        elif kind == "reset":            # the viewer went away: let go of everything
            self.backlog = [0.0, 0.0]
            self.hover = None
            if self.down:
                await self.release()
            if self.held or self.mods:
                self.held, self.mods = [], 0
                await self.hid.send(1, [0] * 8, 0.5)


# ---------------------------------------------------------------------------------------
# The viewer connection
# ---------------------------------------------------------------------------------------
class Service:
    def __init__(self, hid, engine: Engine, token: str) -> None:
        self.hid = hid
        self.engine = engine
        self.token = token
        self.writer: asyncio.StreamWriter | None = None
        self.started = time.monotonic()
        self.last_client = time.monotonic()
        self.had_client = False
        self.bye = False
        self.restart = False
        self.last_status = None
        self.last_status_t = 0.0

    def status(self) -> dict:
        return {"published": bool(self.hid.published), "connected": bool(self.hid.connected),
                "phone": self.hid.phone, "problem": self.hid.problem,
                "pc": os.environ.get("COMPUTERNAME", "this PC"),
                "interval_ms": self.hid.interval_ms, "skipped_chars": self.engine.skipped_chars}

    def push_status(self, force: bool = False) -> None:
        s = self.status()
        now = time.monotonic()
        if self.writer is None or (not force and s == self.last_status and now - self.last_status_t < 3):
            return
        self.last_status, self.last_status_t = s, now
        try:
            self.writer.write(("status " + json.dumps(s) + "\n").encode())
        except (OSError, RuntimeError):
            pass

    async def client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
        except (asyncio.TimeoutError, OSError):
            writer.close()
            return
        if line.decode(errors="replace").strip() != f"auth {self.token}":
            writer.close()
            return
        if self.writer is not None:            # a newer viewer replaces an older one
            try:
                self.writer.close()
            except OSError:
                pass
        self.writer = writer
        self.had_client = True
        self.bye = False
        log("viewer connected")
        self.push_status(force=True)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    self.dispatch(line.decode("utf-8", "replace").strip())
                except (ValueError, IndexError) as e:
                    log("bad command:", line[:80], e)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            if self.writer is writer:
                self.writer = None
                self.last_client = time.monotonic()
                self.engine.push(("reset",))
                log("viewer disconnected")
            try:
                writer.close()
            except OSError:
                pass

    def dispatch(self, text: str) -> None:
        if not text:
            return
        cmd, _, rest = text.partition(" ")
        a = rest.split()
        f = lambda i: float(a[i])     # noqa: E731
        e = self.engine
        if cmd == "hover":
            e.set_hover(f(0), f(1))
        elif cmd in ("down", "to"):
            e.push((cmd, f(0), f(1)))
        elif cmd == "up":
            e.push(("up",))
        elif cmd == "hold":
            e.push(("hold", f(0), f(1), min(5.0, f(2))))
        elif cmd == "swipe":
            e.push(("swipe", f(0), f(1), f(2), f(3), min(5.0, f(4)), min(5.0, f(5))))
        elif cmd == "scroll":
            e.add_scroll(f(0), f(1), f(2), f(3))
        elif cmd == "key":
            e.push(("key", int(a[0]) & 0xFF, int(a[1]) & 0xFF))
        elif cmd == "kdown":
            e.push(("kdown", int(a[0]) & 0xFF, int(a[1]) & 0xFF))
        elif cmd == "kup":
            e.push(("kup", int(a[0]) & 0xFF))
        elif cmd == "text":
            s = json.loads(rest)
            if isinstance(s, str) and s:
                e.push(("text", s[:5000]))
        elif cmd == "consumer":
            e.push(("consumer", a[0]))
        elif cmd == "screen":
            w, h = f(0), f(1)
            if 100 <= w <= 3000 and 100 <= h <= 3000:
                e.screen = (w, h)
        elif cmd == "tune":
            cfg = json.loads(rest)
            if isinstance(cfg.get("drag_scale"), (int, float)):
                e.drag_scale = min(3.0, max(0.3, float(cfg["drag_scale"])))
            if isinstance(cfg.get("wheel_max"), (int, float)):
                e.wheel_max = min(80.0, max(5.0, float(cfg["wheel_max"])))
        elif cmd == "status":
            self.push_status(force=True)
        elif cmd == "bye":
            self.bye = True
        elif cmd == "restart":
            self.restart = True
        else:
            log("unknown command:", cmd)

    async def watchdog(self) -> None:
        """Status to the viewer; stop when the viewer is gone (so the iPhone gets its
        on-screen keyboard back)."""
        while True:
            await asyncio.sleep(0.5)
            self.push_status()
            idle = time.monotonic() - self.last_client
            if self.restart:
                log("restart requested")
                self.quit()
            if self.writer is None:
                if (self.had_client and idle > (1.5 if self.bye else 10)) or \
                        (not self.had_client and time.monotonic() - self.started > 180):
                    log("no viewer: stopping")
                    self.quit()

    def quit(self) -> None:
        try:
            self.hid.teardown()
        finally:
            os._exit(0)

    def changed(self) -> None:
        self.push_status(force=True)


def precise_timers() -> None:
    """Windows sleeps in 15.6 ms steps by default; reports go out every ~15 ms, so ask for 1 ms."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except (OSError, AttributeError):
            pass


async def amain(fake: bool) -> int:
    precise_timers()
    token = load_token()
    loop = asyncio.get_running_loop()
    holder: dict = {}

    def changed() -> None:
        if "svc" in holder:
            holder["svc"].changed()

    async def client(r, w):
        await holder["svc"].client(r, w)

    # The port first: a second copy must leave Bluetooth alone (publishing the HID service
    # twice confuses a paired iPhone).
    try:
        server = await asyncio.start_server(client, "127.0.0.1", PORT)
    except OSError as e:
        log(f"another copy is already running ({e})")
        return 3
    hid = FakeHid(loop, changed) if fake else WinHid(loop, changed)
    engine = Engine(hid)
    holder["svc"] = svc = Service(hid, engine, token)
    log("MirrorLink Bluetooth control started" + (" (fake, no Bluetooth)" if fake else ""))
    async with server:
        await asyncio.gather(server.serve_forever(), hid.start(), hid.watch(), engine.run(), svc.watchdog())
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="no Bluetooth: record the reports (testing)")
    args = ap.parse_args()
    if sys.platform != "win32" and not args.fake:
        sys.exit("Bluetooth control runs on Windows (use --fake to test elsewhere)")
    try:
        sys.exit(asyncio.run(amain(args.fake)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
