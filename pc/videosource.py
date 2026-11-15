"""Where the picture comes from.

  * Cable capture (qtcapture.py): the iPhone's own screen sharing over USB — no app on the
    phone, no red recording dot. Needs the USB capture driver; cable only.
  * The MirrorLink app (mirror.Receiver): USB or Wi-Fi; iOS shows the red dot while it records.

VideoSource runs both and shows whichever has a picture, preferring the cable. It looks
like a Receiver to the rest of the viewer.
"""
from __future__ import annotations

import json
import threading
import time

import av

import qtcapture
from pairing import _dir as config_dir

POINTS_FILE = config_dir() / "phone.json"


def save_points(points) -> None:
    try:
        POINTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        POINTS_FILE.write_text(json.dumps({"points": list(points)}), encoding="utf-8")
    except OSError:
        pass


def load_points():
    try:
        p = json.loads(POINTS_FILE.read_text(encoding="utf-8")).get("points")
        if isinstance(p, list) and len(p) == 2:
            return float(p[0]), float(p[1])
    except (OSError, ValueError):
        pass
    return None


class CableReceiver(threading.Thread):
    """Cable capture with the same face as mirror.Receiver."""

    def __init__(self, stats, display_size: int = 2000, log_path=None) -> None:
        super().__init__(daemon=True, name="cable-capture")
        self.stats = stats
        self.display_size = display_size
        self.status = ""
        self.problem: str | None = None          # why cable capture isn't available
        self.device_name = ""
        self.size: tuple[int, int] | None = None
        self.link_kind: str | None = None        # "cable" while pictures arrive
        self.mode = "H.264 (cable)"
        self.orientation_raw = None
        self.control_ready = False
        self.connected_at = 0.0
        self.kicked = False
        self.resume = threading.Event()
        self.pairing = None
        self.status_note = ""
        self.note_until = 0.0
        self.points = None
        self.count = {"key": 0, "delta": 0, "jpeg": 0, "frames": 0, "errors": 0}
        self.new_frame = threading.Event()
        self._latest = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._log_path = log_path
        self._codec = None
        self._codec_name = None
        self._last_frame_t = 0.0

    # ---- Receiver interface
    @property
    def connected(self) -> bool:
        return self.link_kind is not None

    def latest(self):
        with self._lock:
            f, self._latest = self._latest, None
        return f

    def send_input(self, event) -> bool:
        return False

    def stop(self) -> None:
        self._stop.set()

    # ---- internals
    def log(self, msg: str) -> None:
        if self._log_path:
            try:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(time.strftime("%H:%M:%S ") + msg + "\n")
            except OSError:
                pass

    def _on_video(self, annexb: bytes, codec: str, w: int, h: int, key: bool) -> None:
        if codec != self._codec_name:
            self._codec = av.CodecContext.create(codec, "r")
            self._codec.options = {"flags": "low_delay"}
            self._codec.thread_type = "SLICE"
            self._codec_name = codec
        self.count["key" if key else "delta"] += 1
        t0 = time.perf_counter()
        try:
            frames = self._codec.decode(av.Packet(annexb))
        except av.error.FFmpegError:
            self.count["errors"] += 1
            return
        dt = (time.perf_counter() - t0) * 1000
        with self.stats.lock:
            self.stats.rx_frames += 1
            self.stats.rx_bytes += len(annexb)
            self.stats.decode_ms = self.stats.decode_ms * 0.9 + dt * 0.1
        if frames:
            f = frames[-1]
            self.size = (f.width, f.height)            # follows the phone's rotation by itself
            self.count["frames"] += len(frames)
            self._last_frame_t = time.monotonic()
            if self.link_kind is None:
                self.link_kind = "cable"
                self.connected_at = time.time()
            with self._lock:
                self._latest = f
            self.new_frame.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                cap = qtcapture.UsbCapture(log=self.log)
            except qtcapture.DriverMissing as e:
                self.problem = str(e)
                self._stop.wait(10)
                continue
            except ImportError:
                self.problem = "pyusb isn't installed (run run.bat once)"
                self._stop.wait(30)
                continue
            try:
                self.status = "Starting cable capture…"
                self.device_name = cap.open()
                self.problem = None
                self.log(f"cable capture opened ({self.device_name})")
                self._session(cap)
            except LookupError:
                self.status = ""
                self._stop.wait(1.5)
            except qtcapture.DriverMissing as e:
                self.problem = str(e)
                self.log(f"driver: {e}")
                self._stop.wait(10)
            except Exception as e:            # unplugged, phone said no, USB hiccup...
                self.log(f"cable capture ended: {e!r}")
                self.problem = f"cable capture stopped ({e.__class__.__name__}); retrying"
                self._stop.wait(2)
            finally:
                self.link_kind = None
                self._codec_name = None
                try:
                    cap.close()
                except Exception:
                    pass

    def _session(self, cap) -> None:
        import usb.core
        session = qtcapture.Session(cap.write, self._on_video, log=self.log,
                                    display_size=(self.display_size, self.display_size))
        buf = bytearray()
        started = time.monotonic()
        last_data = time.monotonic()
        while not self._stop.is_set():
            try:
                chunk = cap.read()
            except usb.core.USBTimeoutError:
                chunk = b""
            if chunk:
                last_data = time.monotonic()
                buf += chunk
                for p in qtcapture.split_packets(buf):
                    session.handle(p)
            elif time.monotonic() - last_data > 6:
                raise TimeoutError("the iPhone stopped sending" if session.frames else
                                   "the iPhone didn't start screen sharing (is it unlocked?)")
            if session.frames == 0 and time.monotonic() - started > 15:
                raise TimeoutError("no picture from the iPhone")
        # Polite goodbye so the phone stops streaming and frees its clocks.
        try:
            session.goodbye()
            end = time.monotonic() + 1.5
            while time.monotonic() < end and not session.released.is_set():
                try:
                    chunk = cap.read(timeout=300)
                except usb.core.USBTimeoutError:
                    continue
                buf += chunk
                for p in qtcapture.split_packets(buf):
                    session.handle(p)
        except Exception:
            pass


