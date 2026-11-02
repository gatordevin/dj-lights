"""MCP server for the DJ lights: any chat assistant (Claude Desktop/Code, Codex, Cursor...) can patch a rig, analyse songs, make, play, export shows.
CLI:
  python3 lighting/mcp_server.py                 stdio MCP server (newline-delimited JSON-RPC 2.0); register it with your client:
      claude mcp add --scope user dj-lights -- python3 /ABS/PATH/lighting/mcp_server.py
      Codex ~/.codex/config.toml:  [mcp_servers.dj-lights]  command = "python3"  args = ["/ABS/PATH/lighting/mcp_server.py"]
      Claude Desktop claude_desktop_config.json: "mcpServers": {"dj-lights": {"command": "python3", "args": ["/ABS/PATH/lighting/mcp_server.py"]}}
  python3 lighting/mcp_server.py --list           print the tools (no server)
Tools (TOOLS below; every result is JSON text, previews/3D are PNG images): lights_status, list_rigs, rig_details, create_rig,
  add_lights, edit_light, set_venue, remove_lights, edit_cue, simulate_song, seek, set_sim_speed, find_fixtures, import_fixture, create_profile, analyze_song, fix_grid, make_show,
  preview_show, stage_snapshot, play_song, go_live, manual_mode, trigger_cue, release_cue, set_master, stop_lights,
  test_lights, point_light, export_show, open_web_app, settings.
Protocol: initialize (echoes a supported protocolVersion, returns "instructions"), tools/list, tools/call, ping; notifications
  are ignored. Tool failures come back as isError results with the reason; protocol errors as JSON-RPC errors.
Safety: light-driving tools (play_song, go_live, manual_mode, test_lights, point_light) drive REAL lights when a USB-DMX dongle
  is plugged in or Art-Net is set (lights_status says which); output "none" in settings forces simulation.
Limits: one engine at a time (Session); stdout is reserved for the protocol (library prints go to stderr); the web app runs
  inside this process when opened (it stops when the client closes the server).
Depends on: lighting/{session, dj, patch, ofl, songmap, autoshow, export, show, stage3d, webapp}. Used by: chat clients (see lighting/SKILL.md)."""
import sys, json, base64, traceback, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = """DJ lights: DMX light shows that follow music, for any rig.
Start with lights_status (is a USB-DMX dongle connected? default rig?). No rig yet: ask what lights the user has and where they are,
then create_rig + add_lights (places in plain words: "floor behind the dj", "on stands either side", "truss"; left/right as the crowd
sees it). find_fixtures/import_fixture/create_profile cover any light. rig_details shows addresses + DIP switches to dial in.
Songs: analyze_song (any audio/video file) -> make_show -> preview_show (look at it) -> simulate_song (fast check) -> play_song.
Change a section's look with edit_cue. Moving heads: set measured pan_speed/tilt_speed with edit_light; make_show reports motion. Off-beat lights: fix_grid
(shift 0.5 / bar 1 / bpm). Live DJ sets: go_live (Ableton Link from rekordbox/djay). export_show writes QLC+/CSV/MIDI files.
open_web_app gives the user a visual UI (3D stage, song timeline, rig editor, triggers). Strobe/fog only when asked; lasers never.
Ask before driving real lights (dongle connected) unless the user asked for it."""


def _rig_info(rig):
    from lighting import patch as P
    d = P.load(rig); c = P.check(d); return {"sheet": P.sheet(d), "check": c, "venue": d.get("venue", {}).get("template")}


def _summary(sm):
    return {k: sm[k] for k in ("title", "artist", "file", "duration_s", "bpm", "bpm_confidence", "constant_tempo", "first_downbeat_s",
                               "downbeat_confidence", "grid_source", "nbars", "sections", "drops", "notes") if k in sm} | \
           {"energy_per_bar": [b["energy"] for b in sm["bars"]], "has_vocals": any("vocal" in b for b in sm["bars"])}


