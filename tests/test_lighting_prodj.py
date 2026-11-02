"""lighting/prodjclock: Pro DJ Link beat clock from synthetic packets (documented layout), a loopback UDP listener on a private
port, refusal while following, stale fall-back without a jump, master selection, and (when tshark + captures exist) replay of the
real XZ captures in $LIGHTING_PRODJ_CAPTURES (optional). Never touches the real 50001 socket and never sends to the DJ network."""
import sys, os, time, struct, socket, shutil, subprocess; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._util import check
from lighting.prodjclock import ProDjLinkClock, parse_beat, parse_status, MAGIC


def beat_pkt(dev=1, bar=1, bpm=128.0, pitch=0.0, nb=None, name=b"XDJ-XZ"):
    b = bytearray(0x60); b[:10] = MAGIC; b[10] = 0x28; b[0x0b:0x0b + len(name)] = name; b[0x1f] = 1; b[0x21] = dev
    b[0x22:0x24] = b"\x00\x3c"
    nbms = int(round(60000.0 / bpm)) if nb is None else nb
    struct.pack_into(">I", b, 0x24, nbms); struct.pack_into(">I", b, 0x28, nbms * 2); struct.pack_into(">I", b, 0x2c, 0xFFFFFFFF)
    b[0x3c:0x54] = b"\xff" * 24; struct.pack_into(">I", b, 0x54, 0x100000 + int(round(pitch * 0x100000)))
    struct.pack_into(">H", b, 0x5a, int(round(bpm * 100))); b[0x5c] = bar; b[0x5f] = dev
    return bytes(b)


def cdj_status(dev=1, master=True, playing=True):
    b = bytearray(284); b[:10] = MAGIC; b[10] = 0x0a; b[0x21] = dev; b[0x89] = 0x04 | (0x20 if master else 0) | (0x40 if playing else 0)
    return bytes(b)


def mixer_status(dev=33, master=False):
    b = bytearray(56); b[:10] = MAGIC; b[10] = 0x29; b[0x21] = dev; b[0x27] = 0x20 if master else 0
    return bytes(b)


# --- decoding
p = parse_beat(beat_pkt(2, 3, 120.0, pitch=-0.0305))
check(p and p["device"] == 2 and p["bar"] == 3 and abs(p["bpm"] - 120 * (1 - 0.0305)) < 0.01, f"parse {p}")
check(abs(p["next_beat_ms"] - 500 / (1 - 0.0305)) < 1.0, "nextBeat is at 0% pitch: divide by (1+pitch)")
check(parse_beat(beat_pkt(bar=0)) is None and parse_beat(beat_pkt(bar=5)) is None, "bad beat-in-bar ignored")
check(parse_beat(beat_pkt(bpm=400.0)) is None and parse_beat(b"x" * 96) is None and parse_beat(beat_pkt()[:80]) is None, "bad bpm/magic/short ignored")
check(parse_status(cdj_status(3, True))["master"] and not parse_status(mixer_status())["master"], "status flags")

# --- following on a synthetic grid
c = ProDjLinkClock(state_path=None, listen=False); T = 1000.0; bpm = 120.0; iv = 60 / bpm
check(c.source == "internal" and not c.following, "starts internal")
for i in range(12): c.feed(beat_pkt(1, i % 4 + 1, bpm), t=T + i * iv)
tn = T + 11 * iv + 0.05; mono0 = time.monotonic()
c._anchor = (mono0 - 0.05, c._anchor[1], c._anchor[2]); c._last_rx = mono0 - 0.05        # rebase packet times onto the real monotonic clock
b = c.beat(mono0)
check(c.following and c.source == "prodjlink" and abs(b - (11 + 0.1)) < 0.02, f"follows: beat {b}")
check(int(round(b - 0.1)) % 4 == 3, "bar phase: beat-in-bar 4 -> beat % 4 == 3")
check(abs(c.bpm - 120) < 1e-9 and c.state()["source"] == "prodjlink", "bpm from packet")
for f, a in ((c.set_bpm, (100,)), (c.tap, ()), (c.nudge, (0.25,)), (c.rebase, (0.0,))):
    try: f(*a); check(False, f"{f.__name__} accepted while following")
    except RuntimeError: pass

