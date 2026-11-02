"""Web control console + HTTP API for the light show; ConsoleClient for programs that drive a running console.
CLI:
  python3 lighting/console.py [--show halloween_demo] [--rig halloween] [--port auto|none|/dev/cu.X] [--http 8779] [--lan]
                              [--play] [--clock link|internal] [--audio APP]
      serves http://127.0.0.1:8779 (run_in_background; owns the DMX dongle until stopped).
      --port auto (default): USB dongle when plugged in, simulation otherwise (hot-plug both ways); none: always simulate.
      --clock link (default): follow rekordbox/djay over Ableton Link when they share a session, internal BPM otherwise.
      --audio APP: react to that app's audio (e.g. rekordbox, Music, Spotify) — level/bass/hits for "react"/"onset" layers.
      --lan binds 0.0.0.0 so a phone on the same Wi-Fi can drive it (no login: trusted networks only). --play starts the program.
HTTP API (JSON):
  GET  /state   Engine.state() + {"cues", "songs", "colors", "show"}
  POST /api {"action": ...}
       cue {name} · play {start?, loops?} · song {name, now?} (now: this instant = bar 1, internal clock) · nudge {beats} · stop_program · pause {on?} (toggle) · step {beats} (pauses first)
       bpm {bpm} · tap · master {value 0..1} · blackout {on} · hold {name, on, effect?, color?, group?, rate?, ...}
       identify {fixture, seconds?} · haze {seconds} · laser {armed} · raw {channels: {"45": 255}} ({} clears)
       → {"ok": true, "state": ...} or HTTP 400 {"ok": false, "error": "..."}  (e.g. "tempo follows Ableton Link" for bpm/tap)
Python:
  eng, srv = start_console(rig="halloween", show="halloween_demo", port="auto", http=8779, play=False, clock="link",
                           audio=None, lan=False, background=True)     background=True: server thread; else call srv.serve_forever()
  c = ConsoleClient(url="http://127.0.0.1:8779"); c.alive() -> bool; c.state() -> dict; c.api(action=..., ...) -> state
       (RuntimeError with the console's message on 400; OSError if not running)
       c.follow(hz=30) starts a background poller → c.latest (dict|None), c.ok; c.send(**body) fire-and-forget from UI threads
Contract: unknown actions/cues/songs/colours return 400 with the error text; the engine keeps running.
Limits: no authentication; one console per dongle; the page polls /state 5×/s.
Depends on: lighting/show (Engine), lighting/clock, lighting/fixtures, lighting/dmx, lighting/audiofeed (optional).
Used by: the owner during parties, lighting/stage3d.py, lighting/probe.py."""
import sys, json, time, argparse, threading, urllib.request, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lighting.fixtures import Rig
from lighting.httpguard import refuse
from lighting.show import Engine, load_show, PALETTE
from lighting.dmx import open_output, MemoryOutput, AutoOutput, Universe

PAGE = (Path(__file__).resolve().parent / "console.html")
URL = "http://127.0.0.1:8779"


def handle(eng, show, req):
    a = req.get("action")
    if a == "cue": eng.stop_program(); eng.cue(show, req["name"])
    elif a == "play": eng.play_program(show, req.get("start"), int(req.get("loops", 100)))
    elif a == "song": eng.play_song(show, req["name"], bool(req.get("now", False)))
    elif a == "nudge": eng.nudge(float(req.get("beats", 0.125)))
    elif a == "stop_program": eng.stop_program()
    elif a == "bpm": eng.set_bpm(float(req["bpm"]))
    elif a == "tap": eng.tap()
    elif a == "pause": eng.pause(bool(req.get("on", not eng.paused)))
    elif a == "step":
        if not eng.paused: eng.pause(True)                                 # stepping implies pausing
        eng.step(float(req.get("beats", 1 / 16)))
    elif a == "identify":
        name = req["fixture"]; eng.rig[name]                              # LookupError for unknown fixtures
        eng.hold("identify", [{"group": name, "effect": "strobe", "rate": 0.6, "color": "white"}])
        threading.Timer(float(req.get("seconds", 2.0)), lambda: eng.hold("identify", None)).start()
    elif a == "master":
        v = float(req["value"])
        if not 0 <= v <= 1: raise ValueError("master must be 0..1")
        eng.master = v
    elif a == "blackout": eng.blackout = bool(req["on"])
    elif a == "hold":
        if not req.get("on"): eng.hold(req["name"], None)
        else:
            L = {"group": req.get("group", "all"), "effect": req.get("effect", "color")}
            for k in ("color", "rate", "colors", "beats", "shape", "pan", "tilt", "trigger", "react", "decay"):
                if k in req: L[k] = req[k]
            eng.hold(req["name"], [L])
    elif a == "haze": eng.haze_burst(float(req.get("seconds", 5)))
    elif a == "laser": eng.laser_armed = bool(req["armed"])
    elif a == "raw":
        chans = {int(k): int(v) for k, v in req.get("channels", {}).items()}
        for c, v in chans.items(): Universe._check(c, v)
        eng.raw = chans
    else: raise ValueError(f"unknown action {a!r}")


