"""Gfx: the one drawing interface for apps and games, plus the one offscreen entry point render().

Interface (every backend; origin top-left, y down, colors (r,g,b) or (r,g,b,a) floats 0..1, values clamped;
alpha is honoured by appkit and svg, ignored by canvas):
  g.rect(x, y, w, h, color)                       filled rectangle
  g.oval(cx, cy, r, color)                        filled circle
  g.poly(pts, color)                              filled polygon, pts = [(x,y), ...] (>= 3 points)
  g.line(x0, y0, x1, y1, color, width=1)          straight line (butt caps)
  g.polyline(pts, color, width=1)                 open stroked path (round joins on appkit/svg)
  g.frame(x, y, w, h, color, width=1)             rectangle outline
  g.text(s, x, y, size=16, color=(1,1,1), center=False, bold=True)
        (x,y) = top-left of the text box, or its centre if center=True. Box height ~1.2*size. bold=False = regular.
  g.measure(s, size=16, bold=True) -> (w, h)      text box size, for layout
  g.fit(s, size, width, bold=True) -> str         s, or s shortened with "…" so it fits `width` (labels, table cells)
  g.image(path, x, y, w, h)                       draw a PNG/JPEG file scaled into the box (canvas: PNG only, nearest)
  g.number(n, x, y, size=10, color=(1,1,1))       seven-segment integer (incl. negative), built on rect
Backends:
  graphics/appkit.py AppKitGfx — native (AppKit/CoreGraphics): anti-aliased, real system font, fast.
      Used inside native windows (gui/window.py) and by render(backend="appkit").
  CanvasGfx (here) — pure Python on graphics/canvas.Canvas: no AA, 5x7 bitmap font (A-Z 0-9 basic
      punctuation; lowercase drawn as uppercase; unknown glyphs as a box), ~0.1 s per 800x600 frame.
      Works anywhere; text widths differ from native (layout parity is approximate).
  graphics/svg.py SvgGfx — vector SVG text output (exact text widths when AppKit is available).
Colour helpers: mix(a, b, t) linear blend of two colours · hex_color(c) -> "#rrggbb" (clamped; for SVG/HTML) ·
  ramp(t) scalar 0..1 -> heat-map colour (blue → red).
Figure: mixin for drawable objects with draw(g, x, y, w, h) (Chart, Gantt, ...): .save(path, w=None, h=None, scale=2)
  renders to .png/.svg/.pdf at the class's default `size` and returns the Path.
render(on_draw, w, h, path, backend="auto", scale=1) -> backend name used.
  Calls on_draw(g) on a fresh w x h surface and writes the file. The suffix picks the format: .png (raster; "auto" =
  appkit if it loads, else canvas; scale=2 gives a 2x-resolution PNG of the same drawing), .svg (svg backend),
  .pdf (vector, one page, via appkit). Same drawing code → all three formats.
Limits: canvas backend is pure Python (no AA, 5x7 font); render() scale must be in (0, 8]; PDF output needs the appkit
  backend; svg/canvas text metrics only approximate native; drawing calls are not validated beyond render()'s arguments.
Fails: ValueError on bad size/backend; exceptions from on_draw propagate.
Used by: gui/window.py, games/harness.py, graphics/chart.py, documents/, tests."""
from .canvas import Canvas

