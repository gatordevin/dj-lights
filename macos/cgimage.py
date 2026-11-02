"""CGImage <-> pixels and file I/O with ImageIO/CoreGraphics: the one home for decoding, encoding and wrapping raw
pixels as CGImages (for Vision requests, camera frames, PNG/JPEG export).
Python:
  load(path_or_bytes) -> CGImageRef (+1)        PNG/JPEG/HEIC/TIFF/... via ImageIO (EXIF orientation is NOT applied)
  size(img) -> (w, h)
  to_pixels(img, gray=False, dest=None) -> (w, h, data)   draw into an 8-bit bitmap: gray=True -> w*h luminance bytes
        (CoreGraphics colour conversion), else w*h*4 RGBX (sRGB, X = 255). dest: an int address with room for the
        result (e.g. a Metal buffer) — then data is that address; otherwise data is bytes.
  from_pixels(data, w, h, channels) -> CGImageRef (+1)    channels 1 (gray), 3 (RGB) or 4 (RGBX); data bytes-like
        or an int address (copied, so the source can be freed right after)
  encode(img, fmt="png"|"jpeg", quality=0.9) -> bytes      write(img, path) -> path (format from the suffix)
  release(img)                                    CFRelease (None-safe)
Contract: every +1 object returned must be release()d by the caller. FileNotFoundError for a missing file;
  ValueError for bad sizes/channels/format or undecodable data; RuntimeError if CoreGraphics/ImageIO fail.
Limits: 8-bit only (16-bit/float sources are converted down); colour-managed draws (sRGB / device gray) — fine for CV,
  not for colorimetry; no EXIF rotation; no animated/multi-page decoding (first frame only).
Used by: graphics/appkit.encode, cv/image.py. Depends on: macos.dylib, macos.objc (NSString/NSURL)."""
import sys, ctypes
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from macos.dylib import cfunc, framework
from macos.objc import cls, msg, nsstring

c_void_p, c_size_t, c_bool, c_long, c_double = ctypes.c_void_p, ctypes.c_size_t, ctypes.c_bool, ctypes.c_long, ctypes.c_double
CF, CG, IO = "CoreFoundation", "CoreGraphics", "ImageIO"
_release = cfunc(CF, "CFRelease", None, c_void_p)
_W = cfunc(CG, "CGImageGetWidth", c_size_t, c_void_p); _H = cfunc(CG, "CGImageGetHeight", c_size_t, c_void_p)
_ctx_create = cfunc(CG, "CGBitmapContextCreate", c_void_p, c_void_p, c_size_t, c_size_t, c_size_t, c_size_t, c_void_p, ctypes.c_uint32)


class _Rect(ctypes.Structure):
    _fields_ = [("x", c_double), ("y", c_double), ("w", c_double), ("h", c_double)]


_draw = cfunc(CG, "CGContextDrawImage", None, c_void_p, _Rect, c_void_p)
_ctx_release = cfunc(CG, "CGContextRelease", None, c_void_p)
_SPACES = {}


def _space(gray):
    if gray not in _SPACES:            # shared colour spaces: created once (creating per frame leaks)
        _SPACES[gray] = (cfunc(CG, "CGColorSpaceCreateDeviceGray", c_void_p)() if gray else
                         cfunc(CG, "CGColorSpaceCreateWithName", c_void_p, c_void_p)(c_void_p.in_dll(framework(CG), "kCGColorSpaceSRGB")))
    return _SPACES[gray]


def release(img):
    if img: _release(img)


def size(img): return int(_W(img)), int(_H(img))


def load(src):
    if isinstance(src, (bytes, bytearray, memoryview)):
        data = cfunc(CF, "CFDataCreate", c_void_p, c_void_p, ctypes.c_char_p, c_long)(None, bytes(src), len(src))
        isrc = cfunc(IO, "CGImageSourceCreateWithData", c_void_p, c_void_p, c_void_p)(data, None); _release(data)
        what = "image bytes"
    else:
        p = Path(src)
        if not p.exists(): raise FileNotFoundError(str(p))
        url = msg(cls("NSURL"), "fileURLWithPath:", nsstring(str(p.resolve())))
        isrc = cfunc(IO, "CGImageSourceCreateWithURL", c_void_p, c_void_p, c_void_p)(url, None); what = str(p)
    if not isrc: raise ValueError(f"ImageIO cannot open {what}")
    try:
        img = cfunc(IO, "CGImageSourceCreateImageAtIndex", c_void_p, c_void_p, c_size_t, c_void_p)(isrc, 0, None)
    finally: _release(isrc)
    if not img: raise ValueError(f"ImageIO cannot decode {what}")
    return img


