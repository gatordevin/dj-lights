# Changelog

## 0.1.0 — 2026-11-02
First public release.
- **Web app**: Show (song library, upload, timeline with sections/drops/editable cues, play, 1–16× simulation with seek, live 3D),
  3D Stage (live beams with haze, click-select, drag-to-move, inspector for every light property and motor speed, venue editor,
  undo), Rig (search incl. Open Fixture Library, plan, address/DIP table), Live & Triggers, Export, Setup; localhost request guard.
- **Rig setup in plain words** with automatic DMX addresses and DIP switches; 16 generic fixture layouts + 4 real-unit profiles (2 verified on hardware);
  Open Fixture Library import; profiles from a manual's channel list.
- **Song analysis** for any audio/video format via ffmpeg: tempo, beat grid, downbeats, bars, energy, sections, drops; optional
  vocal activity (Demucs); rekordbox XML grids; per-song grid fixes.
- **Automatic shows** for any rig: build-ups, dark beat before drops, styles and palettes, eye-safe mover aiming, moving-head motor
  physics (moves in the dark, speed-aware timing, motion audit).
- **Playback**: in sync with the song, simulation, live via Ableton Link / Pro DJ Link with audio reaction.
- **Exports**: QLC+ workspace + fixture definitions, DMX frame CSV, cue CSV, MIDI cue track, patch CSV.
- **MCP server** (32 tools) and an agent skill.
