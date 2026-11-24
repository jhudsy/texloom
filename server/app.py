"""TeXLoom relay server: the web application. Its addresses, the database it opens when it starts,
the admin page and the background upkeep (idle eviction, purges), from the command line's settings
(cli.py). The WebSocket protocol is in relay.py, the HTTP endpoints in endpoints.py and the storage
rules in policy.py."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from .db import redact
from .endpoints import MAX_BLOB, Endpoints, headers
from .files import Files
from .policy import GiB, CodeBook, Limits, Policy
from .relay import Relay
from .store import Store

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


async def upkeep(policy: Policy, evict_days: float, purge_days: float | None) -> None:
    """Background work while the server runs, hourly (more often for short eviction times): drop
    the stored copies of projects unopened for `evict_days`, and project lists unused as long, and
    purge the projects of codes that expired more than `purge_days` ago."""
    while True:
        try:
            if evict_days > 0:
                await policy.evict_idle(evict_days)
            if purge_days is not None:
                await policy.purge_expired(purge_days)
            if evict_days > 0 and policy.lists:
                await policy.lists.drop_idle(time.time() - evict_days * 86400)
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
    app = web.Application(middlewares=[headers], client_max_size=MAX_BLOB + 1024)

    async def database(_app: web.Application):
        store = await Store.open(s.database, s.files, s.data, serving=True)
        log.info("database: %s", redact(s.database))
        policy.attach(store)
        await store.start()
        yield
        await store.close()
    app.cleanup_ctx.append(database)
    if s.admin_token:
        from .admin import Admin
        app.router.add_get("/admin", Admin(relay, s.admin_token, s.evict_days, s.purge_days).page)
    if s.evict_days > 0 or s.purge_days is not None:
        async def maintenance(_app: web.Application):
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
        app.router.add_get("/api/arxiv/{id}", ArxivFetcher(relay.memory).handle)
    if s.tex and (s.tex / "busytex").is_dir():  # WebAssembly TeX Live for in-browser compiles
        app.router.add_static("/core", s.tex)
    if s.static and s.static.is_dir():
        async def index(_):
            return web.FileResponse(s.static / "index.html")
        app.router.add_get("/", index)
        app.router.add_static("/", s.static)
    return app
