#!/usr/bin/env python3
"""Moving a stored project to another creation code (issue #32), against a real server.

A project belongs to the code it was first stored with. Its owner can move it to another code
(an owner-signed `move`), which the server checks: the code must be valid and not lapsed, have room
for the project, and the project must be within the size limit for that code. Then the project's
size counts against the new code, everyone connected is told, and writes refused under the old
code go through again.

Starts its own throwaway `texloom-server` (its database: scripts/testdb.py) and drives the desktop
app's sync engine (texloom/replica.py) against it.

    uv run --extra server python scripts/check_move_code.py
"""
import asyncio
import json
import secrets
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import background, check, server, summary, until  # noqa: E402

from pycrdt import Text  # noqa: E402

from texloom.crypto import Keys, b64e  # noqa: E402
from texloom.replica import Replica  # noqa: E402

CODES = """\
FIRS-TCOD-EAAA  70K   first
SECO-NDCO-DEBB  1M    second
TINY-CODE-CCCC  20K   tiny
GONE-CODE-DDDD  1M    until=2020-01-01  lapsed
"""
FIRST, SECOND, TINY, LAPSED = "FIRS-TCOD-EAAA", "seco-ndco-debb", "TINY-CODE-CCCC", "GONE-CODE-DDDD"


async def usage(r: Replica) -> dict:
    """The server's figures for r's project, fresh."""
    before = dict(r.store_info or {})
    await r.ask_usage()
    await until(lambda: r.store_info != before, 3)
    return dict(r.store_info or {})


def write(r: Replica, name: str, size: int) -> None:
    text = secrets.token_hex(size // 2)  # doesn't compress: its stored size is about `size`
    r.local_change(lambda: r.files.__setitem__(name, Text(text)))


async def main() -> int:
    with server("move", codes=CODES) as srv:
        async with background() as tasks:
            url, tmp = srv.url, srv.tmp
            keys = Keys.generate()
            owner = Replica(tmp / "owner", keys, url, FIRST)
            editor_keys = keys.personal("Editor")[2]
            editor = Replica(tmp / "editor", editor_keys, url)
            tasks += [asyncio.create_task(owner.run()), asyncio.create_task(editor.run())]
            await asyncio.wait_for(owner.synced.wait(), 20)
            await asyncio.wait_for(editor.synced.wait(), 20)

            write(owner, "main.tex", 60_000)  # about 46K stored
            stored = await until(lambda: not owner.outbox, 20)
            info = await usage(owner)
            check("the project is stored under the first code", stored and info.get("registered") and info.get("used", 0) > 30_000, info)
            check("its owner, holding that code, sees the code's usage", (info.get("ownerUsed") or 0) >= info.get("used", 1), info)

            # past the first code's 70K (hex text is stored at about three quarters its size): refused, kept in the outbox
            write(owner, "big.tex", 80_000)
            refused = await until(lambda: owner.store_refusal == "quota-full", 20)
            check("a write past the first code's allowance is refused (quota-full)", refused, owner.store_refusal)

            # only the owner may move it
            ts = int(time.time())
            sent = {"t": "move", "c": SECOND, "ts": ts, "s": b64e(editor_keys.sign(f"move:{SECOND}:{ts}".encode()))}
            assert editor.ws is not None
            await editor.ws.send_json(sent)
            await asyncio.sleep(1)
            info_after = await usage(owner)
            check("an editor's move is refused (only the owner's signature counts)", info_after.get("ownerUsed") is not None, info_after)

            for code, want, why in ((LAPSED, "move-bad-code", "a lapsed code"), ("NOPE-NOPE-NOPE", "move-bad-code", "a code that doesn't exist"),
                                    (TINY, "move-no-room", "a code without room for it")):
                no = await owner.move_to_code(code)
                check(f"moving to {why} is refused ({want})", no is not None and no.get("code") == want, no)
            no = await owner.move_to_code(TINY)
            check("a refusal for room says how much is needed and free", no is not None and no.get("need", 0) > no.get("free", -1) >= 0, no)

            # the move itself: to the second code (given in lower case, as people may type it)
            heard_before = editor.store_info
            no = await owner.move_to_code(SECOND)
            check("moving to a code with room succeeds", no is None, no)
            check("the owner's copy now holds the new code", owner.create_code == SECOND, owner.create_code)
            cleared = await until(lambda: owner.store_refusal is None and not owner.outbox, 20)
            check("the refused write goes through under the new code", cleared, (owner.store_refusal, len(owner.outbox)))
            info = await usage(owner)
            check("the new code counts the project, and its holder sees it", (info.get("ownerUsed") or 0) >= info.get("used", 1) > 90_000, info)
            check("the allowance shown is the new code's", info.get("allowance") == 1024 * 1024, info)
            check("everyone connected is told (the editor's figures were refreshed)",
                  await until(lambda: editor.store_info is not heard_before and (editor.store_info or {}).get("allowance") == 1024 * 1024, 5), editor.store_info)
            check("the editor isn't shown the code's usage (it doesn't hold it)", (editor.store_info or {}).get("ownerUsed") is None, editor.store_info)

            # the first code no longer counts it: check it with the server's own code check
            req = urllib.request.Request(url + "/api/code", data=json.dumps({"code": FIRST}).encode(),
                                         headers={"Content-Type": "application/json"})
            first = json.loads(urllib.request.urlopen(req, timeout=5).read())
            check("the first code's usage drops to nothing", first.get("ok") and first.get("used") == 0, first)

            no = await owner.move_to_code(SECOND)
            check("moving to the code it already has is a no-op", no is None, no)

            # a connection may only offer so many wrong codes: two above, then these
            tries = [await owner.move_to_code("NOPE-NOPE-NOPE") for _ in range(4)]
            check("after a few tries a connection is told to reconnect (no guessing codes)",
                  tries[-1] is not None and tries[-1].get("code") == "move-busy", [t and t.get("code") for t in tries])
    return summary("move to another code")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
