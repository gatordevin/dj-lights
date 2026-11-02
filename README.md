# DJ Lights

DMX light shows that follow the music for **any DJ, any lights, any song** — with a web app (live 3D stage, song timeline,
drag-to-place rig editor, cue pads, exports), a command line, and an MCP server so Claude / Codex / Cursor can run it by chat.

- **Any lights**: 18 generic fixture layouts (RGB/RGBW/RGBWA+UV pars, LED bars, moving heads, colour-wheel spots, strobes, fog),
  any brand from the [Open Fixture Library](https://open-fixture-library.org), or type a light's channel list from its manual.
- **Any place**: put lights in plain words — "4 pars on the floor behind the dj", "2 moving heads on the truss"; addresses + DIP
  switches are worked out for you; drag them around in 3D.
- **Any song**: mp3, wav, aiff, flac, m4a, ogg, opus and the audio of mp4/mov/mkv/webm → tempo, beat grid, bars, drops, builds,
  breakdowns; rekordbox XML grids if you have them.
- **Shows that fit your rig**: build-ups, a dark beat before every drop, moving heads that travel in the dark at their real speed,
  eye-safe beam aiming; strobe/fog only when you ask; lasers never automated.
- **Play it**: in sync with the song, simulate at up to 16×, or go live following rekordbox/djay/Traktor over Ableton Link.
- **Export**: QLC+ workspace, DMX frame CSV, cue CSV, MIDI cue track, patch list.

## Quick start (macOS; Linux works without the native 3D window)
```bash
brew install ffmpeg                      # music decoding (Linux: apt install ffmpeg)
python3 -m pip install -r requirements.txt
python3 lighting/dj.py doctor            # what works here (dongle, ffmpeg, ...)
python3 lighting/dj.py web --open        # the web app: http://127.0.0.1:8790
```
Hardware: any FTDI "Open DMX" USB dongle (e.g. DSD TECH SH-RS09B, Enttec Open DMX) or an Art-Net node (`--artnet HOST`).
Without either, everything runs in simulation (watch it in the 3D view).

Command line (same features): `python3 lighting/dj.py setup | rig ... | test | show SONG --preview | play SONG | export SONG | live`
— see `lighting/README.md` (all modules, recipes, accuracy numbers, limits).

## Use it from a chat (MCP)
```bash
claude mcp add --scope user dj-lights -- python3 "$PWD/lighting/mcp_server.py"        # Claude Code
```
Codex (`~/.codex/config.toml`): `[mcp_servers.dj-lights]` · `command = "python3"` · `args = ["/ABS/PATH/lighting/mcp_server.py"]`.
Claude Desktop: add the same command under `mcpServers`. Agents: `lighting/SKILL.md` is the playbook (copy it to
`~/.claude/skills/dj-lighting/SKILL.md` for Claude Code).

## Optional
- Vocal detection (`analyze --vocals`): a Python with torch + demucs at `./.venv-stems` or `LIGHTING_STEMS_PY=/path/to/python`.
- Settings live in `lighting/dj_settings.json`, your rigs in `lighting/rigs/`, grid fixes / cue edits in `lighting/*.json`
  (git-ignored except the examples). Generated files go to `out/` (git-ignored).

## Safety
The web console/app have **no login**: they listen on 127.0.0.1 only, unless you pass `--lan` (trusted networks only).
Strobes are opt-in and flash-limited; lasers are forced off unless you arm them yourself; check local laser rules.
No API keys are needed (the Open Fixture Library is fetched anonymously from GitHub).

## Tests
`python3 run_tests.py` (simulation only — never drives real lights).
