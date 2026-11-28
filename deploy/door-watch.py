#!/usr/bin/env python3
"""The front door's health watcher: keeps nginx sending to the relays that are well.

Open-source nginx notices a relay is down only when a request to it fails, and then tries it again soon after: an app
reconnecting may be sent straight back to a frozen relay (FUTURE_ARCHITECTURE.md, "The front door"). This asks every
relay's /api/health every EVERY seconds; one that fails twice in a row is marked `down` in nginx's upstream
(/etc/nginx/conf.d/texloom-relays.conf) and nginx reloaded (gracefully: open connections stay); one that answers
twice in a row is put back. If every relay fails, none is marked down (nginx then tries them all, which is the best
it can do). A relay refusing new connections answers 503 on /api/health, so it is taken out too.

The relays are listed, one address:port a line, in /etc/texloom/relays (deploy/cluster.py writes it). Run by
texloom-door-watch.service; needs nothing but Python.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RELAYS = Path(os.environ.get("TEXLOOM_RELAYS", "/etc/texloom/relays"))
UPSTREAM = Path(os.environ.get("TEXLOOM_UPSTREAM", "/etc/nginx/conf.d/texloom-relays.conf"))
EVERY = 2.0  # seconds between rounds
TIMEOUT = 1.5  # seconds a relay has to answer
TWICE = 2  # rounds in a row before a relay's state changes


def relays() -> list[str]:
    try:
        return [x.strip() for x in RELAYS.read_text().splitlines() if x.strip() and not x.startswith("#")]
    except OSError:
        return []


def well(address: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://{address}/api/health", timeout=TIMEOUT) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def render(listed: list[str], down: set[str]) -> str:
    if down >= set(listed):  # (every relay down: let nginx try them all)
        down = set()
    lines = "".join(f"    server {a}{' down' if a in down else ''};\n" for a in listed)
    return ("# Written by texloom-door-watch (deploy/door-watch.py) from /etc/texloom/relays: the relays nginx sends\n"
            "# TeXLoom's requests to, those that fail their health check marked down. Changes here are replaced.\n"
            f"upstream texloom_relays {{\n    least_conn;\n{lines}}}\n")


def apply(text: str) -> None:
    if UPSTREAM.exists() and UPSTREAM.read_text() == text:
        return
    old = UPSTREAM.read_text() if UPSTREAM.exists() else None
    UPSTREAM.write_text(text)
    if subprocess.run(["nginx", "-t", "-q"], capture_output=True).returncode != 0:
        print("nginx refused the new upstream: put back", file=sys.stderr, flush=True)
        if old is not None:
            UPSTREAM.write_text(old)
        return
    subprocess.run(["systemctl", "reload", "nginx"], check=False)


def main() -> None:
    down: set[str] = set()
    streak: dict[str, int] = {}  # address -> rounds in a row its answer differed from its state
    while True:
        listed = relays()
        if listed:
            for a in listed:
                ok = well(a)
                if ok == (a not in down):
                    streak[a] = 0
                    continue
                streak[a] = streak.get(a, 0) + 1
                if streak[a] >= TWICE:
                    streak[a] = 0
                    (down.discard if ok else down.add)(a)
                    print(f"{a}: {'well again' if ok else 'not well'}", flush=True)
            down &= set(listed)
            apply(render(listed, down))
        time.sleep(EVERY)


if __name__ == "__main__":
    main()
