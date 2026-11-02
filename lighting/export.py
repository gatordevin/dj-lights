"""Export a song's light show (and the rig) for other lighting software: QLC+ workspace, DMX frame CSV, cue CSV, MIDI cue track, patch list.
CLI:
  python3 lighting/export.py FILE --rig RIG [--formats qlc,dmx,cues,midi,patch,native] [--fps 30] [show options as dj.py show]
      -> out/lighting/exports/<song>__<rig>/ + .zip (README.txt inside says how to import each file)
Python: export(file, rig, formats=ALL, fps=30, show=None, out_dir=None, **show_opts) -> {"dir", "zip", "files": {format: [paths]}}
  render_frames(songmap, rig, show, fps) -> [(t_s, bytes(512))]    the show baked to DMX frames on the song's own beat grid
Formats:
  qlc     QLC+ 4 workspace (<song>.qxw) + one fixture definition per profile (fixtures/*.qxf, manufacturer "djpy"): fixtures
          patched at their addresses, a "Show" with the song audio on one track and the baked light Sequence (one step per
          change, hold in ms) on the other — open the .qxw in QLC+ (copy fixtures/*.qxf to QLC+'s user fixtures folder first:
          macOS ~/Library/Application Support/QLC+/Fixtures).
  dmx     <song>_dmx.csv: time_s then one column per patched channel ("par1.red@3"), one row per frame — for any tool that
          imports DMX recordings, spreadsheets, or custom players.
  cues    <song>_cues.csv: start_s, bar, beat, cue, section — markers for timecode/cue-list software (MA onPC, Lightkey cue lists,
          SoundSwitch/rekordbox notes, video editors).
  midi    <song>_cues.mid: standard MIDI file at the song's tempo: a marker per section/cue change, note C1+k for cue k (the
          legend is in cues.csv / README) and note C4 on each drop — drop it into Ableton/any DAW or MIDI-triggered lighting.
  patch   rig_patch.csv: light, profile, maker/model, mode, start..end address, channels, DIP switches, x/y/z, where.
  native  <song>_show.json + <song>_songmap.json (this toolbox's own formats; `dj.py play FILE --show ..._show.json`).
Fails: LookupError for unknown formats/rig; ValueError from analysis; files are written only under out/lighting/exports.
Limits: shows are baked (effects become fixed per-frame values: a QLC+ user edits steps, not our effects); one universe;
  the QLC+ files follow the QLC+ 4.x XML formats but were checked by our own parser, not yet opened in QLC+ itself;
  MIDI tempo is the song's average BPM (drifting live recordings use the CSVs for exact times).
Depends on: lighting/{songmap, autoshow, show, fixtures, patch, clock}, core/files. Used by: lighting/dj.py (export), webapp, mcp_server."""
import sys, csv, json, math, struct, zipfile, argparse, html
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lighting.fixtures import Rig

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "lighting" / "exports"
FORMATS = ("qlc", "dmx", "cues", "midi", "patch", "native")
QLC_GROUP = {"dimmer": ("Intensity", None, 0), "red": ("Intensity", "Red", 0), "green": ("Intensity", "Green", 0), "blue": ("Intensity", "Blue", 0),
             "white": ("Intensity", "White", 0), "amber": ("Intensity", "Amber", 0), "uv": ("Intensity", "UV", 0), "pan": ("Pan", None, 0),
             "pan_fine": ("Pan", None, 1), "tilt": ("Tilt", None, 0), "tilt_fine": ("Tilt", None, 1), "speed": ("Speed", None, 0),
             "strobe": ("Shutter", None, 0), "program": ("Effect", None, 0), "reset": ("Maintenance", None, 0), "wheel": ("Colour", None, 0),
             "laser": ("Effect", None, 0), "haze": ("Effect", None, 0), "raw": ("Nothing", None, 0), "cell": ("Intensity", None, 0)}
QLC_TYPE = {"par": "Color Changer", "bar": "LED Bar (Pixels)", "moving_head": "Moving Head", "spider": "Effect", "strobe": "Strobe",
            "laser": "Laser", "haze": "Hazer", "effect": "Effect"}


def _slug(s):
    import re
    return re.sub(r"[^A-Za-z0-9]+", "_", s)[:48].strip("_") or "song"


