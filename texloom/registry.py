"""The list of folders this computer keeps in sync (`texloom start` syncs them all).

Only paths are kept here; each folder's own settings and project link stay in its
`.texloom/config.json`.
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


def _file() -> Path:
    return config_dir() / "folders.json"


def folders() -> list[Path]:
    try:
        data = json.loads(_file().read_text())
    except (OSError, ValueError):
        return []
    return [Path(p) for p in data.get("folders", []) if isinstance(p, str)]


def _write(paths: list[Path]) -> None:
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
