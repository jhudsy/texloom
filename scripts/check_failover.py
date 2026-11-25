#!/usr/bin/env python3
"""Automatic failover between two real servers (server/failover.py, issue 12, SCALING.md "Switching
over"; the design is modelled in models/failover): each server has its own PostgreSQL (the second a
streaming standby of the first, as in scripts/check_standby.py) and they share a bucket, a stand-in
(scripts/fakes3.py) that does conditional writes as the real ones must.

The cases:
- failover off (the default): the code isn't even imported, and a server starts as always;
- a server asked to fail over refuses a store that ignores conditional writes, SQLite, and a folder;
- one server holds the lease and serves, the other waits as the standby and says so (and every other
  address says "not the primary", 503);
- the primary is lost (killed, its database crashed, a change the standby never got): the standby
  takes the lease after the wait, promotes its PostgreSQL, serves; the apps and a newcomer end with
  everything, including what the standby never had;
- the primary is cut off from the bucket (it can't renew): it stops serving, closes its connections
  and exits BEFORE the standby serves (never two at once), and the standby takes over;
- a former primary (its database a primary, the lease held by the other) is not allowed to take over by
  waiting.

    uv run --extra server python scripts/check_failover.py
"""

import asyncio
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pg  # noqa: E402
from check_restore import App, text  # noqa: E402
from checklib import PY, ROOT, SERVER, check, free_port, summary, until  # noqa: E402
from fakes3 import FakeS3  # noqa: E402

from texloom.crypto import Keys  # noqa: E402

TTL = 3  # seconds: so the standby waits 2 x (3 + 2) = 10


def get(url: str, timeout: float = 3) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError):
        return 0, {}


class Relay:
    """A `texloom-server --failover`, as a subprocess with a log of its own."""

    def __init__(self, tmp: Path, name: str, database: str, s3_url: str, *extra: str):
        self.tmp, self.name = tmp, name
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        (tmp / name).mkdir(parents=True, exist_ok=True)
        self.args = ["--port", str(self.port), "--database", database, "--data", str(tmp / name),
                     "--files", "s3://bucket", "--s3-endpoint", s3_url, "--s3-key-id", f"relay-{name}", "--s3-secret", "x",
                     "--failover", "--failover-name", name, "--failover-ttl", str(TTL), *extra]
        self.proc: subprocess.Popen | None = None
        self._log = None

    def start(self) -> None:
        self._log = open(self.tmp / f"{self.name}.log", "a")  # noqa: SIM115 - open while it runs
        self.proc = subprocess.Popen(PY + [SERVER, *self.args], cwd=ROOT, stdout=self._log, stderr=subprocess.STDOUT)

    def health(self) -> tuple[int, dict]:
        return get(self.url + "/api/health")

    def info(self) -> int:
        return get(self.url + "/api/info")[0]

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log:
            self._log.close()
            self._log = None

    def log(self) -> str:
        return (self.tmp / f"{self.name}.log").read_text(errors="replace") if (self.tmp / f"{self.name}.log").exists() else ""


async def wait_for(cond, secs: float, every: float = 0.2) -> float | None:
    """Seconds until `cond()` holds, or None."""
    start = time.monotonic()
    while time.monotonic() - start < secs:
        if cond():
            return time.monotonic() - start
        await asyncio.sleep(every)
    return None


async def scalar(url: str, sql: str):
    import asyncpg
    c = await asyncpg.connect(url)
    try:
        return await c.fetchval(sql)
    finally:
        await c.close()


async def caught_up(primary_url: str, standby_url: str) -> bool:
    lsn = await scalar(primary_url, "SELECT pg_current_wal_lsn()::text")
    return bool(await scalar(standby_url, f"SELECT pg_last_wal_replay_lsn() >= '{lsn}'::pg_lsn"))


