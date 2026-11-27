#!/usr/bin/env python3
"""Model-checks two designs with TLA+:
- models/failover (issue 8): the bucket lease, a standby that takes over, and an app that was told
  "stored" by a server that was then replaced;
- models/relays (FUTURE_ARCHITECTURE.md): several relays sharing a database and a bus, relays dying,
  breaks in the bus and a database failover, and whether every change is kept and reaches everyone.

Each configuration (models/*/*.cfg) says what is expected: some hold, and some are expected to be
violated, which shows the model notices when the design is wrong (a part of it taken away). A
"violated" one failing to be violated, or the wrong property failing, is a failure here too.

TLC (the TLA+ model checker, tla2tools.jar, release v1.7.4 of github.com/tlaplus/tlaplus) is fetched
once into ~/.cache/texloom-tools (or TLA2TOOLS names a jar) and checked against the SHA-256 below
before it is ever run. Needs Java; without it this skips.

    uv run python scripts/check_model.py                    # all of them (several minutes)
    uv run python scripts/check_model.py failover/safe      # just those named (model/configuration)
    uv run python scripts/check_model.py relays             # one model's
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
MODELS = ROOT / "models"
URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"
SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
SIZE = 2274532

# The relays model with two relays and a database failover is too big to search exhaustively (hundreds of millions
# of states, tens of GB of state files): those configurations are checked by random simulation instead, this many
# runs of up to this many steps. Simulation finds every hole the "no-…" configurations open within seconds.
SIMULATE = {"relays/failover", "relays/together"}
TRACES, DEPTH = 100_000, 40
RELAYS_CASES = {
    "failover": None,
    "crash": None,
    "break": None,
    "together": None,
    "ack-first": None,
    "no-recheck": "NoLostAck",
    "no-store-again": "NoLostAck",
    "no-reset": "Converged",
    "no-timeline-on-bus": "Converged",
}

# model -> (its module, {configuration: the invariant expected to be violated, or None if everything should hold})
CASES = {
    "failover": ("Failover", {
        "safe": None,
        "freeze-heals": None,
        "freeze-writers": "SingleWriter",
        "short-wait": "SingleWriter",
        "no-hello": "NoLostAck",
    }),
    "relays": ("Relays", RELAYS_CASES),
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


def tlc(jar_: Path, module: Path, cfg: Path, simulate: bool = False) -> tuple[int, str, float]:
    with tempfile.TemporaryDirectory(prefix="texloom-tlc-") as meta:
        start = time.monotonic()
        how = ["-simulate", f"num={TRACES}", "-depth", str(DEPTH)] if simulate else []
        p = subprocess.run(["java", "-XX:+UseParallelGC", "-cp", str(jar_), "tlc2.TLC", "-config", str(cfg), "-deadlock",
                            "-workers", "auto", "-metadir", meta, *how, str(module)],
                           capture_output=True, text=True, check=False, timeout=1800)
        return p.returncode, p.stdout + p.stderr, time.monotonic() - start


def main() -> int:
    if not shutil.which("java"):
        print("skipped: no Java (the model checker needs it)")
        return 0
    every = [f"{m}/{c}" for m, (_, cfgs) in CASES.items() for c in cfgs]
    names = []
    for arg in sys.argv[1:] or every:
        picked = [n for n in every if n == arg or n.startswith(arg + "/")]
        if not picked:
            sys.exit(f"no such model or configuration: {arg} (have {', '.join(every)})")
        names += picked
    jar_ = jar()
    for name in names:
        model, cfg = name.split("/")
        module, cfgs = CASES[model]
        want = cfgs[cfg]
        simulated = name in SIMULATE
        code, out, secs = tlc(jar_, MODELS / model / f"{module}.tla", MODELS / model / f"{cfg}.cfg", simulated)
        states = re.findall(r"(\d[\d,]*) (?:distinct states found|states checked)", out)  # (the last is the total)
        seen = (f"{states[-1]} states{' in ' + format(TRACES, ',') + ' random runs' if simulated else ''}, {secs:.0f}s"
                if states else f"{secs:.0f}s")
        if want is None and simulated:  # (a simulation that ran its course ends with code 1, and says nothing more)
            check(f"{name}: holds ({seen})", "Error:" not in out and "Finished in" in out, out[-600:])
        elif want is None:
            check(f"{name}: holds ({seen})", code == 0 and "No error has been found" in out, out[-600:])
        else:
            violated = re.search(r"Invariant (\w+) is violated", out)
            check(f"{name}: {want} is violated, as it should be ({seen})",
                  code == 12 and violated is not None and violated.group(1) == want, out[-600:])
    return summary("models")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