class Tools:
    def __init__(self):
        from lighting.session import Session
        self.s = Session(); self.web = None

    def lights_status(self):
        from lighting import dj
        d = dj.doctor(); return {"doctor": d, "running": self.s.status(), "web_app": self.web}

    def list_rigs(self):
        from lighting.fixtures import RIGS
        from lighting import dj
        return {"rigs": sorted(p.stem for p in RIGS.glob("*.json")), "default": dj.settings()["rig"]}

    def rig_details(self, rig=None):
        from lighting import dj
        return _rig_info(dj._rig_name(rig))

    def create_rig(self, name, venue="mobile", truss=False, room=None, make_default=True):
        from lighting import patch as P, dj
        P.new_rig(name, venue, truss, room)
        if make_default: dj.save_settings(rig=name)
        return _rig_info(name)

    def add_lights(self, profile, count=1, where="", rig=None, name=None, address="auto", mode=None, aim=None):
        from lighting import patch as P, dj
        d = P.load(dj._rig_name(rig)); names = P.add(d, profile, count, where, name, address, False, mode, aim); P.save(d)
        return {"added": names, **_rig_info(d["_path"])}

    def edit_light(self, light, rig=None, where=None, address=None, pos=None, yaw=None, pitch=None, roll=None, mode=None, invert_pan=None,
                   pan_checked=None, aim=None, rename=None, mount=None, pan_speed=None, tilt_speed=None):
        from lighting import patch as P, dj
        d = P.load(dj._rig_name(rig))
        if where: P.move(d, [light], where)
        kv = {k: v for k, v in dict(address=address, yaw=yaw, pitch=pitch, roll=roll, mode=mode, invert_pan=invert_pan, pan_checked=pan_checked, aim=aim,
                                    mount=mount, pan_speed=pan_speed, tilt_speed=tilt_speed).items() if v is not None}
        if pos is not None: kv["pos"] = ",".join(map(str, pos))
        if kv: P.set_fields(d, light, **kv)
        if rename: P.rename(d, light, rename)
        P.save(d); self.s.refresh_rig(d["name"]); return _rig_info(d["_path"])

    def set_venue(self, rig=None, room=None, deck=None, truss=None, truss_height=None):
        from lighting import patch as P, dj
        d = P.load(dj._rig_name(rig)); P.set_venue(d, room, deck, truss, truss_height); P.save(d); self.s.refresh_rig(d["name"]); return _rig_info(d["_path"])

    def remove_lights(self, lights, rig=None):
        from lighting import patch as P, dj
        d = P.load(dj._rig_name(rig)); P.remove(d, lights); P.save(d); return _rig_info(d["_path"])

    def find_fixtures(self, query="", ofl=False):
        from lighting import dj
        if ofl:
            from lighting.ofl import search
            return {"ofl_keys": search(query), "next": "import_fixture(key)"}
        return {"profiles": [{"name": p.name, "status": p.status, "type": p.type, "channels": p.footprint(), "model": p.d.get("model", "")} for p in dj.find_profiles(query.split())]}

    def import_fixture(self, key, name=None, force=False):
        from lighting.ofl import import_fixture
        dest, pd = import_fixture(key, name, force)
        return {"profile": pd["name"], "type": pd["type"], "modes": {m: len(c) for m, c in pd["modes"].items()}, "default_mode": pd["default_mode"], "notes": pd["notes"]}

    def create_profile(self, name, channels, type="par", pan_deg=None, tilt_deg=None, force=False):
        from lighting.patch import make_profile
        dest, pd = make_profile(name, channels, type, pan_deg, tilt_deg, force=force)
        return {"profile": pd["name"], "channels": {n: c["role"] for n, c in pd["channels"].items()}}

    def analyze_song(self, file, bpm=None, vocals=False):
        from lighting import dj
        return _summary(dj._analyze(file, bpm, vocals))

    def fix_grid(self, file, shift=None, bar=None, bpm=None, clear=False):
        from lighting.songmap import set_fix
        from lighting import dj
        f = set_fix(file, shift, bar, bpm, clear); return {"fix": f, "song": _summary(dj._analyze(file))}

    def make_show(self, file, rig=None, palette="auto", style="club", strobe=False, fog=False, mover_targets="air", mover_lead=0.5):
        from lighting import dj
        from lighting.autoshow import make_show, save_show
        rig_o = dj.load_rig(rig); sm = dj._analyze(file)
        sh = make_show(sm, rig_o, palette, style, strobe, fog, mover_lead, mover_targets); p = save_show(sh)
        return {"show": str(p), "plan": sh["songs"]["song"]["cues"], "warnings": sh["meta"]["warnings"] + sm["notes"], "colors": sh["meta"]["colors"],
                "edited_bars": sh["meta"]["edited_bars"], "mover_leads_beats": sh["meta"].get("mover_leads"), "motion": sh["meta"].get("motion")}

    def edit_cue(self, file, start_bar, end_bar, cue=None, rig=None):
        from lighting.autoshow import set_edit
        from lighting import dj
        set_edit(file, dj.load_rig(rig).name, start_bar, end_bar, cue); return self.make_show(file, rig)

    def simulate_song(self, file, rig=None, rate=4.0, start_bar=1, palette=None, style=None):
        return self.s.simulate(file, rig, rate, start_bar, palette_name=palette, style=style)

    def seek(self, bar): return self.s.seek(bar)
    def set_sim_speed(self, rate): return self.s.set_rate(rate)

    def preview_show(self, file, rig=None, palette="auto", style="club", strobe=False, fog=False):
        from lighting import dj
        from lighting.autoshow import make_show, save_show
        from lighting.show import preview_png
        rig_o = dj.load_rig(rig); sm = dj._analyze(file); sh = make_show(sm, rig_o, palette, style, strobe, fog); p = save_show(sh)
        png = preview_png(rig_o, sh, sm["bpm"], p.with_suffix(".png"), fps=4, song="song")
        return {"_image": str(png), "text": {"preview": str(png), "how_to_read": "one row per light (colour over time, left to right = whole song); movers add a pan/tilt trace; red ticks = bars",
                                            "plan": sh["songs"]["song"]["cues"]}}

    def stage_snapshot(self, rig=None, view="foh", file=None, bar=None):
        """3D render of the rig: at rest, mid-show (file + bar), or what is running now."""
        from lighting import dj
        from lighting.stage3d import snapshot
        from lighting.show import Engine, _song_timeline
        from lighting.clock import ManualClock
        out = Path(dj.OUT) / f"snap_{view}.png"
        st = self.s.status()
        if file:
            from lighting.autoshow import make_show
            rig_o = dj.load_rig(rig); sm = dj._analyze(file); sh = make_show(sm, rig_o)
            clk = ManualClock(sm["bpm"]); eng = Engine(rig_o, clock=clk); tl, end = _song_timeline(sh, "song", 0.0)
            eng.cue(sh, sh["songs"]["song"]["cues"][0][1]); eng._start_timeline(sh, tl, end, "song")
            clk.value = ((bar or 1) - 1) * 4 + 0.05; eng.frame(); r = rig_o
        elif st["mode"] != "idle" and self.s.eng: r = self.s.eng.rig
        else: r = dj.load_rig(rig); r.home()
        snapshot(r, out, view=view)
        return {"_image": str(out), "text": {"view": view, "views": ["foh", "wide", "top", "side", "dj", "crowd", "truss", "photo"]}}

    def play_song(self, file, rig=None, audio=True, start_bar=1, volume=1.0, palette=None, style=None, strobe=None, fog=None):
        return self.s.play(file, rig, audio, start_bar, volume, palette_name=palette, style=style, strobe=strobe, fog=fog)

    def go_live(self, rig=None, clock="link", audio_app=None, palette=None, style=None):
        return self.s.live(rig, clock, audio_app, palette_name=palette, style=style)

    def manual_mode(self, rig=None, bpm=124, palette=None):
        return self.s.manual(rig, bpm, palette_name=palette or "neon")

    def trigger_cue(self, cue): return self.s.trigger(cue)
    def release_cue(self): return self.s.release()

    def set_master(self, master=None, blackout=None):
        if master is not None: self.s.set_master(master)
        if blackout is not None: self.s.set_blackout(blackout)
        return self.s.status()

    def stop_lights(self): return self.s.stop()
    def test_lights(self, rig=None, light=None, seconds=1.5): return self.s.test(rig, light, seconds)
    def point_light(self, light, target, rig=None, seconds=8.0): return self.s.point(rig, light, target, seconds)

    def export_show(self, file, rig=None, formats=None, palette="auto", style="club", strobe=False, fog=False):
        from lighting.export import export, FORMATS
        from lighting import dj
        return export(file, dj.load_rig(rig), formats or list(FORMATS), palette_name=palette, style=style, strobe=strobe, fog=fog)

    def open_web_app(self, port=8790, open_browser=False):
        if not self.web:
            from lighting.webapp import serve
            srv = serve(self.s, port); threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.web = f"http://127.0.0.1:{srv.server_address[1]}"
        if open_browser:
            import subprocess; subprocess.Popen(["open", self.web])
        return {"url": self.web}

    def settings(self, set=None):
        from lighting import dj
        return dj.save_settings(**set) if set else dj.settings()


