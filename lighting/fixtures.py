"""Fixture profiles and a patched rig: drive lights by meaning (colour, dimmer, aim) instead of raw DMX channel numbers.
CLI:
  python3 lighting/fixtures.py profiles                 every profile with its status (verified / documented / partial / guess)
  python3 lighting/fixtures.py show PROFILE             channel chart of one profile
  python3 lighting/fixtures.py rig [RIG.json]           address sheet: which DMX channels each light uses + overlap check
Profiles: lighting/profiles/NAME.json —
  {"name", "maker", "model", "type": par|moving_head|spider|laser|haze|..., "status", "source", "notes",
   "pan_deg", "tilt_deg", "channels": {chname: {"role", "head"?: int, "notes"?, "ranges"?}},
   "modes": {mode: [chname, ...]}, "default_mode"}
  roles: dimmer red green blue white amber uv strobe pan pan_fine tilt tilt_fine speed program reset laser haze raw wheel cell.
  "head" marks per-cell channels (spider bars, LED-bar segments); channels without it apply to the whole fixture.
  "default": 0..255 = value the channel rests at (home): e.g. a shutter that must sit at "open", a gobo wheel at "open".
  strobe channels: "strobe_range": [lo, hi] maps strobe speed 0<x<=1 onto lo..hi (x = 0 -> the channel's default).
  role "wheel" = colour wheel, "slots": [{"value": dmx, "rgb": [r,g,b], "name"?}]; color() picks the nearest slot.
  "invert": true on a pan/tilt channel = that motor runs mirrored (e.g. a spider's Y2 bar); aim() and stage3d both honour it.
  role "cell" = one fixed-colour LED with its own dimmer, "rgb": [r,g,b] its colour (0..1); color() lights the cells
  whose colour is inside the requested one (white cells take min(r,g,b), coloured cells the remainder).
Rig: lighting/rigs/NAME.json — {"name", "venue"?: {...}, "fixtures": [{"name", "profile", "address", "mode"?, "groups"?: [..],
  "pos"?: [x, y, z] metres (x = audience right, y = away from the audience, z = up), "yaw"?: deg (0 = fixture front faces
  the audience, +ccw from above), "mount"?: "floor"|"hung", "pitch"?: deg the fixture's top tips toward its front
  (90 = beam centre aimed at the crowd), "roll"?: deg about that beam axis afterwards (90 = on its side: the tilt
  axis turns vertical, so a spider's bars sweep sideways), "aim"?: [tilt-from-up, yaw] deg for fixtures without pan/tilt,
  "invert_pan"?, "invert_tilt"?, "where"?: text}]}   venue = scene for lighting/stage3d.py (deck, backdrop, tree, haze)
Standard groups (every rig has them without listing them; a rig's own "groups" win on a name clash):
  by kind  wash|pars (fixed colour lights) · movers|beams (pan/tilt heads) · effects|spiders (spider/derby/LED-cell effects)
           strobes · lasers · fog|haze · all       (kind of one light: Fixture.kind)
  by place left|right|center (x < -0.5 m | > 0.5 m | between, as the crowd sees the stage) · floor (z < 1 m) · high (z >= 1.8 m)
  Kind and place groups list their lights left to right, so chases and spreads travel across the stage.
Python:
  p = load_profile("djxfli_beam90")             -> Profile  (.channels(mode) -> [(offset, chname, spec)], .footprint(mode))
  rig = Rig.load("halloween")                   -> Rig (validates: profiles exist, addresses fit 1..512, no overlaps)
  rig["beam1"] / rig.group("movers")            -> Fixture / [Fixture]      rig.standard_groups() -> {name: [fixture names]}
  fx.color(r, g, b, w=None, head=None)          0..1 floats; RGBW fixtures get w=min(r,g,b) extracted when w is None
  fx.dimmer(x)  fx.strobe(x)  fx.aim(pan_deg, tilt_deg, head=None)  fx.speed(x)  fx.set(chname, 0..255)  fx.home()
  emitted_rgb(fx, head=0) -> (r, g, b) 0..255   what a head emits (RGBW + fixed-colour cells × dimmer)
  fx.levels -> {absolute DMX channel: value}    fx.joint_angles() -> [(channel, dmx, degrees, inverted)] for pan/tilt
  fx.set_joint(chname, deg) (inverse of joint_angles)    fx.max_speed() -> (pan, tilt) deg/s: rig "pan_speed"/"tilt_speed",
      profile "pan_speed"/"tilt_speed", or a type default, slowed by the speed channel (lighting/motion simulates travel)    rig.render(universe) writes every fixture into a lighting.dmx.Universe
Contract: unknown profile → LookupError; bad JSON shape, overlapping/out-of-range addresses → ValueError; values outside
  0..1 / degrees outside the profile span → ValueError. A fixture without a role just ignores that call (a par has no aim).
Safety: program/reset/laser/haze channels start at 0 and only change through explicit fx.set / show actions.
Limits: pure-stdlib lookup model, no DMX I/O or timing (see lighting/dmx); only one 512-channel universe; profile status
  (verified/documented/partial/guess) is data, not checked; positions/aim are for visualisation, not validated against hardware.
Depends on: stdlib. Used by: lighting/show.py, lighting/console.py, lighting/stage3d.py, lighting/patch.py, lighting/autoshow.py."""
import sys, json, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HERE = Path(__file__).resolve().parent
PROFILES, RIGS = HERE / "profiles", HERE / "rigs"
ROLES = {"dimmer", "red", "green", "blue", "white", "amber", "uv", "strobe", "pan", "pan_fine", "tilt", "tilt_fine",
         "speed", "program", "reset", "laser", "haze", "raw", "cell", "wheel"}
