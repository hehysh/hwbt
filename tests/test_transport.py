from __future__ import annotations

import pytest
from bleak.exc import BleakError

from hwmusic import transport as transport_module
from hwmusic.transport import (
    BleTransport,
    _describe_conn_params,
    _is_auth_error,
    _looks_like_huawei,
)


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


def test_large_mtu_is_not_capped_at_the_watch_slice_size() -> None:
    transport = BleTransport("AA:BB")
    transport._client = _FakeClient(517)  # type: ignore[assignment]
    transport._apply_mtu()
    assert transport.max_write_size == 514


def test_minimum_mtu_keeps_default_slice_size() -> None:
    transport = BleTransport("AA:BB")
    transport._client = _FakeClient(23)  # type: ignore[assignment]
    transport._apply_mtu()
    assert transport.max_write_size == 244


def test_auth_error_detection() -> None:
    assert _is_auth_error(Exception("Insufficient authentication"))
    assert _is_auth_error(Exception("GATT protocol error 0x05"))
    assert _is_auth_error(Exception("WinError 0x80070005"))
    # ATT 0x03 is a write-type mismatch, not a missing bond: pairing would only
    # push the watch into Huawei Health's QR-code flow.
    assert not _is_auth_error(Exception("Write not permitted"))
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


class _Characteristic:
    def __init__(self, properties: list[str]) -> None:
        self.properties = properties


class _Services:
    def __init__(self, characteristic: _Characteristic | None) -> None:
        self._characteristic = characteristic

    def get_characteristic(self, _uuid: str) -> _Characteristic | None:
        return self._characteristic


class _GattClient:
    """Fake FE01 that only accepts write-without-response, like the GT 2e."""

    def __init__(self, properties: list[str], mtu: int = 247) -> None:
        self.mtu_size = mtu
        self.is_connected = True
        self.services = _Services(_Characteristic(properties))
        self.writes: list[bool] = []
        self.pair_calls = 0

    async def write_gatt_char(self, _uuid: str, _data: bytes, response: bool = True) -> None:
        self.writes.append(response)
        if response and "write" not in self.services._characteristic.properties:  # type: ignore[union-attr]
            raise BleakError("(3, 'GATT Protocol Error: Write Not Permitted')")

    async def pair(self) -> bool:
        self.pair_calls += 1
        return True


def test_write_type_follows_characteristic_properties() -> None:
    transport = BleTransport("AA:BB")
    transport._client = _GattClient(["write-without-response", "notify"])  # type: ignore[assignment]
    transport._apply_write_type()
    assert transport.use_response is False
    assert transport.write_properties == ["write-without-response", "notify"]


def test_write_type_keeps_user_choice_when_both_are_supported() -> None:
    transport = BleTransport("AA:BB")
    transport._client = _GattClient(["write", "write-without-response"])  # type: ignore[assignment]
    transport._apply_write_type()
    assert transport.use_response is True


def test_write_type_forces_response_when_only_write_is_supported() -> None:
    transport = BleTransport("AA:BB")
    transport.use_response = False
    transport._client = _GattClient(["write"])  # type: ignore[assignment]
    transport._apply_write_type()
    assert transport.use_response is True


def test_missing_characteristic_keeps_defaults() -> None:
    transport = BleTransport("AA:BB")
    client = _GattClient([])
    client.services = _Services(None)
    transport._client = client  # type: ignore[assignment]
    transport._apply_write_type()
    assert transport.use_response is True
    assert transport.write_properties == []


@pytest.mark.asyncio
async def test_write_switches_to_without_response_on_not_permitted() -> None:
    transport = BleTransport("AA:BB")
    client = _GattClient(["write-without-response"])
    transport._client = client  # type: ignore[assignment]
    await transport.write(b"\x5a\x00\x01\x00")
    assert client.writes == [True, False]
    assert transport.use_response is False
    # ATT 0x03 must never trigger an OS-level bond (that enters the QR flow).
    assert client.pair_calls == 0


@pytest.mark.asyncio
async def test_write_uses_the_learned_type_next_time() -> None:
    transport = BleTransport("AA:BB")
    client = _GattClient(["write-without-response"])
    transport._client = client  # type: ignore[assignment]
    await transport.write(b"\x5a")
    await transport.write(b"\x5b")
    assert client.writes == [True, False, False]


@pytest.mark.asyncio
async def test_write_raises_when_both_types_fail() -> None:
    transport = BleTransport("AA:BB")

    class _AlwaysRejects(_GattClient):
        async def write_gatt_char(self, _uuid: str, _data: bytes, response: bool = True) -> None:
            self.writes.append(response)
            raise BleakError("(3, 'GATT Protocol Error: Write Not Permitted')")

    client = _AlwaysRejects(["write", "write-without-response"])
    transport._client = client  # type: ignore[assignment]
    with pytest.raises(BleakError):
        await transport.write(b"\x5a")
    assert client.writes == [True, False]
    assert client.pair_calls == 0


class _FakeParams:
    min_connection_interval = 9  # 11.25 ms
    max_connection_interval = 12  # 15 ms
    connection_latency = 0
    link_timeout = 500  # 5000 ms


class _FakeRequest:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeDevice:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.requested: list[object] = []

    def request_preferred_connection_parameters(self, params: object) -> _FakeRequest:
        if self.fail:
            raise OSError("unsupported")
        self.requested.append(params)
        return _FakeRequest()


def _win_transport(device: _FakeDevice) -> BleTransport:
    transport = BleTransport("AA:BB")
    transport._client = type("C", (), {"_requester": device})()  # type: ignore[assignment]
    return transport


def test_describe_conn_params_uses_1_25ms_units() -> None:
    text = _describe_conn_params(_FakeParams())
    assert "11.25–15.00 ms" in text
    assert "5000 ms" in text


def test_high_throughput_is_skipped_outside_windows() -> None:
    transport = _win_transport(_FakeDevice())
    transport._request_high_throughput()
    assert transport.high_throughput is False


def test_high_throughput_request_is_kept_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport_module.sys, "platform", "win32")
    monkeypatch.setattr(transport_module, "_winrt_throughput_params", lambda: _FakeParams())
    device = _FakeDevice()
    transport = _win_transport(device)

    transport._request_high_throughput()

    assert transport.high_throughput is True
    assert len(device.requested) == 1
    assert isinstance(device.requested[0], _FakeParams)
    # The request object must stay referenced, otherwise WinRT drops it.
    assert transport._conn_params_request is not None

    transport._release_high_throughput()
    assert transport.high_throughput is False
    assert transport._conn_params_request is None


def test_high_throughput_failure_does_not_break_the_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport_module.sys, "platform", "win32")
    monkeypatch.setattr(transport_module, "_winrt_throughput_params", lambda: _FakeParams())
    messages: list[str] = []
    transport = _win_transport(_FakeDevice(fail=True))
    transport.on_status = messages.append

    transport._request_high_throughput()

    assert transport.high_throughput is False
    assert any("失败" in message for message in messages)


def test_high_throughput_is_skipped_when_the_api_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_module.sys, "platform", "win32")
    monkeypatch.setattr(transport_module, "_winrt_throughput_params", lambda: None)
    device = _FakeDevice()
    transport = _win_transport(device)

    transport._request_high_throughput()

    assert transport.high_throughput is False
    assert device.requested == []
