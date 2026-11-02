# Agent guide (Codex, Claude, Cursor, Copilot, …)

**Using the lights for someone?** Read [`skills/dj-lighting/SKILL.md`](skills/dj-lighting/SKILL.md) — the playbook (first moves,
building a rig, verifying on real lights, songs → shows, moving heads, exports, safety rules). If the `dj-lights` MCP server is
connected, prefer its tools; otherwise run `python3 lighting/dj.py ... --json` from the repo root.

**Changing the code?**
- Run from the repo root; it is the import root (`python3 lighting/dj.py`, `python3 tests/test_lighting_dj.py`). No packaging step.
- Dependencies: Python 3.11+ standard library + `numpy`; `ffmpeg` on PATH. Don't add third-party packages without discussing it.
- Every module's docstring is its contract (CLI, Python API, inputs/outputs, how it fails, limits, "Used by"). Keep it accurate
  when you change behaviour; raise clear `ValueError` / `LookupError` / `RuntimeError` at boundaries.
- Tests: `python3 run_tests.py` (simulation only — they never drive real lights). Add tests next to the area you change
  (`tests/test_lighting_*.py`). Tests write only to `out/` (git-ignored).
- Safety invariants that must hold: strobes only when asked and flash-rate limited; lasers never driven by automatic shows; moving
  heads aimed out of the crowd's eye zone unless the user asks for floor effects; every light command blacks out on exit; the web
  server stays localhost-only with the request guard (`lighting/httpguard.py`).
- Map of modules: [`lighting/README.md`](lighting/README.md).
