"""Import any light from the Open Fixture Library (open-fixture-library.org, ~thousands of DJ/stage fixtures) as a lighting profile.
CLI:
  python3 lighting/ofl.py search chauvet slimpar            fixtures whose maker/model match every word (cached index, 7 days)
  python3 lighting/ofl.py import chauvet-dj/slimpar-pro-h-usb [--name NAME] [--force]    fetch + convert -> lighting/profiles/NAME.json
  python3 lighting/ofl.py import path/to/fixture.json       convert a downloaded OFL file (offline)
Python:
  search(words, limit=20) -> ["maker/key", ...]          fetch_ofl("maker/key") -> dict (OFL JSON, cached a day)
  convert(ofl_dict, name) -> profile dict (lighting/fixtures format: every OFL mode becomes a mode; largest mode is the default)
  import_fixture(source, name=None, force=False) -> (Path, profile dict)   source = "maker/key" or a .json path
Conversion (OFL capability type -> role): Intensity -> dimmer · ColorIntensity Red/Green/Blue/White/Amber/UV -> those colours
  (Warm/Cold White -> white) · Pan/Tilt (+fineChannelAliases -> pan_fine/tilt_fine; travel from angleStart/End or
  physical.focus, else 540/270 with a note) · PanTiltSpeed -> speed · ShutterStrobe with a Strobe range -> strobe (default =
  its Open value, strobe_range = the first steady Strobe range); shutter without strobe -> raw resting Open · colour
  WheelSlot -> wheel (slot colours from the wheel's hex colours, Open = white) · Fog/FogOutput -> haze · Maintenance named
  reset -> reset · ColorPreset/Effect/Maintenance/Generic/Speed/sound -> program (rests at 0) · everything else (gobo, prism,
  zoom, focus, CMY...) -> raw resting at its OFL defaultValue or 0. Any channel whose name contains "laser" -> laser
  (forced off unless the show arms it), whatever its capability says. Matrix/pixel fixtures: templateChannels with
  $pixelKey are expanded (repeatFor lists, pixel groups, eachPixel*), and each pixel/group becomes one head, so chases run
  along a pixel bar. OFL categories -> type: Moving Head/Scanner -> moving_head, Pixel Bar -> bar, Strobe -> strobe,
  Laser -> laser, Smoke/Hazer -> haze, Effect/Flower -> effect, else par. Status "ofl" (community data: confirm on the unit).
Fails: LookupError for an unknown fixture key (HTTP 404) or a mode channel the file does not define; ValueError for 2-D/3-D
  pixel matrices given by pixelCount (write those by hand); FileExistsError when the profile exists and force is False.
Limits: needs network for search/fetch (web.fetch, cached in out/cache/web); wheel slots split between two colours are skipped;
  only the first OFL capability decides an unusual channel's role; optics (beam angle, lumens) are estimates for the 3D view.
Depends on: web/fetch, lighting/fixtures. Used by: lighting/dj.py (fixtures import/search), tests/test_lighting_dj.py."""
import sys, re, json, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lighting.fixtures import PROFILES, Profile

RAW = "https://raw.githubusercontent.com/OpenLightingProject/open-fixture-library/master/fixtures/{}.json"
TREE = "https://api.github.com/repos/OpenLightingProject/open-fixture-library/git/trees/master?recursive=1"
COLORS = {"red": "red", "green": "green", "blue": "blue", "white": "white", "warm white": "white", "cold white": "white",
          "amber": "amber", "uv": "uv"}
TYPES = (("Moving Head", "moving_head"), ("Scanner", "moving_head"), ("Pixel Bar", "bar"), ("Laser", "laser"), ("Smoke", "haze"),
         ("Hazer", "haze"), ("Strobe", "strobe"), ("Flower", "effect"), ("Effect", "effect"), ("Blinder", "par"), ("Color Changer", "par"),
         ("Dimmer", "par"))
PROGRAM_TYPES = {"ColorPreset", "Effect", "EffectSpeed", "EffectDuration", "EffectParameter", "Maintenance", "Generic", "Speed",
                 "SoundSensitivity", "BeamPosition", "PanTiltSpeed_", "Time"}


def _num(x, unit):
    m = re.fullmatch(rf"(-?[\d.]+){unit}", str(x or "")); return float(m.group(1)) if m else None


