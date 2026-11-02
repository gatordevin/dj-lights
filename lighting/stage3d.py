"""3D stage visualiser: the rig's real DMX levels → physically placed beams, ray traced on the GPU with volumetric haze.
Every fixture's DMX values become emitters (position, direction from pan/tilt/bar tilt, colour, beam angle, lumens); a GLSL
fragment shader ray-marches the venue (ground, deck, truss, hedges, trees) and every fixture part as an oriented box
(moving-head base/yoke/head/lens, spider body + two independently tilting bars + 8 lit LED cells, par cans), lights the surfaces with every
spot cone and integrates single scattering through haze along each beam. It shows what the lights would do, so it
works with or without hardware.
CLI:
  python3 lighting/stage3d.py [--url http://127.0.0.1:8779] [--rig halloween] [--show halloween_demo] [--play]
                              [--clock link|internal] [--audio APP]
      live window (run_in_background). Follows the running console (lighting.console.ConsoleClient); if none answers it
      starts one in this process (console.start_console: web console on the same port, DMX to the dongle when plugged
      in, simulation otherwise; Link clock by default).
      mouse: drag orbit · right/shift-drag pan · scroll zoom · arrows (or W A D) walk · Q/E down/up.  keys: 1-9 cues · P play program · space pause/resume · , . step ∓1/16 beat · - = step ∓1 beat (pauses) · T tap tempo
      B blackout · S strobe (hold) · H haze burst · [ ] haze density · V next view · C snapshot → out/lighting/ · Esc quit
  python3 lighting/stage3d.py audit [--show halloween_demo] [--song NAME]   per cue: % of time a beam crosses the audience at eye height
  python3 lighting/stage3d.py snap [--cue chase] [--beat 2.5] [--view foh|wide|top|side|dj|back] [-o PNG] [--live]
      one offscreen frame (no window).  --debug-frames N (window): save the first N frames the window draws (verification): a cue at a given beat from a private simulated engine, or --live = the running console
Python:
  emitters(rig, t=0.0) -> [Emitter(name, pos, dir, rgb (linear 0..1 × lumens scale), tan_half, aperture, length)]
      from the rig's current fixture values (Engine.frame / show.render fill them; or rig levels from console state)
  kinematics(rig, t) -> (parts, emitters)   articulated geometry (base/yoke/head, spider body/bars/LED cells, par cans)
      and the beams from the same model; Part(name, c, ax, ay, az, half, emit) = oriented box
  inspector(rig) -> ["beam1   pan 132 (+11.3°)  tilt 201 (+62.1°)", ...]   joint table (Fixture.joint_angles)
  load_levels(rig, state) — copy console /state fixture levels into the rig
  aim_at(fixture, [x, y, z]) -> (pan_deg, tilt_deg) | None   point a light at a stage point (inverse of the mounting model)
  eye_hazards(emitters, venue) -> [(name, metres)]  narrow beams crossing the audience box at eye height (venue "audience", "eye_z")
  audit_program(rig, show) -> {cue: {"eye_fraction", "fixtures"}}  offline safety pass over a whole program
  StageRenderer(rig).draw(w, h, cam_eye, cam_target, t, haze=None)   needs a current GL context
  snapshot(rig, path, view="foh", w=1280, h=720, t=0.0) -> path
  VIEWS {name: (eye, target)}
Model: x = audience right, y = away from the audience, z = up (metres). Moving heads: tilt DMX centre = straight up
  (true on the DJXFLI beams), pan rotates about the base; spider bars tilt about the fixture's left-right axis, LEDs fan
  along each bar; pars use the rig's fixed aim. Strobe channels flicker the emitter at the channel's rate.
Limits: single scattering, no extinction, no shadows from fixture bodies; lumens are estimates (profile "optics"), so
  brightness is relative; fixture parts are boxes (≤64 parts); max 32 emitters. Pan direction sense
  is unverified on hardware (flip with invert_pan in the rig).
Fails: ValueError if a fixture has more than 12 parts; LookupError for an unknown view; the CLI prints `stage3d: ...` and exits 1 on those
  (Esc quits the window via SystemExit). Window/snap need a GL context (macOS); other errors propagate.
Depends on: lighting/fixtures, lighting/show, lighting/clock, lighting/console (ConsoleClient, start_console), geometry/vec + frames, graphics/gl, gui/glapp. Used by: the owner to plan aim."""
import sys, math, time, functools, argparse
from dataclasses import dataclass
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from geometry import vec as V
from geometry.frames import exp_so3, mv
from lighting.fixtures import Rig, emitted_rgb

MAXL, MAXP = 32, 64
OUT = Path(__file__).resolve().parents[1] / "out" / "lighting"
VIEWS = {"foh": ((0.0, -8.5, 1.7), (0.0, 1.0, 3.0)), "wide": ((-6.5, -8.0, 3.5), (0.0, 1.0, 3.0)),
         "top": ((0.0, -1.0, 18.0), (0.0, 0.5, 0.0)), "side": ((-8.5, 0.0, 2.5), (0.0, 0.6, 2.5)),
         "dj": ((0.0, 0.9, 2.9), (0.0, -6.0, 2.0)), "crowd": ((1.5, -3.0, 1.6), (0.0, 0.6, 4.5)), "truss": ((0.0, -2.6, 3.2), (0.0, 0.65, 2.85)), "photo": ((0.0, -9.5, 1.6), (0.0, 1.0, 3.2))}


@dataclass
class Emitter:
    name: str
    pos: tuple
    dir: tuple
    rgb: tuple
    tan_half: float
    aperture: float
    length: float


def _sum(*vs): return functools.reduce(V.add, vs)
def _rot(v, axis, ang): return mv(exp_so3(V.scale(axis, ang)), v)          # rotate v about a unit axis (geometry.frames)


