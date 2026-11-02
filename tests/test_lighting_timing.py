"""lighting timing: clocks (internal pause/step/tap, manual, Link-following with seamless fall-back), program/song timelines
on bar lines, stepping back through cues, audio-reactive layers (react, onset pulse) and the audio analyzer on a synthetic
kick track. Link runs against net.abletonlink.LinkPeer on a PRIVATE loopback group (never the real one)."""
import sys, time, math, random; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._util import check
from lighting import clock as C, show as S
from lighting.fixtures import Rig
from lighting.audiofeed import Analyzer
from net import abletonlink as L

# --- internal clock
c = C.InternalClock(120); t = time.monotonic()
check(abs(c.beat(t + 1.0) - c.beat(t) - 2.0) < 1e-9, "120 bpm = 2 beats/s")
c.pause(); b = c.beat(); c.step(0.25); check(abs(c.beat() - b - 0.25) < 1e-12, "step while paused")
c.step(-0.5); check(abs(c.beat() - b + 0.25) < 1e-12, "step back")
try: C.InternalClock(500); check(False, "bpm 500 accepted")
except ValueError: pass
c.pause(False)
try: c.step(1); check(False, "step while running accepted")
except RuntimeError: pass

# --- manual clock + timelines
show = S.load_show("halloween_demo"); rig = Rig.load("halloween")
m = C.ManualClock(120); eng = S.Engine(rig, clock=m)
m.value = 5.0; eng.play_program(show)                                     # mid-bar: first cue now, changes on bar lines
check(eng.timeline[0][0] == 4.0 and eng.timeline[1][0] == 4.0 + 32, f"program aligned to the bar line {eng.timeline[:2]}")
m.value = 4.0 + 32 + 0.1; eng.frame(); check(eng.cue_name == "chase", "second cue after 8 bars")
eng.pause(True); eng.step(-1.0); eng.frame(); check(eng.cue_name == "intro", "stepping back goes back through cues")
eng.pause(False)
m.value = 9.0; eng.play_song(show, "example_128")
check(eng.timeline[0][0] == 12.0, f"song starts on the next downbeat ({eng.timeline[0]})")
m.value = 12.0 + 4 * 16 + 0.01; eng.frame(); check(eng.cue_name == "chase", "song bar 17 → chase")
m.value = 12.0 + 4 * 32 + 0.01; eng.frame(); check(eng.cue_name == "kick_pulse", "song bar 33 → kick_pulse")
m.value = 12.0 + 4 * 72 + 1; eng.frame(); check(eng.program_done and not eng.timeline, "song ends after its bars")
for bad in (lambda: eng.play_song(show, "nope"),):
    try: bad(); check(False, "unknown song accepted")
    except LookupError: pass

# --- includes, song tempo + "start now", nudge, chomp
full = S.load_show("halloween_demo")
song = "I Won't Back Down — Tom Petty"
check("gator_chorus" in full["cues"] and song in full["songs"] and "intro" in full["cues"], "include merges gators into halloween_demo")
m2 = C.ManualClock(120); e2 = S.Engine(Rig.load("halloween"), clock=m2); m2.value = 6.3
e2.play_song(full, song, now=True)
check(m2.bpm == 114 and e2.timeline[0][0] == 8.0 and m2.value == 8.0, f"start now: song bpm + this instant = bar 1 ({e2.timeline[0]}, beat {m2.value})")
m2.value = 8.0 + 4 * 22 + 0.01; e2.frame(); check(e2.cue_name == "gator_chorus", "IWBD bar 23 = first chorus")
m2.value = 8.0 + 4 * 62 + 0.01; e2.frame(); check(e2.cue_name == "gator_singalong", "IWBD bar 63 = sing-along chorus")
ic = C.InternalClock(114); b0 = ic.beat(); ic.nudge(0.125); check(abs(ic.beat() - b0 - 0.125) < 0.01, "nudge shifts the running beat")
r = Rig.load("halloween")
S.render(r, [{"group": "beams", "effect": "sweep", "shape": "chomp", "pan": 80, "center": [0, 60], "beats": 1}], S.Tick(0.0))
closed = [r["beam1"].levels[1], r["beam2"].levels[91]]
S.render(r, [{"group": "beams", "effect": "sweep", "shape": "chomp", "pan": 80, "center": [0, 60], "beats": 1}], S.Tick(0.9))
opened = [r["beam1"].levels[1], r["beam2"].levels[91]]
check(abs(closed[0] - closed[1]) <= 1 and opened[0] < closed[0] < opened[1], f"chomp: shut on the beat, beam1 opens -pan / beam2 +pan {closed} → {opened}")