def _hex(h):
    h = h.lstrip("#"); return [round(int(h[i:i + 2], 16) / 255, 3) for i in (0, 2, 4)]


def _caps(ch): return [ch["capability"]] if "capability" in ch else list(ch.get("capabilities", []))


def _default(ch):
    v = ch.get("defaultValue")
    if isinstance(v, int) and 0 <= v <= 255: return v
    p = _num(v, "%")
    return int(round(p / 100 * 255)) if p is not None else None


class _Doc:
    """Channel lookup for one OFL file: plain channels, fine aliases and $pixelKey templates."""
    def __init__(self, d):
        self.d, self.avail, self.tpl = d, d.get("availableChannels", {}), d.get("templateChannels", {})
        self.fine = {a: (n, k) for n, c in self.avail.items() for k, a in enumerate(c.get("fineChannelAliases", []), 1)}
        m = d.get("matrix") or {}
        if "pixelKeys" in m: self.pixels = [k for z in m["pixelKeys"] for y in z for k in y if k]
        elif "pixelCount" in m:
            dims = m["pixelCount"]
            if sorted(dims)[:2] != [1, 1]: raise ValueError(f"{d.get('name')}: 2-D/3-D pixel matrix {dims} is not supported; write the profile by hand")
            self.pixels = [str(i) for i in range(1, max(dims) + 1)]
        else: self.pixels = []
        self.groups = list(m.get("pixelGroups", {}))
        self.switch = {}                                                     # switching-channel alias -> target in the resting state
        for ch in list(self.avail.values()) + list(self.tpl.values()):
            caps = sorted(_caps(ch), key=lambda c: c.get("dmxRange", [0])[0])
            for c in caps:
                for alias, target in c.get("switchChannels", {}).items(): self.switch.setdefault(alias, target)

    def keys(self, repeat):
        if isinstance(repeat, list): return repeat
        if repeat == "eachPixelGroup": return self.groups
        if str(repeat).startswith("eachPixel"): return self.pixels
        raise ValueError(f"unknown repeatFor {repeat!r}")

    def expand(self, mode):
        """[(channel name, key or None)] in DMX order for one OFL mode."""
        out = []
        for c in mode["channels"]:
            if c is None: out.append((None, None)); continue
            if isinstance(c, str): out.append((c, self.key_of(c))); continue
            if c.get("insert") != "matrixChannels": raise ValueError(f"unsupported mode entry {c!r}")
            keys, tpls = self.keys(c["repeatFor"]), c["templateChannels"]
            pairs = [(k, t) for k in keys for t in tpls] if c.get("channelOrder", "perPixel") == "perPixel" else [(k, t) for t in tpls for k in keys]
            out += [(t.replace("$pixelKey", k), k) for k, t in pairs]
        return out

    def key_of(self, name):
        if name in self.switch: name = self.switch[name]
        if name in self.avail or name in self.fine: return None
        for t in self.tpl:
            m = re.fullmatch(re.escape(t).replace(re.escape("$pixelKey"), "(.+)"), name)
            if m: return m.group(1)
        return None

    def definition(self, name, key):
        if name in self.switch: name = self.switch[name]
        if name in self.avail: return self.avail[name], None
        if name in self.fine: return self.avail[self.fine[name][0]], self.fine[name]
        for t, c in self.tpl.items():
            if key is not None and t.replace("$pixelKey", key) == name: return c, None
            for a in c.get("fineChannelAliases", []):
                if key is not None and a.replace("$pixelKey", key) == name: return c, (t, 1)
        raise LookupError(f"OFL file has no channel {name!r}")


