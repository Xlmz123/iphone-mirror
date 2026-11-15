"""Cable capture: the iPhone's own screen sharing over USB (what QuickTime uses on a Mac).

Why: an app that records the screen makes iOS show the red recording dot. This doesn't:
it's the system feature Apple uses for QuickTime's "New Movie Recording → iPhone", so no
app runs on the phone and there's no red dot. It only works over the cable.

How it works (reverse engineered by Daniel Paulus, quicktime_video_hack, MIT License,
https://github.com/danielpaulus/quicktime_video_hack — the protocol notes below follow his
documentation; this is a separate Python implementation):
  1. A vendor control request (0x40, 0x52, 0, 2) makes the iPhone re-appear on USB with an
     extra "QuickTime" configuration (an interface with class 0xFF, subclass 0x2A).
  2. We switch to that configuration, claim the interface and talk over its two bulk
     endpoints: packets are [u32 length][u32 magic]..., all little-endian.
  3. The phone pings, we ping back, then a short clock handshake (SYNC packets we must
     answer: CWPA, AFMT, CVRP, CLOK, TIME, SKEW, OG, STOP). After that it sends ASYN FEED
     packets: CMSampleBuffers holding H.264 in AVCC form. We answer each with NEED.
  4. To stop: HPA0 + HPD0, then the "disable" control request and the normal configuration.

Windows needs a USB filter driver on the iPhone for step 1 (Windows' own WinUSB can't change
configurations): libusb-win32's libusb0 *UpperFilter* on the iPhone's USB device, installed
e.g. by iPhoneMirror's driver manager. We use it through pyusb's libusb0 backend.
"""
from __future__ import annotations

import glob
import os
import struct
import sys
import threading
import time

APPLE_VID = 0x05AC
QT_SUBCLASS = 0x2A
MUX_SUBCLASS = 0xFE


def magic(s: str) -> int:
    """The u32 value whose little-endian bytes are `s` reversed (how the protocol stores tags)."""
    return int.from_bytes(s.encode("ascii"), "big")


PING, SYNC, ASYN, RPLY = magic("ping"), magic("sync"), magic("asyn"), magic("rply")
CWPA, AFMT, CVRP, CLOK, TIME, SKEW, OG, STOP = (magic(x) for x in
                                               ("cwpa", "afmt", "cvrp", "clok", "time", "skew", "go! ", "stop"))
FEED, EAT, SPRP, TJMP, SRAT, TBAS, RELS = (magic(x) for x in ("feed", "eat!", "sprp", "tjmp", "srat", "tbas", "rels"))
HPD1, HPA1, NEED, HPD0, HPA0 = (magic(x) for x in ("hpd1", "hpa1", "need", "hpd0", "hpa0"))
# serialization tags
KEYV, STRK, IDXK = magic("keyv"), magic("strk"), magic("idxk")
BULV, DICT, DATV, STRV, NMBV, FDSC = (magic(x) for x in ("bulv", "dict", "datv", "strv", "nmbv", "fdsc"))
SBUF, OPTS, STIA, SDAT, SATT, SARY, SSIZ, NSMP = (magic(x) for x in
                                                  ("sbuf", "opts", "stia", "sdat", "satt", "sary", "ssiz", "nsmp"))
MDIA, VDIM, CODC, EXTN, ASBD = (magic(x) for x in ("mdia", "vdim", "codc", "extn", "asbd"))
VIDE, SOUN = magic("vide"), magic("soun")
AVC1, HVC1 = magic("avc1"), magic("hvc1")
EMPTY_CLOCK = 1

u32 = struct.Struct("<I")
u64 = struct.Struct("<Q")


def U32(b, o=0) -> int:
    return u32.unpack_from(b, o)[0]


def U64(b, o=0) -> int:
    return u64.unpack_from(b, o)[0]


# ---------------------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------------------
class Num:
    """The protocol's NSNumber: type 3 = int32, 4 = int64, 5 = int32, 6 = float64."""

    def __init__(self, kind: int, value) -> None:
        self.kind, self.value = kind, value

    @classmethod
    def parse(cls, b: bytes) -> "Num":
        k = b[0]
        if k == 6:
            return cls(6, struct.unpack_from("<d", b, 1)[0])
        if k == 4:
            return cls(4, U64(b, 1))
        if k in (3, 5):
            return cls(k, U32(b, 1))
        raise ValueError(f"unknown NSNumber type {k}")

    def to_bytes(self) -> bytes:
        if self.kind == 6:
            return bytes([6]) + struct.pack("<d", float(self.value))
        if self.kind == 4:
            return bytes([4]) + u64.pack(int(self.value))
        return bytes([self.kind]) + u32.pack(int(self.value))

    def __eq__(self, other):
        return isinstance(other, Num) and (self.kind, self.value) == (other.kind, other.value)

    def __repr__(self):
        return f"Num({self.kind}, {self.value})"


