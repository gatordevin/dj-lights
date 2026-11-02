"""Beat clocks for the light show: where are we in the music (beat, BPM, bar phase), from the app's own tempo or from rekordbox.
Every clock has the same interface, so the engine, effects and songs never care where time comes from:
  c.beat(t=None) -> float     beats at monotonic time t (seconds, default now); beat 0 = a downbeat (bar = 4 beats)
  c.bpm -> float              c.source -> "internal" | "link" | "manual"      c.paused -> bool
  c.pause(on=True)            freeze (the show holds still); resume continues (internal) or re-joins the live music (link)
  c.step(beats)               while paused: move the frozen beat ± (frame-by-frame checking)
  c.nudge(±beats)             shift a running internal clock (line up with music by ear; refused while following Link)
  c.set_bpm(bpm) / c.tap()    internal tempo (tap: 3+ taps within 3 s set BPM, the last tap lands on a beat).
                              LinkClock refuses both while following Link (RuntimeError: change tempo in rekordbox).
  c.state() -> dict           {"source", "bpm", "beat", "paused", ...link status}
Clocks:
  InternalClock(bpm=120)      free-running from its BPM; tap tempo
  LinkClock(listener=None, bpm=120, quantum=4)   follows an Ableton Link session (net/abletonlink.LinkListener) — tempo,
                              beat AND bar phase match the DJ app; when no session is synced it runs its internal clock,
                              handing over without a jump in either direction (beat continuity; bar phase snaps to Link's
                              when Link appears, because the DJ's downbeat is the truth)
  ManualClock(bpm=120)        c.value = beat set by the caller (offline previews, audits, tests)
  FileClock(beat_of, bpm, offset_s=0.0, rate=1.0)   follows a song file being played (rate > 1 / seek(s) / set_rate: fast
                              simulation without audio): beat_of(song_seconds) -> beat (e.g. from
                              lighting/songmap's grid, so tempo drift in the recording is followed); c.start(t0) = the
                              moment playback began; pause(on) freezes (the player pauses the audio too); nudge(±beats)
                              shifts the lights against the audio (latency); set_bpm/tap/step refuse (RuntimeError)
Contract: bpm 30..300 (ValueError); step only while paused (RuntimeError), |step| <= 64 beats. t is time.monotonic() seconds
  (the same clock as net.abletonlink.host_us()).
Limits: no network I/O of its own (LinkClock depends on a net.abletonlink listener); tap tempo needs 3+ taps within 3 s; nudge is
  limited to ±4 beats and step to ±64; times are time.monotonic() so not comparable across processes.
Depends on: net/abletonlink (LinkClock only). Used by: lighting/show.Engine, lighting/console, tests."""
import sys, math, time, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _check_bpm(bpm):
    if not isinstance(bpm, (int, float)) or not 30 <= bpm <= 300: raise ValueError(f"bpm must be 30..300, got {bpm!r}")
    return float(bpm)


class InternalClock:
    source = "internal"

    def __init__(self, bpm=120.0):
        self.bpm = _check_bpm(bpm); self._t0, self._b0 = time.monotonic(), 0.0
        self.paused, self._frozen, self._taps = False, 0.0, []; self.lock = threading.RLock()

    def _live(self, t): return self._b0 + (t - self._t0) * self.bpm / 60.0

    def beat(self, t=None):
        if self.paused: return self._frozen
        return self._live(time.monotonic() if t is None else t)

    def rebase(self, beat, t=None, bpm=None):
        """Continue from `beat` at time t (default now), optionally at a new BPM (used for hand-overs)."""
        with self.lock:
            if bpm is not None: self.bpm = _check_bpm(bpm)
            self._b0, self._t0 = float(beat), time.monotonic() if t is None else t
            if self.paused: self._frozen = float(beat)

    def pause(self, on=True):
        with self.lock:
            if on and not self.paused: self._frozen = self.beat(); self.paused = True
            elif not on and self.paused: self.paused = False; self._b0, self._t0 = self._frozen, time.monotonic()

    def step(self, beats):
        if not self.paused: raise RuntimeError("pause first: stepping only works while paused")
        if not -64 <= beats <= 64: raise ValueError("step must be within ±64 beats")
        with self.lock: self._frozen += beats

    def nudge(self, beats):
        """Shift the running beat by ±beats (line the grid up with music by ear); allowed paused or running."""
        if not -4 <= beats <= 4: raise ValueError("nudge must be within ±4 beats")
        with self.lock:
            if self.paused: self._frozen += beats
            else: self._b0 += beats

    def set_bpm(self, bpm):
        bpm = _check_bpm(bpm)
        with self.lock:
            now = time.monotonic(); b = self.beat(now); self.bpm = bpm
            if self.paused: self._frozen = b
            else: self._b0, self._t0 = b, now

    def tap(self):
        now = time.monotonic(); self._taps = [t for t in self._taps if now - t < 3.0] + [now]
        if len(self._taps) >= 3:
            iv = [b - a for a, b in zip(self._taps, self._taps[1:])]
            self.set_bpm(max(30.0, min(300.0, 60.0 / (sum(iv) / len(iv)))))
        with self.lock: self._b0, self._t0 = math.ceil(self.beat(now) - 0.5), now
        return self.bpm

    def state(self): return {"source": self.source, "bpm": round(self.bpm, 2), "beat": round(self.beat(), 4), "paused": self.paused}


