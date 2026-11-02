"""Objective-C runtime bridge via ctypes: talk to AppKit/Foundation/CoreGraphics from pure Python.

Interface:
  cls(name) -> Class pointer            msg(obj, selector, *args, res=c_void_p) -> result
  nsstring(s) -> NSString               CGRect(x,y,w,h), CGPoint(x,y), CGSize(w,h)  (ctypes structs)
  sel(name) -> SEL                      objc, appkit, cg  (loaded libraries)
  make_class(name, superclass, {selector: (CFUNCTYPE, python_fn, type_encoding)}) -> new ObjC class
  block(fn, *argtypes, restype=None) -> block pointer (int)   an Objective-C block (^) wrapping a Python callable, for
        completion-handler APIs: fn(*args) receives the args WITHOUT the block itself. Global block, kept alive forever
        (copy is a no-op) — create one per long-lived use, not per call in hot loops.
  call_block(blk, *args, argtypes=(), restype=None)   invoke a block you were handed (e.g. a delegate's
        completionHandler): args mapped by argtypes (ctypes types).
  string(nsstring_ptr) -> str | None   NSString -> Python str
  autorelease_pool()  context manager (objc_autoreleasePoolPush/Pop): frees every object autoreleased inside it on exit.
        REQUIRED around Cocoa work in any long-running process without an AppKit event loop (servers, CLI loops):
        nothing drains autoreleased objects otherwise, so they leak forever (2026-10-08: 14 GB in the browser server).
        Don't keep autoreleased pointers (msg results you didn't alloc/retain) past the block.
msg() argument mapping: float->double, int->long, bytes->char*, any ctypes Structure (CGRect, CMTime, ...)->by value,
  a ctypes scalar instance (c_ushort(5), c_bool(1), c_float(.5)) -> exactly that type, other->id.
  arm64 pitfall: arguments past the 8th integer register go on the stack packed by their real size, so for those pass
  exact types (e.g. NSEvent keyCode is unsigned short -> c_ushort, BOOL -> c_bool, float -> c_float) or they misalign.
  So pass floats for CGFloat params (1.0 not 1) and ints for NSInteger/BOOL. Struct returns via res=CGSize etc.
  (works on arm64; Intel would need objc_msgSend_stret for large structs — untested).
Fails: OSError at import if not macOS. Wrong arg types -> undefined behaviour/crash (no runtime check possible).
Used by (autorelease_pool: graphics/appkit.bitmap, browser/engine.spin/jpeg/frame) graphics/appkit.py, gui/window.py, documents/pdf.py, media/avf.py, browser/engine.py.
Limits: macOS only (system Objective-C runtime + AppKit/Foundation/CoreGraphics via ctypes); arm64 verified, large struct
  returns on Intel untested; blocks are never freed; no type checking of selectors/arguments; callbacks cannot
  propagate Python exceptions."""
import ctypes, ctypes.util
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
from macos.dylib import framework, cfunc
from ctypes import c_void_p, c_double, c_long, c_char_p, CFUNCTYPE, Structure

objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
appkit = framework("AppKit")
cg = framework("CoreGraphics")
objc.objc_getClass.restype = c_void_p; objc.objc_getClass.argtypes = [c_char_p]
objc.sel_registerName.restype = c_void_p; objc.sel_registerName.argtypes = [c_char_p]
objc.objc_allocateClassPair.restype = c_void_p
objc.objc_allocateClassPair.argtypes = [c_void_p, c_char_p, ctypes.c_size_t]
objc.class_addMethod.argtypes = [c_void_p, c_void_p, c_void_p, c_char_p]
objc.objc_registerClassPair.argtypes = [c_void_p]

class CGRect(Structure):
    _fields_ = [("x", c_double), ("y", c_double), ("w", c_double), ("h", c_double)]
class CGPoint(Structure):
    _fields_ = [("x", c_double), ("y", c_double)]
class CGSize(Structure):
    _fields_ = [("w", c_double), ("h", c_double)]

def cls(name):
    c = objc.objc_getClass(name.encode())
    if not c: raise LookupError(f"no Objective-C class {name!r}")
    return c

def sel(name): return objc.sel_registerName(name.encode())

_PROTOS, _SELS = {}, {}


def msg(obj, name, *args, res=c_void_p):
    types = tuple(type(a) if isinstance(a, (Structure, ctypes._SimpleCData)) else c_double if isinstance(a, float) else
                  c_char_p if isinstance(a, bytes) else c_long if isinstance(a, int) else c_void_p for a in args)
    key = (res, types); f = _PROTOS.get(key)
    if f is None:                                  # prototype + cast cached per signature (was ~40 µs per call)
        f = _PROTOS[key] = ctypes.cast(objc.objc_msgSend, CFUNCTYPE(res, c_void_p, c_void_p, *types))
    s = _SELS.get(name)
    if s is None: s = _SELS[name] = sel(name)
    return f(obj, s, *args)

def nsstring(s): return msg(cls("NSString"), "stringWithUTF8String:", str(s).encode())

_keep = []  # IMPs must outlive the class
def make_class(name, superclass, methods):
    """Register ObjC subclass `name` of `superclass` with methods {sel: (CFUNCTYPE, fn, encoding)}.
    Each class name can be registered once per process (re-registering raises)."""
    c = objc.objc_allocateClassPair(cls(superclass), name.encode(), 0)
    if not c: raise RuntimeError(f"class {name!r} already registered")
    for s, (ftype, fn, enc) in methods.items():
        imp = ftype(fn); _keep.append(imp)
        objc.class_addMethod(c, sel(s), ctypes.cast(imp, c_void_p), enc)
    objc.objc_registerClassPair(c)
    return c


# ---- Objective-C blocks (clang block ABI): enough for completion handlers in and out ----
_libsys = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
_GLOBAL_BLOCK = c_void_p.in_dll(_libsys, "_NSConcreteGlobalBlock")

class _BlockDesc(Structure):
    _fields_ = [("reserved", ctypes.c_ulong), ("size", ctypes.c_ulong)]

class _Block(Structure):
    _fields_ = [("isa", c_void_p), ("flags", ctypes.c_int), ("reserved", ctypes.c_int), ("invoke", c_void_p),
                ("desc", ctypes.POINTER(_BlockDesc))]

def block(fn, *argtypes, restype=None):
    f = CFUNCTYPE(restype, c_void_p, *argtypes)(lambda _blk, *a: fn(*a))
    d = _BlockDesc(0, ctypes.sizeof(_Block))
    b = _Block(ctypes.addressof(_GLOBAL_BLOCK), 1 << 28, 0, ctypes.cast(f, c_void_p), ctypes.pointer(d))  # BLOCK_IS_GLOBAL
    _keep.extend([f, d, b])
    return ctypes.addressof(b)

def call_block(blk, *args, argtypes=(), restype=None):
    if not blk: raise ValueError("call_block: null block")
    inv = ctypes.cast(blk, ctypes.POINTER(_Block)).contents.invoke
    return ctypes.cast(inv, CFUNCTYPE(restype, c_void_p, *argtypes))(blk, *args)

def string(ns):
    if not ns: return None
    p = msg(ns, "UTF8String")
    return ctypes.string_at(p).decode("utf-8", "replace") if p else None


objc.objc_autoreleasePoolPush.restype = c_void_p
objc.objc_autoreleasePoolPop.argtypes = [c_void_p]

class autorelease_pool:
    def __enter__(self): self._p = objc.objc_autoreleasePoolPush(); return self
    def __exit__(self, *exc): objc.objc_autoreleasePoolPop(self._p); return False
