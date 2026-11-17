"""TeXLoom relay server.

Stores and relays opaque, end-to-end encrypted Yjs updates and file blobs. It never
sees keys or plaintext, and verifies Ed25519 signatures so read-only link holders
can't write.

Storage. The server can't see what it stores and making a project needs no account,
so storage is governed by *allowances*:

  --codes-file FILE   one rule per line:   <code|default>  <allowance>  [until=YYYY-MM-DD]  [label]
                      (repeatable, or listed in TEXLOOM_CODES_FILES: codes are combined)
                      allowance: 0 (store nothing), a size (500M, 5G) or "unlimited".
                      A code's allowance is the total stored across every project
                      created with it; "default" applies to projects created without
                      a code (default 0 = such projects aren't stored on the server).
                      until= is the last day (UTC) a code is valid; after it, the code
                      counts as removed (its projects fall back to "default").
                      The file is re-read whenever it changes.
  --purge-expired-after DAYS
                      delete the stored copies of projects whose code expired more
                      than DAYS days ago (default: never)
  --max-bytes         total the server stores                (default unlimited)
  --min-free          keep this much disk free                (default 2G)
  --project-max-bytes cap per project, except for unlimited allowances (default 200M)

Without --codes-file every project may be stored (subject to the limits above). A
refused write is still relayed live to collaborators online but not acknowledged, so
the sender keeps it and retries later: nobody loses work.

Run:              texloom-server --port 8787 --db data/texloom.db --codes-file data/codes.txt
Add a code:       texloom-server --codes-file data/codes.txt --add-code alice --allowance 2G [--months 12]
Renew a code:     texloom-server --codes-file data/codes.txt --renew-code CODE --months 12 [--allowance 10G]
List codes/usage: texloom-server --codes-file data/codes.txt --list-codes --db data/texloom.db
Purge one code:   texloom-server --codes-file data/codes.txt --purge-code CODE --db data/texloom.db
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import datetime as dt
import hmac
import json
import logging
import math
import os
import re
import secrets
import sys
import tempfile
import time
from collections import defaultdict, deque
from collections.abc import Container
from dataclasses import dataclass
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web

from texloom.crypto import canonical_pub, revoke_msg, signer

from .files import Files, FilesError, open_files
from .lists import MAX_LIST, RETIRED, Lists
from .store import Store, import_sqlite, redact

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
FRAME_LIMITS = {"hello": 4 * KiB, "aw": 64 * KiB, "mode": 4 * KiB, "delete": 4 * KiB, "up": MAX_UPDATE * 4 // 3 + KiB,
                "snap": MAX_SNAPSHOT * 4 // 3 + KiB, "p2p": MAX_FRAME, "revoke": 4 * KiB}
RATE_WINDOW, RATE_MSGS, RATE_BYTES = 10.0, 600, 160 * MiB  # per connection
HELLOS_PER_WINDOW = 5  # a log reply can be large: don't let one connection ask for it in a loop
MAX_CONNECTIONS = 2000
LISTEN_BACKLOG = 2048  # connections waiting to be accepted: everyone reconnects at once after a restart
CLOSE_WAIT = 5.0  # seconds to let connections close cleanly when the server stops
SHUTDOWN_WAIT = 10.0  # then seconds to let other requests (an upload, say) finish
PURGE_PAUSE = 0.01  # seconds between deleted projects: lets live editing through
MODE_SKEW = 600  # seconds a signed mode-change command stays valid
SEND_TIMEOUT = 20  # seconds; a peer that doesn't read is disconnected rather than holding up others
PEER_BUFFER_MAX = 8 * MiB  # bytes waiting to go to one peer: beyond this it isn't reading, and is cut off
OUT_MAX = 256 * MiB  # bytes waiting to go out on all connections: beyond this, peers holding a lot are cut off
STALL = 10  # seconds a large reply may make no progress before its reader is cut off
HELLO_WAIT = 15  # seconds a private server gives a new connection to say who it is
RECHECK = 60  # seconds between a private server's checks that a connected project's code still counts
# A catch-up reply is built whole, for each person. Big ones take turns: together they may use
# about this much memory. Building and sending one takes about four times its stored size.
CATCHUP_MEMORY, CATCHUP_OVERHEAD = 192 * MiB, 4
CATCHUP_TIMEOUT = 120  # seconds to send one; it can be large, and phones can be slow
UPLOAD_TIME = 600  # seconds an upload may take; slow ones use only a connection and a temporary file
# Bytes that connections have sent and the server hasn't finished with, across all connections:
# a WebSocket frame is held whole until it's complete, and anyone can start one.
HELD_MAX = 192 * MiB
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


UNTIL_RE = re.compile(r"^until=(\S*)$", re.IGNORECASE)


def today() -> dt.date:
    """Today in UTC: codes expire at the end of their `until` day, the same everywhere."""
    return dt.datetime.now(dt.UTC).date()


def parse_date(v: str) -> dt.date:
    """'2027-11-07' -> a date."""
    try:
        return dt.date.fromisoformat(v.strip())
    except ValueError:
        raise ValueError(f"not a date (YYYY-MM-DD): {v!r}") from None


def add_months(d: dt.date, months: int) -> dt.date:
    """The same day `months` later, or the end of that month when it is shorter (31 Jan + 1 = 28/29 Feb)."""
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    return min(d.replace(year=year, month=month, day=1) + dt.timedelta(days=d.day - 1),
               nxt - dt.timedelta(days=1))


@dataclass
class Limits:
    max_bytes: float = UNLIMITED
    min_free: float = 2 * GiB
    project_max: float = 200 * MiB


class CodeBook:
    """Storage allowances per creation code, from a text file (see module docstring)."""

    ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

    def __init__(self, path: Path | list[Path] | None):
        # Several files can be given (e.g. one kept by hand and one kept by a sales service):
        # their codes are combined; "default" comes from the first file that sets it. New
        # codes (--add-code) go into the first file.
        self.paths: list[Path] = [] if path is None else list(path) if isinstance(path, list) else [path]
        self.path = self.paths[0] if self.paths else None
        self._mtime: tuple[float, ...] = ()
        self.codes: dict[str, tuple[float, str]] = {}  # normalized code -> (allowance, label)
        self.until: dict[str, dt.date] = {}  # normalized code -> last day it is valid (if it expires)
        self.default: float = UNLIMITED if self.path is None else 0

    @staticmethod
    def normalize(code: object) -> str:
        return re.sub(r"[^0-9A-Z]", "", str(code).upper())[:32]

    @staticmethod
    def _line_fields(parts: list[str]) -> tuple[str, list[str], str | None]:
        """The size, label words and until= date after the code on a line of a codes file
        (`CODE SIZE [label…] [until=YYYY-MM-DD]`). ValueError if the size is missing or wrong."""
        rest = [p for p in parts[1:] if not UNTIL_RE.match(p)]
        dates = [UNTIL_RE.match(p)[1] for p in parts[1:] if UNTIL_RE.match(p)]  # the last day the code is valid
        if not rest:
            raise ValueError("no allowance")
        parse_size(rest[0])
        return rest[0], rest[1:], dates[-1] if dates else None

    def _load(self) -> None:
        if not self.path:
            return
        def mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except FileNotFoundError:
                return -1.0
        mtimes = tuple(mtime(p) for p in self.paths)
        if mtimes == self._mtime:
            return
        codes, until, default = {}, {}, None
        lines = [(p, n, line) for p, m in zip(self.paths, mtimes, strict=True) if m >= 0
                 for n, line in enumerate(p.read_text().splitlines(), 1)]
        for path, n, line in lines:
            parts = line.split("#", 1)[0].split()
            if not parts:
                continue
            try:
                size, label, until_text = self._line_fields(parts)
            except ValueError as e:
                log.warning("%s line %d: %s (ignored: a line is `code allowance [until=YYYY-MM-DD] [label]`)", path, n, e)
                continue
            allowance, label = parse_size(size), " ".join(label)
            expires = None
            if until_text:
                try:
                    expires = parse_date(until_text)
                except ValueError as e:
                    log.warning("%s line %d: %s (ignored: the code doesn't expire)", path, n, e)
            if parts[0].lower() == "default":
                default = allowance if default is None else default
            else:
                c = self.normalize(parts[0])
                if c in codes:
                    continue  # the first file listing a code decides
                codes[c] = (allowance, label)
                if expires:
                    until[c] = expires
        default = default or 0.0
        self.codes, self.until, self.default, self._mtime = codes, until, default, mtimes
        expired = sum(1 for c in codes if self.expired(c))
        log.info("codes file: %d code(s)%s, default allowance %s", len(codes),
                 f" ({expired} expired)" if expired else "", fmt_size(default))

    def expired(self, code: str) -> bool:
        """True once a code's `until` day has passed. An expired code counts as removed: its
        projects fall back to the default allowance and it can't store new projects."""
        u = self.until.get(code)
        return u is not None and today() > u

    def lapsed(self, owner: str) -> bool:
        """True if `owner` was a creation code that has since expired or been removed."""
        self._load()
        return bool(self.path) and owner != "default" and (owner not in self.codes or self.expired(owner))

    def lapsed_since(self, code: str) -> int | None:
        """Whole days since an expired code's last valid day (None if it hasn't expired)."""
        return (today() - self.until[code]).days if self.expired(code) else None

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
        return None if found is None or self.expired(found) else found

    def allowance(self, owner: str) -> float:
        """Allowance for an owner: a code, or 'default'."""
        self._load()
        if self.path is None:
            return UNLIMITED
        if owner in self.codes and not self.expired(owner):
            return self.codes[owner][0]
        return self.default  # 'default', or a code that has since been removed or has expired

    def label(self, owner: str) -> str:
        return self.codes.get(owner, (0, owner))[1] or owner

    def add(self, label: str, allowance: str, until: dt.date | None = None) -> str:
        assert self.path
        parse_size(allowance)
        raw = "".join(secrets.choice(self.ALPHABET) for _ in range(12))  # 60 bits
        code = f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        with os.fdopen(fd, "a") as f:
            if new:
                f.write("# TeXLoom storage allowances:  <code|default>  <0|size|unlimited>  [until=YYYY-MM-DD]  [label]\n"
                        "default  0\n")
            fields = [code, allowance, *([f"until={until.isoformat()}"] if until else []), *([label] if label else [])]
            f.write("  ".join(fields) + "\n")
        return code

    def renew(self, code: str, until: dt.date | None = None, months: int = 0,
              allowance: str | None = None) -> tuple[str, dt.date | None, str]:
        """Set a code's expiry (`until`, or `months` after the later of today and its current
        expiry) and optionally its allowance, rewriting its line in place (in whichever file
        lists it). Returns the label, the new expiry and the allowance as written."""
        assert self.path
        if allowance is not None:
            parse_size(allowance)
        for path in self.paths:
            try:
                return self._renew_in(path, code, until, months, allowance)
            except (KeyError, FileNotFoundError):
                continue
        raise KeyError(f"no such code: {code}")

    def _renew_in(self, path: Path, code: str, until: dt.date | None, months: int,
                  allowance: str | None) -> tuple[str, dt.date | None, str]:
        want = self.normalize(code)
        lines = path.read_text().splitlines(keepends=True)
        for i, line in enumerate(lines):
            body, hash_, comment = line.rstrip("\n").partition("#")
            parts = body.split()
            if not parts or parts[0].lower() == "default" or self.normalize(parts[0]) != want:
                continue
            size, label, until_text = self._line_fields(parts)
            try:
                current = parse_date(until_text) if until_text else None
            except ValueError:
                current = None
            if months:
                until = add_months(max(today(), current or today()), months)
            elif until is None:
                until = current
            size = allowance or size
            new = "  ".join([parts[0], size, *([f"until={until.isoformat()}"] if until else []), *([" ".join(label)] if label else [])])
            lines[i] = new + (f"  {hash_}{comment}" if hash_ else "") + "\n"
            # written beside it and renamed over it, keeping its mode and owner (the install
            # script makes it root:texloom 640, so that the server can read it)
            st = path.stat()
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, st.st_mode & 0o777)
            with os.fdopen(fd, "w") as f:
                f.writelines(lines)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except (PermissionError, AttributeError):  # AttributeError: no chown on Windows
                pass  # not root: the file stays the current user's, as it was if they could write it
            os.replace(tmp, path)
            return " ".join(label), until, size
        raise KeyError(f"no such code: {code}")


