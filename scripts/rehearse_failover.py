#!/usr/bin/env python3
"""A rehearsal of failover with real parts, in Docker (needs Docker; by hand, not in CI: scripts/check_failover.py
is the quick check). SCALING.md, INSTALL.md "Letting the two servers switch over by themselves".

Two "machines" of containers on a private network, as INSTALL.md describes them: each has a relay
(built from this checkout, `--failover`), a PostgreSQL (the second a streaming standby of the first,
set up with the commands INSTALL.md gives, and a role that may only promote through the grant), and a
Caddy running deploy/Caddyfile.failover (both machines' Caddys in front of both relays). They share
a real S3-compatible store (RustFS) that does conditional writes, reached over a network of its own
for each relay, so one relay can be cut off from it alone. Apps (the desktop sync engine) connect
through the Caddys. Then things are broken:

  crash    machine A is killed (relay, PostgreSQL, Caddy), after a change the standby never got
  freeze   machine A is frozen (docker pause: nothing runs, as when a virtual machine is suspended
           but the clock goes on) for longer than the wait, then wakes
  cutoff   relay A can't reach the bucket (network cut), the rest is fine
  outage   the bucket itself goes away for a while, then comes back

    uv run --extra server python scripts/rehearse_failover.py            # all
    uv run --extra server python scripts/rehearse_failover.py crash      # just these
"""

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_restore import App, add_line, text  # noqa: E402
from checklib import ROOT, check, summary, until  # noqa: E402

from texloom.crypto import Keys  # noqa: E402

PG = "postgres:16.6"
S3 = "rustfs/rustfs:latest"
CADDY = "caddy:2"
TTL = 5  # seconds: the standby waits 2 x (5 + 2) = 14
WAIT = 2 * (TTL + 2)
S3_KEY, S3_SECRET = "rehearsal", "rehearsal-secret-123"
IMAGE = "texloom-rehearsal-relay:local"
ADMIN = "rehearsal-admin-token"

HEALTH = ("import urllib.request, urllib.error\n"
          "try:\n r = urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=2); print(r.status, r.read().decode())\n"
          "except urllib.error.HTTPError as e: print(e.code, e.read().decode())\n")


def docker(*args: str, check_: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)
    if check_ and p.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:4])}: {p.stderr.strip()[-400:]}")
    return p


def build_image() -> None:
    with tempfile.TemporaryDirectory(prefix="texloom-rehearsal-") as t:
        ctx = Path(t)
        for f in ("pyproject.toml", "README.md"):
            shutil.copy(ROOT / f, ctx / f)
        for d in ("server", "texloom"):
            shutil.copytree(ROOT / d, ctx / d, ignore=shutil.ignore_patterns("__pycache__"))
        (ctx / "Dockerfile").write_text(
            "FROM python:3.12-slim\nWORKDIR /src\nCOPY pyproject.toml README.md ./\nCOPY server ./server\nCOPY texloom ./texloom\n"
            'RUN pip install --no-cache-dir ".[server]" && useradd -r -d /data relay && mkdir /data && chown relay /data\n'
            'USER relay\nENTRYPOINT ["texloom-server"]\n')
        docker("build", "-q", "-t", IMAGE, str(ctx), timeout=600)


