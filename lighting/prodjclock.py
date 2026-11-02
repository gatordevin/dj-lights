"""Beat clock that follows Pioneer Pro DJ Link (XDJ-XZ, CDJs, DJM): beat, BPM and bar phase straight from the decks.
LISTEN-ONLY: binds UDP 50001 (beat broadcasts) and 50002 (status, optional) with SO_REUSEADDR/SO_REUSEPORT and never sends
a packet, never announces a virtual CDJ, never touches the DJ network's state. Same interface as the clocks in clock.py:
  c = ProDjLinkClock(bpm=120, listen=True)    c.start() / c.stop()      (listen=False: no sockets, feed packets by hand)
  c.beat(t=None)  beats at monotonic t; beat 0 = a downbeat, bar = 4 beats (beat-in-bar 1 -> beat%4 == 0)
  c.bpm  c.source -> "prodjlink" while following else "internal"   c.paused  c.following
  c.pause/step/nudge   as LinkClock;  c.set_bpm / c.tap / c.nudge / c.rebase  RuntimeError while following (change it on the deck)
  c.state() -> {"source","bpm","beat","paused","prodj_device","prodj_master","prodj_packets","prodj_error",...}
  c.feed(payload, port=50001, t=None)   decode one UDP payload (also what the socket threads call; used by tests/replays)
Packet layout (offsets from the UDP payload start):
  magic "Qspt1WmJOL" 0x00-0x09, type 0x0a.  Beat 0x28 (len 96): device 0x21, nextBeat ms 0x24 (AT 0% PITCH: real gap =
  value/(1+pitch)), pitch 0x54-0x57 ((v-0x100000)/0x100000, 0x55-0x57 used), BPM x100 0x5a (track BPM at 0% pitch),
  beat-in-bar 0x5c (1..4).  Effective BPM = bpm/100 * (1+pitch).  CDJ status 0x0a (50002, len >= 212): device 0x21, flags 0x89
  (bit6 play, bit5 master), BPM 0x92, beat-in-bar 0xa6.  Mixer status 0x29: device 0x21, flags 0x27 (bit5 master).
Following rule: beats come from ONE device — the status-reported master if known (fresh <= 3 s), else the first device heard,
  sticky until it has been silent for the stale timeout.  A beat packet is the beat itself: it anchors the grid; beat number is
  the integer on the running grid whose bar position matches beat-in-bar (first packet: beat-in-bar - 1), small phase errors are
  blended in (gain 0.5, so no visible jump), large ones (> 0.25 beat, e.g. a re-cue/loop) snap.
Stale: no beat packet for max(1.5 s, 2.5 beats) (<= 4 s: deck paused/stopped, cable pulled) -> internal clock continues from the
  last beat at the last tempo with no jump (it is re-based every call while following, as LinkClock does); when packets return,
  the bar phase snaps to the deck's (the DJ's downbeat is the truth) and the beat number keeps counting forward (never backwards).
Contract: bpm 30..300 else packet ignored; beat-in-bar outside 1..4 ignored; wrong magic/short/other types ignored (counted).
Limits: 50002 status is unicast to announced peers, so a passive listener normally sees none (beats alone still work, master
  selection then falls back to first-heard); no network latency compensation (add a nudge by ear; LAN jitter ~1 ms); one process
  can share the ports with others only via REUSEPORT (macOS/Linux delivers broadcasts to all). Times are time.monotonic().
Depends on: lighting/clock (InternalClock). Used by: lighting/console (--clock prodjlink), tests."""
import sys, os, time, socket, struct, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lighting.clock import InternalClock

MAGIC = b"Qspt1WmJOL"