class VideoSource:
    """Cable capture when it has a picture, otherwise the MirrorLink app's stream."""

    def __init__(self, app_receiver, cable: CableReceiver | None) -> None:
        self.app = app_receiver
        self.cable = cable
        self.new_frame = threading.Event()
        app_receiver.new_frame = self.new_frame
        if cable is not None:
            cable.new_frame = self.new_frame
        self._saved_points = load_points()

    @property
    def active(self):
        if self.cable is not None and self.cable.link_kind is not None:
            return self.cable
        return self.app

    @property
    def using_cable(self) -> bool:
        return self.active is self.cable

    def start(self) -> None:
        self.app.start()
        if self.cable is not None:
            self.cable.start()

    def stop(self) -> None:
        self.app.stop()
        if self.cable is not None:
            self.cable.stop()
            self.cable.join(timeout=2.5)       # let it hand the phone back its normal USB mode

    def latest(self):
        src = self.active
        other = self.app if src is self.cable else self.cable
        if other is not None:
            other.latest()                     # drop the inactive source's frames
        return src.latest()

    # The app stream isn't needed while the cable works (saves the PC decoding twice).
    def sync_suspend(self) -> None:
        self.app.suspended = self.using_cable

    @property
    def points(self):
        if self.app.points:
            if self.app.points != self._saved_points:
                self._saved_points = self.app.points
                save_points(self.app.points)
        if not self.using_cable:
            return self.app.points
        base = self.app.points or self._saved_points
        size = self.cable.size
        if base is None or size is None:
            return None
        pw, ph = min(base), max(base)
        return (ph, pw) if size[0] > size[1] else (pw, ph)

    @property
    def status(self) -> str:
        if self.cable is not None and not self.app.connected and self.cable.status and not self.cable.problem:
            return self.cable.status
        return self.active.status

    @property
    def orientation_raw(self):
        return None if self.using_cable else self.app.orientation_raw

    def __getattr__(self, name):
        # status, size, link_kind, device_name, mode, control_ready, connected, kicked, ...
        return getattr(self.active, name)
