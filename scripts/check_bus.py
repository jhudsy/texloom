#!/usr/bin/env python3
"""The bus's contract (server/bus.py), checked against each implementation: the in-process bus, and a real
Valkey in Docker reached through the relay's own Redis-protocol client.

For each: messages reach every subscriber (the publisher's own too), in the order they were published, up to
the largest allowed, and stop after unsubscribing; a subscription is in effect when subscribe returns;
counters are exact with two instances adding at once, and their window doesn't move; keys and hashes expire
as asked. Then breaks, which must never lose anything silently: the in-process bus broken by hand; and for
Valkey, its subscriptions' connection killed, a subscriber too slow to keep up (Valkey drops it), the server
restarted, and a network that passes nothing (the server paused). After each, the subscriptions are in effect
again and the relay is told (on_break), and requests made while it was away raised BusError.

Valkey is used as the relays would: as a user allowed only bus.COMMANDS, on TeXLoom's prefix. A wrong
password, and a replica (which can't be written to), are refused with a reason.

    uv run --extra server python scripts/check_bus.py            # Valkey too if Docker is running
    uv run --extra server python scripts/check_bus.py --valkey   # Valkey must be checked (CI)
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from checklib import check, summary, until  # noqa: E402
from pg import free_port  # noqa: E402

from server.bus import COMMANDS, MAX_MESSAGE, Bus, BusError, Hub, MemoryBus, RespBus, open_bus  # noqa: E402

VALKEY = "valkey/valkey:8.1.4"
PASSWORD = "check-bus-password"
NAME = "texloom-check-bus"


class Ear:
    """A bus's callbacks, recorded."""

    def __init__(self) -> None:
        self.got: list[tuple[str, bytes]] = []
        self.breaks = 0

    def message(self, channel: str, data: bytes) -> None:
        self.got.append((channel, data))

    def broke(self) -> None:
        self.breaks += 1

    def on(self, channel: str) -> list[bytes]:
        return [d for c, d in self.got if c == channel]


async def started(bus: Bus) -> Ear:
    ear = Ear()
    await bus.start(ear.message, ear.broke)
    return ear


async def raises(coro) -> bool:
    try:
        await coro
    except BusError:
        return True
    return False


