"""Live audio → light-show signals: level, bass and "seconds since the last hit", from any app's audio output (rekordbox, Music, Spotify).
Effects use them through show.Tick: a layer with "react": "bass" scales its dimmer by the bass; pulse with trigger "onset"
flashes on every kick/hit. Beat *phase* still comes from the clock (internal or Ableton Link) — audio adds energy and
hits, it doesn't replace the bar grid.
CLI:
  python3 lighting/audiofeed.py watch APP [--seconds 20]     live meter for an app's audio (e.g. "rekordbox", "Music", "afplay")
Python:
  a = Analyzer(rate=48000); a.push(samples_mono) -> None; a.signals(now=None) -> {"level", "bass", "since_onset", "onsets"}
       level/bass: 0..1 with automatic gain (they follow the music's own loudness, so quiet and loud tracks both swing
       the lights); since_onset: seconds since the last detected hit (inf before the first); onsets: count so far
  feed = AudioFeed("rekordbox", mute=False).start(); feed.signals(); feed.name; feed.silent; feed.stop()
       taps every process whose argv[0] ends with the pattern (media/audiotap process tap); eng.audio = feed
Detection: one-pole low-pass at ~150 Hz for bass; hit = bass energy over 10 ms rising (×1.3 vs the previous 10 ms) above 1.8× its 0.4 s average and
  above 30 % of the recent bass peak, at most one per 120 ms. Tuned on kick-drum music; spoken word / ambient give few or no hits.
Contract: rate > 0 (ValueError). AudioFeed raises LookupError when no matching process has opened audio yet.
Limits: macOS needs "System Audio Recording" permission for the terminal running Python, otherwise the tap delivers
  silence (feed.silent turns True after 3 s of digital zero). The app must be on this Mac (Link carries no audio).
  ~20-40 ms latency (tap buffer + analysis period).
Depends on: media/audiotap (capture), hw/sysmon (find processes). Used by: lighting/console.py (--audio APP), show.Tick."""
import sys, math, time, threading, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class Analyzer:
    def __init__(self, rate=48000, bass_hz=150.0):
        if not rate > 0: raise ValueError("rate must be > 0")
        self.rate = rate; self.alpha = 1 - math.exp(-2 * math.pi * bass_hz / rate)
        self.lp = 0.0; self.win = max(1, int(rate * 0.010)); self.acc_e = self.acc_b = 0.0; self.n = 0
        self.level_env = self.bass_env = 0.0; self.level_peak = self.bass_peak = 1e-4
        self.avg_b = self.prev_b = 1e-6; self.last_onset = None; self.onsets = 0; self.clock = 0.0       # stream seconds

    def push(self, samples):
        a, lp = self.alpha, self.lp
        for x in samples:
            lp += a * (x - lp); self.acc_e += x * x; self.acc_b += lp * lp; self.n += 1
            if self.n >= self.win: self._window(); self.n = 0; self.acc_e = self.acc_b = 0.0
        self.lp = lp

    def _window(self):
        dt = self.win / self.rate; self.clock += dt
        e = math.sqrt(self.acc_e / self.win); b = math.sqrt(self.acc_b / self.win)
        for name in ("level", "bass"):                                   # fast attack, ~0.25 s release; AGC peak decays over ~8 s
            v = e if name == "level" else b
            env = getattr(self, name + "_env"); env = v if v > env else env + (v - env) * dt / 0.25
            peak = max(v, getattr(self, name + "_peak") * math.exp(-dt / 8.0), 1e-4)
            setattr(self, name + "_env", env); setattr(self, name + "_peak", peak)
        floor = 0.3 * self.bass_peak                                     # ignore tails/hats: a hit is near the recent peak
        rising = b > 1.3 * self.prev_b; self.prev_b = b                   # hits are rising edges, not decaying tails
        if rising and b > 1.8 * self.avg_b and b > floor and (self.last_onset is None or self.clock - self.last_onset > 0.12):
            self.last_onset = self.clock; self.onsets += 1
        self.avg_b += (b - self.avg_b) * dt / 0.4

    def signals(self, now=None):
        now = self.clock if now is None else now
        return {"level": min(1.0, self.level_env / self.level_peak), "bass": min(1.0, self.bass_env / self.bass_peak),
                "since_onset": math.inf if self.last_onset is None else max(0.0, now - self.last_onset), "onsets": self.onsets}


class AudioFeed:
    def __init__(self, app, mute=False, period=0.02):
        from hw.sysmon import find
        self.name, self.mute, self.period = app, mute, period
        self.pids = [pid for pid, _ in find(app)]
        if not self.pids: raise LookupError(f"no running process matching {app!r}")
        self._stop = threading.Event(); self.silent = False; self._zero_since = None
        self.an, self._t_stream0 = None, None

    def start(self):
        from media.audiotap import Recorder
        self.rec = Recorder(self.pids, mute=self.mute)                    # LookupError until the app has played audio
        self.an = Analyzer(self.rec.rate); self.rec.start(); self._t0 = time.monotonic()
        threading.Thread(target=self._run, daemon=True, name="audiofeed").start(); return self

    def _run(self):
        ch = self.rec.channels
        while not self._stop.is_set():
            fa = self.rec.take()
            if fa:
                mono = [sum(fa[i:i + ch]) / ch for i in range(0, len(fa) - ch + 1, ch)] if ch > 1 else fa
                self.an.push(mono)
                if any(mono): self._zero_since = None
                else:
                    self._zero_since = self._zero_since or time.monotonic()
                    self.silent = time.monotonic() - self._zero_since > 3.0
            self._stop.wait(self.period)

    def signals(self):
        if self.an is None: return {}
        s = self.an.signals(); return s

    def stop(self):
        self._stop.set()
        try: self.rec.close()
        except Exception: pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0]); sp = ap.add_subparsers(dest="cmd", required=True)
    w = sp.add_parser("watch"); w.add_argument("app"); w.add_argument("--seconds", type=float, default=20)
    a = ap.parse_args()
    feed = AudioFeed(a.app).start(); end = time.time() + a.seconds
    while time.time() < end:
        s = feed.signals()
        if s:
            bar = lambda v: "#" * int(v * 30)
            print(f"level {bar(s['level']):30} bass {bar(s['bass']):30} hits {s['onsets']:4}" + ("  SILENT (permission?)" if feed.silent else ""), flush=True)
        time.sleep(0.1)
    feed.stop()


if __name__ == "__main__":
    try: main()
    except LookupError as e: print(f"audiofeed: {e}", file=sys.stderr); sys.exit(1)
