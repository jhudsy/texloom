#!/usr/bin/env python3
"""Several relays at once (FUTURE_ARCHITECTURE.md): two servers in one process, sharing a PostgreSQL and a bus
(the in-process one, named: memory:NAME), with apps on each. What one relay's apps send reaches the other's:

- stored changes, presence, peer-to-peer messages to everyone and to one connection on the other relay (signed
  or not), "left" when an app goes, revocations, a project moved to another code (each app told what it may
  see), and a deletion (the other relay's apps told, and closed);
- who is connected is counted across relays (the catch-up's "peers", and the limit per project);
- a relay that goes without a word (its heartbeat stops) is noticed by the other, whose apps hear that its
  connections have left;
- gaps: a change whose passing on was lost is read from the database by the relay that missed it, soon after a
  later one shows the gap, or at its next head check when nothing does; none is passed on twice;
- the bus away from one relay: stored changes still reach its apps (its head check, every second meanwhile);
  once it's back, what was missed is filled, and the apps of sync-only projects are asked to catch up from each
  other ("resync");
- health: a relay whose bus has been away a few seconds, or whose database is unavailable, says so (/api/health:
  503) and refuses new connections, so a front door sends them elsewhere; the connections it has stay; once
  it's well again it takes them;
- creation codes: a relay with no codes file takes another's, through the database, within seconds of the file
  changing (a code added, one removed, the default);
- the desktop helper's sync engine, on either relay: a stored project, and a sync-only one (nothing on the
  server: the apps fill each other in, across the relays); and a stored one while one relay's bus is away.

The bus over Valkey is checked by scripts/check_bus.py, and real relays on separate machines by the lab
(scripts/rehearse_relays.py).

    uv run --extra server python scripts/check_relays.py     # needs PostgreSQL's programs (scripts/pg.py)
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parent.parent
import pg  # noqa: E402
from check_restore import add_line, text  # noqa: E402
from checklib import check, summary, until  # noqa: E402

from aiohttp import WSMsgType  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import server.relay as relay_mod  # noqa: E402
from server.app import Settings, make_app  # noqa: E402
from server.policy import CodeBook  # noqa: E402
from texloom.crypto import Keys, b64e, revoke_msg  # noqa: E402
from texloom.replica import Replica  # noqa: E402

relay_mod.BEAT, relay_mod.GONE_AFTER, relay_mod.HEAD_CHECK, relay_mod.BUS_GRACE = 0.2, 1.0, 2.0, 0.5  # (quicker than in use)
relays: list = []
_init = relay_mod.Relay.__init__


def _recorded(self, *a, **kw):
    _init(self, *a, **kw)
    relays.append(self)


relay_mod.Relay.__init__ = _recorded  # type: ignore[method-assign]


class App:
    """A raw connection, as an app makes one, keeping what it hears."""

    def __init__(self, c: TestClient, keys: Keys, code: str | None = None):
        self.c, self.keys, self.code = c, keys, code
        self.heard: list[dict] = []
        self.closed = False

    async def join(self) -> dict:
        self.ws = await self.c.ws_connect(f"/ws/{self.keys.project}")
        await self.ws.send_json({"t": "hello", "since": 0, "app": "desktop", "v": "0.4.67", "pages": 1, "ka": 1, "rs": 1,
                                 **({"code": self.code} if self.code else {})})
        self.reader = asyncio.create_task(self._read())
        assert await until(lambda: any(m["t"] == "log" for m in self.heard), 10)
        log = next(m for m in self.heard if m["t"] == "log")
        self.id = log["you"]
        return log

    async def _read(self) -> None:
        async for m in self.ws:
            if m.type == WSMsgType.TEXT:
                self.heard.append(json.loads(m.data))
        self.closed = True

    async def send(self, msg: dict) -> None:
        await self.ws.send_json(msg)

    def got(self, t: str, **match) -> list[dict]:
        return [m for m in self.heard if m["t"] == t and all(m.get(k) == v for k, v in match.items())]

    async def heard_one(self, t: str, secs: float = 5, **match) -> dict | None:
        await until(lambda: bool(self.got(t, **match)), secs)
        found = self.got(t, **match)
        return found[-1] if found else None

    async def close(self) -> None:
        await self.ws.close()
        self.reader.cancel()


def signed(keys: Keys, prefix: bytes, data: bytes) -> str:
    return b64e(keys.sign(prefix + data))


async def messages(one: TestClient, two: TestClient) -> None:
    print("messages across relays:")
    keys = Keys.generate()
    a = App(one, keys, "AAAABBBBCCCC")
    await a.join()
    b = App(two, keys)
    log = await b.join()
    check("the catch-up counts who is connected on every relay (peers)", log.get("peers") == 1, log.get("peers"))
    check("connection ids differ between relays", a.id != b.id and a.id >> 32 != b.id >> 32, (a.id, b.id))

    data = b"a stored change"
    await a.send({"t": "up", "id": "1", "d": b64e(data), "s": signed(keys, b"upd:", data)})
    up = await b.heard_one("up")
    check("a stored change reaches the other relay's app, with its number", up is not None and up["q"] == 1, up)
    await a.send({"t": "aw", "d": "presence"})
    aw = await b.heard_one("aw")
    check("presence too, saying whose", aw is not None and aw["from"] == a.id, aw)
    await a.send({"t": "p2p", "d": "to everyone"})
    check("a peer-to-peer message to everyone", (await b.heard_one("p2p", d="to everyone")) is not None)
    await b.send({"t": "p2p", "d": "to one", "to": a.id})
    p = await a.heard_one("p2p", d="to one")
    check("and to one connection on the other relay, saying whose", p is not None and p["from"] == b.id, p)
    d = b"signed, to one"
    await a.send({"t": "p2p", "d": b64e(d), "s": signed(keys, b"p2p:", d), "to": b.id})
    check("signed, to one", (await b.heard_one("p2p", d=b64e(d))) is not None)

    pub = b64e(os.urandom(32))  # (a personal link's key: any 32 bytes do here)
    await a.send({"t": "revoke", "p": pub, "s": b64e(keys.sign(revoke_msg(pub)))})
    check("a revocation", (await b.heard_one("rv")) is not None)

    c = App(two, keys, "AAAABBBBCCCC")
    await c.join()
    await a.send({"t": "move", "c": "DDDDEEEEFFFF", "ts": (ts := int(time.time())), "s": b64e(keys.sign(f"move:DDDDEEEEFFFF:{ts}".encode()))})
    moved_a = await a.heard_one("moved")
    usage_b = await b.heard_one("usage", moved=True)
    usage_c = await c.heard_one("usage", moved=True)
    check("a project moved to another code: the other relay's apps are told, each what it may see",
          moved_a is not None and usage_b is not None and usage_c is not None
          and usage_b["store"].get("ownerUsed") is None and usage_b["store"]["allowance"] is not None, (usage_b, usage_c))
    await c.close()

    await a.close()
    left = await b.heard_one("left", **{"from": a.id})
    check("an app that goes: the other relay's apps hear it has left", left is not None)

    with_limit = relay_mod.MAX_PER_PROJECT
    relay_mod.MAX_PER_PROJECT = 2
    try:
        x = App(one, keys)
        await x.join()
        refused = False
        try:
            y = App(one, keys)
            await y.join()
            await y.close()
        except Exception:
            refused = True
        check("the limit per project counts connections on every relay", refused)
        await x.close()
    finally:
        relay_mod.MAX_PER_PROJECT = with_limit

    a2 = App(one, keys)
    await a2.join()
    await a2.send({"t": "delete", "ts": (ts := int(time.time())), "s": b64e(keys.sign(f"delete:{ts}".encode()))})
    gone = await b.heard_one("deleted")
    await until(lambda: b.closed, 5)
    check("a project deleted for everyone: the other relay's apps are told, and closed", gone is not None and b.closed)


async def gone_relay(one: TestClient, two: TestClient) -> None:
    print("a relay that goes without a word:")
    keys = Keys.generate()
    a, b = App(one, keys), App(two, keys)
    await a.join()
    await b.join()
    first = next(r for r in relays if r.number == a.id >> 32)
    first._beating.cancel()  # (its heartbeat stops: as if its machine froze or died)
    t0 = time.monotonic()
    left = await b.heard_one("left", 10, **{"from": a.id})
    took = time.monotonic() - t0
    check("its connections are said to have left, by the other relay, once its heartbeat is late",
          left is not None and took < relay_mod.GONE_AFTER + 2, f"{took:.1f} s")
    other = next(r for r in relays if r is not first)
    kept = await other.bus.hget(f"c:{keys.project}", str(a.id))
    check("while it is taken for gone, its entries stay on the list (ignored), so it is reachable the moment it's back:"
          " taking them off raced with its writing them again", kept is not None and await other.relay_of(keys.project, a.id) is None,
          kept)
    first._beating = asyncio.create_task(first._beat())  # (back, as a frozen machine that wakes would be)
    await asyncio.sleep(0.5)
    check("and it is heard from again", first.number not in next(r for r in relays if r is not first)._gone)
    await b.send({"t": "p2p", "d": "to one, after", "to": a.id})
    check("a message to one of its connections reaches it at once (its entry wasn't taken off the list meanwhile)",
          (await a.heard_one("p2p", 3, d="to one, after")) is not None)
    await a.close()
    await b.close()


def up(keys: Keys, text_: str, mid: str) -> dict:
    data = text_.encode()
    return {"t": "up", "id": mid, "d": b64e(data), "s": signed(keys, b"upd:", data)}


async def gaps(one: TestClient, two: TestClient) -> None:
    print("gaps, and the bus away:")
    keys = Keys.generate()
    a, b = App(one, keys, "AAAABBBBCCCC"), App(two, keys)  # (a code: the changes are stored)
    await a.join()
    await b.join()
    second = next(r for r in relays if r.number == b.id >> 32)
    dropped: set[int] = set()
    heard = second._from_bus

    def lossy(channel: str, data: bytes) -> None:  # (the bus loses the passing on of these changes)
        head = json.loads(bytes(data).partition(b"\n")[0])
        if head.get("t") == "up" and head.get("q") in dropped:
            return
        heard(channel, data)
    second.bus.on_message = lossy

    dropped.add(2)
    for i in range(1, 4):
        await a.send(up(keys, f"change {i}", str(i)))
    await until(lambda: len(b.got("up")) >= 3, 5)
    qs = [m["q"] for m in b.got("up")]
    check("a change whose passing on was lost is read from the database once a later one shows the gap", sorted(qs) == [1, 2, 3], qs)

    dropped.add(4)
    await a.send(up(keys, "change 4", "4"))
    t0 = time.monotonic()
    got = await until(lambda: any(m["q"] == 4 for m in b.got("up")), relay_mod.HEAD_CHECK + 3)
    check("the last of a burst, with nothing after it to show the gap, comes with the next head check", got,
          f"{time.monotonic() - t0:.1f} s")
    qs = [m["q"] for m in b.got("up")]
    check("and nothing is passed on twice", len(qs) == len(set(qs)), qs)

    second.bus.on_message = heard
    sync = Keys.generate()  # (a sync-only project, its apps on both relays before the bus goes: a relay whose bus is
    await second.store.set_mode(sync.project, True)  # away a while takes no new connections)
    s1, s2 = App(one, sync), App(two, sync)
    await s1.join()
    await s2.join()
    second.bus.simulate_break()
    await a.send(up(keys, "while away", "5"))
    t0 = time.monotonic()
    got = await until(lambda: any(m["q"] == 5 for m in b.got("up")), 5)
    check("with the bus away from a relay, stored changes still reach its apps (its head check, every second)", got,
          f"{time.monotonic() - t0:.1f} s")

    await a.send(up(keys, "missed", "6"))
    second.bus.heal()
    got = await until(lambda: any(m["q"] == 6 for m in b.got("up")), 5)
    resync = await s2.heard_one("resync", 5)
    check("once the bus is back, what was missed is filled, and sync-only projects' apps are asked to resync",
          got and resync is not None, (got, resync))
    await s1.send({"t": "aw", "d": "after"})
    check("... and its subscriptions are in effect again", (await s2.heard_one("aw", d="after")) is not None)
    qs = [m["q"] for m in b.got("up")]
    check("still nothing passed on twice", len(qs) == len(set(qs)) and sorted(qs) == list(range(1, 7)), qs)
    for x in (a, b, s1, s2):
        await x.close()


async def health(one: TestClient, two: TestClient) -> None:
    print("health:")
    second = relays[1]
    client = one if relays[0].number == second.number else two
    r = await client.get("/api/health")
    check("a relay that is well says so (200)", r.status == 200 and (await r.json())["ok"], r.status)
    keys = Keys.generate()
    a = App(client, keys)
    await a.join()
    second.bus.simulate_break()
    try:
        await asyncio.sleep(relay_mod.BUS_GRACE + 0.3)
        r = await client.get("/api/health")
        body = await r.json()
        check("its bus away for a few seconds: it says so (503, why)", r.status == 503 and "bus" in body.get("why", ""), body)
        refused = False
        try:
            await App(client, keys).join()
        except Exception:
            refused = True
        check("and refuses new connections (a front door sends them to another relay)", refused)
        await a.send({"t": "ping"})
        check("the connections it has stay", (await a.heard_one("pong", 3)) is not None and not a.closed)
    finally:
        second.bus.heal()
    await asyncio.sleep(0.2)
    r = await client.get("/api/health")
    check("once the bus is back, it is well again", r.status == 200, r.status)
    second._db_trouble = True
    r = await client.get("/api/health")
    check("a database that was unavailable at its last try: unwell too", r.status == 503 and "database" in (await r.json()).get("why", ""))
    await until(lambda: not second._db_trouble, 5)  # (its head check finds the database answering)
    b = App(client, keys)
    check("and well again once its head check reaches the database: it takes connections", await b.join() is not None)
    for x in (a, b):
        await x.close()


async def shared_codes(url: str, tmp: Path) -> None:
    print("creation codes, shared:")
    import asyncpg
    c = await asyncpg.connect(url)
    await c.execute("CREATE DATABASE codes")  # (a database of its own: the servers above shared theirs)
    await c.close()
    url = url.rsplit("/", 1)[0] + "/codes"
    codes = tmp / "shared-codes.txt"
    codes.write_text("default 0\nAAAABBBBCCCC 10M one\n")
    with_file = TestServer(make_app(Settings(database=url, bus="memory:codes", data=tmp / "c1", codes=CodeBook(codes))))
    without = TestServer(make_app(Settings(database=url, bus="memory:codes", data=tmp / "c2")))
    async with TestClient(with_file) as one, TestClient(without) as two:
        async def valid(c: TestClient, code: str) -> bool:
            r = await c.post("/api/code", json={"code": code})
            return r.status == 200 and (await r.json()).get("ok") is True
        check("a relay with no codes file takes another's codes", await valid(two, "AAAA-BBBB-CCCC"))
        r = await two.get("/api/info")
        info = await r.json()
        check("and its default (here: no storage without a code)", info.get("creation") == "code" and info.get("defaultAllowance") == 0, info)
        codes.write_text("default 0\nAAAABBBBCCCC 10M one\nDDDDEEEEFFFF 1G two\n")
        t0 = time.monotonic()
        added = False
        while time.monotonic() - t0 < 15 and not added:
            added = await valid(two, "DDDD-EEEE-FFFF")
            await asyncio.sleep(0.3)
        check("a code added to the file is valid on the other relay within seconds", added, f"{time.monotonic() - t0:.1f} s")
        codes.write_text("default 0\nDDDDEEEEFFFF 1G two\n")
        t0 = time.monotonic()
        gone = False
        while time.monotonic() - t0 < 15 and not gone:
            gone = not await valid(two, "AAAA-BBBB-CCCC")
            await asyncio.sleep(0.3)
        check("and one removed is refused there too", gone, f"{time.monotonic() - t0:.1f} s")
        forget = subprocess.run([sys.executable, "-m", "server.cli", "--database", url, "--data", str(tmp / "c3"),
                                 "--forget-codes-file", str(codes)], capture_output=True, text=True, cwd=ROOT)
        t0 = time.monotonic()
        dropped = False
        while time.monotonic() - t0 < 15 and not dropped:
            dropped = not await valid(two, "DDDD-EEEE-FFFF")
            await asyncio.sleep(0.3)
        check("a codes file forgotten (--forget-codes-file): its codes stop counting on the other relay too",
              forget.returncode == 0 and dropped, forget.stdout + forget.stderr)


async def apps(one: TestClient, two: TestClient, tmp: Path, store) -> None:
    print("the desktop helper, on either relay:")
    for sync_only in (False, True):
        keys = Keys.generate()
        if sync_only:
            await store.set_mode(keys.project, True)
        url1, url2 = (str(c.make_url("")).rstrip("/") for c in (one, two))
        r1 = Replica(tmp / f"r1-{sync_only}", keys, url1)
        r2 = Replica(tmp / f"r2-{sync_only}", keys, url2)
        tasks = [asyncio.create_task(r.run()) for r in (r1, r2)]
        try:
            await asyncio.wait_for(asyncio.gather(r1.synced.wait(), r2.synced.wait()), 20)
            add_line(r1, "from the first relay")  # (the file is made once, then both write to it)
            assert await until(lambda: "from the first relay" in text(r2), 15), "the file didn't reach the second relay's app"
            add_line(r2, "from the second relay")
            add_line(r1, "and the first again")
            both = await until(lambda: all(w in text(r) for w in ("from the first relay", "from the second relay", "and the first again")
                                           for r in (r1, r2)), 15)
            check(f"{'a sync-only' if sync_only else 'a stored'} project: apps on different relays end with each other's changes",
                  both, (text(r1), text(r2)))
            if sync_only:  # a relay's link to the others lost while one app edits: once it's back, the other catches up at
                second = relays[1]  # once (resync, answered without waiting: they last exchanged moments ago)
                second.bus.simulate_break()
                add_line(r1, "while the second relay's bus is away")
                await asyncio.sleep(1)
                second.bus.heal()
                t0 = time.monotonic()
                got = await until(lambda: "while the second relay's bus is away" in text(r2), 15)
                check("a sync-only project, one relay's bus away a moment: once it's back, the other app has the change within seconds",
                      got and time.monotonic() - t0 < 8, f"{time.monotonic() - t0:.1f} s")
            if not sync_only:
                second = relays[1]
                second.bus.simulate_break()
                try:
                    add_line(r1, "while the second relay's bus is away")
                    got = await until(lambda: "while the second relay's bus is away" in text(r2), 10)
                    check("a stored project, one relay's bus away: its app still gets the other's changes (from the database)", got,
                          text(r2))
                finally:
                    second.bus.heal()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def main() -> int:
    try:
        pg.bindir()
    except SystemExit:
        print("skipped: no PostgreSQL programs (scripts/pg.py)")
        return 0
    with tempfile.TemporaryDirectory() as t, pg.running() as url:
        tmp = Path(t)
        codes = tmp / "codes.txt"
        codes.write_text("default 0\nAAAABBBBCCCC 10M one\nDDDDEEEEFFFF 10M two\n")

        def server(name: str) -> TestServer:
            return TestServer(make_app(Settings(database=url, bus="memory:check-relays", data=tmp / name, codes=CodeBook(codes))))
        async with TestClient(server("one")) as one, TestClient(server("two")) as two:
            await messages(one, two)
            await gone_relay(one, two)
            await gaps(one, two)
            await health(one, two)
            codes.write_text("default 10M\n")  # (the apps below have no code)
            for r in relays:
                r.policy.codes.load()
            await apps(one, two, tmp, relays[0].store)
        await shared_codes(url, tmp)
    return summary("relays")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
