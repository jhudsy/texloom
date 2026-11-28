"""The relay's database: SQLite or PostgreSQL, behind the few calls the store makes (issue 30).

    db = await connect("sqlite:///var/lib/texloom/texloom.db")   # or "postgresql://…"
    await db.execute(sql, *args)      # rows changed
    await db.fetch / fetchrow / fetchval(sql, *args)
    async with db.tx() as c: …        # a transaction; c has the same four calls, and executemany

Both run the same SQL (store.py, lists.py), so it is written for both: parameters are $1, $2…;
booleans come back as 0 and 1 from SQLite, so they're read with bool(); and nothing only one of
them knows (PostgreSQL's DISTINCT ON and ::casts, SQLite's INSERT OR …). An INSERT … SELECT with
ON CONFLICT has a WHERE (SQLite needs one there). What differs is here: connecting, the schema's
types, and the rule for how many servers may use one database.

- **SQLite** (the standard library's sqlite3): one file, nothing to install. It serves one server
  process at a time: a second server on the same file is refused (one-off commands such as
  --purge-code may still use it while it runs). Its calls run, one at a time, in a thread of their
  own; a transaction holds the database until it ends, so within one, use only its `c`.
- **PostgreSQL** (asyncpg): for replicas, a standby, and later several servers at once.

Three things that only matter with several servers sharing a PostgreSQL, and that SQLite (one
server) does trivially: `lock(key)` (one holder at a time, for work that must run once),
`notify(channel, payload)` and `listen(channel, callback)` (a signal to the other servers; a server
doesn't need telling about itself, so callbacks should ignore their own sender).

`copy_tables` moves everything from one to the other (`texloom-server --copy-database-from`).
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import functools
import logging
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from collections.abc import Callable
from typing import Any

SQLITE_MIN = (3, 35)  # RETURNING
POOL_MAX = 10  # PostgreSQL connections
# Seconds a statement may take. A database that is cut off (its network silent, not refusing) leaves a statement
# waiting for an answer that never comes: the relay must give up soon enough to find the new primary after a
# failover (the lab: 120 s held the relays for two minutes). Whole-table reads (the daily recount, a first start)
# say so and get LONG.
COMMAND_TIMEOUT = 20
LONG = 900
START_LOCK = 7316  # PostgreSQL advisory lock taken while creating the tables (several servers starting at once)
MAINTENANCE_LOCK = 7317  # held while a server does the upkeep that must run on one server at a time
ALONE_LOCK = 7318  # held for as long as a server with no bus (one server alone) uses the database
NOTIFY_MAX = 7000  # bytes: PostgreSQL's limit on a notification is 8000
# Seconds to reach one of the database's hosts. With several (a Patroni cluster: `host1,host2`), every one is tried at
# once, each given this long (connect_any)
CONNECT_TIMEOUT = 10


log = logging.getLogger("texloom.db")


def hosts_of(url: str) -> list[str]:
    """A PostgreSQL URL for each of `url`'s hosts (postgresql://user:pw@a:5432,b:5432/db?… gives one for a, one for b)."""
    scheme, sep, rest = url.partition("://")
    netloc, slash, path = rest.partition("/")
    userinfo, at, hosts = netloc.rpartition("@")
    return [f"{scheme}{sep}{userinfo}{at}{h}{slash}{path}" for h in hosts.split(",")] if "," in hosts else [url]


