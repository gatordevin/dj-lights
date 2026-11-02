"""Analyse any song file for lights: tempo, beat grid, downbeats/bars, energy per bar, drops, builds, breakdowns, intro/outro.
CLI:
  python3 lighting/songmap.py FILE [FILE...] [--bpm 128] [--bpm-range 70 180] [--vocals] [--json] [--force]
  python3 lighting/songmap.py FOLDER            every music/video file in it (DJ crate prep); results are cached
Formats: anything ffmpeg decodes — mp3, wav, aiff, flac, alac/m4a/aac, ogg, opus, wma, and the audio track of video
  (mp4, mov, mkv, webm, avi). Use --bpm when you know it (fixes half/double-tempo picks); --bpm-range narrows the search.
Python:
  analyze(path, bpm=None, bpm_range=(70, 180), vocals=False, force=False, rekordbox_xml=None) -> songmap dict (cached in
      out/lighting/songs/). rekordbox_xml (or env LIGHTING_REKORDBOX_XML): use the DJ's own grid from a rekordbox collection
      XML export when the track is in it. set_fix(path, shift=0.5 | bar=1 | bpm=128) remembers a grid correction for a
      file in lighting/song_fixes.json (applied on every later analysis); get_fix(path).
  songmap = {"schema", "file", "title", "artist", "duration_s", "bpm", "bpm_confidence", "constant_tempo",
             "beats": [s...], "downbeat_index": i (beats[i] = bar 1 beat 1), "first_downbeat_s", "nbars",
             "bars": [{"bar", "t", "energy", "low", "mid", "high", "kick", "vocal"?}] (features 0..1 within the song),
             "sections": [{"type": intro|groove|build|drop|peak|breakdown|outro, "start_bar", "end_bar"}] (bars inclusive),
             "drops": [bar...], "vocal_spans"?: [[s0, s1]...], "notes": [...], "analysis_s"}
  beat_at(songmap, t) -> fractional beat number with 0 = bar 1 beat 1 (negative before it), time_at(songmap, beat) -> s
  is_audio_file(path) -> bool    find_audio(folder) -> [Path]
Method (numpy): ffmpeg -> mono 22.05 kHz -> 2048/512 STFT -> 40 log bands -> spectral-flux onset envelope. Tempo:
  autocorrelation over 60-200 BPM x a log-normal prior at 120 BPM folded into bpm_range. Grid: phase-coherence search over
  +-1.5% period then continuous refine (integer BPM snapped when as good). Variable tempo (live recordings): a 16-beat
  window drift check, then dynamic-programming beat tracking (Ellis 2007). Downbeat: the beat phase (of 4) with the most
  harmony change + loudness jumps + low-end hits. Sections from per-bar low/mid/high energy and kick presence on a 4-bar
  phrase grid; drop = bass+kick returning after a low stretch; build = the bars leading into a drop; vocals (optional,
  --vocals) = Demucs vocal stem activity via lighting/stems.py in the stems Python (LIGHTING_STEMS_PY, else ./.venv-stems
  when present).
Accuracy (lighting/README "songmap accuracy", vs rekordbox grids on the owner's library): see README for current numbers.
Fails: FileNotFoundError; ValueError when ffmpeg cannot decode the file or it has no audio / is shorter than 8 s;
  RuntimeError when ffmpeg is missing (install: brew install ffmpeg) or --vocals is asked without a stems Python.
Limits: 4/4 assumed (bars = 4 beats); tempo changes inside a song are tracked but sections assume a steady phrase grid;
  sections are energy heuristics, not musical understanding — check with `dj.py show FILE` (plot) and adjust cues if needed.
Depends on: numpy, ffmpeg/ffprobe, lighting/audio_in (decode_file). Used by: lighting/autoshow.py, lighting/dj.py, tests/test_lighting_dj.py."""
import sys, os, os, json, math, time, hashlib, subprocess, argparse, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np

VERSION = 11
SR, NFFT, HOP = 22050, 2048, 512
FPS = SR / HOP
LAG_S = 0.077          # onset-envelope latency (window + flux peak) vs the attack, measured against rekordbox grids
ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "out" / "lighting" / "songs"
EXTS = {".mp3", ".wav", ".wave", ".aif", ".aiff", ".flac", ".m4a", ".aac", ".alac", ".ogg", ".oga", ".opus", ".wma",
        ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".caf", ".mp2"}
