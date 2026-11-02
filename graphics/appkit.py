"""Native Gfx backend (AppKit + CoreGraphics via macos/objc.py). Same interface as graphics/gfx.py.
AppKitGfx() draws into the *current* NSGraphicsContext: inside a window's drawRect (gui/window.py) or
inside render_png(). Origin top-left requires a flipped context (both callers set that up).
bitmap(w, h, path, flipped=True, bg=None, out=None): context manager, an offscreen sRGB bitmap made current for
  AppKit+CG drawing; yields the CGContextRef and writes a PNG on exit (path=None: no file; out dict gets raw RGB). The building block for any native offscreen render
  (used by render_png and documents/pdf.py render_page).
overlay(rgb, w, h, on_draw) -> RGB bytes: draw Gfx (labels, captions, arrows) over existing raw RGB pixels (e.g. a 3D
  render or video frame); same size out. Used by cad/animate.py captions.
encode(rgb, w, h, fmt="jpeg"|"png", quality=0.7) -> bytes: raw RGB → JPEG/PNG with the native encoder (live streams).
draw_cgimage(ctx, img, x, y, w, h): draw a CGImage upright into a flipped bitmap() context (top-left x, y).
render_png(on_draw, w, h, path, scale=1): Gfx drawing into bitmap() → PNG (scale=2: same drawing at 2x pixels).
render_pdf(on_draw, w, h, path): the same Gfx drawing as a one-page vector PDF (w x h points).
pdf_document(path, w, h): context manager for multi-page vector PDFs: yields a PdfDoc with .page() (a context manager
  yielding (AppKitGfx, CGContextRef) with a flipped, top-left origin) and .link(url, x, y, w, h) for clickable areas.
  Used by documents/pdfwrite.py. No window or screen access needed
(works where `screencapture` fails). ~0.05 s per 800x500 frame. Prefer graphics.gfx.render() to call it.
Colors are sRGB: render_png output is exact (1,0,0 → 255,0,0); edges/text are anti-aliased.
Limits: macOS only (import raises OSError elsewhere); no alpha in output.
Fails: ValueError for bad bitmap/PDF size or encode() arguments (rgb length, fmt, quality); FileNotFoundError from image() for
  an unreadable file; RuntimeError if CoreGraphics cannot create the bitmap/PDF or encode; drawing calls do not validate.
"""
import ctypes, contextlib
from ctypes import c_void_p, c_double
from macos.objc import cls, msg, nsstring, appkit, cg, CGRect, CGSize, CGPoint, autorelease_pool
from .gfx import GfxBase
from .png import write_png

appkit.NSRectFill.argtypes = [CGRect]
appkit.NSRectFillUsingOperation.argtypes = [CGRect, ctypes.c_ulong]
cg.CGColorSpaceCreateWithName.restype = c_void_p; cg.CGColorSpaceCreateWithName.argtypes = [c_void_p]
cg.CGBitmapContextCreate.restype = c_void_p
cg.CGBitmapContextCreate.argtypes = [c_void_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, c_void_p, ctypes.c_uint32]
cg.CGContextTranslateCTM.argtypes = cg.CGContextScaleCTM.argtypes = [c_void_p, c_double, c_double]
cg.CGContextRelease.argtypes = [c_void_p]
cg.CGContextSetRGBFillColor.argtypes = [c_void_p, c_double, c_double, c_double, c_double]
cg.CGContextFillRect.argtypes = [c_void_p, CGRect]

def _nscolor(c):
    return msg(cls("NSColor"), "colorWithSRGBRed:green:blue:alpha:",
               *(float(max(0, min(1, v))) for v in c[:3]), float(c[3]) if len(c) > 3 else 1.0)

