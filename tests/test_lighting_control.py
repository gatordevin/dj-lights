"""lighting/ control stack without hardware: DMX universe + Art-Net packet + OpenDMX break/framing (injected serial), fixture profiles/rig
mapping (beam chart verified on the unit), show render (colours, per-head spider, aim limits, laser safety, blackout),
engine program advance + tap tempo + holds (regression: cue change used to deadlock), console API contract."""
import sys, time, threading; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._util import check, OUT
from lighting import dmx, fixtures as F, show as S

# --- dmx
u = dmx.Universe(); u[1] = 255; u.set_many(510, [1, 2, 3])
fr = u.frame(); check(len(fr) == 512 and fr[0] == 255 and fr[509:] == bytes([1, 2, 3]), "universe 1-based + set_many")
for bad in (lambda: u.__setitem__(0, 1), lambda: u.__setitem__(513, 1), lambda: u.__setitem__(1, 256)):
    try: bad(); check(False, "bad channel/value accepted")
    except ValueError: pass
p = dmx.ArtNet.packet(bytes(range(5)), universe=3, seq=7)
check(p[:8] == b"Art-Net\x00" and p[8:10] == b"\x00\x50" and p[12] == 7 and p[14:16] == b"\x03\x00" and p[16:18] == b"\x00\x06" and p[18:23] == bytes(range(5)), "ArtDmx header (opcode LE, length BE, even-padded)")

class FakeSerial:
    def __init__(self): self.log = []
    def write(self, data, drain=True): self.log.append(("write", bytes(data)))
    def send_break(self, s): self.log.append(("break", s))
    def set_baud(self, b): self.log.append(("baud", b))
    def close(self): self.log.append(("close",))
for mode, expect in (("ioctl", ["break", "write"]), ("baud", ["baud", "write", "baud", "write"])):
    fs = FakeSerial(); o = dmx.OpenDMX("/dev/null", break_mode=mode, slots=24, serial=fs)
    f = bytearray(512); f[0] = 0xAB; f[23] = 0xCD; o.send(bytes(f))
    check([e[0] for e in fs.log] == expect, f"{mode}: break sequence {fs.log[:3]}")
    check(fs.log[-1] == ("write", b"\x00" + bytes(f[:24])), f"{mode}: start code 0 + exactly `slots` channels")
    if mode == "baud": check(fs.log[0] == ("baud", 57600) and fs.log[1] == ("write", b"\x00") and fs.log[2] == ("baud", 250000), "slow-baud break then back to 250k")
    t = time.perf_counter(); o.send(bytes(f)); check(time.perf_counter() - t >= 25 * dmx.SLOT_S * 0.9, "waits for the previous frame to leave the wire")

mem = dmx.MemoryOutput(); s = dmx.Sender(u, [mem], fps=40).start(); time.sleep(0.2); s.stop()
check(mem.count >= 5 and mem.last == bytes(512), "Sender streams continuously and blacks out on stop")

# --- fixtures
beam = F.load_profile("djxfli_beam90")
check([n for _, n, _ in beam.channels()][:9] == ["pan", "tilt", "speed", "dimmer", "strobe", "red", "green", "blue", "white"], "beam chart as read from the unit's manual")
rig = F.Rig.load("halloween"); rig.home()
b = rig["beam1"]; b.dimmer(1).color(0, 0, 1)
check(b.levels[4] == 255 and b.levels[8] == 255 and b.levels[6] == 0 and b.levels[1] == 128, "beam blue = ch4 dimmer + ch8 blue, head centred")
b.color(1, 1, 1); check(b.levels[9] == 255 and b.levels[6] == 0, "RGBW: white extracted from equal RGB")
b.aim(270, -90); check(b.levels[1] == 255 and b.levels[2] == 0, "aim maps ±span/2 to 0..255")
try: b.aim(300, 0); check(False, "aim beyond travel accepted")
except ValueError: pass
sp = rig["spider1"]; sp.color(1, 0, 0, head=0); sp.color(0, 0, 1, head=1)
L = sp.levels; check(L[45] == 255 and L[51] == 255 and L[49] == 0, "spider: fixed-colour LED cells per bar (red led1, blue led7)")
sp.color(1, 0, 1, head=0); check(sp.levels[45] == 255 and sp.levels[47] == 255 and sp.levels[46] == 0, "magenta = red + blue cells")
try: F.Rig("x", [F.Fixture("a", beam, 1), F.Fixture("b", beam, 10)]); check(False, "overlap accepted")
except ValueError: pass
try: F.load_profile("nope"); check(False)
except LookupError: pass
for p in F.PROFILES.glob("*.json"): F.load_profile(p.stem)            # every shipped profile validates

