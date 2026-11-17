#!/usr/bin/env python3
"""Checks texloom/packing.py against scripts/packing-vectors.json, the cases web/scripts/check-packing.mjs
checks web/src/packing.ts against: compressed updates made by either app are read, and anything
that isn't one (cut short, corrupted, something after it) is left as it is.

    uv run python scripts/check_packing.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pycrdt import Doc, Text

from texloom import packing

bad: list[str] = []
v = json.loads((ROOT / "scripts/packing-vectors.json").read_text())
for c in v["cases"]:
    got = packing.unpack(bytes.fromhex(c["data"]))
    if got.hex() != c["update"]:
        bad.append(f"{c['what']}: got {len(got)} bytes, want {len(c['update']) // 2}")
# the update the packed cases hold is a real document's
d = Doc()
d.apply_update(packing.unpack(bytes.fromhex(v["cases"][2]["data"])))
if str(d.get("main.tex", type=Text)) != v["text"]:
    bad.append("the update packed by the web app doesn't give the document back")
for n in (0, 1, 1000, 3_000_000):
    u = os.urandom(n // 2) + bytes(n - n // 2)
    if packing.unpack(packing.pack(u)) != u:
        bad.append(f"pack then unpack changes {n} bytes")
old = packing.MAX_UNPACKED
packing.MAX_UNPACKED = 1000
try:
    big = packing.pack(bytes(5000))
    if packing.unpack(big) != big:
        bad.append("an update larger than the limit is decompressed (a decompression bomb would be)")
finally:
    packing.MAX_UNPACKED = old
print("\n".join(bad) or f"packing: all {len(v['cases'])} cases and the round trips agree")
sys.exit(1 if bad else 0)
