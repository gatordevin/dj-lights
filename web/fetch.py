"""HTTP with a browser User-Agent and gzip (stdlib urllib). One code path for text and binary.
fetch_bytes(url, data=None, headers=None, timeout=20, cache=0, retries=0) -> (bytes, info)  raw body (gzip undone); info = headers
  retries=N: retry up to N times on network errors, timeouts, 429 and 5xx, with exponential backoff (1, 2, 4.. s,
  honouring Retry-After up to 60 s). 4xx other than 429 never retry. Use it for unattended/scheduled jobs.
  cache=N: GET responses are kept N seconds in out/cache/web/ (keyed by URL+headers); a hit returns the cached body
  and its saved headers (an email.message.Message). Errors are never cached.
fetch(url, ...) -> str           body decoded with the response charset, else UTF-8 (errors replaced)
download(url, path, ..., retries=2, max_bytes=None) -> Path streams the body to path in 1 MB chunks (parent dirs created; written to
  path.part then renamed, so a failed download never leaves a truncated file). For PDFs, images, zips, datasets.
  max_bytes bounds decompressed output; overflow raises ValueError, removes .part,
  and leaves an existing destination intact. Gzip is streamed, not fully expanded in RAM.
request(method, url, body=None, headers=None, timeout=20) -> (status, headers: dict lower-cased, body bytes)
    (every Set-Cookie is kept: headers['set-cookie'] = values joined by newlines; cookie_header(headers) -> 'a=1; b=2')
  low-level primitive: any HTTP method, never raises for HTTP status codes (4xx/5xx come back as values), gzip undone.
  Network errors (URLError/TimeoutError) still raise. Used by web/jsonapi.py.
fetch_range(url, start, length, timeout=30) -> (bytes, total_size, headers)
  strict HTTP byte-range read. Requires 206 + matching Content-Range and refuses
  a server that ignores Range, preventing an accidental multi-GB download.
  data: dict (form-encoded → POST) or bytes (POST) or None (GET). headers override the defaults.
Fails: ValueError for non-http(s) URLs. urllib.error.HTTPError / URLError / TimeoutError propagate (callers decide on fallback).
Limits: whole body is held in memory except `download`; the disk cache only covers GET without data and never revalidates; timeout
  default 20 s; retries only inside fetch_bytes/download; no cookies, redirects follow urllib defaults.
Used by: web/search.py, web/read.py, web/jsonapi.py, documents/pdf.py, media/, ml/mnist.py."""
import urllib.request, urllib.parse, urllib.error, gzip, hashlib, json, time, email.message, re
from pathlib import Path

CACHE = Path(__file__).resolve().parents[1] / "out" / "cache" / "web"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

def _retrying(fn, retries):
    """Call fn(); on transient failure (network, timeout, 429, 5xx) back off and retry up to `retries` times."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if attempt == retries or not (e.code == 429 or e.code >= 500): raise
            ra = e.headers.get("Retry-After", "") if e.headers else ""
            time.sleep(min(60, float(ra)) if ra.replace(".", "", 1).isdigit() else 2 ** attempt)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == retries: raise
            time.sleep(2 ** attempt)

def fetch_bytes(url, data=None, headers=None, timeout=20, cache=0, retries=0):
    if not str(url).startswith(("http://", "https://")): raise ValueError(f"fetch: not an http(s) URL: {url!r}")
    if retries: return _retrying(lambda: fetch_bytes(url, data, headers, timeout, cache, 0), retries)
    if cache and data is None:
        key = CACHE / hashlib.sha1(json.dumps([url, headers], sort_keys=True).encode()).hexdigest()
        if key.exists() and time.time() - key.stat().st_mtime < cache:
            meta = json.loads(key.with_suffix(".json").read_text()); info = email.message.Message()
            for k, v in meta: info[k] = v
            return key.read_bytes(), info
        raw, info = fetch_bytes(url, None, headers, timeout)
        CACHE.mkdir(parents=True, exist_ok=True); key.write_bytes(raw)
        key.with_suffix(".json").write_text(json.dumps(list(info.items()))); return raw, info
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip"}
    h.update(headers or {})
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw, r.headers

def request(method, url, body=None, headers=None, timeout=20):
    if not str(url).startswith(("http://", "https://")): raise ValueError(f"request: not an http(s) URL: {url!r}")
    h = {"User-Agent": UA, "Accept-Encoding": "gzip"}; h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: status, hdrs, raw = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e: status, hdrs, raw = e.code, e.headers, e.read()
    if hdrs.get("Content-Encoding") == "gzip": raw = gzip.decompress(raw)
    out = {k.lower(): v for k, v in hdrs.items()}
    if hdrs.get_all("Set-Cookie"): out["set-cookie"] = "\n".join(hdrs.get_all("Set-Cookie"))   # all of them, newline-separated
    return status, out, raw

def fetch_range(url, start, length, timeout=30):
    """Read exactly one HTTP byte range without accepting a full-object fallback."""
    if not str(url).startswith(("http://", "https://")):
        raise ValueError(f"fetch_range: not an http(s) URL: {url!r}")
    if not isinstance(start, int) or start < 0:
        raise ValueError("start must be a non-negative integer")
    if not isinstance(length, int) or length <= 0:
        raise ValueError("length must be a positive integer")
    end = start + length - 1
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Encoding": "identity",
            "Range": f"bytes={start}-{end}",
        },
    )
    try:
        response = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"range request failed: HTTP {exc.code}") from exc
    with response:
        if response.status != 206:
            raise RuntimeError(
                f"server ignored byte range (HTTP {response.status}); refusing full download"
            )
        content_range = response.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
        if not match or int(match.group(1)) != start or int(match.group(2)) != end:
            raise RuntimeError(f"invalid Content-Range: {content_range!r}")
        raw = response.read(length + 1)
        if len(raw) != length:
            raise RuntimeError(f"range returned {len(raw)} bytes, expected {length}")
        total = None if match.group(3) == "*" else int(match.group(3))
        return raw, total, {k.lower(): v for k, v in response.headers.items()}

def cookie_header(hdrs):
    """'a=1; b=2' from a request() header dict's set-cookie values (name=value only), for the next request."""
    return "; ".join(c.split(";", 1)[0].strip() for c in (hdrs.get("set-cookie") or "").split("\n") if "=" in c)

def fetch(url, data=None, headers=None, timeout=20, cache=0, retries=0):
    raw, info = fetch_bytes(url, data, headers, timeout, cache, retries)
    return raw.decode(info.get_content_charset() or "utf-8", "replace")

def download(url, path, data=None, headers=None, timeout=60, retries=2, max_bytes=None):
    if not str(url).startswith(("http://", "https://")): raise ValueError(f"download: not an http(s) URL: {url!r}")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
        raise ValueError("max_bytes must be a positive integer or None")
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True); part = p.with_name(p.name + ".part")
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}; h.update(headers or {})
    if isinstance(data, dict): data = urllib.parse.urlencode(data).encode()
    def go():
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r, part.open("wb") as f:
            source = gzip.GzipFile(fileobj=r) if r.headers.get("Content-Encoding") == "gzip" else r
            count = 0
            while chunk := source.read(min(1 << 20, max_bytes-count+1) if max_bytes is not None else 1 << 20):
                count += len(chunk)
                if max_bytes is not None and count > max_bytes:
                    raise ValueError(f"download exceeds max_bytes={max_bytes}")
                f.write(chunk)
    try:
        _retrying(go, retries)
        part.replace(p)
    finally:
        part.unlink(missing_ok=True)
    return p
