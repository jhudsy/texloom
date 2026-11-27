"""The bus: live messages between relays, and soft state they share (FUTURE_ARCHITECTURE.md).

One relay alone needs none of this: the in-process bus (MemoryBus) passes messages to its own
subscribers. Several relays share a Redis-compatible server (Valkey, Redis, or a managed one),
chosen by the `bus` setting's address:

    ""  or  memory:                                in-process (the default)
    redis://[user:password@]host[:port][?prefix=texloom:]
    rediss://…                                     the same over TLS
    redis+sentinel://[user:password@]s1:26379,s2:26379/<primary name>[?prefix=…]
                                                   ask the Sentinels which server is the primary

The relay speaks the Redis protocol itself (RespBus, below), as files.py speaks S3: a small part of
it, so that a broken connection is always seen and never quietly mended (the contract's point 3).

The contract. Anything used as the bus must keep it; scripts/check_bus.py checks each implementation.

1. publish(channel, data) delivers `data` to every subscriber to `channel` at that moment, on any
   instance sharing the bus, the publisher's own subscription included (a relay ignores what it sent
   itself), at most once. Messages may be up to MAX_MESSAGE bytes.
2. Messages one instance publishes reach each subscriber in the order publish was called.
3. A message is lost only around a break in a subscriber's connection, never silently: once the
   connection is back and every subscription is in effect again, on_break() is called, so the relay
   can find what it missed (gap filling, `resync`). While the bus can't be reached, calls raise
   BusError; a publish that raised may or may not have been delivered.
4. subscribe(channel) returns once the subscription is in effect: anything published after that
   reaches it. A subscription asked for while the bus is away raises BusError, but is made when the
   connection is back (and on_break then covers it), until unsubscribed.
5. Soft state: keys with an expiry (set, get, delete), counters (incr: atomic across instances; its
   expiry is set only when the key is created, so it counts over a fixed window), and hashes (hset,
   hget, hdel, hgetall, hlen; hset with a ttl renews the whole hash's expiry). Soft state may be lost
   (a Redis failover can drop recent writes): nothing whose loss matters is kept here.
6. Channels and keys are names of TeXLoom's own: the implementation puts them under a prefix
   ("texloom:" unless the address says otherwise), so that a Redis access list can confine the
   relays to it (see INSTALL.md: a user allowed only the commands in COMMANDS, on the prefix).

on_message(channel, data) and on_break() are plain functions called in the event loop: they must
not block (hand anything slow to a task).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import ssl
import time
from collections.abc import Callable
from urllib.parse import parse_qs, unquote, urlsplit

log = logging.getLogger("texloom.bus")

MAX_MESSAGE = 8 * 1024 * 1024  # bytes a message may hold (peer-to-peer pieces are up to 4 MiB, before base64)
TIMEOUT = 5.0  # seconds a request may take before the connection is taken as broken
PING_EVERY = 5.0  # seconds between pings on the subscriptions' connection, to notice a silent network
RETRY_MIN, RETRY_MAX = 0.2, 5.0  # seconds between tries to connect again (doubling)
MIN_VERSION = (7, 0)  # EXPIRE … NX (incr's window)
# What the relays send: an access list allowing only these (on the prefix) is enough
COMMANDS = ("hello", "ping", "publish", "subscribe", "unsubscribe", "get", "set", "del", "incrby", "pexpire",
            "hset", "hget", "hdel", "hgetall", "hlen", "multi", "exec")

Handler = Callable[[str, bytes], None]


class BusError(Exception):
    """The bus can't be reached, or refused what was asked."""


