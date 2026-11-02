"""DJ front door for the lights: set up your rig, analyse any music file, make a show, play it in sync, or run live from your DJ software.
CLI (python3 lighting/dj.py COMMAND ...; RIG defaults to the one chosen with `use`; --json on most commands for agents):
  setup                                   guided setup in the terminal (venue, each light, where it is, test)
  doctor [--json]                         what works here: ffmpeg, numpy, DMX dongle, rigs, vocals, rekordbox XML
  use RIG | settings [key=value ...]      default rig / remembered options (port, artnet, latency_ms, palette, style, master, ...)
  fixtures list | find WORDS [--ofl] | show NAME | import maker/fixture | new NAME --channels "dimmer,red,green,blue" [--type par]
  rig new|add|move|set|remove|rename|sheet|check|venues|places ...   (lighting/patch.py: place lights in plain words)
  test [RIG] [LIGHT] [--seconds 2]        identify lights one by one (all of them, or walk one light's channels)
  point [RIG] LIGHT TARGET [--seconds 10] aim a mover at dancefloor|dj|up|back|crowd|ceiling|x,y,z (check pan direction)
  analyze FILE|FOLDER ... [--bpm N] [--vocals] [--json]          tempo, beat grid, bars, drops, sections
  fix FILE [--shift 0.5] [--bar 1] [--bpm 128] [--clear]          correct a song's grid (remembered for that file)
  show FILE [--rig R] [--palette P] [--style club|chill|hype] [--strobe] [--fog] [--mover-lead B] [--mover-targets air|floor] [--preview]
  play FILE [show options] [--show SHOW.json] [--latency-ms 0] [--start-bar 1] [--console] [--no-audio] [--volume 1] [--seconds S]
  crate FOLDER [show options]             analyse + make shows for a whole folder (gig prep); summary table
  web [--port 8790] [--open] [--lan]      the web app: 3D stage, song timeline, rig editor, pads, exports (lighting/webapp.py)
  export FILE [--formats qlc,dmx,cues,midi,patch,native] [show options]   files for QLC+/DAWs/cue software (lighting/export.py)
  mcp                                     how to connect Claude/Codex/Cursor (MCP server lighting/mcp_server.py)
  live [--rig R] [--clock link|prodjlink|internal] [--audio rekordbox|djay|Music|Spotify] [--palette P] [--seconds S]
       live set: lights follow the DJ app's beat (Ableton Link on in rekordbox/djay/Traktor/Serato) + react to its audio;
       web console http://127.0.0.1:8779 (blackout, master, cues)
Output (all light commands): --port auto (USB-DMX dongle if plugged in, else simulation) | none (simulation) | /dev/cu.X;
  --artnet HOST for Art-Net/sACN-style Wi-Fi nodes and software visualisers.
Files: rigs lighting/rigs/*.json (patch.py), profiles lighting/profiles/*.json, settings lighting/dj_settings.json, grid
  fixes lighting/song_fixes.json; generated songmaps + shows + previews in out/lighting/.
Python: settings() -> dict · save_settings(**kv) · output(port, artnet) -> DMX output · play(file, rig, ...) -> summary dict ·
  identify(rig, light=None, seconds, out) · point(rig, light, target, seconds, out) · doctor() -> dict · crate(folder, rig, ...)
Fails: LookupError (unknown rig/light/profile/palette), ValueError (bad file/arguments), RuntimeError (missing ffmpeg/player);
  the CLI prints `dj: ...` and exits 1. Every light command blacks out the rig when it ends or on Ctrl-C.
Limits: one DMX universe; audio playback via afplay (macOS) / ffplay / aplay with no pause; lights follow the file's grid
  from the moment the player starts (use --latency-ms for Bluetooth/wireless speakers); see lighting/README for accuracy.
Depends on: lighting/{fixtures, patch, ofl, songmap, autoshow, show, clock, dmx, console}. Used by: lighting/SKILL.md (agents), the owner."""
import sys, os, json, time, shutil, signal, argparse, subprocess, threading, hashlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lighting.fixtures import Rig, PROFILES, RIGS, load_profile
from core.files import write_atomic

HERE = Path(__file__).resolve().parent
SETTINGS = HERE / "dj_settings.json"
OUT = HERE.parent / "out" / "lighting"
DEFAULTS = {"rig": None, "port": "auto", "artnet": None, "latency_ms": 0, "palette": "auto", "style": "club", "master": 1.0,
            "strobe": False, "fog": False, "mover_lead": 0.5, "mover_targets": "air", "rekordbox_xml": None, "http": 8779,
            "library": None}


