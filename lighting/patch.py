"""Patch a rig the way a DJ sets up: add lights by type and plain-word place ("2 on stands either side", "floor behind the DJ"), auto DMX addresses + DIP switches.
CLI:
  python3 lighting/patch.py new RIG [--venue mobile|club|stage|backyard|living_room] [--truss] [--room W D H] [--force]
  python3 lighting/patch.py add RIG PROFILE [--count N] [--where "on stands either side"] [--name par] [--address auto|N] [--pack]
                                  [--mode M] [--aim dancefloor|dj|up|back|crowd|ceiling] [--pos X,Y,Z] [--yaw D] [--pitch D] [--roll D]
  python3 lighting/patch.py move RIG NAME [NAME...] --where TEXT [--aim T]       python3 lighting/patch.py set RIG NAME key=value ...
  python3 lighting/patch.py remove RIG NAME [NAME...]    rename RIG OLD NEW      sheet RIG      check RIG [--json]
  python3 lighting/patch.py venues | places                                     what the venue templates / place words are
Places (any order, case-free): side  left | right | center | far left | far right | both sides / either side (alternates)
    — left/right as the CROWD sees the DJ; "stage left"/"dj left" = the DJ's own left (= crowd right), "house left" = crowd left.
  spot  truss | ceiling/overhead | stand/t-bar/tripod/totem | floor/uplight | booth/table/facade | behind/back/backdrop |
        front/downstage | over the crowd/dancefloor | wall/side wall
  height "2.5m", "8ft", "8 ft" (overrides the spot's height).   Several lights in one add spread evenly over the spot.
Coordinates written to the rig (lighting/fixtures): x = crowd's right, y = away from the crowd (booth front edge y = 0),
  z = up, metres; truss/ceiling/over-crowd lights are "hung". Fixed lights (pars, bars, strobes) get an "aim" pointed at a
  target with lighting.stage3d.aim_at: --aim, else floor-behind/backdrop -> up the back wall, front floor -> the DJ, others ->
  the dance floor. Movers keep home = centre (shows aim them).
Addresses: auto = the first free start on a 10-channel step (1, 11, 21, ... easy to dial on a display or DIP switches; lights
  wider than 10 channels take whole tens); --pack = first free channel. sheet prints DIP switch settings (switch k = 2^(k-1)).
Python: new_rig(name, venue="mobile", truss=False, room=None, force=False, path=None) -> dict · load(name) -> dict · save(d) ·
  place(where, venue, i=0, n=1) -> {"pos", "mount", "spot", "where"} · add(rig, profile, count=1, where="", ...) -> [names] ·
  move(rig, names, where, aim=None) · set_fields(rig, name, **kv) · remove(rig, names) · rename(rig, old, new) ·
  free_address(d, footprint, pack=False) -> int · dip(address) -> [switch numbers ON] · sheet(rig) -> str ·
  check(rig) -> {"ok", "errors", "warnings", "groups", "fixtures"}      VENUES, TARGET_NAMES
  set_venue(d, room=[w,d,h], deck=[w,d,h], truss=True|False, truss_height=m) -> venue dict   (caller saves)
  make_profile(name, "dimmer,red,green,blue,strobe" | "dimmer,4x(r,g,b)", type_="par", pan_deg=None, tilt_deg=None) -> (Path, dict)
      a profile typed from the light's manual (word -> role via ALIASES; unknown words = raw channels resting at 0)
Fails: LookupError unknown rig/profile/fixture/venue/target; ValueError bad place/address/overlap (the rig is validated by
  lighting.fixtures.Rig before every save, so a saved rig always loads); FileExistsError on new without --force.
Limits: one universe (512 channels); place words give a sensible first guess, not a survey: measure and use --pos for
  precise 3D/aim work; pan direction of real movers still needs a check (`dj.py point`, then invert_pan / yaw).
Depends on: lighting/fixtures, lighting/stage3d (aim_at), core/files. Used by: lighting/dj.py, tests/test_lighting_dj.py."""
import sys, re, json, math, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lighting.fixtures import RIGS, Rig, Fixture, load_profile
from core.files import write_atomic

