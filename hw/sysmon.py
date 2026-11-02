"""System monitor: sample this Mac's CPU, memory, swap, disk I/O, network, battery and top processes over time.
CLI:    python3 hw/sysmon.py now                                   one snapshot, human readable
        python3 hw/sysmon.py record -i 10 -d 3600 [-o out/sysmon/run.jsonl]   sample every 10 s for an hour
        python3 hw/sysmon.py summary out/sysmon/run.jsonl          min/mean/p95/max per metric + anomalies (text)
Python: s = sample() -> dict (cumulative counters + gauges, see SAMPLE KEYS)
        r = rates(prev, cur) -> dict of per-interval metrics (cpu_pct, mem_used_gb, disk_read_mbs, net_rx_kbs, ...)
        record(path, interval, duration, top=5) appends one JSON line per interval: {"t": epoch, **rates, "top": [...]}
        proc_cmdline(pid) -> [argv...]   (identify a process when ps is blocked)
        find(pattern) -> [(pid, argv)]   processes with an argument equal to / ending with pattern (pgrep -f substitute)
        busiest(seconds=5, n=8) -> [(pid, name, cpu_pct, rss_mb, argv)]   live per-process CPU ranking (a `top` substitute)
        proc_rss(pid) -> bytes | None   one process's resident memory (used by hw/guard.py)
        footprint(pid) -> bytes | None  physical footprint = Activity Monitor "Memory" (incl. compressed; browser/memguard.py)
        pressure() -> "normal" | "warn" | "critical" | "?"   kernel memory-pressure level (cheap)
        load(path) -> list of rows;  summarize(rows) -> {metric: {min, mean, p95, max}};  anomalies(rows) -> [str]
How:    mach host_statistics (CPU ticks) and host_statistics64 (VM pages) via ctypes; sysctl (swap, pressure, memsize);
        libproc proc_pidinfo (per-process CPU time + RSS, own-visible processes only); `ioreg` AppleSmartBattery
        (charge, cycles, temperature, voltage, amperage) and IOBlockStorageDriver (cumulative disk bytes); `netstat -ib`.
Fails:  ValueError for interval <= 0 / duration < interval; RuntimeError if a mach call fails. Missing battery
        (desktop Mac) -> battery fields are None. Processes the sandbox can't inspect are skipped silently.
Limits: `top`/`ps` are blocked here, so per-process CPU comes from libproc deltas (sampled processes only, CPU% is of
        one core, like Activity Monitor). No GPU or per-core metrics; no temperatures except the battery's.
        Memory "used" = app (internal - purgeable) + wired + compressed, Activity Monitor's definition.
Used by: tasks/mac_health_report.py (charts + report), humans."""
import sys, os, json, time, ctypes, shutil, plistlib, argparse, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_libc = ctypes.CDLL(None)
_libc.mach_host_self.restype = ctypes.c_uint32
_PAGE = os.sysconf("SC_PAGE_SIZE")

class _TB(ctypes.Structure): _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]
_tb = _TB(); _libc.mach_timebase_info(ctypes.byref(_tb))
_NS_PER_TICK = _tb.numer / _tb.denom

class _VM64(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in ("free", "active", "inactive", "wire")] + \
               [(n, ctypes.c_uint64) for n in ("zero_fill", "reactivations", "pageins", "pageouts", "faults",
                                               "cow_faults", "lookups", "hits", "purges")] + \
               [(n, ctypes.c_uint32) for n in ("purgeable", "speculative")] + \
               [(n, ctypes.c_uint64) for n in ("decompressions", "compressions", "swapins", "swapouts")] + \
               [(n, ctypes.c_uint32) for n in ("compressor", "throttled", "external", "internal")] + \
               [("total_uncompressed", ctypes.c_uint64)]

class _TaskInfo(ctypes.Structure):
    _fields_ = [("virtual", ctypes.c_uint64), ("resident", ctypes.c_uint64), ("user", ctypes.c_uint64),
                ("system", ctypes.c_uint64), ("threads_user", ctypes.c_uint64), ("threads_system", ctypes.c_uint64)] + \
               [(n, ctypes.c_int32) for n in ("policy", "faults", "pageins", "cow", "msg_sent", "msg_recv",
                                             "sc_mach", "sc_unix", "csw", "threadnum", "running", "priority")]

def _sysctl(name, ctype=ctypes.c_int64):
    v = ctype(); n = ctypes.c_size_t(ctypes.sizeof(v))
    if _libc.sysctlbyname(name.encode(), ctypes.byref(v), ctypes.byref(n), None, 0) != 0:
        return None
    return v.value

