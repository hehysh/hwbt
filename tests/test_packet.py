from __future__ import annotations

import pytest

from hwmusic.packet import (
    FrameError,
    PacketParser,
    build_frames,
    crc16,
    serialize,
    serialize_sliced,
)


def test_crc16_check_value() -> None:
    # Standard CRC-16/XMODEM check value
    assert crc16(b"123456789") == 0x31C3


def test_single_frame_layout() -> None:
    body = bytes.fromhex("0100020003000400")
    frame = serialize(0x01, 0x01, body)
    assert frame[0] == 0x5A
    assert int.from_bytes(frame[1:3], "big") == len(body) + 3
    assert frame[3] == 0x00
    assert frame[4] == 0x01 and frame[5] == 0x01
    assert frame[6:-2] == body
    assert int.from_bytes(frame[-2:], "big") == crc16(frame[:-2])


def test_parser_single_frame() -> None:
    parser = PacketParser()
    frame = serialize(0x25, 0x04, b"\x01\x00")
    packets = parser.feed(frame)
    assert len(packets) == 1
    assert (packets[0].service, packets[0].command) == (0x25, 0x04)


def test_parser_handles_split_delivery() -> None:
    parser = PacketParser()
    frame = serialize(0x01, 0x01, b"\x01\x00\x02\x00")
    packets = []
    for byte in frame:
        packets += parser.feed(bytes([byte]))
    assert len(packets) == 1


def test_sliced_roundtrip() -> None:
    body = bytes(range(256)) * 4  # 1024 bytes -> many frames
    frames = serialize_sliced(0x28, 0x06, body, slice_size=244)
    assert len(frames) > 1
    assert all(len(frame) <= 244 for frame in frames)
    assert frames[0][3] == 0x01
    assert frames[-1][3] == 0x03
    parser = PacketParser()
    packets: list = []
    for frame in frames:
        packets += parser.feed(frame)
    assert len(packets) == 1
    assert packets[0].body == body
    assert (packets[0].service, packets[0].command) == (0x28, 0x06)


def test_sliced_sequence_numbers_increment() -> None:
    frames = serialize_sliced(0x01, 0x28, bytes(600), slice_size=100)
    sequences = [frame[4] for frame in frames]
    assert sequences == list(range(len(frames)))


def test_small_body_is_not_sliced() -> None:
    assert serialize_sliced(0x01, 0x01, b"\x01\x00", slice_size=244) == [serialize(0x01, 0x01, b"\x01\x00")]


def test_crc_error_is_reported() -> None:
    parser = PacketParser()
    frame = bytearray(serialize(0x01, 0x01, b"\x01\x00"))
    frame[-1] ^= 0xFF
    with pytest.raises(FrameError):
        parser.feed(bytes(frame))


def test_build_frames_flag() -> None:
    body = bytes(600)
    assert len(build_frames(0x01, 0x28, body, slice_size=244, sliced=False)) == 1
    assert len(build_frames(0x01, 0x28, body, slice_size=244, sliced=True)) > 1
