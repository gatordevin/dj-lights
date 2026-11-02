"""lighting/ DJ layer without hardware: fixture defaults/strobe ranges/colour wheels/standard groups, rig patching in plain words
(addresses, DIP, aim), OFL import (wheels, shutters, fine channels, pixel matrices, switching channels, lasers), aim_at vs the 3D
model, songmap on a synthetic 128 BPM song in several file formats (grid, downbeat, drop, breakdown, fixes, rekordbox XML),
autoshow on different rigs (safety: no strobes unless asked, dark beat before the drop, flash rate, mover beams above the
crowd's eyes), FileClock, dj.play in simulation (blackout after)."""
import sys, json, math, wave, shutil, subprocess, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._util import check, OUT, require_optional
require_optional("numpy", why="song analysis")
import numpy as np
from lighting import fixtures as F, patch as P, ofl, stage3d as S, songmap as SM, autoshow as A, dj
from lighting.clock import FileClock
from lighting.show import render, offline, Tick

D = OUT / "lighting_dj"; shutil.rmtree(D, ignore_errors=True); D.mkdir(parents=True)

# --- fixtures: defaults, strobe ranges, colour wheel
prof = {"name": "t_spot", "type": "moving_head", "pan_deg": 540, "tilt_deg": 270, "default_mode": "6ch", "modes": {"6ch": ["pan", "tilt", "wheel", "shutter", "dim", "gobo"]},
        "channels": {"pan": {"role": "pan"}, "tilt": {"role": "tilt"}, "dim": {"role": "dimmer"}, "gobo": {"role": "raw", "default": 7},
                     "shutter": {"role": "strobe", "default": 4, "strobe_range": [8, 76]},
                     "wheel": {"role": "wheel", "slots": [{"value": 0, "rgb": [1, 1, 1]}, {"value": 20, "rgb": [1, 0, 0]}, {"value": 40, "rgb": [0, 0, 1]}]}}}
fx = F.Fixture("s", F.Profile(prof), 1); fx.home()
check(fx.values[3] == 4 and fx.values[5] == 7, "home rests channels at their profile default (shutter open, gobo)")
fx.strobe(1.0); check(fx.values[3] == 76, "strobe speed maps into strobe_range"); fx.strobe(0); check(fx.values[3] == 4, "strobe 0 -> back to open")
fx.dimmer(1); fx.color(0.9, 0.1, 0.0); check(fx.values[2] == 20 and F.emitted_rgb(fx) == (255, 0, 0), "colour wheel picks the nearest slot; preview colour follows")
for bad in ({"default": 300}, {"strobe_range": [5]}):
    try: F.Profile({**prof, "channels": {**prof["channels"], "dim": {"role": "dimmer", **bad}}}); check(False, f"bad spec accepted {bad}")
    except ValueError: pass

# --- standard groups; a rig's own groups keep their order
hw = F.Rig.load("halloween")
check([f.name for f in hw.group("movers")] == [f.name for f in hw.fixtures if "movers" in f.groups], "explicit group order unchanged")
check(set(hw.standard_groups()["effects"]) == {"spider1", "spider2"} and hw.group("left")[0].pos[0] <= hw.group("left")[-1].pos[0], "kind + place groups, left to right")

