"""The relay's store: projects' encrypted edit logs and what's known about them, in PostgreSQL.

What's kept per project: the log of signed, encrypted updates (and snapshots that replace part of
it), its head and floor, the storage mode, revoked personal links, who owns it (a creation code),
which files are stored (their content is in a file store, files.py), and, for a project deleted
for everyone, the owner's signed tombstone. The server can read none of the projects' content.

Edits to one project are numbered in the database (`head`, updated and read back in the same
transaction as the log row), so the numbering stays right with several servers. Within this
server, `lock(project)` keeps one project's check-then-write steps (is it a replay? may it be
stored? store it) from interleaving, as they did when this was synchronous code on SQLite.

Some things are also kept in memory, because they're consulted on every message: how much each
project and owner stores, owners, deletions, last-seen times, storage modes and revocations.
They're right for one server; several at once need them shared (see ROADMAP.md).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import os
import shutil
import time
from collections import defaultdict
from pathlib import Path

import asyncpg

from texloom.crypto import canonical_pub

from .files import Files, FilesError, FolderFiles

log = logging.getLogger("texloom.relay")

MAX_REVOKED = 1000  # revocations kept per project
# Compacted edits are remembered by hash so a replay of one isn't stored again. Only the most
# recent are kept per project, and each counts against the allowance, so the table can't grow
# without limit. Replaying something older is harmless: edits are idempotent, and the copy
# counts against the allowance only until the next compaction.
GONE_KEEP = 10_000  # sequence numbers
GONE_COST = 100  # bytes a remembered edit counts for
# Rows for projects that store nothing (relay-only mode, revocations) cost nothing to create,
# so they come out of a server-wide budget: a burst of META_BURST, then META_RATE per second.
META_RATE, META_BURST = 1.0, 600
POOL_MAX = 10  # database connections

SCHEMA = """
CREATE TABLE IF NOT EXISTS log (
  project text NOT NULL, seq bigint NOT NULL, snap boolean NOT NULL DEFAULT false,
  data bytea NOT NULL, sig bytea NOT NULL,
  h bytea NOT NULL,  -- content hash, to recognise replayed updates
  PRIMARY KEY (project, seq));