class GfxBase:
    """Subclasses implement rect, oval, poly, text, measure. number() and line() (via poly) are shared."""
    _SEG = {"0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg", "5": "acdfg", "6": "acdefg",
            "7": "abc", "8": "abcdefg", "9": "abcdfg", "-": "g"}
    def number(self, n, x, y, size=10, color=(1, 1, 1)):
        s, t = size, max(2, size * 2 // 5)
        r = {"a": (0, 0, 3*s+t, t), "b": (3*s, 0, t, 3*s+t), "c": (3*s, 3*s, t, 3*s+t), "d": (0, 6*s, 3*s+t, t),
             "e": (0, 3*s, t, 3*s+t), "f": (0, 0, t, 3*s+t), "g": (0, 3*s, 3*s+t, t)}
        for i, ch in enumerate(str(int(n))):
            ox = x + i * (3 * s + t + s)
            for sg in self._SEG[ch]:
                a, b, c, d = r[sg]; self.rect(ox + a, y + b, c, d, color)
    def line(self, x0, y0, x1, y1, color, width=1):
        """Default: a thin quad via poly(). Backends may override with a native stroke."""
        dx, dy = x1 - x0, y1 - y0; n = (dx * dx + dy * dy) ** 0.5
        if n == 0: return
        ox, oy = -dy / n * width / 2, dx / n * width / 2
        self.poly([(x0 + ox, y0 + oy), (x1 + ox, y1 + oy), (x1 - ox, y1 - oy), (x0 - ox, y0 - oy)], color)
    def fit(self, s, size, width, bold=True):
        s = str(s)
        if self.measure(s, size, bold)[0] <= width: return s
        while s and self.measure(s + "…", size, bold)[0] > width: s = s[:-1]
        return s + "…"
    def polyline(self, pts, color, width=1):
        for a, b in zip(pts, pts[1:]): self.line(a[0], a[1], b[0], b[1], color, width)
    def frame(self, x, y, w, h, color, width=1):
        self.polyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)], color, width)
    def rect(self, x, y, w, h, color): raise NotImplementedError
    def poly(self, pts, color): raise NotImplementedError
    def oval(self, cx, cy, r, color): raise NotImplementedError
    def text(self, s, x, y, size=16, color=(1, 1, 1), center=False, bold=True): raise NotImplementedError
    def measure(self, s, size=16, bold=True): raise NotImplementedError
    def image(self, path, x, y, w, h): raise NotImplementedError

def _rgb(c): return tuple(max(0, min(255, int(round(v * 255)))) for v in c[:3])

# 5x7 font: 7 rows of 5 bits (bit 4 = leftmost), 2 hex digits per row.
def _glyphs(spec):  # "CH HEX HEX..." entries separated by "|"; the space glyph is "_"
    out = {}
    for e in spec.replace("\n", "|").strip("|").split("|"):
        ch, hexrows = e.split(" ", 1)
        out[" " if ch == "_" else ch] = bytes.fromhex(hexrows.replace(" ", ""))
    return out
_FONT = _glyphs("""
0 0E 11 13 15 19 11 0E|1 04 0C 04 04 04 04 0E|2 0E 11 01 02 04 08 1F|3 1F 02 04 02 01 11 0E|4 02 06 0A 12 1F 02 02
5 1F 10 1E 01 01 11 0E|6 06 08 10 1E 11 11 0E|7 1F 01 02 04 08 08 08|8 0E 11 11 0E 11 11 0E|9 0E 11 11 0F 01 02 0C
A 0E 11 11 1F 11 11 11|B 1E 11 11 1E 11 11 1E|C 0E 11 10 10 10 11 0E|D 1C 12 11 11 11 12 1C|E 1F 10 10 1E 10 10 1F
F 1F 10 10 1E 10 10 10|G 0E 11 10 17 11 11 0F|H 11 11 11 1F 11 11 11|I 0E 04 04 04 04 04 0E|J 07 02 02 02 02 12 0C
K 11 12 14 18 14 12 11|L 10 10 10 10 10 10 1F|M 11 1B 15 15 11 11 11|N 11 11 19 15 13 11 11|O 0E 11 11 11 11 11 0E
P 1E 11 11 1E 10 10 10|Q 0E 11 11 11 15 12 0D|R 1E 11 11 1E 14 12 11|S 0F 10 10 0E 01 01 1E|T 1F 04 04 04 04 04 04
U 11 11 11 11 11 11 0E|V 11 11 11 11 11 0A 04|W 11 11 11 15 15 15 0A|X 11 11 0A 04 0A 11 11|Y 11 11 11 0A 04 04 04
Z 1F 01 02 04 08 10 1F|. 00 00 00 00 00 0C 0C|, 00 00 00 00 0C 04 08|: 00 0C 0C 00 0C 0C 00|! 04 04 04 04 04 00 04
? 0E 11 01 02 04 00 04|- 00 00 00 1F 00 00 00|— 00 00 00 1F 00 00 00|/ 01 01 02 04 08 10 10|( 02 04 08 08 08 04 02
) 08 04 02 02 02 04 08|' 04 04 08 00 00 00 00|· 00 00 00 04 00 00 00|= 00 00 1F 00 1F 00 00|+ 00 04 04 1F 04 04 00
% 18 19 02 04 08 13 03|_ 00 00 00 00 00 00 00""")
_BOX = bytes.fromhex("1F11111111111F")

