"""One controller for the lights shared by the web app and the MCP server: play/live/manual/test run in the background, state + 3D stage on demand.
Python:
  s = Session()                         one at a time: starting anything stops what was running (blackout in between)
  s.status() -> {"mode": idle|play|live|manual|test, "rig", "output", "song", "bar", "beat", "bpm", "cue", "next", "master",
                 "blackout", "result", "error", "fixtures": [{"name", "rgb": [[r,g,b] per head], "dimmer", "pan", "tilt"}]}
  s.stage(rig=None) -> {"venue", "solids", "parts", "beams"}   3D scene for drawing (lighting.stage3d.kinematics of the
                         running rig, or the named rig at rest): parts = boxes {c, ax, ay, az, half, emit}, beams = {pos,
                         dir, rgb, tan_half, length}
  s.play(file, rig=None, audio=True, start_bar=1, volume=1.0, show_path=None, **show_opts) -> status   (returns at once)
  s.live(rig=None, clock="link", audio_app=None, **show_opts) · s.manual(rig=None, bpm=124, **show_opts)
  s.test(rig=None, light=None, seconds=1.5) · s.point(rig, light, target, seconds=8) · s.stop()
  s.trigger(cue) (manual: switch cue; play/live: hold that look over the show until s.release())
  s.release() · s.set_master(0..1) · s.set_blackout(bool) · s.nudge(beats) (play: shift lights vs audio) · s.tap()
  s.simulate(file, rig=None, rate=4.0, start_bar=1, **show_opts)   the show without audio at 0.1-32x speed
  s.seek(bar) (simulation: jump; with audio: restart the song there) · s.set_rate(x) · s.refresh_rig(rig) (apply saved rig edits
      — positions, mounting, speeds — to what is running) · s.frame() -> latest 3D frame + its sequence number (for streaming)
  3D frames while running come from a display copy of the rig with lighting.motion.MotorSim lag applied (~40 fps background
  loop), so the stage shows where slow heads really point; status()["travel"] = degrees each mover is still behind.
Output: dj settings (port/artnet) unless given to Session(port=, artnet=); "none" = simulation.
Fails: LookupError (unknown rig/cue/light), ValueError (bad arguments), RuntimeError for actions that need a running mode;
  errors inside a background run land in status()["error"].
Limits: one engine at a time; manual triggers during play are overlays (the song's own cues continue underneath).
Depends on: lighting/{dj, autoshow, show, stage3d, clock, fixtures}. Used by: lighting/webapp.py, lighting/mcp_server.py."""
import sys, threading, time, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lighting import dj
from lighting.fixtures import Rig, emitted_rgb


