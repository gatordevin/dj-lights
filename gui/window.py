"""Native macOS window with drawing, keyboard and mouse (AppKit via macos/objc.py; no tkinter). 2D or OpenGL.

run_window(title, w, h, on_draw, on_key=None, on_tick=None, fps=60, on_mouse=None, gl=False)
  gl=False: on_draw(g), g = graphics.appkit.AppKitGfx (Gfx interface: rect/oval/poly/line/text/measure/number;
            see graphics/gfx.py). Origin top-left, size fixed at w x h points.
  gl=True:  on_draw(s) with the view's OpenGL context current; s.w, s.h = view size in points, s.pw, s.ph = in pixels
            (Retina backing, use these for glViewport). Double-buffered, 24-bit depth, 4x MSAA, GPU. Use graphics/gl.py.
            The window is resizable and on_draw sees the new size.
  on_key(code, down): macOS virtual keycode, down=True/False. Names in KEY (KEY["space"], KEY["left"], ...).
  on_mouse(e): e.kind in "down" "drag" "up" "scroll" "magnify"; e.x, e.y (points, top-left origin);
            e.dx, e.dy (drag delta in points; y down); e.button 0 left / 1 right / 2 other; e.clicks;
            e.shift e.ctrl e.alt e.cmd (bools); e.scroll (scroll dy, + = away/up), e.precise (trackpad);
            e.magnify (pinch amount, + = zoom in).
  on_tick(): called fps times/s, then the view is redrawn.
  Never returns: closing the window, Cmd-Q, or raising SystemExit/KeyboardInterrupt in any callback ends the
  process (stdout/stderr are flushed first; Python atexit handlers do NOT run). Other exceptions in callbacks are
  printed by ctypes and the app keeps running.
set_title(s): change the window title (e.g. to show status). mouse() -> (x, y) pointer position in view coords
  (top-left origin), or None before a window exists.
KEY: letters a-z, digits "0".."9", space esc enter tab delete, left right up down, minus equal lbracket rbracket comma
  period slash (US layout keycodes).
Limits: one window per process; no text-input/IME; can't be screenshotted here (screencapture fails). Verify 2D
  visuals with graphics.gfx.render() and GL visuals with graphics.gl.offscreen_png().
Used by: games/*.py, gui/viewer3d.py.
Fails: RuntimeError if a window already exists in the process or no OpenGL pixel format is available (gl=True);
  exceptions in callbacks are swallowed by ctypes (only SystemExit/KeyboardInterrupt quit); no argument validation;
  macos.objc raises OSError at import when not on macOS."""
import sys, ctypes, types
from ctypes import c_void_p, c_bool, c_ushort, c_double, c_ulong, c_long, CFUNCTYPE
from macos.objc import cls, msg, sel, nsstring, make_class, CGRect, CGPoint
from graphics.appkit import AppKitGfx

_LETTERS = dict(a=0, s=1, d=2, f=3, h=4, g=5, z=6, x=7, c=8, v=9, b=11, q=12, w=13, e=14, r=15, y=16, t=17,
                o=31, u=32, i=34, p=35, l=37, j=38, k=40, n=45, m=46)
KEY = dict(_LETTERS, space=49, esc=53, enter=36, tab=48, delete=51, left=123, right=124, down=125, up=126,
           minus=27, equal=24, lbracket=33, rbracket=30, comma=43, period=47, slash=44,
           **{str(i): c for i, c in zip(range(10), (29, 18, 19, 20, 21, 23, 22, 26, 28, 25))})
_win = [None, None]  # window, view

def _bounds(v): return msg(v, "bounds", res=CGRect)