# --- patch: a mobile rig in plain words
rp = D / "rig.json"
d = P.new_rig("t_rig", "mobile", truss=True, path=rp)
pars = P.add(d, "generic_par_rgbw_8ch", 4, "floor behind the dj"); movers = P.add(d, "generic_moving_head_rgbw_9ch", 2, "on the truss")
bars = P.add(d, "generic_ledbar_4seg_rgb_12ch", 2, "on stands either side"); strobe = P.add(d, "generic_strobe_2ch", 1, "truss center")
P.save(d); rig = F.Rig.load(rp)
check([rig[n].address for n in pars] == [1, 11, 21, 31] and rig["mover1"].address == 41 and rig["bar1"].address == 61, "auto addresses on 10-channel steps")
check(P.dip(41) == [1, 4, 6] and P.dip(1) == [1], "DIP switches")
check(rig["par1"].pos[1] > 1.0 and rig["par1"].pos[2] == 0 and rig["mover1"].mount == "hung" and rig["bar1"].pos[0] < 0 < rig["bar2"].pos[0], "places: behind/floor, truss hung, stands either side")
check(rig["par1"].aim_fixed[1] == 180.0, "floor-behind uplights aim up the back wall")
check(P.place("stage left on a stand", d["venue"])["pos"][0] > 0 and P.place("left", d["venue"])["pos"][0] < 0, "stage left = DJ's left = crowd's right")
check(P.place("8ft stand", d["venue"])["pos"][2] == round(8 * 0.3048, 2), "feet heights")
c = P.check(d); check(c["ok"] and set(c["kinds"]) == {"wash", "mover", "strobe"} and any("strobe" in w for w in c["warnings"]), "check: kinds + warnings")
try: P.add(d, "generic_par_7ch", 1, "floor", address=5); check(False, "overlap accepted")
except ValueError: pass
try: P.place("in the kitchen sink", d["venue"]); check(False, "nonsense place accepted")
except ValueError: pass
pth, pd = P.make_profile("t_custom_bar", "dimmer, strobe, 4x(r,g,b), mode", "bar", force=True)
try:
    cf = F.Fixture("c", F.load_profile("t_custom_bar"), 1); cf.home(); cf.dimmer(1); cf.color(1, 0, 0, head=2)
    check(cf.heads == 4 and cf.values[8] == 255 and sum(cf.values) == 510, "typed profile with segments")
finally: pth.unlink()

# --- OFL conversion (inline files: no network)
mh = {"name": "Test Spot", "categories": ["Moving Head"], "physical": {"lens": {"degreesMinMax": [12, 17]}},
      "wheels": {"Color Wheel": {"slots": [{"type": "Open"}, {"type": "Color", "name": "Red", "colors": ["#ff0000"]}, {"type": "Color", "name": "Blue", "colors": ["#0000ff"]}]}},
      "availableChannels": {"Pan": {"fineChannelAliases": ["Pan fine"], "capability": {"type": "Pan", "angleStart": "0deg", "angleEnd": "540deg"}},
                            "Tilt": {"capability": {"type": "Tilt", "angleStart": "0deg", "angleEnd": "270deg"}},
                            "Color Wheel": {"capabilities": [{"dmxRange": [0, 9], "type": "WheelSlot", "slotNumber": 1}, {"dmxRange": [10, 19], "type": "WheelSlot", "slotNumber": 2},
                                                             {"dmxRange": [20, 29], "type": "WheelSlot", "slotNumber": 3}, {"dmxRange": [30, 255], "type": "WheelRotation", "speedStart": "slow", "speedEnd": "fast"}]},
                            "Shutter": {"capabilities": [{"dmxRange": [0, 3], "type": "ShutterStrobe", "shutterEffect": "Closed"}, {"dmxRange": [4, 7], "type": "ShutterStrobe", "shutterEffect": "Open"},
                                                         {"dmxRange": [8, 200], "type": "ShutterStrobe", "shutterEffect": "Strobe"}]},
                            "Dimmer": {"capability": {"type": "Intensity"}}, "Laser Pattern": {"capability": {"type": "Effect"}},
                            "Program": {"capabilities": [{"dmxRange": [0, 9], "type": "NoFunction", "switchChannels": {"Speed / Strobe": "Strobe"}},
                                                         {"dmxRange": [10, 255], "type": "Effect", "switchChannels": {"Speed / Strobe": "Prog Speed"}}]},
                            "Strobe": {"capability": {"type": "ShutterStrobe", "shutterEffect": "Strobe"}}, "Prog Speed": {"capability": {"type": "EffectSpeed"}}},
      "modes": [{"name": "9-channel", "shortName": "9ch", "channels": ["Pan", "Pan fine", "Tilt", "Color Wheel", "Shutter", "Dimmer", "Laser Pattern", "Program", "Speed / Strobe"]}]}
