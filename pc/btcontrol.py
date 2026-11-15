"""Viewer side of Bluetooth control: starts blehid.py (the PC-as-Bluetooth-mouse-and-keyboard
service), talks to it, and turns the viewer's mouse and keyboard into its commands.

Positions are fractions (0..1) of the phone picture as the viewer shows it (already rotated
to the phone's orientation). iOS maps a Bluetooth pointer to the screen as it is currently
shown, so by default they're sent as they are; settings.json "bt_coordinates": "portrait"
converts them back to the phone's upright (portrait) frame instead, in case a phone does it
that way.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import blehid
from helper import _uv

HERE = Path(__file__).resolve().parent
LOG = HERE / "bluetooth.log"
WINRT = ["winrt-runtime", "winrt-Windows.Foundation", "winrt-Windows.Foundation.Collections",
         "winrt-Windows.Devices.Bluetooth", "winrt-Windows.Devices.Bluetooth.GenericAttributeProfile",
         "winrt-Windows.Devices.Enumeration", "winrt-Windows.Devices.Radios", "winrt-Windows.Storage.Streams"]

# pygame key -> HID usage, for keys that are held (iOS repeats them itself) rather than typed
SPECIAL_KEYS = {
    "backspace": 42, "return": 40, "enter": 40, "tab": 43, "escape": 41, "delete": 76,
    "right": 79, "left": 80, "down": 81, "up": 82, "home": 74, "end": 77, "page up": 75, "page down": 78,
    "keypad enter": 40,
}
MOD_SHIFT, MOD_CMD, MOD_ALT = 0x02, 0x08, 0x04
LETTER = {chr(ord("a") + i): 4 + i for i in range(26)}
LETTER.update({str(d): 30 + (d - 1 if d else 9) for d in range(10)})


class BtControl:
    """Keeps blehid.py running and connected; exposes its status."""

    def __init__(self) -> None:
        self.status: dict = {}
        self.connected_service = False
        self.started_at = time.time()
        self.spawned_at = 0.0
        self.spawns = 0
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()
        self.stopping = False
        self.on_connect = None            # called (from the thread) after each (re)connect
        self.problem: str | None = None
        self.kicked = False              # another MirrorLink window took over Bluetooth control
        self.asked_restart = False
        threading.Thread(target=self._run, daemon=True, name="bt-control").start()

    # ---- what the viewer shows
    @property
    def ready(self) -> bool:
        return self.connected_service and bool(self.status.get("connected"))

    def status_line(self) -> str:
        if self.kicked:
            return "Control (Bluetooth): another MirrorLink window is using it (F5 to take it back)"
        if self.problem:
            return "Control (Bluetooth): " + self.problem
        if not self.connected_service:
            if self.spawns and time.time() - self.spawned_at > 6:
                return "Control (Bluetooth): getting Bluetooth support ready (first time takes a minute)…"
            return "Control (Bluetooth): starting…"
        s = self.status
        if s.get("pairing"):
            return "Control (Bluetooth): pairing — compare the code (box in the middle)"
        if s.get("problem"):
            return "Control (Bluetooth): " + s["problem"]
        if not s.get("published"):
            return "Control (Bluetooth): setting up…"
        if s.get("connected"):
            return f"Control (Bluetooth): connected to {s.get('phone') or 'iPhone'}"
        return (f"Control (Bluetooth): waiting for the iPhone — on the phone: Settings → Bluetooth → "
                f"tap \"{s.get('pc') or 'this PC'}\" (first time only)")

    # ---- talking to the service
    def send(self, line: str) -> bool:
        with self.lock:
            if self.sock is None:
                return False
            try:
                self.sock.sendall((line + "\n").encode("utf-8"))
                return True
            except OSError:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
                return False

    @property
    def pairing(self) -> dict | None:
        """{"phone", "pin"} while the PC waits for the user to confirm a pairing code."""
        return self.status.get("pairing") if self.connected_service else None

    def answer_pairing(self, yes: bool) -> None:
        self.send("pair yes" if yes else "pair no")
        self.status = dict(self.status, pairing=None)

    def take_over(self) -> None:
        """F5: reconnect (and take control back from another viewer window). Doesn't
        restart the Bluetooth service: that can make the iPhone need pairing again."""
        self.kicked = False
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None

    def close(self) -> None:
        self.stopping = True
        self.send("bye")
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None

    def _spawn(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return                                  # still starting (first run downloads packages)
        custom = os.environ.get("MIRRORLINK_BT_CMD")    # tests: run the fake service
        if custom:
            cmd = custom.split()
        elif sys.platform != "win32":
            self.problem = "only works on Windows"
            return
        else:
            uv = _uv()
            if uv is None:
                subprocess.run([sys.executable, "-m", "pip", "install", "-q", "uv"], capture_output=True)
                uv = _uv()
            if uv is None:
                self.problem = "couldn't install 'uv' (run run.bat again)"
                return
            cmd = [uv, "run", "--no-project", "--quiet", "--python", "3.13"]
            for pkg in WINRT:
                cmd += ["--with", pkg]
            cmd += ["python", str(HERE / "blehid.py")]
        try:
            if LOG.exists() and LOG.stat().st_size > 1_000_000:
                LOG.unlink()
            out = open(LOG, "a", encoding="utf-8")
            out.write(time.strftime("\n--- %Y-%m-%d %H:%M:%S starting\n"))
            out.flush()
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
            # It keeps running after the viewer closes (see blehid.py), so detach it from
            # this window's console and, where allowed, from any job that would end it.
            base = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0
            try:
                extra = 0x01000000 if sys.platform == "win32" else 0      # CREATE_BREAKAWAY_FROM_JOB
                self.proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=str(HERE),
                                             env=env, creationflags=base | extra,
                                             start_new_session=sys.platform != "win32")
            except OSError:
                self.proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=str(HERE),
                                             env=env, creationflags=base)
            self.spawned_at = time.time()
            self.spawns += 1
        except OSError as e:
            self.problem = f"couldn't start ({e})"

    def _run(self) -> None:
        token = blehid.load_token()
        while not self.stopping:
            if self.kicked:
                time.sleep(0.3)
                continue
            try:
                s = socket.create_connection(("127.0.0.1", blehid.PORT), timeout=1.0)
            except OSError:
                self.connected_service = False
                if self.proc is None or self.proc.poll() is not None:
                    if self.proc is not None and self.proc.poll() not in (None, 0, 3):
                        tail = _log_tail()
                        if time.time() - self.spawned_at < 30 and self.spawns >= 3:
                            self.problem = "keeps stopping — see pc/bluetooth.log" + (f" ({tail})" if tail else "")
                    if self.spawns < 3 or time.time() - self.spawned_at > 30:
                        self._spawn()
                time.sleep(0.5)
                continue
            s.settimeout(None)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                s.sendall(f"auth {token}\n".encode())
            except OSError:
                s.close()
                continue
            with self.lock:
                self.sock = s
            self.connected_service = True
            self.problem = None
            if self.on_connect:
                try:
                    self.on_connect()
                except Exception:
                    pass
            f = s.makefile("rb")
            try:
                for raw in f:
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("status "):
                        try:
                            self.status = json.loads(line[7:])
                        except ValueError:
                            pass
                        # An older copy from before an update: start the new one (once).
                        if self.status.get("version") != blehid.VERSION and not self.asked_restart:
                            self.asked_restart = True
                            self.send("restart")
                    elif line == "kicked":
                        self.kicked = True
            except OSError:
                pass
            with self.lock:
                if self.sock is s:
                    self.sock = None
            self.connected_service = False
            self.status = {}
            time.sleep(0.3)


def _log_tail() -> str:
    try:
        lines = [l.strip() for l in LOG.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
        for l in reversed(lines[-30:]):
            if "Error" in l or "error" in l:
                return l[-140:]
    except OSError:
        pass
    return ""


class BtInput:
    """Same interface as mirror.InputController, but live: the touch follows the mouse."""

    HOVER_EVERY = 0.012

    def __init__(self, bt: BtControl, note) -> None:
        self.bt = bt
        self.note = note
        self.down = None
        self.ripples: list[tuple[float, float, float]] = []
        self.text = ""
        self.text_t = 0.0
        self.rot = 0
        self.points = (402.0, 874.0)        # the phone's screen, portrait, in points
        self.portrait_coords = False
        self.wheel_step = 0.12             # screen fraction per wheel notch
        self.last_hover = (0.0, None)
        self.sent_screen = None
        self.held: dict[int, int] = {}     # pygame key -> HID usage held down
        self.cfg: dict = {}
        bt.on_connect = self._on_connect

    # ---- geometry
    def configure(self, rot: int, points: tuple[float, float] | None, cfg: dict) -> None:
        self.rot = rot
        if cfg is not self.cfg:
            resend = any(cfg.get(k) != self.cfg.get(k) for k in ("bt_drag_scale", "bt_scroll_speed"))
            self.cfg = cfg
            if resend and self.bt.connected_service:
                self._on_connect()
        if points:
            self.points = points
        self.portrait_coords = cfg.get("bt_coordinates") == "portrait"
        try:
            self.wheel_step = min(0.5, max(0.02, float(cfg.get("bt_wheel_step", 0.12))))
        except (TypeError, ValueError):
            pass
        self._send_screen()

    def _shown_points(self) -> tuple[float, float]:
        w, h = self.points
        if self.rot in (90, 270) and not self.portrait_coords:
            return h, w
        return w, h

    def _send_screen(self, force: bool = False) -> None:
        sp = self._shown_points()
        if force or sp != self.sent_screen:
            if self.bt.send(f"screen {sp[0]:.1f} {sp[1]:.1f}"):
                self.sent_screen = sp

    def _on_connect(self) -> None:
        self.sent_screen = None
        self._send_screen(force=True)
        cfg = self.cfg
        tune = {}
        if "bt_drag_scale" in cfg:
            tune["drag_scale"] = cfg["bt_drag_scale"]
        if "bt_scroll_speed" in cfg:
            tune["wheel_max"] = cfg["bt_scroll_speed"]
        if tune:
            self.bt.send("tune " + json.dumps(tune))

    def xy(self, nx: float, ny: float) -> tuple[float, float]:
        """Viewer fractions -> the fractions the phone expects."""
        if not self.portrait_coords or not self.rot:
            return nx, ny
        if self.rot == 90:
            return ny, 1 - nx
        if self.rot == 180:
            return 1 - nx, 1 - ny
        return 1 - ny, nx                    # 270

    def _cmd(self, verb: str, nx: float, ny: float, *extra) -> None:
        x, y = self.xy(nx, ny)
        tail = "".join(f" {v}" for v in extra)
        self.bt.send(f"{verb} {x:.5f} {y:.5f}{tail}")

    def _check(self) -> bool:
        if self.bt.ready:
            return True
        self.note(self.bt.status_line())
        return False

    # ---- pointer
    def hover(self, nx: float, ny: float) -> None:
        now = time.perf_counter()
        if self.down or now - self.last_hover[0] < self.HOVER_EVERY or self.last_hover[1] == (nx, ny):
            return
        if self.bt.ready:
            self.last_hover = (now, (nx, ny))
            self._cmd("hover", nx, ny)

    def press(self, nx: float, ny: float) -> None:
        if not self._check():
            return
        self.down = {"x": nx, "y": ny, "t0": time.perf_counter()}
        self._cmd("down", nx, ny)
        self.ripples.append((nx, ny, time.perf_counter()))

    def motion(self, nx: float, ny: float) -> None:
        if self.down:
            self._cmd("to", nx, ny)

    def release(self, nx: float, ny: float) -> None:
        if not self.down:
            return
        self.down = None
        self._cmd("to", nx, ny)
        self.bt.send("up")

    def right_click(self, nx: float, ny: float) -> None:
        if not self._check():
            return
        self._cmd("hold", nx, ny, 0.7)
        self.ripples.append((nx, ny, time.perf_counter()))

    def wheel_event(self, dx: float, dy: float, pos) -> None:
        if pos is None or not self.bt.ready:
            return
        W, H = self._shown_points()
        # wheel up = see what's above = the finger moves down; scroll right = finger moves left
        fdx, fdy = -dx * self.wheel_step * W, dy * self.wheel_step * H
        x, y = self.xy(*pos)
        if self.portrait_coords and self.rot:
            fdx, fdy = {90: (fdy, -fdx), 180: (-fdx, -fdy), 270: (-fdy, fdx)}[self.rot]
        self.bt.send(f"scroll {x:.5f} {y:.5f} {fdx:.1f} {fdy:.1f}")

    # ---- keyboard
    def type_text(self, s: str) -> None:
        if not self.text:
            self.text_t = time.perf_counter()
        self.text += s

    def key_down(self, pg_key: int, name: str, shift: bool = False) -> bool:
        """A non-text key (Backspace, arrows, ...). Returns True if it's one we send."""
        code = SPECIAL_KEYS.get(name)
        if code is None:
            return False
        self._flush_text()
        if self._check():
            self.held[pg_key] = code
            self.bt.send(f"kdown {code} {MOD_SHIFT if shift else 0}")
        return True

    def key_up(self, pg_key: int) -> None:
        code = self.held.pop(pg_key, None)
        if code is not None:
            self.bt.send(f"kup {code}")

    def command(self, letter: str, shift: bool = False) -> bool:
        """Ctrl+letter on the PC = Cmd+letter on the iPhone (select all, copy, undo...)."""
        code = LETTER.get(letter)
        if code is None:
            return False
        if self._check():
            self.bt.send(f"key {code} {MOD_CMD | (MOD_SHIFT if shift else 0)}")
        return True

    def key(self, k: str) -> None:
        if not self._check():
            return
        if k == "home":
            self.bt.send(f"key {LETTER['h']} {MOD_CMD}")          # Cmd+H
        elif k == "spotlight":
            self.bt.send(f"key 44 {MOD_CMD}")                    # Cmd+Space
        elif k == "switcher":
            self._swipe(0.5, 1.0, 0.5, 0.62, 0.45, 0.6)           # slow swipe up and hold
        elif k == "control":
            self._swipe(0.88, 0.0, 0.88, 0.45, 0.6, 0.0)
        elif k == "notifications":
            self._swipe(0.15, 0.0, 0.15, 0.7, 0.6, 0.0)
        elif k in ("volup", "voldown"):
            self.note("Volume keys don't work over Bluetooth control — use the phone's buttons")
        elif k == "siri":
            self.bt.send("consumer siri")
        elif k == "keyboard":
            self.bt.send("consumer eject")                         # show/hide the on-screen keyboard

    def _swipe(self, x0, y0, x1, y1, secs, hold) -> None:
        a = self.xy(x0, y0)
        b = self.xy(x1, y1)
        self.bt.send(f"swipe {a[0]:.4f} {a[1]:.4f} {b[0]:.4f} {b[1]:.4f} {secs} {hold}")

    def _flush_text(self) -> None:
        if self.text:
            s, self.text = self.text, ""
            if self._check():
                self.bt.send("text " + json.dumps(s))

    # ---- every loop
    def tick(self) -> None:
        now = time.perf_counter()
        if self.text and now - self.text_t > 0.03:
            self._flush_text()
        self.ripples = [r for r in self.ripples if now - r[2] < 0.35]