class AppKitGfx(GfxBase):
    def rect(self, x, y, w, h, color):
        msg(_nscolor(color), "set")
        if len(color) > 3: appkit.NSRectFillUsingOperation(CGRect(x, y, w, h), 2)   # source-over for alpha
        else: appkit.NSRectFill(CGRect(x, y, w, h))
    def oval(self, cx, cy, r, color):
        msg(_nscolor(color), "set")
        msg(msg(cls("NSBezierPath"), "bezierPathWithOvalInRect:", CGRect(cx - r, cy - r, 2 * r, 2 * r)), "fill")
    def _path(self, pts):
        p = msg(cls("NSBezierPath"), "bezierPath")
        msg(p, "moveToPoint:", CGPoint(*pts[0]))
        for q in pts[1:]: msg(p, "lineToPoint:", CGPoint(*q))
        return p
    def poly(self, pts, color):
        if len(pts) < 3: return
        p = self._path([(float(x), float(y)) for x, y in pts]); msg(p, "closePath"); msg(_nscolor(color), "set"); msg(p, "fill")
    def line(self, x0, y0, x1, y1, color, width=1):
        p = self._path([(float(x0), float(y0)), (float(x1), float(y1))])
        msg(p, "setLineWidth:", float(width)); msg(_nscolor(color), "set"); msg(p, "stroke")
    def polyline(self, pts, color, width=1):
        if len(pts) < 2: return
        p = self._path([(float(x), float(y)) for x, y in pts])
        msg(p, "setLineWidth:", float(width)); msg(p, "setLineJoinStyle:", 1); msg(p, "setLineCapStyle:", 1)
        msg(_nscolor(color), "set"); msg(p, "stroke")
    def frame(self, x, y, w, h, color, width=1):
        p = msg(cls("NSBezierPath"), "bezierPathWithRect:", CGRect(x, y, w, h))
        msg(p, "setLineWidth:", float(width)); msg(_nscolor(color), "set"); msg(p, "stroke")
    def image(self, path, x, y, w, h):
        img = msg(msg(cls("NSImage"), "alloc"), "initWithContentsOfFile:", nsstring(str(path)))
        if not img: raise FileNotFoundError(f"image: cannot load {path}")
        msg(img, "drawInRect:fromRect:operation:fraction:respectFlipped:hints:", CGRect(x, y, w, h), CGRect(0, 0, 0, 0),
            2, 1.0, ctypes.c_bool(True), None)
        msg(img, "release")
    def _attrs(self, size, color=(1, 1, 1), bold=True):
        a = msg(cls("NSMutableDictionary"), "dictionary")
        font = msg(cls("NSFont"), "boldSystemFontOfSize:" if bold else "systemFontOfSize:", float(size))
        msg(a, "setObject:forKey:", font, c_void_p.in_dll(appkit, "NSFontAttributeName"))
        msg(a, "setObject:forKey:", _nscolor(color), c_void_p.in_dll(appkit, "NSForegroundColorAttributeName"))
        return a
    def measure(self, s, size=16, bold=True):
        sz = msg(nsstring(str(s)), "sizeWithAttributes:", self._attrs(size, bold=bold), res=CGSize)
        return sz.w, sz.h
    def text(self, s, x, y, size=16, color=(1, 1, 1), center=False, bold=True):
        a, ns = self._attrs(size, color, bold), nsstring(str(s))
        if center:
            sz = msg(ns, "sizeWithAttributes:", a, res=CGSize); x, y = x - sz.w / 2, y - sz.h / 2
        msg(ns, "drawInRect:withAttributes:", CGRect(x, y, 1e5, size * 2.0), a)

_SRGB = []
def _srgb():          # one shared colour space (CGColorSpaceCreate* returns +1; creating one per frame leaked)
    if not _SRGB: _SRGB.append(cg.CGColorSpaceCreateWithName(c_void_p.in_dll(cg, "kCGColorSpaceSRGB")))
    return _SRGB[0]

@contextlib.contextmanager
def bitmap(w, h, path, flipped=True, bg=None, out=None, scale=1):
    """Offscreen sRGB w x h bitmap, current for both AppKit and CoreGraphics drawing; written to PNG `path` on exit.
    Yields the CGContextRef (for raw CG calls). flipped=True: origin top-left (Gfx convention); False: native
    bottom-left (PDF/CG convention). bg: optional (r,g,b) 0..1 fill first (otherwise black).
    path=None skips the PNG; out={} receives {"rgb": bytes (w*h*3), "w", "h"} after the block (in-memory frames)."""
    if not (isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0): raise ValueError(f"bitmap size {w!r}x{h!r}")
    buf = (ctypes.c_ubyte * (w * h * 4))()
    ctx = cg.CGBitmapContextCreate(buf, w, h, 8, w * 4, _srgb(), 5)  # RGBX, sRGB so colors are exact
    if not ctx: raise RuntimeError("CGBitmapContextCreate failed")
    if bg: cg.CGContextSetRGBFillColor(ctx, *(float(v) for v in bg[:3]), 1.0); cg.CGContextFillRect(ctx, CGRect(0, 0, w, h))
    if flipped: cg.CGContextTranslateCTM(ctx, 0.0, float(h)); cg.CGContextScaleCTM(ctx, 1.0, -1.0)
    if scale != 1: cg.CGContextScaleCTM(ctx, float(scale), float(scale))
    NSGC = cls("NSGraphicsContext")
    pool = autorelease_pool().__enter__()      # drawing autoreleases contexts + image caches: free them per bitmap
    msg(NSGC, "saveGraphicsState")
    try:
        msg(NSGC, "setCurrentContext:", msg(NSGC, "graphicsContextWithCGContext:flipped:", ctx, int(flipped)))
        yield ctx
    finally:
        msg(NSGC, "restoreGraphicsState"); cg.CGContextRelease(ctx); pool.__exit__()
    rgbx, rgb = bytes(buf), bytearray(w * h * 3)
    for i in range(3): rgb[i::3] = rgbx[i::4]
    if out is not None: out["rgb"], out["w"], out["h"] = bytes(rgb), w, h
    if path: write_png(path, w, h, bytes(rgb))

