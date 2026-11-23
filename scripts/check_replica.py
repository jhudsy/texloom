#!/usr/bin/env python3
"""Runs texloom/replica.py through scripts/replica-vectors.json, the cases web/scripts/check-replica.mjs
runs web/src/replica.ts through: what a replica does with the server's messages (catching up,
signatures, uploads, revocations, refusals, peers) must be the same in both apps.

Each case gets a fresh replica, with a stand-in connection that records what it sends and a
stand-in server for uploads. Nothing touches the network.

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

from texloom.crypto import Keys, b64d
from texloom.replica import Replica

logging.disable(logging.WARNING)  # the cases are full of things a replica warns about
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

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def read(self) -> bytes:
        return self.body


class FakeHttp:
    """The server's uploads, as the case says it answers for each."""

    def __init__(self, uploads: dict) -> None:
        self.uploads = uploads

    def get(self, url: str, **_: object) -> FakeResponse:
        u = self.uploads.get(url.rsplit("/", 1)[-1])
        if u is None:
            return FakeResponse(404, b"")
        return FakeResponse(u["status"], bytes.fromhex(u.get("body", "")))

    def post(self, *_: object, **__: object) -> FakeResponse:
        return FakeResponse(503, b"")

    put = delete = head = post


def sent_tags(r: Replica, ws: FakeWS) -> list[str]:
    """What was sent: a message's type, and for peer-to-peer messages what kind they are inside."""
    out = []
    for m in ws.sent:
        if m.get("t") == "p2p":
            try:
                out.append("p2p:" + str(json.loads(r.keys.decrypt(b64d(m["d"]))).get("k")))
            except Exception:  # noqa: BLE001
                out.append("p2p:?")
        else:
            out.append(str(m.get("t")))
    return sorted(out)


def state(r: Replica, ws: FakeWS) -> dict:
    return {
        "files": {k: str(v) for k, v in r.files.items()},
        "since": r.since,
        "revoked": sorted(r.keys.revoked),
        "deleted": r.deleted_at is not None,
        "denied": r.denied,
        "min": r.min_version,
        "serverMode": r.server_mode,
        "storeProblem": r.store_problem,
        "closed": ws.closed,
        "sent": sent_tags(r, ws),
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


async def run_case(c: dict, links: dict) -> list[str]:
    keys, _ = Keys.from_link(f"{SERVER}/#{links[c['as']]}")
    with tempfile.TemporaryDirectory() as tmp:
        r = Replica(Path(tmp) / "state", keys, SERVER)
        ws = FakeWS()
        r.ws, r.http = ws, FakeHttp(c["uploads"])  # type: ignore[assignment]
        r.since = c["since"]
        bad = []
        for i, step in enumerate(c["steps"]):
            try:
                await r._handle(json.loads(json.dumps(step["msg"])))
            except Exception as e:  # noqa: BLE001 - run() logs and goes on: so does this, but it counts
                bad.append(f"step {i + 1} raised {type(e).__name__}: {e}")
            await settle(r)
            if "expect" in step:
                bad += [f"after step {i + 1}: {d}" for d in differences(step["expect"], state(r, ws))]
        bad += differences(c["expect"], state(r, ws))
        if r._missing_retry is not None:
            r._missing_retry.cancel()
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
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
