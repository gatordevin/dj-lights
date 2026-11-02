"""Light-show engine: beat- and audio-driven effects layered into cues, played on a rig from any clock (internal, rekordbox Link).
CLI:
  python3 lighting/show.py list                                   effects, colours, shows (cues + songs)
  python3 lighting/show.py play SHOW [--bpm 120] [--loops 1] [--port auto|none] [--start CUE] [--link]   run the program
  python3 lighting/show.py song SHOW SONG [--port ...] [--link]     run a song's cue sheet from the next downbeat
  python3 lighting/show.py cue SHOW CUE [--seconds 30]             hold one cue
  python3 lighting/show.py preview SHOW [--song S] [--fps 10] [--png out/lighting/preview.png]   offline colour/motion strip
Show file lighting/shows/NAME.json:
  {"bpm": 120, "cues": {"cue": [layer, ...]}, "program": [["cue", bars], ...],
   "songs": {"name": {"bpm"?: 128, "cues": [[bar, "cue"], ...], "bars"?: total}}, "include"?: ["other_show", ...]}
  bars are 1-based, 4 beats each; "include" merges other show files' cues/songs (this file wins on name clashes)
  layer = {"group": "movers"|"pars"|fixture|"all", "effect": NAME, ...effect args, "react"?: "level"|"bass", "floor"?: 0.2}
     later layers override earlier ones; "react" scales the layer's dimmer by live audio (floor..1)
Effects (beats at the clock's BPM; "spread" offsets the fixtures/heads of a group, in cycles):
  color      color="purple"|[r,g,b]  dimmer=1
  chase      colors=[...] beats=1                         step through colours, each slot offset by one step
  fade       colors=[...] beats=4 spread=0                smooth crossfade through the colours
  rainbow    beats=8 spread=0.25 dimmer=1                 hue wheel
  pulse      color? beats=1 decay=4 floor=0 trigger="beat"|"onset"   dimmer jumps to 1 on each beat / audio hit, then decays
  strobe     rate=0.5 color="white"                       strobe channel (0..1 speed), dimmer full
  sweep      shape="circle"|"fan"|"figure8"|"tilt_wave"|"pan_wave"|"ballyhoo"|"chomp" pan=90 tilt=40 beats=8 spread=0.25
             (chomp: neighbouring slots — spider bars, beam pairs — snap together every `beats`, then open: a Gator chomp)
             center=[pan_deg, tilt_deg] speed?           pan/tilt shapes (degrees from centre of travel)
  aim        pan=0 tilt=0                                 hold a position
  blackout                                                dimmer 0
  punch      colors=[...] slots=[0..3] decay=9 accent=.85    dark except a full hit on its own beat(s) of the bar; returns {} off-beat so layers coexist
  flame      depth=.3 base=.75 spread=.37 low/high=[r,g,b]    LED fire: red<->orange + slow shallow flicker (flash-safe)
  snap       spots=[[pan,tilt],...] every=1 lead=.5            jump to a new aim in the DARK half-beat before the hit
  Adding an effect: def fx_NAME(a, i, n, t) -> {"color"|"dimmer"|"aim"|"strobe"|"speed": ...}; t = Tick(beat, bpm, level,
  bass, since_onset). It is picked up automatically (EFFECTS) and validated by a dry run in check_layers.
  Registered: fx_color fx_chase fx_fade fx_rainbow fx_pulse fx_strobe fx_sweep fx_aim fx_blackout fx_punch fx_snap fx_flame (called via EFFECTS[name]).
Python:
  eng = Engine(rig, outputs=[dmx out], clock=None|InternalClock|LinkClock|ManualClock, bpm=120); eng.start(); eng.stop()
  eng.cue(show, "name") · eng.set_layers([...]) · eng.play_program(show, start=None, loops=1) · eng.play_song(show, "song")
  eng.stop_program() · eng.hold(name, layers|None) (overlays, e.g. strobe while a button is held)
  clock: eng.beat() · eng.bpm · eng.set_bpm · eng.tap · eng.pause(on) · eng.paused · eng.step(±beats) · eng.nudge(±beats)
         (all delegate to eng.clock; nudge shifts a running internal clock to line up with music by ear)
  eng.audio = object with .signals() -> {"level", "bass", "since_onset"} (lighting/audiofeed.AudioFeed) or None
  eng.master (0..1), eng.blackout, eng.laser_armed (False: role-"laser" channels forced 0), eng.raw {ch: v} (forced last),
  eng.haze_burst(s) (≤ HAZE_MAX_S, rest HAZE_REST_S), eng.state() -> JSON-able snapshot for UIs
  render(rig, layers, tick, master, blackout, laser_armed) — pure: layers → fixture values (engine, previews, tests)
  offline(rig, show, bpm, song, step_beats) -> iterator of (beat, engine) through a whole program/song (ManualClock)
  timeline: the program/song is a list of (start_beat, cue); the cue at the current beat is chosen every frame, so
  stepping backwards while paused also goes back through cues; programs/songs start on the bar line.
Contract: unknown effect/colour/group/cue/song → LookupError at load/cue time (not mid-show); bad args → ValueError/TypeError.
Limits: one universe; effects are stateless functions of the Tick (no motor physics); RGB-only fixtures ignore white.
Depends on: lighting/fixtures, lighting/dmx, lighting/clock. Used by: lighting/console.py, lighting/stage3d.py."""
import sys, json, math, time, colorsys, threading, argparse
from dataclasses import dataclass
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lighting.fixtures import Rig, emitted_rgb
from lighting.dmx import Universe, Sender, open_output, MemoryOutput
from lighting.clock import InternalClock, ManualClock
from core.lookup import pick