# ---------- settings + output ----------
def settings():
    try: s = json.loads(SETTINGS.read_text())
    except (FileNotFoundError, ValueError): s = {}
    return {**DEFAULTS, **s}


def save_settings(**kv):
    s = settings()
    for k, v in kv.items():
        if k not in DEFAULTS: raise LookupError(f"unknown setting {k!r} (have {list(DEFAULTS)})")
        s[k] = v
    write_atomic(SETTINGS, json.dumps({k: v for k, v in s.items() if v != DEFAULTS[k]}, indent=1) + "\n"); return s


def _rig_name(name):
    name = name or settings()["rig"]
    if not name: raise LookupError("no rig given and no default: `dj.py rig new NAME` then `dj.py use NAME` (or `dj.py setup`)")
    return name


def load_rig(name=None): return Rig.load(_rig_name(name))


def output(port="auto", artnet=None):
    from lighting.dmx import AutoOutput, MemoryOutput, open_output
    if artnet: return open_output(artnet=artnet)
    if port in (None, "auto"): return AutoOutput()
    if port == "none": return MemoryOutput()
    return open_output(port)


def _out_name(o): return getattr(o, "mode", None) or (f"dongle {o.port}" if hasattr(o, "port") else type(o).__name__)


def _hold_frames(rig, out, seconds, fn=None):
    """Stream the rig's current values for `seconds` (fn(t) may change them each frame); blackout after."""
    from lighting.dmx import Universe, Sender
    u = Universe(); s = Sender(u, [out], fps=30).start(); t0 = time.monotonic()
    try:
        while (t := time.monotonic() - t0) < seconds:
            if fn: fn(t)
            rig.render(u); time.sleep(1 / 30)
    finally: s.stop(blackout=True)


# ---------- doctor ----------
def doctor():
    import importlib.util
    from lighting.dmx import find_ports
    r = {"python": sys.version.split()[0], "numpy": importlib.util.find_spec("numpy") is not None,
         "ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")),
         "audio_player": next((p for p in ("afplay", "ffplay", "aplay", "paplay") if shutil.which(p)), None),
         "dmx_ports": find_ports(), "rigs": sorted(p.stem for p in RIGS.glob("*.json")), "default_rig": settings()["rig"],
         "profiles": len(list(PROFILES.glob("*.json"))), "mcp_server": str(HERE / "mcp_server.py"), "web_app": str(HERE / "webapp.py")}
    from lighting.songmap import STEMS_FALLBACK
    sp = os.environ.get("LIGHTING_STEMS_PY") or (str(STEMS_FALLBACK) if STEMS_FALLBACK.exists() else None)
    r["vocals_python"] = sp; rx = settings()["rekordbox_xml"] or os.environ.get("LIGHTING_REKORDBOX_XML"); r["rekordbox_xml"] = rx
    if r["default_rig"]:
        from lighting.patch import load, check
        try: c = check(load(r["default_rig"])); r["rig_check"] = {"ok": c["ok"], "errors": c["errors"], "warnings": len(c["warnings"]), "kinds": c.get("kinds")}
        except LookupError as e: r["rig_check"] = {"ok": False, "errors": [str(e)]}
    tips = []
    if not r["ffmpeg"]: tips.append("install ffmpeg (brew install ffmpeg) to read music files")
    if not r["numpy"]: tips.append("pip install numpy (song analysis)")
    if not r["audio_player"]: tips.append("no audio player found: install ffmpeg (ffplay) to play songs")
    if not any(p.get("ftdi") for p in r["dmx_ports"]): tips.append("no USB-DMX dongle seen: shows run in simulation (plug in an FTDI Open-DMX dongle, or use --artnet HOST)")
    if not r["default_rig"]: tips.append("no default rig: `dj.py setup` or `dj.py rig new NAME` + `dj.py use NAME`")
    r["tips"] = tips; return r


# ---------- fixtures ----------
def find_profiles(words):
    ws = [w.lower() for w in words]; out = []
    for p in sorted(PROFILES.glob("*.json")):
        pr = load_profile(p.stem); text = f"{pr.name} {pr.d.get('maker', '')} {pr.d.get('model', '')} {pr.type} {pr.footprint()}ch".lower().replace("_", " ")
        if all(w in text for w in ws): out.append(pr)
    return out


