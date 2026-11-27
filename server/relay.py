"""TeXLoom relay server: the WebSocket protocol.

Stores and relays opaque, end-to-end encrypted Yjs updates and file blobs. It never
sees keys or plaintext, and verifies Ed25519 signatures so read-only link holders
can't write. The storage rules are in policy.py, the HTTP endpoints in endpoints.py, the
application (its addresses, database and background upkeep) in app.py, and the command line
(and how storage allowances work) in cli.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque

from aiohttp import WSCloseCode, WSMsgType, web

from texloom.crypto import b64e, canonical_pub, revoke_msg, signer

from .bus import Bus, BusError, MemoryBus
from .policy import KiB, MiB, Policy
from .db import unavailable
from .store import Store
from .wire import b64d, canonical_id, client_address, verify, version_tuple

log = logging.getLogger("texloom.relay")

MAX_UPDATE = 8 * MiB
MAX_SNAPSHOT = 12 * MiB  # bigger documents just aren't compacted (their log is kept as it is)
# Every WebSocket frame is read whole before it can be checked, so keep frames small: a few
# large ones at once must not exhaust the server's memory. Clients send a p2p message over 4 MiB
# in pieces of 4 MiB, each encrypted and base64-encoded: at most about 5.4 MiB a frame.
MAX_FRAME = 16 * MiB
P2P_FRAME = 6 * MiB
FRAME_LIMITS = {"hello": 4 * KiB, "aw": 64 * KiB, "mode": 4 * KiB, "move": 4 * KiB, "delete": 4 * KiB, "up": MAX_UPDATE * 4 // 3 + KiB,
                "snap": MAX_SNAPSHOT * 4 // 3 + KiB, "p2p": P2P_FRAME, "revoke": 4 * KiB}
RATE_WINDOW, RATE_MSGS, RATE_BYTES = 10.0, 600, 160 * MiB  # per connection
HELLOS_PER_WINDOW = 5  # a log reply can be large: don't let one connection ask for it in a loop
MAX_CONNECTIONS = 2000
MAX_PER_ADDRESS = 32  # connections from one address (an IPv6 /64), as deploy/install.sh's nginx allows
MAX_PER_PROJECT = 256  # connections to one project
LISTEN_BACKLOG = 2048  # connections waiting to be accepted: everyone reconnects at once after a restart
CLOSE_WAIT = 5.0  # seconds to let connections close cleanly when the server stops
SHUTDOWN_WAIT = 10.0  # then seconds to let other requests (an upload, say) finish
MODE_SKEW = 600  # seconds a signed mode-change command stays valid
MOVE_TRIES = 5  # wrong codes a connection may offer to move a project to (each is a guess at one)
SEND_TIMEOUT = 20  # seconds; a peer that doesn't read is disconnected rather than holding up others
PEER_BUFFER_MAX = 8 * MiB  # bytes waiting to go to one peer: beyond this it isn't reading, and is cut off
OUT_MAX = 128 * MiB  # bytes waiting to go out on all connections: beyond this, peers holding a lot are cut off
STALL = 10  # seconds a large reply may make no progress before its reader is cut off
HELLO_WAIT = 10  # seconds a new connection has to say who it is (apps do at once), or it's closed
HOLD_UNVERSIONED = 600  # seconds a refused app too old to say its version is kept (see _hold)
RECHECK = 60  # seconds between a private server's checks that a connected project's code still counts
# Building and sending a catch-up reply holds the rows, their base64 and the JSON text: measured at
# about 2.6 times its stored size (Python's own count, 12 MB snapshots); 4 leaves room for buffers.
CATCHUP_OVERHEAD = 4
FRAME_OVERHEAD = 4  # a frame handled: the raw text, the parsed text, the decoded bytes, the signed message
CATCHUP_TIMEOUT = 120  # seconds to send one page; it can be large, and phones can be slow
MIN_READ = 64 * KiB  # bytes a second a reader of a large reply must take (measured over STALL), or it's cut off
PAGE = 8 * MiB  # stored bytes per page of a catch-up
# What one address may hold of the memory budget at once: two catch-ups (a page, or a snapshot)
ADDRESS_MEMORY = 2 * max(PAGE, MAX_SNAPSHOT) * CATCHUP_OVERHEAD
# Bytes that connections have sent and the server hasn't finished with, across all connections:
# a WebSocket frame is held whole until it's complete, and anyone can start one.
HELD_MAX = 128 * MiB
# What's relayed without a signature (awareness, and peer-to-peer messages: state vectors, requests
# for files, the files, and the pieces of big messages) needs only the project's id, and is sent on
# to everyone in the room. It's counted as bytes delivered (size x recipients); a connection past
# its pace, or the room past its own, isn't read from until it's back within it. Never a reason to
# cut a recipient off: one already holding UNSIGNED_BACKLOG unsent (or slow to take it) is skipped.
# The apps pace their own pieces at 96 MiB per 10 s (P2P_RATE in web/src/replica.ts and
# texloom/replica.py), so a file sent to one peer still goes at full speed.
UNSIGNED_RATE, UNSIGNED_BURST = 12 * MiB, 32 * MiB  # per connection: bytes a second, and at once
ROOM_RATE, ROOM_BURST = 32 * MiB, 96 * MiB  # per project
SMALL = 64 * KiB  # a small message (presence, a state vector) is dropped rather than waited for when its room is busy
AW_RATE, AW_BURST = 32 * KiB, 256 * KiB  # awareness (cursors, names) per connection: past this, dropped
UNSIGNED_BACKLOG = 2 * MiB  # (with one 6 MiB frame on top, still under PEER_BUFFER_MAX)
AW_BACKLOG = 256 * KiB
MAX_PAUSE = 20  # seconds a connection is left unread at a time (its pings must still get through)
# An app that asks for it ("ka" in its hello) is told this in the catch-up, then sends a "ping" when it
# has heard nothing for this many seconds and gives up on the connection when nothing at all comes
# back within as long again. A server that has stopped without closing its connections (its machine
# or network gone, or hung) is noticed in at most twice this. While its connection is left unread
# (its pace), it's sent a "wait" every half of this instead, so it isn't taken for gone.
KEEPALIVE = 10
SLOW = 30  # seconds a recipient that didn't take an unsigned message in SEND_TIMEOUT gets no more of them
# A change sent again that is already stored is passed on again (see _up) if it is among the project's
# last this many: an app resends what wasn't acknowledged, which is recent. Anyone who can hear a project's
# signed changes can replay them, so older ones aren't passed on, and those that are count against the
# sender's pace, as unsigned messages do.
REPUBLISH_RECENT = 1000
SPREAD = 3.0  # seconds over which connections are asked to resync or recheck (ask), so they don't all at once
# A connection's id is its relay's number (from the bus, so every relay's differs) times 2^32 plus a count of
# the relay's own: unique across relays, and still an integer below 2^53, as JavaScript (and older apps) take it.
ID_BITS = 32
# Who is connected to a project, and on which relay, is kept on the bus (c:<project>: connection id -> relay
# number), and each relay says it is alive there ("alive": relay number -> when, by its clock), every BEAT
# seconds. One not heard from for GONE_AFTER seconds is gone: every other relay tells its own connections to
# the projects it shared that the gone relay's connections have left. (The relays' clocks must roughly agree.)
BEAT = 3.0
GONE_AFTER = 10.0
CONN_TTL = 60.0  # seconds a project's list of connections lasts on the bus unless a relay renews it
RENEW = 20.0  # seconds between a relay's renewals of its own entries
# Gaps. For each stored project someone has open here, a relay keeps the number up to which its connections have
# had every change (Track.upto). A change from another relay numbered above the next leaves a gap; if what's
# missing hasn't come through the bus within FILL_WAIT, it is read from the database. A periodic check of the
# heads of every project open here (HEAD_CHECK; every second while the bus is away, so that changes stored
# through other relays still arrive) catches the last change of a burst whose passing on was lost: nothing after
# it would show the gap.
FILL_WAIT = 0.5
# Health (/api/health, and new connections): a relay whose bus has been away for BUS_GRACE seconds, or whose database
# was unavailable at its last try, refuses new connections (503), so a front door sends them to another relay. The
# connections it has stay: they still carry what works (stored changes reach them through the head check), and
# closing them all would turn a bus or database failover, which every relay sees at once, into a storm of reconnects.
BUS_GRACE = 5.0
# Every WS_PING seconds the relay pings every connection (a WebSocket ping frame, which every client answers by
# itself), so that a healthy connection is never quiet for long. A front door can then give up on a relay that
# says nothing for a few seconds more (nginx: proxy_read_timeout 10 s on /ws/), which finds a frozen relay before
# an app's own wait to connect (15 s) runs out: else the app gives up first, nginx counts it as the app's failure,
# not the relay's, and keeps sending new connections there (the lab: collab:pause).
WS_PING = 5.0
HEAD_CHECK = 60.0
HEAD_CHECK_AWAY = 1.0
NUMBERS = (1 << (53 - ID_BITS)) - 1  # relay numbers go round within 1 … NUMBERS


class Pace:
    """A rate with a burst (a generic cell rate algorithm): how long to wait after using n."""

    def __init__(self, rate: float, burst: float):
        self.rate, self.burst, self.tat = rate, burst, 0.0

    def wait(self, n: int) -> float:
        """Seconds that using n now would mean waiting (without using it)."""
        now = time.monotonic()
        return max(0.0, max(self.tat, now) + n / self.rate - now - self.burst / self.rate)

    def take(self, n: int) -> float:
        now = time.monotonic()
        self.tat = max(self.tat, now) + n / self.rate
        return max(0.0, self.tat - now - self.burst / self.rate)


class Conn:
    """Per-connection state: peer id, creation code, and a simple rate limiter."""

    def __init__(self, peer_id: int, address: str | None = None):
        self.id = peer_id
        self.address = address  # for per-address limits (None: not limited, see client_address)
        self.transport = None  # to cut off a peer that stops reading (a polite close would wait for it)
        self.code: str | None = None
        self.code_tried = False
        self.moves = 0  # wrong codes offered to move a project to (MOVE_TRIES)
        self.counted = False  # its app and version noted (note_client)
        self.hellos: list[float] = []
        self.recheck = 0.0  # private servers: when to check the project's code again
        self.held = 0  # bytes received and not yet finished with (see Metered)
        self.aware = False  # has sent awareness, so others must hear when it goes
        self.ka = False  # its app keeps the connection alive (hello "ka"): sent "wait" while left unread
        self.rs = False  # its app understands "resync" and "recheck" (hello "rs"); others are closed instead (see ask)
        self.pace = Pace(UNSIGNED_RATE, UNSIGNED_BURST)  # unsigned messages relayed (see UNSIGNED_RATE)
        self.aw_pace = Pace(AW_RATE, AW_BURST)
        self.pause = 0.0  # seconds to leave it unread before its next message (its pace)
        self.slow_until = 0.0  # skipped for unsigned messages until then (see SLOW)
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

    def try_take(self, n: int) -> bool:
        """Take n now if it fits (and nothing is waiting); False if not."""
        if n and (self.queue or not self._fits(n)):
            return False
        self.used += n
        return True

    def give(self, n: int) -> None:
        if not n:
            return
        self.used -= n
        while self.queue and self._fits(self.queue[0][0]):
            n, turn = self.queue.popleft()
            if not turn.cancelled():
                self.used += n
                turn.set_result(None)


class Track:
    """A stored project's changes passed on to this relay's connections: every one up to `upto`, and those above."""

    def __init__(self, upto: int):
        self.upto = upto
        self.above: set[int] = set()
        self.filling: asyncio.Task | None = None


class Relay:
    """The WebSocket protocol: a room of connections per project."""

    def __init__(self, policy: Policy):
        self.policy = policy
        self.rooms: dict[str, set[web.WebSocketResponse]] = defaultdict(set)
        self.conns: dict[web.WebSocketResponse, Conn] = {}
        self._next_id = 1
        self.memory = Budget(int(policy.limits.memory))  # big work at once (see MEMORY)
        self.held = 0  # bytes held for connections, see Metered
        self.stopping = False
        self.per_address: dict[str, int] = defaultdict(int)  # connections open, by address (MAX_PER_ADDRESS)
        self.per_project: dict[str, int] = defaultdict(int)  # connections open, by project (MAX_PER_PROJECT)
        self.room_pace: dict[str, Pace] = {}  # per project with someone connected (ROOM_RATE)
        self.address_memory: dict[str, Budget] = {}  # each address's share of `memory` (ADDRESS_MEMORY)
        self.bus: Bus = MemoryBus()  # how relays pass messages to each other (bus.py): set when the server starts
        self.number = 1  # this relay's number on the bus (join_bus): the top bits of its connections' ids
        self._inbox: dict[str, deque] = {}  # per project, messages from other relays waiting to be passed on, in order
        self._draining: dict[str, asyncio.Task] = {}  # per project, the task passing them on
        self._subscribed: set[str] = set()  # projects whose channel this relay is subscribed to
        self._gone: set[int] = set()  # relays found gone, whose connections were said to have left
        self._beating: asyncio.Task | None = None  # the heartbeat (_beat)
        self._trouble_at = 0.0  # when trouble with the bus was last logged (_bus_trouble)
        self.tracks: dict[str, Track] = {}  # per stored project open here: the changes passed on to its connections
        self.timeline = 0  # the database's timeline, as this relay last saw it (0: not read yet); see _failover
        self._db_trouble = False  # the database was unavailable lately: the head check looks every second until it's back
        self._bus_away_since: float | None = None  # when the bus was found away (health)
        self._checking: asyncio.Task | None = None  # the periodic head check (_check_heads)
        self._pinging: asyncio.Task | None = None  # pinging every connection (WS_PING)

    @property
    def store(self) -> Store:
        return self.policy.store

    async def has_peers(self, project: str) -> bool:
        """Someone has the project open here."""
        return bool(self.rooms.get(project)) or await self.connected(project) > 0

    async def connected(self, project: str) -> int:
        """How many connections the project has, on every relay (the bus's list; this relay's own if it can't be read)."""
        try:
            entries = await self.bus.hgetall(f"c:{project}")
        except BusError as e:
            self._bus_trouble(e)
            return len(self.rooms.get(project, ()))
        return max(len(self.rooms.get(project, ())), sum(1 for r in entries.values() if int(r) not in self._gone))

    # --- memory for big work: the server's budget, and each address's share of it -----------
    async def take_memory(self, conn: Conn, n: int) -> None:
        """Wait for n bytes of the memory budget: first from the connection's address's share, so
        that one address can't hold the whole budget, then from the budget."""
        if not n:
            return
        mine = self._address_memory(conn.address)
        if mine is not None:
            await mine.take(n)
        try:
            await self.memory.take(n)
        except BaseException:
            self._give_address(conn.address, n)
            raise

    def try_take_memory(self, conn: Conn, n: int) -> bool:
        if not n:
            return True
        mine = self._address_memory(conn.address)
        if mine is not None and not mine.try_take(n):
            return False
        if not self.memory.try_take(n):
            self._give_address(conn.address, n)
            return False
        return True

    def give_memory(self, conn: Conn, n: int) -> None:
        if n:
            self.memory.give(n)
            self._give_address(conn.address, n)

    def _address_memory(self, address: str | None) -> Budget | None:
        if address is None:
            return None
        b = self.address_memory.get(address)
        if b is None:
            b = self.address_memory[address] = Budget(ADDRESS_MEMORY)
        return b

    def _give_address(self, address: str | None, n: int) -> None:
        b = self.address_memory.get(address) if address is not None else None
        if b is not None:
            b.give(n)
            if not b.used and not b.queue:
                del self.address_memory[address]

    async def _admit(self, project: str, ws: web.WebSocketResponse, conn: Conn) -> dict | None:
        """A connection does nothing until it says who it is (the apps' first message is their
        hello), within HELLO_WAIT; on a private server, until its hello is accepted. Returns the hello."""
        try:
            raw = await asyncio.wait_for(ws.receive(), HELLO_WAIT)
            self.settle(conn)
            msg = json.loads(raw.data) if raw.type == WSMsgType.TEXT and len(raw.data) <= FRAME_LIMITS["hello"] else None
        except (asyncio.TimeoutError, ValueError, TypeError):
            msg = None
        if not isinstance(msg, dict) or msg.get("t") != "hello":
            await self.close_or_cut(ws, b"hello first")
            return None
        why = await self.policy.denied(project, msg.get("code"))  # (binds the project to nothing yet)
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

    async def try_again(self, ws: web.WebSocketResponse) -> None:
        """The database is unavailable just now: close the connection saying so (1013, "try again later"). Every app
        connects again after a close, with a growing wait, and sends again what wasn't acknowledged. Meanwhile the head
        check reads the database's timeline every second, to notice a failover as soon as it answers."""
        self._db_trouble = True
        try:
            await asyncio.wait_for(ws.close(code=1013, message=b"database unavailable: try again"), 5)
        except (asyncio.TimeoutError, ConnectionError, RuntimeError):
            self.cut_off(ws, "didn't take its close")

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

    async def send_unsigned(self, ws: web.WebSocketResponse, text: str, backlog: int) -> None:
        """Send something relayed unsigned (see UNSIGNED_RATE): to a peer already holding `backlog`
        unsent, or slow to take the last one, not at all. Such a message is never a reason to cut a
        peer off, or anyone with the project's id could have members cut off."""
        conn = self.conns.get(ws)
        if ws.closed or conn is None:
            return
        if conn.transport is not None and (conn.transport.get_write_buffer_size() > backlog or time.monotonic() < conn.slow_until):
            return
        try:
            await asyncio.wait_for(ws.send_str(text), SEND_TIMEOUT)
        except asyncio.TimeoutError:  # (it's in its buffer; a peer that isn't reading goes at its next missed ping)
            conn.slow_until = time.monotonic() + SLOW
        except (ConnectionError, RuntimeError):
            pass

    def out_waiting(self) -> int:
        """Bytes waiting to go out on every connection."""
        return sum(c.transport.get_write_buffer_size() for c in self.conns.values() if c.transport is not None)

    async def send_large(self, ws: web.WebSocketResponse, msg: dict) -> None:
        """Send a large reply, cutting off a reader that doesn't take it at MIN_READ, so that it
        can't hold the memory (and the catch-up budget) for long."""
        conn = self.conns.get(ws)
        task = asyncio.ensure_future(ws.send_json(msg))
        waiting = -1
        while True:
            done, _ = await asyncio.wait({task}, timeout=STALL)
            if done:
                task.result()
                return
            now = conn.transport.get_write_buffer_size() if conn and conn.transport is not None else 0
            if waiting != -1 and waiting - now < min(MIN_READ * STALL, waiting):  # too little taken since the last look
                self.cut_off(ws, "was too slow taking a large reply")
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

    async def broadcast(self, project: str, msg: dict, exclude=None, tl: int = 0) -> None:
        """To everyone connected to the project, here and on the other relays (but `exclude`), who must all have it:
        one not reading is cut off rather than skipped."""
        text = json.dumps(msg)
        peers = [ws for ws in self.rooms.get(project, ()) if ws is not exclude]
        if peers and len(text) > MiB:
            self.make_room(len(text) * len(peers))
        if peers:
            await asyncio.gather(*(self.send(ws, text) for ws in peers))
        q = msg.get("q", 0) if msg.get("t") == "up" else 0
        await self.pass_on(project, text, msg.get("t", ""), "s", q=q, tl=(tl or self.timeline) if q else 0)

    # --- the bus ------------------------------------------------------------------------
    async def join_bus(self, bus: Bus) -> None:
        """Start using the bus (at start-up), and take this relay's number from its counter."""
        self.bus = bus
        await bus.start(self._from_bus, self._bus_broke)
        self.number = (await bus.incr("relays") - 1) % NUMBERS + 1

        await self.bus.subscribe(f"r:{self.number}")  # (messages for one of this relay's connections)
        self._beating = asyncio.get_running_loop().create_task(self._beat())
        self._checking = asyncio.get_running_loop().create_task(self._check_heads())
        self._pinging = asyncio.get_running_loop().create_task(self._ping_all())

    async def leave_bus(self) -> None:
        """When the server stops: no more heartbeats, and the other relays told at once that it has gone."""
        for task in (self._beating, self._checking, self._pinging):
            if task:
                task.cancel()
        try:  # (marked long gone, so that the others tidy up anything of it left on the bus)
            await self.bus.hset("alive", str(self.number), b"0", ttl=GONE_AFTER * 30)
        except BusError:
            pass

    async def _ping_all(self) -> None:
        async def ping(ws) -> None:
            try:
                await asyncio.wait_for(ws.ping(), WS_PING)
            except Exception:  # (one that is closing, or not reading: its own checks deal with it)
                pass
        while True:
            await asyncio.sleep(WS_PING)
            for ws in list(self.conns):
                if not ws.closed:
                    asyncio.get_running_loop().create_task(ping(ws))

    def _bus_trouble(self, e: Exception) -> None:
        """The bus couldn't be used for something: said once a minute at most (it is soft state, and the relay goes on)."""
        if time.monotonic() - self._trouble_at > 60:
            self._trouble_at = time.monotonic()
            log.warning("the bus: %s", e)

    # Messages to other relays. A message is a header (JSON: the relay it's from, its type, how to send it, the
    # one connection it's for, if just one) and the text to send, on the project's channel (p:<project>, every
    # relay with someone connected to it) or one relay's (r:<number>, for one of its connections). How to send:
    # "s" to everyone, cutting off one that isn't reading; "u" unsigned, skipped for a peer that is behind; "a"
    # presence, likewise with less room (see send and send_unsigned).
    async def pass_on(self, project: str, text: str, t: str, kind: str, to: int | None = None, relay: int | None = None,
                      q: int = 0, tl: int = 0) -> None:
        """`q`, `tl`: a stored change's number, and the database's timeline that numbered it."""
        head = {"r": self.number, "p": project, "t": t, "k": kind, **({"to": to} if to is not None else {}),
                **({"q": q, "tl": tl} if q else {})}
        try:
            await self.bus.publish(f"r:{relay}" if relay is not None else f"p:{project}",
                                   json.dumps(head, separators=(",", ":")).encode() + b"\n" + text.encode())
        except BusError as e:
            self._bus_trouble(e)

    async def relay_of(self, project: str, conn_id: int) -> int | None:
        """The relay holding a connection, by the bus's list."""
        try:
            r = await self.bus.hget(f"c:{project}", str(conn_id))
        except BusError as e:
            self._bus_trouble(e)
            return None
        return None if r is None else int(r)

    def _from_bus(self, channel: str, data: bytes) -> None:
        """A message from another relay (a callback in the event loop): queued, and passed on in order, per project."""
        head, _, body = bytes(data).partition(b"\n")
        try:
            h = json.loads(head)
            project = h["p"]
        except (ValueError, KeyError, TypeError):
            log.warning("ignoring a message on the bus that isn't one of ours: %r", bytes(data[:80]))
            return
        if h.get("r") == self.number or project not in self.rooms:
            return  # its own (it was passed on here first), or nobody here for it
        self._inbox.setdefault(project, deque()).append((h, body.decode()))
        if project not in self._draining:
            self._draining[project] = asyncio.get_running_loop().create_task(self._drain(project))

    async def _drain(self, project: str) -> None:
        try:
            while box := self._inbox.get(project):
                h, text = box.popleft()
                try:
                    await self._deliver(project, h, text)
                except Exception:
                    log.exception("passing on a message from another relay")
        finally:
            self._inbox.pop(project, None)
            self._draining.pop(project, None)

    async def _deliver(self, project: str, h: dict, text: str) -> None:
        to = h.get("to")
        peers = [ws for ws in self.rooms.get(project, ()) if to is None or (c := self.conns.get(ws)) is not None and c.id == to]
        if not peers:
            return
        t, kind = h.get("t"), h.get("k")
        if t == "up" and h.get("q"):
            tl = h.get("tl", 0)
            if tl > self.timeline:  # numbered after a failover this relay hasn't noticed: it does first (models/relays)
                await self._failover(tl)
            if tl == self.timeline and not self._new(project, h["q"]):
                return  # passed on here already (through the bus, or read from the database)
            # (numbered by an older timeline: passed on, as any copy may be, but its number now means another change)
        if t == "moved":  # what's stored and the limits changed: each connection told what it may see (see _move)
            for ws in peers:
                if (c := self.conns.get(ws)) is not None:
                    await self.send(ws, json.dumps({"t": "usage", "store": self.policy.store_status(project, c.code), "moved": True}))
            return
        if kind == "s":
            if len(text) > MiB:
                self.make_room(len(text) * len(peers))
            await asyncio.gather(*(self.send(ws, text) for ws in peers))
        else:
            await asyncio.gather(*(self.send_unsigned(ws, text, AW_BACKLOG if kind == "a" else UNSIGNED_BACKLOG) for ws in peers))
        if t == "deleted":  # the owner deleted it for everyone: those connected here are closed too
            for ws in peers:
                asyncio.create_task(self.close_or_cut(ws, b"deleted"))

    # Who is connected, on the bus. Each relay keeps its own entries, renews them, and removes them as they go.
    async def _joined(self, project: str, conn: Conn) -> None:
        await self._follow(project)
        if project not in self.tracks and not await self.store.relay_only(project):
            # (after subscribing: what is stored from now on comes through the bus, or is found by the head check)
            self.tracks.setdefault(project, Track(await self.store.head(project)))
        try:
            await self.bus.hset(f"c:{project}", str(conn.id), str(self.number).encode(), ttl=CONN_TTL)
        except BusError as e:
            self._bus_trouble(e)

    async def _left(self, project: str, conn: Conn) -> None:
        if project not in self.rooms and (t := self.tracks.pop(project, None)) and t.filling:
            t.filling.cancel()
        await self._follow(project)
        try:
            await self.bus.hdel(f"c:{project}", str(conn.id))
        except BusError as e:
            self._bus_trouble(e)

    async def _follow(self, project: str) -> None:
        """Subscribe to a project's channel while someone is connected to it here, and only then. (Decided on what
        is so now, before any wait, so that a room emptied and filled again ends subscribed.)"""
        want = project in self.rooms
        if want == (project in self._subscribed):
            return
        try:
            if want:
                self._subscribed.add(project)
                await self.bus.subscribe(f"p:{project}")
            else:
                self._subscribed.discard(project)
                await self.bus.unsubscribe(f"p:{project}")
        except BusError as e:  # (a subscription asked for while the bus is away is made when it's back)
            self._bus_trouble(e)

    async def _beat(self) -> None:
        """Every BEAT seconds: say this relay is alive, renew its entries now and then, and find relays that have gone."""
        renewed = 0.0
        while True:
            self._note_bus()
            try:
                now = time.time()
                await self.bus.hset("alive", str(self.number), str(now).encode(), ttl=GONE_AFTER * 30)
                if time.monotonic() - renewed > RENEW:
                    renewed = time.monotonic()
                    await self._rewrite()
                alive = await self.bus.hgetall("alive")
                gone = {int(r) for r, at in alive.items() if now - float(at) > GONE_AFTER} - {self.number}
                back = self._gone - gone
                self._gone -= back  # (heard from again: a relay that was frozen, say)
                if new := gone - self._gone:
                    self._gone |= new
                    await self._said_left(new)
                stale = [r for r, at in alive.items() if now - float(at) > GONE_AFTER * 20]
                if stale:  # (long gone: every relay has had time to notice)
                    await self.bus.hdel("alive", *stale)
            except BusError as e:
                self._bus_trouble(e)
            except Exception:
                log.exception("the relay's heartbeat")
            await asyncio.sleep(BEAT)

    async def _rewrite(self) -> None:
        """Write this relay's entries again (renewing them, and after a bus that failed over and lost them)."""
        for project, room in list(self.rooms.items()):
            for ws in list(room):
                if (c := self.conns.get(ws)) is not None:
                    await self.bus.hset(f"c:{project}", str(c.id), str(self.number).encode(), ttl=CONN_TTL)

    async def _said_left(self, gone: set[int]) -> None:
        """Relays have gone without a word: their connections to projects someone here has open have left. Each relay
        tells its own connections (so nothing needs passing on), and takes the entries off the list."""
        log.info("relay(s) %s gone: telling the connections here", ", ".join(map(str, sorted(gone))))
        for project in list(self.rooms):
            try:
                entries = await self.bus.hgetall(f"c:{project}")
                ids = [cid for cid, r in entries.items() if int(r) in gone]
                if ids:
                    await self.bus.hdel(f"c:{project}", *ids)
            except BusError as e:
                self._bus_trouble(e)
                return
            for cid in ids:
                text = json.dumps({"t": "left", "from": int(cid)})
                await asyncio.gather(*(self.send(ws, text) for ws in list(self.rooms.get(project, ()))))

    def _bus_broke(self) -> None:
        """The connection to the bus broke and is back: anything may have been missed meanwhile."""
        async def mend() -> None:
            # what other relays passed on meanwhile was missed: stored projects read it from the database, and the
            # apps of sync-only ones (nothing stored to read) catch up from each other
            await self._check_heads(once=True)
            sync_only = [ws for project, room in self.rooms.items() if project not in self.tracks for ws in room]
            asyncio.get_running_loop().create_task(self.ask("resync", sync_only))
            await self._keep_number()
            await self._rewrite()  # (a bus that failed over may have lost them)
        task = asyncio.get_running_loop().create_task(mend())
        task.add_done_callback(lambda t: t.cancelled() or not t.exception() or log.warning("after a break in the bus: %s", t.exception()))

    async def _keep_number(self) -> None:
        """A bus that failed over can lose its counter's last additions (copying to a replica lags), and would then
        give a relay starting now a number already in use. Every relay puts the counter back past its own."""
        n = await self.bus.incr("relays", 0)
        if n < self.number:
            await self.bus.incr("relays", self.number - n)

    def _new(self, project: str, q: int) -> bool:
        """A stored change numbered `q` is about to be passed on to this relay's connections: False if it has been
        already. Notes it, and if it leaves a gap, sets the gap to be filled from the database unless it closes."""
        t = self.tracks.get(project)
        if t is None:  # (not known yet: a room just made; its head check sets it)
            return True
        if q <= t.upto or q in t.above:
            return False
        if q == t.upto + 1:
            t.upto = q
            while t.upto + 1 in t.above:
                t.above.discard(t.upto + 1)
                t.upto += 1
        else:
            t.above.add(q)
            if t.filling is None or t.filling.done():
                t.filling = asyncio.get_running_loop().create_task(self._fill(project, FILL_WAIT))
        return True

    async def _fill(self, project: str, wait: float = 0.0, head: int | None = None) -> None:
        """Pass on to this relay's connections the stored changes they haven't had, read from the database (all of
        them up to the head, or up to `head`): a gap that didn't close, or what the head check found."""
        if wait:
            await asyncio.sleep(wait)
        t = self.tracks.get(project)
        if t is None or project not in self.rooms:
            return
        try:
            while True:
                entries, last, done = await self.store.since(project, t.upto, PAGE)
                for e in entries:
                    if head is not None and e["q"] > head:
                        return
                    if e["snap"]:
                        # compacted meanwhile: a snapshot can't be passed on as a change; the apps catch up instead
                        log.info("a gap in %s… was compacted: its apps catch up again", project[:8])
                        await self.ask("recheck", list(self.rooms.get(project, ())))
                        t.upto, t.above = max(t.upto, e["q"]), {q for q in t.above if q > e["q"]}
                        continue
                    if self._new(project, e["q"]):
                        text = json.dumps({"t": "up", "q": e["q"], "d": e["d"], "s": e["s"]})
                        await asyncio.gather(*(self.send(ws, text) for ws in list(self.rooms.get(project, ()))))
                if done or not entries:
                    return
        except Exception as e:
            if not unavailable(e):
                raise
            log.warning("couldn't fill a gap in %s… (the database: %s); the next head check will", project[:8], e)

    async def _check_heads(self, once: bool = False) -> None:
        """The heads of every stored project open here, read at once: any past what this relay has passed on is
        filled. Every HEAD_CHECK seconds, every HEAD_CHECK_AWAY while the bus is away; or `once`."""
        last = 0.0  # (the first check at once: it reads the database's timeline)
        while True:
            if not once:
                await asyncio.sleep(HEAD_CHECK_AWAY)  # (looking every second whether the bus is away, or it's time)
                if self.bus.connected and not self._db_trouble and time.monotonic() - last < HEAD_CHECK:
                    continue
                last = time.monotonic()
            projects = [p for p in self.tracks if p in self.rooms]
            try:
                await self._failover(await self.store.db.timeline())
                self._db_trouble = False
                heads = await self.store.heads(projects) if projects else {}
                for project, head in heads.items():
                    t = self.tracks.get(project)
                    if t is not None and head > t.upto and (t.filling is None or t.filling.done()):
                        t.filling = asyncio.get_running_loop().create_task(self._fill(project, head=head))
            except Exception as e:
                if not unavailable(e):
                    log.exception("checking the heads of open projects")
                else:
                    self._db_trouble = True
            if once:
                return

    async def _failover(self, tl: int) -> None:
        """The database's timeline is `tl`: if it went up, the database failed over, and may have lost changes it had
        acknowledged, and numbers changes again from where its new primary's log ends. So this relay's connections are
        asked to recheck (their hello's "last" finds what's lost, and they send it again), and what it tracks of each
        project starts afresh from the new heads. Changes it stored before and hasn't acknowledged are stored again
        (_up). Decided before any wait, so that it happens once."""
        if tl <= self.timeline:
            return
        before, self.timeline = self.timeline, tl
        if not before:
            return  # (the first reading)
        log.warning("the database failed over (timeline %d -> %d): asking every connection here to recheck", before, tl)
        # recheck first, at once, before this relay passes anything else on (models/relays: no-timeline-on-bus)
        await self.ask("recheck", spread=0)
        heads = await self.store.heads(list(self.tracks))
        for project, t in self.tracks.items():
            t.upto, t.above = heads.get(project, 0), set()

    def db_back(self) -> None:
        """The database is back after a connection to it was lost (the store's listener says so): it may have failed
        over meanwhile, so its timeline is read now."""
        self._db_trouble = True
        asyncio.get_running_loop().create_task(self._check_heads(once=True))

    async def ask(self, t: str, sockets: list | None = None, spread: float = SPREAD) -> None:
        """Ask connections (all of this relay's, or `sockets`) to catch up again, spread over `spread` seconds:
        - "resync": catch up from their peers (they send their state vector, as after connecting). For sync-only
          projects, after this relay may have missed messages from the others (a break in the bus).
        - "recheck": check that the server's log is still the one they saw (they send their hello again, whose
          "last" finds out). After the database failed over, which can lose changes it had acknowledged.
        FUTURE_ARCHITECTURE.md, "Messages"."""
        sockets = list(self.conns) if sockets is None else sockets
        gap = spread / max(len(sockets), 1)
        for ws in sockets:
            conn = self.conns.get(ws)
            if conn is None or ws.closed:
                continue
            if conn.rs:
                await self.send(ws, json.dumps({"t": t}))
            else:
                # compat(app 0.4.67): apps that don't say "rs" in their hello (0.4.67 and older) are closed instead: they connect
                # again and do the same. (Set VERSION to the release that first sends "rs" when the stateless-relays branch is merged.)
                asyncio.create_task(self.close_or_cut(ws, b"catch up again"))
            await asyncio.sleep(gap)

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

    def _note_bus(self) -> None:
        """When the bus was found away (by the heartbeat, every BEAT seconds, and when health is asked)."""
        if self.bus.connected:
            self._bus_away_since = None
        elif self._bus_away_since is None:
            self._bus_away_since = time.monotonic()

    def unhealthy(self) -> str | None:
        """Why this relay shouldn't take new connections just now (None: it should): see BUS_GRACE."""
        self._note_bus()
        if self._bus_away_since is not None and time.monotonic() - self._bus_away_since > BUS_GRACE:
            return "the bus is away"
        if self._db_trouble:
            return "the database is unavailable"
        return None

    async def health(self, _request: web.Request) -> web.Response:
        """For a front door or a monitor: 200 if this relay takes new connections, else 503 saying why."""
        why = "stopping" if self.stopping else self.unhealthy()
        return web.json_response({"ok": why is None, "relay": self.number, "connections": len(self.conns),
                                  **({"why": why} if why else {})}, status=200 if why is None else 503)

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        if self.stopping:
            raise web.HTTPServiceUnavailable(text="server restarting")
        if why := self.unhealthy():
            raise web.HTTPServiceUnavailable(text=why)
        project = request.match_info["project"]
        if not canonical_id(project):
            raise web.HTTPBadRequest(text="bad project id")
        if len(self.conns) >= MAX_CONNECTIONS:
            raise web.HTTPServiceUnavailable(text="too many connections")
        address = client_address(request)
        if address is not None and self.per_address[address] >= MAX_PER_ADDRESS:
            raise web.HTTPTooManyRequests(text="too many connections from this address")
        if self.per_project[project] >= MAX_PER_PROJECT or await self.connected(project) >= MAX_PER_PROJECT:
            raise web.HTTPServiceUnavailable(text="too many connections to this project")
        # no compression: what's sent is ciphertext, and compressed frames would let a client make
        # the server hold far more than it sent (Metered counts bytes as they arrive)
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_FRAME, compress=False)
        conn = Conn(self.number << ID_BITS | self._next_id, address)
        self._next_id = self._next_id % ((1 << ID_BITS) - 1) + 1
        if address is not None:
            self.per_address[address] += 1
        self.per_project[project] += 1
        try:
            await ws.prepare(request)
            self.conns[ws] = conn
            if request.transport:
                conn.transport = request.transport
                request.transport.set_protocol(Metered(request.transport.get_protocol(), self, conn, request.transport))
            await self._serve(project, ws, conn)
        finally:
            self.conns.pop(ws, None)
            if address is not None:
                self.per_address[address] -= 1
                if not self.per_address[address]:
                    del self.per_address[address]
            self.per_project[project] -= 1
            if not self.per_project[project]:
                del self.per_project[project]
        return ws

    async def _serve(self, project: str, ws: web.WebSocketResponse, conn: Conn) -> None:
        gone = await self.store.deleted(project)
        if gone is not None:  # the owner deleted it for everyone: say so, and nothing more
            self.conns.pop(ws, None)
            await ws.send_json(gone)
            await ws.close(code=1008, message=b"deleted")
            return
        first = await self._admit(project, ws, conn)
        if first is None:
            return
        conn.recheck = time.monotonic() + RECHECK
        room = self.rooms[project]
        room.add(ws)
        if project not in self.room_pace:
            self.room_pace[project] = Pace(ROOM_RATE, ROOM_BURST)
        log.info("join %s… (%d peers)", project[:8], len(room))
        try:
            try:
                await self._joined(project, conn)  # (subscribed to the project's channel before the catch-up reads the log)
                await self.handle(project, ws, conn, "hello", first)
            except Exception as e:
                if not unavailable(e):
                    raise
                log.warning("couldn't answer a hello, the database being unavailable (%s): asking the app to try again", e)
                await self.try_again(ws)
                return
            async for raw in ws:
                self.settle(conn)
                if raw.type != WSMsgType.TEXT:
                    continue
                if not conn.allow(len(raw.data)):
                    await ws.close(code=1008, message=b"rate limit")
                    break
                if self.policy.private and time.monotonic() >= conn.recheck:
                    conn.recheck = time.monotonic() + RECHECK
                    why = await self.policy.denied(project, conn.code)
                    if why:
                        await self._refuse(ws, why)
                        break
                cost = len(raw.data) * FRAME_OVERHEAD if len(raw.data) > MiB else 0
                if cost and not self.try_take_memory(conn, cost):  # the server (or this address) is busy with big work
                    await ws.send_json({"t": "err", "m": "server busy: try again"})
                    continue
                msg = None
                try:
                    msg = json.loads(raw.data)
                    if not isinstance(msg, dict):
                        raise BadMessage("not an object")
                    t = field(msg, "t", str)
                    if len(raw.data) > FRAME_LIMITS.get(t, 4 * KiB):
                        raise BadMessage(f"{t} frame too large")
                    if t == "ping":  # (answered here, before anything that could wait on the database)
                        await ws.send_json({"t": "pong"})
                        continue
                    await self.handle(project, ws, conn, t, msg)
                except ConnectionError:  # it went while its message was handled (an answer had nowhere to go)
                    if not ws.closed:  # (no: the database's connection failed)
                        await self.try_again(ws)
                    break
                except (BadMessage, ValueError, TypeError, KeyError, OverflowError, RecursionError) as e:  # (binascii.Error is a ValueError)
                    await ws.send_json({"t": "err", "m": f"bad message: {e}"[:200]})
                except Exception as e:
                    if unavailable(e):
                        log.warning("the database is unavailable (%s): asking the app to try again", e)
                        await self.try_again(ws)
                        break
                    log.exception("error handling message")
                finally:
                    self.give_memory(conn, cost)
                    del raw, msg
                if conn.pause:  # past its pace (unsigned messages): nothing more is read from it for a while
                    pause, conn.pause = min(conn.pause, MAX_PAUSE), 0.0
                    end = time.monotonic() + pause
                    while (left := end - time.monotonic()) > 0:
                        if conn.ka:  # so that its app, hearing nothing, doesn't give up on the connection
                            await ws.send_json({"t": "wait"})
                        await asyncio.sleep(min(left, KEEPALIVE / 2))
        finally:
            room.discard(ws)
            self.conns.pop(ws, None)
            if not room:
                self.rooms.pop(project, None)
                self.room_pace.pop(project, None)
            log.info("leave %s…", project[:8])
            await self._left(project, conn)
            if conn.aware:  # this also covers a crash or a lost connection, where a client can't say goodbye (and a stop: other relays' apps hear it)
                await self.broadcast(project, {"t": "left", "from": conn.id})

    # compat(refused 0.4.30): apps too old to say their version ("unknown" on the admin page): remove _hold and HOLD_UNVERSIONED
    async def _hold(self, project: str, ws, conn: Conn) -> None:
        """Keep a refused app that's too old to say its version (before 0.4.30) connected, hearing
        nothing, for a while. It doesn't understand the refusal, and after a connection that opened it
        waits only a second before the next: closed at once, it would connect every second or two until
        its user updates it. Its pings are still answered (else it would leave and come straight back)."""
        self.rooms[project].discard(ws)  # nothing more of the project reaches it
        try:
            async with asyncio.timeout(HOLD_UNVERSIONED):
                async for raw in ws:
                    if raw.type == WSMsgType.TEXT and not conn.allow(len(raw.data)):
                        break
        except TimeoutError:
            pass

    async def _not_as_seen(self, project: str, last) -> bool:
        """An app's hello says which change it last has here ("last": [its number, its content's
        SHA-256]). True when this server's log isn't the one it got that from: the log ends before
        that number, or has another change there (a server restored from a backup, which numbers
        changes again from where the backup ends)."""
        if not (isinstance(last, list) and len(last) == 2 and isinstance(last[0], int) and not isinstance(last[0], bool)
                and last[0] > 0 and isinstance(last[1], str)):
            return False
        try:
            h = b64d(last[1])
        except ValueError:
            return False
        if last[0] > await self.store.head(project):
            return True
        return await self.store.same_entry(project, last[0], h) is False

    async def handle(self, project: str, ws, conn: Conn, t: str, msg: dict) -> None:
        if await self.store.deleted(project):  # deleted while this connection was closing: nothing more
            return
        relay_only = await self.store.relay_only(project)
        handler = self.HANDLERS.get(t)
        if handler is None:
            raise BadMessage(f"unknown type {t!r}")
        await handler(self, project, ws, conn, msg, relay_only)

    @staticmethod
    def _owner_signed(project: str, text: str, ts: int, sig: bytes) -> bool:
        """An owner's command (mode, move, delete): signed with the owner key, and recent (MODE_SKEW)."""
        return abs(time.time() - ts) <= MODE_SKEW and signer(project, text.encode(), sig) == "owner"

    async def _may_add(self, project: str, logged: bool = False) -> bool:
        """A project this server has never held anything for costs a permanent row: from the same
        budget as other rows that store nothing, so ids can't be minted to fill the disk. True if
        it's known here (registered, a storage mode set; with `logged`, or anything in its log, as
        another server sharing the database may have stored), or the budget has room."""
        return (self.store.owner(project) is not None or await self.store.has_mode(project)
                or logged and await self.store.head(project) > 0 or self.store.meta_budget())

    async def _hello(self, project: str, ws, conn: Conn, msg: dict, relay_only: bool) -> None:
        if not conn.allow_hello():
            await ws.close(code=1008, message=b"rate limit")
            return
        if not conn.counted:  # (once per connection: hellos repeat)
            conn.counted = True
            await self.policy.note_client(msg.get("app"), msg.get("v"))
        conn.ka = msg.get("ka") == 1
        conn.rs = msg.get("rs") == 1
        if self.policy.too_old(msg.get("v")):
            await ws.send_json({"t": "err", "denied": True, **self.policy.update_needed()})
            if version_tuple(msg.get("v")) is None:
                await self._hold(project, ws, conn)
            await ws.close(code=1008, message=b"update needed")
            return
        await self.store.touch(project)
        if msg.get("code") and not conn.code_tried:  # one code check per connection
            conn.code_tried = True
            conn.code = self.policy.codes.check(field(msg, "code", str))
        since = max(0, min(field(msg, "since", int, 0), 2**62))
        # Is the log here the one the app last saw? Not if it was put back from a backup since:
        # the app then starts again from the beginning and sends all it has (as after eviction)
        lost = not relay_only and await self._not_as_seen(project, msg.get("last"))
        if lost:
            since = 0
        start = await self.store.catch_up_start(project, since)
        size = await self.store.since_size(project, start)
        # A long log goes a page at a time ("more", then `logmore`), so a catch-up needs a page's
        # memory whatever the log's size (a page is one entry at least, and a snapshot can be
        # bigger than a page). Every app takes pages ("pages" in its hello, since 0.4.33).
        cost = min(size, max(PAGE, MAX_SNAPSHOT)) * CATCHUP_OVERHEAD if size >= MiB else 0
        await self.take_memory(conn, cost)
        try:
            entries, last, done = await self.store.since(project, start, PAGE)
            head, floor, count = await self.store.log_state(project)
            await asyncio.wait_for(self.send_large(ws,
                {"t": "log", "entries": entries, "head": head, "floor": floor, "count": count, "you": conn.id,
                 "mode": "relay" if relay_only else "server", "peers": await self.connected(project) - 1, "ka": KEEPALIVE,
                 "store": self.policy.store_status(project, conn.code),
                 "rv": [{"p": p, "s": b64e(sig)} for p, sig in (await self.store.revoked(project)).items()],
                 **({"lost": True} if lost else {}), **({} if done else {"more": True})}), CATCHUP_TIMEOUT)
            while not done:
                del entries
                entries, last, done = await self.store.since(project, last, PAGE)
                await asyncio.wait_for(self.send_large(ws, {"t": "logmore", "entries": entries, **({} if done else {"more": True})}),
                                       CATCHUP_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError, RuntimeError):  # too slow, or it has gone
            self.cut_off(ws, "was too slow to catch up")
        finally:
            self.give_memory(conn, cost)

    async def _up(self, project: str, ws, conn: Conn, msg: dict, relay_only: bool) -> None:
        d, s = field(msg, "d", str), field(msg, "s", str)
        data, sig = b64d(d), b64d(s)
        revoked = await self.store.revoked(project)
        ok = len(data) <= MAX_UPDATE and (verify(project, b"upd:" + data, sig, revoked) if len(data) < 64 * KiB
                                          else await asyncio.to_thread(verify, project, b"upd:" + data, sig, revoked))
        if not ok:
            await ws.send_json({"t": "err", "m": "rejected update (bad signature, or this personal link was revoked)"})
            return
        # A change is passed on to the others first and acknowledged after (FUTURE_ARCHITECTURE.md, "Messages"):
        # a relay that stops in between hasn't acknowledged it, so its app sends it again, and then it is
        # passed on (again: receivers ignore a copy they have).
        if relay_only:  # pass it on, but keep nothing
            await self.broadcast(project, {"t": "up", "q": 0, "d": d, "s": s}, ws)
            await ws.send_json({"t": "ack", "q": 0, "id": msg.get("id")})
            return
        # One transaction holding the project: is it a replay, may it be stored, store it. If the database failed over
        # between storing it and acknowledging it (this relay noticed meanwhile), the new primary may not have it: it
        # is stored again, and acknowledged with the number it has there (models/relays: no-store-again).
        for _ in range(3):
            try:
                async with self.store.locked(project) as c:
                    dup = await self.store.duplicate(project, data, c)
                    refused = None if dup is not None else await self.policy.may_store(project, len(data) + len(sig), conn.code, c)
                    seq = None
                    if dup is None and not refused:
                        seq = await self.store.append(project, data, sig, c)
                    recent = dup is not None and dup > 0 and dup > await self.store.head(project, c) - REPUBLISH_RECENT
                    tl = await c.timeline()
            except Exception as e:
                if unavailable(e):  # not stored: collaborators online still get it, and the app is told to try again
                    await self.broadcast(project, {"t": "up", "q": 0, "d": d, "s": s}, ws)
                raise
            await self._failover(tl)  # (a later timeline than this relay knew: it failed over before this was stored)
            if refused:
                # Collaborators online still get it; the sender keeps it (no ack) and retries later.
                await self.broadcast(project, {"t": "up", "q": 0, "d": d, "s": s}, ws)
                await ws.send_json({"t": "err", "code": refused, "m": f"not stored: {refused}", "id": msg.get("id")})
                return
            q = seq if dup is None else dup
            if dup is None:
                self._new(project, seq)  # (its own: passed on to its connections now)
                await self.broadcast(project, {"t": "up", "q": seq, "d": d, "s": s}, ws, tl=tl)
            elif recent:  # stored already: kept once, but a recent one is passed on again
                peers = len(self.rooms.get(project, ())) - 1
                conn.pause = max(conn.pause, conn.pace.take(len(d) * max(peers, 0)))
                await self.broadcast(project, {"t": "up", "q": dup, "d": d, "s": s}, ws, tl=tl)
            if tl == self.timeline:
                break
        await ws.send_json({"t": "ack", "q": q, "id": msg.get("id")})

    async def _snap(self, project: str, ws, conn: Conn, msg: dict, relay_only: bool) -> None:
        upto = field(msg, "upto", int)
        data, sig = b64d(field(msg, "d", str)), b64d(field(msg, "s", str))
        if relay_only or self.store.owner(project) is None:
            return
        if len(data) > MAX_SNAPSHOT or not await asyncio.to_thread(
                verify, project, f"snap:{upto}:".encode() + data, sig, await self.store.revoked(project)):
            await ws.send_json({"t": "err", "m": "rejected snapshot"})
            return
        async with self.store.locked(project) as c:
            # A snapshot must replace part of the log: after the previous snapshot (and anything
            # evicted), up to head.
            after = max(await self.store.last_snapshot(project, c), await self.store.floor(project, c))
            fits = after < upto <= await self.store.head(project, c)
            if fits:
                delta = await self.store.snapshot_delta(project, upto, len(data) + len(sig), c)
                if await self.policy.may_store(project, delta, conn.code, c):
                    return  # over a limit: skip compaction; the log stays as it is
                await self.store.snapshot(project, upto, data, sig, c)
        if not fits:
            await ws.send_json({"t": "err", "m": "rejected snapshot"})
            return
        log.info("compacted %s… up to %d", project[:8], upto)
        # the sender may now delete what the snapshot replaced (changes it kept as uploads)
        await ws.send_json({"t": "snapped", "upto": upto})

    def _recipients(self, project: str, ws, to: int | None) -> list:
        """Everyone else in the room, or the one connection numbered `to`."""
        return [peer for peer in self.rooms.get(project, ()) if peer is not ws and
                (to is None or (c := self.conns.get(peer)) is not None and c.id == to)]

    async def _relay_unsigned(self, project: str, ws, conn: Conn, out: dict, to: int | None) -> None:
        """Pass on something unsigned (see UNSIGNED_RATE), and pace its sender."""
        peers = self._recipients(project, ws, to)
        # to everyone: here, and the other relays (once, on the project's channel); to one: here, or its relay's channel
        there = None if to is None or peers else await self.relay_of(project, to)
        if to is not None and not peers and there is None:
            return
        text = json.dumps(out)
        n = len(text) * (len(peers) + (to is None or there is not None))  # (the other relays' copy counts once)
        room = self.room_pace.get(project) or Pace(ROOM_RATE, ROOM_BURST)
        if len(text) <= SMALL and room.wait(n) > 0:
            return  # the room is busy: something small is dropped rather than waited for
        if len(text) > MiB and self.out_waiting() + n > OUT_MAX:
            return  # the server holds a lot unsent already
        await asyncio.gather(*(self.send_unsigned(peer, text, UNSIGNED_BACKLOG) for peer in peers))
        if to is None:
            await self.pass_on(project, text, "p2p", "u")
        elif there is not None:
            await self.pass_on(project, text, "p2p", "u", to=to, relay=there)
        conn.pause = max(conn.pause, conn.pace.take(n), room.take(n))

    async def _aw(self, project: str, ws, conn: Conn, msg: dict, relay_only: bool) -> None:
        conn.aware = True
        text = json.dumps({"t": "aw", "d": field(msg, "d", str), "from": conn.id})
        peers = self._recipients(project, ws, None)
        room = self.room_pace.get(project) or Pace(ROOM_RATE, ROOM_BURST)
        n = len(text) * len(peers)
        # presence is sent often and superseded by the next: past its pace, or its room's, dropped
        if conn.aw_pace.wait(len(text)) > 0 or room.wait(n) > 0:
            return
        conn.aw_pace.take(len(text))
        room.take(n)
        await asyncio.gather(*(self.send_unsigned(peer, text, AW_BACKLOG) for peer in peers))
        await self.pass_on(project, text, "aw", "a")

    async def _p2p(self, project: str, ws, conn: Conn, msg: dict, relay_only: bool) -> None:
        # Opaque peer-to-peer payload (state-vector sync, blob transfer). Never stored.
        out = {"t": "p2p", "d": field(msg, "d", str), "from": conn.id}
        to = None if msg.get("to") is None else field(msg, "to", int)
        if "s" not in msg:
            await self._relay_unsigned(project, ws, conn, out, to)
            return
        # Signed by a writer (a change, a compile request): checked, so that a made-up signature
        # can't pass something off as signed (unpaced), nor a revoked personal link's be passed on.
        out["s"] = field(msg, "s", str)
        data, sig = b64d(out["d"]), b64d(out["s"])
        revoked = await self.store.revoked(project)
        ok = (verify(project, b"p2p:" + data, sig, revoked) if len(data) < 64 * KiB
              else await asyncio.to_thread(verify, project, b"p2p:" + data, sig, revoked))
        if not ok:
            conn.pause = max(conn.pause, conn.pace.take(len(data)))  # (checking it took work)
            return
        if to is None:
            await self.broadcast(project, out, ws)
        elif peers := self._recipients(project, ws, to):
            for peer in peers:
                await self.send(peer, json.dumps(out))  # cut off, rather than waited on, if it isn't reading
        elif (there := await self.relay_of(project, to)) is not None:
            await self.pass_on(project, json.dumps(out), "p2p", "s", to=to, relay=there)

    async def _usage(self, project: str, ws, conn: Conn, msg: dict, relay_only: bool) -> None:
        # the figures in the hello's reply, fresh (an app showing how much is used asks when it shows them)
        await ws.send_json({"t": "usage", "store": self.policy.store_status(project, conn.code)})

    async def _mode(self, project: str, ws, conn: Conn, msg: dict, relay_only: bool) -> None:
        mode, ts, sig = field(msg, "m", str), field(msg, "ts", int), b64d(field(msg, "s", str))
        # Only the owner chooses where the project is kept: turning storage off deletes the
        # server's copy, and turning it on stores the project against the owner's allowance.
        if mode not in ("server", "relay") or not self._owner_signed(project, f"mode:{mode}:{ts}", ts, sig):
            await ws.send_json({"t": "err", "code": "mode-rejected", "m": "rejected mode change"})
            return
        if not await self._may_add(project):
            await ws.send_json({"t": "err", "code": "mode-busy", "m": "server busy: try again later"})
            return
        await self.store.set_mode(project, mode == "relay")
        log.info("project %s… storage mode -> %s", project[:8], mode)
        await self.broadcast(project, {"t": "mode", "m": mode})

    async def _move(self, project: str, ws, conn: Conn, msg: dict, relay_only: bool) -> None:
        """The owner moves the project to another creation code (issue #32)."""
        code, ts, sig = field(msg, "c", str)[:64], field(msg, "ts", int), b64d(field(msg, "s", str))
        if not self._owner_signed(project, f"move:{code}:{ts}", ts, sig):
            await ws.send_json({"t": "err", "code": "move-rejected", "m": "rejected move"})
            return
        if conn.moves >= MOVE_TRIES:  # each move checks a code: not a way to try many
            await ws.send_json({"t": "err", "code": "move-busy", "m": "too many tries: reconnect and try again"})
            return
        no = await self.policy.move(project, code)
        if no:
            conn.moves += no["code"] == "move-bad-code"
            await ws.send_json({"t": "err", **no})
            return
        conn.code = self.policy.codes.check(code)  # proven to hold it: it sees the code's usage now
        await ws.send_json({"t": "moved"})
        # everyone connected: what's stored and the limits have changed (a refusal may be over)
        for peer in list(self.rooms.get(project, ())):
            c = self.conns.get(peer)
            if c is not None:
                await self.send(peer, json.dumps({"t": "usage", "store": self.policy.store_status(project, c.code), "moved": True}))
        await self.pass_on(project, "", "moved", "s")  # (the other relays tell theirs, each what it may see)

    async def _delete(self, project: str, ws, conn: Conn, msg: dict, relay_only: bool) -> None:
        """The owner deletes the project for everyone."""
        ts, sig = field(msg, "ts", int), b64d(field(msg, "s", str))
        if not self._owner_signed(project, f"delete:{ts}", ts, sig):
            await ws.send_json({"t": "err", "code": "delete-rejected", "m": "rejected deletion"})
            return
        if not await self._may_add(project, logged=True):
            await ws.send_json({"t": "err", "code": "mode-busy", "m": "server busy: try again later"})
            return
        gone = await self.store.delete_project(project, sig, ts)
        log.info("project %s… deleted for everyone by its owner", project[:8])
        room = list(self.rooms.get(project, ()))
        await self.broadcast(project, gone)  # including the owner
        for peer in room:  # each at once: one that isn't reading mustn't hold up the rest
            asyncio.create_task(self.close_or_cut(peer, b"deleted"))

    async def _revoke(self, project: str, ws, conn: Conn, msg: dict, relay_only: bool) -> None:
        """The owner withdraws a personal edit link."""
        pub, sig = canonical_pub(field(msg, "p", str)[:64]), b64d(field(msg, "s", str))
        if pub is None or signer(project, revoke_msg(pub), sig) != "owner":
            await ws.send_json({"t": "err", "m": "rejected revocation"})
            return
        # each new row costs: from the code's project if it has one, else from the server-wide budget
        # for rows that store nothing (a storage mode set once mustn't make the next thousand free)
        new = pub not in await self.store.revoked(project)
        if new and self.store.owner(project) is None and not self.store.meta_budget() \
                or not await self.store.revoke(project, pub, sig):
            await ws.send_json({"t": "err", "m": "rejected revocation: too many"})
            return
        log.info("project %s…: revoked a personal link", project[:8])
        await self.broadcast(project, {"t": "rv", "p": pub, "s": b64e(sig)})  # including the owner

    HANDLERS = {"hello": _hello, "up": _up, "snap": _snap, "aw": _aw, "p2p": _p2p, "usage": _usage,
                "mode": _mode, "move": _move, "delete": _delete, "revoke": _revoke}
