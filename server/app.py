"""TeXLoom relay server: the web application. Its addresses, the database it opens when it starts,
the admin page and the background upkeep (idle eviction, purges), from the command line's settings
(cli.py). The WebSocket protocol is in relay.py, the HTTP endpoints in endpoints.py and the storage
rules in policy.py."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from .bus import MemoryBus, open_bus
from .db import ALONE_LOCK, MAINTENANCE_LOCK, redact
from .endpoints import MAX_BODY, Endpoints, headers
from .files import Files
from .policy import GiB, CodeBook, Limits, Policy
from .relay import Relay
from .store import RECONCILE_EVERY, Store

log = logging.getLogger("texloom.relay")


@dataclass
class Settings:
    """What the server is run with (the command line's options: see cli.py)."""

    database: str
    static: Path | None = None  # the web app
    data: Path = Path("data")  # the server's own folder (--data)
    files: Files | None = None  # where stored files' content goes (default: a folder in `data`)
    tex: Path | None = None  # WebAssembly TeX Live for in-browser compiles (--tex-assets)
    limits: Limits = field(default_factory=Limits)
    codes: CodeBook = field(default_factory=lambda: CodeBook(None))
    private: bool = False
    min_version: str = ""
    codes_url: str = ""
    dropbox_app_key: str = ""
    lists: bool = True  # store people's encrypted project lists
    arxiv: bool = True  # import arXiv sources through this server
    admin_token: str = ""  # /admin, when set
    evict_days: float = 0  # --evict-idle-days (0: never)
    purge_days: float | None = None  # --purge-expired-after (None: never)
    texlive: Path | None = None  # TeX Live files on demand, cached here
    texlive_max: float = 4 * GiB
    texlive_mirrors: list[str] = field(default_factory=list)
    failover: object | None = None  # failover.FailoverSettings, when failing over to a standby (failover.py)
    bus: str = ""  # how several servers pass messages to each other (bus.py); "": one server alone


async def upkeep(policy: Policy, evict_days: float, purge_days: float | None) -> None:
    """Background work while the server runs, hourly (more often for short eviction times): drop
    the stored copies of projects unopened for `evict_days`, and project lists unused as long, and
    purge the projects of codes that expired more than `purge_days` ago; and, once a day, check the usage
    ledger against the stored data. With several servers sharing a database only one does this at a time
    (an advisory lock held while it does: the others find it taken and skip until the next round); with
    one it is always held. A server with no database open yet (a standby) has nothing to do."""
    last_reconcile = time.monotonic()
    while True:
        try:
            store = policy.store
            if store is not None:
                async with store.db.lock(MAINTENANCE_LOCK) as holding:
                    if holding:
                        if evict_days > 0:
                            await policy.evict_idle(evict_days)
                        if purge_days is not None:
                            await policy.purge_expired(purge_days)
                        if evict_days > 0 and policy.lists:
                            await policy.lists.drop_idle(time.time() - evict_days * 86400)
                        if time.monotonic() - last_reconcile >= RECONCILE_EVERY:
                            last_reconcile = time.monotonic()
                            if wrong := await store.reconcile():
                                log.warning("%d project(s) had the wrong usage counted, now put right", wrong)
        except Exception:
            log.exception("maintenance")
        await asyncio.sleep(min(3600, max(60, evict_days * 8640)) if evict_days > 0 else 3600)


def make_app(s: Settings) -> web.Application:
    policy = Policy(None, s.codes, s.limits, private=s.private, lists=s.lists, min_version=s.min_version)
    relay = Relay(policy)
    policy.busy = relay.has_peers  # what someone has open isn't evicted or purged now
    texlive = None
    if s.texlive:  # TeX Live files on demand for the in-browser engine
        from .texlive import TexLive
        tl = TexLive(s.texlive, s.texlive_max, s.limits.min_free, s.texlive_mirrors)
        if tl.ready:
            texlive = tl
        else:
            log.warning("--texlive %s has no index yet: run with --texlive-init first", s.texlive)
    http = Endpoints(policy, relay.memory, texlive, s.codes_url, s.dropbox_app_key)
    # bodies read whole are small (uploads are streamed, and checked against MAX_BLOB as they come)
    app = web.Application(middlewares=[headers], client_max_size=MAX_BODY)

    opened: list[Store] = []
    bus = open_bus(s.bus)
    held = contextlib.AsyncExitStack()  # the database's ALONE_LOCK, while it's held

    async def open_store() -> None:
        store = await Store.open(s.database, s.files, s.data, serving=True)
        log.info("database: %s", redact(s.database))
        if isinstance(bus, MemoryBus) and store.db.kind == "postgresql":
            # With no bus, a second server on this database would split each project's people into rooms that
            # never hear each other: refuse it (SQLite allows one server anyway). A connection holds the lock.
            if not await held.enter_async_context(store.db.lock(ALONE_LOCK)):
                await held.aclose()
                await store.close()
                raise SystemExit(f"another server with no bus is using {redact(s.database)}: two servers sharing a "
                                 "database must both have the same `bus` (a Redis-compatible server)")
        policy.attach(store)
        await store.start()
        opened.append(store)

    panels: list = []  # sections added to the admin page by parts that are on (failover)
    if s.failover:  # only then is failover.py even imported
        from .failover import Failover
        fo = Failover(s.failover, s.files, relay, open_store)
        app.middlewares.append(fo.gate)
        app.router.add_get("/api/health", fo.health)
        app.cleanup_ctx.append(fo.context(app))
        panels.append(fo.admin_panel)

        async def close_stores(_app: web.Application):
            for store in opened:
                await store.close()
        app.on_cleanup.append(close_stores)
    else:
        async def database(_app: web.Application):
            await open_store()
            yield
            await opened[0].close()
        app.cleanup_ctx.append(database)
    async def bus_open(_app: web.Application):
        await relay.join_bus(bus)
        log.info("bus: %s (this relay is number %d)", bus.name, relay.number)
        yield
        await bus.close()
        await held.aclose()
    app.cleanup_ctx.insert(0, bus_open)
    if s.admin_token:
        from .admin import Admin
        app.router.add_get("/admin", Admin(relay, s.admin_token, s.evict_days, s.purge_days, panels).page)
    async def maintenance(_app: web.Application):  # (always: it also keeps the usage ledger right)
        task = asyncio.create_task(upkeep(policy, s.evict_days, s.purge_days))
        yield
        task.cancel()
    app.cleanup_ctx.append(maintenance)
    app.on_shutdown.append(relay.close_all)
    app.router.add_get("/ws/{project}", relay.ws_handler)
    app.router.add_put("/blob/{project}/{id}", http.put_blob)
    app.router.add_get("/blob/{project}/{id}", http.get_blob)
    app.router.add_delete("/blob/{project}/{id}", http.delete_blob)
    app.router.add_get("/api/info", http.info)
    app.router.add_get("/api/list/{id}", http.get_list)
    app.router.add_put("/api/list/{id}", http.put_list)
    app.router.add_delete("/api/list/{id}", http.delete_list)
    if texlive:
        app.router.add_get("/texlive/{year}/{fmt}/{name}", texlive.handle)
    app.router.add_post("/api/code", http.check_code)
    if s.arxiv:  # importing a paper's source (arXiv has no CORS, so the relay fetches it)
        from .arxiv import ArxivFetcher
        app.router.add_get("/api/arxiv/{id}", ArxivFetcher(relay.memory, s.data).handle)
    if s.tex and (s.tex / "busytex").is_dir():  # WebAssembly TeX Live for in-browser compiles
        app.router.add_static("/core", s.tex)
    if s.static and s.static.is_dir():
        async def index(_):
            return web.FileResponse(s.static / "index.html")
        app.router.add_get("/", index)
        app.router.add_static("/", s.static)
    return app