FIXES = Path(__file__).resolve().parent / "song_fixes.json"      # DJ grid corrections (user data): {abs path: {"shift", "bar", "bpm"}}
STEMS_FALLBACK = Path(__file__).resolve().parents[1] / ".venv-stems" / "bin" / "python"


def is_audio_file(p): return Path(p).suffix.lower() in EXTS and not Path(p).name.startswith("._")


def find_audio(folder):
    return sorted(p for p in Path(folder).rglob("*") if p.is_file() and is_audio_file(p))


# ---------- decode ----------
def probe(path):
    if not shutil.which("ffprobe"): return {}
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], capture_output=True, text=True)
    if r.returncode: return {}
    d = json.loads(r.stdout or "{}"); tags = {k.lower(): v for k, v in d.get("format", {}).get("tags", {}).items()}
    st = d.get("streams", [])
    return {"format": d.get("format", {}).get("format_name"), "duration_s": float(d.get("format", {}).get("duration") or 0),
            "title": tags.get("title"), "artist": tags.get("artist"), "has_audio": any(s.get("codec_type") == "audio" for s in st),
            "has_video": any(s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1 for s in st)}


def decode(path):
    """Mono float32 @ SR via lighting.audio_in.decode_file (the one ffmpeg decoder), with errors explained."""
    from lighting.audio_in import decode_file, SR as AUDIO_SR
    if AUDIO_SR != SR: raise RuntimeError("songmap and audio_in sample rates differ")
    if not shutil.which("ffmpeg"): raise RuntimeError("ffmpeg not found: install it (brew install ffmpeg) to read music files")
    try: x = decode_file(path)
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or b"").decode(errors="replace").strip().splitlines()
        raise ValueError(f"cannot decode {Path(path).name}: {msg[-1] if msg else 'not an audio/video file'}") from None
    if not len(x): raise ValueError(f"cannot decode {Path(path).name}: no audio track")
    return x


