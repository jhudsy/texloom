"""Checks of what must always hold, run only when TEXLOOM_CHECKS=1 (the end-to-end checks set it).

A broken one is logged as "INVARIANT BROKEN: …", which scripts/check_synconly.py looks for in every
computer's log. They read files back from disk, which is why they don't run for real users.
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("texloom")
LONE = re.compile(r"[\ud800-\udfff]")  # half a character (only possible from a decoding with surrogates)


def checking() -> bool:
    return os.environ.get("TEXLOOM_CHECKS") == "1"


def broken(what: str, detail: object = "") -> None:
    log.error("INVARIANT BROKEN: %s %s", what, str(detail)[:1500])


def well_formed(where: str, s: str) -> None:
    """Text the project may hold: no "\\r" (line breaks are "\\n" only) and no half characters."""
    if "\r" in s:
        i = s.index("\r")
        broken(f"{where} has \\r", repr(s[max(0, i - 40):i + 40]))
    if m := LONE.search(s):
        broken(f"{where} has half a character", repr(s[max(0, m.start() - 20):m.start() + 20]))


def first_difference(a: str, b: str) -> str:
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    return f"at {i}: {a[max(0, i - 30):i + 30]!r} vs {b[max(0, i - 30):i + 30]!r}"
