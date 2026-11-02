"""Room-microphone sound-reactive features for the light director: kicks, bass, hi-hats/high notes, loudness and song SECTIONS,
published as live/audio.json (fields: see AudioFeatures.snapshot). The DJ plays in the room; the Mac only hears it through
its microphone. Features only: audio samples live in memory for ~1 s and are never written to disk, logged or sent anywhere.
RECEIVE-ONLY networking: the optional beat listener (--listen-beats) binds UDP 50001 and never sends a packet; nothing here can
transmit to the Pioneer deck.
CLI:
  python3 lighting/audio_in.py [--seconds N] [--out PATH|--no-out] [--listen-beats] [--print]   live from the microphone
  python3 lighting/audio_in.py --file PATH [--events]     analyse an audio file offline (ffmpeg decode to a pipe) and print a summary
  python3 lighting/audio_in.py --sim [--seconds N]        synthetic 4/4 track with breakdowns played through the same pipeline, in real time
  default --out is $LIGHTING_AUDIO_OUT or out/lighting/audio.json (live and --sim only; --file writes nothing unless --out is given)
Python:
  f = AudioFeatures(out=None, listen_beats=False).start(); f.snapshot() -> dict per spec; f.stop()
  analyze_file(path) -> {"kicks": [t..], "hats": [t..], "sections": [(t, name)..], "ranges": {..}, "duration": s}
  Engine(); e.push(float32 mono @22050) -> e.snapshot(); estimate_lead(kick_times, beat_times) -> ms | None
Method: STFT 1024 samples (46 ms Hann) every 128 samples (5.8 ms) at 22.05 kHz. Bands bass 30-150, lowmid 150-500, mid 500-4k,
  high 4k-12k Hz -> smoothed envelopes -> AGC (rolling 5th..98th percentile over 20 s) -> 0..1. Kick = rectified spectral flux in
  30-250 Hz (the kick sweep starts ~170 Hz; bass-line notes stay below it) over max(0.45*rolling p95, 3x local mean), refractory 150 ms.
  Hat = flux in 4-12 kHz, same rule, refractory 60 ms. Section (10 Hz, hysteresis, min dwell): groove/breakdown/build/drop/quiet
  from kick presence, smoothed energy and bass vs a 90 s reference, hat density and an 8 s energy trend; 'drop' is held >= 4 s.
Timing: onset_t is when the detector fired (the mic -> pipe -> analysis delay is in it, so lead_ms measures it against the deck).
Limits: a bass-line note with a hard attack can look like a kick; cold start has no reference so the first seconds of a track
  calibrate to whatever is loudest; 'build' needs a preceding breakdown; mono/room acoustics/echo smear sub-50 ms timing.
Depends on: numpy, ffmpeg (avfoundation capture / file decode). Used by: live light designers that read the published audio.json."""
import sys, os, os, json, time, math, threading, argparse, subprocess, socket, collections
from pathlib import Path
import numpy as np

SR = 22050; NFFT = 1024; HOP = 128; FLUX_LAG = 2
BANDS = {"bass": (30, 150), "lowmid": (150, 500), "mid": (500, 4000), "high": (4000, 12000)}
KICK_BAND = (30, 250); HAT_BAND = (4000, 12000)
DEFAULT_OUT = os.environ.get("LIGHTING_AUDIO_OUT", str(Path(__file__).resolve().parents[1] / "out" / "lighting" / "audio.json"))
MAGIC = bytes.fromhex("5173707431576d4a4f4c")
SECTIONS = ("quiet", "breakdown", "build", "drop", "groove")
HF_REJECT = 0.1                                                         # kick-band flux must be >= this x the 4-12k flux (else it is a hat/clap leaking low)
FLOOR = 2e-4                                                            # amplitude below which a band counts as silence
FFMPEG_MIC = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-i", ":0", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]


class Roll:
    """Ring buffer with percentiles (the rolling AGC window)."""
    def __init__(self, cap): self.a = np.zeros(cap); self.i = 0; self.n = 0; self.cap = cap
    def push(self, v): self.a[self.i] = v; self.i = (self.i + 1) % self.cap; self.n = min(self.n + 1, self.cap)
    def pct(self, q): return float(np.percentile(self.a[:self.n], q)) if self.n else 0.0