S, I, B, N, A, O = ({"type": "string"}, {"type": "integer"}, {"type": "boolean"}, {"type": "number"}, {"type": "array", "items": {"type": "string"}}, {"type": "object"})
RIG = {"type": "string", "description": "rig name (default: the default rig)"}
FILE = {"type": "string", "description": "absolute path of a music or video file (mp3, wav, flac, m4a, aiff, ogg, opus, mp4, mov, mkv, webm)"}
SHOWOPT = {"palette": {"type": "string", "description": "auto | neon sunset ice fire forest royal halloween uf candy mono miami gold | colours 'red,#00ffaa,white'"},
           "style": {"type": "string", "enum": ["club", "chill", "hype"]}, "strobe": B, "fog": B}
TOOLS = {
    "lights_status": ("What works here (ffmpeg, DMX dongle or simulation, rigs, default rig) and what is running now.", {}, []),
    "list_rigs": ("List saved rigs and the default one.", {}, []),
    "rig_details": ("A rig's address sheet (address, DIP switches, where each light is) and its check (kinds, warnings).", {"rig": RIG}, []),
    "create_rig": ("Create a new rig from a venue template (mobile, club, stage, backyard, living_room) and make it the default.",
                   {"name": S, "venue": {"type": "string", "enum": ["mobile", "club", "stage", "backyard", "living_room"]}, "truss": B,
                    "room": {"type": "array", "items": N, "description": "[width, depth, ceiling] metres"}, "make_default": B}, ["name"]),
    "add_lights": ("Add lights to a rig, placed in plain words ('floor behind the dj', '2 on stands either side', 'truss', 'over the crowd', '8ft'); "
                   "left/right as the crowd sees it. Addresses are automatic (10-channel steps).",
                   {"profile": {"type": "string", "description": "profile name from find_fixtures"}, "count": I, "where": S, "rig": RIG, "name": S,
                    "address": {"type": ["integer", "string"]}, "mode": S, "aim": {"type": "string", "enum": ["dancefloor", "dj", "up", "back", "crowd", "ceiling"]}}, ["profile"]),
    "edit_light": ("Change one light: move it (where), address, exact pos [x,y,z] m, yaw/pitch/roll, mount floor|hung, mode, invert_pan / pan_checked after "
                   "point_light, aim target, rename, pan_speed/tilt_speed deg/s (measured: degrees of travel / seconds; shows plan moves from it).",
                   {"light": S, "rig": RIG, "where": S, "address": I, "pos": {"type": "array", "items": N}, "yaw": N, "pitch": N, "roll": N, "mode": S,
                    "mount": {"type": "string", "enum": ["floor", "hung"]}, "invert_pan": B, "pan_checked": B, "aim": S, "rename": S, "pan_speed": N, "tilt_speed": N}, ["light"]),
    "remove_lights": ("Remove lights from a rig.", {"lights": A, "rig": RIG}, ["lights"]),
    "set_venue": ("Resize the venue: room [width, depth, ceiling] m, DJ booth/riser deck [w, d, h] m, truss on/off and height.",
                  {"rig": RIG, "room": {"type": "array", "items": N}, "deck": {"type": "array", "items": N}, "truss": B, "truss_height": N}, []),
    "find_fixtures": ("Find light profiles by words (e.g. 'rgbw par 8ch', 'moving head'); ofl=true searches the Open Fixture Library (all brands).",
                      {"query": S, "ofl": B}, []),
    "import_fixture": ("Import an Open Fixture Library fixture (key 'maker/fixture' from find_fixtures ofl=true) as a profile.", {"key": S, "name": S, "force": B}, ["key"]),
    "create_profile": ("Make a profile from the light manual's channel list in DMX order, e.g. 'dimmer,red,green,blue,strobe,mode,speed' or 'dimmer,4x(r,g,b)'.",
                       {"name": S, "channels": S, "type": {"type": "string", "enum": ["par", "bar", "moving_head", "spider", "strobe", "haze", "effect"]},
                        "pan_deg": N, "tilt_deg": N, "force": B}, ["name", "channels"]),
    "analyze_song": ("Analyse a song/video file: BPM, beat grid, bars, sections (intro/groove/build/drop/breakdown/outro), drops, energy per bar.",
                     {"file": FILE, "bpm": {"type": "number", "description": "known tempo (fixes half/double)"}, "vocals": B}, ["file"]),
    "fix_grid": ("Correct a song's beat grid (remembered): shift 0.5 = hits were on the off-beat, bar ±1 = cues a beat early/late, bpm = true tempo, clear.",
                 {"file": FILE, "shift": N, "bar": I, "bpm": N, "clear": B}, ["file"]),
    "make_show": ("Make a light show for a song on a rig; returns the bar-by-bar cue plan and warnings.",
                  {"file": FILE, "rig": RIG, **SHOWOPT, "mover_targets": {"type": "string", "enum": ["air", "floor"]}, "mover_lead": N}, ["file"]),
    "edit_cue": ("Change the look for bars start_bar..end_bar of a song's show on a rig (remembered; cue=null returns them to automatic).",
                 {"file": FILE, "start_bar": I, "end_bar": I, "cue": {"type": ["string", "null"], "enum": ["intro", "groove_a", "groove_b", "dip", "gap", "build1", "build2",
                  "build3", "predrop", "drop", "drive", "peak", "break", "outro", None]}, "rig": RIG}, ["file", "start_bar", "end_bar"]),
    "simulate_song": ("Run a song's show without audio at rate x speed (1-16) to check it fast (3D view in the web app; lights too if a dongle is connected).",
                      {"file": FILE, "rig": RIG, "rate": N, "start_bar": I, "palette": SHOWOPT["palette"], "style": SHOWOPT["style"]}, ["file"]),
    "seek": ("Jump to a bar while a song plays or simulates (with audio the song restarts there).", {"bar": I}, ["bar"]),
    "set_sim_speed": ("Change the simulation speed (0.1-32 x) while simulating.", {"rate": N}, ["rate"]),
    "preview_show": ("Timeline image of a song's show: one row per light (colours over time) — look at it before playing.", {"file": FILE, "rig": RIG, **SHOWOPT}, ["file"]),
    "stage_snapshot": ("3D render of the rig in its venue: now (if running), at rest, or mid-song (file + bar).",
                       {"rig": RIG, "view": {"type": "string", "enum": ["foh", "wide", "top", "side", "dj", "crowd", "truss", "photo"]}, "file": FILE, "bar": I}, []),
    "play_song": ("Play a song with its light show in sync (returns at once; poll lights_status). Drives REAL lights if a dongle is connected.",
                  {"file": FILE, "rig": RIG, "audio": B, "start_bar": I, "volume": N, **SHOWOPT}, ["file"]),
    "go_live": ("Live DJ set: lights follow the DJ app's beat (clock link = Ableton Link on in rekordbox/djay/Traktor; prodjlink = Pioneer decks) and react to an app's audio.",
               {"rig": RIG, "clock": {"type": "string", "enum": ["link", "prodjlink", "internal"]}, "audio_app": S, "palette": SHOWOPT["palette"], "style": SHOWOPT["style"]}, []),
    "manual_mode": ("Lights on an internal beat with the rig's looks; switch looks with trigger_cue.", {"rig": RIG, "bpm": N, "palette": SHOWOPT["palette"]}, []),
    "trigger_cue": ("Manual: switch to a look (intro, groove_a, groove_b, peak, build1-3, predrop, drop, drive, break, dip, gap, outro). During play/live it overlays until release_cue.",
                    {"cue": S}, ["cue"]),
    "release_cue": ("Remove a triggered overlay.", {}, []),
    "set_master": ("Master brightness 0..1 and/or blackout on/off for what is running.", {"master": N, "blackout": B}, []),
    "stop_lights": ("Stop whatever is running (lights black out).", {}, []),
    "test_lights": ("Identify lights: each one flashes white in address order (or walk one light's channels).", {"rig": RIG, "light": S, "seconds": N}, []),
    "point_light": ("Aim a moving head at dancefloor|dj|up|back|crowd|ceiling|'x,y,z' to check its pan direction.", {"light": S, "target": S, "rig": RIG, "seconds": N}, ["light", "target"]),
    "export_show": ("Export a song's show + rig for other software: qlc (QLC+ workspace + fixtures + audio), dmx (frame CSV), cues (CSV), midi (cue markers/notes), patch (CSV), native. Returns a zip path.",
                    {"file": FILE, "rig": RIG, "formats": {"type": "array", "items": {"type": "string", "enum": ["qlc", "dmx", "cues", "midi", "patch", "native"]}}, **SHOWOPT}, ["file"]),
    "open_web_app": ("Start the visual web app (3D stage, song timeline, rig editor, cue triggers, exports) and return its URL; open_browser opens it on this Mac.",
                     {"port": I, "open_browser": B}, []),
    "settings": ("Read or change remembered settings: rig, port (auto|none|/dev/..), artnet, latency_ms, palette, style, master, strobe, fog, mover_lead, mover_targets, rekordbox_xml.",
                 {"set": {"type": "object"}}, []),
}