class Bus:
    """The interface (the contract is in the module docstring)."""

    name = ""

    def __init__(self) -> None:
        self.on_message: Handler = lambda channel, data: None
        self.on_break: Callable[[], None] = lambda: None

    async def start(self, on_message: Handler, on_break: Callable[[], None]) -> None:
        """Connect, and check the bus can be used (BusError, saying why, if not)."""
        self.on_message, self.on_break = on_message, on_break

    async def publish(self, channel: str, data: bytes) -> None:
        raise NotImplementedError

    async def subscribe(self, channel: str) -> None:
        raise NotImplementedError

    async def unsubscribe(self, channel: str) -> None:
        raise NotImplementedError

    async def incr(self, key: str, by: int = 1, ttl: float | None = None) -> int:
        raise NotImplementedError

    async def set(self, key: str, value: bytes, ttl: float) -> None:
        raise NotImplementedError

    async def get(self, key: str) -> bytes | None:
        raise NotImplementedError

    async def delete(self, *keys: str) -> None:
        raise NotImplementedError

    async def hset(self, key: str, field: str, value: bytes, ttl: float | None = None) -> None:
        raise NotImplementedError

    async def hget(self, key: str, field: str) -> bytes | None:
        raise NotImplementedError

    async def hdel(self, key: str, *fields: str) -> None:
        raise NotImplementedError

    async def hgetall(self, key: str) -> dict[str, bytes]:
        raise NotImplementedError

    async def hlen(self, key: str) -> int:
        raise NotImplementedError

    async def close(self) -> None:
        pass


def open_bus(url: str) -> Bus:
    """The bus an address names (the module docstring lists them). ValueError for one that isn't."""
    if url in ("", "memory:"):
        return MemoryBus()
    scheme = urlsplit(url).scheme
    if scheme in ("redis", "rediss", "redis+sentinel"):
        return RespBus(url)
    raise ValueError(f"not a bus address: {url!r} (memory:, redis://, rediss:// or redis+sentinel://)")


# --- in-process -------------------------------------------------------------------------------

class Hub:
    """What MemoryBus instances sharing it have in common: subscribers and soft state. One per relay
    normally; checks give several relays in one process the same one."""

    def __init__(self) -> None:
        self.subs: dict[str, set[MemoryBus]] = collections.defaultdict(set)
        self.keys: dict[str, tuple[object, float | None]] = {}  # key -> (bytes, or a dict for a hash; expires at)

    def _live(self, key: str):
        v = self.keys.get(key)
        if v is None:
            return None
        if v[1] is not None and v[1] <= time.monotonic():
            del self.keys[key]
            return None
        return v


class MemoryBus(Bus):
    """In this process. It never loses anything, except when a check breaks it (simulate_break)."""

    name = "in-process"

    def __init__(self, hub: Hub | None = None):
        super().__init__()
        self.hub = hub or Hub()
        self.channels: set[str] = set()
        self.broken = False

    def _ok(self) -> None:
        if self.broken:
            raise BusError("the bus is away (a check broke it)")

    async def publish(self, channel: str, data: bytes) -> None:
        self._ok()
        if len(data) > MAX_MESSAGE:
            raise BusError(f"a message may be {MAX_MESSAGE} bytes at most")
        loop = asyncio.get_running_loop()
        for sub in list(self.hub.subs.get(channel, ())):
            if not sub.broken:  # (one that is broken misses it, as over a network)
                loop.call_soon(sub._deliver, channel, bytes(data))

    def _deliver(self, channel: str, data: bytes) -> None:
        if channel in self.channels and not self.broken:
            self.on_message(channel, data)

    async def subscribe(self, channel: str) -> None:
        self.channels.add(channel)
        self._ok()
        self.hub.subs[channel].add(self)

    async def unsubscribe(self, channel: str) -> None:
        self.channels.discard(channel)
        subs = self.hub.subs.get(channel)
        if subs is not None:
            subs.discard(self)
            if not subs:
                del self.hub.subs[channel]

    async def incr(self, key: str, by: int = 1, ttl: float | None = None) -> int:
        self._ok()
        v = self.hub._live(key)
        n = (int(v[0]) if v else 0) + by  # type: ignore[call-overload]
        self.hub.keys[key] = (str(n).encode(), v[1] if v else (time.monotonic() + ttl if ttl else None))
        return n

    async def set(self, key: str, value: bytes, ttl: float) -> None:
        self._ok()
        self.hub.keys[key] = (bytes(value), time.monotonic() + ttl)

    async def get(self, key: str) -> bytes | None:
        self._ok()
        v = self.hub._live(key)
        return v[0] if v and isinstance(v[0], bytes) else None

    async def delete(self, *keys: str) -> None:
        self._ok()
        for k in keys:
            self.hub.keys.pop(k, None)

    async def hset(self, key: str, field: str, value: bytes, ttl: float | None = None) -> None:
        self._ok()
        v = self.hub._live(key)
        h = v[0] if v and isinstance(v[0], dict) else {}
        h[field] = bytes(value)
        self.hub.keys[key] = (h, time.monotonic() + ttl if ttl else (v[1] if v else None))

    async def hget(self, key: str, field: str) -> bytes | None:
        return (await self.hgetall(key)).get(field)

    async def hdel(self, key: str, *fields: str) -> None:
        self._ok()
        v = self.hub._live(key)
        if v and isinstance(v[0], dict):
            for f in fields:
                v[0].pop(f, None)
            if not v[0]:
                del self.hub.keys[key]

    async def hgetall(self, key: str) -> dict[str, bytes]:
        self._ok()
        v = self.hub._live(key)
        return dict(v[0]) if v and isinstance(v[0], dict) else {}

    async def hlen(self, key: str) -> int:
        return len(await self.hgetall(key))

    async def close(self) -> None:
        for ch in list(self.channels):
            await self.unsubscribe(ch)

    # for checks: a break in this instance's connection, as a network or a Redis failover would make one
    def simulate_break(self) -> None:
        self.broken = True
        for ch in self.channels:
            self.hub.subs.get(ch, set()).discard(self)

    def heal(self) -> None:
        self.broken = False
        for ch in self.channels:
            self.hub.subs[ch].add(self)
        self.on_break()


