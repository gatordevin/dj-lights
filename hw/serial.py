"""Serial ports (USB CDC-ACM / USB-UART) with the stdlib only: termios + select + ioctl. No pyserial.
CLI:
  python3 hw/serial.py list                                   serial ports + the USB device behind each
  python3 hw/serial.py monitor [PORT] [-b 115200] [--seconds 10]    print what the device sends
  python3 hw/serial.py send [PORT] "command" [--wait 2] [--eol crlf|lf|cr]   send a line, print the response
      PORT defaults to the only USB serial port (error if there are several).
Python:
  with Serial(port, baud=115200, timeout=1.0) as s:
      s.write(b"..."); s.read(n=4096, timeout=None) -> bytes; s.readline(timeout=None) -> bytes (incl. newline, or partial)
      s.read_until(token: bytes, timeout) -> bytes; s.drain(seconds) -> bytes (everything that arrives within `seconds`)
      s.command("status", wait=1.5, eol="\\r\\n", quiet=0.3) -> str   send a line; collect until `quiet` s of silence (max `wait`)
      s.set_dtr(bool); s.set_rts(bool); s.flush_input(); s.baud
      Serial(port, baud=250000, stopbits=2)          non-standard bauds + 2 stop bits (DMX512 = 250000 8N2)
      s.write(data, drain=True)                      drain=False returns once the driver has the bytes (no tcdrain)
      s.send_break(seconds)                          hold TX low (TIOCSBRK/TIOCCBRK) for ~seconds — DMX break; also tcsendbreak-free
      s.set_baud(baud)                               change speed on an open port (e.g. the slow-baud DMX break trick)
  auto_port() -> str          the single USB serial port (/dev/cu.usbmodem*/usbserial*/wchusbserial*/SLAB_*)
Fails: FileNotFoundError missing port; PermissionError/OSError(EBUSY) if another program holds it; ValueError bad
  baud/eol; LookupError when auto_port finds 0 or >1 candidates.
Limits: 8 data bits, no parity, no flow control (stop bits 1 or 2); standard baud rates up to 230400 via termios constants (others via IOSSIOSPEED ioctl);
  native USB-CDC devices ignore baud. Opening the port may toggle DTR/RTS (on ESP32 USB-Serial/JTAG this does NOT reset
  unless the reset sequence is sent).
Used by: humans/agents for MCU consoles (ESP32 etc.), lighting/dmx.py (USB→DMX dongles), tests/test_hw.py (via a pty)."""
import os, sys, time, glob, fcntl, select, struct, termios, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BAUDS = {9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400, 57600: termios.B57600,
         115200: termios.B115200, 230400: termios.B230400}
IOSSIOSPEED = 0x80045402            # macOS: arbitrary baud rate ioctl
TIOCMBIS, TIOCMBIC = 0x8004746C, 0x8004746B
TIOCSBRK, TIOCCBRK = 0x2000747B, 0x2000747A      # _IO('t', 123/122): set/clear break
TIOCM_DTR, TIOCM_RTS = 0x002, 0x004
EOLS = {"crlf": b"\r\n", "lf": b"\n", "cr": b"\r", "\r\n": b"\r\n", "\n": b"\n", "\r": b"\r"}

