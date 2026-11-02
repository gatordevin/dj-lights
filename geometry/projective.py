"""2-D projective geometry: homographies and lower-DOF image transforms, robust estimation (LO-RANSAC / MSAC with
PROSAC ordering) and Levenberg-Marquardt refinement — pure maths, the one home for point-set transform fitting.
All matrices are 3x3 row-major nested lists acting on column vectors (x, y, 1); points are (x, y) tuples.
Python:
  apply(H, pts) -> [(x, y)]          inverse(H) · matmul(A, B) · identity() · normalize(H) (H[2][2] = 1)
  camera_matrix(fx,fy,cx,cy,skew=0) -> fresh3x3 pinhole intrinsics, finite real
      fields and positive focal lengths required (TypeError/ValueError).
  fit(model, src, dst, weights=None) -> 3x3            least-squares fit of model through >= min_points(model) pairs:
      "translation" (2 DOF) · "rigid" (rotation + translation, 3) · "similarity" (+ uniform scale, 4; Umeyama)
      "affine" (6) · "homography" (8; Hartley-normalised DLT, smallest eigenvector of AᵀA via geometry.sparse.sym_eig)
  min_points(model) -> int           1 / 2 / 2 / 3 / 4
  refine_homography(H, src, dst, iters=20) -> H        LM on forward reprojection error (sum |H·src - dst|²)
  errors(H, src, dst) -> [px]       forward reprojection distances
  ransac(src, dst, model="homography", thresh=1.5, confidence=0.9999, max_iter=10000, quality=None, seed=0,
         refine=True) -> Fit         robust estimate. quality: per-match scores (higher = better, e.g. -descriptor
         distance) -> PROSAC-style progressive sampling from the best matches first. MSAC (truncated quadratic) score,
         adaptive iteration count, local optimisation (iterative re-fit on inliers at a widened threshold) whenever a
         new best appears, final least-squares + LM (homography) on the inliers.
  Fit: .H .inliers [bool] .n_inliers .rms (px, inliers) .iterations .model
  score(H, src, dst, thresh) -> (MSAC cost, inliers) · local_optimize(H, inliers, src, dst, model, thresh) -> (H, cost,
      inliers) · finalize(H, inliers, src, dst, model, thresh, iterations, refine=True) -> Fit ·
      iterations_needed(ratio, k, confidence) — the RANSAC building blocks (shared with the GPU cv.ransac)
  similarity_params(M) -> {"angle_deg", "scale", "tx", "ty"}   for rigid/similarity/near-similar affine matrices
  homography_to_affine_error(H, w, h) -> px   how far H departs from its best affine approx over a w x h frame
Contract: src/dst equal-length sequences of (x, y); ValueError for too few points, unknown model or degenerate
  (collinear / coincident) samples in fit(); ransac raises ValueError when no model reaches min_points inliers.
Limits: pure Python — RANSAC over a few thousand matches takes ~10-100 ms; the homography DLT is algebraic (refine
  for geometric optimality); no fundamental/essential matrices (planar scenes only); thresholds are in pixels of dst.
Used by: cv/features.py, cv/flow.py, cv/calib.py, cv/motion.py, cv/image.Image.warp,
graphics/gl.Camera.K. Depends on: geometry.sparse, core.linsolve/validate."""
import sys, math, random
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from geometry.sparse import sym_eig
from core.linsolve import solve, SingularMatrix
from core.validate import finite

MODELS = {"translation": 1, "rigid": 2, "similarity": 2, "affine": 3, "homography": 4}


def identity(): return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]

def camera_matrix(fx, fy, cx, cy, skew=0):
    """OpenCV column-vector pinhole convention; fresh lists, no rendering or I/O."""
    fx,fy = finite("fx",fx,gt=0),finite("fy",fy,gt=0)
    cx,cy,skew = finite("cx",cx),finite("cy",cy),finite("skew",skew)
    return [[fx,skew,cx],[0.0,fy,cy],[0.0,0.0,1.0]]