KIND_GROUPS = {"wash": "wash", "pars": "wash", "movers": "mover", "beams": "mover", "effects": "effect", "spiders": "effect",
               "strobes": "strobe", "lasers": "laser", "fog": "fog", "haze": "fog"}
PLACE_GROUPS = ("left", "right", "center", "floor", "high")
COLOR_ROLES = ("red", "green", "blue", "white", "amber", "uv")


def _unit(x, what):
    if not isinstance(x, (int, float)) or not 0 <= x <= 1: raise ValueError(f"{what} must be 0..1, got {x!r}")
    return int(round(x * 255))


class Profile:
    def __init__(self, d):
        for k in ("name", "channels", "modes", "default_mode"):
            if k not in d: raise ValueError(f"profile missing {k!r}")
        for n, spec in d["channels"].items():
            if spec.get("role") not in ROLES: raise ValueError(f"profile {d['name']}: channel {n} has unknown role {spec.get('role')!r}")
            if "default" in spec and not (isinstance(spec["default"], int) and 0 <= spec["default"] <= 255):
                raise ValueError(f"profile {d['name']}: channel {n} default must be 0..255")
            if "strobe_range" in spec:
                lo_hi = spec["strobe_range"]
                if not (isinstance(lo_hi, list) and len(lo_hi) == 2 and all(isinstance(v, int) and 0 <= v <= 255 for v in lo_hi)):
                    raise ValueError(f"profile {d['name']}: channel {n} strobe_range must be [lo, hi] in 0..255")
            if spec["role"] == "wheel":
                sl = spec.get("slots")
                if not sl or not all(isinstance(x, dict) and isinstance(x.get("value"), int) and 0 <= x["value"] <= 255 and len(x.get("rgb", ())) == 3 for x in sl):
                    raise ValueError(f"profile {d['name']}: wheel channel {n} needs slots [{{'value': 0..255, 'rgb': [r,g,b]}}, ...]")
        for m, chans in d["modes"].items():
            bad = [c for c in chans if c not in d["channels"]]
            if bad: raise ValueError(f"profile {d['name']} mode {m}: undefined channels {bad}")
        if d["default_mode"] not in d["modes"]: raise ValueError(f"profile {d['name']}: default_mode not in modes")
        self.d = d; self.name = d["name"]; self.type = d.get("type", ""); self.status = d.get("status", "guess")
        self.pan_deg, self.tilt_deg = d.get("pan_deg", 0), d.get("tilt_deg", 0)

    def mode(self, mode=None):
        m = mode or self.d["default_mode"]
        if m not in self.d["modes"]: raise ValueError(f"{self.name} has no mode {m!r} (modes: {list(self.d['modes'])})")
        return m

    def channels(self, mode=None):
        return [(i, n, self.d["channels"][n]) for i, n in enumerate(self.d["modes"][self.mode(mode)])]

    def footprint(self, mode=None): return len(self.d["modes"][self.mode(mode)])

    def heads(self, mode=None):
        hs = {s["head"] for _, _, s in self.channels(mode) if "head" in s}
        return max(hs) + 1 if hs else 1


