#!/usr/bin/env python3
"""A server put back from a backup: nobody misses a change (issue 12's cold standby).

A restored server's log ends where the backup does, and it numbers new changes again from
there. So an app that had seen later numbers on the old server could take a new change for one
it already has. Each app says which change it last has (its number and content hash); the server
tells it when its log isn't the one that came from, and the app catches up from the start and
sends everything it has again.

The case: Alice (the owner) and Bob (an editor) edit; the database is backed up; they edit some
more; the server stops and is restored from the backup; Alice comes back first and edits past
the number Bob last saw, and goes; then Bob comes back, with only the server to catch up from
(two apps online together also fill each other's gaps directly). Bob must end with all of
Alice's changes, and the server's copy (a newcomer's) with everyone's.

Starts its own throwaway `texloom-server` on an SQLite database (scripts/testdb.py) and drives
the desktop app's sync engine (texloom/replica.py); the web app's side is in the shared cases
(scripts/replica-vectors.json).

    uv run --extra server python scripts/check_restore.py
"""

import asyncio
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import check, server, summary, until  # noqa: E402

from pycrdt import Text  # noqa: E402

from texloom.crypto import Keys  # noqa: E402
from texloom.replica import Replica  # noqa: E402


def text(r: Replica) -> str:
    t = r.files.get("main.tex")
    return str(t) if t is not None else ""


def add_line(r: Replica, line: str) -> None:
    def change():
        if "main.tex" not in r.files:
            r.files["main.tex"] = Text()
        t = r.files["main.tex"]
        t.insert(len(t), line + "\n")
    r.local_change(change)


class App:
    """One app's copy of the project, started and stopped like the app itself."""

    def __init__(self, folder: Path, keys: Keys, url: str):
        self.folder, self.keys, self.url = folder, keys, url
        self.r: Replica | None = None
        self.task: asyncio.Task | None = None

    async def start(self) -> Replica:
        self.r = Replica(self.folder, self.keys, self.url)
        self.task = asyncio.create_task(self.r.run())
        await asyncio.wait_for(self.r.synced.wait(), 30)
        return self.r

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def edit(self, line: str) -> None:
        """A change of its own, waited for until the server has it (one change each)."""
        assert self.r is not None
        add_line(self.r, line)
        assert await until(lambda: not self.r.outbox, 20), f"{line} wasn't stored"


async def main() -> int:
    with server("restore", kind="sqlite") as srv:
        tmp = srv.tmp
        db_file = Path(srv.database.removeprefix("sqlite:"))
        keys = Keys.generate()
        alice = App(tmp / "alice", keys, srv.url)
        bob = App(tmp / "bob", keys.personal("Bob")[2], srv.url)
        try:
            a, b = await alice.start(), await bob.start()
            for i in range(3):
                await alice.edit(f"alice {i}")
            await bob.edit("bob 0")
            await until(lambda: text(a) == text(b))

            # the nightly backup (while the server runs, as sqlite3's .backup does)
            with sqlite3.connect(db_file) as live, sqlite3.connect(tmp / "backup.db") as copy:
                live.backup(copy)
            backed_up = text(a)

            for i in range(3, 5):
                await alice.edit(f"alice {i}")
            await bob.edit("bob 1")
            check("both have everything before the server goes", await until(lambda: text(a) == text(b) and "bob 1" in text(a)), (text(a), text(b)))
            bob_since = b.since
            await alice.stop()
            await bob.stop()
            srv.stop()

            # put back from the backup, onto the same address
            for suffix in ("", "-wal", "-shm"):
                Path(f"{db_file}{suffix}").unlink(missing_ok=True)
            shutil.copyfile(tmp / "backup.db", db_file)
            srv.start()

            # Alice first, editing until the server's numbers pass where Bob was
            a = await alice.start()
            check("Alice's copy keeps what the backup missed", "alice 4" in text(a) and "bob 1" in text(a), text(a))
            n = 0
            while await_head(db_file, keys.project) <= bob_since + 2:
                await alice.edit(f"alice after {n}")
                n += 1
            check(f"the restored server numbered {n} new changes past where Bob last was", n > 0)
            alice_text = text(a)
            await alice.stop()

            # then Bob, with Alice gone: only the server to catch up from
            b = await bob.start()
            got = await until(lambda: text(b) == alice_text, 20)
            check("Bob ends with all of Alice's changes since the restore", got and all(f"alice after {i}" in text(b) for i in range(n)),
                  f"Bob has: {text(b)!r}")
            await bob.edit("bob 2")
            await bob.stop()

            # and the server's copy has everyone's: a newcomer gets it all
            newcomer = App(tmp / "newcomer", Keys(keys.project, keys.key, None), srv.url)
            c = await newcomer.start()
            await until(lambda: text(c) == text(b), 20)
            check("a newcomer gets everyone's changes from the server",
                  text(c) == text(b) and all(x in text(c) for x in ("alice 4", "bob 1", "bob 2", f"alice after {n - 1}")),
                  (text(c), text(b)))
            check("including what was made after the backup", backed_up != text(c))
            await newcomer.stop()
        finally:
            await alice.stop()
            await bob.stop()
    return summary("restored server")


def await_head(db_file: Path, project: str) -> int:
    """The server's newest change number for the project."""
    with sqlite3.connect(db_file) as c:
        row = c.execute("SELECT seq FROM head WHERE project=?", (project,)).fetchone()
    return row[0] if row else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
