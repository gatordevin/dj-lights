"""net/abletonlink: codec round-trips + a listener following a local test peer on a PRIVATE group/port (loopback):
tempo, measured ghost offset, beat agreement within a few ms, tempo change, bye-bye removal."""
import sys, time, struct; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._util import check
from net import abletonlink as L

tl = L.Timeline(500000, 3_000_000, 10_000_000)
check(abs(tl.bpm - 120) < 1e-9 and abs(tl.to_beats(10_500_000) - 4.0) < 1e-9, "timeline: 120 bpm, +0.5 s = +1 beat")
check(L.Timeline.decode(tl.encode()) == tl, "timeline codec")
e = L.decode_entries(L.encode_entries([(L.TMLN, tl.encode()), (L.SESS, b"12345678")]))
check(e[L.SESS] == b"12345678" and L.Timeline.decode(e[L.TMLN]) == tl, "payload entries round-trip")
for bad in (b"junk", L.DISC_HDR + b"\x01"):
    try: L.parse_discovery(bad); check(False, "bad discovery accepted")
    except ValueError: pass

G, P = "239.255.77.77", 30808                                       # private: never the real Link group
peer = L.LinkPeer(bpm=128.0, group=G, port=P, ghost_offset_us=12_345_678, beat_at_origin=10.0).start()
ln = L.LinkListener(group=G, port=P, ifaces=["127.0.0.1"], samples=30).start()
t0 = time.time()
while not ln.synced and time.time() - t0 < 8: time.sleep(0.05)
check(ln.synced, f"listener measured the peer ({ln.status()})")
check(abs(ln.tempo - 128.0) < 0.01, f"tempo {ln.tempo}")
check(abs(ln.offset_us - 12_345_678) < 3000, f"ghost offset measured within 3 ms ({ln.offset_us - 12_345_678:+.0f} us)")
h = L.host_us(); truth = peer.timeline.to_beats(h + peer.off)
check(abs(ln.beat(h) - truth) < 0.01, f"beat agrees ({ln.beat(h):.4f} vs {truth:.4f})")
check(0 <= ln.phase(4) < 4, "phase in [0, 4)")
peer.set_bpm(100.0); t0 = time.time()
while abs((ln.tempo or 0) - 100) > 0.01 and time.time() - t0 < 3: time.sleep(0.05)
h = L.host_us(); check(abs(ln.tempo - 100) < 0.01 and abs(ln.beat(h) - peer.timeline.to_beats(h + peer.off)) < 0.01, "follows a tempo change, beat continuous")
peer.stop(); t0 = time.time()
while ln.peers and time.time() - t0 < 3: time.sleep(0.05)
check(not ln.peers and ln.tempo is None, "bye-bye removes the peer")
ln.stop()
try: L.LinkPeer(group=L.GROUP, port=L.PORT, iface="192.0.2.5"); check(False, "test peer allowed on the real group")
except ValueError: pass
print("ok: net abletonlink (codec, measurement, beat/phase, tempo change, bye-bye) on a private loopback group")