class Detector:
    """Adaptive-threshold onset detector on a flux stream (one frame per call). Fires on the first rising frame over threshold."""
    def __init__(self, refractory, rel=0.45, k_local=3.0, floor=2e-3, rate=SR / HOP, lvl=0.0):
        self.lvl = lvl; self.ref_t, self.rel, self.k, self.floor, self.rate = refractory, rel, k_local, floor, rate
        self.roll = Roll(int(20 * rate)); self.p95 = floor; self.local = 0.0; self.prev = 0.0; self.last = -1e9
        self.n = 0; self.count = 0; self.strength = 0.0; self.fired_at = -1e9; self.frame = 0

    def step(self, f, t, gate=True, level=None):
        self.frame += 1; self.roll.push(f)
        if self.frame % int(self.rate / 4) == 0: self.p95 = max(self.roll.pct(95), self.floor)
        thr = max(self.rel * self.p95, self.k * self.local, self.floor)
        if level is not None: thr = max(thr, self.lvl * level)          # onset must be a real fraction of the band's own level
        fired = gate and f > thr and f >= self.prev and t - self.last >= self.ref_t
        if fired:
            self.last = t; self.count += 1; self.strength = 0.0; self.fired_at = t
        if t - self.fired_at < 0.04: self.strength = max(self.strength, min(1.0, f / (1.3 * self.p95)))   # peak of the rise
        self.local += (f - self.local) * (1 / self.rate) / 0.6; self.prev = f
        return fired