# --- show render
S.render(rig, [{"group": "all", "effect": "color", "color": "red"}], 0.0)
check(rig["beam1"].levels[6] == 255 and rig["par1"].levels[22] == 255 and rig["par1"].levels[21] == 255, "colour layer reaches beam + par")
S.render(rig, [{"group": "all", "effect": "color", "color": "red"}], 0.0, master=0.5)
check(rig["beam1"].levels[4] == 128, "master scales the dimmer")
S.render(rig, [{"group": "all", "effect": "color", "color": "red"}], 0.0, blackout=True)
check(all(rig[n].levels[a] == 0 for n, a in (("beam1", 4), ("par1", 21), ("spider1", 43))), "blackout zeroes every dimmer")
S.render(rig, [{"group": "spiders", "effect": "chase", "colors": ["red", "blue"]}], 0.0)
check(rig["spider1"].levels[45] == 255 and rig["spider1"].levels[51] == 255, "chase offsets spider heads")
S.render(rig, [{"group": "movers", "effect": "sweep", "shape": "fan", "pan": 1000}], 2.0)
check(0 <= rig["beam1"].levels[1] <= 255, "sweeps beyond travel are clamped, not errors")
for bad in ([{"effect": "nope"}], [{"effect": "color", "color": "plaid"}], [{"effect": "color", "group": "ghosts"}]):
    try: S.check_layers(bad, rig); check(False, f"bad layer accepted {bad}")
    except LookupError: pass
laser = F.Rig("l", [F.Fixture("laser", F.load_profile("uking_laser_zq05031"), 1)])
laser["laser"].set("laser", 200); S.render(laser, [], 0.0)
check(laser["laser"].levels[1] == 0, "laser channel forced off unless armed")

# --- engine
show = S.load_show("halloween_demo")
seen = []
done = threading.Event()
def run():
    for _, e in S.offline(F.Rig.load("halloween"), show, step_beats=0.1):
        if not seen or seen[-1] != e.cue_name: seen.append(e.cue_name)
    done.set()
threading.Thread(target=run, daemon=True).start()
check(done.wait(10), "program runs to the end (cue change used to deadlock on a non-reentrant lock)")
check(seen == [c for c, _ in show["program"]], f"program order {seen}")
eng = S.Engine(F.Rig.load("halloween"), bpm=120); eng.cue(show, "intro"); t0 = None
eng.hold("strobe", [{"group": "all", "effect": "strobe", "rate": 1.0}]); eng.frame(t0)
check(eng.rig["beam1"].levels[5] == 255, "hold overlays strobe on the running cue")
eng.hold("strobe", None); eng.frame(t0); check(eng.rig["beam1"].levels[5] == 0, "hold released")
eng2 = S.Engine(F.Rig.load("halloween"), bpm=120)
for _ in range(4): eng2.tap(); time.sleep(0.25)
check(200 < eng2.bpm < 280, f"tap tempo ~240 bpm from 0.25 s taps (got {eng2.bpm:.0f})")
eng2.pause(True); b0 = eng2.beat(); time.sleep(0.15); eng2.set_bpm(150)
check(eng2.beat() == b0 and eng2.state()["paused"], "pause freezes the beat (even across a tempo change)")
eng2.pause(False); time.sleep(0.1); check(0 < eng2.beat() - b0 < 0.5, "resume continues from the frozen beat")
png = S.preview_png(F.Rig.load("halloween"), show, 120, OUT / "lighting_preview.png", fps=2)
check(png.exists() and png.stat().st_size > 500, "preview PNG")

# --- console contract
from lighting import console as C
eng3 = S.Engine(F.Rig.load("halloween"), bpm=120); eng3.cue(show, "intro")
C.handle(eng3, show, {"action": "cue", "name": "spooky"}); check(eng3.cue_name == "spooky", "console cue")
C.handle(eng3, show, {"action": "master", "value": 0.3}); check(eng3.master == 0.3, "console master")
C.handle(eng3, show, {"action": "pause"}); check(eng3.paused, "console pause toggles on")
C.handle(eng3, show, {"action": "pause"}); check(not eng3.paused, "and off")
C.handle(eng3, show, {"action": "identify", "fixture": "beam2", "seconds": 0.1}); check("identify" in eng3.overlay, "identify flashes one fixture")
time.sleep(0.25); check("identify" not in eng3.overlay, "identify ends by itself")
for bad in ({"action": "master", "value": 3}, {"action": "cue", "name": "nope"}, {"action": "explode"}):
    try: C.handle(eng3, show, bad); check(False, f"console accepted {bad}")
    except (ValueError, LookupError): pass
print("ok: lighting control (dmx frame/break/artnet, profiles, rig, render, engine program/hold/tap, preview, console api)")
