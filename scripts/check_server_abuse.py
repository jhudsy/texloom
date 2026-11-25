#!/usr/bin/env python3
"""What the security review of 2026-11-24 found the server let an outsider do, checked against a
throwaway server (SQLite, nothing online):

- revocations: anyone can make a keypair, so a project's owner can be anyone. Each stored
  revocation is a row that stores nothing, so a new one on a project without a creation code
  comes out of the server-wide budget for such rows (store.META_BURST, then META_RATE a second),
  not just the first: a storage mode set once used to make the next thousand free, per project;
- TeX Live: the package index comes from a mirror, which mustn't be able to name a file outside
  the cache (../../etc/texloom/texloom.toml) for the server to serve or delete; and anyone can
  ask for any file, so the packages one address (an IPv6 /64) has fetched are counted;
- ids: each id has four base64 spellings, and only the one clients use is accepted, so a record
  kept by id (a revocation, a deletion) can't be sidestepped with another;
- connections: one must say hello first and soon; one address (an IPv6 /64) and one project
  have a limited number;
- what's relayed unsigned (awareness, peer-to-peer messages) needs only a project's id: its
  sender is paced, presence past a small pace is dropped, and it never gets a member cut off;
- memory: every catch-up goes in pages, an address has only its share of the memory budget,
  and an arXiv import takes little of it;
- private servers: a project is bound to a creation code by a signed write, not by connecting;
- allowances: writes under way at once under one code can't together pass it;
- replays: a change from before an eviction, or a big one compacted long ago, isn't stored again;
- project lists: retired ids are counted;
- request bodies: only uploads may be large.

    uv run --extra server python scripts/check_server_abuse.py
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import os
import tarfile
import zipfile
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from aiohttp import WSMsgType, WSServerHandshakeError, web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

from checklib import check, summary  # noqa: E402
import server.lists  # noqa: E402
import server.relay as relay_mod  # noqa: E402
import server.store as store_mod  # noqa: E402
from server import arxiv  # noqa: E402
from server.app import Settings, make_app  # noqa: E402
from server.files import FolderFiles  # noqa: E402
from server.policy import CodeBook, MiB  # noqa: E402
from server.store import META_BURST, Store  # noqa: E402
from server.texlive import YEAR, TexLive, parse_tlpdb, safe_path  # noqa: E402
from texloom.crypto import b64d, b64e  # noqa: E402


def keypair() -> tuple[Ed25519PrivateKey, str]:
    sk = Ed25519PrivateKey.generate()
    return sk, b64e(sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


class patched:
    """Set module constants for a while (smaller limits, so a check is quick)."""

    def __init__(self, module, **values):
        self.module, self.values, self.old = module, values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.old[k] = getattr(self.module, k, None)
            setattr(self.module, k, v)

    def __exit__(self, *_):
        for k, v in self.old.items():
            setattr(self.module, k, v)


def client(tmp: Path, codes: str | None = None, **settings) -> TestClient:
    path = None
    if codes is not None:
        path = tmp / "codes.txt"
        path.write_text(codes)
    app = make_app(Settings(database=f"sqlite:///{tmp}/t.db", data=tmp, codes=CodeBook(path), **settings))
    return TestClient(TestServer(app))


async def join(c: TestClient, pid: str, code: str | None = None, address: str | None = None, pages: bool = True):
    """A connection that has said hello and been caught up; and the reply."""
    ws = await c.ws_connect(f"/ws/{pid}", max_msg_size=64 * MiB, headers={"X-Real-IP": address} if address else None)
    await ws.send_json({"t": "hello", "since": 0, "app": "web", **({"pages": 1} if pages else {}), **({"code": code} if code else {})})
    reply = await ws.receive_json(timeout=10)
    more = reply.get("more")
    while more:
        more = (await ws.receive_json(timeout=10)).get("more")
    return ws, reply


async def until_type(ws, t: str, timeout: float = 10) -> tuple[dict | None, list]:
    """Messages until one of type `t` (None if the connection closed first), and those before it."""
    seen = []
    try:
        while True:
            m = await ws.receive(timeout=timeout)
            if m.type != WSMsgType.TEXT:
                return None, seen
            j = json.loads(m.data)
            if j.get("t") == t:
                return j, seen
            seen.append(j)
    except (asyncio.TimeoutError, TimeoutError):
        return None, seen


def up(sk: Ed25519PrivateKey, data: bytes, mid: str = "1") -> dict:
    return {"t": "up", "id": mid, "d": b64e(data), "s": b64e(sk.sign(b"upd:" + data))}


def alias(pid: str) -> str:
    """Another spelling of the same 32 bytes: the last character's two unused bits set."""
    abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    return pid[:-1] + abc[abc.index(pid[-1]) ^ 1]


