"""USB device discovery on macOS (no pyusb/libusb): what's plugged in, and which serial port belongs to it.
CLI:    python3 hw/usb.py [--all] [--json]        devices (vendor/product names, VID:PID, serial, speed, port)
Python: devices(include_hubs=False) -> [Device(name, vendor, vid, pid, serial, location, speed, ports, known)]
        find(vid=None, pid=None, name=None) -> [Device]      ports(): /dev/cu.* serial ports
Device.ports: the /dev/cu.* callout ports created for that device (matched via its IORegistry subtree).
Device.known: friendly identification from KNOWN (VID:PID table), e.g. Espressif USB-Serial/JTAG = ESP32-S3/C3/C6/H2.
How: `ioreg -a -r -c IOUSBHostDevice -l` (plist, parsed with plistlib), walked recursively (devices behind hubs are
  nested children); serial ports = "IOCalloutDevice" entries in each device's subtree, not descending into nested devices.
Fails: RuntimeError if ioreg fails. Limits: macOS only; reads descriptors, doesn't claim interfaces (raw USB transfers
  would need IOKit user clients via ctypes - not built yet); `system_profiler SPUSBDataType` returns nothing in this sandbox.
Used by: hw/serial.py (auto port pick), humans."""
import sys, json, glob, plistlib, argparse, subprocess
from dataclasses import dataclass, field, asdict

KNOWN = {(0x303A, 0x1001): "Espressif USB-Serial/JTAG (ESP32-S3/C3/C6/H2 native USB)",
         (0x303A, 0x0002): "Espressif ESP32-S2/S3 USB-OTG (TinyUSB CDC)",
         (0x10C4, 0xEA60): "Silicon Labs CP210x USB-UART (ESP32 dev boards, many others)",
         (0x1A86, 0x7523): "WCH CH340 USB-UART", (0x1A86, 0x55D4): "WCH CH9102 USB-UART",
         (0x0403, 0x6001): "FTDI FT232R USB-UART (also USB→DMX dongles: DSD TECH SH-RS09B, Enttec Open DMX/DMX USB Pro)", (0x0403, 0x6010): "FTDI FT2232 dual UART/JTAG",
         (0x2E8A, 0x0005): "Raspberry Pi Pico (MicroPython CDC)", (0x2E8A, 0x000A): "Raspberry Pi Pico SDK CDC",
         (0x2341, 0x0043): "Arduino Uno", (0x239A, 0x8029): "Adafruit board (CircuitPython)"}
SPEEDS = {0: "low (1.5M)", 1: "full (12M)", 2: "high (480M)", 3: "super (5G)", 4: "super+ (10G)"}

@dataclass
class Device:
    name: str
    vendor: str
    vid: int
    pid: int
    serial: str = ""
    location: str = ""
    speed: str = ""
    ports: list = field(default_factory=list)
    known: str = ""
    @property
    def id(self): return f"{self.vid:04x}:{self.pid:04x}"

def _is_dev(n): return isinstance(n, dict) and "idVendor" in n and "idProduct" in n and n.get("IOObjectClass", "IOUSBHostDevice") in ("IOUSBHostDevice", "IOUSBDevice")

def _walk_ports(node, out, top=True):
    """Serial ports in this device's subtree, NOT descending into nested USB devices (hubs contain their children)."""
    if isinstance(node, dict):
        if not top and _is_dev(node): return out
        if node.get("IOCalloutDevice"): out.append(node["IOCalloutDevice"])
        for c in node.get("IORegistryEntryChildren", []): _walk_ports(c, out, False)
    return out

def _all_devs(nodes, out):
    for n in nodes:
        if _is_dev(n): out.append(n)
        if isinstance(n, dict): _all_devs(n.get("IORegistryEntryChildren", []), out)
    return out

def _ioreg():
    """Raw plist bytes of the USB device tree (replaced by tests)."""
    r = subprocess.run(["ioreg", "-a", "-r", "-c", "IOUSBHostDevice", "-l"], capture_output=True)
    if r.returncode: raise RuntimeError(f"ioreg failed: {r.stderr.decode()[:300]}")
    return r.stdout

def devices(include_hubs=False):
    raw = _ioreg()
    if not raw.strip(): return []
    out = []
    for d in _all_devs(plistlib.loads(raw), []):   # -r returns top-level matches; devices behind hubs are nested
        name = d.get("USB Product Name") or d.get("kUSBProductString") or d.get("IORegistryEntryName", "?")
        if not include_hubs and (d.get("bDeviceClass") == 9 or "Hub" in name or "BILLBOARD" in name.upper()): continue
        vid, pid = int(d.get("idVendor", 0)), int(d.get("idProduct", 0))
        out.append(Device(name, d.get("USB Vendor Name") or d.get("kUSBVendorString", ""), vid, pid,
                          d.get("USB Serial Number") or d.get("kUSBSerialNumberString", ""),
                          f"0x{int(d.get('locationID', 0)):08x}", SPEEDS.get(d.get("USBSpeed", d.get("Device Speed")), ""),
                          sorted(set(_walk_ports(d, []))), KNOWN.get((vid, pid), "")))
    return out

def find(vid=None, pid=None, name=None):
    return [d for d in devices() if (vid is None or d.vid == vid) and (pid is None or d.pid == pid)
            and (name is None or name.lower() in (d.name + " " + d.vendor).lower())]

def ports(): return sorted(glob.glob("/dev/cu.*"))

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="List USB devices"); ap.add_argument("--all", action="store_true"); ap.add_argument("--json", action="store_true")
    a = ap.parse_args(); ds = devices(a.all)
    if a.json: print(json.dumps([asdict(d) | {"id": d.id} for d in ds], indent=1))
    else:
        for d in ds:
            print(f"{d.id}  {d.vendor} — {d.name}  serial={d.serial or '-'}  {d.speed}  ports={','.join(d.ports) or '-'}")
            if d.known: print(f"           ↳ {d.known}")
        if not ds: print("(no USB devices besides hubs; --all shows hubs)")
