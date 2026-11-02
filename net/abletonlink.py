"""Ableton Link listener: follow the tempo, beat and bar phase of DJ/music apps on the LAN (rekordbox, djay, Ableton, Traktor…).
Link peers announce their session timeline by UDP multicast (224.76.78.75:20808, "_asdp_v1" messages) and answer
clock-measurement pings ("_link_v1"). This module only LISTENS and measures; it never announces itself, so it cannot
join, start, stop or change anyone's session tempo.
CLI:
  python3 net/abletonlink.py watch [--seconds 30]     peers, tempo, beat and bar position as they change
Python:
  ln = LinkListener().start()                         background threads (multicast receive + ping measurement)
  ln.tempo -> float|None (BPM)    ln.beat(host_us=None) -> float|None (session beats now)   ln.phase(quantum=4) -> 0..quantum
  ln.playing -> bool|None (start/stop sync, only if a peer shares it)   ln.peers -> {node_id_hex: {...}}   ln.synced -> bool
  ln.status() -> dict (tempo, beat, peers, session, offset_us, measured_at)          ln.stop()
  host_us() -> int  this process's monotonic clock in microseconds (the "host time" every beat() refers to)
  Codec (pure, tested): encode_entries / decode_entries, parse_discovery(bytes) -> (type, ttl, group, node, entries),
  parse_measurement(bytes) -> (type, entries), Timeline(micros_per_beat, beat_origin_ub, time_origin_us).to_beats(ghost_us)
  LinkPeer(...) — a minimal announcing/ping-answering peer, ONLY for tests/rehearsal on a private group/port.
How beats are derived: a peer's "tmln" entry maps ghost time → beats; ghost = host + offset, and the offset is measured
  Link-style (ping/pong exchanges with a peer's measurement endpoint, ~50 samples, median). Until a measurement
  succeeds tempo is known but beat()/phase() return None (synced False).
Rekordbox: enable LINK in Performance mode (Export mode has no Link). djay: Settings → Ableton Link. Same LAN or Mac.
Fails: OSError if the UDP port can't be bound (another listener without SO_REUSEPORT). No peers → tempo None.
Limits: IPv4 only; one session tracked (the one with most peers); no track/title information exists in Link;
  measurement assumes symmetric network delay (as Link does). Protocol details from the open-source Link SDK
  (github.com/Ableton/link, include/ableton/discovery + link/v1) — verified against our own peer, not yet against rekordbox.
Depends on: net/lan (interface addresses). Used by: lighting/clock.py (LinkClock)."""
import sys, time, struct, socket, threading, statistics, argparse, random
from dataclasses import dataclass
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

GROUP, PORT = "224.76.78.75", 20808
DISC_HDR, MEAS_HDR = b"_asdp_v\x01", b"_link_v\x01"
ALIVE, RESPONSE, BYEBYE = 1, 2, 3
PING, PONG = 1, 2
K = lambda s: struct.unpack(">I", s.encode())[0]
TMLN, SESS, STST, MEP4, HT, GT, PGT = (K(x) for x in ("tmln", "sess", "stst", "mep4", "__ht", "__gt", "_pgt"))


def host_us(): return time.monotonic_ns() // 1000


# ---------- codec ----------
def encode_entries(entries):
    """[(key_int, value_bytes)] → payload bytes (key u32 BE, size u32 BE, value)."""
    return b"".join(struct.pack(">II", k, len(v)) + v for k, v in entries)


def decode_entries(buf):
    out, i = {}, 0
    while i + 8 <= len(buf):
        k, n = struct.unpack_from(">II", buf, i); i += 8
        if i + n > len(buf): raise ValueError("truncated Link payload entry")
        out[k] = buf[i:i + n]; i += n
    return out


def parse_discovery(data):
    if not data.startswith(DISC_HDR) or len(data) < 20: raise ValueError("not a Link discovery message")
    mtype, ttl, group = struct.unpack_from(">BBH", data, 8)
    return mtype, ttl, group, data[12:20], decode_entries(data[20:])


def parse_measurement(data):
    if not data.startswith(MEAS_HDR) or len(data) < 9: raise ValueError("not a Link measurement message")
    return data[8], decode_entries(data[9:])


@dataclass
class Timeline:
    micros_per_beat: int
    beat_origin_ub: int          # micro-beats
    time_origin_us: int          # ghost time

    @property
    def bpm(self): return 60e6 / self.micros_per_beat

    def to_beats(self, ghost_us): return (self.beat_origin_ub + (ghost_us - self.time_origin_us) * 1e6 / self.micros_per_beat) / 1e6

    @classmethod
    def decode(cls, b): return cls(*struct.unpack(">qqq", b[:24]))

    def encode(self): return struct.pack(">qqq", self.micros_per_beat, self.beat_origin_ub, self.time_origin_us)


