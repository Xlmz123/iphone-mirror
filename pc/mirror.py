"""MirrorLink viewer — shows your iPhone's screen on your PC over USB.

Run:  python mirror.py
Keys: F1 = stats overlay   F11 = fullscreen   Esc = quit

Design for low latency:
  * network thread decodes every packet as it arrives (H.264, no B-frames)
  * only the NEWEST decoded frame is kept; the window never queues frames
  * scaling to window size happens once per displayed frame, in FFmpeg
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading
import time

import av
import pygame

import usbmux

PORT = 47800
HEADER = struct.Struct(">IBQ")   # length, type, timestamp(us)

MSG_HELLO, MSG_KEY, MSG_DELTA = 0x01, 0x02, 0x03
MSG_REQ_KEY, MSG_PING, MSG_PONG = 0x10, 0x11, 0x12


_send_lock = threading.Lock()


def send_msg(sock: socket.socket, mtype: int, payload: bytes = b"") -> None:
    with _send_lock:   # pinger + receiver both send; keep messages whole
        sock.sendall(HEADER.pack(len(payload), mtype, 0) + payload)


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
    """Connects over USB, decodes video, publishes the latest frame."""

    def __init__(self, stats: Stats) -> None:
        super().__init__(daemon=True)
        self.stats = stats
        self.status = "Starting…"
        self.device_name = ""
        self.size: tuple[int, int] | None = None
        self.connected = False
        self._latest: av.VideoFrame | None = None
        self._lock = threading.Lock()
        self.new_frame = threading.Event()
        self._stop = threading.Event()
        self.sock: socket.socket | None = None

    def latest(self) -> av.VideoFrame | None:
        with self._lock:
            f, self._latest = self._latest, None
        return f

    def stop(self) -> None:
        self._stop.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.status = "Looking for iPhone on USB…"
                self.sock = usbmux.connect(PORT)
                self.connected = True
                self.status = "Connected — waiting for video (tap Start Mirroring on the phone)"
                self._session(self.sock)
            except usbmux.UsbmuxError as e:
                self.status = str(e)
            except (OSError, ConnectionError) as e:
                if not self._stop.is_set():
                    self.status = f"Disconnected ({e.__class__.__name__}). Reconnecting…"
            finally:
                self.connected = False
                if self.sock:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                    self.sock = None
            self._stop.wait(1.0)

    def _session(self, sock: socket.socket) -> None:
        codec = av.CodecContext.create("h264", "r")
        codec.options = {"flags": "low_delay"}   # output each frame immediately
        codec.thread_type = "SLICE"              # frame threading would add latency
        waiting_for_key = True

        # Ping every second to measure the USB round trip.
        def pinger() -> None:
            while self.sock is sock and not self._stop.is_set():
                try:
                    send_msg(sock, MSG_PING, struct.pack(">d", time.perf_counter()))
                except OSError:
                    return
                time.sleep(1.0)
        threading.Thread(target=pinger, daemon=True).start()

        f = sock.makefile("rb", buffering=1 << 20)
        while not self._stop.is_set():
            hdr = f.read(HEADER.size)
            if len(hdr) < HEADER.size:
                raise ConnectionError("phone closed the connection")
            length, mtype, _ts = HEADER.unpack(hdr)
            payload = f.read(length)
            if len(payload) < length:
                raise ConnectionError("phone closed the connection")

            if mtype == MSG_HELLO:
                info = json.loads(payload)
                self.size = (int(info["w"]), int(info["h"]))
                self.device_name = info.get("name", "iPhone")
                self.status = f"Streaming from {self.device_name}"
            elif mtype == MSG_PONG:
                (sent,) = struct.unpack(">d", payload[:8])
                with self.stats.lock:
                    self.stats.rtt_ms = (time.perf_counter() - sent) * 1000
            elif mtype in (MSG_KEY, MSG_DELTA):
                if waiting_for_key and mtype != MSG_KEY:
                    continue
                waiting_for_key = False
                t0 = time.perf_counter()
                try:
                    frames = codec.decode(av.Packet(payload))
                except av.error.FFmpegError:
                    # Corrupt/missing data: ask the phone for a fresh keyframe.
                    waiting_for_key = True
                    send_msg(sock, MSG_REQ_KEY)
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
    rx = Receiver(stats)
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
                f"{rx.device_name or 'iPhone'} · {'USB' if rx.connected else 'not connected'}",
                f"video {s['rx_fps']} fps · shown {s['show_fps']} fps · {s['mbps']:.1f} Mbps",
                f"decode {s['decode_ms']:.1f} ms · USB round trip {rtt}",
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
