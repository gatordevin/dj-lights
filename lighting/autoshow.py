"""Make a light show for any song on any rig: songmap sections (intro/build/drop/breakdown/outro) -> cues for whatever lights the rig has.
CLI:
  python3 lighting/autoshow.py FILE --rig RIG [--palette auto|NAME|red,blue,white] [--style club|chill|hype] [--strobe]
                               [--fog] [--mover-lead 0.5] [--mover-targets air|floor] [--out out/lighting/shows/X.json] [--preview]
  python3 lighting/autoshow.py palettes                     named colour palettes
Python:
  make_show(songmap, rig, palette="auto", style="club", strobe=False, fog=False, mover_lead=0.5, mover_targets="air") -> show dict
      (lighting/show format: "cues", "songs": {"song": {"bpm", "bars", "cues": [[bar, cue], ...]}}, "meta": {...})
  live_show(rig, palette="auto", style="club", strobe=False, audio_react=True) -> show dict with a "program" for live DJ sets (beat clock /
      Link / audio-reactive; no song file needed)
  mover_spots(rig, fixture, targets="air"|"floor") -> ([[pan, tilt], ...], "air"|"floor"|"cone")  aim spots per mover;
      safe_spots(rig, fixture) = the "air" ones (beams above the crowd's eyes); meta.warnings says when that is impossible
  Moving-head speed: spots are visited in nearest-travel order and each head starts its move early enough for its longest
      snap (lighting.motion.move_time at the song's tempo, meta.mover_leads); heads too slow for a look snap less often;
      meta.motion = lighting.motion.audit (share of lit time a head is still travelling) + warnings above 10 %.
  set_edit(file, rig_name, start_bar, end_bar, cue|None) / get_edits(file, rig_name): DJ cue edits per song + rig, kept in
      lighting/show_edits.json and applied by every make_show (edits="auto"; pass {} to ignore them)
  PALETTES {name: (a, b, c, cool)}
How lights are used (groups resolve on any rig through lighting.fixtures standard groups, so the show only names kinds):
  wash (pars, bars, uplights): the beat — punches on the kick in grooves, colour chases across the stage (left to right),
      slow fades in breakdowns, a dark beat before each drop, full hits on the drop.
  movers (moving heads): move only in the dark ("snap" lead = --mover-lead beats before the hit; use 1.0 for slow heads),
      land on spots computed per fixture by inverse kinematics from its rig position (stage3d.aim_at) that keep beams
      above the crowd's eyes; slow circles in breakdowns.
  effects (spiders, derbies, LED-cell effects): hits on the off-beats and colour drifts.
  strobes: only with --strobe, and then only as single flashes on drop downbeats (punch, <= 2.6 flashes/s guard).
  lasers: never driven by auto shows (arm and run them from the console yourself). fog: with --fog, "meta.haze_bars" lists
      bars (4 before each drop) where the player fires a short haze burst (engine rest rules apply).
Bar plan from the songmap: intro -> intro; groove -> groove_a/groove_b alternating every 8 bars (dip when quiet); peak ->
  peak; build -> build1..build3 by distance to the drop, the last bar -> predrop (beat 4 dark); drop bar -> drop, then
  drive/peak; breakdown -> break; outro -> outro; vocal gaps (with --vocals analysis) in grooves -> gap. Styles: chill
  swaps hits for fades/slow sweeps; hype adds more full-rig hits.
Palette "auto": a stable pick per song (hash of the title) from PALETTES, nudged by tempo (slow -> warm, fast -> neon).
Fails: LookupError for an unknown palette/style or a rig with no lights the show can use; ValueError for bad colours.
Limits: one look vocabulary (club-style); the choreography is rule-based, so check it once (`dj.py show FILE --preview`).
Depends on: lighting/show (render, check_layers, PALETTE), lighting/fixtures, lighting/stage3d (aim_at), lighting/songmap.
Used by: lighting/dj.py, tests/test_lighting_dj.py."""
import sys, json, math, hashlib, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lighting.fixtures import Rig
from lighting.show import check_layers, rgb, PALETTE, EFFECTS, fx_snap


