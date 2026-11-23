#!/usr/bin/env python3
"""Checks texloom/folderrules.py's rules (and folder.py's settle_for) against scripts/folder-rules.json, the cases
web/scripts/check-folder-rules.mjs checks web/src/foldersync.ts against: which files are the
project's, which are text, merging a file saved while the project changed, how long a changed
file settles, Windows line endings, and renames.

    uv run python scripts/check_folder_rules.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from texloom.folder import settle_for
from texloom.folderrules import crlf, is_text, merge3, on_disk, pick_moves, synced_file

v = json.loads((ROOT / "scripts/folder-rules.json").read_text())
bad: list[str] = []


def check(kind: str, what: str, got: object, want: object) -> None:
    if got != want:
        bad.append(f"{kind}: {what}: got {json.dumps(got, ensure_ascii=False)}, want {json.dumps(want, ensure_ascii=False)}")


for c in v["synced"]:
    check("synced", f"{c['path']} (main {c['main']})", synced_file(c["path"], c["main"]), c["want"])
for c in v["text"]:
    data = b"a" * c["size"] if "size" in c else bytes.fromhex(c["hex"])
    check("text", c["path"], is_text(c["path"], data), c["want"])
for c in v["merge"]:
    check("merge", c["what"], merge3(c["base"], c["theirs"], c["ours"]), c["want"])
for c in v["settle"]:
    check("settle", f"{c['size']} bytes, {c['times']} times", round(settle_for(c["size"], c["times"]), 6), c["want"])
for c in v["crlf"]:
    check("crlf", c["hex"], crlf(bytes.fromhex(c["hex"])), c["want"])
for c in v["onDisk"]:
    check("onDisk", f"{c['text']!r} windows={c['windows']}", on_disk(c["text"], c["windows"]).hex(), c["want"])
for c in v["moves"]:
    check("moves", c["what"], pick_moves(c["gone"], c["new"]), c["want"])

n = sum(len(x) for x in v.values() if isinstance(x, list))
print("\n".join(bad) if bad else f"folder rules (desktop): all {n} cases agree")
sys.exit(1 if bad else 0)