async def connect_any(url: str, *args, timeout: float = CONNECT_TIMEOUT, **kwargs):
    """A connection to the database at `url`: with several hosts, to the first that answers and is what the URL asks
    for (`target_session_attrs=primary`: the one taking writes), every host tried at once. asyncpg tries them in turn
    within one timeout, so a host that doesn't answer at all (a machine gone: its packets go nowhere) used it all, and
    the primary after a failover was never reached (the deploy rehearsal: every relay off the database for minutes)."""
    import asyncpg
    urls = hosts_of(url)
    if len(urls) == 1:
        return await asyncpg.connect(url, *args, timeout=timeout, **kwargs)
    tries = [asyncio.ensure_future(asyncpg.connect(u, *args, timeout=timeout, **kwargs)) for u in urls]
    chosen, last = None, None
    try:
        for done in asyncio.as_completed(tries):
            try:
                chosen = await done
                return chosen
            except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError,
                    asyncpg.exceptions.TargetServerAttributeNotMatched) as e:  # (a standby, when the primary is asked for)
                last = e
        raise last or OSError(f"none of the {len(urls)} hosts answered")
    finally:
        async def tidy() -> None:  # the other tries: stopped, or closed if they connected too
            for t in tries:
                t.cancel()
            for r in await asyncio.gather(*tries, return_exceptions=True):
                if isinstance(r, asyncpg.Connection) and r is not chosen:
                    await r.close()
        asyncio.ensure_future(tidy())


def unavailable(e: BaseException) -> bool:
    """Whether an error means the database can't be used just now: unreachable, restarting, or failed over (this
    server now a standby, or its name pointing elsewhere). What was asked will work again soon, so the answer is
    "try again", not a failure. Not a mistake in what was asked."""
    if isinstance(e, (OSError, TimeoutError)):  # (refused, reset, a name that doesn't resolve)
        return True
    try:
        import asyncpg
    except ImportError:
        return False
    return isinstance(e, (asyncpg.PostgresConnectionError, asyncpg.InterfaceError, asyncpg.exceptions.CannotConnectNowError,
                          asyncpg.exceptions.OperatorInterventionError, asyncpg.exceptions.ReadOnlySQLTransactionError))


class DatabaseError(Exception):
    pass


def redact(url: str) -> str:
    """A database address fit for logs: without its password."""
    return re.sub(r"(://[^:/@]*:)[^@]*@", r"\1***@", url)


def sqlite_path(url: str) -> Path | None:
    """The file of a sqlite: address (sqlite:///var/lib/texloom/texloom.db, or sqlite:texloom.db
    relative to the working folder); None for any other address."""
    if not url.startswith("sqlite:"):
        return None
    rest = url.removeprefix("sqlite:")
    if rest.startswith("//"):
        rest = rest[2:]
        if not rest.startswith("/"):
            raise DatabaseError(f"{url}: sqlite:///absolute/path, or sqlite:relative/path")
    if not rest:
        raise DatabaseError(f"{url}: which file? (sqlite:///var/lib/texloom/texloom.db)")
    return Path(rest)


async def connect(url: str, schema: str = "", postgresql_schema: str = "", serving: bool = False) -> Database:
    """Open the database at `url`, creating `schema`'s tables (and, on PostgreSQL, running
    `postgresql_schema` too) if they aren't there. `serving`: for a server, which on SQLite must be
    the only one."""
    if (path := sqlite_path(url)) is not None:
        db: Database = await SQLite.open(path, serving)
        if schema:
            await db.create(schema)
    elif url.startswith(("postgresql://", "postgres://")):
        db = await PostgreSQL.open(url)
        if schema:
            await db.create(schema + "\n" + postgresql_schema)
    else:
        raise DatabaseError(f"{redact(url)}: a database address starts sqlite: or postgresql://")
    return db