def fx_snapdark(a, i, n, t):
    """snap + move in the dark: the head jumps to its next spot `lead` beats before each hit and is held DARK until the hit, so
    slow heads travel unseen and light up already in place. Registered into lighting.show.EFFECTS when autoshow is imported
    (shows using it are played through dj/session/webapp/mcp, which import autoshow)."""
    out = fx_snap(a, i, n, t); every, lead = a.get("every", 1), a.get("lead", 0.5)
    if ((t.beat + lead) % every) < lead: out["dimmer"] = 0.0
    return out


EFFECTS.setdefault("snapdark", fx_snapdark)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "lighting" / "shows"
def C(*v): return [round(x, 3) for x in v]
PALETTES = {
    "neon": (C(1, 0, .55), C(0, .85, 1), C(.55, 1, 0), C(.2, 0, .7)), "sunset": (C(1, .3, 0), C(1, .1, .45), C(.6, 0, 1), C(.25, 0, .5)),
    "ice": (C(0, .6, 1), C(1, 1, 1), C(0, .2, 1), C(0, .1, .45)), "fire": (C(1, 0, 0), C(1, .35, 0), C(1, .7, 0), C(.4, 0, 0)),
    "forest": (C(0, 1, .2), C(.5, 1, 0), C(0, .8, .8), C(0, .25, .2)), "royal": (C(.55, 0, 1), C(1, .75, .1), C(.1, .2, 1), C(.15, 0, .45)),
    "halloween": (C(.55, .05, 1), C(.05, 1, .15), C(1, .3, 0), C(.1, 0, .6)), "uf": (C(1, .27, .03), C(0, .12, .85), C(1, 1, 1), C(0, .05, .4)),
    "candy": (C(1, .2, .6), C(.3, .8, 1), C(1, .9, .2), C(.4, .1, .5)), "mono": (C(1, 1, 1), C(.8, .85, 1), C(1, .9, .75), C(.1, .1, .25)),
    "miami": (C(1, 0, .6), C(0, 1, 1), C(.6, 0, 1), C(0, .2, .5)), "gold": (C(1, .6, .05), C(1, .85, .4), C(1, 1, 1), C(.35, .15, 0)),
}
STYLES = ("club", "chill", "hype")
SONG = "song"


def palette(name, songmap=None):
    """(a, b, c, cool) from a palette name, "auto" or a comma list of colour names/hex (#ff0080)."""
    if isinstance(name, (list, tuple)) and len(name) == 4: return tuple(name)
    name = (name or "auto").strip()
    if name in PALETTES: return PALETTES[name]
    if name == "auto":
        title = (songmap or {}).get("title", "") + str((songmap or {}).get("artist", "")); bpm = (songmap or {}).get("bpm", 124)
        pool = ["sunset", "fire", "gold", "royal"] if bpm < 100 else ["neon", "miami", "ice", "candy"] if bpm >= 135 else list(PALETTES)
        pool = [p for p in pool if p not in ("mono", "uf", "halloween")]
        return PALETTES[pool[int(hashlib.sha1(title.encode()).hexdigest(), 16) % len(pool)]]
    cols = []
    for c in name.split(","):
        c = c.strip().lower()
        if c.startswith("#") and len(c) == 7: cols.append(C(*(int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))))
        elif c in PALETTE: cols.append(list(PALETTE[c]))
        else: raise LookupError(f"unknown colour or palette {c!r} (palettes: {', '.join(PALETTES)}; colours: {', '.join(PALETTE)})")
    if not 1 <= len(cols) <= 4: raise ValueError("give 1-4 colours")
    while len(cols) < 3: cols.append(cols[len(cols) % max(1, len(cols))])
    cool = cols[3] if len(cols) == 4 else C(*(x * 0.3 for x in cols[0]))
    return tuple(cols[:3]) + (cool,)