def render_frames(sm, rig, show, fps=30):
    """Bake the show to DMX frames at `fps` on the song's beat grid (same engine + timeline as dj.play)."""
    from lighting.show import Engine, _song_timeline
    from lighting.clock import ManualClock
    from lighting.songmap import beat_at
    clk = ManualClock(sm["bpm"]); eng = Engine(rig, clock=clk); sheet = show["songs"]["song"]["cues"]
    eng.cue(show, sheet[0][1]); tl, end = _song_timeline(show, "song", 0.0); eng._start_timeline(show, tl, end, "song")
    frames = []; n = int(sm["duration_s"] * fps)
    for k in range(n + 1):
        t = k / fps; clk.value = beat_at(sm, t); eng.frame(); frames.append((round(t, 4), eng.universe.frame()))
        if not eng.timeline and eng.program_done: break
    return frames


def _channels(rig):
    return [(f.address + i, f"{f.name}.{n}", f, i, s) for f in sorted(rig.fixtures, key=lambda f: f.address) for i, n, s in f.chans]


def _cue_rows(sm, show):
    from lighting.songmap import time_at
    secs = {}
    for s in sm["sections"]:
        for b in range(s["start_bar"], s["end_bar"] + 1): secs[b] = s["type"]
    return [{"start_s": round(time_at(sm, (bar - 1) * 4), 3), "bar": bar, "beat": (bar - 1) * 4, "cue": cue, "section": secs.get(bar, "")}
            for bar, cue in show["songs"]["song"]["cues"]]


# ---------- QLC+ ----------
def qxf(profile, manufacturer="djpy"):
    """QLC+ fixture definition XML for one lighting profile (all modes)."""
    d = profile.d; e = html.escape; out = ['<?xml version="1.0" encoding="UTF-8"?>', '<!DOCTYPE FixtureDefinition>',
        '<FixtureDefinition xmlns="http://www.qlcplus.org/FixtureDefinition">',
        ' <Creator>', '  <Name>Q Light Controller Plus</Name>', '  <Version>4.12.7</Version>', '  <Author>lighting/export.py</Author>', ' </Creator>',
        f' <Manufacturer>{e(manufacturer)}</Manufacturer>', f' <Model>{e(profile.name)}</Model>', f' <Type>{QLC_TYPE.get(profile.type, "Other")}</Type>']
    for n, s in d["channels"].items():
        grp, col, byte = QLC_GROUP.get(s["role"], ("Nothing", None, 0))
        if s["role"] == "cell":
            r, g, b = s["rgb"]; col = "White" if min(s["rgb"]) > 0 else "Red" if r else "Green" if g else "Blue"
        out.append(f' <Channel Name="{e(n)}"' + (f' Default="{s["default"]}"' if "default" in s else "") + '>')
        out.append(f'  <Group Byte="{byte}">{grp}</Group>')
        if col: out.append(f'  <Colour>{col}</Colour>')
        if s["role"] == "wheel":
            sl = sorted(s["slots"], key=lambda x: x["value"])
            for k, x in enumerate(sl):
                hi = (sl[k + 1]["value"] - 1) if k + 1 < len(sl) else 255
                rgb = "#%02x%02x%02x" % tuple(int(c * 255) for c in x["rgb"])
                out.append(f'  <Capability Min="{x["value"]}" Max="{hi}" Preset="ColorMacro" Res1="{rgb}">{e(x.get("name", rgb))}</Capability>')
        elif s["role"] == "strobe" and "strobe_range" in s:
            lo, hi = s["strobe_range"]; dflt = s.get("default", 0)
            if lo > 0: out.append(f'  <Capability Min="0" Max="{lo - 1}">Open / no strobe (rest {dflt})</Capability>')
            out.append(f'  <Capability Min="{lo}" Max="{hi}">Strobe slow to fast</Capability>')
            if hi < 255: out.append(f'  <Capability Min="{hi + 1}" Max="255">Other (see manual)</Capability>')
        else: out.append(f'  <Capability Min="0" Max="255">{e(s.get("notes", n))[:60] or n}</Capability>')
        out.append(' </Channel>')
    for m, chans in d["modes"].items():
        out.append(f' <Mode Name="{e(m)}">')
        for i, n in enumerate(chans): out.append(f'  <Channel Number="{i}">{e(n)}</Channel>')
        heads = {}
        for i, n in enumerate(chans):
            h = d["channels"][n].get("head")
            if h is not None: heads.setdefault(h, []).append(i)
        for h in sorted(heads):
            out.append('  <Head>'); out += [f'   <Channel>{i}</Channel>' for i in heads[h]]; out.append('  </Head>')
        out.append(' </Mode>')
    o = d.get("optics", {}); body = [int(x * 1000) for x in o.get("body", [0.2, 0.2, 0.2])]
    out += [' <Physical>', f'  <Bulb Type="LED" Lumens="{int(o.get("lumens", 0))}" ColourTemperature="0"/>',
            f'  <Dimensions Weight="0" Width="{body[0]}" Height="{body[2]}" Depth="{body[1]}"/>',
            f'  <Lens Name="Other" DegreesMin="{o.get("beam_deg", 0)}" DegreesMax="{o.get("beam_deg", 0)}"/>',
            f'  <Focus Type="{"Head" if profile.type == "moving_head" else "Fixed"}" PanMax="{int(d.get("pan_deg", 0))}" TiltMax="{int(d.get("tilt_deg", 0))}"/>',
            '  <Technical PowerConsumption="0" DmxConnector="3-pin"/>', ' </Physical>', '</FixtureDefinition>']
    return "\n".join(out) + "\n"