SHOWS = Path(__file__).resolve().parent / "shows"
PALETTE = {"red": (1, 0, 0), "orange": (1, 0.35, 0), "amber": (1, 0.6, 0), "yellow": (1, 1, 0), "lime": (0.5, 1, 0),
           "green": (0, 1, 0), "cyan": (0, 1, 1), "blue": (0, 0, 1), "uv": (0.35, 0, 1), "purple": (0.6, 0, 1),
           "magenta": (1, 0, 1), "pink": (1, 0.3, 0.6), "white": (1, 1, 1), "warm": (1, 0.75, 0.45),
           "uf_orange": (1, 0.27, 0.03), "uf_blue": (0, 0.12, 0.85), "off": (0, 0, 0)}
HAZE_MAX_S, HAZE_REST_S = 20.0, 30.0
BAR = 4


@dataclass
class Tick:
    """What an effect may depend on. level/bass 0..1 from live audio (0 without audio); since_onset in seconds (inf: none)."""
    beat: float
    bpm: float = 120.0
    level: float = 0.0
    bass: float = 0.0
    since_onset: float = math.inf


def rgb(c):
    if isinstance(c, str):
        return pick(PALETTE, c, "colour")
    if isinstance(c, (list, tuple)) and len(c) == 3 and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in c): return tuple(c)
    raise ValueError(f"colour must be a palette name or [r,g,b] 0..1, got {c!r}")


def _frac(x): return x - math.floor(x)