op = ofl.convert(mh, "t_ofl"); opr = F.Profile(op); roles = {n: s["role"] for _, n, s in opr.channels()}
check(roles == {"pan": "pan", "pan_fine": "pan_fine", "tilt": "tilt", "color_wheel": "wheel", "shutter": "strobe", "dimmer": "dimmer",
                "laser_pattern": "laser", "program": "program", "speed_strobe": "strobe"}, f"OFL roles {roles}")
check(op["pan_deg"] == 540 and op["tilt_deg"] == 270 and op["channels"]["shutter"]["default"] == 4 and op["channels"]["shutter"]["strobe_range"] == [8, 200], "OFL travel + shutter")
check([sl["value"] for sl in op["channels"]["color_wheel"]["slots"]] == [0, 10, 20], "OFL wheel slots from WheelSlot ranges")
bar = {"name": "Pix Bar", "categories": ["Pixel Bar"], "matrix": {"pixelCount": [4, 1, 1], "pixelGroups": {"Master": "all"}},
       "availableChannels": {"Dimmer": {"capability": {"type": "Intensity"}}},
       "templateChannels": {"Red $pixelKey": {"capability": {"type": "ColorIntensity", "color": "Red"}}, "Green $pixelKey": {"capability": {"type": "ColorIntensity", "color": "Green"}},
                            "Blue $pixelKey": {"capability": {"type": "ColorIntensity", "color": "Blue"}}},
       "modes": [{"name": "3ch", "channels": ["Red Master", "Green Master", "Blue Master"]},
                 {"name": "13ch", "channels": ["Dimmer", {"insert": "matrixChannels", "repeatFor": "eachPixelABC", "channelOrder": "perPixel", "templateChannels": ["Red $pixelKey", "Green $pixelKey", "Blue $pixelKey"]}]}]}
bp = F.Profile(ofl.convert(bar, "t_bar"))
check(bp.type == "bar" and bp.heads("13ch") == 4 and bp.heads("3ch") == 1 and bp.d["default_mode"] == "13ch", "OFL pixel matrix -> heads per pixel")

# --- aim_at agrees with the 3D model
for mount, pos, yaw in (("hung", (1.5, -1, 3.5), 30), ("floor", (0, 1, 0.2), 180)):
    f = F.Fixture("m", F.load_profile("generic_moving_head_16bit_11ch"), 1, pos=pos, yaw=yaw, mount=mount); r1 = F.Rig("t", [f])
    tgt = (0, -4, 0) if mount == "hung" else (3, -2, 3.8); pt = S.aim_at(f, tgt); check(pt is not None, f"{mount}: reachable")
    f.home(); f.dimmer(1); f.color(1, 1, 1); f.aim(*pt); em = S.emitters(r1)[0]
    want = [t - p for t, p in zip(tgt, em.pos)]; n = math.sqrt(sum(c * c for c in want))
    err = math.degrees(math.acos(max(-1, min(1, sum(a * b / n for a, b in zip(want, em.dir))))))
    check(err < 1.0, f"{mount}: aim_at within 1 deg of the drawn beam ({err:.2f})")

# --- songmap: synthetic 128 BPM song (intro 1-8, groove 9-32, breakdown 33-40, drop 41, outro 57-64), several formats
from lighting.audio_in import synth_track, SR
x = np.concatenate([np.zeros(int(SR * 0.5), np.float32), synth_track(bpm=128, bars=64)])
wav = D / "song.wav"
with wave.open(str(wav), "wb") as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes((x * 32767).astype("<i2").tobytes())
files = [wav]
if shutil.which("ffmpeg"):
    for ext, args in (("mp3", ["-c:a", "libmp3lame"]), ("m4a", ["-c:a", "aac"]), ("flac", ["-c:a", "flac"])):
        o = D / f"song.{ext}"; subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(wav)] + args + [str(o)], check=True); files.append(o)
    o = D / "song.mp4"; subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=64x64:r=5", "-i", str(wav), "-shortest", "-c:v", "libx264", "-c:a", "aac", str(o)], check=True); files.append(o)