async def revocations(tmp: Path) -> None:
    """Three made-up projects, each with a storage mode, then 300 revocations each (half the
    budget, more than the connection's own rate allows at once): no more than the budget is stored."""
    codes = tmp / "codes.txt"
    codes.write_text("default 0\n")  # nothing may be stored without a code
    app = make_app(Settings(database=f"sqlite:///{tmp}/t.db", data=tmp, codes=CodeBook(codes)))
    accepted = 0
    async with TestClient(TestServer(app)) as c:
        for _ in range(3):
            sk, pid = keypair()
            ws, _ = await join(c, pid)
            ts = int(time.time())
            await ws.send_json({"t": "mode", "m": "relay", "ts": ts, "s": b64e(sk.sign(f"mode:relay:{ts}".encode()))})
            for _ in range(META_BURST // 2):
                p = os.urandom(32)
                await ws.send_json({"t": "revoke", "p": b64e(p), "s": b64e(sk.sign(b"revoke:" + p))})
            await ws.send_json({"t": "usage"})
            while (m := await ws.receive_json())["t"] != "usage":
                accepted += m["t"] == "rv"
            await ws.close()
    rows = sqlite3.connect(tmp / "t.db").execute("SELECT COUNT(*) FROM revoked").fetchone()[0]
    check("the revocations told to clients are the ones stored", rows == accepted, (rows, accepted))
    check(f"revocations without a code come out of the budget (at most about {META_BURST})", rows <= META_BURST + 5, rows)


async def texlive(tmp: Path) -> None:
    """An index naming a file outside the cache: never listed, never served."""
    bad = " RELOC/tex/../../../secret.toml\n"
    record = f"name evil\ncontainerchecksum abc\ncontainersize 10\nrunfiles size=1\n{bad} RELOC/tex/latex/evil/ok.sty\n"
    check("the index keeps only paths under the cache", [r[3] for r in parse_tlpdb(record)] == [["tex/latex/evil/ok.sty"]])
    check("paths that could leave it are refused", not any(safe_path(p) for p in ("../x", "tex/../../x", "/etc/x", "a//b", "./a", "a\\b")))
    root = tmp / "texlive"
    root.mkdir()
    (tmp / "secret.toml").write_text("admin_token = 'SECRET'\n")
    tl = TexLive(root)
    tl._write_index(record, "https://mirror.example/")
    tl.db.execute("INSERT INTO files VALUES (?,?,?)", ("secret.toml", "tex/../../../secret.toml", "evil"))  # an index from before
    (root / "texmf" / "tex").mkdir(parents=True)

    async def no_network(pkg: str) -> None:
        raise RuntimeError("no network in this check")
    tl.install = no_network
    app = web.Application()
    app.router.add_get("/texlive/{year}/{fmt}/{name}", tl.handle)
    async with TestClient(TestServer(app)) as c:
        r = await c.get(f"/texlive/{YEAR}/26/secret.toml")
        check("a file outside the cache isn't served", r.status == 404 and "SECRET" not in await r.text(), r.status)
        r = await c.get(f"/texlive/{YEAR}/{'9' * 5000}/a.sty")
        check("a format number too long to read is just a miss", r.status == 404, r.status)


async def texlive_fetches(tmp: Path) -> None:
    """Many big packages asked for from one /64: only so many bytes an hour are fetched for it."""
    big = 200 * 1024 * 1024
    record = "\n\n".join(f"name p{i}\ncontainerchecksum abc\ncontainersize {big}\nrunfiles size=1\n RELOC/tex/latex/p{i}/p{i}.sty"
                         for i in range(8))
    root = tmp / "texlive"
    root.mkdir()
    tl = TexLive(root)
    tl._write_index(record, "https://mirror.example/")
    tried = []

    async def no_network(pkg: str) -> None:
        tried.append(pkg)
        raise RuntimeError("no network in this check")
    tl.install = no_network
    app = web.Application()
    app.router.add_get("/texlive/{year}/{fmt}/{name}", tl.handle)
    async with TestClient(TestServer(app)) as c:
        for i in range(7):
            await c.get(f"/texlive/{YEAR}/26/p{i}.sty", headers={"X-Real-IP": f"2001:db8:5:6::{i + 1}"})
        check("one /64 can't have every package fetched (5 of 200 MB, of 7 asked for)", len(tried) == 5, len(tried))
        await c.get(f"/texlive/{YEAR}/26/p7.sty", headers={"X-Real-IP": "2001:db8:5:7::1"})
        check("another address still can", len(tried) == 6, len(tried))


async def ids(tmp: Path) -> None:
    """Another spelling of a project's or list's id is refused everywhere ids come in."""
    _, pid = keypair()
    other = alias(pid)
    check("(the other spelling decodes to the same key)", other != pid and b64d(other) == b64d(pid))
    async with client(tmp) as c:
        try:
            ws = await c.ws_connect(f"/ws/{other}")
            await ws.close()
            status = 101
        except WSServerHandshakeError as e:
            status = e.status
        check("a WebSocket to another spelling is refused", status == 400, status)
        r = await c.get(f"/blob/{other}/{'0' * 32}")
        check("so is a stored file under it", r.status == 400, r.status)
        r = await c.get(f"/api/list/{other}")
        check("and a project list", r.status == 400, r.status)
        r = await c.get(f"/api/list/{pid}")
        check("(the id clients use is fine)", r.status == 404, r.status)


async def connections(tmp: Path) -> None:
    """Hello first, and soon; per-/64 and per-project limits."""
    with patched(relay_mod, HELLO_WAIT=1.0, MAX_PER_PROJECT=4):
        async with client(tmp) as c:
            _, pid = keypair()
            ws = await c.ws_connect(f"/ws/{pid}")
            m = await ws.receive(timeout=5)
            check("a connection that says nothing is closed", m.type in (WSMsgType.CLOSE, WSMsgType.CLOSED), m.type)
            ws = await c.ws_connect(f"/ws/{pid}")
            await ws.send_json({"t": "aw", "d": "x"})
            m = await ws.receive(timeout=5)
            check("one whose first message isn't its hello is closed", m.type in (WSMsgType.CLOSE, WSMsgType.CLOSED), m.type)
            open_ = []
            refused = None
            for i in range(relay_mod.MAX_PER_ADDRESS + 1):
                _, p = keypair()
                try:
                    open_.append(await c.ws_connect(f"/ws/{p}", headers={"X-Real-IP": f"2001:db8:1:2::{i + 1:x}"}))
                except WSServerHandshakeError as e:
                    refused = e.status
            check(f"one IPv6 /64 gets {relay_mod.MAX_PER_ADDRESS} connections, no more", len(open_) == relay_mod.MAX_PER_ADDRESS
                  and refused == 429, (len(open_), refused))
            try:
                open_.append(await c.ws_connect(f"/ws/{pid}", headers={"X-Real-IP": "2001:db8:1:3::1"}))
                ok = True
            except WSServerHandshakeError:
                ok = False
            check("another /64 still connects", ok)
            for w in open_:
                await w.close()
            n = 0
            many = []
            for i in range(6):
                try:
                    many.append(await c.ws_connect(f"/ws/{pid}", headers={"X-Real-IP": f"192.0.2.{i + 1}"}))
                    n += 1
                except WSServerHandshakeError:
                    pass
            check("a project has a limited number of connections", n == 4, n)
            for w in many:
                await w.close()


async def unsigned(tmp: Path) -> None:
    """Unsigned messages relayed to a room: paced, presence dropped past its pace, never a cut-off."""
    async with client(tmp) as c:
        _, pid = keypair()
        victim, _ = await join(c, pid, address="192.0.2.1")  # (then reads nothing for a while)
        attackers = [(await join(c, pid, address=f"192.0.2.{10 + i}"))[0] for i in range(6)]
        blob = "A" * (4 * 1024 * 1024 + 512 * 1024)
        await asyncio.gather(*(a.send_json({"t": "p2p", "d": blob}) for a in attackers))
        await asyncio.sleep(2)
        try:
            await victim.send_json({"t": "usage"})
            got, _ = await until_type(victim, "usage")
        except ConnectionError:  # (cut off)
            got = None
        check("a member flooded with unsigned messages isn't cut off", got is not None)
        for a in attackers:
            await a.close()
        await victim.close()

        _, pid = keypair()
        listener, _ = await join(c, pid, address="192.0.2.2")
        talker, _ = await join(c, pid, address="192.0.2.3")
        for i in range(550):
            await talker.send_json({"t": "aw", "d": "x" * 1000})
        await talker.send_json({"t": "usage"})
        await until_type(talker, "usage")
        await asyncio.sleep(0.5)
        heard = 0
        try:
            while True:
                m = await listener.receive_json(timeout=1)
                heard += m.get("t") == "aw"
        except (asyncio.TimeoutError, TimeoutError):
            pass
        check("presence past its pace is dropped (550 sent)", 100 < heard < 400, heard)
        await listener.close()
        await talker.close()

    with patched(relay_mod, UNSIGNED_RATE=256 * 1024, UNSIGNED_BURST=256 * 1024):
        async with client(tmp) as c:
            _, pid = keypair()
            readers = [(await join(c, pid, address=f"192.0.2.{20 + i}"))[0] for i in range(2)]

            async def read(ws):
                try:
                    while True:
                        await ws.receive(timeout=30)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
            tasks = [asyncio.create_task(read(r)) for r in readers]
            sender, _ = await join(c, pid, address="192.0.2.30")
            start = time.monotonic()
            for _ in range(6):  # 600 KB to each of two: 1.2 MB delivered, at 256 KB a second
                await sender.send_json({"t": "p2p", "d": "B" * 100_000})
            await sender.send_json({"t": "usage"})
            await until_type(sender, "usage", 30)
            took = time.monotonic() - start
            check("a connection relaying unsigned messages is paced", took > 2.5, round(took, 1))
            for t in tasks:
                t.cancel()
            for w in [*readers, sender]:
                await w.close()


async def memory(tmp: Path) -> None:
    """Every long log goes in pages; an address has only its share of the budget; arXiv takes little."""
    async with client(tmp) as c:
        sk, pid = keypair()
        ws, _ = await join(c, pid)
        for i in range(2):
            await ws.send_json(up(sk, os.urandom(5 * 1024 * 1024), str(i)))
            await until_type(ws, "ack", 30)
        await ws.close()
        ws, reply = await join(c, pid, pages=False)
        check("a hello without pages still gets a long log in pages", reply.get("t") == "log" and reply.get("more") is True,
              (reply.get("t"), reply.get("more"), len(reply.get("entries", []))))
        await ws.close()
    relay = relay_mod.Relay(relay_mod.Policy(None))
    one, two, other = relay_mod.Conn(1, "192.0.2.1"), relay_mod.Conn(2, "192.0.2.1"), relay_mod.Conn(3, "192.0.2.2")
    share = getattr(relay_mod, "ADDRESS_MEMORY", relay.memory.limit)
    await relay.take_memory(one, share) if hasattr(relay, "take_memory") else await relay.memory.take(share)
    waits = asyncio.create_task(relay.take_memory(two, MiB) if hasattr(relay, "take_memory") else relay.memory.take(MiB))
    await asyncio.sleep(0.1)
    check("an address holding its share of the memory budget waits for more", not waits.done() and share < relay.memory.limit,
          (waits.done(), share >> 20))
    if hasattr(relay, "take_memory"):
        await asyncio.wait_for(relay.take_memory(other, MiB), 1)
        relay.give_memory(one, share)
        await asyncio.wait_for(waits, 1)
        check("others don't, and it goes on when the share is given back", True)
    waits.cancel()
    check("an arXiv import takes little of the budget", arxiv.IMPORT_MEMORY <= 32 * MiB, arxiv.IMPORT_MEMORY >> 20)
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as t:
        for name, data in (("main.tex", b"\\documentclass{article}"), ("../evil.tex", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    src, out = io.BytesIO(gzip.compress(b.getvalue())), io.BytesIO()
    try:
        arxiv.to_zip(src, out)
        names = zipfile.ZipFile(out).namelist()
    except TypeError:  # (one that takes the whole archive in memory)
        names = None
    check("an arXiv archive is unpacked from a file, a piece at a time", names == ["main.tex"], names)


async def private(tmp: Path) -> None:
    """On a private server, connecting with a code binds nothing; the first signed write does."""
    code = "ABCD-EFGH-JKMN"
    async with client(tmp, f"default 0\n{code} 1G\n", private=True) as c:
        sk, pid = keypair()
        ws, reply = await join(c, pid, code=code)
        db = sqlite3.connect(tmp / "t.db")
        owner = db.execute("SELECT owner FROM projects WHERE project=?", (pid,)).fetchone()
        check("connecting with a code doesn't bind a project to it", reply.get("t") == "log" and owner is None, owner)
        await ws.send_json(up(sk, b"hello"))
        got, _ = await until_type(ws, "ack")
        owner = db.execute("SELECT owner FROM projects WHERE project=?", (pid,)).fetchone()
        check("its first signed write does", got is not None and owner == ("ABCDEFGHJKMN",), owner)
        await ws.close()


async def quota(tmp: Path) -> None:
    """Two first writes at once under one code with room for one: one is stored."""
    code = "QRST-VWXY-Z234"
    async with client(tmp, f"default 0\n{code} 3000\n") as c:
        (sa, pa), (sb, pb) = keypair(), keypair()
        wa, _ = await join(c, pa, code=code)
        wb, _ = await join(c, pb, code=code)
        await asyncio.gather(wa.send_json(up(sa, os.urandom(2000))), wb.send_json(up(sb, os.urandom(2000))))
        await asyncio.gather(until_type(wa, "ack", 3), until_type(wb, "ack", 3))
        rows = sqlite3.connect(tmp / "t.db").execute("SELECT COUNT(*) FROM log").fetchone()[0]
        check("writes at once under one code can't together pass its allowance", rows == 1, rows)
        await wa.close()
        await wb.close()


async def replays(tmp: Path) -> None:
    """Old signed changes replayed: after an eviction, and a big one compacted long ago."""
    store = await Store.open(f"sqlite:{tmp / 't.db'}", FolderFiles(tmp / "files"), tmp, serving=True)
    try:
        _, a = keypair()
        await store.register(a, "default", "check")
        old = os.urandom(100)
        await store.append(a, old, b"s" * 64)
        await store.evict(a)
        check("a change from before an eviction isn't stored again", await store.duplicate(a, old) == 0)
        with patched(store_mod, GONE_KEEP=2):
            _, b = keypair()
            await store.register(b, "default", "check")
            big = os.urandom(70 * 1024)
            await store.append(b, big, b"s" * 64)
            for i in range(5):
                await store.append(b, os.urandom(10), b"s" * 64)
            await store.snapshot(b, 6, b"snap", b"s" * 64)
            check("a big change compacted long ago isn't stored again", await store.duplicate(b, big) == 0)
    finally:
        await store.close()


async def lists(tmp: Path) -> None:
    """Retired list ids are counted: past the limit, no new lists."""
    with patched(server.lists, MAX_RETIRED_TOTAL=3):
        async with client(tmp) as c:
            statuses = []
            for _ in range(4):
                sk, lid = keypair()
                data = b"list"
                r = await c.put(f"/api/list/{lid}", data=data, headers={
                    "X-Seq": "1", "X-Sig": b64e(sk.sign(f"list:{lid}:1:".encode() + data))})
                statuses.append(r.status)
                await c.delete(f"/api/list/{lid}", headers={"X-Sig": b64e(sk.sign(f"listdel:{lid}".encode()))})
            check("lists can't be made and retired without end", statuses == [200, 200, 200, 507], statuses)


async def bodies(tmp: Path) -> None:
    """Only uploads may send a large body; the page's policy covers worker scripts too."""
    async with client(tmp) as c:
        r = await c.post("/api/code", data=json.dumps({"code": "x" * 100_000}), headers={"Content-Type": "application/json"})
        check("a creation code check with a large body is refused", r.status == 413, r.status)

        async def chunks():
            for _ in range(40):
                yield b"x" * 10_000
        _, lid = keypair()
        r = await c.put(f"/api/list/{lid}", data=chunks(), headers={"X-Seq": "1", "X-Sig": b64e(b"s" * 64)})
        check("so is a project list larger than a list may be, sent without its length", r.status == 413, r.status)
        r = await c.get("/core/busytex/busytex_worker.js")
        check("worker scripts get the page's policy", "worker-src" in r.headers.get("Content-Security-Policy", "")
              and r.headers.get("Cross-Origin-Opener-Policy") == "same-origin", dict(r.headers))


async def main() -> int:
    for name, part in (("Revocations", revocations), ("TeX Live", texlive), ("TeX Live fetches", texlive_fetches), ("Ids", ids),
                       ("Connections", connections), ("Unsigned messages", unsigned), ("Memory", memory),
                       ("Private servers", private), ("Allowances", quota), ("Replays", replays), ("Project lists", lists),
                       ("Request bodies and headers", bodies)):
        print(f"{name}:", flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                await part(Path(tmp))
            except Exception as e:  # (a server without what's checked, say): this part fails, the rest go on
                check(f"{name.lower()}: checked to the end", False, repr(e)[:300])
    return summary("server abuse")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