def parse_beat(b):
    """Beat packet payload -> dict(device, bar (1..4), bpm_track, pitch (fraction), bpm (effective), next_beat_ms, next_bar_ms) or None."""
    if len(b) < 0x60 or not b.startswith(MAGIC) or b[10] != 0x28: return None
    bar, raw = b[0x5c], struct.unpack_from(">H", b, 0x5a)[0]
    pitch = (((b[0x55] << 16) | (b[0x56] << 8) | b[0x57]) - 0x100000) / 0x100000
    if not 1 <= bar <= 4 or raw == 0xFFFF: return None
    bpm = raw / 100.0 * (1 + pitch)
    if not 30 <= bpm <= 300: return None
    nb, nbar = struct.unpack_from(">I", b, 0x24)[0], struct.unpack_from(">I", b, 0x2c)[0]
    return {"device": b[0x21], "bar": bar, "bpm_track": raw / 100.0, "pitch": pitch, "bpm": bpm,
            "next_beat_ms": nb / (1 + pitch), "next_bar_ms": None if nbar == 0xFFFFFFFF else nbar / (1 + pitch)}


def parse_status(b):
    """CDJ (0x0a) / mixer (0x29) status payload -> dict(device, master, playing, kind) or None."""
    if not b.startswith(MAGIC) or len(b) < 0x28: return None
    if b[10] == 0x0a and len(b) >= 0xa8: f = b[0x89]; return {"device": b[0x21], "master": bool(f & 0x20), "playing": bool(f & 0x40), "kind": "cdj"}
    if b[10] == 0x29 and len(b) >= 0x38: f = b[0x27]; return {"device": b[0x21], "master": bool(f & 0x20), "playing": bool(f & 0x40), "kind": "mixer"}
    return None


