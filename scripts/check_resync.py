#!/usr/bin/env python3
"""Asked to catch up again: "resync" and "recheck" (FUTURE_ARCHITECTURE.md, "Messages").

With several relays, a relay can miss what the others passed on (a break in its link to the bus), and a
database that fails over can lose changes it had acknowledged. The relay then asks its connections to catch up
again (Relay.ask), spread over a few seconds:
- "resync": the app sends its state vector to its peers (sv1), as after connecting, and they send what it lacks;
- "recheck": the app sends its hello again, whose "last" tells it whether the server's log is still the one it
  saw; if not, it sends everything again, as after a server restored from a backup (scripts/check_restore.py).

Checked against a throwaway server, with the desktop helper's sync engine and the web app:
- each says it understands these ("rs" in its hello);
- on "resync" it sends its state vector; on "recheck" its hello, staying connected;
- after a recheck once the server has lost the app's last change, the app sends it again: a newcomer
  catching up from the server alone gets every change;
- an app that doesn't say "rs" (0.4.67 and older) is closed instead, and so connects again.

    cd web && npm run build && cd ..
    uv run --extra server --with playwright python scripts/check_resync.py
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from pathlib import Path

import webcheck
from check_restore import add_line, text

import server.relay as relay_mod
from checklib import until
from texloom import __version__
from texloom.crypto import Keys
from texloom.replica import Replica

relays: list[relay_mod.Relay] = []  # the server's relay (recorded as it's made)
hellos: list[dict] = []  # every hello the server is sent, as sent
_init = relay_mod.Relay.__init__
_hello = relay_mod.Relay.HANDLERS["hello"]


def _recorded(self, *a, **kw):
    _init(self, *a, **kw)
    relays.append(self)


async def _heard_hello(self, project, ws, conn, msg, relay_only):
    hellos.append(dict(msg))
    await _hello(self, project, ws, conn, msg, relay_only)


relay_mod.Relay.__init__ = _recorded  # type: ignore[method-assign]
relay_mod.Relay.HANDLERS["hello"] = _heard_hello


def on_server(s: webcheck.Session, coro):
    """Run a coroutine on the server's own event loop (from any thread) and wait for it."""
    return asyncio.run_coroutine_threadsafe(coro, s.loop).result(60)


def in_thread(s: webcheck.Session, what: str, coro) -> None:
    failed: list[BaseException] = []

    def run() -> None:
        try:
            asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001
            failed.append(e)
    t = threading.Thread(target=run)
    t.start()
    t.join()
    if failed:
        s.check(f"{what}: ran to the end", False, repr(failed[0]))


def ask(s: webcheck.Session, t: str) -> None:
    on_server(s, relays[0].ask(t, spread=0.2))