class Engine:
    """Pure feature extractor. push(mono float32 @ SR, t_end=None): t_end = epoch of the last sample (live), omit for file time."""
    def __init__(self, record=False):
        self.win = np.hanning(NFFT).astype(np.float32); self.scale = 2.0 / self.win.sum()
        fr = np.fft.rfftfreq(NFFT, 1 / SR); self.fr = fr
        self.bb = {k: (fr >= lo) & (fr < hi) for k, (lo, hi) in BANDS.items()}
        self.kb = (fr >= KICK_BAND[0]) & (fr < KICK_BAND[1]); self.hb = (fr >= HAT_BAND[0]) & (fr < HAT_BAND[1])
        self.fb = (fr >= 30) & (fr < 12000)
        self.buf = np.zeros(0, np.float32); self.n_samples = 0; self.nframes = 0; self.prev_mags = collections.deque(maxlen=FLUX_LAG)
        self.off = None; self.t_stream = 0.0; self.t_now = 0.0                      # offset stream->epoch, stream time of last frame end
        rate = SR / HOP
        names = list(BANDS) + ["rms", "flux"]
        self.env = {k: 0.0 for k in names}; self.roll = {k: Roll(int(20 * rate / 2)) for k in names}; self.lohi = {k: (0.0, FLOOR) for k in names}
        self.out = {k: 0.0 for k in names}; self.centroid = 0.5; self.raw_rms = 0.0
        self.kick = Detector(0.150, lvl=0.3); self.hat = Detector(0.060, lvl=0.20); self.lvl_k = 0.0; self.lvl_h = 0.0; self.g3 = collections.deque(maxlen=4)
        self.kick_t = None; self.hat_t = None; self.kicks = collections.deque(maxlen=256); self.hats = collections.deque(maxlen=256)
        self.record = record; self.rec_k = []; self.rec_h = []; self.rec_s = []
        # section state
        self.sec = "quiet"; self.sec_since = 0.0; self.next_sec = 0.0; self.e_s = 0.0; self.b_s = 0.0
        self.hist_e = Roll(900); self.hist_b = Roll(900); self.hist_trend = collections.deque(maxlen=100); self.hist_kick = collections.deque(maxlen=900)
        self.hat_fast = 0.0; self.hat_slow = 0.0; self.trend = 0.0; self.last_hat_count = 0; self.no_kick_since = None; self.nobuild_since = None
        self.n_ranges = {k: [1e9, -1e9] for k in names}
        self.lock = threading.Lock(); self.last_push_wall = None; self.sec_log = []

    # ---- time
    def epoch(self, ts):
        return ts + (self.off or 0.0)

    def push(self, x, t_end=None):
        with self.lock:
            x = np.asarray(x, np.float32)
            self.n_samples += len(x)
            if t_end is not None:
                cand = t_end - self.n_samples / SR
                self.off = cand if self.off is None or cand < self.off else self.off + 0.002 * (cand - self.off)
                self.last_push_wall = t_end
            self.buf = np.concatenate([self.buf, x]) if len(self.buf) else x
            if len(self.buf) < NFFT: return
            nfr = (len(self.buf) - NFFT) // HOP + 1
            idx = np.arange(nfr)[:, None] * HOP + np.arange(NFFT)[None, :]
            frames = self.buf[idx]
            first_end = (self.nframes * HOP + NFFT) / SR
            self.buf = self.buf[nfr * HOP:]
            rms = np.sqrt((frames * frames).mean(1))
            mags = np.abs(np.fft.rfft(frames * self.win, axis=1)).astype(np.float32) * self.scale
            self._frames(mags, rms, first_end)
            self.nframes += nfr

    def _frames(self, mags, rms, first_end):
        n = len(mags); lag = FLUX_LAG
        ref = np.concatenate([np.array(self.prev_mags).reshape(-1, mags.shape[1]), mags]) if self.prev_mags else mags
        npad = len(ref) - n                                                   # frames of history available (0..lag)
        for j in range(n): pass
        pw = mags * mags
        bands = {k: np.sqrt(pw[:, m].sum(1)) for k, m in self.bb.items()}
        # rectified flux against the frame `lag` back (clamped to oldest available)
        def flux(mask):
            cur = mags[:, mask]; prev = np.empty_like(cur)
            for j in range(n):
                p = j + npad - lag
                prev[j] = ref[max(p, 0), :][mask] if (p >= 0 or npad) else cur[j]
            return np.maximum(cur - prev, 0).sum(1)
        fk, fh, ff = flux(self.kb), flux(self.hb), flux(self.fb)
        fb150, flm = flux(self.bb["bass"]), flux(self.bb["lowmid"])
        ek, eh = mags[:, self.kb].sum(1), mags[:, self.hb].sum(1)
        for m in list(mags[-lag:]): self.prev_mags.append(m)
        cent_w = mags[:, self.fb]; cent = (cent_w * self.fr[self.fb]).sum(1) / (cent_w.sum(1) + 1e-9)
        rate = SR / HOP; dt = 1.0 / rate
        for j in range(n):
            ts = first_end + j * HOP / SR; self.t_stream = ts
            # envelopes: fast attack, ~120 ms release
            vals = {k: float(bands[k][j]) for k in BANDS}; vals["rms"] = float(rms[j]); vals["flux"] = float(ff[j])
            for k, v in vals.items():
                e = self.env[k]; a = 1.0 if v > e else dt / 0.12
                self.env[k] = v if v > e else e + (v - e) * a
            self.raw_rms = vals["rms"]
            if cent_w[j].sum() > 1e-4:
                self.centroid += (math.log(max(cent[j], 30) / 150.0) / math.log(5000 / 150.0) - self.centroid) * dt / 0.15
            self.frame_i = getattr(self, "frame_i", 0) + 1
            if self.frame_i % 2 == 0:
                for k in self.env: self.roll[k].push(self.env[k])
            if self.frame_i % int(rate / 4) == 0:
                for k in self.env:
                    lo, hi = self.roll[k].pct(5), self.roll[k].pct(98); self.lohi[k] = (lo, hi)
            for k in self.env:
                lo, hi = self.lohi[k]; den = max(hi - lo, 0.3 * hi, FLOOR)
                v = min(1.0, max(0.0, (self.env[k] - lo) / den)); self.out[k] = v
                r = self.n_ranges[k]; r[0] = min(r[0], v); r[1] = max(r[1], v)
            te = self.epoch(ts)
            self.lvl_k += (float(ek[j]) - self.lvl_k) * dt / 1.0; self.lvl_h += (float(eh[j]) - self.lvl_h) * dt / 1.0
            self.g3.append((float(fb150[j]), float(flm[j]), float(fh[j]), float(fk[j])))
            mb, mm = max(g[0] for g in self.g3), max(g[1] for g in self.g3); mh, mk = max(g[2] for g in self.g3), max(g[3] for g in self.g3)
            gate = 0.3 * mm <= mb and mm >= 0.6 * mb and mk >= HF_REJECT * mh      # kick = percussive low onset: mostly < 150 Hz (not clap/stab) but with punch above (not a pure bass tone)
            if self.kick.step(float(fk[j]), ts, gate, self.lvl_k):
                self.kick_t = te; self.kicks.append(te); self.hist_kick.append(ts)
                if self.record: self.rec_k.append(ts)
            if self.hat.step(float(fh[j]), ts, True, self.lvl_h):
                self.hat_t = te; self.hats.append(te)
                if self.record: self.rec_h.append(ts)
            if ts >= self.next_sec: self._section(ts); self.next_sec = ts + 0.1

    # ---- section classifier (10 Hz)
    def _section(self, ts):
        dt = 0.1
        self.e_s += (self.raw_rms - self.e_s) * dt / 1.2; self.b_s += (self.env["bass"] - self.b_s) * dt / 1.0
        self.hist_e.push(self.e_s); self.hist_b.push(self.b_s)
        ref_e = max(self.hist_e.pct(90), 1e-5); ref_b = max(self.hist_b.pct(90), 1e-5)
        en = min(1.5, self.e_s / ref_e); bn = min(1.5, self.b_s / ref_b)
        self.hist_trend.append(en)
        if len(self.hist_trend) >= 80:
            now = np.mean(list(self.hist_trend)[-20:]); old = np.mean(list(self.hist_trend)[:20])
            self.trend = float(np.clip((now - old) / 0.5, -1, 1))
        else: self.trend = 0.0
        hc = self.hat.count; self.hat_fast += ((hc - self.last_hat_count) / dt - self.hat_fast) * dt / 2.0
        self.hat_slow += ((hc - self.last_hat_count) / dt - self.hat_slow) * dt / 8.0; self.last_hat_count = hc
        ks = [t for t in self.hist_kick if ts - t < 12.0]
        last_age = ts - ks[-1] if ks else 1e9
        iv = np.diff(ks[-9:]); iv = iv[(iv > 0.25) & (iv < 1.0)]; period = float(np.median(iv)) if len(iv) >= 3 else 0.5
        recent = [t for t in ks if ts - t < 4.0]; recent15 = [t for t in ks if ts - t < 1.6]
        active = last_age < max(1.0, 2.2 * period) and len(recent) >= 3 and bn > 0.25
        self.hist_active = getattr(self, "hist_active", collections.deque(maxlen=900)); self.hist_active.append(active)
        groove_known = sum(self.hist_active) * dt >= 6.0
        silent = self.raw_rms < 1.5e-3 and self.env["rms"] < 1.5e-3
        cur, since = self.sec, self.sec_since; new = cur
        held = cur == "drop" and ts - since < 4.0
        kick_return = len(recent15) >= 2 and last_age < 1.0 and bn > 0.3           # kicks AND bass back strongly after a gap
        if silent: new = "quiet"
        elif held: new = "drop"
        elif cur in ("breakdown", "build") and kick_return: new = "drop"
        elif active: new = "groove"
        elif groove_known:                                                         # no steady kick, but a groove played recently
            rising = self.trend > 0.25 or self.hat_fast > 1.5 * self.hat_slow + 1.0
            if cur in ("breakdown", "build") and rising and en > 0.12: new = "build"
            elif cur == "build" and self.trend > -0.1: new = "build"
            else: new = "breakdown"
        else: new = "groove" if en > 0.35 else "quiet"                              # before any groove: sustained sound is 'groove'
        # hysteresis: min dwell 1.5 s between non-drop changes
        if new != cur and not (cur == "quiet" or new in ("drop", "quiet")) and ts - since < 1.5: new = cur
        if new != cur:
            self.sec = new; self.sec_since = ts
            self.sec_log.append((ts, new))

    def snapshot(self, now=None):
        with self.lock:
            o = self.out; t = now if now is not None else (time.time() if self.off is not None else self.t_stream)
            def det(d, tt, lst): return {"onset_t": tt, "strength": round(float(d.strength), 3) if tt else 0.0, "count": d.count}
            return {"t": t, "ok": True, "age_s": 0.0,
                    "rms": round(o["rms"], 4), "bass": round(o["bass"], 4), "lowmid": round(o["lowmid"], 4), "mid": round(o["mid"], 4),
                    "high": round(o["high"], 4), "kick": det(self.kick, self.kick_t, self.kicks), "hat": det(self.hat, self.hat_t, self.hats),
                    "centroid": round(float(min(1, max(0, self.centroid))), 4), "flux": round(o["flux"], 4), "section": self.sec,
                    "section_since": self.epoch(self.sec_since), "trend": round(self.trend, 3), "lead_ms": None}