def _role(doc, name, ch, fine):
    """(role, extra spec fields) for one OFL channel."""
    caps = _caps(ch); dflt = _default(ch); extra = {} if dflt is None else {"default": dflt}
    if "laser" in name.lower(): return "laser", {}
    if fine:
        t = caps[0].get("type") if caps else ""
        return {"Pan": "pan_fine", "Tilt": "tilt_fine"}.get(t, "raw"), {}
    useful = [c for c in caps if c.get("type") != "NoFunction"] or caps
    def span(c): r = c.get("dmxRange", [0, 255]); return r[1] - r[0]
    main = max(useful, key=span) if useful else {}; t = main.get("type", "")
    if t == "Intensity": return "dimmer", {}
    if t == "ColorIntensity":
        col = COLORS.get(str(main.get("color", "")).lower())
        return (col, {}) if col else ("raw", {**extra, "default": extra.get("default", 0)})
    if t in ("Pan", "Tilt"): return t.lower(), {}
    if t == "PanTiltSpeed": return "speed", {}
    if any(c.get("type") == "WheelSlot" for c in caps): main, t = next(c for c in caps if c.get("type") == "WheelSlot"), "WheelSlot"
    if any(c.get("type") == "ShutterStrobe" for c in caps):
        opens = [c for c in caps if c.get("type") == "ShutterStrobe" and c.get("shutterEffect") == "Open"]
        strobes = [c for c in caps if c.get("type") == "ShutterStrobe" and c.get("shutterEffect") == "Strobe"]
        rest = dflt if dflt is not None else (opens[0]["dmxRange"][0] if opens and "dmxRange" in opens[0] else 0)
        if strobes:
            s = next((c for c in strobes if not c.get("randomTiming")), strobes[0]); lo, hi = s.get("dmxRange", [1, 255])
            return "strobe", {"default": rest, "strobe_range": [lo, hi]}
        return "raw", {"default": rest, "notes": "shutter: rests open"}
    if t == "WheelSlot":
        wname = main.get("wheel") or name; wheel = doc.d.get("wheels", {}).get(wname)
        if wheel is None and len(doc.d.get("wheels", {})) and "colo" in name.lower():
            wheel = next((w for k, w in doc.d["wheels"].items() if "colo" in k.lower()), None)
        slots = []
        if wheel and any(s.get("type") == "Color" for s in wheel["slots"]):
            for c in caps:
                n = c.get("slotNumber")
                if c.get("type") != "WheelSlot" or not isinstance(n, int) or not 1 <= n <= len(wheel["slots"]): continue
                sl = wheel["slots"][n - 1]
                if sl.get("type") == "Open": rgb = [1, 1, 1]
                elif sl.get("type") == "Color" and sl.get("colors"): rgb = _hex(sl["colors"][0])
                else: continue
                slots.append({"value": c["dmxRange"][0], "rgb": rgb, "name": sl.get("name", sl.get("type", "").lower())})
        if slots: return "wheel", {"slots": slots}
        return "raw", {"default": dflt if dflt is not None else 0}
    if t in ("Fog", "FogOutput"): return "haze", {}
    if t == "Maintenance" and "reset" in (name + " " + str(main.get("comment", ""))).lower(): return "reset", {}
    if t in PROGRAM_TYPES or "program" in name.lower() or "macro" in name.lower() or "auto" in name.lower() or "sound" in name.lower():
        return "program", {}
    return "raw", {"default": dflt if dflt is not None else 0}


def _slug(s): return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "ch"


def convert(d, name):
    """OFL fixture dict -> lighting profile dict (see module docstring)."""
    doc = _Doc(d); chans, modes, notes = {}, {}, []
    cats = d.get("categories", []); typ = next((t for c in cats for k, t in TYPES if c == k), "par")
    pan_deg = tilt_deg = 0
    focus = d.get("physical", {}).get("focus", {})
    for mode in d["modes"]:
        heads = {}; order = []
        for k, (cname, key) in enumerate(doc.expand(mode)):
            if cname is None:
                n = f"unused{k + 1}"; chans.setdefault(n, {"role": "raw", "default": 0}); order.append(n); continue
            ch, fine = doc.definition(cname, key)
            role, extra = _role(doc, cname, ch, fine)
            spec = {"role": role, "ofl": cname, **extra}
            if key is not None and key not in ("Master", "all"):
                spec["head"] = heads.setdefault(key, len(heads))
            if role in ("pan", "tilt"):
                c0 = _caps(ch)[0]; a0, a1 = _num(c0.get("angleStart"), "deg"), _num(c0.get("angleEnd"), "deg")
                span = abs(a1 - a0) if a0 is not None and a1 is not None else focus.get("panMax" if role == "pan" else "tiltMax")
                if role == "pan": pan_deg = span or pan_deg or 540
                else: tilt_deg = span or tilt_deg or 270
            n = _slug(cname)
            while n in chans and chans[n] != spec: n += "_"
            chans[n] = spec; order.append(n)
        if len(heads) == 1:                                                  # one key only (e.g. a 'Master' group) = whole fixture
            for n in order: chans[n].pop("head", None)
        modes[_slug(mode.get("shortName") or mode["name"])] = order
    if any(s["role"] == "pan" for s in chans.values()) and not focus.get("panMax") and not pan_deg: pan_deg = 540
    if (any(s["role"] == "pan" for s in chans.values()) or any(s["role"] == "tilt" for s in chans.values())) and not any(
            _num(_caps(c)[0].get("angleStart"), "deg") is not None for c in doc.avail.values() if _caps(c)):
        notes.append("pan/tilt travel not in the OFL file: assumed 540/270, check the manual")
    if any(s["role"] == "laser" for s in chans.values()): notes.append("laser channels are forced off unless a show arms the laser")
    ph = d.get("physical", {}); lens = ph.get("lens", {}).get("degreesMinMax") or [25, 25]; dims = ph.get("dimensions") or [200, 150, 200]
    lumens = ph.get("bulb", {}).get("lumens") or int((ph.get("power") or 60) * 12)
    out = {"name": name, "maker": d.get("meta", {}).get("manufacturer") or name.split("_")[1] if name.startswith("ofl_") else "",
           "model": d.get("name", name), "type": typ, "status": "ofl",
           "source": "Open Fixture Library (community data, MIT); confirm the channel chart against your light's manual / display",
           "notes": "; ".join(notes), "channels": chans, "modes": modes,
           "default_mode": max(modes, key=lambda m: len(modes[m])),
           "optics": {"beam_deg": max(lens), "lumens": lumens, "aperture": 0.06, "body": [round(x / 1000, 3) for x in dims]}}
    if pan_deg: out["pan_deg"] = pan_deg
    if tilt_deg: out["tilt_deg"] = tilt_deg
    Profile(out)                                                              # validate the result now
    return out


