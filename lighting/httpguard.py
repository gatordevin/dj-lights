"""Local web UI request guard: block DNS-rebinding and cross-site (CSRF) requests to the lights' HTTP servers on this machine.
Python: refuse(handler, method, lan=False, need_type=False) -> None | "reason"   call first in do_GET/do_POST; on a reason reply 403.
  Host header must be localhost / 127.0.0.1 / [::1] (any port), or — only when lan=True — a bare IP address (DNS rebinding needs a
  domain name, so names other than localhost are always refused). POST with an Origin header must come from the same host.
  need_type=True also requires a JSON or binary Content-Type on POST (a cross-site page can only send those after a CORS preflight,
  which these servers never grant).
Fails: never raises; returns the reason string. Limits: no authentication — anyone who can reach the port on the LAN (with --lan)
  can control the lights; use --lan only on trusted networks.
Depends on: stdlib. Used by: lighting/webapp.py, lighting/console.py."""
import re, urllib.parse

LOCAL = ("localhost", "127.0.0.1", "[::1]", "::1")


def _hostname(h):
    h = (h or "").strip().lower()
    if h.startswith("["): return h[:h.index("]") + 1] if "]" in h else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def refuse(handler, method, lan=False, need_type=False):
    host = handler.headers.get("Host", "")
    name = _hostname(host)
    ip = re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", name) or (name.startswith("[") and name.endswith("]"))
    if name not in LOCAL and not (lan and ip): return f"host {host!r} not allowed"
    if method == "POST":
        origin = handler.headers.get("Origin")
        if origin and origin != "null":
            o = urllib.parse.urlparse(origin)
            if o.netloc.lower() != host.lower(): return f"cross-site request from {origin} refused"
        elif origin == "null": return "opaque-origin request refused"
        if need_type:
            ct = (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ct not in ("application/json", "application/octet-stream"): return "POST needs Content-Type application/json"
    return None