def length_and_magic(b: bytes, expect: int | None = None) -> tuple[int, int]:
    if len(b) < 8:
        raise ValueError("truncated element")
    n, m = U32(b), U32(b, 4)
    if expect is not None and m != expect:
        raise ValueError(f"expected {expect:08x}, got {m:08x}")
    if n < 8 or n > len(b):
        raise ValueError(f"bad element length {n} (have {len(b)})")
    return n, m


def parse_value(b: bytes):
    n, m = length_and_magic(b)
    body = b[8:n]
    if m == STRV:
        return body.decode("utf-8", "replace")
    if m == DATV:
        return bytes(body)
    if m == BULV:
        return bool(body[:1] == b"\x01")
    if m == NMBV:
        return Num.parse(body)
    if m == DICT:
        return parse_dict(b[:n])
    if m == FDSC:
        return parse_fdsc(b[:n])
    return ("unknown", m, bytes(body))


def parse_dict(b: bytes, marker: int = DICT) -> dict:
    """A dictionary with string keys (strk) or index keys (idxk) — both become a dict."""
    n, _ = length_and_magic(b, marker)
    out: dict = {}
    rest = b[8:n]
    while rest:
        kvlen, _ = length_and_magic(rest, KEYV)
        kv = rest[8:kvlen]
        klen, kmagic = length_and_magic(kv)
        if kmagic == STRK:
            key = kv[8:klen].decode("utf-8", "replace")
        elif kmagic == IDXK:
            key = struct.unpack_from("<H", kv, 8)[0]
        else:
            raise ValueError(f"unknown key type {kmagic:08x}")
        out[key] = parse_value(kv[klen:])
        rest = rest[kvlen:]
    return out


def _elem(tag: int, body: bytes) -> bytes:
    return u32.pack(len(body) + 8) + u32.pack(tag) + body


def serialize_value(v) -> bytes:
    if isinstance(v, bool):
        return _elem(BULV, b"\x01" if v else b"\x00")
    if isinstance(v, Num):
        return _elem(NMBV, v.to_bytes())
    if isinstance(v, str):
        return _elem(STRV, v.encode())
    if isinstance(v, (bytes, bytearray)):
        return _elem(DATV, bytes(v))
    if isinstance(v, list):                       # list of (key, value): a nested string-key dict
        return serialize_dict(v)
    raise TypeError(f"can't serialize {type(v)}")


def serialize_dict(entries: list) -> bytes:
    """String-key dictionary from a list of (key, value) pairs (order matters to the phone)."""
    body = b""
    for k, v in entries:
        body += _elem(KEYV, _elem(STRK, k.encode()) + serialize_value(v))
    return _elem(DICT, body)


def parse_fdsc(b: bytes) -> dict:
    """CMFormatDescription: media type, size, codec and the extensions (with SPS/PPS)."""
    n, _ = length_and_magic(b, FDSC)
    rest = b[8:n]
    out: dict = {}
    while rest:
        ln, m = length_and_magic(rest)
        el = rest[:ln]
        if m == MDIA:
            out["media"] = U32(el, 8)
        elif m == VDIM:
            out["width"], out["height"] = U32(el, 8), U32(el, 12)
        elif m == CODC:
            out["codec"] = U32(el, 8)
        elif m == EXTN:
            out["extensions"] = parse_dict(el, EXTN)
        rest = rest[ln:]
    return out


def cmtime(value: int, scale: int = 1_000_000_000, flags: int = 1, epoch: int = 0) -> bytes:
    return struct.pack("<QIIQ", value & 0xFFFFFFFFFFFFFFFF, scale, flags, epoch)


def parse_cmtime(b: bytes, o: int = 0) -> tuple[int, int]:
    value, scale, _flags, _epoch = struct.unpack_from("<QIIQ", b, o)
    return value, scale