class Conn:
    """Per-connection state: peer id, creation code, and a simple rate limiter."""

    def __init__(self, peer_id: int):
        self.id = peer_id
        self.transport = None  # to cut off a peer that stops reading (a polite close would wait for it)
        self.code: str | None = None
        self.code_tried = False
        self.counted = False  # its app and version noted (note_client)
        self.hellos: list[float] = []
        self.recheck = 0.0  # private servers: when to check the project's code again
        self.held = 0  # bytes received and not yet finished with (see Metered)
        self.aware = False  # has sent awareness, so others must hear when it goes
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


class Metered(asyncio.Protocol):
    """Counts what a WebSocket connection has sent, and cuts off a connection holding a lot
    when the total is too high, so unfinished messages can't exhaust the server's memory."""

    def __init__(self, inner, relay: "Relay", conn: "Conn", transport):
        self.inner, self.relay, self.conn, self.transport = inner, relay, conn, transport

    def data_received(self, data: bytes) -> None:
        self.conn.held += len(data)
        self.relay.held += len(data)
        if self.relay.held > HELD_MAX and self.conn.held > MiB:
            log.info("dropping a connection: too much unfinished data on the server")
            self.transport.abort()
            return
        self.inner.data_received(data)

    def connection_lost(self, exc) -> None:
        self.relay.settle(self.conn)
        self.inner.connection_lost(exc)

    def eof_received(self):
        return self.inner.eof_received()

    def pause_writing(self) -> None:
        self.inner.pause_writing()

    def resume_writing(self) -> None:
        self.inner.resume_writing()


class Budget:
    """Memory that big replies may use at once. They wait in turn; one too big for the whole
    budget goes when nothing else is running."""

    def __init__(self, limit: int):
        self.limit, self.used = limit, 0
        self.queue: deque[tuple[int, asyncio.Future]] = deque()

    def _fits(self, n: int) -> bool:
        return self.used == 0 or self.used + n <= self.limit

    async def take(self, n: int) -> None:
        if not n:
            return
        if not self.queue and self._fits(n):
            self.used += n
            return
        turn = asyncio.get_running_loop().create_future()
        self.queue.append((n, turn))
        try:
            await turn
        except asyncio.CancelledError:
            if turn.done() and not turn.cancelled():  # given its turn just as it gave up
                self.give(n)
            raise

    def give(self, n: int) -> None:
        self.used -= n
        while self.queue and self._fits(self.queue[0][0]):
            n, turn = self.queue.popleft()
            if not turn.cancelled():
                self.used += n
                turn.set_result(None)