def to_pixels(img, gray=False, dest=None):
    w, h = size(img); bpr = w if gray else w * 4
    buf = None
    if dest is None: buf = (ctypes.c_ubyte * (bpr * h))(); addr = ctypes.addressof(buf)
    else: addr = int(dest)
    ctx = _ctx_create(addr, w, h, 8, bpr, _space(gray), 0 if gray else 5)      # gray: alpha none · RGBX: noneSkipLast
    if not ctx: raise RuntimeError("CGBitmapContextCreate failed")
    try: _draw(ctx, _Rect(0, 0, w, h), img)          # bitmap memory is top row first: no flip needed
    finally: _ctx_release(ctx)
    return (w, h, bytes(buf)) if buf is not None else (w, h, addr)


def from_pixels(data, w, h, channels):
    if channels not in (1, 3, 4): raise ValueError("channels must be 1, 3 or 4")
    if not (isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0): raise ValueError(f"bad size {w}x{h}")
    n = w * h * channels
    if isinstance(data, int): raw = ctypes.string_at(data, n)
    else:
        raw = bytes(data)
        if len(raw) != n: raise ValueError(f"from_pixels: need {n} bytes, got {len(raw)}")
    cfd = cfunc(CF, "CFDataCreate", c_void_p, c_void_p, ctypes.c_char_p, c_long)(None, raw, n)
    prov = cfunc(CG, "CGDataProviderCreateWithCFData", c_void_p, c_void_p)(cfd)
    info = {1: 0, 3: 0, 4: 5}[channels]
    img = cfunc(CG, "CGImageCreate", c_void_p, c_size_t, c_size_t, c_size_t, c_size_t, c_size_t, c_void_p, ctypes.c_uint32,
                c_void_p, c_void_p, c_bool, ctypes.c_int)(w, h, 8, 8 * channels, w * channels, _space(channels == 1), info,
                                                         prov, None, False, 0)
    _release(prov); _release(cfd)
    if not img: raise RuntimeError("CGImageCreate failed")
    return img


def encode(img, fmt="png", quality=0.9):
    if fmt not in ("png", "jpeg"): raise ValueError("fmt must be png or jpeg")
    if not 0 <= float(quality) <= 1: raise ValueError("quality must be 0..1")
    out = cfunc(CF, "CFDataCreateMutable", c_void_p, c_void_p, c_long)(None, 0)
    dest = cfunc(IO, "CGImageDestinationCreateWithData", c_void_p, c_void_p, c_void_p, c_size_t, c_void_p)(
        out, nsstring("public.png" if fmt == "png" else "public.jpeg"), 1, None)
    q = cfunc(CF, "CFNumberCreate", c_void_p, c_void_p, ctypes.c_int, c_void_p)(None, 13, ctypes.byref(c_double(float(quality))))
    keys = (c_void_p * 1)(c_void_p.in_dll(framework(IO), "kCGImageDestinationLossyCompressionQuality")); vals = (c_void_p * 1)(q)
    props = cfunc(CF, "CFDictionaryCreate", c_void_p, c_void_p, c_void_p, c_void_p, c_long, c_void_p, c_void_p)(
        None, keys, vals, 1, c_void_p.in_dll(framework(CF), "kCFTypeDictionaryKeyCallBacks"),
        c_void_p.in_dll(framework(CF), "kCFTypeDictionaryValueCallBacks"))
    try:
        cfunc(IO, "CGImageDestinationAddImage", None, c_void_p, c_void_p, c_void_p)(dest, img, props)
        if not cfunc(IO, "CGImageDestinationFinalize", c_bool, c_void_p)(dest): raise RuntimeError("image encode failed")
        n = cfunc(CF, "CFDataGetLength", c_long, c_void_p)(out)
        return ctypes.string_at(cfunc(CF, "CFDataGetBytePtr", c_void_p, c_void_p)(out), n)
    finally:
        for o in (props, q, dest, out):
            if o: _release(o)


def write(img, path):
    p = Path(path); suf = p.suffix.lower()
    fmt = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}.get(suf)
    if not fmt: raise ValueError(f"write: unsupported suffix {suf!r} (png/jpg)")
    p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(encode(img, fmt)); return p
