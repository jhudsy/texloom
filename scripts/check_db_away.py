#!/usr/bin/env python3
"""The database away for a while (PostgreSQL restarting, failing over, unreachable), and a server that accepts
connections but never answers (hung, or its machine frozen). Found in the Docker lab (scripts/rehearse_relays.py):
a change sent while the database was away was dropped without a word, its app didn't send it again until it
reconnected, and its collaborators missed everything it did meanwhile; and an app sent to a frozen server waited
for good.

Checked with a throwaway server whose database is made to fail as an unreachable one does (db.unavailable):
- a change sent meanwhile is passed on to the collaborators online at once (live, not stored), and its sender is
  asked to try again (its connection closed with 1013), so it sends it again once it has reconnected;
- a new connection whose hello can't be answered is asked to try again too;
- the desktop helper's sync engine, through an outage: its change is stored once the database is back, without
  a restart, and while the database is away it tries less and less often (its wait grows, rather than starting
  again at a second each time the server accepts it);
- a server that accepts a connection but never answers is given up on after the connect timeout, and tried again.

    uv run --extra server python scripts/check_db_away.py
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_restore import add_line, text  # noqa: E402
from checklib import check, summary, until  # noqa: E402

from aiohttp import WSMsgType, WSServerHandshakeError  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import texloom.replica as replica_mod  # noqa: E402
from server.app import Settings, make_app  # noqa: E402
from server.store import Store  # noqa: E402
from texloom.crypto import Keys, b64e  # noqa: E402
from texloom.replica import Replica  # noqa: E402

away = False  # the database is unreachable while this is set
slow = 0.0  # seconds each change's check waits on the database (a primary cut off: statements hang, then fail)


def failing(name: str, error: Exception):
    real = getattr(Store, name)

    async def call(self, *a, **kw):
        if away:
            raise error
        return await real(self, *a, **kw)
    return call


# as asyncpg raises them: a refused connection, and (what the lab saw, a primary's name gone) a name that doesn't resolve
_duplicate = Store.duplicate


async def _slow_duplicate(self, *a, **kw):
    if slow:
        await asyncio.sleep(slow)
    return await _duplicate(self, *a, **kw)


Store.duplicate = _slow_duplicate  # type: ignore[method-assign]

for _name, _error in (("touch", ConnectionRefusedError(111, "Connect call failed")),
                      ("catch_up_start", ConnectionRefusedError(111, "Connect call failed")),
                      ("duplicate", socket.gaierror(-2, "Name or service not known")),
                      ("heads", ConnectionRefusedError(111, "Connect call failed"))):
    setattr(Store, _name, failing(_name, _error))


async def raw(c: TestClient, pid: str):
    ws = await c.ws_connect(f"/ws/{pid}")
    await ws.send_json({"t": "hello", "since": 0, "app": "desktop", "v": "0.4.66", "pages": 1, "ka": 1})
    return ws


async def until_type(ws, t: str, secs: float = 5):
    end = time.monotonic() + secs
    while (left := end - time.monotonic()) > 0:
        try:
            m = await ws.receive(timeout=left)
        except TimeoutError:
            return None
        if m.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            return {"t": "closed", "code": ws.close_code}
        if m.type == WSMsgType.TEXT and json.loads(m.data).get("t") == t:
            return json.loads(m.data)
    return None


async def relay_side(c: TestClient) -> None:
    global away
    keys = Keys.generate()
    a, b = await raw(c, keys.project), await raw(c, keys.project)
    await until_type(a, "log")
    await until_type(b, "log")
    away = True
    data = b"a change made while the database is away"
    await a.send_json({"t": "up", "id": "1", "d": b64e(data), "s": b64e(keys.sign(b"upd:" + data))})
    heard = await until_type(b, "up")
    check("a change sent while the database is away reaches the collaborators online at once (live: q 0)",
          heard is not None and heard.get("q") == 0, heard)
    closed = await until_type(a, "ack")
    check("and its sender isn't told it's stored, but asked to try again (closed, 1013)", closed == {"t": "closed", "code": 1013}, closed)
    try:  # (refused at once, 503, once the relay knows the database is away; else its hello is answered with 1013)
        late = await raw(c, keys.project)
        answer = await until_type(late, "log")
    except WSServerHandshakeError as e:
        late, answer = None, {"t": "refused", "status": e.status}
    check("a new connection is asked to try again too (refused, 503, or its hello closed, 1013)",
          answer in ({"t": "closed", "code": 1013}, {"t": "refused", "status": 503}), answer)
    away = False
    global slow
    for _ in range(50):  # (the relay is well again once its head check reaches the database: a second at most)
        if (await c.get("/api/health")).status == 200:
            break
        await asyncio.sleep(0.2)
    slow = 6.0  # (more than the keep-alive's wait: an app hearing nothing for that long gives up)
    c2 = await raw(c, keys.project)
    await until_type(c2, "log")
    data = b"a change while the database is slow"
    await c2.send_json({"t": "up", "id": "2", "d": b64e(data), "s": b64e(keys.sign(b"upd:" + data))})
    waits, ack = 0, None
    end = time.monotonic() + 12
    while time.monotonic() < end and ack is None:
        try:
            m = await c2.receive(timeout=end - time.monotonic())
        except TimeoutError:
            break
        if m.type != WSMsgType.TEXT:
            break
        j = json.loads(m.data)
        waits += j.get("t") == "wait"
        if j.get("t") == "ack":
            ack = j
    slow = 0.0
    check("a change waiting on a slow database: its app is told to wait meanwhile (so it doesn't give up), then acknowledged",
          ack is not None and waits >= 1 and not c2.closed, (waits, ack))
    await c2.close()
    for ws in (b, late):
        if ws is not None:
            await ws.close()


async def desktop(c: TestClient, tmp: Path) -> None:
    global away
    url = str(c.make_url("")).rstrip("/")
    keys = Keys.generate()
    r = Replica(tmp / "alice", keys, url)
    tries = 0
    connect = r._opened

    async def counted(ws):
        nonlocal tries
        tries += 1
        await connect(ws)
    r._opened = counted  # type: ignore[method-assign]
    task = asyncio.create_task(r.run())
    try:
        await asyncio.wait_for(r.synced.wait(), 20)
        add_line(r, "before")
        await until(lambda: not r.outbox, 10)
        away = True
        tries = 0
        add_line(r, "while away")
        await asyncio.sleep(12)
        check("desktop: while the database is away it tries less and less often (its wait grows)", 2 <= tries <= 5, f"{tries} tries in 12 s")
        away = False
        stored = await until(lambda: not r.outbox, 40)
        newcomer = Replica(tmp / "bob", keys, url)
        t2 = asyncio.create_task(newcomer.run())
        await asyncio.wait_for(newcomer.synced.wait(), 20)
        got = await until(lambda: "while away" in text(newcomer), 10)
        check("desktop: once the database is back, the change is sent again and stored, with no restart", stored and got,
              repr(text(newcomer)))
        t2.cancel()
        await asyncio.gather(t2, return_exceptions=True)
    finally:
        away = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def silent(tmp: Path) -> None:
    accepted = 0
    held = []

    async def accept(reader, writer):
        nonlocal accepted
        accepted += 1
        held.append(writer)  # (never answered, never closed: a hung server)
    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    old = replica_mod.CONNECT_TIMEOUT
    replica_mod.CONNECT_TIMEOUT = 1
    r = Replica(tmp / "silent", Keys.generate(), f"http://127.0.0.1:{port}")
    task = asyncio.create_task(r.run())
    try:
        await asyncio.sleep(5)
        check("desktop: a server that accepts but never answers is given up on, and tried again", accepted >= 2, f"{accepted} connections in 5 s")
    finally:
        replica_mod.CONNECT_TIMEOUT = old
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for w in held:
            w.close()
        server.close()


async def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        async with TestClient(TestServer(make_app(Settings(database=f"sqlite:///{tmp}/t.db", data=tmp)))) as c:
            print("relay:")
            await relay_side(c)
            print("desktop helper:")
            await desktop(c, tmp)
        await silent(tmp)
    return summary("database away")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