class ManualClock(InternalClock):
    source = "manual"

    def __init__(self, bpm=120.0): super().__init__(bpm); self.value = 0.0

    def beat(self, t=None): return self._frozen if self.paused else self.value

    def pause(self, on=True):
        with self.lock:
            if on and not self.paused: self._frozen = self.value; self.paused = True
            elif not on and self.paused: self.paused = False; self.value = self._frozen

    def set_bpm(self, bpm): self.bpm = _check_bpm(bpm)

    def nudge(self, beats):
        if self.paused: self._frozen += beats
        else: self.value += beats

    def rebase(self, beat, t=None, bpm=None):
        if bpm is not None: self.bpm = _check_bpm(bpm)
        self.value = float(beat)
        if self.paused: self._frozen = float(beat)


class LinkClock:
    def __init__(self, listener=None, bpm=120.0, quantum=4):
        if listener is None:
            from net.abletonlink import LinkListener
            listener = LinkListener().start()
        self.link, self.quantum, self.internal = listener, quantum, InternalClock(bpm)
        self.paused, self._frozen, self._was_link = False, 0.0, False; self.lock = threading.RLock()

    @property
    def following(self): return self.link.synced

    @property
    def source(self): return "link" if self.following else "internal"

    @property
    def bpm(self): return self.link.tempo if self.following else self.internal.bpm

    def _live(self, t):
        from net.abletonlink import host_us
        if self.following:
            host = host_us() if t is None else int(t * 1e6)
            b = self.link.beat(host)
            if b is not None:
                self._was_link = True
                self.internal.rebase(b, t, bpm=max(30.0, min(300.0, self.link.tempo)))   # ready for a seamless fall-back
                return b
        if self._was_link: self._was_link = False                    # Link vanished: internal already continues from the last beat
        return self.internal.beat(t)

    def beat(self, t=None):
        if self.paused: return self._frozen
        return self._live(t)

    def pause(self, on=True):
        with self.lock:
            if on and not self.paused: self._frozen = self.beat(); self.paused = True
            elif not on and self.paused:
                self.paused = False
                if not self.following: self.internal.rebase(self._frozen)  # internal: continue from the frozen beat

    def step(self, beats):
        if not self.paused: raise RuntimeError("pause first: stepping only works while paused")
        if not -64 <= beats <= 64: raise ValueError("step must be within ±64 beats")
        with self.lock: self._frozen += beats

    def nudge(self, beats):
        if self.following: raise RuntimeError("the beat follows Ableton Link — nudge in the DJ app")
        self.internal.nudge(beats)

    def rebase(self, beat, t=None):
        if self.following: raise RuntimeError("the beat follows Ableton Link")
        self.internal.rebase(beat, t)

    def set_bpm(self, bpm):
        if self.following: raise RuntimeError("tempo follows Ableton Link (rekordbox/djay) — change it in the DJ app")
        self.internal.set_bpm(bpm)

    def tap(self):
        if self.following: raise RuntimeError("tempo follows Ableton Link (rekordbox/djay) — tap tempo is off")
        return self.internal.tap()

    def state(self):
        st = self.link.status()
        return {"source": self.source, "bpm": round(self.bpm, 2), "beat": round(self.beat(), 4), "paused": self.paused,
                "link_peers": len(st["peers"]), "link_tempo": st["tempo"], "link_synced": st["synced"]}


class FileClock:
    """Beat clock that follows a song file being played (see module docstring)."""
    source = "file"

    def __init__(self, beat_of, bpm, offset_s=0.0, rate=1.0):
        if not 0.1 <= rate <= 32: raise ValueError("rate must be 0.1..32")
        self.beat_of, self.bpm, self.offset, self.rate = beat_of, _check_bpm(bpm), float(offset_s), float(rate)
        self.t0, self.paused, self._frozen_t = None, False, 0.0; self.lock = threading.RLock()

    def start(self, t0=None):
        with self.lock: self.t0 = time.monotonic() if t0 is None else t0; self.paused = False

    def song_time(self, t=None):
        if self.t0 is None: return self.offset
        if self.paused: return self._frozen_t
        return ((time.monotonic() if t is None else t) - self.t0) * self.rate + self.offset

    def beat(self, t=None): return self.beat_of(self.song_time(t))

    def pause(self, on=True):
        with self.lock:
            if on and not self.paused: self._frozen_t = self.song_time(); self.paused = True
            elif not on and self.paused: self.t0 = time.monotonic() - (self._frozen_t - self.offset) / self.rate; self.paused = False

    def seek(self, song_s):
        """Jump to song time song_s (simulation without audio; with audio the player must restart there)."""
        with self.lock:
            if self.paused: self._frozen_t = float(song_s)
            else: self.offset, self.t0 = float(song_s), time.monotonic()

    def set_rate(self, rate):
        if not 0.1 <= rate <= 32: raise ValueError("rate must be 0.1..32")
        with self.lock: now = self.song_time(); self.rate = float(rate); self.seek(now)

    def nudge(self, beats):
        if not -4 <= beats <= 4: raise ValueError("nudge must be within ±4 beats")
        with self.lock: self.offset -= beats * 60.0 / self.bpm

    def step(self, beats): raise RuntimeError("the song file sets the beat: use nudge to line the lights up")
    def set_bpm(self, bpm): raise RuntimeError("the song file sets the tempo")
    def tap(self): raise RuntimeError("the song file sets the tempo")
    def rebase(self, beat, t=None, bpm=None): raise RuntimeError("the song file sets the beat")

    def state(self): return {"source": self.source, "bpm": round(self.bpm, 2), "beat": round(self.beat(), 4), "paused": self.paused,
                             "song_s": round(self.song_time(), 3)}
