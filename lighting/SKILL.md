---
name: dj-lighting
description: Set up and run DMX party/DJ lights that follow music — patch any rig in plain words (pars, LED bars, moving heads, spiders, strobes, fog; any brand via the Open Fixture Library), analyse any song or video file (BPM, beat grid, bars, drops, builds, breakdowns, vocals), generate a light show for that rig, play it in sync, or run lights live from rekordbox/djay/Pro DJ Link. Use for "set up my lights", "add a moving head", "make the lights go with this song", "light show for my DJ set", "why are the lights off-beat", DMX addresses/DIP switches, or anything in the dj-lights repo.
---

# DJ lighting

All of it is one CLI: `python3 lighting/dj.py ...`, run from
the repo root (the import root; run modules with `python3 path/to/file.py`). Most commands take
`--json`. Read `lighting/README.md` for the architecture and limits.

## Three ways in

- **MCP tools** (`dj-lights` server: `lights_status`, `create_rig`, `add_lights`, `edit_light`, `set_venue`, `make_show`, `preview_show`,
  `edit_cue`, `simulate_song`, `play_song`, `export_show`, `open_web_app`, …). Use them when they are available. They cover the CLI below.
- **Web app** for the person: `open_web_app` (or `python3 lighting/dj.py web --open`) gives them the 3D stage, timeline, drag-to-place
  editing and exports. Send them the URL when they want to see or tweak things themselves.
- **CLI** `python3 lighting/dj.py ...` (below). Most commands take `--json`.

## First moves

1. `python3 lighting/dj.py doctor --json`: ffmpeg/numpy present? DMX dongle seen (`dmx_ports`)? Is there a default rig?
   With no dongle every light command runs in **simulation** (safe, nothing physical happens).
2. Find out the rig. If `default_rig` is set, run `python3 lighting/dj.py rig check RIG --json`. If it isn't, build one (below).
3. Never invent a person's lights. Ask what they own (brand/model or channel count, how many, where each one is) when
   it isn't in the conversation or an existing rig.

## Build a rig (non-interactive; `dj.py setup` is the same thing as a terminal wizard for humans)

```bash
python3 lighting/dj.py rig new NAME --venue mobile|club|stage|backyard|living_room [--truss] [--room W D H]
python3 lighting/dj.py fixtures find WORDS            # local profiles: verified units, generic_* layouts, imports
python3 lighting/dj.py fixtures find WORDS --ofl      # Open Fixture Library (needs network) -> maker/key
python3 lighting/dj.py fixtures import maker/key      # -> profiles/ofl_*.json (all modes; check which mode the light is set to)
python3 lighting/dj.py fixtures new NAME --channels "dimmer,red,green,blue,strobe,mode,speed" --type par   # from the manual
python3 lighting/dj.py rig add NAME PROFILE --count N --where "on stands either side" [--mode 8ch] [--aim dancefloor]
python3 lighting/dj.py rig sheet NAME                 # what to dial on each light: address, display code, DIP switches
python3 lighting/dj.py use NAME                       # make it the default
```

- **Picking a profile.** A verified profile for the exact unit is best, then an OFL import of the exact model, then the
  `generic_*` layout with the same channel count and order, then `fixtures new` typed from the manual's DMX chart. The
  channel **mode** set on the light must match the profile mode, e.g. a light showing `8CH`.
- **Places.** Left/right are **as the crowd sees the DJ**. "stage left" and "dj left" mean the DJ's own left. Spots: truss,
  ceiling, stand/t-bar, floor/uplight, booth, behind, front, over the crowd, wall. Heights look like "2.5m" or "8ft".
  Several lights in one `add` spread over the spot. Use `--pos x,y,z` (metres; x = crowd's right, y = away from the crowd,
  z = up) when the user gives measurements.
- **Addresses** are automatic on 10-channel steps. Fix one with `rig set NAME LIGHT address=N`. Overlaps are refused.
- `rig check` warnings matter. Template/OFL profiles are unverified until tested. Mover pan direction is unchecked
  until `point` has been run.

## Verify on the real lights (only when the user is there to watch)

```bash
python3 lighting/dj.py test [RIG]                 # each light flashes white in address order; the user confirms which one lit
python3 lighting/dj.py test RIG LIGHT             # walks that light's channels: red, green, blue, pan, tilt, strobe...
python3 lighting/dj.py point [RIG] LIGHT dancefloor   # aim a mover by IK; ask where the beam actually landed
python3 lighting/dj.py rig set RIG LIGHT invert_pan=true   # it swung to the mirror side
python3 lighting/dj.py rig set RIG LIGHT pan_checked=true  # it was right
```