class Cluster:
    """The containers of one rehearsal, under a prefix of their own, all removed afterwards."""

    def __init__(self, tmp: Path):
        self.p = "tlr" + secrets.token_hex(2)
        self.tmp = tmp
        self.ports: dict[str, int] = {}

    def n(self, name: str) -> str:
        return f"{self.p}-{name}"

    # --- building it -----------------------------------------------------------------

    def _wait(self, what: str, cond, secs: float = 90) -> None:
        end = time.monotonic() + secs
        while time.monotonic() < end:
            try:
                if cond():
                    return
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
            time.sleep(0.5)
        raise RuntimeError(f"timed out waiting for {what}")

    def psql(self, c: str, sql: str, db: str = "postgres") -> str:
        return docker("exec", "-e", "PGPASSWORD=admin", c, "psql", "-h", "127.0.0.1", "-U", "postgres", "-d", db, "-tAX",
                      "-v", "ON_ERROR_STOP=1", "-c", sql).stdout.strip()

    def up(self) -> None:
        for net in ("net", "s3a", "s3b"):
            docker("network", "create", self.n(net))
        docker("run", "-d", "--name", self.n("s3"), "--network", self.n("s3a"), "-p", "127.0.0.1::9000",
               "-e", f"RUSTFS_ACCESS_KEY={S3_KEY}", "-e", f"RUSTFS_SECRET_KEY={S3_SECRET}", S3)
        docker("network", "connect", self.n("s3b"), self.n("s3"))
        s3_port = self.port(self.n("s3"), 9000)
        self._wait("the store", lambda: subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--aws-sigv4", "aws:amz:us-east-1:s3", "-u", f"{S3_KEY}:{S3_SECRET}",
             "-X", "PUT", f"http://127.0.0.1:{s3_port}/bucket"], capture_output=True, text=True).stdout in ("200", "409"))

        # the primary's PostgreSQL, set up as INSTALL.md says: a role for the server that may promote, a role to replicate
        docker("run", "-d", "--name", self.n("pg-a"), "--network", self.n("net"), "-e", "POSTGRES_PASSWORD=admin", PG,
               "-c", "wal_level=replica", "-c", "max_wal_senders=5")
        self._wait("PostgreSQL a", lambda: self.psql(self.n("pg-a"), "SELECT 1") == "1")
        for sql in ("CREATE ROLE texloom LOGIN PASSWORD 'tl'", "CREATE DATABASE texloom OWNER texloom",
                    "CREATE ROLE replicator REPLICATION LOGIN PASSWORD 'rp'"):
            self.psql(self.n("pg-a"), sql)
        # (a function's privileges are per database: in the one the server uses, as INSTALL.md says)
        self.psql(self.n("pg-a"), "GRANT EXECUTE ON FUNCTION pg_promote(boolean, integer) TO texloom", db="texloom")
        self.psql(self.n("pg-a"), "GRANT pg_monitor TO texloom")  # (optional: lets the admin page show the standby's state and delay)
        docker("exec", "-u", "postgres", self.n("pg-a"), "bash", "-c",
               'echo "host replication replicator all scram-sha-256" >> "$PGDATA/pg_hba.conf"')
        self.psql(self.n("pg-a"), "SELECT pg_reload_conf()")

        self.relay("a", "s3a")  # creates the lease and the tables; the standby's base backup needs the tables
        self._wait("relay a to serve", lambda: self.health("a")[0] == 200)

        # the standby's PostgreSQL: a streaming base backup, as INSTALL.md does it
        docker("run", "--rm", "--network", self.n("net"), "-v", f"{self.n('pgb')}:/var/lib/postgresql/data", "-e", "PGPASSWORD=rp", PG,
               "bash", "-c", "chown postgres:postgres /var/lib/postgresql/data && chmod 700 /var/lib/postgresql/data && "
               f"gosu postgres pg_basebackup -h {self.n('pg-a')} -U replicator -D /var/lib/postgresql/data -R -X stream --checkpoint=fast",
               timeout=300)
        docker("run", "-d", "--name", self.n("pg-b"), "--network", self.n("net"), "-v", f"{self.n('pgb')}:/var/lib/postgresql/data", PG)
        self._wait("the standby PostgreSQL", lambda: self.psql(self.n("pg-b"), "SELECT pg_is_in_recovery()") == "t")
        self.relay("b", "s3b")
        self._wait("relay b to wait as the standby", lambda: self.health("b")[1].get("role") == "standby")

        # a Caddy per machine, from the example file: the site on :8080 without TLS, this machine's relay first
        example = (ROOT / "deploy" / "Caddyfile.failover").read_text()
        for me, peer in (("a", "b"), ("b", "a")):
            conf = example.replace("texloom.example.org {", ":8080 {").replace(
                "reverse_proxy 127.0.0.1:8787 PEER:8787", f"reverse_proxy {self.n('relay-' + me)}:8787 {self.n('relay-' + peer)}:8787")
            (self.tmp / f"Caddyfile-{me}").write_text(conf)
            docker("run", "-d", "--name", self.n(f"caddy-{me}"), "--network", self.n("net"), "-p", "127.0.0.1::8080",
                   "-v", f"{self.tmp / f'Caddyfile-{me}'}:/etc/caddy/Caddyfile:ro", CADDY)
            self.ports[me] = self.port(self.n(f"caddy-{me}"), 8080)
        self._wait("the Caddys to see the primary", lambda: all(self.get(self.ports[m], "/api/info") == 200 for m in "ab"))

    def relay(self, me: str, s3net: str) -> None:
        db = self.n("pg-" + me)
        docker("create", "--name", self.n("relay-" + me), "--network", self.n("net"), "--restart", "on-failure:30", IMAGE,
               "--host", "0.0.0.0", "--port", "8787", "--database", f"postgresql://texloom:tl@{db}/texloom", "--data", "/data",
               "--min-free", "1M", "--files", "s3://bucket", "--s3-endpoint", f"http://{self.n('s3')}:9000", "--s3-region", "us-east-1",
               "--s3-key-id", S3_KEY, "--s3-secret", S3_SECRET, "--failover", "--failover-name", me, "--failover-ttl", str(TTL),
               "--admin-token", ADMIN)
        docker("network", "connect", self.n(s3net), self.n("relay-" + me))
        docker("start", self.n("relay-" + me))

    def down(self) -> None:
        names = docker("ps", "-a", "--filter", f"name={self.p}-", "--format", "{{.Names}}", check_=False).stdout.split()
        if names:
            docker("rm", "-f", *names, check_=False)
        docker("volume", "rm", "-f", self.n("pgb"), check_=False)
        for net in ("net", "s3a", "s3b"):
            docker("network", "rm", self.n(net), check_=False)

    # --- looking ---------------------------------------------------------------------

    def port(self, container: str, inner: int) -> int:
        return int(docker("port", container, f"{inner}/tcp").stdout.split(":")[-1].split()[0])

    def health(self, me: str) -> tuple[int, dict]:
        """(status, body) of a relay's own /api/health: 0 if it can't be asked (not running)."""
        p = docker("exec", self.n("relay-" + me), "python", "-c", HEALTH, check_=False, timeout=15)
        if p.returncode != 0 or not p.stdout.strip():
            return 0, {}
        status, _, body = p.stdout.strip().partition(" ")
        try:
            return int(status), json.loads(body or "{}")
        except ValueError:
            return 0, {}

    @staticmethod
    def get(port: int, path: str) -> int:
        import urllib.error
        import urllib.request
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3).status
        except urllib.error.HTTPError as e:
            return e.code
        except OSError:
            return 0

    def logs(self, container: str, tail: int = 25) -> str:
        p = docker("logs", "--tail", str(tail), self.n(container), check_=False)
        return (p.stdout + p.stderr)[-1500:]

    def lsn_caught_up(self) -> bool:
        lsn = self.psql(self.n("pg-a"), "SELECT pg_current_wal_lsn()")
        return self.psql(self.n("pg-b"), f"SELECT pg_last_wal_replay_lsn() >= '{lsn}'::pg_lsn") == "t"