async def refusals(tmp: Path, s3: FakeS3) -> None:
    """What a server asked to fail over refuses, and what it leaves alone when it isn't asked."""
    base = ["--data", str(tmp / "refuse"), "--files", "s3://bucket", "--s3-endpoint", s3.url, "--s3-key-id", "x", "--s3-secret", "x"]

    async def run(*args: str) -> subprocess.CompletedProcess:
        # (off the event loop, which is serving the stand-in store these talk to)
        return await asyncio.to_thread(subprocess.run, PY + [SERVER, *args], cwd=ROOT, capture_output=True, text=True,
                                       timeout=60, check=False)

    s3.conditional = False
    r = await run(*base, "--database", "postgresql://nobody@127.0.0.1:1/x", "--failover")
    check("a store that ignores conditional writes is refused, saying so",
          r.returncode != 0 and "conditional writes" in r.stderr, r.stderr[-300:])
    r = await run(*base, "--failover-check")
    check("--failover-check says so too", r.returncode != 0 and "conditional writes" in r.stderr, r.stderr[-300:])
    s3.conditional, s3.racy = True, True
    r = await run(*base, "--failover-check")
    check("a store whose conditions aren't atomic (simultaneous writers all win) is refused",
          r.returncode != 0 and "simultaneous" in r.stderr, r.stderr[-300:])
    s3.racy = False
    r = await run(*base, "--failover-check")
    check("--failover-check passes a store that does them", r.returncode == 0 and "can hold a failover lease" in r.stdout, r.stderr[-300:])
    check("and leaves nothing of its test behind", not [k for k in s3.objects if "check-" in k], list(s3.objects))
    r = await run("--data", str(tmp / "refuse"), "--failover", "--files", "s3://bucket", "--s3-endpoint", s3.url,
            "--s3-key-id", "x", "--s3-secret", "x", "--database", f"sqlite:{tmp / 'x.db'}")
    check("SQLite is refused", r.returncode != 0 and "needs PostgreSQL" in r.stderr, r.stderr[-300:])
    r = await run("--data", str(tmp / "refuse"), "--failover", "--database", "postgresql://nobody@127.0.0.1:1/x")
    check("a folder for files is refused", r.returncode != 0 and "s3://" in r.stderr, r.stderr[-300:])
    code = ("import sys; from server.app import Settings, make_app; "
            f"make_app(Settings(database='sqlite:{tmp / 'y.db'}')); "
            "print('failover' in ''.join(m for m in sys.modules if m.startswith('server')))")
    r = await asyncio.to_thread(run_py, code)
    check("failover off: server/failover.py isn't even imported", r.stdout.strip() == "False", r.stdout + r.stderr[-300:])


def run_py(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)