VERSION_RE = re.compile(r"\d{1,4}(\.\d{1,6}){0,3}")
APPS = ("desktop", "web")


def version_tuple(v: object) -> tuple[int, ...] | None:
    """"0.4.30" as (0, 4, 30), or None if it isn't a version."""
    return tuple(int(x) for x in v.split(".")) if isinstance(v, str) and VERSION_RE.fullmatch(v) else None


class Relay:
    def __init__(self, store: Store | None, limits: Limits | None = None, codes: CodeBook | None = None, lists: bool = True,
                 private: bool = False, min_version: str = ""):
        # the oldest app version allowed (--min-version): older ones, or apps too old to say, are
        # asked to update, so that support for older data and protocols can be dropped
        self.min_version = version_tuple(min_version) if min_version else None
        self.codes_url = ""  # where people learn how to get a creation code (/api/info)
        self.store: Store = store  # type: ignore[assignment]  # (attach, when the server starts)
        self.private = private  # only projects created with a valid creation code may use this server
        self.want_lists = lists
        self.lists: Lists | None = None
        if store is not None:
            self.attach(store)
        self.limits = limits or Limits()
        self.codes = codes or CodeBook(None)
        self.rooms: dict[str, set[web.WebSocketResponse]] = defaultdict(set)
        self.conns: dict[web.WebSocketResponse, Conn] = {}
        self._next_id = 1
        self.texlive = None  # TexLive, when serving TeX Live files on demand
        self.inflight = 0  # bytes of large frames being handled right now
        self.catchup = Budget(CATCHUP_MEMORY)
        self.held = 0  # bytes held for connections, see Metered
        self.stopping = False

    def attach(self, store: Store) -> None:
        self.store = store
        self.lists = Lists(store.pool) if self.want_lists else None

    # --- private servers -----------------------------------------------------------
    async def denied(self, project: str, code: object, claim: bool = True) -> str | None:
        """On a private server, why this project may not use it (None if it may). A project
        belongs to a creation code, so people with its link need nothing else. One that has no
        code yet gets the first valid one offered; one whose code has lapsed waits for a renewal."""
        if not self.private:
            return None
        owner = self.store.owner(project)
        if owner and owner != "default":
            return "code-expired" if self.codes.lapsed(owner) else None
        valid = self.codes.check(code)
        if not valid:
            return "needs-code"
        if not claim:  # asked before anything is verified: say yes, but don't bind the project yet
            return None
        if owner:
            await self.store.claim(project, valid)
        else:
            await self.store.register(project, valid, self.codes.label(valid))
        return None

    async def _admit(self, project: str, ws: web.WebSocketResponse, conn: Conn) -> dict | None:
        """On a private server a connection does nothing until its hello is accepted. Returns the hello."""
        try:
            raw = await asyncio.wait_for(ws.receive(), HELLO_WAIT)
            self.settle(conn)
            msg = json.loads(raw.data) if raw.type == WSMsgType.TEXT and len(raw.data) <= FRAME_LIMITS["hello"] else None
        except (asyncio.TimeoutError, ValueError, TypeError):
            msg = None
        if not isinstance(msg, dict) or msg.get("t") != "hello":
            await ws.close(code=1008, message=b"hello first")
            return None
        why = await self.denied(project, msg.get("code"))
        if why:
            await self._refuse(ws, why)
            return None
        return msg

    @staticmethod
    async def _refuse(ws: web.WebSocketResponse, why: str) -> None:
        """A private server turns this connection's project away, saying why."""
        await ws.send_json({"t": "err", "code": why, "denied": True, "m": "this server is private"})
        await ws.close(code=1008, message=b"private server")

    def settle(self, conn: Conn) -> None:
        """What this connection sent has been dealt with."""
        self.held -= conn.held
        conn.held = 0

    # --- storage policy ------------------------------------------------------------
    async def may_store(self, project: str, nbytes: int, code: str | None) -> str | None:
        """None if this write may be stored, else a reason code for the client."""
        owner = self.store.owner(project)
        if owner is None:  # first write: the creator's code (or the default) owns it
            owner = code or "default"
            if self.codes.allowance(owner) <= 0:
                return "needs-code" if owner == "default" else "quota-full"
        elif self.codes.lapsed(owner) and nbytes > 0 and self.codes.allowance(owner) <= self.store.owner_usage(owner) + nbytes:
            return "code-expired"
        allowance = self.codes.allowance(owner)
        if nbytes > 0:
            if self.store.owner_usage(owner) + nbytes > allowance:
                return "quota-full" if self.store.owner(project) or owner != "default" else "needs-code"
            if allowance != UNLIMITED and self.store.usage[project] + nbytes > self.limits.project_max:
                return "project-full"
            if self.store.total + nbytes > self.limits.max_bytes or self.store.free_for_data() - nbytes < self.limits.min_free:
                return "server-full"
        if self.store.owner(project) is None:
            await self.store.register(project, owner, self.codes.label(owner) if code else "default")
        return None

    async def purge(self, owner: str) -> tuple[int, int, int]:
        """Drop the stored copies of every project created with `owner` (a creation code), as
        idle eviction does. Writers' re-uploads are refused while the code is expired or
        removed, so the projects carry on relay-only, on people's devices. Projects someone
        has open are skipped (and picked up next time). Returns (purged, bytes freed, skipped)."""
        tally = [0, 0, 0]
        await self._purging(owner, tally)
        return tally[0], tally[1], tally[2]

    async def _purging(self, owner: str, tally: list[int], pause: float = 0) -> None:
        """Purge a code's projects one at a time, pausing after each. `tally`: purged, freed, skipped."""
        if self.lists:
            await self.lists.purge_owner(owner)
        for p in self.store.stored_by(owner):
            if self.rooms.get(p):
                tally[2] += 1
                continue
            tally[1] += await self.store.evict(p)
            tally[0] += 1
            await asyncio.sleep(pause)

    async def purge_expired(self, after_days: float) -> None:
        """Purge the projects of codes that expired more than `after_days` ago, letting live
        editing through between projects (a lapsed plan can hold gigabytes)."""
        self.codes._load()
        for c in list(self.codes.until):
            days = self.codes.lapsed_since(c)
            if days is None or days <= after_days or not (self.store.stored_by(c) or (self.lists and await self.lists.count(c))):
                continue
            tally = [0, 0, 0]
            await self._purging(c, tally, PURGE_PAUSE)
            n, freed, skipped = tally
            log.info("purged %d project(s) of code %s… (%s), expired %d day(s) ago%s", n, c[:4],
                     fmt_size(freed), days, f"; {skipped} open, left for later" if skipped else "")

    def _open(self) -> bool:
        return not self.private and self.codes.open_creation

    def store_status(self, project: str, conn: Conn) -> dict:
        owner = self.store.owner(project)
        allowance = self.codes.allowance(owner or conn.code or "default")
        return {
            "registered": owner is not None,
            "creation": "open" if self._open() else "code",
            "canCreate": self._open() or bool(conn.code),
            "used": self.store.usage.get(project, 0),
            "projectMax": self.limits.project_max if allowance != UNLIMITED else None,
            "allowance": None if allowance == UNLIMITED else allowance,
        }

    # --- websocket ---------------------------------------------------------------------
    def cut_off(self, ws: web.WebSocketResponse, why: str = "stopped reading") -> None:
        """Drop a connection at once. Closing politely would wait to write to a peer that isn't
        reading, holding its buffers and its connection slot for as long as it likes."""
        conn = self.conns.get(ws)
        log.info("disconnecting a peer that %s", why)
        if conn and conn.transport is not None:
            conn.transport.abort()
        else:
            asyncio.create_task(ws.close(code=1008, message=b"too slow"))

    async def close_or_cut(self, ws: web.WebSocketResponse, why: bytes) -> None:
        """Close politely, or cut off a peer that doesn't let us."""
        try:
            await asyncio.wait_for(ws.close(code=1008, message=why), 5)
        except (asyncio.TimeoutError, ConnectionError, RuntimeError):
            self.cut_off(ws, "didn't take its close")

    def backed_up(self, ws: web.WebSocketResponse) -> bool:
        conn = self.conns.get(ws)
        return bool(conn and conn.transport is not None and conn.transport.get_write_buffer_size() > PEER_BUFFER_MAX)

    async def send(self, ws: web.WebSocketResponse, text: str) -> None:
        """Send to one peer; one that doesn't keep up is cut off instead of stalling the room or
        piling up what's sent to it."""
        if ws.closed:
            return
        if self.backed_up(ws):
            self.cut_off(ws)
            return
        try:
            await asyncio.wait_for(ws.send_str(text), SEND_TIMEOUT)
        except asyncio.TimeoutError:
            self.cut_off(ws)
        except (ConnectionError, RuntimeError):
            pass

    async def send_large(self, ws: web.WebSocketResponse, msg: dict) -> None:
        """Send a large reply, cutting off a reader that stops taking it, so that it can't hold
        the memory (and the catch-up budget) for long."""
        conn = self.conns.get(ws)
        task = asyncio.ensure_future(ws.send_json(msg))
        waiting = -1
        while True:
            done, _ = await asyncio.wait({task}, timeout=STALL)
            if done:
                task.result()
                return
            now = conn.transport.get_write_buffer_size() if conn and conn.transport is not None else 0
            if waiting != -1 and now >= waiting:  # nothing taken since the last look
                self.cut_off(ws)
                task.cancel()
                return
            waiting = now

    def make_room(self, need: int) -> None:
        """Before sending something large: if what's waiting to go out on every connection would
        pass OUT_MAX, cut off the connections holding a lot, which aren't reading."""
        waiting = [(c.transport.get_write_buffer_size(), ws) for ws, c in self.conns.items() if c.transport is not None]
        if sum(n for n, _ in waiting) + need <= OUT_MAX:
            return
        for n, ws in waiting:
            if n > MiB:
                self.cut_off(ws, "held too much unsent data")

    async def broadcast(self, project: str, msg: dict, exclude=None) -> None:
        text = json.dumps(msg)
        peers = [ws for ws in self.rooms.get(project, ()) if ws is not exclude]
        if peers and len(text) > MiB:
            self.make_room(len(text) * len(peers))
        if peers:
            await asyncio.gather(*(self.send(ws, text) for ws in peers))

    async def close_all(self, _app: web.Application | None = None) -> None:
        """When the server stops, close every connection at once ("going away"), so clients start
        reconnecting straight away and the process exits in seconds rather than waiting on each."""
        self.stopping = True
        if not self.conns:
            return
        log.info("closing %d connections", len(self.conns))
        closing = [asyncio.ensure_future(ws.close(code=WSCloseCode.GOING_AWAY, message=b"server restarting"))
                   for ws in list(self.conns)]
        _done, late = await asyncio.wait(closing, timeout=CLOSE_WAIT)
        for f in late:  # a client that doesn't answer: the shutdown that follows drops it
            f.cancel()

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        if self.stopping:
            raise web.HTTPServiceUnavailable(text="server restarting")
        project = request.match_info["project"]
        if not PROJECT_RE.match(project):
            raise web.HTTPBadRequest(text="bad project id")
        if len(self.conns) >= MAX_CONNECTIONS:
            raise web.HTTPServiceUnavailable(text="too many connections")
        # no compression: what's sent is ciphertext, and compressed frames would let a client make
        # the server hold far more than it sent (Metered counts bytes as they arrive)
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_FRAME, compress=False)
        await ws.prepare(request)
        conn = self.conns[ws] = Conn(self._next_id)
        self._next_id += 1
        if request.transport:
            conn.transport = request.transport
            request.transport.set_protocol(Metered(request.transport.get_protocol(), self, conn, request.transport))
        gone = self.store.deleted.get(project)
        if gone is not None:  # the owner deleted it for everyone: say so, and nothing more
            self.conns.pop(ws, None)
            await ws.send_json(gone)
            await ws.close(code=1008, message=b"deleted")
            return ws
        first = None
        if self.private:
            try:
                first = await self._admit(project, ws, conn)
            finally:
                if first is None:
                    self.conns.pop(ws, None)
            if first is None:
                return ws
            conn.recheck = time.monotonic() + RECHECK
        room = self.rooms[project]
        room.add(ws)
        log.info("join %s… (%d peers)", project[:8], len(room))
        try:
            if first:
                await self.handle(project, ws, conn, "hello", first)
            async for raw in ws:
                self.settle(conn)
                if raw.type != WSMsgType.TEXT:
                    continue
                if not conn.allow(len(raw.data)):
                    await ws.close(code=1008, message=b"rate limit")
                    break
                if self.private and time.monotonic() >= conn.recheck:
                    conn.recheck = time.monotonic() + RECHECK
                    why = await self.denied(project, conn.code)
                    if why:
                        await self._refuse(ws, why)
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
                except Exception:
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
            if conn.aware and not self.stopping:  # this also covers a crash or a lost connection, where a client can't say goodbye
                await self.broadcast(project, {"t": "left", "from": conn.id})
        return ws

    async def handle(self, project: str, ws, conn: Conn, t: str, msg: dict) -> None:
        if project in self.store.deleted:  # deleted while this connection was closing: nothing more
            return
        relay_only = self.store.relay_only(project)
        if t == "hello":
            if not conn.allow_hello():
                await ws.close(code=1008, message=b"rate limit")
                return
            if not conn.counted:  # (once per connection: hellos repeat)
                conn.counted = True
                await self.note_client(msg.get("app"), msg.get("v"))
            if self.too_old(msg.get("v")):
                await ws.send_json({"t": "err", "denied": True, **self.update_needed()})
                await ws.close(code=1008, message=b"update needed")
                return
            await self.store.touch(project)
            if msg.get("code") and not conn.code_tried:  # one code check per connection
                conn.code_tried = True
                conn.code = self.codes.check(field(msg, "code", str))
            since = max(0, min(field(msg, "since", int, 0), 2**62))
            size = await self.store.since_size(project, since)
            cost = size * CATCHUP_OVERHEAD if size >= MiB else 0
            await self.catchup.take(cost)
            try:
                entries = await self.store.since(project, since)
                await asyncio.wait_for(self.send_large(ws,
                    {"t": "log", "entries": entries, "head": await self.store.head(project),
                     "floor": await self.store.floor(project), "count": await self.store.count(project), "you": conn.id,
                     "mode": "relay" if relay_only else "server", "peers": len(self.rooms[project]) - 1,
                     "store": self.store_status(project, conn),
                     "rv": [{"p": p, "s": b64e(sig)} for p, sig in (await self.store.revoked(project)).items()]}), CATCHUP_TIMEOUT)
            except (asyncio.TimeoutError, ConnectionError, RuntimeError):  # too slow, or it has gone
                self.cut_off(ws, "was too slow to catch up")
            finally:
                self.catchup.give(cost)
        elif t == "up":
            d, s = field(msg, "d", str), field(msg, "s", str)
            data, sig = b64d(d), b64d(s)
            if len(data) > MAX_UPDATE or not verify(project, b"upd:" + data, sig, await self.store.revoked(project)):
                await ws.send_json({"t": "err", "m": "rejected update (bad signature, or this personal link was revoked)"})
                return
            if relay_only:  # pass it on, but keep nothing
                await ws.send_json({"t": "ack", "q": 0, "id": msg.get("id")})
                await self.broadcast(project, {"t": "up", "q": 0, "d": d, "s": s}, ws)
                return
            async with self.store.lock(project):
                dup = await self.store.duplicate(project, data)
                refused = None if dup is not None else await self.may_store(project, len(data) + len(sig), conn.code)
                seq = None if dup is not None or refused else await self.store.append(project, data, sig)
            if dup is not None:  # a replay of something already stored (or compacted): acknowledge, keep nothing
                await ws.send_json({"t": "ack", "q": dup, "id": msg.get("id")})
                return
            if refused:
                # Collaborators online still get it; the sender keeps it (no ack) and retries later.
                await ws.send_json({"t": "err", "code": refused, "m": f"not stored: {refused}", "id": msg.get("id")})
                await self.broadcast(project, {"t": "up", "q": 0, "d": d, "s": s}, ws)
                return
            await ws.send_json({"t": "ack", "q": seq, "id": msg.get("id")})
            await self.broadcast(project, {"t": "up", "q": seq, "d": d, "s": s}, ws)
        elif t == "snap":
            upto = field(msg, "upto", int)
            data, sig = b64d(field(msg, "d", str)), b64d(field(msg, "s", str))
            if relay_only or self.store.owner(project) is None:
                return
            if len(data) > MAX_SNAPSHOT or not await asyncio.to_thread(
                    verify, project, f"snap:{upto}:".encode() + data, sig, await self.store.revoked(project)):
                await ws.send_json({"t": "err", "m": "rejected snapshot"})
                return
            async with self.store.lock(project):
                # A snapshot must replace part of the log: after the previous snapshot (and anything
                # evicted), up to head.
                after = max(await self.store.last_snapshot(project), await self.store.floor(project))
                if not (after < upto <= await self.store.head(project)):
                    await ws.send_json({"t": "err", "m": "rejected snapshot"})
                    return
                if await self.may_store(project, await self.store.snapshot_delta(project, upto, len(data) + len(sig)), conn.code):
                    return  # over a limit: skip compaction; the log stays as it is
                await self.store.snapshot(project, upto, data, sig)
            log.info("compacted %s… up to %d", project[:8], upto)
            # the sender may now delete what the snapshot replaced (changes it kept as uploads)
            await ws.send_json({"t": "snapped", "upto": upto})
        elif t == "aw":
            conn.aware = True
            await self.broadcast(project, {"t": "aw", "d": field(msg, "d", str), "from": conn.id}, ws)
        elif t == "p2p":
            # Opaque peer-to-peer payload (state-vector sync, blob transfer). Never stored.
            out = {"t": "p2p", "d": field(msg, "d", str), "from": conn.id}
            if "s" in msg:  # a signed update: don't pass on one from a revoked personal link
                out["s"] = field(msg, "s", str)
                revoked = await self.store.revoked(project)
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
                    or who in await self.store.revoked(project) or (mode == "relay" and who != "owner")):
                await ws.send_json({"t": "err", "code": "mode-rejected", "m": "rejected mode change"})
                return
            if self.store.owner(project) is None and not self.store.has_mode(project) and not self.store.meta_budget():
                await ws.send_json({"t": "err", "code": "mode-busy", "m": "server busy: try again later"})
                return
            async with self.store.lock(project):
                await self.store.set_mode(project, mode == "relay")
            log.info("project %s… storage mode -> %s", project[:8], mode)
            await self.broadcast(project, {"t": "mode", "m": mode})
        elif t == "delete":  # the owner deletes the project for everyone
            ts, sig = field(msg, "ts", int), b64d(field(msg, "s", str))
            if abs(time.time() - ts) > MODE_SKEW or signer(project, f"delete:{ts}".encode(), sig) != "owner":
                await ws.send_json({"t": "err", "code": "delete-rejected", "m": "rejected deletion"})
                return
            # a project this server has never held anything for costs a permanent row: from the
            # same budget as other rows that store nothing, so ids can't be minted to fill the disk
            known = self.store.owner(project) is not None or self.store.has_mode(project) or await self.store.head(project) > 0
            if not known and not self.store.meta_budget():
                await ws.send_json({"t": "err", "code": "mode-busy", "m": "server busy: try again later"})
                return
            async with self.store.lock(project):
                gone = await self.store.delete_project(project, sig, ts)
            log.info("project %s… deleted for everyone by its owner", project[:8])
            room = list(self.rooms.get(project, ()))
            await self.broadcast(project, gone)  # including the owner
            for peer in room:  # each at once: one that isn't reading mustn't hold up the rest
                asyncio.create_task(self.close_or_cut(peer, b"deleted"))
        elif t == "revoke":  # the owner withdraws a personal edit link
            pub, sig = canonical_pub(field(msg, "p", str)[:64]), b64d(field(msg, "s", str))
            if pub is None or signer(project, revoke_msg(pub), sig) != "owner":
                await ws.send_json({"t": "err", "m": "rejected revocation"})
                return
            known = self.store.owner(project) is not None or self.store.has_mode(project)
            if not known and not self.store.meta_budget() or not await self.store.revoke(project, pub, sig):
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
        if project in self.store.deleted:
            return web.json_response({"code": "deleted"}, status=410)
        # before reading a large body; the project is bound to the code only once the upload is signed
        why = await self.denied(project, request.headers.get("X-Create-Code"), claim=False)
        if why:
            return web.json_response({"code": why}, status=403)
        try:
            sig = b64d(request.headers.get("X-Sig", ""))
        except (ValueError, binascii.Error):
            raise web.HTTPForbidden(text="bad signature")
        if not (len(sig) == 64 or 162 <= len(sig) <= 417):  # an owner or personal-link signature
            raise web.HTTPForbidden(text="bad signature")
        # Read to a temporary file, so a slow or stalled upload holds up nobody else, and only
        # one large file is in memory at a time.
        size = 0
        with tempfile.SpooledTemporaryFile(max_size=MiB, dir=self.store.dir) as spool:
            try:
                async with asyncio.timeout(UPLOAD_TIME):
                    async for piece in request.content.iter_chunked(64 * KiB):
                        size += len(piece)
                        if size > MAX_BLOB:
                            raise web.HTTPRequestEntityTooLarge(max_size=MAX_BLOB, actual_size=size)
                        if size > MiB and self.store.free_disk() < self.limits.min_free:
                            return web.json_response({"code": "server-full"}, status=507)
                        spool.write(piece)
            except TimeoutError:
                raise web.HTTPRequestTimeout() from None
            # one copy in memory: the signed message (prefix + file), with the file a view into it
            prefix = f"blob:{bid}:".encode()
            signed = bytearray(len(prefix) + size)
            signed[: len(prefix)] = prefix
            spool.seek(0)
            if spool.readinto(memoryview(signed)[len(prefix):]) != size:
                raise web.HTTPBadRequest(text="short upload")
            data = memoryview(signed)[len(prefix):]
        # checking the signature reads the whole file: on a worker thread, so live editing carries on
        if not await asyncio.to_thread(verify, project, signed, sig, await self.store.revoked(project)):
            raise web.HTTPForbidden(text="bad signature")
        if project in self.store.deleted:  # deleted while it was uploading
            return web.json_response({"code": "deleted"}, status=410)
        if await self.denied(project, request.headers.get("X-Create-Code")):  # a genuine upload: now it may claim
            return web.json_response({"code": "needs-code"}, status=403)
        if self.store.relay_only(project):
            raise web.HTTPConflict(text="project is relay-only: blobs are exchanged peer to peer")
        kind = "output" if request.headers.get("X-Kind") == "output" else "file"
        async with self.store.lock(project):  # the check and the reservation; not the (maybe slow) write
            if await self.store.has_blob(project, bid):
                if kind == "file":
                    await self.store.mark_file(project, bid)
                return web.Response(status=200)
            refused = await self.may_store(project, len(data), self.codes.check(request.headers.get("X-Create-Code")))
            if refused:
                status = {"needs-code": 403, "code-expired": 403, "quota-full": 507, "project-full": 413, "server-full": 507}[refused]
                return web.json_response({"code": refused}, status=status)
            self.store.reserve(project, len(data))
        try:
            stored = await self.store.put_blob(project, bid, data, kind, reserved=True)
        except (FilesError, OSError):
            log.exception("storing a file")
            return web.json_response({"code": "store-failed"}, status=503)
        return web.Response(status=201 if stored else 200)

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
        if who is None or who in await self.store.revoked(project):
            raise web.HTTPForbidden(text="bad signature")
        # X-Only-Output: a client that can't check history asks to delete only marked output
        any_kind = who == "owner" and request.headers.get("X-Only-Output") != "1"
        if await self.store.delete_blob(project, bid, any_kind=any_kind):
            return web.Response(status=204)
        # gone already, or a project file that only the owner may delete
        return web.Response(status=404 if not await self.store.has_blob(project, bid) else 403)

    async def get_blob(self, request: web.Request) -> web.Response:
        project, bid = request.match_info["project"], request.match_info["id"]
        if not PROJECT_RE.match(project) or not BLOB_RE.match(bid):
            raise web.HTTPBadRequest()
        if project in self.store.deleted:
            raise web.HTTPGone()
        if await self.denied(project, None):
            raise web.HTTPForbidden()
        size = await self.store.blob_size(project, bid)
        if size is None:
            raise web.HTTPNotFound()
        return web.Response(body=self.store.read_blob(project, bid), content_type="application/octet-stream", headers={
            "Content-Length": str(size),
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Disposition": "attachment",  # ciphertext; never something to render
        })

    async def note_client(self, app: object, version: object) -> None:
        """Count an app connecting, by kind and version (shown on the admin page)."""
        a = app if app in APPS else "unknown"
        v = version if version_tuple(version) else "unknown"
        await self.store.note_client(a, v)

    def too_old(self, version: object) -> bool:
        """Below --min-version (an app that doesn't say is older than this check)."""
        if not self.min_version:
            return False
        v = version_tuple(version)
        return v is None or v < self.min_version

    def update_needed(self) -> dict:
        m = ".".join(map(str, self.min_version or ()))
        return {"code": "update-needed", "min": m, "m": f"this server needs TeXLoom {m} or later"}

    async def info(self, _request: web.Request) -> web.Response:
        self.codes._load()
        d = self.codes.default if self.codes.path else UNLIMITED
        return web.json_response({
            "private": self.private,
            "creation": "open" if self._open() else "code",
            "defaultAllowance": None if d == UNLIMITED else d,
            "projectMaxBytes": self.limits.project_max,
            "projectLists": self.lists is not None,
            "texlive": self.texlive.info() if self.texlive else None,
            **({"codesUrl": self.codes_url} if self.codes_url else {}),
            **({"minVersion": ".".join(map(str, self.min_version))} if self.min_version else {}),
        })

    # --- encrypted project lists (see lists.py) -----------------------------------------
    def _list_request(self, request: web.Request) -> str:
        if not self.lists:
            raise web.HTTPNotFound()
        if self.min_version and self.too_old(request.headers.get("X-TeXLoom", "").partition("/")[2]):  # app/version
            raise web.HTTPUpgradeRequired(text=json.dumps(self.update_needed()), content_type="application/json")
        lid = request.match_info["id"]
        if not PROJECT_RE.match(lid):
            raise web.HTTPBadRequest()
        return lid

    async def get_list(self, request: web.Request) -> web.Response:
        lid = self._list_request(request)
        assert self.lists
        row = await self.lists.get(lid)
        if row is None:
            raise web.HTTPNotFound()
        if row[0] == RETIRED:
            return web.json_response({"code": "list-retired"}, status=410)
        return web.Response(body=row[1], content_type="application/octet-stream",
                            headers={"X-Seq": str(row[0]), "Cache-Control": "no-store"})

    def _list_signed(self, lid: str, msg: bytes, request: web.Request) -> bool:
        try:
            sig = b64d(request.headers.get("X-Sig", ""))
        except (ValueError, binascii.Error):
            return False
        return len(sig) == 64 and signer(lid, msg, sig) == "owner"

    async def put_list(self, request: web.Request) -> web.Response:
        lid = self._list_request(request)
        assert self.lists
        if (request.content_length or 0) > MAX_LIST:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_LIST, actual_size=request.content_length or 0)
        try:
            seq = int(request.headers.get("X-Seq", ""))
        except ValueError:
            raise web.HTTPBadRequest()
        data = await request.read()
        if len(data) > MAX_LIST or not self._list_signed(lid, f"list:{lid}:{seq}:".encode() + data, request):
            raise web.HTTPForbidden(text="bad signature")
        row = await self.lists.meta(lid)
        if row and row[0] == RETIRED:
            return web.json_response({"code": "list-retired"}, status=410)
        current = row[0] if row else 0
        if seq != current + 1:  # someone else updated it: the client merges and tries again
            return web.json_response({"seq": current}, status=409)
        owner = row[1] if row else (self.codes.check(request.headers.get("X-Create-Code")) or "default")
        if self.codes.allowance(owner) <= 0 or (self.private and owner == "default"):
            return web.json_response({"code": "code-expired" if self.codes.lapsed(owner) else "needs-code"}, status=403)
        if self.store.free_for_data() - len(data) < self.limits.min_free or (not row and not await self.lists.room_for(owner)):
            return web.json_response({"code": "list-full"}, status=507)
        if not await self.lists.put(lid, seq, data, owner):  # another update got there first (another server, say)
            now = await self.lists.meta(lid)
            return web.json_response({"seq": now[0] if now else 0}, status=409)
        return web.json_response({"seq": seq})

    async def delete_list(self, request: web.Request) -> web.Response:
        lid = self._list_request(request)
        assert self.lists
        if not self._list_signed(lid, f"listdel:{lid}".encode(), request):
            raise web.HTTPForbidden(text="bad signature")
        await self.lists.retire(lid)
        return web.Response(status=204)

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
       "connect-src 'self' https: wss: http://localhost:* http://127.0.0.1:* ws://localhost:* ws://127.0.0.1:*; img-src 'self' blob: data:; "
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
    resp.headers["Access-Control-Allow-Headers"] = "X-Sig, X-Seq, X-Create-Code, X-Kind, X-Only-Output, X-TeXLoom, Content-Type"
    resp.headers["Access-Control-Expose-Headers"] = "X-Seq"  # project lists kept on several servers are read and written from any page
    resp.headers["Access-Control-Allow-Methods"] = "GET, PUT, POST, DELETE, OPTIONS"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if not request.path.startswith(("/ws/", "/blob/", "/api/", "/core/")):
        resp.headers["Content-Security-Policy"] = CSP
    if isinstance(resp, web.HTTPException) and resp.status >= 400:
        raise resp
    return resp


