"""Frame coding for the Huawei lite-wearable protocol.

Unslotted frame (single piece)::

    5A | len(BE16) | 00 | service | command | body... | CRC16(BE16)

Fragmented frame (any piece)::

    5A | len(BE16) | piece(1|2|3) | seq | [service | command] | data... | CRC16

``len`` counts everything after the two length bytes, including the piece/seq
bytes but excluding the CRC. CRC is CRC-16/XMODEM over the whole frame minus the
trailing CRC bytes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import MAGIC
from .tlv import TLV, TLVError

# Commands whose body is raw data rather than TLV (HuaweiPacket.parseData)
NON_TLV_COMMANDS = frozenset({(0x0A, 0x05), (0x28, 0x06), (0x2C, 0x05), (0x1C, 0x05)})

_CRC_TABLE: list[int] = []
for _i in range(256):
    _crc = _i << 8
    for _ in range(8):
        _crc = ((_crc << 1) ^ 0x1021) & 0xFFFF if _crc & 0x8000 else (_crc << 1) & 0xFFFF
    _CRC_TABLE.append(_crc)


def crc16(data: bytes) -> int:
    """CRC-16/XMODEM (poly 0x1021, init 0x0000, no reflection, no xorout)."""
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ byte) & 0xFF]
    return crc


class FrameError(ValueError):
    pass


@dataclass(slots=True)
class Packet:
    service: int
    command: int
    body: bytes
    tlv: TLV | None
    raw: bytes = b""

    @classmethod
    def from_body(cls, service: int, command: int, body: bytes) -> Packet:
        tlv: TLV | None = None
        if (service, command) not in NON_TLV_COMMANDS:
            try:
                tlv = TLV.parse(body)
            except TLVError:
                tlv = None
        return cls(service=service, command=command, body=body, tlv=tlv)

    @property
    def is_encrypted(self) -> bool:
        return bool(self.tlv and self.tlv.get(0x7C) and self.tlv.get(0x7C)[0] == 1)

    @property
    def result_code(self) -> int | None:
        return self.tlv.integer(0x7F) if self.tlv else None

    @property
    def ok(self) -> bool:
        from .constants import RESULT_SUCCESS

        return self.result_code in (None, RESULT_SUCCESS)

    def __repr__(self) -> str:
        return f"Packet(0x{self.service:02X}/0x{self.command:02X}, {len(self.body)}B)"


def serialize(service: int, command: int, body: bytes) -> bytes:
    length = 2 + len(body) + 1
    if length > 0x7FFF:
        raise FrameError("frame too large")
    frame = bytearray([MAGIC])
    frame += length.to_bytes(2, "big")
    frame.append(0x00)  # no fragmentation
    frame.append(service & 0xFF)
    frame.append(command & 0xFF)
    frame += body
    frame += crc16(bytes(frame)).to_bytes(2, "big")
    return bytes(frame)


def serialize_sliced(service: int, command: int, body: bytes, slice_size: int) -> list[bytes]:
    """Split ``body`` into frames of at most ``slice_size`` bytes."""
    header_length = 5
    footer_length = 2
    if slice_size <= header_length + footer_length + 2:
        raise FrameError(f"slice size {slice_size} is too small")
    max_body = slice_size - header_length - footer_length
    packet_count = (len(body) + 2 + max_body - 1) // max_body
    if packet_count <= 1:
        return [serialize(service, command, body)]

    frames: list[bytes] = []
    offset = 0
    piece = 0x01
    flag = 0x00
    for index in range(packet_count):
        packet_size = min(slice_size, len(body) - offset + header_length + footer_length)
        content_size = packet_size - header_length - footer_length
        if index == packet_count - 1:
            piece = 0x03
        frame = bytearray([MAGIC])
        frame += (packet_size - header_length).to_bytes(2, "big")
        frame.append(piece)
        frame.append(flag)
        flag = (flag + 1) & 0xFF
        if piece == 0x01:
            frame.append(service & 0xFF)
            frame.append(command & 0xFF)
            piece = 0x02
            content_size -= 2
        frame += body[offset : offset + content_size]
        offset += content_size
        frame += crc16(bytes(frame)).to_bytes(2, "big")
        frames.append(bytes(frame))
    return frames


def build_frames(
    service: int,
    command: int,
    body: bytes,
    *,
    slice_size: int = 0xF4,
    sliced: bool = False,
) -> list[bytes]:
    if sliced:
        return serialize_sliced(service, command, body, slice_size)
    return [serialize(service, command, body)]


class PacketParser:
    """Reassembles notification bytes into packets."""

    def __init__(self) -> None:
        self._pending = bytearray()
        self._slices = bytearray()

    def reset(self) -> None:
        self._pending.clear()
        self._slices.clear()

    def feed(self, data: bytes) -> list[Packet]:
        self._pending += data
        packets: list[Packet] = []
        while self._pending:
            if self._pending[0] != MAGIC:
                self.reset()
                raise FrameError(f"unexpected magic 0x{self._pending[0]:02X}")
            if len(self._pending) < 3:
                break
            expected = int.from_bytes(self._pending[1:3], "big")
            if not 1 <= expected <= 0x7FFF:
                self.reset()
                raise FrameError(f"bad frame length {expected}")
            total = 3 + expected + 2
            if len(self._pending) < total:
                break
            frame = bytes(self._pending[:total])
            del self._pending[:total]

            piece = frame[3]
            sliced = 1 <= piece <= 3
            extra = 2 if sliced else 1
            if expected < extra:
                raise FrameError("frame too short")
            expected_crc = int.from_bytes(frame[3 + expected : 5 + expected], "big")
            actual_crc = crc16(frame[: 3 + expected])
            if expected_crc != actual_crc:
                self._slices.clear()
                raise FrameError(
                    f"CRC mismatch: got 0x{expected_crc:04X}, computed 0x{actual_crc:04X}"
                )

            payload = frame[3 + extra : 3 + expected]
            if sliced:
                self._slices += payload
                if piece != 3:
                    continue
                payload = bytes(self._slices)
                self._slices.clear()
            if len(payload) < 2:
                raise FrameError("payload without service/command")
            packets.append(Packet.from_body(payload[0], payload[1], payload[2:]))
        return packets


def hexdump(data: bytes, limit: int = 96) -> str:
    """Compact hex dump for logs (truncates long buffers)."""
    head = data[:limit]
    text = " ".join(f"{b:02X}" for b in head)
    if len(data) > limit:
        text += f" … ({len(data)}B)"
    return text