def _frame(f):
    """Fixture axes in the world: up (beam centre at tilt 0), fwd (its front), right (its tilt axis). Mounting order:
    pitch about right (+ tips the top toward the front: 90 = beam centre aimed at the crowd), then roll about that beam
    axis (90 = on its side: tilt axis vertical, so tilts sweep sideways), then yaw about z. mount "hung" flips first."""
    up, fwd, right = (0.0, 0.0, 1.0), (0.0, -1.0, 0.0), (1.0, 0.0, 0.0)
    if f.mount == "hung": up, right = (0.0, 0.0, -1.0), (-1.0, 0.0, 0.0)
    p = math.radians(getattr(f, "pitch", 0.0)); up, fwd = _rot(up, right, p), _rot(fwd, right, p)
    r = math.radians(getattr(f, "roll", 0.0)); fwd, right = _rot(fwd, up, r), _rot(right, up, r)
    yw = math.radians(f.yaw); z = (0.0, 0.0, 1.0)
    return _rot(up, z, yw), _rot(fwd, z, yw), _rot(right, z, yw)


def _dir(up, fwd, right, pan_rad, tilt_rad):
    f2 = _sum(V.scale(fwd, math.cos(pan_rad)), V.scale(right, math.sin(pan_rad)))
    return V.norm(_sum(V.scale(up, math.cos(tilt_rad)), V.scale(f2, math.sin(tilt_rad))))


def aim_at(f, target):
    """Inverse of the mounting model: (pan_deg, tilt_deg) that point fixture f's beam at the stage point target [x, y, z]
    (metres, rig frame), or None when no pan/tilt inside the fixture's travel reaches it. Movers: pan/tilt as Fixture.aim
    takes them; spiders (tilt only): the tilt that best faces the point (pan 0); fixed lights: the rig "aim" pair
    [tilt-from-up, yaw]. Uses the same _frame/_dir as kinematics(), so a light aimed this way is drawn hitting the point."""
    up, fwd, right = _frame(f); piv = _sum(f.pos, V.scale(up, 0.15))
    d = tuple(t - p for t, p in zip(target, piv)); n = math.sqrt(sum(c * c for c in d))
    if n < 1e-6: return None
    d = tuple(c / n for c in d); du, df, dr = (sum(a * b for a, b in zip(d, ax)) for ax in (up, fwd, right))
    if "cell" in f.roles or ("tilt" in f.roles and "pan" not in f.roles):
        t = math.degrees(math.atan2(df, du)); T = f.profile.tilt_deg / 2
        return (0.0, t) if not T or abs(t) <= T + 1e-9 else None
    tilt = math.degrees(math.acos(max(-1.0, min(1.0, du)))); pan = math.degrees(math.atan2(dr, df))
    if not ("pan" in f.roles or "tilt" in f.roles): return (pan, tilt)
    P, T = f.profile.pan_deg / 2, f.profile.tilt_deg / 2
    cands = []
    for p0, t0 in ((pan, tilt), (pan + 180, -tilt), (pan - 180, -tilt)):
        for k in (-360, 0, 360):
            p1 = p0 + k
            if (abs(p1) <= P + 1e-9 or (not P and abs(p1) < 1e-9)) and abs(t0) <= T + 1e-9: cands.append((abs(p1) + abs(t0), p1, t0))
    if not cands: return None
    _, p1, t0 = min(cands); return (round(p1, 3), round(t0, 3))


def _ray_box(o, d, lo, hi):
    t0, t1 = 0.0, 1e9
    for i in range(3):
        if abs(d[i]) < 1e-12:
            if not lo[i] <= o[i] <= hi[i]: return None
            continue
        a, b = (lo[i] - o[i]) / d[i], (hi[i] - o[i]) / d[i]
        if a > b: a, b = b, a
        t0, t1 = max(t0, a), min(t1, b)
        if t0 > t1: return None
    return t0 if t0 > 1e-4 else None


def _ray_sphere(o, d, c, r):
    oc = tuple(p - q for p, q in zip(o, c)); b = sum(x * y for x, y in zip(oc, d)); cc = sum(x * x for x in oc) - r * r
    disc = b * b - cc
    if disc <= 0: return None
    t = -b - math.sqrt(disc)
    return t if t > 1e-4 else None


def venue_solids(venue):
    """Opaque primitives of the venue (shared by beam_length and the shader's scene): [("box", lo, hi) | ("sphere", c, r)]."""
    out = []
    W, D, H = venue.get("deck", (0, 0, 0))
    if H: out.append(("box", (-W / 2, 0.0, 0.0), (W / 2, D, H)))
    sw, sd, sh = venue.get("step", (0, 0, 0))
    if sh: out.append(("box", (-sw / 2, -sd, 0.0), (sw / 2, 0.0, sh)))
    tx, ty, top, sz = venue.get("truss", (0, 0, 0, 0))
    if top:
        for x in (-tx, tx): out.append(("box", (x - sz / 2, ty - sz / 2, H), (x + sz / 2, ty + sz / 2, top)))
        out.append(("box", (-tx - 0.2, ty - 0.12, top - 0.06), (tx + 0.2, ty + 0.12, top)))
    hb = venue.get("hedge_back")
    if hb: out.append(("box", (-hb[2] / 2, hb[0], 0.0), (hb[2] / 2, hb[0] + 1.2, hb[1])))
    for x, y, cz, r in venue.get("trees", []): out.append(("sphere", (x, y, cz), r))
    return out


def beam_length(pos, d, venue):
    """Distance along the beam to the first opaque surface (ground, deck, truss, hedge, tree canopies), else 40 m."""
    best = 40.0
    if d[2] < -1e-6: best = min(best, -pos[2] / d[2])
    for kind, a, b in venue_solids(venue):
        t = _ray_box(pos, d, a, b) if kind == "box" else _ray_sphere(pos, d, a, b)
        if t is not None: best = min(best, t)
    return max(0.05, best)


def _strobe_on(f, t):
    for i, _, s in f.chans:
        if s["role"] == "strobe":
            v = f.values[i]
            if v >= 10:
                hz = 1 + 19 * (v - 10) / 245
                return (t * hz) % 1.0 < 0.35
    return True


@dataclass
class Part:
    """One rigid piece of a fixture for drawing: oriented box (centre, unit axes ax/ay/az, half sizes) + emissive RGB."""
    name: str
    c: tuple
    ax: tuple
    ay: tuple
    az: tuple
    half: tuple
    emit: tuple = (0.0, 0.0, 0.0)


