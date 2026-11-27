"""The relay's store: projects' encrypted edit logs and what's known about them, in its database
(SQLite or PostgreSQL: db.py).

What's kept per project: the log of signed, encrypted updates (and snapshots that replace part of
it), its head and floor, the storage mode, revoked personal links, who owns it (a creation code),
which files are stored (their content is in a file store, files.py), and, for a project deleted
for everyone, the owner's signed tombstone. The server can read none of the projects' content.

Edits to one project are numbered in the database (`head`, updated and read back in the same
transaction as the log row), so the numbering stays right with several servers. A project's
check-then-write steps (is it a replay? may it be stored? store it) run in one transaction that
holds the project (`locked`: a PostgreSQL advisory lock, so servers sharing the database take
turns; SQLite has one writer anyway). An owner's allowance is checked in that transaction too,
against what the database says the owner stores, holding the owner, so writes through several
servers can't together pass it (FUTURE_ARCHITECTURE.md, "Where each piece of state goes").

Some things are also kept in memory, because they're consulted often: how much each project and
owner stores, owners, deletions, last-seen times, storage modes and revocations. They follow the
database once a transaction has committed, and are what's shown (the admin page, "used"); the
limits that must hold are checked against the database. The server's total is the exception: it
is checked against this server's count, read again from the database every TOTALS_EVERY seconds,
so with several servers it can be passed by what they accept in that time.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
import time
from collections import OrderedDict, defaultdict
from collections.abc import Callable
from pathlib import Path

from texloom.crypto import b64e, canonical_pub

from .db import LONG, Database, DatabaseError, connect, copy_tables
from .files import Files, FilesError, FolderFiles

log = logging.getLogger("texloom.relay")

MAX_REVOKED = 1000  # revocations kept per project
# Compacted edits are remembered by hash so a replay of one isn't stored again (anyone in a
# project's room hears its signed edits, so anyone can replay them). Only the most recent are
# kept per project, and each counts against the allowance, so the table can't grow without limit.
# Eviction and turning storage off keep them too (else the whole history could be replayed into
# an emptied project). A replay of something older is stored, but edits are idempotent and the
# copy counts only until the next compaction (clients compact every few hundred entries, so small
# ones can't fill a project); big ones (KEEP_BIG and up) are remembered for good, in `kept`, so
# that a few old big edits can't fill a project without making a compaction due.
GONE_KEEP = 10_000  # sequence numbers
GONE_COST = 100  # bytes a remembered edit counts for
KEEP_BIG = 64 * 1024  # an edit at least this big is remembered for good
# Rows for projects that store nothing (relay-only mode, revocations) cost nothing to create,
# so they come out of a server-wide budget: a burst of META_BURST, then META_RATE per second.
META_RATE, META_BURST = 1.0, 600
FACTS_KEPT = 20_000  # projects whose deletion and storage mode are kept in memory (see Store._facts)
CODES_CHANNEL = "texloom_codes"  # what servers sharing a database tell each other: "the codes changed" (Policy.share_codes)
CHANNEL = "texloom_project"  # what servers sharing a database tell each other: "this project changed" (see Store._told)
RECONCILE_EVERY = 86400  # seconds between recounts of the usage ledger against the stored data (see Store.reconcile)
REVOKED_KEPT = 10_000  # projects whose revocations are kept in memory
# An upload's reservation counts from when it's allowed until it's stored, and at most this long
# (more than an upload may take, endpoints.UPLOAD_TIME), so a server that stops mid-upload leaves
# nothing counted for good.
RESERVE_TIME = 900
TOTALS_EVERY = 60  # seconds between readings of the server's total from the database (see the module docstring)
ENTRY_BYTES = "octet_length(data) + octet_length(sig)"  # what a log entry stores
# What projects' data counts for, as (project, bytes) rows to add up: the log, stored files, and
# remembered edits (GONE_COST each). `where` narrows it, to one project say.
USAGE = (f"SELECT project, SUM({ENTRY_BYTES}) AS n FROM log {{where}} GROUP BY project"
         " UNION ALL SELECT project, SUM(size) AS n FROM stored {where} GROUP BY project"
         f" UNION ALL SELECT project, COUNT(*) * {GONE_COST} AS n FROM gone {{where}} GROUP BY project"
         f" UNION ALL SELECT project, COUNT(*) * {GONE_COST} AS n FROM kept {{where}} GROUP BY project")

SCHEMA = """
CREATE TABLE IF NOT EXISTS log (
  project text NOT NULL, seq bigint NOT NULL, snap boolean NOT NULL DEFAULT false,
  data bytea NOT NULL, sig bytea NOT NULL,
  h bytea NOT NULL,  -- content hash, to recognise replayed updates
  PRIMARY KEY (project, seq));
CREATE INDEX IF NOT EXISTS log_h ON log (project, h);
CREATE TABLE IF NOT EXISTS head (project text PRIMARY KEY, seq bigint NOT NULL);
CREATE TABLE IF NOT EXISTS mode (project text PRIMARY KEY, relay boolean NOT NULL);
-- Stored files; their content is in the file store under "<project>/<id>".
-- kind 'output' = compiled PDF/log/SyncTeX, replaced by every compile: any writer may erase the
-- stored content. 'file' = content of project files: only the owner may erase it (deleting a
-- file from the project is a document edit, and History can restore it only while this exists)
CREATE TABLE IF NOT EXISTS stored (
  project text NOT NULL, id text NOT NULL, size bigint NOT NULL, kind text NOT NULL DEFAULT 'file',
  PRIMARY KEY (project, id));
-- Stored files that no longer count and can't be fetched, waiting for their content to be
-- erased (see Store._reap). Kept here so a restart carries on where it stopped.
CREATE TABLE IF NOT EXISTS trash (project text NOT NULL, id text NOT NULL, PRIMARY KEY (project, id));
CREATE TABLE IF NOT EXISTS revoked (
  project text NOT NULL, pub text NOT NULL, sig bytea NOT NULL, at double precision NOT NULL,
  PRIMARY KEY (project, pub));
