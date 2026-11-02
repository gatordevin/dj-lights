"""Capture (and silence) another process's audio with a Core Audio process tap → 16-bit WAV. No virtual sound card.
macOS 14.2+ process taps: tap a process's output, optionally mute it on the speakers (mute=True: the tap still gets
the audio, the user hears nothing), stream it through a private aggregate device and write a WAV you can analyse
with media/audio.py (levels, waveform). Used to let the agent *hear* its headless browser.
Python:
  pids_to_objects(pids) -> [AudioObjectID]           Core Audio process objects for Unix PIDs (skips unknown pids)
  rec = Recorder(pids, mute=True)                    tap these processes (e.g. WebKit's GPU process)
     rec.start(); ...; path = rec.stop(wav_path)     or  rec.record(seconds, wav_path)
     rec.frames, rec.rate, rec.channels              captured so far
     rec.take() -> array('f')                        streaming: samples since the last take (live analysis, lighting/audiofeed)
     rec.close()                                     destroy tap + aggregate device (also done by stop())
  peak(path) -> float 0..1                           quick loudness check of a WAV
Contract: pids must be running processes that Core Audio knows (they appear once they open audio). Raises
  LookupError (no audio process for those pids), RuntimeError(OSStatus) on Core Audio failures.
Permission: capturing other processes needs the macOS "System Audio Recording" privacy permission for the app that
  runs Python (System Settings → Privacy & Security → Screen & System Audio Recording → enable your terminal). Without
  it Core Audio delivers *silence* (no error) — `Recorder.silent` tells you, so check it.
Limits: float32 stereo mixdown at the device rate (usually 48 kHz) → int16 WAV; the IO callback runs on a Core Audio
  thread holding the GIL briefly per buffer (fine for minutes of audio, not for heavy work in the callback).
Used by: browser/engine.py (Tab.listen), lighting/audiofeed.py (live levels/onsets). Depends on: macos/objc (NSArray/NSDictionary/CATapDescription), stdlib wave."""
import sys, ctypes, time, uuid, wave, array
from ctypes import c_uint32, c_int32, c_int, c_void_p, c_double, POINTER, Structure, byref, sizeof, CFUNCTYPE
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from macos.dylib import framework, cfunc
from macos.objc import cls, msg, nsstring, string

_ca = framework("CoreAudio")


class _Addr(Structure):
    _fields_ = [("sel", c_uint32), ("scope", c_uint32), ("elem", c_uint32)]


class _ASBD(Structure):
    _fields_ = [("rate", c_double), ("fmt", c_uint32), ("flags", c_uint32), ("bpp", c_uint32), ("fpp", c_uint32),
                ("bpf", c_uint32), ("ch", c_uint32), ("bits", c_uint32), ("res", c_uint32)]


class _AB(Structure):
    _fields_ = [("ch", c_uint32), ("size", c_uint32), ("data", c_void_p)]


class _ABL(Structure):
    _fields_ = [("n", c_uint32), ("b", _AB * 2)]


_IOPROC = CFUNCTYPE(c_int32, c_uint32, c_void_p, POINTER(_ABL), c_void_p, c_void_p, c_void_p, c_void_p)
_ca.AudioObjectGetPropertyData.argtypes = [c_uint32, POINTER(_Addr), c_uint32, c_void_p, POINTER(c_uint32), c_void_p]
_ca.AudioHardwareCreateProcessTap.argtypes = [c_void_p, POINTER(c_uint32)]
_ca.AudioHardwareDestroyProcessTap.argtypes = [c_uint32]
_ca.AudioHardwareCreateAggregateDevice.argtypes = [c_void_p, POINTER(c_uint32)]
_ca.AudioHardwareDestroyAggregateDevice.argtypes = [c_uint32]
_ca.AudioDeviceCreateIOProcID.argtypes = [c_uint32, _IOPROC, c_void_p, POINTER(c_void_p)]
_ca.AudioDeviceDestroyIOProcID.argtypes = [c_uint32, c_void_p]
_ca.AudioDeviceStart.argtypes = _ca.AudioDeviceStop.argtypes = [c_uint32, c_void_p]


def _cc(s): return int.from_bytes(s.encode(), "big")


def _ok(st, what):
    if st: raise RuntimeError(f"{what} failed (OSStatus {st})")


def _get(obj, sel, ctype, qualifier=None):
    v = ctype(); s = c_uint32(sizeof(v))
    q, qs = (byref(qualifier), sizeof(qualifier)) if qualifier is not None else (None, 0)
    _ok(_ca.AudioObjectGetPropertyData(obj, byref(_Addr(_cc(sel), _cc("glob"), 0)), qs, q, byref(s), byref(v)), f"get {sel}")
    return v


def pids_to_objects(pids):
    out = []
    for p in pids:
        o = _get(1, "id2p", c_uint32, c_int(int(p))).value          # kAudioHardwarePropertyTranslatePIDToProcessObject
        if o: out.append(o)
    return out


