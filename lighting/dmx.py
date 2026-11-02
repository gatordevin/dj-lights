"""DMX512 output: a 512-channel universe streamed continuously to a USB→DMX dongle (FTDI "Open DMX"), Art-Net, or memory.
CLI:
  python3 lighting/dmx.py ports                         USB→DMX candidates (FTDI/serial ports + the USB device behind each)
  python3 lighting/dmx.py send 1=255 2=128 ... [--hold 5] [--port P] [--artnet HOST]   set raw channels, hold, then blackout
  python3 lighting/dmx.py selftest [--port P] [--seconds 3]   open the dongle, stream frames, report achieved fps
Python:
  u = Universe()                     u[ch] = v (1-based ch 1..512, v 0..255); u.set_many(start, values); u.clear(); u.frame() -> bytes(512)
  OpenDMX(port, break_mode="ioctl"|"baud", slots=512, serial=None)   serial= any object with write/send_break/set_baud/close   FTDI/RS-485 dongle: 250000 baud 8N2, break + mark-after-break, start code 0, slots
  ArtNet(host="255.255.255.255", universe=0)  ArtDmx over UDP 6454 (Wi-Fi nodes, QLC+/visualisers)
  MemoryOutput()                     keeps .last (bytes) and .count — simulation and tests
  AutoOutput()                       dongle if plugged in, else simulation; retries every 3 s (hot-plug both ways); .mode
  out.send(frame: bytes)             one DMX packet (frame = 512 channel bytes, no start code); out.close()
  s = Sender(universe, [outputs], fps=30).start()   background thread re-sending the universe forever (fixtures expect
       continuous refresh; many fall back to auto/sound mode or blackout when DMX stops). s.fps_actual, s.frames, s.errors,
       s.stop(blackout=True) sends a few all-zero frames before closing.
  find_ports() -> [{"port", "usb", "known", "ftdi"}]  serial ports, FTDI first; "auto" only ever selects an FTDI one
       (an Arduino/RoboClaw/ESP32 must never receive DMX bytes by accident)
Contract: channels 1..512 and values 0..255 (ValueError/TypeError otherwise); frame() is a fresh 512-byte copy.
  OpenDMX raises FileNotFoundError for a missing port, OSError(EBUSY) if another program (QLC+, SoundSwitch) holds it.
Timing (DMX512-A): break >= 92 us, mark-after-break >= 12 us, then 513 slots of 44 us = 22.7 ms per full frame, so
  ~40 fps is the ceiling at 512 slots; default 30 fps. `slots` (24..512) can shorten frames to only patched channels.
  break_mode "ioctl" holds TIOCSBRK for ~0.2 ms (sleep-timed); "baud" sends one 0x00 byte at 57600 baud (156 us low,
  then the stop bits are the MAB) — the trick OLA/others use when a driver's break is unreliable. Either way we wait
  for the previous frame to leave the wire (computed from slot count) before breaking, so frames are never truncated.
Limits: only "Open DMX"-style dumb FTDI dongles (DSD TECH SH-RS09B, Enttec Open DMX). Enttec DMX USB Pro (message
  protocol), SoundSwitch Micro DMX and other "smart" interfaces are NOT driven by this (Pro protocol is an easy add).
  macOS's built-in FTDI driver must expose /dev/cu.usbserial-*. Timing is from user space — fine for fixtures, not a
  certified DMX source. Hardware status is in lighting/README.md ("verified on hardware").
Depends on: hw/serial (8N2, break, baud), hw/usb (device names). Used by: lighting/show.py, lighting/console.py, lighting/probe.py."""
import sys, time, socket, struct, threading, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DMX_BAUD, SLOTS = 250000, 512
SLOT_S = 11 / DMX_BAUD                     # 1 start + 8 data + 2 stop bits per slot


class Universe:
    """512 DMX channel values. Thread-safe enough for one writer (show loop) + one reader (Sender): frame() copies."""
    def __init__(self):
        self.data = bytearray(SLOTS)
        self.lock = threading.Lock()

    @staticmethod
    def _check(ch, v=0):
        if not isinstance(ch, int) or isinstance(ch, bool): raise TypeError(f"channel must be int, not {type(ch).__name__}")
        if not 1 <= ch <= SLOTS: raise ValueError(f"DMX channel {ch} outside 1..512")
        if not isinstance(v, int) or not 0 <= v <= 255: raise ValueError(f"DMX value {v!r} outside 0..255 (int)")

    def __setitem__(self, ch, v):
        self._check(ch, v); self.data[ch - 1] = v

    def __getitem__(self, ch):
        self._check(ch); return self.data[ch - 1]

    def set_many(self, start, values):
        values = bytes(values)
        self._check(start); self._check(start + len(values) - 1 if values else start)
        with self.lock: self.data[start - 1:start - 1 + len(values)] = values

    def load(self, frame):
        frame = bytes(frame)
        if len(frame) != SLOTS: raise ValueError(f"frame must be {SLOTS} bytes, got {len(frame)}")
        with self.lock: self.data[:] = frame

    def clear(self):
        with self.lock: self.data[:] = bytes(SLOTS)

    def frame(self):
        with self.lock: return bytes(self.data)


