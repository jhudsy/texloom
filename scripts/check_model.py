#!/usr/bin/env python3
"""Model-checks the failover design (models/failover/Failover.tla, issue 8): the bucket lease, a
standby that takes over, and an app that was told "stored" by a server that was then replaced.

Each configuration (models/failover/*.cfg) says what is expected: some hold, and some are expected
to be violated, which shows the model notices when the design is wrong (timers set too short; the
app's hello check removed; a frozen server waking). A "violated" one failing to be violated, or the
wrong property failing, is a failure here too.

TLC (the TLA+ model checker, tla2tools.jar, release v1.7.4 of github.com/tlaplus/tlaplus) is fetched
once into ~/.cache/texloom-tools (or TLA2TOOLS names a jar) and checked against the SHA-256 below
before it is ever run. Needs Java; without it this skips.

    uv run python scripts/check_model.py            # all of them (a few minutes)
    uv run python scripts/check_model.py safe       # just those named
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import check, summary  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "models" / "failover"
URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"
SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
SIZE = 2274532

# configuration -> the invariant expected to be violated, or None if everything is expected to hold
CASES = {
    "safe": None,
    "freeze-heals": None,
    "freeze-writers": "SingleWriter",
    "short-wait": "SingleWriter",
    "no-hello": "NoLostAck",
}


def jar() -> Path:
    """tla2tools.jar, fetched if need be, and only returned once it is the one pinned above."""
    if os.environ.get("TLA2TOOLS"):
        return Path(os.environ["TLA2TOOLS"])
    path = Path.home() / ".cache" / "texloom-tools" / "tla-1.7.4" / "tla2tools.jar"

    def ok(p: Path) -> bool:
        return p.exists() and p.stat().st_size == SIZE and hashlib.sha256(p.read_bytes()).hexdigest() == SHA256

    if not ok(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"fetching {URL}", flush=True)
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t) / "tla2tools.jar"
            with urllib.request.urlopen(URL, timeout=120) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
            if not ok(tmp):
                sys.exit("tla2tools.jar isn't the pinned release (size or SHA-256 differs): not running it")
            shutil.move(tmp, path)
    return path


def tlc(jar_: Path, cfg: Path) -> tuple[int, str, float]:
    with tempfile.TemporaryDirectory(prefix="texloom-tlc-") as meta:
        start = time.monotonic()
        p = subprocess.run(["java", "-XX:+UseParallelGC", "-cp", str(jar_), "tlc2.TLC", "-config", str(cfg), "-deadlock",
                            "-workers", "auto", "-metadir", meta, str(MODEL / "Failover.tla")],
                           capture_output=True, text=True, check=False, timeout=1800)
        return p.returncode, p.stdout + p.stderr, time.monotonic() - start


def main() -> int:
    if not shutil.which("java"):
        print("skipped: no Java (the model checker needs it)")
        return 0
    names = sys.argv[1:] or list(CASES)
    unknown = [n for n in names if n not in CASES]
    if unknown:
        sys.exit(f"no such configuration: {', '.join(unknown)} (have {', '.join(CASES)})")
    jar_ = jar()
    for name in names:
        want = CASES[name]
        code, out, secs = tlc(jar_, MODEL / f"{name}.cfg")
        states = re.findall(r"(\d[\d,]*) distinct states found", out)  # (the last is the total)
        seen = f"{states[-1]} states, {secs:.0f}s" if states else f"{secs:.0f}s"
        if want is None:
            check(f"{name}: holds ({seen})", code == 0 and "No error has been found" in out, out[-600:])
        else:
            violated = re.search(r"Invariant (\w+) is violated", out)
            check(f"{name}: {want} is violated, as it should be ({seen})",
                  code == 12 and violated is not None and violated.group(1) == want, out[-600:])
    return summary("failover model")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