class Database(abc.ABC):
    """What the store uses. Rows read back by name (`r["seq"]`), by position, or as a tuple."""

    kind = ""

    def __init__(self, url: str):
        self.url = url

    @abc.abstractmethod
    async def execute(self, sql: str, *args) -> int:
        """Rows changed."""

    @abc.abstractmethod
    async def fetch(self, sql: str, *args, timeout: float | None = None) -> list: ...

    @abc.abstractmethod
    async def fetchrow(self, sql: str, *args): ...

    @abc.abstractmethod
    async def fetchval(self, sql: str, *args) -> Any: ...

    @abc.abstractmethod
    def tx(self) -> contextlib.AbstractAsyncContextManager[Any]:
        """A transaction, yielding its connection (execute, executemany, fetch, fetchrow, fetchval)."""

    @abc.abstractmethod
    def snapshot(self) -> contextlib.AbstractAsyncContextManager[Any]:
        """A read-only transaction seeing the database as it was when it began, whatever is written meanwhile (a
        backup of a database in use), yielding its connection (fetch, fetchrow, fetchval)."""

    @abc.abstractmethod
    async def create(self, schema: str) -> None:
        """Run `schema` (CREATE … IF NOT EXISTS), one server at a time."""

    @abc.abstractmethod
    async def close(self) -> None: ...

    # What other servers sharing this database need: nothing, for one server (SQLite).

    @contextlib.asynccontextmanager
    async def lock(self, key: int):
        """`async with db.lock(KEY) as got:` — `got` is True if this server holds the lock for the block (no
        other server does meanwhile), False if another does: skip the work then, it is being done."""
        yield True

    async def notify(self, channel: str, payload: str) -> None:
        """Tell the other servers on this database (their `listen` callbacks get `payload`)."""

    async def listen(self, channel: str, callback: Callable[[str], None]) -> None:
        """Call `callback(payload)` for what other servers `notify` on `channel`, in the event loop. After the
        connection to the database was lost and is back, it is called with "" once: something may have been
        missed, so look again at everything."""


# --- PostgreSQL -------------------------------------------------------------------------

def _changed(status: str) -> int:
    """Rows a statement changed, from its status ("DELETE 3", "INSERT 0 1", "UPDATE 0")."""
    try:
        return int(status.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0


class _PgCalls:
    """The calls, on a pool or on one connection (in a transaction)."""

    def __init__(self, on):
        self._on = on

    async def execute(self, sql: str, *args) -> int:
        return _changed(await self._on.execute(sql, *args))

    async def executemany(self, sql: str, rows) -> None:
        await self._on.executemany(sql, rows)

    async def fetch(self, sql: str, *args, timeout: float | None = None) -> list:
        """`timeout`: seconds this statement may take, if not COMMAND_TIMEOUT (LONG for a whole table)."""
        return await self._on.fetch(sql, *args, timeout=timeout)

    async def fetchrow(self, sql: str, *args):
        return await self._on.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args):
        return await self._on.fetchval(sql, *args)

    async def xlock(self, key: int) -> None:
        """In a transaction: wait for `key` (Store.key) and hold it until the transaction ends, so
        servers sharing this database take turns at it."""
        await self._on.execute("SELECT pg_advisory_xact_lock($1)", key)

    async def timeline(self) -> int:
        """The database's timeline, which goes up by one at every promotion of a standby (a failover). Read from the
        name of the WAL file being written, which follows a promotion at once: pg_control_checkpoint()'s lags it
        until the next checkpoint (the lab saw 2 s, writes already going to the new primary). Any user may read it."""
        return int(await self._on.fetchval("SELECT ('x' || substr(pg_walfile_name(pg_current_wal_lsn()), 1, 8))::bit(32)::int"))