# ---------- test / point ----------
def identify(rig, light=None, seconds=2.0, out=None, say=False):
    """All lights one at a time in address order (white, centred), or one light's channels step by step. Returns the steps."""
    out = out or output(); steps = []
    def speak(msg):
        print(msg, flush=True); steps.append(msg)
        if say and shutil.which("say"): subprocess.Popen(["say", msg.split(" (")[0]])
    if light is None:
        for f in sorted(rig.fixtures, key=lambda f: f.address):
            rig.home(); f.dimmer(1); f.color(1, 1, 1)
            speak(f"{f.name} ({f.profile.name}, address {f.address}-{f.last}, {f.where or 'no place set'})"); _hold_frames(rig, out, seconds)
        return steps
    f = rig[light]; plan = [("dimmer + white", lambda: (f.dimmer(1), f.color(1, 1, 1)))]
    for role, col in (("red", (1, 0, 0)), ("green", (0, 1, 0)), ("blue", (0, 0, 1))):
        if role in f.roles or "cell" in f.roles or "wheel" in f.roles: plan.append((role, lambda c=col: (f.dimmer(1), f.color(*c))))
    if "white" in f.roles: plan.append(("white only", lambda: (f.dimmer(1), f.color(1, 1, 1, w=1.0) if False else f.set(next(n for _, n, s in f.chans if s["role"] == "white"), 255))))
    for h in range(f.heads if f.heads > 1 else 0): plan.append((f"segment {h + 1} only", lambda h=h: (f.dimmer(1), f.color(1, 1, 1, head=h))))
    if "pan" in f.roles: plan += [("pan left", lambda: (f.dimmer(1), f.color(1, 1, 1), f.aim(-f.profile.pan_deg / 4, None))), ("pan right", lambda: (f.dimmer(1), f.color(1, 1, 1), f.aim(f.profile.pan_deg / 4, None)))]
    if "tilt" in f.roles: plan += [("tilt forward", lambda: (f.dimmer(1), f.color(1, 1, 1), f.aim(None, f.profile.tilt_deg / 4))), ("tilt back", lambda: (f.dimmer(1), f.color(1, 1, 1), f.aim(None, -f.profile.tilt_deg / 4)))]
    if "strobe" in f.roles: plan.append(("strobe slow", lambda: (f.dimmer(1), f.color(1, 1, 1), f.strobe(0.15))))
    for name, fn in plan:
        rig.home(); fn(); speak(f"{f.name}: {name}  dmx {f.address}..{f.last} = {f.values}"); _hold_frames(rig, out, seconds)
    return steps


def point(rig, light, target, seconds=10.0, out=None):
    from lighting.stage3d import aim_at
    from lighting.patch import target as named_target
    f = rig[light]
    if "pan" not in f.roles and "tilt" not in f.roles: raise ValueError(f"{light} cannot move (no pan/tilt)")
    pt = [float(x) for x in target.split(",")] if "," in target else named_target(target, rig.venue, f.pos)
    r = aim_at(f, pt)
    if r is None: raise ValueError(f"{light} cannot reach {target} {pt} from {list(f.pos)} ({f.mount}); move it or pick another target")
    rig.home(); f.dimmer(1); f.color(1, 1, 1); f.aim(*r)
    info = {"light": light, "target": target, "point": pt, "pan_deg": r[0], "tilt_deg": r[1], "dmx": f.values[:],
            "check": f"Is {light} hitting the {target}? If it swings to the mirror side: `dj.py rig set {rig.name} {light} invert_pan=true`; "
                     f"if right: `dj.py rig set {rig.name} {light} pan_checked=true`"}
    print(json.dumps(info), flush=True); _hold_frames(rig, out or output(), seconds); return info


# ---------- songs ----------
def _show_opts(a, s):
    g = lambda k: getattr(a, k, None)
    return dict(palette_name=g("palette") or s["palette"], style=g("style") or s["style"], strobe=bool(g("strobe")) or s["strobe"],
                fog=bool(g("fog")) or s["fog"], mover_lead=g("mover_lead") or s["mover_lead"], mover_targets=g("mover_targets") or s["mover_targets"])


def _analyze(path, bpm=None, vocals=False):
    from lighting.songmap import analyze
    return analyze(path, bpm=bpm, vocals=vocals, rekordbox_xml=settings()["rekordbox_xml"])


def _wav(sm, start_s):
    OUT.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(f"{sm['file']}|{start_s:.3f}".encode()).hexdigest()[:10]; w = OUT / "songs" / f"play-{h}.wav"
    if not w.exists():
        w.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start_s:.3f}", "-i", sm["file"], "-vn", "-ac", "2", "-ar", "44100", str(w)], capture_output=True, text=True)
        if r.returncode: raise ValueError(f"cannot prepare audio: {r.stderr.strip()[-200:]}")
    return w


