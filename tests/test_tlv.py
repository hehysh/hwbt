from __future__ import annotations

import pytest

from hwmusic.tlv import TLV, TLVError, be16, be32, decode_varint, encode_varint


@pytest.mark.parametrize("value", [0, 1, 127, 128, 255, 300, 16383, 16384, 1_000_000])
def test_varint_roundtrip(value: int) -> None:
    encoded = encode_varint(value)
    decoded, size = decode_varint(encoded)
    assert decoded == value
    assert size == len(encoded)


def test_varint_known_values() -> None:
    assert encode_varint(0) == b"\x00"
    assert encode_varint(127) == b"\x7f"
    assert encode_varint(128) == b"\x81\x00"
    assert encode_varint(300) == b"\x82\x2c"


def test_tlv_roundtrip() -> None:
    tlv = TLV().put(0x01, b"abc").put(0x02).put(0x7F, be32(100000))
    encoded = tlv.serialize()
    assert encoded == b"\x01\x03abc" + b"\x02\x00" + b"\x7f\x04\x00\x01\x86\xa0"
    assert TLV.parse(encoded) == tlv


def test_tlv_integer_and_text() -> None:
    tlv = TLV().put(0x01, be16(7)).put(0x02, "hello")
    assert tlv.integer(0x01) == 7
    assert tlv.text(0x02) == "hello"
    assert tlv.integer(0x03) is None


def test_tlv_trailing_zero_padding() -> None:
    tlv = TLV.parse(b"\x01\x01A\x00")
    assert tlv.items == [(0x01, b"A")]


def test_tlv_rejects_truncated_item() -> None:
    with pytest.raises(TLVError):
        TLV.parse(b"\x01\x05ab")


def test_tlv_long_value() -> None:
    payload = bytes(range(256)) * 3
    encoded = TLV().put(0x7E, payload).serialize()
    assert TLV.parse(encoded).get(0x7E) == payload