# venue templates: room = [width, depth of the floor in front of the booth, ceiling], deck = DJ booth/riser [W, D, H]
VENUES = {
    "mobile": {"room": [10.0, 10.0, 3.0], "deck": [2.0, 1.0, 0.0], "haze": 0.3, "about": "mobile/wedding DJ: table on the floor, stands either side"},
    "club": {"room": [14.0, 12.0, 4.0], "deck": [4.0, 2.5, 0.6], "haze": 0.5, "about": "club: booth on a riser, lights rigged to the ceiling"},
    "stage": {"room": [20.0, 18.0, 7.0], "deck": [8.0, 5.0, 1.2], "haze": 0.5, "truss": True, "about": "festival/concert stage with a truss"},
    "backyard": {"room": [12.0, 12.0, 8.0], "deck": [2.4, 1.2, 0.3], "haze": 0.4, "about": "outdoor party: low deck, no ceiling (8 m for trees)"},
    "living_room": {"room": [5.0, 5.0, 2.4], "deck": [1.5, 0.8, 0.0], "haze": 0.2, "about": "house party: small room, low ceiling"},
}
SPOTS = {"truss": ("truss", "goalpost"), "ceiling": ("ceiling", "overhead", "hung", "rigged"),
         "stand": ("stand", "stands", "t-bar", "tbar", "tripod", "totem", "pole", "tree"),
         "floor": ("floor", "uplight", "uplights", "ground"), "booth": ("booth", "table", "facade", "desk", "riser"),
         "behind": ("behind", "back", "backdrop", "rear", "upstage"), "front": ("front", "downstage", "edge"),
         "crowd": ("crowd", "dancefloor", "dance floor", "audience"), "wall": ("wall", "walls")}
TARGET_NAMES = ("dancefloor", "dj", "up", "back", "crowd", "ceiling")


ALIASES = {"dim": "dimmer", "dimmer": "dimmer", "master": "dimmer", "intensity": "dimmer", "brightness": "dimmer",
           "r": "red", "red": "red", "g": "green", "green": "green", "b": "blue", "blue": "blue", "w": "white", "white": "white",
           "ww": "white", "cw": "white", "a": "amber", "amber": "amber", "uv": "uv", "purple": "uv",
           "strobe": "strobe", "str": "strobe", "shutter": "strobe", "flash": "strobe",
           "pan": "pan", "p": "pan", "panfine": "pan_fine", "pan_fine": "pan_fine", "pf": "pan_fine",
           "tilt": "tilt", "t": "tilt", "tiltfine": "tilt_fine", "tilt_fine": "tilt_fine", "tf": "tilt_fine",
           "speed": "speed", "xyspeed": "speed", "ptspeed": "speed", "mode": "program", "program": "program", "macro": "program",
           "auto": "program", "sound": "program", "function": "program", "effect": "program", "effectspeed": "program",
           "programspeed": "program", "reset": "reset", "fog": "haze", "haze": "haze", "laser": "laser"}