def tool_list():
    return [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p, "required": r}} for n, (d, p, r) in TOOLS.items()]


def call(tools, name, args):
    if name not in TOOLS: raise LookupError(f"unknown tool {name!r}")
    res = getattr(tools, name)(**(args or {}))
    content = []
    if isinstance(res, dict) and "_image" in res:
        content.append({"type": "image", "data": base64.b64encode(Path(res["_image"]).read_bytes()).decode(), "mimeType": "image/png"})
        res = res.get("text", {})
    content.insert(0, {"type": "text", "text": json.dumps(res, indent=1, default=str)})
    return {"content": content, "isError": False}


def handle(tools, msg):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None: return None                                                # notification
    try:
        if method == "initialize":
            v = params.get("protocolVersion"); v = v if v in PROTOCOLS else PROTOCOLS[0]
            r = {"protocolVersion": v, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "dj-lights", "version": "1.0"}, "instructions": INSTRUCTIONS}
        elif method == "ping": r = {}
        elif method == "tools/list": r = {"tools": tool_list()}
        elif method == "tools/call":
            try: r = call(tools, params.get("name"), params.get("arguments") or {})
            except Exception as e:
                print(traceback.format_exc(), file=sys.stderr)
                r = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
        else: return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": r}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}


def serve_stdio(inp=None, out=None):
    inp = inp or sys.stdin; out = out or sys.stdout; sys.stdout = sys.stderr       # keep stray prints off the protocol stream
    tools = Tools()
    for line in inp:
        line = line.strip()
        if not line: continue
        try: msg = json.loads(line)
        except ValueError:
            out.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n"); out.flush(); continue
        resp = handle(tools, msg)
        if resp is not None: out.write(json.dumps(resp) + "\n"); out.flush()
    tools.s.stop()


if __name__ == "__main__":
    if "--list" in sys.argv:
        for t in tool_list(): print(f"{t['name']:16} {t['description']}")
    else: serve_stdio()
