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
- upkeep runs on one server at a time, on a standby (no database open) does nothing, and on SQLite runs;
- checks made in one transaction holding the project (Store.locked; FUTURE_ARCHITECTURE.md, "Building it",
  step 1), with writes racing through two servers: the same change is stored once; a creation code's
  allowance, and a project's limit, are never passed together; an upload under way through one server
  counts for the other, until it is stored or its reservation runs out; project lists count too; and a
  transaction that fails leaves the server's memory as it was;
- a second server with no bus on a database another such server uses refuses to start, saying why; once
  the first has stopped it starts; and two with a bus may share it.

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
from server.lists import Lists  # noqa: E402
from server.policy import CodeBook, Limits, Policy  # noqa: E402
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


async def up(store: Store, policy: Policy, project: str, data: bytes, code: str | None) -> int | str | None:
    """A change as relay._up stores it: its seq (a replay: the first copy's), or why it was refused. It pauses
    between checking and storing, as a busy server might, so that racing writes overlap there."""
    async with store.locked(project) as c:
        dup = await store.duplicate(project, data, c)
        if dup is not None:
            return dup
        refused = await policy.may_store(project, len(data) + 64, code, c)
        if refused:
            return refused
        await asyncio.sleep(0.02)
        return await store.append(project, data, b"s" * 64, c)


