"""TeXLoom relay server.

Stores and relays opaque, end-to-end encrypted Yjs updates and file blobs. It never
sees keys or plaintext, and verifies Ed25519 signatures so read-only link holders
can't write.

Storage. The server can't see what it stores and making a project needs no account,
so storage is governed by *allowances*:

  --codes-file FILE   one rule per line:   <code|default>  <allowance>  [label]
                      allowance: 0 (store nothing), a size (500M, 5G) or "unlimited".
                      A code's allowance is the total stored across every project
                      created with it; "default" applies to projects created without
                      a code (default 0 = such projects aren't stored on the server).
                      The file is re-read whenever it changes.
  --max-bytes         total the server stores                (default unlimited)
  --min-free          keep this much disk free                (default 2G)
  --project-max-bytes cap per project, except for unlimited allowances (default 200M)

Without --codes-file every project may be stored (subject to the limits above). A
refused write is still relayed live to collaborators online but not acknowledged, so
the sender keeps it and retries later: nobody loses work.

Run:              texloom-server --port 8787 --db data/texloom.db --codes-file data/codes.txt
Add a code:       texloom-server --codes-file data/codes.txt --add-code alice --allowance 2G
List codes/usage: texloom-server --codes-file data/codes.txt --list-codes --db data/texloom.db
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Container
from dataclasses import dataclass
from pathlib import Path

from aiohttp import WSMsgType, web

from texloom.crypto import canonical_pub, revoke_msg, signer

log = logging.getLogger("texloom.relay")

PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")  # base64url(32-byte pubkey)
BLOB_RE = re.compile(r"^[0-9a-f]{32}$")
KiB, MiB, GiB = 1 << 10, 1 << 20, 1 << 30
UNLIMITED = math.inf
MAX_UPDATE = 8 * MiB
MAX_SNAPSHOT = 12 * MiB  # bigger documents just aren't compacted (their log is kept as it is)
MAX_BLOB = 50 * MiB  # uploaded over HTTP
# Every WebSocket frame is read whole before it can be checked, so keep frames small: a few
# large ones at once must not exhaust the server's memory. p2p frames carry files only in
# relay-only projects, up to P2P_BLOB (clients check).
MAX_FRAME = 16 * MiB
FRAME_LIMITS = {"hello": 4 * KiB, "aw": 64 * KiB, "mode": 4 * KiB, "up": MAX_UPDATE * 4 // 3 + KiB,
                "snap": MAX_SNAPSHOT * 4 // 3 + KiB, "p2p": MAX_FRAME, "revoke": 4 * KiB}
RATE_WINDOW, RATE_MSGS, RATE_BYTES = 10.0, 600, 160 * MiB  # per connection
HELLOS_PER_WINDOW = 5  # a log reply can be large: don't let one connection ask for it in a loop
MAX_CONNECTIONS = 2000
MODE_SKEW = 600  # seconds a signed mode-change command stays valid
MAX_REVOKED = 1000  # revocations kept per project
# Rows for projects that store nothing (relay-only mode, revocations) cost nothing to create,
# so they come out of a server-wide budget: a burst of META_BURST, then META_RATE per second.
META_RATE, META_BURST = 1.0, 600
SEND_TIMEOUT = 20  # seconds; a peer that doesn't read is disconnected rather than holding up others
INFLIGHT_MAX = 192 * MiB  # bytes of large frames being handled at once, server-wide


def b64d(s: object) -> bytes:
    if not isinstance(s, str):
        raise ValueError("expected base64 text")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def verify(project: str, msg: bytes, sig: bytes, revoked: Container[str] = frozenset()) -> bool:
    """Signed by the project's owner key, or by a personal key the owner granted and hasn't revoked.
    `revoked` holds canonical spellings, as signer() returns them."""
    who = signer(project, msg, sig)
    return who is not None and who not in revoked


def parse_size(v: str) -> float:
    """'0', '500M', '2G', '1.5G', 'unlimited' -> bytes (or inf)."""
    v = v.strip().lower()
    if v in ("unlimited", "inf", "none"):
        return UNLIMITED
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?", v)
    if not m:
        raise ValueError(f"not a size: {v!r}")
    return int(float(m[1]) * {"": 1, "k": KiB, "m": MiB, "g": GiB, "t": 1 << 40}[m[2]])


def fmt_size(n: float) -> str:
    if n == UNLIMITED:
        return "unlimited"
    for unit, size in (("G", GiB), ("M", MiB), ("K", KiB)):
        if n >= size:
            return f"{n / size:.1f}{unit}".replace(".0", "")
    return str(int(n))


@dataclass
class Limits:
    max_bytes: float = UNLIMITED
    min_free: float = 2 * GiB
    project_max: float = 200 * MiB


class CodeBook:
    """Storage allowances per creation code, from a text file (see module docstring)."""

    ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

    def __init__(self, path: Path | None):
        self.path = path
        self._mtime = -1.0
        self.codes: dict[str, tuple[float, str]] = {}  # normalized code -> (allowance, label)
        self.default: float = UNLIMITED if path is None else 0

    @staticmethod
    def normalize(code: object) -> str:
        return re.sub(r"[^0-9A-Z]", "", str(code).upper())[:32]

    def _load(self) -> None:
        if not self.path:
            return
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self.codes, self.default, self._mtime = {}, 0, -1.0
            return
        if mtime == self._mtime:
            return
        codes, default = {}, 0.0
        for n, line in enumerate(self.path.read_text().splitlines(), 1):
            parts = line.split("#", 1)[0].split()
            if not parts:
                continue
            try:
                allowance = parse_size(parts[1]) if len(parts) > 1 else UNLIMITED
                label = " ".join(parts[2:])
            except ValueError:  # old format "code label": unlimited
                allowance, label = UNLIMITED, " ".join(parts[1:])
            if parts[0].lower() == "default":
                default = allowance
            else:
                codes[self.normalize(parts[0])] = (allowance, label)
        self.codes, self.default, self._mtime = codes, default, mtime
        log.info("codes file: %d code(s), default allowance %s", len(codes), fmt_size(default))

    @property
    def open_creation(self) -> bool:
        self._load()
        return self.default > 0

    def check(self, code: object) -> str | None:
        """The normalized code if valid (constant-time comparison), else None."""
        if not code:
            return None
        self._load()
        given = self.normalize(code)
        found = None
        for c in self.codes:
            if hmac.compare_digest(c, given):
                found = c
        return found

    def allowance(self, owner: str) -> float:
        """Allowance for an owner: a code, 'default', or 'existing' (grandfathered)."""
        self._load()
        if owner == "existing" or self.path is None:
            return UNLIMITED
        if owner in self.codes:
            return self.codes[owner][0]
        return self.default  # 'default', or a code that has since been removed

    def label(self, owner: str) -> str:
        return self.codes.get(owner, (0, owner))[1] or owner

    def add(self, label: str, allowance: str) -> str:
        assert self.path
        parse_size(allowance)
        raw = "".join(secrets.choice(self.ALPHABET) for _ in range(12))  # 60 bits
        code = f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        with os.fdopen(fd, "a") as f:
            if new:
                f.write("# TeXLoom storage allowances:  <code|default>  <0|size|unlimited>  [label]\n"
                        "default  0\n")
            f.write(f"{code}  {allowance}  {label}\n")
        return code


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self._revoked: dict[str, dict[str, bytes]] = {}
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS log (
              project TEXT NOT NULL, seq INTEGER NOT NULL, snap INTEGER NOT NULL DEFAULT 0,
              data BLOB NOT NULL, sig BLOB NOT NULL, PRIMARY KEY (project, seq));
            CREATE TABLE IF NOT EXISTS head (project TEXT PRIMARY KEY, seq INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS mode (project TEXT PRIMARY KEY, relay INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS blob (
              project TEXT NOT NULL, id TEXT NOT NULL, data BLOB NOT NULL,
              PRIMARY KEY (project, id));
            CREATE TABLE IF NOT EXISTS revoked (
              project TEXT NOT NULL, pub TEXT NOT NULL, sig BLOB NOT NULL, at REAL NOT NULL,
              PRIMARY KEY (project, pub));
            CREATE TABLE IF NOT EXISTS projects (
              project TEXT PRIMARY KEY, created REAL NOT NULL, label TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS gone (
              project TEXT NOT NULL, h BLOB NOT NULL, PRIMARY KEY (project, h)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS floor (project TEXT PRIMARY KEY, seq INTEGER NOT NULL);
            """
        )
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(projects)")}
        if "owner" not in cols:
            self.db.execute("ALTER TABLE projects ADD COLUMN owner TEXT NOT NULL DEFAULT 'existing'")
        if "seen" not in cols:  # last time anyone connected (for idle-project eviction)
            self.db.execute("ALTER TABLE projects ADD COLUMN seen REAL NOT NULL DEFAULT 0")
            self.db.execute("ALTER TABLE projects ADD COLUMN evicted REAL NOT NULL DEFAULT 0")
        if "kind" not in {r[1] for r in self.db.execute("PRAGMA table_info(blob)")}:
            # 'output' = compiled PDF/log/SyncTeX, replaced by every compile: any writer may erase
            # the stored content. 'file' = content of project files: only the owner may erase it
            # (deleting a file from the project is a document edit, and History can restore it
            # only while this content exists)
            self.db.execute("ALTER TABLE blob ADD COLUMN kind TEXT NOT NULL DEFAULT 'file'")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(log)")}
        if "h" not in cols:  # content hash, to recognise replayed updates
            self.db.execute("ALTER TABLE log ADD COLUMN h BLOB")
            for project, seq, data in self.db.execute("SELECT project, seq, data FROM log").fetchall():
                self.db.execute("UPDATE log SET h=? WHERE project=? AND seq=?",
                                (hashlib.sha256(data).digest(), project, seq))
        self.db.execute("CREATE INDEX IF NOT EXISTS log_h ON log(project, h)")
        with self.db:  # projects stored before registration existed are grandfathered in
            self.db.execute(
                "INSERT OR IGNORE INTO projects(project, created, label, owner) "
                "SELECT project, ?, 'existing', 'existing' FROM (SELECT project FROM log UNION SELECT project FROM blob)",
                (time.time(),),
            )
        self.usage: dict[str, int] = defaultdict(int)
        for q in ("SELECT project, SUM(length(data) + length(sig)) FROM log GROUP BY project",
                  "SELECT project, SUM(length(data)) FROM blob GROUP BY project"):
            for project, n in self.db.execute(q):
                self.usage[project] += n or 0
        self.total = sum(self.usage.values())
        self.owners: dict[str, str] = dict(self.db.execute("SELECT project, owner FROM projects"))
        self.seen: dict[str, float] = {p: max(s, c) for p, s, c in self.db.execute("SELECT project, seen, created FROM projects")}
        self.dir = path.parent
        self.meta_tokens, self.meta_at = float(META_BURST), time.monotonic()

    # --- registration & accounting -------------------------------------------
    def owner(self, project: str) -> str | None:
        return self.owners.get(project)

    def register(self, project: str, owner: str, label: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO projects(project, created, label, owner) VALUES (?,?,?,?)",
                            (project, time.time(), label, owner))
        self.owners[project] = owner
        log.info("registered project %s… (%s)", project[:8], label)

    def meta_budget(self) -> bool:
        """Take one unit from the server-wide budget for rows that store nothing (see META_RATE)."""
        now = time.monotonic()
        self.meta_tokens = min(META_BURST, self.meta_tokens + (now - self.meta_at) * META_RATE)
        self.meta_at = now
        if self.meta_tokens < 1:
            return False
        self.meta_tokens -= 1
        return True

    def touch(self, project: str) -> None:
        """Remember that someone opened a stored project (written at most hourly)."""
        if project not in self.owners:
            return  # nothing stored: nothing to keep track of (and no unbounded map)
        now = time.time()
        if now - self.seen.get(project, 0) < 3600:
            return
        self.seen[project] = now
        with self.db:
            self.db.execute("UPDATE projects SET seen=?, evicted=0 WHERE project=?", (now, project))

    def idle(self, days: float) -> list[str]:
        """Stored projects nobody has opened for `days`."""
        cutoff = time.time() - days * 86400
        return [p for p, n in self.usage.items() if n > 0 and self.seen.get(p, 0) < cutoff]

    def evict(self, project: str) -> int:
        """Drop the stored copy (log and files) of an idle project. Its writers re-upload it
        when they next connect; until then, link holders without a copy can't open it.
        Sequence numbers keep counting (so old entries can't be replayed into the gap);
        `floor` tells clients that everything up to it is gone."""
        freed = self.usage.get(project, 0)
        with self.db:
            self.db.execute("DELETE FROM log WHERE project=?", (project,))
            self.db.execute("DELETE FROM blob WHERE project=?", (project,))
            self.db.execute("INSERT INTO floor(project, seq) VALUES (?,?) "
                            "ON CONFLICT(project) DO UPDATE SET seq=excluded.seq", (project, self.head(project)))
            self.db.execute("UPDATE projects SET evicted=? WHERE project=?", (time.time(), project))
        self._recount(project)
        return freed

    def owner_usage(self, owner: str) -> int:
        return sum(self.usage.get(p, 0) for p, o in self.owners.items() if o == owner)

    def _recount(self, project: str) -> None:
        n = self.db.execute("SELECT COALESCE(SUM(length(data) + length(sig)), 0) FROM log WHERE project=?",
                            (project,)).fetchone()[0]
        n += self.db.execute("SELECT COALESCE(SUM(length(data)), 0) FROM blob WHERE project=?",
                             (project,)).fetchone()[0]
        self.total += n - self.usage[project]
        self.usage[project] = n

    def free_disk(self) -> int:
        return shutil.disk_usage(self.dir).free

    # --- log ---------------------------------------------------------------------
    def head(self, project: str) -> int:
        row = self.db.execute("SELECT seq FROM head WHERE project=?", (project,)).fetchone()
        return row[0] if row else 0

    def floor(self, project: str) -> int:
        """Entries up to this sequence number were dropped by eviction."""
        row = self.db.execute("SELECT seq FROM floor WHERE project=?", (project,)).fetchone()
        return row[0] if row else 0

    def duplicate(self, project: str, data: bytes) -> int | None:
        """A replay of an update already stored (its seq), or already compacted away (0)."""
        h = hashlib.sha256(data).digest()
        row = self.db.execute("SELECT seq FROM log WHERE project=? AND h=?", (project, h)).fetchone()
        if row:
            return row[0]
        return 0 if self.db.execute("SELECT 1 FROM gone WHERE project=? AND h=?", (project, h)).fetchone() else None

    def append(self, project: str, data: bytes, sig: bytes) -> int:
        seq = self.head(project) + 1
        with self.db:
            self.db.execute("INSERT INTO log(project, seq, data, sig, h) VALUES (?,?,?,?,?)",
                            (project, seq, data, sig, hashlib.sha256(data).digest()))
            self.db.execute("INSERT INTO head(project, seq) VALUES (?,?) "
                            "ON CONFLICT(project) DO UPDATE SET seq=excluded.seq", (project, seq))
        self.usage[project] += len(data) + len(sig)
        self.total += len(data) + len(sig)
        return seq

    def last_snapshot(self, project: str) -> int:
        row = self.db.execute("SELECT MAX(seq) FROM log WHERE project=? AND snap=1", (project,)).fetchone()
        return row[0] or 0

    def snapshot_delta(self, project: str, upto: int, size: int) -> int:
        """Bytes a snapshot would add (negative when it frees space)."""
        freed = self.db.execute("SELECT COALESCE(SUM(length(data) + length(sig)), 0) FROM log "
                                "WHERE project=? AND seq<=?", (project, upto)).fetchone()[0]
        return size - freed

    def snapshot(self, project: str, upto: int, data: bytes, sig: bytes) -> None:
        with self.db:
            # remember what the snapshot replaces, so those updates can't be stored again
            self.db.execute("INSERT OR IGNORE INTO gone(project, h) SELECT project, h FROM log "
                            "WHERE project=? AND seq<=? AND snap=0 AND h IS NOT NULL", (project, upto))
            self.db.execute("DELETE FROM log WHERE project=? AND seq<=?", (project, upto))
            self.db.execute("INSERT INTO log(project, seq, snap, data, sig, h) VALUES (?,?,1,?,?,?)",
                            (project, upto, data, sig, hashlib.sha256(data).digest()))
        self._recount(project)

    def since(self, project: str, since: int) -> list[dict]:
        snap = self.last_snapshot(project)
        start = snap - 1 if snap and since < snap else since  # behind the snapshot: send it too
        rows = self.db.execute(
            "SELECT seq, snap, data, sig FROM log WHERE project=? AND seq>? ORDER BY seq", (project, start)
        ).fetchall()
        return [{"q": q, "snap": bool(sn), "d": b64e(d), "s": b64e(s)} for q, sn, d, s in rows]

    def count(self, project: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM log WHERE project=?", (project,)).fetchone()[0]

    # --- storage mode & blobs ---------------------------------------------------
    def revoked(self, project: str) -> dict[str, bytes]:
        """Personal keys the owner revoked: canonical pub -> owner's signature."""
        if project not in self._revoked:
            rows = self.db.execute("SELECT pub, sig FROM revoked WHERE project=?", (project,)).fetchall()
            self._revoked[project] = {c: sig for p, sig in rows if (c := canonical_pub(p))}
            if len(self._revoked) > 10000:
                self._revoked.pop(next(iter(self._revoked)))
        return self._revoked[project]

    def revoke(self, project: str, pub: str, sig: bytes) -> bool:
        """Record a revocation (`pub` canonical). False if this project has too many already."""
        known = self.revoked(project)
        if pub in known:
            return True
        if len(known) >= MAX_REVOKED:
            return False
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO revoked(project, pub, sig, at) VALUES (?,?,?,?)",
                            (project, pub, sig, time.time()))
        self._revoked.pop(project, None)
        return True

    def relay_only(self, project: str) -> bool:
        row = self.db.execute("SELECT relay FROM mode WHERE project=?", (project,)).fetchone()
        return bool(row and row[0])

    def has_mode(self, project: str) -> bool:
        return self.db.execute("SELECT 1 FROM mode WHERE project=?", (project,)).fetchone() is not None

    def set_mode(self, project: str, relay_only: bool) -> None:
        with self.db:
            self.db.execute("INSERT INTO mode(project, relay) VALUES (?,?) "
                            "ON CONFLICT(project) DO UPDATE SET relay=excluded.relay", (project, int(relay_only)))
            if relay_only:  # forget everything we stored for this project
                self.db.execute("DELETE FROM log WHERE project=?", (project,))
                self.db.execute("DELETE FROM blob WHERE project=?", (project,))
        self._recount(project)

    def has_blob(self, project: str, bid: str) -> bool:
        return self.db.execute("SELECT 1 FROM blob WHERE project=? AND id=?", (project, bid)).fetchone() is not None

    def put_blob(self, project: str, bid: str, data: bytes, kind: str = "file") -> None:
        with self.db:
            cur = self.db.execute("INSERT OR IGNORE INTO blob(project, id, data, kind) VALUES (?,?,?,?)",
                                  (project, bid, data, kind))
        if cur.rowcount:
            self.usage[project] += len(data)
            self.total += len(data)

    def mark_file(self, project: str, bid: str) -> None:
        """Content uploaded as a project file is a file, even if it was first stored as output."""
        with self.db:
            self.db.execute("UPDATE blob SET kind='file' WHERE project=? AND id=? AND kind!='file'", (project, bid))

    def delete_blob(self, project: str, bid: str, any_kind: bool) -> bool:
        with self.db:
            cur = self.db.execute("DELETE FROM blob WHERE project=? AND id=?" + ("" if any_kind else " AND kind='output'"),
                                  (project, bid))
        if cur.rowcount:
            self._recount(project)
        return bool(cur.rowcount)

    def get_blob(self, project: str, bid: str) -> bytes | None:
        row = self.db.execute("SELECT data FROM blob WHERE project=? AND id=?", (project, bid)).fetchone()
        return row[0] if row else None