# phase correction: a late downbeat (bar 1 where grid says bar 4 + 0.1) snaps to the deck's bar phase
c2 = ProDjLinkClock(state_path=None, listen=False)
for i in range(8): c2.feed(beat_pkt(1, i % 4 + 1, 120.0), t=T + i * 0.5)
c2.feed(beat_pkt(1, 1, 120.0), t=T + 8 * 0.5 + 0.0)           # on time, bar 1 again -> beat 8
check(abs(c2._anchor[1] - 8.0) < 1e-9, f"downbeat lands on bar line {c2._anchor[1]}")
c2.feed(beat_pkt(1, 3, 120.0), t=T + 9 * 0.5 + 0.02)         # 20 ms late (0.04 beat, blended, not snapped)
check(abs(c2._anchor[1] - 10.0) < 0.5, "beat-in-bar 3 maps to beat 10")
c2.feed(beat_pkt(1, 2, 120.0), t=T + 10 * 0.5)               # deck jumped (loop/re-cue): bar phase snaps, number nearest
check(int(round(c2._anchor[1])) % 4 == 1, "snaps to the deck's bar phase")

# tempo change with pitch
c3 = ProDjLinkClock(state_path=None, listen=False); c3.feed(beat_pkt(1, 1, 128.0, pitch=0.06), t=T); check(abs(c3._anchor[2] - 128 * 1.06) < 0.01, "effective BPM = track bpm x (1+pitch)")

# --- stale -> internal, no jump, then resume
c4 = ProDjLinkClock(state_path=None, listen=False); now = time.monotonic()
c4.feed(beat_pkt(1, 2, 120.0), t=now - 0.3)
b_live = c4.beat(now); check(c4.source == "prodjlink" and abs(b_live - (1 + 0.6)) < 0.02, f"live {b_live}")
t_late = now + 3.0                                            # beyond the 2.5-beat timeout
b_after = c4.beat(t_late); check(abs(b_after - (1 + 0.6 + 6.0)) < 0.05, f"internal carries on without a jump ({b_after})")
check(c4.internal.bpm == 120.0, "internal took the deck tempo")
c4._last_rx = time.monotonic() - 10                           # really stale in wall time
check(c4.source == "internal", "source falls back to internal"); c4.set_bpm(100); check(abs(c4.bpm - 100) < 1e-9, "tempo editable again")
n_before = c4.beat(); c4.feed(beat_pkt(1, 3, 100.0))          # resumes: number keeps counting forward from the carried beat
check(c4._anchor[1] > n_before - 2.1 and int(round(c4._anchor[1])) % 4 == 2, f"resume keeps counting ({n_before} -> {c4._anchor[1]})")

# --- pause / step like LinkClock
c5 = ProDjLinkClock(state_path=None, listen=False)
try: c5.step(1); check(False, "step while running")
except RuntimeError: pass
c5.pause(); z = c5.beat(); c5.step(0.5); check(abs(c5.beat() - z - 0.5) < 1e-12, "step while paused"); c5.pause(False)

# --- master selection (status) and sticky first device
c6 = ProDjLinkClock(state_path=None, listen=False); t0 = time.monotonic()
c6.feed(cdj_status(2, master=True), 50002, t=t0)
check(not c6.feed(beat_pkt(1, 1, 120.0), t=t0) and c6.feed(beat_pkt(2, 1, 126.0), t=t0), "only the master's beats are followed")
check(abs(c6._anchor[2] - 126) < 1e-9, "master's tempo")
c7 = ProDjLinkClock(state_path=None, listen=False); t0 = time.monotonic()
c7.feed(beat_pkt(1, 1, 120.0), t=t0); check(not c7.feed(beat_pkt(33, 1, 99.0), t=t0 + 0.1), "sticky first device")
check(c7.feed(beat_pkt(33, 2, 99.0), t=t0 + 6.0), "other device accepted once the first is silent")
c7.feed(b"garbage", t=t0); check(c7.ignored >= 1 and c7.state()["prodj_ignored"] >= 1, "junk counted")