async def races(url: str, tmp: Path) -> None:
    print("\n== checks that hold through two servers at once")
    codes = tmp / "codes.txt"
    # allowances of 10,000 bytes; "default" can't store
    codes.write_text("default 0\nAAAABBBBCCCC 10000 one\nDDDDEEEEFFFF 10000 two\nGGGGHHHHJJJJ 10000 three\n"
                     "KKKKMMMMNNNN 1000000 four\n")
    files = FolderFiles(tmp / "files-r")
    sa = await Store.open(url, files, tmp / "d-ra", serving=True)
    sb = await Store.open(url, files, tmp / "d-rb", serving=True)
    limits = Limits(min_free=0, project_max=10**9)  # (one shared object: the project limit is set for its own check)
    pa, pb = Policy(sa, CodeBook(codes), limits), Policy(sb, CodeBook(codes), limits)
    for pol in (pa, pb):
        pol.codes.load()
    both = [(sa, pa), (sb, pb)]

    # the same change, sent through both at once many times (a resend after a reconnect, say)
    p = pid(20)
    change = b"c" * 300
    got = await asyncio.gather(*[up(st, pol, p, change, "AAAABBBBCCCC") for _ in range(10) for st, pol in both])
    rows = await sa.db.fetchval("SELECT COUNT(*) FROM log WHERE project=$1", p)
    check("the same change sent through two servers at once is stored once, and each copy is told its number",
          rows == 1 and set(got) == {1}, (rows, set(got)))

    # one code's allowance, written to through both servers and five of its projects at once
    ps = [pid(30 + k) for k in range(5)]
    size = 1000 + 64
    got = await asyncio.gather(*[up(both[i // 5 % 2][0], both[i // 5 % 2][1], ps[i % 5], bytes([i]) * 1000, "DDDDEEEEFFFF")
                                 for i in range(50)])
    stored = sum(isinstance(g, int) for g in got)
    used = await sa.owner_total("DDDDEEEEFFFF", sa.db)
    check("writes racing through two servers never pass a code's allowance together",
          stored == 10000 // size and used == stored * size and all(isinstance(g, int) or g == "quota-full" for g in got),
          (stored, used, got))
    p1 = ps[0]

    # one project's limit (5,000 here), likewise
    p3 = pid(23)
    limits.project_max = 5000
    got = await asyncio.gather(*[up(both[i % 2][0], both[i % 2][1], p3, bytes([i]) * 1000, "KKKKMMMMNNNN") for i in range(12)])
    n = await sa.db.fetchval("SELECT bytes FROM usage WHERE project=$1", p3)
    check("... nor a project's limit", n == 4 * size and got.count("project-full") == 8, (n, got))
    limits.project_max = 10**9

    # an upload under way through one server counts for the other
    p4, p5 = pid(24), pid(25)
    async with sa.locked(p4) as c:
        ok = await pa.may_store(p4, 8000, "GGGGHHHHJJJJ", c) is None
        rid = await sa.reserve_upload(p4, 8000, c)
    refused = await up(sb, pb, p5, b"u" * 3000, "GGGGHHHHJJJJ")
    check("an upload allowed through one server counts at once for the other (its code is then full)",
          ok and refused == "quota-full" and sa.pending.get(p4) == 8000, (ok, refused, sa.pending.get(p4)))
    await sa.put_blob(p4, "1" * 32, b"f" * 8000, "file", rid)
    after = await sa.owner_total("GGGGHHHHJJJJ", sa.db)
    check("once stored it counts as stored, not twice, and the reservation is gone",
          after == 8000 and await sa.db.fetchval("SELECT COUNT(*) FROM reserved") == 0 and not sa.pending.get(p4),
          (after, sa.pending))
    await sa.db.execute("INSERT INTO reserved(id, project, owner, bytes, until) VALUES ('old', $1, 'GGGGHHHHJJJJ', 1000, $2)",
                        p4, time.time() - 1)
    check("a reservation a stopped server left behind stops counting when it runs out",
          await sa.owner_total("GGGGHHHHJJJJ", sa.db) == 8000)
    await sb.read_total()
    check("and is cleared out (with the server's total read again from the database)",
          await sa.db.fetchval("SELECT COUNT(*) FROM reserved") == 0 and sb.total == await sa.db.fetchval(
              "SELECT (SELECT SUM(bytes) FROM usage) + COALESCE((SELECT SUM(octet_length(data)) FROM lists), 0)"), sb.total)

    # project lists count against the code too
    lists = Lists(sb.db, sb.lists_grew)
    async with sb.owner_locked("GGGGHHHHJJJJ") as c:
        await lists.put("L" * 43, 1, b"l" * 1500, "GGGGHHHHJJJJ", c)
    check("a project list counts against its code, through any server",
          await sa.owner_total("GGGGHHHHJJJJ", sa.db) == 9500 and await up(sa, pa, p5, b"v" * 600, "GGGGHHHHJJJJ") == "quota-full")

    # a transaction that fails: the database and this server's memory are as they were
    before = (dict(sa.usage), sa.total)
    try:
        async with sa.locked(p1) as c:
            await sa.append(p1, b"w" * 500, b"s" * 64, c)
            raise RuntimeError("stop")
    except RuntimeError:
        pass
    check("a transaction that fails changes neither the database nor the server's memory",
          (dict(sa.usage), sa.total) == before and await sa.db.fetchval("SELECT COUNT(*) FROM log WHERE project=$1 AND data=$2", p1, b"w" * 500) == 0)
    await sa.close()
    await sb.close()


async def alone(url: str, tmp: Path) -> None:
    print("\n== a server with no bus is alone on its database")
    from aiohttp.test_utils import TestServer
    from server.app import Settings, make_app

    def server(name: str, bus: str = "") -> TestServer:
        return TestServer(make_app(Settings(database=url, bus=bus, data=tmp / name)))
    first = server("one")
    await first.start_server()
    second = server("two")
    try:
        await second.start_server()
        why = ""
    except SystemExit as e:
        why = str(e)
    check("a second server with no bus on the same database refuses to start, saying why", "no bus" in why, why)
    await second.close()
    await first.close()
    again = server("three")
    await again.start_server()
    check("once the first has stopped, another starts", again.started)
    await again.close()
    # (a bus that isn't in-process lets several share it: the lock isn't taken; checked here with the shape of
    # one, a Valkey being for check_bus.py)
    import server.app as app_mod
    from server.bus import Bus
    real = app_mod.open_bus

    class Shared(Bus):  # (stands in for a real bus: not the in-process one)
        name = "shared"
    app_mod.open_bus = lambda u: Shared() if u else real(u)
    try:
        a, b = server("four", "shared"), server("five", "shared")
        await a.start_server()
        await b.start_server()
        check("two servers with a bus share the database", a.started and b.started)
        await a.close()
        await b.close()
    finally:
        app_mod.open_bus = real


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
            await races(url, tmp)
            await alone(url, tmp)
            await upkeeps(url, tmp)
    return summary("groundwork")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
