from __future__ import annotations

from hwmusic.transport import BleTransport, _looks_like_huawei


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