def qxw(sm, rig, frames, audio_name, manufacturer="djpy"):
    """QLC+ workspace XML: patched fixtures, baked Sequence, Audio and a Show with both on tracks."""
    e = html.escape; fx = sorted(rig.fixtures, key=lambda f: f.address); fid = {f.name: i for i, f in enumerate(fx)}
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<!DOCTYPE Workspace>', '<Workspace xmlns="http://www.qlcplus.org/Workspace" CurrentWindow="ShowManager">',
           ' <Creator>', '  <Name>Q Light Controller Plus</Name>', '  <Version>4.12.7</Version>', '  <Author>lighting/export.py</Author>', ' </Creator>',
           ' <Engine>', '  <InputOutputMap>', '   <BeatGenerator BeatType="Disabled" BPM="0"/>', '   <Universe Name="Universe 1" ID="0"/>', '  </InputOutputMap>']
    for f in fx:
        out += ['  <Fixture>', f'   <Manufacturer>{e(manufacturer)}</Manufacturer>', f'   <Model>{e(f.profile.name)}</Model>', f'   <Mode>{e(f.mode)}</Mode>',
                f'   <ID>{fid[f.name]}</ID>', f'   <Name>{e(f.name)}</Name>', '   <Universe>0</Universe>', f'   <Address>{f.address - 1}</Address>',
                f'   <Channels>{len(f.chans)}</Channels>', '  </Fixture>']
    dur_ms = int(round(frames[-1][0] * 1000)) + 1000
    out += ['  <Function ID="0" Type="Scene" Name="song: all channels" Hidden="True">', '   <Speed FadeIn="0" FadeOut="0" Duration="0"/>']
    out += [f'   <FixtureVal ID="{fid[f.name]}">' + ",".join(f"{i},0" for i in range(len(f.chans))) + '</FixtureVal>' for f in fx]
    out += ['  </Function>', f'  <Function ID="1" Type="Sequence" Name="{e(sm["title"])} lights" BoundScene="0">', '   <Speed FadeIn="0" FadeOut="0" Duration="0"/>',
            '   <Direction>Forward</Direction>', '   <RunOrder>SingleShot</RunOrder>', '   <SpeedModes FadeIn="PerStep" FadeOut="PerStep" Duration="PerStep"/>']
    steps, k = [], 0
    while k < len(frames):                                                   # one step per change; hold until the next change
        j = k + 1
        while j < len(frames) and frames[j][1] == frames[k][1]: j += 1
        t1 = frames[j][0] if j < len(frames) else frames[k][0] + 1.0
        steps.append((frames[k][1], int(round((t1 - frames[k][0]) * 1000)))); k = j
    nvals = sum(len(f.chans) for f in fx)
    for n, (fr, hold) in enumerate(steps):
        body = ":".join(f"{fid[f.name]}:" + ",".join(f"{i},{fr[f.address - 1 + i]}" for i in range(len(f.chans))) for f in fx)
        out.append(f'   <Step Number="{n}" FadeIn="0" Hold="{hold}" FadeOut="0" Values="{nvals}">{body}</Step>')
    out += ['  </Function>', f'  <Function ID="2" Type="Audio" Name="{e(sm["title"])}">', f'   <Speed FadeIn="0" FadeOut="0" Duration="{dur_ms}"/>',
            f'   <Source>{e(audio_name)}</Source>', '  </Function>',
            f'  <Function ID="3" Type="Show" Name="{e(sm["title"])} show">', f'   <TimeDivision Type="Time" BPM="{int(round(sm["bpm"]))}"/>',
            '   <Track ID="0" Name="Lights" SceneID="0" isMute="0">', f'    <ShowFunction ID="1" StartTime="0" Duration="{dur_ms}" Color="#5c8bd6"/>', '   </Track>',
            '   <Track ID="1" Name="Audio" isMute="0">', f'    <ShowFunction ID="2" StartTime="0" Duration="{dur_ms}" Color="#62c370"/>', '   </Track>',
            '  </Function>', ' </Engine>', '</Workspace>']
    return "\n".join(out) + "\n", len(steps)


