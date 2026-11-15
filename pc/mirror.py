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

MSG_HELLO, MSG_KEY, MSG_DELTA, MSG_PAIR_INFO = 0x01, 0x02, 0x03, 0x21
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

    @property
    def connected(self) -> bool:
        return self.link_kind is not None

    def latest(self) -> av.VideoFrame | None:
        with self._lock:
            f, self._latest = self._latest, None
        return f

    def stop(self) -> None:
        self._stop.set()
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
                link.close()
            self._stop.wait(0.5)

    # ---- streaming ----------------------------------------------------------

    def _session(self, link: PlainLink) -> None:
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

                if mtype in (MSG_KEY, MSG_DELTA):
                    # Acknowledge on arrival: tells the phone the link is keeping up.
                    send_msg(link, MSG_ACK, struct.pack(">Q", ts))
                    if waiting_for_key and mtype != MSG_KEY:
                        continue
                    waiting_for_key = False
                    t0 = time.perf_counter()
                    try:
                        frames = codec.decode(av.Packet(payload))
                    except av.error.FFmpegError:
                        waiting_for_key = True          # ask for a clean restart point
                        send_msg(link, MSG_REQ_KEY)
                        continue
                    dt = (time.perf_counter() - t0) * 1000
                    with self.stats.lock:
                        self.stats.rx_frames += 1
                        self.stats.rx_bytes += length
                        self.stats.decode_ms = self.stats.decode_ms * 0.9 + dt * 0.1
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

                elif mtype == MSG_PONG:
                    (sent,) = struct.unpack(">d", payload[:8])
                    with self.stats.lock:
                        self.stats.rtt_ms = (time.perf_counter() - sent) * 1000
        finally:
            alive.clear()


# ----------------------------------------------------------------------------- UI

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


def main() -> None:
    ap = argparse.ArgumentParser(description="Show your iPhone's screen on this PC.")
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
    info = pygame.display.Info()
    desk_h = info.current_h or 1080
    win_h = int(desk_h * 0.85)
    win_w = int(win_h * 9 / 19.5)          # typical iPhone aspect until we know the real one
    screen = pygame.display.set_mode((win_w, win_h), pygame.RESIZABLE)
    font = pygame.font.SysFont("segoeui,arial", 16)
    big = pygame.font.SysFont("segoeui,arial", 20)

    stats = Stats()
    rx = Receiver(stats, host=args.host, usb_only=args.usb_only)
    rx.start()

    show_stats = True
    fullscreen = False
    sized_for: tuple[int, int] | None = None
    last_surface: pygame.Surface | None = None
    last_rect = (0, 0, 0, 0)
    running = True
    last_draw = 0.0
    max_frames = int(os.environ.get("MIRRORLINK_EXIT_AFTER_FRAMES", "0"))  # for automated tests

    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    running = False
                elif ev.key == pygame.K_F1:
                    show_stats = not show_stats
                elif ev.key == pygame.K_F11:
                    fullscreen = not fullscreen
                    screen = pygame.display.set_mode(
                        (0, 0) if fullscreen else (win_w, win_h),
                        pygame.FULLSCREEN if fullscreen else pygame.RESIZABLE)
                    last_surface = None
                    last_draw = 0.0
            elif ev.type == pygame.VIDEORESIZE and not fullscreen:
                win_w, win_h = ev.w, ev.h
                last_surface = None
                last_draw = 0.0

        # Resize the window once to the phone's real aspect ratio.
        if rx.size and rx.size != sized_for and not fullscreen:
            pw, ph = rx.size
            win_h = int(desk_h * 0.85)
            win_w = int(win_h * pw / ph)
            screen = pygame.display.set_mode((win_w, win_h), pygame.RESIZABLE)
            sized_for = rx.size

        got = rx.new_frame.wait(0.004)
        frame = rx.latest() if got else None
        if got:
            rx.new_frame.clear()

        if frame is not None:
            sw, sh = screen.get_size()
            x, y, w, h = fit(frame.width, frame.height, sw, sh)
            rgb = frame.to_ndarray(format="rgb24", width=w, height=h, interpolation="BILINEAR")
            last_surface = pygame.image.frombuffer(rgb.tobytes(), (w, h), "RGB")
            last_rect = (x, y, w, h)
            with stats.lock:
                stats.shown += 1
            if max_frames:
                max_frames -= 1
                if max_frames == 0:
                    running = False

        stats.tick()
        now = time.perf_counter()
        if frame is None and now - last_draw < 0.25:
            continue          # nothing new: don't burn CPU redrawing
        last_draw = now
        screen.fill((12, 12, 14))
        if last_surface is not None:
            screen.blit(last_surface, last_rect[:2])
        else:
            msg = big.render(rx.status, True, (220, 220, 225))
            screen.blit(msg, msg.get_rect(center=(screen.get_width() // 2, screen.get_height() // 2)))

        if show_stats:
            s = stats.snapshot
            rtt = f"{s['rtt_ms']:.1f} ms" if s["rtt_ms"] is not None else "–"
            lines = [
                f"{rx.device_name or 'iPhone'} · "
                f"{ {'usb': 'USB', 'wifi': 'Wi-Fi (encrypted)'}.get(rx.link_kind or '', 'not connected') }"
                f"{' · paired' if rx.pairing else ''}",
                f"video {s['rx_fps']} fps · shown {s['show_fps']} fps · {s['mbps']:.1f} Mbps",
                f"decode {s['decode_ms']:.1f} ms · round trip {rtt}"
                f"{' · ' + str(rx.size[0]) + '×' + str(rx.size[1]) if rx.size else ''}",
                *([rx.status_note] if time.time() < rx.note_until else []),
                "F1 stats · F11 fullscreen · Esc quit",
            ]
            pad = 8
            surf = [font.render(t, True, (235, 235, 240)) for t in lines]
            bw = max(t.get_width() for t in surf) + pad * 2
            bh = sum(t.get_height() for t in surf) + pad * 2
            box = pygame.Surface((bw, bh), pygame.SRCALPHA)
            box.fill((0, 0, 0, 150))
            screen.blit(box, (10, 10))
            yy = 10 + pad
            for t in surf:
                screen.blit(t, (10 + pad, yy))
                yy += t.get_height()

        pygame.display.flip()

    snap = os.environ.get("MIRRORLINK_SNAPSHOT")   # for automated tests
    if snap:
        pygame.image.save(screen, snap)
    rx.stop()
    pygame.quit()


if __name__ == "__main__":
    main()
