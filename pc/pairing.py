"""Stores the phone's pairing ID + key on this PC (learned once over USB)."""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path


def _dir() -> Path:
    if os.environ.get("MIRRORLINK_HOME"):
        return Path(os.environ["MIRRORLINK_HOME"])
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "MirrorLink"
    return Path.home() / ".config" / "mirrorlink"


PATH = _dir() / "pairing.json"


class Pairing:
    def __init__(self, id_hex: str, key: bytes, name: str = "iPhone", last_host: str | None = None):
        self.id_hex = id_hex
        self.key = key
        self.name = name
        self.last_host = last_host

    @property
    def id_bytes(self) -> bytes:
        return bytes.fromhex(self.id_hex)

    def save(self) -> None:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "id": self.id_hex,
            "key": base64.b64encode(self.key).decode(),
            "name": self.name,
            "last_host": self.last_host,
        }, indent=2))
        tmp.replace(PATH)
        if sys.platform != "win32":
            os.chmod(PATH, 0o600)


def load() -> Pairing | None:
    try:
        d = json.loads(PATH.read_text())
        key = base64.b64decode(d["key"])
        if len(key) != 32 or len(bytes.fromhex(d["id"])) != 16:
            return None
        return Pairing(d["id"], key, d.get("name", "iPhone"), d.get("last_host"))
    except (OSError, ValueError, KeyError):
        return None


def forget() -> None:
    try:
        PATH.unlink()
    except FileNotFoundError:
        pass
