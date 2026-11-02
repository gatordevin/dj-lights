"""Shared test helpers. `from tests._util import ROOT, OUT, check, require_optional` after the root path insert.
OUT = out/tests/ (disposable artefacts). check(cond, msg) raises AssertionError with msg (works under -O too)."""
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "tests"
OUT.mkdir(parents=True, exist_ok=True)

def check(cond, msg="check failed"):
    if not cond: raise AssertionError(msg)


def require_optional(*modules, why="optional fabrication backend", as_skip=False):
    """Exit 0 with an explicit 'skipped:' line when an optional third-party module (numpy, manifold3d) is missing —
  those tests run under the documented fabrication Python, not the stdlib-only python3.
  Use as_skip=True INSIDE unittest.TestCase: raises unittest.SkipTest instead of
  SystemExit (which unittest correctly treats as an error). Default preserves
  whole-script exit0 with an explicit skipped: line. Returns None when importable;
  as_skip must be bool. No installation or real backend execution."""
    import importlib.util, sys
    if type(as_skip) is not bool: raise TypeError("as_skip must be bool")
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if missing:
        message=f"needs {', '.join(missing)} ({why}; run under the fabrication Python)"
        if as_skip:
            import unittest
            raise unittest.SkipTest(message)
        print("skipped: "+message); sys.exit(0)