async def poll(cond, secs: float, every: float = 0.5) -> float | None:
    """Seconds until cond() holds (run off the event loop: it asks Docker), or None."""
    start = time.monotonic()
    while time.monotonic() - start < secs:
        if await asyncio.to_thread(cond):
            return time.monotonic() - start
        await asyncio.sleep(every)
    return None


class Scene:
    """Alice reaches the system through machine B's Caddy and Bob through machine A's."""

    def __init__(self, cl: Cluster, tmp: Path):
        self.cl, self.tmp = cl, tmp
        self.keys = Keys.generate()
        self.alice = App(tmp / "alice", self.keys, f"http://127.0.0.1:{cl.ports['b']}")
        self.bob = App(tmp / "bob", self.keys.personal("Bob")[2], f"http://127.0.0.1:{cl.ports['a']}")
        self.ra = self.rb = None

    async def start(self) -> None:
        self.ra, self.rb = await self.alice.start(), await self.bob.start()
        for i in range(3):
            await self.alice.edit(f"alice {i}")
        await self.bob.edit("bob 0")
        await until(lambda: text(self.ra) == text(self.rb))

    async def stop(self) -> None:
        await self.alice.stop()
        await self.bob.stop()

    async def newcomer_sees(self, want: list[str], port: int) -> bool:
        n = App(self.tmp / f"newcomer-{secrets.token_hex(2)}", Keys(self.keys.project, self.keys.key, None), f"http://127.0.0.1:{port}")
        try:
            r = await n.start()
            await until(lambda: all(x in text(r) for x in want), 25)
            return all(x in text(r) for x in want)
        finally:
            await n.stop()


def admin_page(port: int) -> str:
    """The admin page's text, through a Caddy (which sends it to the primary), with the token."""
    import base64
    import re
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}/admin", headers={
        "Authorization": "Basic " + base64.b64encode(f"x:{ADMIN}".encode()).decode()})
    with urllib.request.urlopen(req, timeout=10) as r:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.read().decode()))