def _has(rig, g):
    try: rig.group(g); return True
    except LookupError: return False


def _segment_safe(p, q, venue):
    """True if the straight beam from p to target q never passes through the crowd's eye zone."""
    x0, x1, y0, y1 = venue.get("audience", (-8.0, 8.0, -14.0, -1.0)); z0, z1 = venue.get("eye_z", (1.0, 2.0))
    for k in range(1, 41):
        s = k / 40; x, y, z = (a + (b - a) * s for a, b in zip(p, q))
        if x0 <= x <= x1 and y0 <= y <= y1 and z0 - 0.15 <= z <= z1 + 0.15: return False
    return True


TARGETS = ("air", "floor")


def mover_spots(rig, f, targets="air", n=8):
    """([pan, tilt] spots, what they hit) for mover f. targets "air": beams stay above the crowd's eyes — ceiling/air over
    the floor, far and side walls at a height the head can reach without dipping into the eye zone, the wall behind the
    DJ; each candidate must be reachable (stage3d.aim_at) and its straight beam must miss the audience eye zone (venue
    "audience" x "eye_z" +- 0.15 m beam radius). If fewer than 2 such spots exist (e.g. heads hung at 2.5 m under a 3 m ceiling) or
    targets is "floor": spots on the dance floor (gobos/washes on the floor; the beams cross the crowd)."""
    from lighting.stage3d import aim_at
    if targets not in TARGETS: raise LookupError(f"mover targets must be one of {TARGETS}")
    v = rig.venue or {}; room = v.get("room") or [12.0, 12.0, 6.0]; W, D, H = v.get("deck") or [2.0, 1.0, 0.0]
    top = max(2.9, min(room[2], 8.0)); rw, rd = room[0], room[1]; z1 = v.get("eye_z", (1.0, 2.0))[1] + 0.2
    lens = (f.pos[0], f.pos[1], f.pos[2] + (-0.15 if f.mount == "hung" else 0.15))
    def pick(cands, check):
        out = []
        for t in cands:
            r = aim_at(f, t)
            if r is None or (check and not _segment_safe(lens, t, v)): continue
            if all(abs(r[0] - a) + abs(r[1] - b) > 12 for a, b in out): out.append([round(r[0], 1), round(r[1], 1)])
        return out
    air = []
    if targets == "air":
        for fx in (-0.42, -0.25, -0.1, 0.1, 0.25, 0.42):                             # ceiling / air over the floor
            for fy, fz in ((-0.55, 1.0), (-0.3, 0.97), (-0.12, 0.95)): air.append([fx * rw, fy * rd, fz * top])
        for zw in sorted({min(top, max(2.6, lens[2] + 0.15)), lens[2] - 0.03}):
            if zw < z1: continue
            for fx in (-0.45, -0.25, 0.0, 0.25, 0.45): air.append([fx * rw, -rd, zw])  # far wall behind the crowd
            for sx in (-1, 1):
                for fy in (-0.7, -0.4, -0.1): air.append([sx * rw / 2, fy * rd, zw])  # side walls
        for fx in (-0.4, 0.0, 0.4): air.append([fx * rw, D + 2.5, min(top, 3.5)])    # back wall behind the DJ
        out = pick(air, True)
        if len(out) >= 2:
            order = sorted(out, key=lambda s_: s_[0]); return (order[::2] + order[1::2])[:n], "air"
    floor = [[fx * rw, fy * rd, 0.0] for fx in (-0.3, -0.15, 0.0, 0.15, 0.3) for fy in (-0.2, -0.35, -0.5)]
    out = pick(floor, False)
    if len(out) < 2:                                                                  # unplaced/odd mounting: a modest cone
        T = (f.profile.tilt_deg or 90) / 2; c = min(35.0, T * 0.6)
        out = [[max(-f.profile.pan_deg / 2, min(f.profile.pan_deg / 2, p)), c * k] for p, k in ((-60, 1), (-20, 0.6), (20, 1), (60, 0.6), (0, -0.4))]
        return out[:n], "cone"
    order = sorted(out, key=lambda s_: s_[0]); return (order[::2] + order[1::2])[:n], "floor"


