"""MirrorLink viewer — shows your iPhone's screen on your PC over USB or Wi-Fi.

Run:  python mirror.py              (USB if plugged in, otherwise Wi-Fi)
      python mirror.py --usb-only
      python mirror.py --host 192.168.1.23   (skip auto-discovery)
      python mirror.py --forget       (remove this PC's pairing)
Keys: F1 = stats overlay   F11 = fullscreen   Esc = quit

Design for low latency:
  * network thread decodes every packet as it arrives (H.264, no B-frames)
  * every video frame is acknowledged on arrival, so the phone can skip frames
    instead of building up a backlog when the link is slow
  * only the NEWEST decoded frame is kept; the window never queues frames
  * scaling to window size happens once per displayed frame, in FFmpeg
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys
import threading
import time

import av
import pygame

import discovery
import pairing as pairing_store
import usbmux
from secure import AuthError, PlainLink, SecureLink

USB_PORT = 47800
WIFI_PORT = 47801
HEADER = struct.Struct(">IBQ")   # length, type, timestamp(us)

MSG_HELLO, MSG_KEY, MSG_DELTA, MSG_JPEG, MSG_ORIENT = 0x01, 0x02, 0x03, 0x04, 0x05
MSG_PAIR_INFO, MSG_KICKED, MSG_INPUT, MSG_INPUT_STATUS = 0x21, 0x22, 0x30, 0x31
MSG_REQ_KEY, MSG_PING, MSG_PONG, MSG_ACK, MSG_CONFIG, MSG_PAIR_REQ = 0x10, 0x11, 0x12, 0x13, 0x14, 0x20

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")


def load_settings() -> dict:
    """pc/settings.json, minus the help text. Bad/missing file -> {} (phone keeps its defaults)."""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except (OSError, ValueError):
        return {}


def settings_mtime() -> float:
    try:
        return os.path.getmtime(SETTINGS_PATH)
    except OSError:
        return 0.0


def send_msg(link: PlainLink, mtype: int, payload: bytes = b"") -> None:
    link.send(HEADER.pack(len(payload), mtype, 0) + payload)


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rx_frames = 0
        self.rx_bytes = 0
        self.decode_ms = 0.0
        self.rtt_ms: float | None = None
        self.shown = 0
        self.snapshot = {"rx_fps": 0, "show_fps": 0, "mbps": 0.0, "decode_ms": 0.0, "rtt_ms": None}
        self._t = time.perf_counter()

    def tick(self) -> None:
        now = time.perf_counter()
        dt = now - self._t
        if dt < 1.0:
            return
        with self.lock:
            self.snapshot = {
                "rx_fps": round(self.rx_frames / dt),
                "show_fps": round(self.shown / dt),
                "mbps": self.rx_bytes * 8 / dt / 1e6,
                "decode_ms": self.decode_ms,
                "rtt_ms": self.rtt_ms,
            }
            self.rx_frames = self.rx_bytes = self.shown = 0
        self._t = now


class Receiver(threading.Thread):
    """Finds the phone (USB first, then Wi-Fi), decodes video, publishes the latest frame."""

    def __init__(self, stats: Stats, host: str | None = None, usb_only: bool = False) -> None:
        super().__init__(daemon=True)
        self.stats = stats
        self.host = host
        self.usb_only = usb_only
        self.status = "Starting…"
        self.device_name = ""
        self.points: tuple[float, float] | None = None   # the phone's screen in points (portrait)
        self.size: tuple[int, int] | None = None
        self.link_kind: str | None = None        # "usb" / "wifi" / None
        self._latest: av.VideoFrame | None = None
        self._lock = threading.Lock()
        self.new_frame = threading.Event()
        self._stop = threading.Event()
        self.link: PlainLink | None = None
        self.pairing = pairing_store.load()
        self.status_note = ""
        self.note_until = 0.0
        self.count = {"key": 0, "delta": 0, "jpeg": 0, "frames": 0, "errors": 0}   # totals this session
        self.mode = "H.264"
        self.orientation_raw = None          # phone's SCStreamFrameInfo.videoOrientation value
        self.control_ready = False           # control helper (WDA) reachable on the phone
        self.connected_at = 0.0
        self.kicked = False                  # another viewer took over
        self.resume = threading.Event()

    @property
    def connected(self) -> bool:
        return self.link_kind is not None

    def latest(self) -> av.VideoFrame | None:
        with self._lock:
            f, self._latest = self._latest, None
        return f

    def send_input(self, event: dict) -> bool:
        """Send a touch/key/text event to the phone. Returns False if not connected."""
        link = self.link
        if link is None:
            return False
        try:
            send_msg(link, MSG_INPUT, json.dumps(event, separators=(",", ":")).encode())
            return True
        except OSError:
            return False

    def stop(self) -> None:
        self._stop.set()
        self.resume.set()
        if self.link:
            self.link.close()

    # ---- connecting -------------------------------------------------------

    def _try_usb(self) -> PlainLink | None:
        try:
            return PlainLink(usbmux.connect(USB_PORT))
        except usbmux.UsbmuxError as e:
            self._usb_error = str(e)
        except OSError as e:
            self._usb_error = f"USB service unavailable ({e.__class__.__name__})"
        return None

    def _try_wifi(self) -> PlainLink | None:
        p = self.pairing
        if p is None:
            return None
        candidates: list[tuple[str, int]] = []
        if self.host:
            candidates.append((self.host, WIFI_PORT))
        else:
            if p.last_host:
                candidates.append((p.last_host, WIFI_PORT))
            self.status = "Looking for your iPhone on Wi-Fi…"
            for c in discovery.find(p.id_hex, timeout=3.0):
                if c not in candidates:
                    candidates.append(c)
        for host, port in candidates:
            try:
                sock = socket.create_connection((host, port), timeout=2.0)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(5.0)
                link = SecureLink(sock, p.key, p.id_bytes)
                sock.settimeout(None)
                if p.last_host != host:
                    p.last_host = host
                    p.save()
                return link
            except AuthError:
                self._wifi_error = ("This PC's pairing is out of date (pairing was reset on the phone). "
                                    "Connect once with the USB cable.")
            except OSError:
                continue
        return None

    def run(self) -> None:
        while not self._stop.is_set():
            self._usb_error = self._wifi_error = None
            self.status = "Looking for iPhone on USB…"
            link = self._try_usb()
            if link is None and not self.usb_only:
                link = self._try_wifi()

            if link is None:
                if self._wifi_error:
                    self.status = self._wifi_error
                elif self.pairing is None or self.usb_only:
                    self.status = self._usb_error or "Plug in your iPhone"
                    if self.pairing is None and not self.usb_only:
                        self.status += " — the first connection must be over USB to pair."
                else:
                    self.status = ("iPhone not found on USB or Wi-Fi. Open MirrorLink on the phone; "
                                   "for Wi-Fi, both must be on the same network.")
                self._stop.wait(1.5)
                continue

            self.link = link
            self.link_kind = link.kind
            self.connected_at = time.time()
            self.control_ready = False
            self.status = ("Connected over " + ("USB" if link.kind == "usb" else "Wi-Fi") +
                           " — tap Start Mirroring on the phone")
            try:
                self._session(link)
            except (OSError, ConnectionError, ValueError) as e:
                if not self._stop.is_set():
                    self.status = f"Disconnected ({e.__class__.__name__}). Reconnecting…"
            except Exception as e:   # e.g. InvalidTag: tampered/corrupt Wi-Fi data
                self.status = f"Connection dropped ({e.__class__.__name__}). Reconnecting…"
            finally:
                self.link_kind = None
                self.link = None
                self.control_ready = False
                link.close()
            if self.kicked:
                # Another viewer took over. Don't fight it: wait for the user.
                self.status = ("Another MirrorLink viewer took over this iPhone. "
                               "Press R in this window to take it back.")
                self.resume.clear()
                self.resume.wait()
                self.kicked = False
                continue
            self._stop.wait(0.5)

    # ---- streaming ----------------------------------------------------------

    def _session(self, link: PlainLink) -> None:
        self.count = {"key": 0, "delta": 0, "jpeg": 0, "frames": 0, "errors": 0}
        jpeg_codec = None   # created on first JPEG frame (phone's background fallback)
        codec = av.CodecContext.create("h264", "r")
        codec.options = {"flags": "low_delay"}   # output each frame immediately
        codec.thread_type = "SLICE"              # frame threading would add latency
        waiting_for_key = True
        alive = threading.Event()
        alive.set()

        def background() -> None:
            # Ping every second (round-trip time); re-send settings.json when it's
            # saved; on Wi-Fi, switch to USB as soon as the cable is plugged in.
            n = 0
            sent_mtime = settings_mtime()
            while alive.is_set() and not self._stop.is_set():
                try:
                    send_msg(link, MSG_PING, struct.pack(">d", time.perf_counter()))
                    m = settings_mtime()
                    if m != sent_mtime:
                        sent_mtime = m
                        cfg = load_settings()
                        if cfg:
                            send_msg(link, MSG_CONFIG, json.dumps(cfg).encode())
                            self.status_note = "settings.json applied"
                            self.note_until = time.time() + 3
                except OSError:
                    return
                n += 1
                if link.kind == "wifi" and n % 3 == 0 and not self.usb_only:
                    try:
                        if any(d["ConnectionType"] == "USB" for d in usbmux.list_devices()):
                            self.status = "Cable detected — switching to USB…"
                            link.close()
                            return
                    except Exception:
                        pass
                time.sleep(1.0)
        threading.Thread(target=background, daemon=True).start()

        try:
            while not self._stop.is_set():
                length, mtype, ts = HEADER.unpack(link.read_exact(HEADER.size))
                payload = link.read_exact(length)

                if mtype == MSG_JPEG:
                    # Phone is in the background: iOS blocks its hardware video encoder,
                    # so it sends standalone JPEG pictures instead.
                    send_msg(link, MSG_ACK, struct.pack(">Q", ts))
                    self.count["jpeg"] += 1
                    self.mode = "JPEG (phone app in background)"
                    if jpeg_codec is None:
                        jpeg_codec = av.CodecContext.create("mjpeg", "r")
                    t0 = time.perf_counter()
                    try:
                        frames = jpeg_codec.decode(av.Packet(payload))
                    except av.error.FFmpegError:
                        self.count["errors"] += 1
                        continue
                    dt = (time.perf_counter() - t0) * 1000
                    with self.stats.lock:
                        self.stats.rx_frames += 1
                        self.stats.rx_bytes += length
                        self.stats.decode_ms = self.stats.decode_ms * 0.9 + dt * 0.1
                    self.count["frames"] += len(frames)
                    if frames:
                        with self._lock:
                            self._latest = frames[-1]
                        self.new_frame.set()
                    continue

                if mtype in (MSG_KEY, MSG_DELTA):
                    self.mode = "H.264"
                    # Acknowledge on arrival: tells the phone the link is keeping up.
                    send_msg(link, MSG_ACK, struct.pack(">Q", ts))
                    self.count["key" if mtype == MSG_KEY else "delta"] += 1
                    if waiting_for_key and mtype != MSG_KEY:
                        continue
                    waiting_for_key = False
                    t0 = time.perf_counter()
                    try:
                        frames = codec.decode(av.Packet(payload))
                    except av.error.FFmpegError:
                        self.count["errors"] += 1
                        waiting_for_key = True          # ask for a clean restart point
                        send_msg(link, MSG_REQ_KEY)
                        continue
                    dt = (time.perf_counter() - t0) * 1000
                    with self.stats.lock:
                        self.stats.rx_frames += 1
                        self.stats.rx_bytes += length
                        self.stats.decode_ms = self.stats.decode_ms * 0.9 + dt * 0.1
                    self.count["frames"] += len(frames)
                    if frames:
                        if self.size is None:
                            self.size = (frames[-1].width, frames[-1].height)
                        with self._lock:
                            self._latest = frames[-1]   # older undisplayed frames are dropped
                        self.new_frame.set()

                elif mtype == MSG_HELLO:
                    info = json.loads(payload)
                    if "w" in info and "h" in info:
                        self.size = (int(info["w"]), int(info["h"]))
                    self.device_name = info.get("name", "iPhone")
                    if info.get("pt_w") and info.get("pt_h"):
                        self.points = (float(info["pt_w"]), float(info["pt_h"]))
                    if "w" in info:
                        self.status = f"Streaming from {self.device_name}"
                    # Send our tuning settings (first hello of a session only).
                    if not getattr(link, "_config_sent", False):
                        cfg = load_settings()
                        if cfg:
                            send_msg(link, MSG_CONFIG, json.dumps(cfg).encode())
                        link._config_sent = True
                    # Over USB: (re)pair if this PC doesn't know this phone's current key.
                    if link.kind == "usb" and (self.pairing is None or self.pairing.id_hex != info.get("id")):
                        send_msg(link, MSG_PAIR_REQ)

                elif mtype == MSG_PAIR_INFO:
                    info = json.loads(payload)
                    old_host = self.pairing.last_host if self.pairing and self.pairing.id_hex == info["id"] else None
                    self.pairing = pairing_store.Pairing(info["id"], base64.b64decode(info["key"]),
                                                         info.get("name", "iPhone"), old_host)
                    self.pairing.save()

                elif mtype == MSG_ORIENT:
                    try:
                        self.orientation_raw = json.loads(payload).get("o")
                    except ValueError:
                        pass

                elif mtype == MSG_INPUT_STATUS:
                    try:
                        self.control_ready = bool(json.loads(payload).get("ready"))
                    except ValueError:
                        pass

                elif mtype == MSG_KICKED:
                    self.kicked = True
                    raise ConnectionError("another viewer took over")

                elif mtype == MSG_PONG:
                    (sent,) = struct.unpack(">d", payload[:8])
                    with self.stats.lock:
                        self.stats.rtt_ms = (time.perf_counter() - sent) * 1000
        finally:
            alive.clear()


# ----------------------------------------------------------------------------- UI

import numpy as np  # noqa: E402  (PyAV already depends on it)

from helper import ControlHelper  # noqa: E402
from btcontrol import BtControl, BtInput  # noqa: E402

TOP_BAR = 34          # strip above the phone picture for the Home / App Switcher buttons (like macOS)

# Rotation (degrees clockwise) that makes the picture upright, keyed by the phone's
# reported orientation value. iOS tags frames like EXIF / CGImagePropertyOrientation:
# 1 = upright, 3 = upside down, 6 = needs 90° clockwise, 8 = needs 90° counter-clockwise.
# Override in settings.json with "orientation_map": {"<value>": degrees} if needed,
# or press Ctrl+R to rotate by hand.
DEFAULT_ORIENTATION_MAP = {"1": 0, "2": 0, "3": 180, "4": 180, "5": 90, "6": 90, "7": 270, "8": 270}


def enable_dpi_awareness() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # crisp, no Windows bitmap scaling
        except Exception:
            pass


def fit(src_w: int, src_h: int, box_w: int, box_h: int) -> tuple[int, int, int, int]:
    scale = min(box_w / src_w, box_h / src_h)
    w, h = max(2, int(src_w * scale)) & ~1, max(2, int(src_h * scale)) & ~1
    return (box_w - w) // 2, (box_h - h) // 2, w, h


def rotation_for(raw, overrides: dict) -> int:
    if raw is None:
        return 0
    table = dict(DEFAULT_ORIENTATION_MAP)
    for k, v in (overrides or {}).items():
        try:
            table[str(k)] = int(v)
        except (TypeError, ValueError):
            pass
    return int(table.get(str(raw), 0)) % 360


def clipboard_text() -> str:
    try:
        if not pygame.scrap.get_init():
            pygame.scrap.init()
        t = pygame.scrap.get_text()
        if t:
            return t
    except Exception:
        pass
    try:
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        t = r.clipboard_get()
        r.destroy()
        return t
    except Exception:
        return ""


class InputController:
    """Turns mouse and keyboard into iPhone gestures, the way macOS iPhone Mirroring does:

    click = tap · click-and-hold or right-click = long press · click-drag = swipe/drag
    scroll wheel / trackpad = scroll · typing = typing · Ctrl+1/2/3/4 = Home / App Switcher /
    Search / Control Center (macOS uses Cmd).
    Coordinates are sent normalized (0..1) in the upright picture you see.
    """

    TAP_SLOP = 0.012       # how far (fraction of screen) a press can move and still be a tap
    LONG_MS = 550          # hold this long without moving = long press (iOS context menus)

    def __init__(self, rx: "Receiver", note) -> None:
        self.rx = rx
        self.note = note
        self.down = None
        self.text = ""
        self.text_t = 0.0
        self.wheel = [0.0, 0.0]
        self.wheel_pos = (0.5, 0.5)
        self.wheel_t = 0.0
        self.ripples: list[tuple[float, float, float]] = []   # (nx, ny, t) tap feedback

    def send(self, ev: dict) -> None:
        if not self.rx.connected:
            return
        if not self.rx.control_ready:
            self.note("Control isn't ready yet (see the status at the top-left, F1)")
        self.rx.send_input(ev)

    # --- pointer --------------------------------------------------------------
    def press(self, nx: float, ny: float) -> None:
        self.down = {"x": nx, "y": ny, "t0": time.perf_counter(), "pts": [[nx, ny, 0.0]],
                     "drag": False, "long": False}

    def motion(self, nx: float, ny: float) -> None:
        d = self.down
        if not d or d["long"]:
            return
        t = (time.perf_counter() - d["t0"]) * 1000
        lx, ly, lt = d["pts"][-1]
        if abs(nx - lx) + abs(ny - ly) > 0.002 and t - lt >= 8:
            d["pts"].append([nx, ny, t])
        if abs(nx - d["x"]) > self.TAP_SLOP or abs(ny - d["y"]) > self.TAP_SLOP:
            d["drag"] = True

    def release(self, nx: float, ny: float) -> None:
        d, self.down = self.down, None
        if not d or d["long"]:
            return
        t = (time.perf_counter() - d["t0"]) * 1000
        if d["drag"]:
            pts = d["pts"] + [[nx, ny, t]]
            self.send({"t": "path", "pts": self._simplify(pts)})
        else:
            self.send({"t": "tap", "x": d["x"], "y": d["y"]})
            self.ripples.append((d["x"], d["y"], time.perf_counter()))

    def right_click(self, nx: float, ny: float) -> None:
        self.send({"t": "long", "x": nx, "y": ny, "ms": 800})
        self.ripples.append((nx, ny, time.perf_counter()))

    @staticmethod
    def _simplify(pts: list) -> list:
        # Keep the shape and real timing, but cap the size (≤ 40 points, ≤ 4 s).
        if len(pts) > 40:
            step = (len(pts) - 1) / 39
            pts = [pts[round(i * step)] for i in range(40)]
        total = pts[-1][2]
        if total > 4000:
            k = 4000 / total
            pts = [[x, y, t * k] for x, y, t in pts]
        return [[round(x, 4), round(y, 4), round(t)] for x, y, t in pts]

    def wheel_event(self, dx: float, dy: float, pos) -> None:
        if pos is None:
            return
        self.wheel[0] += dx
        self.wheel[1] += dy
        self.wheel_pos = pos
        self.wheel_t = time.perf_counter()

    def _flush_wheel(self) -> None:
        dx, dy = self.wheel
        self.wheel = [0.0, 0.0]
        if abs(dx) < 0.05 and abs(dy) < 0.05:
            return
        step = 0.10                                   # screen fraction per wheel notch
        ddx = max(-0.6, min(0.6, -dx * step))         # scroll right = finger moves left
        ddy = max(-0.6, min(0.6, dy * step))          # wheel up = content moves down = finger moves down
        x, y = self.wheel_pos
        x = min(0.95 - max(0, ddx), max(0.05 - min(0, ddx), x))
        y = min(0.93 - max(0, ddy), max(0.07 - min(0, ddy), y))
        fling = max(abs(dx), abs(dy)) >= 4            # fast spin = flick with momentum
        dur = 90 if fling else 140
        pts = [[x + ddx * i / 4, y + ddy * i / 4, dur * i / 4] for i in range(5)]
        if not fling:
            pts.append([x + ddx, y + ddy, dur + 70])  # pause before lifting: no momentum, precise
        self.send({"t": "path", "pts": self._simplify(pts)})

    # --- keyboard -------------------------------------------------------------
    def type_text(self, s: str) -> None:
        if not self.text:
            self.text_t = time.perf_counter()
        self.text += s

    def key(self, k: str) -> None:
        self.send({"t": "key", "k": k})

    # --- called every loop ----------------------------------------------------
    def tick(self) -> None:
        now = time.perf_counter()
        d = self.down
        if d and not d["drag"] and not d["long"] and (now - d["t0"]) * 1000 >= self.LONG_MS:
            d["long"] = True                       # held still: long press now, like iOS
            self.send({"t": "long", "x": d["x"], "y": d["y"], "ms": 750})
            self.ripples.append((d["x"], d["y"], now))
        if self.wheel != [0.0, 0.0] and now - self.wheel_t > 0.08:
            self._flush_wheel()
        if self.text and now - self.text_t > 0.04:
            s, self.text = self.text, ""
            self.send({"t": "text", "s": s})
        self.ripples = [r for r in self.ripples if now - r[2] < 0.35]


def main() -> None:
    ap = argparse.ArgumentParser(description="Show and control your iPhone from this PC.")
    ap.add_argument("--host", help="phone's Wi-Fi IP address (skips auto-discovery)")
    ap.add_argument("--usb-only", action="store_true", help="never use Wi-Fi")
    ap.add_argument("--forget", action="store_true", help="delete this PC's pairing and exit")
    args = ap.parse_args()
    if args.forget:
        pairing_store.forget()
        print("Pairing removed. Connect over USB to pair again.")
        return

    enable_dpi_awareness()
    pygame.init()
    pygame.display.set_caption("MirrorLink")
    try:
        pygame.display.set_icon(pygame.image.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mirrorlink.png")))
    except (pygame.error, FileNotFoundError):
        pass
    info = pygame.display.Info()
    desk_w, desk_h = (info.current_w or 1920), (info.current_h or 1080)
    win_scale = 1.0

    def window_size_for(pw: int, ph: int, rot: int) -> tuple[int, int]:
        w, h = (ph, pw) if rot in (90, 270) else (pw, ph)
        long_side = desk_h * 0.82 * win_scale
        if h >= w:
            H = long_side
            W = H * w / h
        else:
            W = min(long_side * 1.15, desk_w * 0.95)
            H = W * h / w
        return max(240, int(W)), max(240, int(H) + TOP_BAR)

    win_w, win_h = window_size_for(9, 19.5, 0)
    screen = pygame.display.set_mode((win_w, win_h), pygame.RESIZABLE)
    pygame.key.start_text_input()
    font = pygame.font.SysFont("segoeui,arial", 15)
    big = pygame.font.SysFont("segoeui,arial", 19)
    btn_font = pygame.font.SysFont("segoeui,arial", 14, bold=True)

    stats = Stats()
    rx = Receiver(stats, host=args.host, usb_only=args.usb_only)
    rx.start()
    helper = ControlHelper()
    use_bt = load_settings().get("control", "bluetooth") != "helper"
    bt = BtControl() if use_bt else None
    auto_tried_for = 0.0
    go_home_after_launch = False   # WDA's blank screen comes to the front when it starts

    notes: list[tuple[str, float]] = []
    def note(msg: str) -> None:
        if not notes or notes[-1][0] != msg:
            notes.append((msg, time.time() + 3))

    ctl = BtInput(bt, note) if use_bt else InputController(rx, note)
    show_stats = True
    fullscreen = False
    manual_rot = 0
    sized_for = None
    last_surface = None
    last_rect = (0, TOP_BAR, 0, 0)
    last_frame = None
    running = True
    last_draw = 0.0
    max_frames = int(os.environ.get("MIRRORLINK_EXIT_AFTER_FRAMES", "0"))  # for automated tests

    cfg_cache = {"mtime": None, "map": {}, "all": {}}

    def current_rotation() -> int:
        m = settings_mtime()
        if m != cfg_cache["mtime"]:                   # re-read settings.json only when saved
            cfg_cache["mtime"] = m
            cfg_cache["all"] = load_settings()
            cfg_cache["map"] = cfg_cache["all"].get("orientation_map", {})
        return (rotation_for(rx.orientation_raw, cfg_cache["map"]) + manual_rot) % 360

    def image_area() -> tuple[int, int, int, int]:
        sw, sh = screen.get_size()
        top = 0 if fullscreen else TOP_BAR
        return 0, top, sw, sh - top

    def to_norm(pos):
        x, y, w, h = last_rect
        if w <= 0 or h <= 0:
            return None
        nx, ny = (pos[0] - x) / w, (pos[1] - y) / h
        if 0 <= nx <= 1 and 0 <= ny <= 1:
            return nx, ny
        return None

    def bar_buttons():
        """Home and App Switcher buttons in the top strip (right side), like macOS."""
        sw = screen.get_width()
        h = TOP_BAR - 10
        apps = pygame.Rect(sw - 8 - 104, 5, 104, h)
        home = pygame.Rect(apps.left - 6 - 64, 5, 64, h)
        rotate = pygame.Rect(home.left - 6 - 64, 5, 64, h)
        return {"rotate": rotate, "home": home, "switcher": apps}

    def home_zone():
        x, y, w, h = last_rect
        zh = max(10, int(h * 0.025))
        return pygame.Rect(x, y + h - zh, w, zh)

    def set_window(size):
        nonlocal screen, win_w, win_h
        win_w, win_h = size
        if not fullscreen:
            screen = pygame.display.set_mode((win_w, win_h), pygame.RESIZABLE)

    while running:
        dirty = False
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN and use_bt and bt.pairing:
                # A pairing code is on screen: Enter = the codes match, Esc = no.
                if ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    bt.answer_pairing(True)
                    note("Paired — tap Pair on the iPhone too if it's still asking")
                elif ev.key == pygame.K_ESCAPE:
                    bt.answer_pairing(False)
            elif ev.type == pygame.TEXTINPUT and use_bt and bt.pairing:
                pass
            elif ev.type == pygame.KEYDOWN:
                # Ctrl shortcuts (Cmd on a Mac keyboard). Ctrl+Alt is AltGr on many
                # layouts (e.g. AltGr+Q = @), so that's typing, not a shortcut.
                ctrl = bool(ev.mod & (pygame.KMOD_CTRL | pygame.KMOD_META)) and not (ev.mod & pygame.KMOD_ALT)
                if ctrl and ev.key == pygame.K_q:
                    running = False
                elif ctrl and ev.key in (pygame.K_1, pygame.K_h):
                    ctl.key("home")
                elif ctrl and ev.key == pygame.K_2:
                    ctl.key("switcher")
                elif ctrl and ev.key == pygame.K_3:
                    ctl.key("spotlight")
                elif ctrl and ev.key == pygame.K_4:
                    ctl.key("control")
                elif ctrl and ev.key == pygame.K_5:
                    ctl.key("notifications")
                elif ctrl and ev.key == pygame.K_UP:
                    ctl.key("volup")
                elif ctrl and ev.key == pygame.K_DOWN:
                    ctl.key("voldown")
                elif ctrl and ev.key == pygame.K_v:
                    t = clipboard_text()
                    if t:
                        ctl.type_text(t.replace("\r\n", "\n"))
                elif ctrl and ev.key == pygame.K_k:
                    if use_bt:
                        ctl.key("keyboard")
                        note("Showing / hiding the iPhone's on-screen keyboard")
                elif ctrl and ev.key == pygame.K_r:
                    manual_rot = (manual_rot + 90) % 360
                    sized_for = None
                    note(f"Rotated view ({manual_rot}° extra). Ctrl+R again to keep turning.")
                elif ctrl and ev.key in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
                    win_scale = min(1.6, win_scale + 0.1); sized_for = None
                elif ctrl and ev.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
                    win_scale = max(0.4, win_scale - 0.1); sized_for = None
                elif ctrl and ev.key in (pygame.K_0, pygame.K_KP0):
                    win_scale = 1.0; sized_for = None
                elif ev.key == pygame.K_F1:
                    show_stats = not show_stats
                elif ev.key == pygame.K_F5 and use_bt:
                    bt.take_over()
                    note("Reconnecting Bluetooth control…")
                elif ev.key == pygame.K_F5:
                    if rx.link_kind != "usb":
                        note("Plug in the USB cable to start the control helper")
                    elif helper.start("manual"):
                        note("Starting the control helper…")
                elif ev.key == pygame.K_F11:
                    fullscreen = not fullscreen
                    screen = pygame.display.set_mode(
                        (0, 0) if fullscreen else (win_w, win_h),
                        pygame.FULLSCREEN if fullscreen else pygame.RESIZABLE)
                    last_surface = None
                elif ev.key == pygame.K_r and rx.kicked and not ctrl:
                    rx.resume.set()
                elif ctrl and use_bt and not rx.kicked:
                    # other Ctrl+letter = Cmd+letter on the iPhone (Ctrl+A select all, Ctrl+Z undo...)
                    ctl.command(pygame.key.name(ev.key), bool(ev.mod & pygame.KMOD_SHIFT))
                elif not ctrl and use_bt:
                    if not rx.kicked:
                        ctl.key_down(ev.key, pygame.key.name(ev.key), bool(ev.mod & pygame.KMOD_SHIFT))
                elif not ctrl:
                    if ev.key == pygame.K_BACKSPACE:
                        ctl.type_text("\b")
                    elif ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        ctl.type_text("\n")
                    elif ev.key == pygame.K_TAB:
                        ctl.type_text("\t")
            elif ev.type == pygame.KEYUP and use_bt:
                ctl.key_up(ev.key)
            elif ev.type == pygame.TEXTINPUT:
                if not rx.kicked:
                    ctl.type_text(ev.text)
            elif ev.type == pygame.MOUSEBUTTONDOWN:
                if ev.button == 1:
                    hit = next((k for k, r in bar_buttons().items() if r.collidepoint(ev.pos)), None)
                    if hit == "rotate" and not fullscreen and ev.pos[1] < TOP_BAR:
                        manual_rot = (manual_rot + 90) % 360
                        sized_for = None
                    elif hit and not fullscreen and ev.pos[1] < TOP_BAR:
                        ctl.key(hit)
                    elif home_zone().collidepoint(ev.pos):
                        ctl.key("home")             # the home bar, like on macOS
                    else:
                        p = to_norm(ev.pos)
                        if p:
                            ctl.press(*p)
                elif ev.button == 3:
                    p = to_norm(ev.pos)
                    if p:
                        ctl.right_click(*p)
                dirty = True
            elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                x, y, w, h = last_rect
                nx = min(1, max(0, (ev.pos[0] - x) / w)) if w else 0
                ny = min(1, max(0, (ev.pos[1] - y) / h)) if h else 0
                ctl.release(nx, ny)
                dirty = True
            elif ev.type == pygame.MOUSEMOTION:
                if ctl.down:
                    x, y, w, h = last_rect
                    if w and h:
                        ctl.motion(min(1, max(0, (ev.pos[0] - x) / w)), min(1, max(0, (ev.pos[1] - y) / h)))
                elif use_bt:
                    p = to_norm(ev.pos)
                    if p:
                        ctl.hover(*p)
                dirty = True                         # hover bar / home pill
            elif ev.type == pygame.MOUSEWHEEL:
                p = to_norm(pygame.mouse.get_pos())
                dx = getattr(ev, "precise_x", ev.x)
                dy = getattr(ev, "precise_y", ev.y)
                if getattr(ev, "flipped", False):
                    dx, dy = -dx, -dy
                ctl.wheel_event(dx, dy, p)
            elif ev.type == pygame.VIDEORESIZE and not fullscreen:
                win_w, win_h = ev.w, ev.h
                last_surface = None
                dirty = True

        ctl.tick()

        # Start the control helper automatically (USB only, once per connection).
        if (not use_bt and rx.connected and rx.link_kind == "usb" and not rx.control_ready and not helper.busy
                and rx.connected_at != auto_tried_for and time.time() - rx.connected_at > 4):
            auto_tried_for = rx.connected_at
            helper.start("auto")
        if helper.status.startswith("Control: helper started"):
            go_home_after_launch = True
        if go_home_after_launch and rx.control_ready:
            go_home_after_launch = False
            helper.status = ""
            ctl.key("home")
            note("Control ready: click to tap, drag to swipe, type to type")

        # Resize the window to the phone's real aspect ratio / orientation.
        rot = current_rotation()
        if use_bt:
            pts = rx.points
            if pts is None and rx.size:            # older phone app: estimate from the picture
                pts = (402.0, 402.0 * rx.size[1] / max(1, rx.size[0]))
            ctl.configure(rot, pts, cfg_cache["all"])
        if rx.size and (rx.size, rot, win_scale) != sized_for and not fullscreen:
            set_window(window_size_for(rx.size[0], rx.size[1], rot))
            sized_for = (rx.size, rot, win_scale)
            last_surface = None
            dirty = True

        got = rx.new_frame.wait(0.004)
        frame = rx.latest() if got else None
        if got:
            rx.new_frame.clear()
        if frame is not None:
            last_frame = frame
        if (frame is not None or (last_surface is None and last_frame is not None)) and last_frame is not None:
            f = last_frame
            ax, ay, aw, ah = image_area()
            rw, rh = (f.height, f.width) if rot in (90, 270) else (f.width, f.height)
            x, y, w, h = fit(rw, rh, aw, ah)
            sw_, sh_ = (h, w) if rot in (90, 270) else (w, h)     # size before rotation
            rgb = f.to_ndarray(format="rgb24", width=sw_, height=sh_, interpolation="BILINEAR")
            if rot:
                rgb = np.ascontiguousarray(np.rot90(rgb, k=-(rot // 90)))
            last_surface = pygame.image.frombuffer(rgb.tobytes(), (w, h), "RGB")
            last_rect = (ax + x, ay + y, w, h)
            if frame is not None:
                with stats.lock:
                    stats.shown += 1
                if max_frames:
                    max_frames -= 1
                    if max_frames == 0:
                        running = False
            dirty = True

        stats.tick()
        now = time.perf_counter()
        if not dirty and not ctl.ripples and now - last_draw < 0.25:
            continue
        last_draw = now

        # ---- draw -------------------------------------------------------------
        screen.fill((12, 12, 14))
        mouse = pygame.mouse.get_pos()
        if last_surface is not None:
            screen.blit(last_surface, last_rect[:2])
        else:
            msg = big.render(rx.status, True, (220, 220, 225))
            screen.blit(msg, msg.get_rect(center=(screen.get_width() // 2, screen.get_height() // 2)))

        # Top strip: Home / App Switcher buttons appear on hover (like macOS).
        if not fullscreen and mouse[1] < TOP_BAR + 6 and pygame.mouse.get_focused():
            for name, r in bar_buttons().items():
                hover = r.collidepoint(mouse)
                pygame.draw.rect(screen, (70, 70, 78) if hover else (44, 44, 50), r, border_radius=7)
                label = btn_font.render({"home": "Home", "rotate": "Rotate"}.get(name, "App Switcher"), True, (235, 235, 240))
                screen.blit(label, label.get_rect(center=r.center))
            hint = font.render("Ctrl+1 Home · Ctrl+2 Apps · Ctrl+3 Search", True, (130, 130, 140))
            if hint.get_width() + 20 < bar_buttons()["rotate"].left:
                screen.blit(hint, (10, (TOP_BAR - hint.get_height()) // 2))

        # Home bar at the bottom of the phone picture: highlight on hover, click = Home.
        if last_surface is not None and home_zone().collidepoint(mouse):
            hz = home_zone()
            pill = pygame.Rect(0, 0, int(last_rect[2] * 0.36), max(5, hz.height // 2))
            pill.center = hz.center
            pygame.draw.rect(screen, (245, 245, 250), pill, border_radius=4)

        # Tap feedback.
        x0, y0, w0, h0 = last_rect
        for nx, ny, t in ctl.ripples:
            a = min(1.0, max(0.0, (now - t) / 0.35))
            r = int(10 + 18 * a)
            surf = pygame.Surface((r * 2 + 2, r * 2 + 2), pygame.SRCALPHA)
            pygame.draw.circle(surf, (255, 255, 255, int(160 * (1 - a))), (r + 1, r + 1), r, 3)
            screen.blit(surf, (x0 + nx * w0 - r - 1, y0 + ny * h0 - r - 1))

        # Status / stats overlay.
        if show_stats:
            s = stats.snapshot
            rtt = f"{s['rtt_ms']:.1f} ms" if s["rtt_ms"] is not None else "–"
            if use_bt:
                control = bt.status_line()
            elif rx.control_ready:
                control = "Control: ready"
            elif helper.status:
                control = helper.status
            elif rx.connected and rx.link_kind != "usb":
                control = "Control: not running — plug in the USB cable once to start it"
            else:
                control = "Control: not ready (F5 to start)"
            lines = [
                f"{rx.device_name or 'iPhone'} · "
                f"{ {'usb': 'USB', 'wifi': 'Wi-Fi (encrypted)'}.get(rx.link_kind or '', 'not connected') }"
                f"{' · paired' if rx.pairing else ''}",
                f"video {s['rx_fps']} fps · shown {s['show_fps']} fps · {s['mbps']:.1f} Mbps · {rx.mode}",
                f"decode {s['decode_ms']:.1f} ms · round trip {rtt}"
                f"{' · ' + str(rx.size[0]) + '×' + str(rx.size[1]) if rx.size else ''}"
                f" · orientation {rx.orientation_raw if rx.orientation_raw is not None else '–'} ({rot}°)",
                control,
                *([rx.status_note] if time.time() < rx.note_until else []),
                ("F1 stats · F5 reconnect control · F11 fullscreen · Ctrl+K phone keyboard · Ctrl+R rotate · Ctrl+Q quit"
                 if use_bt else "F1 stats · F5 start control · F11 fullscreen · Ctrl+R rotate · Ctrl+Q quit"),
            ]
            pad = 7
            surfs = [font.render(t, True, (235, 235, 240)) for t in lines]
            bw = max(t.get_width() for t in surfs) + pad * 2
            bh = sum(t.get_height() for t in surfs) + pad * 2
            box = pygame.Surface((bw, bh), pygame.SRCALPHA)
            box.fill((0, 0, 0, 160))
            bx, by = 8, (8 if fullscreen else TOP_BAR + 6)
            screen.blit(box, (bx, by))
            yy = by + pad
            for t in surfs:
                screen.blit(t, (bx + pad, yy))
                yy += t.get_height()

        # Toast notes (bottom).
        tnow = time.time()
        notes[:] = [n for n in notes if n[1] > tnow]
        if notes:
            t = font.render(notes[-1][0], True, (255, 255, 255))
            r = t.get_rect(midbottom=(screen.get_width() // 2, screen.get_height() - 30))
            bg = pygame.Surface((r.width + 20, r.height + 10), pygame.SRCALPHA)
            bg.fill((0, 0, 0, 190))
            screen.blit(bg, (r.x - 10, r.y - 5))
            screen.blit(t, r)
        pr = bt.pairing if use_bt else None
        if pr:
            lines = [(big, f"Pair with {(pr.get('phone') or 'iPhone')[:22]}?", (255, 255, 255)),
                     (big, f"Code: {pr['pin']}" if pr.get("pin") else "", (120, 220, 255)),
                     (font, "Same code on the iPhone?", (220, 220, 225)),
                     (font, "Tap Pair there, then press Enter here.", (220, 220, 225)),
                     (font, "Esc = cancel", (160, 160, 170))]
            surfs = [f_.render(t_, True, c_) for f_, t_, c_ in lines if t_]
            bw = max(x.get_width() for x in surfs) + 40
            bh = sum(x.get_height() + 6 for x in surfs) + 30
            box = pygame.Surface((bw, bh), pygame.SRCALPHA)
            box.fill((20, 22, 30, 235))
            bx, by = (screen.get_width() - bw) // 2, (screen.get_height() - bh) // 2
            screen.blit(box, (bx, by))
            pygame.draw.rect(screen, (90, 160, 255), (bx, by, bw, bh), 2, border_radius=8)
            yy = by + 15
            for x in surfs:
                screen.blit(x, (bx + (bw - x.get_width()) // 2, yy))
                yy += x.get_height() + 6
            dirty = True
        if rx.kicked:
            t = big.render(rx.status, True, (255, 220, 120))
            screen.blit(t, t.get_rect(center=(screen.get_width() // 2, screen.get_height() // 2)))

        pygame.display.flip()

    snap = os.environ.get("MIRRORLINK_SNAPSHOT")   # for automated tests
    if snap:
        pygame.image.save(screen, snap)
    if bt is not None:
        bt.close()
    rx.stop()
    pygame.quit()


def run_with_crash_log() -> None:
    """The taskbar shortcut has no console, so record crashes in pc/viewer-error.log
    and show a message box instead of vanishing silently."""
    try:
        main()
    except Exception:
        import traceback
        text = traceback.format_exc()
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "viewer-error.log")
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(time.strftime("\n--- %Y-%m-%d %H:%M:%S\n") + text)
        except OSError:
            pass
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, f"MirrorLink stopped because of an error.\n\nDetails were saved to:\n{path}",
                                                 "MirrorLink", 0x10)
            except Exception:
                pass
        raise


if __name__ == "__main__":
    run_with_crash_log()