# ---------- MIDI ----------
def _vlq(n):
    b = [n & 0x7F]; n >>= 7
    while n: b.append(0x80 | (n & 0x7F)); n >>= 7
    return bytes(reversed(b))


def midi(sm, show, rows, ppq=480):
    """Type-1 SMF: track 0 tempo + time signature + section/cue markers; track 1 notes (cue k -> note 24+k, drops -> 60)."""
    from lighting.songmap import beat_at
    bpm = sm["bpm"]; tick = lambda t: max(0, int(round((t - sm["first_downbeat_s"]) * bpm / 60 * ppq + ppq * 4)))   # bar 1 = start of MIDI bar 2
    cues = sorted({r["cue"] for r in rows}); note = {c: 24 + k for k, c in enumerate(cues)}
    ev0 = [(0, b"\xff\x51\x03" + int(60e6 / bpm).to_bytes(3, "big")), (0, b"\xff\x58\x04\x04\x02\x18\x08"), (0, b"\xff\x03" + _vlq(len(b"tempo")) + b"tempo")]
    for r in rows:
        txt = f"bar {r['bar']}: {r['cue']}" + (f" ({r['section']})" if r["section"] else ""); tb = txt.encode()
        ev0.append((tick(r["start_s"]), b"\xff\x06" + _vlq(len(tb)) + tb))
    ev1 = [(0, b"\xff\x03" + _vlq(len(b"cues")) + b"cues")]
    for k, r in enumerate(rows):
        t0 = tick(r["start_s"]); t1 = tick(rows[k + 1]["start_s"]) if k + 1 < len(rows) else t0 + ppq * 4
        ev1 += [(t0, bytes([0x90, note[r["cue"]], 100])), (max(t0 + 1, t1 - 1), bytes([0x80, note[r["cue"]], 0]))]
    from lighting.songmap import time_at
    for d in sm.get("drops", []):
        t0 = tick(time_at(sm, (d - 1) * 4)); ev1 += [(t0, bytes([0x91, 60, 127])), (t0 + ppq, bytes([0x81, 60, 0]))]
    def track(evs):
        evs = sorted(evs, key=lambda x: (x[0], x[1][0] == 0x90)); data, last = b"", 0
        for t, e in evs: data += _vlq(t - last) + e; last = t
        data += b"\x00\xff\x2f\x00"; return b"MTrk" + struct.pack(">I", len(data)) + data
    return b"MThd" + struct.pack(">IHHH", 6, 1, 2, ppq) + track(ev0) + track(ev1), note