for f in files:
    sm = SM.analyze(f, force=True); types = {s["type"]: s["start_bar"] for s in sm["sections"]}
    check(abs(sm["bpm"] - 128) < 0.05, f"{f.suffix}: bpm {sm['bpm']}")
    check(abs(sm["first_downbeat_s"] - 0.5) < 0.04, f"{f.suffix}: first downbeat {sm['first_downbeat_s']}")
    check(sm["drops"] == [41] and types.get("breakdown") == 33 and types.get("intro") == 1 and types.get("outro") == 57, f"{f.suffix}: sections {sm['sections']} drops {sm['drops']}")
check(sm["has_video"] and files[-1].suffix == ".mp4", "video container: audio track analysed")
sm = SM.analyze(wav)
for b in (0.0, 13.25, 100.0): check(abs(SM.beat_at(sm, SM.time_at(sm, b)) - b) < 1e-6, "beat_at/time_at inverse")
SM.FIXES = D / "fixes.json"
SM.set_fix(wav, shift=0.5); s2 = SM.analyze(wav)
check(abs((s2["beats"][0] - sm["beats"][0]) % (60 / 128) - 30 / 128) < 0.002 and "shifted" in s2["grid_source"], "grid fix: half-beat shift remembered")
SM.set_fix(wav, clear=True); SM.set_fix(wav, bar=1); s3 = SM.analyze(wav); check(s3["downbeat_index"] == (sm["downbeat_index"] + 1) % 4, "grid fix: bar 1 moved")
SM.set_fix(wav, clear=True)
import urllib.parse
(D / "rb.xml").write_text(f'<DJ_PLAYLISTS><COLLECTION><TRACK Location="file://localhost{urllib.parse.quote(str(wav.resolve()))}" AverageBpm="128.00">'
                          '<TEMPO Inizio="0.250" Bpm="128.00" Metro="4/4" Battito="3"/></TRACK></COLLECTION></DJ_PLAYLISTS>')
s4 = SM.analyze(wav, rekordbox_xml=str(D / "rb.xml"))
check(s4["grid_source"] == "rekordbox xml" and abs(s4["first_downbeat_s"] - (0.25 + 2 * 60 / 128)) < 1e-3, "rekordbox XML grid (Battito 3 -> bar 1 two beats later)")
for bad in (D / "nope.mp3",):
    try: SM.analyze(bad); check(False)
    except FileNotFoundError: pass
(D / "junk.mp3").write_bytes(b"not audio at all" * 100)
try: SM.analyze(D / "junk.mp3"); check(False, "junk decoded")
except ValueError: pass

# --- autoshow on different rigs
sm = SM.analyze(wav)
show = A.make_show(sm, rig, "neon", "club")
check(not any(L["group"] == "strobes" for c in show["cues"].values() for L in c), "no strobe layers unless asked")
check(any(L["group"] == "strobes" for c in A.make_show(sm, rig, "neon", "club", strobe=True)["cues"].values() for L in c), "--strobe adds drop flashes")
sheet = dict((c, b) for b, c in show["songs"]["song"]["cues"]); check(sheet.get("drop") == 41 and sheet.get("predrop") == 40, f"drop/predrop bars {show['songs']['song']['cues']}")
lay = show["cues"]["predrop"]; render(rig, lay, Tick(39 * 4 + 3.5, 128.0))
check(all(f.values[i] == 0 for f in rig.fixtures for i, _, s_ in f.chans if s_["role"] == "dimmer") and all(F.emitted_rgb(f, h) == (0, 0, 0) for f in rig.fixtures for h in range(f.heads)),
      "beat 4 of the pre-drop bar is dark")