def run_window(title, w, h, on_draw, on_key=None, on_tick=None, fps=60, on_mouse=None, gl=False):
    if _win[0]: raise RuntimeError("run_window: only one window per process")
    app = msg(cls("NSApplication"), "sharedApplication")
    view = [None]
    def guard(fn):  # ctypes swallows exceptions in callbacks, so turn SystemExit into a real quit
        def f(*a):
            try: return fn(*a)
            except (SystemExit, KeyboardInterrupt):
                sys.stdout.flush(); sys.stderr.flush(); msg(app, "terminate:", None)
        return f
    def key(down):
        def f(s, c, ev):
            if on_key: on_key(msg(ev, "keyCode", res=c_ushort), down)
        return guard(f)
    def mouse_ev(kind, button):
        def f(s, c, ev):
            if not on_mouse: return
            v = view[0]; p = msg(v, "convertPoint:fromView:", msg(ev, "locationInWindow", res=CGPoint), None, res=CGPoint)
            y = p.y if gl is False else _bounds(v).h - p.y                 # GL view isn't flipped
            flags = msg(ev, "modifierFlags", res=c_ulong)
            e = types.SimpleNamespace(kind=kind, x=p.x, y=y, button=button, dx=0.0, dy=0.0, scroll=0.0, magnify=0.0, precise=False,
                                      clicks=0, shift=bool(flags & 1 << 17), ctrl=bool(flags & 1 << 18), alt=bool(flags & 1 << 19), cmd=bool(flags & 1 << 20))
            if kind in ("down", "drag", "up"): e.clicks = msg(ev, "clickCount", res=c_long)
            if kind == "drag": e.dx, e.dy = msg(ev, "deltaX", res=c_double), msg(ev, "deltaY", res=c_double)
            if kind == "scroll":
                e.scroll = msg(ev, "scrollingDeltaY", res=c_double); e.precise = bool(msg(ev, "hasPreciseScrollingDeltas", res=c_bool))
                e.dx = msg(ev, "scrollingDeltaX", res=c_double)
            if kind == "magnify": e.magnify = msg(ev, "magnification", res=c_double)
            on_mouse(e)
        return guard(f)
    @guard
    def tick(s, c, t):
        if on_tick: on_tick()
        msg(view[0], "setNeedsDisplay:", 1)
    if gl:
        def draw(s, c, r):
            v = view[0]; ctx = msg(v, "openGLContext"); msg(ctx, "makeCurrentContext")
            b = _bounds(v); px = msg(v, "convertRectToBacking:", b, res=CGRect)
            on_draw(types.SimpleNamespace(w=b.w, h=b.h, pw=int(px.w), ph=int(px.h), scale=px.w / max(1, b.w)))
            msg(ctx, "flushBuffer")
    else:
        g = AppKitGfx()
        def draw(s, c, r): on_draw(g)
    B0, V1 = CFUNCTYPE(c_bool, c_void_p, c_void_p), CFUNCTYPE(None, c_void_p, c_void_p, c_void_p)
    methods = {
        "acceptsFirstResponder": (B0, lambda s, c: True, b"c@:"),
        "acceptsFirstMouse:": (CFUNCTYPE(c_bool, c_void_p, c_void_p, c_void_p), lambda s, c, e: True, b"c@:@"),
        "applicationShouldTerminateAfterLastWindowClosed:": (CFUNCTYPE(c_bool, c_void_p, c_void_p, c_void_p), lambda s, c, a: True, b"c@:@"),
        "drawRect:": (CFUNCTYPE(None, c_void_p, c_void_p, CGRect), guard(draw), b"v@:{CGRect=dddd}"),
        "keyDown:": (V1, key(True), b"v@:@"), "keyUp:": (V1, key(False), b"v@:@"),
        "tick:": (V1, tick, b"v@:@"),
        "scrollWheel:": (V1, mouse_ev("scroll", 0), b"v@:@"), "magnifyWithEvent:": (V1, mouse_ev("magnify", 0), b"v@:@")}
    for prefix, button in (("mouse", 0), ("rightMouse", 1), ("otherMouse", 2)):
        for suffix, kind in (("Down", "down"), ("Dragged", "drag"), ("Up", "up")):
            methods[f"{prefix}{suffix}:"] = (V1, mouse_ev(kind, button), b"v@:@")
    if not gl: methods["isFlipped"] = (B0, lambda s, c: True, b"c@:")
    view_cls = make_class("PyGLView" if gl else "PyGfxView", "NSOpenGLView" if gl else "NSView", methods)
    msg(app, "setActivationPolicy:", 0)
    frame = CGRect(0, 0, float(w), float(h))
    if gl:
        attrs = (ctypes.c_uint32 * 16)(5, 73, 12, 24, 8, 24, 55, 1, 56, 4, 59, 0)   # doublebuf, accel, depth24, color24, MSAA x4
        pf = msg(msg(cls("NSOpenGLPixelFormat"), "alloc"), "initWithAttributes:", ctypes.cast(attrs, c_void_p).value)
        if not pf: raise RuntimeError("no OpenGL pixel format (GPU unavailable?)")
        v = msg(msg(view_cls, "alloc"), "initWithFrame:pixelFormat:", frame, pf)
        msg(v, "setWantsBestResolutionOpenGLSurface:", 1)
    else:
        v = msg(msg(view_cls, "alloc"), "initWithFrame:", frame)
    view[0] = v
    win = msg(msg(cls("NSWindow"), "alloc"), "initWithContentRect:styleMask:backing:defer:", frame, 15, 2, 0)
    _win[0], _win[1] = win, v
    msg(win, "setTitle:", nsstring(title))
    msg(win, "setContentView:", v); msg(win, "makeFirstResponder:", v); msg(win, "center")
    msg(app, "setDelegate:", v)
    msg(win, "makeKeyAndOrderFront:", None)
    msg(app, "activateIgnoringOtherApps:", 1)
    msg(cls("NSTimer"), "scheduledTimerWithTimeInterval:target:selector:userInfo:repeats:", 1.0 / fps, v, sel("tick:"), None, 1)
    msg(app, "run")

def set_title(s):
    if _win[0]: msg(_win[0], "setTitle:", nsstring(s))

def mouse():
    if not _win[0]: return None
    v = _win[1]; p = msg(_win[0], "mouseLocationOutsideOfEventStream", res=CGPoint)
    p = msg(v, "convertPoint:fromView:", p, None, res=CGPoint)
    return (p.x, p.y) if msg(v, "isFlipped", res=c_bool) else (p.x, _bounds(v).h - p.y)
