#!/usr/bin/env python3
"""Check texloom/xr.py against scripts/xr-vectors.json, the cases that web/scripts/check-xr.mjs
checks web/src/xr.ts against: which documents a compile builds first for \\externaldocument.

    python scripts/check_xr.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from texloom.xr import compile_order, external_documents  # noqa: E402

bad = []
for case in json.loads(Path(__file__).with_name("xr-vectors.json").read_text()):
    files = case["files"]
    got = compile_order(files, case["main"])
    if got != case["order"]:
        bad.append(f"{case['what']}: order {got}, want {case['order']}")
    for doc, want in case["externals"].items():
        got = [list(e) for e in external_documents(files, doc)]
        if got != want:
            bad.append(f"{case['what']}: {doc} reads {got}, want {want}")
print("\n".join(bad) or "xr: all cases agree")
sys.exit(1 if bad else 0)