# --- real UDP listener on a private loopback port (never 50001)
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
c8 = ProDjLinkClock(state_path=None, listen=True, ports=(port,), bind="127.0.0.1")
check(c8.state()["prodj_listening"], f"listener bound ({c8.error})")
tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for i in range(6): tx.sendto(beat_pkt(1, i % 4 + 1, 150.0), ("127.0.0.1", port)); time.sleep(0.05)
time.sleep(0.2); check(c8.beats == 6 and c8.source == "prodjlink" and abs(c8.bpm - 150) < 1e-9, f"udp receive {c8.state()}")
c8.stop(); tx.close(); check(c8.state()["prodj_listening"] is False, "stopped")
c9 = ProDjLinkClock(state_path=None, listen=True, ports=(port, port), bind="127.0.0.1"); c10 = ProDjLinkClock(state_path=None, listen=True, ports=(port,), bind="127.0.0.1")
check(c10.state()["prodj_listening"], "SO_REUSEPORT lets a second listener share the port"); c9.stop(); c10.stop()

# --- replay real captures (optional)
caps = sorted((Path(os.environ.get("LIGHTING_PRODJ_CAPTURES", "/nonexistent"))).glob("*.pcapng")); tshark = shutil.which("tshark") or "/opt/homebrew/bin/tshark"
replayed = 0
if caps and Path(tshark).exists():
    for cap in caps:
        r = subprocess.run([tshark, "-r", str(cap), "-Y", "udp.dstport==50001 || udp.dstport==50002", "-T", "fields", "-E", "separator=\t",
                            "-e", "frame.time_epoch", "-e", "udp.dstport", "-e", "udp.payload"], capture_output=True, text=True)
        rows = [l.split("\t") for l in r.stdout.splitlines() if l.count("\t") == 2]
        pk = [(float(a), int(b), bytes.fromhex(p)) for a, b, p in rows if p]
        beats = [(t, d) for t, port_, d in pk if port_ == 50001 and parse_beat(d)]
        if len(beats) < 8: continue
        rc = ProDjLinkClock(state_path=None, listen=False); prev = None; fed = 0
        for t, port_, d in pk:
            rc.feed(d, port_, t=t)
            if parse_beat(d) and rc._anchor and rc._last_rx == t:
                bn = rc._anchor[1]; fed += 1
                if prev is not None: check(bn >= prev - 1e-6 or abs(bn - prev) < 4.1, f"{cap.name}: beat went backwards {prev}->{bn}")
                check(abs(bn - round(bn)) < 0.26 and int(round(bn)) % 4 == parse_beat(d)["bar"] - 1, f"{cap.name}: bar phase")
                prev = bn
        check(fed >= 8, f"{cap.name}: followed {fed}"); replayed += 1
print(f"ok: lighting prodjclock (decode, follow, refusal, stale fall-back, master select, UDP loopback+REUSEPORT, {replayed} capture(s) replayed)")


# --- audible-deck preference (state file, status flag bit 0x08 = on-air)
import json, tempfile, os
def _state(decks, master=None):
    return {"t": time.time(), "decks": {str(k): {"playing": True, "flags_hex": f} for k, f in decks.items()}, "master_deck": master}
tf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); tf.close()
json.dump(_state({1: "0xc4", 2: "0xec"}, master=2), open(tf.name, "w"))            # deck 2 on-air + master, deck 1 not on-air
cp = ProDjLinkClock(listen=False, state_path=tf.name); now = time.monotonic()
cp.feed(beat_pkt(2, 1, 128.0), t=now); cp.feed(beat_pkt(1, 1, 90.0), t=now + 0.2)       # audible deck heard first, then the cued deck
check(abs(cp.bpm - 128.0) < 0.5 and cp._preferred(now) == 2, "follows the on-air deck, ignores the cued deck's beats")
json.dump(_state({1: "0xcc", 2: "0xec"}, master=2), open(tf.name, "w")); cp._pref_t = -1e9      # both on-air -> master
check(cp._preferred(time.monotonic()) == 2, "both on-air -> master deck")
json.dump(_state({1: "0xcc", 2: "0xc4"}, master=2), open(tf.name, "w")); cp._pref_t = -1e9      # only deck 1 on-air
check(cp._preferred(time.monotonic()) == 1, "only deck 1 on-air -> deck 1")
st = _state({1: "0xec"}); st["t"] -= 10; json.dump(st, open(tf.name, "w")); cp._pref_t = -1e9    # stale file -> no preference
check(cp._preferred(time.monotonic()) is None, "stale state -> no preference (old rule)")
os.unlink(tf.name)
print("prodj audible-deck preference: ok")