# ---- effects: f(args, i, n, t: Tick) -> attributes for slot i of n (slots = the group's fixtures × heads) ----
def fx_color(a, i, n, t): return {"color": rgb(a.get("color", "white")), "dimmer": a.get("dimmer", 1.0)}
def fx_blackout(a, i, n, t): return {"dimmer": 0.0}
def fx_chase(a, i, n, t):
    cs = [rgb(c) for c in a.get("colors", ["red", "blue"])]
    return {"color": cs[(int(t.beat // a.get("beats", 1)) + i) % len(cs)], "dimmer": a.get("dimmer", 1.0)}
def fx_fade(a, i, n, t):
    cs = [rgb(c) for c in a.get("colors", ["red", "blue"])]; pos = t.beat / a.get("beats", 4) + i * a.get("spread", 0.0) * len(cs)
    k = int(pos) % len(cs); f = _frac(pos); c0, c1 = cs[k], cs[(k + 1) % len(cs)]
    return {"color": tuple(c0[j] + (c1[j] - c0[j]) * f for j in range(3)), "dimmer": a.get("dimmer", 1.0)}
def fx_rainbow(a, i, n, t):
    return {"color": colorsys.hsv_to_rgb(_frac(t.beat / a.get("beats", 8) + i * a.get("spread", 0.25)), 1, 1), "dimmer": a.get("dimmer", 1.0)}
def fx_pulse(a, i, n, t):
    trig = a.get("trigger", "beat")
    if trig == "onset": x = t.since_onset * t.bpm / 60.0                     # beats since the last audio hit
    elif trig == "beat": x = _frac(t.beat / a.get("beats", 1) + i * a.get("spread", 0.0))
    else: raise ValueError("pulse trigger must be 'beat' or 'onset'")
    lvl = a.get("floor", 0.0) + (1 - a.get("floor", 0.0)) * math.exp(-a.get("decay", 4.0) * min(x, 50.0))
    out = {"dimmer": lvl}
    if "color" in a: out["color"] = rgb(a["color"])
    return out
def fx_strobe(a, i, n, t): return {"strobe": a.get("rate", 0.5), "dimmer": 1.0, "color": rgb(a.get("color", "white"))}
def fx_aim(a, i, n, t): return {"aim": (a.get("pan", 0.0), a.get("tilt", 0.0))}
def fx_sweep(a, i, n, t):
    ph = 2 * math.pi * (t.beat / a.get("beats", 8) + i * a.get("spread", 0.25))
    P, T = a.get("pan", 90.0) / 2, a.get("tilt", 40.0) / 2; cp, ct = a.get("center", (0.0, 0.0))
    shape = a.get("shape", "circle")
    if shape == "circle": p, q = P * math.cos(ph), T * math.sin(ph)
    elif shape == "figure8": p, q = P * math.sin(ph), T * math.sin(2 * ph)
    elif shape == "fan": p, q = P * math.sin(ph) * (1 if i % 2 == 0 else -1), 0.0
    elif shape == "pan_wave": p, q = P * math.sin(ph), 0.0
    elif shape == "tilt_wave": p, q = 0.0, T * math.sin(ph)
    elif shape == "ballyhoo": p, q = P * math.sin(ph), T * math.sin(3 * ph + 1)
    elif shape == "chomp":                                                   # jaws: pairs snap shut on the beat, open slowly
        f = _frac(t.beat / a.get("beats", 1)); g = 1 - math.exp(-5 * f); sgn = -1 if i % 2 == 0 else 1   # first of a pair opens toward -x
        p, q = sgn * P * g, sgn * T * g
    else: raise LookupError(f"unknown sweep shape {shape!r}")
    out = {"aim": (cp + p, ct + q)}
    if "speed" in a: out["speed"] = a["speed"]
    return out

def fx_punch(a, i, n, t):
    """Blackout-and-hit: the fixture is DARK except for a full-brightness hit on its own beat(s) of the bar, which decays very fast (default decay 9).
    slots = beats of the 4-beat bar this fixture hits (0..3, default all); colors cycle per hit ('step': 'hit') or per bar ('bar'); downbeat_color overrides on beat 0;
    accent = brightness of non-downbeat hits (default 0.85). Returns {} off its own beats so other layers' hits show (later layers override earlier ones).
    Flash-rate guard: above max_hz (default 2.6 hits/s) it only hits every 2nd beat (every 4th above 2*max_hz), so no fixture flashes faster than ~3 Hz at any tempo."""
    b = t.beat; k = math.floor(b); frac = b - k; hz = t.bpm / 60.0; mx = a.get("max_hz", 2.6)
    skip = 1 if hz <= mx else 2 if hz <= 2 * mx else 4
    if k % skip: return {}
    bb = k % 4
    if bb not in a.get("slots", (0, 1, 2, 3)): return {}
    cs = [rgb(c) for c in a.get("colors", [a.get("color", "white")])]
    ci = (k // 4 if a.get("step", "hit") == "bar" else k) + i
    col = rgb(a["downbeat_color"]) if (bb == 0 and "downbeat_color" in a) else cs[ci % len(cs)]
    lvl = math.exp(-a.get("decay", 9.0) * frac * skip) * (1.0 if bb == 0 else a.get("accent", 0.85))
    return {"dimmer": lvl, "color": col}
def fx_snap(a, i, n, t):
    """Jump to a new aim spot every `every` beats, switching `lead` beats BEFORE the hit (default half a beat) so the move happens in the dark and the light
    arrives already in place. spots = [[pan, tilt], ...] in degrees (keep inside the audited safe range), cycled; each slot i starts one spot later."""
    spots = a.get("spots", [[-60, 52], [-30, 70], [0, 58], [30, 72], [60, 52], [20, 45], [-20, 76], [0, 66]])
    j = int(math.floor((t.beat + a.get("lead", 0.5)) / a.get("every", 1))) + i
    pan, tilt = spots[j % len(spots)]; out = {"aim": (pan, tilt)}
    if "speed" in a: out["speed"] = a["speed"]
    return out

def fx_flame(a, i, n, t):
    """LED fire look: red<->orange colour and a gently flickering dimmer, built from three slow incommensurate waves (<= ~2.4 Hz at 128 BPM) with shallow depth
    (default 30%), so it reads as flame without strobing. depth 0..0.5, base = mean brightness, spread offsets each slot/head so cells flicker independently."""
    ph = t.beat + i * a.get("spread", 0.37)
    w = (math.sin(2 * math.pi * 0.37 * ph) + 0.6 * math.sin(2 * math.pi * 0.61 * ph + 1.3) + 0.35 * math.sin(2 * math.pi * 1.13 * ph + 2.1)) / 1.95   # -1..1
    x = 0.5 + 0.5 * w; depth = min(0.5, a.get("depth", 0.3)); lo, hi = rgb(a.get("low", [0.9, 0.04, 0.0])), rgb(a.get("high", [1.0, 0.42, 0.0]))
    return {"color": tuple(lo[j] + (hi[j] - lo[j]) * x for j in range(3)), "dimmer": min(1.0, a.get("base", 0.75) + depth * (x - 0.5))}
EFFECTS = {k[3:]: v for k, v in dict(globals()).items() if k.startswith("fx_")}
REACT = ("level", "bass")


def check_layers(layers, rig):
    for L in layers:
        if L.get("effect") not in EFFECTS: raise LookupError(f"unknown effect {L.get('effect')!r} (have {sorted(EFFECTS)})")
        rig.group(L.get("group", "all"))
        if "color" in L: rgb(L["color"])
        for c in L.get("colors", []): rgb(c)
        if L.get("react") not in (None,) + REACT: raise ValueError(f"react must be one of {REACT}")
        EFFECTS[L["effect"]](L, 0, 1, Tick(0.0))                         # dry run catches bad shapes/args now


def _clamp_aim(f, p, t):
    P, T = f.profile.pan_deg / 2, f.profile.tilt_deg / 2
    return (max(-P, min(P, p)) if P else None, max(-T, min(T, t)) if T else None)


def render(rig, layers, tick, master=1.0, blackout=False, laser_armed=False):
    """Pure: home every fixture, apply layers in order (later override), then master/blackout/safety. Returns the rig.
    tick: Tick or a plain beat number. Colour and aim are per head (spider bars); dimmer/strobe/speed per fixture."""
    if not isinstance(tick, Tick): tick = Tick(float(tick))
    rig.home()
    st = {f.name: {"dimmer": 0.0, "strobe": 0.0, "speed": None, "heads": [{"color": (0, 0, 0), "aim": None} for _ in range(f.heads)]}
          for f in rig.fixtures}
    for L in layers:
        fn = EFFECTS[L["effect"]]; react = L.get("react")
        k_audio = 1.0 if not react else L.get("floor", 0.0) + (1 - L.get("floor", 0.0)) * getattr(tick, react)
        slots = [(f, h) for f in rig.group(L.get("group", "all")) for h in range(f.heads)]
        for i, (f, h) in enumerate(slots):
            for k, v in fn(L, i, len(slots), tick).items():
                if k in ("color", "aim"): st[f.name]["heads"][h][k] = v
                elif k == "dimmer": st[f.name][k] = v * k_audio
                else: st[f.name][k] = v
    for f in rig.fixtures:
        s = st[f.name]; dim = 0.0 if blackout else max(0.0, min(1.0, s["dimmer"])) * master
        for h, hs in enumerate(s["heads"]):
            c = [max(0.0, min(1.0, x)) for x in hs["color"]]
            if "dimmer" not in f.roles: c = [x * dim for x in c]          # RGB-only: dimming via colour
            f.color(*c, head=h if f.heads > 1 else None)
            if hs["aim"] is not None: f.aim(*_clamp_aim(f, *hs["aim"]), head=h if f.heads > 1 else None)
        f.dimmer(dim)
        if s["strobe"]: f.strobe(max(0.0, min(1.0, s["strobe"])))
        if s["speed"] is not None: f.speed(max(0.0, min(1.0, s["speed"])))
        if "laser" in f.roles and not laser_armed:
            for i, _, spec in f.chans:
                if spec["role"] == "laser": f.values[i] = 0
    return rig


def _program_timeline(show, start_beat, start=None, loops=1):
    prog = show.get("program") or [[c, 8] for c in show["cues"]]
    for c, bars in prog:
        if c not in show["cues"]: raise LookupError(f"program uses unknown cue {c!r}")
        if not bars > 0: raise ValueError(f"program bars must be > 0 ({c})")
    prog = prog * max(1, loops)
    if start:
        k = next((i for i, (c, _) in enumerate(prog) if c == start), None)
        if k is None: raise LookupError(f"no cue {start!r} in the program")
        prog = prog[k:]
    tl, b = [], start_beat
    for c, bars in prog: tl.append((b, c)); b += BAR * bars
    return tl, b


def _song_timeline(show, name, start_beat):
    songs = show.get("songs", {})
    if name not in songs: raise LookupError(f"no song {name!r} (have {list(songs)})")
    s = songs[name]; rows = sorted(s["cues"], key=lambda r: r[0])
    if not rows: raise ValueError(f"song {name} has no cues")
    for bar, c in rows:
        if c not in show["cues"]: raise LookupError(f"song {name}: unknown cue {c!r}")
        if bar < 1: raise ValueError(f"song {name}: bars are 1-based")
    end = s.get("bars", rows[-1][0] + 7) * BAR
    return [(start_beat + (bar - 1) * BAR, c) for bar, c in rows], start_beat + end


class Engine:
    def __init__(self, rig, outputs=None, bpm=120.0, fps=40, clock=None):
        self.rig, self.fps = rig, fps
        self.clock = clock or InternalClock(bpm)
        self.universe = Universe(); self.outputs = outputs or [MemoryOutput()]
        self.layers, self.cue_name, self.master, self.blackout, self.laser_armed = [], None, 1.0, False, False
        self.overlay, self.raw, self.audio = {}, {}, None
        self._haze_until, self._haze_last = 0.0, -1e9
        self.timeline, self.timeline_end, self.timeline_name, self.show = [], None, None, None
        self.lock = threading.RLock(); self._stop = threading.Event(); self._thread = None; self.sender = None
        self.error, self.program_done = None, False

    # clock (delegated, so UIs and tests keep one API)
    def beat(self, t=None): return self.clock.beat(t)
    @property
    def bpm(self): return self.clock.bpm
    @property
    def paused(self): return self.clock.paused
    def set_bpm(self, bpm): self.clock.set_bpm(bpm)
    def tap(self): return self.clock.tap()
    def pause(self, on=True): self.clock.pause(on)
    def step(self, beats): self.clock.step(beats)
    def nudge(self, beats): self.clock.nudge(beats)

    def tick(self, t=None):
        b = self.clock.beat(t); sig = self.audio.signals() if self.audio else {}
        return Tick(b, self.clock.bpm, sig.get("level", 0.0), sig.get("bass", 0.0), sig.get("since_onset", math.inf))

    # content
    def set_layers(self, layers, name=None):
        check_layers(layers, self.rig)
        with self.lock: self.layers, self.cue_name = list(layers), name

    def cue(self, show, name):
        if name not in show["cues"]: raise LookupError(f"no cue {name!r} (have {list(show['cues'])})")
        self.set_layers(show["cues"][name], name)

    def _next_downbeat(self):
        b = self.beat(); return b if abs(b / BAR - round(b / BAR)) < 1e-6 else math.ceil(b / BAR) * BAR

    def _start_timeline(self, show, tl, end, name):
        for c in show["cues"].values(): check_layers(c, self.rig)
        with self.lock:
            self.show, self.timeline, self.timeline_end, self.timeline_name, self.program_done = show, tl, end, name, False
            if self.beat() >= tl[0][0]: self._apply_timeline(self.beat())

    def play_program(self, show, start=None, loops=1):
        """The show's program: first cue now, cue changes on bar lines (aligned to the clock's downbeats)."""
        tl, end = _program_timeline(show, math.floor(self.beat() / BAR) * BAR, start, loops)
        self.cue(show, tl[0][1]); self._start_timeline(show, tl, end, "program")

    def play_song(self, show, name, now=False):
        """A song's cue sheet ([bar, cue] rows). Following Link: from the DJ's next bar. Otherwise: the clock takes the
        song's bpm, and now=True makes this instant bar 1 beat 1 (press on the song's first downbeat); else next downbeat."""
        bpm = show.get("songs", {}).get(name, {}).get("bpm")
        following = getattr(self.clock, "source", "") == "link"
        if bpm and not following:
            try: self.clock.set_bpm(bpm)
            except RuntimeError: pass
        if now and not following and hasattr(self.clock, "rebase") and not self.paused:
            self.clock.rebase(math.ceil(self.beat() / BAR) * BAR)               # this instant becomes a downbeat
        tl, end = _song_timeline(show, name, self._next_downbeat())
        self._start_timeline(show, tl, end, f"song:{name}")

    def stop_program(self):
        with self.lock: self.timeline, self.timeline_end, self.timeline_name = [], None, None

    def _apply_timeline(self, beat):
        if not self.timeline: return
        if self.timeline_end is not None and beat >= self.timeline_end:
            self.timeline, self.timeline_name, self.program_done = [], None, True; return
        cur = None
        for b0, c in self.timeline:
            if b0 <= beat: cur = c
            else: break
        if cur and cur != self.cue_name: self.cue(self.show, cur)

    def hold(self, name, layers):
        """Overlay layers on top of whatever cue/program runs (e.g. strobe while a button is held); layers=None removes it."""
        if layers: check_layers(layers, self.rig)
        with self.lock:
            if layers: self.overlay[name] = list(layers)
            else: self.overlay.pop(name, None)

    def haze_burst(self, seconds):
        if not 0 < seconds <= HAZE_MAX_S: raise ValueError(f"haze burst must be 0 < s <= {HAZE_MAX_S}")
        now = time.monotonic()
        if now - self._haze_last < HAZE_REST_S: raise RuntimeError(f"haze resting: wait {HAZE_REST_S - (now - self._haze_last):.0f} s")
        if not [f for f in self.rig.fixtures if "haze" in f.roles]: raise LookupError("rig has no haze fixture")
        self._haze_until, self._haze_last = now + seconds, now

    # loop
    def frame(self, t=None):
        with self.lock:
            tk = self.tick(t); self._apply_timeline(tk.beat)
            render(self.rig, self.layers + [L for ls in self.overlay.values() for L in ls], tk, self.master, self.blackout, self.laser_armed)
            now = time.monotonic()
            for f in self.rig.fixtures:
                for i, _, s in f.chans:
                    if s["role"] == "haze": f.values[i] = 255 if now < self._haze_until else 0
            self.rig.render(self.universe)
            for ch, v in self.raw.items(): self.universe[ch] = v
        return tk.beat

    def start(self):
        self.sender = Sender(self.universe, self.outputs, fps=min(self.fps, 40)).start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="show"); self._thread.start(); return self

    def _run(self):
        while not self._stop.is_set():
            try: self.frame()
            except Exception as e: self.error = f"{type(e).__name__}: {e}"
            self._stop.wait(1.0 / self.fps)

    def stop(self):
        self._stop.set()
        if self._thread: self._thread.join(2)
        if self.sender: self.sender.stop(blackout=True)

    def state(self):
        tk = self.tick(); b = tk.beat; o = self.outputs[0]
        upcoming = [c for b0, c in self.timeline if b0 > b][:3]
        return {"rig": self.rig.name, "output": getattr(o, "mode", None) or (f"dongle {o.port}" if hasattr(o, "port") else type(o).__name__),
                "clock": self.clock.state(), "bpm": round(self.bpm, 1), "beat": round(b, 4), "bar": int(b // BAR) + 1,
                "cue": self.cue_name, "timeline": self.timeline_name, "program": ([self.cue_name] + upcoming) if self.timeline else [],
                "master": self.master, "blackout": self.blackout, "paused": self.paused, "laser_armed": self.laser_armed,
                "haze_on": time.monotonic() < self._haze_until, "holds": sorted(self.overlay), "raw": {str(c): v for c, v in self.raw.items()},
                "audio": {"level": round(tk.level, 3), "bass": round(tk.bass, 3),
                          "since_onset": None if math.isinf(tk.since_onset) else round(tk.since_onset, 2),
                          "source": getattr(self.audio, "name", None)},
                "fps": round(self.sender.fps_actual, 1) if self.sender else 0, "dmx_errors": self.sender.errors if self.sender else 0,
                "error": self.error,
                "fixtures": [{"name": f.name, "type": f.profile.type, "address": f.address, "levels": list(f.values),
                              "rgb": [emitted_rgb(f, h) for h in range(f.heads)],
                              "pan": [f.values[i] for i, _, s in f.chans if s["role"] == "pan"],
                              "tilt": [f.values[i] for i, _, s in f.chans if s["role"] == "tilt"],
                              "strobe": any(f.values[i] for i, _, s in f.chans if s["role"] == "strobe"),
                              "channels": [n for _, n, _ in f.chans],
                              "joints": [[n, v, round(d, 1), inv] for n, v, d, inv in f.joint_angles()]} for f in self.rig.fixtures]}


def load_show(name, _seen=None):
    p = Path(name) if Path(name).suffix else SHOWS / f"{name}.json"
    if not p.exists(): raise LookupError(f"no show {name!r} (have {[x.stem for x in SHOWS.glob('*.json')]})")
    _seen = (_seen or set()) | {p.resolve()}
    d = json.loads(p.read_text())
    for inc in d.get("include", []):
        q = Path(inc) if Path(inc).suffix else SHOWS / f"{inc}.json"
        if q.resolve() in _seen: raise ValueError(f"show include cycle at {q.name}")
        sub = load_show(str(q), _seen)
        d["cues"] = {**sub.get("cues", {}), **d.get("cues", {})}; d["songs"] = {**sub.get("songs", {}), **d.get("songs", {})}
    if "cues" not in d or not d["cues"]: raise ValueError(f"show {p.name} has no cues")
    return d


def offline(rig, show, bpm=None, song=None, step_beats=0.25):
    """Yield (beat, engine) across a whole program/song without real time (ManualClock): previews, audits, tests."""
    clk = ManualClock(bpm or show.get("songs", {}).get(song, {}).get("bpm") or show.get("bpm", 120))
    eng = Engine(rig, clock=clk)
    eng.play_song(show, song) if song else eng.play_program(show)
    end = eng.timeline_end; k = 0
    while k * step_beats < end:
        clk.value = k * step_beats; eng.frame(); yield clk.value, eng; k += 1


def preview_png(rig, show, bpm, path, fps=10, song=None):
    """Offline timeline PNG: per fixture head a colour band (what it emits) and, for movers, a 40 px trace of pan (white)
    and tilt (yellow); red ticks mark bar lines. Returns the path."""
    from graphics.png import write_png
    bpm = bpm or show.get("bpm", 120); step = bpm / 60 / fps                 # beats per column
    frames = [[f.values[:] for f in e.rig.fixtures] for _, e in offline(rig, show, bpm, song, step)]
    rows = []
    for f in rig.fixtures:
        rows += [("color", f, h) for h in range(f.heads)]
        if {"pan", "tilt"} & f.roles: rows.append(("motion", f, None))
    W = len(frames); H = sum(14 if k == "color" else 44 for k, _, _ in rows); img = bytearray(W * H * 3)
    def px(x, y, c):
        if 0 <= x < W and 0 <= y < H: img[(y * W + x) * 3:(y * W + x) * 3 + 3] = bytes(c)
    for x, vals in enumerate(frames):
        for f, v in zip(rig.fixtures, vals): f.values = v
        y = 0; b = x * step
        for kind, f, h in rows:
            if kind == "color":
                for yy in range(y, y + 12): px(x, yy, emitted_rgb(f, h))
                y += 14
            else:
                for role, col in (("pan", (255, 255, 255)), ("tilt", (255, 210, 0))):
                    for i, _, s in f.chans:
                        if s["role"] == role: px(x, y + 40 - f.values[i] * 40 // 255, col)
                if b % BAR < step: px(x, y + 41, (255, 0, 0))
                y += 44
    write_png(path, W, H, bytes(img))
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0]); sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("list")
    for name in ("play", "song", "cue", "preview"):
        x = sp.add_parser(name); x.add_argument("show"); x.add_argument("--bpm", type=float); x.add_argument("--rig", default="halloween")
        if name == "cue": x.add_argument("cue"); x.add_argument("--seconds", type=float, default=30)
        if name == "song": x.add_argument("song")
        if name == "play": x.add_argument("--loops", type=int, default=1); x.add_argument("--start")
        if name != "preview": x.add_argument("--port", default="auto"); x.add_argument("--link", action="store_true", help="follow Ableton Link")
        else: x.add_argument("--png", default="out/lighting/preview.png"); x.add_argument("--fps", type=int, default=10); x.add_argument("--song")
    a = ap.parse_args()
    if a.cmd == "list":
        print("effects:", ", ".join(sorted(EFFECTS))); print("colours:", ", ".join(PALETTE))
        for p in sorted(SHOWS.glob("*.json")):
            d = load_show(p.stem); inc = json.loads(p.read_text()).get("include", [])
            print(f"show {p.stem}: bpm {d.get('bpm', 120)}" + (f" (includes {', '.join(inc)})" if inc else "") +
                  f", cues {', '.join(d['cues'])}" + (f"; songs {', '.join(d.get('songs', {}))}" if d.get("songs") else ""))
        return
    rig, show = Rig.load(a.rig), load_show(a.show); bpm = a.bpm or show.get("bpm", 120)
    if a.cmd == "preview":
        Path(a.png).parent.mkdir(parents=True, exist_ok=True); print(preview_png(rig, show, a.bpm, a.png, a.fps, a.song)); return
    from lighting.dmx import AutoOutput
    out = MemoryOutput() if a.port == "none" else AutoOutput() if a.port == "auto" else open_output(a.port)
    clock = None
    if a.link:
        from lighting.clock import LinkClock
        clock = LinkClock(bpm=bpm)
    eng = Engine(rig, [out], bpm=bpm, clock=clock)
    try:
        if a.cmd == "cue":
            eng.cue(show, a.cue); eng.start(); time.sleep(a.seconds)
        else:
            eng.play_song(show, a.song) if a.cmd == "song" else eng.play_program(show, a.start, a.loops)
            eng.start(); last = None
            while not eng.program_done:
                if eng.cue_name != last: last = eng.cue_name; print(f"bar {eng.state()['bar']:4}: {last}", flush=True)
                time.sleep(0.1)
    except KeyboardInterrupt: pass
    finally: eng.stop()


if __name__ == "__main__":
    try: main()
    except (LookupError, ValueError) as e: print(f"show: {e}", file=sys.stderr); sys.exit(1)
