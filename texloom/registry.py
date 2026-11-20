"""The list of folders this computer keeps in sync (`texloom start` syncs them all).

Only paths are kept here; each folder's own settings and project link stay in the folder, in
this computer's part of its `.texloom` (paths.state_dir).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def config_dir() -> Path:
    """Per-user configuration directory for texloom."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return Path(os.environ.get("TEXLOOM_HOME") or base / "texloom")


def computer_id() -> str:
    """This computer's id (random, made once): names its part of each synced folder's `.texloom`,
    so that a folder shared through Dropbox or the like keeps each computer's records apart, and
    tells computers sharing such a folder apart (sharedfolder.py)."""
    p = config_dir() / "computer-id"
    try:
        cid = p.read_text().strip()
        if len(cid) == 16 and all(c in "0123456789abcdef" for c in cid):
            return cid
    except OSError:
        pass
    import secrets
    cid = secrets.token_hex(8)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(cid)
    os.replace(tmp, p)
    return cid


def _file() -> Path:
    return config_dir() / "folders.json"


# the last read of the list, as the app reads it every second: (path, date, size, inode), folders
_cache: tuple[tuple, list[Path]] | None = None


def folders() -> list[Path]:
    global _cache
    f = _file()
    try:
        st = f.stat()
        key = (f, st.st_mtime_ns, st.st_size, st.st_ino)  # each write replaces the file: a new inode
        if _cache is None or _cache[0] != key:
            data = json.loads(f.read_text())
            _cache = (key, [Path(p) for p in data.get("folders", []) if isinstance(p, str)] if isinstance(data, dict) else [])
    except (OSError, ValueError):
        return []
    return list(_cache[1])


def _write(paths: list[Path]) -> None:
    global _cache
    _cache = None
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"folders": [str(p) for p in paths]}, indent=2))
    tmp.replace(f)


def add(d: Path) -> bool:
    """Add a folder (absolute path). False if it was already there."""
    d = d.resolve()
    have = folders()
    if d in have:
        return False
    _write([*have, d])
    return True


def remove(d: Path) -> bool:
    d = d.resolve()
    have = folders()
    if d not in have:
        return False
    _write([p for p in have if p != d])
    return True