def make_app(database: str, static: Path | None, tex: Path | None = None,
             limits: Limits | None = None, codes: CodeBook | None = None, arxiv: bool = True,
             admin_token: str = "", evict_days: float = 0, texlive: Path | None = None,
             texlive_max: float = 4 * GiB, texlive_mirrors: list[str] | None = None,
             purge_days: float | None = None, lists: bool = True, private: bool = False,
             codes_url: str = "", min_version: str = "", files: Files | None = None,
             data: Path = Path("data")) -> web.Application:
    relay = Relay(None, limits, codes, lists, private, min_version)
    relay.codes_url = codes_url
    if texlive:  # TeX Live files on demand for the in-browser engine
        from .texlive import TexLive
        tl = TexLive(texlive, texlive_max, (limits or Limits()).min_free, texlive_mirrors)
        if tl.ready:
            relay.texlive = tl
        else:
            log.warning("--texlive %s has no index yet: run with --texlive-init first", texlive)
    app = web.Application(middlewares=[headers], client_max_size=MAX_BLOB + 1024)

    async def store(_app: web.Application):
        s = await Store.open(database, files, data)
        log.info("database: %s", redact(database))
        relay.attach(s)
        await s.start()
        yield
        await s.close()
    app.cleanup_ctx.append(store)
    if admin_token:
        from .admin import Admin
        app.router.add_get("/admin", Admin(relay, admin_token, evict_days, purge_days).page)
    if evict_days > 0 or purge_days is not None:
        async def maintenance(_app: web.Application):
            async def loop():
                while True:
                    try:
                        if evict_days > 0:
                            for p in relay.store.idle(evict_days):
                                if relay.rooms.get(p):
                                    continue  # someone is connected right now
                                freed = await relay.store.evict(p)
                                log.info("evicted idle project %s… (%s, unopened for %g days)", p[:8], fmt_size(freed), evict_days)
                                await asyncio.sleep(PURGE_PAUSE)
                        if purge_days is not None:
                            await relay.purge_expired(purge_days)
                        if evict_days > 0 and relay.lists:
                            await relay.lists.drop_idle(time.time() - evict_days * 86400)
                    except Exception:
                        log.exception("maintenance")
                    await asyncio.sleep(min(3600, max(60, evict_days * 8640)) if evict_days > 0 else 3600)
            task = asyncio.create_task(loop())
            yield
            task.cancel()
        app.cleanup_ctx.append(maintenance)
    app.on_shutdown.append(relay.close_all)
    app.router.add_get("/ws/{project}", relay.ws_handler)
    app.router.add_put("/blob/{project}/{id}", relay.put_blob)
    app.router.add_get("/blob/{project}/{id}", relay.get_blob)
    app.router.add_delete("/blob/{project}/{id}", relay.delete_blob)
    app.router.add_route("OPTIONS", "/blob/{project}/{id}", lambda r: web.Response())
    app.router.add_get("/api/info", relay.info)
    app.router.add_get("/api/list/{id}", relay.get_list)
    app.router.add_put("/api/list/{id}", relay.put_list)
    app.router.add_delete("/api/list/{id}", relay.delete_list)
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


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="TeXLoom relay server", epilog=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, metavar="FILE",
                   help="settings file (TOML; default: TEXLOOM_CONFIG, else /etc/texloom/texloom.toml if it exists). "
                        "Every option below can be set there by name, and as TEXLOOM_<NAME> in the environment")
    p.add_argument("--host", default="127.0.0.1", help="interface to listen on (default: localhost only)")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--database", "--db", dest="database", default="postgresql:///texloom", metavar="URL",
                   help="the PostgreSQL database, e.g. postgresql:///texloom (this machine, as this user) or "
                        "postgresql://texloom:PASSWORD@db.example.org/texloom")
    p.add_argument("--data", type=Path, default=Path("data"), metavar="DIR",
                   help="this server's own folder: uploads are spooled here, --min-free is kept free here, and "
                        "stored files go in DIR/files unless --files says otherwise")
    p.add_argument("--files", default="", metavar="DIR or s3://BUCKET[/PREFIX]",
                   help="where stored files' content goes (default: DIR/files of --data)")
    p.add_argument("--s3-endpoint", default="", metavar="URL", help="for --files s3://…: the store's address")
    p.add_argument("--s3-region", default="auto", help="for --files s3://…: the store's region (Garage: garage; R2: auto)")
    p.add_argument("--s3-key-id", default="", help="for --files s3://…: the access key id")
    p.add_argument("--s3-secret", default="", help="for --files s3://…: the secret key (put it in the settings file)")
    p.add_argument("--static", type=Path, default=Path("web/dist"))
    p.add_argument("--tex-assets", type=Path, default=Path("tex-assets"),
                   help="directory containing busytex/ (in-browser TeX); optional")
    p.add_argument("--max-bytes", type=parse_size, default=UNLIMITED, help="total storage cap, e.g. 20G")
    p.add_argument("--min-free", type=parse_size, default=2 * GiB, help="keep this much disk free, e.g. 2G")
    p.add_argument("--project-max-bytes", type=parse_size, default=200 * MiB, help="per-project cap, e.g. 200M")
    p.add_argument("--codes-file", type=Path, action="append",
                   help="storage allowances per creation code (see above); repeatable. TEXLOOM_CODES_FILES adds "
                        "more (separated by ':'). --add-code writes to the first")
    p.add_argument("--add-code", metavar="LABEL", help="create a creation code with this label, print it, exit")
    p.add_argument("--allowance", help="allowance for --add-code (default 1G) or --renew-code (default: unchanged): "
                                       "0, a size, or unlimited")
    p.add_argument("--until", type=parse_date, metavar="YYYY-MM-DD",
                   help="for --add-code / --renew-code: the last day the code is valid")
    p.add_argument("--months", type=int, default=0,
                   help="for --add-code / --renew-code: valid for this many months (a renewal counts from the "
                        "later of today and the code's current expiry)")
    p.add_argument("--renew-code", metavar="CODE", help="change a code's expiry (--until/--months) and/or allowance, then exit")
    p.add_argument("--purge-code", metavar="CODE",
                   help="delete the stored copies of every project created with this code (expired or removed "
                        "codes only), then exit. Prefer --purge-expired-after, which does it inside the server")
    p.add_argument("--list-codes", action="store_true", help="list codes, allowances, expiry and usage, then exit")
    p.add_argument("--import-sqlite", type=Path, metavar="FILE",
                   help="copy a database from an older server (SQLite) into the empty --database, then exit")
    p.add_argument("--purge-expired-after", type=float, metavar="DAYS", default=None,
                   help="delete the stored copies of projects whose creation code expired more than DAYS days "
                        "ago (checked hourly; default: never). Their files stay on people's devices")
    p.add_argument("--private", action="store_true",
                   help="only projects created with a valid creation code may use this server, even without "
                        "storage; people with such a project's link need nothing else (needs --codes-file)")
    p.add_argument("--min-version", default="", metavar="X.Y.Z",
                   help="the oldest TeXLoom app allowed to connect: older ones, and those too old to say, are asked "
                        "to update. The admin page shows which versions connect")
    p.add_argument("--codes-url", default="", metavar="URL",
                   help="a web page saying how to get a creation code for this server; the apps link to it where "
                        "they ask for a code (default: none, and they say to ask whoever runs it)")
    p.add_argument("--no-project-lists", action="store_true",
                   help="don't store people's encrypted project lists (the sync link and passkey features)")
    p.add_argument("--no-arxiv", action="store_true", help="disable importing arXiv sources through this server")
    p.add_argument("--evict-idle-days", type=float, default=0,
                   help="drop the stored copy of projects nobody opened for this many days (0 = never); their "
                        "writers re-upload them on their next visit")
    p.add_argument("--admin-token", default="", help="enable /admin (read-only usage page) with this password "
                                                     "(at least 16 characters; put it in the settings file)")
    p.add_argument("--admin-token-file", type=Path, help="as --admin-token, read from this file")
    p.add_argument("--texlive", type=Path, metavar="DIR",
                   help="serve TeX Live files on demand to the in-browser engine, cached in DIR")
    p.add_argument("--texlive-init", action="store_true", help="download/refresh TeX Live's package index into --texlive, then exit")
    p.add_argument("--texlive-max", type=parse_size, default=4 * GiB, help="disk the TeX Live cache may use (default 4G)")
    p.add_argument("--texlive-mirror", action="append", default=[], metavar="URL",
                   help="TeX Live mirror to use first (…/systems/texlive/tlnet/), e.g. one in your country; "
                        "repeatable. Without it, mirror.ctan.org picks one near the server")
    return p