def _angles(f):
    """Joint angles (radians) from Fixture.joint_angles(): {"pan": a, "tilt": {head: a}}."""
    out = {"pan": 0.0, "tilt": {}}; spec = {n: s for _, n, s in f.chans}
    for n, _, deg, _ in f.joint_angles():
        s = spec[n]
        if s["role"] == "pan": out["pan"] = math.radians(deg)
        else: out["tilt"][s.get("head", 0)] = math.radians(deg)
    return out


def inspector(rig):
    """Human-readable joint table for movers: ["beam1   pan 132 (+11.3°)  tilt 201 (+62.1°)", ...] ("*" = inverted motor)."""
    rows = []
    for f in rig.fixtures:
        j = f.joint_angles()
        if j: rows.append(f"{f.name:8} " + "  ".join(f"{n} {v:3d} ({d:+6.1f}°){'*' if inv else ''}" for n, v, d, inv in j))
    return rows


def kinematics(rig, t=0.0):
    """The rig's articulated geometry for its current DMX values → (parts, emitters). One model for both, so what is
    drawn (yoke, head, spider bars, LED cells) is exactly where each beam comes from.
      moving head: base → yoke turns by pan about the fixture's up → head tilts about the yoke's right axis; lens on the head
      spider: body → two parallel bars side by side (offset along the fixture's front axis; Y1 bar tinted orange, Y2 blue), each tilting about its own long
              axis (= the fixture's right axis); 4 LED cells along each bar, the second bar mirrored (cell order reversed)
      par / fixed: a can aimed by the rig's "aim"."""
    parts, ems = [], []; venue = rig.venue
    for f in rig.fixtures:
        o = f.profile.d.get("optics", {}); lum = o.get("lumens", 0)
        th, ap = math.tan(math.radians(o.get("beam_deg", 10) / 2)), o.get("aperture", 0.05)
        on = 1.0 if _strobe_on(f, t) else 0.0; up, fwd, right = _frame(f); A = _angles(f)
        dim = 1.0
        for i, _, s in f.chans:
            if s["role"] == "dimmer": dim = f.values[i] / 255
        P = lambda c, ax, ay, az, half, emit=(0.0, 0.0, 0.0), n="": parts.append(Part(f"{f.name}{n}", c, ax, ay, az, half, emit))
        if "cell" in f.roles:
            P(_sum(f.pos, V.scale(up, 0.05)), right, fwd, up, (0.14, 0.07, 0.05), n=".body")
            pitch, off, fanr = o.get("cell_pitch", 0.035), o.get("bar_offset", 0.045), math.radians(o.get("cell_fan_deg", 6))
            for h in range(f.heads):
                a = A["tilt"].get(h, 0.0)
                bu = _sum(V.scale(up, math.cos(a)), V.scale(fwd, math.sin(a))); bf = _sum(V.scale(up, -math.sin(a)), V.scale(fwd, math.cos(a)))
                piv = _sum(f.pos, V.scale(up, 0.15), V.scale(fwd, off * (1 if h == 0 else -1)))
                P(piv, right, bf, bu, (0.075, 0.028, 0.028), emit=((0.09, 0.04, 0.0) if h == 0 else (0.0, 0.03, 0.09)), n=f".bar{h + 1}")   # Y1 orange, Y2 blue tint
                cells = [(i, n, s) for i, n, s in f.chans if s["role"] == "cell" and s.get("head", 0) == h]
                for j, (i, n, s) in enumerate(cells):
                    sgn = 1 if h == 0 else -1; x = (j - (len(cells) - 1) / 2) * pitch * sgn
                    lvl = f.values[i] / 255 * dim * on; col = tuple(c * lvl for c in s["rgb"])
                    cc = _sum(piv, V.scale(right, x), V.scale(bu, 0.03))
                    P(cc, right, bf, bu, (0.012, 0.012, 0.004), emit=col, n=f".{n}")
                    fan = (j - (len(cells) - 1) / 2) * fanr * sgn
                    d = V.norm(_sum(V.scale(bu, math.cos(fan)), V.scale(right, math.sin(fan))))
                    rgb = tuple(c * lum / 1000 for c in col)
                    if max(rgb) > 1e-4: ems.append(Emitter(f"{f.name}.{n}", cc, d, rgb, th, ap, beam_length(cc, d, venue)))
            continue
        moving = "pan" in f.roles or "tilt" in f.roles
        if moving:
            pa = A["pan"]; yf = _sum(V.scale(fwd, math.cos(pa)), V.scale(right, math.sin(pa))); yr = _sum(V.scale(right, math.cos(pa)), V.scale(fwd, -math.sin(pa)))
            ta = A["tilt"].get(0, 0.0); d = _sum(V.scale(up, math.cos(ta)), V.scale(yf, math.sin(ta))); hf = _sum(V.scale(up, -math.sin(ta)), V.scale(yf, math.cos(ta)))
            P(_sum(f.pos, V.scale(up, 0.04)), right, fwd, up, (0.12, 0.10, 0.04), n=".base")
            for sx in (-1, 1): P(_sum(f.pos, V.scale(up, 0.16), V.scale(yr, 0.095 * sx)), yr, yf, up, (0.012, 0.045, 0.08), n=".yoke")
            piv = _sum(f.pos, V.scale(up, 0.2))
            P(piv, yr, hf, d, (0.075, 0.07, 0.1), n=".head")
            lens = _sum(piv, V.scale(d, 0.105))
        else:
            d = _dir(up, fwd, right, math.radians(f.aim_fixed[1]), math.radians(f.aim_fixed[0]))
            bx, by, _ = V.basis(d); piv = _sum(f.pos, V.scale(up, 0.1))
            P(piv, bx, by, d, (0.085, 0.085, 0.09), n=".can"); lens = _sum(piv, V.scale(d, 0.095)); bx2, by2 = bx, by
        c = emitted_rgb(f, 0); col = tuple(x / 255 * on for x in c)
        if moving: P(lens, yr, hf, d, (0.05, 0.05, 0.004), emit=col, n=".lens")
        else: P(lens, bx2, by2, d, (0.07, 0.07, 0.004), emit=col, n=".lens")
        rgb = tuple(x * lum / 1000 for x in col)
        if lum and max(rgb) > 1e-4: ems.append(Emitter(f.name, lens, d, rgb, th, ap, beam_length(lens, d, venue)))
    return parts, ems[:MAXL]


