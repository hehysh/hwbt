"""Generate the application icon (PNG for the window, ICO for the exe).

Pure Python on purpose: no Pillow/build dependency is needed on the packaging
machine. Run from the repository root::

    python packaging/make_icon.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PNG_PATH = ROOT / "src" / "hwmusic" / "gui" / "icon.png"
ICO_PATH = ROOT / "packaging" / "icon.ico"

SIZE = 256
SCALE = 4
S = SIZE * SCALE

ACCENT = (199, 0, 11, 255)

WHITE = (255, 255, 255, 255)
SCREEN = (32, 12, 14, 255)
NOTE = (255, 255, 255, 255)


class Canvas:
    def __init__(self, size: int) -> None:
        self.size = size
        self.pixels = [[(0, 0, 0, 0)] * size for _ in range(size)]

    def rounded_rect(self, x0: int, y0: int, x1: int, y1: int, radius: int, color) -> None:
        for y in range(max(0, y0), min(self.size, y1)):
            for x in range(max(0, x0), min(self.size, x1)):
                dx = 0
                dy = 0
                if x < x0 + radius:
                    dx = x0 + radius - x
                elif x >= x1 - radius:
                    dx = x - (x1 - radius - 1)
                if y < y0 + radius:
                    dy = y0 + radius - y
                elif y >= y1 - radius:
                    dy = y - (y1 - radius - 1)
                if dx * dx + dy * dy <= radius * radius:
                    self.pixels[y][x] = color

    def circle(self, cx: float, cy: float, radius: float, color) -> None:
        r2 = radius * radius
        for y in range(max(0, int(cy - radius) - 1), min(self.size, int(cy + radius) + 2)):
            for x in range(max(0, int(cx - radius) - 1), min(self.size, int(cx + radius) + 2)):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r2:
                    self.pixels[y][x] = color

    def thick_line(self, x0: float, y0: float, x1: float, y1: float, width: float, color) -> None:
        half = width / 2
        for y in range(max(0, int(min(y0, y1) - half) - 1), min(self.size, int(max(y0, y1) + half) + 2)):
            for x in range(max(0, int(min(x0, x1) - half) - 1), min(self.size, int(max(x0, x1) + half) + 2)):
                if _distance_to_segment(x, y, x0, y0, x1, y1) <= half:
                    self.pixels[y][x] = color

    def downsample(self, factor: int) -> list[list[tuple[int, int, int, int]]]:
        out_size = self.size // factor
        out = []
        for y in range(out_size):
            row = []
            for x in range(out_size):
                r = g = b = a = 0
                for dy in range(factor):
                    for dx in range(factor):
                        px = self.pixels[y * factor + dy][x * factor + dx]
                        r += px[0] * px[3]
                        g += px[1] * px[3]
                        b += px[2] * px[3]
                        a += px[3]
                if a == 0:
                    row.append((0, 0, 0, 0))
                else:
                    row.append((r // a, g // a, b // a, a // (factor * factor)))
            out.append(row)
        return out


def _distance_to_segment(px: float, py: float, x0: float, y0: float, x1: float, y1: float) -> float:
    dx, dy = x1 - x0, y1 - y0
    if dx == 0 and dy == 0:
        return math.hypot(px - x0, py - y0)
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


def build_canvas() -> Canvas:
    canvas = Canvas(S)
    u = SCALE

    # Background: rounded square in Huawei red.
    canvas.rounded_rect(0, 0, S, S, 56 * u, ACCENT)

    # Watch body (white) with a screen and two strap stubs.
    canvas.rounded_rect(int(70 * u), int(34 * u), int(186 * u), int(222 * u), 26 * u, WHITE)
    canvas.rounded_rect(int(82 * u), int(24 * u), int(174 * u), int(60 * u), 14 * u, WHITE)
    canvas.rounded_rect(int(82 * u), int(196 * u), int(174 * u), int(232 * u), 14 * u, WHITE)
    canvas.rounded_rect(int(82 * u), int(56 * u), int(174 * u), int(200 * u), 20 * u, SCREEN)

    # Music note: eighth note with a beam, centered on the screen.
    head_r = 12 * u
    canvas.circle(112 * u, 158 * u, head_r, NOTE)
    canvas.circle(158 * u, 146 * u, head_r, NOTE)
    canvas.thick_line(121 * u, 156 * u, 121 * u, 104 * u, 7 * u, NOTE)
    canvas.thick_line(167 * u, 144 * u, 167 * u, 92 * u, 7 * u, NOTE)
    canvas.thick_line(121 * u, 104 * u, 167 * u, 92 * u, 12 * u, NOTE)
    return canvas


def png_bytes(rows: list[list[tuple[int, int, int, int]]]) -> bytes:
    height = len(rows)
    width = len(rows[0])
    raw = bytearray()
    for row in rows:
        raw.append(0)
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def ico_bytes(png: bytes) -> bytes:
    # Single 256x256 entry, PNG-compressed (supported since Windows Vista).
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


def main() -> None:
    rows = build_canvas().downsample(SCALE)
    png = png_bytes(rows)
    PNG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PNG_PATH.write_bytes(png)
    ICO_PATH.parent.mkdir(parents=True, exist_ok=True)
    ICO_PATH.write_bytes(ico_bytes(png))
    print(f"wrote {PNG_PATH} ({len(png)} bytes)")
    print(f"wrote {ICO_PATH}")


if __name__ == "__main__":
    main()
