"""Run the test suite: python3 run_tests.py (simulation only; tests write to out/tests)."""
import subprocess, sys, time
from pathlib import Path
root = Path(__file__).resolve().parent; bad = 0
for t in sorted((root / "tests").glob("test_*.py")):
    t0 = time.time(); r = subprocess.run([sys.executable, str(t)], cwd=root, capture_output=True, text=True, timeout=900)
    last = (r.stdout.strip().splitlines() or [""])[-1]
    print(f"{'ok  ' if r.returncode == 0 else 'FAIL'} {t.name:32} {time.time() - t0:5.1f}s  {last[:90]}")
    if r.returncode: bad += 1; print(r.stderr[-1500:])
sys.exit(1 if bad else 0)
