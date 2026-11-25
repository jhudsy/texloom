"""Automatic failover between two servers sharing a bucket (SCALING.md, "Switching over"; the design
is modelled in models/failover, and models/failover/README.md says why each rule is as it is).

Off unless `failover` is set, and then this module is the only part of the server that knows: with
it off nothing here is imported, no task runs and nothing is added to the path of a request.

Two servers, each with its own PostgreSQL (the second a standby of the first, kept up by streaming
replication, INSTALL.md "A standby server") and the same bucket. The bucket holds one small object,
the lease: {"ver": n, "holder": "name"}, changed only by conditional writes ("if it is still the
version I read"), so the store decides who wins.

- The **primary** renews the lease every TTL/3 (its version moves). It serves only while it last
  renewed less than TTL ago by its own clock (counted from when the request was sent, which is
  early, so safe). If a renewal finds the version changed, or TTL passes without one, it stops
  serving, closes its connections and exits, and the service manager starts it again as a standby.
- The **standby** watches the version. When it has not changed for `wait` on its own clock, it
  claims the lease with a conditional write on the version it saw (so a renewal in between makes
  the claim fail), promotes its PostgreSQL (pg_promote), opens the store and serves.
- `wait` is 2 x (TTL + SLACK): the most one clock may run faster than another (2: real clocks
  differ by parts per million) times the primary's longest time to notice, plus a margin. Nothing
  ever compares one machine's clock with another's, so they need not agree.
- Clocks: CLOCK_BOOTTIME where there is one (it keeps counting while a machine is suspended, which
  shortens the one case the model shows timers can't cover: a suspended virtual machine waking after
  the standby took over; what keeps that harmless is the apps' hello check).
- A node whose own PostgreSQL is a primary, but which isn't the lease's last holder, must not claim
  by waiting: it is a former primary with a history the new one doesn't have. It says so and waits to be
  rebuilt as a standby (INSTALL.md).

Until a node is the primary every address except /api/health answers 503, and /api/health says
what it is, so a front end or a DNS health check sends people only to the primary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiohttp import web

from .db import DatabaseError, is_standby, promote
from .files import LEASE, Files, FilesError

log = logging.getLogger("texloom.failover")

SLACK = 2.0  # seconds: how long the primary may take to notice its lease has run out (its watchdog's period, a request's delay)
MAX_RATE = 2.0  # how much faster one machine's clock may run than another's
MIN_TTL = 2.0
EXIT_FENCED = 75  # the exit status of a server that stopped itself (a service manager restarts it)


@dataclass
class FailoverSettings:
    name: str = ""  # this server's name in the lease (default: its host name): different on each
    ttl: float = 15.0  # seconds a primary may go without renewing its lease, on its own clock
    database: str = ""  # this server's own PostgreSQL, to promote


def clock() -> float:
    """Seconds, on a clock that only goes forward, and where there is one (Linux) keeps counting
    while the machine is suspended."""
    boottime = getattr(time, "CLOCK_BOOTTIME", None)
    return time.clock_gettime(boottime) if boottime is not None else time.monotonic()


def encode(ver: int, holder: str) -> bytes:
    return json.dumps({"ver": ver, "holder": holder}, separators=(",", ":")).encode()


def decode(data: bytes) -> tuple[int, str]:
    d = json.loads(data)
    if not (isinstance(d, dict) and isinstance(d.get("ver"), int) and isinstance(d.get("holder"), str)):
        raise ValueError(f"not a lease: {data[:80]!r}")
    return d["ver"], d["holder"]


RACERS = 8  # simultaneous writers in the check's race


async def check_conditional(files: Files) -> None:
    """Does the store honour conditional writes, including when writers race? Everything above
    depends on it, so a server asked to fail over checks first (and --failover-check does it on
    demand), on objects of its own. One at a time is the easy part; what matters is that when
    several create-only (or same-version) writes arrive together, exactly one wins."""
    token = secrets.token_hex(6)
    one, race = f"_failover/check-{token}", f"_failover/check-{token}-race"
    problems = []
    try:
        try:
            e1 = await files.lease_write(b"one", None, one)
            if e1 is None:
                raise FilesError("a write to an object that isn't there was refused")
            if await files.lease_write(b"two", None, one) is not None:
                problems.append("a write that should only create the object overwrote it (If-None-Match: * is ignored)")
            if await files.lease_write(b"two", '"not-the-version"', one) is not None:
                problems.append("a write for a version the object isn't at went through (If-Match is ignored)")
            e2 = await files.lease_write(b"three", e1, one)
            if e2 is None:
                problems.append("a write for the current version was refused")
            elif await files.lease_write(b"four", e1, one) is not None:
                problems.append("a write for an old version went through (If-Match is ignored)")
            got = await files.lease_read(one)
            if got is None or got[0] not in (b"three", b"four"):
                problems.append("the object read back isn't what was written")

            async def winners(round_: int, version: str | None) -> list[str]:
                # (each round's writers send different content: an ETag is the content's, so a writer that
                # rewrote what is there would leave the version unchanged and could "win" again)
                done = await asyncio.gather(*(files.lease_write(f"round-{round_}-racer-{i}".encode(), version, race)
                                              for i in range(RACERS)), return_exceptions=True)
                return [d for d in done if isinstance(d, str)]

            first = await winners(1, None)
            if len(first) != 1:
                problems.append(f"{len(first)} of {RACERS} simultaneous create-only writes succeeded: the check and the write "
                                "aren't one step, so two servers could both take the lease")
            elif len(await winners(2, first[0])) != 1:
                problems.append(f"more than one of {RACERS} simultaneous writes for the same version succeeded")
        except FilesError as e:
            raise FilesError(f"{files.name} can't hold a failover lease: {e}. (A store that rejects If-Match or "
                             "If-None-Match on writes can't: see INSTALL.md.)") from None
    finally:
        for name in (one, race):
            try:
                await files.lease_delete(name)
            except FilesError:
                pass
    if problems:
        raise FilesError(f"{files.name} doesn't do conditional writes properly, so it can't hold a failover lease: "
                         + "; ".join(problems))


class Failover:
    """One server's part in the failover: a standby until it holds the lease, a primary while it does."""

    def __init__(self, s: FailoverSettings, files: Files, relay, open_store: Callable[[], Awaitable[None]]):
        if s.ttl < MIN_TTL:
            raise ValueError(f"failover_ttl is at least {MIN_TTL:g} seconds")
        self.s, self.files, self.relay, self.open_store = s, files, relay, open_store
        self.name = s.name or socket.gethostname()
        self.ttl = s.ttl
        self.poll = s.ttl / 3
        self.wait = MAX_RATE * (s.ttl + SLACK)
        self.role = "standby"  # standby, promoting, primary, fenced
        self.ready = False  # its store is open
        self.holder = ""  # the lease's holder, as last read
        self._ver = 0  # the version this server last wrote (as primary)
        self._etag: str | None = None
        self._last_ok = 0.0  # when its last renewal was sent
        self._seen_ver = -1  # (standby) the version it last saw, and when
        self._seen_at = 0.0
        self._tasks: list[asyncio.Task] = []
        self._exit = os._exit  # (the checks replace it)

    # --- what the rest of the server asks ---------------------------------------------

    def can_serve(self) -> bool:
        return self.role == "primary" and self.ready and clock() - self._last_ok < self.ttl

    @web.middleware
    async def gate(self, request: web.Request, handler):
        if request.path == "/api/health" or self.can_serve():
            return await handler(request)
        return web.json_response({"error": "not the primary", "role": self.role, "holder": self.holder},
                                 status=503, headers={"Retry-After": "5"})

    async def health(self, _request: web.Request) -> web.Response:
        ok = self.can_serve()
        return web.json_response({"role": self.role, "name": self.name, "holder": self.holder, "ready": ok},
                                 status=200 if ok else 503)

    def context(self, app: web.Application):
        """The server's start and stop, in place of the plain database opening (cleanup_ctx)."""
        async def ctx(_app):
            self._tasks = [asyncio.create_task(self.run(), name="failover")]
            yield
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
        return ctx

    # --- the lease ------------------------------------------------------------------

    async def run(self) -> None:
        try:
            await self._standby()
            await self._become_primary()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a server that can't carry on stops, so another can take over
            log.exception("failover")
            await self.fence(f"failed: {e}")

    async def _standby(self) -> None:
        """Watch the lease until this server may take it. Returns holding it (self._etag, self._ver)."""
        reclaimed = False
        while True:
            try:
                got = await self.files.lease_read(LEASE)
                if got is None:  # no lease at all: the first server to ask holds it
                    sent = clock()
                    etag = await self.files.lease_write(encode(1, self.name), None, LEASE)
                    if etag is not None:
                        self._took(1, etag, sent)
                        log.info("failover: %s created the lease and is the primary", self.name)
                        return
                else:
                    ver, holder = decode(got[0])
                    etag = got[1]
                    self.holder = holder
                    if holder == self.name:
                        # this server's own name: it is starting again (a restart, a deployment). If it
                        # is alone it may claim the lease at once. If a version moves under its name, there
                        # is another server with the same name: neither can tell, so refuse.
                        if reclaimed and ver != self._seen_ver:
                            raise RuntimeError(f"another server is using the failover name {self.name!r}: "
                                               "each server needs a name of its own (failover_name)")
                        if not reclaimed:
                            reclaimed, self._seen_ver = True, ver
                            await asyncio.sleep(self.poll)
                            continue
                        sent = clock()
                        new = await self.files.lease_write(encode(ver + 1, self.name), etag, LEASE)
                        if new is not None:
                            self._took(ver + 1, new, sent)
                            log.info("failover: %s took its own lease back and is the primary", self.name)
                            return
                    else:
                        reclaimed = False
                        if ver != self._seen_ver:
                            self._seen_ver, self._seen_at = ver, clock()
                            log.info("failover: %s holds the lease (version %d)", holder, ver)
                        elif clock() - self._seen_at >= self.wait:
                            if not await is_standby(self.s.database):
                                log.error("failover: %s's lease hasn't been renewed for %.0f s, but this server's "
                                          "database is a primary (a former one): not taking over. Rebuild it as a "
                                          "standby of the new primary (INSTALL.md, \"A standby server\")", holder, self.wait)
                                self._seen_at = clock()  # (say it again after another wait)
                            else:
                                sent = clock()
                                new = await self.files.lease_write(encode(ver + 1, self.name), etag, LEASE)
                                if new is not None:
                                    self._took(ver + 1, new, sent)
                                    log.warning("failover: %s took the lease from %s: no renewal for %.0f s",
                                                self.name, holder, self.wait)
                                    return
                                self._seen_ver = -1  # somebody renewed or claimed first: look again
            except FilesError as e:
                log.warning("failover: the bucket: %s", e)
            except DatabaseError as e:  # (while deciding whether it may take over)
                log.warning("failover: %s", e)
            await asyncio.sleep(self.poll)

    def _took(self, ver: int, etag: str, sent: float) -> None:
        """This server holds the lease at `ver`; its time starts when the request was sent."""
        self._ver, self._etag, self._last_ok = ver, etag, sent
        self.holder = self.name

    async def _become_primary(self) -> None:
        self.role = "promoting"
        self._tasks.append(asyncio.create_task(self._renew(), name="failover-renew"))
        self._tasks.append(asyncio.create_task(self._watchdog(), name="failover-watchdog"))
        try:
            await promote(self.s.database)  # a standby's PostgreSQL becomes a primary; a primary's is left alone
        except DatabaseError as e:
            raise RuntimeError(str(e)) from None
        await self.open_store()
        self.role, self.ready = "primary", True
        log.info("failover: %s is serving", self.name)

    async def _renew(self) -> None:
        """Renew every TTL/3. The lease's time counts from when the request is sent."""
        while True:
            await asyncio.sleep(self.poll)
            sent = clock()
            try:
                new = await self.files.lease_write(encode(self._ver + 1, self.name), self._etag, LEASE)
            except FilesError as e:
                log.warning("failover: couldn't renew the lease: %s", e)
                continue  # the watchdog stops this server if it goes on
            if new is None:
                await self.fence("the lease was taken: another server holds it now")
                return
            self._ver, self._etag, self._last_ok = self._ver + 1, new, sent

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(min(0.5, self.poll))
            if self.role in ("primary", "promoting") and clock() - self._last_ok >= self.ttl:
                await self.fence(f"the lease wasn't renewed for {self.ttl:g} s (the bucket can't be reached?)")
                return

    async def fence(self, why: str) -> None:
        """Stop serving at once: refuse requests, close every connection, and exit (a service manager
        starts the server again, as a standby)."""
        if self.role == "fenced":
            return
        self.role, self.ready = "fenced", False
        log.error("failover: %s stopping: %s", self.name, why)
        try:
            await asyncio.wait_for(self.relay.close_all(), 3)
        except Exception:  # (stopping is the point: a connection that won't close is dropped by the exit)
            log.debug("failover: closing connections", exc_info=True)
        logging.shutdown()
        self._exit(EXIT_FENCED)