def parse_sbuf(b: bytes) -> dict:
    """CMSampleBuffer: sample data (H.264 AVCC), presentation time, optional new format."""
    n, _ = length_and_magic(b, SBUF)
    rest = b[8:n]
    out: dict = {"data": b"", "fdsc": None, "pts": None}
    while rest:
        ln, m = length_and_magic(rest)
        if m == SDAT:
            out["data"] = bytes(rest[8:ln])
        elif m == OPTS:
            out["pts"] = parse_cmtime(rest, 8)
        elif m == FDSC:
            out["fdsc"] = parse_fdsc(rest[:ln])
        rest = rest[ln:]
    return out


# ---------------------------------------------------------------------------------------
# H.264 / HEVC: AVCC samples -> Annex B for the decoder
# ---------------------------------------------------------------------------------------
START = b"\x00\x00\x00\x01"


def parameter_sets(fdsc: dict) -> tuple[str, bytes, int]:
    """(codec name for FFmpeg, Annex B parameter sets, NAL length size) from a format description."""
    codec = fdsc.get("codec")
    name = "hevc" if codec == HVC1 else "h264"
    blob = None
    ext = fdsc.get("extensions") or {}
    atoms = ext.get(49)                               # SampleDescriptionExtensionAtoms
    if isinstance(atoms, dict):
        blob = atoms.get(105)                         # 'avcC' (H.264)
        if not isinstance(blob, bytes):
            blob = next((v for v in atoms.values() if isinstance(v, bytes) and len(v) > 6), None)
    if not blob:
        return name, b"", 4
    try:
        if name == "h264":
            nal_len = (blob[4] & 3) + 1
            out, i = b"", 6
            for _ in range(blob[5] & 0x1F):           # SPS
                ln = int.from_bytes(blob[i:i + 2], "big")
                out += START + blob[i + 2:i + 2 + ln]
                i += 2 + ln
            for _ in range(blob[i]):                  # PPS
                ln = int.from_bytes(blob[i + 1:i + 3], "big")
                out += START + blob[i + 3:i + 3 + ln]
                i += 2 + ln
            return name, out, nal_len
        nal_len = (blob[21] & 3) + 1                  # hvcC
        out, i = b"", 23
        for _ in range(blob[22]):
            count = int.from_bytes(blob[i + 1:i + 3], "big")
            i += 3
            for _ in range(count):
                ln = int.from_bytes(blob[i:i + 2], "big")
                out += START + blob[i + 2:i + 2 + ln]
                i += 2 + ln
        return name, out, nal_len
    except IndexError:
        return name, b"", 4


def avcc_to_annexb(data: bytes, nal_len: int = 4, hevc: bool = False) -> tuple[bytes, bool]:
    """Returns (Annex B bytes, contains a keyframe)."""
    out = bytearray()
    key = False
    i = 0
    while i + nal_len <= len(data):
        ln = int.from_bytes(data[i:i + nal_len], "big")
        i += nal_len
        nal = data[i:i + ln]
        i += ln
        if not nal:
            continue
        if hevc:
            key = key or 16 <= (nal[0] >> 1) & 0x3F <= 21      # IRAP pictures
        else:
            key = key or (nal[0] & 0x1F) == 5                  # IDR
        out += START + nal
    return bytes(out), key


# ---------------------------------------------------------------------------------------
# Packets we send
# ---------------------------------------------------------------------------------------
def ping_packet() -> bytes:
    return u32.pack(16) + u32.pack(PING) + u64.pack(0x0000000100000000)


def reply_clock(corr: int, clock_ref: int) -> bytes:
    return u32.pack(28) + u32.pack(RPLY) + u64.pack(corr) + u32.pack(0) + u64.pack(clock_ref)


def reply_time(corr: int, ns: int) -> bytes:
    return u32.pack(44) + u32.pack(RPLY) + u64.pack(corr) + u32.pack(0) + cmtime(ns)


def reply_afmt(corr: int) -> bytes:
    d = serialize_dict([("Error", Num(3, 0))])
    return u32.pack(20 + len(d)) + u32.pack(RPLY) + u64.pack(corr) + u32.pack(0) + d


def reply_skew(corr: int, skew: float) -> bytes:
    return u32.pack(28) + u32.pack(RPLY) + u64.pack(corr) + u32.pack(0) + struct.pack("<d", skew)


def reply_zero(corr: int) -> bytes:                     # OG and STOP
    return u32.pack(24) + u32.pack(RPLY) + u64.pack(corr) + u64.pack(0)


def asyn(clock_ref: int, tag: int, payload: bytes = b"") -> bytes:
    return u32.pack(20 + len(payload)) + u32.pack(ASYN) + u64.pack(clock_ref) + u32.pack(tag) + payload