-- seen: the last time anyone connected (for idle-project eviction)
CREATE TABLE IF NOT EXISTS projects (
  project text PRIMARY KEY, created double precision NOT NULL, owner text NOT NULL,
  seen double precision NOT NULL DEFAULT 0, evicted double precision NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS gone (
  project text NOT NULL, h bytea NOT NULL, seq bigint NOT NULL DEFAULT 0, PRIMARY KEY (project, h));
CREATE TABLE IF NOT EXISTS floor (project text PRIMARY KEY, seq bigint NOT NULL);
-- hashes of big compacted or evicted edits, remembered for good (KEEP_BIG)
CREATE TABLE IF NOT EXISTS kept (project text NOT NULL, h bytea NOT NULL, PRIMARY KEY (project, h));
CREATE TABLE IF NOT EXISTS deleted (
  project text PRIMARY KEY, at double precision NOT NULL, sig bytea NOT NULL, ts bigint NOT NULL);
-- the apps that connect (for the admin page, and choosing --min-version): how often, last when
CREATE TABLE IF NOT EXISTS clients (
  app text NOT NULL, version text NOT NULL, n bigint NOT NULL, seen double precision NOT NULL,
  PRIMARY KEY (app, version));
-- encrypted project lists (lists.py)
CREATE TABLE IF NOT EXISTS lists (
  id text PRIMARY KEY, seq bigint NOT NULL, data bytea NOT NULL, owner text NOT NULL, seen double precision NOT NULL);
CREATE INDEX IF NOT EXISTS lists_owner ON lists (owner, seq);
CREATE INDEX IF NOT EXISTS lists_seen ON lists (seen);
-- What each project's data counts for (USAGE below, added up), kept in step with every change to it, so
-- that a server starting, or another server sharing this database, reads it rather than adding up the
-- log. Only projects with something stored have a row. Store.reconcile checks it against a recount.
CREATE TABLE IF NOT EXISTS usage (project text PRIMARY KEY, bytes bigint NOT NULL);
-- Uploads under way (Store.reserve_upload): counted against their project and owner from when they're
-- allowed until they're stored, so that writes through any server meanwhile see them. Rows a stopped
-- server leaves behind stop counting at `until` (RESERVE_TIME) and are cleared out.
CREATE TABLE IF NOT EXISTS reserved (
  id text PRIMARY KEY, project text NOT NULL, owner text NOT NULL, bytes bigint NOT NULL, until double precision NOT NULL);
CREATE INDEX IF NOT EXISTS reserved_owner ON reserved (owner);
CREATE INDEX IF NOT EXISTS reserved_project ON reserved (project);
-- what an owner stores is added up from its projects (Store.owner_total)
CREATE INDEX IF NOT EXISTS projects_owner ON projects (owner);
-- Creation codes, for every server sharing the database: each server copies its codes files here when they change
-- (source: the file's path; code "default" for the default line; size as written: "2G", "unlimited"), and takes all.
CREATE TABLE IF NOT EXISTS codes (
  source text NOT NULL, code text NOT NULL, size text NOT NULL, label text NOT NULL, until text NOT NULL,
  PRIMARY KEY (source, code));
"""
SCHEMA_POSTGRESQL = """
-- ciphertext doesn't compress: store it as it is, out of line, without trying
ALTER TABLE log ALTER COLUMN data SET STORAGE EXTERNAL;
"""
# Each table's primary key, for copying them in order (db.copy_tables), and the boolean columns
TABLES = {"log": ("project", "seq"), "head": ("project",), "mode": ("project",), "stored": ("project", "id"),
          "trash": ("project", "id"), "revoked": ("project", "pub"), "projects": ("project",), "gone": ("project", "h"),
          "kept": ("project", "h"), "floor": ("project",), "deleted": ("project",), "clients": ("app", "version"), "lists": ("id",),
          "usage": ("project",), "reserved": ("id",), "codes": ("source", "code")}
BOOLEANS = frozenset({"snap", "relay"})


class Recent:
    """The `most` most recently used of something kept in the database, kept in memory."""

    def __init__(self, most: int):
        self.most = most
        self.items: OrderedDict = OrderedDict()

    def get(self, key: str):
        """What's kept for `key` (now the most recently used), or None."""
        if key not in self.items:
            return None
        self.items.move_to_end(key)
        return self.items[key]

    def put(self, key: str, value) -> None:
        self.items[key] = value
        self.items.move_to_end(key)
        while len(self.items) > self.most:
            self.items.popitem(last=False)

    def drop(self, key: str) -> None:
        self.items.pop(key, None)


def key(kind: str, name: str) -> int:
    """The number a project ("project") or an owner ("owner") is locked by in the database (db.py, xlock)."""
    return int.from_bytes(hashlib.sha256(f"{kind}:{name}".encode()).digest()[:8], "big", signed=True)


class Tx:
    """A transaction's calls (db.py), and what to do once it has committed: `later` holds functions (or
    coroutine functions) that bring this server's memory into step with what the transaction wrote, so
    that a transaction that fails leaves memory as it was."""

    def __init__(self, calls):
        self._calls = calls
        self.later: list = []

    def __getattr__(self, name: str):
        return getattr(self._calls, name)

    async def done(self) -> None:
        for fn in self.later:
            if asyncio.iscoroutine(r := fn()):
                await r


class Store:
    """Open with `await Store.open(url, ...)`; close with `await store.close()`."""

    def __init__(self, db: Database, files: Files, data: Path):
        self.db = db
        self.files = files
        self.dir = data  # this server's own disk: uploads are spooled here, and it must keep room
        self._revoked = Recent(REVOKED_KEPT)  # project -> {canonical pub: owner's signature}
        self.usage: dict[str, int] = defaultdict(int)
        self.total = 0
        self.owners: dict[str, str] = {}
        # Per project: its owner-signed deletion (the message telling clients) and its storage mode
        # (None: none set), for the projects in use. Anyone can add these rows, a few a second, so
        # they stay in the database, not all in memory (FACTS_KEPT at most).
        self._facts = Recent(FACTS_KEPT)
        self.owner_bytes: dict[str, int] = defaultdict(int)  # checked on every stored edit: kept, not added up
        self.list_bytes: dict[str, int] = defaultdict(int)  # bytes of project lists, by owner
        self.seen: dict[str, float] = {}
        # evicted (or purged), or turned relay-only, and not opened or written to since: what's kept
        # of them is only what's remembered of their changes (GONE_KEEP), so they aren't evicted again
        self.evicted: set[str] = set()
        self.meta_tokens, self.meta_at = float(META_BURST), time.monotonic()
        self.pending: dict[str, int] = defaultdict(int)  # bytes of uploads under way (see put_blob)
        self._reaper: asyncio.Task | None = None
        self._locks: dict[str, list] = {}  # key -> [lock, users]
        self.node = secrets.token_hex(4)  # this server's name in what it tells the others (it ignores its own)
        self._refreshing: set[asyncio.Task] = set()  # looking again at projects the others changed
        self.on_back: Callable[[], None] = lambda: None  # told when the database is back after its connection was lost
        self._totals: asyncio.Task | None = None  # reading the server's total again (TOTALS_EVERY)

    @classmethod
    async def open(cls, url: str, files: Files | None = None, data: Path = Path("data"), serving: bool = False) -> Store:
        """`serving`: for a server (an SQLite database allows only one), not a one-off command.
        Exits, saying why, if the database can't be used."""
        data.mkdir(parents=True, exist_ok=True)
        try:
            db = await connect(url, SCHEMA, SCHEMA_POSTGRESQL, serving)
        except DatabaseError as e:
            raise SystemExit(str(e)) from None
        s = cls(db, files or FolderFiles(data / "files"), data)
        await s._load()
        return s

    async def _load(self) -> None:
        c = self.db
        ledger = await c.fetch("SELECT project, bytes FROM usage")
        if ledger:
            for project, n in ledger:
                self.usage[project] += int(n)
        else:  # first start with a ledger (or nothing stored): add it up once, and keep it from now on
            for project, n in await c.fetch(USAGE.format(where=""), timeout=LONG):
                self.usage[project] += int(n or 0)
            async with c.tx() as t:
                await t.executemany("INSERT INTO usage(project, bytes) VALUES ($1,$2) ON CONFLICT (project) DO NOTHING",
                                    [(p, n) for p, n in self.usage.items() if n])
        self.list_bytes = defaultdict(int, {r["owner"]: int(r["n"]) for r in await c.fetch(
            "SELECT owner, SUM(octet_length(data)) AS n FROM lists GROUP BY owner")})
        self.total = sum(self.usage.values()) + sum(self.list_bytes.values())
        projects = await c.fetch("SELECT project, owner, seen, created, evicted FROM projects")
        self.owners = {r["project"]: r["owner"] for r in projects}
        self.evicted = {r["project"] for r in projects if r["evicted"]}
        self.seen = {r["project"]: max(r["seen"], r["created"]) for r in projects}
        for project, n in self.usage.items():
            if project in self.owners:
                self.owner_bytes[self.owners[project]] += n

    async def start(self) -> None:
        """Before serving: check that the file store works, drop files a stop left behind for
        projects that keep none, and carry on erasing what's in the trash. (An evicted project's
        leftovers stay: its writers may have uploaded again since, and what's there is valid.)"""
        await self.files.check()
        log.info("stored files: %s", self.files.name)
        await self.db.listen(CHANNEL, self._heard)
        self._totals = asyncio.get_running_loop().create_task(self._keep_totals())
        left = await self.db.fetch("SELECT DISTINCT project FROM stored WHERE project IN "
                                     "(SELECT project FROM deleted UNION SELECT project FROM mode WHERE relay)")
        for (project,) in left:
            async with self.locked(project) as c:
                await self._trash_files(project, c)
        self._kick_reaper()

    async def close(self) -> None:
        for task in (self._reaper, self._totals):
            if task and not task.done():
                task.cancel()
        await self.files.close()
        await self.db.close()

    def tx(self):
        """A transaction: `async with store.tx() as c:`, and then only `c`'s calls until it ends."""
        return self.db.tx()

    @contextlib.asynccontextmanager
    async def _holding(self, key: str):
        """One at a time per key, among this server's tasks."""
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                del self._locks[key]

    def lock(self, project: str):
        """Among this server's tasks, one at a time per project (see `locked`, which holds it)."""
        return self._holding(f"project:{project}")

    @contextlib.asynccontextmanager
    async def locked(self, project: str):
        """A transaction holding a project, for checking and then changing what's stored for it, so that
        nothing else (on this server or another sharing the database) changes it in between:
        `async with store.locked(project) as c:`, then only `c`'s calls. This server's tasks queue here
        rather than in the database, each holding a connection. Memory follows once it commits (Tx)."""
        async with self._holding(f"project:{project}"):
            async with self.tx() as calls:
                await calls.xlock(key("project", project))
                c = Tx(calls)
                yield c
            await c.done()

    @contextlib.asynccontextmanager
    async def owner_locked(self, owner: str):
        """A transaction holding an owner, for checking its allowance and then storing (project lists).
        Taken after a project's lock, never before, so two can't wait for each other."""
        async with self.tx() as calls:
            await calls.xlock(key("owner", owner))
            c = Tx(calls)
            yield c
        await c.done()

    async def owner_of(self, project: str, c) -> str | None:
        """Who owns a project, as the database says now (memory: `owner`)."""
        return await c.fetchval("SELECT owner FROM projects WHERE project=$1", project)

    async def owner_total(self, owner: str, c) -> int:
        """What an owner stores, as the database says now: its projects, its project lists, and uploads
        to its projects under way. Exact when `c` holds the owner (Policy.may_store)."""
        return int(await c.fetchval(
            "SELECT COALESCE((SELECT SUM(u.bytes) FROM usage u JOIN projects p ON p.project=u.project WHERE p.owner=$1), 0)"
            " + COALESCE((SELECT SUM(octet_length(data)) FROM lists WHERE owner=$1), 0)"
            " + COALESCE((SELECT SUM(bytes) FROM reserved WHERE owner=$1 AND until>$2), 0)", owner, time.time()))

    async def project_total(self, project: str, c) -> int:
        """What a project stores, as the database says now, uploads under way included."""
        return int(await c.fetchval(
            "SELECT COALESCE((SELECT bytes FROM usage WHERE project=$1), 0)"
            " + COALESCE((SELECT SUM(bytes) FROM reserved WHERE project=$1 AND until>$2), 0)", project, time.time()))

    # --- registration & accounting -------------------------------------------
    def owner(self, project: str) -> str | None:
        return self.owners.get(project)

    async def register(self, project: str, owner: str, label: str, c: Tx | None = None) -> None:
        if c is None:
            async with self.locked(project) as c:
                return await self.register(project, owner, label, c)
        await c.execute("INSERT INTO projects(project, created, owner) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                        project, time.time(), owner)
        # deleted meanwhile (a delete between a request's check and this): the project stays unregistered
        if await c.execute("DELETE FROM projects WHERE project=$1 AND EXISTS (SELECT 1 FROM deleted WHERE project=$1)", project):
            return
        owner = await self.owner_of(project, c)  # (registered already, by another server, if it was)

        def registered() -> None:
            self._own(project, owner)
            log.info("registered project %s… (%s)", project[:8], label)
        c.later.append(registered)

    async def claim(self, project: str, owner: str, c: Tx | None = None) -> None:
        """Move a project to `owner` (a private server's re-registration, or its owner's `move`)."""
        if c is None:
            async with self.locked(project) as c:
                return await self.claim(project, owner, c)
        await c.execute("UPDATE projects SET owner=$1 WHERE project=$2", owner, project)
        c.later.append(lambda: self._own(project, owner))
        c.later.append(lambda: self._told(project))

    def _own(self, project: str, owner: str) -> None:
        n = self.usage.get(project, 0)
        if project in self.owners:
            self.owner_bytes[self.owners[project]] -= n
        self.owner_bytes[owner] += n
        self.owners[project] = owner

    # --- servers sharing a database --------------------------------------------------
    async def _told(self, project: str) -> None:
        """Tell the other servers on this database that something about a project changed (its owner, its
        storage mode, what's stored, that it's deleted): any of them holding what it knew of it looks again.
        Best effort: the project's own server is the one that changes it, and a server that missed this
        looks again when it next hears, or at its next reconcile."""
        try:
            await self.db.notify(CHANNEL, json.dumps({"p": project, "n": self.node}, separators=(",", ":")))
        except (DatabaseError, OSError) as e:
            log.warning("couldn't tell the other servers about project %s…: %s", project[:8], e)
        except Exception:  # (a failing database call: the change itself is made, and is what matters)
            log.warning("couldn't tell the other servers about project %s…", project[:8], exc_info=True)

    def _heard(self, payload: str) -> None:
        """What another server told us (a callback in the event loop): look again at that project, or, for ""
        (the connection was lost and came back: we may have missed some), at everything."""
        if payload:
            try:
                d = json.loads(payload)
                project, node = d["p"], d["n"]
            except (ValueError, KeyError, TypeError):
                log.warning("ignoring a notification that isn't one of ours: %r", payload[:80])
                return
            if node == self.node:
                return  # our own
            work = self.refresh_project(project)
        else:
            self.on_back()  # (the relay checks whether the database failed over meanwhile)
            work = self.refresh_all()
        task = asyncio.get_running_loop().create_task(work)
        self._refreshing.add(task)
        task.add_done_callback(self._refreshing.discard)

    async def refresh_project(self, project: str) -> None:
        """Take what the database now says of a project (its registration, and what it counts for) over what
        this server remembers: it was changed by another server. What's kept only for speed (its storage
        mode, the revocations) is dropped, to be read again when asked for."""
        try:
            row = await self.db.fetchrow("SELECT owner, seen, created, evicted FROM projects WHERE project=$1", project)
            n = int(await self.db.fetchval("SELECT bytes FROM usage WHERE project=$1", project) or 0)
        except DatabaseError:
            log.warning("couldn't look again at project %s…", project[:8], exc_info=True)
            return
        self._facts.drop(project)
        self._revoked.drop(project)
        if row is None:  # gone (deleted, or never registered here)
            if project in self.owners:
                self.owner_bytes[self.owners[project]] -= self.usage.get(project, 0)
                del self.owners[project]
            self._grew_total(self.usage.pop(project, 0) * -1)
            self.seen.pop(project, None)
            self.evicted.discard(project)
            return
        self._own(project, row["owner"])
        self._grew(project, n + self.pending.get(project, 0) - self.usage.get(project, 0))
        self.seen[project] = max(row["seen"], row["created"])
        (self.evicted.add if row["evicted"] else self.evicted.discard)(project)

    async def refresh_all(self) -> None:
        """Look again at everything this server remembers, as at the start (it may have missed what others said)."""
        self._facts.items.clear()
        self._revoked.items.clear()
        self.usage = defaultdict(int)
        self.total = 0
        self.owners = {}
        self.owner_bytes = defaultdict(int)
        self.seen = {}
        self.evicted = set()
        await self._load()
        for project, n in self.pending.items():
            self._grew(project, n)

    def _grew_total(self, n: int) -> None:
        self.total += n

    def _grew(self, project: str, n: int) -> None:
        self.usage[project] += n
        self.total += n
        if project in self.owners:
            self.owner_bytes[self.owners[project]] += n

    def meta_budget(self) -> bool:
        """Take one unit from the server-wide budget for rows that store nothing (see META_RATE)."""
        now = time.monotonic()
        self.meta_tokens = min(META_BURST, self.meta_tokens + (now - self.meta_at) * META_RATE)
        self.meta_at = now
        if self.meta_tokens < 1:
            return False
        self.meta_tokens -= 1
        return True

    async def touch(self, project: str) -> None:
        """Remember that someone opened a stored project (written at most hourly)."""
        if project not in self.owners:
            return  # nothing stored: nothing to keep track of (and no unbounded map)
        now = time.time()
        if now - self.seen.get(project, 0) < 3600:
            return
        self.seen[project] = now
        self.evicted.discard(project)
        await self.db.execute("UPDATE projects SET seen=$1, evicted=0 WHERE project=$2", now, project)

    def idle(self, days: float) -> list[str]:
        """Stored projects nobody has opened for `days` (not those evicted already, which keep only
        the hashes of what they stored: see GONE_KEEP)."""
        cutoff = time.time() - days * 86400
        return [p for p, n in self.usage.items() if n > 0 and p not in self.evicted and self.seen.get(p, 0) < cutoff]

    async def evict(self, project: str) -> int:
        """Drop the stored copy (log and files) of an idle project. Its writers re-upload it
        when they next connect; until then, link holders without a copy can't open it.
        Sequence numbers keep counting (so old entries can't be replayed into the gap);
        `floor` tells clients that everything up to it is gone."""
        freed = self.usage.get(project, 0)
        async with self.locked(project) as c:
            await self._forget_log(c, project)
            await c.execute("UPDATE projects SET evicted=$1 WHERE project=$2", time.time(), project)
            await self._trash_files(project, c)
            c.later.append(lambda: self.evicted.add(project))
            c.later.append(lambda: self._told(project))
        return freed - self.usage.get(project, 0)  # (what's remembered of it stays)

    @staticmethod
    async def _forget_log(c, project: str) -> None:
        """In transaction `c`, drop a project's log (eviction, relay-only mode). Its numbers keep
        counting, and `floor` says that nothing up to head can be stored again (a replay of an old,
        validly signed snapshot); its changes are remembered by hash, as compaction does, so that
        an old, validly signed change isn't stored again either (see GONE_KEEP)."""
        await c.execute("INSERT INTO gone(project, h, seq) SELECT project, h, seq FROM log "
                        "WHERE project=$1 AND NOT snap ON CONFLICT DO NOTHING", project)
        await c.execute("INSERT INTO kept(project, h) SELECT project, h FROM log "
                        "WHERE project=$1 AND NOT snap AND octet_length(data)>=$2 ON CONFLICT DO NOTHING", project, KEEP_BIG)
        await c.execute("DELETE FROM gone WHERE project=$1 AND seq <= COALESCE((SELECT seq FROM head WHERE project=$1), 0) - $2",
                        project, GONE_KEEP)
        await c.execute("DELETE FROM log WHERE project=$1", project)
        await c.execute("INSERT INTO floor(project, seq) SELECT $1, COALESCE((SELECT seq FROM head WHERE project=$1), 0) "
                        "WHERE true ON CONFLICT (project) DO UPDATE SET seq=excluded.seq", project)

    def owner_usage(self, owner: str) -> int:
        """What an owner (a creation code, or "default") stores: projects and project lists."""
        return self.owner_bytes.get(owner, 0) + self.list_bytes.get(owner, 0)

    def lists_grew(self, owner: str, n: int) -> None:
        """Project-list entries of `owner` now take `n` more bytes (see lists.py)."""
        self.list_bytes[owner] += n
        self.total += n

    def stored_by(self, owner: str) -> list[str]:
        """Projects registered to this owner that have something stored."""
        return [p for p, o in self.owners.items() if o == owner and self.usage.get(p, 0) > 0 and p not in self.evicted]

    async def _recount(self, project: str, c: Tx) -> None:
        """In `c`, holding the project: add up what its data counts for, from the tables. The ledger is set to
        it, and this server's count (which also holds its uploads under way, as `pending`) follows."""
        n = int(await c.fetchval(f"SELECT COALESCE(SUM(n), 0) FROM ({USAGE.format(where='WHERE project=$1')}) u", project))
        if n:
            await c.execute("INSERT INTO usage(project, bytes) VALUES ($1,$2) "
                            "ON CONFLICT (project) DO UPDATE SET bytes = excluded.bytes", project, n)
        else:
            await c.execute("DELETE FROM usage WHERE project=$1", project)
        c.later.append(lambda: self._grew(project, n + self.pending.get(project, 0) - self.usage[project]))

    async def reconcile(self) -> int:
        """Check the usage ledger (and this server's counts) against the stored data, project by project,
        and put right any that differ (a change that didn't keep it in step, a server that didn't know
        of the ledger). Returns how many were wrong. Each one found is checked again, and recounted, under
        the project's lock, so a write meanwhile is neither undone nor reported."""
        truth: dict[str, int] = defaultdict(int)
        for project, n in await self.db.fetch(USAGE.format(where=""), timeout=LONG):
            truth[project] += int(n or 0)
        ledger = {r["project"]: int(r["bytes"]) for r in await self.db.fetch("SELECT project, bytes FROM usage")}
        wrong = 0
        for project in sorted(set(truth) | set(ledger)):
            if truth.get(project, 0) == ledger.get(project, 0) and truth.get(project, 0) == self.usage.get(project, 0) - self.pending.get(project, 0):
                continue
            async with self.locked(project) as c:
                now = int(await c.fetchval(f"SELECT COALESCE(SUM(n), 0) FROM ({USAGE.format(where='WHERE project=$1')}) u", project))
                led = int(await c.fetchval("SELECT bytes FROM usage WHERE project=$1", project) or 0)
                mem = self.usage.get(project, 0) - self.pending.get(project, 0)
                if now != led or now != mem:
                    await self._recount(project, c)
                    wrong += 1
                    log.warning("usage of project %s… was %s in the ledger and %s counted here, but is %s: put right",
                                project[:8], led, mem, now)
        return wrong

    def free_disk(self) -> int:
        return shutil.disk_usage(self.dir).free

    async def _keep_totals(self) -> None:
        while True:
            await asyncio.sleep(TOTALS_EVERY)
            try:
                await self.read_total()
            except Exception:  # (a database away for a while: try again next time)
                log.warning("couldn't read the server's total again", exc_info=True)

    async def read_total(self) -> None:
        """Read the server's total from the database (what every server sharing it stores), and clear out
        reservations that have run out."""
        now = time.time()
        await self.db.execute("DELETE FROM reserved WHERE until<=$1", now)
        self.total = int(await self.db.fetchval(
            "SELECT COALESCE((SELECT SUM(bytes) FROM usage), 0) + COALESCE((SELECT SUM(octet_length(data)) FROM lists), 0)"
            " + COALESCE((SELECT SUM(bytes) FROM reserved WHERE until>$1), 0)", now))

    # --- log ---------------------------------------------------------------------
    async def head(self, project: str, c=None) -> int:
        return await (c or self.db).fetchval("SELECT seq FROM head WHERE project=$1", project) or 0

    async def put_codes(self, source: str, rows: list[tuple[str, str, str, str]]) -> None:
        """One codes file's lines ((code, size, label, until) rows) in place of what it had before, and the others told."""
        async with self.tx() as c:
            await c.execute("DELETE FROM codes WHERE source=$1", source)
            if rows:
                await c.executemany("INSERT INTO codes(source, code, size, label, until) VALUES ($1,$2,$3,$4,$5) "
                                    "ON CONFLICT (source, code) DO NOTHING", [(source, *r) for r in rows])
        try:
            await self.db.notify(CODES_CHANNEL, "")
        except (DatabaseError, OSError) as e:
            log.warning("couldn't tell the other servers the codes changed: %s", e)

    async def all_codes(self) -> list[tuple[str, str, str, str, str]]:
        return [tuple(r) for r in await self.db.fetch("SELECT source, code, size, label, until FROM codes")]

    async def heads(self, projects: list[str]) -> dict[str, int]:
        """The heads of many projects at once (a relay's periodic check of the projects it has open)."""
        out: dict[str, int] = {}
        for i in range(0, len(projects), 500):
            chunk = projects[i:i + 500]
            marks = ",".join(f"${n + 1}" for n in range(len(chunk)))
            for r in await self.db.fetch(f"SELECT project, seq FROM head WHERE project IN ({marks})", *chunk):
                out[r["project"]] = int(r["seq"])
        return out

    async def floor(self, project: str, c=None) -> int:
        """Entries up to this sequence number were dropped by eviction."""
        return await (c or self.db).fetchval("SELECT seq FROM floor WHERE project=$1", project) or 0

    async def duplicate(self, project: str, data: bytes, c=None) -> int | None:
        """A replay of an update already stored (its seq), or already compacted away (0). Exact in a
        transaction holding the project (`locked`), which then stores it or not."""
        h = hashlib.sha256(data).digest()
        seq = await (c or self.db).fetchval(
            "SELECT COALESCE((SELECT seq FROM log WHERE project=$1 AND h=$2 LIMIT 1),"
            " (SELECT 0 FROM gone WHERE project=$1 AND h=$2), (SELECT 0 FROM kept WHERE project=$1 AND h=$2))", project, h)
        return None if seq is None else int(seq)

    async def append(self, project: str, data: bytes, sig: bytes, c: Tx | None = None) -> int:
        if c is None:
            async with self.locked(project) as c:
                return await self.append(project, data, sig, c)
        seq = await c.fetchval("INSERT INTO head(project, seq) VALUES ($1, 1) "
                               "ON CONFLICT (project) DO UPDATE SET seq = head.seq + 1 RETURNING seq", project)
        await c.execute("INSERT INTO log(project, seq, data, sig, h) VALUES ($1,$2,$3,$4,$5)",
                        project, seq, data, sig, hashlib.sha256(data).digest())
        await c.execute("INSERT INTO usage(project, bytes) VALUES ($1,$2) "
                        "ON CONFLICT (project) DO UPDATE SET bytes = usage.bytes + excluded.bytes", project, len(data) + len(sig))
        c.later.append(lambda: self._grew(project, len(data) + len(sig)))
        c.later.append(lambda: self.evicted.discard(project))
        return int(seq)

    async def same_entry(self, project: str, seq: int, h: bytes) -> bool | None:
        """Is the change numbered `seq` here the one with content hash `h` (a client's record of the
        last change it has)? None when it can't be told: no change of that number is known any
        more (compacted long ago, evicted), or it is a snapshot's number."""
        rows = await self.db.fetch("SELECT h FROM log WHERE project=$1 AND seq=$2 AND NOT snap"
                                   " UNION ALL SELECT h FROM gone WHERE project=$1 AND seq=$2", project, seq)
        if not rows:
            return None
        return any(bytes(r["h"]) == h for r in rows)

    async def last_snapshot(self, project: str, c=None) -> int:
        return await (c or self.db).fetchval("SELECT MAX(seq) FROM log WHERE project=$1 AND snap", project) or 0

    async def snapshot_delta(self, project: str, upto: int, size: int, c=None) -> int:
        """Bytes a snapshot would add (negative when it frees space)."""
        r = await (c or self.db).fetchrow(
            f"SELECT COALESCE(SUM({ENTRY_BYTES}), 0) AS freed, COUNT(*) FILTER (WHERE NOT snap) AS remembered, "
            "COUNT(*) FILTER (WHERE NOT snap AND octet_length(data)>=$4) AS kept, "
            "(SELECT COUNT(*) FROM gone WHERE project=$1 AND seq<=$3) AS forgotten FROM log WHERE project=$1 AND seq<=$2",
            project, upto, upto - GONE_KEEP, KEEP_BIG)
        return size - int(r["freed"]) + (r["remembered"] + r["kept"] - r["forgotten"]) * GONE_COST

    async def snapshot(self, project: str, upto: int, data: bytes, sig: bytes, c: Tx | None = None) -> None:
        if c is None:
            async with self.locked(project) as c:
                return await self.snapshot(project, upto, data, sig, c)
        # remember what the snapshot replaces, so those updates can't be stored again
        await c.execute("INSERT INTO gone(project, h, seq) SELECT project, h, seq FROM log "
                        "WHERE project=$1 AND seq<=$2 AND NOT snap ON CONFLICT DO NOTHING", project, upto)
        await c.execute("INSERT INTO kept(project, h) SELECT project, h FROM log WHERE project=$1 AND seq<=$2 AND NOT snap "
                        "AND octet_length(data)>=$3 ON CONFLICT DO NOTHING", project, upto, KEEP_BIG)
        await c.execute("DELETE FROM gone WHERE project=$1 AND seq<=$2", project, upto - GONE_KEEP)
        await c.execute("DELETE FROM log WHERE project=$1 AND seq<=$2", project, upto)
        await c.execute("INSERT INTO log(project, seq, snap, data, sig, h) VALUES ($1,$2,true,$3,$4,$5)",
                        project, upto, data, sig, hashlib.sha256(data).digest())
        await self._recount(project, c)

    async def catch_up_start(self, project: str, since: int) -> int:
        """Where a catch-up from `since` begins: before the last snapshot, if `since` is behind it
        (the snapshot is then sent too)."""
        snap = await self.last_snapshot(project)
        return snap - 1 if snap and since < snap else since

    async def since_size(self, project: str, start: int) -> int:
        """Bytes that since() would return."""
        return int(await self.db.fetchval(
            f"SELECT COALESCE(SUM({ENTRY_BYTES}), 0) FROM log WHERE project=$1 AND seq>$2",
            project, start))

    async def since(self, project: str, start: int, limit: int | None = None) -> tuple[list[dict], int, bool]:
        """Entries after `start`, in order: (entries, the last one's seq, whether that's all). With
        `limit`, about that many bytes at most (always at least one entry): the sizes are read
        first, then only that page's rows, so a long log goes out without ever being in memory whole."""
        upto, done = None, True
        if limit:
            sizes = await self.db.fetch(f"SELECT seq, {ENTRY_BYTES} AS n FROM log"
                                        " WHERE project=$1 AND seq>$2 ORDER BY seq LIMIT 4096", project, start)
            size, done = 0, len(sizes) < 4096
            for r in sizes:
                if upto is not None and size + r["n"] > limit:
                    done = False
                    break
                size, upto = size + r["n"], r["seq"]
            if upto is None:
                return [], start, True
        rows = await self.db.fetch("SELECT seq, snap, data, sig FROM log WHERE project=$1 AND seq>$2 AND seq<=$3 ORDER BY seq",
                                     project, start, upto if upto is not None else 2**62)
        out = [{"q": r["seq"], "snap": bool(r["snap"]), "d": b64e(r["data"]), "s": b64e(r["sig"])} for r in rows]
        return out, (rows[-1]["seq"] if rows else start), done

    async def log_state(self, project: str) -> tuple[int, int, int]:
        """The log's head, floor and number of entries, in one query."""
        r = await self.db.fetchrow(
            "SELECT COALESCE((SELECT seq FROM head WHERE project=$1), 0) AS head,"
            " COALESCE((SELECT seq FROM floor WHERE project=$1), 0) AS floor,"
            " (SELECT COUNT(*) FROM log WHERE project=$1) AS count", project)
        return r["head"], r["floor"], r["count"]

    # --- storage mode & revocations ---------------------------------------------------
    async def revoked(self, project: str) -> dict[str, bytes]:
        """Personal keys the owner revoked: canonical pub -> owner's signature."""
        known = self._revoked.get(project)
        if known is None:
            rows = await self.db.fetch("SELECT pub, sig FROM revoked WHERE project=$1", project)
            known = {c: r["sig"] for r in rows if (c := canonical_pub(r["pub"]))}
            self._revoked.put(project, known)
        return known

    async def revoke(self, project: str, pub: str, sig: bytes) -> bool:
        """Record a revocation (`pub` canonical). False if this project has too many already."""
        known = await self.revoked(project)
        if pub in known:
            return True
        if len(known) >= MAX_REVOKED:
            return False
        await self.db.execute("INSERT INTO revoked(project, pub, sig, at) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                                project, pub, sig, time.time())
        self._revoked.drop(project)
        await self._told(project)  # (the other servers drop what they knew of its revocations)
        return True

    async def _fact(self, project: str) -> tuple[dict | None, bool | None]:
        """(its deletion message or None, its storage mode or None), cached."""
        fact = self._facts.get(project)
        if fact is not None:
            return fact
        r = await self.db.fetchrow(
            "SELECT d.at, d.ts, d.sig, (SELECT relay FROM mode WHERE project=$1) AS relay "
            "FROM (SELECT 1) one LEFT JOIN deleted d ON d.project=$1", project)
        fact = (self._deleted_msg(r["at"], r["ts"], r["sig"]) if r["sig"] is not None else None,
                None if r["relay"] is None else bool(r["relay"]))
        self._facts.put(project, fact)
        return fact

    async def deleted(self, project: str) -> dict | None:
        """The owner-signed message saying the project was deleted for everyone, if it was."""
        return (await self._fact(project))[0]

    async def relay_only(self, project: str) -> bool:
        return bool((await self._fact(project))[1])

    async def has_mode(self, project: str) -> bool:
        return (await self._fact(project))[1] is not None

    async def deleted_count(self) -> int:
        return await self.db.fetchval("SELECT COUNT(*) FROM deleted")

    async def set_mode(self, project: str, relay_only: bool, c: Tx | None = None) -> None:
        if c is None:
            async with self.locked(project) as c:
                return await self.set_mode(project, relay_only, c)
        await c.execute("INSERT INTO mode(project, relay) VALUES ($1,$2) "
                        "ON CONFLICT (project) DO UPDATE SET relay=excluded.relay", project, relay_only)
        if relay_only:  # forget everything we stored for this project, as eviction does
            await self._forget_log(c, project)
            await self._trash_files(project, c)
            c.later.append(lambda: self.evicted.add(project))
        else:
            await self._recount(project, c)
        c.later.append(lambda: self._facts.drop(project))  # (read again when asked)
        c.later.append(lambda: self._told(project))

    # --- deleted for everyone ---------------------------------------------------------
    @staticmethod
    def _deleted_msg(at: float, ts: int, sig: bytes) -> dict:
        return {"t": "deleted", "at": int(at * 1000), "ts": ts, "s": b64e(sig)}

    async def delete_project(self, project: str, sig: bytes, ts: int, c: Tx | None = None) -> dict:
        """The owner deleted the project for everyone: forget what's stored and keep, for good, the
        fact (the owner's signature proves it), so that no copy can bring it back here."""
        if c is None:
            async with self.locked(project) as c:
                return await self.delete_project(project, sig, ts, c)
        await c.execute("INSERT INTO deleted(project, at, sig, ts) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                        project, time.time(), sig, ts)
        # the tombstone above is all that's kept: its registration (owner, dates) goes too
        for table in ("log", "gone", "kept", "head", "floor", "mode", "projects", "usage", "reserved"):
            await c.execute(f"DELETE FROM {table} WHERE project=$1", project)
        await self._trash_files(project, c)
        r = await c.fetchrow("SELECT at, ts, sig FROM deleted WHERE project=$1", project)  # the first deletion's, if there were two

        def forget() -> None:
            self._facts.drop(project)
            self.owners.pop(project, None)  # its bytes came off its owner in _recount (before this)
            self.usage.pop(project, None)
            self.seen.pop(project, None)
            self.evicted.discard(project)
        c.later.append(forget)
        c.later.append(lambda: self._told(project))
        return self._deleted_msg(r["at"], r["ts"], bytes(r["sig"]))

    # --- stored files ---------------------------------------------------------------
    async def _trash_files(self, project: str, c: Tx) -> None:
        """In `c`, holding the project: erase its stored files. Once it commits they no longer count or can be
        fetched, and their content is erased in the background (see _reap)."""
        await c.execute("INSERT INTO trash(project, id) SELECT project, id FROM stored WHERE project=$1 ON CONFLICT DO NOTHING", project)
        await c.execute("DELETE FROM stored WHERE project=$1", project)
        await self._recount(project, c)
        c.later.append(self._kick_reaper)

    def _kick_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.get_running_loop().create_task(self._reap())

    async def _reap(self) -> None:
        while True:
            rows = await self.db.fetch("SELECT project, id FROM trash LIMIT 100")
            if not rows:
                return
            for project, bid in rows:
                key = f"{project}/{bid}"
                async with self._holding(key):
                    if not await self.db.fetchval("SELECT 1 FROM trash WHERE project=$1 AND id=$2", project, bid):
                        continue  # uploaded again meanwhile: it's a stored file once more
                    try:
                        await self.files.delete(key)
                    except (FilesError, OSError):
                        log.exception("erasing a stored file (tried again later)")
                        return
                    await self.db.execute("DELETE FROM trash WHERE project=$1 AND id=$2", project, bid)

    async def empty_trash(self) -> None:
        """Erase everything in the trash now (the command line's --purge-code)."""
        await self.files.check()
        if self._reaper and not self._reaper.done():
            await self._reaper
        await self._reap()

    async def blob_size(self, project: str, bid: str, c=None) -> int | None:
        """A stored file's size; None if it isn't stored (or is in the trash)."""
        n = await (c or self.db).fetchval("SELECT size FROM stored WHERE project=$1 AND id=$2", project, bid)
        return None if n is None else int(n)

    async def has_blob(self, project: str, bid: str, c=None) -> bool:
        return await self.blob_size(project, bid, c) is not None

    def reserve(self, project: str, n: int) -> None:
        """Count an upload in this server's memory while it's under way (`pending`; put_blob takes it back)."""
        self.pending[project] += n
        self._grew(project, n)

    async def reserve_upload(self, project: str, n: int, c: Tx) -> str:
        """In `c`, holding the project, once the upload is allowed (Policy.may_store): count it against the
        project and its owner until put_blob, given the returned id, has stored it (or RESERVE_TIME has passed),
        so that writes through any server meanwhile see it."""
        rid = secrets.token_hex(8)
        owner = await self.owner_of(project, c) or "default"
        await c.execute("INSERT INTO reserved(id, project, owner, bytes, until) VALUES ($1,$2,$3,$4,$5)",
                        rid, project, owner, n, time.time() + RESERVE_TIME)
        c.later.append(lambda: self.reserve(project, n))
        return rid

    async def put_blob(self, project: str, bid: str, data: bytes | memoryview, kind: str = "file",
                       reservation: str | None = None) -> bool:
        """Store a file's content (counted from the start: with a `reservation` (reserve_upload), from when it
        was allowed). False if it was stored already. FilesError if the file store failed."""
        n = len(data)
        if reservation is None:
            self.reserve(project, n)
        key = f"{project}/{bid}"
        try:
            async with self._holding(key):
                if await self.has_blob(project, bid):
                    if kind == "file":
                        await self.mark_file(project, bid)
                    return False
                await self.files.put(key, data)
                async with self.locked(project) as c:
                    await c.execute("INSERT INTO stored(project, id, size, kind) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                                    project, bid, n, kind)
                    await c.execute("DELETE FROM trash WHERE project=$1 AND id=$2", project, bid)  # not to be erased now
                    c.later.append(lambda: self.evicted.discard(project))
                return True
        finally:
            self.pending[project] -= n
            if not self.pending[project]:
                del self.pending[project]
            # the stored size instead of the reservation (or nothing), both at once
            async with self.locked(project) as c:
                if reservation is not None:
                    await c.execute("DELETE FROM reserved WHERE id=$1", reservation)
                await self._recount(project, c)

    async def mark_file(self, project: str, bid: str, c=None) -> None:
        """Content uploaded as a project file is a file, even if it was first stored as output."""
        await (c or self.db).execute("UPDATE stored SET kind='file' WHERE project=$1 AND id=$2 AND kind<>'file'", project, bid)

    async def delete_blob(self, project: str, bid: str, any_kind: bool) -> bool:
        async with self.locked(project) as c:
            n = await c.execute("DELETE FROM stored WHERE project=$1 AND id=$2" + ("" if any_kind else " AND kind='output'"),
                                project, bid)
            if n:
                await c.execute("INSERT INTO trash(project, id) VALUES ($1,$2) ON CONFLICT DO NOTHING", project, bid)
                await self._recount(project, c)
                c.later.append(self._kick_reaper)
        return bool(n)

    def read_blob(self, project: str, bid: str):
        """A stored file's content, a piece at a time."""
        return self.files.get(f"{project}/{bid}")

    # --- copies of the stored files, for backups and restores ------------------------
    async def files_to_folder(self, dest: Path) -> tuple[int, int]:
        """Make `dest` hold exactly the stored files (<project>/<id>), copying only those it lacks
        (a file never changes, so one already there is the same). Returns (copied, removed)."""
        want = {(r["project"], r["id"]): r["size"] for r in await self.db.fetch("SELECT project, id, size FROM stored")}
        copied = removed = 0
        for (project, bid), size in want.items():
            path = dest / project / bid
            if path.is_file() and path.stat().st_size == size:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_name(path.name + ".part")
            try:
                with open(part, "wb") as f:  # noqa: ASYNC230 - the command line's, not the server's
                    async for piece in self.files.get(f"{project}/{bid}"):
                        f.write(piece)
            except FileNotFoundError:
                part.unlink(missing_ok=True)
                log.warning("stored file %s/%s… isn't in the file store", project[:8], bid[:8])
                continue
            os.replace(part, path)
            copied += 1
        for path in list(dest.glob("*/*")):
            if path.is_file() and (path.parent.name, path.name) not in want:
                path.unlink()
                removed += 1
        for folder in list(dest.iterdir()) if dest.is_dir() else []:
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        return copied, removed

    async def files_from_folder(self, src: Path) -> tuple[int, int]:
        """Put back into the file store every stored file it lacks, from `src` (<project>/<id>, as
        files_to_folder or a folder store leaves them). Returns (copied, still missing)."""
        rows = await self.db.fetch("SELECT project, id FROM stored")
        copied = missing = 0
        for project, bid in rows:
            key = f"{project}/{bid}"
            if await self.files.exists(key):
                continue
            path = src / project / bid
            if not path.is_file():
                missing += 1
                continue
            await self.files.put(key, await asyncio.to_thread(path.read_bytes))
            copied += 1
        return copied, missing

    # --- the admin page and app versions ----------------------------------------------
    async def note_client(self, app: str, version: str) -> None:
        await self.db.execute("INSERT INTO clients(app, version, n, seen) VALUES ($1,$2,1,$3) "
                                "ON CONFLICT (app, version) DO UPDATE SET n = clients.n + 1, seen = excluded.seen",
                                app, version, time.time())

    async def admin_rows(self) -> tuple[dict[str, bool], list, list, dict[str, int]]:
        """Storage modes, project registrations, the apps that connect, and each project's latest
        snapshot's size (its log entry: a snapshot too big for a message is an upload, and its
        entry a short reference to it)."""
        c = self.db
        modes = {r["project"]: bool(r["relay"]) for r in await c.fetch("SELECT project, relay FROM mode")}
        projects = [tuple(r) for r in await c.fetch("SELECT project, created, owner, seen, evicted FROM projects")]
        apps = [tuple(r) for r in await c.fetch("SELECT app, version, n, seen FROM clients ORDER BY seen DESC LIMIT 200")]
        snaps = {r["project"]: r["n"] for r in await c.fetch(
            "SELECT l.project, octet_length(l.data) AS n FROM log l JOIN (SELECT project, MAX(seq) AS seq FROM log"
            " WHERE snap GROUP BY project) newest ON l.project = newest.project AND l.seq = newest.seq")}
        return modes, projects, apps, snaps


async def copy_database(src_url: str, dst_url: str, progress=None) -> dict[str, int]:
    """Copy everything in one server's database into a new one (SQLite to PostgreSQL, say). Neither
    may be in use: a server on either SQLite file is refused; for PostgreSQL, stop it first."""
    if src_url == dst_url:
        raise DatabaseError("that's the same database: copy into a new one")
    src = await connect(src_url, SCHEMA, SCHEMA_POSTGRESQL, serving=True)
    try:
        dst = await connect(dst_url, SCHEMA, SCHEMA_POSTGRESQL, serving=True)
        try:
            return await copy_tables(src, dst, TABLES, BOOLEANS, progress)
        finally:
            await dst.close()
    finally:
        await src.close()
