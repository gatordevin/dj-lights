"""Vocal activity of a song from its Demucs vocal stem (worker run by lighting/songmap.py --vocals in a Python with torch + demucs).
CLI:
  /path/to/stems-python lighting/stems.py FILE [--hop 0.1] [--device mps|cpu]  -> JSON on stdout:
      {"hop_s", "activity": [0..1 per hop], "spans": [[s0, s1], ...] (sung stretches >= 1 s, gaps < 0.6 s bridged), "device", "seconds"}
Method: ffmpeg -> 44.1 kHz stereo -> htdemucs vocal stem -> RMS per hop in dB -> 0..1 between the stem's 20th and 98th
  percentile -> spans where activity > 0.35 (hysteresis 0.25).
Fails: exit 1 with the reason on stderr (missing torch/demucs, undecodable file). The stem itself is never written to disk.
Limits: ~0.1x real time on an M-series GPU (mps), several times slower on cpu; first run downloads the htdemucs weights.
  Runs in the stems Python (its own numpy/torch), so it imports nothing from this workspace.
Depends on: numpy, torch, demucs, ffmpeg. Used by: lighting/songmap.py (_vocals)."""
import sys, json, time, argparse, subprocess


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file"); ap.add_argument("--hop", type=float, default=0.1); ap.add_argument("--device", default=None)
    a = ap.parse_args(); t0 = time.time()
    import numpy as np
    try:
        import torch
        from demucs.pretrained import get_model
        from demucs.apply import apply_model
    except ImportError as e:
        print(f"stems: needs torch + demucs in this Python ({e})", file=sys.stderr); sys.exit(1)
    dev = a.device or ("mps" if torch.backends.mps.is_available() else "cpu")
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", a.file, "-vn", "-f", "f32le", "-ar", "44100", "-ac", "2", "-"], capture_output=True)
    if r.returncode or not r.stdout: print(f"stems: cannot decode {a.file}", file=sys.stderr); sys.exit(1)
    wav = np.frombuffer(r.stdout, np.float32).reshape(-1, 2).T.copy()
    m = get_model("htdemucs"); m.eval(); m.to(dev)
    x = torch.from_numpy(wav); ref = x.mean(0); x = (x - ref.mean()) / (ref.std() + 1e-8)
    with torch.no_grad():
        out = apply_model(m, x[None].to(dev), shifts=0, split=True, overlap=0.1, device=dev, progress=False, num_workers=0)[0]
    v = out[m.sources.index("vocals")].mean(0).cpu().float().numpy() * float(ref.std() + 1e-8)
    hop = int(44100 * a.hop); nh = len(v) // hop
    db = 10 * np.log10((v[:nh * hop].reshape(nh, hop) ** 2).mean(1) + 1e-12)
    lo, hi = np.percentile(db, 20), np.percentile(db, 98); act = np.clip((db - lo) / (hi - lo + 1e-9), 0, 1)
    spans, on, s0 = [], False, 0.0
    for i, x_ in enumerate(act):
        t = i * a.hop
        if not on and x_ > 0.35: on, s0 = True, t
        elif on and x_ < 0.25: on = False; spans.append([s0, t])
    if on: spans.append([s0, nh * a.hop])
    merged = []
    for s in spans:
        if merged and s[0] - merged[-1][1] < 0.6: merged[-1][1] = s[1]
        else: merged.append(s)
    merged = [[round(x0, 2), round(x1, 2)] for x0, x1 in merged if x1 - x0 >= 1.0]
    print(json.dumps({"hop_s": a.hop, "activity": [round(float(x_), 3) for x_ in act], "spans": merged, "device": dev, "seconds": round(time.time() - t0, 1)}))


if __name__ == "__main__":
    main()