# --- the Redis protocol (Valkey, Redis) ---------------------------------------------------------

class RespError(Exception):
    """An error reply (-ERR …) to one request: the connection itself is fine."""


def _encode(args: tuple) -> bytes:
    out = [b"*%d\r\n" % len(args)]
    for a in args:
        b = a if isinstance(a, (bytes, bytearray, memoryview)) else str(a).encode()
        out += [b"$%d\r\n" % len(b), bytes(b), b"\r\n"]
    return b"".join(out)


async def _read(r: asyncio.StreamReader):
    line = await r.readuntil(b"\r\n")
    t, rest = line[:1], line[1:-2]
    if t == b"+":
        return rest.decode()
    if t == b"-":
        return RespError(rest.decode())
    if t == b":":
        return int(rest)
    if t == b"$":
        n = int(rest)
        return None if n < 0 else (await r.readexactly(n + 2))[:-2]
    if t == b"*":
        n = int(rest)
        return None if n < 0 else [await _read(r) for _ in range(n)]
    raise ConnectionError(f"not the Redis protocol: {line[:40]!r}")


class _Conn:
    """One connection: requests are written in order and their replies taken in order (pipelined)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.r, self.w = reader, writer
        self.waiting: collections.deque[asyncio.Future] = collections.deque()

    @classmethod
    async def open(cls, host: str, port: int, tls: ssl.SSLContext | None, user: str | None, password: str | None,
                   ) -> tuple[_Conn, dict]:
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=tls), TIMEOUT)
        except (OSError, TimeoutError) as e:
            raise BusError(f"can't reach the bus at {host}:{port}: {e}") from None
        c = cls(r, w)
        hello: tuple = ("HELLO", 2) + (("AUTH", user or "default", password) if password else ())
        try:
            w.write(_encode(hello))
            reply = await asyncio.wait_for(_read(r), TIMEOUT)
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ConnectionError) as e:
            w.close()
            raise BusError(f"the bus at {host}:{port} didn't answer: {e}") from None
        if isinstance(reply, RespError):
            w.close()
            raise BusError(f"the bus at {host}:{port} refused us: {reply}")
        info = {reply[i].decode() if isinstance(reply[i], bytes) else reply[i]: reply[i + 1] for i in range(0, len(reply), 2)}
        return c, info

    def send(self, *commands: tuple) -> list[asyncio.Future]:
        loop = asyncio.get_running_loop()
        futures = [loop.create_future() for _ in commands]
        self.waiting.extend(futures)
        self.w.write(b"".join(_encode(c) for c in commands))
        return futures

    def close(self) -> None:
        self.w.close()


class RespBus(Bus):
    """A Redis-compatible server: requests on one connection (so what one relay publishes goes out in
    order), subscriptions on another."""

    def __init__(self, url: str):
        super().__init__()
        u = urlsplit(url)
        self.url = url
        self.sentinel = u.scheme == "redis+sentinel"
        self.tls = ssl.create_default_context() if u.scheme == "rediss" else None
        self.user = unquote(u.username) if u.username else None
        self.password = unquote(u.password) if u.password else None
        hosts = u.netloc.rsplit("@", 1)[-1].split(",")
        default = 26379 if self.sentinel else 6379
        self.hosts = [(h.rsplit(":", 1)[0], int(h.rsplit(":", 1)[1])) if ":" in h else (h, default) for h in hosts if h]
        self.primary = u.path.strip("/") if self.sentinel else ""
        if self.sentinel and not self.primary:
            raise ValueError("a redis+sentinel:// address names the primary after the hosts: redis+sentinel://s1,s2,s3/<name>")
        self.prefix = parse_qs(u.query).get("prefix", ["texloom:"])[0]
        self.name = f"{u.scheme}://{','.join(f'{h}:{p}' for h, p in self.hosts)}{'/' + self.primary if self.primary else ''}"
        self._cmd: _Conn | None = None
        self._cmd_reader: asyncio.Task | None = None
        self._connecting: asyncio.Lock | None = None
        self._sub: _Conn | None = None
        self._sub_task: asyncio.Task | None = None
        self.channels: set[str] = set()
        self._confirm: dict[str, list[asyncio.Future]] = collections.defaultdict(list)  # channel -> subscribes waiting
        self._pongs: collections.deque[asyncio.Future] = collections.deque()
        self._started = asyncio.Event()
        self._closed = False

    # where the primary is
    async def _address(self) -> tuple[str, int]:
        if not self.sentinel:
            return self.hosts[0]
        problems = []
        for host, port in self.hosts:
            try:
                c, _ = await _Conn.open(host, port, None, None, None)
            except BusError as e:
                problems.append(str(e))
                continue
            try:
                c.w.write(_encode(("SENTINEL", "get-master-addr-by-name", self.primary)))
                reply = await asyncio.wait_for(_read(c.r), TIMEOUT)
            except (OSError, TimeoutError, asyncio.IncompleteReadError, ConnectionError) as e:
                problems.append(f"{host}:{port}: {e}")
                continue
            finally:
                c.close()
            if isinstance(reply, list) and len(reply) == 2:
                return reply[0].decode(), int(reply[1])
            problems.append(f"{host}:{port} doesn't know {self.primary!r}")
        raise BusError("no Sentinel said where the primary is: " + "; ".join(problems))

    async def _open(self) -> _Conn:
        host, port = await self._address()
        c, info = await _Conn.open(host, port, self.tls, self.user, self.password)
        role = info.get("role", b"")
        role = role.decode() if isinstance(role, bytes) else role
        if role not in ("master", "primary"):
            c.close()
            raise BusError(f"the bus at {host}:{port} is a {role or 'server of unknown role'}, not a primary (it can't be written to)")
        version = info.get("version", b"0")
        version = version.decode() if isinstance(version, bytes) else str(version)
        if tuple(int(x) for x in version.split(".")[:2]) < MIN_VERSION:
            c.close()
            raise BusError(f"the bus at {host}:{port} is version {version}: {'.'.join(map(str, MIN_VERSION))} or later is needed")
        return c

    async def start(self, on_message: Handler, on_break: Callable[[], None]) -> None:
        await super().start(on_message, on_break)
        self._connecting = asyncio.Lock()
        await self._command_conn()  # fails here, saying why, if the bus can't be used
        self._sub_task = asyncio.get_running_loop().create_task(self._subscriptions())
        await self._started.wait()

    # --- requests -------------------------------------------------------------------------
    async def _command_conn(self) -> _Conn:
        if self._closed:
            raise BusError("the bus is closed")
        if self._cmd is not None:
            return self._cmd
        assert self._connecting is not None, "start() first"
        async with self._connecting:
            if self._cmd is None:
                c = await self._open()
                self._cmd = c
                self._cmd_reader = asyncio.get_running_loop().create_task(self._replies(c))
            return self._cmd

    async def _replies(self, c: _Conn) -> None:
        try:
            while True:
                reply = await _read(c.r)
                if c.waiting:
                    f = c.waiting.popleft()
                    if not f.done():
                        f.set_result(reply)
        except (OSError, asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._drop(c)

    def _drop(self, c: _Conn) -> None:
        if self._cmd is c:
            self._cmd = None
        c.close()
        while c.waiting:
            f = c.waiting.popleft()
            if not f.done():
                f.set_exception(BusError("lost the connection to the bus"))

    async def _call(self, *commands: tuple):
        """Send requests together and return their replies (one reply for one request)."""
        c = await self._command_conn()
        futures = c.send(*commands)
        try:
            replies = await asyncio.wait_for(asyncio.gather(*futures), TIMEOUT)
        except TimeoutError:
            self._drop(c)  # (replies would come out of step)
            raise BusError("the bus didn't answer in time") from None
        for r in replies:
            if isinstance(r, RespError):
                raise BusError(f"the bus refused a request: {r}")
        return replies[0] if len(replies) == 1 else replies

    async def _transaction(self, *commands: tuple) -> list:
        replies = await self._call(("MULTI",), *commands, ("EXEC",))
        done = replies[-1]
        if not isinstance(done, list):
            raise BusError("a transaction on the bus didn't run")
        for r in done:
            if isinstance(r, RespError):
                raise BusError(f"the bus refused a request: {r}")
        return done

    def _k(self, key: str) -> str:
        return self.prefix + key

    @staticmethod
    def _ms(ttl: float) -> int:
        return max(1, int(ttl * 1000))

    async def publish(self, channel: str, data: bytes) -> None:
        if len(data) > MAX_MESSAGE:
            raise BusError(f"a message may be {MAX_MESSAGE} bytes at most")
        await self._call(("PUBLISH", self._k(channel), data))

    async def incr(self, key: str, by: int = 1, ttl: float | None = None) -> int:
        if ttl is None:
            return int(await self._call(("INCRBY", self._k(key), by)))
        n, _ = await self._transaction(("INCRBY", self._k(key), by), ("PEXPIRE", self._k(key), self._ms(ttl), "NX"))
        return int(n)

    async def set(self, key: str, value: bytes, ttl: float) -> None:
        await self._call(("SET", self._k(key), value, "PX", self._ms(ttl)))

    async def get(self, key: str) -> bytes | None:
        return await self._call(("GET", self._k(key)))

    async def delete(self, *keys: str) -> None:
        if keys:
            await self._call(("DEL", *map(self._k, keys)))

    async def hset(self, key: str, field: str, value: bytes, ttl: float | None = None) -> None:
        if ttl is None:
            await self._call(("HSET", self._k(key), field, value))
        else:
            await self._transaction(("HSET", self._k(key), field, value), ("PEXPIRE", self._k(key), self._ms(ttl)))

    async def hget(self, key: str, field: str) -> bytes | None:
        return await self._call(("HGET", self._k(key), field))

    async def hdel(self, key: str, *fields: str) -> None:
        if fields:
            await self._call(("HDEL", self._k(key), *fields))

    async def hgetall(self, key: str) -> dict[str, bytes]:
        flat = await self._call(("HGETALL", self._k(key)))
        return {flat[i].decode(): flat[i + 1] for i in range(0, len(flat), 2)}

    async def hlen(self, key: str) -> int:
        return int(await self._call(("HLEN", self._k(key))))

    # --- subscriptions --------------------------------------------------------------------
    async def subscribe(self, channel: str) -> None:
        self.channels.add(channel)
        c = self._sub
        if c is None:
            raise BusError("the bus is away (subscribed when it is back)")
        f = asyncio.get_running_loop().create_future()
        self._confirm[channel].append(f)
        c.w.write(_encode(("SUBSCRIBE", self._k(channel))))
        try:
            await asyncio.wait_for(f, TIMEOUT)
        except TimeoutError:
            raise BusError("the bus didn't confirm a subscription in time") from None

    async def unsubscribe(self, channel: str) -> None:
        self.channels.discard(channel)
        c = self._sub
        if c is not None:  # (no need to wait: what still comes for it is dropped)
            c.w.write(_encode(("UNSUBSCRIBE", self._k(channel))))

    async def _subscriptions(self) -> None:
        """Keep the subscriptions' connection: read what comes, ping it, and after a break connect again,
        subscribe to everything again, and then tell the relay (on_break)."""
        wait, broke = RETRY_MIN, False
        while not self._closed:
            try:
                c = await self._open()
            except BusError as e:
                if not broke:
                    log.warning("the bus is away: %s", e)
                broke = True
                self._started.set()
                await asyncio.sleep(wait)
                wait = min(RETRY_MAX, wait * 2)
                continue
            wait = RETRY_MIN
            reader = asyncio.get_running_loop().create_task(self._read_subs(c))
            pinger = asyncio.get_running_loop().create_task(self._ping(c))
            try:
                if self.channels:  # again, after a break: every one in effect before on_break
                    chans = list(self.channels)
                    waits = []
                    for ch in chans:
                        f = asyncio.get_running_loop().create_future()
                        self._confirm[ch].append(f)
                        waits.append(f)
                    c.w.write(_encode(("SUBSCRIBE", *map(self._k, chans))))
                    await asyncio.wait_for(asyncio.gather(*waits), TIMEOUT * 4)
                self._sub = c
                self._started.set()
                if broke:
                    log.info("the bus is back")
                    try:
                        self.on_break()
                    except Exception:
                        log.exception("after a break in the bus")
                await asyncio.wait({reader, pinger}, return_when=asyncio.FIRST_COMPLETED)
            except (TimeoutError, BusError, OSError):
                pass
            finally:
                self._sub = None
                reader.cancel()
                pinger.cancel()
                c.close()
                for fs in self._confirm.values():
                    for f in fs:
                        if not f.done():
                            f.set_exception(BusError("lost the connection to the bus"))
                self._confirm.clear()
                self._pongs.clear()
            if not self._closed:
                broke = True
                log.warning("lost the subscriptions' connection to the bus: connecting again")

    async def _read_subs(self, c: _Conn) -> None:
        with contextlib.suppress(OSError, asyncio.IncompleteReadError, ConnectionError):
            while True:
                m = await _read(c.r)
                if m == "PONG" and self._pongs:  # (a ping's answer while subscribed to nothing: subscribed, ["pong", ""])
                    f = self._pongs.popleft()
                    if not f.done():
                        f.set_result(None)
                    continue
                if not isinstance(m, list) or not m:
                    continue
                kind = m[0].decode() if isinstance(m[0], bytes) else m[0]
                if kind == "message":
                    channel = m[1].decode()[len(self.prefix):]
                    if channel in self.channels:
                        try:
                            self.on_message(channel, m[2])
                        except Exception:
                            log.exception("handling a message from the bus")
                elif kind == "subscribe":
                    fs = self._confirm.get(m[1].decode()[len(self.prefix):])
                    if fs:
                        f = fs.pop(0)
                        if not f.done():
                            f.set_result(None)
                elif kind == "pong" and self._pongs:
                    f = self._pongs.popleft()
                    if not f.done():
                        f.set_result(None)

    async def _ping(self, c: _Conn) -> None:
        """A network that stops passing anything shows no break: a ping unanswered in time is one."""
        while True:
            await asyncio.sleep(PING_EVERY)
            f = asyncio.get_running_loop().create_future()
            self._pongs.append(f)
            c.w.write(_encode(("PING",)))
            try:
                await asyncio.wait_for(f, TIMEOUT)
            except TimeoutError:
                return

    async def close(self) -> None:
        self._closed = True
        for t in (self._sub_task, self._cmd_reader):
            if t and not t.done():
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        if self._cmd is not None:
            self._drop(self._cmd)
