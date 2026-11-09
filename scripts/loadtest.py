#!/usr/bin/env python3
"""Load test for the relay: what breaks first, and at what size.

Starts its own throwaway `texloom-server` on this machine with an empty database, and drives it
with simulated clients speaking the real protocol from several processes. It can't be pointed at
another server. Results depend heavily on the machine: for real capacity, run it on one like
the server you plan to use.

  uv run python scripts/loadtest.py all          # every scenario, about 15 minutes
  uv run python scripts/loadtest.py rooms files  # just these
  uv run python scripts/loadtest.py browser --people 30 --seconds 180
                                                 # a crowded project for a real browser to join
"""
import argparse
import asyncio
import json
import multiprocessing as mp
import os
import random
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256
from pathlib import Path

import aiohttp
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
MiB = 1 << 20
MAX_SAMPLES = 40_000  # per process and kind of latency
TOO_SLOW_MS = 200  # a step fails if the slowest 5% of edits take longer than this to arrive


def b64e(b: bytes) -> str:
    return urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s: str) -> bytes:
    return urlsafe_b64decode(s + "=" * (-len(s) % 4))


def new_project() -> dict:
    sk = Ed25519PrivateKey.generate()
    pub = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {"project": b64e(pub), "seed": sk.private_bytes_raw().hex()}


def stamped(size: int) -> bytes:
    """Opaque to the relay, like ciphertext: the send time, then random bytes."""
    return struct.pack(">d", time.monotonic()) + os.urandom(size - 8)


def stamp_of(d: str) -> float:
    return struct.unpack(">d", b64d(d[:12])[:8])[0]


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    return sorted(values)[min(len(values) - 1, int(len(values) * p))]


# ---------------------------------------------------------------------------------------
# simulated clients (one process per worker)
# ---------------------------------------------------------------------------------------
class Stats:
    def __init__(self):
        self.sent = self.delivered = self.errors = self.dropped = self.refused = self.retried = 0
        self.why: dict[str, int] = {}
        self.edit: list[float] = []
        self.ack: list[float] = []
        self.quiet: list[float] = []
        self.logs: list[float] = []  # when catch-up replies arrived
        self.joins: list[float] = []
        self.transfer = 0.0

    def sample(self, into: list[float], ms: float) -> None:
        if len(into) < MAX_SAMPLES:
            into.append(ms)
        elif random.random() < 0.05:
            into[random.randrange(MAX_SAMPLES)] = ms


async def reader(ws, spec: dict, c: dict, st: Stats, pending: dict) -> None:
    frm, to = spec["from"], spec["to"]
    async for m in ws:
        if m.type != aiohttp.WSMsgType.TEXT:
            continue
        msg = json.loads(m.data)
        kind = msg.get("t")
        now = time.monotonic()
        if kind == "up":
            sent = stamp_of(msg["d"])
            if frm <= sent < to:
                st.delivered += 1
                st.sample(st.edit, (now - sent) * 1e3)
        elif kind == "ack":
            sent = pending.pop(msg.get("id"), None)
            if sent is not None:
                st.sample(st.ack, (now - sent) * 1e3)
        elif kind == "aw" and c.get("probe"):
            sent = stamp_of(msg["d"])
            if frm <= sent < to:
                st.sample(st.quiet, (now - sent) * 1e3)
        elif kind == "err":
            st.refused += 1
    if time.monotonic() < spec["stop"]:
        st.dropped += 1


async def paced(rate: float, spec: dict, send) -> None:
    gap = 1 / rate
    due = spec["start"] + random.random() * gap
    while True:
        await asyncio.sleep(max(0, due - time.monotonic()))
        if time.monotonic() >= spec["to"]:
            return
        await send()
        due += gap
        due = max(due, time.monotonic() - 1)  # fallen behind: carry on, don't burst


