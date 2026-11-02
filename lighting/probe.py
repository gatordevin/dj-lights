"""Find out what a fixture's DMX channels do: hold raw values or play numbered, spoken steps on the real rig while a person watches.
Works with or without a running console: if lighting/console.py answers (ConsoleClient), values go through its "raw"
override (the show keeps running underneath); otherwise this opens the USB dongle itself (only one program can).
CLI:
  python3 lighting/probe.py set 4=255 8=255 [--clear]       hold raw channels (console: until `clear`; standalone: --seconds)
  python3 lighting/probe.py clear                           release all raw overrides
  python3 lighting/probe.py steps "6=255 7=255" "6=255 7=10" ... [--hold 5] [--base "1=128 3=128"] [--speak]
  python3 lighting/probe.py walk START COUNT [--value 255] [--hold 4] [--base ...] [--speak]   one channel at a time
      steps: space-separated CH=VAL (other channels 0, --base on every step); prints/speaks "step N" as each starts
  common: [--url http://127.0.0.1:8779] [--port auto] [--seconds 60] (standalone hold time for `set`)
Python: parse("4=255 8=255") -> {4: 255, 8: 255};  play(steps, hold, base=None, sink=None, say=print)
        sink(dict) applies one step: console_sink(client) or dongle_sink(output) (both return (apply, close))
Contract: channels 1..512, values 0..255 (ValueError). Standalone runs black out at the end. Needs an observer.
Lesson (2026-10-12): find the real chart first (search the fixture's menu text, read the printed manual); use this to
  confirm, one change at a time — a moving-but-dark head usually just lacks its dimmer channel.
Limits: needs a person watching; standalone mode takes the USB dongle exclusively; steps are timed with time.sleep (no sync to the show);
  --speak uses macOS `say`; the console path goes over local HTTP via ConsoleClient.
Fails: ValueError for malformed CH=VAL / channel or value out of range; a missing dongle or unreachable console errors propagate.
Depends on: lighting/dmx, lighting/console (ConsoleClient). Used by: fixture verification (lighting/README.md)."""
import sys, time, argparse, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lighting.dmx import Universe, Sender, open_output


def parse(spec):
    out = {}
    for p in (spec or "").split():
        ch, sep, v = p.partition("=")
        if not sep: raise ValueError(f"bad CH=VAL {p!r}")
        out[int(ch)] = int(v); Universe._check(int(ch), int(v))
    return out


def console_sink(client):
    return (lambda chans: client.api(action="raw", channels={str(c): v for c, v in chans.items()}),
            lambda: client.api(action="raw", channels={}))


def dongle_sink(output):
    u = Universe(); s = Sender(u, [output]).start()
    def apply(chans):
        u.clear()
        for c, v in chans.items(): u[c] = v
    return apply, lambda: s.stop()


def play(steps, hold, base=None, sink=None, say=print):
    apply, close = sink or dongle_sink(open_output("auto"))
    try:
        for i, st in enumerate(steps, 1):
            apply({**(base or {}), **st})
            say(f"STEP {i}: " + " ".join(f"{c}={v}" for c, v in sorted(st.items())))
            time.sleep(hold)
    finally: close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0]); sp = ap.add_subparsers(dest="cmd", required=True)
    st = sp.add_parser("set"); st.add_argument("pairs", nargs="+"); st.add_argument("--clear", action="store_true")
    sp.add_parser("clear")
    a1 = sp.add_parser("steps"); a1.add_argument("steps", nargs="+")
    a2 = sp.add_parser("walk"); a2.add_argument("start", type=int); a2.add_argument("count", type=int); a2.add_argument("--value", type=int, default=255)
    for x in (a1, a2):
        x.add_argument("--speak", action="store_true"); x.add_argument("--hold", type=float, default=5); x.add_argument("--base", default="")
    for x in sp.choices.values():
        x.add_argument("--url", default="http://127.0.0.1:8779"); x.add_argument("--port", default="auto"); x.add_argument("--seconds", type=float, default=60)
    a = ap.parse_args()
    from lighting.console import ConsoleClient
    client = ConsoleClient(a.url); live = client.alive()
    if a.cmd in ("set", "clear"):
        if live:
            cur = {} if (a.cmd == "clear" or a.clear) else {int(k): v for k, v in (client.state().get("raw") or {}).items()}
            chans = {**cur, **(parse(" ".join(a.pairs)) if a.cmd == "set" else {})}
            client.api(action="raw", channels={str(c): v for c, v in chans.items()})
            print("console raw override:", " ".join(f"{c}={v}" for c, v in sorted(chans.items())) or "(cleared)")
        elif a.cmd == "set":
            apply, close = dongle_sink(open_output(a.port)); apply(parse(" ".join(a.pairs)))
            print(f"holding for {a.seconds:.0f} s (no console running) ...");
            try: time.sleep(a.seconds)
            finally: close()
        return
    steps = [parse(s) for s in a.steps] if a.cmd == "steps" else [{c: a.value} for c in range(a.start, a.start + a.count)]
    def say(line):
        print(line, flush=True)
        if a.speak: subprocess.Popen(["say", line.split(":")[0].lower()])
    play(steps, a.hold, parse(a.base), console_sink(client) if live else dongle_sink(open_output(a.port)), say)


if __name__ == "__main__":
    try: main()
    except (LookupError, ValueError, RuntimeError) as e: print(f"probe: {e}", file=sys.stderr); sys.exit(1)
