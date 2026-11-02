# lighting/: DMX light control, beat/audio-synced shows, a ray-traced 3D stage, photometry
Branch: Engineering & Science

Drive real DMX512 fixtures from this Mac (USB→DMX dongle) by meaning — colour, dimmer, aim — on a beat clock that can
follow rekordbox/djay over Ableton Link and react to live audio; see and check everything in a 3D model of the venue.
**Any DJ, any lights, any song:** `dj.py` is the front door — patch your own lights in plain words, analyse any music or
video file (beats, bars, drops, builds, breakdowns), make a show for whatever lights you have, play it in sync, or run live
from your DJ software. Agents: read `lighting/SKILL.md` first.

## DJ quick start (any rig, any song)
**Easiest: the web app** — `python3 lighting/dj.py web --open` (http://127.0.0.1:8790): pick/drop a song → its beats, drops and
sections on a timeline with the cue plan (click a cue to change it, saved) → ⚡ Simulate at up to 16× or ▶ Play with audio, watching
the **live 3D stage** (beams, haze, floor pools, motor travel of slow heads); **3D Stage** tab: click a light to select it, drag it
to move it (shift-drag = height), edit position/rotation/mounting/address/mode/motor speed in the inspector, resize the venue,
undo; **Rig** tab: add lights by search (any brand via OFL), top-view plan, address/DIP table; **Live & Triggers**: follow
rekordbox/djay over Link, pads; **Export**: QLC+/DMX CSV/cue CSV/MIDI/patch zip; **Setup**: output, latency, music folder.
**From a chat** (Claude Code/Desktop, Codex, Cursor): the MCP server `lighting/mcp_server.py` (32 tools; `dj.py mcp` prints the
config; registered here for Claude Code user scope + Codex on 2026-11-02).
```
python3 lighting/dj.py doctor                                   # ffmpeg, dongle, rigs: what works here
python3 lighting/dj.py setup                                    # guided (terminal) — or the same with commands:
python3 lighting/dj.py rig new gig --venue mobile --truss       # venues: mobile club stage backyard living_room
python3 lighting/dj.py fixtures find rgbw par                   # 18 generic layouts + verified units; --ofl searches ~all brands
python3 lighting/dj.py fixtures import chauvet-dj/slimpar-pro-h-usb     # any Open Fixture Library light (or `fixtures new NAME --channels ...`)
python3 lighting/dj.py rig add gig generic_par_rgbw_8ch --count 4 --where "floor behind the dj"
python3 lighting/dj.py rig add gig generic_moving_head_rgbw_9ch --count 2 --where "on the truss"
python3 lighting/dj.py rig sheet gig                            # addresses (10-step) + DIP switches to dial on each light
python3 lighting/dj.py use gig && python3 lighting/dj.py test   # flash each light in turn; `test gig par1` walks its channels
python3 lighting/dj.py point mover1 dancefloor                  # check pan direction, then rig set ... invert_pan / pan_checked
python3 lighting/dj.py show song.mp3 --preview                  # analyse + choreograph; PNG timeline in out/lighting/shows/
python3 lighting/dj.py play song.mp3 --console                  # music + lights in sync (+ web console :8779)
python3 lighting/dj.py crate ~/Music/gig                        # prep a whole folder
python3 lighting/dj.py live --clock link --audio rekordbox      # live set: follow the DJ app's beat, react to its audio
```
- Moving heads: shows plan around each head's speed — spots in short-travel order (back and forth), each head starts moving
  `meta.mover_leads` beats early and is held **dark while travelling** (effect `snapdark`), slow heads snap less often;
  `meta.motion` reports the share of lit time a head was still moving (Spooky on example_mobile: 0.6-1.8 %, was 9-38 % before
  dark moves). Set a measured speed per light: Stage inspector or `dj.py rig set RIG LIGHT pan_speed=120 tilt_speed=90`
  (deg/s = travel degrees / seconds). DJXFLI beam profile: pan 120 / tilt 90 deg/s **estimated** from the owner seeing it relight
  while still moving.
- Places: left/right **as the crowd sees it**; "stage left"/"dj left" = the DJ's own left. Spots: truss, ceiling, stands,
  floor/uplight, booth, behind, front, over the crowd, wall; heights "2.5m"/"8ft". `dj.py rig places` lists them.
- Grid wrong on a song? `dj.py fix song.mp3 --shift 0.5` (on the off-beat), `--bar 1` (bar 1 a beat late), `--bpm 128`
  (half/double tempo). rekordbox users: `dj.py settings rekordbox_xml=/path/collection.xml` uses your own grids.
- Bluetooth/wireless speakers lag: `dj.py settings latency_ms=180`. Slow moving heads: `--mover-lead 1.0` (move longer in the
  dark). Strobes/fog only with `--strobe` / `--fog`; lasers are never driven by auto shows.
- Output: `--port auto` (dongle if plugged in, else simulation), `--port none`, `--artnet HOST` (Wi-Fi DMX nodes, visualisers).

## Architecture (each layer only uses the ones above it)
```
 net/abletonlink ─┐            media/audiotap ─┐
 clock.py  ───────┤ beat, BPM, bar phase       audiofeed.py  level / bass / hits
                  ▼                              ▼
 dmx.py ◄── show.py Engine:  timeline (program | song) → cue layers → effects(Tick) → fixtures.py (profiles, rig) → Universe
                  ▲                                                                               │
 console.py  HTTP API + web page + ConsoleClient ◄──── stage3d.py (3D window, snapshots, audit)  ▼  dongle / Art-Net / sim
             probe.py (raw channel hunting through the console)
```
- **Time** is a `Clock` (`clock.py`): `InternalClock` (BPM, tap), `LinkClock` (follows a Link session, falls back to
  internal without a jump), `ManualClock` (offline). Nothing else keeps time.
- **What plays** is a timeline of `(start beat, cue)` (`Engine.play_program` / `play_song`), checked every frame, so
  pause/step/back-step and Link re-syncs all just work. Programs and songs start on bar lines.
- **What a cue looks like** is a list of layers; each layer = an effect over a group, optionally scaled by audio
  (`"react": "bass"`). Effects are pure functions of a `Tick(beat, bpm, level, bass, since_onset)`.
- **What the lights are** is a profile (`profiles/*.json`, roles → channels) + a rig (`rigs/*.json`, addresses + 3D
  placement + venue). `Fixture.joint_angles()` is the one DMX→degrees conversion; `stage3d.kinematics()` is the one
  geometry model (drawn parts and beams come from it).
- **Control surfaces** all go through the console's API: the web page, the 3D window, `probe.py`, scripts (`ConsoleClient`).

## Modules
| module | what |
|---|---|
| `dmx.py` | `Universe` (512 ch), `OpenDMX` FTDI dongle (250k 8N2, break via ioctl or slow-baud byte), `ArtNet`, `MemoryOutput`, **`AutoOutput`** (dongle when plugged in, sim otherwise, hot-plug), background `Sender`. CLI `ports` / `send 1=255` / `selftest` |
| `fixtures.py` | profiles + rig: `load_profile`, `Rig.load`, `fx.color/dimmer/strobe/aim/speed/set/home`, `joint_angles()`, `emitted_rgb(fx, head)`, groups, overlap checks, fixed-colour LED cells, per-channel `invert`, mounting `pos/yaw/pitch/roll/mount/aim`. CLI `profiles` / `show P` / `rig` |
| `clock.py` | **beat clocks**: `InternalClock`, `LinkClock` (Ableton Link via `net/abletonlink`), `ManualClock`; one interface: `beat(t)`, `bpm`, `pause`, `step`, `set_bpm`, `tap`, `state()` |
| `show.py` | **engine + effects**: `Engine(rig, outputs, clock=)`, cues, `play_program`, **`play_song`** (bar cue sheets), holds, master/blackout, haze, laser arming, raw overrides; `render()` pure; `offline()` whole-show iterator; `preview_png`. Effects: color chase fade rainbow pulse(beat/onset) strobe sweep aim blackout; layers can `react` to audio. CLI `list` / `play` / `song` / `cue` / `preview` (`--link`) |
| `audiofeed.py` | **live audio → signals**: `Analyzer` (level, bass, hits with AGC; pure) + `AudioFeed(app)` (taps rekordbox/Music/Spotify output via `media/audiotap`). CLI `watch APP` |
| `audio_in.py` | **room microphone → features** (numpy + ffmpeg avfoundation `:0`): kick (30-250 Hz flux, percussive-onset gate, 150 ms refractory) and hat/high-note onsets, 4 AGC'd bands (bass/lowmid/mid/high), rms, centroid, flux, **section** (quiet/breakdown/build/drop/groove, drop held 4 s) → atomic `live/audio.json` @20 Hz (spec `the owner's DJ research notes`). Features only, audio never written. `AudioFeatures().start()/snapshot()/stop()`, `--file PATH` offline, `--sim`, `--listen-beats` (receive-only UDP 50001 → `lead_ms` via `estimate_lead`). Test: `tests/test_lighting_audio.py` (ground truth = the owner's DJ research notes: kick R/P ≥ 0.97, ~12 ms latency) |
| `console.py` + `console.html` | **web console + API** http://127.0.0.1:8779, **`start_console()`** (whole stack in one call) and **`ConsoleClient`**. Page: clock (internal / LINK ●), songs, cues, program, pause/step, tap/BPM, audio meters, holds, colour overrides, identify buttons, joints table, master/blackout/haze/laser. `--clock link\|internal --audio APP --lan --port auto\|none` |
| `stage3d.py` | **3D stage**: DMX → articulated fixtures (`kinematics`: yokes/heads, spider bars Y1 orange / Y2 blue + LED cells, par cans) → GPU ray tracing with volumetric haze; live window following the console (starts one if needed), joint inspector, frame stepping, 60 fps (per-ray culling, adaptive resolution); `snap`; **`audit`** eye-height safety per cue (`eye_hazards`) |
| `director.py` | **live DJ-reactive designer** for the Pioneer XDJ-XZ: clock (ProDjLinkClock, listen-only) + `live/state.json` (decks, loops, tracks, master, transition) → scenes by tempo band / loop build+release / mix; deck 1 = UF blue, deck 2 = UF orange; hard safety (master ≤ 0.55, laser never, no flash > 3 Hz, strobe ONLY with `--strobe` and then only as rare pars-only hits, PANIC file, blackout on exit). With `--rig actual` (beam+par+spider, ≤ 4 fixtures) the **roles designer** of `songlook.py` replaces the 8-fixture scenes: audio-reactive (kick/hat onsets, bass/high bands, sections breakdown/build/drop/groove from `live/audio.json`), a personality per track id remembered in `live/tracks/`, deck state (loops, mixes, track changes, tempo) blended in; `--designer legacy` forces the old scenes; `--preview-songs DIR` renders three songs' looks offline. Live: `python3 lighting/director.py --rig actual --state .../live/state.json --seconds N --master 0.5` (add `--strobe` to allow the rare par hits). Tests: `tests/test_lighting_director.py` (scenes) and `tests/test_lighting_director2.py` (roles). `python3 lighting/director.py --state .../live/state.json --seconds N --master 0.5 [--sim]`; `--preview-set PNG`, `--write-show` (→ `shows/director.json`, audit with `stage3d.py audit --show director`). Test: `tests/test_lighting_director.py` |
| `songlook.py` | **per-song looks + roles designer for the 3-fixture rig** (`--rig actual`): `AudioReader` (live/audio.json, stale > 2 s = no audio), `TrackMemory` (live/tracks/<track id>.json: plays, first seen, avg tempo, band means, rms percentiles, section lengths, frozen look; aggregate numbers only), `make_look` (stable hash of the rekordbox track id -> palette family / beam shapes / spider pattern / tilt style / accent style, nudged by centroid, bass- vs hat-heaviness, tempo, dynamics), `RoleDesigner` (par = bass-breathing wash in the song's base colour; beam = kick + beat accents, downbeat strongest, sweeps in safe aim only; spider = highs: low-depth shimmer, colour drift, tilt flicks, budgeted glints, LED-cell chases around the 8-cell ring), `EventBudget` + `FlashGovernor` (<= 3 strong luminance transitions/s per fixture whatever the audio), `SynthSong` (synthetic audio for tests/previews). Test: `tests/test_lighting_director2.py` |
| `probe.py` | channel hunting: `set` / `clear` raw overrides, `steps` / `walk` with spoken step numbers — through the running console (show keeps running) or the dongle directly |
| `photometry.py` | beam maths: lux on a target, lumens needed, spot size, saturated-colour share |
| `dj.py` | **DJ front door** (one CLI for people and agents): setup wizard, doctor, settings/default rig, fixtures (find/import/new), rig editing (patch.py), test/identify, point (IK aim check), analyze, fix (grid corrections), show, **play** (afplay/ffplay + `FileClock`, `--latency-ms`, `--start-bar`, console), crate, **live** (Link/Pro DJ Link/internal + app audio + console). `--json` for agents |
| `patch.py` | **rig patching in plain words**: venue templates (`mobile`/`club`/`stage`/`backyard`/`living_room`), `place("2 on stands either side")` → positions/mount, fixed lights aimed by IK at named targets, auto addresses on 10-channel steps + DIP switches, `check` (kinds, warnings), `make_profile` from a manual's channel list (`dimmer,strobe,4x(r,g,b)`) |
| `ofl.py` | **Open Fixture Library import**: search + convert any OFL fixture (wheels → colour-wheel role, shutter/strobe ranges + resting defaults, fine pan/tilt, pixel matrices → heads, switching channels, laser channels forced safe) |
| `songmap.py` | **any music/video file → beats, bars, drops**: ffmpeg decode, tempo + phase-coherent grid (+DP tracking for drifting tempo), downbeat, per-bar low/mid/high/kick energy, sections (intro/groove/peak/build/drop/breakdown/outro), optional vocal activity (`stems.py`, Demucs), rekordbox XML grids, per-song fixes; cached in out/lighting/songs |
| `autoshow.py` | **show for any song on any rig**: songmap → bar plan (build-up grading, dark beat before each drop, drive/peak, breakdown fades) → cues on standard groups (wash/movers/effects/strobes), movers aimed per fixture by IK at spots above the crowd's eyes (`mover_spots`, eye-zone check), palettes, styles club/chill/hype, `live_show` program for live sets |
| `webapp.py` + `webapp.html` | **web app** (stdlib server, one offline page): Show (song list/upload, timeline with sections/drops/editable cues, play/simulate/seek, live 3D, light swatches, preview), 3D Stage (canvas renderer of stage3d's model: beams with haze/glow/pools, click-select, drag-to-move, inspector for every light field + motor speed, venue editor, undo), Rig (add/search/OFL, plan, table), Live & Triggers (Link/Pro DJ Link/manual, pads, tap, nudge), Export, Setup; JSON API + SSE `/api/stream` (status 10/s, 3D frames ~30/s) |
| `session.py` | one shared controller (web app + MCP): play/simulate(0.1-32×)/seek/rate/live/manual/test/point in the background, triggers/master/blackout, `refresh_rig` (edits apply live), a ~40 fps display loop that mirrors the DMX into a display rig with **motor lag** and caches 3D frames |
| `motion.py` | **moving-head physics**: `MotorSim` (per-axis top speed + acceleration, from `Fixture.max_speed()` = rig/profile/type speed slowed by the speed channel), `move_time`, `audit` (share of lit time a head is still travelling) |
| `export.py` | **exports**: QLC+ 4 workspace (patched fixtures + generated .qxf definitions + baked Sequence + audio on a Show timeline; tags checked against QLC+ source, not yet opened in QLC+), DMX frame CSV, cue CSV, MIDI cue track (tempo, markers, notes), patch CSV, native JSON, zip + README |
| `mcp_server.py` | **MCP server** (stdio JSON-RPC, stdlib): 32 tools — status, rigs/lights/venue editing, fixtures/OFL/profiles, analyze/fix grid, make/preview/edit shows, 3D snapshot, play/simulate/seek/live/manual/pads/master/stop, test/point, export, open the web app, settings |
| `httpguard.py` | request guard for the local web servers: localhost-only Host names (bare IPs with `--lan`), same-origin POSTs with a JSON/binary Content-Type — blocks DNS rebinding and cross-site requests (there is no login) |
| `stems.py` | vocal-activity worker run in the stems Python (torch + demucs; `LIGHTING_STEMS_PY`) for `songmap --vocals` |

Profiles (`profiles/*.json`, status = how much is proven):

| profile | fixture | status |
|---|---|---|
| `djxfli_beam90` | DJXFLI 90W RGBW beam, 13CH: pan, tilt, XY speed, **dimmer 4**, strobe, R G B W 6-9, colour auto, motor auto, auto, reset | **verified** on the unit |
| `jcyw_spider_8x10` | JCYW spider, 15CH: tilt1, tilt2, dimmer, strobe, LED1-8, effect, effect speed, reset | **verified** (manual + LED colours R,G,B,W per bar; bars mirrored: `invert` on tilt1, in/out checked) |
| `coz_par36_rgb` | CO-Z 36-LED RGB par, display `d021` = DMX at 21: dimmer, R, G, B, strobe, program, speed | partial (dimmer + green verified) |
| `uking_laser_zq05031` | U`King ZQ05031 laser, 6CH simple mode | documented (official manual) |
| `generic_fogger` | 1-channel fog/haze | guess |
| `generic_*` (17) | templates for common DJ lights: dimmer 1ch, RGB 3/4/5/7ch, RGBW 4/8ch, RGBWA+UV 6/10ch, LED bars 4/8 segments, moving heads RGBW 9ch / 16-bit 11ch, colour-wheel spot 8ch, strobe 2ch | template (check the manual) |
| `ofl_*` | imported from the Open Fixture Library (`dj.py fixtures import maker/key`) | ofl (community data) |

Profile extras (lighting/fixtures): channel `"default"` (resting value: shutter open, gobo open), strobe `"strobe_range"`,
role `"wheel"` with colour `"slots"`. Every rig also has **standard groups** without listing them: wash/pars, movers/beams,
effects/spiders, strobes, lasers, fog/haze, left/right/center, floor/high (left to right), so shows are portable across rigs.

Shows (`shows/*.json`): `halloween_demo` (program + effects demo; includes `gators`), `gators` — UF orange/blue cues
(`gator_glow/verse/chorus/solo/singalong/finale/hit/end`, chorus = **chomp**: beam/spider-bar pairs snap shut on the beat)
and the song **"I Won't Back Down — Tom Petty"** (114 BPM, 82 bars + ring-out from a chord chart: intro 1, verse 5, chorus
23, verse 33, chorus 43, solo 53, sing-along chorus 63, finale 71, hit 81, end 83). Spider LEDs are fixed colours, so
Gator cues give spiders red + blue; true orange is on beams/pars.

Rig `rigs/halloween.json`: 2 beams on the truss crossbar (~3 m), 2 spiders sideways on the outside of the uprights
(pitch 75, roll 90, bars sweep left-right), 4 pars on the ground behind the stage uplighting hedge/trees; `venue` = the
owner's backyard from a photo (8×4 ft deck, 4 ft high, skirted step, goalpost truss, brick patio, hedges, tall trees),
metres (x audience-right, y away from the audience, z up). Addresses: beam1 1, par1 21, spider1 41 (plugged in
2026-10-12); planned par2-4 61/71/81, beam2 91, spider2 111.

## Recipes
```
python3 lighting/stage3d.py --play                      # everything: 3D window + web console + real lights (run_in_background)
python3 lighting/console.py --play --audio rekordbox    # console only, lights react to rekordbox's audio
python3 net/abletonlink.py watch                        # is rekordbox/djay sharing Link? (rekordbox: Performance mode → LINK)
python3 lighting/audiofeed.py watch rekordbox           # live level/bass/hit meter for an app
python3 lighting/show.py preview halloween_demo --song example_128 && open out/lighting/preview.png
python3 lighting/stage3d.py audit                       # eye-safety pass over the whole program
python3 lighting/stage3d.py snap --cue chase --view truss   # one moment offline (views: foh wide top side dj crowd truss photo)
python3 lighting/probe.py set 4=255 8=255 / clear        # raw channels while the show runs
python3 lighting/fixtures.py rig                        # address sheet
```
- **Sync to a DJ set**: turn on LINK in rekordbox (Performance mode) or djay; the console (default `--clock link`)
  shows `LINK ●` and its beat/bar = the DJ's. Tap/BPM are refused while following (change tempo in the DJ app).
  Programs and songs change cues on the DJ's bar lines; pause/step still work (resume re-joins the music).
- **Lights that hit with the music**: run with `--audio rekordbox` (rekordbox must be on this Mac); use cues with
  `{"effect": "pulse", "trigger": "onset"}` (flash per kick) or `"react": "bass"` / `"level"` on any layer
  (`kick_pulse`, `bass_wash` in the demo). Without audio these layers sit at their `floor`.
- **A song's light script**: add to the show file `"songs": {"Track name": {"bpm": 128, "bars": 96, "cues": [[1, "intro"],
  [17, "chase"], [33, "kick_pulse"], ...]}}`; start it from the page's Songs row — without Link, click **on the song's
  first beat** (that instant becomes bar 1 and the clock takes the song's BPM); with Link it starts on the DJ's next bar.
  Early/late by ear → **nudge ⅛** buttons. Copy `example_128` or the Gator song; shared cues can live in their own file
  (`"include": ["gators"]`). Check it: `show.py preview SHOW --song NAME`, `stage3d.py audit`.
- **A new effect**: `def fx_NAME(a, i, n, t): return {...}` in show.py (attributes: color, dimmer, aim, strobe, speed);
  it is registered automatically; add it to the "Registered" line of the docstring and a test.
- **A new fixture**: find the real chart (see below) → `profiles/NAME.json` (+ `optics` for 3D) → rig entry with
  address + pos → console Identify button → `probe.py` to confirm → set the profile's `status`.

## Verify a fixture (lessons from 2026-10-12)
1. **Find the real chart first.** Cheap fixtures are rebadged OEM models: search the **menu text on the display**
   (`Addr CHnd SLnd bLnd LEd`) and the channel count, not the brand; `browser/sites/amazon.py item ASIN` → `docs` lists
   manual PDFs; manuals.plus pages are often AI summaries without the table. Best: the printed manual.
2. OEM menu: `SLnd` **SL 1 = DMX**, SL 2 = slave, `auto`/`soun` = built-in shows; `bLnd` = no-signal behaviour; `LEd` =
   display timeout. A head that moves but stays dark is missing its dimmer channel; auto/sound channels light it with
   colour cycling, which misleads channel hunting.
3. Confirm one change at a time (`probe.py set`, observer reports; `--speak` for timed steps); mirrored motors get
   `"invert": true`; then compare the real light with the 3D window paused (`space`, `,` `.` to step, joints table).

## songmap accuracy (2026-11-02, vs rekordbox grids on the owner's library, 90 random tracks in 3 sets)
- Tempo: 82/90 exact (±0.5 BPM); 5 half/double (hip-hop at 72-76 vs 145-152 — use `--bpm` or `fix --bpm`); 3 wrong
  (medleys, live tempo). Beat grid within 35 ms of rekordbox: 65/90 (most misses = half a beat out, where rekordbox and the
  bass disagree: 30-190 Hz attack picks the beat and agreed with rekordbox on 89 % of 70 tracks). Downbeat (bar 1): 55/65 of
  the grids that matched. ~0.3 s per song. A synthetic song in wav/mp3/m4a/flac/aiff/opus/mp4/mkv/webm gives identical results
  (grid within 15 ms, sections exactly as built). These are agreement numbers: rekordbox itself is sometimes off.
- Sections/drops are energy heuristics (bass + kick returning after a low stretch, snapped to 4-bar phrases); no ground truth
  was available beyond synthetic songs and spot checks (Spooky: drops 9/33/65 vs rekordbox's bass cut at 31-33).


## Limits
- One universe, one dongle (FTDI "Open DMX" style; Enttec Pro / SoundSwitch interfaces not supported). User-space timing.
- Link: listen-only (never joins/alters the DJ's session); IPv4; verified against our own test peer, **not yet against
  rekordbox itself** — first real check: `net/abletonlink.py watch` with LINK on. Link carries no track names.
- Audio: needs the app on this Mac and macOS "System Audio Recording" permission (granted for this terminal 2026-10-12;
  verified with afplay: 16/16 kicks). Hit detection is tuned for kick-driven music.
- Effects are stateless functions of the Tick; cheap heads lag fast sweeps. Spider has no pan under DMX.
- stage3d: ≤ 12 parts per fixture, ≤ 64 parts, ≤ 32 beams; single scattering, no shadows; lumens are estimates; venue
  from one photo; pan direction of the beams not yet checked against the real heads (`invert_pan` in the rig).
- Console has no login: localhost by default; `--lan` only on a trusted network. Laser forced off unless armed.
- dj.py play: no pause/seek during playback (start with `--start-bar`); lights start when the player process starts
  (afplay ~tens of ms) — tune `latency_ms` by ear; songs assumed 4/4; generic/OFL profiles are unverified until `dj.py test`.
- Plain-word places are a first guess for aim and 3D: measure and use `--pos x,y,z` when precise aiming matters; mover pan
  direction must be checked once per unit (`dj.py point`). Auto shows keep beams above the crowd's eyes; when a rig cannot
  (heads hung low under a low ceiling) the show says so in meta.warnings and aims at the floor.

## Depends on / used by
hw/serial + hw/usb (dongle), net/abletonlink (+ net/lan), media/audiotap (+ media/synth in tests), hw/sysmon (find apps),
geometry/vec + frames, graphics/gl (ShaderProgram, Upscaler, hud_text), gui/glapp, core/files. Used by the owner's parties.
Tests: `test_lighting.py` (photometry), `test_lighting_control.py` (dmx, profiles, render, program, console API),
`test_lighting_timing.py` (clocks + Link, songs, step-back, audio react, kick analyzer), `test_lighting_stage.py`
(kinematics, occlusion, eye hazards, AutoOutput, GPU frame), `test_net_abletonlink.py`, `test_lighting_web.py` (exports re-read independently, MCP in-process + real stdio, web API, motor physics + dark moves + motion
audit, cue edits, 8x simulation/seek/rate, venue/undo, SSE stream), `test_lighting_dj.py` (profile defaults/
wheels/standard groups, plain-word patching + DIP, OFL import, aim_at vs the 3D model, songmap on a synthetic song in 5 formats +
grid fixes + rekordbox XML, autoshow safety on several rigs, FileClock, play in simulation).

## Ideas / next
- Check Link against rekordbox on the night; if rekordbox runs on another computer, audio needs a mic/line-in feed
  (Core Audio input device tap — small addition to media/audiotap).
- Per-song files (`songs/*.json`) once there are many; a "record cues live" mode that writes a song's cue sheet from
  button presses on bar lines.
- Done 2026-11-02: phrase/drop detection (songmap), IK aiming (stage3d.aim_at), any-rig shows (autoshow).
- Next: Enttec DMX USB Pro protocol + multi-universe; Serato/Traktor grid import; pause/seek in `dj.py play` (decode to a
  stream we control); a "record cues live" mode; hooks/lyrics from the owner's DJ research notes's lyrics pipeline into autoshow.
- stage3d: fog volume from the fogger, truss/fixture shadows, click a surface to aim a mover there (aim_at exists).