def draw_cgimage(ctx, img, x, y, w, h):
    """Draw a CGImageRef into a FLIPPED (top-left origin) bitmap context at x, y (top-left corner), w x h — un-flipping
    locally so the image is upright. The one home for this; media/avf contact sheets and media/motion use it."""
    cg.CGContextSaveGState.argtypes = cg.CGContextRestoreGState.argtypes = [c_void_p]
    cg.CGContextDrawImage.argtypes = [c_void_p, CGRect, c_void_p]
    cg.CGContextSaveGState(ctx); cg.CGContextTranslateCTM(ctx, float(x), float(y + h)); cg.CGContextScaleCTM(ctx, 1.0, -1.0)
    cg.CGContextDrawImage(ctx, CGRect(0, 0, float(w), float(h)), img); cg.CGContextRestoreGState(ctx)

def render_png(on_draw, w, h, path, scale=1):
    with bitmap(int(round(w * scale)), int(round(h * scale)), path, scale=scale):
        on_draw(AppKitGfx())

# ---- vector PDF output (CoreGraphics PDF context) ----
cg.CGPDFContextCreateWithURL.restype = c_void_p
cg.CGPDFContextCreateWithURL.argtypes = [c_void_p, ctypes.POINTER(CGRect), c_void_p]
cg.CGPDFContextBeginPage.argtypes = [c_void_p, c_void_p]
cg.CGPDFContextEndPage.argtypes = cg.CGPDFContextClose.argtypes = [c_void_p]
cg.CGPDFContextSetURLForRect.argtypes = [c_void_p, c_void_p, CGRect]
cg.CGContextSaveGState.argtypes = cg.CGContextRestoreGState.argtypes = [c_void_p]

def _fileurl(path):
    return msg(cls("NSURL"), "fileURLWithPath:", nsstring(str(path)))

class PdfDoc:
    """Multi-page vector PDF being written (see pdf_document)."""
    def __init__(self, ctx, w, h): self.ctx, self.w, self.h, self.pages = ctx, w, h, 0
    @contextlib.contextmanager
    def page(self):
        cg.CGPDFContextBeginPage(self.ctx, None); self.pages += 1
        cg.CGContextSaveGState(self.ctx)
        cg.CGContextTranslateCTM(self.ctx, 0.0, float(self.h)); cg.CGContextScaleCTM(self.ctx, 1.0, -1.0)
        NSGC = cls("NSGraphicsContext"); msg(NSGC, "saveGraphicsState")
        try:
            msg(NSGC, "setCurrentContext:", msg(NSGC, "graphicsContextWithCGContext:flipped:", self.ctx, 1))
            yield AppKitGfx(), self.ctx
        finally:
            msg(NSGC, "restoreGraphicsState"); cg.CGContextRestoreGState(self.ctx); cg.CGPDFContextEndPage(self.ctx)
    def link(self, url, x, y, w, h):
        """Make the box (top-left coords of the current page) a clickable link to url."""
        u = msg(cls("NSURL"), "URLWithString:", nsstring(url))
        if u: cg.CGPDFContextSetURLForRect(self.ctx, u, CGRect(x, self.h - y - h, w, h))

@contextlib.contextmanager
def pdf_document(path, w, h):
    """Context manager: a vector PDF of w x h point pages at `path` (parent dirs created). Yields a PdfDoc."""
    if not (w > 0 and h > 0): raise ValueError(f"pdf page size {w!r}x{h!r}")
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    box = CGRect(0, 0, float(w), float(h))
    ctx = cg.CGPDFContextCreateWithURL(_fileurl(Path(path).resolve()), ctypes.byref(box), None)
    if not ctx: raise RuntimeError(f"cannot create PDF at {path}")
    doc = PdfDoc(ctx, w, h)
    try: yield doc
    finally: cg.CGPDFContextClose(ctx); cg.CGContextRelease(ctx)

def render_pdf(on_draw, w, h, path):
    with pdf_document(path, w, h) as doc, doc.page() as (g, _):
        on_draw(g)


def overlay(rgb, w, h, on_draw):
    """Raw RGB (w*h*3, top row first) -> new RGB with on_draw(AppKitGfx()) drawn on top (top-left origin)."""
    if len(rgb) != w * h * 3: raise ValueError("rgb must be w*h*3 bytes")
    from macos import cgimage
    img = cgimage.from_pixels(rgb, w, h, 3); out = {}
    try:
        with bitmap(w, h, None, out=out) as ctx:
            draw_cgimage(ctx, img, 0, 0, w, h); on_draw(AppKitGfx())
    finally: cgimage.release(img)
    return out["rgb"]


def encode(rgb, w, h, fmt="jpeg", quality=0.7):
    """Raw RGB bytes (w*h*3) → JPEG or PNG bytes with the native encoder (ImageIO; ~5 ms for 1280x800 JPEG), via
    macos.cgimage (every CF object released explicitly — the old NSBitmapImageRep version leaked a frame per call)."""
    if len(rgb) != w * h * 3: raise ValueError("rgb must be w*h*3 bytes")
    if fmt not in ("jpeg", "png"): raise ValueError("fmt must be jpeg or png")
    if not 0 <= float(quality) <= 1: raise ValueError("quality must be 0..1")
    from macos import cgimage
    img = cgimage.from_pixels(rgb, w, h, 3)
    try: return cgimage.encode(img, fmt, quality)
    finally: cgimage.release(img)