class MemoryOutput:
    def __init__(self): self.last, self.count, self.closed = bytes(SLOTS), 0, False
    def send(self, frame): self.last = bytes(frame); self.count += 1
    def close(self): self.closed = True


class OpenDMX:
    def __init__(self, port, break_mode="ioctl", slots=SLOTS, break_s=0.0002, mab_s=0.00002, serial=None):
        from hw.serial import Serial
        if break_mode not in ("ioctl", "baud"): raise ValueError("break_mode must be 'ioctl' or 'baud'")
        if not 24 <= slots <= SLOTS: raise ValueError("slots must be 24..512")
        self.port, self.break_mode, self.slots, self.break_s, self.mab_s = port, break_mode, slots, break_s, mab_s
        self.ser = serial or Serial(port, baud=DMX_BAUD, stopbits=2, timeout=0.1)   # serial= injects a stand-in (tests)
        self._busy_until = 0.0

    def send(self, frame):
        if len(frame) < self.slots: raise ValueError(f"frame shorter than {self.slots} slots")
        pkt = b"\x00" + bytes(frame[:self.slots])                       # start code 0 + channel data
        wait = self._busy_until - time.perf_counter()                  # previous frame still on the wire?
        if wait > 0: time.sleep(wait)
        if self.break_mode == "ioctl":
            self.ser.send_break(self.break_s); time.sleep(self.mab_s)
        else:
            self.ser.set_baud(57600); self.ser.write(b"\x00")        # 9 bit-times low = 156 us break, stop bits = MAB
            self.ser.set_baud(DMX_BAUD)
        t0 = time.perf_counter()
        self.ser.write(pkt, drain=False)
        self._busy_until = t0 + len(pkt) * SLOT_S + 0.0005

    def close(self):
        try: self.ser.close()
        except OSError: pass


class ArtNet:
    """ArtDmx (Art-Net 4) sender. universe = 15-bit port address (net<<8 | subnet<<4 | universe)."""
    def __init__(self, host="255.255.255.255", universe=0, port=6454):
        if not 0 <= universe < 32768: raise ValueError("Art-Net universe must be 0..32767")
        self.addr, self.universe, self.seq = (host, port), universe, 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    @staticmethod
    def packet(frame, universe=0, seq=0):
        data = bytes(frame)
        if len(data) % 2: data += b"\x00"
        return (b"Art-Net\x00" + struct.pack("<H", 0x5000) + struct.pack(">H", 14) + bytes([seq & 0xFF, 0])
                + struct.pack("<H", universe) + struct.pack(">H", len(data)) + data)

    def send(self, frame):
        self.seq = self.seq % 255 + 1
        self.sock.sendto(self.packet(frame, self.universe, self.seq), self.addr)

    def close(self): self.sock.close()


class Sender:
    def __init__(self, universe, outputs, fps=30):
        if not isinstance(universe, Universe): raise TypeError("universe must be a lighting.dmx.Universe")
        if not outputs: raise ValueError("need at least one output")
        if not 1 <= fps <= 44: raise ValueError("fps must be 1..44 (a full DMX frame takes 22.7 ms)")
        self.universe, self.outputs, self.fps = universe, list(outputs), fps
        self.frames, self.errors, self.fps_actual, self.last_error = 0, 0, 0.0, None
        self._stop, self._thread = threading.Event(), None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="dmx-sender", daemon=True); self._thread.start(); return self

    def _run(self):
        period, nxt, t_win, n_win = 1.0 / self.fps, time.perf_counter(), time.perf_counter(), 0
        while not self._stop.is_set():
            f = self.universe.frame()
            for o in self.outputs:
                try: o.send(f)
                except Exception as e: self.errors += 1; self.last_error = f"{type(e).__name__}: {e}"
            self.frames += 1; n_win += 1
            now = time.perf_counter()
            if now - t_win >= 1.0: self.fps_actual, t_win, n_win = n_win / (now - t_win), now, 0
            nxt += period
            if nxt < now: nxt = now                                    # fell behind: don't burst to catch up
            self._stop.wait(max(0.0, nxt - now))

    def stop(self, blackout=True):
        self._stop.set()
        if self._thread: self._thread.join(2)
        for o in self.outputs:
            try:
                if blackout:
                    for _ in range(3): o.send(bytes(SLOTS)); time.sleep(0.03)
                o.close()
            except Exception: pass


