"""Dependency-free contract validators for finite numbers, integers and non-empty text.

Python:
  finite(name, value, ge=None, gt=None, le=None, lt=None) -> float
  integer(name, value, ge=None, gt=None, le=None, lt=None) -> int
  nonempty(name, value) -> stripped str
Booleans are rejected as numbers/integers. Bounds are optional and may be combined; contradictory bounds simply
reject every value. Raises TypeError for the wrong Python type and ValueError for NaN/infinity, empty text or a
bound violation. Depends on: stdlib only. Used by: hw.digital, ml.hwsim, ml.fixedweight."""
import math


def _bounds(name, value, ge, gt, le, lt):
    checks = ((ge, lambda: value >= ge, f">= {ge}"), (gt, lambda: value > gt, f"> {gt}"),
              (le, lambda: value <= le, f"<= {le}"), (lt, lambda: value < lt, f"< {lt}"))
    for bound, ok, description in checks:
        if bound is not None and not ok():
            raise ValueError(f"{name} must be {description}")


def finite(name, value, *, ge=None, gt=None, le=None, lt=None):
    """Return value as float after finite/type/bound checks."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    out = float(value)
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite")
    _bounds(name, out, ge, gt, le, lt)
    return out


def integer(name, value, *, ge=None, gt=None, le=None, lt=None):
    """Return an int after exact integer/type/bound checks."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    _bounds(name, value, ge, gt, le, lt)
    return value


def nonempty(name, value):
    """Return stripped non-empty text."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    out = value.strip()
    if not out:
        raise ValueError(f"{name} must not be empty")
    return out
