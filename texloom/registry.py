"""The list of folders this computer keeps in sync (`texloom start` syncs them all).

Only paths are kept here; each folder's own settings and project link stay in the folder, in
this computer's part of its `.texloom` (paths.state_dir).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .paths import CachedFile, write_json


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


def _read(f: Path) -> list[Path]:
    data = json.loads(f.read_text())
    return [Path(p) for p in data.get("folders", []) if isinstance(p, str)] if isinstance(data, dict) else []


_cache = CachedFile(_read)  # the app reads the list every second


def folders() -> list[Path]:
    try:
        return list(_cache.get(_file(), []))
    except (OSError, ValueError):
        return []


def _write(paths: list[Path]) -> None:
    _cache.forget()
    write_json(_file(), {"folders": [str(p) for p in paths]}, indent=2)


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