def safe_spots(rig, f, n=8): return mover_spots(rig, f, "air", n)[0]


def _order_spots(f, spots, bpm=124.0, max_beats=1.5):
    """Spots for a head to snap between: nearest-travel order visited back and forth (no long wrap-around jump), dropping the
    farthest-out spot while any single move would take longer than max_beats at this tempo (keeps >= 2 spots)."""
    from lighting.motion import move_time
    if len(spots) < 3: return spots
    t = lambda a, b: move_time(f, b[0] - a[0], b[1] - a[1])
    left, tour = list(spots[1:]), [spots[0]]
    while left:
        nxt = min(left, key=lambda q: t(tour[-1], q)); tour.append(nxt); left.remove(nxt)
    while len(tour) > 2 and max(t(a, b) for a, b in zip(tour, tour[1:])) * bpm / 60 > max_beats:
        k = max(range(len(tour)), key=lambda i: max(t(tour[i], tour[j]) for j in (i - 1, i + 1) if 0 <= j < len(tour)))
        tour.pop(k)
    return tour + tour[-2:0:-1]                                                        # ping-pong: a b c d c b | a b ...


def _lead_for(f, spots, bpm, lead):
    """Beats before the hit a head must start moving so even its longest snap lands in the dark (>= the asked lead)."""
    from lighting.motion import move_time
    if len(spots) < 2: return lead
    worst = max(move_time(f, b[0] - a[0], b[1] - a[1]) for a, b in zip(spots, spots[1:] + spots[:1]))
    return max(lead, min(4.0, math.ceil((worst + 0.05) * bpm / 60 * 4) / 4))