async def client(http, spec: dict, c: dict, st: Stats) -> None:
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(c["seed"]))
    ws, wait = None, 0.5
    while ws is None:  # as the web app does when a connection attempt fails
        try:
            ws = await http.ws_connect(f"{spec['url']}/ws/{c['project']}", max_msg_size=32 * MiB)
            await ws.send_json({"t": "hello", "since": 0})
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            ws = None
            st.retried += 1
            st.why[type(e).__name__ + ": " + str(e)[:70]] = st.why.get(type(e).__name__ + ": " + str(e)[:70], 0) + 1
            if time.monotonic() + wait > spec["start"]:
                st.errors += 1
                return
            await asyncio.sleep(wait)
            wait *= 1.5
    pending: dict[str, float] = {}
    count = [0]

    async def edit():
        data = stamped(80)
        count[0] += 1
        sid = str(count[0])
        if spec["from"] <= time.monotonic() < spec["to"]:
            st.sent += c["others"]
            pending[sid] = time.monotonic()
        await ws.send_json({"t": "up", "id": sid, "d": b64e(data), "s": b64e(sk.sign(b"upd:" + data))})

    async def cursor():
        await ws.send_json({"t": "aw", "d": b64e(stamped(60))})

    jobs = [asyncio.create_task(reader(ws, spec, c, st, pending))]
    try:
        senders = []
        if c.get("edits"):
            senders.append(paced(c["edits"], spec, edit))
        if c.get("cursors"):
            senders.append(paced(c["cursors"], spec, cursor))
        await asyncio.gather(*senders)
        await asyncio.sleep(max(0, spec["stop"] - time.monotonic()))
    except (ConnectionError, aiohttp.ClientError, RuntimeError):
        st.errors += 1
    finally:
        await ws.close()
        for j in jobs:
            j.cancel()


async def returning(http, spec: dict, c: dict, st: Stats) -> None:
    """A client that reconnects when it's cut off, backing off as the web app does."""
    backoff = 1.0
    while time.monotonic() < spec["stop"]:
        try:
            async with http.ws_connect(f"{spec['url']}/ws/{c['project']}", max_msg_size=32 * MiB) as ws:
                await ws.send_json({"t": "hello", "since": 0})
                async for m in ws:
                    if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("t") == "log":
                        st.logs.append(time.monotonic())
                        backoff = 1.0
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            st.errors += 1
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


async def joiner(http, spec: dict, project: str, st: Stats) -> None:
    await asyncio.sleep(max(0, spec["from"] + 1 - time.monotonic()))
    t0 = time.monotonic()
    try:
        async with http.ws_connect(f"{spec['url']}/ws/{project}", max_msg_size=32 * MiB) as ws:
            await ws.send_json({"t": "hello", "since": 0})
            async for m in ws:
                if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("t") == "log":
                    st.joins.append((time.monotonic() - t0) * 1e3)
                    return
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        st.errors += 1


def blob_id() -> str:
    return os.urandom(16).hex()


async def put_blob(http, url: str, room: dict, data: bytes, st: Stats, bid: str | None = None) -> str:
    bid = bid or blob_id()
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(room["seed"]))
    async with http.put(f"{url}/blob/{room['project']}/{bid}", data=data,
                        headers={"X-Sig": b64e(sk.sign(f"blob:{bid}:".encode() + data))}) as r:
        if r.status >= 300:
            st.errors += 1
    return bid


async def transfer(http, spec: dict, st: Stats) -> None:
    job = spec["transfer"]
    body = os.urandom(job["size"])
    made = []
    if job["kind"] == "get":
        made = [await put_blob(http, spec["url"], job["room"], body, st)]
    await asyncio.sleep(max(0, spec["from"] + 1 - time.monotonic()))
    t0 = time.monotonic()

    async def get():
        async with http.get(f"{spec['url']}/blob/{job['room']['project']}/{made[0]}") as r:
            await r.read()
            if r.status >= 300:
                st.errors += 1

    if job["kind"] == "get":
        await asyncio.gather(*(get() for _ in range(job["count"])))
    else:
        await asyncio.gather(*(put_blob(http, spec["url"], job["room"], os.urandom(16) + body[16:], st) for _ in range(job["count"])))
    st.transfer = time.monotonic() - t0