async def contract(kind: str, make: Callable[[], Bus]) -> None:
    """What every implementation must do. Two instances, as two relays."""
    print(f"\n== {kind}: the contract")
    a, b = make(), make()
    ea, eb = await started(a), await started(b)

    await a.subscribe("p:one")
    await b.subscribe("p:one")
    await b.publish("p:one", b"hello")
    check("a message reaches every subscriber, the publisher's own subscription too",
          await until(lambda: ea.on("p:one") == [b"hello"] and eb.on("p:one") == [b"hello"], 5), (ea.got, eb.got))

    n = 2000
    await asyncio.gather(*[b.publish("p:one", str(i).encode()) for i in range(n)])
    check(f"{n} messages published at once arrive once each, in the order published",
          await until(lambda: len(ea.on("p:one")) == n + 1, 10) and ea.on("p:one")[1:] == [str(i).encode() for i in range(n)],
          len(ea.on("p:one")))

    big = bytes(range(256)) * (4 * 1024 * 1024 // 256)
    await b.publish("p:one", big)
    check("a 4 MiB message arrives whole", await until(lambda: ea.on("p:one")[-1:] == [big], 10))
    check("one larger than MAX_MESSAGE is refused", await raises(b.publish("p:one", b"x" * (MAX_MESSAGE + 1))))

    await a.unsubscribe("p:one")
    before = len(ea.on("p:one"))
    await b.publish("p:one", b"after")
    await until(lambda: eb.on("p:one")[-1:] == [b"after"], 5)
    await asyncio.sleep(0.2)
    check("after unsubscribing nothing more arrives", len(ea.on("p:one")) == before)

    chans = [f"p:x{i}" for i in range(100)]
    for ch in chans:
        await a.subscribe(ch)
        await b.publish(ch, ch.encode())
    check("a subscription is in effect when subscribe returns (100 at once, each published to at once)",
          await until(lambda: all(ea.on(ch) == [ch.encode()] for ch in chans), 5),
          sum(1 for ch in chans if ea.on(ch) == [ch.encode()]))
    await b.publish("p:nobody", b"x")  # (no subscribers: nothing happens)

    # counters
    await a.delete("count")
    got = await asyncio.gather(*[(a if i % 2 else b).incr("count") for i in range(500)])
    check("a counter added to by two instances at once is exact, and each gets a different value",
          await a.get("count") == b"500" and sorted(got) == list(range(1, 501)), await a.get("count"))
    await a.incr("window", 1, ttl=1.0)
    await asyncio.sleep(0.6)
    second = await b.incr("window", 1, ttl=1.0)
    await asyncio.sleep(0.6)
    check("a counter's window starts when it is created and isn't moved by adding to it",
          second == 2 and await a.get("window") is None, (second, await a.get("window")))
    check("by a given amount", await a.incr("count", 10) == 510 and await a.incr("count", -10) == 500)

    # keys and hashes
    await a.set("k", b"\x00binary\xff", ttl=0.5)
    check("a key set by one instance is read by another", await b.get("k") == b"\x00binary\xff")
    await asyncio.sleep(0.7)
    check("and is gone after its time", await b.get("k") is None)
    await a.set("k2", b"v", ttl=30)
    await b.delete("k2", "not-there")
    check("deleted (and deleting a key that isn't there is fine)", await a.get("k2") is None)

    await a.hset("conns:p", "1", b"relay-a")
    await b.hset("conns:p", "2", b"relay-b")
    check("a hash written by two instances holds both", await a.hgetall("conns:p") == {"1": b"relay-a", "2": b"relay-b"}
          and await b.hlen("conns:p") == 2)
    await a.hdel("conns:p", "1", "9")
    check("a field removed", await b.hgetall("conns:p") == {"2": b"relay-b"})
    await a.hdel("conns:p", "2")
    check("and the hash, with its last field", await b.hlen("conns:p") == 0 and await b.hgetall("conns:p") == {})
    await a.hset("alive", "a", b"1", ttl=0.6)
    await asyncio.sleep(0.4)
    await b.hset("alive", "b", b"1", ttl=0.6)
    await asyncio.sleep(0.4)
    check("setting a field with a time renews the whole hash's", await a.hgetall("alive") == {"a": b"1", "b": b"1"})
    await asyncio.sleep(0.4)
    check("which then runs out", await a.hgetall("alive") == {})
    check("nothing so far was a break", ea.breaks == 0 and eb.breaks == 0, (ea.breaks, eb.breaks))
    await a.close()
    await b.close()


async def memory_breaks() -> None:
    print("\n== in-process: a break (by hand)")
    hub = Hub()
    a, b = MemoryBus(hub), MemoryBus(hub)
    ea, _ = await started(a), await started(b)
    await a.subscribe("p:1")
    a.simulate_break()
    await b.publish("p:1", b"missed")
    check("while broken, requests raise BusError, and messages are missed", await raises(a.get("x"))
          and await raises(a.publish("p:1", b"x")))
    a.heal()
    await b.publish("p:1", b"after")
    check("once back, the relay is told, and its subscriptions are in effect again",
          ea.breaks == 1 and await until(lambda: ea.on("p:1") == [b"after"], 2), (ea.breaks, ea.got))


def docker(*args: str, check_: bool = True) -> str:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if check_ and r.returncode:
        raise RuntimeError(f"docker {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def cli(container: str, *args: str) -> str:
    return docker("exec", container, "valkey-cli", "--user", "admin", "--pass", PASSWORD, "--no-auth-warning", *args)


def docker_ready() -> bool:
    return bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@contextlib.contextmanager
def valkey(name: str, port: int, *extra: str):
    """A Valkey on 127.0.0.1:port: an admin user for the check, and `relay`, allowed only what the relays send."""
    users = [f"admin on >{PASSWORD} ~* &* +@all",
             f"relay on >{PASSWORD} resetkeys ~texloom:* resetchannels &texloom:* -@all " + " ".join(f"+{c}" for c in COMMANDS),
             "default off"]
    acl = [w for u in users for w in ("--user", *u.split())]  # (each word its own argument, as in a config file)
    docker("rm", "-f", name, check_=False)
    docker("network", "create", NAME, check_=False)  # (the replica reaches the primary by name on it)
    docker("run", "-d", "--rm", "--name", name, "--network", NAME, "-p", f"127.0.0.1:{port}:6379", VALKEY, "valkey-server",
           "--save", "", "--appendonly", "no", *acl, *extra)
    try:
        for _ in range(100):
            if subprocess.run(["docker", "exec", name, "valkey-cli", "--user", "admin", "--pass", PASSWORD, "--no-auth-warning",
                               "ping"], capture_output=True, text=True).stdout.strip() == "PONG":
                break
            time.sleep(0.1)
        yield
    finally:
        docker("rm", "-f", name, check_=False)
        if name == NAME:
            docker("network", "rm", NAME, check_=False)


async def valkey_breaks(url: str) -> None:
    print("\n== Valkey: breaks")
    a, b = RespBus(url), RespBus(url)
    ea, _ = await started(a), await started(b)
    await a.subscribe("p:1")
    await a.subscribe("p:2")

    cli(NAME, "client", "kill", "type", "pubsub")
    check("its subscriptions' connection killed: once back the relay is told", await until(lambda: ea.breaks == 1, 10), ea.breaks)
    await b.publish("p:1", b"one")
    await b.publish("p:2", b"two")
    check("... with every subscription in effect again", await until(lambda: ea.on("p:1") == [b"one"] and ea.on("p:2") == [b"two"], 5),
          ea.got)

    # too slow: Valkey drops a subscriber whose unread messages pass its limit (set low for this: 1 MB; Valkey's
    # own, 32 MB at once or 8 MB for a minute, is well above the largest message)
    cli(NAME, "config", "set", "client-output-buffer-limit", "pubsub 1mb 512kb 5")
    sub = a._sub
    assert sub is not None
    sub.w.transport.pause_reading()  # (as if the relay were too busy to read)
    for _ in range(12):
        await b.publish("p:1", b"y" * 256 * 1024)
    await asyncio.sleep(0.5)
    sub.w.transport.resume_reading()
    check("a subscriber too slow to keep up is dropped by Valkey, and that is a break, not a silent loss",
          await until(lambda: ea.breaks == 2, 15), ea.breaks)
    await b.publish("p:1", b"after slow")
    check("... after which messages flow again", await until(lambda: ea.on("p:1")[-1:] == [b"after slow"], 5))
    cli(NAME, "config", "set", "client-output-buffer-limit", "pubsub 32mb 8mb 60")

    docker("restart", "-t", "0", NAME)
    await asyncio.sleep(0.3)
    away = await raises(b.get("x"))
    check("a request while the server restarts raises BusError (or it was already back)", away or await b.get("x") is None)
    check("once it is back the relay is told", await until(lambda: ea.breaks == 3, 30), ea.breaks)
    ok = False
    for _ in range(50):  # (the requests' connection is made again by the next request)
        with contextlib.suppress(BusError):
            await b.publish("p:2", b"back")
            ok = True
            break
        await asyncio.sleep(0.2)
    check("and requests work again, the subscriptions in effect", ok and await until(lambda: ea.on("p:2")[-1:] == [b"back"], 5))

    docker("pause", NAME)
    t0 = time.monotonic()
    check("a server that answers nothing (paused): requests raise BusError in time",
          await raises(b.get("x")) and time.monotonic() - t0 < 8, round(time.monotonic() - t0, 1))
    await asyncio.sleep(12)  # (more than a ping's interval and its wait)
    docker("unpause", NAME)
    check("and the silence is noticed as a break (a ping unanswered); once it answers, the relay is told",
          await until(lambda: ea.breaks == 4, 30), ea.breaks)
    await b.publish("p:1", b"unpaused")
    check("... and messages flow again", await until(lambda: ea.on("p:1")[-1:] == [b"unpaused"], 10))
    await a.close()
    await b.close()


async def valkey_refusals(port: int) -> None:
    print("\n== Valkey: what is refused")
    bad = RespBus(f"redis://relay:wrong@127.0.0.1:{port}")
    try:
        await bad.start(lambda c, d: None, lambda: None)
        why = ""
    except BusError as e:
        why = str(e)
    check("a wrong password: refused at start, saying so", "refused" in why, why)
    await bad.close()

    raw = RespBus(f"redis://admin:{PASSWORD}@127.0.0.1:{port}")
    await raw.start(lambda c, d: None, lambda: None)
    await raw.set("x", b"1", 30)
    keys = cli(NAME, "keys", "*").split()
    check("keys go under the prefix", keys == ["texloom:x"], keys)
    await raw.close()

    replica_port = free_port()
    with valkey(NAME + "-replica", replica_port, "--replicaof", NAME, "6379", "--masteruser", "admin",
                "--masterauth", PASSWORD):
        r = RespBus(f"redis://relay:{PASSWORD}@127.0.0.1:{replica_port}")
        try:
            await r.start(lambda c, d: None, lambda: None)
            why = ""
        except BusError as e:
            why = str(e)
        check("a replica: refused at start (it can't be written to)", "not a primary" in why, why)
        await r.close()


def addresses() -> None:
    print("\n== addresses")
    check("nothing, or memory:, is the in-process bus", isinstance(open_bus(""), MemoryBus) and isinstance(open_bus("memory:"), MemoryBus))
    r = open_bus("redis+sentinel://u:p%40ss@s1,s2:26380,s3/texloom-bus?prefix=tl:")
    check("a Sentinel address: its hosts (default port 26379), the primary's name, the prefix, the password",
          isinstance(r, RespBus) and r.hosts == [("s1", 26379), ("s2", 26380), ("s3", 26379)] and r.primary == "texloom-bus"
          and r.prefix == "tl:" and r.password == "p@ss" and r.user == "u", vars(r) if isinstance(r, RespBus) else r)
    for wrong in ("http://x", "redis+sentinel://s1,s2", "postgresql://db"):
        try:
            open_bus(wrong)
            refused = False
        except ValueError:
            refused = True
        check(f"{wrong!r} is refused", refused)


async def main() -> int:
    addresses()
    hub = Hub()
    await contract("in-process", lambda: MemoryBus(hub))
    await memory_breaks()
    if not docker_ready():
        if "--valkey" in sys.argv:
            check("Docker is running (for Valkey)", False)
        else:
            print("\nskipped Valkey: Docker isn't running")
        return summary("bus")
    port = free_port()
    print(f"\n(starting {VALKEY})")
    with valkey(NAME, port):
        url = f"redis://relay:{PASSWORD}@127.0.0.1:{port}"
        await contract("Valkey", lambda: RespBus(url))
        await valkey_breaks(url)
        await valkey_refusals(port)
    return summary("bus")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
