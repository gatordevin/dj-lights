"""Write a royalty-free demo song to try the lights without your own music: examples/demo_128bpm.wav (64 bars, ~2 min).
Structure: intro (hats + bass) bars 1-8, kick groove 9-32 (claps from 17), breakdown 33-40, DROP at 41, groove to 56, outro 57-64.
Usage: python3 examples/make_demo_track.py [--bpm 128] [--out examples/demo_128bpm.wav]"""
import sys, wave, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from lighting.audio_in import synth_track, SR

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("--bpm", type=float, default=128.0); ap.add_argument("--out")
a = ap.parse_args()
out = Path(a.out or Path(__file__).resolve().parent / f"demo_{int(a.bpm)}bpm.wav")
x = np.concatenate([np.zeros(int(SR * 0.5), np.float32), synth_track(bpm=a.bpm, bars=64)])
with wave.open(str(out), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
print(f"wrote {out} ({len(x) / SR:.0f} s at {a.bpm:g} BPM) — try: python3 lighting/dj.py show {out} --rig example_mobile --preview")
