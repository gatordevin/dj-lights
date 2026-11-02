"""Music synthesis in pure Python: oscillators, ADSR, chords, bass, noise drums, a mixer with voiceover ducking → WAV.
CLI:    python3 media/synth.py song [-o out/media/song.wav] [--seconds 60] [--bpm 96] [--key C] [--seed 1] [--sr 44100]
        python3 media/synth.py tone A4 [--wave saw] [--seconds 1] [-o out/media/tone.wav]
        python3 media/synth.py duck MUSIC.wav VOICE.wav [-o out/media/mix.wav] [--depth-db -14] [--music-db -6]
Python (signals are array('f') mono floats in -1..1; sr = sample rate, default SR = 44100):
  freq(note) -> Hz           "A4"=440, "C#3", "Eb5", or a MIDI number (69 = A4)
  osc(wave, hz, seconds, sr, amp=1, rng=None) -> signal   wave: sine | saw | square | triangle | noise
  adsr(n, a, d, s, r, sr) -> envelope (len n; a/d/r in s, s = sustain level 0..1; release ends at n)
  tone(note, seconds, wave="sine", env=(a,d,s,r), amp, sr, detune_cents=0, cutoff=None) -> enveloped (and low-passed) note
  lowpass(sig, cutoff_hz, sr) / highpass(...) -> one-pole filters
  chord_notes("Am", octave=4) -> ["A4","C5","E5"]   (maj, m, 7, maj7, m7, sus2, sus4, dim; root may have #/b)
  kick(sr) / snare(sr, rng) / hat(sr, rng, open_=False) -> one drum hit each
  Track(seconds, sr): .add(sig, at_s, gain=1)  sums clips onto a timeline; .samples
  song(seconds=60, bpm=96, key="C", progression=("I","V","vi","IV"), seed=1, sr) -> signal  original background track:
      intro (pad) → +bass/arp → +drums → outro fade; deterministic for a seed
  envelope_follow(sig, sr, window_s=0.02) -> [rms per window];  duck_gain(voice, sr, n, depth_db, threshold_db, attack_s, release_s)
  duck(music, voice, sr, depth_db=-14, threshold_db=-45, attack_s=0.04, release_s=0.35) -> music with gain reduced under speech
  mix([(sig, gain_db, pan), ...], seconds=None, peak_db=-1) -> (left, right) normalised so the peak = peak_db
  write_wav(path, sig_or_lr, sr) -> Path   16-bit PCM, mono or stereo;  read_wav(path, sr=None) -> (mono signal, sr)
      (read_wav resamples linearly when sr is given and differs; channels are averaged)
Fails: ValueError on unknown wave/note/chord/sr/negative durations; FileNotFoundError on missing WAV; ValueError for
  non-16-bit WAVs (use media.audio.decode first).
Limits: naive (aliasing) saw/square tempered by one-pole low-pass; ~1-2 s CPU per voice-minute at 44.1 kHz (pure Python);
  mono synthesis with constant-power pan only in mix(); no reverb. Never plays audio (files only).
Depends on: stdlib only (wave, array, math, random).  Used by: tasks/explainer_video.py, tests/test_media_video.py."""
import sys, math, wave, array, random, re, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SR = 44100
WAVES = ("sine", "saw", "square", "triangle", "noise")
_NOTE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_CHORDS = {"": (0, 4, 7), "maj": (0, 4, 7), "m": (0, 3, 7), "7": (0, 4, 7, 10), "maj7": (0, 4, 7, 11),
           "m7": (0, 3, 7, 10), "sus2": (0, 2, 7), "sus4": (0, 5, 7), "dim": (0, 3, 6)}
_DEGREES = {"I": (0, ""), "ii": (2, "m"), "iii": (4, "m"), "IV": (5, ""), "V": (7, ""), "vi": (9, "m"), "vii": (11, "dim")}

def _check_sr(sr):
    if not (isinstance(sr, int) and 8000 <= sr <= 192000): raise ValueError(f"sample rate must be int 8000..192000, got {sr!r}")

def midi(note):
    if isinstance(note, (int, float)) and not isinstance(note, bool): return float(note)
    m = re.fullmatch(r"([A-Ga-g])([#b]?)(-?\d)", str(note).strip())
    if not m: raise ValueError(f"bad note {note!r} (want e.g. 'A4', 'C#3', 'Eb5' or a MIDI number)")
    return 12 * (int(m.group(3)) + 1) + _NOTE[m.group(1).upper()] + {"#": 1, "b": -1, "": 0}[m.group(2)]

def freq(note): return 440.0 * 2 ** ((midi(note) - 69) / 12)

def _name(m):
    return ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"][int(m) % 12] + str(int(m) // 12 - 1)

