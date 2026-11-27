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

from .policy import KiB, MiB, Policy
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

    @property
    def store(self) -> Store:
        return self.policy.store

    def has_peers(self, project: str) -> bool:
        """Someone has the project open here."""
        return bool(self.rooms.get(project))

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
        if not canonical_id(project):
            raise web.HTTPBadRequest(text="bad project id")
        if len(self.conns) >= MAX_CONNECTIONS:
            raise web.HTTPServiceUnavailable(text="too many connections")
        address = client_address(request)
        if address is not None and self.per_address[address] >= MAX_PER_ADDRESS:
            raise web.HTTPTooManyRequests(text="too many connections from this address")
        if self.per_project[project] >= MAX_PER_PROJECT:
            raise web.HTTPServiceUnavailable(text="too many connections to this project")
        # no compression: what's sent is ciphertext, and compressed frames would let a client make
        # the server hold far more than it sent (Metered counts bytes as they arrive)
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_FRAME, compress=False)
        conn = Conn(self._next_id, address)
        self._next_id += 1
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
            await self.handle(project, ws, conn, "hello", first)
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
                    break
                except (BadMessage, ValueError, TypeError, KeyError, OverflowError, RecursionError) as e:  # (binascii.Error is a ValueError)
                    await ws.send_json({"t": "err", "m": f"bad message: {e}"[:200]})
                except Exception:
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
            if conn.aware and not self.stopping:  # this also covers a crash or a lost connection, where a client can't say goodbye
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
                 "mode": "relay" if relay_only else "server", "peers": len(self.rooms[project]) - 1, "ka": KEEPALIVE,
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
        if relay_only:  # pass it on, but keep nothing
            await ws.send_json({"t": "ack", "q": 0, "id": msg.get("id")})
            await self.broadcast(project, {"t": "up", "q": 0, "d": d, "s": s}, ws)
            return
        # one transaction holding the project: is it a replay, may it be stored, store it
        async with self.store.locked(project) as c:
            dup = await self.store.duplicate(project, data, c)
            refused = None if dup is not None else await self.policy.may_store(project, len(data) + len(sig), conn.code, c)
            seq = None
            if dup is None and not refused:
                seq = await self.store.append(project, data, sig, c)
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
        if not peers:
            return
        text = json.dumps(out)
        n = len(text) * len(peers)
        room = self.room_pace.get(project) or Pace(ROOM_RATE, ROOM_BURST)
        if len(text) <= SMALL and room.wait(n) > 0:
            return  # the room is busy: something small is dropped rather than waited for
        if len(text) > MiB and self.out_waiting() + n > OUT_MAX:
            return  # the server holds a lot unsent already
        await asyncio.gather(*(self.send_unsigned(peer, text, UNSIGNED_BACKLOG) for peer in peers))
        conn.pause = max(conn.pause, conn.pace.take(n), room.take(n))

    async def _aw(self, project: str, ws, conn: Conn, msg: dict, relay_only: bool) -> None:
        conn.aware = True
        text = json.dumps({"t": "aw", "d": field(msg, "d", str), "from": conn.id})
        peers = self._recipients(project, ws, None)
        room = self.room_pace.get(project) or Pace(ROOM_RATE, ROOM_BURST)
        n = len(text) * len(peers)
        # presence is sent often and superseded by the next: past its pace, or its room's, dropped
        if not peers or conn.aw_pace.wait(len(text)) > 0 or room.wait(n) > 0:
            return
        conn.aw_pace.take(len(text))
        room.take(n)
        await asyncio.gather(*(self.send_unsigned(peer, text, AW_BACKLOG) for peer in peers))

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
        else:
            for peer in self._recipients(project, ws, to):
                await self.send(peer, json.dumps(out))  # cut off, rather than waited on, if it isn't reading

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