class _Swap(ctypes.Structure):
    _fields_ = [("total", ctypes.c_uint64), ("avail", ctypes.c_uint64), ("used", ctypes.c_uint64),
                ("pagesize", ctypes.c_uint32), ("encrypted", ctypes.c_bool)]

def _cpu_ticks():
    info = (ctypes.c_uint32 * 4)(); cnt = ctypes.c_uint32(4)
    if _libc.host_statistics(_libc.mach_host_self(), 3, info, ctypes.byref(cnt)) != 0:
        raise RuntimeError("host_statistics(HOST_CPU_LOAD_INFO) failed")
    return {"user": info[0], "system": info[1], "idle": info[2], "nice": info[3]}

def _vm():
    vm = _VM64(); cnt = ctypes.c_uint32(ctypes.sizeof(_VM64) // 4)
    if _libc.host_statistics64(_libc.mach_host_self(), 4, ctypes.byref(vm), ctypes.byref(cnt)) != 0:
        raise RuntimeError("host_statistics64(HOST_VM_INFO64) failed")
    return vm

def _ioreg(cls, by_name=False):
    try:
        out = subprocess.run(["ioreg", "-rn" if by_name else "-rc", cls, "-a"], capture_output=True, timeout=10).stdout
        return plistlib.loads(out) if out.strip() else []
    except Exception:
        return []

def _battery():
    b = _ioreg("AppleSmartBattery", by_name=True)
    if not b: return None
    b = b[0]
    amps = b.get("InstantAmperage", b.get("Amperage", 0))
    if amps >= 2**63: amps -= 2**64                       # ioreg reports signed currents as unsigned 64-bit
    return {"pct": b.get("CurrentCapacity"), "charging": bool(b.get("IsCharging")),
            "external": bool(b.get("ExternalConnected")), "cycles": b.get("CycleCount"),
            "temp_c": round(b.get("Temperature", 0) / 100, 2), "voltage_v": b.get("Voltage", 0) / 1000,
            "amps": amps / 1000, "health_pct": round(100 * b.get("AppleRawMaxCapacity", 0) / b["DesignCapacity"], 1)
            if b.get("DesignCapacity") else None}

def _disk_bytes():
    r = w = 0
    for d in _ioreg("IOBlockStorageDriver"):
        st = d.get("Statistics", {})
        r += st.get("Bytes (Read)", 0); w += st.get("Bytes (Write)", 0)
    return r, w

def _net_bytes():
    rx = tx = 0
    try:
        lines = subprocess.run(["netstat", "-ib"], capture_output=True, text=True, timeout=10).stdout.splitlines()
    except Exception:
        return 0, 0
    for ln in lines[1:]:
        f = ln.split()
        if len(f) >= 10 and f[2].startswith("<Link#") and not f[0].startswith("lo"):
            try: rx += int(f[-5]); tx += int(f[-2])
            except ValueError: pass
    return rx, tx

_lp = ctypes.CDLL("/usr/lib/libproc.dylib")

def _procs():
    """{pid: (name, cpu_seconds, rss_bytes)} for every process libproc lets us inspect."""
    buf = (ctypes.c_int * 8192)()
    n = _lp.proc_listpids(1, 0, buf, ctypes.sizeof(buf)) // 4
    out, ti, name = {}, _TaskInfo(), ctypes.create_string_buffer(256)
    for pid in buf[:n]:
        if pid <= 0: continue
        if _lp.proc_pidinfo(pid, 4, ctypes.c_uint64(0), ctypes.byref(ti), ctypes.sizeof(ti)) != ctypes.sizeof(ti):
            continue
        _lp.proc_name(pid, name, 256)
        out[pid] = (name.value.decode(errors="replace") or f"pid{pid}", (ti.user + ti.system) * _NS_PER_TICK / 1e9,
                    ti.resident)
    return out

def pressure():
    """The kernel's memory-pressure level now: "normal" | "warn" | "critical" ("?" if unreadable). One sysctl, cheap."""
    return {1: "normal", 2: "warn", 4: "critical"}.get(_sysctl("kern.memorystatus_vm_pressure_level", ctypes.c_int32), "?")

def proc_rss(pid):
    """Resident memory of one process in bytes, or None if it's gone / not inspectable (cheap: one libproc call)."""
    ti = _TaskInfo()
    if _lp.proc_pidinfo(int(pid), 4, ctypes.c_uint64(0), ctypes.byref(ti), ctypes.sizeof(ti)) != ctypes.sizeof(ti): return None
    return ti.resident

def footprint(pid):
    """Physical footprint of one process in bytes (what Activity Monitor's "Memory" column shows: resident + compressed +
    swapped dirty memory), or None if gone / not inspectable. Prefer this over proc_rss under memory pressure, when RSS
    hides everything the compressor has squeezed out."""
    buf = (ctypes.c_uint64 * 40)()
    if _lp.proc_pid_rusage(int(pid), 2, buf) != 0: return None          # RUSAGE_INFO_V2: uuid (2 words), then counters
    return buf[2 + 7]                                                   # ri_phys_footprint

def proc_cmdline(pid):
    """argv of a process as a list of str (ps is blocked here). [] if it's gone or not inspectable."""
    mib = (ctypes.c_int * 3)(1, 49, int(pid))                    # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(0)
    if _libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not size.value: return []
    buf = ctypes.create_string_buffer(size.value)
    if _libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0: return []
    raw = buf.raw[:size.value]; argc = int.from_bytes(raw[:4], "little")
    rest = raw[4:].split(b"\0")
    rest = [x for x in rest[1:] if x] if rest else []                 # skip exec path and padding NULs
    return [x.decode(errors="replace") for x in rest[:argc]]

def find(pattern):
    """Processes with an argv element that equals or ends with `pattern` (e.g. "sweep.py" matches
    ".../tasks/sweep.py"): [(pid, argv)] — a `pgrep -f` substitute. Matching whole arguments (not the joined command
    line) avoids hitting shells whose `bash -c` script merely mentions the pattern (that killed its own caller once)."""
    import os
    if not pattern: raise ValueError("pattern required")
    me = os.getpid()
    out = []
    for pid in _procs():
        a = proc_cmdline(pid) if pid != me else []
        if any(x == pattern or x.endswith(pattern) for x in a): out.append((pid, a))
    return out

def busiest(seconds=5.0, n=8):
    """Live per-process CPU over `seconds`: [(pid, name, cpu_pct_of_one_core, rss_mb, argv)] busiest first."""
    a = _procs(); time.sleep(seconds); b = _procs()
    out = [(pid, nm, round(100 * (cs - a[pid][1]) / seconds, 1), round(rss / 2**20)) for pid, (nm, cs, rss) in b.items() if pid in a]
    out.sort(key=lambda x: -x[2])
    return [(pid, nm, pct, rss, proc_cmdline(pid)) for pid, nm, pct, rss in out[:n]]

def biggest(n=10, group=False, by="footprint"):
    """Memory hogs now: [(pid, name, mb, argv)] largest first; by="footprint" (default: Activity Monitor's number,
    includes compressed/swapped memory) or "rss". group=True sums by process name ->
    [(name, count, rss_mb)] (WebKit/Chrome-style apps spread over many processes). Note: RSS double-counts shared pages."""
    ps = _procs()
    if by == "footprint":
        ps = {pid: (nm, cs, footprint(pid) or rss) for pid, (nm, cs, rss) in ps.items()}
    elif by != "rss": raise ValueError("by must be 'footprint' or 'rss'")
    if group:
        g = {}
        for pid, (nm, _, rss) in ps.items(): c, m = g.get(nm, (0, 0)); g[nm] = (c + 1, m + rss)
        return sorted(((nm, c, round(m / 2**20)) for nm, (c, m) in g.items()), key=lambda x: -x[2])[:n]
    out = sorted(((pid, nm, round(rss / 2**20)) for pid, (nm, _, rss) in ps.items()), key=lambda x: -x[2])[:n]
    return [(pid, nm, mb, proc_cmdline(pid)) for pid, nm, mb in out]

def sample(procs=True):
    """One snapshot: cumulative counters (cpu ticks, disk/net bytes, process CPU) + gauges (memory, swap, battery)."""
    vm, sw = _vm(), _Swap()
    n = ctypes.c_size_t(ctypes.sizeof(sw))
    _libc.sysctlbyname(b"vm.swapusage", ctypes.byref(sw), ctypes.byref(n), None, 0)
    total = _sysctl("hw.memsize")
    used = ((vm.internal - vm.purgeable) + vm.wire + vm.compressor) * _PAGE
    du = shutil.disk_usage("/")
    dr, dw = _disk_bytes(); nr, nt = _net_bytes()
    return {"t": time.time(), "cpu": _cpu_ticks(), "ncpu": os.cpu_count(), "load1": os.getloadavg()[0],
            "mem_total": total, "mem_used": used, "mem_compressed": vm.compressor * _PAGE, "mem_wired": vm.wire * _PAGE,
            "mem_free": (vm.free + vm.speculative) * _PAGE, "pageouts": vm.pageouts, "swapouts": vm.swapouts,
            "swap_used": sw.used, "swap_total": sw.total,
            "pressure": pressure(),
            "disk_free": du.free, "disk_total": du.total, "disk_read": dr, "disk_write": dw,
            "net_rx": nr, "net_tx": nt, "battery": _battery(), "procs": _procs() if procs else {}}

def rates(prev, cur, top=5):
    """Per-interval metrics between two samples. cpu_pct is whole-machine (0-100); top lists the processes that
    used the most CPU in the interval as [name, cpu_pct_of_one_core, rss_mb]."""
    dt = cur["t"] - prev["t"]
    if dt <= 0: raise ValueError("samples must be in time order")
    d = {k: cur["cpu"][k] - prev["cpu"][k] for k in cur["cpu"]}
    tot = sum(d.values()) or 1
    G, M = 1024 ** 3, 1024 ** 2
    b = cur["battery"] or {}
    ps = []
    for pid, (name, cs, rss) in cur["procs"].items():
        if pid in prev["procs"] and prev["procs"][pid][0] == name:
            ps.append((name, round(100 * (cs - prev["procs"][pid][1]) / dt, 1), round(rss / M), pid))
    ps.sort(key=lambda p: -p[1])
    return {"t": round(cur["t"], 1), "dt": round(dt, 2),
            "cpu_pct": round(100 * (tot - d["idle"]) / tot, 1), "cpu_user_pct": round(100 * (d["user"] + d["nice"]) / tot, 1),
            "cpu_sys_pct": round(100 * d["system"] / tot, 1), "load1": round(cur["load1"], 2),
            "mem_used_gb": round(cur["mem_used"] / G, 2), "mem_total_gb": round(cur["mem_total"] / G, 1),
            "mem_compressed_gb": round(cur["mem_compressed"] / G, 2), "swap_used_gb": round(cur["swap_used"] / G, 2),
            "pageouts_s": round((cur["pageouts"] - prev["pageouts"]) / dt, 1),
            "swapouts_s": round((cur["swapouts"] - prev["swapouts"]) / dt, 1), "pressure": cur["pressure"],
            "disk_free_gb": round(cur["disk_free"] / G, 2), "disk_read_mbs": round((cur["disk_read"] - prev["disk_read"]) / dt / M, 3),
            "disk_write_mbs": round((cur["disk_write"] - prev["disk_write"]) / dt / M, 3),
            "net_rx_kbs": round((cur["net_rx"] - prev["net_rx"]) / dt / 1024, 2),
            "net_tx_kbs": round((cur["net_tx"] - prev["net_tx"]) / dt / 1024, 2),
            "batt_pct": b.get("pct"), "batt_charging": b.get("charging"), "batt_external": b.get("external"),
            "batt_temp_c": b.get("temp_c"), "batt_amps": b.get("amps"), "batt_cycles": b.get("cycles"),
            "batt_health_pct": b.get("health_pct"), "nprocs": len(cur["procs"]), "top": ps[:top]}

def record(path, interval=10.0, duration=3600.0, top=5, quiet=True):
    """Append one rates() row per interval to JSONL `path` until `duration` seconds have passed. Returns row count."""
    if interval <= 0: raise ValueError("interval must be > 0")
    if duration < interval: raise ValueError("duration must be >= interval")
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    end, prev, n = time.time() + duration, sample(), 0
    while time.time() < end:
        time.sleep(max(0.0, prev["t"] + interval - time.time()))
        cur = sample(); row = rates(prev, cur, top); prev = cur
        with path.open("a") as f: f.write(json.dumps(row) + "\n")
        n += 1
        if not quiet: print(f"{time.strftime('%H:%M:%S')} cpu {row['cpu_pct']:5.1f}%  mem {row['mem_used_gb']:.1f} GB  "
                            f"batt {row['batt_pct']}%  top {row['top'][:1]}", flush=True)
    return n

def load(path):
    return [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]

NUMERIC = ["cpu_pct", "cpu_user_pct", "cpu_sys_pct", "load1", "mem_used_gb", "mem_compressed_gb", "swap_used_gb",
           "pageouts_s", "swapouts_s", "disk_free_gb", "disk_read_mbs", "disk_write_mbs", "net_rx_kbs", "net_tx_kbs",
           "batt_pct", "batt_temp_c", "batt_amps", "nprocs"]

def summarize(rows):
    """{metric: {min, mean, p95, max}} over rows for every NUMERIC metric present."""
    out = {}
    for k in NUMERIC:
        v = sorted(r[k] for r in rows if r.get(k) is not None)
        if v: out[k] = {"min": v[0], "mean": round(sum(v) / len(v), 3), "p95": v[min(len(v) - 1, int(0.95 * len(v)))], "max": v[-1]}
    return out

def anomalies(rows, z=4.0):
    """Human-readable notes on unusual events: CPU spikes (robust z-score over median/MAD), sustained high CPU,
    memory pressure changes, swapping, disk-space drops, battery drain or heat, and processes that dominated CPU."""
    notes = []
    if len(rows) < 3: return notes
    def robust(k):
        v = sorted(r[k] for r in rows if r.get(k) is not None)
        med = v[len(v) // 2]; mad = sorted(abs(x - med) for x in v)[len(v) // 2] or 1e-9
        return med, mad
    for k, label, unit in [("cpu_pct", "CPU", "%"), ("disk_write_mbs", "disk write", " MB/s"),
                           ("disk_read_mbs", "disk read", " MB/s"), ("net_rx_kbs", "network in", " KB/s")]:
        med, mad = robust(k)
        spikes = [r for r in rows if r.get(k) is not None and (r[k] - med) / (1.4826 * mad) > z and r[k] > med * 2 + 1]
        if spikes:
            worst = max(spikes, key=lambda r: r[k])
            who = f" (busiest CPU process then: {worst['top'][0][0]} {worst['top'][0][1]}%)" if worst.get("top") else ""
            notes.append(f"{label} spikes: {len(spikes)} intervals above the normal {med:g}{unit}; peak {worst[k]:g}{unit} at "
                         f"{time.strftime('%H:%M:%S', time.localtime(worst['t']))}{who}")
    hi = [r for r in rows if r["cpu_pct"] > 80]
    if len(hi) >= 6: notes.append(f"sustained high CPU: {len(hi)} of {len(rows)} intervals above 80%")
    pr = sorted({r["pressure"] for r in rows})
    if pr != ["normal"]: notes.append(f"memory pressure levels seen: {', '.join(pr)}")
    so = sum(r["swapouts_s"] * r["dt"] for r in rows)
    if so > 0: notes.append(f"swapping: {so:.0f} pages swapped out during the run")
    sw = [r["swap_used_gb"] for r in rows]
    if sw[0] and sw[0] > 0.5 * rows[0]["mem_total_gb"]:
        notes.append(f"swap already large at start: {sw[0]:.1f} GB used (RAM {rows[0]['mem_total_gb']} GB)")
    if rows[-1]["disk_free_gb"] < rows[0]["disk_free_gb"] - 1:
        notes.append(f"disk free space dropped {rows[0]['disk_free_gb'] - rows[-1]['disk_free_gb']:.1f} GB")
    bp = [r for r in rows if r.get("batt_pct") is not None]
    if bp:
        hours = (bp[-1]["t"] - bp[0]["t"]) / 3600 or 1
        drain = (bp[0]["batt_pct"] - bp[-1]["batt_pct"]) / hours
        if not bp[-1]["batt_external"] and drain > 15: notes.append(f"fast battery drain: {drain:.0f}%/h on battery")
        if any(not r["batt_external"] for r in bp) and any(r["batt_external"] for r in bp): notes.append("power source changed during the run")
        if max(r["batt_temp_c"] for r in bp) > 40: notes.append(f"battery warm: max {max(r['batt_temp_c'] for r in bp)} °C")
    hogs = {}
    for r in rows:
        for name, pct, *_ in r.get("top", []): hogs[name] = hogs.get(name, 0) + pct * r["dt"] / 100
    tot = sum(hogs.values()) or 1
    for name, cs in sorted(hogs.items(), key=lambda kv: -kv[1])[:3]:
        if cs / tot > 0.3: notes.append(f"{name} used {100 * cs / tot:.0f}% of all sampled process CPU ({cs:.0f} CPU-seconds)")
    return notes

def _fmt_now():
    a = sample(); time.sleep(1); b = sample(); r = rates(a, b)
    for k, v in r.items(): print(f"{k:18} {v}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("now")
    rp = sub.add_parser("record"); rp.add_argument("-i", "--interval", type=float, default=10)
    rp.add_argument("-d", "--duration", type=float, default=3600); rp.add_argument("-o", "--out", default="out/sysmon/run.jsonl")
    rp.add_argument("--top", type=int, default=5); rp.add_argument("-v", action="store_true")
    sp = sub.add_parser("summary"); sp.add_argument("path")
    a = ap.parse_args()
    if a.cmd == "now": _fmt_now()
    elif a.cmd == "record": print(record(a.out, a.interval, a.duration, a.top, quiet=not a.v), "rows ->", a.out)
    else:
        rows = load(a.path)
        for k, v in summarize(rows).items(): print(f"{k:18} {v}")
        print("\n".join("! " + n for n in anomalies(rows)) or "no anomalies")