What the person sees is the ground truth. If a step lights the wrong colour or channel, the profile is wrong: fix the
profile (or make one with `fixtures new`). Don't change the show to compensate.

## Songs → shows

```bash
python3 lighting/dj.py analyze FILE_OR_FOLDER [--bpm N] [--vocals] --json   # beats, bars, drops, sections; ~0.3 s/song (cached)
python3 lighting/dj.py show FILE [--palette auto|neon|fire|uf|red,#00ffaa] [--style club|chill|hype] [--strobe] [--fog] [--preview] --json
python3 lighting/dj.py play FILE [--console] [--start-bar N] [--latency-ms 150] [--volume 0..1] [--port none] [--seconds S]
python3 lighting/dj.py crate FOLDER --json        # gig prep: every file in a folder
python3 lighting/dj.py live --clock link --audio rekordbox   # live set: follow the DJ app (Ableton Link ON in the app)
```

- Any audio or video format ffmpeg reads works: mp3, wav, aiff, flac, m4a/aac/alac, ogg, opus, mp4, mov, mkv, webm.
- **Look at the preview** (`--preview` gives a PNG path; read it) before saying a show is good. The `plan` field lists
  `[bar, cue]`, and `warnings` says when movers can't stay above head height or the downbeat is unclear.
- **Grid problems** are the usual cause of "lights off-beat". Tempo half or double: `dj.py fix FILE --bpm 128`. Hits on
  the off-beat: `fix FILE --shift 0.5`. Cue changes a beat early or late: `fix FILE --bar 1` (or -1). Fixes are
  remembered per file. A rekordbox user can export the collection as XML and run `dj.py settings rekordbox_xml=PATH`.
- **Everything late against the sound** (Bluetooth or wireless speakers): `dj.py settings latency_ms=150`, tuned by ear.
- `play` blocks for the length of the song: use `--seconds` or run it in the background. Without a dongle it simulates.
  `--port none` forces simulation, and `--no-audio` drives the lights without playing sound.

## Moving heads, edits, exports

- Shows plan moves from each head's speed and keep heads dark while they travel. `make_show` returns `motion` (lit-while-moving %)
  and `mover_leads_beats`. If the person times a full pan, set it with `edit_light pan_speed=<deg/s>` (= travel deg / seconds).
- `edit_cue(file, start_bar, end_bar, cue)` changes a section's look. Edits are remembered per song and rig. Use cue=null to undo.
- `simulate_song(rate=8)` runs a show fast without audio. `seek(bar)` jumps around.
- `export_show(formats=[qlc, dmx, cues, midi, patch, native])` writes a zip with a README. QLC+ files follow QLC+'s XML formats
  but haven't been opened in QLC+ itself yet, so say so.

## Safety rules (keep them)

- Strobes only with `--strobe` (photosensitive guests). Auto shows cap strong flashes at about 3 per second per light.
- Lasers are never driven by auto shows. Only the user arms a laser, from the console.
- Fog only with `--fog`. It bursts 4 bars before drops, and the engine enforces rest time between bursts.
- Movers aim at spots whose beams stay above the crowd's eyes (`autoshow.mover_spots`, eye-zone check). Pass
  `--mover-targets floor` only when the user asks for floor effects.
- Ask before driving real lights (`play`/`test`/`point`/`live` with a dongle plugged in) when the user hasn't asked for it
  in this conversation. Every command blacks out when it ends or on Ctrl-C.

## Python (agents composing their own flows)

```python
from lighting import dj, patch, songmap, autoshow
sm = songmap.analyze("song.mp3")                     # dict: bpm, beats, bars[{energy, low, mid, high, kick}], sections, drops
rig = dj.load_rig("gig"); show = autoshow.make_show(sm, rig, "neon", "club")
autoshow.save_show(show)                             # out/lighting/shows/*.json; lighting.show.load_show(path) reads it back
dj.play("song.mp3", rig, show=show, port="none", audio=False, seconds=10, verbose=False)
```

Files: rigs `lighting/rigs/*.json`, profiles `lighting/profiles/*.json`, settings `lighting/dj_settings.json`, grid fixes
`lighting/song_fixes.json`; generated output in `out/lighting/`. Tests: `python3 tests/test_lighting_dj.py`.