class ProDjLinkClock:
    source_name = "prodjlink"

    STATE_PATH = os.environ.get("LIGHTING_PRODJ_STATE", str(Path(__file__).resolve().parents[1] / "out" / "lighting" / "state.json"))   # published by tools/xz_dj_watch.py

    def __init__(self, bpm=120.0, listen=True, ports=(50001, 50002), bind="0.0.0.0", state_path="default"):
        self.internal = InternalClock(bpm); self.ports, self.bind = tuple(ports), bind
        self.state_path = self.STATE_PATH if state_path == "default" else state_path     # None disables the audible-deck preference
        self._seen, self._pref, self._pref_t = {}, None, -1e9
        self.paused, self._frozen = False, 0.0; self.lock = threading.RLock()
        self._anchor = None            # (t, beat, bpm) of the last accepted beat packet
        self._last_rx, self._device, self._was_live = None, None, False
        self._master, self._master_t = None, -1e9
        self.packets = self.beats = self.ignored = 0; self.error = None
        self._socks, self._threads, self._stop = [], [], threading.Event()
        if listen: self.start()

    # --- network (receive only)
    def start(self):
        if self._socks: return self
        self._stop.clear()
        for port in self.ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"): s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                s.bind((self.bind, port)); s.settimeout(0.25)
            except OSError as e:
                self.error = f"udp {port}: {e}"
                if port == self.ports[0]: return self           # beats are essential; status is optional
                continue
            self._socks.append(s)
            th = threading.Thread(target=self._rx, args=(s, port), daemon=True); th.start(); self._threads.append(th)
        return self

    def _rx(self, s, port):
        while not self._stop.is_set():
            try: data, _ = s.recvfrom(2048)
            except socket.timeout: continue
            except OSError: return
            self.feed(data, port)

    def stop(self):
        self._stop.set()
        for s in self._socks:
            try: s.close()
            except OSError: pass
        for th in self._threads: th.join(1.0)
        self._socks, self._threads = [], []

    # --- decoding
    def feed(self, payload, port=50001, t=None):
        """Decode one UDP payload received at monotonic time t (default now). Returns True if it moved the clock."""
        t = time.monotonic() if t is None else t
        if port == 50002 or (len(payload) > 10 and payload[10] in (0x0a, 0x29)):
            st = parse_status(payload)
            if st:
                with self.lock:
                    self.packets += 1
                    if st["master"]: self._master, self._master_t = st["device"], t
                    elif self._master == st["device"]: self._master = None
            else: self.ignored += 1
            return False
        pk = parse_beat(payload)
        if not pk: self.ignored += 1; return False
        with self.lock:
            self.packets += 1
            if not self._accept(pk["device"], t): return False
            self.beats += 1; self._device = pk["device"]
            pred = self._live(t) if self._anchor else None
            if pred is None: k, new = float(pk["bar"] - 1), float(pk["bar"] - 1)
            else:
                k = round((pred - (pk["bar"] - 1)) / 4.0) * 4.0 + (pk["bar"] - 1)
                err = k - pred
                new = pred + 0.5 * err if abs(err) <= 0.25 else k
            self._anchor, self._last_rx = (t, new, pk["bpm"]), t
            return True

    def _fresh(self, t): return self._last_rx is not None and 0 <= t - self._last_rx <= self._timeout()

    def _preferred(self, t):
        """Audible deck from the watcher's state file (status flag bit 0x08 = on-air): the only on-air deck; if both are on-air the master;
        None when unknown/stale (then the old rule applies). Re-read at most every 0.5 s."""
        if not self.state_path: return None
        if t - self._pref_t > 0.5:
            self._pref_t, self._pref = t, None
            try:
                import json, os, time as _t
                st = json.load(open(self.state_path))
                if _t.time() - st["t"] <= 3.0:
                    air = [int(k) for k, d in st["decks"].items() if d.get("playing") and int(str(d.get("flags_hex", "0x0")), 16) & 0x08]
                    if len(air) == 1: self._pref = air[0]
                    elif len(air) > 1: self._pref = st.get("master_deck") if st.get("master_deck") in air else min(air)
            except Exception: self._pref = None
        return self._pref

    def _accept(self, dev, t):
        self._seen[dev] = t
        pref = self._preferred(t)
        if pref is not None and (t - self._seen.get(pref, -1e9) <= 2.0 or dev == pref): return dev == pref
        if self._master is not None and t - self._master_t <= 3.0: return dev == self._master
        if self._device is None or dev == self._device or not self._fresh(t): return True
        return False

    def _timeout(self):
        bpm = self._anchor[2] if self._anchor else 120.0
        return min(4.0, max(1.5, 2.5 * 60.0 / bpm))

    # --- clock interface
    @property
    def following(self): return self._fresh(time.monotonic())

    @property
    def source(self): return self.source_name if self.following else "internal"

    @property
    def bpm(self): return self._anchor[2] if self.following else self.internal.bpm

    def _live(self, t):
        a = self._anchor
        return a[1] + (t - a[0]) * a[2] / 60.0

    def beat(self, t=None):
        if self.paused: return self._frozen
        now = time.monotonic(); tt = now if t is None else t
        with self.lock:
            if self._anchor and self._fresh(tt):
                b = self._live(tt); self._was_live = True
                self.internal.rebase(b, tt, bpm=max(30.0, min(300.0, self._anchor[2])))    # ready for a seamless fall-back
                return b
            self._was_live = False                      # packets stopped: internal already continues from the last beat
            return self.internal.beat(tt)

    def pause(self, on=True):
        with self.lock:
            if on and not self.paused: self._frozen = self.beat(); self.paused = True
            elif not on and self.paused:
                self.paused = False
                if not self.following: self.internal.rebase(self._frozen)

    def step(self, beats):
        if not self.paused: raise RuntimeError("pause first: stepping only works while paused")
        if not -64 <= beats <= 64: raise ValueError("step must be within ±64 beats")
        with self.lock: self._frozen += beats

    def _refuse(self, what):
        if self.following: raise RuntimeError(f"{what} follows Pro DJ Link (the deck) — change it on the deck")

    def nudge(self, beats): self._refuse("the beat"); self.internal.nudge(beats)

    def rebase(self, beat, t=None): self._refuse("the beat"); self.internal.rebase(beat, t)

    def set_bpm(self, bpm): self._refuse("tempo"); self.internal.set_bpm(bpm)

    def tap(self): self._refuse("tempo (tap is off)"); return self.internal.tap()

    def state(self):
        return {"source": self.source, "bpm": round(self.bpm, 2), "beat": round(self.beat(), 4), "paused": self.paused,
                "prodj_device": self._device, "prodj_master": self._master, "prodj_packets": self.packets,
                "prodj_beats": self.beats, "prodj_ignored": self.ignored, "prodj_listening": bool(self._socks), "prodj_error": self.error}
