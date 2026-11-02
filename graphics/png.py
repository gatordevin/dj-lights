"""Minimal PNG codec (stdlib zlib only).
write_png(path, w, h, rgb)  — rgb: bytes, len == w*h*3, row-major, top row first. Creates parent dirs.
read_png(path) -> (w, h, rgb) — 8-bit RGB or RGBA (alpha dropped), non-interlaced; what we write and
  what most tools write. Raises ValueError for other formats (palette, 16-bit, interlaced, grey).
Limits: pure Python, so decoding is slow for large images (per-byte unfilter loop); writes only 8-bit RGB with filter 0
  (unoptimised size); read does not verify CRCs and drops alpha; reads whole file into memory.
Used by: graphics/canvas.py, graphics/appkit.py, tests (pixel checks)."""
import struct, zlib
from pathlib import Path

def write_png(path, w, h, rgb):
    if len(rgb) != w * h * 3: raise ValueError(f"rgb has {len(rgb)} bytes, expected {w*h*3}")
    stride = w * 3
    raw = b"".join(b"\0" + rgb[y * stride:(y + 1) * stride] for y in range(h))
    def chunk(k, d):
        c = struct.pack(">I", len(d)) + k + d
        return c + struct.pack(">I", zlib.crc32(k + d))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

def read_png(path):
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n": raise ValueError(f"{path}: not a PNG")
    pos, idat, hdr = 8, [], None
    while pos < len(data):
        n, k = struct.unpack(">I4s", data[pos:pos + 8]); d = data[pos + 8:pos + 8 + n]; pos += 12 + n
        if k == b"IHDR": hdr = struct.unpack(">IIBBBBB", d)
        elif k == b"IDAT": idat.append(d)
        elif k == b"IEND": break
    w, h, depth, ctype, _, _, inter = hdr
    if depth != 8 or ctype not in (2, 6) or inter: raise ValueError(f"{path}: unsupported PNG (depth {depth}, type {ctype}, interlace {inter})")
    bpp = 3 if ctype == 2 else 4; stride = w * bpp
    raw, out, prev = zlib.decompress(b"".join(idat)), bytearray(), bytearray(stride)
    for y in range(h):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0; b = prev[i]; c = prev[i - bpp] if i >= bpp else 0
            if f == 1: line[i] = (line[i] + a) & 255
            elif f == 2: line[i] = (line[i] + b) & 255
            elif f == 3: line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c; pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += line if bpp == 3 else b"".join(line[i:i + 3] for i in range(0, stride, 4)); prev = line
    return w, h, bytes(out)