async def desktop(s: webcheck.Session, tmp: Path) -> None:
    keys = Keys.generate()
    r = Replica(tmp / "desk", keys, s.urls["texloom"])
    sent: list = []
    p2p = r.p2p

    async def spy(payload, to=None):
        sent.append(payload.get("k"))
        await p2p(payload, to)
    r.p2p = spy  # type: ignore[method-assign]
    drops = 0
    dropped = r._dropped

    def counted() -> None:
        nonlocal drops
        drops += 1
        dropped()
    r._dropped = counted  # type: ignore[method-assign]
    task = asyncio.create_task(r.run())
    try:
        await asyncio.wait_for(r.synced.wait(), 30)
        mine = [h for h in hellos if h.get("app") == "desktop"]
        s.check("desktop: says it understands resync and recheck (\"rs\" in its hello)", bool(mine) and mine[-1].get("rs") == 1, str(mine[-1:]))
        sent.clear()
        await asyncio.to_thread(ask, s, "resync")
        s.check("desktop: on \"resync\", sends its state vector to its peers", await until(lambda: "sv1" in sent, 5), str(sent))
        before = len(hellos)
        await asyncio.to_thread(ask, s, "recheck")
        again = await until(lambda: len(hellos) > before, 5)
        s.check("desktop: on \"recheck\", sends its hello again, and stays connected",
                again and hellos[-1].get("app") == "desktop" and await until(lambda: r._caught_up, 10) and drops == 0,
                f"{len(hellos) - before} hellos, {drops} drops")

        # the database fails over and loses the last change it acknowledged
        for line in ("one", "two", "three"):
            add_line(r, line)
            assert await until(lambda: not r.outbox, 20), f"{line} wasn't stored"

        async def lose() -> None:
            db = relays[0].store.db
            p = keys.project
            await db.execute("DELETE FROM log WHERE project=$1 AND seq=(SELECT seq FROM head WHERE project=$1)", p)
            await db.execute("UPDATE head SET seq=seq-1 WHERE project=$1", p)
        await asyncio.to_thread(on_server, s, lose())
        await asyncio.to_thread(ask, s, "recheck")
        await asyncio.sleep(1)
        resent = await until(lambda: r._caught_up and not r.outbox, 20)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        newcomer = Replica(tmp / "new", keys, s.urls["texloom"])  # (alone: only the server to catch up from)
        t2 = asyncio.create_task(newcomer.run())
        await asyncio.wait_for(newcomer.synced.wait(), 30)
        got = await until(lambda: all(w in text(newcomer) for w in ("one", "two", "three")), 15)
        s.check("desktop: after a recheck, the server having lost its last change, it sends it again (a newcomer has every change)",
                resent and got, repr(text(newcomer)))
        t2.cancel()
        await asyncio.gather(t2, return_exceptions=True)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def older(s: webcheck.Session) -> None:
    import aiohttp
    url = s.urls["texloom"].replace("http", "ws", 1)
    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(f"{url}/ws/{Keys.generate().project}")
        await ws.send_json({"t": "hello", "since": 0, "app": "desktop", "v": "0.4.66", "pages": 1, "ka": 1})
        await ws.receive_json(timeout=10)
        await asyncio.to_thread(ask, s, "resync")
        closed = False
        end = time.monotonic() + 5
        while time.monotonic() < end:
            m = await ws.receive(timeout=5)
            if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                closed = True
                break
            if m.type == aiohttp.WSMsgType.TEXT and m.json().get("t") in ("resync", "recheck"):
                break
        s.check("an app that doesn't say \"rs\" isn't sent \"resync\": its connection is closed, so it connects again", closed)


def web(s: webcheck.Session) -> None:
    page = s.project({"main.tex": "\\documentclass{article}\n\\begin{document}\nHi.\n\\end{document}\n"})
    page.wait_for_function("() => __texloom.r.status === 'online'", timeout=30000)
    mine = [h for h in hellos if h.get("app") == "web"]
    s.check("web: says it understands resync and recheck (\"rs\" in its hello)", bool(mine) and mine[-1].get("rs") == 1, str(mine[-1:]))
    page.evaluate("""() => { const r = __texloom.r, p2p = r.p2p.bind(r); window.__k = []
      r.p2p = async (p, to) => { __k.push(p.k); return p2p(p, to) } }""")
    ask(s, "resync")
    try:
        page.wait_for_function("() => __k.includes('sv1')", timeout=5000)
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    s.check("web: on \"resync\", sends its state vector to its peers", ok, str(page.evaluate("() => __k")))
    page.evaluate("() => { window.__st = []; setInterval(() => { const st = __texloom.r.status; if (__st[__st.length - 1] !== st) __st.push(st) }, 50) }")
    before = len(hellos)
    ask(s, "recheck")
    page.wait_for_timeout(1500)
    seen = page.evaluate("() => __st")
    s.check("web: on \"recheck\", sends its hello again, and stays connected",
            len(hellos) > before and hellos[-1].get("app") == "web" and seen == ["online"], f"{len(hellos) - before} hellos, {seen}")


def main() -> None:
    with webcheck.Session() as s, tempfile.TemporaryDirectory() as tmp:
        in_thread(s, "desktop", desktop(s, Path(tmp)))
        in_thread(s, "an older app", older(s))
        web(s)
        s.done()


if __name__ == "__main__":
    main()