def _i64(b): return struct.unpack(">q", b[:8])[0]


# ---------- listener ----------
class LinkListener:
    def __init__(self, group=GROUP, port=PORT, ifaces=None, samples=50):
        self.group, self.port, self.samples = group, port, samples
        self.ifaces = ifaces
        self.peers, self.offset_us, self.measured_at, self.session = {}, None, None, None
        self._stop = threading.Event(); self.lock = threading.Lock(); self.error = None

    def _sockets(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"): s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("", self.port))
        ips = self.ifaces
        if ips is None:
            try:
                from net.lan import ipv4_addresses
                ips = [ip for _, ip in ipv4_addresses(loopback=True)]
            except Exception: ips = []
        joined = 0
        for ip in ips or ["0.0.0.0"]:
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(self.group) + socket.inet_aton(ip)); joined += 1
            except OSError: pass
        if not joined: s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(self.group) + socket.inet_aton("0.0.0.0"))
        s.settimeout(0.5); self.sock = s
        m = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); m.bind(("0.0.0.0", 0)); m.settimeout(0.05); self.msock = m

    def start(self):
        self._sockets()
        for fn in (self._recv, self._measure):
            threading.Thread(target=fn, daemon=True, name=f"link-{fn.__name__}").start()
        return self

    def stop(self):
        self._stop.set()
        for s in (getattr(self, "sock", None), getattr(self, "msock", None)):
            if s: s.close()

    def handle(self, data, addr, now_s=None):
        """Process one discovery datagram (public for tests)."""
        try: mtype, ttl, _, node, e = parse_discovery(data)
        except ValueError: return
        key = node.hex(); now_s = time.monotonic() if now_s is None else now_s
        with self.lock:
            if mtype == BYEBYE: self.peers.pop(key, None); return
            if TMLN not in e: return
            p = {"timeline": Timeline.decode(e[TMLN]), "session": e.get(SESS, b"").hex(), "expires": now_s + max(ttl, 1),
                 "addr": addr[0], "playing": None}
            if STST in e and len(e[STST]) >= 17: p["playing"] = bool(e[STST][0])
            if MEP4 in e and len(e[MEP4]) >= 6:
                ip, port = struct.unpack(">IH", e[MEP4][:6]); p["mep"] = (socket.inet_ntoa(struct.pack(">I", ip)), port)
            self.peers[key] = p

    def _recv(self):
        while not self._stop.is_set():
            try: data, addr = self.sock.recvfrom(2048)
            except socket.timeout: data = None
            except OSError: break
            if data: self.handle(data, addr)
            now = time.monotonic()
            with self.lock:
                for k in [k for k, p in self.peers.items() if p["expires"] < now]: self.peers.pop(k)
                sessions = {}
                for p in self.peers.values(): sessions[p["session"]] = sessions.get(p["session"], 0) + 1
                new = max(sessions, key=sessions.get) if sessions else None
                if new != self.session: self.session, self.offset_us, self.measured_at = new, None, None

    def _peer(self):
        with self.lock:
            cands = [p for p in self.peers.values() if p["session"] == self.session and "mep" in p]
            return random.choice(cands) if cands else None

    def measure(self, peer):
        """Link's ping/pong ghost-time measurement against one peer → offset_us (ghost - host) or None."""
        data, sess = [], bytes.fromhex(peer["session"]); payload = [(HT, struct.pack(">q", host_us()))]
        tries = 0
        while len(data) < self.samples and tries < self.samples * 3 and not self._stop.is_set():
            tries += 1
            try:
                self.msock.sendto(MEAS_HDR + bytes([PING]) + encode_entries(payload), peer["mep"])
                pkt, _ = self.msock.recvfrom(2048)
            except (socket.timeout, OSError): continue
            now = host_us()
            try: mtype, e = parse_measurement(pkt)
            except ValueError: continue
            if mtype != PONG or (SESS in e and e[SESS] != sess): continue
            gt = _i64(e[GT]) if GT in e else 0; prev_h = _i64(e[HT]) if HT in e else 0; prev_g = _i64(e[PGT]) if PGT in e else 0
            if gt and prev_h:
                data.append(gt - (now + prev_h) / 2)
                if prev_g: data.append((gt + prev_g) / 2 - prev_h)
            payload = [(HT, struct.pack(">q", now)), (PGT, struct.pack(">q", gt))]
        return statistics.median(data) if len(data) >= 6 else None

    def _measure(self):
        while not self._stop.is_set():
            stale = self.measured_at is None or time.monotonic() - self.measured_at > 15
            p = self._peer() if stale else None
            if p:
                off = self.measure(p)
                if off is not None:
                    with self.lock: self.offset_us, self.measured_at = off, time.monotonic()
            self._stop.wait(0.2 if p is None or self.offset_us is None else 1.0)

    # ---- queries ----
    def _timeline(self):
        with self.lock:
            for p in self.peers.values():
                if p["session"] == self.session: return p["timeline"], p.get("playing")
        return None, None

    @property
    def tempo(self):
        tl, _ = self._timeline(); return round(tl.bpm, 3) if tl else None

    @property
    def synced(self): return self.offset_us is not None and self._timeline()[0] is not None

    @property
    def playing(self): return self._timeline()[1]

    def beat(self, host=None):
        tl, _ = self._timeline()
        if tl is None or self.offset_us is None: return None
        return tl.to_beats((host_us() if host is None else host) + self.offset_us)

    def phase(self, quantum=4, host=None):
        b = self.beat(host); return None if b is None else b % quantum

    def status(self):
        with self.lock: peers = {k: {"addr": p["addr"], "session": p["session"], "bpm": round(p["timeline"].bpm, 2)} for k, p in self.peers.items()}
        return {"tempo": self.tempo, "beat": self.beat(), "synced": self.synced, "playing": self.playing, "peers": peers,
                "session": self.session, "offset_us": self.offset_us}


