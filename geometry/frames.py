"""Rigid-transform helpers (3x3 rotations + translations) as plain tuples: rotx/roty/rotz, compose, apply, invert.
The one shared place for "place this part at angle a" maths, so builders (mech/), robots (robotics/) and renderers don't
each carry a private _rotz. Matrices are row-major 3x3 tuples of tuples; vectors are 3-tuples.
Python:
  I3                                identity
  cols(x, y, z) -> R                rotation whose columns are the images of local x, y, z (build a pose from axes)
  rotx(a) / roty(a) / rotz(a)       right-handed rotation by angle a (rad) about the axis
  matmul(A, B) -> A·B               mv(R, v) -> R·v               transpose(R)
  compose(R1, p1, R2, p2) -> (R, p) pose of frame 2 given in frame 1, expressed in frame 1's parent: x -> R1(R2 x + p2) + p1
  apply(R, p, v) -> R·v + p         invert(R, p) -> (R^T, -R^T p)
  frame_from(origin, z_axis=(0,0,1), x_axis=None) -> (R, p)   frame whose z is z_axis, x = x_axis projected ⟂ z
                                    (default +X, or +Y when z is near ±X); ValueError for zero/parallel axes
  axis_to_z(axis) -> R              rotation taking local +Z onto `axis` ("x", "y", "-x", ... or a unit vector)
  exp_so3(w) -> R / log_so3(R) -> w rotation vector <-> matrix (Rodrigues; stable near 0 and π)   skew(w) -> [w]×
Contract: pure functions, no validation beyond axis_to_z (ValueError for an unknown/zero axis). Exact to float64.
Used by: mech/frame3d.py (exp/log), mech/parts.py, mech/wheels.py, robotics/arm.py, robotics/armsim.py, robotics/rover.py, robotics/roversim.py.
Depends on: nothing (math only). physics/linalg has list-based rotx/roty/rotz for the dynamics code (separate layer).
Limits: rotations are assumed orthonormal and never re-orthogonalised (drift accumulates over long compose chains);
  log_so3 uses small-angle series near 0 and a special branch near π; tuples only (no batching or quaternions)."""
import math

I3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def cols(x, y, z):
    """Rotation matrix whose columns are x, y, z (the images of the local axes)."""
    return tuple((x[i], y[i], z[i]) for i in range(3))


def rotx(a):
    c, s = math.cos(a), math.sin(a); return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))


def roty(a):
    c, s = math.cos(a), math.sin(a); return ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))


def rotz(a):
    c, s = math.cos(a), math.sin(a); return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


def matmul(A, B):
    return tuple(tuple(A[i][0] * B[0][j] + A[i][1] * B[1][j] + A[i][2] * B[2][j] for j in range(3)) for i in range(3))


def mv(R, v):
    return tuple(R[i][0] * v[0] + R[i][1] * v[1] + R[i][2] * v[2] for i in range(3))


def transpose(R):
    return tuple(tuple(R[j][i] for j in range(3)) for i in range(3))


def apply(R, p, v):
    x = mv(R, v); return (x[0] + p[0], x[1] + p[1], x[2] + p[2])


def compose(R1, p1, R2, p2):
    return matmul(R1, R2), apply(R1, p1, p2)


def invert(R, p):
    Rt = transpose(R); q = mv(Rt, p); return Rt, (-q[0], -q[1], -q[2])


_AX = {"z": I3, "-z": rotx(math.pi), "x": roty(math.pi / 2), "-x": roty(-math.pi / 2),
       "y": rotx(-math.pi / 2), "-y": rotx(math.pi / 2)}


def axis_to_z(axis):
    """Rotation R with R·(0,0,1) = axis (for parts built along +Z that must point elsewhere)."""
    if isinstance(axis, str):
        if axis not in _AX: raise ValueError(f"unknown axis {axis!r}; use x, y, z, -x, -y, -z or a vector")
        return _AX[axis]
    n = math.sqrt(sum(a * a for a in axis))
    if n < 1e-12: raise ValueError("axis must be non-zero")
    z = tuple(a / n for a in axis)
    if z[2] < -1 + 1e-12: return _AX["-z"]
    # Rodrigues about k = (0,0,1) x z, angle acos(z2)
    kx, ky, c = -z[1], z[0], z[2]; s = math.hypot(kx, ky)
    if s < 1e-12: return I3
    kx, ky = kx / s, ky / s; v = 1 - c
    return ((c + kx * kx * v, kx * ky * v, ky * s), (kx * ky * v, c + ky * ky * v, -kx * s), (-ky * s, kx * s, c))


def frame_from(origin, z_axis=(0.0, 0.0, 1.0), x_axis=None):
    """(R, p) with columns (x, y, z) = an orthonormal frame whose z is `z_axis` (x projected ⟂ z; default any)."""
    from geometry.vec import unit
    z = unit(z_axis, "z axis")
    if x_axis is None: x_axis = (1.0, 0.0, 0.0) if abs(z[0]) < 0.9 else (0.0, 1.0, 0.0)
    xd = sum(a * b for a, b in zip(x_axis, z)); x = unit(tuple(a - xd * b for a, b in zip(x_axis, z)), "x axis (parallel to z?)")
    y = (z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0])
    return tuple((x[i], y[i], z[i]) for i in range(3)), tuple(float(c) for c in origin)


def exp_so3(w):
    """Rotation matrix of rotation vector w (axis × angle, rad) — Rodrigues, accurate near zero."""
    x, y, z = w; th2 = x * x + y * y + z * z; th = math.sqrt(th2)
    if th < 1e-8: a, b = 1.0 - th2 / 6.0, 0.5 - th2 / 24.0
    else: a, b = math.sin(th) / th, (1.0 - math.cos(th)) / th2
    return ((1 - b * (y * y + z * z), -a * z + b * x * y, a * y + b * x * z),
            (a * z + b * x * y, 1 - b * (x * x + z * z), -a * x + b * y * z),
            (-a * y + b * x * z, a * x + b * y * z, 1 - b * (x * x + y * y)))


def log_so3(R):
    """Rotation vector of rotation matrix R (inverse of exp_so3), angle in [0, π]; stable near 0 and π."""
    tr = R[0][0] + R[1][1] + R[2][2]; c = max(-1.0, min(1.0, (tr - 1) / 2))
    v = (R[2][1] - R[1][2], R[0][2] - R[2][0], R[1][0] - R[0][1])
    s = 0.5 * math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]); th = math.atan2(s, c)   # atan2: full precision near 0
    if th < 1e-6: k = 0.5 + th * th / 12.0
    elif math.pi - th > 1e-5: k = th / (2 * s)
    else:                                                     # near π: axis from the symmetric part
        d = [R[i][i] for i in range(3)]; i = max(range(3), key=lambda j: d[j])
        a = [0.0, 0.0, 0.0]; a[i] = math.sqrt(max((d[i] - c) / (1 - c), 0.0))
        for j in range(3):
            if j != i: a[j] = (R[i][j] + R[j][i]) / (2 * (1 - c) * a[i])
        n = math.sqrt(sum(t * t for t in a)); s = 1 if sum(a[j] * v[j] for j in range(3)) >= 0 else -1
        return tuple(s * th * t / n for t in a)
    return (k * v[0], k * v[1], k * v[2])


def skew(w):
    """[w]× so that skew(w)·v = w × v."""
    return ((0.0, -w[2], w[1]), (w[2], 0.0, -w[0]), (-w[1], w[0], 0.0))
