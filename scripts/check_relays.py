#!/usr/bin/env python3
"""Several relays at once (FUTURE_ARCHITECTURE.md): two servers in one process, sharing a PostgreSQL and a bus
(the in-process one, named: memory:NAME), with apps on each. What one relay's apps send reaches the other's:

- stored changes, presence, peer-to-peer messages to everyone and to one connection on the other relay (signed
  or not), "left" when an app goes, revocations, a project moved to another code (each app told what it may
  see), and a deletion (the other relay's apps told, and closed);
- who is connected is counted across relays (the catch-up's "peers", and the limit per project);
- a relay that goes without a word (its heartbeat stops) is noticed by the other, whose apps hear that its
  connections have left;
- the desktop helper's sync engine, on either relay: a stored project, and a sync-only one (nothing on the
  server: the apps fill each other in, across the relays).

The bus over Valkey is checked by scripts/check_bus.py, and real relays on separate machines by the lab
(scripts/rehearse_relays.py).

    uv run --extra server python scripts/check_relays.py     # needs PostgreSQL's programs (scripts/pg.py)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
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

relay_mod.BEAT, relay_mod.GONE_AFTER = 0.2, 1.0  # (quicker than in use)
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
    first._beating = asyncio.create_task(first._beat())  # (back, as a frozen machine that wakes would be)
    await asyncio.sleep(0.5)
    check("and it is heard from again", first.number not in next(r for r in relays if r is not first)._gone)
    await a.close()
    await b.close()


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
            codes.write_text("default 10M\n")  # (the apps below have no code)
            for r in relays:
                r.policy.codes.load()
            await apps(one, two, tmp, relays[0].store)
    return summary("relays")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