class Conn:
    """Per-connection state: peer id, creation code, and a simple rate limiter."""

    def __init__(self, peer_id: int):
        self.id = peer_id
        self.code: str | None = None
        self.code_tried = False
        self.hellos: list[float] = []
        self.window_start = time.monotonic()
        self.msgs = 0
        self.bytes = 0

    def allow(self, nbytes: int) -> bool:
        now = time.monotonic()
        if now - self.window_start > RATE_WINDOW:
            self.window_start, self.msgs, self.bytes = now, 0, 0
        self.msgs += 1
        self.bytes += nbytes
        return self.msgs <= RATE_MSGS and self.bytes <= RATE_BYTES

    def allow_hello(self) -> bool:
        now = time.monotonic()
        self.hellos = [t for t in self.hellos if now - t < RATE_WINDOW] + [now]
        return len(self.hellos) <= HELLOS_PER_WINDOW


class BadMessage(Exception):
    pass


def field(msg: dict, key: str, typ: type, default=None):
    v = msg.get(key, default)
    if not isinstance(v, typ) or isinstance(v, bool) and typ is int:
        raise BadMessage(f"bad field {key!r}")
    return v


class Relay:
    def __init__(self, store: Store, limits: Limits | None = None, codes: CodeBook | None = None):
        self.store = store
        self.limits = limits or Limits()
        self.codes = codes or CodeBook(None)
        self.rooms: dict[str, set[web.WebSocketResponse]] = defaultdict(set)
        self.conns: dict[web.WebSocketResponse, Conn] = {}
        self._next_id = 1
        self.texlive = None  # TexLive, when serving TeX Live files on demand
        self.inflight = 0  # bytes of large frames being handled right now
        self.blob_reads = asyncio.Semaphore(4)  # concurrent blob uploads being read

    # --- storage policy ------------------------------------------------------------
    def may_store(self, project: str, nbytes: int, code: str | None) -> str | None:
        """None if this write may be stored, else a reason code for the client."""
        owner = self.store.owner(project)
        if owner is None:  # first write: the creator's code (or the default) owns it
            owner = code or "default"
            if self.codes.allowance(owner) <= 0:
                return "needs-code" if owner == "default" else "quota-full"
        allowance = self.codes.allowance(owner)
        if nbytes > 0:
            if self.store.owner_usage(owner) + nbytes > allowance:
                return "quota-full" if self.store.owner(project) or owner != "default" else "needs-code"
            if allowance != UNLIMITED and self.store.usage[project] + nbytes > self.limits.project_max:
                return "project-full"
            if self.store.total + nbytes > self.limits.max_bytes or self.store.free_disk() - nbytes < self.limits.min_free:
                return "server-full"
        if self.store.owner(project) is None:
            self.store.register(project, owner, self.codes.label(owner) if code else "default")
        return None

    def store_status(self, project: str, conn: Conn) -> dict:
        owner = self.store.owner(project)
        allowance = self.codes.allowance(owner or conn.code or "default")
        return {
            "registered": owner is not None,
            "creation": "open" if self.codes.open_creation else "code",
            "canCreate": self.codes.open_creation or bool(conn.code),
            "used": self.store.usage.get(project, 0),
            "projectMax": self.limits.project_max if allowance != UNLIMITED else None,
            "allowance": None if allowance == UNLIMITED else allowance,
        }

    # --- websocket ---------------------------------------------------------------------
    async def send(self, ws: web.WebSocketResponse, text: str) -> None:
        """Send to one peer; one that doesn't keep up is disconnected instead of stalling the room."""
        if ws.closed:
            return
        try:
            await asyncio.wait_for(ws.send_str(text), SEND_TIMEOUT)
        except asyncio.TimeoutError:
            log.info("disconnecting a peer that stopped reading")
            asyncio.create_task(ws.close(code=1008, message=b"too slow"))
        except (ConnectionError, RuntimeError):
            pass

    async def broadcast(self, project: str, msg: dict, exclude=None) -> None:
        text = json.dumps(msg)
        peers = [ws for ws in self.rooms.get(project, ()) if ws is not exclude]
        if peers:
            await asyncio.gather(*(self.send(ws, text) for ws in peers))

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        project = request.match_info["project"]
        if not PROJECT_RE.match(project):
            raise web.HTTPBadRequest(text="bad project id")
        if len(self.conns) >= MAX_CONNECTIONS:
            raise web.HTTPServiceUnavailable(text="too many connections")
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_FRAME)
        await ws.prepare(request)
        room = self.rooms[project]
        room.add(ws)
        conn = self.conns[ws] = Conn(self._next_id)
        self._next_id += 1
        log.info("join %s… (%d peers)", project[:8], len(room))
        try:
            async for raw in ws:
                if raw.type != WSMsgType.TEXT:
                    continue
                if not conn.allow(len(raw.data)):
                    await ws.close(code=1008, message=b"rate limit")
                    break
                size = len(raw.data) if len(raw.data) > MiB else 0
                if size and self.inflight + size > INFLIGHT_MAX:  # the server is busy with big frames
                    await ws.send_json({"t": "err", "m": "server busy: try again"})
                    continue
                self.inflight += size
                try:
                    msg = json.loads(raw.data)
                    if not isinstance(msg, dict):
                        raise BadMessage("not an object")
                    t = field(msg, "t", str)
                    if len(raw.data) > FRAME_LIMITS.get(t, 4 * KiB):
                        raise BadMessage(f"{t} frame too large")
                    await self.handle(project, ws, conn, t, msg)
                except (BadMessage, ValueError, TypeError, KeyError, OverflowError, binascii.Error, RecursionError) as e:
                    await ws.send_json({"t": "err", "m": f"bad message: {e}"[:200]})
                except Exception:  # noqa: BLE001 - one bad frame mustn't take the connection down
                    log.exception("error handling message")
                finally:
                    self.inflight -= size
                    del raw
        finally:
            room.discard(ws)
            self.conns.pop(ws, None)
            if not room:
                self.rooms.pop(project, None)
            log.info("leave %s…", project[:8])
        return ws

    async def handle(self, project: str, ws, conn: Conn, t: str, msg: dict) -> None:
        relay_only = self.store.relay_only(project)
        if t == "hello":
            if not conn.allow_hello():
                await ws.close(code=1008, message=b"rate limit")
                return
            self.store.touch(project)
            if msg.get("code") and not conn.code_tried:  # one code check per connection
                conn.code_tried = True
                conn.code = self.codes.check(field(msg, "code", str))
            since = field(msg, "since", int, 0)
            entries = self.store.since(project, max(0, min(since, 2**62)))
            await ws.send_json(
                {"t": "log", "entries": entries, "head": self.store.head(project),
                 "floor": self.store.floor(project), "count": self.store.count(project), "you": conn.id,
                 "mode": "relay" if relay_only else "server", "peers": len(self.rooms[project]) - 1,
                 "store": self.store_status(project, conn),
                 "rv": [{"p": p, "s": b64e(sig)} for p, sig in self.store.revoked(project).items()]})
        elif t == "up":
            d, s = field(msg, "d", str), field(msg, "s", str)
            data, sig = b64d(d), b64d(s)
            if len(data) > MAX_UPDATE or not verify(project, b"upd:" + data, sig, self.store.revoked(project)):
                await ws.send_json({"t": "err", "m": "rejected update (bad signature, or this personal link was revoked)"})
                return
            if relay_only:  # pass it on, but keep nothing
                await ws.send_json({"t": "ack", "q": 0, "id": msg.get("id")})
                await self.broadcast(project, {"t": "up", "q": 0, "d": d, "s": s}, ws)
                return
            dup = self.store.duplicate(project, data)
            if dup is not None:  # a replay of something already stored (or compacted): acknowledge, keep nothing
                await ws.send_json({"t": "ack", "q": dup, "id": msg.get("id")})
                return
            refused = self.may_store(project, len(data) + len(sig), conn.code)
            if refused:
                # Collaborators online still get it; the sender keeps it (no ack) and retries later.
                await ws.send_json({"t": "err", "code": refused, "m": f"not stored: {refused}", "id": msg.get("id")})
                await self.broadcast(project, {"t": "up", "q": 0, "d": d, "s": s}, ws)
                return
            seq = self.store.append(project, data, sig)
            await ws.send_json({"t": "ack", "q": seq, "id": msg.get("id")})
            await self.broadcast(project, {"t": "up", "q": seq, "d": d, "s": s}, ws)
        elif t == "snap":
            upto = field(msg, "upto", int)
            data, sig = b64d(field(msg, "d", str)), b64d(field(msg, "s", str))
            if relay_only or self.store.owner(project) is None:
                return
            # A snapshot must replace part of the log: after the previous snapshot (and anything
            # evicted), up to head.
            after = max(self.store.last_snapshot(project), self.store.floor(project))
            if not (after < upto <= self.store.head(project)) or len(data) > MAX_SNAPSHOT:
                await ws.send_json({"t": "err", "m": "rejected snapshot"})
                return
            if not verify(project, f"snap:{upto}:".encode() + data, sig, self.store.revoked(project)):
                await ws.send_json({"t": "err", "m": "rejected snapshot"})
                return
            if self.may_store(project, self.store.snapshot_delta(project, upto, len(data) + len(sig)), conn.code):
                return  # over a limit: skip compaction; the log stays as it is
            self.store.snapshot(project, upto, data, sig)
            log.info("compacted %s… up to %d", project[:8], upto)
        elif t == "aw":
            await self.broadcast(project, {"t": "aw", "d": field(msg, "d", str)}, ws)
        elif t == "p2p":
            # Opaque peer-to-peer payload (state-vector sync, blob transfer). Never stored.
            out = {"t": "p2p", "d": field(msg, "d", str), "from": conn.id}
            if "s" in msg:  # a signed update: don't pass on one from a revoked personal link
                out["s"] = field(msg, "s", str)
                revoked = self.store.revoked(project)
                if revoked and not verify(project, b"p2p:" + b64d(out["d"]), b64d(out["s"]), revoked):
                    return
            to = msg.get("to")
            if to is None:
                await self.broadcast(project, out, ws)
            else:
                to = field(msg, "to", int)
                for peer in list(self.rooms[project]):
                    c = self.conns.get(peer)
                    if c and c.id == to and not peer.closed:
                        await peer.send_json(out)
        elif t == "mode":
            mode, ts, sig = field(msg, "m", str), field(msg, "ts", int), b64d(field(msg, "s", str))
            who = signer(project, f"mode:{mode}:{ts}".encode(), sig)
            # Anyone with edit access may turn server storage on; only the owner may turn it off,
            # since that deletes the server's copy.
            if (mode not in ("server", "relay") or abs(time.time() - ts) > MODE_SKEW or who is None
                    or who in self.store.revoked(project) or (mode == "relay" and who != "owner")):
                await ws.send_json({"t": "err", "m": "rejected mode change"})
                return
            if self.store.owner(project) is None and not self.store.has_mode(project) and not self.store.meta_budget():
                await ws.send_json({"t": "err", "m": "server busy: try again later"})
                return
            self.store.set_mode(project, mode == "relay")
            log.info("project %s… storage mode -> %s", project[:8], mode)
            await self.broadcast(project, {"t": "mode", "m": mode})
        elif t == "revoke":  # the owner withdraws a personal edit link
            pub, sig = canonical_pub(field(msg, "p", str)[:64]), b64d(field(msg, "s", str))
            if pub is None or signer(project, revoke_msg(pub), sig) != "owner":
                await ws.send_json({"t": "err", "m": "rejected revocation"})
                return
            known = self.store.owner(project) is not None or self.store.has_mode(project)
            if not known and not self.store.meta_budget() or not self.store.revoke(project, pub, sig):
                await ws.send_json({"t": "err", "m": "rejected revocation: too many"})
                return
            log.info("project %s…: revoked a personal link", project[:8])
            await self.broadcast(project, {"t": "rv", "p": pub, "s": b64e(sig)})  # including the owner
        else:
            raise BadMessage(f"unknown type {t!r}")

    # --- HTTP ----------------------------------------------------------------------------
    async def put_blob(self, request: web.Request) -> web.Response:
        project, bid = request.match_info["project"], request.match_info["id"]
        if not PROJECT_RE.match(project) or not BLOB_RE.match(bid):
            raise web.HTTPBadRequest()
        if (request.content_length or 0) > MAX_BLOB:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_BLOB, actual_size=request.content_length or 0)
        try:
            sig = b64d(request.headers.get("X-Sig", ""))
        except (ValueError, binascii.Error):
            raise web.HTTPForbidden(text="bad signature")
        if not (len(sig) == 64 or 162 <= len(sig) <= 417):  # an owner or personal-link signature
            raise web.HTTPForbidden(text="bad signature")
        async with self.blob_reads:  # only a few large bodies in memory at once
            data = await request.read()
        if len(data) > MAX_BLOB or not verify(project, f"blob:{bid}:".encode() + data, sig, self.store.revoked(project)):
            raise web.HTTPForbidden(text="bad signature")
        if self.store.relay_only(project):
            raise web.HTTPConflict(text="project is relay-only: blobs are exchanged peer to peer")
        kind = "output" if request.headers.get("X-Kind") == "output" else "file"
        if self.store.has_blob(project, bid):
            if kind == "file":
                self.store.mark_file(project, bid)
            return web.Response(status=200)
        refused = self.may_store(project, len(data), self.codes.check(request.headers.get("X-Create-Code")))
        if refused:
            status = {"needs-code": 403, "quota-full": 507, "project-full": 413, "server-full": 507}[refused]
            return web.json_response({"code": refused}, status=status)
        self.store.put_blob(project, bid, data, kind)
        return web.Response(status=201)

    async def delete_blob(self, request: web.Request) -> web.Response:
        """Erase stored content. Any writer may erase compiled output (replaced by every compile);
        only the owner may erase other content (e.g. outputs stored before outputs were marked).
        This isn't deleting a file from the project, which any editor does in the document."""
        project, bid = request.match_info["project"], request.match_info["id"]
        if not PROJECT_RE.match(project) or not BLOB_RE.match(bid):
            raise web.HTTPBadRequest()
        try:
            sig = b64d(request.headers.get("X-Sig", ""))
        except (ValueError, binascii.Error):
            raise web.HTTPForbidden(text="bad signature")
        who = signer(project, f"del:{bid}".encode(), sig)
        if who is None or who in self.store.revoked(project):
            raise web.HTTPForbidden(text="bad signature")
        # X-Only-Output: a client that can't check history asks to delete only marked output
        any_kind = who == "owner" and request.headers.get("X-Only-Output") != "1"
        if self.store.delete_blob(project, bid, any_kind=any_kind):
            return web.Response(status=204)
        # gone already, or a project file that only the owner may delete
        return web.Response(status=404 if not self.store.has_blob(project, bid) else 403)

    async def get_blob(self, request: web.Request) -> web.Response:
        project, bid = request.match_info["project"], request.match_info["id"]
        if not PROJECT_RE.match(project) or not BLOB_RE.match(bid):
            raise web.HTTPBadRequest()
        data = self.store.get_blob(project, bid)
        if data is None:
            raise web.HTTPNotFound()
        return web.Response(body=data, content_type="application/octet-stream", headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Disposition": "attachment",  # ciphertext; never something to render
        })

    async def info(self, _request: web.Request) -> web.Response:
        self.codes._load()
        d = self.codes.default if self.codes.path else UNLIMITED
        return web.json_response({
            "creation": "open" if self.codes.open_creation else "code",
            "defaultAllowance": None if d == UNLIMITED else d,
            "projectMaxBytes": self.limits.project_max,
            "texlive": self.texlive.info() if self.texlive else None,
        })

    async def check_code(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            code = str(body.get("code", ""))[:64] if isinstance(body, dict) else ""
        except ValueError:
            raise web.HTTPBadRequest()
        c = self.codes.check(code)
        if not c:
            return web.json_response({"ok": False})
        allowance = self.codes.allowance(c)
        return web.json_response({"ok": True, "allowance": None if allowance == UNLIMITED else allowance,
                                  "used": self.store.owner_usage(c)})


# Content-Security-Policy for the web app. Scripts only from this origin (WebAssembly
# allowed for in-browser TeX); network to this origin and https: (LLM providers) and
# localhost (local LLMs); no framing, plugins or base/form tricks.
CSP = ("default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; "
       "connect-src 'self' https: http://localhost:* http://127.0.0.1:*; img-src 'self' blob: data:; "
       "style-src 'self' 'unsafe-inline'; font-src 'self' data:; object-src 'none'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")


@web.middleware
async def headers(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
    resp.headers["Access-Control-Allow-Origin"] = "*"  # sync data is ciphertext; keys never leave the page
    resp.headers["Access-Control-Allow-Headers"] = "X-Sig, X-Create-Code, X-Kind, X-Only-Output, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, PUT, POST, DELETE, OPTIONS"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if not request.path.startswith(("/ws/", "/blob/", "/api/", "/core/")):
        resp.headers["Content-Security-Policy"] = CSP
    if isinstance(resp, web.HTTPException) and resp.status >= 400:
        raise resp
    return resp


def make_app(db: Path, static: Path | None, tex: Path | None = None,
             limits: Limits | None = None, codes: CodeBook | None = None, arxiv: bool = True,
             admin_token: str = "", evict_days: float = 0, texlive: Path | None = None,
             texlive_max: float = 4 * GiB, texlive_mirrors: list[str] | None = None) -> web.Application:
    relay = Relay(Store(db), limits, codes)
    if texlive:  # TeX Live files on demand for the in-browser engine
        from .texlive import TexLive
        tl = TexLive(texlive, texlive_max, (limits or Limits()).min_free, texlive_mirrors)
        if tl.ready:
            relay.texlive = tl
        else:
            log.warning("--texlive %s has no index yet: run with --texlive-init first", texlive)
    app = web.Application(middlewares=[headers], client_max_size=MAX_BLOB + 1024)
    if admin_token:
        from .admin import Admin
        app.router.add_get("/admin", Admin(relay, admin_token, evict_days).page)
    if evict_days > 0:
        async def evict_idle(_app: web.Application):
            async def loop():
                while True:
                    for p in relay.store.idle(evict_days):
                        if relay.rooms.get(p):
                            continue  # someone is connected right now
                        freed = relay.store.evict(p)
                        log.info("evicted idle project %s… (%s, unopened for %g days)", p[:8], fmt_size(freed), evict_days)
                    await asyncio.sleep(min(3600, max(60, evict_days * 8640)))
            task = asyncio.create_task(loop())
            yield
            task.cancel()
        app.cleanup_ctx.append(evict_idle)
    app.router.add_get("/ws/{project}", relay.ws_handler)
    app.router.add_put("/blob/{project}/{id}", relay.put_blob)
    app.router.add_get("/blob/{project}/{id}", relay.get_blob)
    app.router.add_delete("/blob/{project}/{id}", relay.delete_blob)
    app.router.add_route("OPTIONS", "/blob/{project}/{id}", lambda r: web.Response())
    app.router.add_get("/api/info", relay.info)
    if relay.texlive:
        app.router.add_get("/texlive/{year}/{fmt}/{name}", relay.texlive.handle)
    app.router.add_post("/api/code", relay.check_code)
    if arxiv:  # importing a paper's source (arXiv has no CORS, so the relay fetches it)
        from .arxiv import ArxivFetcher
        app.router.add_get("/api/arxiv/{id}", ArxivFetcher().handle)
    if tex and (tex / "busytex").is_dir():  # WebAssembly TeX Live for in-browser compiles
        app.router.add_static("/core", tex)
    if static and static.is_dir():
        async def index(_):
            return web.FileResponse(static / "index.html")
        app.router.add_get("/", index)
        app.router.add_static("/", static)
    return app


def main() -> None:
    p = argparse.ArgumentParser(description="TeXLoom relay server", epilog=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1", help="interface to listen on (default: localhost only)")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--db", type=Path, default=Path("data/texloom.db"))
    p.add_argument("--static", type=Path, default=Path("web/dist"))
    p.add_argument("--tex-assets", type=Path, default=Path("tex-assets"),
                   help="directory containing busytex/ (in-browser TeX); optional")
    p.add_argument("--max-bytes", type=parse_size, default=UNLIMITED, help="total storage cap, e.g. 20G")
    p.add_argument("--min-free", type=parse_size, default=2 * GiB, help="keep this much disk free, e.g. 2G")
    p.add_argument("--project-max-bytes", type=parse_size, default=200 * MiB, help="per-project cap, e.g. 200M")
    p.add_argument("--codes-file", type=Path, help="storage allowances per creation code (see above)")
    p.add_argument("--add-code", metavar="LABEL", help="create a creation code with this label, print it, exit")
    p.add_argument("--allowance", default="1G", help="allowance for --add-code: 0, a size, or unlimited (default 1G)")
    p.add_argument("--list-codes", action="store_true", help="list codes, allowances and usage, then exit")
    p.add_argument("--no-arxiv", action="store_true", help="disable importing arXiv sources through this server")
    p.add_argument("--evict-idle-days", type=float, default=0,
                   help="drop the stored copy of projects nobody opened for this many days (0 = never); "
                        "their writers re-upload them on their next visit")
    p.add_argument("--admin-token-file", type=Path,
                   help="enable /admin (read-only usage page); password = this file's content "
                        "(or set TEXLOOM_ADMIN_TOKEN)")
    p.add_argument("--texlive", type=Path, metavar="DIR",
                   help="serve TeX Live files on demand to the in-browser engine, cached in DIR")
    p.add_argument("--texlive-init", action="store_true", help="download/refresh TeX Live's package index into --texlive, then exit")
    p.add_argument("--texlive-max", type=parse_size, default=4 * GiB, help="disk the TeX Live cache may use (default 4G)")
    p.add_argument("--texlive-mirror", action="append", default=[], metavar="URL",
                   help="extra TeX Live mirror (…/systems/texlive/tlnet/) to try if the others fail; repeatable")
    a = p.parse_args()
    if a.texlive_init:
        if not a.texlive:
            sys.exit("--texlive-init needs --texlive DIR")
        from .texlive import TexLive
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        tl = TexLive(a.texlive, extra_mirrors=a.texlive_mirror)
        asyncio.run(tl.build_index(tl.index_sources() if tl.ready else None))
        return
    codes = CodeBook(a.codes_file)
    if a.add_code is not None or a.list_codes:
        if not a.codes_file:
            sys.exit("--codes-file is required")
        if a.add_code is not None:
            print(codes.add(a.add_code, a.allowance))
            return
        codes._load()
        store = Store(a.db)
        print(f"{'default':16}  {fmt_size(codes.default):>9}  used {fmt_size(store.owner_usage('default')):>7}  "
              f"{sum(1 for o in store.owners.values() if o == 'default')} project(s)")
        for c, (allowance, label) in codes.codes.items():
            n = sum(1 for o in store.owners.values() if o == c)
            print(f"{c[:4]}-{c[4:8]}-{c[8:]}  {fmt_size(allowance):>9}  used {fmt_size(store.owner_usage(c)):>7}  "
                  f"{n} project(s)  {label}")
        print(f"{'existing':16}  {'unlimited':>9}  used {fmt_size(store.owner_usage('existing')):>7}  (stored before allowances)")
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    admin_token = (a.admin_token_file.read_text().strip() if a.admin_token_file else
                   os.environ.get("TEXLOOM_ADMIN_TOKEN", "").strip())
    if admin_token and len(admin_token) < 16:
        sys.exit("the admin token must be at least 16 characters")
    limits = Limits(a.max_bytes, a.min_free, a.project_max_bytes)
    log.info("limits: total %s, keep %s free, %s per project; %s",
             fmt_size(limits.max_bytes), fmt_size(limits.min_free), fmt_size(limits.project_max),
             f"allowances from {a.codes_file}" if codes.path else "no codes file: every project may be stored")
    web.run_app(make_app(a.db, a.static, a.tex_assets, limits, codes, arxiv=not a.no_arxiv,
                        admin_token=admin_token, evict_days=a.evict_idle_days, texlive=a.texlive,
                        texlive_max=a.texlive_max, texlive_mirrors=a.texlive_mirror), host=a.host, port=a.port)


if __name__ == "__main__":
    main()