def make_profile(name, channels, type_="par", pan_deg=None, tilt_deg=None, model="", force=False):
    """Profile from the manual's channel list, in DMX order: "dimmer,red,green,blue,strobe,mode,speed" or with segments
    "dimmer,strobe,4x(red,green,blue)". Words map to roles via ALIASES (unknown words become raw channels resting at 0;
    "gobo", "colorwheel", "prism", "zoom" too). Writes lighting/profiles/NAME.json with status "custom"."""
    from lighting.fixtures import PROFILES, Profile
    if not re.fullmatch(r"[a-z0-9_]+", name): raise ValueError("profile name: lowercase letters, digits and _ only")
    dest = PROFILES / f"{name}.json"
    if dest.exists() and not force: raise FileExistsError(f"profile {name} exists (force=True / --force to overwrite)")
    toks, order, chans = re.findall(r"\s*(\d+)\s*x\s*\(([^)]*)\)\s*|([^,]+)", channels), [], {}
    head = -1
    def add(word, h=None):
        w = re.sub(r"[^a-z0-9_]", "", word.strip().lower())
        if not w: return
        role = ALIASES.get(w, ALIASES.get(w.replace("_", ""), "raw"))
        n = w if h is None else f"{w}{h + 1}"
        while n in chans: n += "_"
        chans[n] = {"role": role, **({"head": h} if h is not None else {}), **({"default": 0} if role == "raw" else {})}; order.append(n)
    for count, group, single in toks:
        if count:
            for k in range(int(count)):
                head += 1
                for w in group.split(","): add(w, head)
        else: add(single)
    if not order: raise ValueError("no channels given (e.g. --channels dimmer,red,green,blue)")
    d = {"name": name, "maker": "custom", "model": model or f"{len(order)}-channel {type_}", "type": type_, "status": "custom",
         "source": f"typed from the manual: {channels}", "notes": "", "channels": chans, "modes": {f"{len(order)}ch": order},
         "default_mode": f"{len(order)}ch", "optics": {"beam_deg": 25 if type_ != "moving_head" else 12, "lumens": 800, "aperture": 0.06, "body": [0.2, 0.15, 0.2]}}
    if any(c["role"] == "pan" for c in chans.values()): d["pan_deg"] = pan_deg or 540
    if any(c["role"] == "tilt" for c in chans.values()): d["tilt_deg"] = tilt_deg or 270
    Profile(d); dest.write_text(json.dumps(d, indent=1) + "\n"); return dest, d


def _venue(d):
    v = d.get("venue") or {}; room = v.get("room") or VENUES["mobile"]["room"]; deck = v.get("deck") or VENUES["mobile"]["deck"]
    return v, room, deck


def new_rig(name, venue="mobile", truss=False, room=None, force=False, path=None):
    if venue not in VENUES: raise LookupError(f"unknown venue {venue!r} (have {list(VENUES)})")
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", name): raise ValueError("rig name: letters, digits, - and _ only")
    p = Path(path) if path else RIGS / f"{name}.json"
    if p.exists() and not force: raise FileExistsError(f"rig {name} exists (use --force to replace it)")
    t = VENUES[venue]; rm = [float(x) for x in (room or t["room"])]
    if len(rm) != 3 or min(rm) <= 0: raise ValueError("room = width depth ceiling (metres, > 0)")
    W, D, H = t["deck"]
    v = {"template": venue, "room": rm, "deck": [W, D, H], "haze": t["haze"],
         "audience": [-rm[0] / 2, rm[0] / 2, -rm[1], -1.0], "eye_z": [1.0, 2.0]}
    if truss or t.get("truss"):
        v["truss"] = [round(W / 2 + 0.4, 2), round(D / 2, 2), round(min(rm[2] - 0.3, H + 2.6 if venue == "mobile" else H + 4.0), 2), 0.29]
    d = {"name": name, "notes": f"made by lighting/patch.py ({venue}: {t['about']})", "venue": v, "fixtures": [], "_path": str(p)}
    save(d); return d


def load(name):
    p = Path(name) if Path(name).suffix else RIGS / f"{name}.json"
    if not p.exists(): raise LookupError(f"no rig {name!r} (have {[x.stem for x in sorted(RIGS.glob('*.json'))]}); make one: patch.py new NAME")
    d = json.loads(p.read_text()); d.setdefault("name", p.stem); d["_path"] = str(p); return d


def save(d):
    body = {k: v for k, v in d.items() if not k.startswith("_")}
    rig = _validate(body)
    p = Path(d.get("_path") or RIGS / f"{body['name']}.json")
    write_atomic(p, json.dumps(body, indent=1) + "\n"); d["_path"] = str(p); return rig


def _validate(body):
    """Load the rig through lighting.fixtures exactly as shows will (profiles exist, addresses fit, no overlaps)."""
    fx = [Fixture(f["name"], load_profile(f["profile"]), f["address"], f.get("mode"), f.get("groups", ()), f.get("invert_pan", False),
                  f.get("invert_tilt", False), f.get("pos", (0, 0, 0)), f.get("yaw", 0.0), f.get("mount", "floor"), f.get("aim"),
                  f.get("pitch", 0.0), f.get("roll", 0.0), f.get("where", ""), f.get("pan_speed"), f.get("tilt_speed")) for f in body["fixtures"]]
    r = Rig(body.get("name", "rig"), fx); r.venue = body.get("venue", {}); return r