class CanvasGfx(GfxBase):
    """Pure-Python backend. .canvas is the underlying Canvas; .save_png(path) writes it."""
    def __init__(self, w, h):
        self.canvas = Canvas(int(w), int(h))
    def rect(self, x, y, w, h, color):
        self.canvas.rect(x, y, x + w, y + h, _rgb(color))
    def oval(self, cx, cy, r, color):
        self.canvas.circle(cx, cy, r, _rgb(color))
    def poly(self, pts, color):
        self.canvas.polygon([(float(x), float(y)) for x, y in pts], _rgb(color))
    def _px(self, size): return max(1, round(size / 10))
    def measure(self, s, size=16, bold=True):
        return max(0, len(str(s)) * 6 - 1) * self._px(size), size * 1.2
    def image(self, path, x, y, w, h):
        from .png import read_png
        iw, ih, rgb = read_png(path)
        for j in range(int(h)):
            sy = min(ih - 1, int(j * ih / h))
            for i in range(int(w)):
                sx = min(iw - 1, int(i * iw / w)); k = 3 * (sy * iw + sx)
                self.canvas.set(int(x) + i, int(y) + j, (rgb[k], rgb[k + 1], rgb[k + 2]))
    def text(self, s, x, y, size=16, color=(1, 1, 1), center=False, bold=True):
        s, p = str(s), self._px(size)
        w, h = self.measure(s, size)
        if center: x, y = x - w / 2, y - h / 2
        y += (h - 7 * p) / 2                     # vertically centre the glyphs in the box
        for i, ch in enumerate(s.upper()):
            rows = _FONT.get(ch, _BOX)
            for r, bits in enumerate(rows):
                for c in range(5):
                    if bits >> (4 - c) & 1: self.rect(x + (i * 6 + c) * p, y + r * p, p, p, color)
    def save_png(self, path):
        self.canvas.save_png(path)

BACKENDS = ("auto", "appkit", "canvas")


def mix(a, b, t): return tuple(x * (1 - t) + y * t for x, y in zip(a, b))


def hex_color(c): return "#%02x%02x%02x" % tuple(max(0, min(255, round(v * 255))) for v in c[:3])


_RAMP = ((0.19, 0.07, 0.23), (0.27, 0.39, 0.92), (0.11, 0.81, 0.83), (0.38, 0.99, 0.42), (0.95, 0.80, 0.23),
         (0.98, 0.42, 0.11), (0.48, 0.02, 0.01))


def ramp(t):
    """Scalar 0..1 -> perceptual 'turbo'-like colour (dark blue → cyan → green → yellow → red); t is clamped.
    For heat maps and stress plots (cad.flex.draw)."""
    t = 0.0 if t != t else max(0.0, min(1.0, float(t))); x = t * (len(_RAMP) - 1); i = min(int(x), len(_RAMP) - 2)
    return mix(_RAMP[i], _RAMP[i + 1], x - i)


class Figure:
    """Mixin: anything with draw(g, x, y, w, h) gets save(path) in every render format."""
    size = (800, 450)

    def save(self, path, w=None, h=None, scale=2):
        from pathlib import Path
        p = Path(path); p.parent.mkdir(parents=True, exist_ok=True); w = w or self.size[0]; h = h or self.size[1]
        render(lambda g: self.draw(g, 0, 0, w, h), w, h, str(p), scale=scale if p.suffix.lower() == ".png" else 1)
        return p

def render(on_draw, w, h, path, backend="auto", scale=1):
    if not (isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0):
        raise ValueError(f"render: w, h must be positive ints, got {w!r}, {h!r}")
    if backend not in BACKENDS: raise ValueError(f"render: backend must be one of {BACKENDS}")
    if not (isinstance(scale, (int, float)) and 0 < scale <= 8): raise ValueError(f"render: scale must be in (0, 8], got {scale!r}")
    suffix = str(path).lower().rsplit(".", 1)[-1]
    if suffix == "svg":
        from .svg import render_svg
        render_svg(on_draw, w, h, path); return "svg"
    if suffix == "pdf":
        from . import appkit
        appkit.render_pdf(on_draw, w, h, path); return "pdf"
    if backend in ("auto", "appkit"):
        try:
            from . import appkit
        except OSError:
            if backend == "appkit": raise
        else:
            appkit.render_png(on_draw, w, h, path, scale); return "appkit"
    g = CanvasGfx(w, h); on_draw(g); g.save_png(path); return "canvas"
