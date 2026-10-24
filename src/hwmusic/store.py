"""Persistent settings: our identity towards the watch and pairing tokens."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .crypto import random_bytes


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / "HuaweiMusic"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "HuaweiMusic"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hwmusic"


@dataclass
class Settings:
    device_id: str = ""
    tokens: dict[str, str] = field(default_factory=dict)
    last_address: str = ""
    last_name: str = ""
    fast_mode: bool = False
    debug: bool = False

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        path = path or config_dir() / "settings.json"
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                data = {}
        settings = cls(
            device_id=data.get("device_id") or "",
            tokens=dict(data.get("tokens") or {}),
            last_address=data.get("last_address") or "",
            last_name=data.get("last_name") or "",
            fast_mode=bool(data.get("fast_mode")),
            debug=bool(data.get("debug")),
        )
        if not settings.device_id:
            settings.device_id = random_bytes(16).hex().upper()
        return settings

    def save(self, path: Path | None = None) -> None:
        path = path or config_dir() / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "device_id": self.device_id,
            "tokens": self.tokens,
            "last_address": self.last_address,
            "last_name": self.last_name,
            "fast_mode": self.fast_mode,
            "debug": self.debug,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), "utf-8")
        tmp.replace(path)

    # -- pairing tokens --------------------------------------------------
    def token_for(self, address: str) -> bytes | None:
        raw = self.tokens.get(address.upper())
        if not raw:
            return None
        try:
            return bytes.fromhex(raw)
        except ValueError:
            return None

    def set_token(self, address: str, token: bytes | None) -> None:
        if token is None:
            self.tokens.pop(address.upper(), None)
        else:
            self.tokens[address.upper()] = token.hex().upper()

    @property
    def device_id_bytes(self) -> bytes:
        return self.device_id.encode("ascii")