def _words(text): return " " + re.sub(r"[^a-z0-9.' ]+", " ", text.lower().replace("-", " ")) + " "


def place(where, venue, i=0, n=1):
    """Plain-word place -> {"pos": [x, y, z], "mount", "spot", "side"} for light i of n placed together (see module docstring)."""
    w = _words(where or ""); room = venue.get("room") or VENUES["mobile"]["room"]; W, D, H = venue.get("deck") or VENUES["mobile"]["deck"]
    rw, rd, rh = room
    side = None; far = " far " in w
    if re.search(r" (stage|dj|performer'?s?) left ", w): side = 1
    elif re.search(r" (stage|dj|performer'?s?) right ", w): side = -1
    elif re.search(r" (both|either|each) sides? | sides ", w): side = "alt"
    elif " left " in w: side = -1
    elif " right " in w: side = 1
    elif re.search(r" (center|centre|middle) ", w): side = 0
    spots = [s for s, keys in SPOTS.items() if any(f" {k} " in w for k in keys)]
    if "crowd" in spots and "front" in spots: spots.remove("front")               # "in front of the crowd" = over the floor edge
    spot = next((s for s in ("truss", "ceiling", "crowd", "wall", "stand", "floor", "booth", "behind", "front") if s in spots), None)
    if where and not spots and side is None and not re.search(r"\d", w): raise ValueError(f"no place words in {where!r} (try: {', '.join(SPOTS)})")
    if spot == "booth" and "behind" in spots: spot = "behind"                       # "behind the booth" = on the floor behind it
    spot = spot or "stand"
    tr = venue.get("truss")
    if spot == "truss" and not tr: spot = "ceiling"                                # no truss in this venue: hang from the ceiling
    behind, front = "behind" in spots, "front" in spots
    # x: spread n lights over the spot's span (or one side of it)
    span = {"truss": 2 * tr[0] * 0.9 if tr else W, "ceiling": 0.6 * rw, "crowd": 0.6 * rw, "wall": rw, "stand": W + 1.6,
            "booth": W * 0.8, "floor": W + 0.8, "behind": W + 0.8, "front": W + 0.4}[spot]
    if far: span *= 1.4
    if side == "alt" or (side is None and spot in ("stand", "wall") and n > 1) or (side is None and spot == "stand" and n == 1 and False):
        s = -1 if i % 2 == 0 else 1; k, m = i // 2, (n + 1 - (i % 2)) // 2 or 1
        x = s * span / 2 * (1.0 if m == 1 else 0.6 + 0.4 * k / max(1, m - 1))
    elif side in (-1, 1): x = side * span / 2 * (0.3 + 0.7 * (i + 0.5) / n) if n > 1 else side * span / 2 * (0.85 if spot != "stand" else 1.0)
    else: x = 0.0 if n == 1 else -span / 2 + span * (i + 0.5) / n
    if spot == "wall": x = math.copysign(rw / 2 * 0.95, x or (side if side in (-1, 1) else -1))
    # y, z, mount by spot
    y = {"truss": tr[1] if tr else 0.0, "ceiling": -1.0, "crowd": -0.4 * rd, "wall": -0.35 * rd, "stand": 0.3, "booth": 0.15,
         "floor": D + 0.4 if behind else -0.5 if front else 0.4, "behind": D + 0.4, "front": -0.6}[spot]
    if spot in ("ceiling", "truss") and behind: y = (tr[1] if tr and spot == "truss" else D)
    if spot == "ceiling" and front: y = -1.5
    z = {"truss": (tr[2] - 0.12) if tr else rh - 0.25, "ceiling": rh - 0.25, "crowd": rh - 0.25, "wall": min(2.6, rh - 0.3),
         "stand": min(2.4, rh - 0.3), "booth": H + 0.9, "floor": 0.0, "behind": 0.0, "front": 0.0}[spot]
    m = re.search(r" (\d+(?:\.\d+)?) ?(m|meters?|metres?|ft|foot|feet|') ", w)
    if m: z = float(m.group(1)) * (0.3048 if m.group(2) in ("ft", "foot", "feet", "'") else 1.0)
    if z > rh: raise ValueError(f"{where!r}: height {z:.2f} m is above the {rh} m ceiling")
    mount = "hung" if spot in ("truss", "ceiling", "crowd") else "floor"
    return {"pos": [round(x, 2), round(y, 2), round(z, 2)], "mount": mount, "spot": spot, "side": side}