def estimate_lead(kick_onset_times, beat_packet_times, max_offset=0.25, min_matches=1):
    """Median of (kick onset - nearest beat packet) in ms (positive = the mic hears it later than the deck announces it).
    Kicks further than max_offset from every beat (or than 45 % of the beat interval) are ignored. None when fewer than min_matches."""
    k = np.asarray(sorted(kick_onset_times), float); b = np.asarray(sorted(beat_packet_times), float)
    if len(k) == 0 or len(b) < 2: return None
    lim = min(max_offset, 0.45 * float(np.median(np.diff(b)))) if len(b) > 2 else max_offset
    i = np.clip(np.searchsorted(b, k), 1, len(b) - 1)
    near = np.where(np.abs(k - b[i - 1]) <= np.abs(k - b[i]), b[i - 1], b[i]); off = k - near
    off = off[np.abs(off) <= lim]
    return float(np.median(off) * 1000.0) if len(off) >= min_matches else None


class BeatListener:
    """RECEIVE-ONLY Pro DJ Link beat-packet listener (UDP 50001, SO_REUSEPORT so it coexists with other listeners). Never sends."""
    def __init__(self, port=50001, bind="0.0.0.0"):
        self.port, self.bind = port, bind; self.beats = {}; self.sock = None; self._stop = threading.Event()

    def feed(self, payload, t=None):
        b = bytes(payload)
        if len(b) < 0x22 or not b.startswith(MAGIC) or b[10] != 0x28: return False
        self.beats.setdefault(b[0x21], collections.deque(maxlen=256)).append(time.time() if t is None else t); return True

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try: s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError): pass
        s.bind((self.bind, self.port)); s.settimeout(0.5); self.sock = s
        threading.Thread(target=self._run, daemon=True, name="beatlisten").start(); return self

    def _run(self):
        while not self._stop.is_set():
            try: data, _ = self.sock.recvfrom(2048)
            except socket.timeout: continue
            except OSError: return
            self.feed(data)

    def stop(self):
        self._stop.set()
        try: self.sock.close()
        except Exception: pass

    def lead_ms(self, kicks, min_matches=16):
        """Best (lowest-spread) device estimate once enough kicks matched, else None."""
        best = None
        for dev, bt in list(self.beats.items()):
            bt = list(bt)
            if len(bt) < 8: continue
            k = [t for t in kicks if bt[0] - 0.5 <= t <= bt[-1] + 0.5]
            if len(k) < min_matches: continue
            b = np.asarray(bt); lim = min(0.25, 0.45 * float(np.median(np.diff(b))))
            kk = np.asarray(k); i = np.clip(np.searchsorted(b, kk), 1, len(b) - 1)
            off = kk - np.where(np.abs(kk - b[i - 1]) <= np.abs(kk - b[i]), b[i - 1], b[i]); off = off[np.abs(off) <= lim]
            if len(off) < min_matches: continue
            mad = float(np.median(np.abs(off - np.median(off))))
            if best is None or mad < best[0]: best = (mad, float(np.median(off) * 1000.0))
        return None if best is None else best[1]