def fetch_ofl(key):
    from web.fetch import fetch_bytes
    import urllib.error
    key = key.strip().strip("/").removesuffix(".json")
    if key.count("/") != 1: raise ValueError(f"OFL key must be maker/fixture (e.g. chauvet-dj/slimpar-pro-h-usb), got {key!r}")
    try: b, _ = fetch_bytes(RAW.format(key), cache=86400, retries=1)
    except urllib.error.HTTPError as e:
        if e.code == 404: raise LookupError(f"no OFL fixture {key!r}: try `python3 lighting/ofl.py search WORDS`") from None
        raise
    return json.loads(b)


def search(words, limit=20):
    from web.fetch import fetch_bytes
    b, _ = fetch_bytes(TREE, cache=7 * 86400, retries=1)
    keys = [t["path"][9:-5] for t in json.loads(b).get("tree", []) if t["path"].startswith("fixtures/") and t["path"].endswith(".json") and t["path"].count("/") == 2]
    ws = [w.lower() for w in (words.split() if isinstance(words, str) else words)]
    hits = [k for k in keys if all(w in k.replace("-", " ") or w in k for w in ws)]
    return sorted(hits, key=len)[:limit]


def import_fixture(source, name=None, force=False):
    p = Path(source)
    if p.suffix == ".json" and p.exists(): d = json.loads(p.read_text()); key = p.stem
    else: d = fetch_ofl(source); key = source.strip("/").removesuffix(".json")
    name = name or "ofl_" + _slug(key.replace("/", "_"))
    dest = PROFILES / f"{name}.json"
    if dest.exists() and not force: raise FileExistsError(f"profile {name} exists (use --force to overwrite)")
    prof = convert(d, name)
    dest.write_text(json.dumps(prof, indent=1) + "\n"); return dest, prof


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0]); sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("search"); s.add_argument("words", nargs="+"); s.add_argument("--limit", type=int, default=20)
    i = sp.add_parser("import"); i.add_argument("source"); i.add_argument("--name"); i.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.cmd == "search":
        hits = search(a.words, a.limit); print("\n".join(hits) if hits else "no match (try fewer / shorter words)")
    else:
        dest, prof = import_fixture(a.source, a.name, a.force)
        print(f"saved {dest.relative_to(PROFILES.parent.parent)}  type={prof['type']}  modes: " +
              ", ".join(f"{m} ({len(c)}ch)" for m, c in prof["modes"].items()) + f"  default {prof['default_mode']}")
        if prof["notes"]: print("note:", prof["notes"])


if __name__ == "__main__":
    try: main()
    except (LookupError, ValueError, FileExistsError) as e: print(f"ofl: {e}", file=sys.stderr); sys.exit(1)
