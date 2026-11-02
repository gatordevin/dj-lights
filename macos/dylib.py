"""Load macOS system frameworks / dylibs and bind C functions with ctypes — the one place for that plumbing (no AppKit
load, so numeric code like ml/blas stays light).
Python:
  framework(name) -> ctypes.CDLL   "AVFoundation" -> /System/Library/Frameworks/AVFoundation.framework/AVFoundation
                                   (cached; a path containing "/" is loaded as given, e.g. "/usr/lib/libproc.dylib")
  cfunc(lib, name, restype, *argtypes) -> the bound C function (restype/argtypes set; lib = CDLL or framework name)
Contract: OSError if the library can't be loaded; AttributeError for an unknown symbol.
Limits: macOS paths only (other OSes raise OSError at framework()).
Used by: macos/objc.py, graphics/gl.py, ml/blas.py, media (avf, videowrite, audiotap, vision, speech), documents (pdf,
  pdfwrite), browser/engine.py, hw/sysmon.py. Depends on: ctypes."""
import ctypes

_CACHE = {}


def framework(name):
    if name not in _CACHE:
        path = name if "/" in name else f"/System/Library/Frameworks/{name}.framework/{name}"
        _CACHE[name] = ctypes.CDLL(path)
    return _CACHE[name]


def cfunc(lib, name, restype, *argtypes):
    lib = framework(lib) if isinstance(lib, str) else lib
    f = getattr(lib, name); f.restype = restype; f.argtypes = list(argtypes); return f
