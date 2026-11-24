#!/usr/bin/env python3
"""What the security review of 2026-11-24 found the server let an outsider do, checked against a
throwaway server (SQLite, nothing online):

- revocations: anyone can make a keypair, so a project's owner can be anyone. Each stored
  revocation is a row that stores nothing, so a new one on a project without a creation code
  comes out of the server-wide budget for such rows (store.META_BURST, then META_RATE a second),
  not just the first: a storage mode set once used to make the next thousand free, per project;
- TeX Live: the package index comes from a mirror, which mustn't be able to name a file outside
  the cache (../../etc/texloom/texloom.toml) for the server to serve or delete.

    uv run --extra server python scripts/check_server_abuse.py
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

from checklib import check, summary  # noqa: E402
from server.app import Settings, make_app  # noqa: E402
from server.policy import CodeBook  # noqa: E402
from server.store import META_BURST  # noqa: E402
from server.texlive import YEAR, TexLive, parse_tlpdb, safe_path  # noqa: E402
from texloom.crypto import b64e  # noqa: E402


def keypair() -> tuple[Ed25519PrivateKey, str]:
    sk = Ed25519PrivateKey.generate()
    return sk, b64e(sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


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
            ws = await c.ws_connect(f"/ws/{pid}")
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


async def main() -> int:
    for name, part in (("Revocations", revocations), ("TeX Live", texlive)):
        print(f"{name}:", flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            await part(Path(tmp))
    return summary("server abuse")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
