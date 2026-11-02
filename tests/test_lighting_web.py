"""lighting/ web app + MCP server + exports without hardware (simulation output): exports re-read by independent parsers (QLC+ XML
structure/step values/holds/addresses, MIDI chunks/tempo/markers, CSVs), the MCP protocol in-process and over real stdio
(initialize, tools/list, tools/call incl. an image result and errors), the web API (rig, song, show, manual mode + pads,
3D stage, stop, file guard), Session background play."""
import sys, json, time, struct, subprocess, threading, urllib.request, urllib.error, csv, wave, shutil
import xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests._util import check, OUT, require_optional
require_optional("numpy", why="song analysis")
import numpy as np
from lighting import patch as P, export as X, mcp_server as M, webapp as W, dj
from lighting.session import Session
from lighting.fixtures import Rig

D = OUT / "lighting_web"; shutil.rmtree(D, ignore_errors=True); D.mkdir(parents=True)
dj.SETTINGS = D / "settings.json"; dj.save_settings(port="none")                       # never drive real lights from tests
from lighting.audio_in import synth_track, SR
x = np.concatenate([np.zeros(int(SR * 0.5), np.float32), synth_track(bpm=128, bars=24)])
wav = D / "song.wav"
with wave.open(str(wav), "wb") as w: w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes((x * 32767).astype("<i2").tobytes())
rp = D / "rig.json"; d = P.new_rig("t_web", "club", truss=True, path=rp)
P.add(d, "generic_par_rgbw_8ch", 2, "floor behind the dj"); P.add(d, "generic_moving_head_rgbw_9ch", 2, "on the truss"); P.add(d, "generic_ledbar_4seg_rgb_12ch", 1, "booth"); P.save(d)
rig = Rig.load(rp)

