#!/usr/bin/env python3
"""The relay's database, on SQLite and on PostgreSQL (issue 30): every backend must pass the same
checks, and give the same answers.

The same steps run through the store (server/store.py) and the project lists (server/lists.py) on
each database: registering and claiming projects, appending, replays, catch-ups in pages,
snapshots, storage modes, revocations, eviction, deletion for everyone, stored files and their
trash, app versions, the admin page's rows, and project lists. Each answer is checked, and the
answers from the two databases must be identical. Then, on reopening, the totals read back must
match; a copy from one database to the other (texloom-server --copy-database-from) must hold
every row; and SQLite's own rules hold: one server per file, and a transaction's calls go
through it. Before any of that, in the code: store.TABLES names every table the schema creates (or
the copy would leave one behind), and the SQL in store.py and lists.py uses nothing only one of
the databases knows (see db.py).

PostgreSQL needs its programs (scripts/pg.py); without them only SQLite is checked, unless
--postgresql says it must be.

    uv run --extra server python scripts/check_store.py [--postgresql]
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pg
import testdb

from server.db import DatabaseError, connect
from server.files import FolderFiles
from server.lists import RETIRED, Lists
from server.store import (
    BOOLEANS,
    SCHEMA,
    SCHEMA_POSTGRESQL,
    TABLES,
    Store,
    copy_database,
)

failures: list[str] = []


def ok(cond: bool, what: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + what)
    if not cond:
        failures.append(what)


# the same names on every run, so that the answers can be compared
P = [hashlib.sha256(f"project {i}".encode()).hexdigest()[:43] for i in range(6)]
F1, F2 = "1" * 32, "2" * 32
OWNER, OTHER = "AAAABBBBCCCC", "DDDDEEEEFFFF"


def blob(i: int, n: int = 1000) -> bytes:
    return hashlib.sha256(str(i).encode()).digest() * (n // 32 + 1)


async def steps(url: str, tmp: Path) -> list:
    """Run everything, checking as it goes; returns the answers, for comparing with another database."""
    out: list = []

    def note(label: str, value):
        out.append((label, value))
        return value

    store = await Store.open(url, FolderFiles(tmp / "files"), tmp, serving=True)
    await store.start()
    lists = Lists(store.db, store.lists_grew)
    a, b, c, d = P[:4]

    # registration
    await store.register(a, "default", "check")
    ok(note("owner", (store.owner(a), store.owner(b))) == ("default", None), "a project is registered")
    await store.claim(a, OWNER)
    ok(note("claimed", store.owner(a)) == OWNER, "a project created without a code can be claimed")

    # the log
    seqs = [await store.append(a, blob(i, 300), b"s" * 64) for i in range(10)]
    ok(note("seqs", seqs) == list(range(1, 11)), "edits are numbered 1, 2, 3…")
    ok(note("head", await store.head(a)) == 10 and await store.head(b) == 0, "the head is the last number (0 for none)")
    ok(note("dup", await store.duplicate(a, blob(3, 300))) == 4, "a replayed edit is recognised (its number)")
    ok(await store.duplicate(a, b"new") is None, "a new edit isn't a replay")
    usage = note("usage", store.usage[a])
    ok(usage == 10 * (len(blob(0, 300)) + 64) and store.owner_usage(OWNER) == usage, "what a project stores is counted, and its owner's")
    entries, last, done = await store.since(a, 3)
    ok(note("since", [(e["q"], e["snap"]) for e in entries]) == [(i, False) for i in range(4, 11)] and last == 10 and done,
       "a catch-up returns the entries after a point, in order")
    ok(all(type(e["snap"]) is bool for e in entries) and json.dumps(entries), "entries say snap as true or false (not 0 or 1)")
    page, last, done = await store.since(a, 0, limit=1000)
    ok(note("page", ([e["q"] for e in page], last, done)) == ([1, 2], 2, False), "in pages, about the size asked for")
    one, last, done = await store.since(a, 0, limit=1)
    ok(note("tiny page", ([e["q"] for e in one], last, done)) == ([1], 1, False), "a page has at least one entry")
    ok(note("since_size", await store.since_size(a, 8)) == 2 * (len(blob(0, 300)) + 64), "and its size is known in advance")
    ok(note("state", await store.log_state(a)) == (10, 0, 10), "head, floor and count in one")

    # concurrent appends (several connections, one project, as the relay does under its lock)
    got = await asyncio.gather(*[store.append(b, blob(i, 100), b"t" * 64) for i in range(30)])
    ok(note("concurrent", sorted(got)) == list(range(1, 31)), "edits appended at once get distinct numbers")

    # a snapshot replaces what it covers, which can't then be stored again
    delta = note("delta", await store.snapshot_delta(a, 6, 564))
    ok(delta == 564 - 6 * (len(blob(0, 300)) + 64) + 6 * 100, "a snapshot's cost: its size, less what it frees, plus what's remembered")
    await store.snapshot(a, 6, b"S" * 500, b"z" * 64)
    ok(note("last snap", await store.last_snapshot(a)) == 6, "the snapshot is the latest")
    ok(note("after snap", [(e["q"], e["snap"]) for e in (await store.since(a, 0))[0]]) == [(6, True)] + [(i, False) for i in range(7, 11)],
       "it stands for the entries it replaced")
    ok(note("gone", await store.duplicate(a, blob(1, 300))) == 0, "a replay of a replaced edit is recognised (0)")
    h2, h8 = (hashlib.sha256(blob(i, 300)).digest() for i in (1, 7))
    ok(note("same entry", (await store.same_entry(a, 2, h2), await store.same_entry(a, 8, h8), await store.same_entry(a, 8, h2),
                           await store.same_entry(a, 6, h2), await store.same_entry(a, 99, h2)))
       == (True, True, False, False, None), "an app's last change is recognised by number and hash, in the log or replaced by a snapshot")
    ok(note("catch up", (await store.catch_up_start(a, 2), await store.catch_up_start(a, 8))) == (5, 8),
       "a catch-up from before the snapshot starts with it")
    ok(note("usage after", store.usage[a]) == usage + delta, "and the project's size is what the snapshot said")

    # storage modes and revocations
    ok(not await store.has_mode(c) and not await store.relay_only(c), "no storage mode until one is set")
    await store.append(c, b"c1", b"s" * 64)
    await store.set_mode(c, True)
    ok(note("relay only", (await store.relay_only(c), await store.has_mode(c), await store.log_state(c))) == (True, True, (1, 1, 0)),
       "relay-only forgets the log, and nothing up to its head can come back")
    await store.set_mode(c, False)
    ok(note("stored again", await store.relay_only(c)) is False, "and back to stored")
    ok(await store.revoke(a, "pubkey1", b"r" * 64) and await store.revoke(a, "pubkey1", b"r" * 64), "a revocation is recorded (once)")
    ok(note("revoked", sorted(await store.revoked(a))) == [], "only valid keys are read back")  # "pubkey1" isn't a real key

    # stored files
    await store.put_blob(a, F1, blob(1, 5000), "output")
    await store.put_blob(a, F2, blob(2, 3000), "file")
    ok(note("blob", (await store.blob_size(a, F1), await store.has_blob(a, "0" * 32))) == (len(blob(1, 5000)), False), "files are stored")
    await store.put_blob(a, F1, blob(1, 5000), "file")  # the same content, as a project file now
    ok(not await store.delete_blob(a, F1, any_kind=False), "a file uploaded again as a project file isn't output any more")
    ok(await store.delete_blob(a, F1, any_kind=True) and await store.blob_size(a, F1) is None, "the owner can erase it")
    await store._reaper
    ok(note("trash", await store.db.fetchval("SELECT COUNT(*) FROM trash")) == 0, "and its content goes")

    # eviction and deletion
    await store.append(d, b"d" * 100, b"s" * 64)
    await store.register(d, OTHER, "check")
    await store.touch(d)
    ok(note("idle", sorted(store.idle(-1))) == sorted([a, b, d]), "stored projects unopened for a time are listed (for eviction)")
    freed = await store.evict(d)
    ok(note("evicted", (freed, await store.log_state(d), store.usage[d])) == (164, (1, 1, 0), 0), "an evicted project's log goes; its numbering stays")
    msg = await store.delete_project(b, b"x" * 64, 1234)
    ok(note("deleted", (msg["t"], msg["ts"], await store.deleted_count(), await store.log_state(b))) == ("deleted", 1234, 1, (0, 0, 0)),
       "a project deleted for everyone keeps only its tombstone")
    ok((await store.deleted(b))["s"] == msg["s"] and await store.deleted(a) is None, "the tombstone is what's told to clients")

    # app versions and the admin page
    await store.note_client("web", "0.4.51")
    await store.note_client("web", "0.4.51")
    await store.note_client("desktop", "0.4.50")
    modes, projects, apps, snaps = await store.admin_rows()
    ok(note("admin", (modes, sorted((p, o) for p, _, o, _, _ in projects), sorted((x, v, n) for x, v, n, _ in apps), snaps))
       == ({c: False}, sorted([(a, OWNER), (d, OTHER)]), [("desktop", "0.4.50", 1), ("web", "0.4.51", 2)], {a: 500}),
       "the admin page's rows: modes, projects, app versions, snapshot sizes")
    ok(all(type(v) is bool for v in modes.values()), "storage modes read back as true or false")

    # project lists
    l1, l2 = "list" + "1" * 40, "list" + "2" * 40
    ok(await lists.put(l1, 1, b"v1" * 100, OWNER) and not await lists.put(l1, 1, b"again", OWNER), "a list is created once")
    ok(await lists.put(l1, 2, b"v2" * 150, OWNER), "and updated from the version before")
    ok(not await lists.put(l1, 2, b"late", OWNER) and not await lists.put(l1, 5, b"skip", OWNER), "an update from an older version is refused")
    ok(note("list", (await lists.get(l1))[:2]) == (2, b"v2" * 150), "the list reads back")
    ok(note("meta", await lists.meta(l1)) == (2, OWNER, 300), "and its size and owner, without its contents")
    ok(store.list_bytes[OWNER] == 300, "its bytes count against its owner")
    await lists.put(l2, 1, b"x" * 50, OTHER)
    ok(note("count", (await lists.count(OWNER), await lists.room_for(OWNER), await lists.room_for("default"))) == (1, True, True), "lists are counted per owner")
    await lists.retire(l1)
    ok(note("retired", await lists.meta(l1)) == (RETIRED, OWNER, 0) and store.list_bytes[OWNER] == 0, "a retired list keeps its id, and nothing else")
    ok(not await lists.put(l1, 1, b"back", OWNER), "and can't be started again")
    ok(note("drop idle", await lists.drop_idle(time.time() + 10)) == 1 and await lists.meta(l1) is not None, "idle lists go, retired ids stay")
    await lists.put(l2, 1, b"y" * 40, OTHER)
    ok(note("purge", await lists.purge_owner(OTHER)) == 1 and store.list_bytes[OTHER] == 0, "a code's lists go with it")

    total, owner_usage = store.total, (store.owner_usage(OWNER), store.owner_usage(OTHER))
    usages = dict(store.usage)
    await store.close()

    # reopened: what's read back adds up to what was counted
    store = await Store.open(url, FolderFiles(tmp / "files"), tmp)
    ok(note("reopened", (store.total, (store.owner_usage(OWNER), store.owner_usage(OTHER)))) == (total, owner_usage),
       "reopened, the totals read back match what was counted")
    ok({p: n for p, n in store.usage.items() if n} == {p: n for p, n in usages.items() if n}, "and each project's")
    ok(b not in store.owners, "a deleted project's registration is gone")
    await store.close()
    return out


# SQL that only one of the databases runs (see db.py), and where it goes wrong
ONE_DATABASE_ONLY = {
    r"\bDISTINCT\s+ON\b": "DISTINCT ON is PostgreSQL's",
    r"::\s*[a-z]": "::casts are PostgreSQL's (CAST(x AS type) works in both)",
    r"\bINSERT\s+OR\b": "INSERT OR … is SQLite's (ON CONFLICT works in both)",
    r"\bINSERT\b(?:(?!\bWHERE\b).)*?\bSELECT\b(?:(?!\bWHERE\b).)*?\bON\s+CONFLICT\b":
        "an INSERT … SELECT with ON CONFLICT needs a WHERE (SQLite's parser does)",
}
SQL_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER)\b")


def sql_texts(path: Path) -> list[tuple[int, str]]:
    """The SQL in a module: its string literals that look like SQL (f-strings with {…} for what's
    put in), with their line numbers."""
    out, parts = [], set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) else "{…}" for v in n.values)
            parts.update(id(v) for v in n.values)  # (each is read as part of the whole)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in parts:
            text = n.value
        else:
            continue
        if SQL_RE.search(text):
            out.append((n.lineno, text))
    return out


def code_rules() -> None:
    created = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))
    ok(created == set(TABLES), "store.TABLES names every table the schema creates, so a copy takes them all"
       + (f" (missing: {sorted(created - set(TABLES))}, extra: {sorted(set(TABLES) - created)})" if created != set(TABLES) else ""))
    found = []
    for name in ("store.py", "lists.py"):
        for line, text in sql_texts(ROOT / "server" / name):
            found += [f"server/{name}:{line}: {why}" for rule, why in ONE_DATABASE_ONLY.items() if re.search(rule, text, re.I | re.S)]
    ok(not found, "the shared SQL uses nothing only SQLite or only PostgreSQL knows" + "".join(f"\n      {f}" for f in found))


async def table_rows(url: str) -> dict[str, list]:
    db = await connect(url, SCHEMA, SCHEMA_POSTGRESQL)
    try:
        rows = {}
        for t, key in TABLES.items():
            rows[t] = [tuple(bool(v) if k in BOOLEANS else v for k, v in zip(r.keys(), r, strict=True))
                       for r in await db.fetch(f"SELECT * FROM {t} ORDER BY {', '.join(key)}")]
        return rows
    finally:
        await db.close()


async def sqlite_rules(tmp: Path) -> None:
    url = f"sqlite:{tmp / 'rules.db'}"
    store = await Store.open(url, FolderFiles(tmp / "files"), tmp, serving=True)
    try:
        await Store.open(url, FolderFiles(tmp / "files"), tmp, serving=True)
        ok(False, "a second server on the same SQLite file is refused")
    except SystemExit as e:
        ok("another server is using" in str(e), "a second server on the same SQLite file is refused, saying why")
    other = await Store.open(url, FolderFiles(tmp / "files"), tmp)
    await other.register(P[5], "default", "a one-off command")
    await other.close()
    ok(await store.db.fetchval("SELECT owner FROM projects WHERE project=$1", P[5]) == "default",
       "a one-off command can use it while the server runs")
    try:
        async with store.tx():
            await store.db.fetchval("SELECT 1")
        ok(False, "inside a transaction, the database's own calls are refused")
    except RuntimeError:
        ok(True, "inside a transaction, the database's own calls are refused (they'd wait for it forever)")
    try:
        async with store.tx() as c:
            await c.execute("INSERT INTO head(project, seq) VALUES ($1, 5)", P[4])
            raise ValueError
    except ValueError:
        pass
    ok(await store.head(P[4]) == 0, "a transaction that fails changes nothing")
    await store.close()
    for bad in ("sqlite:", "sqlite://relative.db", "mysql://x", "texloom.db"):
        try:
            await connect(bad)
            ok(False, f"{bad!r} is refused")
        except DatabaseError:
            pass
    ok(True, "addresses that aren't sqlite: or postgresql:// are refused")


def main() -> None:
    need_pg = "--postgresql" in sys.argv
    try:
        pg.bindir()
        have_pg = True
    except SystemExit:
        if need_pg:
            raise
        have_pg = False
    print("code:")
    code_rules()
    with tempfile.TemporaryDirectory(prefix="texloom-store-") as d:
        tmp = Path(d)
        answers = {}
        for kind in ("sqlite", "postgresql") if have_pg else ("sqlite",):
            print(f"{kind}:")
            with testdb.running(tmp / kind / "db", kind) as url:
                answers[kind] = asyncio.run(steps(url, tmp / kind))
                if kind == "sqlite":
                    sqlite_url = url
                    sqlite_rows = asyncio.run(table_rows(url))
                else:
                    # SQLite → PostgreSQL → SQLite: every row arrives, and arrives the same
                    with testdb.running(tmp / "pg2" / "db", "postgresql") as fresh:
                        print("copying:")
                        counts = asyncio.run(copy_database(sqlite_url, fresh))
                        ok(asyncio.run(table_rows(fresh)) == sqlite_rows, f"SQLite to PostgreSQL: every row copied ({sum(counts.values())})")
                        back = f"sqlite:{tmp / 'back.db'}"
                        asyncio.run(copy_database(fresh, back))
                        ok(asyncio.run(table_rows(back)) == sqlite_rows, "and back to SQLite, the same")
                        try:
                            asyncio.run(copy_database(sqlite_url, fresh))
                            ok(False, "copying into a database that isn't empty is refused")
                        except DatabaseError as e:
                            ok("isn't empty" in str(e), "copying into a database that isn't empty is refused")
        if not have_pg:
            print("copying:")
            copy = f"sqlite:{tmp / 'copy.db'}"
            asyncio.run(copy_database(sqlite_url, copy))
            ok(asyncio.run(table_rows(copy)) == sqlite_rows, "SQLite to SQLite: every row copied")
            print("postgresql: its programs aren't installed (scripts/pg.py), skipped")
        if len(answers) == 2:
            diff = [(x, y) for x, y in zip(answers["sqlite"], answers["postgresql"], strict=True) if x != y]
            ok(not diff, "SQLite and PostgreSQL give the same answers" + (f": first difference {diff[0]}" if diff else ""))
        print("sqlite's rules:")
        asyncio.run(sqlite_rules(tmp))
    print(json.dumps({"failures": failures}) if failures else "store: all checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
