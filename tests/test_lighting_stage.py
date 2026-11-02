"""lighting/stage3d + AutoOutput: DMX levels → emitter geometry (moving-head pan/tilt, spider cells, par aim), beam
occlusion against the venue, eye-height hazard check + program audit, sim fallback/hot-plug of the DMX output, and one
offscreen GPU frame (graphics.gl.Upscaler path too) that must show coloured light."""
import sys, math; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._util import check, OUT
from lighting import stage3d as S3, dmx
from lighting.fixtures import Rig
from lighting.show import Engine, load_show, render

rig = Rig.load("halloween")
# beam centred = straight up; tilt full = 90 deg toward the audience; pan swings it sideways
render(rig, [{"group": "beam1", "effect": "color", "color": "red"}], 0.0)
b = rig["beam1"]
ems = {e.name: e for e in S3.emitters(rig)}
check(set(ems) == {"beam1"}, f"only lit fixtures emit ({set(ems)})")
d = ems["beam1"].dir; check(abs(d[2] - 1) < 1e-4, f"tilt centre points up (8-bit centre = 0.35° off) {d}")
check(ems["beam1"].rgb[0] > 0 and ems["beam1"].rgb[1] == 0, "red emitter")
b.aim(0, 90); d = S3.emitters(rig)[0].dir; check(abs(d[1] + 1) < 1e-3 and abs(d[2]) < 0.02, f"tilt +90 faces the audience (-y) {d}")
b.aim(90, 90); d = S3.emitters(rig)[0].dir; check(abs(abs(d[0]) - 1) < 1e-3, f"pan 90 swings sideways {d}")
# spider: 8 fixed-colour cells, both bars, fanned
render(rig, [{"group": "spider1", "effect": "color", "color": "white"}], 0.0)
cells = [e for e in S3.emitters(rig) if e.name.startswith("spider1.")]
check(len(cells) == 2, f"white lights only the two white cells ({[e.name for e in cells]})")
render(rig, [{"group": "spider1", "effect": "color", "color": "magenta"}], 0.0)
cells = sorted(e.name for e in S3.emitters(rig))
check(cells == ["spider1.led1", "spider1.led3", "spider1.led5", "spider1.led7"], f"magenta = red + blue cells on both bars {cells}")
right = S3._frame(rig["spider1"])[2]
xs = sorted(sum(a * b_ for a, b_ in zip(e.dir, right)) for e in S3.emitters(rig)); check(xs[0] < 0 < xs[-1], "cells fan out along the bar axis")
# pars: fixed aim from the rig (12 deg from vertical toward the back)
render(rig, [{"group": "pars", "effect": "color", "color": "blue"}], 0.0)
pe = [e for e in S3.emitters(rig) if e.name.startswith("par")]
check(len(pe) == 4 and all(abs(math.degrees(math.acos(e.dir[2])) - 12) < 1e-6 and e.dir[1] > 0 for e in pe), "pars uplight backward at 12°")
check(all(e.length < 40 for e in pe), "par beams end in the hedge/canopy")
# side-mounted spider (pitch 75, roll 90): beam centre toward the crowd 15 deg up, tilts sweep sideways
render(rig, [{"group": "spider1", "effect": "color", "color": "white"}, {"group": "spider1", "effect": "aim", "tilt": 0}], 0.0)
c = [e for e in S3.emitters(rig) if e.name.startswith("spider1.")]
check(all(e.dir[1] < -0.8 and e.dir[2] > 0 for e in c), f"side-mounted spider projects at the crowd, upward {[e.dir for e in c]}")
render(rig, [{"group": "spider1", "effect": "color", "color": "white"}, {"group": "spider1", "effect": "aim", "tilt": 40}], 0.0)
c2 = [e for e in S3.emitters(rig) if e.name.startswith("spider1.")]
check(all(abs(a.dir[0] - b_.dir[0]) > 0.3 and abs(a.dir[2] - b_.dir[2]) < 0.15 for a, b_ in zip(c, c2)), "its tilt swings the bars sideways, not up/down")
# spider bars are mirrored on the unit (Y1 flagged invert): the same aim sends opposite DMX, and both bars point the same way in 3D
sp = rig["spider1"]; sp.home(); sp.dimmer(1); sp.aim(None, 30, head=0); sp.aim(None, 30, head=1)
for k in range(1, 9): sp.set(f"led{k}", 255)
check(sp.levels[41] + sp.levels[42] == 255 and sp.levels[41] != sp.levels[42], f"bars mirrored in DMX {sp.levels[41]}/{sp.levels[42]}")
w = {e.name: e.dir for e in S3.emitters(rig) if e.name in ("spider1.led4", "spider1.led5")}
check(all(abs(a - b_) < 1e-9 for a, b_ in zip(w["spider1.led4"], w["spider1.led5"])), "same aim = parallel bars in 3D (led4 sits alongside led5: bar 2 is the mirrored twin)")
# occlusion + hazards
v = rig.venue
check(S3.beam_length((0, 0.65, 3.4), (0, 0, 1), v) == 40.0, "straight up is unobstructed")
check(abs(S3.beam_length((0, -3, 1.5), (0, 0, -1), v) - 1.5) < 1e-9, "ground stops a downward beam")
flat = S3.Emitter("x", (0, 0.0, 1.5), (0, -1, 0), (1, 1, 1), 0.02, 0.04, 40.0)
up = S3.Emitter("y", (0, 0.0, 1.5), (0, 0, 1), (1, 1, 1), 0.02, 0.04, 40.0)
check([n for n, _ in S3.eye_hazards([flat, up], v)] == ["x"], "a level beam into the crowd is an eye hazard; straight up is not")
wide = S3.Emitter("w", (0, 0.0, 1.5), (0, -1, 0), (1, 1, 1), 0.3, 0.04, 40.0)
check(S3.eye_hazards([wide], v) == [], "wide washes are not flagged")
aud = S3.audit_program(Rig.load("halloween"), load_show("halloween_demo"))
check(set(aud) >= {"intro", "chase", "rainbow"} and all(r["eye_fraction"] == 0 for r in aud.values()), f"demo show is eye-safe: {aud}")