class PostgreSQL(_PgCalls, Database):
    kind = "postgresql"

    def __init__(self, url: str, pool):
        Database.__init__(self, url)
        _PgCalls.__init__(self, pool)
        self.pool = pool
        self._listeners: dict[str, list[Callable[[str], None]]] = {}
        self._listen_task: asyncio.Task | None = None
        self._listen_conn = None  # the connection that is listening now
        self._listening = asyncio.Event()  # set once it is listening (and again after each reconnect)

    @classmethod
    async def open(cls, url: str) -> PostgreSQL:
        import asyncpg
        try:
            pool = await asyncpg.create_pool(url, min_size=1, max_size=POOL_MAX, command_timeout=COMMAND_TIMEOUT,
                                             connect=connect_any)
        except (OSError, asyncpg.PostgresError) as e:
            raise DatabaseError(f"can't reach the database {redact(url)}: {e}") from None
        try:
            standby = await pool.fetchval("SELECT pg_is_in_recovery()")
        except (OSError, asyncpg.PostgresError) as e:
            await pool.close()
            raise DatabaseError(f"can't use the database {redact(url)}: {e}") from None
        if standby:
            await pool.close()
            raise DatabaseError(f"{redact(url)} is a standby, which can't be written to. Promote it first "
                                "(INSTALL.md, \"A standby, switched by hand\"), or point `database` at the primary")
        return cls(url, pool)

    @contextlib.asynccontextmanager
    async def tx(self):
        async with self.pool.acquire() as c, c.transaction():
            yield _PgCalls(c)

    @contextlib.asynccontextmanager
    async def snapshot(self):
        async with self.pool.acquire() as c, c.transaction(isolation="repeatable_read", readonly=True):
            yield _PgCalls(c)

    async def create(self, schema: str) -> None:
        async with self.pool.acquire() as c, c.transaction():
            await c.execute(f"SELECT pg_advisory_xact_lock({START_LOCK})")
            await c.execute(schema)

    @contextlib.asynccontextmanager
    async def lock(self, key: int):
        # a session lock on a connection of its own, for as long as the block runs (and gone if it dies)
        import asyncpg
        try:
            c = await connect_any(self.url)
        except (OSError, asyncpg.PostgresError, TimeoutError) as e:
            raise DatabaseError(f"can't reach the database {redact(self.url)}: {e}") from None
        got = False
        try:
            got = bool(await c.fetchval("SELECT pg_try_advisory_lock($1)", key))
            yield got
        finally:
            try:
                if got:
                    await c.fetchval("SELECT pg_advisory_unlock($1)", key)
            except (OSError, asyncpg.PostgresError):
                pass  # (the connection is closed next, which drops the lock anyway)
            await c.close()

    async def notify(self, channel: str, payload: str) -> None:
        if len(payload.encode()) > NOTIFY_MAX:
            raise DatabaseError("a notification must be small: tell the others what to look at, not the data")
        await self.pool.execute("SELECT pg_notify($1, $2)", channel, payload)

    async def listen(self, channel: str, callback: Callable[[str], None]) -> None:
        """Returns once this is listening: what is told after that is heard."""
        self._listeners.setdefault(channel, []).append(callback)
        if self._listen_task is None:
            self._listen_task = asyncio.get_running_loop().create_task(self._listen_loop(), name="db-listen")
        elif self._listen_conn is not None and not self._listen_conn.is_closed():
            await self._listen_conn.add_listener(channel, lambda _c, _pid, _ch, payload, cb=callback: self._call(cb, payload))
            return
        try:
            await asyncio.wait_for(self._listening.wait(), 20)
        except TimeoutError:
            log.warning("not listening to the database yet: still trying")

    async def _listen_loop(self) -> None:
        """One connection of its own listens on every channel asked for, and is made again, with the
        callbacks told to look again at everything, if it is lost."""
        import asyncpg
        first = True
        while True:
            conn = None
            try:
                conn = await connect_any(self.url)
                for channel, callbacks in list(self._listeners.items()):
                    for cb in callbacks:
                        await conn.add_listener(channel, lambda _c, _pid, _ch, payload, cb=cb: self._call(cb, payload))
                self._listen_conn = conn
                self._listening.set()
                if not first:
                    for callbacks in list(self._listeners.values()):
                        for cb in callbacks:
                            self._call(cb, "")
                first = False
                while not conn.is_closed():
                    await asyncio.sleep(1)
                self._listening.clear()
                self._listen_conn = None
                log.warning("the listening connection to the database closed: making it again")
            except asyncio.CancelledError:
                raise
            except (OSError, asyncpg.PostgresError, TimeoutError) as e:
                log.warning("can't listen to the database: %s", e)
            finally:
                if conn is not None and not conn.is_closed():
                    try:
                        await conn.close(timeout=5)
                    except Exception:  # (closing a connection that is already failing)
                        log.debug("closing the listening connection", exc_info=True)
            await asyncio.sleep(2)

    @staticmethod
    def _call(cb: Callable[[str], None], payload: str) -> None:
        try:
            cb(payload)
        except Exception:
            log.exception("handling a notification")

    async def close(self) -> None:
        if self._listen_task is not None:
            self._listen_task.cancel()
            await asyncio.gather(self._listen_task, return_exceptions=True)
            self._listen_task = None
        await self.pool.close()


