"""BLE transport (Windows WinRT / macOS CoreBluetooth / Linux BlueZ) via bleak."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from . import constants as C

LOG = logging.getLogger("hwmusic.ble")


class SliceTooLarge(Exception):
    """The negotiated ATT MTU cannot carry our frame; shrink and retry."""


# Windows/BlueZ report these when a GATT operation needs an OS-level bond.
_AUTH_ERROR_TOKENS = (
    "insufficient",
    "authenticat",
    "encrypt",
    "not authorized",
    "0x80070005",
    "protocol error 0x05",
    "protocol error 0x0f",
)

# ATT 0x03 "Write Not Permitted": the characteristic does not accept the write
# type we used (Write Request vs. Write Command). Huawei's FE01 is usually
# write-without-response only, so a Write Request is rejected outright.
_WRITE_TYPE_ERROR_TOKENS = (
    "write not permitted",
    "not permitted",
    "0x03",
)


def _is_auth_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(token in message for token in _AUTH_ERROR_TOKENS)


def _is_write_type_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(token in message for token in _WRITE_TYPE_ERROR_TOKENS)


def _winrt_throughput_params() -> object | None:
    """Return WinRT's ``ThroughputOptimized`` parameters, or ``None``.

    Split out so the Windows-only import is easy to stub in tests and so a
    missing API (anything before Windows 11) is a plain ``None``.
    """
    try:
        from winrt.windows.devices.bluetooth import (
            BluetoothLEPreferredConnectionParameters as Params,
        )
    except Exception:  # noqa: BLE001 - not on Windows, or too old
        return None
    preferred = getattr(Params, "throughput_optimized", None)
    if preferred is None:
        preferred = getattr(Params, "ThroughputOptimized", None)
    return preferred


def _describe_conn_params(params: object) -> str:
    """Render WinRT connection parameters in milliseconds (1.25 ms units)."""
    def number(name: str) -> float | None:
        try:
            value = getattr(params, name)
        except Exception:  # noqa: BLE001 - property may be missing
            return None
        return value if isinstance(value, (int, float)) else None

    low = number("min_connection_interval")
    high = number("max_connection_interval")
    if low is None or high is None:
        return "间隔由系统决定"
    text = f"间隔 {low * 1.25:.2f}–{high * 1.25:.2f} ms"
    latency = number("connection_latency")
    if latency is not None:
        text += f"，延迟 {int(latency)} 个事件"
    timeout = number("link_timeout")
    if timeout is not None:
        text += f"，超时 {timeout * 10:.0f} ms"
    return text


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
        self.write_properties: list[str] = []
        self._client: BleakClient | None = None
        self._callback: Callable[[bytes], None] | None = None
        self._disconnect_cb: Callable[[], None] | None = None
        self._closed = False
        # Kept alive for as long as the link is up: WinRT drops the request as
        # soon as the object is garbage collected.
        self._conn_params_request: object | None = None
        self.high_throughput = False
        # Optional human-readable progress hook (the GUI shows these lines).
        self.on_status: Callable[[str], None] | None = None

    def _status(self, message: str) -> None:
        LOG.info(message)
        if self.on_status is not None:
            self.on_status(message)

    async def _recover_auth(self, exc: BaseException) -> bool:
        """Bond with the watch at OS level, then let the caller retry once.

        Windows refuses to touch the encrypted characteristics of an unpaired
        watch, so a plain BLE connect is not enough: the device has to be
        paired in Windows first. Asking the OS to pair here avoids making the
        user find the Bluetooth settings page.
        """
        if not _is_auth_error(exc):
            return False
        client = self._client
        if client is None or getattr(client, "pair", None) is None:
            return False
        self._status("手表要求系统级配对，正在请求 Windows 配对（手表上如弹出提示请确认）…")
        try:
            paired = await client.pair()
        except NotImplementedError:
            LOG.info("this BLE backend cannot pair programmatically")
            return False
        except Exception as err:  # noqa: BLE001 - pairing may fail in many ways
            self._status(f"系统配对未完成：{err}")
            return False
        self._status("系统配对完成" if paired else "系统配对被取消或未完成")
        return bool(paired)

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
        self._apply_mtu()
        # WinRT updates GattSession.max_pdu_size asynchronously after the link
        # is up; a second look avoids sizing every frame for a 23-byte MTU.
        await asyncio.sleep(0.2)
        self._apply_mtu()
        self._apply_write_type()
        self._request_high_throughput()

    # -- link parameters ------------------------------------------------
    def _request_high_throughput(self) -> None:
        """Ask Windows for a short connection interval during the transfer.

        The watch drives the upload: it asks for a chunk, we write it, it asks
        for the next one. Between those two requests the link sits idle for a
        whole connection interval, and every write waits for the next
        connection event. Windows' default ("balanced") policy uses a long
        interval, which is why a BLE upload can crawl at ~10 KB/s even with a
        514-byte MTU. Android's own app calls
        ``requestConnectionPriority(CONNECTION_PRIORITY_HIGH)`` for exactly
        this reason; on Windows 11 the equivalent is
        ``RequestPreferredConnectionParameters(ThroughputOptimized)``.

        This is a hint: the watch may refuse it, and older Windows versions do
        not have the API at all. Either way the upload still works.
        """
        if sys.platform != "win32":
            return
        device = getattr(self._client, "_requester", None)
        if device is None:
            return
        preferred = _winrt_throughput_params()
        if preferred is None:
            self._status("当前 Windows 不支持请求高吞吐蓝牙连接参数，保持系统默认")
            return
        try:
            request = getattr(device, "request_preferred_connection_parameters", None)
            if request is None:
                request = device.RequestPreferredConnectionParameters  # type: ignore[attr-defined]
            self._conn_params_request = request(preferred)
        except Exception as exc:  # noqa: BLE001 - a hint must never break the link
            self._status(f"请求高吞吐蓝牙连接参数失败：{exc}（保持系统默认，不影响上传）")
            return
        self.high_throughput = True
        self._status(f"已请求高吞吐蓝牙连接参数：{_describe_conn_params(preferred)}")
        LOG.info("requested throughput-optimized connection parameters")

    def _release_high_throughput(self) -> None:
        """Drop the request so Windows can go back to its power-friendly policy."""
        request, self._conn_params_request = self._conn_params_request, None
        self.high_throughput = False
        if request is None:
            return
        for name in ("close", "Close"):
            closer = getattr(request, name, None)
            if closer is None:
                continue
            try:
                closer()
            except Exception:  # noqa: BLE001 - best effort
                LOG.debug("could not release connection parameter request", exc_info=True)
            return

    def _apply_write_type(self) -> None:
        """Pick the write type the FE01 characteristic actually advertises.

        A BLE characteristic can support Write Request (``write``), Write
        Command (``write-without-response``), or both. Sending the wrong one
        makes the watch answer ATT 0x03 "Write Not Permitted", which looks like
        a pairing problem but is not.
        """
        client = self._client
        if client is None:
            return
        characteristic = None
        try:
            characteristic = client.services.get_characteristic(C.WRITE_UUID)
        except Exception:  # noqa: BLE001 - services may not be resolved yet
            LOG.debug("could not look up %s", C.WRITE_UUID, exc_info=True)
        if characteristic is None:
            self._status("未能读取 FE01 特征属性，先按默认写入方式尝试")
            return
        props = [str(item).lower() for item in (characteristic.properties or [])]
        self.write_properties = props
        self._status(f"FE01 特征属性：{', '.join(props) if props else '未知'}")
        with_response = "write" in props
        without_response = "write-without-response" in props
        if without_response and not with_response:
            if self.use_response:
                self._status("FE01 只支持无响应写入，已自动切换为无响应写入")
            self.use_response = False
        elif with_response and not without_response:
            self.use_response = True
        elif with_response and without_response and self.use_response:
            # Each write-with-response costs a full round trip, so the upload
            # can only ever send one packet per connection interval. Fast mode
            # removes that wait and is usually several times faster.
            self._status("FE01 支持无响应写入；开启「快速模式」可明显提速")

    def _apply_mtu(self) -> None:
        """Size frames to the negotiated ATT MTU when the OS reports it."""
        if self._client is None:
            return
        try:
            mtu = int(self._client.mtu_size)
        except Exception:  # noqa: BLE001 - backend may not expose it
            return
        if mtu > 23:
            # The ATT payload is MTU - 3 and one frame is one ATT write, so use
            # all of it: the watch advertises a 1022-byte slice and larger
            # writes mean fewer round trips per upload unit.
            self.max_write_size = max(C.MIN_SLICE_SIZE, mtu - 3)
            self._status(f"蓝牙 MTU {mtu}，单帧最多 {self.max_write_size}B")
        else:
            self._status("蓝牙 MTU 尚未协商（23），先按 244B 发送，写入失败会自动缩小")

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
        try:
            await self._client.start_notify(C.NOTIFY_UUID, self._on_notify)
        except BleakError as exc:
            if not await self._recover_auth(exc):
                raise
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
        self._release_high_throughput()
        if self._client is not None:
            try:
                if self._client.is_connected:
                    await self._client.disconnect()
            except BleakError:
                pass
            self._client = None

    # -- io -------------------------------------------------------------
    async def _write_once(self, data: bytes) -> None:
        client = self._client
        if client is None:
            raise BleakError("蓝牙连接已断开")
        await client.write_gatt_char(C.WRITE_UUID, data, response=self.use_response)

    async def write(self, data: bytes) -> None:
        if self._client is None or not self._client.is_connected:
            raise BleakError("蓝牙连接已断开")
        if len(data) > self.max_write_size:
            raise SliceTooLarge(f"frame {len(data)}B > negotiated {self.max_write_size}B")
        try:
            await self._write_once(data)
            return
        except BleakError as exc:
            if _is_write_type_error(exc):
                self.use_response = not self.use_response
                kind = "带响应" if self.use_response else "无响应"
                self._status(f"写入被拒（{exc}），改用{kind}写入重试…")
                try:
                    await self._write_once(data)
                    return
                except BleakError as retry_exc:
                    exc = retry_exc
            if await self._recover_auth(exc):
                await self._write_once(data)
                return
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
