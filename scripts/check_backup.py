#!/usr/bin/env python3
"""Backups (texloom-server --backup): the database copied as it was at one moment while a server writes to it, into
an SQLite file whatever the database is; of several servers trying at once, one makes it; and the copy restored into
a new database, where an app finds its project.

For each kind of database (SQLite, and a throwaway PostgreSQL): a server, an app editing all the while, two backups
begun together (as each relay of a cluster tries every hour), the copy checked (each project's log ends where its head
says: nothing half-copied), then put into a new database with --copy-database-from, and a server on that serving it.

    uv run --extra server python scripts/check_backup.py
"""

import asyncio
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import testdb  # noqa: E402
from check_restore import App, text  # noqa: E402
from checklib import PY, ROOT, SERVER, Server, check, server, summary  # noqa: E402

from server.db import connect  # noqa: E402
from server.store import SCHEMA, SCHEMA_POSTGRESQL  # noqa: E402
from texloom.crypto import Keys  # noqa: E402

FILLER = "~filler"  # a project sorting after any real one, its log long enough that copying it takes a while


def cli(*args: str) -> subprocess.Popen:
    return subprocess.Popen(PY + [SERVER, *args], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


async def fill(url: str, rows: int = 40_000) -> None:
    """A long log for FILLER: copying it takes long enough for the app's changes to land meanwhile, after the real
    project's rows were copied and before its head is (a copy without one moment's view then disagrees with itself)."""
    db = await connect(url, SCHEMA, SCHEMA_POSTGRESQL)
    try:
        for start in range(1, rows + 1, 2000):
            async with db.tx() as c:
                await c.executemany("INSERT INTO log (project, seq, data, sig, h) VALUES ($1, $2, $3, $4, $5)",
                                    [(FILLER, n, b"x" * 200, b"s", n.to_bytes(8, "big")) for n in range(start, start + 2000)])
        await db.execute("INSERT INTO head (project, seq) VALUES ($1, $2)", FILLER, rows)
    finally:
        await db.close()


def consistent(path: Path) -> tuple[bool, str]:
    """Every project's log in the copy ends where its head says (a copy taken table by table while changes were
    stored would have heads past their logs, or logs past their heads)."""
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT h.project, h.seq, (SELECT MAX(seq) FROM log l WHERE l.project = h.project) "
                          "FROM head h").fetchall()
    bad = [r for r in rows if r[2] is not None and r[1] != r[2]]
    return bool(rows) and not bad, f"{len(rows)} project(s), mismatched: {bad[:3]}"


async def one(kind: str) -> None:
    print(f"{kind}:")
    with server(f"backup-{kind}", "--default-allowance", "unlimited", kind=kind) as srv:
        data = str(srv.tmp / "relay")
        keys = Keys.generate()
        app = App(srv.tmp / "app", keys, srv.url)
        await app.start()
        for i in range(3):
            await app.edit(f"before {i}")
        await fill(srv.database)
        writing = True

        async def keep_writing() -> None:
            n = 0
            while writing:
                await app.edit(f"during {n}")
                n += 1
        writer = asyncio.create_task(keep_writing())
        try:
            await asyncio.sleep(0.5)
            both = [cli("--database", srv.database, "--data", data, "--backup", str(srv.tmp / f"backup-{x}"),
                        "--backup-every", "60") for x in "ab"]
            results = [await asyncio.to_thread(p.communicate) for p in both]
            codes = sorted(p.returncode for p in both)
        finally:
            writing = False
            await writer
        made = [i for i, p in enumerate(both) if p.returncode == 0]
        check("of two servers making a backup at once, one does it and the other leaves it (status 75)",
              codes == [0, 75], [(p.returncode, r[1][-400:]) for p, r in zip(both, results)])
        if not made:
            return
        out = results[made[0]][0].split()
        copy = Path(out[0])
        check("it says what to send off-site: the database's copy (an SQLite file) and the stored files' folder",
              copy.name == "texloom.db" and copy.is_file() and len(out) == 2, out)
        ok, why = consistent(copy)
        check("the copy is the database at one moment, though changes were being stored meanwhile", ok, why)
        again = cli("--database", srv.database, "--data", data, "--backup", str(srv.tmp / "backup-c"), "--backup-every", "60")
        await asyncio.to_thread(again.communicate)
        check("another within the hour is left (status 75)", again.returncode == 75)
        lines = set(text(app.r).splitlines())
        await app.stop()

        with testdb.running(kind=kind) as url:
            put = cli("--database", url, "--copy-database-from", f"sqlite:{copy}")
            _, err = await asyncio.to_thread(put.communicate)
            check("the copy goes into a new database (--copy-database-from)", put.returncode == 0, err[-500:])
            restored = Server(srv.tmp / "restored", url, "--default-allowance", "unlimited")
            (srv.tmp / "restored").mkdir()
            restored.start()
            try:
                fresh = App(srv.tmp / "fresh", keys, restored.url)
                await fresh.start()
                got = set(text(fresh.r).splitlines())
                await fresh.stop()
                check("and a server on it serves the project as it was then", {f"before {i}" for i in range(3)} <= got <= lines,
                      sorted(got)[:8])
            finally:
                restored.stop()


async def main() -> int:
    await one("sqlite")
    await one("postgresql")
    return summary("backups")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