# ---------- main ----------
def export(file, rig, formats=FORMATS, fps=30, show=None, out_dir=None, **show_opts):
    from lighting.songmap import analyze
    from lighting.autoshow import make_show
    from lighting.patch import dip
    from core.files import write_atomic
    bad = [f for f in formats if f not in FORMATS]
    if bad: raise LookupError(f"unknown export formats {bad} (have {FORMATS})")
    if not 5 <= fps <= 44: raise ValueError("fps must be 5..44")
    rig = rig if isinstance(rig, Rig) else Rig.load(rig); sm = analyze(file)
    if isinstance(show, (str, Path)):
        from lighting.show import load_show; show = load_show(str(show))
    show = show or make_show(sm, rig, **show_opts)
    stem = _slug(sm["title"]); d = Path(out_dir) if out_dir else OUT / f"{stem}__{_slug(rig.name)}"; d.mkdir(parents=True, exist_ok=True)
    files = {}; rows = _cue_rows(sm, show)
    frames = render_frames(sm, rig, show, fps) if {"qlc", "dmx"} & set(formats) else None
    if "dmx" in formats:
        p = d / f"{stem}_dmx.csv"; ch = _channels(rig)
        with open(p, "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["time_s"] + [f"{n}@{a}" for a, n, *_ in ch])
            for t, fr in frames: w.writerow([t] + [fr[a - 1] for a, *_ in ch])
        files["dmx"] = [str(p)]
    if "cues" in formats or "midi" in formats:
        p = d / f"{stem}_cues.csv"
        with open(p, "w", newline="") as fh:
            w = csv.DictWriter(fh, ["start_s", "bar", "beat", "cue", "section"]); w.writeheader(); w.writerows(rows)
        files["cues"] = [str(p)]
    if "midi" in formats:
        data, notes = midi(sm, show, rows); p = d / f"{stem}_cues.mid"; p.write_bytes(data)
        (d / f"{stem}_midi_notes.txt").write_text("".join(f"note {n} = cue {c}\n" for c, n in sorted(notes.items(), key=lambda x: x[1])) + "note 60 (C4, channel 2) = drop\n")
        files["midi"] = [str(p), str(d / f"{stem}_midi_notes.txt")]
    if "patch" in formats:
        p = d / "rig_patch.csv"
        with open(p, "w", newline="") as fh:
            w = csv.writer(fh); w.writerow(["light", "profile", "maker", "model", "mode", "start", "end", "channels", "dip_on", "x_m", "y_m", "z_m", "mount", "where", "channel_list"])
            for f in sorted(rig.fixtures, key=lambda f: f.address):
                w.writerow([f.name, f.profile.name, f.profile.d.get("maker", ""), f.profile.d.get("model", ""), f.mode, f.address, f.last, len(f.chans),
                            " ".join(map(str, dip(f.address))), *f.pos, f.mount, f.where, " ".join(f"{f.address + i}={n}" for i, n, _ in f.chans)])
        files["patch"] = [str(p)]
    if "native" in formats:
        a, b = d / f"{stem}_show.json", d / f"{stem}_songmap.json"
        write_atomic(a, json.dumps(show, indent=1) + "\n"); write_atomic(b, json.dumps(sm) + "\n"); files["native"] = [str(a), str(b)]
    if "qlc" in formats:
        fd = d / "fixtures"; fd.mkdir(exist_ok=True); qf = []
        for pr in {f.profile.name: f.profile for f in rig.fixtures}.values():
            p = fd / f"djpy-{pr.name}.qxf"; p.write_text(qxf(pr)); qf.append(str(p))
        audio = d / f"{stem}{Path(sm['file']).suffix.lower()}"
        if not audio.exists(): audio.write_bytes(Path(sm["file"]).read_bytes())
        xml, nsteps = qxw(sm, rig, frames, audio.name); p = d / f"{stem}.qxw"; p.write_text(xml)
        files["qlc"] = [str(p), str(audio)] + qf
    readme = d / "README.txt"
    readme.write_text(f"""Light show export: {sm['title']}  ({sm['bpm']} BPM, {sm['nbars']} bars, drops at bars {sm['drops']})
Rig: {rig.name}, {len(rig.fixtures)} lights, one DMX universe. Made by dj-lights lighting/export.py.

QLC+ (free, qlcplus.org): copy fixtures/*.qxf into QLC+'s user fixtures folder (macOS: ~/Library/Application Support/QLC+/Fixtures,
  Windows: %USERPROFILE%\\QLC+\\Fixtures, Linux: ~/.qlcplus/fixtures), restart QLC+, open {stem}.qxw, set Universe 1's output to your
  DMX interface (Inputs/Outputs tab), then Show Manager -> "{sm['title']} show" -> play. The lights are a baked sequence (one step per change).
{stem}_dmx.csv: every DMX channel value per frame ({fps} fps): time_s, then "light.channel@address" columns.
{stem}_cues.csv: cue list with start times, bars and song sections - paste into cue-list/timecode software as markers.
{stem}_cues.mid: MIDI at {sm['bpm']} BPM; markers name each cue; notes per cue ({stem}_midi_notes.txt), drops on note 60 channel 2.
  Bar 1 of the song is MIDI bar 2 (one empty bar of lead-in); line the audio's first downbeat ({sm['first_downbeat_s']} s) up with bar 2.
rig_patch.csv: where each light is and how to address it (start address, DIP switches ON).
{stem}_show.json / _songmap.json: native files (python3 lighting/dj.py play SONG --show {stem}_show.json).
""")
    z = d.with_suffix(".zip")
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(d.rglob("*")):
            if p.is_file(): zf.write(p, p.relative_to(d.parent))
    return {"dir": str(d), "zip": str(z), "files": files, "frames": len(frames) if frames else 0, "cues": len(rows)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file"); ap.add_argument("--rig", required=True); ap.add_argument("--formats", default=",".join(FORMATS)); ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--palette", default="auto"); ap.add_argument("--style", default="club"); ap.add_argument("--strobe", action="store_true"); ap.add_argument("--fog", action="store_true")
    a = ap.parse_args()
    r = export(a.file, a.rig, [f.strip() for f in a.formats.split(",") if f.strip()], a.fps, palette_name=a.palette, style=a.style, strobe=a.strobe, fog=a.fog)
    print(json.dumps(r, indent=1))


if __name__ == "__main__":
    try: main()
    except (LookupError, ValueError, FileNotFoundError) as e: print(f"export: {e}", file=sys.stderr); sys.exit(1)