def matmul(A, B): return [[sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def normalize(H):
    s = H[2][2]
    if abs(s) < 1e-15: raise ValueError("homography with H[2][2] == 0 cannot be normalised")
    return [[v / s for v in r] for r in H]


def inverse(H):
    a, b, c = H[0]; d, e, f = H[1]; g, h, i = H[2]
    A, B, C = e * i - f * h, -(d * i - f * g), d * h - e * g
    det = a * A + b * B + c * C
    if abs(det) < 1e-18: raise ValueError("singular 3x3 matrix")
    return [[A / det, -(b * i - c * h) / det, (b * f - c * e) / det],
            [B / det, (a * i - c * g) / det, -(a * f - c * d) / det],
            [C / det, -(a * h - b * g) / det, (a * e - b * d) / det]]


def apply(H, pts):
    out = []
    for x, y in pts:
        q = H[2][0] * x + H[2][1] * y + H[2][2]
        if abs(q) < 1e-15: q = 1e-15
        out.append(((H[0][0] * x + H[0][1] * y + H[0][2]) / q, (H[1][0] * x + H[1][1] * y + H[1][2]) / q))
    return out


def errors(H, src, dst):
    return [math.hypot(p[0] - q[0], p[1] - q[1]) for p, q in zip(apply(H, src), dst)]


def min_points(model):
    if model not in MODELS: raise ValueError(f"unknown model {model!r}; known: {', '.join(MODELS)}")
    return MODELS[model]


def _hartley(pts, w=None):
    w = w or [1.0] * len(pts); W = sum(w)
    cx = sum(wi * p[0] for wi, p in zip(w, pts)) / W; cy = sum(wi * p[1] for wi, p in zip(w, pts)) / W
    d = sum(wi * math.hypot(p[0] - cx, p[1] - cy) for wi, p in zip(w, pts)) / W
    if d < 1e-12: raise ValueError("degenerate points (all coincident)")
    s = math.sqrt(2) / d
    return [[s, 0.0, -s * cx], [0.0, s, -s * cy], [0.0, 0.0, 1.0]]


def _dlt(src, dst, w):
    T1, T2 = _hartley(src, w), _hartley(dst, w)
    a, b = apply(T1, src), apply(T2, dst)
    # AᵀA from four weighted Σ p pᵀ sums (p = (x, y, 1)): rows are (-p, 0, u p) and (0, -p, v p)
    P = [0.0] * 6; U = [0.0] * 6; V = [0.0] * 6; Q = [0.0] * 6           # xx xy x yy y 1
    for (x, y), (u, v), wi in zip(a, b, w):
        e = (x * x, x * y, x, y * y, y, 1.0); cu = -wi * u; cv = -wi * v; cq = wi * (u * u + v * v)
        for k in range(6):
            ek = e[k]; P[k] += wi * ek; U[k] += cu * ek; V[k] += cv * ek; Q[k] += cq * ek
    def m3(S): return [[S[0], S[1], S[2]], [S[1], S[3], S[4]], [S[2], S[4], S[5]]]
    Pm, Um, Vm, Qm = m3(P), m3(U), m3(V), m3(Q); Z = [[0.0] * 3 for _ in range(3)]
    blocks = [[Pm, Z, Um], [Z, Pm, Vm], [[list(r) for r in zip(*Um)], [list(r) for r in zip(*Vm)], Qm]]
    M = [[blocks[bi][bj][i][j] for bj in range(3) for j in range(3)] for bi in range(3) for i in range(3)]
    vals, vecs = sym_eig(M)
    if len(src) > 4 and vals[1] < 1e-12 * max(1.0, vals[-1]): raise ValueError("degenerate points (collinear)")
    h = vecs[0]; Hn = [h[0:3], h[3:6], h[6:9]]
    H = matmul(inverse(T2), matmul(Hn, T1))
    if abs(H[2][2]) < 1e-15: raise ValueError("degenerate homography")
    H = normalize(H)
    if abs(inverse_det(H)) < 1e-10: raise ValueError("degenerate homography")      # collinear 4-point samples
    return H


def inverse_det(H):
    a, b, c = H[0]; d, e, f = H[1]; g, h, i = H[2]
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _umeyama(src, dst, w, scale):
    W = sum(w); mx = sum(wi * p[0] for wi, p in zip(w, src)) / W; my = sum(wi * p[1] for wi, p in zip(w, src)) / W
    ux = sum(wi * q[0] for wi, q in zip(w, dst)) / W; uy = sum(wi * q[1] for wi, q in zip(w, dst)) / W
    sxx = sxy = syx = syy = var = 0.0
    for wi, (x, y), (u, v) in zip(w, src, dst):
        x -= mx; y -= my; u -= ux; v -= uy
        sxx += wi * u * x; sxy += wi * u * y; syx += wi * v * x; syy += wi * v * y; var += wi * (x * x + y * y)
    if var < 1e-18: raise ValueError("degenerate points (all coincident)")
    ang = math.atan2(syx - sxy, sxx + syy); c, s = math.cos(ang), math.sin(ang)
    k = ((sxx + syy) * c + (syx - sxy) * s) / var if scale else 1.0
    return [[k * c, -k * s, ux - k * (c * mx - s * my)], [k * s, k * c, uy - k * (s * mx + c * my)], [0.0, 0.0, 1.0]]


def _affine(src, dst, w):
    A = [[0.0] * 3 for _ in range(3)]; bx = [0.0] * 3; by = [0.0] * 3
    for wi, (x, y), (u, v) in zip(w, src, dst):
        r = (x, y, 1.0)
        for i in range(3):
            for j in range(3): A[i][j] += wi * r[i] * r[j]
            bx[i] += wi * r[i] * u; by[i] += wi * r[i] * v
    try: px, py = solve(A, bx), solve(A, by)
    except SingularMatrix: raise ValueError("degenerate points (collinear)") from None
    return [px, py, [0.0, 0.0, 1.0]]


def fit(model, src, dst, weights=None):
    n = min_points(model)
    if len(src) != len(dst): raise ValueError("src and dst lengths differ")
    if len(src) < n: raise ValueError(f"{model} needs >= {n} point pairs, got {len(src)}")
    w = list(weights) if weights is not None else [1.0] * len(src)
    if model == "translation":
        W = sum(w); tx = sum(wi * (q[0] - p[0]) for wi, p, q in zip(w, src, dst)) / W
        ty = sum(wi * (q[1] - p[1]) for wi, p, q in zip(w, src, dst)) / W
        return [[1.0, 0.0, tx], [0.0, 1.0, ty], [0.0, 0.0, 1.0]]
    if model in ("rigid", "similarity"): return _umeyama(src, dst, w, model == "similarity")
    if model == "affine": return _affine(src, dst, w)
    return _dlt(src, dst, w)


def _normal_eqs(h, src, dst):
    """Gauss-Newton normal equations of the forward reprojection error, exploiting the sparse Jacobian: each point's
    two rows are built from a = (x/q, y/q, 1/q), so JᵀJ is assembled from ~30 scalar sums per point."""
    s00 = s01 = s02 = s11 = s12 = s22 = 0.0                       # Σ a aᵀ (shared by the u and v blocks)
    u00 = u01 = u10 = u11 = u20 = u21 = 0.0                       # Σ -u a_i a_j (i<3, j<2)
    v00 = v01 = v10 = v11 = v20 = v21 = 0.0
    w00 = w01 = w11 = 0.0                                         # Σ (u²+v²) a_i a_j (i,j < 2)
    g0 = g1 = g2 = g3 = g4 = g5 = g6 = g7 = 0.0; cost = 0.0
    h0, h1, h2, h3, h4, h5, h6, h7 = h
    for (x, y), (X, Y) in zip(src, dst):
        q = h6 * x + h7 * y + 1.0
        if abs(q) < 1e-15: q = 1e-15
        a0, a1, a2 = x / q, y / q, 1.0 / q
        u = (h0 * x + h1 * y + h2) / q; v = (h3 * x + h4 * y + h5) / q; r1 = u - X; r2 = v - Y; cost += r1 * r1 + r2 * r2
        s00 += a0 * a0; s01 += a0 * a1; s02 += a0 * a2; s11 += a1 * a1; s12 += a1 * a2; s22 += a2 * a2
        ua0, ua1, va0, va1 = -u * a0, -u * a1, -v * a0, -v * a1
        u00 += a0 * ua0; u01 += a0 * ua1; u10 += a1 * ua0; u11 += a1 * ua1; u20 += a2 * ua0; u21 += a2 * ua1
        v00 += a0 * va0; v01 += a0 * va1; v10 += a1 * va0; v11 += a1 * va1; v20 += a2 * va0; v21 += a2 * va1
        k = u * u + v * v; w00 += k * a0 * a0; w01 += k * a0 * a1; w11 += k * a1 * a1
        g0 += a0 * r1; g1 += a1 * r1; g2 += a2 * r1; g3 += a0 * r2; g4 += a1 * r2; g5 += a2 * r2
        t = -u * r1 - v * r2; g6 += a0 * t; g7 += a1 * t
    A = [[s00, s01, s02, 0, 0, 0, u00, u01], [s01, s11, s12, 0, 0, 0, u10, u11], [s02, s12, s22, 0, 0, 0, u20, u21],
         [0, 0, 0, s00, s01, s02, v00, v01], [0, 0, 0, s01, s11, s12, v10, v11], [0, 0, 0, s02, s12, s22, v20, v21],
         [u00, u10, u20, v00, v10, v20, w00, w01], [u01, u11, u21, v01, v11, v21, w01, w11]]
    return A, [g0, g1, g2, g3, g4, g5, g6, g7], cost


def _cost(h, src, dst):
    h0, h1, h2, h3, h4, h5, h6, h7 = h; c = 0.0
    for (x, y), (X, Y) in zip(src, dst):
        q = h6 * x + h7 * y + 1.0
        if abs(q) < 1e-15: q = 1e-15
        c += ((h0 * x + h1 * y + h2) / q - X) ** 2 + ((h3 * x + h4 * y + h5) / q - Y) ** 2
    return c


def refine_homography(H, src, dst, iters=20):
    h = [v for r in normalize(H) for v in r][:8]; lam = 1e-3
    for _ in range(iters):
        JtJ, Jtr, cost = _normal_eqs(h, src, dst)
        if cost < 1e-24: break
        improved = False
        for _ in range(10):
            A = [[JtJ[i][j] + (lam * (JtJ[i][i] + 1e-12) if i == j else 0.0) for j in range(8)] for i in range(8)]
            try: dh = solve(A, [-g for g in Jtr])
            except SingularMatrix: lam *= 10; continue
            hn = [a + b for a, b in zip(h, dh)]; cn = _cost(hn, src, dst)
            if cn < cost:
                improved = cost - cn > 1e-12 * cost; h, lam = hn, lam * 0.3; break
            lam *= 10
        if not improved: break
    return [h[0:3], h[3:6], h[6:8] + [1.0]]


class Fit:
    def __init__(self, H, inliers, model, iterations, src, dst):
        self.H, self.inliers, self.model, self.iterations = H, inliers, model, iterations
        self.n_inliers = sum(inliers)
        e = [d for d, ok in zip(errors(H, src, dst), inliers) if ok]
        self.rms = math.sqrt(sum(v * v for v in e) / len(e)) if e else float("inf")

    def __repr__(self): return f"Fit({self.model}, {self.n_inliers} inliers, rms {self.rms:.3f} px, {self.iterations} it)"


def _score(H, src, dst, t2):
    s = 0.0; inl = []
    for p, q in zip(apply(H, src), dst):
        e2 = (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2
        ok = e2 < t2; inl.append(ok); s += e2 if ok else t2          # MSAC: truncated quadratic
    return s, inl


def score(H, src, dst, thresh):
    """MSAC cost (sum of min(e², t²)) and inlier mask of H over all pairs."""
    return _score(H, src, dst, thresh * thresh)


def local_optimize(H, inliers, src, dst, model, thresh):
    """LO-RANSAC step: re-fit on the inliers at shrinking thresholds (3t, 2t, 1.5t, t) -> (H, cost, inliers); returns
    the input when that does not improve the MSAC cost. Shared by the CPU ransac here and cv.ransac (GPU)."""
    k = min_points(model); t2 = thresh * thresh; s0, _ = _score(H, src, dst, t2); best = (s0, H, inliers); inl = inliers
    for wider in (3.0, 2.0, 1.5, 1.0):
        idx = [i for i, ok in enumerate(inl) if ok]
        if len(idx) < k: break
        try: Hn = fit(model, [src[i] for i in idx], [dst[i] for i in idx])
        except ValueError: break
        _, inl = _score(Hn, src, dst, t2 * wider * wider)
        sn, inn = _score(Hn, src, dst, t2)
        if sn < best[0]: best = (sn, Hn, inn)
    return best[1], best[0], best[2]


def finalize(H, inliers, src, dst, model, thresh, iterations, refine=True):
    """Least-squares re-fit on the inliers (+ LM for homographies) -> Fit."""
    t2 = thresh * thresh; idx = [i for i, ok in enumerate(inliers) if ok]
    if len(idx) < min_points(model): raise ValueError(f"RANSAC found no {model} supported by >= {min_points(model)} matches")
    try: H = fit(model, [src[i] for i in idx], [dst[i] for i in idx])
    except ValueError: pass
    if refine and model == "homography" and len(idx) > 4:
        H = refine_homography(H, [src[i] for i in idx], [dst[i] for i in idx])
    _, inl = _score(H, src, dst, t2)
    return Fit(H, inl, model, iterations, src, dst)


def iterations_needed(ratio, k, confidence):
    """Standard RANSAC stopping count for inlier ratio `ratio` and sample size k."""
    if ratio >= 1.0: return 1
    if ratio <= 0: return 1 << 30
    return int(math.ceil(math.log(1 - confidence) / math.log(max(1e-12, 1 - ratio ** k))))


def ransac(src, dst, model="homography", thresh=1.5, confidence=0.9999, max_iter=10000, quality=None, seed=0, refine=True):
    k = min_points(model); n = len(src)
    if n != len(dst): raise ValueError("src and dst lengths differ")
    if n < k: raise ValueError(f"{model} RANSAC needs >= {k} matches, got {n}")
    if thresh <= 0: raise ValueError("thresh must be > 0")
    rng = random.Random(seed); t2 = thresh * thresh
    order = sorted(range(n), key=lambda i: -quality[i]) if quality is not None else list(range(n))
    best = (float("inf"), None, None); it = 0; need = max_iter; pool = max(k, min(n, 2 * k + 4)) if quality is not None else n
    while it < min(need, max_iter):
        it += 1
        if quality is not None and pool < n and it % 4 == 0: pool += 1        # PROSAC: grow the sampling pool
        sample = rng.sample(order[:pool], k)
        try: H = fit(model, [src[i] for i in sample], [dst[i] for i in sample])
        except ValueError: continue
        s, inl = _score(H, src, dst, t2)
        if s < best[0]:
            H, s, inl = local_optimize(H, inl, src, dst, model, thresh)
            best = (s, H, inl); need = iterations_needed(sum(inl) / n, k, confidence)
    if best[1] is None: raise ValueError(f"RANSAC found no {model} supported by >= {k} matches")
    return finalize(best[1], best[2], src, dst, model, thresh, it, refine)


def similarity_params(M):
    a, b = M[0][0], M[1][0]
    return {"angle_deg": math.degrees(math.atan2(b, a)), "scale": math.hypot(a, b), "tx": M[0][2], "ty": M[1][2]}


def homography_to_affine_error(H, w, h):
    corners = [(0, 0), (w, 0), (w, h), (0, h), (w / 2, h / 2)]
    A = fit("affine", corners, apply(H, corners))
    return max(errors(A, corners, apply(H, corners)))
