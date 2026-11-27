#!/usr/bin/env python3
"""Keeping connections alive: a server that stops answering without closing anything is noticed.

When a server's machine or network goes, or it hangs, or a laptop wakes on another network, the
connection isn't closed: nothing more arrives. A browser sends no WebSocket pings of its own, so the
web app looked connected (its status green) for as long as that lasted, and its changes reached
nobody. Now a server offers a keep-alive ("ka" in its catch-up, seconds): an app that has heard
nothing for that long sends a "ping", and gives the connection up when nothing at all comes back
within as long again; it then shows as offline and connects again as after any drop.

Checked against a throwaway server whose keep-alive is shortened, and which is made to hang (its
event loop blocked, so connections stay open but nothing is read or answered):

- the server: it offers the keep-alive, answers a ping, and while it leaves a connection unread (its
  pace for unsigned messages) it sends that app a "wait" now and then, so it isn't taken for gone;
  an app that didn't ask gets none;
- the desktop helper and the web app: a quiet connection stays up (its pings are answered); a server
  that hangs is noticed within twice the keep-alive; and they connect again once it answers.

    cd web && npm run build && cd ..
    uv run --extra server --with playwright python scripts/check_keepalive.py
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
from pathlib import Path

import webcheck

import server.relay as relay_mod
from checklib import until
from texloom import __version__
from texloom.crypto import Keys
from texloom.replica import Replica

KA = 2  # seconds (5 in use)


def in_thread(s: webcheck.Session, what: str, coro) -> None:
    """Run a coroutine to the end on a thread of its own (the browser's API owns this one)."""
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


def hang(s: webcheck.Session, secs: float) -> None:
    """The server stops answering anything for `secs`, its connections left open."""
    s.loop.call_soon_threadsafe(time.sleep, secs)


async def server_side(s: webcheck.Session) -> None:
    import aiohttp
    url = s.urls["texloom"].replace("http", "ws", 1)

    async def join(http, pid: str, ka: bool):
        ws = await http.ws_connect(f"{url}/ws/{pid}")
        await ws.send_json({"t": "hello", "since": 0, "app": "desktop", "v": __version__, "pages": 1, **({"ka": 1} if ka else {})})
        return ws, await first(ws, "log")

    async def first(ws, t: str, secs: float = 10) -> dict | None:
        end = time.monotonic() + secs
        while (left := end - time.monotonic()) > 0:
            try:
                m = await ws.receive_json(timeout=left)
            except (TimeoutError, TypeError):
                return None
            if m.get("t") == t:
                return m
        return None

    async with aiohttp.ClientSession() as http:
        ws, log = await join(http, Keys.generate().project, ka=True)
        s.check("the server offers a keep-alive in its catch-up", log is not None and log.get("ka") == KA, str(log and log.get("ka")))
        await ws.send_json({"t": "ping"})
        s.check("and answers a ping", await first(ws, "pong") is not None)
        await ws.close()

        old = relay_mod.UNSIGNED_RATE, relay_mod.UNSIGNED_BURST
        relay_mod.UNSIGNED_RATE = relay_mod.UNSIGNED_BURST = 256 * 1024
        try:
            for ka in (True, False):
                pid = Keys.generate().project
                reader, _ = await join(http, pid, ka=False)
                reading = asyncio.create_task(drain(reader))
                sender, _ = await join(http, pid, ka=ka)
                waits = 0

                async def count():
                    nonlocal waits
                    async for m in sender:
                        waits += m.type == aiohttp.WSMsgType.TEXT and m.json().get("t") == "wait"
                counting = asyncio.create_task(count())
                for _ in range(6):  # 600 KB to the reader at 256 KB a second: left unread for a few seconds
                    await sender.send_json({"t": "p2p", "d": "B" * 100_000})
                await sender.send_json({"t": "ping"})
                await asyncio.sleep(5)
                if ka:
                    s.check("a connection left unread (its pace) is sent a 'wait' now and then", waits >= 2, f"{waits} waits")
                else:
                    s.check("but not one whose app didn't ask for the keep-alive", waits == 0, f"{waits} waits")
                for t in (reading, counting):
                    t.cancel()
                await sender.close()
                await reader.close()
        finally:
            relay_mod.UNSIGNED_RATE, relay_mod.UNSIGNED_BURST = old


async def drain(ws) -> None:
    async for _ in ws:
        pass


async def desktop(s: webcheck.Session, tmp: Path) -> None:
    r = Replica(tmp / "desk", Keys.generate(), s.urls["texloom"])
    drops = 0
    dropped = r._dropped

    def counted() -> None:
        nonlocal drops
        drops += 1
        dropped()
    r._dropped = counted
    task = asyncio.create_task(r.run())
    try:
        await asyncio.wait_for(r.synced.wait(), 30)
        s.check("desktop: takes up the server's keep-alive", r._ka == KA, str(r._ka))
        await asyncio.sleep(3 * KA + 1)
        s.check("desktop: a quiet connection stays up (its pings are answered)", drops == 0 and r.connected.is_set(), f"{drops} drops")
        hang(s, 4 * KA)
        start = time.monotonic()
        gone = await until(lambda: not r.connected.is_set(), 3 * KA + 2)
        took = time.monotonic() - start
        s.check("desktop: a server that stops answering is noticed within twice the keep-alive", gone and took <= 2 * KA + 1.5, f"{took:.1f} s")
        back = await until(lambda: r.connected.is_set() and r._caught_up, 30)
        s.check("desktop: and connected to again once it answers", back)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def web(s: webcheck.Session) -> None:
    page = s.project({"main.tex": "\\documentclass{article}\n\\begin{document}\nHi.\n\\end{document}\n"})
    page.wait_for_function("() => __texloom.r.status === 'online'", timeout=30000)
    s.check("web: takes up the server's keep-alive", page.evaluate("() => __texloom.r.ka") == KA * 1000)
    page.evaluate("""() => { window.__seen = []; setInterval(() => {
      const st = __texloom.r.status; if (__seen[__seen.length - 1] !== st) __seen.push(st) }, 100) }""")
    page.wait_for_timeout((3 * KA + 1) * 1000)
    seen = page.evaluate("() => __seen")
    s.check("web: a quiet connection stays up (its pings are answered)", seen == ["online"], str(seen))
    hang(s, 4 * KA)
    start = time.monotonic()
    try:
        page.wait_for_function("() => __texloom.r.status !== 'online'", timeout=(3 * KA + 2) * 1000)
        took = time.monotonic() - start
    except Exception:  # noqa: BLE001
        took = None
    s.check("web: a server that stops answering is noticed within twice the keep-alive (no longer shown as online)",
            took is not None and took <= 2 * KA + 1.5, f"{took if took is None else round(took, 1)} s")
    try:
        page.wait_for_function("() => __texloom.r.status === 'online'", timeout=30000)
        back = True
    except Exception:  # noqa: BLE001
        back = False
    s.check("web: and connected to again once it answers", back, str(page.evaluate("() => __seen")))


def main() -> None:
    relay_mod.KEEPALIVE = KA
    with webcheck.Session() as s, tempfile.TemporaryDirectory() as tmp:
        in_thread(s, "server", server_side(s))
        in_thread(s, "desktop", desktop(s, Path(tmp)))
        web(s)
        s.done()


if __name__ == "__main__":
    main()