def emitters(rig, t=0.0):
    return kinematics(rig, t)[1]


def eye_hazards(ems, venue, min_lumens=0.05):
    """Emitters whose beam passes through the audience box at eye height. venue["audience"] = [x0, x1, y0, y1]
    (default: 16 m wide, from 1 m in front of the stage out to 14 m), venue["eye_z"] = [z0, z1] (default 1.0-2.0 m).
    Returns [(emitter name, distance along beam to the first eye-height crossing m)]. Bright narrow beams only
    (rgb sum ≥ min_lumens, tan_half < 0.12): wide washes are not an eye hazard."""
    x0, x1, y0, y1 = venue.get("audience", (-8.0, 8.0, -14.0, -1.0)); z0, z1 = venue.get("eye_z", (1.0, 2.0))
    out = []
    for e in ems:
        if e.tan_half >= 0.12 or sum(e.rgb) < min_lumens: continue
        lo, hi = 0.0, e.length                                         # slab intersection of the beam segment with the box
        for p, d, a, b in ((e.pos[0], e.dir[0], x0, x1), (e.pos[1], e.dir[1], y0, y1), (e.pos[2], e.dir[2], z0, z1)):
            if abs(d) < 1e-9:
                if not a <= p <= b: lo, hi = 1, 0; break
                continue
            t1, t2 = sorted(((a - p) / d, (b - p) / d)); lo, hi = max(lo, t1), min(hi, t2)
        if lo < hi: out.append((e.name, round(lo, 2)))
    return out


def audit_program(rig, show, bpm=None, step_beats=0.25, song=None):
    """Run a show's program (or a song) offline and report, per cue, the fraction of time any beam crosses the audience
    at eye height: {cue: {"eye_fraction", "fixtures"}}."""
    from lighting.show import offline
    per = {}
    for b, eng in offline(rig, show, bpm, song, step_beats):
        hz = eye_hazards(emitters(rig, b * 60 / eng.bpm), rig.venue)
        c = per.setdefault(eng.cue_name, [0, 0, set()]); c[0] += 1; c[1] += bool(hz); c[2].update(n for n, _ in hz)
    return {cue: {"eye_fraction": round(h / n, 3), "fixtures": sorted(fx)} for cue, (n, h, fx) in per.items()}


def load_levels(rig, state):
    for fs in state.get("fixtures", []):
        if fs["name"] in rig.by_name:
            f = rig.by_name[fs["name"]]
            if len(fs["levels"]) == len(f.values): f.values = list(fs["levels"])