async def _codes_command(a: argparse.Namespace, codes: CodeBook, files: Files) -> None:
    """--list-codes and --purge-code: they read (and purge) the store."""
    store = await Store.open(a.database, files, a.data)
    try:
        if a.purge_code is not None:
            c = CodeBook.normalize(a.purge_code)
            if c in codes.codes and not codes.expired(c):
                sys.exit(f"{a.purge_code} is still valid{f' (until {codes.until[c]})' if c in codes.until else ''}: "
                         "let it expire, or delete its line, first")
            n, freed, _ = await Relay(store, codes=codes).purge(c)
            await store.empty_trash()
            print(f"purged {n} project(s), {fmt_size(freed)} freed. If the server is running, restart it so its "
                  "storage totals are up to date.")
            return

        def expiry(c: str) -> str:
            if c not in codes.until:
                return ""
            days = codes.lapsed_since(c)
            return f"  EXPIRED {codes.until[c]} ({days} day{'s' * (days != 1)} ago)" if days is not None else f"  until {codes.until[c]}"
        print(f"{'default':16}  {fmt_size(codes.default):>9}  used {fmt_size(store.owner_usage('default')):>7}  "
              f"{sum(1 for o in store.owners.values() if o == 'default')} project(s)")
        for c, (allowance, label) in codes.codes.items():
            n = sum(1 for o in store.owners.values() if o == c)
            print(f"{c[:4]}-{c[4:8]}-{c[8:]}  {fmt_size(allowance):>9}  used {fmt_size(store.owner_usage(c)):>7}  "
                  f"{n} project(s){expiry(c)}  {label}")
        removed = sorted({o for o in store.owners.values() if o not in codes.codes and o != "default"})
        for c in removed:
            if store.owner_usage(c):
                print(f"{c[:4]}-{c[4:8]}-{c[8:]}  {'removed':>9}  used {fmt_size(store.owner_usage(c)):>7}  "
                      f"{len(store.stored_by(c))} stored project(s)  (--purge-code to delete them)")
    finally:
        await store.close()


