#!/usr/bin/env python3
"""deploy/rebuild-standby.py (INSTALL.md, "A standby, switched by hand"): turning the old primary
into a standby of the new one, with throwaway PostgreSQL clusters (scripts/pg.py).

The case: a primary P with a streaming standby S; the link is cut, P takes a change S never gets
("diverged"), P is lost, S is promoted and takes another ("two"). Then P is rebuilt from S. It must
refuse, changing nothing, when this machine's server is the primary, when the host is another
cluster, when it isn't told to go ahead, and when the host can't be reached; put its data back when
the base backup fails; and, when it goes ahead, leave P a standby that is streaming from S, with S's
history (not "diverged"), its old data kept aside, and following S's later changes.

    uv run python scripts/check_rebuild_standby.py
"""

import getpass
import http.server
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pg  # noqa: E402
from checklib import ROOT, check, summary  # noqa: E402

SCRIPT = ROOT / "deploy" / "rebuild-standby.py"
USER = pg.USER


def sql(port: int, query: str) -> str:
    p = subprocess.run([str(pg.bindir() / "psql"), "-h", "127.0.0.1", "-p", str(port), "-U", USER, "-d", "postgres", "-tAX", "-c", query],
                       capture_output=True, text=True, check=False)
    return p.stdout.strip() if p.returncode == 0 else f"ERROR {p.stderr.strip()[-200:]}"


def waiting(cond, secs: float = 30) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.3)
    return False


class Health(http.server.ThreadingHTTPServer):
    status = 200


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(self.server.status)  # type: ignore[attr-defined]
        self.end_headers()

    def log_message(self, *args):
        pass


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="texloom-rebuild-") as t:
        tmp = Path(t)
        p_dir, s_dir, q_dir = tmp / "p", tmp / "s", tmp / "q"
        _, port_p = pg.start(p_dir)
        _, port_q = pg.start(q_dir)  # an unrelated cluster
        port_s = 0
        health = Health(("127.0.0.1", 0), Handler)
        threading.Thread(target=health.serve_forever, daemon=True).start()
        dead_url = "http://127.0.0.1:1/api/health"  # (nothing there: this machine's server isn't the primary)
        try:
            sql(port_p, "CREATE TABLE t (v text)")
            sql(port_p, "INSERT INTO t VALUES ('one')")
            pg.standby_of(s_dir, port_p)
            _, port_s = pg.start(s_dir)
            check("the standby has the first change", waiting(lambda: sql(port_s, "SELECT count(*) FROM t") == "1"))

            # the link is cut: P takes a change S never gets, and is lost; S is promoted and takes another
            pg.stop(s_dir)
            sql(port_p, "INSERT INTO t VALUES ('diverged')")
            pg.stop(p_dir, "immediate")
            pg.start(s_dir, port_s)
            pg.promote(s_dir)
            sql(port_s, "INSERT INTO t VALUES ('two')")

            def helper(*extra: str, host_port: int | None = None, health_url: str = dead_url, stdin=subprocess.DEVNULL) -> subprocess.CompletedProcess:
                args = [sys.executable, str(SCRIPT), "127.0.0.1", "--port", str(host_port or port_s), "--user", USER, "--data", str(p_dir),
                        "--bin", str(pg.bindir()), "--os-user", getpass.getuser(), "--no-service", "--health-url", health_url,
                        "--start-options", f"-p {port_p} -c listen_addresses=127.0.0.1 -c unix_socket_directories= -c fsync=off",
                        "--local-conninfo", f"host=127.0.0.1 port={port_p} user={USER} dbname=postgres", *extra]
                # (a locale PostgreSQL can start with, as scripts/pg.py gives its own: macOS wants one set)
                env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
                return subprocess.run(args, capture_output=True, text=True, timeout=180, stdin=stdin, env=env, check=False)

            def untouched() -> bool:
                return (p_dir / "PG_VERSION").exists() and not list(tmp.glob("p.old-*"))

            r = helper("--yes", health_url=f"http://127.0.0.1:{health.server_address[1]}/api/health")
            check("it refuses when this machine's server is the primary (answers 200), and changes nothing",
                  r.returncode != 0 and "answers 200" in r.stderr and untouched(), r.stderr[-300:])
            r = helper("--yes", host_port=port_q)
            check("it refuses a host that is another cluster, and changes nothing",
                  r.returncode != 0 and "different cluster" in r.stderr and untouched(), r.stderr[-300:])
            r = helper("--yes", host_port=1)
            check("it refuses a host it can't ask, and changes nothing", r.returncode != 0 and "can't ask" in r.stderr and untouched(), r.stderr[-300:])
            r = helper()
            check("it won't go ahead unasked, and changes nothing",
                  r.returncode != 0 and "not told to go ahead" in r.stderr and untouched(), r.stderr[-300:])

            r = helper("--yes", "--slot", "bad slot name")
            check("a base backup that fails puts the old data back",
                  r.returncode != 0 and "FAILED" in r.stdout and untouched(), (r.stdout + r.stderr)[-400:])

            r = helper("--yes", "--slot", "rebuilt")
            check("it goes ahead when told to", r.returncode == 0 and "now a standby" in r.stdout, (r.stdout + r.stderr)[-500:])
            check("the old primary is a standby", sql(port_p, "SELECT pg_is_in_recovery()") == "t")
            check("streaming from the new primary", sql(port_p, "SELECT status FROM pg_stat_wal_receiver") == "streaming")
            rows = sql(port_p, "SELECT string_agg(v, ',' ORDER BY v) FROM t")
            check("with the new primary's history, and not the change only it had", rows == "one,two", rows)
            kept = list(tmp.glob("p.old-*"))
            check("its old data is kept aside", len(kept) == 1 and (kept[0] / "PG_VERSION").exists(), kept)
            sql(port_s, "INSERT INTO t VALUES ('three')")
            check("and it follows the new primary's later changes",
                  waiting(lambda: sql(port_p, "SELECT count(*) FROM t WHERE v='three'") == "1"))
        finally:
            health.shutdown()
            pg.stop(p_dir)
            pg.stop(s_dir)
            pg.stop(q_dir)
    return summary("rebuild standby")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
