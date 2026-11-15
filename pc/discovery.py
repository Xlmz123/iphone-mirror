"""Finds your paired iPhone on the local network (Bonjour / mDNS)."""
from __future__ import annotations

import threading
import time

SERVICE = "_mirrorlink._tcp.local."


def find(device_id_hex: str, timeout: float = 3.0) -> list[tuple[str, int]]:
    """Return [(ip, port)] for the phone with this pairing ID, or [] if not seen."""
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError:
        return []

    found: list[tuple[str, int]] = []
    done = threading.Event()

    class Listener(ServiceListener):
        def _check(self, zc: "Zeroconf", type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name, timeout=1500)
            if not info:
                return
            props = {k.decode(): (v or b"").decode() for k, v in (info.properties or {}).items()}
            if props.get("id") != device_id_hex:
                return
            for ip in info.parsed_addresses():
                if ":" not in ip:            # prefer IPv4
                    found.append((ip, info.port))
            if found:
                done.set()

        def add_service(self, zc, type_, name):  # noqa: D401
            self._check(zc, type_, name)

        def update_service(self, zc, type_, name):
            self._check(zc, type_, name)

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, SERVICE, Listener())
        done.wait(timeout)
        time.sleep(0.05)
    finally:
        zc.close()
    return found