def _cues(rig, pal, style, strobe, lead, targets="air", report=None, bpm=124.0, leads=None):
    a, b, c, cool = pal; L = lambda g, e, **k: {"group": g, "effect": e, **k}
    W, M, E, S = (_has(rig, g) for g in ("wash", "movers", "effects", "strobes"))
    movers = rig.group("movers") if M else []
    spots, used = {}, {}
    for f in movers:
        spots[f.name], used[f.name] = mover_spots(rig, f, targets); spots[f.name] = _order_spots(f, spots[f.name], bpm)
    if report is not None: report.update(used)
    flead = {f.name: _lead_for(f, spots[f.name], bpm, lead) for f in movers}             # per head, from its real speed
    effs = rig.group("effects") if E else []
    E_SPOTS = [[0, -40], [0, -20], [0, 30], [0, 45], [0, 30], [0, -20]]                  # tilt-only effects, back and forth
    elead = max([_lead_for(f, E_SPOTS, bpm, lead) for f in effs if f.joint_angles()] or [lead])
    if leads is not None: leads.update({f.name: elead for f in effs if f.joint_angles()})
    def esnap(every, spots_=E_SPOTS):
        e = every
        while elead > 0.75 * e and e < 8: e *= 2
        return L("effects", "snapdark", spots=spots_, every=e, lead=min(elead, e - 0.25))
    if leads is not None: leads.update(flead)
    def snap(every, tag=""):
        out = []
        for f in movers:
            e = every
            while flead[f.name] > 0.75 * e and e < 8: e *= 2                           # slow head: snap less often so it can arrive
            out.append(L(f.name, "snapdark", spots=spots[f.name], every=e, lead=min(flead[f.name], e - 0.25)))
        return out
    def mcol(color, dim=1.0): return [L("movers", "color", color=color, dimmer=dim)] if M else []
    eff = lambda *ls: list(ls) if E else []
    wash = lambda *ls: list(ls) if W else []
    hits_e = eff(L("effects", "punch", colors=[b, a, c], slots=[1, 3], decay=10), esnap(2))
    chill = style == "chill"
    lit = [g for g, ok in (("wash", W), ("movers", M), ("effects", E)) if ok]          # "everything" = these kinds (never strobes/lasers/fog)
    every = lambda e, **k: [L(g, e, **k) for g in lit]
    cues = {
        "intro": wash(L("wash", "fade", colors=[cool, a], beats=16, spread=0.15, dimmer=0.55)) + mcol(cool, 0.35)
                 + [L(f.name, "sweep", shape="circle", pan=20, tilt=10, center=spots[f.name][0], beats=32) for f in movers]
                 + eff(L("effects", "fade", colors=[cool, b], beats=16, dimmer=0.3)),
        "groove_a": (wash(L("wash", "fade", colors=[a, b], beats=8, spread=0.2, dimmer=0.8)) if chill else
                     wash(L("wash", "punch", colors=[a, b], slots=[0, 2], decay=7), L("wash", "punch", color=c, slots=[1, 3], decay=11, accent=0.45)))
                    + mcol(c, 0.9) + snap(4 if chill else 2) + (eff(L("effects", "fade", colors=[b, a], beats=8, dimmer=0.5)) if chill else hits_e),
        "groove_b": (wash(L("wash", "fade", colors=[b, c, a], beats=8, spread=0.25, dimmer=0.8)) if chill else
                     wash(L("wash", "chase", colors=[a, b, c], beats=1, dimmer=1.0), L("wash", "punch", colors=[a, b, c], slots=[0], decay=6)))
                    + mcol(a, 0.9) + snap(4 if chill else 2) + (eff(L("effects", "fade", colors=[c, a], beats=8, dimmer=0.5)) if chill else hits_e),
        "dip": wash(L("wash", "fade", colors=[cool, a], beats=8, dimmer=0.45)) + mcol(cool, 0.3) + snap(8),
        "peak": (wash(L("wash", "punch", colors=[a, b, c, a], downbeat_color="white", slots=[0, 1, 2, 3], decay=8))
                 + mcol("white") + snap(1 if style == "hype" else 2) + eff(L("effects", "punch", colors=[c, a], slots=[0, 1, 2, 3], decay=9), esnap(1, [[0, -40], [0, 40]]))),
        "break": wash(L("wash", "fade", colors=[cool, b], beats=16, spread=0.3, dimmer=0.5)) + mcol(cool, 0.35)
                 + [L(f.name, "sweep", shape="circle", pan=25, tilt=12, center=spots[f.name][0], beats=32) for f in movers]
                 + eff(L("effects", "fade", colors=[cool, a], beats=16, dimmer=0.3)),
        "build1": wash(L("wash", "punch", colors=[a, b], slots=[0, 2], decay=9, accent=0.6)) + mcol(c, 0.7) + snap(4) + eff(L("effects", "punch", color=b, slots=[1, 3], decay=11, accent=0.5)),
        "build2": wash(L("wash", "punch", colors=[a, b, c], slots=[0, 1, 2, 3], decay=10, accent=0.7)) + mcol(c, 0.85) + snap(2) + eff(L("effects", "punch", colors=[b, a], slots=[1, 3], decay=10)),
        "build3": wash(L("wash", "punch", colors=[a, c], slots=[0, 1, 2, 3], downbeat_color="white", decay=12, accent=0.9)) + mcol("white", 0.9) + snap(1)
                  + eff(L("effects", "punch", colors=[c, a], slots=[0, 1, 2, 3], decay=12)),
        # the held breath: hits on beats 1-3, beat 4 completely DARK while movers travel to the drop look
        "predrop": every("punch", colors=["white", c, a], slots=[0, 1, 2], decay=11, accent=0.95) + snap(1),
        "drop": every("punch", color="white", slots=[0], decay=3.2) + every("punch", colors=[a, b, c], slots=[1, 2, 3], decay=8, accent=0.9) + snap(1),
        "drive": wash(L("wash", "punch", colors=[a, b], slots=[0, 2], decay=6), L("wash", "punch", colors=[c, "white"], slots=[1, 3], decay=9, accent=0.8))
                 + mcol(b) + snap(2) + eff(L("effects", "punch", colors=[c, a, b], slots=[1, 3], decay=9), esnap(2, [[0, -40], [0, 40]])),
        "gap": wash(L("wash", "punch", color=a, slots=[0], decay=7, accent=0.4)) + mcol(cool, 0.3) + snap(4),
        "outro": wash(L("wash", "fade", colors=[a, cool], beats=16, spread=0.2, dimmer=0.4)) + mcol(cool, 0.25)
                 + [L(f.name, "sweep", shape="circle", pan=15, tilt=8, center=spots[f.name][0], beats=32) for f in movers]
                 + eff(L("effects", "fade", colors=[cool, b], beats=16, dimmer=0.25)),
    }
    if chill:
        cues["peak"] = wash(L("wash", "fade", colors=[a, b, c], beats=4, spread=0.25)) + mcol("white", 0.8) + snap(4) + eff(L("effects", "fade", colors=[c, a], beats=4, dimmer=0.6))
        cues["drive"] = cues["groove_b"]
    if S and strobe:
        for k in ("drop",): cues[k] = cues[k] + [L("strobes", "punch", color="white", slots=[0], decay=6)]
        if style == "hype": cues["peak"] = cues["peak"] + [L("strobes", "punch", color="white", slots=[0], decay=8)]
    if not (W or M or E): raise LookupError("this rig has no wash, mover or effect lights for a show (check `dj.py rig check`)")
    for name, layers in cues.items():
        if not layers: cues[name] = [L(lit[0], "blackout")]
        check_layers(cues[name], rig)
    return cues