# ---------- test/rehearsal peer ----------
class LinkPeer:
    """Announces a timeline and answers pings like a Link peer would. ghost = host + ghost_offset_us. Use on a private
    group/port (tests) — on the real group it would appear in DJs' Link sessions."""
    def __init__(self, bpm=120.0, group=GROUP, port=PORT, ghost_offset_us=7_000_000, beat_at_origin=0.0, ttl=5, iface="127.0.0.1"):
        if (group, port) == (GROUP, PORT) and iface != "127.0.0.1": raise ValueError("LinkPeer on the real Link group: refusing")
        self.node, self.sess = random.randbytes(8), random.randbytes(8)
        self.group, self.port, self.iface, self.ttl, self.off = group, port, iface, ttl, ghost_offset_us
        g0 = host_us() + ghost_offset_us
        self.timeline = Timeline(int(round(60e6 / bpm)), int(beat_at_origin * 1e6), g0)
        self._stop = threading.Event()

    def ghost(self): return host_us() + self.off

    def message(self, mtype=ALIVE):
        mep = struct.pack(">IH", struct.unpack(">I", socket.inet_aton(self.iface))[0], self.msock.getsockname()[1])
        body = encode_entries([(TMLN, self.timeline.encode()), (SESS, self.sess), (MEP4, mep)])
        return DISC_HDR + struct.pack(">BBH", mtype, self.ttl, 0) + self.node + body

    def start(self):
        self.msock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); self.msock.bind((self.iface, 0)); self.msock.settimeout(0.1)
        self.out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.iface))
        self.out.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        threading.Thread(target=self._announce, daemon=True).start(); threading.Thread(target=self._pong, daemon=True).start()
        return self

    def _announce(self):
        while not self._stop.is_set():
            try: self.out.sendto(self.message(), (self.group, self.port))
            except OSError: pass
            self._stop.wait(0.25)

    def _pong(self):
        while not self._stop.is_set():
            try: pkt, addr = self.msock.recvfrom(2048)
            except (socket.timeout, OSError): continue
            try: mtype, _ = parse_measurement(pkt)
            except ValueError: continue
            if mtype == PING:
                reply = MEAS_HDR + bytes([PONG]) + encode_entries([(SESS, self.sess), (GT, struct.pack(">q", self.ghost()))]) + pkt[9:]
                self.msock.sendto(reply, addr)

    def set_bpm(self, bpm):
        """Tempo change keeping the beat continuous (what a DJ nudging the tempo does)."""
        g = self.ghost(); b = self.timeline.to_beats(g)
        self.timeline = Timeline(int(round(60e6 / bpm)), int(b * 1e6), g)

    def stop(self):
        self._stop.set()
        try: self.out.sendto(self.message(BYEBYE), (self.group, self.port))
        except OSError: pass
        self.msock.close(); self.out.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0]); sp = ap.add_subparsers(dest="cmd", required=True)
    w = sp.add_parser("watch"); w.add_argument("--seconds", type=float, default=30)
    a = ap.parse_args()
    ln = LinkListener().start(); end = time.time() + a.seconds; last = None
    print(f"listening on {GROUP}:{PORT} for {a.seconds:.0f} s (enable LINK in rekordbox Performance mode / djay)", flush=True)
    while time.time() < end:
        st = ln.status()
        line = (f"{len(st['peers'])} peer(s)  tempo {st['tempo']}  " +
                (f"beat {st['beat']:.2f}  bar {int(st['beat'] // 4) + 1} beat {int(st['beat'] % 4) + 1}" if st["beat"] is not None else "not yet measured"))
        if line != last or st["beat"] is not None: print(line, flush=True); last = line
        time.sleep(0.5)
    ln.stop()


if __name__ == "__main__": main()
