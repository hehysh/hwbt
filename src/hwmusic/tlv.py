"""TLV coding used inside Huawei protocol frames.

Wire format per item::

    tag (1 byte) | varint length | value

The varint is big-endian, 7 bits per byte, continuation bit 0x80 on every byte
except the last (Gadgetbridge ``HuaweiTLV``/``VarInt``).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator


class TLVError(ValueError):
    """Raised when a TLV blob cannot be decoded."""


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint cannot encode negative values")
    groups = 0
    v = value
    while True:
        groups += 1
        v >>= 7
        if v == 0:
            break
    out = bytearray(groups)
    v = value
    for i in range(groups - 1, -1, -1):
        if i == groups - 1:
            out[i] = v & 0x7F
        else:
            out[i] = (v & 0x7F) | 0x80
        v >>= 7
    return bytes(out)


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Return ``(value, size)`` for the varint starting at ``offset``."""
    value = 0
    i = offset
    while i < len(data):
        byte = data[i]
        value = (value << 7) | (byte & 0x7F)
        i += 1
        if not byte & 0x80:
            return value, i - offset
    raise TLVError("truncated varint")


class TLV:
    """An ordered list of (tag, value) items."""

    __slots__ = ("items",)

    def __init__(self, items: Iterable[tuple[int, bytes]] | None = None) -> None:
        self.items: list[tuple[int, bytes]] = list(items or ())

    # -- building ----------------------------------------------------------
    def put(self, tag: int, value: bytes | bytearray | int | str = b"") -> TLV:
        """Append an item and return ``self`` (chainable)."""
        self.items.append((tag & 0xFF, _as_bytes(value)))
        return self

    def add(self, tag: int, value: bytes | bytearray | int | str = b"") -> TLV:
        return self.put(tag, value)

    def copy(self) -> TLV:
        return TLV(self.items)

    def merge(self, other: TLV) -> TLV:
        self.items.extend(other.items)
        return self

    # -- encoding ----------------------------------------------------------
    def serialize(self) -> bytes:
        out = bytearray()
        for tag, value in self.items:
            out.append(tag & 0xFF)
            out += encode_varint(len(value))
            out += value
        return bytes(out)

    @classmethod
    def parse(cls, data: bytes) -> TLV:
        tlv = cls()
        i = 0
        n = len(data)
        while i < n:
            tag = data[i]
            i += 1
            # A single trailing 0x00 byte is padding inside some encrypted payloads
            if i == n and tag == 0:
                break
            size, used = decode_varint(data, i)
            i += used
            if size > n - i:
                raise TLVError(f"TLV item 0x{tag:02X} overruns payload ({size} > {n - i})")
            tlv.items.append((tag, bytes(data[i : i + size])))
            i += size
        return tlv

    # -- reading -----------------------------------------------------------
    def get(self, tag: int) -> bytes | None:
        for item_tag, value in self.items:
            if item_tag == tag:
                return value
        return None

    def get_all(self, tag: int) -> list[bytes]:
        return [value for item_tag, value in self.items if item_tag == tag]

    def integer(self, tag: int) -> int | None:
        value = self.get(tag)
        if not value or len(value) > 8:
            return None
        return int.from_bytes(value, "big")

    def boolean(self, tag: int) -> bool | None:
        value = self.get(tag)
        if not value:
            return None
        return value[0] != 0

    def text(self, tag: int) -> str | None:
        value = self.get(tag)
        if value is None:
            return None
        return value.decode("utf-8", "replace")

    def has(self, tag: int) -> bool:
        return any(item_tag == tag for item_tag, _ in self.items)

    def tags(self) -> list[int]:
        return [tag for tag, _ in self.items]

    def __iter__(self) -> Iterator[tuple[int, bytes]]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TLV) and self.items == other.items

    def __repr__(self) -> str:
        body = " ".join(
            f"{tag:02X}={value.hex().upper() if value else '∅'}" for tag, value in self.items
        )
        return f"TLV({body})"


def _as_bytes(value: bytes | bytearray | int | str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("cannot encode negative integer")
        length = max(1, (value.bit_length() + 7) // 8)
        return value.to_bytes(length, "big")
    raise TypeError(f"unsupported TLV value type: {type(value)!r}")


def be16(value: int) -> bytes:
    return int(value).to_bytes(2, "big")


def be32(value: int) -> bytes:
    return int(value).to_bytes(4, "big")
