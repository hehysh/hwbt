"""BLE transport (Windows WinRT / macOS CoreBluetooth / Linux BlueZ) via bleak."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from . import constants as C

LOG = logging.getLogger("hwmusic.ble")


class SliceTooLarge(Exception):
    """The negotiated ATT MTU cannot carry our frame; shrink and retry."""


@dataclass(slots=True)
class ScanResult:
    address: str
    name: str
    rssi: int | None = None

    def label(self) -> str:
        return f"{self.name or '未知设备'}  ({self.address})"


def _looks_like_huawei(name: str, service_uuids: list[str] | None) -> bool:
    for uuid in service_uuids or []:
        compact = uuid.lower().replace("-", "")
        if "fe86" in compact:
            return True
    upper = (name or "").upper()
    return any(upper.startswith(prefix.upper()) for prefix in C.NAME_PREFIXES)


async def scan(timeout: float = 8.0, *, show_all: bool = False) -> list[ScanResult]:
    """Scan for Huawei wearables."""
    try:
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except TypeError:  # older bleak
        devices = await BleakScanner.discover(timeout=timeout)
        return [ScanResult(d.address, d.name or "") for d in devices
                if show_all or _looks_like_huawei(d.name or "", list(d.metadata.get("uuids", [])) if d.metadata else [])]

    results: list[ScanResult] = []
    for device, adv in discovered.values():
        name = adv.local_name or device.name or ""
        uuids = list(adv.service_uuids or [])
        if show_all or _looks_like_huawei(name, uuids):
            results.append(ScanResult(device.address, name, adv.rssi))
    results.sort(key=lambda item: (item.rssi or -999), reverse=True)
    return results


class BleTransport:
    """Thin wrapper so the session does not depend on bleak."""

    def __init__(self, address: str, name: str = "") -> None:
        self.address = address
        self.name = name
        self.max_write_size = C.DEFAULT_SLICE_SIZE
        self.use_response = True
        self._client: BleakClient | None = None
        self._callback: Callable[[bytes], None] | None = None
        self._disconnect_cb: Callable[[], None] | None = None
        self._closed = False

    # -- lifecycle ------------------------------------------------------
    async def connect(self, address: str | None = None) -> None:
        if address:
            self.address = address
        self._client = BleakClient(
            self.address,
            disconnected_callback=self._on_disconnect,
            timeout=25.0,
        )
        LOG.info("connecting to %s", self.address)
        await self._client.connect()
        if not self._client.is_connected:
            raise BleakError("connect() returned without an active link")

    def on_disconnect(self, callback: Callable[[], None]) -> None:
        self._disconnect_cb = callback

    def _on_disconnect(self, _client: BleakClient) -> None:
        if self._closed:
            return
        if self._disconnect_cb:
            self._disconnect_cb()

    async def start_notify(self, callback: Callable[[bytes], None]) -> None:
        if self._client is None:
            raise BleakError("not connected")
        self._callback = callback
        await self._client.start_notify(C.NOTIFY_UUID, self._on_notify)

    def _on_notify(self, _sender: int, data: bytearray) -> None:
        if self._callback is not None:
            self._callback(bytes(data))

    async def stop_notify(self) -> None:
        if self._client and self._client.is_connected:
            try:
                await self._client.stop_notify(C.NOTIFY_UUID)
            except BleakError:
                pass

    async def disconnect(self) -> None:
        self._closed = True
        if self._client is not None:
            try:
                if self._client.is_connected:
                    await self._client.disconnect()
            except BleakError:
                pass
            self._client = None

    # -- io -------------------------------------------------------------
    async def write(self, data: bytes) -> None:
        if self._client is None or not self._client.is_connected:
            raise BleakError("蓝牙连接已断开")
        if len(data) > self.max_write_size:
            raise SliceTooLarge(f"frame {len(data)}B > negotiated {self.max_write_size}B")
        try:
            await self._client.write_gatt_char(C.WRITE_UUID, data, response=self.use_response)
        except BleakError as exc:
            message = str(exc).lower()
            if any(token in message for token in ("too long", "length", "attr", "mtu", "invalid")):
                self.max_write_size = max(C.MIN_SLICE_SIZE, (self.max_write_size * 3) // 4)
                LOG.warning("write failed (%s); reducing frame size to %d", exc, self.max_write_size)
                raise SliceTooLarge(str(exc)) from exc
            raise

    async def __aenter__(self) -> BleTransport:  # noqa: PYI034
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.disconnect()


async def wait_for_notifications(seconds: float) -> None:
    await asyncio.sleep(seconds)
