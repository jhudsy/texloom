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

import time

import asyncpg

from .store import changed

MAX_LIST = 256 * 1024  # bytes per entry: thousands of projects
MAX_PER_OWNER = 32  # entries per creation code (a list, and the passkeys that unlock it)
MAX_TOTAL = 50_000  # entries in all, for servers that let anyone create them
RETIRED = -1  # sequence number of a replaced or deleted list: its id can't be used again
RETIRED_KEEP = 365 * 86400  # seconds a retired id is remembered (like removal records in a list)




class Lists:
    """Entries in the table `lists` (created with the store's schema, store.py)."""

    def __init__(self, pool: asyncpg.Pool, grew=None):
        self.pool = pool
        # grew(owner, bytes): told of every change in what an owner's entries take, so they count
        # against its allowance and the server's total (Store.lists_grew)
        self.grew = grew or (lambda owner, n: None)

    async def get(self, lid: str) -> tuple[int, bytes, str] | None:
        """(sequence number, data, owner) of an entry; reading it counts as using it."""
        row = await self.pool.fetchrow("SELECT seq, data, owner, seen FROM lists WHERE id=$1", lid)
        if row is None:
            return None
        now = time.time()
        if now - row["seen"] > 86400:
            await self.pool.execute("UPDATE lists SET seen=$1 WHERE id=$2", now, lid)
        return row["seq"], row["data"], row["owner"]

    async def meta(self, lid: str) -> tuple[int, str, int] | None:
        """(sequence number, owner, bytes) of an entry, without reading its contents."""
        row = await self.pool.fetchrow("SELECT seq, owner, octet_length(data) AS n FROM lists WHERE id=$1", lid)
        return (row["seq"], row["owner"], row["n"]) if row else None

    async def put(self, lid: str, seq: int, data: bytes, owner: str) -> bool:
        """Store version `seq`, if the entry is still at seq - 1 (or new, for 1). False if someone
        else's update got there first."""
        if seq == 1:
            status = await self.pool.execute(
                "INSERT INTO lists(id, seq, data, owner, seen) VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                lid, seq, data, owner, time.time())
            if changed(status) != 1:
                return False
            self.grew(owner, len(data))
            return True
        row = await self.pool.fetchrow(
            "WITH old AS (SELECT octet_length(data) AS n FROM lists WHERE id=$4 AND seq=$5) "
            "UPDATE lists SET seq=$1, data=$2, seen=$3 WHERE id=$4 AND seq=$5 RETURNING owner, (SELECT n FROM old) AS was",
            seq, data, time.time(), lid, seq - 1)
        if row is None:
            return False
        self.grew(row["owner"], len(data) - row["was"])
        return True

    async def retire(self, lid: str) -> None:
        """Delete an entry's contents but remember its id, so a device still holding a replaced
        (perhaps leaked) link can't quietly start the list again under that link."""
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow("WITH old AS (SELECT octet_length(data) AS n FROM lists WHERE id=$3) "
                                   "UPDATE lists SET seq=$1, data=''::bytea, seen=$2 WHERE id=$3 RETURNING owner, (SELECT n FROM old) AS was",
                                   RETIRED, time.time(), lid)
            if row:
                self.grew(row["owner"], -row["was"])
            # old markers go after a year; none are pushed out to make room, or anyone could retire
            # enough lists of their own to bring a replaced (leaked) id back to life
            await c.execute("DELETE FROM lists WHERE seq=$1 AND seen<$2", RETIRED, time.time() - RETIRED_KEEP)

    async def count(self, owner: str) -> int:
        return await self.pool.fetchval("SELECT COUNT(*) FROM lists WHERE owner=$1 AND seq<>$2", owner, RETIRED)

    async def room_for(self, owner: str) -> bool:
        if await self.pool.fetchval("SELECT COUNT(*) FROM lists WHERE seq<>$1", RETIRED) >= MAX_TOTAL:
            return False
        return owner == "default" or await self.count(owner) < MAX_PER_OWNER

    async def _deleted(self, rows) -> int:
        for r in rows:
            self.grew(r["owner"], -r["n"])
        return len(rows)

    async def purge_owner(self, owner: str) -> int:
        return await self._deleted(await self.pool.fetch(
            "DELETE FROM lists WHERE owner=$1 RETURNING owner, octet_length(data) AS n", owner))

    async def drop_idle(self, before: float) -> int:
        """Drop lists unused since `before`. Not the markers of retired ids: those stay a year (see retire)."""
        return await self._deleted(await self.pool.fetch(
            "DELETE FROM lists WHERE seen < $1 AND seq <> $2 RETURNING owner, octet_length(data) AS n", before, RETIRED))