def load_profile(name):
    p = PROFILES / f"{name}.json"
    if not p.exists(): raise LookupError(f"no fixture profile {name!r} (have: {[x.stem for x in sorted(PROFILES.glob('*.json'))]})")
    return Profile(json.loads(p.read_text()))


class Fixture:
    def __init__(self, name, profile, address, mode=None, groups=(), invert_pan=False, invert_tilt=False,
                 pos=(0.0, 0.0, 0.0), yaw=0.0, mount="floor", aim=None, pitch=0.0, roll=0.0, where="", pan_speed=None, tilt_speed=None):
        if not isinstance(address, int) or address < 1: raise ValueError(f"{name}: address must be an int >= 1")
        self.name, self.profile, self.address, self.mode = name, profile, address, profile.mode(mode)
        self.groups = list(groups)
        if mount not in ("floor", "hung"): raise ValueError(f"{name}: mount must be 'floor' or 'hung'")
        if len(pos) != 3: raise ValueError(f"{name}: pos must be [x, y, z] metres")
        self.pos, self.yaw, self.mount = tuple(float(c) for c in pos), float(yaw), mount
        self.pitch, self.roll = float(pitch), float(roll)
        self.aim_fixed = tuple(aim) if aim else (0.0, 0.0)       # fixed fixtures: (tilt from up, extra yaw) degrees
        self.invert_pan, self.invert_tilt = invert_pan, invert_tilt
        self.chans = profile.channels(self.mode)
        if address + len(self.chans) - 1 > 512: raise ValueError(f"{name}: channels {address}..{address + len(self.chans) - 1} exceed 512")
        self.values = [0] * len(self.chans)
        self.roles = {s["role"] for _, _, s in self.chans}
        self.heads = profile.heads(self.mode)
        self.where = where
        self.pan_speed, self.tilt_speed = pan_speed, tilt_speed               # deg/s overrides (rig), else profile/defaults

    @property
    def last(self): return self.address + len(self.chans) - 1

    @property
    def kind(self):
        """mover | effect | wash | strobe | laser | fog | other — what a light is for, from its roles and profile type."""
        t, r = self.profile.type, self.roles
        colour = r & {"red", "green", "blue", "white", "amber", "uv", "wheel", "cell"}
        if t == "laser" or ("laser" in r and not colour): return "laser"
        if "haze" in r or t in ("haze", "fog", "fogger", "hazer"): return "fog"
        if "cell" in r or t in ("spider", "derby", "effect", "moonflower"): return "effect"
        if "pan" in r or "tilt" in r: return "mover"
        if t == "strobe": return "strobe"
        if r & {"red", "green", "blue", "white", "amber", "uv", "wheel", "dimmer"}: return "wash"
        return "other"

    def max_speed(self):
        """(pan, tilt) top speed in deg/s right now: rig override, else profile "pan_speed"/"tilt_speed", else a typical value
        for the fixture type (budget DJ heads: pan 180, tilt 135; spiders tilt 200), slowed by the fixture's speed channel
        (role "speed": 0 = fastest, 255 = ~15 % of top speed)."""
        d = self.profile.d; base = {"moving_head": (180.0, 135.0), "spider": (0.0, 200.0)}.get(self.profile.type, (180.0, 135.0))
        ps = self.pan_speed or d.get("pan_speed") or base[0]; ts = self.tilt_speed or d.get("tilt_speed") or base[1]
        sp = [self.values[i] for i, _, s in self.chans if s["role"] == "speed"]
        k = 1.0 - 0.85 * (sp[0] / 255) if sp else 1.0
        return ps * k, ts * k

    def set_joint(self, chname, deg):
        """Write a pan/tilt channel (and its fine channel) to point at `deg` from centre: the inverse of joint_angles()."""
        for i, n, s in self.chans:
            if n != chname: continue
            span = self.profile.pan_deg if s["role"] == "pan" else self.profile.tilt_deg
            inv = self.invert_pan if s["role"] == "pan" else self.invert_tilt
            frac = max(0.0, min(1.0, 0.5 + (deg * (-1 if inv else 1)) / span)); v16 = int(round(frac * 65535))
            if s.get("invert"): v16 = 65535 - v16
            fine = [j for j, _, s2 in self.chans if s2["role"] == s["role"] + "_fine" and s2.get("head") == s.get("head")]
            self.values[i] = v16 >> 8 if fine else int(round(v16 / 257))
            for j in fine: self.values[j] = v16 & 0xFF
            return self
        raise LookupError(f"{self.name} has no channel {chname!r}")

    def places(self):
        """Place groups this light belongs to (see Standard groups)."""
        x, _, z = self.pos; out = ["left" if x < -0.5 else "right" if x > 0.5 else "center"]
        if z < 1.0: out.append("floor")
        if z >= 1.8: out.append("high")
        return out

    def _idx(self, role, head=None):
        return [i for i, _, s in self.chans if s["role"] == role and (head is None or s.get("head", head) == head)]

    def set(self, chname, value):
        if not isinstance(value, int) or not 0 <= value <= 255: raise ValueError(f"{self.name}.{chname}: value {value!r} not 0..255")
        for i, n, _ in self.chans:
            if n == chname: self.values[i] = value; return self
        raise LookupError(f"{self.name} ({self.profile.name}) has no channel {chname!r}")

    def _role(self, role, value, head=None):
        for i in self._idx(role, head): self.values[i] = value
        return self

    def dimmer(self, x):
        v = _unit(x, "dimmer")
        if "dimmer" in self.roles: return self._role("dimmer", v)
        return self                                            # RGB-only fixtures: scale in color() via _master
    def strobe(self, x):
        v = _unit(x, "strobe")
        for i, _, s in self.chans:
            if s["role"] != "strobe": continue
            if v == 0: self.values[i] = s.get("default", 0)
            elif "strobe_range" in s: lo, hi = s["strobe_range"]; self.values[i] = int(round(lo + (hi - lo) * v / 255))
            else: self.values[i] = v
        return self
    def speed(self, x): return self._role("speed", _unit(x, "speed"))

    def color(self, r, g, b, w=None, head=None):
        r, g, b = (_unit(v, "colour") / 255 for v in (r, g, b))
        if "cell" in self.roles:
            cells = [(i, s["rgb"]) for i, _, s in self.chans if s["role"] == "cell" and (head is None or s.get("head") == head)]
            wc = min(r, g, b) if any(min(c) > 0 for _, c in cells) else 0.0
            for i, c in cells:
                if min(c) > 0: lvl = wc
                else: lvl = min(x - wc for x, ci in zip((r, g, b), c) if ci > 0) / 1.0
                self.values[i] = int(round(max(0.0, min(1.0, lvl)) * 255))
            return self
        if "wheel" in self.roles and max(r, g, b) > 0:
            for i, _, s in self.chans:
                if s["role"] != "wheel" or (head is not None and s.get("head", head) != head): continue
                m = max(r, g, b); want = (r / m, g / m, b / m)
                self.values[i] = min(s["slots"], key=lambda sl: sum((a - c) ** 2 for a, c in zip(want, sl["rgb"])))["value"]
        if "white" in self.roles and w is None:
            w = min(r, g, b); r, g, b = r - w, g - w, b - w
        for role, v in (("red", r), ("green", g), ("blue", b), ("white", w or 0.0)):
            self._role(role, _unit(v, role), head)
        return self

    def aim(self, pan_deg=None, tilt_deg=None, head=None):
        """Degrees from the centre of travel (pan ±pan_deg/2, tilt ±tilt_deg/2)."""
        for role, deg, span, inv in (("pan", pan_deg, self.profile.pan_deg, self.invert_pan), ("tilt", tilt_deg, self.profile.tilt_deg, self.invert_tilt)):
            if deg is None or role not in self.roles: continue
            if not span: raise ValueError(f"{self.profile.name} has no {role}_deg")
            if abs(deg) > span / 2 + 1e-9: raise ValueError(f"{self.name}: {role} {deg}° outside ±{span / 2}°")
            frac = 0.5 + (-deg if inv else deg) / span
            for r2, shift in ((role, 8), (role + "_fine", 0)):
                for i in self._idx(r2, head):
                    v16 = int(round((1 - frac if self.chans[i][2].get("invert") else frac) * 65535))
                    self.values[i] = (v16 >> shift) & 0xFF
        return self

    def home(self):
        """All zero (programs/reset/laser/haze off) except channels with a profile "default", dimmer 0, head centred."""
        self.values = [s.get("default", 0) for _, _, s in self.chans]
        if "pan" in self.roles or "tilt" in self.roles: self.aim(0 if "pan" in self.roles else None, 0 if "tilt" in self.roles else None)
        return self

    @property
    def levels(self): return {self.address + i: v for i, v in enumerate(self.values)}

    def joint_angles(self):
        """What the current DMX values command, in degrees from centre: [(channel, dmx, deg, inverted)] for every
        pan/tilt channel (fine channels folded in; per-channel "invert" and rig invert_pan/invert_tilt honoured)."""
        out = []; fine = {(s["role"][:-5], s.get("head")): self.values[i] for i, _, s in self.chans if s["role"] in ("pan_fine", "tilt_fine")}
        for i, n, s in self.chans:
            if s["role"] not in ("pan", "tilt"): continue
            key = (s["role"], s.get("head"))
            v16 = self.values[i] * 256 + fine[key] if key in fine else self.values[i] * 257   # 8-bit: 0..255 → 0..65535 exactly
            if s.get("invert"): v16 = 65535 - v16
            span = self.profile.pan_deg if s["role"] == "pan" else self.profile.tilt_deg
            inv = self.invert_pan if s["role"] == "pan" else self.invert_tilt
            out.append((n, self.values[i], (v16 / 65535 - 0.5) * span * (-1 if inv else 1), bool(s.get("invert"))))
        return out