for f in rig.group("movers"):
    for p_, t_ in A.safe_spots(rig, f):
        rig.home(); f.dimmer(1); f.color(1, 1, 1); f.aim(p_, t_)
        check(not S.eye_hazards([e for e in S.emitters(rig) if e.name.startswith(f.name)], rig.venue), f"{f.name} spot {p_},{t_} crosses eye height")
lum, flips = {f.name: 0.0 for f in rig.fixtures}, {f.name: 0 for f in rig.fixtures}
step = 1 / 16; n = 0
for beat, eng in offline(rig, show, song="song", step_beats=step):
    n += 1
    for f in rig.fixtures:
        v = max(max(F.emitted_rgb(f, h)) for h in range(f.heads)) / 255
        if v > 0.75 and lum[f.name] < 0.25: flips[f.name] += 1
        lum[f.name] = v
secs = n * step * 60 / 128
check(max(flips.values()) / secs <= 3.0, f"strong flashes <= 3/s per light ({max(flips.values()) / secs:.2f}/s)")
for name, build in (("pars only", lambda d_: P.add(d_, "generic_rgb_3ch", 3, "floor")), ("movers only", lambda d_: P.add(d_, "generic_spot_colorwheel_8ch", 2, "stands either side")),
                    ("pixel bars", lambda d_: P.add(d_, "generic_ledbar_8seg_rgb_24ch", 2, "floor behind"))):
    d2 = P.new_rig("t2", "club", path=D / "rig2.json", force=True); build(d2); P.save(d2); r2 = F.Rig.load(D / "rig2.json")
    sh2 = A.make_show(sm, r2, "auto", "hype"); check(sum(1 for _ in offline(r2, sh2, song="song", step_beats=2.0)) > 100, f"{name}: show plays")
    A.live_show(r2)
try: A.make_show(sm, F.Rig("empty", [F.Fixture("f", F.load_profile("generic_fogger"), 1)]), "auto"); check(False, "fog-only rig accepted")
except LookupError: pass
check(A.palette("red,#00ff80")[1] == [0.0, 1.0, 0.502], "custom palette with hex")

# --- FileClock
clk = FileClock(lambda ts: SM.beat_at(sm, ts), sm["bpm"]); t0 = 1000.0; clk.start(t0)
check(abs(clk.beat(t0 + sm["first_downbeat_s"])) < 1e-6 and abs(clk.beat(t0 + sm["first_downbeat_s"] + 60 / 128 * 8) - 8) < 1e-3, "FileClock follows the grid")
clk.nudge(1); check(abs(clk.beat(t0 + sm["first_downbeat_s"]) + 1) < 1e-6, "nudge shifts the lights a beat later")
for bad in (lambda: clk.set_bpm(120), lambda: clk.tap(), lambda: clk.step(1)):
    try: bad(); check(False, "tempo change accepted")
    except RuntimeError: pass

# --- dj.play in simulation, blackout after
from lighting.dmx import MemoryOutput
mem = MemoryOutput(); dj.output = lambda port="auto", artnet=None: mem
r = dj.play(str(wav), rig, audio=False, seconds=1.2, verbose=False, palette_name="ice")
check(r["frames"] > 20 and r["dmx_errors"] == 0 and r["error"] is None and mem.last == bytes(512), f"play streams then blacks out {r}")
dj.SETTINGS = D / "dj_settings.json"; dj.save_settings(rig="t_rig", latency_ms=120); check(dj.settings()["latency_ms"] == 120, "settings round trip")
print("ok: lighting dj (fixture defaults/wheels/groups, plain-word patching + DIP, OFL import, aim_at, songmap formats/grid/drops/fixes/rekordbox xml, autoshow safety on several rigs, FileClock, play)")
