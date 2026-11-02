"""GPU window apps with an orbit camera: the shared boilerplate of 3D simulators (window, mouse orbit, title, snapshots).
A GL app is just on_draw(s) + optional on_key/tick/status; this module opens it in a native window (gui.window) or
renders one frame of it offscreen through the very same on_draw, so what a test or a PNG shows is what the window shows.
Python:
  surface(pw, ph) -> s                 the on_draw argument of gui.window GL views (s.w, s.h, s.pw, s.ph, s.scale)
  snapshot(on_draw, w, h, path) -> path    one frame offscreen (graphics.gl.offscreen_png, MSAA) — no window/screen needed
  orbit_mouse(cam) -> on_mouse(e)      left-drag orbit · right/shift-drag pan · scroll/pinch zoom for a graphics.gl.OrbitCamera
  run(title, w, h, on_draw, on_key=None, tick=None, status=None, cam=None, on_mouse=None, fps=60)
      opens the window and never returns. status() -> str is shown in the title bar after every key and tick.
      on_key(code, down) may raise SystemExit to quit (gui.window turns it into a clean app exit).
Contract: on_draw(s) must issue GL calls only (a context is current). w, h > 0.
Used by: robotics/armsim.py, robotics/roversim.py, games/harness.py (GL games), cad/explorer.py. Depends on: gui.window, graphics.gl.
Limits: macOS + OpenGL 2.1 (GPU) only; run() opens a native window and never returns (one window per process, via
  gui.window); snapshot() needs a GPU-capable CGL context but no screen. Wrong-sized/invalid arguments are not checked beyond w, h > 0."""
import sys, math, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def surface(pw, ph):
    return types.SimpleNamespace(w=pw, h=ph, pw=pw, ph=ph, scale=1.0)


def snapshot(on_draw, w, h, path):
    if w <= 0 or h <= 0: raise ValueError("snapshot size must be positive")
    from graphics.gl import offscreen_png
    return offscreen_png(lambda pw, ph: on_draw(surface(pw, ph)), int(w), int(h), str(path))


def orbit_mouse(cam):
    def on_mouse(e):
        if e.kind == "drag":
            if e.button == 1 or e.shift: cam.pan(e.dx, e.dy, 700)          # 1 = right button
            else: cam.rotate(e.dx, e.dy)
        elif e.kind == "scroll": cam.zoom(math.exp(max(-3.0, min(3.0, 0.1 * e.scroll))))     # always > 0: big trackpad flicks used to give 0/negative
        elif e.kind == "magnify": cam.zoom(math.exp(max(-3.0, min(3.0, e.magnify))))
    return on_mouse


def run(title, w, h, on_draw, on_key=None, tick=None, status=None, cam=None, on_mouse=None, fps=60):
    import gui.window as W
    def retitle():
        if status: W.set_title(status())
    def key(code, down):
        if on_key: on_key(code, down)
        retitle()
    def ticker():
        if tick: tick()
        retitle()
    mouse = on_mouse or (orbit_mouse(cam) if cam is not None else None)
    retitle()
    W.run_window(title, w, h, on_draw, on_key=key, on_tick=ticker, on_mouse=mouse, fps=fps, gl=True)
