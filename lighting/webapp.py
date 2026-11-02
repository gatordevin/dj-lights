"""DJ lights web app: 3D stage with live beams, song timeline (sections/drops/cues + playhead), rig editor with a drag-to-place plan, cue triggers, exports, setup.
CLI:
  python3 lighting/webapp.py [--port 8790] [--lan] [--open] [--output none]     then open http://127.0.0.1:8790
      (run_in_background); --lan serves other devices on a trusted network (phone/tablet as a remote) — there is no login.
Python: serve(session, port=8790, lan=False) -> ThreadingHTTPServer (call .serve_forever(); the MCP server's open_web_app uses it)
API (JSON; POST bodies JSON): GET /api/status /api/stage?rig= /api/doctor /api/settings /api/rigs /api/rig?name= /api/profiles?q=
  /api/ofl?q= /api/songs /api/palettes /api/file?path= (only files under out/lighting) · POST /api/settings /api/rig/new /api/rig/add
  /api/rig/set /api/rig/remove /api/ofl/import /api/profile/new /api/upload?name= (raw bytes) /api/analyze /api/show /api/play /api/live
  /api/manual /api/stop /api/trigger /api/release /api/master /api/blackout /api/nudge /api/tap /api/test /api/point /api/fix /api/export
  /api/simulate /api/seek /api/rate /api/show/edit (cue for a bar range, remembered) /api/rig/venue /api/rig/undo
  GET /api/stream = Server-Sent Events: status ~10/s + 3D frames ~30/s (motor lag applied) while something runs
  Errors: HTTP 400 {"ok": false, "error"}; success {"ok": true, ...}.
Page: lighting/webapp.html (one file, no external libraries; works offline at a venue).
Security: lighting/httpguard on every request — only localhost Host names (bare IPs too with --lan), same-origin POSTs with a
  JSON/binary Content-Type: blocks DNS rebinding and cross-site requests from other web pages; there is no login (keep --lan to
  trusted networks); file downloads only from out/lighting, uploads only music/video names into out/lighting/uploads.
Fails: OSError when the port is busy (--port N or 0 for any). Limits: one session (one engine) shared by every browser tab; the 3D view is
  a light-weight canvas renderer of lighting/stage3d's model (the ray-traced window is stage3d.py); uploads go to out/lighting/uploads.
Depends on: lighting/{session, dj, patch, ofl, songmap, autoshow, export, show, fixtures}. Used by: the owner/DJs, lighting/mcp_server.py."""
import sys, json, argparse, mimetypes, threading, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lighting import dj
from lighting.httpguard import refuse

HERE = Path(__file__).resolve().parent
OUTL = HERE.parent / "out" / "lighting"
UPLOADS = OUTL / "uploads"


def _rig_json(name):
    from lighting import patch as P
    d = P.load(name); c = P.check(d); rig = P._validate({k: v for k, v in d.items() if not k.startswith("_")})
    by = {f["name"]: f for f in d["fixtures"]}
    return {"name": d["name"], "venue": d.get("venue", {}), "notes": d.get("notes", ""), "check": {k: c[k] for k in ("ok", "errors", "warnings", "groups")},
            "fixtures": [{"name": f.name, "profile": f.profile.name, "model": f.profile.d.get("model", ""), "status": f.profile.status, "kind": f.kind,
                          "mode": f.mode, "modes": list(f.profile.d["modes"]), "address": f.address, "last": f.last, "channels": [n for _, n, _ in f.chans],
                          "dip": P.dip(f.address), "pos": list(f.pos), "mount": f.mount, "yaw": f.yaw, "where": by[f.name].get("where", ""),
                          "invert_pan": f.invert_pan, "pan_checked": bool(by[f.name].get("pan_checked")), "heads": f.heads,
                          "pitch": f.pitch, "roll": f.roll, "aim": list(f.aim_fixed), "moves": bool(f.joint_angles()),
                          "pan_speed": f.pan_speed, "tilt_speed": f.tilt_speed, "speed": [round(x) for x in f.max_speed()],
                          "pan_deg": f.profile.pan_deg, "tilt_deg": f.profile.tilt_deg}
                         for f in sorted(rig.fixtures, key=lambda f: f.address)]}


