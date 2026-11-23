#!/usr/bin/env python3
"""Checks texloom.example.toml against the server's options (server/config.py, server/cli.py):

- it parses as it is, and with every commented-out setting uncommented, as someone would;
- every setting of texloom-server is in it, so a new one can't go undocumented;
- TEXLOOM_<NAME> overrides the file, and an unknown setting is refused by name.

    uv run --extra server python scripts/check_config.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import COMMANDS, parse
from server.cli import parser

EXAMPLE = ROOT / "texloom.example.toml"
failures: list[str] = []


def ok(cond: bool, what: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + what)
    if not cond:
        failures.append(what)


def load(text: str, env: dict[str, str] | None = None):
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(text)
    saved = {k: os.environ.get(k) for k in (env or {})}
    os.environ.update(env or {})
    try:
        return parse(parser(), ["--config", f.name])
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.unlink(f.name)


def main() -> None:
    text = EXAMPLE.read_text()
    a = load(text)
    ok(a.database.startswith("postgresql://") and a.min_free > 0, "the example parses as it is")
    everything = re.sub(r"(?m)^# ((?:\[s3\])|(?:[a-z_0-9]+ = .*))$", r"\1", text)
    b = load(everything)
    ok(b.files.startswith("s3://") and b.s3_endpoint and b.min_version and b.texlive_mirror,
       "and with every setting uncommented")
    named = set(re.findall(r"(?m)^#? ?([a-z_0-9]+) = ", text)) | {f"s3_{k}" for k in re.findall(r"(?m)^#? ?(endpoint|region|key_id|secret) = ", text)}
    options = {x.dest for x in parser()._actions if x.dest not in COMMANDS}
    missing = sorted(o for o in options - named if f"{o} = " not in text)  # (admin_token is in a comment)
    ok(not missing, f"every setting is in the example{f' (missing: {missing})' if missing else ''}")
    c = load(text, {"TEXLOOM_MIN_FREE": "5G"})
    ok(c.min_free == 5 << 30, "TEXLOOM_<NAME> overrides the file")
    err = StringIO()
    try:
        with redirect_stderr(err):
            load(text + "\nmin_fre = \"1G\"\n")
        refused = False
    except SystemExit:
        refused = "unknown setting 'min_fre'" in err.getvalue()
    ok(refused, "a misspelled setting is refused by name")
    print("settings: all checks passed" if not failures else f"settings: FAILED ({len(failures)})")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
