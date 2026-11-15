"""Tiny usbmux client: opens a TCP connection to a port on an iPhone over USB.

usbmux is Apple's USB multiplexing service. On Windows it's the
"Apple Mobile Device Service", installed by the Apple Devices app (Microsoft
Store) or iTunes, and it listens on 127.0.0.1:27015. On macOS/Linux it's the
unix socket /var/run/usbmuxd.

No third-party dependencies.
"""
from __future__ import annotations

import os
import plistlib
import socket
import struct
import sys

USBMUXD_TCP = ("127.0.0.1", 27015)
USBMUXD_UNIX = "/var/run/usbmuxd"

_MSG_PLIST = 8
_VERSION_PLIST = 1


class UsbmuxError(Exception):
    pass


def _open_mux() -> socket.socket:
    path = os.environ.get("USBMUXD_SOCKET_ADDRESS")
    if path:  # e.g. "127.0.0.1:27015" or "UNIX:/path"
        if path.startswith("UNIX:"):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(path[5:])
            return s
        host, port = path.rsplit(":", 1)
        return socket.create_connection((host, int(port)), timeout=3)

    if sys.platform == "win32":
        try:
            return socket.create_connection(USBMUXD_TCP, timeout=3)
        except OSError as e:
            raise UsbmuxError(
                "Can't reach Apple Mobile Device Service. Install iTunes (the version "
                "from apple.com, not the Microsoft Store) and plug in your iPhone."
            ) from e
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(USBMUXD_UNIX)
    return s


def _send(sock: socket.socket, payload: dict, tag: int = 1) -> None:
    body = plistlib.dumps(payload)
    sock.sendall(struct.pack("<IIII", 16 + len(body), _VERSION_PLIST, _MSG_PLIST, tag) + body)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise UsbmuxError("usbmux closed the connection")
        buf += chunk
    return bytes(buf)


def _recv(sock: socket.socket) -> dict:
    length, _version, _msgtype, _tag = struct.unpack("<IIII", _recv_exact(sock, 16))
    return plistlib.loads(_recv_exact(sock, length - 16))


def _base(msg_type: str) -> dict:
    return {
        "MessageType": msg_type,
        "ClientVersionString": "mirrorlink",
        "ProgName": "mirrorlink",
        "kLibUSBMuxVersion": 3,
    }


def list_devices() -> list[dict]:
    """Return attached devices: [{'DeviceID': int, 'SerialNumber': str, 'ConnectionType': 'USB'|'Network'}]."""
    with _open_mux() as s:
        _send(s, _base("ListDevices"))
        reply = _recv(s)
    out = []
    for d in reply.get("DeviceList", []):
        props = d.get("Properties", {})
        out.append({
            "DeviceID": d.get("DeviceID", props.get("DeviceID")),
            "SerialNumber": props.get("SerialNumber", "?"),
            "ConnectionType": props.get("ConnectionType", "?"),
        })
    return out


def connect(port: int, device_id: int | None = None, prefer_usb: bool = True) -> socket.socket:
    """Open a raw TCP stream to `port` on the phone. Returns a normal socket."""
    devices = list_devices()
    if not devices:
        raise UsbmuxError("No iPhone found. Plug it in with a cable, unlock it, and tap 'Trust'.")
    if device_id is None:
        if prefer_usb:
            devices.sort(key=lambda d: d["ConnectionType"] != "USB")
        device_id = devices[0]["DeviceID"]

    s = _open_mux()
    msg = _base("Connect")
    msg["DeviceID"] = device_id
    msg["PortNumber"] = socket.htons(port)   # usbmux wants network byte order
    _send(s, msg)
    reply = _recv(s)
    if reply.get("Number", -1) != 0:
        s.close()
        code = reply.get("Number")
        hint = " (is MirrorLink open on the phone?)" if code == 3 else ""
        raise UsbmuxError(f"Phone refused connection on port {port}: code {code}{hint}")
    s.settimeout(None)
    try:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass  # unix socket (macOS/Linux) has no Nagle to disable
    return s
