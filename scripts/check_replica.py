#!/usr/bin/env python3
"""Runs texloom/replica.py through scripts/replica-vectors.json, the cases web/scripts/check-replica.mjs
runs web/src/replica.ts through: what a replica does with the server's messages (catching up,
signatures, uploads, revocations, refusals, peers) must be the same in both apps.

Each case gets a fresh replica, with a stand-in connection that records what it sends and a
stand-in server for files and uploads. Nothing touches the network. Besides the server's messages,
a case's steps can edit a file here, drop and reopen the connection, acknowledge or refuse the
last change sent, add a file, and fetch one.

    uv run python scripts/check_replica.py [words in a case's name]
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp
from pycrdt import Doc, Map, Text

from texloom.crypto import Keys, b64d
from texloom.packing import unpack
from texloom import replica as replica_module
from texloom.replica import Replica

logging.disable(logging.WARNING)  # the cases are full of things a replica warns about
replica_module.ASK_ONE = 0.4  # how long a peer asked for a file has to answer, and in what order peers are asked: short, and fixed
replica_module.ask_order = sorted
SERVER = "http://test"


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send_json(self, m: dict) -> None:
        self.sent.append(m)

    async def close(self) -> None:
        self.closed = True


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status, self.body = status, body

    async def json(self, **_: object) -> object:
        return json.loads(self.body or b"null")

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientError(f"status {self.status}")

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def read(self) -> bytes:
        return self.body


class FakeHttp:
    """The server's files: what it answers for each (the case's `uploads`), and what it's sent
    (answered as the case's `put` says), checked as the server would: signed by a writer, and the
    content the id names."""

    def __init__(self, c: dict, keys: Keys) -> None:
        self.uploads, self.answer, self.keys = c["uploads"], c.get("put", {"status": 200}), keys
        self.received: list[dict] = []

    def get(self, url: str, **_: object) -> FakeResponse:
        u = self.uploads.get(url.rsplit("/", 1)[-1])
        if u is None:
            return FakeResponse(404, b"")
        return FakeResponse(u["status"], bytes.fromhex(u.get("body", "")))

    def put(self, url: str, data: bytes = b"", headers: dict | None = None, **_: object) -> FakeResponse:
        bid, h = url.rsplit("/", 1)[-1], headers or {}
        try:
            signed = self.keys.verify(f"blob:{bid}:".encode() + data, b64d(h.get("X-Sig", "")))
            right = self.keys.blob_id(self.keys.decrypt(data)) == bid
        except Exception:  # noqa: BLE001 - counts as not ok
            signed = right = False
        self.received.append({"id": bid, "output": h.get("X-Kind") == "output", "ok": signed and right})
        body = json.dumps({"code": self.answer["code"]}).encode() if "code" in self.answer else b""
        return FakeResponse(self.answer["status"], body)

    def delete(self, *_: object, **__: object) -> FakeResponse:
        return FakeResponse(204, b"")


def sent_tags(r: Replica, ws: FakeWS) -> list[str]:
    """What was sent: a message's type, and for peer-to-peer messages what kind they are inside."""
    out = []
    for m in ws.sent:
        if m.get("t") == "p2p":
            try:
                k = str(json.loads(r.keys.decrypt(b64d(m["d"]))).get("k"))
                # who a file was asked of (or refused to), the new part of asking one peer at a time
                out.append("p2p:" + k + (f"->{m['to']}" if k in ("breq", "bno") and m.get("to") is not None else ""))
            except Exception:  # noqa: BLE001
                out.append("p2p:?")
        else:
            out.append(str(m.get("t")))
    return sorted(out)


def sent_files(r: Replica, socks: list[FakeWS]) -> dict:
    """The files the changes sent to the server make, applied to an empty document."""
    d = Doc()
    files = d.get("files", type=Map)
    for ws in socks:
        for m in ws.sent:
            if m.get("t") == "up":
                d.apply_update(unpack(r.keys.decrypt(b64d(m["d"]))))
    return {k: str(v) for k, v in files.items()}


def state(r: Replica, socks: list[FakeWS], http: FakeHttp, got: dict) -> dict:
    pending = r.dir / "blobs-pending"
    return {
        "files": {k: str(v) for k, v in r.files.items()},
        "since": r.since,
        "revoked": sorted(r.keys.revoked),
        "deleted": r.deleted_at is not None,
        "denied": r.denied,
        "min": r.min_version,
        "serverMode": r.server_mode,
        "storage": r.storage,
        "storeProblem": r.store_problem,
        "closed": any(w.closed for w in socks),
        "sent": sorted(t for w in socks for t in sent_tags(r, w)),
        "pending": len(r.outbox),
        "sentFiles": sent_files(r, socks),
        "uploaded": sorted(http.received, key=lambda u: (u["id"], u["output"])),
        "pendingFiles": len(list(pending.iterdir())) if pending.exists() else 0,
        "got": got.get("v", None),
        "last": list(r.last) if r.last else None,
        "lastSeq": r.last[0] if r.last else None,
        "helloLast": next((m.get("last") for w in reversed(socks) for m in reversed(w.sent) if m.get("t") == "hello"), None),
    }


async def settle(r: Replica) -> None:
    """Let what a message started (answers to peers, a reconnect's timer aside) finish."""
    for _ in range(5):
        await asyncio.sleep(0.01)
        if r._answer_tasks:
            await asyncio.gather(*list(r._answer_tasks), return_exceptions=True)


def differences(want: dict, got: dict) -> list[str]:
    out = []
    for k, v in want.items():
        g = sorted(got[k]) if k == "sent" else got[k]
        w = sorted(v) if k == "sent" else v
        if g != w:
            out.append(f"{k}: got {json.dumps(g)}, want {json.dumps(w)}")
    return out


def last_up(socks: list[FakeWS]) -> str:
    ups = [m for w in socks for m in w.sent if m.get("t") == "up"]
    return ups[-1]["id"] if ups else "none"


async def run_case(c: dict, links: dict) -> list[str]:
    keys, _ = Keys.from_link(f"{SERVER}/#{links[c['as']]}")
    with tempfile.TemporaryDirectory() as tmp:
        r = Replica(Path(tmp) / "state", keys, SERVER)
        socks = [FakeWS()]
        http = FakeHttp(c, keys)
        r.ws, r.http = socks[0], http  # type: ignore[assignment]
        r.connected.set()  # connected, as if hello had gone (the cases start from there)
        r.since = c["since"]
        got: dict = {}
        fetching: list[asyncio.Task] = []

        async def fetch(bid: str) -> None:
            try:
                got["v"] = (await r.get_blob(bid)).hex()
            except Exception:  # noqa: BLE001 - not to be had: shown as such
                got["v"] = "unavailable"

        async def look() -> dict:
            await settle(r)
            if fetching and not fetching[-1].done():
                got["v"] = "waiting"
            return state(r, socks, http, got)

        bad = []
        for i, step in enumerate(c["steps"]):
            try:
                if "msg" in step:
                    await r._handle(json.loads(json.dumps(step["msg"])))
                elif "edit" in step:
                    f, s = step["edit"]["file"], step["edit"]["append"]
                    r.local_change(lambda: r.files.__setitem__(f, Text(s)) if f not in r.files else r.files[f].insert(len(r.files[f]), s))
                elif "ackLast" in step:
                    await r._handle({"t": "ack", "id": last_up(socks), "q": step["ackLast"]})
                elif "refuseLast" in step:
                    await r._handle({"t": "err", "code": step["refuseLast"], "id": last_up(socks), "m": "refused"})
                elif "reconnect" in step:
                    r._dropped()
                    socks.append(FakeWS())
                    await r._opened(socks[-1])
                elif "wait" in step:
                    await asyncio.sleep(step["wait"] / 1000)
                elif "putFile" in step:
                    await r.put_blob(bytes.fromhex(step["putFile"]["hex"]), step["putFile"].get("kind", "file"))
                elif "getFile" in step:
                    got.pop("v", None)
                    fetching.append(asyncio.get_running_loop().create_task(fetch(step["getFile"])))
            except Exception as e:  # noqa: BLE001 - run() logs and goes on: so does this, but it counts
                bad.append(f"step {i + 1} raised {type(e).__name__}: {e}")
            await settle(r)
            if "expect" in step:
                bad += [f"after step {i + 1}: {d}" for d in differences(step["expect"], await look())]
        bad += differences(c["expect"], await look())
        for t in fetching:
            t.cancel()
        if r._missing_retry is not None:
            r._missing_retry.cancel()
        return bad


async def desktop_only(links: dict) -> list[str]:
    """What only the desktop app does: take compile requests (signed, recent, each once), and keep
    the latest storage setting it has seen across a restart."""
    import time

    from texloom.crypto import b64e
    owner, _ = Keys.from_link(f"{SERVER}/#{links['owner']}")
    member, _ = Keys.from_link(f"{SERVER}/#{links['member']}")
    bad: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        r = Replica(Path(tmp) / "state", member, SERVER)
        r.ws, r.http = FakeWS(), FakeHttp({"uploads": {}}, member)  # type: ignore[assignment]
        asked: list[str] = []

        async def on_request(frm, clean, nonce) -> None:
            asked.append(nonce)
        r.on_compile_request = on_request

        def creq(payload: dict, by: Keys | None = member) -> dict:
            data = member.encrypt(json.dumps({"k": "creq", **payload}).encode())
            return {"t": "p2p", "d": b64e(data), "from": 7, **({"s": b64e(by.sign(b"p2p:" + data))} if by else {})}
        now = int(time.time() * 1000)
        for what, msg, taken in [
            ("a fresh signed compile request is taken", creq({"n": "nonce-one-abcdef", "at": now}), True),
            ("the same request again (replayed) isn't", creq({"n": "nonce-one-abcdef", "at": now}), False),
            ("one made ten minutes ago isn't", creq({"n": "nonce-two-abcdef", "at": now - 600_000}), False),
            ("one dated far ahead isn't", creq({"n": "nonce-three-abcd", "at": now + 3_600_000}), False),
            ("one without a time (an older app) isn't, and nothing breaks", creq({"n": "nonce-four-abcde"}), False),
            ("one without a nonce isn't", creq({"at": now}), False),
            ("an unsigned one isn't", creq({"n": "nonce-five-abcde", "at": now}, by=None), False),
            ("another fresh one is", creq({"n": "nonce-six-abcdef", "at": now, "clean": True}), True),
        ]:
            before = len(asked)
            try:
                await r._handle(msg)
            except Exception as e:  # noqa: BLE001
                bad.append(f"{what}: raised {type(e).__name__}: {e}")
            if (len(asked) > before) != taken:
                bad.append(f"{what}: {'taken' if len(asked) > before else 'not taken'}")

    with tempfile.TemporaryDirectory() as tmp:
        def signed(mode: str, at: int) -> dict:
            return {"m": mode, "at": at, "s": b64e(owner.sign(f"owner:storage:{mode}:{at}".encode()))}
        r = Replica(Path(tmp) / "state", member, SERVER)
        r.meta["storage"] = signed("relay", 2000)
        r.save()
        r2 = Replica(Path(tmp) / "state", member, SERVER)
        r2.meta["storage"] = signed("server", 1000)  # (an editor putting back an older one)
        if r2.storage != "relay":
            bad.append(f"after a restart, an older storage setting counted: {r2.storage}")
    return bad


async def main() -> int:
    v = json.loads((ROOT / "scripts/replica-vectors.json").read_text())
    words = [w.lower() for w in sys.argv[1:]]
    failed = 0
    cases = [c for c in v["cases"] if all(w in c["what"].lower() for w in words)]
    for c in cases:
        bad = await run_case(c, v["links"])
        if bad:
            failed += 1
            print(f"FAIL {c['what']}\n     " + "\n     ".join(bad))
    print(f"replica (desktop): {len(cases) - failed} of {len(cases)} cases agree" + ("" if not failed else f", {failed} don't"))
    if not words:
        extra = await desktop_only(v["links"])
        print(*(f"FAIL {b}" for b in extra), sep="\n") if extra else print("replica (desktop only): compile requests and the kept storage setting check out")
        failed += len(extra)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
