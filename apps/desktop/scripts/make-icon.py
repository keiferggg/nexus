"""Generate build/icon.png (512x512): rounded square, blue-violet gradient, white 'N'. No dependencies."""
import struct
import zlib
from pathlib import Path

S = 512
R = 96
def inside_round(x, y):
    cx = min(max(x, R), S - 1 - R)
    cy = min(max(y, R), S - 1 - R)
    return (x - cx) ** 2 + (y - cy) ** 2 <= R * R

def in_n(x, y):
    l, r, t, b, w = 150, 362, 130, 382, 52
    if not (t <= y <= b):
        return False
    if l <= x <= l + w or r - w <= x <= r:
        return True
    # diagonal from top-left to bottom-right
    fy = t + (x - l) * (b - t) / (r - l)
    return l <= x <= r and abs(y - fy) <= w * 0.75

rows = []
for y in range(S):
    row = bytearray([0])
    for x in range(S):
        if not inside_round(x, y):
            row += bytes([0, 0, 0, 0])
            continue
        t = (x + y) / (2 * S)
        r, g, b = int(106 + (185 - 106) * t), int(168 + (156 - 168) * t), int(255 + (255 - 255) * t)
        if in_n(x, y):
            r, g, b = 255, 255, 255
        row += bytes([r, g, b, 255])
    rows.append(bytes(row))
raw = zlib.compress(b"".join(rows), 9)
def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", S, S, 8, 6, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")
out = Path(__file__).resolve().parents[1] / "build" / "icon.png"
out.write_bytes(png)
print(out, len(png))