async def scenario_crash(cl: Cluster, tmp: Path) -> None:
    scene = Scene(cl, tmp)
    try:
        await scene.start()
        page = await asyncio.to_thread(admin_page, cl.ports["a"])
        i = page.find("Failover")
        check("the admin page shows failover, and the standby following it (streaming, how far behind)",
              i >= 0 and "Standbys following" in page and "streaming" in page, page[i:i + 420])
        print("   (the page says: " + page[i:i + 330] + ")", flush=True)
        check("the standby has what the primary has", await poll(cl.lsn_caught_up, 20) is not None)
        # the standby's link is cut, so it misses what follows
        docker("network", "disconnect", cl.n("net"), cl.n("pg-b"))
        await scene.alice.edit("alice tail")
        await scene.bob.edit("bob tail")
        await until(lambda: text(scene.ra) == text(scene.rb) and "bob tail" in text(scene.ra))
        for c in ("relay-a", "caddy-a", "pg-a"):
            docker("update", "--restart", "no", cl.n(c), check_=False)
        lost = time.monotonic()
        docker("kill", cl.n("relay-a"), cl.n("caddy-a"), cl.n("pg-a"))
        docker("network", "connect", cl.n("net"), cl.n("pg-b"))
        up = await poll(lambda: cl.health("b")[0] == 200, 120)
        check(f"machine A is lost: B takes over ({(time.monotonic() - lost):.0f} s; its wait is {WAIT} s)", up is not None, cl.logs("relay-b"))
        check("its PostgreSQL was promoted", cl.psql(cl.n("pg-b"), "SELECT pg_is_in_recovery()") == "f")
        await scene.alice.edit("alice after")
        edited = time.monotonic() - lost
        check(f"Alice, through the surviving Caddy and without restarting, edits again ({edited:.0f} s after the loss)", edited < 90, edited)
        # (Bob's Caddy was on the lost machine: he reaches the system through the other, as a second address in DNS would)
        await scene.bob.stop()
        scene.bob = App(tmp / "bob", scene.keys.personal("Bob")[2], f"http://127.0.0.1:{cl.ports['b']}")
        scene.rb = await scene.bob.start()
        want = ["alice 0", "alice 2", "bob 0", "alice tail", "bob tail", "alice after"]
        check("a newcomer gets everything, including what the standby never had", await scene.newcomer_sees(want, cl.ports["b"]))
        check("Alice and Bob end with the same text", await until(lambda: text(scene.ra) == text(scene.rb), 20), (text(scene.ra), text(scene.rb)))
    finally:
        await scene.stop()


async def scenario_freeze(cl: Cluster, tmp: Path) -> None:
    scene = Scene(cl, tmp)
    both_serving = []
    try:
        await scene.start()
        check("the standby has what the primary has", await poll(cl.lsn_caught_up, 20) is not None)
        machine_a = [cl.n("relay-a"), cl.n("caddy-a"), cl.n("pg-a")]
        docker("pause", *machine_a)
        frozen = time.monotonic()
        up = await poll(lambda: cl.health("b")[0] == 200, 120)
        check(f"machine A is frozen: B takes over ({(time.monotonic() - frozen):.0f} s)", up is not None, cl.logs("relay-b"))
        # Alice's connection ran through the frozen relay and nothing reset it, so she notices only when her
        # keep-alive pings go unanswered (the app pings every 30 s)
        add_line(scene.ra, "alice while a is frozen")
        stored = await until(lambda: not scene.alice.r.outbox, 100)
        noticed = time.monotonic() - frozen
        check(f"Alice's connection to the frozen relay is noticed and her edit stored by B ({noticed:.0f} s after the freeze)", stored, noticed)
        docker("unpause", *machine_a)
        t_end = time.monotonic() + 20
        exited = None
        while time.monotonic() < t_end:  # never two that answer "primary"
            a, b = await asyncio.gather(asyncio.to_thread(cl.health, "a"), asyncio.to_thread(cl.health, "b"))
            if a[0] == 200 and b[0] == 200:
                both_serving.append(time.monotonic() - frozen)
            if exited is None and a[0] != 200:
                exited = time.monotonic() - frozen
            await asyncio.sleep(0.3)
        check("the machine that woke never answered as the primary while B did", not both_serving, both_serving)
        check("it is not serving, and (restarted by Docker as systemd would) waits as a standby or stays out",
              cl.health("a")[0] != 200, cl.logs("relay-a"))
        want = ["alice 0", "bob 0", "alice while a is frozen"]
        check("a newcomer gets everything from B", await scene.newcomer_sees(want, cl.ports["b"]))
        await scene.bob.stop()
        scene.bob = App(tmp / "bob", scene.keys.personal("Bob")[2], f"http://127.0.0.1:{cl.ports['b']}")
        scene.rb = await scene.bob.start()
        check("Alice and Bob end with the same text", await until(lambda: text(scene.ra) == text(scene.rb), 20), (text(scene.ra), text(scene.rb)))
    finally:
        await scene.stop()