class Rig:
    def __init__(self, name, fixtures):
        self.name, self.fixtures = name, list(fixtures)
        names = [f.name for f in self.fixtures]
        if len(set(names)) != len(names): raise ValueError(f"duplicate fixture names in rig {name}")
        spans = sorted((f.address, f.last, f.name) for f in self.fixtures)
        for (a0, a1, n0), (b0, b1, n1) in zip(spans, spans[1:]):
            if b0 <= a1: raise ValueError(f"rig {name}: {n0} ({a0}-{a1}) overlaps {n1} ({b0}-{b1})")
        self.by_name = {f.name: f for f in self.fixtures}
        self.venue = {}

    @classmethod
    def load(cls, path_or_name):
        p = Path(path_or_name)
        if not p.suffix: p = RIGS / f"{path_or_name}.json"
        d = json.loads(p.read_text())
        fx = [Fixture(f["name"], load_profile(f["profile"]), f["address"], f.get("mode"), f.get("groups", ()),
                      f.get("invert_pan", False), f.get("invert_tilt", False), f.get("pos", (0.0, 0.0, 0.0)), f.get("yaw", 0.0),
                      f.get("mount", "floor"), f.get("aim"), f.get("pitch", 0.0), f.get("roll", 0.0), f.get("where", ""),
                      f.get("pan_speed"), f.get("tilt_speed"))
              for f in d["fixtures"]]
        rig = cls(d.get("name", p.stem), fx); rig.venue = d.get("venue", {}); return rig

    def __getitem__(self, name):
        if name not in self.by_name: raise LookupError(f"no fixture {name!r} in rig {self.name} (have {list(self.by_name)})")
        return self.by_name[name]

    def group(self, g):
        if g == "all": return list(self.fixtures)
        out = [f for f in self.fixtures if g in f.groups or f.name == g or f.profile.type == g]
        if out: return out                                                   # the rig's own groups keep their order
        if g in KIND_GROUPS: out = [f for f in self.fixtures if f.kind == KIND_GROUPS[g]]
        elif g in PLACE_GROUPS: out = [f for f in self.fixtures if g in f.places()]
        if not out: raise LookupError(f"no fixtures in group {g!r}")
        return sorted(out, key=lambda f: (f.pos[0], f.pos[1]))

    def groups(self): return sorted({g for f in self.fixtures for g in f.groups})

    def standard_groups(self):
        """{group: [fixture names]} for the standard kind/place groups that have lights in this rig."""
        out = {}
        for g in list(KIND_GROUPS) + list(PLACE_GROUPS):
            try: out[g] = [f.name for f in self.group(g)]
            except LookupError: pass
        return out

    def home(self):
        for f in self.fixtures: f.home()

    def render(self, universe):
        for f in self.fixtures: universe.set_many(f.address, f.values)

    def sheet(self):
        rows = [f"{'fixture':10} {'profile':22} {'address':>7} {'channels':>9}  status"]
        for f in sorted(self.fixtures, key=lambda f: f.address):
            rows.append(f"{f.name:10} {f.profile.name:22} {f.address:7d} {f.address:4d}-{f.last:<4d}  {f.profile.status}")
        return "\n".join(rows)