class Session:
    def __init__(self, port=None, artnet=None):
        self.port, self.artnet = port, artnet
        self.lock = threading.RLock(); self.mode = "idle"; self.eng = None; self.show = None; self.sm = None
        self.thread = None; self.stop_evt = None; self.result = None; self.error = None; self.rig_name = None; self.out = None
        self.song_file = None; self.last_play = None; self.motor = None; self.disp = None
        self._frame, self._seq = None, 0; self._disp_stop = threading.Event(); self._disp_thread = None

    # ---------- plumbing ----------
    def _port(self):
        s = dj.settings(); return (self.port or s["port"]), (self.artnet or s["artnet"])

    def _rig(self, rig):
        name = dj._rig_name(rig); return Rig.load(name), name

    def _run(self, mode, rig_name, target):
        self.stop(); ev = threading.Event(); port, artnet = self._port(); out = dj.output(port, artnet)
        with self.lock:
            self.mode, self.rig_name, self.stop_evt, self.result, self.error, self.out = mode, rig_name, ev, None, None, out
            self.eng = self.show = self.sm = None
        def go():
            try: r = target(ev, out)
            except Exception as e: r = None; self.error = f"{type(e).__name__}: {e}"
            with self.lock:
                if self.stop_evt is ev: self.result, self.mode, self.eng = r, "idle", None
        self.thread = threading.Thread(target=go, daemon=True, name=f"lights-{mode}"); self.thread.start()
        t0 = time.monotonic()                                                # wait briefly for the engine to come up
        while time.monotonic() - t0 < 5 and self.eng is None and self.thread.is_alive() and mode in ("play", "live", "manual"): time.sleep(0.02)
        self._start_display(rig_name)
        return self.status()

    def _start_display(self, rig_name):
        """Background loop: mirror the running engine's DMX into a display rig, apply motor lag, cache a 3D frame (~40 fps)."""
        from lighting.motion import MotorSim
        from lighting.stage3d import kinematics
        self._disp_stop.set()
        if self._disp_thread and self._disp_thread.is_alive(): self._disp_thread.join(1)
        stop = self._disp_stop = threading.Event()
        try: disp = Rig.load(dj._rig_name(rig_name))
        except (LookupError, ValueError): return
        self.disp, self.motor = disp, MotorSim()
        src_attr = lambda: (self.eng.rig if self.eng else getattr(self, "stage_rig", None) if self.mode == "test" else None)
        def loop():
            last = time.monotonic()
            while not stop.is_set():
                now = time.monotonic(); dt, last = now - last, now; src = src_attr()
                if src is not None:
                    lk = self.eng.lock if self.eng else threading.RLock()       # the engine zeroes then re-renders each frame:
                    with lk:                                                   # copy only complete frames
                        for f, g in zip(src.fixtures, disp.fixtures): g.values[:] = f.values
                    rate = getattr(self.eng.clock, "rate", 1.0) if self.eng else 1.0   # simulated time runs `rate` x: so do the motors
                    self.motor.step(disp, dt * rate).apply(disp)
                    parts, ems = kinematics(disp, now)
                    with self.lock: self._frame = self._pack(disp.venue, parts, ems); self._seq += 1
                elif self.mode == "idle": break
                stop.wait(0.025)
        self._disp_thread = threading.Thread(target=loop, daemon=True, name="lights-3d"); self._disp_thread.start()

    @staticmethod
    def _pack(venue, parts, ems):
        rnd = lambda v: [round(float(x), 3) for x in v]
        return {"venue": venue, "parts": [{"name": p.name, "c": rnd(p.c), "ax": rnd(p.ax), "ay": rnd(p.ay), "az": rnd(p.az), "half": rnd(p.half), "emit": rnd(p.emit)} for p in parts],
                "beams": [{"name": m.name, "pos": rnd(m.pos), "dir": rnd(m.dir), "rgb": rnd(m.rgb), "tan_half": round(m.tan_half, 4), "length": round(m.length, 2)} for m in ems]}

    def frame(self):
        with self.lock: return self._frame, self._seq

    def stop(self):
        self._disp_stop.set()
        with self.lock: ev, th = self.stop_evt, self.thread
        if ev: ev.set()
        if th and th.is_alive(): th.join(5)
        with self.lock: self.mode, self.eng, self.stop_evt = "idle", None, None
        return self.status()

    # ---------- modes ----------
    def simulate(self, file, rig=None, rate=4.0, start_bar=1, **show_opts):
        """The song's show without audio at `rate` x speed (0.1-32): fast checking; seek with s.seek(bar)."""
        return self.play(file, rig, audio=False, start_bar=start_bar, rate=rate, **show_opts)

    def play(self, file, rig=None, audio=True, start_bar=1, volume=1.0, show_path=None, latency_ms=None, rate=1.0, **show_opts):
        r, name = self._rig(rig); s = dj.settings(); self.song_file = str(file)
        self.last_play = dict(file=file, rig=rig, audio=audio, volume=volume, show_path=show_path, latency_ms=latency_ms, rate=rate, **show_opts)
        opts = {**dict(palette_name=s["palette"], style=s["style"], strobe=s["strobe"], fog=s["fog"], mover_lead=s["mover_lead"],
                       mover_targets=s["mover_targets"]), **{k: v for k, v in show_opts.items() if v is not None}}
        def started(eng, sm, show, proc):
            with self.lock: self.eng, self.sm, self.show = eng, sm, show
        def target(ev, out):
            return dj.play(file, r, show_path, s["latency_ms"] if latency_ms is None else latency_ms, start_bar, out=out, master=s["master"],
                           audio=audio, verbose=False, volume=volume, stop=ev, on_start=started, rate=rate, **opts)
        return self._run("play", name, target)

    def live(self, rig=None, clock="link", audio_app=None, **show_opts):
        r, name = self._rig(rig); s = dj.settings()
        opts = {**dict(palette_name=s["palette"], style=s["style"], strobe=s["strobe"]), **{k: v for k, v in show_opts.items() if v is not None}}
        def started(eng, show):
            with self.lock: self.eng, self.show = eng, show
        return self._run("live", name, lambda ev, out: dj.live(r, clock, audio_app, http=None, stop=ev, on_start=started, out=out, **opts))

    def manual(self, rig=None, bpm=124, **show_opts):
        """Lights on an internal beat clock with the rig's live looks; switch looks with trigger(cue)."""
        from lighting.autoshow import live_show
        from lighting.show import Engine
        from lighting.clock import InternalClock
        r, name = self._rig(rig); show = live_show(r, audio_react=False, **{k: v for k, v in show_opts.items() if v is not None})
        def target(ev, out):
            eng = Engine(r, [out], clock=InternalClock(bpm)); eng.cue(show, "groove_a"); eng.master = dj.settings()["master"]; eng.start()
            with self.lock: self.eng, self.show = eng, show
            try: ev.wait()
            finally: eng.stop()
            return {"output": dj._out_name(out)}
        return self._run("manual", name, target)

    def test(self, rig=None, light=None, seconds=1.5):
        r, name = self._rig(rig)
        if light: r[light]
        def target(ev, out):
            steps = []
            for f in ([r[light]] if light else sorted(r.fixtures, key=lambda f: f.address)):
                if ev.is_set(): break
                steps += dj.identify(r, f.name if light else None, seconds, out) if light else []
                if not light:
                    r.home(); f.dimmer(1); f.color(1, 1, 1); steps.append(f"{f.name} (address {f.address}-{f.last})"); dj._hold_frames(r, out, seconds)
            return {"steps": steps}
        self.stage_rig = r
        return self._run("test", name, target)

    def point(self, rig, light, target_name, seconds=8.0):
        r, name = self._rig(rig); self.stage_rig = r
        return self._run("test", name, lambda ev, out: dj.point(r, light, target_name, seconds, out))

    # ---------- controls ----------
    def _need_eng(self):
        if not self.eng: raise RuntimeError("nothing running: start play, live or manual first")
        return self.eng

    def trigger(self, cue):
        e = self._need_eng()
        if cue not in self.show["cues"]: raise LookupError(f"no cue {cue!r} (have {sorted(self.show['cues'])})")
        if self.mode == "manual": e.stop_program(); e.cue(self.show, cue)
        else: e.hold("trigger", self.show["cues"][cue])
        return self.status()

    def release(self): self._need_eng().hold("trigger", None); return self.status()
    def set_master(self, x):
        if not 0 <= x <= 1: raise ValueError("master must be 0..1")
        self._need_eng().master = float(x); return self.status()
    def set_blackout(self, on): self._need_eng().blackout = bool(on); return self.status()
    def nudge(self, beats): self._need_eng().nudge(float(beats)); return self.status()
    def tap(self): self._need_eng().tap(); return self.status()

    def seek(self, bar):
        from lighting.songmap import time_at
        e = self._need_eng()
        if self.mode != "play" or not self.sm: raise RuntimeError("seek works while a song plays or simulates")
        if not 1 <= bar <= self.sm["nbars"]: raise ValueError(f"bar must be 1..{self.sm['nbars']}")
        lp = dict(self.last_play)
        if lp.get("audio"):                                                   # audio can't jump: restart the song there
            return self.play(lp.pop("file"), lp.pop("rig"), lp.pop("audio"), int(bar), **{k: v for k, v in lp.items() if k != "start_bar"})
        e.clock.seek(time_at(self.sm, (bar - 1) * 4)); return self.status()

    def set_rate(self, rate):
        e = self._need_eng()
        if not hasattr(e.clock, "set_rate") or (self.last_play or {}).get("audio"): raise RuntimeError("speed changes only while simulating (no audio)")
        e.clock.set_rate(float(rate)); self.last_play["rate"] = float(rate); return self.status()

    def refresh_rig(self, rig=None):
        """Apply a saved rig's geometry/speed edits to the running engine and 3D display (same fixtures by name)."""
        if not self.eng or (rig and dj._rig_name(rig) != self.rig_name): return False
        fresh = Rig.load(self.rig_name)
        for target in (self.eng.rig, self.disp):
            if target is None: continue
            for f in target.fixtures:
                if f.name not in fresh.by_name: continue
                g = fresh.by_name[f.name]
                for k in ("pos", "yaw", "pitch", "roll", "mount", "aim_fixed", "invert_pan", "invert_tilt", "pan_speed", "tilt_speed", "where"): setattr(f, k, getattr(g, k))
            target.venue = fresh.venue
        return True

    # ---------- reading ----------
    def status(self):
        with self.lock: e, sm, show, mode = self.eng, self.sm, self.show, self.mode
        st = {"mode": mode, "rig": self.rig_name, "output": dj._out_name(self.out) if self.out else None, "result": self.result, "error": self.error,
              "song": None, "cues": sorted(show["cues"]) if show else []}
        if sm: st["song"] = {"title": sm["title"], "bpm": sm["bpm"], "nbars": sm["nbars"], "file": sm["file"], "drops": sm["drops"]}
        if self.last_play and mode == "play": st["rate"] = self.last_play.get("rate", 1.0); st["audio"] = bool(self.last_play.get("audio"))
        if self.motor and self.disp: st["travel"] = {f.name: round(self.motor.travel(f.name), 1) for f in self.disp.fixtures if f.joint_angles()}
        if e:
            b = e.beat(); sheet = (show or {}).get("songs", {}).get("song", {}).get("cues", [])
            nxt = next(([bb, c] for bb, c in sheet if bb > int(b // 4) + 1), None)
            st.update({"beat": round(b, 3), "bar": int(math.floor(b / 4)) + 1, "bpm": round(e.bpm, 2), "cue": e.cue_name, "next": nxt,
                       "master": e.master, "blackout": e.blackout, "holds": sorted(e.overlay), "clock": e.clock.state(),
                       "song_s": round(e.clock.song_time(), 2) if hasattr(e.clock, "song_time") else None,
                       "fps": round(e.sender.fps_actual, 1) if e.sender else 0, "engine_error": e.error,
                       "fixtures": [{"name": f.name, "kind": f.kind, "rgb": [list(emitted_rgb(f, h)) for h in range(f.heads)],
                                     "levels": list(f.values), "address": f.address} for f in e.rig.fixtures]})
        return st

    def stage(self, rig=None, preview=False):
        """3D scene of what the lights are doing now (running rig, with motor lag) or the named rig at rest."""
        from lighting.stage3d import kinematics, venue_solids
        rnd = lambda v: [round(float(x), 3) for x in v]
        with self.lock: e, fr = self.eng, self._frame
        if e and fr and (rig is None or rig == self.rig_name):
            return {**fr, "solids": [[k, rnd(a), rnd(b) if isinstance(b, (list, tuple)) else round(b, 3)] for k, a, b in venue_solids(fr["venue"])]}
        if e and (rig is None or rig == self.rig_name):
            with e.lock: parts, ems = kinematics(e.rig); venue = e.rig.venue
        else:
            r = getattr(self, "stage_rig", None) if (self.mode == "test" and rig in (None, self.rig_name)) else None
            if r is None:
                r, _ = self._rig(rig); r.home()
                if preview:                                                    # idle: dim white beams show where each light points
                    for f in r.fixtures:
                        if f.kind in ("wash", "mover", "effect", "strobe"): f.dimmer(0.6); f.color(0.75, 0.8, 1.0)
            parts, ems = kinematics(r); venue = r.venue
        rnd = lambda v: [round(float(x), 3) for x in v]
        return {"venue": venue, "solids": [[k, rnd(a), rnd(b) if isinstance(b, (list, tuple)) else round(b, 3)] for k, a, b in venue_solids(venue)],
                "parts": [{"name": p.name, "c": rnd(p.c), "ax": rnd(p.ax), "ay": rnd(p.ay), "az": rnd(p.az), "half": rnd(p.half), "emit": rnd(p.emit)} for p in parts],
                "beams": [{"name": m.name, "pos": rnd(m.pos), "dir": rnd(m.dir), "rgb": rnd(m.rgb), "tan_half": round(m.tan_half, 4), "length": round(m.length, 2)} for m in ems]}