def set_venue(d, room=None, deck=None, truss=None, truss_height=None):
    """Change the venue: room [w, d, ceiling] m (audience box follows), DJ booth/riser deck [w, d, h], truss on/off and height.
    Lights keep their positions; re-place them with move() if the room changed a lot."""
    v = d.setdefault("venue", {})
    if room is not None:
        room = [float(x) for x in room]
        if len(room) != 3 or not (2 <= room[0] <= 100 and 2 <= room[1] <= 100 and 1.8 <= room[2] <= 40): raise ValueError("room = width 2-100, depth 2-100, ceiling 1.8-40 m")
        v["room"] = room; v["audience"] = [-room[0] / 2, room[0] / 2, -room[1], -1.0]
    if deck is not None:
        deck = [float(x) for x in deck]
        if len(deck) != 3 or min(deck) < 0 or deck[0] > 40 or deck[1] > 20 or deck[2] > 4: raise ValueError("deck = width, depth, height (m)")
        v["deck"] = deck
    W, D, H = v.get("deck") or VENUES["mobile"]["deck"]; rh = (v.get("room") or VENUES["mobile"]["room"])[2]
    if truss is False: v.pop("truss", None)
    elif truss or truss_height is not None or ("truss" in v and deck is not None):
        old = v.get("truss"); top = float(truss_height) if truss_height is not None else (old[2] if old else min(rh - 0.3, H + 2.6))
        if not H + 1.0 <= top <= rh: raise ValueError(f"truss height must be between {H + 1.0:.1f} m and the {rh} m ceiling")
        v["truss"] = [round(W / 2 + 0.4, 2), round(D / 2, 2), round(top, 2), 0.29]
    return v


def target(name, venue, pos):
    """Named aim target -> stage point [x, y, z] for a light at pos."""
    room = venue.get("room") or VENUES["mobile"]["room"]; W, D, H = venue.get("deck") or VENUES["mobile"]["deck"]; x, y, z = pos
    pts = {"dancefloor": [x * 0.4, -0.35 * room[1], 0.0], "crowd": [x * 0.4, -0.35 * room[1], 1.2], "dj": [0.0, D / 2, H + 1.6],
           "up": [x, y, room[2] + 5], "back": [x, D + 2.5, min(room[2], 3.5)], "ceiling": [x, y - 1.5, room[2]]}
    if name not in pts: raise LookupError(f"unknown aim target {name!r} (have {list(pts)})")
    return pts[name]


def _default_aim(spot, place_words):
    if spot in ("behind",) or (spot == "floor" and " behind " in place_words): return "back"
    if spot == "floor" and " front " in place_words: return "dj"
    if spot == "floor": return "up"
    return "dancefloor"


def _aim_fixed(entry, venue, aim_name):
    """Rig 'aim' [tilt-from-up, yaw] for a fixed light so its beam hits the target (stage3d.aim_at on a temporary fixture)."""
    from lighting.stage3d import aim_at
    f = Fixture(entry["name"], load_profile(entry["profile"]), 1, entry.get("mode"), pos=entry["pos"], yaw=entry.get("yaw", 0.0),
                mount=entry.get("mount", "floor"), pitch=entry.get("pitch", 0.0), roll=entry.get("roll", 0.0))
    if f.kind in ("mover", "effect", "fog", "other") or "pan" in f.roles or "tilt" in f.roles: return None
    r = aim_at(f, target(aim_name, venue, entry["pos"]))
    if r is None: return None
    pan, tilt = r; return [round(tilt, 1), round(pan, 1)]


def free_address(d, footprint, pack=False, taken=None):
    used = sorted((f["address"], f["address"] + _footprint(f) - 1) for f in d["fixtures"]) + list(taken or [])
    def fits(a): return a + footprint - 1 <= 512 and all(a > hi or a + footprint - 1 < lo for lo, hi in used)
    step = 1 if pack else 10
    for a in range(1, 513, step):
        if fits(a): return a
    raise ValueError(f"no room for {footprint} more channels in the universe (try --pack)")