class Serial:
    def __init__(self, port, baud=115200, timeout=1.0, stopbits=1):
        if not Path(port).exists(): raise FileNotFoundError(f"no serial port {port}")
        if not isinstance(baud, int) or baud <= 0: raise ValueError(f"bad baud {baud!r}")
        if stopbits not in (1, 2): raise ValueError(f"stopbits must be 1 or 2, not {stopbits!r}")
        self.port, self.baud, self.timeout, self._buf, self.stopbits = port, baud, timeout, b"", stopbits
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            a = termios.tcgetattr(self.fd)
            a[0] = 0; a[1] = 0; a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL | (termios.CSTOPB if stopbits == 2 else 0); a[3] = 0
            a[4] = a[5] = BAUDS.get(baud, termios.B115200)
            a[6][termios.VMIN] = 0; a[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, a)
            if baud not in BAUDS: fcntl.ioctl(self.fd, IOSSIOSPEED, struct.pack("L", baud))
        except Exception:
            os.close(self.fd); raise

    def __enter__(self): return self
    def __exit__(self, *e): self.close()
    def close(self):
        if self.fd is not None: os.close(self.fd); self.fd = None

    def write(self, data, drain=True):
        data = bytes(data); sent = 0
        while sent < len(data):
            select.select([], [self.fd], [], self.timeout)
            try: sent += os.write(self.fd, data[sent:])
            except BlockingIOError: pass
        if drain: termios.tcdrain(self.fd)
        return sent

    def send_break(self, seconds):
        """Hold the line in the break (spacing) state for about `seconds` (sleep-timed; USB adds ~0.1-1 ms)."""
        if not 0 < seconds <= 1: raise ValueError("break must be 0 < seconds <= 1")
        fcntl.ioctl(self.fd, TIOCSBRK)
        try: time.sleep(seconds)
        finally: fcntl.ioctl(self.fd, TIOCCBRK)

    def set_baud(self, baud):
        if not isinstance(baud, int) or baud <= 0: raise ValueError(f"bad baud {baud!r}")
        if baud in BAUDS:
            a = termios.tcgetattr(self.fd); a[4] = a[5] = BAUDS[baud]; termios.tcsetattr(self.fd, termios.TCSADRAIN, a)
        else: fcntl.ioctl(self.fd, IOSSIOSPEED, struct.pack("L", baud))
        self.baud = baud

    def _fill(self, timeout):
        r, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not r: return False
        try: chunk = os.read(self.fd, 65536)
        except BlockingIOError: return False
        if not chunk: return False
        self._buf += chunk; return True

    def read(self, n=4096, timeout=None):
        end = time.time() + (self.timeout if timeout is None else timeout)
        while not self._buf and time.time() < end: self._fill(end - time.time())
        out, self._buf = self._buf[:n], self._buf[n:]; return out

    def read_until(self, token, timeout=None):
        end = time.time() + (self.timeout if timeout is None else timeout)
        while token not in self._buf and time.time() < end: self._fill(end - time.time())
        i = self._buf.find(token)
        cut = i + len(token) if i >= 0 else len(self._buf)
        out, self._buf = self._buf[:cut], self._buf[cut:]; return out

    def readline(self, timeout=None): return self.read_until(b"\n", timeout)

    def drain(self, seconds):
        end = time.time() + seconds
        while time.time() < end: self._fill(end - time.time())
        out, self._buf = self._buf, b""; return out

    def flush_input(self):
        termios.tcflush(self.fd, termios.TCIFLUSH); self._buf = b""

    def command(self, line, wait=1.5, eol="\r\n", quiet=0.3):
        if eol not in EOLS: raise ValueError(f"eol must be one of {list(EOLS)}")
        self.drain(0.05); self.write(line.encode() + EOLS[eol])
        end, last = time.time() + wait, None          # last = time of the most recent byte (None: nothing yet)
        while (now := time.time()) < end:
            if last is not None and now - last >= quiet: break
            if self._fill(end - now if last is None else min(quiet - (now - last), end - now)): last = time.time()
        out, self._buf = self._buf, b""
        return out.decode(errors="replace")

    def _modem(self, req, bits): fcntl.ioctl(self.fd, req, struct.pack("I", bits))
    def set_dtr(self, on): self._modem(TIOCMBIS if on else TIOCMBIC, TIOCM_DTR)
    def set_rts(self, on): self._modem(TIOCMBIS if on else TIOCMBIC, TIOCM_RTS)

USB_PATTERNS = ("/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*", "/dev/cu.SLAB_*")

def auto_port():
    c = sorted({p for pat in USB_PATTERNS for p in glob.glob(pat)})
    if len(c) != 1: raise LookupError(f"{'no' if not c else 'several'} USB serial ports: {c} - pass PORT explicitly")
    return c[0]

def main():
    ap = argparse.ArgumentParser(description="Serial port tool"); sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    s = sub.add_parser("monitor"); s.add_argument("port", nargs="?"); s.add_argument("-b", "--baud", type=int, default=115200); s.add_argument("--seconds", type=float, default=10)
    s = sub.add_parser("send"); s.add_argument("port_or_cmd"); s.add_argument("cmd", nargs="?"); s.add_argument("-b", "--baud", type=int, default=115200)
    s.add_argument("--wait", type=float, default=2.0); s.add_argument("--eol", default="crlf")
    a = ap.parse_args()
    if a.cmd == "list":
        from hw.usb import devices
        owner = {p: d for d in devices() for p in d.ports}
        for p in sorted(glob.glob("/dev/cu.*")):
            d = owner.get(p); print(f"{p:32} " + (f"{d.id} {d.vendor} — {d.name}" + (f"  ({d.known})" if d.known else "") if d else "(not USB)"))
        return
    if a.cmd == "monitor":
        with Serial(a.port or auto_port(), a.baud) as s:
            end = time.time() + a.seconds
            while time.time() < end:
                data = s.read(timeout=min(0.5, end - time.time()))
                if data: sys.stdout.write(data.decode(errors="replace")); sys.stdout.flush()
        return
    port, cmd = (a.port_or_cmd, a.cmd) if a.cmd is not None else (auto_port(), a.port_or_cmd)
    with Serial(port, a.baud) as s: print(s.command(cmd, wait=a.wait, eol=a.eol))

if __name__ == "__main__":
    try: main()
    except (LookupError, FileNotFoundError, ValueError) as e: print(f"serial: {e}", file=sys.stderr); sys.exit(1)
