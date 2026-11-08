"""Encrypted project lists: what lets someone find their projects again on another device.

A list holds project links, and a link *is* a key, so the server must never be able to read
one. An entry here is an opaque blob filed under an Ed25519 public key; whoever holds the
matching private key can replace it. Clients derive both that key and the encryption key from
a secret the server never sees (see texloom/listsync.py and web/src/listsync.ts), so entries
are unreadable here and can't be overwritten by anyone else. The same mechanism stores the
small "unlock" entries that let a passkey recover the list's secret.

Who may create an entry follows the storage rules: on a server with creation codes, a valid
code (the entry then belongs to it, and goes when the code's projects are purged). Updates
need no code from the client, so a second device can use the list, but stop once the code
that created it has expired. Reading needs only the id, a 256-bit value derived from the secret.
"""

from __future__ import annotations

import sqlite3
import time

MAX_LIST = 256 * 1024  # bytes per entry: thousands of projects
MAX_PER_OWNER = 32  # entries per creation code (a list, and the passkeys that unlock it)
MAX_TOTAL = 50_000  # entries in all, for servers that let anyone create them


class Lists:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS lists (id TEXT PRIMARY KEY, seq INTEGER NOT NULL, data BLOB NOT NULL, "
            "owner TEXT NOT NULL, seen REAL NOT NULL)")
        self.db.commit()

    def get(self, lid: str) -> tuple[int, bytes, str] | None:
        """(sequence number, data, owner) of an entry; reading it counts as using it."""
        row = self.db.execute("SELECT seq, data, owner, seen FROM lists WHERE id=?", (lid, )).fetchone()
        if row is None:
            return None
        now = time.time()
        if now - row[3] > 86400:
            with self.db:
                self.db.execute("UPDATE lists SET seen=? WHERE id=?", (now, lid))
        return row[0], row[1], row[2]

    def put(self, lid: str, seq: int, data: bytes, owner: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO lists(id, seq, data, owner, seen) VALUES (?,?,?,?,?) "
                            "ON CONFLICT(id) DO UPDATE SET seq=excluded.seq, data=excluded.data, seen=excluded.seen",
                            (lid, seq, data, owner, time.time()))

    def delete(self, lid: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM lists WHERE id=?", (lid, ))

    def count(self, owner: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM lists WHERE owner=?", (owner, )).fetchone()[0]

    def room_for(self, owner: str) -> bool:
        if self.db.execute("SELECT COUNT(*) FROM lists").fetchone()[0] >= MAX_TOTAL:
            return False
        return owner == "default" or self.count(owner) < MAX_PER_OWNER

    def purge_owner(self, owner: str) -> int:
        with self.db:
            return self.db.execute("DELETE FROM lists WHERE owner=?", (owner, )).rowcount

    def drop_idle(self, before: float) -> int:
        with self.db:
            return self.db.execute("DELETE FROM lists WHERE seen < ?", (before, )).rowcount