VERT = "void main(){ gl_Position = gl_Vertex; }"
FRAG = r"""
#version 120
#define MAXL %(MAXL)d
#define MAXP %(MAXP)d
#define MAXT 8
#define MAXF 12
uniform vec2 res; uniform vec3 camPos; uniform vec3 camTarget; uniform float fovy; uniform float time;
uniform int nL; uniform vec3 Lp[MAXL]; uniform vec3 Ld[MAXL]; uniform vec3 Lc[MAXL]; uniform vec3 Lk[MAXL];
uniform int nF; uniform vec4 Fs[MAXF]; uniform vec2 Fr[MAXF];   // per fixture: bounding sphere + part range
uniform int nP; uniform vec3 Pc[MAXP]; uniform vec3 Px[MAXP]; uniform vec3 Py[MAXP]; uniform vec3 Pz[MAXP]; uniform vec3 Ph[MAXP]; uniform vec3 Pe[MAXP];
uniform vec3 deck; uniform vec3 stepb; uniform vec4 truss; uniform vec3 patio; uniform vec3 hedgeB; uniform vec4 hedgeR;
uniform int nT; uniform vec4 trees[MAXT];
uniform float haze; uniform float exposure;

float hash(vec3 p){ p = fract(p * 0.3183099 + 0.1); p *= 17.0; return fract(p.x * p.y * p.z * (p.x + p.y + p.z)); }
float noise(vec3 x){ vec3 i = floor(x); vec3 f = fract(x); f = f * f * (3.0 - 2.0 * f);
  return mix(mix(mix(hash(i), hash(i + vec3(1,0,0)), f.x), mix(hash(i + vec3(0,1,0)), hash(i + vec3(1,1,0)), f.x), f.y),
             mix(mix(hash(i + vec3(0,0,1)), hash(i + vec3(1,0,1)), f.x), mix(hash(i + vec3(0,1,1)), hash(i + vec3(1,1,1)), f.x), f.y), f.z); }
float act[MAXF];                                   // fixtures whose bounding sphere this pixel's ray passes
float sdBox(vec3 p, vec3 b){ vec3 q = abs(p) - b; return length(max(q, 0.0)) + min(max(q.x, max(q.y, q.z)), 0.0); }

// materials: 0 ground, 1 skirt, 2 truss, 3 crossbar, 4 hedge, 5 trunk, 6 canopy, 10+i fixture part i
float map(vec3 p, out float m){
  float d = p.z; m = 0.0; float k;
  k = sdBox(p - vec3(0.0, deck.y * 0.5, deck.z * 0.5), deck * 0.5); if (k < d){ d = k; m = 1.0; }
  k = sdBox(p - vec3(0.0, -stepb.y * 0.5, stepb.z * 0.5), stepb * 0.5); if (k < d){ d = k; m = 1.0; }
  if (truss.z > 0.0){
    float hz = (truss.z - deck.z) * 0.5;
    vec3 q = p - vec3(0.0, truss.y, deck.z + hz); q.x = abs(q.x) - truss.x;
    k = sdBox(q, vec3(truss.w * 0.5, truss.w * 0.5, hz)); if (k < d){ d = k; m = 2.0; }
    k = sdBox(p - vec3(0.0, truss.y, truss.z - 0.03), vec3(truss.x + 0.2, 0.12, 0.03)); if (k < d){ d = k; m = 3.0; }
  }
  if (hedgeB.y > 0.0 && p.y > hedgeB.x - 2.0){
    k = sdBox(p - vec3(0.0, hedgeB.x + 0.6, hedgeB.y * 0.5), vec3(hedgeB.z * 0.5, 0.6, hedgeB.y * 0.5)) - 0.35 * noise(p * 1.7);
    if (k < d){ d = k; m = 4.0; }
  }
  if (hedgeR.w > 0.0){
    vec2 c = p.xy - hedgeR.xy; float ring = abs(length(c) - hedgeR.z) - 0.7;
    k = max(max(ring, p.z - hedgeR.w), -c.y - 1.5) - 0.35 * noise(p * 1.7); if (k < d){ d = k; m = 4.0; }
  }
  for (int i = 0; i < MAXT; i++){ if (i >= nT) break;
    vec4 t = trees[i]; vec3 q = p - t.xyz;
    k = max(length(p.xy - t.xy) - 0.35, p.z - t.z); if (k < d){ d = k; m = 5.0; }
    k = length(q * vec3(1.0, 1.0, 1.25)) - t.w + 0.9 * noise(p * 0.9); if (k < d){ d = k * 0.7; m = 6.0; }
  }
  for (int f = 0; f < MAXF; f++){ if (f >= nF) break;
    if (act[f] < 0.5) continue;
    float fs = length(p - Fs[f].xyz) - Fs[f].w;
    if (fs > d) continue;
    if (fs > 0.05){ d = min(d, fs); continue; }                                  // far from this fixture: its sphere is a safe step
    int i0 = int(Fr[f].x + 0.5); int cnt = int(Fr[f].y + 0.5);
    for (int j = 0; j < 12; j++){ if (j >= cnt) break; int i = i0 + j;           // <= 12 parts per fixture
      vec3 v = p - Pc[i];
      k = sdBox(vec3(dot(v, Px[i]), dot(v, Py[i]), dot(v, Pz[i])), Ph[i]) - 0.003; if (k < d){ d = k; m = 10.0 + float(i); } }
  }
  return d;
}
vec3 nrm(vec3 p){ float m; const vec2 e = vec2(1.0, -1.0) * 0.004;
  return normalize(e.xyy * map(p + e.xyy, m) + e.yyx * map(p + e.yyx, m) + e.yxy * map(p + e.yxy, m) + e.xxx * map(p + e.xxx, m)); }
float phaseHG(float c){ float g = 0.35; return (1.0 - g * g) / pow(1.0 + g * g - 2.0 * g * c, 1.5) / 12.566; }

vec3 albedo(vec3 p, float m){
  if (m < 0.5){
    float r = length(p.xy - patio.xy);
    if (r < patio.z){ vec2 b = floor(vec2(p.x * 4.0 + p.y * 4.0, p.x * 4.0 - p.y * 4.0) * 1.5);
      float v = hash(vec3(b, 1.0)); return vec3(0.42, 0.17, 0.12) * (0.65 + 0.5 * v); }
    return vec3(0.10, 0.17, 0.06) * (0.7 + 0.6 * noise(p * 5.0));
  }
  if (m < 1.5) return vec3(0.025);
  if (m < 2.5) return vec3(0.55, 0.56, 0.58) * (0.6 + 0.4 * step(0.5, fract(p.z * 4.0 + p.x * 4.0)));
  if (m < 3.5) return vec3(0.45, 0.30, 0.16);
  if (m < 4.5) return vec3(0.06, 0.14, 0.05) * (0.6 + 0.8 * noise(p * 4.0));
  if (m < 5.5) return vec3(0.16, 0.11, 0.07);
  if (m < 6.5) return vec3(0.09, 0.18, 0.06) * (0.6 + 0.8 * noise(p * 2.5));
  return vec3(0.30, 0.30, 0.33);
}

void main(){
  vec2 uv = (gl_FragCoord.xy - 0.5 * res) / res.y;
  vec3 fw = normalize(camTarget - camPos); vec3 rt = normalize(cross(fw, vec3(0.0, 0.0, 1.0))); vec3 up = cross(rt, fw);
  vec3 rd = normalize(fw + (uv.x * rt + uv.y * up) * 2.0 * tan(fovy * 0.5));
  for (int f = 0; f < MAXF; f++){                                               // per-ray fixture culling
    act[f] = 0.0; if (f >= nF) continue;
    vec3 oc = camPos - Fs[f].xyz; float b = dot(oc, rd); float c = dot(oc, oc) - Fs[f].w * Fs[f].w;
    if (c < 0.0 || (b < 0.0 && b * b - c > 0.0)) act[f] = 1.0; }
  float t = 0.02, m = -1.0, mm; bool hit = false;
  for (int i = 0; i < 110; i++){ float d = map(camPos + rd * t, mm); if (d < 0.0015 * t){ hit = true; m = mm; break; } t += d; if (t > 60.0) break; }
  float tHit = hit ? t : 60.0;
  vec3 col;
  if (!hit){ col = mix(vec3(0.02, 0.025, 0.05), vec3(0.004, 0.005, 0.015), clamp(rd.z * 2.0, 0.0, 1.0)); }
  else {
    vec3 p = camPos + rd * t; vec3 n = nrm(p); vec3 alb = albedo(p, m);
    vec3 E = vec3(0.05, 0.055, 0.09) * (0.6 + 0.4 * n.z);
    for (int i = 0; i < MAXL; i++){ if (i >= nL) break;
      vec3 L = Lp[i] - p; float dist = length(L); vec3 l = L / dist;
      float c = dot(-l, Ld[i]); float ch = 1.0 / sqrt(1.0 + Lk[i].x * Lk[i].x); float co = 1.0 / sqrt(1.0 + 1.44 * Lk[i].x * Lk[i].x);
      float omega = 6.2832 * (1.0 - ch) + 1e-5;
      E += Lc[i] * smoothstep(co, ch, c) * max(dot(n, l), 0.0) / (dist * dist + 0.01) / omega * 1.2 * step(dist, Lk[i].z + 0.3);
    }
    col = alb * E;
    if (m > 9.5){ int pid = int(m - 10.0 + 0.5); vec3 em = vec3(0.0);
      for (int i = 0; i < MAXP; i++){ if (i == pid) em = Pe[i]; }
      col += em * 3.0; }
  }
  vec3 V = vec3(0.0); float j = hash(vec3(gl_FragCoord.xy, 7.0));
  for (int i = 0; i < MAXL; i++){ if (i >= nL) break;
    vec3 a = Ld[i]; vec3 w0 = camPos - Lp[i]; float tanh = Lk[i].x, ap = Lk[i].y, len = Lk[i].z;
    float ph = phaseHG(dot(a, -rd)); float omega = 6.2832 * (1.0 - 1.0 / sqrt(1.0 + tanh * tanh)) + 1e-5;
    if (tanh < 0.12){
      float b = dot(rd, a); float dd = dot(rd, w0); float e = dot(a, w0); float den = 1.0 - b * b;
      float tr = den > 1e-5 ? (b * e - dd) / den : 0.0; tr = clamp(tr, 0.0, tHit);
      float s = clamp(dot(camPos + rd * tr - Lp[i], a), 0.0, len);
      vec3 q = Lp[i] + a * s; float tr2 = clamp(dot(q - camPos, rd), 0.0, tHit);
      float delta = length(camPos + rd * tr2 - q); float sig = 0.5 * (ap + tanh * s); float sinA = max(sqrt(den), 0.04);
      float dens = haze * (0.35 + 1.3 * noise(q * 0.7 + vec3(0.0, 0.0, -time * 0.15)));
      V += Lc[i] * dens * ph * exp(-0.5 * delta * delta / (sig * sig)) / (2.5066 * sig * sinA) / (omega * (s * s + 0.3)) * sig * sig * 5.0 * smoothstep(len, len - 0.3, s);
    } else {
      float tEnd = min(tHit, 25.0); float dt = tEnd / 12.0;
      for (int k = 0; k < 12; k++){
        vec3 x = camPos + rd * ((float(k) + j) * dt); vec3 v = x - Lp[i]; float s = dot(v, a);
        if (s > 0.0 && s < len){ float r = length(v - a * s); float cr = ap + tanh * s;
          V += Lc[i] * smoothstep(cr, cr * 0.55, r) * haze * ph / (omega * (s * s + 0.3)) * dt * 0.25; }
      }
    }
    vec3 toL = Lp[i] - camPos; float tl = dot(toL, rd);
    if (tl > 0.0 && tl < tHit + 0.05){ float dl = length(toL - rd * tl); float lc = dot(normalize(camPos - Lp[i]), a);
      V += Lc[i] * smoothstep(0.0, 1.0, (lc - 0.2) * 1.5) * 0.0006 / (dl * dl + 4e-4) * min(1.0, tl * tl * 0.05); }
  }
  col += V;
  col = vec3(1.0) - exp(-col * exposure);
  gl_FragColor = vec4(pow(col, vec3(1.0 / 2.2)), 1.0);
}
""" % {"MAXL": MAXL, "MAXP": MAXP}
MAXF = 12