async def scenario_cutoff(cl: Cluster, tmp: Path) -> None:
    scene = Scene(cl, tmp)
    try:
        await scene.start()
        check("the standby has what the primary has", await poll(cl.lsn_caught_up, 20) is not None)
        docker("network", "disconnect", cl.n("s3a"), cl.n("relay-a"))
        cut = time.monotonic()
        stopped = await poll(lambda: cl.health("a")[0] != 200, 30, 0.3)
        a_stopped = time.monotonic() - cut
        check(f"relay A can't reach the bucket and stops serving ({a_stopped:.1f} s; its lease is {TTL} s)", stopped is not None and a_stopped <= TTL + 4, a_stopped)
        up = await poll(lambda: cl.health("b")[0] == 200, 120)
        b_serving = time.monotonic() - cut
        check(f"B takes over, after A had stopped ({a_stopped:.1f} s, then {b_serving:.1f} s)", up is not None and a_stopped < b_serving, cl.logs("relay-b"))
        await scene.alice.edit("alice after")
        check("Bob, through machine A's Caddy (A's relay is down, B's is the primary), edits too", await until(lambda: "alice after" in text(scene.rb), 40), text(scene.rb))
        await scene.bob.edit("bob after")
        check("a newcomer gets everything", await scene.newcomer_sees(["alice 0", "bob 0", "alice after", "bob after"], cl.ports["b"]))
    finally:
        await scene.stop()


async def scenario_outage(cl: Cluster, tmp: Path) -> None:
    scene = Scene(cl, tmp)
    try:
        await scene.start()
        check("the standby has what the primary has", await poll(cl.lsn_caught_up, 20) is not None)
        docker("stop", cl.n("s3"))
        stopped_at = time.monotonic()
        await poll(lambda: cl.health("a")[0] != 200, 30, 0.3)
        check("with the bucket gone, the primary stops serving (a failover needs it: everything waits)", cl.health("a")[0] != 200)
        await asyncio.sleep(max(0, 2 * WAIT - (time.monotonic() - stopped_at)))  # longer than the standby's wait
        docker("start", cl.n("s3"))
        back = time.monotonic()
        up = await poll(lambda: cl.health("a")[0] == 200 or cl.health("b")[0] == 200, 120)
        who = "a" if cl.health("a")[0] == 200 else "b"
        check(f"the bucket is back: service resumes by itself ({time.monotonic() - back:.0f} s)", up is not None, cl.logs("relay-a") + cl.logs("relay-b"))
        check("by the same primary, with no needless switch to the standby (whose own view of the lease was just as stale)",
              who == "a", f"primary is now {who}: " + cl.logs("relay-b", 12))
        await scene.alice.edit("alice after")
        check("a newcomer gets everything", await scene.newcomer_sees(["alice 0", "bob 0", "alice after"], cl.ports["b" if who == "b" else "a"]))
    finally:
        await scene.stop()


SCENARIOS = {"crash": scenario_crash, "freeze": scenario_freeze, "cutoff": scenario_cutoff, "outage": scenario_outage}


async def main() -> int:
    if not shutil.which("docker") or docker("version", "--format", "{{.Server.Version}}", check_=False).returncode != 0:
        print("skipped: Docker isn't running")
        return 0
    names = sys.argv[1:] or list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        sys.exit(f"no such scenario: {', '.join(unknown)} (have {', '.join(SCENARIOS)})")
    print("building the relay image…", flush=True)
    build_image()
    for name in names:
        print(f"\n== {name}", flush=True)
        with tempfile.TemporaryDirectory(prefix=f"texloom-rehearsal-{name}-") as t:
            cl = Cluster(Path(t))
            try:
                cl.up()
                await SCENARIOS[name](cl, Path(t))
            except Exception as e:
                check(f"{name}: ran to the end", False, f"{type(e).__name__}: {e}")
                print(cl.logs("relay-a", 30), cl.logs("relay-b", 30), sep="\n", flush=True)
            finally:
                if os.environ.get("REHEARSE_LOGS"):
                    print(f"--- relay a:\n{cl.logs('relay-a', 60)}\n--- relay b:\n{cl.logs('relay-b', 60)}", flush=True)
                await asyncio.to_thread(cl.down)
    return summary("failover rehearsal")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
