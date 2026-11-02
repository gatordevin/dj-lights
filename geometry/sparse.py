"""Sparse symmetric linear solver (skyline LDLᵀ with RCM ordering) and lowest generalized eigenpairs, pure Python.
For finite-element / frame stiffness matrices with a few hundred to a few thousand unknowns, where dense LU
(physics.linalg.solve, O(n³)) is too slow: cost is O(n·b²) with b the profile bandwidth after reordering.
Python:
  K = SymSparse(n)              K.add(i, j, v)   accumulate (either triangle; stored once, symmetric)
  K.get(i, j) -> float          K.matvec(x) -> list        K.copy()      K.n
  K.submatrix(keep) -> SymSparse    rows/cols `keep` (list of indices, in that order)
  f = K.factor(shift=0.0) -> LDL    factorise K - shift·I (or K - shift·diag(M) via factor(shift, mdiag=M))
  f.solve(b) -> x               f.negatives -> int   number of negative pivots (= eigenvalues below the shift:
                                  Sylvester inertia; 0 means K - shift is positive definite)
  eig_lowest(K, M, k=3, tol=1e-10, iters=200) -> (eigenvalues[k], vectors[k])   K φ = λ M φ; M is a list (diagonal
                                  lumped mass, entries >= 0) or a positive semi-definite SymSparse (e.g. rigid bodies
                                  with offset centroids); massless DOFs are fine as long as K is positive definite
  sym_eig(A) -> (values ascending, vectors as columns-list)   dense symmetric Jacobi (small A, e.g. reduced systems)
  rcm(adjacency) -> order       reverse Cuthill-McKee ordering of a graph given as {i: set(neighbours)}
Contract: indices 0 <= i < n (IndexError otherwise); values finite floats. factor() raises ZeroDivisionError-free
  ValueError("singular ... at unknown i") when a pivot is ~0 relative to the diagonal (unsupported mechanism / missing
  boundary condition). Indefinite matrices factor fine (no pivoting) unless a pivot vanishes exactly.
Limits: no pivoting (fine for stiffness matrices, may lose accuracy on badly ordered indefinite systems); eig_lowest is
  subspace iteration (k+4 M-orthonormalised vectors, seeded random start) — slow for tightly clustered spectra; sym_eig is O(p³)
  per sweep (p <= ~50).
Used by: mech/frame2d.py, mech/frame3d.py. Depends on: nothing (stdlib)."""
import math
from collections import deque

__all__ = ["SymSparse", "LDL", "eig_lowest", "sym_eig", "rcm"]


def rcm(adj):
    """Reverse Cuthill-McKee order of the graph {i: neighbours} (all n nodes 0..n-1 must be keys)."""
    n = len(adj); seen = [False] * n; order = []
    deg = {i: len(adj[i]) for i in adj}
    for start in sorted(range(n), key=lambda i: deg[i]):
        if seen[start]: continue
        # pseudo-peripheral start: BFS twice from the lowest-degree node of this component
        root = start
        for _ in range(2):
            lv = {root: 0}; q = deque([root])
            while q:
                a = q.popleft()
                for b in adj[a]:
                    if b not in lv: lv[b] = lv[a] + 1; q.append(b)
            far = max(lv.values()); root = min((i for i in lv if lv[i] == far), key=lambda i: deg[i])
        seen[root] = True; q = deque([root])
        while q:
            a = q.popleft(); order.append(a)
            for b in sorted((b for b in adj[a] if not seen[b]), key=lambda i: deg[i]):
                seen[b] = True; q.append(b)
    order.reverse()
    return order


class SymSparse:
    def __init__(self, n):
        if not isinstance(n, int) or n < 0: raise ValueError("n must be a non-negative int")
        self.n = n; self.rows = [dict() for _ in range(n)]          # rows[i][j] for j <= i (lower triangle)

    def _chk(self, i):
        if not 0 <= i < self.n: raise IndexError(f"index {i} outside 0..{self.n - 1}")

    def add(self, i, j, v):
        self._chk(i); self._chk(j)
        if i < j: i, j = j, i
        r = self.rows[i]; r[j] = r.get(j, 0.0) + v

    def get(self, i, j):
        if i < j: i, j = j, i
        return self.rows[i].get(j, 0.0)

    def copy(self):
        c = SymSparse(self.n); c.rows = [dict(r) for r in self.rows]; return c

    def matvec(self, x):
        if len(x) != self.n: raise ValueError("matvec: length mismatch")
        y = [0.0] * self.n
        for i, r in enumerate(self.rows):
            for j, v in r.items():
                y[i] += v * x[j]
                if j != i: y[j] += v * x[i]
        return y

    def submatrix(self, keep):
        pos = {g: k for k, g in enumerate(keep)}; s = SymSparse(len(keep))
        for g in keep:
            a = pos[g]
            for j, v in self.rows[g].items():
                b = pos.get(j)
                if b is not None: s.add(a, b, v)
        return s

    def factor(self, shift=0.0, mdiag=None):
        return LDL(self, shift, mdiag)