def _player(wav, volume=1.0):
    if shutil.which("afplay"): return ["afplay", "-v", f"{volume:.2f}", str(wav)]
    if shutil.which("ffplay"): return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume", str(int(volume * 100)), str(wav)]
    for p in ("paplay", "aplay"):
        if shutil.which(p): return [p, str(wav)]
    raise RuntimeError("no audio player (afplay/ffplay/aplay): install ffmpeg, or use --no-audio")


def play(file, rig, show=None, latency_ms=0, start_bar=1, port="auto", artnet=None, master=1.0, console=False, http=8779,
         audio=True, seconds=None, verbose=True, volume=1.0, stop=None, on_start=None, out=None, rate=1.0, **show_opts):
    """Play a song file with its light show in sync. Returns {"frames", "dmx_errors", "output", "bars_played", ...}.
    stop: threading.Event that ends playback early; on_start(engine, songmap, show, player_process) is called once running
    (UIs/agents watch eng.state()); out: an already-open DMX output (else output(port, artnet))."""
    from lighting.songmap import beat_at, time_at
    from lighting.autoshow import make_show, SONG
    from lighting.show import Engine, _song_timeline, load_show
    from lighting.clock import FileClock
    sm = _analyze(file); rig = rig if isinstance(rig, Rig) else Rig.load(rig)
    show = load_show(str(show)) if isinstance(show, (str, Path)) else show or make_show(sm, rig, **show_opts)
    if SONG not in show.get("songs", {}): raise LookupError(f"show has no song sheet {SONG!r} (make it with `dj.py show FILE`)")
    if not 1 <= start_bar <= sm["nbars"]: raise ValueError(f"start bar must be 1..{sm['nbars']}")
    start_s = 0.0 if start_bar == 1 else max(0.0, time_at(sm, (start_bar - 1) * 4) - 60.0 / sm["bpm"])
    if rate != 1.0 and audio: raise ValueError("rate != 1 only without audio (simulation)")
    clk = FileClock(lambda ts: beat_at(sm, ts), sm["bpm"], offset_s=start_s - latency_ms / 1000.0, rate=rate)
    out = out or output(port, artnet); eng = Engine(rig, [out], clock=clk, fps=40); eng.master = master
    sheet = show["songs"][SONG]["cues"]; first = next((c for b, c in reversed(sheet) if b <= start_bar), sheet[0][1])
    eng.cue(show, first); tl, end = _song_timeline(show, SONG, 0.0); eng._start_timeline(show, tl, end, f"song:{sm['title']}")
    srv = None
    if console:
        from lighting.console import serve
        srv = serve(eng, show, sm["title"], http); threading.Thread(target=srv.serve_forever, daemon=True).start()
    haze = set(show.get("meta", {}).get("haze_bars", [])); proc = None; last_bar = None; bars = 0
    eng.start()
    try:
        if audio: proc = subprocess.Popen(_player(_wav(sm, start_s), volume), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        clk.start(); limit = seconds or (sm["duration_s"] - start_s + 1.0)
        if on_start: on_start(eng, sm, show, proc)
        if verbose: print(f"playing {sm['title']} — {sm['bpm']} BPM, {sm['nbars']} bars, output {_out_name(out)}" + (f", console http://127.0.0.1:{http}" if srv else ""), flush=True)
        while (clk.song_time() < sm["duration_s"] + 0.5 if proc is None and not seconds else time.monotonic() - clk.t0 < limit) \
                and (proc is None or proc.poll() is None) and not (stop and stop.is_set()):
            b = eng.beat(); bar = int(b // 4) + 1
            if bar != last_bar and bar >= 1:
                bars += 1; last_bar = bar
                if bar in haze and clk.rate == 1.0:
                    try: eng.haze_burst(6.0)
                    except (RuntimeError, LookupError): pass
                if verbose: print(f"  bar {bar:3d}/{sm['nbars']}  {next((c for b0, c in reversed(sheet) if b0 <= bar), sheet[0][1])}", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt: pass
    finally:
        if proc and proc.poll() is None: proc.terminate()
        eng.stop()
        if srv: srv.shutdown()
    snd = eng.sender
    return {"title": sm["title"], "bars_played": bars, "frames": snd.frames if snd else 0, "dmx_errors": snd.errors if snd else 0,
            "fps": round(snd.fps_actual, 1) if snd else 0, "output": _out_name(out), "error": eng.error}


def crate(folder, rig, **show_opts):
    from lighting.songmap import find_audio
    from lighting.autoshow import make_show, save_show
    rig = rig if isinstance(rig, Rig) else Rig.load(rig); rows = []
    for f in find_audio(folder):
        try:
            sm = _analyze(f); sh = make_show(sm, rig, **show_opts); p = save_show(sh)
            rows.append({"file": str(f), "title": sm["title"], "bpm": sm["bpm"], "bars": sm["nbars"], "drops": sm["drops"], "show": str(p),
                         "warnings": sh["meta"]["warnings"] + sm["notes"]})
        except (ValueError, RuntimeError) as e: rows.append({"file": str(f), "error": str(e)})
    return rows


def live(rig, clock="link", audio=None, port="auto", artnet=None, http=8779, seconds=None, stop=None, on_start=None, out=None, **show_opts):
    """Live set: lights follow the DJ app's beat (+ its audio). http=None: no console. stop/on_start/out as in play()."""
    from lighting.autoshow import live_show
    from lighting.show import Engine
    from lighting.console import serve
    rig = rig if isinstance(rig, Rig) else Rig.load(rig); opts = {k: v for k, v in show_opts.items() if k != "fog"}
    show = live_show(rig, audio_react=bool(audio), **opts)
    if clock == "link":
        from lighting.clock import LinkClock; clk = LinkClock(bpm=124)
    elif clock == "prodjlink":
        from lighting.prodjclock import ProDjLinkClock; clk = ProDjLinkClock(bpm=124)
    elif clock == "internal":
        from lighting.clock import InternalClock; clk = InternalClock(124)
    else: raise ValueError("clock must be link, prodjlink or internal")
    out = out or output(port, artnet); eng = Engine(rig, [out], clock=clk)
    if audio:
        from lighting.audiofeed import AudioFeed
        try: eng.audio = AudioFeed(audio).start()
        except LookupError as e: print(f"audio off ({e})", flush=True)
    eng.play_program(show, loops=1000); eng.start(); srv = serve(eng, show, "live", http) if http else None
    if srv: threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"live on rig {rig.name}: clock {clock}, output {_out_name(out)}" + (f", console http://127.0.0.1:{http}" if srv else "") + " (Ctrl-C to stop)", flush=True)
    if on_start: on_start(eng, show)
    t0 = time.monotonic()
    try:
        while (seconds is None or time.monotonic() - t0 < seconds) and not (stop and stop.is_set()): time.sleep(0.2)
    except KeyboardInterrupt: pass
    finally:
        eng.stop()
        if srv: srv.shutdown()
    return {"output": _out_name(out), "frames": eng.sender.frames if eng.sender else 0}


# ---------- guided setup ----------
def setup():
    from lighting import patch as P
    if not sys.stdin.isatty():
        print("setup is interactive. Without a terminal use: dj.py rig new NAME --venue mobile; dj.py rig add NAME PROFILE --count N "
              "--where '...'; dj.py use NAME; dj.py test"); return
    ask = lambda q, d="": (input(f"{q}{f' [{d}]' if d else ''}: ").strip() or d)
    name = ask("Name for this rig (e.g. wedding_kit)", "myrig")
    print("Venues: " + ", ".join(f"{k} ({v['about']})" for k, v in P.VENUES.items()))
    venue = ask("Venue type", "mobile"); truss = ask("Is there a truss / goalpost over the booth? y/n", "n").lower().startswith("y")
    try: d = P.new_rig(name, venue, truss)
    except FileExistsError:
        if not ask(f"Rig {name} exists. Add to it? y/n", "y").lower().startswith("y"): return
        d = P.load(name)
    print("\nNow each kind of light. Describe it (e.g. 'rgbw par 8ch', 'moving head', 'led bar'), 'ofl chauvet slimpar' to search the"
          " Open Fixture Library, 'new' to type the channel list from its manual, or press Enter when done.")
    while True:
        q = ask("\nLight")
        if not q: break
        prof = None
        if q.lower().startswith("ofl "):
            from lighting.ofl import search, import_fixture
            hits = search(q[4:])
            for i, h in enumerate(hits[:12], 1): print(f"  {i}. {h}")
            k = ask("Number", "1") if hits else ""
            if hits and k.isdigit() and 1 <= int(k) <= len(hits): _, pd = import_fixture(hits[int(k) - 1], force=True); prof = pd["name"]
        elif q.lower() == "new":
            nm = ask("Profile name (lowercase_with_underscores)"); ch = ask("Channels in order, comma separated (e.g. dimmer,red,green,blue,strobe,mode)")
            ty = ask("Type: par, bar, moving_head, spider, strobe, haze", "par"); prof = P.make_profile(nm, ch, ty, force=True)[1]["name"]
        else:
            hits = find_profiles(q.split()) or find_profiles(q.split()[:1])
            for i, pr in enumerate(hits[:12], 1): print(f"  {i}. {pr.name:30} {pr.footprint():2d}ch  {pr.d.get('model', '')}")
            k = ask("Number (or Enter to skip)", "1" if hits else "")
            if hits and k.isdigit() and 1 <= int(k) <= len(hits): prof = hits[int(k) - 1].name
        if not prof: print("  skipped"); continue
        cnt = int(ask("How many", "1")); where = ask("Where (e.g. 'floor behind the dj', 'on stands either side', 'truss')", "on stands either side")
        try: print("  added", ", ".join(P.add(d, prof, cnt, where))); P.save(d)
        except (ValueError, LookupError) as e: print("  could not add:", e)
    print("\n" + P.sheet(d)); save_settings(rig=name); print(f"\n{name} is now your default rig.")
    c = P.check(d)
    for w in c["warnings"][:8]: print("check:", w)
    if ask("Flash each light once now to check addresses? y/n", "y").lower().startswith("y"): identify(Rig.load(name), None, 1.5)
    print("Next: dj.py show SONG.mp3 --preview, then dj.py play SONG.mp3")


# ---------- CLI ----------
def _add_out(p):
    p.add_argument("--port"); p.add_argument("--artnet")


def _add_show(p):
    p.add_argument("--rig"); p.add_argument("--palette"); p.add_argument("--style", choices=("club", "chill", "hype")); p.add_argument("--strobe", action="store_true")
    p.add_argument("--fog", action="store_true"); p.add_argument("--mover-lead", type=float); p.add_argument("--mover-targets", choices=("air", "floor"))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "rig":
        from lighting import patch
        sys.argv = ["dj.py rig"] + argv[1:]; return patch.main()
    ap = argparse.ArgumentParser(prog="dj.py", description=__doc__.splitlines()[0]); sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("setup"); d = sp.add_parser("doctor"); d.add_argument("--json", action="store_true")
    u = sp.add_parser("use"); u.add_argument("rig")
    st = sp.add_parser("settings"); st.add_argument("kv", nargs="*")
    fx = sp.add_parser("fixtures"); fx.add_argument("action", choices=("list", "find", "show", "import", "new")); fx.add_argument("args", nargs="*")
    fx.add_argument("--ofl", action="store_true"); fx.add_argument("--channels"); fx.add_argument("--type", default="par"); fx.add_argument("--name")
    fx.add_argument("--pan-deg", type=float); fx.add_argument("--tilt-deg", type=float); fx.add_argument("--force", action="store_true"); fx.add_argument("--json", action="store_true")
    sp.add_parser("rig", help="rig editing: see `dj.py rig -h`")
    t = sp.add_parser("test"); t.add_argument("names", nargs="*"); t.add_argument("--seconds", type=float, default=2.0); t.add_argument("--say", action="store_true"); _add_out(t)
    pt = sp.add_parser("point"); pt.add_argument("names", nargs="+"); pt.add_argument("--seconds", type=float, default=10.0); _add_out(pt)
    an = sp.add_parser("analyze"); an.add_argument("paths", nargs="+"); an.add_argument("--bpm", type=float); an.add_argument("--vocals", action="store_true"); an.add_argument("--json", action="store_true")
    fi = sp.add_parser("fix"); fi.add_argument("file"); fi.add_argument("--shift", type=float); fi.add_argument("--bar", type=int); fi.add_argument("--bpm", type=float); fi.add_argument("--clear", action="store_true")
    sh = sp.add_parser("show"); sh.add_argument("file"); _add_show(sh); sh.add_argument("--preview", action="store_true"); sh.add_argument("--json", action="store_true"); sh.add_argument("--out")
    pl = sp.add_parser("play"); pl.add_argument("file"); _add_show(pl); _add_out(pl); pl.add_argument("--show"); pl.add_argument("--latency-ms", type=float)
    pl.add_argument("--start-bar", type=int, default=1); pl.add_argument("--console", action="store_true"); pl.add_argument("--no-audio", action="store_true")
    pl.add_argument("--seconds", type=float); pl.add_argument("--master", type=float); pl.add_argument("--json", action="store_true")
    pl.add_argument("--volume", type=float, default=1.0, help="audio volume 0..1 (afplay/ffplay)")
    cr = sp.add_parser("crate"); cr.add_argument("folder"); _add_show(cr); cr.add_argument("--json", action="store_true")
    wb = sp.add_parser("web"); wb.add_argument("--port", type=int, default=8790); wb.add_argument("--open", action="store_true"); wb.add_argument("--lan", action="store_true")
    ex = sp.add_parser("export"); ex.add_argument("file"); _add_show(ex); ex.add_argument("--formats", default="qlc,dmx,cues,midi,patch,native"); ex.add_argument("--fps", type=int, default=30)
    sp.add_parser("mcp")
    lv = sp.add_parser("live"); lv.add_argument("--rig"); lv.add_argument("--clock", default="link", choices=("link", "prodjlink", "internal")); lv.add_argument("--audio")
    lv.add_argument("--palette"); lv.add_argument("--style", choices=("club", "chill", "hype")); lv.add_argument("--strobe", action="store_true"); lv.add_argument("--seconds", type=float); _add_out(lv)
    a = ap.parse_args(argv); s = settings()
    port = getattr(a, "port", None) or s["port"]; artnet = getattr(a, "artnet", None) or s["artnet"]
    if a.cmd == "setup":
        try: return setup()
        except (EOFError, KeyboardInterrupt): print("\nsetup stopped — lights added so far are saved (`dj.py rig sheet RIG`)"); return
    if a.cmd == "doctor":
        r = doctor()
        if a.json: print(json.dumps(r, indent=1)); return
        for k, v in r.items():
            if k != "tips": print(f"{k:14} {v}")
        for tip in r["tips"]: print("tip:", tip)
        return
    if a.cmd == "web":
        sys.argv = ["webapp.py", "--port", str(a.port)] + (["--open"] if a.open else []) + (["--lan"] if a.lan else [])
        from lighting.webapp import main as wmain; return wmain()
    if a.cmd == "mcp":
        m = HERE / "mcp_server.py"
        print(f"Claude Code:    claude mcp add --scope user dj-lights -- python3 {m}\nCodex:          ~/.codex/config.toml\n"
              f"                [mcp_servers.dj-lights]\n                command = \"python3\"\n                args = [\"{m}\"]\n"
              f"Claude Desktop: \"mcpServers\": {{\"dj-lights\": {{\"command\": \"python3\", \"args\": [\"{m}\"]}}}}"); return
    if a.cmd == "use": load_rig(a.rig); save_settings(rig=a.rig); print(f"default rig: {a.rig}"); return
    if a.cmd == "settings":
        kv = {}
        for item in a.kv:
            k, v = item.split("=", 1)
            kv[k] = None if v in ("", "none", "null") else (v.lower() == "true") if v.lower() in ("true", "false") else float(v) if v.replace(".", "", 1).lstrip("-").isdigit() else v
        print(json.dumps(save_settings(**kv) if kv else settings(), indent=1)); return
    if a.cmd == "fixtures":
        if a.action == "list" or (a.action == "find" and not a.args):
            rows = [(pr.name, pr.status, pr.type, pr.footprint(), pr.d.get("model", "")) for pr in find_profiles([])]
        if a.action in ("list", "find") and not a.ofl:
            rows = [(pr.name, pr.status, pr.type, pr.footprint(), pr.d.get("model", "")) for pr in find_profiles(a.args)]
            if a.json: print(json.dumps([dict(zip(("name", "status", "type", "channels", "model"), r)) for r in rows], indent=1)); return
            for r in rows: print(f"{r[0]:32} {r[1]:9} {r[2]:12} {r[3]:3d}ch  {r[4]}")
            if a.action == "find" and not rows: print("no local match: try `dj.py fixtures find WORDS --ofl` (Open Fixture Library) or `fixtures new`")
            return
        if a.action == "find":
            from lighting.ofl import search
            hits = search(a.args); print("\n".join(hits) if hits else "no match"); return
        if a.action == "show":
            from lighting.fixtures import main as fmain
            sys.argv = ["fixtures.py", "show", a.args[0]]; return fmain()
        if a.action == "import":
            from lighting.ofl import import_fixture
            dest, pd = import_fixture(a.args[0], a.name, a.force)
            print(f"saved {dest.name}: {pd['type']}, modes " + ", ".join(f"{m} ({len(c)}ch)" for m, c in pd["modes"].items()) + f"; default {pd['default_mode']}"); return
        if a.action == "new":
            from lighting.patch import make_profile
            if not (a.args and a.channels): raise ValueError('usage: dj.py fixtures new NAME --channels "dimmer,red,green,blue" [--type par]')
            dest, pd = make_profile(a.args[0], a.channels, a.type, a.pan_deg, a.tilt_deg, force=a.force)
            print(f"saved {dest.name}: " + ", ".join(f"{n}={c['role']}" for n, c in pd["channels"].items())); return
    if a.cmd == "test":
        rig_n, light = (a.names + [None, None])[:2] if a.names and (RIGS / f"{a.names[0]}.json").exists() else (None, a.names[0] if a.names else None)
        rig = load_rig(rig_n); identify(rig, light, a.seconds, output(port, artnet), a.say); return
    if a.cmd == "point":
        names = a.names if len(a.names) == 3 else [None] + a.names
        if len(names) != 3: raise ValueError("usage: dj.py point [RIG] LIGHT TARGET")
        point(load_rig(names[0]), names[1], names[2], a.seconds, output(port, artnet)); return
    if a.cmd == "analyze":
        from lighting.songmap import find_audio, summary
        files = [f for p in a.paths for f in (find_audio(p) if Path(p).is_dir() else [Path(p)])]
        for f in files:
            try: sm = _analyze(f, a.bpm, a.vocals)
            except (ValueError, FileNotFoundError) as e: print(f"skip {f}: {e}", file=sys.stderr); continue
            print(json.dumps(sm) if a.json else summary(sm))
            if not a.json:
                for n in sm["notes"]: print("   note:", n)
        return
    if a.cmd == "fix":
        from lighting.songmap import set_fix, summary
        f = set_fix(a.file, a.shift, a.bar, a.bpm, a.clear); print(f"fix for {Path(a.file).name}: {f or 'none'}")
        print(summary(_analyze(a.file))); return
    opts = _show_opts(a, s)
    if a.cmd == "show":
        from lighting.autoshow import make_show, save_show
        from lighting.songmap import summary
        rig = load_rig(a.rig); sm = _analyze(a.file); show = make_show(sm, rig, **opts); p = save_show(show, a.out)
        res = {"show": str(p), "song": summary(sm), "plan": show["songs"]["song"]["cues"], "warnings": show["meta"]["warnings"] + sm["notes"]}
        if a.preview:
            from lighting.show import preview_png
            res["preview"] = str(preview_png(rig, show, sm["bpm"], p.with_suffix(".png"), fps=4, song="song"))
        if a.json: print(json.dumps(res, indent=1)); return
        print(res["song"]); print("show ->", res["show"]); print(" ".join(f"{b}:{c}" for b, c in res["plan"]))
        for w in res["warnings"]: print("note:", w)
        if "preview" in res: print("preview ->", res["preview"])
        return
    if a.cmd == "play":
        r = play(a.file, load_rig(a.rig), a.show, a.latency_ms if a.latency_ms is not None else s["latency_ms"], a.start_bar, port, artnet,
                 a.master if a.master is not None else s["master"], a.console, s["http"], not a.no_audio, a.seconds, not a.json, a.volume, **opts)
        print(json.dumps(r) if a.json else f"done: {r['bars_played']} bars, {r['frames']} DMX frames ({r['fps']} fps) to {r['output']}" + (f", error {r['error']}" if r["error"] else "")); return
    if a.cmd == "export":
        from lighting.export import export
        r = export(a.file, load_rig(a.rig), [f.strip() for f in a.formats.split(",") if f.strip()], a.fps, **opts)
        print(f"exported {r['frames']} frames, {r['cues']} cues -> {r['zip']}"); [print(f"  {k}: " + ", ".join(Path(p).name for p in v)) for k, v in r["files"].items()]; return
    if a.cmd == "crate":
        rows = crate(a.folder, load_rig(a.rig), **opts)
        if a.json: print(json.dumps(rows, indent=1)); return
        for r in rows: print(f"{r.get('title', Path(r['file']).name)[:44]:44} " + (f"{r['bpm']:6.1f} BPM {r['bars']:3d} bars drops {r['drops']}" if "bpm" in r else f"ERROR {r['error']}"))
        return
    if a.cmd == "live":
        live(load_rig(a.rig), a.clock, a.audio, port, artnet, s["http"], a.seconds, **opts); return


if __name__ == "__main__":
    try: main()
    except (LookupError, ValueError, RuntimeError, FileExistsError, FileNotFoundError) as e: print(f"dj: {e}", file=sys.stderr); sys.exit(1)