async def run_clients(spec: dict) -> dict:
    st = Stats()
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0), timeout=timeout) as http:
        run = returning if spec.get("returning") else client
        jobs = [run(http, spec, c, st) for c in spec["clients"]]
        if spec.get("transfer"):
            jobs.append(transfer(http, spec, st))
        if spec.get("joiners"):
            jobs += [joiner(http, spec, spec["joiners"]["project"], st) for _ in range(spec["joiners"]["n"])]
        wall, cpu = time.monotonic(), time.process_time()
        try:
            await asyncio.wait_for(asyncio.gather(*jobs), timeout=max(1.0, spec["stop"] - time.monotonic()) + 15)
        except asyncio.TimeoutError:
            pass
        out = vars(st)
        out["busy"] = (time.process_time() - cpu) / max(0.001, time.monotonic() - wall)
    return out


def worker(spec: dict, results) -> None:
    try:
        results.put(asyncio.run(run_clients(spec)))
    except BaseException as e:  # noqa: BLE001 - the parent must always get an answer
        results.put({"crash": repr(e)})


# ---------------------------------------------------------------------------------------
# the server under test
# ---------------------------------------------------------------------------------------
class Server:
    def __init__(self, port: int, static: Path | None = None):
        self.dir = Path(tempfile.mkdtemp(prefix="texloom-loadtest-"))
        self.port, self.static = port, static
        self.db = self.dir / "t.db"
        (self.dir / "codes.txt").write_text("default unlimited\n")
        self.proc: subprocess.Popen | None = None
        self.url = f"http://127.0.0.1:{port}"

    def preload(self, projects: int) -> None:
        """Projects already stored, as many owners as a real server would have."""
        from server.relay import Store
        Store(self.db).db.close()
        db = sqlite3.connect(self.db)
        now = time.time()
        with db:
            db.executemany("INSERT INTO projects(project, created, label, owner, seen) VALUES (?,?,'',?,?)",
                           ((f"{i:043d}", now, f"code{i // 3}", now) for i in range(projects)))
            db.executemany("INSERT INTO log(project, seq, snap, data, sig, h) VALUES (?,1,0,?,?,?)",
                           ((f"{i:043d}", os.urandom(300), b"s" * 64, sha256(str(i).encode()).digest()) for i in range(projects)))
            db.executemany("INSERT INTO head(project, seq) VALUES (?,1)", ((f"{i:043d}",) for i in range(projects)))
        db.close()

    def start(self) -> float:
        """Returns how long the server took to come up."""
        exe = Path(sys.executable).parent / "texloom-server"
        args = [str(exe), "--host", "127.0.0.1", "--port", str(self.port), "--db", str(self.db),
                "--codes-file", str(self.dir / "codes.txt"), "--min-free", "0", "--project-max-bytes", "unlimited"]
        if self.static:
            args += ["--static", str(self.static)]
        t0 = time.monotonic()
        self.proc = subprocess.Popen(args, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=(self.dir / "server.log").open("ab"))
        while time.monotonic() - t0 < 120:
            try:
                urllib.request.urlopen(f"{self.url}/api/info", timeout=1).read()
                return time.monotonic() - t0
            except OSError:
                time.sleep(0.02)
        raise RuntimeError("the server didn't start: see " + str(self.dir / "server.log"))

    def stop(self) -> float:
        """Returns how long the server took to exit."""
        t0 = time.monotonic()
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(100)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        return time.monotonic() - t0

    def close(self) -> None:
        self.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def usage(self) -> tuple[float, float]:
        """CPU seconds so far, and memory in MB."""
        pid = self.proc.pid
        if Path(f"/proc/{pid}/stat").exists():
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            tick = os.sysconf("SC_CLK_TCK")
            return (int(fields[11]) + int(fields[12])) / tick, int(fields[21]) * os.sysconf("SC_PAGE_SIZE") / MiB
        out = subprocess.run(["ps", "-o", "time=", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout.split()
        secs = 0.0
        for part in out[0].split(":"):
            secs = secs * 60 + float(part)
        return secs, int(out[1]) / 1024


class Watch(threading.Thread):
    """Samples the server's CPU over the measured window, and its peak memory."""

    def __init__(self, server: Server, frm: float, to: float):
        super().__init__(daemon=True)
        self.server, self.frm, self.to = server, frm, to
        self.peak, self.cpu, self.start_mb = 0.0, float("nan"), float("nan")
        self.done = threading.Event()

    def run(self):
        at_start = None
        while not self.done.is_set():
            try:
                cpu, rss = self.server.usage()
            except (IndexError, ValueError, OSError, AttributeError):
                break
            now = time.monotonic()
            self.peak = max(self.peak, rss)
            if at_start is None and now >= self.frm:
                at_start = (cpu, now)
                self.start_mb = rss
            if at_start and now >= self.to and self.cpu != self.cpu:
                self.cpu = (cpu - at_start[0]) / (now - at_start[1]) * 100
            time.sleep(0.2)


def step(server: Server, a, label: str, clients: list[dict], warm=3.0, dur=8.0, connect=None, extra: dict | None = None) -> dict:
    """Connects `clients`, measures for `dur` seconds and returns one row of results."""
    probe_room = new_project()
    probe = [dict(probe_room, probe=True, cursors=20, others=1), dict(probe_room, probe=True, others=1)]
    connect = connect if connect is not None else 6 + len(clients) / 100
    now = time.monotonic()
    spec = {"url": server.url, "start": now + connect, "from": now + connect + warm, "to": now + connect + warm + dur}
    spec["stop"] = spec["to"] + 2.5
    n = max(1, a.workers)
    parts = [{**spec, "clients": clients[i::n]} for i in range(n)]
    parts.append({**spec, "clients": probe, **(extra or {})})
    ctx = mp.get_context("spawn")
    results = ctx.Queue()
    procs = [ctx.Process(target=worker, args=(p, results), daemon=True) for p in parts]
    watch = Watch(server, spec["from"], spec["to"])
    watch.start()
    for p in procs:
        p.start()
    out = [results.get(timeout=spec["stop"] - time.monotonic() + 40) for _ in procs]
    for p in procs:
        p.join(5)
    watch.done.set()
    watch.join(2)
    time.sleep(1)  # everyone has gone: what memory stays taken?
    after_mb = server.usage()[1]
    crashed = [o["crash"] for o in out if "crash" in o]
    if crashed:
        raise RuntimeError(crashed[0])
    pick = lambda k: [v for o in out for v in o[k]]  # noqa: E731
    total = lambda k: sum(o[k] for o in out)  # noqa: E731
    sent, delivered = total("sent"), total("delivered")
    row = {
        "step": label, "connections": len(clients), "edits_out": round(sum(c.get("edits", 0) for c in clients)),
        "deliveries_per_s": round(delivered / dur), "delivered_pct": round(100 * delivered / sent, 1) if sent else None,
        "edit_p50": percentile(pick("edit"), 0.5), "edit_p95": percentile(pick("edit"), 0.95), "edit_p99": percentile(pick("edit"), 0.99),
        "ack_p95": percentile(pick("ack"), 0.95), "quiet_p95": percentile(pick("quiet"), 0.95),
        "quiet_max": max(pick("quiet"), default=float("nan")), "cpu_pct": watch.cpu, "start_mb": watch.start_mb, "peak_mb": watch.peak, "after_mb": after_mb,
        "errors": total("errors"), "dropped": total("dropped"), "refused": total("refused"),
        "client_busy": max(o["busy"] for o in out[:-1]) if len(out) > 1 else 0,
        "joins_p95": percentile(pick("joins"), 0.95), "joins_max": max(pick("joins"), default=float("nan")),
        "transfer_s": max(o["transfer"] for o in out),
        "retried": total("retried"), "why": {k: v for o in out for k, v in o["why"].items()},
    }
    return row


def failed(r: dict) -> bool:
    bad_delivery = r["delivered_pct"] is not None and r["delivered_pct"] < 98
    return bool(r["edit_p95"] > TOO_SLOW_MS or bad_delivery or r["errors"] or r["dropped"])


COLUMNS = [("step", "step", "{}"), ("connections", "conns", "{}"), ("edits_out", "edits/s", "{}"), ("deliveries_per_s", "deliv/s", "{}"), ("delivered_pct", "deliv%", "{}"),
           ("edit_p50", "p50ms", "{:.1f}"), ("edit_p95", "p95ms", "{:.1f}"), ("edit_p99", "p99ms", "{:.1f}"), ("ack_p95", "ack95", "{:.1f}"),
           ("quiet_p95", "quiet95", "{:.1f}"), ("quiet_max", "quietmax", "{:.0f}"), ("cpu_pct", "cpu%", "{:.0f}"), ("start_mb", "MBstart", "{:.0f}"), ("peak_mb", "MBpeak", "{:.0f}"), ("after_mb", "MBafter", "{:.0f}"),
           ("retried", "retried", "{}"), ("errors", "err", "{}"), ("dropped", "drop", "{}"), ("refused", "refused", "{}"), ("client_busy", "client", "{:.2f}")]


def show(r: dict, a, extra: str = "") -> dict:
    cells = []
    for key, name, fmt in COLUMNS:
        v = r[key]
        cells.append(f"{name}=" + ("-" if v is None or v != v else fmt.format(v)))
    note = " !! TOO SLOW" if failed(r) else ""
    if r["client_busy"] > 0.8:
        note += " ?? simulated clients overloaded: not a valid reading"
    print("  " + " ".join(cells) + (" " + extra if extra else "") + note, flush=True)
    if r["why"]:
        print("    connection failures:", r["why"], flush=True)
    a.rows.append({**r, "extra": extra})
    return r


def rooms_of(rooms: int, people: int, typing: int, edits: float, cursors: float) -> list[dict]:
    out = []
    for _ in range(rooms):
        room = new_project()
        for i in range(people):
            out.append(dict(room, others=people - 1, edits=edits if i < typing else 0, cursors=cursors if i < typing else 0))
    return out


# ---------------------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------------------
def scenario(name: str):
    def wrap(fn):
        SCENARIOS[name] = fn
        return fn
    return wrap


SCENARIOS: dict = {}


@scenario("idle")
def idle(a):
    """What just being connected costs (long enough to include the 30-second heartbeat)."""
    s = Server(a.port)
    try:
        s.start()
        alone = s.usage()[1]
        print(f"  server alone: {alone:.0f} MB", flush=True)
        for n in (200, 1000, 1950):
            r = step(s, a, f"idle {n}", rooms_of(n // 3, 3, 0, 0, 0), dur=32)
            show(r, a, f"per_connection={(r['peak_mb'] - alone) * 1024 / n:.0f}KB")
    finally:
        s.close()


@scenario("rooms")
def rooms(a):
    """Many small projects: how many edits per second before typing feels slow?"""
    for n, rate in [(50, 2), (100, 2), (200, 2), (400, 2), (650, 2), (650, 4), (650, 8), (650, 16)]:
        s = Server(a.port)
        try:
            s.start()
            r = show(step(s, a, f"{n} rooms x3, {rate}/s", rooms_of(n, 3, 2, rate, 2)), a)
        finally:
            s.close()
        if failed(r):
            break


@scenario("projects")
def projects(a):
    """The same load with more projects already stored, and how long startup takes."""
    for n in (0, 1000, 10_000, 100_000):
        s = Server(a.port)
        try:
            s.preload(n)
            up = s.start()
            show(step(s, a, f"{n} stored", rooms_of(100, 3, 2, 2, 2)), a, f"startup={up:.2f}s")
        finally:
            s.close()


@scenario("one-room")
def one_room(a):
    """One project, everyone typing and moving their cursor; then mostly watching."""
    for n in (5, 10, 20, 50, 100):
        s = Server(a.port)
        try:
            s.start()
            r = show(step(s, a, f"{n} typing", rooms_of(1, n, n, 2, 2)), a)
        finally:
            s.close()
        if failed(r):
            break
    for n in (50, 200):
        s = Server(a.port)
        try:
            s.start()
            show(step(s, a, f"{n} people, 3 typing", rooms_of(1, n, 3, 2, 2)), a)
        finally:
            s.close()


@scenario("reconnect")
def reconnect(a):
    """1,000 people connected when the server restarts."""
    s = Server(a.port)
    try:
        s.start()
        clients = [c for c in rooms_of(333, 3, 0, 0, 0)][:999]
        n = max(1, a.workers)
        now = time.monotonic()
        spec = {"url": s.url, "returning": True, "start": now, "from": now, "to": now, "stop": now + 160}
        ctx = mp.get_context("spawn")
        results = ctx.Queue()
        procs = [ctx.Process(target=worker, args=({**spec, "clients": clients[i::n]}, results), daemon=True) for i in range(n)]
        for p in procs:
            p.start()
        time.sleep(12)
        t_down = time.monotonic()
        took = s.stop()
        print(f"  the old server took {took:.1f}s to exit, with everyone connected", flush=True)
        up = s.start()
        ready = time.monotonic()
        out = [results.get(timeout=240) for _ in procs]
        back = sorted(t for o in out for t in o.get("logs", []) if t > t_down)
        errors = sum(o.get("errors", 0) for o in out)
        print(f"  {len(back)} of {len(clients)} back. Server restart {up:.2f}s. Back after server ready: "
              f"p50 {percentile(back, .5) - ready:.2f}s, p95 {percentile(back, .95) - ready:.2f}s, last {back[-1] - ready if back else float('nan'):.2f}s. Failed attempts: {errors}", flush=True)
        a.rows.append({"step": "reconnect", "back": len(back), "of": len(clients), "last_s": back[-1] - ready if back else None, "failed_attempts": errors, "restart_s": up, "exit_s": took})
    finally:
        s.close()


@scenario("files")
def files(a):
    """Large uploads and downloads while people are editing."""
    jobs = [("no transfers", None, 0, 0), ("4 x 5 MB up", "put", 5 * MiB, 4), ("4 x 50 MB up", "put", 50 * MiB, 4),
            ("4 x 50 MB up, again", "put", 50 * MiB, 4), ("20 x 50 MB down", "get", 50 * MiB, 20)]
    for label, kind, size, count in jobs:
        s = Server(a.port)
        try:
            s.start()
            extra = {"transfer": {"kind": kind, "size": size, "count": count, "room": new_project()}} if kind else None
            r = step(s, a, label, rooms_of(100, 3, 2, 1, 2), extra=extra, connect=8 if kind == "get" else None)
            show(r, a, f"transfer={r['transfer_s']:.2f}s")
        finally:
            s.close()


async def seed_snapshot(url: str, room: dict, size: int) -> None:
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(room["seed"]))
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(f"{url}/ws/{room['project']}", max_msg_size=32 * MiB) as ws:
            await ws.send_json({"t": "hello", "since": 0})
            data = stamped(80)
            await ws.send_json({"t": "up", "id": "1", "d": b64e(data), "s": b64e(sk.sign(b"upd:" + data))})
            async for m in ws:
                if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("t") == "ack":
                    break
            snap = os.urandom(size)
            await ws.send_json({"t": "snap", "upto": 1, "d": b64e(snap), "s": b64e(sk.sign(b"snap:1:" + snap))})
            await asyncio.sleep(1.5)


@scenario("joiners")
def joiners(a):
    """Bursts of people opening a project with a large snapshot."""
    for size, n in [(2 * MiB, 20), (2 * MiB, 50), (int(11.9 * MiB), 20), (int(11.9 * MiB), 50)]:
        s = Server(a.port)
        try:
            s.start()
            room = new_project()
            asyncio.run(seed_snapshot(s.url, room, size))
            stored = sqlite3.connect(s.db).execute("SELECT COUNT(*) FROM log WHERE snap=1").fetchone()[0]
            r = step(s, a, f"{n} join {size / MiB:.0f} MB", [], extra={"joiners": {"project": room["project"], "n": n}}, connect=4, dur=12)
            show(r, a, f"snapshot_stored={stored} join_p95={r['joins_p95']:.0f}ms join_max={r['joins_max']:.0f}ms")
        finally:
            s.close()


async def crowd(url: str, keys, people: int, seconds: float, rate: float) -> None:
    """Real encrypted Yjs edits, so a browser joining has to do its real work."""
    from pycrdt import Doc, Map, Text
    base = Doc()
    files, meta = base.get("files", type=Map), base.get("meta", type=Map)
    files["main.tex"] = Text("\\documentclass{article}\n\\begin{document}\n" + "A line of text in this document.\n" * 200 + "\\end{document}\n")
    meta["main"] = "main.tex"
    seed = base.get_update()

    def signed(update: bytes) -> dict:
        data = keys.encrypt(update)
        return {"t": "up", "id": os.urandom(4).hex(), "d": b64e(data), "s": b64e(keys.sign(b"upd:" + data))}

    def varint(n: int) -> bytes:
        out = bytearray()
        while n > 127:
            out.append(n & 127 | 128)
            n >>= 7
        return bytes(out) + bytes([n])

    async def person(http, i: int) -> None:
        doc = Doc(client_id=1000 + i)
        doc.apply_update(seed)
        text = doc.get("files", type=Map)["main.tex"]
        pending: list[bytes] = []
        doc.observe(lambda e: pending.append(e.update))
        body = json.dumps({"user": {"name": f"sim {i}", "color": "#88aacc", "colorLight": "#88aacc33"}}).encode()
        clock = 0
        async with http.ws_connect(f"{url}/ws/{keys.project}", max_msg_size=32 * MiB) as ws:
            await ws.send_json({"t": "hello", "since": 0})
            end = time.monotonic() + seconds
            await asyncio.sleep(random.random() / rate)
            while time.monotonic() < end:
                clock += 1
                aw = varint(1) + varint(1000 + i) + varint(clock) + varint(len(body)) + body
                await ws.send_json({"t": "aw", "d": b64e(keys.encrypt(aw))})
                text.insert(random.randrange(len(text) + 1), "x")
                while pending:
                    await ws.send_json(signed(pending.pop(0)))
                await asyncio.sleep(1 / rate)

    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(f"{url}/ws/{keys.project}") as ws:
            await ws.send_json({"t": "hello", "since": 0})
            await ws.send_json(signed(seed))
            await asyncio.sleep(0.5)
        await asyncio.gather(*(person(http, i) for i in range(people)))


@scenario("soak")
def soak(a):
    """Round after round of people coming and going: does memory keep climbing?"""
    s = Server(a.port)
    try:
        s.start()
        print(f"  server alone: {s.usage()[1]:.0f} MB", flush=True)
        for i in range(10):
            show(step(s, a, f"round {i + 1}", rooms_of(150, 3, 2, 2, 2), dur=5), a)
    finally:
        s.close()


def browser(a):
    from texloom.crypto import Keys
    s = Server(a.port, ROOT / "web" / "dist")
    try:
        s.start()
        keys = Keys.generate()
        print(f"LINK {s.url}/#{keys.fragment()}", flush=True)
        time.sleep(a.lead)
        print(f"crowd of {a.people} starting for {a.seconds:.0f}s", flush=True)
        asyncio.run(crowd(s.url, keys, a.people, a.seconds, a.rate))
    finally:
        s.close()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scenarios", nargs="+", help=f"any of: all, browser, {', '.join(SCENARIOS)}")
    p.add_argument("--workers", type=int, default=8, help="client processes")
    p.add_argument("--port", type=int, default=8811)
    p.add_argument("--out", type=Path, help="write the results as JSON")
    p.add_argument("--people", type=int, default=30, help="browser: simulated people")
    p.add_argument("--seconds", type=float, default=180, help="browser: how long")
    p.add_argument("--lead", type=float, default=20, help="browser: seconds to open the link first")
    p.add_argument("--rate", type=float, default=2, help="browser: edits and cursor updates per second, each")
    a = p.parse_args()
    a.rows = []
    if a.scenarios == ["browser"]:
        browser(a)
        return
    names = list(SCENARIOS) if a.scenarios == ["all"] else a.scenarios
    for n in names:
        print(f"== {n}: {SCENARIOS[n].__doc__}", flush=True)
        SCENARIOS[n](a)
    if a.out:
        a.out.write_text(json.dumps(a.rows, indent=1, default=str))


if __name__ == "__main__":
    main()