class LDL:
    """Skyline LDLᵀ of a SymSparse (RCM-reordered). Use via SymSparse.factor()."""
    def __init__(self, K, shift=0.0, mdiag=None):
        n = K.n; self.n = n
        adj = {i: set() for i in range(n)}
        for i, r in enumerate(K.rows):
            for j in r:
                if j != i: adj[i].add(j); adj[j].add(i)
        self.perm = rcm(adj) if n else []                  # new index k -> old index perm[k]
        inv = [0] * n
        for k, o in enumerate(self.perm): inv[o] = k
        self.inv = inv
        rows = [dict() for _ in range(n)]
        scale = 0.0
        for i, r in enumerate(K.rows):
            for j, v in r.items():
                a, b = inv[i], inv[j]
                if a < b: a, b = b, a
                rows[a][b] = rows[a].get(b, 0.0) + v
        for o in range(n):
            k = inv[o]; s = shift * (1.0 if mdiag is None else mdiag[o])
            rows[k][k] = rows[k].get(k, 0.0) - s
            scale = max(scale, abs(rows[k][k]))
        first = [min(r) if r else k for k, r in enumerate(rows)]
        self.first = first; L = []; D = [0.0] * n; neg = 0
        tiny = 1e-13 * (scale or 1.0)
        for i in range(n):
            fi = first[i]; ri = rows[i]; li = [0.0] * (i - fi); wi = [0.0] * (i - fi)   # wi = L_ik * D_k
            for j in range(fi, i):
                fj = first[j]; lo = max(fi, fj); s = ri.get(j, 0.0)
                if lo < j:
                    lj = L[j]; s -= sum(wi[k - fi] * lj[k - fj] for k in range(lo, j))
                wi[j - fi] = s; li[j - fi] = s / D[j]
            d = ri.get(i, 0.0) - sum(wi[k] * li[k] for k in range(i - fi))
            if abs(d) <= tiny:
                raise ValueError(f"singular matrix: zero pivot at unknown {self.perm[i]} (unsupported DOF / mechanism?)")
            if d < 0: neg += 1
            D[i] = d; L.append(li)
        self.L, self.D, self.negatives = L, D, neg

    def solve(self, b):
        n = self.n
        if len(b) != n: raise ValueError("solve: length mismatch")
        y = [b[o] for o in self.perm]; L, first = self.L, self.first
        for i in range(n):                                  # forward: L y = b
            fi = first[i]; li = L[i]
            if li: y[i] -= sum(li[k] * y[fi + k] for k in range(len(li)))
        for i in range(n): y[i] /= self.D[i]
        for i in range(n - 1, -1, -1):                      # backward: Lᵀ x = y
            fi = first[i]; li = L[i]; yi = y[i]
            if yi:
                for k in range(len(li)): y[fi + k] -= li[k] * yi
        x = [0.0] * n
        for k, o in enumerate(self.perm): x[o] = y[k]
        return x