# --- SQLite -----------------------------------------------------------------------------

@functools.lru_cache(maxsize=512)
def _sqlite_sql(sql: str) -> str:
    return re.sub(r"\$(\d+)", r"?\1", sql)


def _octet_length(v) -> int | None:
    if v is None:
        return None
    return len(v) if isinstance(v, (bytes, bytearray, memoryview)) else len(str(v).encode())


class _SqliteCalls:
    """The calls, run on the database's own thread."""

    def __init__(self, db: SQLite):
        self._db = db

    def _run(self, fn):
        return asyncio.get_running_loop().run_in_executor(self._db._thread, fn)

    async def execute(self, sql: str, *args) -> int:
        return await self._run(lambda: self._db._con.execute(_sqlite_sql(sql), args).rowcount)

    async def executemany(self, sql: str, rows) -> None:
        await self._run(lambda: self._db._con.executemany(_sqlite_sql(sql), rows))

    async def fetch(self, sql: str, *args, timeout: float | None = None) -> list:
        return await self._run(lambda: self._db._con.execute(_sqlite_sql(sql), args).fetchall())

    async def fetchrow(self, sql: str, *args):
        return await self._run(lambda: self._db._con.execute(_sqlite_sql(sql), args).fetchone())

    async def fetchval(self, sql: str, *args):
        row = await self.fetchrow(sql, *args)
        return None if row is None else row[0]

    async def xlock(self, key: int) -> None:
        """Nothing to do: a transaction holds the whole database (BEGIN IMMEDIATE), and one server at most uses it."""

    async def timeline(self) -> int:
        """Always 1: SQLite has no standby to fail over to."""
        return 1