async def scenario(kind: str, tmp: Path) -> None:
    """kind: "crash" (the primary and its database are lost) or "cutoff" (it can't reach the bucket)."""
    print(f"\n== {kind}")
    s3 = FakeS3()
    s3_url = await s3.start()
    pg_a, pg_b = tmp / "pg-a", tmp / "pg-b"
    url_a, port_a = pg.start(pg_a)
    a = Relay(tmp, "a", url_a, s3_url)
    b: Relay | None = None
    keys = Keys.generate()
    apps: list[App] = []
    url_b = ""
    try:
        a.start()
        check("the first server creates the lease and serves", await wait_for(lambda: a.health()[0] == 200, 40) is not None, a.log()[-500:])
        check("it says it is the primary", a.health()[1].get("role") == "primary", a.health())
        check("the lease is in the bucket", "_failover/lease" in s3.objects, list(s3.objects))

        pg.standby_of(pg_b, port_a)
        url_b, port_b = pg.start(pg_b)
        b = Relay(tmp, "b", url_b, s3_url)
        b.start()
        ok = await wait_for(lambda: b.health()[1].get("holder") == "a", 40)
        check("the second server waits as the standby, and says who holds the lease", ok is not None and b.health()[1].get("role") == "standby", b.health())
        check("the standby answers every address but /api/health with 503", b.info() == 503 and b.health()[0] == 503)
        check("the primary serves them", a.info() == 200)

        alice = App(tmp / "alice", keys, a.url)
        bob = App(tmp / "bob", keys.personal("Bob")[2], a.url)
        apps = [alice, bob]
        ra, rb = await alice.start(), await bob.start()
        for i in range(3):
            await alice.edit(f"alice {i}")
        await bob.edit("bob 0")
        await until(lambda: text(ra) == text(rb))
        ok = False
        for _ in range(100):
            if await caught_up(url_a, url_b):
                ok = True
                break
            await asyncio.sleep(0.1)
        check("the standby's database has what the primary's has", ok)

        if kind == "crash":
            pg.stop(pg_b)  # the link to the standby is cut: it misses what follows
            await alice.edit("alice tail")
            await bob.edit("bob tail")
            await until(lambda: text(ra) == text(rb) and "bob tail" in text(ra))
            await alice.stop()
            await bob.stop()
            apps = []
            lost_at = time.monotonic()
            a.kill()
            pg.stop(pg_a, "immediate")
            pg.start(pg_b, port_b)  # the standby's database is back (on the same address), still following nothing
            took = await wait_for(lambda: b.health()[0] == 200, 90)
            check("the standby takes over after the wait, and serves", took is not None, b.log()[-800:])
            if took is not None:
                waited = time.monotonic() - lost_at
                check(f"... not sooner than its wait ({waited:.0f} s)", waited >= 2 * (TTL + 2) - 2.5, waited)
            check("it is the primary, and its database was promoted",
                  b.health()[1].get("role") == "primary" and await scalar(url_b, "SELECT pg_is_in_recovery()") is False, b.health())
        else:
            s3.fail_for.add("relay-a")  # the primary can't reach the bucket
            cut_at = time.monotonic()
            stopped = await wait_for(lambda: not a.alive() or a.health()[0] != 200, 30)
            a_stopped = time.monotonic() - cut_at
            check(f"the primary stops serving when it can't renew ({a_stopped:.1f} s)", stopped is not None and a_stopped <= TTL + 3, a.log()[-600:])
            check("it closed its connections: an app connected to it is disconnected", await wait_for(lambda: alice.r is not None and alice.r.ws is None, 15) is not None)
            check("and exited, to be started again as a standby", await wait_for(lambda: not a.alive(), 15) is not None and a.proc is not None and a.proc.returncode == 75, a.proc.returncode if a.proc else None)
            took = await wait_for(lambda: b.health()[0] == 200, 90)
            b_serving = time.monotonic() - cut_at
            check("the standby takes over", took is not None, b.log()[-800:])
            check(f"and only after the primary had stopped ({a_stopped:.1f} s against {b_serving:.1f} s)", a_stopped < b_serving)
            await alice.stop()
            await bob.stop()
            apps = []

        # the apps and a newcomer, on the new primary
        assert b is not None
        alice = App(tmp / "alice", keys, b.url)
        bob = App(tmp / "bob", keys.personal("Bob")[2], b.url)
        newcomer = App(tmp / "newcomer", type(keys)(keys.project, keys.key, None), b.url)
        apps = [alice, bob, newcomer]
        ra, rb = await alice.start(), await bob.start()
        await alice.edit("alice after")
        c = await newcomer.start()
        want = ["alice 0", "alice 2", "bob 0", "alice after"] + (["alice tail", "bob tail"] if kind == "crash" else [])
        await until(lambda: all(x in text(c) for x in want), 20)
        check("a newcomer gets everything from the new primary" + (", including what the standby never had" if kind == "crash" else ""),
              all(x in text(c) for x in want), text(c))
        check("the apps carry on editing", await until(lambda: "alice after" in text(rb), 10), text(rb))
    finally:
        for app in apps:
            await app.stop()
        a.stop()
        if b:
            b.stop()
        pg.stop(pg_b)
        pg.stop(pg_a)
        await s3.stop()


async def former_primary(tmp: Path) -> None:
    """A server whose database is a primary (a former one), with the lease held by another, must
    not take over by waiting: it would serve a history the new primary doesn't have."""
    print("\n== a former primary")
    s3 = FakeS3()
    s3_url = await s3.start()
    url, _ = pg.start(tmp / "pg-old")
    old = Relay(tmp, "old", url, s3_url)
    try:
        # the other server holds the lease (and never renews it, being no server at all)
        from server.failover import encode
        from server.files import S3Files
        files = S3Files(s3_url, "bucket", "", "auto", "x", "x")
        await files.lease_write(encode(7, "new"), None)
        await files.close()
        old.start()
        waited = await wait_for(lambda: "not taking over" in old.log(), 60)
        check("it refuses to take over, saying why", waited is not None, old.log()[-600:])
        check("and keeps waiting rather than serving", old.alive() and old.health()[0] == 503)
        lease = s3.objects.get("_failover/lease")
        check("the lease is still the other server's", lease is not None and b'"new"' in lease[0], lease)
    finally:
        old.stop()
        pg.stop(tmp / "pg-old")
        await s3.stop()


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="texloom-failover-") as t:
        tmp = Path(t)
        s3 = FakeS3()
        await s3.start()
        try:
            await refusals(tmp, s3)
        finally:
            await s3.stop()
        for kind in ("crash", "cutoff"):
            (tmp / kind).mkdir()
            await scenario(kind, tmp / kind)
        (tmp / "former").mkdir()
        await former_primary(tmp / "former")
    return summary("failover")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