# AutoOutput: sim when the dongle is missing, switches when it appears, falls back when it fails
class Dev:
    def __init__(self): self.n, self.fail = 0, False
    def send(self, f):
        if self.fail: raise OSError("unplugged")
        self.n += 1
    def close(self): pass
state = {"present": False}; dev = Dev()
def opener():
    if not state["present"]: raise LookupError("no USB→DMX candidates")
    return dev
ao = dmx.AutoOutput(retry_s=0.0, opener=opener)
ao.send(bytes(512)); check(ao.mode.startswith("sim") and ao.count == 1, "sim without a dongle")
state["present"] = True; ao.send(bytes(512)); check(ao.dev is dev and dev.n == 1, "picks up a hot-plugged dongle")
dev.fail = True; ao.send(bytes(512)); check(ao.dev is None and ao.mode.startswith("sim"), "falls back to sim when the dongle fails")

# auto port selection never picks a non-FTDI serial device (RoboClaw/Arduino safety)
orig = dmx.find_ports
dmx.find_ports = lambda: [{"port": "/dev/cu.usbmodem1", "usb": "03eb:2404 USB Roboclaw", "known": "", "ftdi": False}]
try: dmx.open_output("auto"); check(False, "auto picked a RoboClaw")
except LookupError as e: check("ignoring non-FTDI" in str(e), f"clear refusal: {e}")
finally: dmx.find_ports = orig

# one GPU frame offscreen (full res and through the Upscaler)
eng = Engine(Rig.load("halloween")); eng.cue(load_show("halloween_demo"), "chase"); eng.frame(0.5)
try:
    from graphics.png import read_png
    for scale in (1.0, 0.5):
        r = S3.StageRenderer(eng.rig, scale=scale)
        from graphics.gl import offscreen_png
        p = offscreen_png(lambda w, h: r.draw(w, h, *S3.VIEWS["photo"], 1.0), 320, 180, str(OUT / f"stage3d_{scale}.png"))
        w, h, px = read_png(p)
        sat = sum(1 for i in range(0, len(px), 3) if max(px[i:i + 3]) - min(px[i:i + 3]) > 60)
        check(sat > 50, f"scale {scale}: coloured beams visible ({sat} saturated px)")
    gpu = "gpu frame ok"
except OSError as e:
    gpu = f"gpu skipped ({e})"
print(f"ok: lighting stage3d (emitters, occlusion, eye hazards, audit, AutoOutput hot-plug; {gpu})")