CREATE INDEX IF NOT EXISTS log_h ON log (project, h);
-- ciphertext doesn't compress: store it as it is, out of line, without trying
ALTER TABLE log ALTER COLUMN data SET STORAGE EXTERNAL;
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
"""
TABLES = ("log", "head", "mode", "stored", "trash", "revoked", "projects", "gone", "floor", "deleted", "clients", "lists")


def changed(status: str) -> int:
    """Rows a statement changed, from its status ("DELETE 3", "INSERT 0 1", "UPDATE 0")."""
    try:
        return int(status.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Store:
    """Open with `await Store.open(dsn, ...)`; close with `await store.close()`."""

    def __init__(self, pool: asyncpg.Pool, files: Files, data: Path):
        self.pool = pool
        self.files = files
        self.dir = data  # this server's own disk: uploads are spooled here, and it must keep room
        self._revoked: dict[str, dict[str, bytes]] = {}
        self._modes: dict[str, bool] = {}  # project -> relay-only, for projects that have a mode
        self.usage: dict[str, int] = defaultdict(int)
        self.total = 0
        self.owners: dict[str, str] = {}
        self.deleted: dict[str, dict] = {}  # project -> the message telling clients, signed by the owner
        self.owner_bytes: dict[str, int] = defaultdict(int)  # checked on every stored edit: kept, not added up
        self.seen: dict[str, float] = {}
        self.meta_tokens, self.meta_at = float(META_BURST), time.monotonic()
        self.pending: dict[str, int] = defaultdict(int)  # bytes of uploads under way (see put_blob)
        self._reaper: asyncio.Task | None = None
        self._locks: dict[str, list] = {}  # key -> [lock, users]

    @classmethod
    async def open(cls, dsn: str, files: Files | None = None, data: Path = Path("data")) -> Store:
        data.mkdir(parents=True, exist_ok=True)
        try:
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=POOL_MAX, command_timeout=120)
        except (OSError, asyncpg.PostgresError) as e:
            raise SystemExit(f"can't reach the database {redact(dsn)}: {e}") from None
        async with pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(7316)")  # several servers starting at once
            await c.execute(SCHEMA)
            # a deleted project keeps only its tombstone
            await c.execute("DELETE FROM projects WHERE project IN (SELECT project FROM deleted)")
        s = cls(pool, files or FolderFiles(data / "files"), data)
        await s._load()
        return s

    async def _load(self) -> None:
        async with self.pool.acquire() as c:
            for q in ("SELECT project, SUM(octet_length(data) + octet_length(sig)) FROM log GROUP BY project",
                      "SELECT project, SUM(size) FROM stored GROUP BY project",
                      f"SELECT project, COUNT(*) * {GONE_COST} FROM gone GROUP BY project"):
                for project, n in await c.fetch(q):
                    self.usage[project] += int(n or 0)
            self.total = sum(self.usage.values())
            self.owners = {r["project"]: r["owner"] for r in await c.fetch("SELECT project, owner FROM projects")}
            self.deleted = {r["project"]: self._deleted_msg(r["at"], r["ts"], r["sig"])
                            for r in await c.fetch("SELECT project, at, ts, sig FROM deleted")}
            self.seen = {r["project"]: max(r["seen"], r["created"]) for r in await c.fetch("SELECT project, seen, created FROM projects")}
            self._modes = {r["project"]: r["relay"] for r in await c.fetch("SELECT project, relay FROM mode")}
        for project, n in self.usage.items():
            if project in self.owners:
                self.owner_bytes[self.owners[project]] += n

    async def start(self) -> None:
        """Before serving: check that the file store works, drop files a stop left behind for
        projects that keep none, and carry on erasing what's in the trash. (An evicted project's
        leftovers stay: its writers may have uploaded again since, and what's there is valid.)"""
        await self.files.check()
        log.info("stored files: %s", self.files.name)
        left = await self.pool.fetch("SELECT DISTINCT project FROM stored WHERE project IN "
                                     "(SELECT project FROM deleted UNION SELECT project FROM mode WHERE relay)")
        for (project,) in left:
            await self._drop_files(project)
        self._kick_reaper()

    async def close(self) -> None:
        if self._reaper and not self._reaper.done():
            self._reaper.cancel()
        await self.files.close()
        await self.pool.close()

    @contextlib.asynccontextmanager
    async def tx(self):
        async with self.pool.acquire() as c, c.transaction():
            yield c

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
        """Hold while checking and then changing what's stored for a project, so that another
        message for it can't change things in between."""
        return self._holding(f"project:{project}")

    # --- registration & accounting -------------------------------------------
    def owner(self, project: str) -> str | None:
        return self.owners.get(project)

    async def register(self, project: str, owner: str, label: str) -> None:
        await self.pool.execute("INSERT INTO projects(project, created, owner) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                                project, time.time(), owner)
        self._own(project, owner)
        log.info("registered project %s… (%s)", project[:8], label)

    async def claim(self, project: str, owner: str) -> None:
        """Move a project created without a code to `owner` (a private server's re-registration)."""
        await self.pool.execute("UPDATE projects SET owner=$1 WHERE project=$2", owner, project)
        self._own(project, owner)
        log.info("project %s… now belongs to a creation code", project[:8])

    def _own(self, project: str, owner: str) -> None:
        n = self.usage.get(project, 0)
        if project in self.owners:
            self.owner_bytes[self.owners[project]] -= n
        self.owner_bytes[owner] += n
        self.owners[project] = owner

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
        await self.pool.execute("UPDATE projects SET seen=$1, evicted=0 WHERE project=$2", now, project)

    def idle(self, days: float) -> list[str]:
        """Stored projects nobody has opened for `days`."""
        cutoff = time.time() - days * 86400
        return [p for p, n in self.usage.items() if n > 0 and self.seen.get(p, 0) < cutoff]

    async def evict(self, project: str) -> int:
        """Drop the stored copy (log and files) of an idle project. Its writers re-upload it
        when they next connect; until then, link holders without a copy can't open it.
        Sequence numbers keep counting (so old entries can't be replayed into the gap);
        `floor` tells clients that everything up to it is gone."""
        freed = self.usage.get(project, 0)
        async with self.lock(project), self.tx() as c:
            await c.execute("DELETE FROM log WHERE project=$1", project)
            await c.execute("DELETE FROM gone WHERE project=$1", project)
            await c.execute("INSERT INTO floor(project, seq) SELECT $1, COALESCE((SELECT seq FROM head WHERE project=$1), 0) "
                            "ON CONFLICT (project) DO UPDATE SET seq=excluded.seq", project)
            await c.execute("UPDATE projects SET evicted=$1 WHERE project=$2", time.time(), project)
        await self._drop_files(project)
        return freed

    def owner_usage(self, owner: str) -> int:
        return self.owner_bytes.get(owner, 0)

    def stored_by(self, owner: str) -> list[str]:
        """Projects registered to this owner that have something stored."""
        return [p for p, o in self.owners.items() if o == owner and self.usage.get(p, 0) > 0]

    async def _recount(self, project: str) -> None:
        n = await self.pool.fetchval(
            "SELECT (SELECT COALESCE(SUM(octet_length(data) + octet_length(sig)), 0) FROM log WHERE project=$1)"
            f" + (SELECT COALESCE(SUM(size), 0) FROM stored WHERE project=$1) + {GONE_COST} * (SELECT COUNT(*) FROM gone WHERE project=$1)",
            project)
        self._grew(project, int(n) + self.pending.get(project, 0) - self.usage[project])

    def free_disk(self) -> int:
        return shutil.disk_usage(self.dir).free

    def free_for_data(self) -> int:
        """Room for new data on this server's disk."""
        return self.free_disk()

    # --- log ---------------------------------------------------------------------
    async def head(self, project: str) -> int:
        return await self.pool.fetchval("SELECT seq FROM head WHERE project=$1", project) or 0

    async def floor(self, project: str) -> int:
        """Entries up to this sequence number were dropped by eviction."""
        return await self.pool.fetchval("SELECT seq FROM floor WHERE project=$1", project) or 0

    async def duplicate(self, project: str, data: bytes) -> int | None:
        """A replay of an update already stored (its seq), or already compacted away (0)."""
        h = hashlib.sha256(data).digest()
        seq = await self.pool.fetchval(
            "SELECT COALESCE((SELECT seq FROM log WHERE project=$1 AND h=$2 LIMIT 1),"
            " (SELECT 0 FROM gone WHERE project=$1 AND h=$2))", project, h)
        return None if seq is None else int(seq)

    async def append(self, project: str, data: bytes, sig: bytes) -> int:
        async with self.tx() as c:
            seq = await c.fetchval("INSERT INTO head(project, seq) VALUES ($1, 1) "
                                   "ON CONFLICT (project) DO UPDATE SET seq = head.seq + 1 RETURNING seq", project)
            await c.execute("INSERT INTO log(project, seq, data, sig, h) VALUES ($1,$2,$3,$4,$5)",
                            project, seq, data, sig, hashlib.sha256(data).digest())
        self._grew(project, len(data) + len(sig))
        return int(seq)

    async def last_snapshot(self, project: str) -> int:
        return await self.pool.fetchval("SELECT MAX(seq) FROM log WHERE project=$1 AND snap", project) or 0

    async def snapshot_delta(self, project: str, upto: int, size: int) -> int:
        """Bytes a snapshot would add (negative when it frees space)."""
        r = await self.pool.fetchrow(
            "SELECT COALESCE(SUM(octet_length(data) + octet_length(sig)), 0) AS freed, COUNT(*) FILTER (WHERE NOT snap) AS remembered, "
            "(SELECT COUNT(*) FROM gone WHERE project=$1 AND seq<=$3) AS forgotten FROM log WHERE project=$1 AND seq<=$2",
            project, upto, upto - GONE_KEEP)
        return size - int(r["freed"]) + (r["remembered"] - r["forgotten"]) * GONE_COST

    async def snapshot(self, project: str, upto: int, data: bytes, sig: bytes) -> None:
        async with self.tx() as c:
            # remember what the snapshot replaces, so those updates can't be stored again
            await c.execute("INSERT INTO gone(project, h, seq) SELECT project, h, seq FROM log "
                            "WHERE project=$1 AND seq<=$2 AND NOT snap ON CONFLICT DO NOTHING", project, upto)
            await c.execute("DELETE FROM gone WHERE project=$1 AND seq<=$2", project, upto - GONE_KEEP)
            await c.execute("DELETE FROM log WHERE project=$1 AND seq<=$2", project, upto)
            await c.execute("INSERT INTO log(project, seq, snap, data, sig, h) VALUES ($1,$2,true,$3,$4,$5)",
                            project, upto, data, sig, hashlib.sha256(data).digest())
        await self._recount(project)

    async def _since_start(self, project: str, since: int) -> int:
        snap = await self.last_snapshot(project)
        return snap - 1 if snap and since < snap else since  # behind the snapshot: send it too

    async def since_size(self, project: str, since: int) -> int:
        """Bytes that since() would return."""
        return int(await self.pool.fetchval(
            "SELECT COALESCE(SUM(octet_length(data) + octet_length(sig)), 0) FROM log WHERE project=$1 AND seq>$2",
            project, await self._since_start(project, since)))

    async def since(self, project: str, since: int) -> list[dict]:
        rows = await self.pool.fetch("SELECT seq, snap, data, sig FROM log WHERE project=$1 AND seq>$2 ORDER BY seq",
                                     project, await self._since_start(project, since))
        return [{"q": r["seq"], "snap": r["snap"], "d": b64e(r["data"]), "s": b64e(r["sig"])} for r in rows]

    async def count(self, project: str) -> int:
        return await self.pool.fetchval("SELECT COUNT(*) FROM log WHERE project=$1", project)

    # --- storage mode & revocations ---------------------------------------------------
    async def revoked(self, project: str) -> dict[str, bytes]:
        """Personal keys the owner revoked: canonical pub -> owner's signature."""
        if project not in self._revoked:
            rows = await self.pool.fetch("SELECT pub, sig FROM revoked WHERE project=$1", project)
            self._revoked[project] = {c: r["sig"] for r in rows if (c := canonical_pub(r["pub"]))}
            if len(self._revoked) > 10000:
                self._revoked.pop(next(iter(self._revoked)))
        return self._revoked[project]

    async def revoke(self, project: str, pub: str, sig: bytes) -> bool:
        """Record a revocation (`pub` canonical). False if this project has too many already."""
        known = await self.revoked(project)
        if pub in known:
            return True
        if len(known) >= MAX_REVOKED:
            return False
        await self.pool.execute("INSERT INTO revoked(project, pub, sig, at) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                                project, pub, sig, time.time())
        self._revoked.pop(project, None)
        return True

    def relay_only(self, project: str) -> bool:
        return self._modes.get(project, False)

    def has_mode(self, project: str) -> bool:
        return project in self._modes

    async def set_mode(self, project: str, relay_only: bool) -> None:
        async with self.tx() as c:
            await c.execute("INSERT INTO mode(project, relay) VALUES ($1,$2) "
                            "ON CONFLICT (project) DO UPDATE SET relay=excluded.relay", project, relay_only)
            if relay_only:  # forget everything we stored for this project
                await c.execute("DELETE FROM log WHERE project=$1", project)
                await c.execute("DELETE FROM gone WHERE project=$1", project)
        self._modes[project] = relay_only
        if relay_only:
            await self._drop_files(project)
        else:
            await self._recount(project)

    # --- deleted for everyone ---------------------------------------------------------
    @staticmethod
    def _deleted_msg(at: float, ts: int, sig: bytes) -> dict:
        return {"t": "deleted", "at": int(at * 1000), "ts": ts, "s": b64e(sig)}

    async def delete_project(self, project: str, sig: bytes, ts: int) -> dict:
        """The owner deleted the project for everyone: forget what's stored and keep, for good, the
        fact (the owner's signature proves it), so that no copy can bring it back here."""
        at = time.time()
        async with self.tx() as c:
            await c.execute("INSERT INTO deleted(project, at, sig, ts) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING", project, at, sig, ts)
            # the tombstone above is all that's kept: its registration (owner, dates) goes too
            for table in ("log", "gone", "head", "floor", "mode", "projects"):
                await c.execute(f"DELETE FROM {table} WHERE project=$1", project)
        msg = self.deleted.setdefault(project, self._deleted_msg(at, ts, sig))  # a second deletion keeps the first
        self._modes.pop(project, None)
        await self._drop_files(project)
        self.owners.pop(project, None)  # its bytes came off its owner in _recount
        self.usage.pop(project, None)
        self.seen.pop(project, None)
        return msg

    # --- stored files ---------------------------------------------------------------
    async def _drop_files(self, project: str) -> None:
        """Erase a project's stored files: at once they no longer count or can be fetched, and their
        content is erased in the background (see _reap)."""
        async with self.tx() as c:
            await c.execute("INSERT INTO trash(project, id) SELECT project, id FROM stored WHERE project=$1 ON CONFLICT DO NOTHING", project)
            await c.execute("DELETE FROM stored WHERE project=$1", project)
        await self._recount(project)
        self._kick_reaper()

    def _kick_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.get_running_loop().create_task(self._reap())

    async def _reap(self) -> None:
        while True:
            rows = await self.pool.fetch("SELECT project, id FROM trash LIMIT 100")
            if not rows:
                return
            for project, bid in rows:
                key = f"{project}/{bid}"
                async with self._holding(key):
                    if not await self.pool.fetchval("SELECT 1 FROM trash WHERE project=$1 AND id=$2", project, bid):
                        continue  # uploaded again meanwhile: it's a stored file once more
                    try:
                        await self.files.delete(key)
                    except (FilesError, OSError):
                        log.exception("erasing a stored file (tried again later)")
                        return
                    await self.pool.execute("DELETE FROM trash WHERE project=$1 AND id=$2", project, bid)

    async def empty_trash(self) -> None:
        """Erase everything in the trash now (the command line's --purge-code)."""
        await self.files.check()
        if self._reaper and not self._reaper.done():
            await self._reaper
        await self._reap()

    async def blob_size(self, project: str, bid: str) -> int | None:
        """A stored file's size; None if it isn't stored (or is in the trash)."""
        n = await self.pool.fetchval("SELECT size FROM stored WHERE project=$1 AND id=$2", project, bid)
        return None if n is None else int(n)

    async def has_blob(self, project: str, bid: str) -> bool:
        return await self.blob_size(project, bid) is not None

    def reserve(self, project: str, n: int) -> None:
        """Count an upload from the moment it's accepted, so uploads under way at once can't together
        pass a limit (put_blob takes the reservation back when it ends)."""
        self.pending[project] += n
        self._grew(project, n)

    async def put_blob(self, project: str, bid: str, data: bytes | memoryview, kind: str = "file", reserved: bool = False) -> bool:
        """Store a file's content (counted from the start: see reserve). False if it was stored
        already. FilesError if the file store failed."""
        n = len(data)
        if not reserved:
            self.reserve(project, n)
        key = f"{project}/{bid}"
        try:
            async with self._holding(key):
                if await self.has_blob(project, bid):
                    if kind == "file":
                        await self.mark_file(project, bid)
                    return False
                await self.files.put(key, data)
                async with self.tx() as c:
                    await c.execute("INSERT INTO stored(project, id, size, kind) VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                                    project, bid, n, kind)
                    await c.execute("DELETE FROM trash WHERE project=$1 AND id=$2", project, bid)  # not to be erased now
                return True
        finally:
            self.pending[project] -= n
            if not self.pending[project]:
                del self.pending[project]
            await self._recount(project)  # the stored size instead of the reservation (or nothing)

    async def mark_file(self, project: str, bid: str) -> None:
        """Content uploaded as a project file is a file, even if it was first stored as output."""
        await self.pool.execute("UPDATE stored SET kind='file' WHERE project=$1 AND id=$2 AND kind<>'file'", project, bid)

    async def delete_blob(self, project: str, bid: str, any_kind: bool) -> bool:
        async with self.tx() as c:
            n = changed(await c.execute("DELETE FROM stored WHERE project=$1 AND id=$2" + ("" if any_kind else " AND kind='output'"),
                                        project, bid))
            if n:
                await c.execute("INSERT INTO trash(project, id) VALUES ($1,$2) ON CONFLICT DO NOTHING", project, bid)
        if n:
            await self._recount(project)
            self._kick_reaper()
        return bool(n)

    def read_blob(self, project: str, bid: str):
        """A stored file's content, a piece at a time."""
        return self.files.get(f"{project}/{bid}")

    # --- copies of the stored files, for backups and restores ------------------------
    async def files_to_folder(self, dest: Path) -> tuple[int, int]:
        """Make `dest` hold exactly the stored files (<project>/<id>), copying only those it lacks
        (a file never changes, so one already there is the same). Returns (copied, removed)."""
        want = {(r["project"], r["id"]): r["size"] for r in await self.pool.fetch("SELECT project, id, size FROM stored")}
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
        rows = await self.pool.fetch("SELECT project, id FROM stored")
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
        await self.pool.execute("INSERT INTO clients(app, version, n, seen) VALUES ($1,$2,1,$3) "
                                "ON CONFLICT (app, version) DO UPDATE SET n = clients.n + 1, seen = excluded.seen",
                                app, version, time.time())

    async def admin_rows(self) -> tuple[dict[str, bool], list, list]:
        """Storage modes, project registrations, and the apps that connect."""
        async with self.pool.acquire() as c:
            modes = {r["project"]: r["relay"] for r in await c.fetch("SELECT project, relay FROM mode")}
            projects = [tuple(r) for r in await c.fetch("SELECT project, created, owner, seen, evicted FROM projects")]
            apps = [tuple(r) for r in await c.fetch("SELECT app, version, n, seen FROM clients ORDER BY seen DESC")]
        return modes, projects, apps


def redact(dsn: str) -> str:
    """A database address fit for logs: without its password."""
    import re
    return re.sub(r"(://[^:/@]*:)[^@]*@", r"\1***@", dsn)


async def import_sqlite(path: Path, dsn: str) -> dict[str, int]:
    """Copy a database from before PostgreSQL (SQLite, with stored files already moved out of it)
    into an empty PostgreSQL database. Returns the rows copied per table."""
    import sqlite3
    if not Path(path).is_file():
        raise SystemExit(f"no such database: {path}")
    src = sqlite3.connect(path)  # (only read; run this with the old server stopped)
    have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "blob" in have:
        raise SystemExit(f"{path} still holds stored files: start a TeXLoom server from before PostgreSQL on it once, "
                         "which moves them out to the file store, then import it")
    store = await Store.open(dsn, FolderFiles(Path("/nonexistent")), Path(path).parent)  # (only the schema is used)
    try:
        async with store.tx() as c:
            for t in TABLES:
                if await c.fetchval(f"SELECT EXISTS (SELECT 1 FROM {t})"):
                    raise SystemExit(f"the database already has data (table {t}): import only into an empty one")
            counts = {}
            for t in TABLES:
                if t not in have:
                    continue
                theirs = {r["column_name"] for r in await c.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_name=$1", t)}
                cols = [r[1] for r in src.execute(f"PRAGMA table_info({t})") if r[1] in theirs]  # (old columns left behind)
                rows = src.execute(f"SELECT {', '.join(cols)} FROM {t}").fetchall()
                if t == "log":
                    rows = [tuple(bool(v) if col == "snap" else v for col, v in zip(cols, r)) for r in rows]
                elif t == "mode":
                    rows = [tuple(bool(v) if col == "relay" else v for col, v in zip(cols, r)) for r in rows]
                if rows:
                    await c.copy_records_to_table(t, records=rows, columns=cols)
                counts[t] = len(rows)
        return counts
    finally:
        await store.pool.close()
        src.close()