def serve(eng, show, show_name, http_port=8779, lan=False):
    lock = threading.Lock()
    def state():
        return {**eng.state(), "cues": list(show["cues"]), "songs": list(show.get("songs", {})),
                "colors": {k: v for k, v in PALETTE.items() if k != "off"}, "show": show_name}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, code, body, ctype="application/json"):
            b = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            why = refuse(self, "GET", lan)
            if why: return self._send(403, {"ok": False, "error": why})
            if self.path in ("/", "/index.html"): self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/state": self._send(200, state())
            else: self._send(404, {"ok": False, "error": "not found"})
        def do_POST(self):
            why = refuse(self, "POST", lan)
            if why: return self._send(403, {"ok": False, "error": why})
            if self.path != "/api": return self._send(404, {"ok": False, "error": "not found"})
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                with lock: handle(eng, show, req)
                self._send(200, {"ok": True, "state": state()})
            except (KeyError, ValueError, LookupError, RuntimeError, TypeError) as e:
                self._send(400, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    srv = ThreadingHTTPServer(("0.0.0.0" if lan else "127.0.0.1", http_port), H)
    print(f"console: http://127.0.0.1:{http_port}" + ("  (LAN: this Mac's IP, same port)" if lan else ""), flush=True)
    return srv


def start_console(rig="halloween", show="halloween_demo", port="auto", http=8779, play=False, clock="link", audio=None,
                  lan=False, background=True):
    """Build the whole stack (rig, show, DMX output, clock, optional audio, engine, HTTP server) → (engine, server)."""
    rig_o, show_o = (rig if isinstance(rig, Rig) else Rig.load(rig)), (show if isinstance(show, dict) else load_show(show))
    out = MemoryOutput() if port == "none" else AutoOutput() if port == "auto" else open_output(port)
    bpm = show_o.get("bpm", 120)
    if clock == "link":
        from lighting.clock import LinkClock
        clk = LinkClock(bpm=bpm)
    elif clock == "prodjlink":
        from lighting.prodjclock import ProDjLinkClock
        clk = ProDjLinkClock(bpm=bpm)
    elif clock == "internal":
        from lighting.clock import InternalClock
        clk = InternalClock(bpm)
    else: raise ValueError("clock must be 'link', 'prodjlink' or 'internal'")
    eng = Engine(rig_o, [out], clock=clk)
    if audio:
        from lighting.audiofeed import AudioFeed
        try: eng.audio = AudioFeed(audio).start()
        except LookupError as e: print(f"console: audio off ({e})", flush=True)
    eng.play_program(show_o, loops=100) if play else eng.cue(show_o, next(iter(show_o["cues"])))
    eng.start(); srv = serve(eng, show_o, show if isinstance(show, str) else "show", http, lan)
    if background: threading.Thread(target=srv.serve_forever, daemon=True, name="console-http").start()
    return eng, srv


class ConsoleClient:
    def __init__(self, url=URL):
        self.url, self.latest, self.ok, self.last_error = url.rstrip("/"), None, False, None

    def state(self, timeout=1.0):
        with urllib.request.urlopen(self.url + "/state", timeout=timeout) as r: return json.load(r)

    def alive(self):
        try: self.state(); return True
        except (OSError, ValueError): return False

    def api(self, timeout=2.0, **body):
        req = urllib.request.Request(self.url + "/api", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r: return json.load(r)["state"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(json.load(e).get("error", str(e))) from None

    def send(self, **body):
        """Fire-and-forget api() on a thread (UI callbacks must not block); errors are kept in .last_error."""
        def go():
            try: self.api(**body); self.last_error = None
            except (OSError, RuntimeError) as e: self.last_error = str(e)
        threading.Thread(target=go, daemon=True).start()

    def follow(self, hz=30):
        def run():
            while True:
                try: self.latest = self.state(); self.ok = True
                except (OSError, ValueError): self.ok = False
                time.sleep(1 / hz)
        threading.Thread(target=run, daemon=True, name="console-follow").start(); return self


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--show", default="halloween_demo"); ap.add_argument("--rig", default="halloween")
    ap.add_argument("--port", default="auto"); ap.add_argument("--http", type=int, default=8779)
    ap.add_argument("--lan", action="store_true"); ap.add_argument("--play", action="store_true")
    ap.add_argument("--clock", default="link", choices=["link", "prodjlink", "internal"]); ap.add_argument("--audio")
    a = ap.parse_args()
    eng, srv = start_console(a.rig, a.show, a.port, a.http, a.play, a.clock, a.audio, a.lan, background=False)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally: srv.server_close(); eng.stop()


if __name__ == "__main__":
    try: main()
    except (LookupError, ValueError, OSError) as e: print(f"console: {e}", file=sys.stderr); sys.exit(1)
