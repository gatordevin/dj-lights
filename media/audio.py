"""Audio decode + analysis: prove a file really contains sound, and see its shape.
Decoding uses macOS `afconvert` (any CoreAudio-readable input: m4a/aac/HE-AAC, mp4 audio track, mp3, wav, caf...).
CLI:    python3 media/audio.py FILE [--png out/media/wave.png] [--wav out/media/x.wav]
            prints format (afinfo), duration, peak/RMS dBFS, % silent windows; draws a waveform PNG
Python: info(path) -> {"format", "duration_s", "sample_rate", "channels", "bitrate"}     (via afinfo)
        decode(src, wav=None, rate=None) -> Path   16-bit little-endian PCM WAV (default out/media/<stem>.wav)
        read_mono(wav, mix="peak"|"mean") -> (int16 array, sr, channels)   rms_envelope(samples, sr, window_s=0.05,
            full_scale=32768) -> [0..1 per window]   dbfs(v) -> dB (THE audio-level helpers: media/takes, media/synth use them)
        levels(wav, window_s=0.05) -> {"duration_s", "sample_rate", "channels", "peak_db", "rms_db",
                                        "silent_pct", "envelope": [rms 0..1 per window]}
        waveform_png(wav, out, w=900, h=220, title="") -> Path   min/max envelope drawn with graphics.gfx
Fails: FileNotFoundError; RuntimeError when afconvert/afinfo can't read the input (message included).
Limits: macOS only (afconvert/afinfo); analysis is pure Python (~1-2 s per minute of stereo 48 kHz audio);
  silence threshold = -50 dBFS RMS per window.
Depends on: graphics/gfx.py (waveform).  Used by: media/README recipes, tests/test_media.py."""
import sys, re, wave, array, math, subprocess, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]

def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode: raise RuntimeError(f"{cmd[0]} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout

def info(path):
    if not Path(path).exists(): raise FileNotFoundError(path)
    out = _run(["afinfo", str(path)])
    g = lambda pat, f=str: (lambda m: f(m.group(1)) if m else None)(re.search(pat, out))
    return {"format": g(r"Data format:\s*(.+)"), "duration_s": g(r"estimated duration:\s*([\d.]+)", float),
            "sample_rate": g(r"(\d+) Hz", int), "channels": g(r"(\d+) ch", int), "bitrate": g(r"bit rate:\s*(\d+)", int)}

def decode(src, wav=None, rate=None):
    src = Path(src)
    if not src.exists(): raise FileNotFoundError(src)
    wav = Path(wav or ROOT / "out/media" / (src.stem + ".wav")); wav.parent.mkdir(parents=True, exist_ok=True)
    _run(["afconvert", str(src), "-o", str(wav), "-f", "WAVE", "-d", "LEI16" + (f"@{rate}" if rate else "")])
    return wav

def read_mono(wav, mix="peak"):
    """16-bit PCM WAV -> (mono int16 array, sample rate, channels). mix: "peak" keeps the loudest channel per frame (level
    checks), "mean" averages channels (what a listener hears; edits, speech analysis)."""
    if mix not in ("peak", "mean"): raise ValueError("mix must be 'peak' or 'mean'")
    with wave.open(str(wav)) as w:
        if w.getsampwidth() != 2: raise ValueError(f"{wav}: need 16-bit PCM (use decode())")
        ch, sr, data = w.getnchannels(), w.getframerate(), array.array("h", w.readframes(w.getnframes()))
    if sys.byteorder == "big": data.byteswap()
    if ch == 1: return data, sr, ch
    if mix == "peak": return array.array("h", (max(data[i:i + ch], key=abs) for i in range(0, len(data), ch))), sr, ch
    return array.array("h", (sum(data[i:i + ch]) // ch for i in range(0, len(data), ch))), sr, ch

_frames = read_mono      # old private name kept for callers inside this module

def rms_envelope(samples, sr, window_s=0.05, full_scale=32768.0):
    """RMS per window (list, 0..1 when full_scale is the samples' full scale: 32768 for int16, 1.0 for float)."""
    if window_s <= 0: raise ValueError("window_s must be > 0")
    n = max(1, int(sr * window_s))
    return [math.sqrt(sum(x * x for x in samples[i:i + n]) / max(1, len(samples[i:i + n]))) / full_scale for i in range(0, len(samples), n)]

def dbfs(v, floor=-120.0):
    """Linear amplitude (0..1) -> dB full scale; silence -> floor."""
    return 20 * math.log10(v) if v > 1e-9 else floor

def levels(wav, window_s=0.05):
    mono, sr, ch = read_mono(wav)
    env = rms_envelope(mono, sr, window_s)
    db = dbfs
    peak = max((abs(x) for x in mono), default=0) / 32768
    rms = math.sqrt(sum(e * e for e in env) / len(env)) if env else 0
    return {"duration_s": len(mono) / sr, "sample_rate": sr, "channels": ch, "peak_db": round(db(peak), 1),
            "rms_db": round(db(rms), 1), "silent_pct": round(100 * sum(db(e) < -50 for e in env) / max(1, len(env)), 1),
            "envelope": env}

def waveform_png(wav, out, w=900, h=220, title=""):
    from graphics.gfx import render
    mono, sr, _ = _frames(wav)
    cols = w - 40; step = max(1, len(mono) // cols); mid = 40 + (h - 60) / 2; amp = (h - 60) / 2
    lv = levels(wav)
    def paint(g):
        g.rect(0, 0, w, h, (0.07, 0.08, 0.11))
        g.text(title or Path(wav).name, 20, 8, 13, (0.9, 0.92, 0.95))
        g.text(f"{lv['duration_s']:.1f} s · {sr} Hz · peak {lv['peak_db']} dBFS · rms {lv['rms_db']} dBFS · silent {lv['silent_pct']}%",
               20, h - 20, 11, (0.6, 0.63, 0.7))
        g.line(20, mid, w - 20, mid, (0.25, 0.27, 0.33))
        for c in range(cols):
            blk = mono[c * step:(c + 1) * step]
            if not blk: break
            lo, hi = min(blk) / 32768, max(blk) / 32768
            g.rect(20 + c, mid - hi * amp, 1, max(1, (hi - lo) * amp), (0.36, 0.72, 0.98))
        for s in range(0, int(lv["duration_s"]) + 1, 5):
            x = 20 + s * sr / step; g.text(f"{s}s", x, h - 38, 9, (0.5, 0.53, 0.6))
    Path(out).parent.mkdir(parents=True, exist_ok=True); render(paint, w, h, str(out))
    return Path(out)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Decode and analyse audio"); ap.add_argument("file")
    ap.add_argument("--png"); ap.add_argument("--wav"); a = ap.parse_args()
    i = info(a.file); print(f"format: {i['format']}  ({i['duration_s']} s, {i['bitrate']} bps)")
    wav = decode(a.file, a.wav); lv = levels(wav)
    print(f"decoded -> {wav}: {lv['duration_s']:.2f} s {lv['sample_rate']} Hz {lv['channels']} ch | peak {lv['peak_db']} dBFS, "
          f"rms {lv['rms_db']} dBFS, silent windows {lv['silent_pct']}%")
    png = waveform_png(wav, a.png or ROOT / "out/media" / (Path(a.file).stem + "_wave.png"), title=Path(a.file).name)
    print("waveform:", png)
