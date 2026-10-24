from __future__ import annotations

import pytest
from bleak.exc import BleakError

from hwmusic.transport import BleTransport, _is_auth_error, _looks_like_huawei


class _FakeClient:
    def __init__(self, mtu: int) -> None:
        self.mtu_size = mtu


def test_detects_huawei_by_service_uuid() -> None:
    assert _looks_like_huawei("", ["0000fe86-0000-1000-8000-00805f9b34fb"])
    assert _looks_like_huawei("", ["FE86"])


def test_detects_huawei_by_name() -> None:
    assert _looks_like_huawei("HUAWEI WATCH GT 4", None)
    assert _looks_like_huawei("HONOR Band 6", [])
    assert not _looks_like_huawei("Galaxy Buds", ["0000180f-0000-1000-8000-00805f9b34fb"])


def test_mtu_is_used_for_write_size() -> None:
    transport = BleTransport("AA:BB")
    transport._client = _FakeClient(247)  # type: ignore[assignment]
    transport._apply_mtu()
    assert transport.max_write_size == 244

    transport._client = _FakeClient(100)  # type: ignore[assignment]
    transport._apply_mtu()
    assert transport.max_write_size == 97


def test_minimum_mtu_keeps_default_slice_size() -> None:
    transport = BleTransport("AA:BB")
    transport._client = _FakeClient(23)  # type: ignore[assignment]
    transport._apply_mtu()
    assert transport.max_write_size == 244


def test_auth_error_detection() -> None:
    assert _is_auth_error(Exception("Insufficient authentication"))
    assert _is_auth_error(Exception("GATT protocol error 0x05"))
    assert _is_auth_error(Exception("WinError 0x80070005"))
    assert _is_auth_error(Exception("Write not permitted"))
    assert not _is_auth_error(Exception("Characteristic not found"))


class _PairingClient:
    def __init__(self, result: bool = True, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.paired = 0

    async def pair(self) -> bool:
        self.paired += 1
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_recover_auth_ignores_unrelated_errors() -> None:
    transport = BleTransport("AA:BB")
    client = _PairingClient()
    transport._client = client  # type: ignore[assignment]
    assert await transport._recover_auth(Exception("Characteristic not found")) is False
    assert client.paired == 0


@pytest.mark.asyncio
async def test_recover_auth_pairs_when_asked() -> None:
    transport = BleTransport("AA:BB")
    client = _PairingClient()
    transport._client = client  # type: ignore[assignment]
    assert await transport._recover_auth(Exception("Insufficient encryption")) is True
    assert client.paired == 1


@pytest.mark.asyncio
async def test_recover_auth_survives_pairing_failure() -> None:
    transport = BleTransport("AA:BB")
    client = _PairingClient(error=RuntimeError("user cancelled"))
    transport._client = client  # type: ignore[assignment]
    assert await transport._recover_auth(Exception("Insufficient encryption")) is False


class _NotifyClient:
    def __init__(self) -> None:
        self.calls = 0
        self.paired = False

    async def start_notify(self, _uuid: str, _callback) -> None:
        self.calls += 1
        if self.calls == 1:
            raise BleakError("Insufficient authentication")

    async def pair(self) -> bool:
        self.paired = True
        return True


@pytest.mark.asyncio
async def test_start_notify_pairs_then_retries() -> None:
    transport = BleTransport("AA:BB")
    client = _NotifyClient()
    transport._client = client  # type: ignore[assignment]
    await transport.start_notify(lambda _data: None)
    assert client.paired is True
    assert client.calls == 2