# --- audio-reactive layers (fake feed)
class Feed:
    def __init__(self): self.s = {"level": 0.0, "bass": 0.0, "since_onset": math.inf}
    def signals(self): return self.s
f = Feed(); eng = S.Engine(Rig.load("halloween"), clock=C.ManualClock(120)); eng.audio = f
eng.set_layers([{"group": "pars", "effect": "color", "color": "orange", "react": "bass", "floor": 0.2}])
eng.frame(); low = eng.rig["par1"].levels[21]
f.s["bass"] = 1.0; eng.frame(); high = eng.rig["par1"].levels[21]
check(high > low * 3 and low > 0, f"react: bass scales the dimmer floor..1 ({low} → {high})")
eng.set_layers([{"group": "beams", "effect": "pulse", "color": "white", "trigger": "onset", "decay": 6}])
f.s["since_onset"] = 0.0; eng.frame(); hit = eng.rig["beam1"].levels[4]
f.s["since_onset"] = 0.5; eng.frame(); later = eng.rig["beam1"].levels[4]
check(hit == 255 and later < 20, f"onset pulse flashes on a hit and decays ({hit} → {later})")
try: S.check_layers([{"effect": "color", "react": "treble"}], eng.rig); check(False, "bad react accepted")
except ValueError: pass

# --- analyzer on a synthetic 120 bpm kick track (16 kicks + hats)
from media import synth as M
sr = 22050; tr = M.Track(8.5, sr); rng = random.Random(1)
for k in range(16): tr.add(M.kick(sr), 0.25 + k * 0.5, 0.9)
for k in range(32): tr.add(M.hat(sr, rng), 0.5 + k * 0.25, 0.25)
a = Analyzer(sr); hits = []
for i in range(0, len(tr.samples), 256):
    n = a.onsets; a.push(tr.samples[i:i + 256])
    if a.onsets > n: hits.append(a.last_onset)
check(len(hits) == 16 and all(abs(h - (0.25 + k * 0.5)) < 0.03 for k, h in enumerate(hits)), f"16 kicks found on time {[round(h, 2) for h in hits]}")
check(0 < a.signals()["level"] <= 1 and 0 < a.signals()["bass"] <= 1, "level/bass normalised")

# --- Link clock: follows the DJ app, falls back without a jump, refuses local tempo while following
G, P = "239.255.77.78", 30809
peer = L.LinkPeer(bpm=126.0, group=G, port=P, ghost_offset_us=3_000_000, beat_at_origin=100.0).start()
ln = L.LinkListener(group=G, port=P, ifaces=["127.0.0.1"], samples=30).start()
lc = C.LinkClock(ln, bpm=100)
t0 = time.time()
while lc.source != "link" and time.time() - t0 < 8: time.sleep(0.05)
check(lc.source == "link" and abs(lc.bpm - 126) < 0.01, f"follows Link ({lc.state()})")
h = L.host_us(); truth = peer.timeline.to_beats(h + peer.off)
check(abs(lc.beat(h / 1e6) - truth) < 0.01, "beat = the DJ's beat")
try: lc.set_bpm(90); check(False, "set_bpm accepted while following Link")
except RuntimeError: pass
lc.pause(); fz = lc.beat(); lc.step(0.5); check(abs(lc.beat() - fz - 0.5) < 1e-9, "pause + step work while following Link"); lc.pause(False)
last = lc.beat(); peer.stop()
t0 = time.time()
while lc.source == "link" and time.time() - t0 < 8: time.sleep(0.05)
b1 = lc.beat(); check(lc.source == "internal" and 0 <= b1 - last < 1.5 * (time.time() - t0 + 0.2) * 126 / 60 + 0.1, f"falls back without jumping ({last:.2f} → {b1:.2f})")
lc.set_bpm(110); check(abs(lc.bpm - 110) < 1e-9, "internal tempo usable again after Link leaves")
ln.stop()
print("ok: lighting timing (clocks incl. Link follow/fall-back, bar-aligned program/songs, step-back, audio react/onset, kick analyzer)")
