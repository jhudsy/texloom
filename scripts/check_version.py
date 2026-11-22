#!/usr/bin/env python3
"""Checks that the desktop app's version is the same in its three places: pyproject.toml,
texloom/__init__.py and uv.lock. A release changes all three (scripts/release.py); one left behind
gives an app that reports the wrong version.

    uv run python scripts/check_version.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PLACES = {
    "pyproject.toml": (ROOT / "pyproject.toml", re.compile(r'^\[project\]\s*\nname = "texloom"\s*\nversion = "([^"]+)"', re.MULTILINE)),
    "texloom/__init__.py": (ROOT / "texloom" / "__init__.py", re.compile(r'^__version__ = "([^"]+)"', re.MULTILINE)),
    "uv.lock": (ROOT / "uv.lock", re.compile(r'^\[\[package\]\]\s*\nname = "texloom"\s*\nversion = "([^"]+)"', re.MULTILINE)),
}


def versions() -> dict[str, str | None]:
    """The version each place holds (None where it can't be found)."""
    out: dict[str, str | None] = {}
    for name, (path, pattern) in PLACES.items():
        m = pattern.search(path.read_text("utf-8"))
        out[name] = m.group(1) if m else None
    return out


def main() -> int:
    found = versions()
    missing = [name for name, v in found.items() if v is None]
    if missing:
        print(f"version: not found in {', '.join(missing)}")
        return 1
    if len(set(found.values())) != 1:
        print("version: the three places disagree:")
        for name, v in found.items():
            print(f"  {name}: {v}")
        return 1
    print(f"version: {next(iter(found.values()))} in all three places")
    return 0


if __name__ == "__main__":
    sys.exit(main())