def _footprint(f): return load_profile(f["profile"]).footprint(f.get("mode"))


def dip(address):
    if not 1 <= address <= 512: raise ValueError("address must be 1..512")
    return [k + 1 for k in range(10) if address >> k & 1]


def _base(profile):
    t = profile.type; return {"moving_head": "mover", "spider": "spider", "bar": "bar", "strobe": "strobe", "laser": "laser",
                             "haze": "fog", "par": "par", "effect": "fx"}.get(t, t or "light")


def add(d, profile, count=1, where="", name=None, address="auto", pack=False, mode=None, aim=None, pos=None, yaw=None,
        pitch=None, roll=None, groups=()):
    """Add `count` lights of `profile` placed by `where` (or pos). Returns the new names; caller saves."""
    pr = load_profile(profile); pr.mode(mode)
    if not 1 <= count <= 64: raise ValueError("count must be 1..64")
    if aim is not None and aim not in TARGET_NAMES: raise LookupError(f"unknown aim target {aim!r} (have {list(TARGET_NAMES)})")
    base = name or _base(pr); have = {f["name"] for f in d["fixtures"]}; venue = d.get("venue") or {}
    k = 1; names = []; taken = []
    for i in range(count):
        if count == 1 and name and name not in have: nm = name
        else:
            while f"{base}{k}" in have: k += 1
            nm = f"{base}{k}"; k += 1
        have.add(nm)
        e = {"name": nm, "profile": pr.name}
        if mode: e["mode"] = mode
        fp = pr.footprint(mode)
        if address == "auto" or address is None: a = free_address(d, fp, pack, taken)
        else:
            a = int(address) + (i * fp if count > 1 else 0)
            if not 1 <= a <= 512 - fp + 1: raise ValueError(f"{nm}: address {a} does not fit {fp} channels")
        e["address"] = a; taken.append((a, a + fp - 1))
        if pos is not None:
            pl = {"pos": [float(c) for c in pos], "mount": "floor", "spot": "custom"}; e["where"] = where or "custom position"
        else:
            pl = place(where, venue, i, count); e["where"] = where or "on a stand (default)"
        e["pos"] = pl["pos"]
        if pl["mount"] == "hung": e["mount"] = "hung"
        for key, val in (("yaw", yaw), ("pitch", pitch), ("roll", roll)):
            if val is not None: e[key] = float(val)
        if "pitch" not in e and pr.type == "spider": e["pitch"] = 45.0 if e.get("mount") == "hung" else 70.0
        if groups: e["groups"] = list(groups)
        fa = _aim_fixed(e, venue, aim or _default_aim(pl["spot"], _words(where)))
        if fa: e["aim"] = fa
        d["fixtures"].append(e); names.append(nm)
    _validate({k2: v for k2, v in d.items() if not k2.startswith("_")})
    return names


def _get(d, name):
    for f in d["fixtures"]:
        if f["name"] == name: return f
    raise LookupError(f"no light {name!r} in rig {d['name']} (have {[f['name'] for f in d['fixtures']]})")


def move(d, names, where, aim=None):
    venue = d.get("venue") or {}
    for i, nm in enumerate(names):
        f = _get(d, nm); pl = place(where, venue, i, len(names))
        f["pos"], f["where"] = pl["pos"], where
        if pl["mount"] == "hung": f["mount"] = "hung"
        else: f.pop("mount", None)
        f.pop("aim", None); fa = _aim_fixed(f, venue, aim or _default_aim(pl["spot"], _words(where)))
        if fa: f["aim"] = fa