# ---------- features ----------
def _stft_bands(x):
    """Per-frame band energies: 40 log bands 30 Hz-11 kHz, plus low/mid/high and a 12-bin chroma (100-2000 Hz)."""
    n = 1 + max(0, (len(x) - NFFT) // HOP); win = np.hanning(NFFT).astype(np.float32)
    freqs = np.fft.rfftfreq(NFFT, 1 / SR); edges = np.geomspace(30, 11000, 41)
    bidx = np.clip(np.searchsorted(edges, freqs) - 1, -1, 39)
    pitch = np.where((freqs >= 100) & (freqs <= 2000), np.round(12 * np.log2(np.maximum(freqs, 1) / 440.0)).astype(int) % 12, -1)
    lo, mi, hi = (freqs >= 30) & (freqs < 150), (freqs >= 150) & (freqs < 2500), (freqs >= 2500) & (freqs < 11000)
    bands = np.zeros((n, 40), np.float32); chroma = np.zeros((n, 12), np.float32); lmh = np.zeros((n, 3), np.float32)
    M = np.zeros((40, len(freqs)), np.float32); M[bidx[bidx >= 0], np.nonzero(bidx >= 0)[0]] = 1
    C = np.zeros((12, len(freqs)), np.float32); C[pitch[pitch >= 0], np.nonzero(pitch >= 0)[0]] = 1
    L = np.stack([lo, mi, hi]).astype(np.float32)
    for s in range(0, n, 2048):
        idx = np.arange(s, min(n, s + 2048))[:, None] * HOP + np.arange(NFFT)[None]
        mag = np.abs(np.fft.rfft(x[idx] * win, axis=1)).astype(np.float32); pw = mag * mag
        bands[s:s + len(idx)] = mag @ M.T; chroma[s:s + len(idx)] = pw @ C.T; lmh[s:s + len(idx)] = pw @ L.T
    return bands, chroma, lmh


def _onset(bands):
    lb = np.log1p(100 * bands / (bands.max() + 1e-9))
    d = np.maximum(0, np.diff(lb, axis=0, prepend=lb[:1])); o = d.sum(1)
    low = d[:, :8].sum(1)                                                     # 30-~95 Hz: kick
    punch = d[:, :14].sum(1)                                                  # 30-~190 Hz: kick + bass attack (beat phase)
    k = int(FPS)                                                              # remove the slow trend (1 s)
    trend = np.convolve(o, np.ones(k) / k, "same"); o = np.maximum(0, o - trend)
    return o / (o.std() + 1e-9), low / (low.std() + 1e-9), punch / (punch.std() + 1e-9)


def _tempo(o, bpm_range, hint=None):
    """Coarse BPM: onset autocorrelation with harmonic support and a 120-BPM log-normal prior folded into bpm_range."""
    oc = o - o.mean(); ac = np.correlate(oc, oc, "full")[len(oc) - 1:]; ac = ac / (ac[0] + 1e-9)
    def at(lag):
        i = int(lag); f = lag - i
        return ac[i] * (1 - f) + ac[i + 1] * f if i + 1 < len(ac) else 0.0
    def score(bpm):
        L = 60 * FPS / bpm; return at(L) + 0.5 * at(2 * L) + 0.25 * at(4 * L) + 0.25 * at(L / 2)
    if hint:
        cands = np.arange(hint * 0.97, hint * 1.03, 0.05)
        return float(cands[np.argmax([score(b) for b in cands])]), 1.0
    lo, hi = bpm_range; bpms = np.arange(lo, hi, 0.25)
    raw = np.array([score(b) for b in bpms])
    prior = np.exp(-0.5 * (np.log2(bpms / 120.0) / 0.9) ** 2)
    best = bpms[int(np.argmax(raw * prior))]
    conf = float(np.clip(raw.max() / (np.median(np.abs(raw)) * 4 + 1e-9), 0, 1))
    return float(best), conf


def _grid(o, bpm, n):
    """Constant beat grid: best period within +-1.5 % and phase by folding the onset envelope (phase coherence)."""
    t = np.arange(n) / FPS; w = o.copy()
    best = (-1, bpm, 0.0)
    for b in np.linspace(bpm * 0.985, bpm * 1.015, 241):
        P = 60.0 / b; ph = (t % P) / P; h = np.bincount(np.minimum((ph * 96).astype(int), 95), weights=w, minlength=96)
        h = h + np.roll(h, 1) * 0.5 + np.roll(h, -1) * 0.5; k = int(np.argmax(h))
        if h[k] > best[0]: best = (h[k], b, (k + 0.5) / 96 * P)
    _, b0, phi = best

    def fit(b, phi):
        P = 60.0 / b; beats = np.arange(phi, n / FPS, P); fi = beats * FPS
        return np.interp(fi, np.arange(n), o).sum() / max(1, len(beats))
    # continuous refine (coordinate search on bpm and phase)
    step_b, step_p = 0.02, 0.004
    cur = fit(b0, phi)
    for _ in range(40):
        moved = False
        for db, dp in ((step_b, 0), (-step_b, 0), (0, step_p), (0, -step_p)):
            v = fit(b0 + db, phi + dp)
            if v > cur + 1e-9: cur, b0, phi, moved = v, b0 + db, phi + dp, True
        if not moved:
            step_b /= 2; step_p /= 2
            if step_b < 0.0005: break
    r = round(b0)
    if abs(b0 - r) < 0.06:                                                    # most DJ tracks are integer BPM
        alt = max(((fit(r, phi + dp), phi + dp) for dp in np.linspace(-0.012, 0.012, 25)))
        if alt[0] >= cur * 0.995: b0, phi = float(r), alt[1]
    P = 60.0 / b0; phi = phi % P
    return b0, phi


def _drift(o, beats, P):
    """Per-16-beat best local offset (s) vs the grid, and which windows have enough rhythm to judge (onset energy >= median)."""
    offs, power = [], []; n = len(o)
    for s in range(0, len(beats) - 16, 16):
        bt = beats[s:s + 16]; best = (-1, 0.0)
        for dt in np.linspace(-0.2 * P, 0.2 * P, 41):
            v = np.interp((bt + dt) * FPS, np.arange(n), o).sum()
            if v > best[0]: best = (v, dt)
        offs.append(best[1]); power.append(best[0])
    power = np.array(power); return np.array(offs), power >= np.median(power) if len(power) else power.astype(bool)


def _drifts(offs, P):
    """True when the rhythmic windows wander off a steady grid: a spread > 70 ms that is not just a few outliers."""
    spread = np.percentile(offs, 90) - np.percentile(offs, 10)
    return spread > 0.07 and np.mean(np.abs(offs - np.median(offs)) > 0.035) > 0.3


def _on_kick(punch, phi, P, n):
    """Beats sit on the kick: phi or phi + half a beat, whichever has more 30-190 Hz attack (agreed with rekordbox on 89 % of
    70 library tracks; the full-band phase alone: 76 %, offbeat hats and basslines pull it)."""
    t = np.arange(phi, n / FPS - 0.05, P); g = lambda tt: np.interp(tt * FPS, np.arange(n), punch).mean()
    return phi + P / 2 if g(t + P / 2) > g(t) else phi


def _dp_beats(o, bpm, tight=100.0):
    """Dynamic-programming beat tracker (Ellis 2007) for songs whose tempo moves."""
    P = 60 * FPS / bpm; n = len(o); C = o.astype(np.float64).copy(); back = -np.ones(n, int)
    lo, hi = int(round(P / 2)), int(round(2 * P))
    lags = np.arange(lo, hi + 1); pen = -tight * np.log(lags / P) ** 2
    for t in range(hi, n):
        cand = C[t - lags] + pen; k = int(np.argmax(cand)); C[t] = o[t] + cand[k]; back[t] = t - lags[k]
    t = int(np.argmax(C[-int(P):])) + n - int(P); out = []
    while t >= 0: out.append(t); t = back[t]
    return np.array(out[::-1], float) / FPS


def _downbeat(beats, bands, chroma, lmh, punch, n):
    """Index (0..3) of the first downbeat among beats[0:4], and a confidence margin. Equal-weight evidence that changes land on
    bar lines: loudness jump, 4- and 8-beat band-energy change, 2-beat harmony change, + half-weight bass punch (82 % vs
    rekordbox downbeats on 60 library tracks; the earlier harmony+jump+kick mix: 70 %)."""
    if len(beats) < 16: return 0, 0.0
    fi = np.clip((beats * FPS).astype(int), 0, n - 1); edges = np.append(fi, n)
    seg = lambda M: np.array([M[a:max(a + 1, b)].mean(0) for a, b in zip(edges[:-1], edges[1:])])
    lb = seg(np.log1p(100 * bands / (bands.max() + 1e-9))); ch = seg(chroma)
    ch = ch / (np.linalg.norm(ch, axis=1, keepdims=True) + 1e-9)
    rms = np.log1p(seg(lmh).sum(1) * 1e3); jump = np.r_[0, np.abs(np.diff(rms))]
    def checker(M, h):
        out = np.zeros(len(M))
        for k in range(h, len(M) - h): out[k] = np.linalg.norm(M[k - h:k].mean(0) - M[k:k + h].mean(0))
        return out
    hit = np.array([punch[max(0, i - 2):i + 3].max() for i in fi])
    z = lambda v: (v - v.mean()) / (v.std() + 1e-9)
    s = z(jump) + z(checker(lb, 4)) + z(checker(lb, 8)) + z(checker(ch, 2)) + 0.5 * z(hit)
    per = np.array([s[p::4].mean() for p in range(4)]); order = np.sort(per)
    return int(np.argmax(per)), float(order[-1] - order[-2])


def _norm(v):
    lo, hi = np.percentile(v, 5), np.percentile(v, 95)
    return np.clip((v - lo) / (hi - lo + 1e-9), 0, 1)


def _sections(feats, nb):
    """Per-bar labels -> sections + drop bars (see module docstring)."""
    low, mid, high, kick, en = (np.array([b[k] for b in feats]) for k in ("low", "mid", "high", "kick", "energy"))
    if nb < 8: return [{"type": "groove", "start_bar": 1, "end_bar": max(1, nb)}], []
    sm = lambda v, k=2: np.convolve(np.r_[v[:1].repeat(k), v, v[-1:].repeat(k)], np.ones(2 * k + 1) / (2 * k + 1), "valid")
    E, LO, K = sm(en, 1), sm(low, 1), sm(kick, 1)
    lab = np.array(["groove"] * nb, dtype=object)
    full = (LO > 0.45) & (K > 0.45)
    qlo = np.percentile(E, 35)
    lowpart = (E < qlo) | (LO < 0.3) | (K < 0.3)
    # drops: a full bass+kick block (>= 4 bars) right after a low block (>= 2 bars), snapped to the 4-bar phrase grid
    drops, strength = [], []
    for b in range(2, nb - 3):
        before, after = slice(max(0, b - 4), b), slice(b, min(nb, b + 4))
        jump = LO[after].mean() - LO[before].mean()
        if lowpart[before].mean() >= 0.5 and full[after].mean() >= 0.75 and jump > 0.25:
            q = int(math.floor(b / 4 + 0.5)) * 4
            bb = min(max(q if abs(b - q) <= 1 else b, 1), nb - 1)               # snap to the 4-bar phrase grid when within a bar
            if not drops or bb - drops[-1] >= 12: drops.append(bb); strength.append(jump)
            elif jump > strength[-1]: drops[-1], strength[-1] = bb, jump      # same drop: keep the biggest bass jump
    # intro / outro
    first_full = next((b for b in range(nb) if full[b:b + 4].mean() >= 0.75), None)
    last_full = next((b for b in range(nb - 1, -1, -1) if full[max(0, b - 3):b + 1].mean() >= 0.75), None)
    if first_full is not None and first_full >= 2:
        lab[:first_full] = "intro"
    if last_full is not None and nb - 1 - last_full >= 2:
        lab[last_full + 1:] = "outro"
    # breakdowns: low stretches >= 4 bars between the intro and outro
    b = 0
    while b < nb:
        if lowpart[b] and lab[b] == "groove":
            e = b
            while e < nb and lowpart[e] and lab[e] == "groove": e += 1
            if e - b >= 4: lab[b:e] = "breakdown"
            b = e
        else: b += 1
    for k in range(1, nb):                                                    # snap section edges to the 4-bar phrase grid (within 1 bar)
        if lab[k] == lab[k - 1]: continue
        q = int(math.floor(k / 4 + 0.5)) * 4
        if abs(k - q) != 1 or not 0 < q < nb: continue
        if q > k and all(lab[j] == lab[k] for j in range(k, q + 1)): lab[k:q] = lab[k - 1]
        elif q < k and all(lab[j] == lab[k - 1] for j in range(q - 1, k)): lab[q:k] = lab[k]
    # builds: up to 8 bars before each drop (the end of a breakdown, or a rising stretch)
    for D in drops:
        run = 0
        while D - 1 - run >= 0 and lab[D - 1 - run] == "breakdown": run += 1
        n_build = min(8, max(4, run // 2)) if run else 4                   # long breakdowns keep their first half
        lab[D] = "drop"
        for k in range(D - 1, max(-1, D - 1 - n_build), -1):
            if lab[k] in ("breakdown", "groove", "peak"): lab[k] = "build"
            else: break
        for k in range(D + 1, min(nb, D + 16)):
            if lab[k] == "groove": lab[k] = "drop"
            else: break
    # peaks: the loudest groove bars
    qhi = np.percentile(E, 80)
    for k in range(nb):
        if lab[k] == "groove" and E[k] >= qhi and K[k] > 0.5: lab[k] = "peak"
    secs, s0 = [], 0
    for k in range(1, nb + 1):
        if k == nb or lab[k] != lab[s0]:
            secs.append({"type": str(lab[s0]), "start_bar": s0 + 1, "end_bar": k}); s0 = k
    return secs, [D + 1 for D in drops]


def _vocals(path, beats_bar_t, bar_t_end):
    py = os.environ.get("LIGHTING_STEMS_PY") or (str(STEMS_FALLBACK) if STEMS_FALLBACK.exists() else None)
    if not py: raise RuntimeError("--vocals needs a Python with torch + demucs: set LIGHTING_STEMS_PY=/path/to/python")
    r = subprocess.run([py, str(Path(__file__).with_name("stems.py")), str(path)], capture_output=True, text=True, timeout=1800)
    if r.returncode: raise RuntimeError(f"vocal separation failed: {(r.stderr or r.stdout).strip()[-300:]}")
    d = json.loads(r.stdout); hop = d["hop_s"]; act = np.array(d["activity"], float)
    per_bar = []
    for t0, t1 in zip(beats_bar_t, bar_t_end):
        a, b = int(t0 / hop), max(int(t0 / hop) + 1, int(t1 / hop)); per_bar.append(float(act[a:b].mean()) if a < len(act) else 0.0)
    return per_bar, d.get("spans", [])


def get_fix(path):
    try: return json.loads(FIXES.read_text()).get(str(Path(path).expanduser().resolve()), {})
    except (FileNotFoundError, ValueError): return {}


def set_fix(path, shift=None, bar=None, bpm=None, clear=False):
    """Remember a DJ's grid correction for one file: shift = beats to move the whole grid (0.5 = it was on the off-beat),
    bar = beats to move bar 1 (1..3), bpm = the true tempo (fixes half/double). Accumulates; clear=True forgets it."""
    from core.files import write_atomic
    try: allf = json.loads(FIXES.read_text())
    except (FileNotFoundError, ValueError): allf = {}
    k = str(Path(path).expanduser().resolve()); f = {} if clear else dict(allf.get(k, {}))
    if shift is not None:
        if not -1 <= shift <= 1: raise ValueError("shift must be -1..1 beats")
        f["shift"] = round(((f.get("shift", 0) + shift) + 0.5) % 1 - 0.5, 4)
    if bar is not None:
        if not isinstance(bar, int) or not -3 <= bar <= 3: raise ValueError("bar must move bar 1 by -3..3 beats")
        f["bar"] = (f.get("bar", 0) + bar) % 4
    if bpm is not None:
        if not 40 <= bpm <= 250: raise ValueError("bpm must be 40..250")
        f["bpm"] = float(bpm)
    f = {a: b for a, b in f.items() if b}
    if f: allf[k] = f
    else: allf.pop(k, None)
    write_atomic(FIXES, json.dumps(allf, indent=1) + "\n"); return f


def rekordbox_grid(xml_path, audio_path):
    """Beat grid for audio_path from a rekordbox collection XML export (File > Export Collection in xml format):
    -> {"bpm", "beats_from": [(start_s, bpm, beat_in_bar)]} or None when the track is not in the export."""
    import xml.etree.ElementTree as ET, urllib.parse
    want = str(Path(audio_path).expanduser().resolve()); name = Path(audio_path).name; hit = None
    for tr in ET.parse(xml_path).getroot().iter("TRACK"):
        loc = tr.get("Location")
        if not loc or tr.find("TEMPO") is None: continue
        pth = urllib.parse.unquote(urllib.parse.urlparse(loc).path)
        if pth == want: hit = tr; break
        if Path(pth).name == name and hit is None: hit = tr
    if hit is None: return None
    marks = [(float(t.get("Inizio")), float(t.get("Bpm")), int(t.get("Battito", "1"))) for t in hit.iter("TEMPO")]
    return {"bpm": float(hit.get("AverageBpm") or marks[0][1]), "marks": sorted(marks)}


def _grid_from_marks(marks, dur):
    """Beats + downbeat index from tempo marks [(start_s, bpm, beat_in_bar_at_start)]."""
    beats, bib = [], []
    for k, (t0, bpm, b1) in enumerate(marks):
        t1 = marks[k + 1][0] if k + 1 < len(marks) else dur; P = 60.0 / bpm; t = t0; j = 0
        while t < t1 - 1e-6: beats.append(t); bib.append((b1 - 1 + j) % 4); t += P; j += 1
    d0 = next((i for i, b in enumerate(bib[:4]) if b == 0), 0)
    return np.array(beats), d0


def _key(path, bpm, rng, vocals, extra=""):
    st = Path(path).stat(); h = hashlib.sha1(f"{Path(path).resolve()}|{st.st_size}|{int(st.st_mtime)}|{VERSION}|{bpm}|{tuple(rng)}|{vocals}|{extra}".encode()).hexdigest()[:10]
    stem = "".join(c if c.isalnum() else "_" for c in Path(path).stem)[:40]
    return CACHE / f"{stem}-{h}.json"


def analyze(path, bpm=None, bpm_range=(70, 180), vocals=False, force=False, rekordbox_xml=None):
    path = Path(path).expanduser()
    if not path.exists(): raise FileNotFoundError(f"no such file: {path}")
    if bpm is not None and not 40 <= bpm <= 250: raise ValueError("bpm hint must be 40..250")
    lo, hi = bpm_range
    if not 40 <= lo < hi <= 250: raise ValueError("bpm range must satisfy 40 <= lo < hi <= 250")
    fix = get_fix(path); bpm = bpm or fix.get("bpm"); rbx = os.environ.get("LIGHTING_REKORDBOX_XML") if rekordbox_xml is None else rekordbox_xml
    rb = rekordbox_grid(rbx, path) if rbx and Path(rbx).exists() else None
    cp = _key(path, bpm, bpm_range, vocals, f"{sorted(fix.items())}|{rb and rb['marks'][:3]}")
    if cp.exists() and not force: return json.loads(cp.read_text())
    t_start = time.time(); meta = probe(path)
    if meta and not meta.get("has_audio", True): raise ValueError(f"{path.name} has no audio track")
    x = decode(path)
    if len(x) < 8 * SR: raise ValueError(f"{path.name}: only {len(x) / SR:.1f} s of audio (need >= 8 s)")
    bands, chroma, lmh = _stft_bands(x); n = len(bands)
    o, low_on, punch = _onset(bands)
    coarse, conf = _tempo(o, bpm_range, bpm)
    b0, phi = _grid(o, coarse, n)
    P = 60.0 / b0; notes = []
    phi = _on_kick(punch, phi, P, n)
    beats = np.arange(phi, n / FPS - 0.05, P)
    offs, strong = _drift(o, beats, P); constant = True
    if strong.sum() >= 4 and _drifts(offs[strong], P):
        dp = _dp_beats(o, b0)
        fit = lambda bt: np.interp(bt * FPS, np.arange(n), o).mean()
        if len(dp) > 16 and fit(dp) > 1.15 * fit(beats):                     # keep the steady grid unless tracking is clearly better
            beats = dp; constant = False; notes.append("tempo moves: beats tracked one by one (dynamic programming)")
            b0 = float(60.0 / np.median(np.diff(beats)))
    d0, dconf = _downbeat(beats, bands, chroma, lmh, punch, n)
    beats = beats + LAG_S                                                     # envelope frame time -> attack time
    grid_src = "analysed"
    if rb:                                                                    # the DJ's own rekordbox grid wins
        beats, d0 = _grid_from_marks(rb["marks"], n / FPS); b0 = rb["bpm"]; dconf = 1.0; grid_src = "rekordbox xml"
        constant = len(rb["marks"]) == 1; P = 60.0 / b0
    if fix.get("shift"): beats = beats + fix["shift"] * (60.0 / b0); grid_src += f" + shifted {fix['shift']:+g} beat"
    if fix.get("bar"): d0 = (d0 + fix["bar"]) % 4; grid_src += f" + bar 1 moved {fix['bar']} beat(s)"
    beats = beats[beats >= 0]
    if len(beats) < 8: raise ValueError(f"{path.name}: too short for a beat grid")
    if dconf < 0.15: notes.append("downbeat unclear: if lights change cue a beat off, nudge with the player")
    bar_beats = beats[d0::4]; nb = len(bar_beats) - 1 if len(bar_beats) > 1 else 0
    if nb < 1: raise ValueError(f"{path.name}: too short for a bar grid")
    fi = np.minimum(((bar_beats - LAG_S) * FPS).astype(int), n - 1); en = lmh.sum(1)
    def bar_mean(v, k): a, b = fi[k], max(fi[k] + 1, fi[k + 1]); return float(v[a:b].mean())
    raw = {"low": [], "mid": [], "high": [], "rms": [], "kick": []}
    for k in range(nb):
        for j, nm in enumerate(("low", "mid", "high")): raw[nm].append(10 * math.log10(bar_mean(lmh[:, j], k) + 1e-10))
        raw["rms"].append(10 * math.log10(bar_mean(en, k) + 1e-10))
        bt = beats[(beats >= bar_beats[k] - 1e-6) & (beats < bar_beats[k + 1] - 1e-6)] - LAG_S
        on = np.interp(bt * FPS, np.arange(n), low_on).mean() if len(bt) else 0.0
        off = np.interp((bt + P / 2) * FPS, np.arange(n), low_on).mean() if len(bt) else 0.0
        raw["kick"].append(on - 0.5 * off)
    nz = {k: _norm(np.array(v)) for k, v in raw.items()}
    energy = np.clip(0.45 * nz["low"] + 0.35 * nz["rms"] + 0.2 * nz["high"], 0, 1)
    bars = [{"bar": k + 1, "t": round(float(bar_beats[k]), 4), "energy": round(float(energy[k]), 3), "low": round(float(nz["low"][k]), 3),
             "mid": round(float(nz["mid"][k]), 3), "high": round(float(nz["high"][k]), 3), "kick": round(float(nz["kick"][k]), 3)} for k in range(nb)]
    secs, drops = _sections(bars, nb)
    out = {"schema": 1, "version": VERSION, "file": str(path.resolve()), "title": (meta.get("title") or path.stem), "artist": meta.get("artist"),
           "format": meta.get("format"), "has_video": bool(meta.get("has_video")), "duration_s": round(len(x) / SR, 2),
           "bpm": round(b0, 2), "bpm_confidence": round(conf, 2), "constant_tempo": constant,
           "beats": [round(float(b), 4) for b in beats], "downbeat_index": d0, "first_downbeat_s": round(float(beats[d0]), 4),
           "downbeat_confidence": round(dconf, 3), "grid_source": grid_src, "nbars": nb, "bars": bars, "sections": secs, "drops": drops, "notes": notes}
    if vocals:
        vb, spans = _vocals(path, bar_beats[:-1], bar_beats[1:])
        for b, v in zip(out["bars"], vb): b["vocal"] = round(v, 3)
        out["vocal_spans"] = spans
    out["analysis_s"] = round(time.time() - t_start, 2)
    CACHE.mkdir(parents=True, exist_ok=True); cp.write_text(json.dumps(out))
    return out


def beat_at(sm, t):
    """Fractional beat at time t (s): 0 = bar 1 beat 1. Interpolates the grid; extrapolates at the song's tempo outside it."""
    b = sm["beats"]; d0 = sm["downbeat_index"]; P = 60.0 / sm["bpm"]
    if t <= b[0]: return (t - b[0]) / P - d0
    if t >= b[-1]: return (len(b) - 1 - d0) + (t - b[-1]) / P
    return float(np.interp(t, b, np.arange(len(b)))) - d0


def time_at(sm, beat):
    b = sm["beats"]; i = beat + sm["downbeat_index"]; P = 60.0 / sm["bpm"]
    if i <= 0: return b[0] + i * P
    if i >= len(b) - 1: return b[-1] + (i - len(b) + 1) * P
    return float(np.interp(i, np.arange(len(b)), b))


def summary(sm):
    secs = " ".join(f"{s['type']}@{s['start_bar']}" for s in sm["sections"])
    return (f"{sm['title'][:48]:48} {sm['bpm']:7.2f} BPM{'' if sm['constant_tempo'] else '~'}  {sm['nbars']:3d} bars  "
            f"drops {sm['drops']}  | {secs}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+"); ap.add_argument("--bpm", type=float); ap.add_argument("--bpm-range", nargs=2, type=float, default=(70, 180))
    ap.add_argument("--vocals", action="store_true"); ap.add_argument("--json", action="store_true"); ap.add_argument("--force", action="store_true")
    a = ap.parse_args(); files = []
    for p in a.paths: files += find_audio(p) if Path(p).is_dir() else [Path(p)]
    if not files: raise ValueError("no music/video files found")
    for f in files:
        try: sm = analyze(f, a.bpm, tuple(a.bpm_range), a.vocals, a.force)
        except (ValueError, FileNotFoundError) as e: print(f"skip {f.name}: {e}", file=sys.stderr); continue
        print(json.dumps(sm) if a.json else summary(sm))


if __name__ == "__main__":
    try: main()
    except (ValueError, RuntimeError, FileNotFoundError) as e: print(f"songmap: {e}", file=sys.stderr); sys.exit(1)
