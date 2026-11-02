"""LAN discovery: lists devices on the current Wi-Fi/Ethernet subnet from the ARP cache, with MAC vendor (IEEE OUI) lookup.
CLI: python3 net/lan.py [--iface en0] [--json]
  interface() -> {"iface","ip","netmask","gateway"}      primary IPv4 interface (default route via netstat/ifconfig)
  ipv4_addresses(loopback=False) -> [(iface, ip)]        every interface's IPv4 (multicast joins on Wi-Fi + Ethernet)
  arp_table(iface=None) -> [(ip, mac)]                   parsed `arp -an`, incomplete entries and broadcast/multicast dropped
  vendor(mac) -> str | None                              IEEE OUI lookup (out/data/oui.csv, downloaded once); None if unknown
  is_random(mac) -> bool                                 locally-administered bit set: a privacy/randomized MAC (phones, laptops)
  scan(iface=None) -> [{"ip","mac","vendor","random","self","gateway"}]   sorted by IP
Passive only: it reads what the OS already knows, so hosts the Mac has not talked to recently are missing.
For an active probe of the whole subnet (finds every live host + its TCP services) use net/sweep.py.
Fails: LookupError if there is no IPv4 interface; RuntimeError if arp is unavailable. Vendor lookup needs network once.
Used by: humans/agents, net/abletonlink.py (ipv4_addresses).
Limits: macOS-style `netstat -rn`/`ifconfig`/`arp -an` output parsing (IPv4 only, 10 s subprocess timeout); the OUI table is
  cached at out/data/oui.csv after a one-time download; passive ARP cache only."""
import sys, csv, json, re, subprocess, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
OUI = Path(__file__).resolve().parents[1] / "out" / "data" / "oui.csv"
_oui = {}

def _run(*cmd):
    try: return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired) as e: raise RuntimeError(f"{cmd[0]} failed: {e}")

def interface():
    gw = re.search(r"^default\s+(\d+\.\d+\.\d+\.\d+)\s+\S+\s+(\S+)", _run("netstat", "-rn", "-f", "inet"), re.M)
    if not gw: raise LookupError("no IPv4 default route")
    iface = gw.group(2)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-f]+)", _run("ifconfig", iface))
    if not m: raise LookupError(f"{iface} has no IPv4 address")
    n = int(m.group(2), 16)
    return {"iface": iface, "ip": m.group(1), "netmask": ".".join(str(n >> s & 255) for s in (24, 16, 8, 0)), "gateway": gw.group(1)}

def ipv4_addresses(loopback=False):
    out, iface = [], None
    for line in _run("ifconfig").splitlines():
        m = re.match(r"^(\S+?):? flags=", line)
        if m: iface = m.group(1); continue
        m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", line)
        if m and iface and (loopback or not m.group(1).startswith("127.")): out.append((iface, m.group(1)))
    return out

def arp_table(iface=None):
    out = []
    for ip, mac, ifc in re.findall(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-f:]+) on (\S+)", _run("arp", "-an")):
        mac = ":".join(p.zfill(2) for p in mac.split(":"))
        if (iface and ifc != iface) or mac == "ff:ff:ff:ff:ff:ff" or ip.startswith(("224.", "239.", "255.")) or ip.endswith(".255"): continue
        out.append((ip, mac))
    return sorted(out, key=lambda t: tuple(map(int, t[0].split("."))))

def _load():
    if not _oui:
        if not OUI.exists():
            from web.fetch import download
            download("https://standards-oui.ieee.org/oui/oui.csv", OUI)
        with open(OUI, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) > 2 and row[0] == "MA-L": _oui[row[1].upper()] = row[2]

def vendor(mac):
    _load(); return _oui.get(mac.replace(":", "").upper()[:6])

def is_random(mac): return bool(int(mac.split(":")[0], 16) & 2)

def scan(iface=None):
    info = interface(); iface = iface or info["iface"]
    rows = [{"ip": ip, "mac": mac, "vendor": vendor(mac), "random": is_random(mac), "self": ip == info["ip"], "gateway": ip == info["gateway"]}
            for ip, mac in arp_table(iface)]
    return rows

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="passive LAN discovery"); ap.add_argument("--iface"); ap.add_argument("--json", action="store_true")
    a = ap.parse_args(); rows = scan(a.iface)
    if a.json: print(json.dumps(rows, indent=1)); sys.exit()
    for r in rows:
        tag = " (this Mac)" if r["self"] else " (gateway)" if r["gateway"] else ""
        print(f"{r['ip']:<14} {r['mac']:<17} {r['vendor'] or ('randomized MAC' if r['random'] else 'unknown vendor')}{tag}")
