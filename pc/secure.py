"""Links to the phone. Mirrors ios/MirrorLink/SecureChannel.swift exactly.

PlainLink  - USB (the cable is private; messages go as-is)
SecureLink - Wi-Fi: mutual proof of the pairing key, then ChaCha20-Poly1305
             records with per-direction counters (no replay/reorder/tamper).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import socket
import struct
import threading

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"MLK1"


class AuthError(Exception):
    """The phone didn't prove it holds our pairing key (or rejected ours)."""


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("phone closed the connection")
        buf += chunk
    return bytes(buf)


def _hkdf(key: bytes, salt: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(key)


def _nonce(counter: int) -> bytes:
    return b"\x00\x00\x00\x00" + struct.pack(">Q", counter)


class PlainLink:
    kind = "usb"

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._r = sock.makefile("rb", buffering=1 << 20)
        self._lock = threading.Lock()

    def read_exact(self, n: int) -> bytes:
        data = self._r.read(n)
        if data is None or len(data) < n:
            raise ConnectionError("phone closed the connection")
        return data

    def send(self, data: bytes) -> None:
        with self._lock:
            self.sock.sendall(data)

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class SecureLink(PlainLink):
    kind = "wifi"

    def __init__(self, sock: socket.socket, key: bytes, device_id: bytes):
        self.sock = sock
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._send_ctr = 0
        self._recv_ctr = 0

        nc = os.urandom(32)
        sock.sendall(MAGIC + device_id + nc)
        try:
            reply = _recv_exact(sock, 64)
        except ConnectionError as e:
            raise AuthError("phone rejected this PC's pairing") from e
        ns, proof = reply[:32], reply[32:]
        expected = hmac.new(key, b"ML-server" + nc + ns, hashlib.sha256).digest()
        if not hmac.compare_digest(proof, expected):
            raise AuthError("device failed to prove it's your paired iPhone")
        sock.sendall(hmac.new(key, b"ML-client" + nc + ns, hashlib.sha256).digest())

        salt = nc + ns
        self._tx = ChaCha20Poly1305(_hkdf(key, salt, b"ML c2s"))
        self._rx = ChaCha20Poly1305(_hkdf(key, salt, b"ML s2c"))
        self._r = sock.makefile("rb", buffering=1 << 20)

    def _read_record(self) -> None:
        hdr = self._r.read(4)
        if hdr is None or len(hdr) < 4:
            raise ConnectionError("phone closed the connection")
        (n,) = struct.unpack(">I", hdr)
        if n < 16 or n > (1 << 24):
            raise ConnectionError("bad record size")
        body = self._r.read(n)
        if body is None or len(body) < n:
            raise ConnectionError("phone closed the connection")
        # Raises InvalidTag if anything was modified, replayed or reordered.
        self._buf += self._rx.decrypt(_nonce(self._recv_ctr), body, None)
        self._recv_ctr += 1

    def read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._read_record()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def send(self, data: bytes) -> None:
        with self._lock:   # counter order must match wire order
            ct = self._tx.encrypt(_nonce(self._send_ctr), data, None)
            self._send_ctr += 1
            self.sock.sendall(struct.pack(">I", len(ct)) + ct)