class SQLite(Database):
    kind = "sqlite"

    def __init__(self, path: Path, con: sqlite3.Connection, thread: ThreadPoolExecutor, held):
        super().__init__(f"sqlite:{path}")
        self.path = path
        self._con, self._thread, self._held = con, thread, held
        self._busy = asyncio.Lock()  # one statement or transaction at a time on the one connection
        self._in_tx: asyncio.Task | None = None
        self._calls = _SqliteCalls(self)

    @classmethod
    async def open(cls, path: Path, serving: bool) -> SQLite:
        if sqlite3.sqlite_version_info < SQLITE_MIN:
            raise DatabaseError(f"SQLite {sqlite3.sqlite_version} is too old: {'.'.join(map(str, SQLITE_MIN))} or later is needed")
        path.parent.mkdir(parents=True, exist_ok=True)
        held = _hold(path) if serving else None
        thread = ThreadPoolExecutor(1, thread_name_prefix="sqlite")

        def opened() -> sqlite3.Connection:
            con = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.create_function("octet_length", 1, _octet_length, deterministic=True)
            con.execute("PRAGMA busy_timeout = 10000")  # a one-off command writing meanwhile
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = FULL")  # what's acknowledged is on disk
            return con
        try:
            con = await asyncio.get_running_loop().run_in_executor(thread, opened)
        except sqlite3.Error as e:
            thread.shutdown()
            if held:
                held.close()
            raise DatabaseError(f"can't open the database {path}: {e}") from None
        return cls(path, con, thread, held)

    def _check(self) -> None:
        if self._in_tx is not None and self._in_tx is asyncio.current_task():
            raise RuntimeError("inside a transaction, use its connection (async with db.tx() as c: c.…)")

    async def _one(self, call, *args):
        self._check()
        async with self._busy:
            return await call(*args)

    async def execute(self, sql: str, *args) -> int:
        return await self._one(self._calls.execute, sql, *args)

    async def fetch(self, sql: str, *args, timeout: float | None = None) -> list:
        return await self._one(self._calls.fetch, sql, *args)

    async def fetchrow(self, sql: str, *args):
        return await self._one(self._calls.fetchrow, sql, *args)

    async def fetchval(self, sql: str, *args):
        return await self._one(self._calls.fetchval, sql, *args)

    async def timeline(self) -> int:
        return 1  # (no standby to fail over to)

    def tx(self):
        return self._transaction("BEGIN IMMEDIATE")

    def snapshot(self):
        # (a read transaction sees the database as it was at its first read, in WAL mode, while others write)
        return self._transaction("BEGIN DEFERRED")

    @contextlib.asynccontextmanager
    async def _transaction(self, begin: str):
        self._check()
        async with self._busy:
            self._in_tx = asyncio.current_task()
            try:
                await self._calls.execute(begin)
                try:
                    yield self._calls
                except BaseException:
                    await asyncio.shield(self._calls.execute("ROLLBACK"))
                    raise
                await asyncio.shield(self._calls.execute("COMMIT"))
            finally:
                self._in_tx = None

    async def create(self, schema: str) -> None:
        schema = re.sub(r"\bbytea\b", "blob", schema)
        async with self._busy:
            await self._calls._run(lambda: self._con.executescript(schema))

    async def close(self) -> None:
        async with self._busy:
            await self._calls._run(self._con.close)
        self._thread.shutdown()
        if self._held:
            self._held.close()


def _hold(path: Path):
    """Take the file that says a server is using the database at `path`, or refuse."""
    lock = path.with_name(path.name + ".server")
    f = open(lock, "a+b")  # noqa: SIM115 - held until the database is closed
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise DatabaseError(f"another server is using the database {path}: an SQLite database serves one "
                            "server at a time (several need PostgreSQL)") from None
    return f


# --- moving from one to the other -------------------------------------------------------

COPY_BATCH = 500


async def copy_tables(src, dst: Database, tables: dict[str, tuple[str, ...]],
                      booleans: frozenset[str] = frozenset(), progress=None) -> dict[str, int]:
    """Copy every row of `tables` (name -> primary key columns) from `src` (a database, or a snapshot of one) into
    `dst`, whose tables must be empty. A batch at a time, in key order. Returns the rows copied per table."""
    for t in tables:
        if await dst.fetchval(f"SELECT 1 FROM {t} LIMIT 1") is not None:
            raise DatabaseError(f"{redact(dst.url)} isn't empty (its table {t} has rows): copy into a new database")
    counts = {}
    for t, key in tables.items():
        n, last = 0, None
        keys = ", ".join(key)
        while True:
            after = f"WHERE ({keys}) > ({', '.join(f'${i + 1}' for i in range(len(key)))}) " if last else ""
            rows = await src.fetch(f"SELECT * FROM {t} {after}ORDER BY {keys} LIMIT {COPY_BATCH}", *(last or ()))
            if not rows:
                break
            cols = list(rows[0].keys())
            values = [[bool(v) if c in booleans and v is not None else v for c, v in zip(cols, r, strict=True)] for r in rows]
            async with dst.tx() as c:
                await c.executemany(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join(f'${i + 1}' for i in range(len(cols)))})",
                                    values)
            n += len(rows)
            last = tuple(rows[-1][k] for k in key)
            if progress:
                progress(t, n)
        counts[t] = n
    return counts