def plan_bars(sm, style="club"):
    """Bar-by-bar cue names from the songmap sections (see module docstring)."""
    nb = sm["nbars"]; lab = ["groove"] * nb
    for s in sm["sections"]:
        for k in range(s["start_bar"] - 1, min(nb, s["end_bar"])): lab[k] = s["type"]
    out = [None] * nb; drops = [d - 1 for d in sm.get("drops", []) if 1 <= d <= nb]
    for k, t in enumerate(lab):
        out[k] = {"intro": "intro", "peak": "peak", "breakdown": "break", "outro": "outro", "drop": "drive", "build": "build1"}.get(t, "groove")
    for D in drops:                                                                   # grade the build by distance to the drop
        for off in range(1, 9):
            k = D - off
            if k < 0 or lab[k] not in ("build", "breakdown", "groove", "peak", "intro") or (lab[k] == "intro" and off > 2): break
            if off == 1: out[k] = "predrop"
            elif lab[k] == "build" or off <= 4: out[k] = "build3" if off == 2 else "build2" if off <= 4 else "build1"
        out[D] = "drop"
        for k in range(D + 1, min(nb, D + 8)):
            if lab[k] == "drop" and style == "hype": out[k] = "peak"
    run = 0
    for k in range(nb):                                                               # long drop stretches: drive / peak every 8 bars
        if out[k] == "drive": out[k] = "drive" if (run // 8) % 2 == 0 else "peak"; run += 1
        elif out[k] != "drop": run = 0
    run = 0
    for k in range(nb):                                                               # alternate groove looks every 8 bars, dip when quiet
        if out[k] == "groove":
            e = sm["bars"][k]["energy"]
            v = sm["bars"][k].get("vocal")
            out[k] = "dip" if e < 0.3 else "gap" if (v is not None and v < 0.08 and e < 0.6) else "groove_a" if (run // 8) % 2 == 0 else "groove_b"; run += 1
        else: run = 0
    return out


EDITS = Path(__file__).resolve().parent / "show_edits.json"         # DJ cue edits (user data): {song file: {rig: {bar: cue}}}


def get_edits(file, rig_name):
    try: d = json.loads(EDITS.read_text())
    except (FileNotFoundError, ValueError): return {}
    return {int(b): c for b, c in d.get(str(Path(file).expanduser().resolve()), {}).get(rig_name, {}).items()}


def set_edit(file, rig_name, start_bar, end_bar, cue=None):
    """Use `cue` for bars start_bar..end_bar of this song on this rig (None = back to the automatic plan); remembered."""
    from core.files import write_atomic
    if cue is not None and cue not in CUE_NAMES: raise LookupError(f"unknown cue {cue!r} (have {CUE_NAMES})")
    if not 1 <= start_bar <= end_bar: raise ValueError("bars must satisfy 1 <= start <= end")
    try: d = json.loads(EDITS.read_text())
    except (FileNotFoundError, ValueError): d = {}
    e = d.setdefault(str(Path(file).expanduser().resolve()), {}).setdefault(rig_name, {})
    for b in range(start_bar, end_bar + 1):
        if cue is None: e.pop(str(b), None)
        else: e[str(b)] = cue
    write_atomic(EDITS, json.dumps(d, indent=1) + "\n"); return {int(k): v for k, v in e.items()}


CUE_NAMES = ("intro", "groove_a", "groove_b", "dip", "gap", "build1", "build2", "build3", "predrop", "drop", "drive", "peak", "break", "outro")


def make_show(sm, rig, palette_name="auto", style="club", strobe=False, fog=False, mover_lead=0.5, mover_targets="air", edits="auto", motion=True):
    if style not in STYLES: raise LookupError(f"unknown style {style!r} (have {STYLES})")
    if not 0.25 <= mover_lead <= 2: raise ValueError("mover_lead must be 0.25..2 beats")
    rig = rig if isinstance(rig, Rig) else Rig.load(rig)
    aims, leads = {}, {}; pal = palette(palette_name, sm)
    cues = _cues(rig, pal, style, strobe, mover_lead, mover_targets, aims, sm["bpm"], leads); bars = plan_bars(sm, style)
    ed = get_edits(sm["file"], rig.name) if edits == "auto" else (edits or {})
    for b, c in ed.items():
        if 1 <= b <= len(bars) and c in cues: bars[b - 1] = c
    warn = [f"{n}: cannot keep its beam above the crowd's eyes from where it is rigged — aimed at the {u}" for n, u in aims.items() if mover_targets == "air" and u != "air"]
    warn += [f"{n}: slow head — starts moving {v:g} beats before each hit (set its real speed in the rig if this looks wrong)" for n, v in leads.items() if v > 1.0]
    sheet, last = [], None
    for k, c in enumerate(bars):
        if c != last: sheet.append([k + 1, c]); last = c
    used = {c for _, c in sheet}
    haze = sorted({max(1, d - 4) for d in sm.get("drops", [])}) if fog and _has(rig, "fog") else []
    show = {"bpm": sm["bpm"], "notes": f"auto show for {sm['title']} on rig {rig.name} ({style}; {len(sm.get('drops', []))} drop(s))",
            "cues": {k: v for k, v in cues.items() if k in used}, "songs": {SONG: {"bpm": sm["bpm"], "bars": sm["nbars"], "cues": sheet}},
            "meta": {"file": sm["file"], "title": sm["title"], "rig": rig.name, "style": style, "palette": palette_name, "colors": list(pal),
                     "strobe": strobe, "haze_bars": haze, "drops": sm.get("drops", []), "first_downbeat_s": sm["first_downbeat_s"],
                     "bar_plan": bars, "mover_lead": mover_lead, "mover_leads": leads, "mover_aims": aims, "edited_bars": sorted(ed), "warnings": warn}}
    if motion and leads:
        from lighting.motion import audit
        mo = show["meta"]["motion"] = audit(rig, show, sm); rig.home()
        warn += [f"{n}: lit while still travelling {v['lit_moving_pct']:g}% of its lit time (max {v['max_lag_deg']:g} deg behind) — slow it down: --mover-lead, or check its speed"
                 for n, v in mo.items() if v["lit_moving_pct"] > 10]
    return show


def live_show(rig, palette_name="auto", style="club", strobe=False, mover_lead=0.5, mover_targets="air", audio_react=True):
    """A show for live DJ sets: the same looks, cycled by a program on bar lines (beat clock / Link), pulse layers react to audio."""
    rig = rig if isinstance(rig, Rig) else Rig.load(rig)
    aims = {}; pal = palette(palette_name if palette_name != "auto" else "neon"); cues = _cues(rig, pal, style, strobe, mover_lead, mover_targets, aims, 128.0)
    for name in ("groove_a", "groove_b", "drive", "peak"):                            # kick pulses only when an audio feed exists:
        if not audio_react: break                                                       # without one they would hold the wash dark
        cues[name] = cues[name] + ([{"group": "wash", "effect": "pulse", "trigger": "onset", "decay": 6, "floor": 0.0, "react": "bass"}] if _has(rig, "wash") else [])
        check_layers(cues[name], rig)
    prog = [["intro", 8], ["groove_a", 16], ["groove_b", 16], ["build1", 4], ["build2", 2], ["build3", 1], ["predrop", 1], ["drop", 1], ["drive", 15],
            ["peak", 8], ["break", 8], ["groove_a", 16], ["groove_b", 16], ["outro", 8]]
    return {"bpm": 124, "notes": f"live program for rig {rig.name} ({style})", "cues": cues, "program": prog,
            "meta": {"rig": rig.name, "style": style, "palette": palette_name, "colors": list(pal), "mover_aims": aims}}


def save_show(show, path=None):
    from core.files import write_atomic
    if path is None:
        stem = "".join(ch if ch.isalnum() else "_" for ch in show["meta"].get("title", "show"))[:40]
        path = OUT / f"{stem}__{show['meta']['rig']}.json"
    write_atomic(Path(path), json.dumps(show, indent=1) + "\n"); return Path(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file"); ap.add_argument("--rig"); ap.add_argument("--palette", default="auto"); ap.add_argument("--style", default="club", choices=STYLES)
    ap.add_argument("--strobe", action="store_true"); ap.add_argument("--fog", action="store_true"); ap.add_argument("--mover-lead", type=float, default=0.5)
    ap.add_argument("--bpm", type=float); ap.add_argument("--out"); ap.add_argument("--preview", action="store_true")
    ap.add_argument("--mover-targets", default="air", choices=TARGETS)
    a = ap.parse_args()
    if a.file == "palettes":
        for k, v in PALETTES.items(): print(f"{k:10} " + "  ".join("#%02x%02x%02x" % tuple(int(x * 255) for x in c) for c in v))
        return
    if not a.rig: raise ValueError("--rig is required")
    from lighting.songmap import analyze, summary
    sm = analyze(a.file, a.bpm); rig = Rig.load(a.rig); show = make_show(sm, rig, a.palette, a.style, a.strobe, a.fog, a.mover_lead, a.mover_targets)
    for w in show["meta"]["warnings"]: print("note:", w)
    p = save_show(show, a.out); print(summary(sm)); print(f"show -> {p}")
    print(" ".join(f"{b}:{c}" for b, c in show["songs"][SONG]["cues"]))
    if a.preview:
        from lighting.show import preview_png
        png = p.with_suffix(".png"); preview_png(rig, show, sm["bpm"], png, fps=4, song=SONG); print(f"preview -> {png}")


if __name__ == "__main__":
    try: main()
    except (LookupError, ValueError, RuntimeError, FileNotFoundError) as e: print(f"autoshow: {e}", file=sys.stderr); sys.exit(1)