def emitted_rgb(f, head=0):
    """Approximate colour a fixture head emits (0..255 RGB) from its DMX values: RGBW channels and fixed-colour cells,
    scaled by its dimmer (RGB-only fixtures dim via colour). Used by previews, the console view and lighting/stage3d."""
    v = dict.fromkeys(("red", "green", "blue", "white", "dimmer"), 0)
    for i, n, s in f.chans:
        if s["role"] == "wheel" and not f.roles & {"red", "green", "blue", "white"}:
            sl = next((x for x in sorted(s["slots"], key=lambda x: x["value"], reverse=True) if x["value"] <= f.values[i]), s["slots"][0])
            for c, k in zip(sl["rgb"], ("red", "green", "blue")): v[k] = int(c * 255)
        if s["role"] in v and (s.get("head") is None or s.get("head") == head): v[s["role"]] = f.values[i]
        if s["role"] == "cell" and s.get("head", 0) == head:
            for c, k in zip(s["rgb"], ("red", "green", "blue")): v[k] = min(255, v[k] + int(c * f.values[i]))
    d = v["dimmer"] / 255 if "dimmer" in f.roles else 1.0
    return tuple(int(min(255, (v[c] + v["white"]) * d)) for c in ("red", "green", "blue"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0]); sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("profiles"); s = sp.add_parser("show"); s.add_argument("profile")
    r = sp.add_parser("rig"); r.add_argument("rig", nargs="?", default="halloween")
    a = ap.parse_args()
    if a.cmd == "profiles":
        for p in sorted(PROFILES.glob("*.json")):
            pr = load_profile(p.stem); print(f"{pr.name:22} {pr.status:10} {pr.type:12} {pr.d.get('model', '')}")
    elif a.cmd == "show":
        pr = load_profile(a.profile); print(f"{pr.name} — {pr.d.get('model', '')}  [{pr.status}] {pr.d.get('source', '')}")
        for m in pr.d["modes"]:
            print(f"mode {m}:")
            for i, n, s in pr.channels(m): print(f"  ch{i + 1:<3} {n:14} {s['role']:9} {'head ' + str(s['head']) if 'head' in s else '':7} {s.get('notes', '')}")
    else: print(Rig.load(a.rig).sheet())


if __name__ == "__main__":
    try: main()
    except (LookupError, ValueError) as e: print(f"fixtures: {e}", file=sys.stderr); sys.exit(1)
