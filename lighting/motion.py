"""Moving-head motor physics: where a head actually points while it travels (top speed + acceleration per axis), for the 3D view and show planning.
Python:
  m = MotorSim(accel_s=0.25)         per (fixture, pan/tilt channel): actual angle + velocity
  m.step(rig, dt) -> self            move every joint toward what the rig's DMX commands now (Fixture.joint_angles),
                                     limited by Fixture.max_speed() (deg/s) and reaching that speed in accel_s
  m.apply(rig) -> rig                write the ACTUAL angles back into the rig's pan/tilt channels (use on a display copy)
  m.travel(name) -> deg              how far that fixture still has to go (largest joint); m.moving(name, deg=3) -> bool
  move_time(f, dpan, dtilt, accel_s=0.25) -> s    time for f to travel by (dpan, dtilt) degrees (trapezoid per axis)
  audit(rig, show, songmap, fps=12) -> {fixture: {"lit_moving_pct", "max_lag_deg", "moves"}}   play a song's show offline with
      motor lag: share of the time a mover is lit while still more than 4 deg from where it was told to be
Fails: ValueError for dt < 0 or accel_s <= 0. Limits: axes independent, no overshoot/settling, no homing; speeds are profile or
  type defaults unless measured (lighting/fixtures max_speed) — time a full pan on the real head to set pan_speed.
Depends on: lighting/fixtures (+ show/clock/songmap for audit). Used by: lighting/session.py (3D display), lighting/autoshow.py (leads, audit)."""
import sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def move_time(f, dpan, dtilt, accel_s=0.25):
    def axis(d, v):
        d = abs(d)
        if d < 1e-9: return 0.0
        if v <= 0: return math.inf
        a = v / accel_s
        return 2 * math.sqrt(d / a) if d < v * accel_s else d / v + accel_s
    vp, vt = f.max_speed()
    return max(axis(dpan, vp), axis(dtilt, vt))


class MotorSim:
    def __init__(self, accel_s=0.25):
        if accel_s <= 0: raise ValueError("accel_s must be > 0")
        self.accel_s, self.state, self.cmd = accel_s, {}, {}

    def step(self, rig, dt):
        if dt < 0: raise ValueError("dt must be >= 0")
        dt = min(dt, 1.0)
        for f in rig.fixtures:
            if not f.joint_angles(): continue
            vp, vt = f.max_speed(); spec = {n: s for _, n, s in f.chans}
            for n, _, deg, _ in f.joint_angles():
                key = (f.name, n); vmax = vp if spec[n]["role"] == "pan" else vt; a = vmax / self.accel_s
                pos, vel = self.state.get(key, (deg, 0.0)); self.cmd[key] = deg
                err = deg - pos
                if abs(err) < 1e-6 and abs(vel) < 1e-6: self.state[key] = (deg, 0.0); continue
                v_stop = math.sqrt(2 * a * abs(err))                          # fastest speed that can still stop on target
                want = math.copysign(min(vmax, v_stop), err)
                vel = vel + max(-a * dt, min(a * dt, want - vel)); nxt = pos + vel * dt
                if (deg - pos) * (deg - nxt) <= 0: nxt, vel = deg, 0.0            # arrived (no overshoot)
                self.state[key] = (nxt, vel)
        return self

    def apply(self, rig):
        for f in rig.fixtures:
            for n, _, _, _ in f.joint_angles():
                if (f.name, n) in self.state: f.set_joint(n, self.state[(f.name, n)][0])
        return rig

    def travel(self, name):
        return max((abs(self.cmd[k] - self.state[k][0]) for k in self.state if k[0] == name), default=0.0)

    def moving(self, name, deg=3.0): return self.travel(name) > deg


def audit(rig, show, sm, fps=12):
    """Offline: play the song's show with motor lag; per mover how much of its lit time it was still travelling."""
    from lighting.show import Engine, _song_timeline
    from lighting.clock import ManualClock
    from lighting.songmap import beat_at
    from lighting.fixtures import emitted_rgb
    movers = [f for f in rig.fixtures if f.joint_angles()]
    if not movers: return {}
    clk = ManualClock(sm["bpm"]); eng = Engine(rig, clock=clk); sheet = show["songs"]["song"]["cues"]
    eng.cue(show, sheet[0][1]); tl, end = _song_timeline(show, "song", 0.0); eng._start_timeline(show, tl, end, "song")
    m = MotorSim(); stats = {f.name: [0, 0, 0.0, 0] for f in movers}; last = {}
    for k in range(int(sm["duration_s"] * fps)):
        clk.value = beat_at(sm, k / fps); eng.frame(); m.step(rig, 1 / fps)
        for f in movers:
            lit = max(max(emitted_rgb(f, h)) for h in range(f.heads)) > 40; lag = m.travel(f.name); st = stats[f.name]
            cmd = tuple(round(d) for _, _, d, _ in f.joint_angles())
            if last.get(f.name) not in (None, cmd): st[3] += 1
            last[f.name] = cmd
            if lit: st[0] += 1; st[1] += lag > 4.0; st[2] = max(st[2], lag)
    return {n: {"lit_moving_pct": round(100 * b / a, 1) if a else 0.0, "max_lag_deg": round(c, 1), "moves": mv} for n, (a, b, c, mv) in stats.items()}
