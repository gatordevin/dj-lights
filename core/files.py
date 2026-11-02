"""Atomic and private file writes: the one home for crash-safe replace and "write this so only I can read it" (mode 600).
Python: write_atomic(path, data, mode=None) -> Path   str/bytes/dict|list (→ JSON, indent 1, sorted keys) written to a
          temp file in the same folder and os.replace()d, so readers never see a half-written file and a crash leaves
          the old version (temp removed on failure). Parent dirs created. mode None = normal file (0o666 & ~umask).
        write_private(path, data, atomic=True) -> Path   the same with mode 600 (parent dirs 700 if new)
        chmod_private(path) -> Path   make an existing file 600 (e.g. after a download/move)
        read_json(path, default=None) -> object | a deep copy of default if missing (callers may mutate it)
Fails: TypeError for unsupported data; OSError from the filesystem. Limits: POSIX permissions only (no encryption) —
  anything running as this user can read it.
Used by: agents/store (board/mail/registry), web/polite + geo/gtfs (caches), freelance/ledger, web/credentials, browser/server + viewer (session/token files), browser/sites/venmo, finance/subscriptions,
  tasks/spending_review. Depends on: stdlib only (tier 0: every domain may import core).""" 
import os, copy, json, tempfile
from pathlib import Path


def _bytes(data):
    if isinstance(data, bytes): return data
    if isinstance(data, str): return data.encode()
    if isinstance(data, (dict, list)): return json.dumps(data, indent=1, sort_keys=True, default=str).encode()
    raise TypeError(f"write_atomic: unsupported data type {type(data).__name__}")


def _read_umask():
    m = os.umask(0o022); os.umask(m); return m


_FILE_MODE = 0o666 & ~_read_umask()       # read once at import: os.umask() is process-global, not thread-safe


def write_atomic(path, data, mode=None, _dir_mode=0o777):
    p = Path(path); b = _bytes(data)
    if not p.parent.exists(): p.parent.mkdir(parents=True, mode=_dir_mode, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix="." + p.name + ".")
    try:
        os.fchmod(fd, _FILE_MODE if mode is None else mode)
        with os.fdopen(fd, "wb") as f: fd = None; f.write(b)
        os.replace(tmp, p)
    except BaseException:
        if fd is not None:
            try: os.close(fd)
            except OSError: pass
        try: os.unlink(tmp)
        except OSError: pass
        raise
    return p


def write_private(path, data, atomic=True):
    p = Path(path)
    if atomic: return write_atomic(p, data, mode=0o600, _dir_mode=0o700)
    b = _bytes(data)
    if not p.parent.exists(): p.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f: f.write(b)
    os.chmod(p, 0o600)
    return p


def chmod_private(path):
    os.chmod(path, 0o600); return Path(path)


def read_json(path, default=None):
    p = Path(path)
    try: return json.loads(p.read_text())
    except FileNotFoundError: return copy.deepcopy(default)
