#!/usr/bin/env python3
"""Several relays through a database failover (FUTURE_ARCHITECTURE.md, "Decided"; models/relays).

Two relays in one process share a bus and reach PostgreSQL through a multi-host address (target_session_attrs=
primary), so that after a failover they find the new primary by themselves. A real failover, with throwaway
PostgreSQLs (scripts/pg.py): a primary and a streaming standby; the standby is stopped, so it lacks the next
change; the primary crashes; the standby is started and promoted. Its timeline goes up, and its log lacks the last
change, which the app was told was stored. Then:

- each relay notices the failover (the timeline went up), and asks its apps to recheck;
- the app finds the server's log lacks the change it was told was stored, and sends it again: an app that connects
  afterwards, alone, gets every change from the database;
- a change stored before a failover the relay noticed before acknowledging it is stored again first, and
  acknowledged with the number the new primary gave it (models/relays: no-store-again);
- a change from another relay numbered by a newer timeline than this relay has seen makes it deal with the failover
  (recheck) before passing the change on (models/relays: no-timeline-on-bus).

    uv run --extra server python scripts/check_relays_failover.py     # needs PostgreSQL's programs (scripts/pg.py)
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pg  # noqa: E402
from check_relays import App  # noqa: E402
from check_restore import add_line, text  # noqa: E402
from checklib import check, summary, until  # noqa: E402

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import server.relay as relay_mod  # noqa: E402
from server.app import Settings, make_app  # noqa: E402
from server.db import _PgCalls  # noqa: E402
from server.files import FolderFiles  # noqa: E402
from server.policy import CodeBook  # noqa: E402
from server.store import Store  # noqa: E402
from texloom.crypto import Keys, b64e  # noqa: E402
from texloom.replica import Replica  # noqa: E402

relays: list = []
_init = relay_mod.Relay.__init__


def _recorded(self, *a, **kw):
    _init(self, *a, **kw)
    relays.append(self)


relay_mod.Relay.__init__ = _recorded  # type: ignore[method-assign]


async def caught_up(primary: str, standby: str) -> bool:
    import asyncpg
    p, s = await asyncpg.connect(primary), await asyncpg.connect(standby)
    try:
        lsn = await p.fetchval("SELECT pg_current_wal_lsn()::text")
        for _ in range(100):
            if await s.fetchval(f"SELECT pg_last_wal_replay_lsn() >= '{lsn}'::pg_lsn"):
                return True
            await asyncio.sleep(0.1)
        return False
    finally:
        await p.close()
        await s.close()


async def real_failover(tmp: Path, one: TestClient, two: TestClient, primary: Path, standby: Path, ports: tuple[int, int],
                        primary_url: str, standby_url: str) -> None:
    print("a real failover, which loses the last change:")
    keys = Keys.generate()
    url1, url2 = (str(c.make_url("")).rstrip("/") for c in (one, two))
    alice = Replica(tmp / "alice", keys, url1)
    bob = Replica(tmp / "bob", keys, url2)
    tasks = [asyncio.create_task(r.run()) for r in (alice, bob)]
    try:
        await asyncio.wait_for(asyncio.gather(alice.synced.wait(), bob.synced.wait()), 20)
        for line in ("one", "two"):
            add_line(alice, line)
            assert await until(lambda: not alice.outbox, 10), f"{line} wasn't stored"
        check("(the standby has what the primary has)", await caught_up(primary_url, standby_url))
        before = {r.number: r.timeline for r in relays}
        pg.stop(standby)  # (so it lacks what comes next)
        add_line(alice, "three, lost in the failover")
        check("(the last change is acknowledged by the primary)", await until(lambda: not alice.outbox, 10))
        await until(lambda: "three" in text(bob), 5)
        bob_task = tasks.pop()
        bob_task.cancel()  # (Bob goes: only the server can give the change back to a newcomer)
        await asyncio.gather(bob_task, return_exceptions=True)
        pg.stop(primary, "immediate")  # the primary crashes
        pg.start(standby, ports[1])
        pg.promote(standby)
        noticed = await until(lambda: all(r.timeline > before[r.number] for r in relays), 30)
        check("each relay notices the failover (the timeline went up)", noticed,
              {r.number: (before[r.number], r.timeline) for r in relays})
        resent = await until(lambda: not alice.outbox and alice._caught_up, 30)
        newcomer = Replica(tmp / "carol", keys, url2)
        t3 = asyncio.create_task(newcomer.run())
        try:
            await asyncio.wait_for(newcomer.synced.wait(), 20)
            got = await until(lambda: "three" in text(newcomer), 15)
        finally:
            t3.cancel()
            await asyncio.gather(t3, return_exceptions=True)
        check("the app asked to recheck finds its change lost, and sends it again: a newcomer alone gets every change",
              resent and got, repr(text(newcomer)))
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def store_again(one: TestClient) -> None:
    print("stored before a failover, acknowledged after it:")
    first = relays[0]
    keys = Keys.generate()
    a = App(one, keys)
    await a.join()
    data = b"stored, then the database failed over before the acknowledgement"
    store: Store = first.store
    real_timeline = _PgCalls.timeline
    later = {"tl": 0}

    async def timeline(self) -> int:  # (the database's timeline, as the new primary will report it)
        return later["tl"] or await real_timeline(self)
    _PgCalls.timeline = timeline  # type: ignore[method-assign]
    passing_on = relay_mod.Relay.broadcast
    stores = []

    async def failover_meanwhile(self, project, msg, exclude=None, tl=0):
        # just after it is stored, the database fails over and the new primary lacks it; this relay notices
        if msg.get("t") == "up" and msg.get("q") and project == keys.project and not stores:
            stores.append(msg["q"])
            await store.db.execute("DELETE FROM log WHERE project=$1 AND seq=$2", project, msg["q"])
            await store.db.execute("UPDATE head SET seq=seq-1 WHERE project=$1", project)
            later["tl"] = self.timeline + 1
            await self._failover(later["tl"])
        elif msg.get("t") == "up" and project == keys.project:
            stores.append(msg["q"])
        await passing_on(self, project, msg, exclude, tl)
    relay_mod.Relay.broadcast = failover_meanwhile  # type: ignore[method-assign]
    try:
        await a.send({"t": "up", "id": "1", "d": b64e(data), "s": b64e(keys.sign(b"upd:" + data))})
        ack = await a.heard_one("ack", 10)
    finally:
        relay_mod.Relay.broadcast = passing_on
    rows = await store.db.fetchval("SELECT COUNT(*) FROM log WHERE project=$1", keys.project)
    check("it is stored again on the new primary before it is acknowledged, with the number it has there",
          ack is not None and len(stores) == 2 and rows == 1 and ack["q"] == stores[-1], (ack, stores, rows))
    check("and the relay's apps are asked to recheck", (await a.heard_one("recheck", 5)) is not None)
    _PgCalls.timeline = real_timeline  # type: ignore[method-assign]
    await a.close()
    # (this relay now thinks the timeline is one past the database's: put it back for what follows)
    first.timeline = await store.db.timeline()


async def newer_on_bus(one: TestClient, two: TestClient) -> None:
    print("a change numbered by a newer timeline, from the bus:")
    keys = Keys.generate()
    a, b = App(one, keys, "AAAABBBBCCCC"), App(two, keys)
    await a.join()
    await b.join()
    second = next(r for r in relays if r.number == b.id >> 32)
    tl = second.timeline
    head = {"r": 999, "p": keys.project, "t": "up", "k": "s", "q": 1, "tl": tl + 1}
    await second._deliver(keys.project, head, json.dumps({"t": "up", "q": 1, "d": "x", "s": "y"}))
    await until(lambda: len([m for m in b.heard if m["t"] in ("recheck", "up")]) >= 2, 5)
    order = [m["t"] for m in b.heard if m["t"] in ("recheck", "up")]
    check("the relay deals with the failover first: its apps are asked to recheck, and then get the change",
          order[:2] == ["recheck", "up"] and second.timeline == tl + 1, order)
    second.timeline = tl
    await a.close()
    await b.close()


async def main() -> int:
    try:
        pg.bindir()
    except SystemExit:
        print("skipped: no PostgreSQL programs (scripts/pg.py)")
        return 0
    with tempfile.TemporaryDirectory(prefix="texloom-relays-failover-") as t:
        tmp = Path(t)
        primary, standby = tmp / "primary", tmp / "standby"
        primary_url, pport = pg.start(primary)
        try:
            boot = await Store.open(primary_url, FolderFiles(tmp / "files"), tmp / "boot")  # (the tables, for the standby's copy)
            await boot.close()
            pg.standby_of(standby, pport)
            standby_url, sport = pg.start(standby)
            both = f"postgresql://texloom@127.0.0.1:{pport},127.0.0.1:{sport}/postgres?target_session_attrs=primary"
            codes = tmp / "codes.txt"
            codes.write_text("default 10M\nAAAABBBBCCCC 10M one\n")

            def server(name: str) -> TestServer:
                return TestServer(make_app(Settings(database=both, bus="memory:check-relays-failover", data=tmp / name,
                                                    codes=CodeBook(codes))))
            async with TestClient(server("one")) as one, TestClient(server("two")) as two:
                await until(lambda: all(r.timeline for r in relays), 70)  # (read at their first head check)
                await store_again(one)
                await newer_on_bus(one, two)
                await real_failover(tmp, one, two, primary, standby, (pport, sport), primary_url, standby_url)
        finally:
            pg.stop(standby)
            pg.stop(primary)
    return summary("relays through a failover")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
