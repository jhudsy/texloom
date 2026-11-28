#!/usr/bin/env python3
"""A warm standby taking over: nobody misses a change (issue 12; INSTALL.md, "A standby, switched by hand").

PostgreSQL streams the primary's changes to a standby. When the primary is lost the standby is
promoted and the server started on it, at the same address (in real life the name moves). The
standby is nearly current, not exactly: the last moments before the loss may not have reached it.
Those changes are in the apps, which notice that the new server's log isn't the one they last saw
and send everything again (as after a restore: scripts/check_restore.py).

The case: a server on a primary PostgreSQL with a standby streaming from it; the standby refuses
to be a server's database until promoted; Alice and Bob edit and the standby catches up; the link
to the standby is cut and they edit some more (changes the standby never gets); the primary is
lost (a crash, not a clean stop); the standby is promoted and the server started on it. Alice and
Bob come back, and a newcomer, with only the server to catch up from, must get everything,
including what the standby never had.

Starts its own throwaway PostgreSQL servers (scripts/pg.py, which needs PostgreSQL's programs
including pg_basebackup) and `texloom-server`, and drives the desktop app's sync engine.

    uv run --extra server python scripts/check_standby.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pg  # noqa: E402
from check_restore import App, text  # noqa: E402
from checklib import Server, check, summary, until  # noqa: E402

from texloom.crypto import Keys  # noqa: E402


async def scalar(url: str, sql: str, *args):
    import asyncpg
    c = await asyncpg.connect(url)
    try:
        return await c.fetchval(sql, *args)
    finally:
        await c.close()


async def head(url: str, project: str) -> int:
    """The server's newest change number for the project, in the database at `url`."""
    return await scalar(url, "SELECT seq FROM head WHERE project=$1", project) or 0


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="texloom-standby-") as t:
        tmp = Path(t)
        primary, standby = tmp / "primary", tmp / "standby"
        (tmp / "relay-a").mkdir()
        (tmp / "relay-b").mkdir()
        (tmp / "probe").mkdir()
        files = tmp / "files"  # a bucket, in real life: both servers see the same files
        primary_url, primary_port = pg.start(primary)
        relay = Server(tmp / "relay-a", primary_url, "--files", str(files))
        keys = Keys.generate()
        alice = App(tmp / "alice", keys, relay.url)
        bob = App(tmp / "bob", keys.personal("Bob")[2], relay.url)
        standby_url = ""
        try:
            relay.start()  # creates the tables, so the standby's base backup has them
            pg.standby_of(standby, primary_port)
            standby_url, _ = pg.start(standby)

            probe = Server(tmp / "probe", standby_url)
            try:
                probe.start(secs=15)
                said = ""
            except RuntimeError:
                said = (tmp / "probe" / "server.log").read_text()
            probe.stop()
            check("the server won't start on a standby, and says why", "is a standby" in said, said[-300:])

            a, b = await alice.start(), await bob.start()
            for i in range(3):
                await alice.edit(f"alice {i}")
            await bob.edit("bob 0")
            await until(lambda: text(a) == text(b))

            async def caught_up() -> bool:
                lsn = await scalar(primary_url, "SELECT pg_current_wal_lsn()::text")
                return bool(await scalar(standby_url, f"SELECT pg_last_wal_replay_lsn() >= '{lsn}'::pg_lsn"))
            ok = False
            for _ in range(100):
                if await caught_up():
                    ok = True
                    break
                await asyncio.sleep(0.1)
            check("the standby has what the primary has", ok)
            # (replication is asynchronous: the two heads are read a moment apart, so wait for them to agree, and
            # say what they were if they never do)
            heads = (0, 0)
            for _ in range(100):
                heads = (await head(standby_url, keys.project), await head(primary_url, keys.project))
                if heads[0] == heads[1] > 0:
                    break
                await asyncio.sleep(0.1)
            check("and is in step with the server's log", heads[0] == heads[1] > 0, f"standby {heads[0]}, primary {heads[1]}")

            # the link to the standby is cut; edits go on, and the standby never gets them
            pg.stop(standby)
            for i in range(2):
                await alice.edit(f"alice tail {i}")
            await bob.edit("bob tail")
            await until(lambda: text(a) == text(b) and "bob tail" in text(a))
            before = await head(primary_url, keys.project)
            await alice.stop()
            await bob.stop()

            # the primary is lost: a crash, with its database and its server
            relay.stop()
            pg.stop(primary, "immediate")

            # the standby is promoted and the server started on it, at the same address
            standby_url, _ = pg.start(standby)
            pg.promote(standby)
            after = await head(standby_url, keys.project)
            check("the promoted standby lacks what came after the link was cut", 0 < after < before, (after, before))
            relay.tmp, relay.database = tmp / "relay-b", standby_url
            relay.start()
            check("the server runs on the promoted standby", await scalar(standby_url, "SELECT pg_is_in_recovery()") is False)

            a, b = await alice.start(), await bob.start()
            for want in ("alice 2", "bob 0", "alice tail 1", "bob tail"):
                check(f"their copies keep {want!r}", want in text(a) and want in text(b), (text(a), text(b)))
            await alice.edit("alice after")
            await until(lambda: text(a) == text(b))
            check("editing carries on", "alice after" in text(b), text(b))
            await alice.stop()
            await bob.stop()

            newcomer = App(tmp / "newcomer", type(keys)(keys.project, keys.key, None), relay.url)
            c = await newcomer.start()
            want = ("alice 0", "bob 0", "alice tail 0", "alice tail 1", "bob tail", "alice after")
            await until(lambda: all(x in text(c) for x in want), 20)
            check("a newcomer gets everything from the new server, including what the standby never had",
                  all(x in text(c) for x in want), text(c))
            await newcomer.stop()
            check("the new server's log has moved on from where the standby was", await head(standby_url, keys.project) > after)
        finally:
            await alice.stop()
            await bob.stop()
            relay.stop()
            pg.stop(standby)
            pg.stop(primary)
    return summary("warm standby")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
