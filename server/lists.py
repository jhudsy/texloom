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
RETIRED = -1  # sequence number of a replaced or deleted list: its id can't be used again
MAX_RETIRED = 256  # markers kept per creation code; a code can't fill the server with them


class Lists:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS lists (id TEXT PRIMARY KEY, seq INTEGER NOT NULL, data BLOB NOT NULL, "
            "owner TEXT NOT NULL, seen REAL NOT NULL)")
        # owner and seen sit after the blob: without these, reading them walks every entry's pages
        self.db.execute("CREATE INDEX IF NOT EXISTS lists_owner ON lists(owner, seq)")
        self.db.execute("CREATE INDEX IF NOT EXISTS lists_seen ON lists(seen)")
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

    def meta(self, lid: str) -> tuple[int, str] | None:
        """(sequence number, owner) of an entry, without reading its contents."""
        return self.db.execute("SELECT seq, owner FROM lists WHERE id=?", (lid, )).fetchone()

    def put(self, lid: str, seq: int, data: bytes, owner: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO lists(id, seq, data, owner, seen) VALUES (?,?,?,?,?) "
                            "ON CONFLICT(id) DO UPDATE SET seq=excluded.seq, data=excluded.data, seen=excluded.seen",
                            (lid, seq, data, owner, time.time()))

    def retire(self, lid: str) -> None:
        """Delete an entry's contents but remember its id, so a device still holding a replaced
        (perhaps leaked) link can't quietly start the list again under that link."""
        with self.db:
            self.db.execute("UPDATE lists SET seq=?, data=x'', seen=? WHERE id=?", (RETIRED, time.time(), lid))
            self.db.execute("DELETE FROM lists WHERE seq=?1 AND owner=(SELECT owner FROM lists WHERE id=?2) AND id NOT IN "
                            "(SELECT id FROM lists WHERE seq=?1 AND owner=(SELECT owner FROM lists WHERE id=?2) "
                            "ORDER BY seen DESC LIMIT ?3)", (RETIRED, lid, MAX_RETIRED))

    def count(self, owner: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM lists WHERE owner=? AND seq<>?", (owner, RETIRED)).fetchone()[0]

    def room_for(self, owner: str) -> bool:
        if self.db.execute("SELECT COUNT(*) FROM lists WHERE seq<>?", (RETIRED, )).fetchone()[0] >= MAX_TOTAL:
            return False
        return owner == "default" or self.count(owner) < MAX_PER_OWNER

    def purge_owner(self, owner: str) -> int:
        with self.db:
            return self.db.execute("DELETE FROM lists WHERE owner=?", (owner, )).rowcount

    def drop_idle(self, before: float) -> int:
        with self.db:
            return self.db.execute("DELETE FROM lists WHERE seen < ?", (before, )).rowcount
