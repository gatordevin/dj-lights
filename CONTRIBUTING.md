# Contributing

Thanks for helping! Fixture profiles, bug reports with `dj.py doctor` output, and pull requests are all welcome.

## Setup
```bash
git clone https://github.com/gatordevin/dj-lights.git && cd dj-lights
brew install ffmpeg            # or: sudo apt install ffmpeg
python3 -m pip install -r requirements.txt
python3 run_tests.py           # all tests, simulation only
```
No hardware needed: without a DMX interface every command runs in simulation; watch it with `python3 lighting/dj.py web --open`.

## Ground rules
- **Standard library + numpy only** (plus `ffmpeg` as a program). Ask in an issue before adding a dependency.
- The repo root is the import root: run files as `python3 lighting/x.py`.
- **Docstrings are contracts.** The first docstring of every module lists its CLI, Python API, failure modes, limits and users —
  update it with your change.
- **Tests with every change** (`tests/test_lighting_*.py`), written so they never touch real lights (`port="none"`, `MemoryOutput`).
- Keep the safety invariants (see [AGENTS.md](AGENTS.md)): opt-in strobes with a flash-rate limit, no automatic lasers, eye-safe
  mover aim, blackout on exit, localhost-only web server.

## Adding a fixture profile
1. Easiest: if your light is in the [Open Fixture Library](https://open-fixture-library.org),
   `python3 lighting/dj.py fixtures import maker/fixture-key` and check it with `dj.py test RIG LIGHT`.
2. Otherwise: `python3 lighting/dj.py fixtures new my_light --channels "dimmer,red,green,blue,strobe,mode,speed" --type par`
   (segments: `"dimmer,4x(r,g,b)"`), or copy a `lighting/profiles/generic_*.json`.
3. Verify on the real light (`dj.py test RIG LIGHT` walks every channel), set `"status": "verified"` and the `"source"` (what you
   checked and how), and add motor speeds for moving heads (`"pan_speed"`, `"tilt_speed"` in deg/s — time a full pan).
4. Open a PR with the JSON. Profile format: `lighting/fixtures.py` docstring.

## Adding an effect
Effects are pure functions in `lighting/show.py`: `def fx_NAME(a, i, n, t) -> {"color"|"dimmer"|"aim"|"strobe"|"speed": ...}`
(`a` = the layer's arguments, `i`/`n` = this slot of how many, `t` = `Tick(beat, bpm, level, bass, since_onset)`). They are
registered automatically. Keep them stateless and flash-safe; add a test.

## Pull requests
Small, focused PRs with a clear description and passing `python3 run_tests.py`. For UI changes include a screenshot.
