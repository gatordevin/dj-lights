# Security

## Model
- **No accounts, no API keys.** DJ Lights runs locally. The only network access it makes on its own is anonymous HTTPS to GitHub
  (Open Fixture Library search/import); Art-Net (UDP) only when you point it at a node; Ableton Link / Pro DJ Link listening on the
  local network in live mode.
- **The web app and console have no login.** They bind to `127.0.0.1` by default. Every request goes through
  `lighting/httpguard.py`: only `localhost`/`127.0.0.1` host names (bare IP addresses too with `--lan`), POSTs only from the same
  origin with a JSON/binary content type. This blocks DNS-rebinding and cross-site (CSRF) attacks from web pages you visit.
- **`--lan` exposes full control** of the lights to anyone on your network — use it only on networks you trust.
- File access through the web API is limited: downloads only from `out/lighting/`, uploads only music/video file names into
  `out/lighting/uploads/`. The MCP server talks over stdio to the assistant that launched it and can read the song files you name.

## Reporting a vulnerability
Please report privately via **GitHub → Security → Report a vulnerability** on this repository (or open an issue asking for a
contact without details). Include steps to reproduce. You'll get a reply as soon as possible; please allow time for a fix before
disclosing publicly.