# --- exports, re-read independently
r = X.export(wav, rig, out_dir=D / "exp", fps=20)
files = {k: [Path(p) for p in v] for k, v in r["files"].items()}
qxw = next(p for p in files["qlc"] if p.suffix == ".qxw"); ns = {"q": "http://www.qlcplus.org/Workspace"}
root = ET.parse(qxw).getroot(); fx = root.findall("q:Engine/q:Fixture", ns)
check(len(fx) == 5 and sorted(int(f.find("q:Address", ns).text) for f in fx) == sorted(f.address - 1 for f in rig.fixtures), "QLC+: fixtures patched at 0-based addresses")
seq = next(f for f in root.findall("q:Engine/q:Function", ns) if f.get("Type") == "Sequence"); steps = seq.findall("q:Step", ns)
nch = sum(len(f.chans) for f in rig.fixtures)
for st in steps[:50]:
    chunks = st.text.split(":"); pairs = sum(len(chunks[i + 1].split(",")) // 2 for i in range(0, len(chunks), 2))
    check(int(st.get("Values")) == nch == pairs, "QLC+ step: Values = channel count = pairs in the text")
total = sum(int(s_.get("Hold")) for s_ in steps) / 1000
sm = dj._analyze(wav); check(abs(total - sm["duration_s"]) < 2.0, f"QLC+ step holds add up to the song ({total:.1f} vs {sm['duration_s']})")
show_f = next(f for f in root.findall("q:Engine/q:Function", ns) if f.get("Type") == "Show")
check([t.get("Name") for t in show_f.findall("q:Track", ns)] == ["Lights", "Audio"] and next(f for f in root.findall("q:Engine/q:Function", ns) if f.get("Type") == "Audio").find("q:Source", ns).text.endswith(".wav"), "QLC+ show: lights + audio tracks")
for q in [p for p in files["qlc"] if p.suffix == ".qxf"]:
    fr = ET.parse(q).getroot(); fns = {"f": "http://www.qlcplus.org/FixtureDefinition"}
    chans = {c.get("Name") for c in fr.findall("f:Channel", fns)}
    for m in fr.findall("f:Mode", fns): check(all(c.text in chans for c in m.findall("f:Channel", fns)), f"{q.name}: mode channels defined")
bar = ET.parse(next(p for p in files["qlc"] if "ledbar" in p.name)).getroot()
check(len(bar.findall("f:Mode/f:Head", {"f": "http://www.qlcplus.org/FixtureDefinition"})) == 4, "LED bar segments exported as QLC+ heads")
rows = list(csv.reader(open(files["dmx"][0])))
check(rows[0][0] == "time_s" and len(rows[0]) == nch + 1 and len(rows) - 1 == r["frames"], "DMX CSV: one column per channel, one row per frame")
cues = list(csv.DictReader(open(files["cues"][0]))); check(len(cues) == r["cues"] and cues[0]["bar"] == "1", "cue CSV")
mid = files["midi"][0].read_bytes(); check(mid[:4] == b"MThd" and struct.unpack(">IHHH", mid[4:14]) == (6, 1, 2, 480), "MIDI header type 1, 2 tracks")
pos, tracks = 14, []
while pos < len(mid): tag, n = mid[pos:pos + 4], struct.unpack(">I", mid[pos + 4:pos + 8])[0]; tracks.append((tag, mid[pos + 8:pos + 8 + n])); pos += 8 + n
check(len(tracks) == 2 and all(t == b"MTrk" and b.endswith(b"\xff\x2f\x00") for t, b in tracks) and pos == len(mid), "MIDI chunks well formed")
check(b"\xff\x51\x03" + int(60e6 / sm["bpm"]).to_bytes(3, "big") in tracks[0][1] and tracks[0][1].count(b"\xff\x06") == len(cues), "MIDI tempo + one marker per cue")
check(Path(r["zip"]).exists() and "QLC+" in (D / "exp" / "README.txt").read_text(), "zip + README")
try: X.export(wav, rig, ["xlights"]); check(False, "unknown format accepted")
except LookupError: pass

# --- MCP in-process
tools = M.Tools(); tools.s.port = "none"
init = M.handle(tools, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
check(init["result"]["protocolVersion"] == "2025-03-26" and "tools" in init["result"]["capabilities"] and init["result"]["instructions"], "initialize")
check(M.handle(tools, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None, "notifications get no reply")
tl = M.handle(tools, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
check(len(tl) == len(M.TOOLS) >= 25 and all(t["inputSchema"]["type"] == "object" for t in tl), "tools/list")
def call(name, **a): return M.handle(tools, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": a}})["result"]
res = call("rig_details", rig=str(rp)); check(not res["isError"] and "address" in json.loads(res["content"][0]["text"])["sheet"].lower(), "rig_details")
res = call("analyze_song", file=str(wav)); check(json.loads(res["content"][0]["text"])["bpm"] == 128.0, "analyze_song")
res = call("preview_show", file=str(wav), rig=str(rp)); check(res["content"][1]["type"] == "image" and res["content"][1]["mimeType"] == "image/png", "preview_show returns an image")
res = call("make_show", file=str(wav), rig=str(rp)); check(json.loads(res["content"][0]["text"])["plan"][0][0] == 1, "make_show plan")
res = call("nope"); check(res["isError"] and "unknown tool" in res["content"][0]["text"], "unknown tool -> isError")
res = call("add_lights", profile="no_such_profile", rig=str(rp)); check(res["isError"] and "LookupError" in res["content"][0]["text"], "tool failure -> isError with reason")
check(M.handle(tools, {"jsonrpc": "2.0", "id": 9, "method": "bogus"})["error"]["code"] == -32601, "unknown method")
res = call("play_song", file=str(wav), rig=str(rp), audio=False); st = json.loads(res["content"][0]["text"])
check(st["mode"] == "play" and st["output"] == "MemoryOutput", f"play_song runs in the background in simulation {st}")
time.sleep(0.5); res = call("stop_lights"); check(json.loads(res["content"][0]["text"])["mode"] == "idle", "stop_lights")

# --- MCP over real stdio
env = {**__import__("os").environ}
p = subprocess.Popen([sys.executable, str(Path(M.__file__))], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_rigs", "arguments": {}}}]
out, err = p.communicate("\n".join(json.dumps(m) for m in msgs) + "\n", timeout=60)
lines = [json.loads(l) for l in out.splitlines() if l.strip()]
check([l["id"] for l in lines] == [1, 2, 3] and "example_mobile" in lines[2]["result"]["content"][0]["text"], f"stdio: only protocol on stdout {out[:200]} {err[-300:]}")

# --- web app API
s = Session(port="none"); srv = W.serve(s, 0); port = srv.server_address[1]; threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"
def get(path): return json.load(urllib.request.urlopen(base + path, timeout=30))
def post(path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try: return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e: return json.load(e)
check(b"DJ Lights" in urllib.request.urlopen(base + "/", timeout=10).read(), "page served")
rj = get(f"/api/rig?name={rp}")["data"]; check(len(rj["fixtures"]) == 5 and rj["fixtures"][0]["dip"] == [1], "rig API")
check(post("/api/rig/set", {"rig": str(rp), "light": "par1", "pos": [-1.0, 1.5, 0.0]})["data"]["fixtures"][0]["pos"] == [-1.0, 1.5, 0.0], "drag-to-place saves pos")
an = post("/api/analyze", {"file": str(wav)})["data"]; check(an["nbars"] > 20 and an["bars"][0]["section"], "analyze API")
sh = post("/api/show", {"file": str(wav), "rig": str(rp), "palette": "neon"})["data"]; check(sh["preview"].startswith("/api/file") and sh["plan"], "show API + preview")
check(urllib.request.urlopen(base + sh["preview"], timeout=10).read()[:4] == b"\x89PNG", "preview PNG served")
st = post("/api/manual", {"rig": str(rp), "bpm": 128})["data"]; check(st["mode"] == "manual" and "drop" in st["cues"], "manual mode")
post("/api/trigger", {"cue": "drop"}); time.sleep(0.3); check(get("/api/status")["data"]["cue"] == "drop", "pad switches the look")
stg = get("/api/stage")["data"]; check(len(stg["parts"]) > 10 and isinstance(stg["beams"], list) and stg["venue"]["template"] == "club", "3D stage data")
check(post("/api/master", {"value": 0.5})["data"]["master"] == 0.5, "master")
check(post("/api/stop", {})["data"]["mode"] == "idle", "stop")
bad = post("/api/trigger", {"cue": "drop"}); check(not bad["ok"] and "nothing running" in bad["error"], "errors come back as 400 + reason")
try: urllib.request.urlopen(base + "/api/file?path=/etc/hosts", timeout=5); check(False, "file guard")
except urllib.error.HTTPError as e: check(e.code == 400, "file outside out/lighting refused")
up = urllib.request.Request(base + "/api/upload?name=x.exe", data=b"MZ", headers={"Content-Type": "application/octet-stream"})
try: urllib.request.urlopen(up, timeout=5); check(False, "exe upload accepted")
except urllib.error.HTTPError as e: check(e.code == 400, "only music/video uploads")
def raw_req(path, method="GET", headers=None, data=None):
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    try: return urllib.request.urlopen(req, timeout=10).status
    except urllib.error.HTTPError as e: return e.code
check(raw_req("/api/status", headers={"Host": "evil.example.com"}) == 403, "DNS-rebinding host refused")
check(raw_req("/api/stop", "POST", {"Content-Type": "application/json", "Origin": "http://evil.example.com"}, b"{}") == 403, "cross-site POST refused")
check(raw_req("/api/stop", "POST", {"Content-Type": "text/plain"}, b"{}") == 403, "simple (no-preflight) POST refused")
check(raw_req("/api/stop", "POST", {"Content-Type": "application/json", "Origin": base}, b"{}") == 200, "same-origin POST allowed")
check(raw_req("/api/status", headers={"Host": f"localhost:{port}"}) == 200, "localhost host allowed")
srv.shutdown(); s.stop()
# --- moving-head physics, dark moves, cue edits, simulation speed, venue/undo, live stream
from lighting import motion as MO, autoshow as A
from lighting.show import Tick
mh = Rig.load(rp)["mover1"]; mh.home()
for n, _, _, _ in mh.joint_angles(): mh.set_joint(n, 33.0)
check(all(abs(dg - 33.0) < 1.5 for _, _, dg, _ in mh.joint_angles()), "set_joint inverts joint_angles")
r1 = Rig.load(rp); f1 = r1["mover1"]; f1.home(); m = MO.MotorSim(); m.step(r1, 0.01); f1.aim(120, 40); t = 0.0
while True:
    m.step(r1, 0.01); t += 0.01
    if not m.moving("mover1", 0.5) or t > 10: break
check(abs(t - MO.move_time(f1, 120, 40)) < 0.15, f"motor sim travel time {t:.2f} vs predicted {MO.move_time(f1, 120, 40):.2f}")
fast = MO.move_time(f1, 120, 0); f1.speed(1.0); check(MO.move_time(f1, 120, 0) > 4 * fast, "speed channel slows the head")
lay = {"spots": [[0, 0], [90, 30]], "every": 2, "lead": 1.0}
check(A.fx_snapdark(lay, 0, 1, Tick(1.2))["dimmer"] == 0.0 and "dimmer" not in A.fx_snapdark(lay, 0, 1, Tick(0.5)), "snapdark: dark only while travelling")
A.EDITS = D / "edits.json"; sm = dj._analyze(wav)
show0 = A.make_show(sm, rig)
check(show0["meta"]["motion"] and all(v["lit_moving_pct"] < 10 for v in show0["meta"]["motion"].values()), f"movers lit while travelling < 10 % {show0['meta']['motion']}")
slow = Rig.load(rp); slow["mover1"].pan_speed = 30.0; sh_slow = A.make_show(sm, slow)
check(sh_slow["meta"]["mover_leads"]["mover1"] > show0["meta"]["mover_leads"]["mover1"], "slower head starts its moves earlier")
A.set_edit(wav, rig.name, 3, 6, "peak"); sh_e = A.make_show(sm, rig, motion=False)
check(sh_e["meta"]["bar_plan"][2:6] == ["peak"] * 4 and sh_e["meta"]["edited_bars"] == [3, 4, 5, 6], "cue edits applied + remembered")
A.set_edit(wav, rig.name, 3, 6, None); check(A.make_show(sm, rig, motion=False)["meta"]["edited_bars"] == [], "edit removed")
try: A.set_edit(wav, rig.name, 1, 2, "laser_party"); check(False, "unknown cue accepted")
except LookupError: pass
s2 = Session(port="none"); st = s2.simulate(wav, str(rp), rate=8)
check(st["mode"] == "play" and st["rate"] == 8.0 and not st["audio"], "simulate at 8x")
time.sleep(1.0); b1 = s2.status()["bar"]; check(b1 >= 4, f"8x simulation advances fast (bar {b1} after 1 s at 128 BPM)")
s2.seek(20); time.sleep(0.15); check(s2.status()["bar"] in (20, 21), "seek"); s2.set_rate(2); check(s2.status()["rate"] == 2.0, "rate change")
fr, seq = s2.frame(); time.sleep(0.5); fr2, seq2 = s2.frame(); check(seq2 - seq >= 4 and fr2["beams"] is not None, f"3D frames stream from the display loop ({seq2 - seq} in 0.5 s)")
check("travel" in s2.status() and "mover1" in s2.status()["travel"], "status reports mover travel")
s2.stop()
s3 = Session(port="none"); srv2 = W.serve(s3, 0); port2 = srv2.server_address[1]; threading.Thread(target=srv2.serve_forever, daemon=True).start()
def post2(path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port2}" + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try: return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e: return json.load(e)
v = post2("/api/rig/venue", {"rig": str(rp), "room": [12, 9, 4], "truss": True, "truss_height": 3.2})["data"]["venue"]
check(v["room"] == [12.0, 9.0, 4.0] and v["truss"][2] == 3.2 and v["audience"][2] == -9.0, "venue edit")
post2("/api/rig/set", {"rig": str(rp), "light": "mover2", "pan_speed": 90, "mount": "floor"})
rj = post2("/api/rig/undo", {"rig": str(rp)})["data"]; m2 = next(f for f in rj["fixtures"] if f["name"] == "mover2")
check(m2["pan_speed"] is None and m2["mount"] == "hung" and rj["undo_left"] == 1, "undo restores the light")
check(post2("/api/rig/undo", {"rig": str(rp)})["data"]["venue"].get("room") != [12.0, 9.0, 4.0] and not post2("/api/rig/undo", {"rig": str(rp)})["ok"], "undo walks back, then refuses")
post2("/api/manual", {"rig": str(rp)})
resp = urllib.request.urlopen(f"http://127.0.0.1:{port2}/api/stream", timeout=10); msgs = []
while len(msgs) < 6: line = resp.readline().decode().strip(); msgs += [json.loads(line[6:])] if line.startswith("data: ") else []
resp.close(); check(any("stage" in m_ and m_["stage"]["parts"] for m_ in msgs) and any(m_.get("status", {}).get("mode") == "manual" for m_ in msgs), "SSE stream: status + 3D frames")
cut = post2("/api/show/edit", {"file": str(wav), "rig": str(rp), "start_bar": 2, "end_bar": 2, "cue": "drop"})["data"]
check(cut["edited_bars"] == [2] and "drop" in cut["cue_names"], "cue edit through the API"); A.set_edit(wav, Rig.load(rp).name, 2, 2, None)
post2("/api/stop", {}); srv2.shutdown(); s3.stop()
s4 = Session(port="none"); s4.manual(str(rp)); s4.trigger("groove_b"); time.sleep(0.4); lit = []
for _ in range(25): fr, _ = s4.frame(); lit.append(sum(1 for b_ in fr["beams"] if max(b_["rgb"]) > 0.001 and b_["name"].split(".")[0] in ("par1", "par2", "bar1"))); time.sleep(0.04)
s4.stop(); check(min(lit) >= 3, f"3D frames never catch a half-rendered (dark) engine frame; wash lit in manual mode {lit}")
res = call("simulate_song", file=str(wav), rig=str(rp), rate=4); check(json.loads(res["content"][0]["text"])["rate"] == 4.0, "MCP simulate_song")
res = call("seek", bar=10); check(not res["isError"], "MCP seek"); call("stop_lights")
res = call("edit_cue", file=str(wav), rig=str(rp), start_bar=1, end_bar=2, cue="intro"); check(json.loads(res["content"][0]["text"])["edited_bars"] == [1, 2], "MCP edit_cue")
res = call("edit_light", light="mover1", rig=str(rp), pan_speed=150); check(not res["isError"] and Rig.load(rp)["mover1"].pan_speed == 150, "MCP edit_light speed")
print("ok: lighting web (QLC+/MIDI/CSV exports re-read, MCP in-process + stdio, web API: rig/song/show/manual pads/3D/stop/guards, motor physics + dark moves + motion audit, cue edits, 8x simulation/seek/rate, venue/undo, SSE stream)")
