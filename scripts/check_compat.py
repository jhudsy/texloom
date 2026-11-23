#!/usr/bin/env python3
"""Lists the code kept for older apps, servers and data (issue 27), and checks its markers.

Each place is marked with a comment just above it:

    # compat(KIND VERSION): what it handles; for data, whether it is converted when read
    // compat(KIND VERSION): … (in TypeScript and JavaScript)

KIND says what has to happen before the code can go:
  app     apps older than VERSION: removable once every server's --min-version is VERSION or later
          (apps only reach each other through a server, so this covers what they send each other too)
  server  servers older than VERSION: removable once every server people use runs VERSION or later
  data    data written before VERSION (in projects, links, lists or a computer's own files): an app
          or server that reads it should convert it ("converted on read: …"), or say why it can't
          ("not converted: …"). Removable only once what is still unconverted no longer matters;
          see "Code kept for older apps, servers and data" in DESIGN.md.

Run it to see them all; `--min-version X.Y.Z` and `--servers X.Y.Z` also say which app and server
markers could go. It fails if a marker is malformed, names a version after this one, or (for data)
doesn't say whether it is converted.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from texloom import __version__  # noqa: E402

PLACES = ("texloom", "server", "web/src", "editors/vscode")
SUFFIXES = {".py", ".ts", ".js"}
SKIP = ("node_modules", "locales", "dist", "out")
LOOSE = re.compile(r"(?:#|//)\s*compat\b")
MARK = re.compile(r"(?:#|//) compat\((app|server|data) (\d+\.\d+\.\d+)\): (\S.*)$")
CONVERTED = re.compile(r"\b(converted on read|converted as|not converted)\b")


def vt(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def files():
    for place in PLACES:
        for p in sorted((ROOT / place).rglob("*")):
            if p.suffix in SUFFIXES and p.is_file() and not any(s in p.parts for s in SKIP):
                yield p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-version", metavar="X.Y.Z", help="the lowest --min-version of the servers people use")
    ap.add_argument("--servers", metavar="X.Y.Z", help="the oldest server version people use")
    a = ap.parse_args()
    marks, problems = [], []
    for p in files():
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if not LOOSE.search(line):
                continue
            where = f"{p.relative_to(ROOT)}:{n}"
            m = MARK.search(line)
            if not m:
                problems.append(f"{where}: not a marker: compat(app|server|data X.Y.Z): …")
                continue
            kind, version, text = m.groups()
            if vt(version) > vt(__version__):
                problems.append(f"{where}: {version} is after this version ({__version__})")
            if kind == "data" and not CONVERTED.search(text):
                problems.append(f"{where}: a data marker says whether it is converted (\"converted on read: …\" or \"not converted: …\")")
            marks.append((kind, vt(version), version, where, text))

    for kind, title in (("app", "older apps"), ("server", "older servers"), ("data", "older data")):
        group = sorted(m for m in marks if m[0] == kind)
        if not group:
            continue
        print(f"\n{title} ({len(group)})")
        for _, t, version, where, text in group:
            can = (kind == "app" and a.min_version and t <= vt(a.min_version)) or \
                  (kind == "server" and a.servers and t <= vt(a.servers))
            print(f"  {'REMOVABLE ' if can else ''}{version:8} {where}\n           {text}")
    if problems:
        print("\nproblems:", *problems, sep="\n  ")
        return 1
    print(f"\ncompat markers: {len(marks)}, all well formed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