def chord_notes(name, octave=4):
    m = re.fullmatch(r"([A-G][#b]?)(maj7|maj|m7|m|7|sus2|sus4|dim|)", str(name).strip())
    if not m: raise ValueError(f"bad chord {name!r} (root + one of {sorted(_CHORDS)})")
    root = midi(m.group(1) + str(octave))
    return [_name(root + iv) for iv in _CHORDS[m.group(2)]]

def osc(wave_, hz, seconds, sr=SR, amp=1.0, rng=None):
    if wave_ not in WAVES: raise ValueError(f"wave must be one of {WAVES}, got {wave_!r}")
    if seconds < 0: raise ValueError("seconds must be >= 0")
    _check_sr(sr); n = int(round(seconds * sr)); inc = hz / sr
    if wave_ == "noise":
        r = rng or random.Random(0); return array.array("f", (amp * (2 * r.random() - 1) for _ in range(n)))
    if wave_ == "sine":
        w = 2 * math.pi * inc; return array.array("f", (amp * math.sin(w * i) for i in range(n)))
    if wave_ == "saw": return array.array("f", (amp * (2 * ((i * inc) % 1.0) - 1) for i in range(n)))
    if wave_ == "square": return array.array("f", (amp if (i * inc) % 1.0 < 0.5 else -amp for i in range(n)))
    return array.array("f", (amp * (4 * abs(((i * inc) % 1.0) - 0.5) - 1) for i in range(n)))   # triangle

def adsr(n, a, d, s, r, sr=SR):
    if min(a, d, r) < 0 or not 0 <= s <= 1: raise ValueError("adsr: a, d, r >= 0 and 0 <= s <= 1")
    A, D, R = int(a * sr), int(d * sr), int(r * sr)
    rel_start = max(0, n - R); env = array.array("f", bytes(4 * n))
    for i in range(n):
        if i < A: v = i / A
        elif i < A + D: v = 1 - (1 - s) * (i - A) / D
        else: v = s
        if i >= rel_start and R: v *= (n - i) / R
        env[i] = v
    return env

def lowpass(sig, cutoff, sr=SR):
    if cutoff <= 0: raise ValueError("cutoff must be > 0")
    k = 1 - math.exp(-2 * math.pi * cutoff / sr); y = 0.0; out = array.array("f", sig)
    for i, x in enumerate(sig): y += k * (x - y); out[i] = y
    return out

def highpass(sig, cutoff, sr=SR):
    lp = lowpass(sig, cutoff, sr); return array.array("f", (x - l for x, l in zip(sig, lp)))

def tone(note, seconds, wave_="sine", env=(0.01, 0.1, 0.7, 0.1), amp=0.5, sr=SR, detune_cents=0.0, cutoff=None, rng=None):
    hz = freq(note) * 2 ** (detune_cents / 1200)
    sig = osc(wave_, hz, seconds, sr, 1.0, rng)
    if cutoff: sig = lowpass(sig, cutoff, sr)
    e = adsr(len(sig), *env, sr=sr)
    return array.array("f", (amp * x * v for x, v in zip(sig, e)))

def kick(sr=SR, amp=0.9):
    n = int(0.35 * sr); ph = 0.0; out = array.array("f", bytes(4 * n))
    for i in range(n):
        t = i / sr; ph += 2 * math.pi * (45 + 75 * math.exp(-t * 30)) / sr
        out[i] = amp * math.sin(ph) * math.exp(-t * 9)
    return out

def snare(sr=SR, rng=None, amp=0.5):
    r = rng or random.Random(1); n = int(0.22 * sr)
    nz = highpass(array.array("f", (2 * r.random() - 1 for _ in range(n))), 1200, sr)
    return array.array("f", (amp * (0.8 * nz[i] + 0.4 * math.sin(2 * math.pi * 185 * i / sr)) * math.exp(-i / sr * 18)
                             for i in range(n)))

def hat(sr=SR, rng=None, open_=False, amp=0.25):
    r = rng or random.Random(2); n = int((0.25 if open_ else 0.05) * sr); dec = 12 if open_ else 70
    nz = highpass(array.array("f", (2 * r.random() - 1 for _ in range(n))), 6000, sr)
    return array.array("f", (amp * nz[i] * math.exp(-i / sr * dec) for i in range(n)))

class Track:
    """A mono timeline of fixed length; add() sums clips (clipped at the end)."""
    def __init__(self, seconds, sr=SR):
        _check_sr(sr)
        if seconds <= 0: raise ValueError("Track seconds must be > 0")
        self.sr = sr; self.samples = array.array("f", bytes(4 * int(round(seconds * sr))))
    def add(self, sig, at_s, gain=1.0):
        s, buf = int(round(at_s * self.sr)), self.samples
        if s < 0: sig, s = sig[-s:], 0
        for i in range(min(len(sig), len(buf) - s)): buf[s + i] += gain * sig[i]
        return self