def main() -> None:
    from .config import parse
    p = parser()
    a = parse(p)
    try:
        files = open_files(a.files, a.data / "files",
                           {"endpoint": a.s3_endpoint, "region": a.s3_region, "key_id": a.s3_key_id, "secret": a.s3_secret})
    except FilesError as e:
        p.error(str(e))
    if a.texlive_init:
        if not a.texlive:
            sys.exit("--texlive-init needs --texlive DIR")
        from .texlive import TexLive
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        tl = TexLive(a.texlive, extra_mirrors=a.texlive_mirror)
        asyncio.run(tl.build_index(tl.index_sources() if tl.ready or tl.extra_mirrors else None))
        return
    if a.import_sqlite:
        counts = asyncio.run(import_sqlite(a.import_sqlite, a.database))
        print("copied " + ", ".join(f"{n} {t}" for t, n in counts.items() if n) + f" into {redact(a.database)}")
        return
    codes = CodeBook(a.codes_file or None)
    if a.until and a.months:
        sys.exit("give --until or --months, not both")
    if any(x is not None for x in (a.add_code, a.renew_code, a.purge_code)) or a.list_codes:
        if not a.codes_file:
            sys.exit("--codes-file is required")
        if a.add_code is not None:
            until = add_months(today(), a.months) if a.months else a.until
            print(codes.add(a.add_code, a.allowance or "1G", until))
            return
        if a.renew_code is not None:
            if not (a.until or a.months or a.allowance):
                sys.exit("--renew-code needs --until, --months and/or --allowance")
            try:
                label, until, size = codes.renew(a.renew_code, a.until, a.months, a.allowance)
            except (KeyError, FileNotFoundError):
                sys.exit(f"no such code in {', '.join(map(str, a.codes_file))}: {a.renew_code}")
            print(f"{label or a.renew_code}: {size}, {f'valid until {until}' if until else 'no expiry'}")
            return
        codes._load()
        asyncio.run(_codes_command(a, codes, files))
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    admin_token = (a.admin_token_file.read_text().strip() if a.admin_token_file else a.admin_token.strip())
    if admin_token and len(admin_token) < 16:
        sys.exit("the admin token must be at least 16 characters")
    if a.private and not codes.path:
        sys.exit("--private needs --codes-file: it's the creation codes that decide who may use the server")
    limits = Limits(a.max_bytes, a.min_free, a.project_max_bytes)
    if a.config:
        log.info("settings from %s", a.config)
    if a.private:
        log.info("private server: only projects with a valid creation code may connect")
    log.info("limits: total %s, keep %s free, %s per project; %s",
             fmt_size(limits.max_bytes), fmt_size(limits.min_free), fmt_size(limits.project_max),
             f"allowances from {', '.join(map(str, a.codes_file))}" if codes.path else "no codes file: every project may be stored")
    if a.min_version and not version_tuple(a.min_version):
        p.error("--min-version is a version such as 0.4.30")
    if a.codes_url and not re.match(r"https?://", a.codes_url, re.I):
        p.error("--codes-url must be an http:// or https:// address")
    web.run_app(make_app(a.database, a.static, a.tex_assets, limits, codes, arxiv=not a.no_arxiv, codes_url=a.codes_url,
                        admin_token=admin_token, evict_days=a.evict_idle_days, texlive=a.texlive,
                        texlive_max=a.texlive_max, texlive_mirrors=a.texlive_mirror,
                        purge_days=a.purge_expired_after, lists=not a.no_project_lists, private=a.private,
                        min_version=a.min_version, files=files, data=a.data), host=a.host, port=a.port,
                backlog=LISTEN_BACKLOG, shutdown_timeout=SHUTDOWN_WAIT)


if __name__ == "__main__":
    main()