class StageRenderer:
    """GPU renderer of a rig's venue + emitters. scale < 1 ray-traces fewer pixels and upscales (graphics.gl.Upscaler)."""
    def __init__(self, rig, haze=None, exposure=2.2, scale=1.0):
        self.rig, self.prog, self.up = rig, None, None
        self.haze = rig.venue.get("haze", 0.5) if haze is None else haze
        self.exposure, self.scale = exposure, scale

    def _render(self, w, h, eye, target, t, fovy, em, parts):
        from graphics.gl import gl, ShaderProgram, fullscreen_quad
        if self.prog is None: self.prog = ShaderProgram(VERT, FRAG)
        v = self.rig.venue
        gl.glViewport(0, 0, int(w), int(h)); gl.glDisable(gl.DEPTH_TEST); gl.glDisable(gl.BLEND)
        gl.glClearColor(0.0, 0.0, 0.0, 1.0); gl.glClear(gl.COLOR | gl.DEPTH)       # a stale depth buffer hid every other frame
        p = self.prog; p.use()
        p.set("res", float(w), float(h)); p.set("camPos", *map(float, eye)); p.set("camTarget", *map(float, target))
        p.set("fovy", math.radians(fovy)); p.set("time", float(t)); p.set("haze", float(self.haze)); p.set("exposure", float(self.exposure))
        pad = lambda xs, n=MAXL, fill=(0.0, 0.0, 0.0): (list(xs) + [fill] * n)[:n]
        p.set("nL", len(em))
        p.set_vec3_array("Lp", pad([e.pos for e in em])); p.set_vec3_array("Ld", pad([e.dir for e in em]))
        p.set_vec3_array("Lc", pad([e.rgb for e in em])); p.set_vec3_array("Lk", pad([(e.tan_half, e.aperture, e.length) for e in em]))
        parts = parts[:MAXP]; p.set("nP", len(parts))
        groups = {}                                               # parts are emitted fixture by fixture: contiguous ranges
        for i, q in enumerate(parts): groups.setdefault(q.name.split(".")[0], []).append(i)
        fs = []
        for idx in list(groups.values())[:MAXF]:
            pts = [parts[i] for i in idx]; c = tuple(sum(q.c[k] for q in pts) / len(pts) for k in range(3))
            r = max(math.dist(c, q.c) + math.sqrt(sum(h * h for h in q.half)) for q in pts) + 0.01
            if len(idx) > 12: raise ValueError(f"fixture has {len(idx)} parts; the shader handles <= 12 per fixture")
            fs.append((c, r, idx[0], len(idx)))
        p.set("nF", len(fs))
        for k, (c, r, i0, n) in enumerate(fs): p.set(f"Fs[{k}]", *map(float, c), float(r)); p.set(f"Fr[{k}]", float(i0), float(n))
        for nm, get in (("Pc", lambda q: q.c), ("Px", lambda q: q.ax), ("Py", lambda q: q.ay), ("Pz", lambda q: q.az),
                        ("Ph", lambda q: q.half), ("Pe", lambda q: q.emit)):
            p.set_vec3_array(nm, pad([get(q) for q in parts], MAXP))
        p.set("deck", *map(float, v.get("deck", (0, 0, 0)))); p.set("stepb", *map(float, v.get("step", (0, 0, 0))))
        p.set("truss", *map(float, v.get("truss", (0, 0, 0, 0)))); p.set("patio", *map(float, v.get("patio", (0, 0, 0))))
        p.set("hedgeB", *map(float, v.get("hedge_back", (0, 0, 0)))); p.set("hedgeR", *map(float, v.get("hedge_ring", (0, 0, 0, 0))))
        trees = v.get("trees", [])[:8]; p.set("nT", len(trees))
        for i, tr in enumerate(trees): p.set(f"trees[{i}]", *map(float, tr))
        fullscreen_quad(); gl.glUseProgram(0)

    def draw(self, w, h, eye, target, t=0.0, fovy=50.0):
        parts, em = kinematics(self.rig, t)
        if self.scale >= 0.999: self._render(w, h, eye, target, t, fovy, em, parts)
        else:
            from graphics.gl import Upscaler
            if self.up is None: self.up = Upscaler()
            self.up.draw(lambda rw, rh: self._render(rw, rh, eye, target, t, fovy, em, parts), w, h, self.scale)
        return em