# ---------------------------------------------------------------- synthetic audio (--sim, tests of the pipeline)
def synth_track(bpm=128, bars=64, sr=SR):
    """4/4 test structure (intro hats+bass, kick groove, breakdown pad, groove, outro) as float32 mono; also used by --sim."""
    beat = 60.0 / bpm; spb = int(round(beat * sr)); total = bars * 4 * spb; buf = np.zeros(total + sr)
    env = lambda n, d: np.exp(-np.arange(n) / (sr * d)); rg = np.random.default_rng(3)
    def add(snd, pos): buf[pos:pos + len(snd)] += snd[:max(0, len(buf) - pos)] if pos < len(buf) else 0
    n = int(sr * .25); tt = np.arange(n) / sr; kick = np.sin(2 * np.pi * np.cumsum(50 + 120 * np.exp(-tt * 30)) / sr) * env(n, .09) * .9
    hat = lambda: rg.standard_normal(int(sr * .05)) * env(int(sr * .05), .012) * .18
    clap = lambda: rg.standard_normal(int(sr * .18)) * env(int(sr * .18), .04) * .35
    for bar in range(bars):
        sec = bar % 64 + 1; kicks = 9 <= sec <= 32 or 41 <= sec <= 56
        for b in range(4):
            pos = int(round((bar * 4 + b) * beat * sr))
            if kicks: add(kick, pos)
            if (17 <= sec <= 32 or 41 <= sec <= 56) and b in (1, 3): add(clap(), pos)
            if not 33 <= sec <= 40:
                for h in (0, 1): add(hat() * (1.4 if h else 1.0), pos + h * spb // 2)
                m = spb; t2 = np.arange(m) / sr; f = (55.0, 65.4, 49.0, 58.3)[(bar // 4) % 4]
                add(np.sin(2 * np.pi * f * t2) * .35 * np.minimum(1, np.linspace(0, 1, m) * 20) * env(m, .5), pos + (spb // 2 if b % 2 else 0))
        if 33 <= sec <= 40 and bar % 2 == 0:
            m = 8 * spb // 2; t2 = np.arange(m) / sr; f = (55.0, 65.4, 49.0, 58.3)[(bar // 2) % 4] * 4
            add((np.sin(2 * np.pi * f * t2) + .5 * np.sin(2 * np.pi * f * 1.5 * t2)) * .12, pos if False else int(round(bar * 4 * beat * sr)))
    return (buf[:total] / max(1e-9, np.abs(buf).max()) * 0.8).astype(np.float32)


def decode_file(path):
    """Whole file -> mono float32 @ SR via an ffmpeg pipe (no temp files)."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout[:len(r.stdout) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0


def analyze_file(path, chunk=2048):
    e = Engine(record=True); x = decode_file(path)
    for i in range(0, len(x), chunk): e.push(x[i:i + chunk])
    return {"kicks": list(e.rec_k), "hats": list(e.rec_h), "sections": [(0.0, "quiet")] + list(e.sec_log), "duration": len(x) / SR,
            "ranges": {k: [round(v[0], 3), round(v[1], 3)] for k, v in e.n_ranges.items()}, "engine": e}


# ---------------------------------------------------------------- live object
class AudioFeatures:
    def __init__(self, out=None, source="mic", path=None, listen_beats=False, beat_port=50001, rate_hz=20.0, seconds=None):
        self.out, self.source, self.path, self.rate_hz, self.seconds = out, source, path, rate_hz, seconds
        self.eng = Engine(); self.beats = BeatListener(beat_port) if listen_beats else None
        self._stop = threading.Event(); self.threads = []; self.proc = None; self.lead = None; self.alive = False; self.t0 = None
        self._last = None

    def start(self):
        self.t0 = time.time(); self._stop.clear()
        if self.beats: self.beats.start()
        for fn in ((self._sim if self.source == "sim" else self._capture), self._publish):
            th = threading.Thread(target=fn, daemon=True, name=fn.__name__); th.start(); self.threads.append(th)
        return self

    def stop(self):
        self._stop.set()
        if self.proc:
            try: self.proc.kill()
            except Exception: pass
        if self.beats: self.beats.stop()
        for th in self.threads: th.join(timeout=1.5)

    def _capture(self):
        cmd = FFMPEG_MIC
        while not self._stop.is_set():
            try: self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            except OSError: self.alive = False; self._stop.wait(2.0); continue
            fd = self.proc.stdout.fileno(); rem = b""
            while not self._stop.is_set():
                try: d = os.read(fd, 4096)
                except OSError: break
                if not d: break
                d = rem + d; n = len(d) // 2 * 2; rem = d[n:]
                if n: self.alive = True; self._last = time.time(); self.eng.push(np.frombuffer(d[:n], dtype="<i2").astype(np.float32) / 32768.0, time.time())
            self.alive = False
            try: self.proc.kill()
            except Exception: pass
            self._stop.wait(2.0)

    def _sim(self):
        """Synthetic 4/4 track (starts at the first kick bar, loops through breakdown/drop/outro) paced in real time through the real pipeline."""
        x = synth_track(128, 64); x = x[int(8 * 4 * 60 / 128 * SR):]; i = 0; step = int(SR * 0.02); t0 = time.time(); n = 0
        while not self._stop.is_set():
            self.alive = True; self._last = time.time()
            self.eng.push(x[i:i + step], time.time()); i += step; n += 1
            if i + step > len(x): i = 0
            self._stop.wait(max(0.0, t0 + n * step / SR - time.time()))

    def snapshot(self):
        s = self.eng.snapshot(); now = time.time(); s["t"] = now
        age = now - self._last if self._last else 1e9
        s["age_s"] = round(min(age, 1e6), 3); s["ok"] = bool(self.alive and age < 1.0); s["lead_ms"] = self.lead
        return s

    def _publish(self):
        per = 1.0 / self.rate_hz; nxt = time.time(); last_lead = 0
        while not self._stop.is_set():
            if self.beats and time.time() - last_lead > 1.0:
                last_lead = time.time(); l = self.beats.lead_ms(list(self.eng.kicks))
                if l is not None: self.lead = round(l, 1) if self.lead is None else round(self.lead * 0.7 + l * 0.3, 1)
            if self.out:
                try:
                    tmp = str(self.out) + ".tmp"
                    with open(tmp, "w") as f: json.dump(self.snapshot(), f)
                    os.replace(tmp, self.out)
                except OSError: pass
            nxt += per; self._stop.wait(max(0.0, nxt - time.time()))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=None); ap.add_argument("--file"); ap.add_argument("--sim", action="store_true")
    ap.add_argument("--out", default=None); ap.add_argument("--no-out", action="store_true"); ap.add_argument("--listen-beats", action="store_true")
    ap.add_argument("--print", action="store_true"); ap.add_argument("--events", action="store_true")
    a = ap.parse_args()
    if a.file:
        r = analyze_file(a.file); eng = r.pop("engine"); r["n_kicks"], r["n_hats"] = len(r["kicks"]), len(r["hats"])
        if not a.events: r["kicks"] = r["kicks"][:5]; r["hats"] = r["hats"][:5]
        print(json.dumps(r, default=float)); 
        if a.out: Path(a.out).write_text(json.dumps(eng.snapshot()))
        return
    out = None if a.no_out else (a.out or DEFAULT_OUT)
    f = AudioFeatures(out=out, source="sim" if a.sim else "mic", listen_beats=a.listen_beats).start()
    end = time.time() + a.seconds if a.seconds else None; mx = {}; lastk = lasth = 0
    try:
        while end is None or time.time() < end:
            time.sleep(0.25); s = f.snapshot()
            for k in ("rms", "bass", "lowmid", "mid", "high", "centroid", "flux"): mx.setdefault(k, [9, -9]); mx[k] = [min(mx[k][0], s[k]), max(mx[k][1], s[k])]
            if a.print or sys.stdout.isatty():
                bar = lambda v: "#" * int(v * 10)
                print(f"{'ok ' if s['ok'] else 'NO '} rms {bar(s['rms']):10} bass {bar(s['bass']):10} high {bar(s['high']):10} kick {s['kick']['count']:4} hat {s['hat']['count']:4} "
                      f"{s['section']:9} trend {s['trend']:+.2f} lead {s['lead_ms']}", flush=True)
    except KeyboardInterrupt: pass
    s = f.snapshot(); f.stop()
    print(json.dumps({"ranges": mx, "kicks": s["kick"]["count"], "hats": s["hat"]["count"], "section": s["section"], "ok": s["ok"]}))


if __name__ == "__main__":
    main()