def _num(v): return msg(cls("NSNumber"), "numberWithBool:", int(v)) if isinstance(v, bool) else msg(cls("NSNumber"), "numberWithLongLong:", v)


class Recorder:
    def __init__(self, pids, mute=True):
        objs = pids_to_objects(pids)
        if not objs: raise LookupError(f"no Core Audio process for pids {list(pids)} (has it played sound yet?)")
        arr = msg(cls("NSMutableArray"), "array")
        for o in objs: msg(arr, "addObject:", msg(cls("NSNumber"), "numberWithUnsignedInt:", o))
        desc = msg(msg(cls("CATapDescription"), "alloc"), "initStereoMixdownOfProcesses:", arr)
        msg(desc, "setMuteBehavior:", 1 if mute else 0)                   # CATapMuted: speakers get nothing
        msg(desc, "setPrivate:", 1); msg(desc, "setName:", nsstring("dj-lights-tap"))
        self.tap = c_uint32(); _ok(_ca.AudioHardwareCreateProcessTap(desc, byref(self.tap)), "AudioHardwareCreateProcessTap")
        tuid = string(_get(self.tap.value, "tuid", c_void_p).value)
        f = _get(self.tap.value, "tfmt", _ASBD)
        self.rate, self.channels, self.float = int(f.rate), int(f.ch), bool(f.flags & 1)
        d = msg(cls("NSMutableDictionary"), "dictionary")
        sub = msg(cls("NSMutableDictionary"), "dictionary")
        msg(sub, "setObject:forKey:", nsstring(tuid), nsstring("uid")); msg(sub, "setObject:forKey:", _num(True), nsstring("drift"))
        taps = msg(cls("NSMutableArray"), "array"); msg(taps, "addObject:", sub)
        for k, v in (("name", nsstring("dj-lights-tap")), ("uid", nsstring(str(uuid.uuid4()))), ("private", _num(True)),
                     ("stacked", _num(False)), ("tapautostart", _num(True)), ("taps", taps)):
            msg(d, "setObject:forKey:", v, nsstring(k))
        self.agg = c_uint32(); _ok(_ca.AudioHardwareCreateAggregateDevice(d, byref(self.agg)), "AudioHardwareCreateAggregateDevice")
        self._chunks, self._proc, self._running = [], c_void_p(), False
        def cb(dev, now, inp, intime, out, outtime, ud):
            l = inp.contents
            for k in range(min(l.n, 2)):
                b = l.b[k]
                if b.data and b.size: self._chunks.append(ctypes.string_at(b.data, b.size))
            return 0
        self._cb = _IOPROC(cb)
        _ok(_ca.AudioDeviceCreateIOProcID(self.agg.value, self._cb, None, byref(self._proc)), "AudioDeviceCreateIOProcID")

    def start(self):
        _ok(_ca.AudioDeviceStart(self.agg.value, self._proc), "AudioDeviceStart"); self._running = True; return self

    @property
    def frames(self): return sum(len(c) for c in self._chunks) // (4 * self.channels)

    def take(self):
        """Streaming: remove and return the audio captured since the last take() as array('f') (interleaved, self.channels
        per frame; float32 −1..1). Don't mix with stop(path), which writes only what take() hasn't consumed."""
        n = len(self._chunks); raw = b"".join(self._chunks[:n]); del self._chunks[:n]     # GIL: atomic vs the IO callback's append
        fa = array.array("f"); fa.frombytes(raw[:len(raw) // 4 * 4]); return fa

    def _pcm16(self):
        fa = array.array("f"); raw = b"".join(self._chunks); fa.frombytes(raw[:len(raw) // 4 * 4])
        return array.array("h", (int(max(-1.0, min(1.0, x)) * 32767) for x in fa)), max((abs(x) for x in fa), default=0.0)

    def stop(self, path):
        if self._running: _ca.AudioDeviceStop(self.agg.value, self._proc); self._running = False
        pcm, pk = self._pcm16(); self.peak = pk; self.silent = pk == 0.0
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(self.channels); w.setsampwidth(2); w.setframerate(self.rate); w.writeframes(pcm.tobytes())
        self.close(); return Path(path)

    def record(self, seconds, path, spin=None):
        self.start(); t0 = time.time()
        while time.time() - t0 < seconds: (spin(0.05) if spin else time.sleep(0.05))
        return self.stop(path)

    def close(self):
        if getattr(self, "agg", None) and self.agg.value:
            if self._running: _ca.AudioDeviceStop(self.agg.value, self._proc); self._running = False
            _ca.AudioDeviceDestroyIOProcID(self.agg.value, self._proc); _ca.AudioHardwareDestroyAggregateDevice(self.agg.value); self.agg = c_uint32(0)
        if getattr(self, "tap", None) and self.tap.value:
            _ca.AudioHardwareDestroyProcessTap(self.tap.value); self.tap = c_uint32(0)


def peak(path):
    with wave.open(str(path)) as w:
        a = array.array("h"); a.frombytes(w.readframes(w.getnframes()))
    return max((abs(x) for x in a), default=0) / 32767