def snapshot(rig, path, view="foh", w=1280, h=720, t=0.0, haze=None, eye=None, target=None):
    from graphics.gl import offscreen_png
    if view not in VIEWS and eye is None: raise LookupError(f"unknown view {view!r} (have {list(VIEWS)})")
    e, tg = (eye, target) if eye is not None else VIEWS[view]
    r = StageRenderer(rig, haze)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return offscreen_png(lambda pw, ph: r.draw(pw, ph, e, tg, t), w, h, str(path))


# ---------- live window ----------
def run_window(url, rig_name, show_name, play, debug_frames=0, render_scale=0.5, clock="link", audio=None):
    from gui import glapp
    from gui.window import KEY
    from graphics.gl import OrbitCamera, hud_text
    from lighting.console import ConsoleClient, start_console
    src = ConsoleClient(url)
    if not src.alive(): start_console(rig_name, show_name, "auto", int(url.rsplit(":", 1)[-1].split("/")[0]), play, clock, audio)
    src.follow(30); rig = Rig.load(rig_name); R = StageRenderer(rig, scale=render_scale)
    cam = OrbitCamera(fov=50); cam.target = [0.0, 3.0, 2.0]; cam.dist, cam.yaw, cam.pitch = 15.0, 0.0, 10.0
    views = list(VIEWS); vi = [0]; t0 = time.monotonic(); held = set(); moving = set(); frames = [0]; fps = [0.0, time.monotonic(), 0]; hud_cache = {}

    def set_view(name):
        (ex, ey, ez), tg = VIEWS[name]; cam.target = list(tg)
        dx, dy, dz = tg[0] - ex, tg[1] - ey, tg[2] - ez; d = math.sqrt(dx * dx + dy * dy + dz * dz)
        cam.dist = d; cam.pitch = math.degrees(math.asin(-dz / d)); cam.yaw = math.degrees(math.atan2(dx, dy))

    def on_draw(s):
        st = src.latest
        if st: load_levels(rig, st)
        t = time.monotonic() - t0
        walk(); R.draw(s.pw, s.ph, cam.eye(), cam.target, t, fovy=cam.fov)
        fps[2] += 1; now = time.monotonic()
        if now - fps[1] >= 0.5:
            fps[0] = fps[2] / (now - fps[1]); fps[1], fps[2] = now, 0
            if fps[0] < 50 and R.scale > 0.3: R.scale = max(0.3, R.scale * 0.85)          # keep it interactive
            elif fps[0] > 58 and R.scale < render_scale: R.scale = min(render_scale, R.scale * 1.08)
            if debug_frames: print(f"fps {fps[0]:.1f} ({s.pw}x{s.ph} px, ray-traced at {R.scale:.2f})", flush=True)
        hz = eye_hazards(emitters(rig, t), rig.venue)
        if hz: hud_text(["EYE HAZARD: " + ", ".join(sorted({n.split('.')[0] for n, _ in hz})) + " crossing the audience at head height"],
                        s.w, s.h, x=10, y=-10, size=13, color=(1, 0.3, 0.2), scale=s.scale)
        tick4 = int(t * 4)                                                  # overlay text changes <= 4x/s (each change re-renders it)
        if st and (st.get("paused") or hud_cache.get("t") != tick4 or "rows" not in hud_cache):
            hud_cache["t"] = tick4; hud_cache["rows"] = inspector(rig); hud_cache["st"] = st; hud_cache["fps"] = fps[0]
        st_h = hud_cache.get("st")
        if st_h:
            st = st_h
            hud_text([f"{st.get('cue') or '-'}   bar {st.get('bar')}   {st.get('bpm')} BPM   {st.get('output', '')}",
                      f"{hud_cache['fps']:.0f} fps (render {R.scale:.2f}) · haze {R.haze:.2f}" + ("   PAUSED" if st.get("paused") else "") + ("   BLACKOUT" if st.get("blackout") else "") + ("" if src.ok else "   (console not answering)"),
                      f"beat {st.get('beat', 0):.3f}" + ("   paused: , . step 1/16   - = step 1 beat" if st.get("paused") else ""),
                      "drag orbit · right-drag pan · scroll zoom · arrows (or W A D) walk · Q/E down/up",
                      "1-9 cues  P play  space PAUSE  , . - = step  T tap  B blackout  S strobe  H haze  [ ] density  V view  C snapshot"],
                     s.w, s.h, size=12, scale=s.scale)
            hud_text(["joints (DMX → degrees, * = mirrored motor)"] + hud_cache["rows"], s.w, s.h, x=-10, y=10, size=12, scale=s.scale)
        if frames[0] < debug_frames:                                   # read back what the window actually shows
            from graphics.gl import gl as G
            from graphics.png import write_png
            import ctypes
            buf = (ctypes.c_ubyte * (s.pw * s.ph * 3))(); G.glPixelStorei(0x0D05, 1)
            G.glReadPixels(0, 0, s.pw, s.ph, 0x1907, 0x1401, buf)
            raw = bytes(buf); rows = [raw[y * s.pw * 3:(y + 1) * s.pw * 3] for y in range(s.ph)][::-1]
            OUT.mkdir(parents=True, exist_ok=True); write_png(OUT / f"window_frame{frames[0]}.png", s.pw, s.ph, b"".join(rows))
        frames[0] += 1

    def walk():
        """WASD / arrows: move the orbit target (and so the camera) along the ground; Q/E: down/up."""
        yw = math.radians(cam.yaw); fwd = (math.sin(yw), math.cos(yw)); sp = 0.08
        mv = {"w": (fwd[0], fwd[1], 0), "up": (fwd[0], fwd[1], 0), "s_": (-fwd[0], -fwd[1], 0), "down": (-fwd[0], -fwd[1], 0),
              "a": (-fwd[1], fwd[0], 0), "left": (-fwd[1], fwd[0], 0), "d": (fwd[1], -fwd[0], 0), "right": (fwd[1], -fwd[0], 0),
              "q": (0, 0, -1), "e": (0, 0, 1)}
        for k in moving:
            if k in mv:
                for i in range(3): cam.target[i] += mv[k][i] * sp
        cam.target[2] = max(0.2, cam.target[2])

    def on_key(code, down):
        names = {v: k for k, v in KEY.items()}; k = names.get(code)
        if k is None: return
        if k in ("w", "a", "d", "q", "e", "up", "down", "left", "right"):
            (moving.add if down else moving.discard)(k); return
        if not down:
            if k == "s" and "s" in held: src.send(action="hold", name="strobe", on=False); held.discard("s")
            return
        st = src.latest or {}
        if k == "esc": raise SystemExit
        if k.isdigit() and k != "0":
            cues = st.get("cues", []); i = int(k) - 1
            if i < len(cues): src.send(action="cue", name=cues[i])
        elif k == "p": src.send(action="play")
        elif k == "space": src.send(action="pause")
        elif k == "t": src.send(action="tap")
        elif k == "b": src.send(action="blackout", on=not st.get("blackout"))
        elif k == "s" and "s" not in held: held.add("s"); src.send(action="hold", name="strobe", on=True, effect="strobe", rate=0.8, color="white")
        elif k == "h": src.send(action="haze", seconds=5)
        elif k == "v": vi[0] = (vi[0] + 1) % len(views); set_view(views[vi[0]])
        elif k == "c":
            OUT.mkdir(parents=True, exist_ok=True); fn = OUT / f"stage_{time.strftime('%H%M%S')}.png"
            glapp.snapshot(on_draw, 1280, 720, fn); print("snapshot", fn, flush=True)

    def status(): return f"Stage 3D — {(src.latest or {}).get('cue') or 'waiting for console'} — view {views[vi[0]]}"
    set_view("photo")
    # [ and ] are not in KEY: map their virtual keycodes (33, 30) directly
    def on_key2(code, down):
        steps = {43: -1 / 16, 47: 1 / 16, 27: -1.0, 24: 1.0}              # , . - =  (virtual keycodes)
        if down and code in steps: src.send(action="step", beats=steps[code]); return
        if down and code == 33: R.haze = max(0.0, R.haze - 0.1)
        elif down and code == 30: R.haze = min(3.0, R.haze + 0.1)
        else: on_key(code, down)
    orbit = glapp.orbit_mouse(cam)
    def on_mouse(e):
        if debug_frames: print(f"mouse {e.kind} button={e.button} dx={getattr(e, 'dx', 0):.1f} dy={getattr(e, 'dy', 0):.1f} -> yaw {cam.yaw:.1f} pitch {cam.pitch:.1f} dist {cam.dist:.1f}", flush=True)
        if e.kind == "drag" and not (e.button == 1 or e.shift): cam.rotate(e.dx, e.dy)   # with orbit's own call: 0.8 deg/pt
        orbit(e)
    def on_key3(code, down):
        if debug_frames: print(f"key {code} {'down' if down else 'up'}", flush=True)
        on_key2(code, down)
    glapp.run("Stage 3D", 1280, 720, on_draw, on_key=on_key3, status=status, cam=cam, on_mouse=on_mouse, fps=60)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", nargs="?", default="window", choices=["window", "snap", "audit"])
    ap.add_argument("--url", default="http://127.0.0.1:8779"); ap.add_argument("--rig", default="halloween")
    ap.add_argument("--show", default="halloween_demo"); ap.add_argument("--play", action="store_true")
    ap.add_argument("--cue"); ap.add_argument("--song"); ap.add_argument("--beat", type=float, default=0.0); ap.add_argument("--view", default="foh")
    ap.add_argument("-o", "--out"); ap.add_argument("--w", type=int, default=1280); ap.add_argument("--h", type=int, default=720)
    ap.add_argument("--live", action="store_true"); ap.add_argument("--haze", type=float)
    ap.add_argument("--clock", default="link", choices=["link", "internal"]); ap.add_argument("--audio", help="react to this app's audio")
    ap.add_argument("--scale", type=float, default=0.5, help="window: fraction of pixels ray traced (upscaled); 1 = full res")
    ap.add_argument("--debug-frames", type=int, default=0, help="save the window's first N frames to out/lighting/window_frameK.png")
    a = ap.parse_args()
    if a.cmd == "window": return run_window(a.url, a.rig, a.show, a.play, a.debug_frames, a.scale, a.clock, a.audio)
    rig = Rig.load(a.rig)
    if a.cmd == "audit":
        from lighting.show import load_show
        for cue, r in audit_program(rig, load_show(a.show), song=a.song).items():
            print(f"{cue:12} eye-height crossings {r['eye_fraction'] * 100:5.1f}% of the time" + (f"  ({', '.join(r['fixtures'])})" if r["fixtures"] else ""))
        return
    if a.live:
        from lighting.console import ConsoleClient
        load_levels(rig, ConsoleClient(a.url).state(timeout=2))
    else:
        from lighting.show import Engine, load_show
        from lighting.clock import ManualClock
        show = load_show(a.show)
        clk = ManualClock(show.get("bpm", 120)); clk.value = a.beat; eng = Engine(rig, clock=clk)
        eng.cue(show, a.cue or next(iter(show["cues"]))); eng.frame()
    out = a.out or str(OUT / f"stage_{a.cue or 'live'}_{a.view}.png")
    print(snapshot(rig, out, a.view, a.w, a.h, t=a.beat * 0.5, haze=a.haze))


if __name__ == "__main__":
    try: main()
    except (LookupError, ValueError) as e: print(f"stage3d: {e}", file=sys.stderr); sys.exit(1)