def _degree_chord(key, deg):
    off, q = _DEGREES[deg]
    return _name(midi(key + "4") + off)[:-1] + q

def song(seconds=60, bpm=96, key="C", progression=("I", "V", "vi", "IV"), seed=1, sr=SR):
    """Original background track, peak-normalised to 0.8; sections scale with `seconds` (intro 1/8, build, full, outro fade)."""
    _check_sr(sr)
    if seconds < 4: raise ValueError("song needs >= 4 s")
    if not (40 <= bpm <= 220): raise ValueError("bpm must be 40..220")
    if key not in ("C", "C#", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"): raise ValueError(f"bad key {key!r}")
    for d in progression:
        if d not in _DEGREES: raise ValueError(f"bad degree {d!r} (use {sorted(_DEGREES)})")
    rng, beat = random.Random(seed), 60.0 / bpm; bar = 4 * beat
    nbars = int(math.ceil(seconds / bar)); total = nbars * bar + 1.0
    pad, bass, arp, drums = Track(total, sr), Track(total, sr), Track(total, sr), Track(total, sr)
    intro, full = max(1, nbars // 8), max(2, nbars // 4)
    k, sn, hh = kick(sr), snare(sr, rng), hat(sr, rng)
    for b in range(nbars):
        t0 = b * bar; ch = _degree_chord(key, progression[b % len(progression)])
        notes = chord_notes(ch, 3)
        for j, nt in enumerate(notes):            # pad: two detuned saws per chord tone, soft and low-passed
            for dc in (-7, 7):
                pad.add(tone(nt, bar + 0.4, "saw", (0.6, 0.4, 0.8, 0.6), 0.07, sr, dc, cutoff=900), t0)
        if b >= intro:                            # bass: root on 1 and the "and" of 2, octave on 3
            root = midi(notes[0]) - 12
            for at, m, ln in ((0, root, 1.4), (1.5, root, 0.4), (2, root + 12, 0.9), (3, root + 7, 0.9)):
                bass.add(tone(m, ln * beat, "triangle", (0.005, 0.08, 0.6, 0.08), 0.35, sr, cutoff=600), t0 + at * beat)
            up = [midi(n) + 12 for n in notes] + [midi(notes[0]) + 24]
            for s in range(8):                    # arpeggio: 8ths over chord tones, pattern varies per bar
                m = up[(s + (b % 3)) % len(up)] if s % 4 != 3 else up[rng.randrange(len(up))]
                arp.add(tone(m, beat * 0.45, "square", (0.003, 0.12, 0.25, 0.1), 0.05, sr, cutoff=2500), t0 + s * beat / 2)
        if b >= intro + 1:
            for s in range(8): drums.add(hh, t0 + s * beat / 2, 0.8 if s % 2 else 1.0)
        if b >= full and b < nbars - 1:
            for bt in (0, 2): drums.add(k, t0 + bt * beat)
            if b % 4 == 3: drums.add(k, t0 + 3.5 * beat, 0.7)
            for bt in (1, 3): drums.add(sn, t0 + bt * beat)
    n = int(round(seconds * sr)); fade = min(2 * bar, seconds / 4)
    out = array.array("f", bytes(4 * n))
    for i in range(n):
        t = i / sr; g = min(1.0, t / 0.5, (seconds - t) / fade)
        out[i] = g * (pad.samples[i] + bass.samples[i] + arp.samples[i] + 0.8 * drums.samples[i])
    peak = max(map(abs, out), default=0)
    if peak > 0:
        k = 0.8 / peak
        for i in range(n): out[i] *= k
    return out

def envelope_follow(sig, sr=SR, window_s=0.02):
    from media.audio import rms_envelope                    # one RMS-envelope implementation (media/audio)
    return rms_envelope(sig, sr, window_s, full_scale=1.0)

def duck_gain(voice, sr, n, depth_db=-14, threshold_db=-45, attack_s=0.04, release_s=0.35, window_s=0.02):
    """Per-sample gain (len n): 1 when the voice is quiet, 10**(depth_db/20) under speech, with attack/release smoothing."""
    if depth_db > 0: raise ValueError("depth_db must be <= 0")
    env, thr, low = envelope_follow(voice, sr, window_s), 10 ** (threshold_db / 20), 10 ** (depth_db / 20)
    ka, kr = 1 - math.exp(-window_s / max(attack_s, 1e-4)), 1 - math.exp(-window_s / max(release_s, 1e-4))
    win, g, gains = max(1, int(sr * window_s)), 1.0, []
    for e in env:
        tgt = low if e > thr else 1.0; g += (ka if tgt < g else kr) * (tgt - g); gains.append(g)
    out = array.array("f", bytes(4 * n))
    for i in range(n):
        j = i // win; out[i] = gains[j] if j < len(gains) else 1.0
    return out

def duck(music, voice, sr=SR, depth_db=-14, threshold_db=-45, attack_s=0.04, release_s=0.35):
    g = duck_gain(voice, sr, len(music), depth_db, threshold_db, attack_s, release_s)
    return array.array("f", (m * x for m, x in zip(music, g)))

def mix(tracks, seconds=None, sr=SR, peak_db=-1.0):
    """tracks = [(signal, gain_db, pan -1..1), ...] → (left, right), normalised to peak_db (never boosts > +12 dB)."""
    if not tracks: raise ValueError("mix: no tracks")
    n = int(round(seconds * sr)) if seconds else max(len(s) for s, *_ in tracks)
    L, R = array.array("f", bytes(4 * n)), array.array("f", bytes(4 * n))
    for sig, gdb, pan in tracks:
        if not -1 <= pan <= 1: raise ValueError("pan must be in -1..1")
        g = 10 ** (gdb / 20); a = (pan + 1) * math.pi / 4
        gl, gr = g * math.cos(a) * math.sqrt(2), g * math.sin(a) * math.sqrt(2)
        for i in range(min(n, len(sig))): x = sig[i]; L[i] += gl * x; R[i] += gr * x
    peak = max(max(map(abs, L), default=0), max(map(abs, R), default=0))
    if peak > 0:
        k = min(10 ** (peak_db / 20) / peak, 4.0)
        for buf in (L, R):
            for i in range(n): buf[i] *= k
    return L, R

def write_wav(path, sig, sr=SR):
    _check_sr(sr); path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    chans = list(sig) if isinstance(sig, tuple) else [sig]
    n = min(len(c) for c in chans); data = array.array("h", bytes(2 * n * len(chans)))
    for c, ch in enumerate(chans):
        for i in range(n): data[i * len(chans) + c] = int(max(-1.0, min(1.0, ch[i])) * 32767)
    if sys.byteorder == "big": data.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(len(chans)); w.setsampwidth(2); w.setframerate(sr); w.writeframes(data.tobytes())
    return path

def read_wav(path, sr=None):
    if not Path(path).exists(): raise FileNotFoundError(path)
    with wave.open(str(path)) as w:
        if w.getsampwidth() != 2: raise ValueError(f"{path}: need 16-bit PCM WAV (use media.audio.decode)")
        ch, rate, raw = w.getnchannels(), w.getframerate(), array.array("h", w.readframes(w.getnframes()))
    if sys.byteorder == "big": raw.byteswap()
    mono = array.array("f", (sum(raw[i:i + ch]) / (ch * 32768) for i in range(0, len(raw), ch))) if ch > 1 else \
        array.array("f", (x / 32768 for x in raw))
    if sr and sr != rate:
        _check_sr(sr); step = rate / sr; m = int(len(mono) / step)
        mono = array.array("f", ((lambda p, f: mono[p] * (1 - f) + mono[min(p + 1, len(mono) - 1)] * f)(int(i * step), i * step % 1)
                                 for i in range(m)))
        rate = sr
    return mono, rate

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Pure-Python music synthesis → WAV (never plays)")
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("song"); s.add_argument("-o", default="out/media/song.wav"); s.add_argument("--seconds", type=float, default=60)
    s.add_argument("--bpm", type=float, default=96); s.add_argument("--key", default="C"); s.add_argument("--seed", type=int, default=1)
    s.add_argument("--sr", type=int, default=SR)
    t = sp.add_parser("tone"); t.add_argument("note"); t.add_argument("--wave", default="sine")
    t.add_argument("--seconds", type=float, default=1.0); t.add_argument("-o", default="out/media/tone.wav")
    d = sp.add_parser("duck"); d.add_argument("music"); d.add_argument("voice"); d.add_argument("-o", default="out/media/mix.wav")
    d.add_argument("--depth-db", type=float, default=-14); d.add_argument("--music-db", type=float, default=-6)
    a = ap.parse_args()
    if a.cmd == "song": p = write_wav(a.o, song(a.seconds, a.bpm, a.key, seed=a.seed, sr=a.sr), a.sr)
    elif a.cmd == "tone": p = write_wav(a.o, tone(a.note, a.seconds, a.wave, amp=0.5))
    else:
        m, sr = read_wav(a.music); v, _ = read_wav(a.voice, sr)
        p = write_wav(a.o, mix([(duck(m, v, sr, a.depth_db), a.music_db, 0), (v, 0, 0)], sr=sr), sr)
    print(f"wrote {p}")