def hpd1_packet(width: int = 1920, height: int = 1200) -> bytes:
    """Tells the phone we're a display: its size decides the picture size the phone sends."""
    d = serialize_dict([
        ("Valeria", True),
        ("HEVCDecoderSupports444", True),
        ("DisplaySize", [("Width", Num(6, float(width))), ("Height", Num(6, float(height)))]),
    ])
    return asyn(EMPTY_CLOCK, HPD1, d)


def asbd_default() -> bytes:
    """AudioStreamBasicDescription: 48 kHz, LPCM, 16-bit stereo (+ two sample-rate doubles)."""
    return (struct.pack("<d", 48000.0) + u32.pack(magic("lpcm")) +
            struct.pack("<IIIIIII", 12, 4, 1, 4, 2, 16, 0) + struct.pack("<dd", 48000.0, 48000.0))


def hpa1_packet(device_audio_clock: int) -> bytes:
    d = serialize_dict([
        ("BufferAheadInterval", Num(6, 0.07300000000000001)),
        ("deviceUID", "Valeria"),
        ("ScreenLatency", Num(6, 0.04)),
        ("formats", asbd_default()),
        ("EDIDAC3Support", Num(3, 0)),
        ("deviceName", "Valeria"),
    ])
    return asyn(device_audio_clock, HPA1, d)


# ---------------------------------------------------------------------------------------
# The conversation with the phone (no USB here: testable with recorded packets)
# ---------------------------------------------------------------------------------------
class Clock:
    def __init__(self, ref: int) -> None:
        self.ref = ref
        self.start = time.monotonic_ns()

    def now_ns(self) -> int:
        return time.monotonic_ns() - self.start


class Session:
    """Answers the phone's packets. `write(bytes)` sends; `on_video(annexb, codec, w, h, key)`
    receives each picture."""

    def __init__(self, write, on_video, log=print, display_size=(1920, 1200)) -> None:
        self.write = write
        self.on_video = on_video
        self.log = log
        self.display_size = display_size
        self.need = None
        self.clock: Clock | None = None
        self.audio_clock: Clock | None = None
        self.device_audio_ref = None
        self.codec = "h264"
        self.params = b""
        self.nal_len = 4
        self.size = (0, 0)
        self.sent_params = False
        self.released = threading.Event()
        self.audio_first = None      # (device ns, local ns) at the first audio sample
        self.audio_last = None
        self.frames = 0

    # -- format
    def _format(self, fdsc: dict | None) -> None:
        if not fdsc or fdsc.get("media", VIDE) != VIDE:
            return
        codec, params, nal_len = parameter_sets(fdsc)
        size = (fdsc.get("width", self.size[0]), fdsc.get("height", self.size[1]))
        if params and (params != self.params or codec != self.codec or size != self.size):
            self.log(f"video format: {codec} {size[0]}x{size[1]}")
            self.codec, self.params, self.nal_len, self.size = codec, params, nal_len, size
            self.sent_params = False

    # -- packets
    def handle(self, p: bytes) -> None:
        """One packet without its 4-byte length."""
        kind = U32(p)
        if kind == PING:
            self.write(ping_packet())
        elif kind == SYNC:
            self._sync(p)
        elif kind == ASYN:
            self._asyn(p)
        else:
            self.log(f"unknown packet {p[:4]!r}")

    def _sync(self, p: bytes) -> None:
        clock_ref, sub, corr = U64(p, 4), U32(p, 12), U64(p, 16)
        rest = p[24:]
        if sub == CWPA:
            self.device_audio_ref = U64(rest)
            self.audio_clock = Clock(self.device_audio_ref + 1000)
            hpd1 = hpd1_packet(*self.display_size)
            self.write(hpd1)
            self.write(hpd1)
            self.write(reply_clock(corr, self.audio_clock.ref))
            self.write(hpa1_packet(self.device_audio_ref))
        elif sub == CVRP:
            device_video_ref = U64(rest)
            try:
                payload = parse_dict(rest[8:])
                self._format(payload.get("FormatDescription"))
            except (ValueError, struct.error) as e:
                self.log(f"CVRP payload: {e}")
            self.need = asyn(device_video_ref, NEED)
            self.write(self.need)
            self.write(reply_clock(corr, device_video_ref + 0x1000AF))
        elif sub == CLOK:
            self.clock = Clock(clock_ref + 0x10000)
            self.write(reply_clock(corr, self.clock.ref))
        elif sub == TIME:
            c = self.clock or Clock(0)
            self.write(reply_time(corr, c.now_ns()))
        elif sub == AFMT:
            self.write(reply_afmt(corr))
        elif sub == SKEW:
            self.write(reply_skew(corr, self._skew()))
        elif sub == OG:
            self.write(reply_zero(corr))
        elif sub == STOP:
            self.write(reply_zero(corr))
        else:
            self.log(f"unknown SYNC {p[12:16]!r}; answering with zeros")
            self.write(reply_zero(corr))

    def _skew(self) -> float:
        """How many of our audio-clock ticks per 48 kHz device tick (48000 = in step)."""
        if not self.audio_first or not self.audio_last:
            return 48000.0
        (d0, l0), (d1, l1) = self.audio_first, self.audio_last
        if d1 <= d0 or l1 <= l0:
            return 48000.0
        return 48000.0 * (l1 - l0) / (d1 - d0)

    def _asyn(self, p: bytes) -> None:
        sub = U32(p, 12)
        if sub == FEED:
            try:
                sb = parse_sbuf(p[16:])
            except (ValueError, struct.error) as e:
                self.log(f"bad FEED: {e}")
                if self.need:
                    self.write(self.need)
                return
            self._format(sb["fdsc"])
            if sb["data"] and self.params:
                annexb, key = avcc_to_annexb(sb["data"], self.nal_len, self.codec == "hevc")
                if key or not self.sent_params:
                    annexb = self.params + annexb      # decoders need SPS/PPS before keyframes
                    self.sent_params = True
                self.frames += 1
                self.on_video(annexb, self.codec, self.size[0], self.size[1], key)
            if self.need:
                self.write(self.need)
        elif sub == EAT:
            try:
                sb = parse_sbuf(p[16:])
                if sb["pts"] and self.audio_clock:
                    v, scale = sb["pts"]
                    dev_ns = v * 1_000_000_000 // max(1, scale)
                    pair = (dev_ns, self.audio_clock.now_ns())
                    if self.audio_first is None:
                        self.audio_first = pair
                    self.audio_last = pair
            except (ValueError, struct.error):
                pass
        elif sub == RELS:
            self.released.set()
        # SPRP, TJMP, SRAT, TBAS: information only

    def goodbye(self) -> None:
        """Ask the phone to stop streaming (it answers with STOP / RELS)."""
        if self.device_audio_ref is not None:
            self.write(asyn(self.device_audio_ref, HPA0))
        self.write(asyn(EMPTY_CLOCK, HPD0))