class AutoOutput:
    """The USB dongle when present, otherwise simulation; re-checks every `retry_s` so hot-plugging works either way.
    .mode -> "dongle /dev/cu.usbserial-…" | "sim (no dongle)"; .last = last frame (like MemoryOutput)."""
    def __init__(self, break_mode="ioctl", retry_s=3.0, opener=None):
        self.break_mode, self.retry_s, self.dev, self.next_try, self.last, self.count = break_mode, retry_s, None, 0.0, bytes(SLOTS), 0
        self._open = opener or (lambda: open_output("auto", break_mode=self.break_mode))
        self.error = None

    @property
    def mode(self): return f"dongle {getattr(self.dev, 'port', '')}".strip() if self.dev else "sim (no dongle)"

    def send(self, frame):
        self.last = bytes(frame); self.count += 1
        now = time.monotonic()
        if self.dev is None and now >= self.next_try:
            self.next_try = now + self.retry_s
            try: self.dev = self._open(); self.error = None
            except (LookupError, OSError) as e: self.error = str(e)
        if self.dev is not None:
            try: self.dev.send(frame)
            except OSError as e:                                   # unplugged mid-show: fall back, retry later
                self.error = f"{type(e).__name__}: {e}"
                try: self.dev.close()
                except OSError: pass
                self.dev, self.next_try = None, now + self.retry_s

    def close(self):
        if self.dev: self.dev.close(); self.dev = None


def find_ports():
    import glob
    from hw.usb import devices
    try: owner = {p: d for d in devices() for p in d.ports}
    except RuntimeError: owner = {}
    out = []
    for p in sorted(set(glob.glob("/dev/cu.usbserial*")) | {p for p in owner}):
        d = owner.get(p)
        out.append({"port": p, "usb": f"{d.id} {d.vendor} {d.name}".strip() if d else "", "known": d.known if d else "",
                    "ftdi": bool(d and d.vid == 0x0403)})
    return sorted(out, key=lambda r: (not r["ftdi"], r["port"]))


def open_output(port=None, artnet=None, break_mode="ioctl"):
    """port='auto' → the single FTDI dongle (other serial devices are never auto-selected); artnet=HOST → Art-Net;
    neither → MemoryOutput (simulation)."""
    if artnet: return ArtNet(artnet)
    if port == "auto":
        # ONLY FTDI (0403:xxxx) dongles: never stream DMX bytes into some other serial device (Arduino, RoboClaw motor
        # controller, ESP32...) just because it happens to be the only port — they may act on them.
        c = [r for r in find_ports() if r["ftdi"]]
        if len(c) != 1:
            others = [r["port"] for r in find_ports() if not r["ftdi"]]
            raise LookupError(f"{'no' if not c else 'several'} FTDI USB→DMX dongles {[r['port'] for r in c]}"
                              + (f" (ignoring non-FTDI serial ports {others})" if others else "") + " — pass --port to choose")
        port = c[0]["port"]
    if port: return OpenDMX(port, break_mode=break_mode)
    return MemoryOutput()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0]); sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("ports")
    s = sp.add_parser("send"); s.add_argument("pairs", nargs="+", help="CH=VALUE"); s.add_argument("--hold", type=float, default=5)
    for x in (s, st := sp.add_parser("selftest")):
        x.add_argument("--port", default="auto"); x.add_argument("--artnet"); x.add_argument("--break-mode", default="ioctl")
    st.add_argument("--seconds", type=float, default=3)
    a = ap.parse_args()
    if a.cmd == "ports":
        rows = find_ports()
        for r in rows: print(f"{r['port']:34} {r['usb']}  {('(' + r['known'] + ')') if r['known'] else ''}")
        if not rows: print("no USB serial ports — is the USB→DMX dongle plugged in? (python3 hw/usb.py lists USB devices)")
        return
    u = Universe(); out = open_output(a.port, a.artnet, a.break_mode)
    if a.cmd == "send":
        for p in a.pairs:
            ch, _, v = p.partition("="); u[int(ch)] = int(v)
    s = Sender(u, [out]).start()
    try: time.sleep(a.hold if a.cmd == "send" else a.seconds)
    finally:
        fps, n, err = s.fps_actual, s.frames, s.last_error; s.stop()
    print(f"{type(out).__name__} {getattr(out, 'port', '')}: {n} frames, {fps:.1f} fps, errors {s.errors}" + (f" (last: {err})" if err else ""))


if __name__ == "__main__":
    try: main()
    except (LookupError, FileNotFoundError, ValueError) as e: print(f"dmx: {e}", file=sys.stderr); sys.exit(1)