def _songs():
    from lighting.songmap import CACHE, find_audio
    seen = {}
    for p in sorted(CACHE.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try: d = json.loads(p.read_text())
        except ValueError: continue
        if d.get("file") and d["file"] not in seen and Path(d["file"]).exists() and "/out/tests/" not in d["file"]:
            seen[d["file"]] = {"file": d["file"], "title": d["title"], "bpm": d["bpm"], "bars": d["nbars"], "analysed": True}
    lib = dj.settings().get("library")
    if lib and Path(lib).is_dir():
        for f in find_audio(lib)[:500]:
            seen.setdefault(str(f), {"file": str(f), "title": f.stem, "analysed": False})
    if UPLOADS.is_dir():
        for f in sorted(UPLOADS.iterdir()): seen.setdefault(str(f), {"file": str(f), "title": f.stem, "analysed": False})
    return list(seen.values())


def _analysis(file, bpm=None):
    sm = dj._analyze(file, bpm)
    lab = {}
    for s in sm["sections"]:
        for b in range(s["start_bar"], s["end_bar"] + 1): lab[b] = s["type"]
    return {"title": sm["title"], "file": sm["file"], "bpm": sm["bpm"], "nbars": sm["nbars"], "duration_s": sm["duration_s"], "drops": sm["drops"],
            "sections": sm["sections"], "notes": sm["notes"], "grid_source": sm.get("grid_source"), "first_downbeat_s": sm["first_downbeat_s"],
            "bars": [{"bar": b["bar"], "t": b["t"], "energy": b["energy"], "low": b["low"], "high": b["high"], "section": lab.get(b["bar"], "")} for b in sm["bars"]]}


def _url(p): return "/api/file?path=" + urllib.parse.quote(str(p))


class App:
    def __init__(self, session): self.s = session; self.history = {}

    def _remember(self, rig):
        """Keep the rig file before a change (per rig, last 50) for /api/rig/undo."""
        from lighting import patch as P
        d = P.load(rig or dj._rig_name(None)); h = self.history.setdefault(d["_path"], [])
        h.append(Path(d["_path"]).read_text()); del h[:-50]; return d["_path"]

    def _changed(self, path):
        self.s.refresh_rig(Path(path).stem if Path(path).parent == (HERE / "rigs") else None); return _rig_json(path)

    def get(self, path, q):
        from lighting import patch as P
        if path == "/api/status": return {**self.s.status(), "default_rig": dj.settings()["rig"]}
        if path == "/api/stage": return self.s.stage(q.get("rig") or None, q.get("preview") == "1")
        if path == "/api/doctor": return dj.doctor()
        if path == "/api/settings": return dj.settings()
        if path == "/api/rigs":
            from lighting.fixtures import RIGS
            return {"rigs": sorted(p.stem for p in RIGS.glob("*.json")), "default": dj.settings()["rig"], "venues": {k: v["about"] for k, v in P.VENUES.items()}}
        if path == "/api/rig": return _rig_json(q.get("name") or dj._rig_name(None))
        if path == "/api/profiles":
            return [{"name": p.name, "status": p.status, "type": p.type, "channels": p.footprint(), "model": p.d.get("model", ""), "modes": list(p.d["modes"])}
                    for p in dj.find_profiles((q.get("q") or "").split())]
        if path == "/api/ofl":
            from lighting.ofl import search
            return search(q.get("q") or "", 30)
        if path == "/api/songs": return _songs()
        if path == "/api/palettes":
            from lighting.autoshow import PALETTES
            return {k: ["#%02x%02x%02x" % tuple(int(c * 255) for c in col) for col in v] for k, v in PALETTES.items()}
        raise LookupError(f"no endpoint {path}")

    def post(self, path, b):
        from lighting import patch as P
        s = self.s
        if path == "/api/settings": return dj.save_settings(**b)
        if path == "/api/rig/new":
            P.new_rig(b["name"], b.get("venue", "mobile"), bool(b.get("truss")))
            if b.get("make_default", True): dj.save_settings(rig=b["name"])
            return _rig_json(b["name"])
        if path == "/api/rig/add":
            self._remember(b.get("rig")); d = P.load(b.get("rig") or dj._rig_name(None))
            names = P.add(d, b["profile"], int(b.get("count", 1)), b.get("where", ""), b.get("name") or None, b.get("address") or "auto", False, b.get("mode") or None)
            P.save(d); return {"added": names, **self._changed(d["_path"])}
        if path == "/api/rig/set":
            self._remember(b.get("rig")); d = P.load(b.get("rig") or dj._rig_name(None)); light = b["light"]
            if b.get("where"): P.move(d, [light], b["where"])
            kv = {k: b[k] for k in ("address", "yaw", "pitch", "roll", "mode", "invert_pan", "pan_checked", "aim", "mount") if b.get(k) is not None}
            kv.update({k: b[k] for k in ("pan_speed", "tilt_speed") if k in b})
            if b.get("pos") is not None: kv["pos"] = ",".join(str(round(float(x), 2)) for x in b["pos"])
            if kv: P.set_fields(d, light, **kv)
            if b.get("rename"): P.rename(d, light, b["rename"])
            P.save(d); return self._changed(d["_path"])
        if path == "/api/rig/remove":
            self._remember(b.get("rig")); d = P.load(b.get("rig") or dj._rig_name(None)); P.remove(d, b["lights"]); P.save(d); return self._changed(d["_path"])
        if path == "/api/rig/venue":
            self._remember(b.get("rig")); d = P.load(b.get("rig") or dj._rig_name(None))
            P.set_venue(d, b.get("room"), b.get("deck"), b.get("truss"), b.get("truss_height")); P.save(d); return self._changed(d["_path"])
        if path == "/api/rig/undo":
            d = P.load(b.get("rig") or dj._rig_name(None)); h = self.history.get(d["_path"])
            if not h: raise LookupError("nothing to undo for this rig (since the web app started)")
            from core.files import write_atomic
            prev = h.pop(); P._validate(json.loads(prev)); write_atomic(Path(d["_path"]), prev); return {**self._changed(d["_path"]), "undo_left": len(h)}
        if path == "/api/ofl/import":
            from lighting.ofl import import_fixture
            _, pd = import_fixture(b["key"], b.get("name"), bool(b.get("force"))); return {"profile": pd["name"], "modes": {m: len(c) for m, c in pd["modes"].items()}}
        if path == "/api/profile/new":
            _, pd = P.make_profile(b["name"], b["channels"], b.get("type", "par"), b.get("pan_deg"), b.get("tilt_deg"), force=bool(b.get("force")))
            return {"profile": pd["name"]}
        if path == "/api/analyze": return _analysis(b["file"], b.get("bpm"))
        if path == "/api/show":
            from lighting.autoshow import make_show, save_show
            from lighting.show import preview_png
            rig = dj.load_rig(b.get("rig")); sm = dj._analyze(b["file"])
            sh = make_show(sm, rig, b.get("palette") or "auto", b.get("style") or "club", bool(b.get("strobe")), bool(b.get("fog")),
                           float(b.get("mover_lead") or 0.5), b.get("mover_targets") or "air")
            p = save_show(sh); png = preview_png(rig, sh, sm["bpm"], p.with_suffix(".png"), fps=4, song="song")
            from lighting.autoshow import CUE_NAMES
            return {"show": str(p), "plan": sh["songs"]["song"]["cues"], "warnings": sh["meta"]["warnings"] + sm["notes"], "colors": sh["meta"]["colors"],
                    "preview": _url(png), "analysis": _analysis(b["file"]), "edited_bars": sh["meta"]["edited_bars"], "cue_names": list(CUE_NAMES),
                    "motion": sh["meta"].get("motion", {}), "mover_leads": sh["meta"].get("mover_leads", {})}
        if path == "/api/simulate":
            return s.simulate(b["file"], b.get("rig") or None, float(b.get("rate") or 4), int(b.get("start_bar") or 1),
                              palette_name=b.get("palette") or None, style=b.get("style") or None, strobe=b.get("strobe"), fog=b.get("fog"))
        if path == "/api/seek": return s.seek(int(b["bar"]))
        if path == "/api/rate": return s.set_rate(float(b["rate"]))
        if path == "/api/show/edit":
            from lighting.autoshow import set_edit
            rig = dj.load_rig(b.get("rig")); set_edit(b["file"], rig.name, int(b["start_bar"]), int(b["end_bar"]), b.get("cue") or None)
            return self.post("/api/show", b)
        if path == "/api/play":
            return s.play(b["file"], b.get("rig") or None, bool(b.get("audio", True)), int(b.get("start_bar") or 1), float(b.get("volume", 1.0)),
                          palette_name=b.get("palette") or None, style=b.get("style") or None, strobe=b.get("strobe"), fog=b.get("fog"))
        if path == "/api/live": return s.live(b.get("rig") or None, b.get("clock", "link"), b.get("audio_app") or None, palette_name=b.get("palette") or None)
        if path == "/api/manual": return s.manual(b.get("rig") or None, float(b.get("bpm") or 124), palette_name=b.get("palette") or "neon")
        if path == "/api/stop": return s.stop()
        if path == "/api/trigger": return s.trigger(b["cue"])
        if path == "/api/release": return s.release()
        if path == "/api/master": return s.set_master(float(b["value"]))
        if path == "/api/blackout": return s.set_blackout(bool(b["on"]))
        if path == "/api/nudge": return s.nudge(float(b["beats"]))
        if path == "/api/tap": return s.tap()
        if path == "/api/test": return s.test(b.get("rig") or None, b.get("light") or None, float(b.get("seconds") or 1.5))
        if path == "/api/point": return s.point(b.get("rig") or None, b["light"], b["target"], float(b.get("seconds") or 8))
        if path == "/api/fix":
            from lighting.songmap import set_fix
            f = set_fix(b["file"], b.get("shift"), b.get("bar"), b.get("bpm"), bool(b.get("clear"))); return {"fix": f, "analysis": _analysis(b["file"])}
        if path == "/api/export":
            from lighting.export import export, FORMATS
            r = export(b["file"], dj.load_rig(b.get("rig")), b.get("formats") or list(FORMATS), palette_name=b.get("palette") or "auto",
                       style=b.get("style") or "club", strobe=bool(b.get("strobe")), fog=bool(b.get("fog")))
            readme = (Path(r["dir"]) / "README.txt").read_text()
            return {**r, "download": _url(r["zip"]), "readme": readme, "file_urls": {k: [{"name": Path(p).name, "url": _url(p)} for p in v] for k, v in r["files"].items()}}
        raise LookupError(f"no endpoint {path}")


def serve(session, port=8790, lan=False):
    app = App(session); page = HERE / "webapp.html"

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items(): self.send_header(k, v)
            self.end_headers(); self.wfile.write(data)

        def _err(self, e): self._send(400, {"ok": False, "error": f"{type(e).__name__}: {e}"})

        def _stream(self):
            """Server-Sent Events: {"status"} ~10x/s and {"stage"} (3D frame, motor lag applied) whenever a new one exists, ~30x/s."""
            import time
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive"); self.end_headers()
            last_seq, last_st = -1, 0.0
            try:
                while True:
                    msg = {}; now = time.monotonic()
                    if now - last_st >= 0.1: msg["status"] = {**app.s.status(), "default_rig": dj.settings()["rig"]}; last_st = now
                    fr, seq = app.s.frame()
                    if fr is not None and seq != last_seq and app.s.mode != "idle": msg["stage"] = fr; last_seq = seq
                    if msg: self.wfile.write(b"data: " + json.dumps(msg, default=str).encode() + b"\n\n"); self.wfile.flush()
                    time.sleep(0.03)
            except (BrokenPipeError, ConnectionResetError, OSError): return

        def do_GET(self):
            why = refuse(self, "GET", lan)
            if why: return self._send(403, {"ok": False, "error": why})
            u = urllib.parse.urlparse(self.path); q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"): return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
                if u.path == "/api/stream": return self._stream()
                if u.path == "/api/file":
                    p = Path(q.get("path", "")).resolve()
                    if OUTL.resolve() not in p.parents or not p.is_file(): raise LookupError("only files under out/lighting can be fetched")
                    ct = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                    extra = {"Content-Disposition": f'attachment; filename="{p.name}"'} if p.suffix in (".zip", ".csv", ".mid", ".qxw") else None
                    return self._send(200, p.read_bytes(), ct, extra)
                self._send(200, {"ok": True, "data": app.get(u.path, q)})
            except Exception as e: self._err(e)

        def do_POST(self):
            why = refuse(self, "POST", lan, need_type=True)
            if why: return self._send(403, {"ok": False, "error": why})
            u = urllib.parse.urlparse(self.path); n = int(self.headers.get("Content-Length") or 0)
            if n > 2_000_000_000: return self._send(413, {"ok": False, "error": "upload too large (2 GB max)"})
            raw = self.rfile.read(n) if n else b""
            try:
                if u.path == "/api/upload":
                    name = Path(urllib.parse.parse_qs(u.query).get("name", ["upload"])[0]).name
                    from lighting.songmap import is_audio_file
                    if not is_audio_file(name): raise ValueError(f"{name}: not a music/video file type")
                    UPLOADS.mkdir(parents=True, exist_ok=True); p = UPLOADS / name; p.write_bytes(raw)
                    return self._send(200, {"ok": True, "data": {"file": str(p)}})
                b = json.loads(raw or b"{}")
                self._send(200, {"ok": True, "data": app.post(u.path, b)})
            except Exception as e: self._err(e)

    class Srv(ThreadingHTTPServer):
        daemon_threads = True
        def handle_error(self, request, client_address):                    # a browser closing the live stream is normal
            if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)): return
            super().handle_error(request, client_address)
    srv = Srv(("0.0.0.0" if lan else "127.0.0.1", port), H)
    return srv


def main():
    from lighting.session import Session
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8790); ap.add_argument("--lan", action="store_true"); ap.add_argument("--open", action="store_true")
    ap.add_argument("--output", help="auto | none | /dev/cu.X (default: dj settings)")
    a = ap.parse_args(); s = Session(port=a.output); srv = serve(s, a.port, a.lan)
    url = f"http://127.0.0.1:{srv.server_address[1]}"; print(f"DJ lights web app: {url}" + ("  (LAN: this computer's IP, same port)" if a.lan else ""), flush=True)
    if a.open:
        import subprocess; subprocess.Popen(["open", url])
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally: s.stop()


if __name__ == "__main__":
    main()
