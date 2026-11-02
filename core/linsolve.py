"""Small dense linear solves: Gaussian elimination with partial pivoting for square systems of plain Python lists
(least-squares normal equations, Newton steps of small implicit integrators, sketch constraints, thermal networks).
Python:
  solve(A, b, tiny=1e-300) -> x       A: n rows of n floats, b: n floats; A and b are not modified
  SingularMatrix                       raised when a pivot magnitude <= tiny (subclass of ValueError and ZeroDivisionError,
                                       so callers catching either keep working)
Contract: A square n x n, len(b) == n, n >= 1 (ValueError otherwise). Double precision; fsum back-substitution.
Limits: O(n^3) pure Python — fine to ~200 unknowns. Large sparse systems: geometry.sparse; float64 LAPACK: ml.lapack;
  symmetric positive-definite: physics.linalg.solve_spd.
Used by: physics/linalg.py (solve), geometry/sketch.py, mech/thermal.py, mech/hyperelastic.py, mech/pneumatics.py.
Depends on: nothing."""
import math


class SingularMatrix(ValueError, ZeroDivisionError):
    pass


def solve(A, b, tiny=1e-300):
    n = len(b)
    if n < 1 or len(A) != n or any(len(r) != n for r in A): raise ValueError(f"solve: need a square {n}x{n} system")
    M = [list(map(float, row)) + [float(b[i])] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) <= tiny: raise SingularMatrix(f"singular matrix (pivot {M[p][c]:.3g} in column {c})")
        M[c], M[p] = M[p], M[c]; Mc = M[c]; piv = Mc[c]
        for r in range(c + 1, n):
            f = M[r][c] / piv
            if f:
                Mr = M[r]
                for k in range(c, n + 1): Mr[k] -= f * Mc[k]
    x = [0.0] * n
    for i in range(n - 1, -1, -1): x[i] = (M[i][n] - math.fsum(M[i][k] * x[k] for k in range(i + 1, n))) / M[i][i]
    return x
