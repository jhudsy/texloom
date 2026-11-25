#!/usr/bin/env python3
"""Turn this machine's PostgreSQL into a standby of another: for the old primary after a failover
(STANDBY.md, "Afterwards: rebuild the old primary"). Run as root on the machine to be rebuilt:

    sudo /var/www/texloom/deploy/rebuild-standby.py NEW-PRIMARY-HOST          # Debian/Ubuntu's one cluster
    sudo /var/www/texloom/deploy/rebuild-standby.py NEW-PRIMARY-HOST --cluster 17/main

It stops this machine's texloom server and PostgreSQL, moves the data directory aside (it keeps it,
as DIR.old-DATE, until you delete it), takes a streaming base backup of NEW-PRIMARY-HOST as the
replication role, starts PostgreSQL as its standby, and waits until it is streaming. If failover is on
(`failover = true` in the settings), it then starts the texloom server again, which waits as the standby;
without failover a server can't run on a standby database (it says so and stops), so the server is left
stopped (`--start-service` starts it anyway). The replication password is PGPASSWORD in the environment,
or the postgres user's ~/.pgpass.

It refuses, changing nothing, when
- this machine's texloom server answers 200 on /api/health: it is the primary, and this would destroy
  the live database;
- NEW-PRIMARY-HOST isn't a copy of the same cluster (a different "system identifier": a wrong host);
- it isn't told to go ahead (type the host's name, or --yes).
If the base backup fails, the old data directory is put back.

Other layouts: --data DIR (a data directory, with --bin DIR for PostgreSQL's programs, --os-user, and
--start-options / --local-conninfo as the checks use them: see scripts/check_rebuild_standby.py).
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def die(msg: str, code: int = 1) -> None:
    print(f"rebuild-standby: {msg}", file=sys.stderr)
    sys.exit(code)


def say(msg: str) -> None:
    print(f"rebuild-standby: {msg}", flush=True)


class Pg:
    """The local PostgreSQL: where it is, and how to run its programs as the right user."""

    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.bin = Path(a.bin) if a.bin else None
        self.os_user = a.os_user
        self.cluster = None
        if a.data:
            self.data = Path(a.data)
            self.port = None
        else:
            self.cluster = self._debian_cluster(a.cluster)
            self.data = Path(self.cluster["data"])
            self.port = self.cluster["port"]

    @staticmethod
    def _debian_cluster(want: str | None) -> dict:
        if not shutil.which("pg_lsclusters"):
            die("no pg_lsclusters: this isn't Debian or Ubuntu's PostgreSQL. Use --data DIR (and --bin DIR)")
        rows = subprocess.run(["pg_lsclusters", "-h"], capture_output=True, text=True, check=False).stdout.splitlines()
        clusters = [dict(zip(("version", "name", "port", "status", "owner", "data", "log"), r.split(None, 6))) for r in rows if r.strip()]
        if want:
            clusters = [c for c in clusters if f"{c['version']}/{c['name']}" == want]
        if len(clusters) != 1:
            names = ", ".join(f"{c['version']}/{c['name']}" for c in clusters) or "none"
            die(f"which PostgreSQL cluster? found: {names}. Name one with --cluster VERSION/NAME")
        return clusters[0]

    def prog(self, name: str) -> str:
        return str(self.bin / name) if self.bin else name

    def run(self, *cmd: str, env: dict[str, str] | None = None, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
        """A command as the PostgreSQL user (when root, or it is not this user already)."""
        full = list(cmd)
        e = {**os.environ, **(env or {})}
        if os.geteuid() == 0 and self.os_user != "root":
            full = ["runuser", "-u", self.os_user, "--", "env", *(f"{k}={v}" for k, v in (env or {}).items()), *cmd]
        p = subprocess.run(full, capture_output=capture, text=True, env=e, check=False)
        if check and p.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:2])}: {(p.stderr or p.stdout).strip()[-600:]}")
        return p

    def stop(self) -> None:
        if self.cluster:
            subprocess.run(["pg_ctlcluster", self.cluster["version"], self.cluster["name"], "stop", "-m", "fast"], check=False,
                           capture_output=True)
        else:
            self.run(self.prog("pg_ctl"), "-D", str(self.data), "-m", "fast", "-w", "stop", check=False)

    def start(self) -> None:
        if self.cluster:
            p = subprocess.run(["pg_ctlcluster", self.cluster["version"], self.cluster["name"], "start"], capture_output=True, text=True, check=False)
            if p.returncode != 0:
                raise RuntimeError(f"pg_ctlcluster start: {p.stderr.strip()[-400:]}")
        else:
            log = self.data / "rebuild-start.log"
            args = [self.prog("pg_ctl"), "-D", str(self.data), "-w", "-t", "60", "-l", str(log)]
            if self.a.start_options:
                args += ["-o", self.a.start_options]
            try:
                self.run(*args, "start")
            except RuntimeError as e:
                tail = log.read_text(errors="replace").strip().splitlines()[-8:] if log.exists() else []
                raise RuntimeError(f"{e}\n  PostgreSQL's log:\n    " + "\n    ".join(tail)) from None

    def conninfo(self) -> list[str]:
        if self.a.local_conninfo:
            return [self.a.local_conninfo]
        return [f"port={self.port} dbname=postgres"]

    def system_id(self) -> str:
        out = self.run(self.prog("pg_controldata"), str(self.data)).stdout
        m = re.search(r"Database system identifier:\s*(\d+)", out)
        if not m:
            raise RuntimeError("pg_controldata didn't say the system identifier")
        return m.group(1)

    def sql(self, query: str) -> str:
        return self.run(self.prog("psql"), *self.conninfo(), "-tAX", "-c", query).stdout.strip()


def remote_system_id(pg: Pg, host: str, port: int, user: str, env: dict[str, str]) -> str:
    """The other cluster's system identifier, by the replication protocol (the role has no other way in)."""
    p = pg.run(pg.prog("psql"), f"host={host} port={port} user={user} replication=true dbname=postgres", "-tA", "-c", "IDENTIFY_SYSTEM",
               env=env, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"can't ask {host}:{port} as {user}: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout.strip().split("|")[0]


def failover_on() -> bool:
    """Is automatic failover on for this machine's server (the settings file, or TEXLOOM_FAILOVER)?"""
    on = re.compile(r"^\s*(failover\s*=\s*true|TEXLOOM_FAILOVER=(1|true|yes|on))\s*$", re.I | re.M)
    if os.environ.get("TEXLOOM_FAILOVER", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    for f in ("/etc/texloom/texloom.toml", "/etc/texloom/env"):
        try:
            if on.search(Path(f).read_text()):
                return True
        except OSError:
            pass
    return False


def is_primary_here(url: str) -> bool:
    try:
        return urllib.request.urlopen(url, timeout=3).status == 200
    except urllib.error.HTTPError:
        return False
    except OSError:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", help="the new primary's PostgreSQL")
    ap.add_argument("--port", type=int, default=5432)
    ap.add_argument("--user", default="replicator", help="the replication role (default: replicator)")
    ap.add_argument("--slot", default="standby", help="the replication slot on the new primary (made if it isn't there)")
    ap.add_argument("--cluster", metavar="VERSION/NAME", help="Debian/Ubuntu's cluster, if there is more than one")
    ap.add_argument("--data", help="a data directory, instead of a Debian cluster")
    ap.add_argument("--bin", help="PostgreSQL's programs, if not on PATH")
    ap.add_argument("--os-user", default="postgres", help="the user PostgreSQL runs as (default: postgres)")
    ap.add_argument("--start-options", help="(with --data) options for pg_ctl start -o")
    ap.add_argument("--local-conninfo", help="how psql reaches this PostgreSQL (default: its Debian port, over the socket)")
    ap.add_argument("--health-url", default="http://127.0.0.1:8787/api/health", help="this machine's texloom server")
    ap.add_argument("--no-service", action="store_true", help="don't stop and start the texloom service")
    ap.add_argument("--start-service", action="store_true",
                    help="start the texloom service afterwards even if failover is off (it then can't run on a standby database)")
    ap.add_argument("--delete-old", action="store_true", help="delete the old data directory afterwards instead of keeping it")
    ap.add_argument("--yes", action="store_true", help="don't ask")
    a = ap.parse_args()

    pg = Pg(a)
    env = {} if not os.environ.get("PGPASSWORD") else {"PGPASSWORD": os.environ["PGPASSWORD"]}

    if is_primary_here(a.health_url):
        die(f"this machine's texloom server ({a.health_url}) answers 200: it is the primary, and this would destroy the "
            "live database. Nothing was changed.")
    if not (pg.data / "PG_VERSION").exists():
        die(f"{pg.data} isn't a PostgreSQL data directory")
    try:
        mine = pg.system_id()
        theirs = remote_system_id(pg, a.host, a.port, a.user, env)
    except RuntimeError as e:
        die(f"{e}. Nothing was changed.")
    if mine != theirs:
        die(f"{a.host}:{a.port} is a different cluster (system identifier {theirs}, this one's {mine}): a standby must be a copy of "
            "the same one. Wrong host? Nothing was changed.")

    size = sum(f.stat().st_size for f in pg.data.rglob("*") if f.is_file()) / 2**20
    say(f"this will replace the PostgreSQL data in {pg.data} ({size:.0f} MB) with a copy of {a.host}:{a.port}, and make it its standby")
    if not a.yes:
        if not sys.stdin.isatty():
            die("not told to go ahead (--yes), and there is no one to ask. Nothing was changed.")
        if input(f"type {a.host} to go ahead: ").strip() != a.host:
            die("not confirmed. Nothing was changed.")

    if not a.no_service:
        say("stopping the texloom server")
        subprocess.run(["systemctl", "stop", "texloom"], check=False)
    say("stopping PostgreSQL")
    pg.stop()

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    old = pg.data.with_name(f"{pg.data.name}.old-{stamp}")
    os.rename(pg.data, old)
    try:
        say(f"taking a base backup of {a.host}:{a.port} (this takes as long as the database is big)")
        base = [pg.prog("pg_basebackup"), "-h", a.host, "-p", str(a.port), "-U", a.user, "-D", str(pg.data), "-R", "-X", "stream",
                "--checkpoint=fast", "-S", a.slot]
        p = pg.run(*base, "-C", env=env, check=False)
        if p.returncode != 0 and "already exists" in (p.stderr or ""):
            shutil.rmtree(pg.data, ignore_errors=True)
            p = pg.run(*base, env=env, check=False)  # the slot is there already: use it
        if p.returncode != 0:
            raise RuntimeError(f"pg_basebackup: {(p.stderr or p.stdout).strip()[-500:]}")
        say("starting PostgreSQL as a standby")
        pg.start()
        end = time.monotonic() + 90
        state = ""
        while time.monotonic() < end:
            try:
                recovering = pg.sql("SELECT pg_is_in_recovery()") == "t"
                state = pg.sql("SELECT status FROM pg_stat_wal_receiver") if recovering else "not in recovery"
            except RuntimeError:
                state = "not up yet"
            if state == "streaming":
                break
            time.sleep(1)
        if state != "streaming":
            raise RuntimeError(f"it started but isn't streaming from {a.host} (state: {state}): see PostgreSQL's log")
    except Exception as e:
        shutil.rmtree(pg.data, ignore_errors=True)
        os.rename(old, pg.data)
        pg.stop()
        say(f"FAILED: {e}")
        say(f"the old data directory is back where it was ({pg.data}), PostgreSQL is stopped and the texloom server was left stopped")
        sys.exit(1)

    say(f"PostgreSQL here is now a standby of {a.host}:{a.port}, streaming")
    if a.delete_old:
        shutil.rmtree(old, ignore_errors=True)
        say("deleted the old data directory")
    else:
        say(f"the old data directory is kept as {old}: delete it when you are sure (it has the changes this one had that the new "
            "primary didn't; the apps send those again)")
    if not a.no_service:
        if a.start_service or failover_on():
            say("starting the texloom server (it waits as the standby)")
            subprocess.run(["systemctl", "start", "texloom"], check=False)
        else:
            say("the texloom server is left stopped: failover is off, and without it a server can't run on a standby "
                "database. Promote this PostgreSQL when you need it (STANDBY.md), then start it.")


if __name__ == "__main__":
    main()