def sym_eig(A):
    """Dense symmetric eigen-decomposition by cyclic Jacobi. Returns (values ascending, vectors) with vectors[k] the
    unit eigenvector of values[k]."""
    n = len(A); a = [list(map(float, r)) for r in A]
    if any(len(r) != n for r in a): raise ValueError("sym_eig: square matrix required")
    V = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for _ in range(100):
        off = math.sqrt(sum(a[i][j] ** 2 for i in range(n) for j in range(n) if i != j))
        nrm = math.sqrt(sum(a[i][j] ** 2 for i in range(n) for j in range(n))) or 1.0
        if off <= 1e-15 * nrm: break
        for p in range(n - 1):
            for q in range(p + 1, n):
                if abs(a[p][q]) < 1e-300: continue
                th = (a[q][q] - a[p][p]) / (2 * a[p][q])
                t = (1.0 if th >= 0 else -1.0) / (abs(th) + math.sqrt(th * th + 1))
                c = 1 / math.sqrt(t * t + 1); s = t * c
                for k in range(n):
                    akp, akq = a[k][p], a[k][q]; a[k][p] = c * akp - s * akq; a[k][q] = s * akp + c * akq
                for k in range(n):
                    apk, aqk = a[p][k], a[q][k]; a[p][k] = c * apk - s * aqk; a[q][k] = s * apk + c * aqk
                for k in range(n):
                    vkp, vkq = V[k][p], V[k][q]; V[k][p] = c * vkp - s * vkq; V[k][q] = s * vkp + c * vkq
    idx = sorted(range(n), key=lambda i: a[i][i])
    return [a[i][i] for i in idx], [[V[k][i] for k in range(n)] for i in idx]


def eig_lowest(K, M, k=3, tol=1e-10, iters=200):
    """Lowest k eigenpairs of K φ = λ M φ by subspace iteration with Rayleigh-Ritz.
    M: diagonal as a list, or a SymSparse. Vectors are M-normalised (φᵀ M φ = 1). Requires K positive definite."""
    n = K.n
    if isinstance(M, SymSparse):
        if M.n != n: raise ValueError("eig_lowest: M size mismatch")
        Mv = M.matvec; diag = [M.get(i, i) for i in range(n)]
    else:
        if len(M) != n: raise ValueError("eig_lowest: mass length mismatch")
        if any(m < 0 for m in M): raise ValueError("eig_lowest: negative mass")
        diag = list(M); Mv = lambda x: [diag[i] * x[i] for i in range(n)]
    massive = [i for i in range(n) if diag[i] > 0]
    k = min(k, len(massive))
    if k == 0: return [], []
    p = min(len(massive), k + 4)
    f = K.factor()
    import random
    rng = random.Random(12345)
    X = [[diag[i] for i in range(n)]] + [[rng.uniform(-1, 1) * (1.0 if diag[i] > 0 else 0.0) for i in range(n)]
                                         for _ in range(p - 1)]
    lam_old = lam = None
    for it in range(iters):
        Y = [f.solve(Mv(x)) for x in X]
        Y = _m_orthonormal(Y, Mv, rng, diag)                  # keeps the subspace well conditioned
        KY = [K.matvec(y) for y in Y]
        A = [[sum(a * b for a, b in zip(Y[r], KY[c])) for c in range(p)] for r in range(p)]
        A = [[(A[r][c] + A[c][r]) / 2 for c in range(p)] for r in range(p)]
        vals, vecs = sym_eig(A)
        X = [[sum(vecs[c][r] * Y[r][i] for r in range(p)) for i in range(n)] for c in range(p)]
        lam = vals[:k]
        if it >= 3 and lam_old and all(abs(a - b) <= tol * max(abs(a), 1e-300) for a, b in zip(lam, lam_old)): break
        lam_old = lam
    out = []
    for c in range(k):
        x = X[c]; m = math.sqrt(max(sum(a * b for a, b in zip(x, Mv(x))), 1e-300))
        out.append([v / m for v in x])
    return list(lam), out


def _m_orthonormal(Y, Mv, rng, diag):
    """Modified Gram-Schmidt in the M inner product (twice, for stability); a vector that collapses is replaced by a
    fresh random one so the subspace keeps its dimension."""
    out = []
    for y in Y:
        v = list(y)
        for _ in range(2):
            for q in out:
                Mq = Mv(q); c = sum(a * b for a, b in zip(v, Mq)); v = [a - c * b for a, b in zip(v, q)]
            nv = math.sqrt(max(sum(a * b for a, b in zip(v, Mv(v))), 0.0))
            ny = math.sqrt(max(sum(a * b for a, b in zip(y, Mv(y))), 0.0)) or 1.0
            if nv > 1e-10 * ny: break
            v = [rng.uniform(-1, 1) * (1.0 if d > 0 else 0.0) for d in diag]
        nv = math.sqrt(max(sum(a * b for a, b in zip(v, Mv(v))), 1e-300)); out.append([a / nv for a in v])
    return out
