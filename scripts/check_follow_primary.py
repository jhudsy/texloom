#!/usr/bin/env python3
"""deploy/follow-primary.sh (INSTALL.md, "Moving traffic to the primary"): it runs a command while
this machine's server is the primary, as /api/health says, and not otherwise.

Against a stand-in health endpoint that can be flipped between 200 (the primary) and 503 (a
standby, or a server that stopped itself), with short timings:
- nothing runs while it says 503, or while the server isn't there at all;
- it runs once the server has said 200 CONFIRM times in a row, and not before;
- it runs again after REFRESH seconds while that goes on (a DNS record would be kept right);
- it stops when the answer goes back to 503, and runs again, at once, when it is 200 again;
- a command that fails is tried again every RETRY seconds, and the failure is said;
- the command's environment is the script's (keys go in the settings file, not on a command line).

    uv run python scripts/check_follow_primary.py
"""

import http.server
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import ROOT, check, summary  # noqa: E402

SCRIPT = ROOT / "deploy" / "follow-primary.sh"


class Health(http.server.ThreadingHTTPServer):
    status = 503


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(self.server.status)  # type: ignore[attr-defined]
        self.end_headers()

    def log_message(self, *args):
        pass


def runs(f: Path) -> int:
    return len(f.read_text().splitlines()) if f.exists() else 0


def sleep(s: float) -> None:
    time.sleep(s)


def main() -> int:
    server = Health(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="texloom-follow-") as t:
        tmp = Path(t)
        log = tmp / "ran.txt"
        env = {**os.environ, "HEALTH_URL": f"http://127.0.0.1:{port}/api/health", "INTERVAL": "0.2", "CONFIRM": "3",
               "REFRESH": "3", "RETRY": "1", "KEY": "secret-from-the-environment",
               "ON_PRIMARY": f'echo "$KEY" >> {log}'}
        proc = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            server.status = 503
            sleep(1.5)
            check("nothing runs while the server says it is not the primary", runs(log) == 0, runs(log))

            server.status = 200
            sleep(0.3)
            check("... nor before it has said so CONFIRM times in a row", runs(log) == 0, runs(log))
            sleep(1.2)
            check("it runs when it has", runs(log) == 1, runs(log))
            check("with the script's environment", log.read_text().strip() == "secret-from-the-environment")
            sleep(1.0)
            check("and not again at once", runs(log) == 1, runs(log))
            sleep(3.0)
            check("it runs again after REFRESH while it stays the primary", runs(log) == 2, runs(log))

            server.status = 503
            sleep(0.5)
            before = runs(log)
            sleep(4.0)
            check("it stops when the server is not the primary any more", runs(log) == before, (before, runs(log)))
            server.status = 200
            sleep(1.5)
            check("and runs again, at once, when it is", runs(log) == before + 1, (before, runs(log)))

            # no server at all is not the primary either
            server.shutdown()
            server.server_close()
            before = runs(log)
            sleep(4.0)
            check("a server that isn't there is not the primary", runs(log) == before, (before, runs(log)))
        finally:
            proc.terminate()
            out = proc.communicate(timeout=10)[0]
        check("it says what it is doing", "this server is the primary" in out and "not the primary any more" in out, out[-400:])

        # a failing command is tried again every RETRY seconds
        server = Health(("127.0.0.1", port), Handler)
        server.status = 200
        threading.Thread(target=server.serve_forever, daemon=True).start()
        fails = tmp / "fails.txt"
        env["ON_PRIMARY"] = f"echo x >> {fails}; exit 3"
        proc = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            sleep(4.2)
        finally:
            proc.terminate()
            out = proc.communicate(timeout=10)[0]
            server.shutdown()
        check("a command that fails is tried again every RETRY seconds", 3 <= runs(fails) <= 5, runs(fails))
        check("and the failure is said, with its status", "it failed (status 3)" in out, out[-300:])

        env.pop("ON_PRIMARY")
        r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=10, check=False)
        check("without ON_PRIMARY it refuses to start, saying so", r.returncode != 0 and "ON_PRIMARY" in r.stderr, r.stderr)
    return summary("follow the primary")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