def set_fields(d, name, **kv):
    f = _get(d, name)
    for k, v in kv.items():
        if k in ("address",): f[k] = int(v)
        elif k in ("yaw", "pitch", "roll"): f[k] = float(v)
        elif k in ("pan_speed", "tilt_speed"):
            if v in (None, "", 0, "0"): f.pop(k, None)
            elif not 5 <= float(v) <= 2000: raise ValueError(f"{k} must be 5..2000 deg/s")
            else: f[k] = float(v)
        elif k in ("invert_pan", "invert_tilt", "pan_checked"): f[k] = str(v).lower() in ("1", "true", "yes", "on")
        elif k == "pos": f[k] = [float(c) for c in str(v).split(",")]
        elif k == "aim":
            if str(v) in TARGET_NAMES: f["aim"] = _aim_fixed(f, d.get("venue") or {}, str(v)) or f.get("aim")
            else: f["aim"] = [float(c) for c in str(v).split(",")]
        elif k == "groups": f[k] = [g for g in str(v).split(",") if g]
        elif k == "mount":
            if v not in ("floor", "hung"): raise ValueError("mount must be floor or hung")
            if v == "floor": f.pop("mount", None)
            else: f["mount"] = "hung"
        elif k in ("mode", "profile", "where"): f[k] = str(v)
        else: raise ValueError(f"cannot set {k!r} (address, mode, profile, pos, aim, yaw, pitch, roll, mount, invert_pan, invert_tilt, pan_checked, pan_speed, tilt_speed, groups, where)")


def remove(d, names):
    for nm in names: _get(d, nm)
    d["fixtures"] = [f for f in d["fixtures"] if f["name"] not in names]


def rename(d, old, new):
    if any(f["name"] == new for f in d["fixtures"]): raise ValueError(f"{new!r} already exists")
    _get(d, old)["name"] = new


def sheet(d):
    rig = _validate({k: v for k, v in d.items() if not k.startswith("_")})
    rows = [f"rig {rig.name}: {len(rig.fixtures)} lights, venue {d.get('venue', {}).get('template', 'custom')}",
            f"{'light':10} {'profile':28} {'mode':6} {'addr':>4} {'chans':>8}  {'display':7} {'DIP ON':14} where"]
    by = {f["name"]: f for f in d["fixtures"]}
    for f in sorted(rig.fixtures, key=lambda f: f.address):
        rows.append(f"{f.name:10} {f.profile.name[:28]:28} {f.mode[:6]:6} {f.address:4d} {f.address:4d}-{f.last:<3d}  "
                    f"d{f.address:03d}    {','.join(map(str, dip(f.address))):14} {by[f.name].get('where', '')}")
    rows.append("Set each light to DMX mode with the address above (display: A001/d001 style; DIP: switch k adds 2^(k-1)).")
    return "\n".join(rows)