def split_packets(buf: bytearray) -> list[bytes]:
    """Take whole packets ([u32 length incl. itself][...]) off the front of `buf`."""
    out = []
    while len(buf) >= 4:
        n = U32(buf)
        if n < 8:
            raise ValueError(f"bad packet length {n}")
        if len(buf) < n:
            break
        out.append(bytes(buf[4:n]))
        del buf[:n]
    return out


# ---------------------------------------------------------------------------------------
# USB (pyusb + libusb0 filter driver on Windows)
# ---------------------------------------------------------------------------------------
class DriverMissing(Exception):
    pass


def _backend():
    import usb.backend.libusb0 as lb0
    import usb.backend.libusb1 as lb1
    candidates = [None]   # default search (System32 on Windows, where the filter installs libusb0.dll)
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", ""),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            if base:
                candidates += glob.glob(os.path.join(base, "iPhoneMirror", "**", "libusb0.dll"), recursive=True)
        candidates.append(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "libusb0.dll"))
    for c in candidates:
        try:
            be = lb0.get_backend(find_library=(lambda _x, c=c: c) if c else None)
            if be is not None:
                return be
        except Exception:
            continue
    if sys.platform != "win32":
        be = lb1.get_backend()
        if be is not None:
            return be
    raise DriverMissing("the USB capture driver (libusb0) isn't installed")


def _configs(dev):
    try:
        return list(dev)
    except Exception:
        return []


def _find_iface(cfg, subclass: int):
    for intf in cfg:
        if intf.bInterfaceClass == 0xFF and intf.bInterfaceSubClass == subclass:
            return intf
    return None


def _apple_devices(backend):
    import usb.core
    devs = list(usb.core.find(find_all=True, idVendor=APPLE_VID, backend=backend) or [])
    # iPhones/iPads: they have the usbmux interface
    return [d for d in devs if any(_find_iface(c, MUX_SUBCLASS) or _find_iface(c, QT_SUBCLASS) for c in _configs(d))]


