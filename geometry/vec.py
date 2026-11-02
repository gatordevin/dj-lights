"""Vector maths on plain tuples: 3D sub/add/scale/dot/cross/unit, orthonormal bases, 2D orientation + polygon area, N-dim
dot — the ONE home for these helpers (don't re-define `_cross`/`_unit` in your module; `meta/audit.py` flags copies).
Python (3-vectors are any length-3 sequences; results are tuples):
  sub(a, b) · add(a, b) · scale(a, k) · dot(a, b) · cross(a, b) · length(a) · lerp(a, b, t)
  norm(v)  -> unit vector, zero-safe: returns (0, 0, 1) for a (near-)zero vector (rendering/normals)
  unit(v, what="vector") -> unit vector, strict: ValueError "<what> must be non-zero" (inputs from callers)
  basis(axis) -> (u, v, w) right-handed orthonormal frame with w = unit(axis) (u ⟂ w chosen deterministically)
  newell(points) -> unit normal of a planar polygon (Newell's method, robust for non-convex)
  cross2(o, a, b) -> z of (a - o) × (b - o): > 0 when o→a→b turns counter-clockwise (2D orientation test)
  poly_area(poly) -> signed shoelace area of a 2D polygon (CCW > 0)
  dotn(a, b) -> exactly-rounded N-dim dot product (math.fsum)
Contract: pure functions, no state; only unit()/basis() validate (ValueError). float64.
Used by: geometry (mesh re-exports, planar, triangulate, sweep, sketch, collide, hull), physics/linalg, graphics (gl, sky),
  world/flora, cad (model, paths), robotics/grasp. Depends on: math only (tier 0).
Limits: tuples/lists of floats only (no numpy, no batching); norm() silently returns (0, 0, 1) for degenerate vectors;
  poly_area/cross2 are 2D only; no tolerance handling beyond the 1e-15 length cut-offs."""
import math


def sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def add(a, b): return (a[0] + b[0], a[1] + b[1], a[2] + b[2])
def scale(a, k): return (a[0] * k, a[1] * k, a[2] * k)
def dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def cross(a, b): return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
def length(a): return math.sqrt(dot(a, a))
def lerp(a, b, t): return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def norm(v):
    n = math.sqrt(dot(v, v)); return (v[0] / n, v[1] / n, v[2] / n) if n > 1e-15 else (0.0, 0.0, 1.0)


def unit(v, what="vector"):
    if len(v) != 3: raise ValueError(f"{what} must be a 3-vector")
    n = math.sqrt(dot(v, v))
    if n < 1e-15: raise ValueError(f"{what} must be non-zero")
    return (v[0] / n, v[1] / n, v[2] / n)


def basis(axis):
    w = unit(axis, "axis"); helper = (1.0, 0.0, 0.0) if abs(w[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = norm(cross(w, helper)); return u, cross(w, u), w


def vector_area(pts):
    """Newell vector area of a closed 3D polygon: its length is the (projected) area, folds cancel."""
    nx = ny = nz = 0.0
    for i in range(len(pts)):
        a, b = pts[i - 1], pts[i]
        nx += (a[1] - b[1]) * (a[2] + b[2]); ny += (a[2] - b[2]) * (a[0] + b[0]); nz += (a[0] - b[0]) * (a[1] + b[1])
    return (nx / 2, ny / 2, nz / 2)


def newell(pts): return norm(vector_area(pts))


def cross2(o, a, b): return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def poly_area(poly): return 0.5 * sum(poly[i - 1][0] * poly[i][1] - poly[i][0] * poly[i - 1][1] for i in range(len(poly)))


def dotn(a, b): return math.fsum(x * y for x, y in zip(a, b))