def check(d):
    """Problems and helpful facts before a show: {"ok", "errors", "warnings", "groups", "fixtures"}."""
    errors, warns = [], []
    try: rig = _validate({k: v for k, v in d.items() if not k.startswith("_")})
    except (LookupError, ValueError) as e: return {"ok": False, "errors": [str(e)], "warnings": [], "groups": {}, "fixtures": []}
    if not rig.fixtures: errors.append("no lights yet: add some with `dj.py rig add RIG PROFILE --where ...`")
    kinds = {}
    for f in rig.fixtures:
        kinds.setdefault(f.kind, []).append(f.name)
        if f.profile.status in ("template", "guess", "ofl", "partial"):
            warns.append(f"{f.name}: profile {f.profile.name} is '{f.profile.status}' — confirm its channels with `dj.py test {d['name']} {f.name}`")
        if f.pos == (0.0, 0.0, 0.0): warns.append(f"{f.name}: no position (3D view and aiming assume the middle of the booth)")
        if f.kind == "mover" and not (f.invert_pan or any(x.get("pan_checked") for x in d["fixtures"] if x["name"] == f.name)):  # set pan_checked=true once seen right
            warns.append(f"{f.name}: mover pan direction unchecked — `dj.py point {d['name']} {f.name} dancefloor`, then set invert_pan if it swings the wrong way")
        if f.kind == "laser": warns.append(f"{f.name}: laser — shows keep it OFF unless armed; keep beams above head height and check local laser rules")
        if f.kind == "strobe": warns.append(f"{f.name}: strobe — used only when a show is made with --strobe (photosensitive guests)")
        if f.kind == "other": warns.append(f"{f.name}: no colour/dimmer/pan channels found — shows will not use it")
    if rig.fixtures and not (set(kinds) & {"wash", "mover", "effect"}): warns.append("no wash, mover or effect lights: auto shows will be very plain")
    return {"ok": not errors, "errors": errors, "warnings": warns, "kinds": kinds, "groups": rig.standard_groups(),
            "channels_used": sum(len(f.chans) for f in rig.fixtures),
            "fixtures": [{"name": f.name, "profile": f.profile.name, "kind": f.kind, "address": f.address, "last": f.last,
                          "pos": list(f.pos), "mount": f.mount, "places": f.places()} for f in rig.fixtures]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0]); sp = ap.add_subparsers(dest="cmd", required=True)
    n = sp.add_parser("new"); n.add_argument("rig"); n.add_argument("--venue", default="mobile"); n.add_argument("--truss", action="store_true")
    n.add_argument("--room", nargs=3, type=float); n.add_argument("--force", action="store_true")
    a = sp.add_parser("add"); a.add_argument("rig"); a.add_argument("profile"); a.add_argument("--count", type=int, default=1)
    a.add_argument("--where", default=""); a.add_argument("--name"); a.add_argument("--address", default="auto"); a.add_argument("--pack", action="store_true")
    a.add_argument("--mode"); a.add_argument("--aim", choices=TARGET_NAMES); a.add_argument("--pos"); a.add_argument("--group", action="append", default=[])
    for k in ("yaw", "pitch", "roll"): a.add_argument(f"--{k}", type=float)
    m = sp.add_parser("move"); m.add_argument("rig"); m.add_argument("names", nargs="+"); m.add_argument("--where", required=True); m.add_argument("--aim", choices=TARGET_NAMES)
    s = sp.add_parser("set"); s.add_argument("rig"); s.add_argument("name"); s.add_argument("kv", nargs="+")
    r = sp.add_parser("remove"); r.add_argument("rig"); r.add_argument("names", nargs="+")
    rn = sp.add_parser("rename"); rn.add_argument("rig"); rn.add_argument("old"); rn.add_argument("new")
    sh = sp.add_parser("sheet"); sh.add_argument("rig")
    c = sp.add_parser("check"); c.add_argument("rig"); c.add_argument("--json", action="store_true")
    sp.add_parser("venues"); sp.add_parser("places")
    o = ap.parse_args()
    if o.cmd == "venues":
        for k, v in VENUES.items(): print(f"{k:12} room {v['room']} booth {v['deck']}  {v['about']}")
        return
    if o.cmd == "places": print(__doc__.split("Places")[1].split("Coordinates")[0].strip()); return
    if o.cmd == "new": d = new_rig(o.rig, o.venue, o.truss, o.room, o.force); print(f"made rig {o.rig} ({o.venue}) -> {d['_path']}"); return
    d = load(o.rig)
    if o.cmd == "add":
        pos = [float(x) for x in o.pos.split(",")] if o.pos else None
        names = add(d, o.profile, o.count, o.where, o.name, o.address, o.pack, o.mode, o.aim, pos, o.yaw, o.pitch, o.roll, o.group)
        save(d); print(f"added {', '.join(names)}"); print(sheet(d))
    elif o.cmd == "move": move(d, o.names, o.where, o.aim); save(d); print(sheet(d))
    elif o.cmd == "set":
        set_fields(d, o.name, **dict(kv.split("=", 1) for kv in o.kv)); save(d); print(sheet(d))
    elif o.cmd == "remove": remove(d, o.names); save(d); print(sheet(d))
    elif o.cmd == "rename": rename(d, o.old, o.new); save(d); print(sheet(d))
    elif o.cmd == "sheet": print(sheet(d))
    else:
        r = check(d)
        if o.json: print(json.dumps(r, indent=1)); return
        print(sheet(d)); print()
        for e in r["errors"]: print("ERROR  ", e)
        for w in r["warnings"]: print("check  ", w)
        print("groups:", ", ".join(f"{g}={len(v)}" for g, v in r["groups"].items()))
        sys.exit(0 if r["ok"] else 1)


if __name__ == "__main__":
    try: main()
    except (LookupError, ValueError, FileExistsError) as e: print(f"patch: {e}", file=sys.stderr); sys.exit(1)
