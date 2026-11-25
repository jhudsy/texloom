#!/usr/bin/env python3
"""What several servers sharing one database need of each (SCALING.md, "Three ways to run it", way 3), built
first because it is safe with one server and checked here with two sharing a PostgreSQL:

- the usage ledger: a database with data and no ledger (before it existed) gets one at the first start; a
  ledger that has drifted is found and put right by a recount (Store.reconcile), and only that project is touched;
- the lock: one server holds it at a time, it is free again afterwards; SQLite (one server) always holds it;
- notifications: what one server tells reaches the others, a server that was disconnected is told to look
  again at everything when it is back, and one that is too big is refused;
- two stores on one database: when one changes a project (its owner, evicts it, deletes it) the other,
  which remembered the old, takes the new (its totals too), ignores what it said itself, and ignores what isn't
  one of ours;
- upkeep runs on one server at a time, on a standby (no database open) does nothing, and on SQLite runs.

    uv run --extra server python scripts/check_groundwork.py        # needs PostgreSQL's programs for the second half
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pg  # noqa: E402
from checklib import check, summary  # noqa: E402

from server.app import upkeep  # noqa: E402
from server.db import MAINTENANCE_LOCK, DatabaseError, connect  # noqa: E402
from server.files import FolderFiles  # noqa: E402
from server.store import Store  # noqa: E402


def pid(i: int) -> str:
    return hashlib.sha256(f"groundwork {i}".encode()).hexdigest()[:43]


async def until(cond, secs: float = 8) -> bool:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


async def ledger(tmp: Path) -> None:
    print("\n== the usage ledger (SQLite)")
    url = f"sqlite:{tmp / 'ledger.db'}"
    store = await Store.open(url, FolderFiles(tmp / "files-l"), tmp / "d-l")
    a, b = pid(1), pid(2)
    for p in (a, b):
        await store.register(p, "default", "check")
        for i in range(3):
            await store.append(p, b"x" * (100 + i), b"s" * 64)
    mine = dict(store.usage)
    check("the ledger has a row for each project with data", {r[0]: r[1] for r in await store.db.fetch("SELECT project, bytes FROM usage")} == mine)
    await store.close()

    # a database from before the ledger: data, and no ledger
    db = await connect(url)
    await db.execute("DELETE FROM usage")
    await db.close()
    store = await Store.open(url, FolderFiles(tmp / "files-l"), tmp / "d-l")
    check("a database with no ledger gets one at the first start, adding up what is stored", dict(store.usage) == mine and
          {r[0]: r[1] for r in await store.db.fetch("SELECT project, bytes FROM usage")} == mine)

    # drift: a row that is wrong, and one for a project with nothing
    await store.db.execute("UPDATE usage SET bytes = bytes + 999 WHERE project=$1", a)
    await store.db.execute("INSERT INTO usage(project, bytes) VALUES ($1, 5)", pid(3))
    wrong = await store.reconcile()
    rows = {r[0]: r[1] for r in await store.db.fetch("SELECT project, bytes FROM usage")}
    check("a recount finds the two that were wrong and puts them right", wrong == 2 and rows == mine, (wrong, rows))
    check("and the next finds nothing", await store.reconcile() == 0)
    store.usage[b] += 7  # (this server's count is off while the ledger is right)
    check("a count in memory that is off is put right too", await store.reconcile() == 1 and store.usage[b] == mine[b])
    await store.close()


async def database(url: str) -> None:
    print("\n== the lock, notifications (PostgreSQL)")
    one, two = await connect(url), await connect(url)
    async with one.lock(MAINTENANCE_LOCK) as g1:
        async with two.lock(MAINTENANCE_LOCK) as g2:
            check("one server holds the lock and the other is told so", g1 is True and g2 is False, (g1, g2))
    async with two.lock(MAINTENANCE_LOCK) as g3:
        check("it is free again when the holder is done", g3 is True)
    async with one.lock(MAINTENANCE_LOCK) as g1:
        pass
    async with one.lock(1) as x, two.lock(2) as y:
        check("different locks don't hold each other", x is True and y is True)

    heard: list[str] = []
    await two.listen("chan", heard.append)
    await one.notify("chan", "hello")
    check("what one server tells reaches another", await until(lambda: heard == ["hello"]), heard)
    try:
        await one.notify("chan", "x" * 8000)
        big = False
    except DatabaseError:
        big = True
    check("a notification that is too big is refused", big)

    heard.clear()
    victim = await one.fetchval("SELECT pid FROM pg_stat_activity WHERE query ILIKE 'LISTEN%' AND pid <> pg_backend_pid() LIMIT 1")
    await one.execute("SELECT pg_terminate_backend($1)", victim)
    check("a listener that lost its connection is told to look again at everything, when it is back", await until(lambda: "" in heard, 25), heard)
    await one.notify("chan", "after")
    check("and hears what comes after", await until(lambda: "after" in heard), heard)
    await one.close()
    await two.close()


async def stores(url: str, tmp: Path) -> None:
    print("\n== two stores on one database")
    files = FolderFiles(tmp / "files-s")
    sa = await Store.open(url, files, tmp / "d-a", serving=True)
    sb = await Store.open(url, files, tmp / "d-b", serving=True)
    await sa.start()
    await sb.start()
    p = pid(10)
    calls = {"a": 0, "b": 0}
    for name, st in (("a", sa), ("b", sb)):
        original = st.refresh_project

        async def spy(project, original=original, name=name):
            calls[name] += 1
            await original(project)
        st.refresh_project = spy
    await sa.register(p, "OWNERX", "check")
    for i in range(3):
        await sa.append(p, b"x" * 200, b"s" * 64)
    await sa.claim(p, "OWNERY")
    total = lambda st: sum(st.usage.values()) + sum(st.list_bytes.values())  # noqa: E731
    check("a project another server changed (its owner) is taken from the database by this one",
          await until(lambda: sb.owners.get(p) == "OWNERY" and sb.usage.get(p) == sa.usage.get(p) > 0),
          (sb.owners.get(p), sb.usage.get(p), sa.usage.get(p)))
    check("its owner's count moved with it, and the total adds up",
          sb.owner_bytes["OWNERY"] == sb.usage[p] and sb.total == total(sb), (dict(sb.owner_bytes), sb.total, total(sb)))
    check("the server that said it did not look again at itself", calls["a"] == 0, calls)

    await sa.evict(p)
    check("an eviction is taken too (what is stored, and that it is evicted)",
          await until(lambda: p in sb.evicted and sb.usage.get(p) == sa.usage.get(p)), (sb.usage.get(p), sa.usage.get(p)))
    check("... and the other's total still adds up", sb.total == total(sb) and sa.total == total(sa))

    await sa.delete_project(p, b"z" * 64, 1)
    check("a project deleted for everyone is forgotten by the other server, and the totals agree",
          await until(lambda: p not in sb.owners and not sb.usage.get(p) and sb.total == total(sb) and sb.owner_bytes["OWNERY"] == 0),
          (sb.owners.get(p), sb.usage.get(p), sb.total, total(sb)))

    sa._heard("not json")
    sa._heard('{"no": "project"}')
    await asyncio.sleep(0.3)
    check("what isn't one of ours is ignored", calls["a"] == 0)

    sb.usage["made-up"] = 12345  # this server's memory is wrong; "told to look again at everything" sets it right
    sb.total += 12345
    sb._heard("")
    check("a server told to look again at everything does, and what was wrong in memory goes",
          await until(lambda: "made-up" not in sb.usage and sb.total == total(sb)), (sb.usage.get("made-up"), sb.total, total(sb)))
    check("...and its count agrees with the ledger", await sb.reconcile() == 0)
    await sa.close()
    await sb.close()


async def upkeeps(url: str, tmp: Path) -> None:
    print("\n== upkeep")
    sql_db = await connect(f"sqlite:{tmp / 'u.db'}")
    pg_a, pg_b = await connect(url), await connect(url)
    ran = {"x": 0, "y": 0, "z": 0, "sqlite": 0}

    def policy(name: str, db) -> SimpleNamespace:
        async def evict_idle(days):
            ran[name] += 1
            await asyncio.sleep(1.5)  # (long enough that the other, starting at once, finds the lock taken)

        async def reconcile():
            return 0
        return SimpleNamespace(store=None if db is None else SimpleNamespace(db=db, reconcile=reconcile),
                               evict_idle=evict_idle, purge_expired=None, lists=None)

    tasks = [asyncio.create_task(upkeep(policy("x", pg_a), 1, None)), asyncio.create_task(upkeep(policy("y", pg_b), 1, None)),
             asyncio.create_task(upkeep(policy("z", None), 1, None)), asyncio.create_task(upkeep(policy("sqlite", sql_db), 1, None))]
    await asyncio.sleep(4)
    check("with two servers on one database, one does the upkeep and the other leaves it", ran["x"] + ran["y"] == 1, ran)
    check("a server with no database open has nothing to do (and doesn't fail)", ran["z"] == 0 and not tasks[2].done(), ran)
    check("on SQLite (one server) it runs", ran["sqlite"] == 1, ran)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await pg_a.close()
    await pg_b.close()
    await sql_db.close()


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="texloom-groundwork-") as t:
        tmp = Path(t)
        await ledger(tmp)
        try:
            pg.bindir()
        except SystemExit:
            print("\nskipped the PostgreSQL half: no PostgreSQL programs")
            return summary("groundwork")
        with pg.running() as url:
            await database(url)
            await stores(url, tmp)
            await upkeeps(url, tmp)
    return summary("groundwork")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