class UsbCapture:
    """One capture session on one iPhone."""

    def __init__(self, log=print) -> None:
        self.log = log
        self.backend = _backend()
        self.dev = None
        self.ep_in = self.ep_out = None
        self.iface = None
        self.mux_config = None
        self.serial = None

    def open(self, timeout: float = 10.0) -> str:
        """Find the iPhone and switch it to the QuickTime configuration. Returns its name."""
        import usb.core
        import usb.util
        devs = _apple_devices(self.backend)
        if not devs:
            raise LookupError("no iPhone on USB")
        dev = devs[0]
        try:
            self.serial = usb.util.get_string(dev, dev.iSerialNumber)
        except Exception:
            self.serial = None
        try:
            name = usb.util.get_string(dev, dev.iProduct) or "iPhone"
        except Exception:
            name = "iPhone"
        for c in _configs(dev):
            if _find_iface(c, MUX_SUBCLASS) and not _find_iface(c, QT_SUBCLASS):
                self.mux_config = c.bConfigurationValue
        if not any(_find_iface(c, QT_SUBCLASS) for c in _configs(dev)):
            self.log("enabling the iPhone's screen-sharing configuration")
            try:
                dev.ctrl_transfer(0x40, 0x52, 0x00, 0x02, 0, timeout=2000)
            except usb.core.USBError as e:
                self.log(f"(enable request: {e} — usually fine)")
            usb.util.dispose_resources(dev)
            deadline = time.monotonic() + timeout
            dev = None
            while time.monotonic() < deadline:          # it disconnects and comes back
                time.sleep(0.5)
                for d in _apple_devices(self.backend):
                    try:
                        s = usb.util.get_string(d, d.iSerialNumber)
                    except Exception:
                        s = None
                    if (self.serial is None or s == self.serial) and any(_find_iface(c, QT_SUBCLASS) for c in _configs(d)):
                        dev = d
                        break
                if dev is not None:
                    break
            if dev is None:
                raise TimeoutError("the iPhone didn't come back with its screen-sharing configuration")
        self.dev = dev
        qt_cfg = next(c for c in _configs(dev) if _find_iface(c, QT_SUBCLASS))
        try:
            dev.set_configuration(qt_cfg.bConfigurationValue)
        except usb.core.USBError as e:
            raise DriverMissing(f"couldn't switch the iPhone's USB configuration ({e}) — is the capture driver installed?")
        cfg = dev.get_active_configuration()
        intf = _find_iface(cfg, QT_SUBCLASS)
        if intf is None:
            raise RuntimeError("screen-sharing interface missing after switching")
        self.iface = intf.bInterfaceNumber
        usb.util.claim_interface(dev, self.iface)
        for ep in intf:
            if usb.util.endpoint_type(ep.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
                continue
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                self.ep_in = ep.bEndpointAddress
            else:
                self.ep_out = ep.bEndpointAddress
        if self.ep_in is None or self.ep_out is None:
            raise RuntimeError("screen-sharing endpoints not found")
        for ep in (self.ep_in, self.ep_out):
            try:
                dev.clear_halt(ep)
            except usb.core.USBError:
                pass
        return name

    def write(self, data: bytes) -> None:
        self.dev.write(self.ep_out, data, timeout=2000)

    def read(self, size: int = 512 * 1024, timeout: int = 1000) -> bytes:
        return bytes(self.dev.read(self.ep_in, size, timeout=timeout))

    def close(self) -> None:
        import usb.util
        dev = self.dev
        if dev is None:
            return
        try:
            usb.util.release_interface(dev, self.iface)
        except Exception:
            pass
        try:
            dev.ctrl_transfer(0x40, 0x52, 0x00, 0x00, 0, timeout=1000)     # disable screen sharing
        except Exception:
            pass
        try:
            if self.mux_config:
                dev.set_configuration(self.mux_config)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass
        self.dev = None


def available() -> tuple[bool, str]:
    """Can cable capture work on this PC right now? (pyusb + driver + an iPhone on USB)"""
    try:
        import usb.core  # noqa: F401
    except ImportError:
        return False, "pyusb isn't installed (run run.bat once)"
    try:
        be = _backend()
    except DriverMissing as e:
        return False, str(e)
    try:
        return (bool(_apple_devices(be)), "")
    except Exception as e:
        return False, f"USB error: {e}"
