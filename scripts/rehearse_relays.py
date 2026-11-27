#!/usr/bin/env python3
"""A test lab for the stateless relays, in Docker (FUTURE_ARCHITECTURE.md, "Building it", step 9). Needs
Docker; run by hand, not in CI. It builds several "machines" of containers, keeps each tier available with
the tools the design names, breaks any part of it and times how long things take to come back.

The machines are a table (MACHINES below): change it to change them. As it is:

  a, b, c   each runs an etcd member, a PostgreSQL kept available by Patroni, a Valkey, a Sentinel and a
            relay (today's relay, built from this checkout). The voters share the machines, as the
            design's stage 3 allows, so any one machine can be lost. Patroni starts on a, and Valkey's
            primary is a's, so machine a holds both primaries.
  edge      the front door: nginx in front of every relay, as production runs it (deploy/rehearsal/nginx.conf):
            least_conn, and passive health checks (a relay that fails is left out for 5 s)
  store     the bucket: an S3-compatible store (RustFS)
  probe     where the rehearsal asks from (psql, valkey-cli, HTTP). It is never broken.

All are on one private network. Each machine's containers are also on a network of their own, so a machine
that is cut off still reaches its own parts. Every image is pinned; Patroni's is built from
deploy/rehearsal/Dockerfile.patroni and the relay's from deploy/rehearsal/Dockerfile.relay. Everything is
named with a prefix of its own and labelled texloom-rehearsal=relays, and removed at the end, also after a
failure or Ctrl-C.

A container, or a whole machine (all of its containers together), can be broken in three ways:

  kill    it stops at once (docker kill): its connections close, and its name stops resolving
  pause   it freezes (docker pause): nothing runs and nothing answers, but nothing is closed either, as when
          a machine hangs or a virtual machine is suspended
  cut     its network is cut (docker network disconnect): connections in progress go silent, and new ones
          fail at once (to Docker, its name is gone). A machine cut off still reaches its own parts; one
          container cut off reaches nothing.

The scenarios (each on a lab of its own, built and removed again; `how` is kill, pause or cut):

  pg[:how]           the PostgreSQL primary is broken. Patroni promotes a standby. Writes through a
                     multi-host address (target_session_attrs=primary) are timed until they work again. Then
                     the old primary is mended and timed until it follows the new one. A primary that was
                     frozen or cut off is watched for how long it still takes writes.
  valkey[:how]       the Valkey primary is broken: the Sentinels promote a replica. Timed likewise.
  door[:how]         relay a is broken: requests through the front door show when nginx first leaves it out,
                     how often it tries it again, and how long requests wait meanwhile; then when it sends
                     to it again once it is mended
  spread[:how]       apps open WebSockets through the front door, one after another and then all at once:
                     how least_conn spreads them over the relays. With `how`, relay a is broken first, and
                     the apps' waits to connect are timed.
  machine[:how[:m]]  machine m (default a, which holds both primaries) is broken whole: all of the above at
                     once, and etcd keeps its majority
  relay-pg[:how]     ONE of today's relays, on the Patroni cluster through a multi-host address, with apps
                     (the desktop sync engine) storing changes through the front door. The PostgreSQL
                     primary is broken, and what today's code does is measured, not fixed: whether the relay
                     finds the new primary, how long it takes, and whether the apps' changes are stored.
  collab[:how[:t]]   several relays at once: three apps on one stored project, each on a relay of its own, and two on a
                     sync-only one, all editing while `t` is broken (a relay, default relay-a; valkey or pg, whichever is
                     the primary; or a machine): timed until every app has every change and all of it is stored; then
                     a newcomer catches up from the database alone
  up                 bring the lab up, say how to reach it, and wait (Ctrl-C takes it down)
  down               remove anything a rehearsal of relays left behind (after a hard stop)

With no scenario named, every one runs except up and down: relay-pg only with kill, and spread as it is,
then with relay a killed and with it frozen. It takes about 40 minutes: Patroni's leader key lives 30 s, and
an app that nginx sends to a frozen relay waits 5 minutes. Times are printed as they are measured, and in a
table at the end.

    uv run --extra server python scripts/rehearse_relays.py                     # every scenario
    uv run --extra server python scripts/rehearse_relays.py pg valkey:pause     # just these
    uv run --extra server python scripts/rehearse_relays.py machine:cut:b
    uv run --extra server python scripts/rehearse_relays.py up

Settings, in the environment:
  RELAYS=n                 how many relays (default 3), spread over the machines that run one
  REHEARSE_PATRONI_TTL=s   a shorter leader key than Patroni's 30 s, for a quicker run
  REHEARSE_LOGS=1          print the relays' logs at the end of each scenario
  REHEARSE_NGINX_WS_TIMEOUT=60s   nginx's proxy_read_timeout for WebSockets instead of nginx.conf's 1h
                           (with 60 s, a WebSocket sent to a frozen relay is tried on another after 60 s)

To add a scenario, write `async def scenario_x(lab, how, target)` and add it to SCENARIOS. When the relays
take a `bus` setting (step 7), the lab passes them the Sentinels' address by itself (Lab.relay_args).
"""

import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_restore import App, add_line, text  # noqa: E402
from checklib import ROOT, check, summary, until  # noqa: E402
from rehearse_failover import docker  # noqa: E402

from texloom.crypto import Keys  # noqa: E402

# Images, all pinned. The Patroni image is built on postgres:16.6 and the relay's on python:3.12.16-slim
# (their Dockerfiles, in deploy/rehearsal/).
ETCD = "quay.io/coreos/etcd:v3.5.24"
VALKEY = "valkey/valkey:8.1.4"
S3 = "rustfs/rustfs:1.0.6"
NGINX = "nginx:1.30.0-alpine"
HERE = ROOT / "deploy" / "rehearsal"
LABEL = "texloom-rehearsal=relays"
RUN = secrets.token_hex(2)
PATRONI_IMAGE = f"texloom-rehearsal-relays-patroni:{RUN}"
RELAY_IMAGE = f"texloom-rehearsal-relays-relay:{RUN}"

# Which containers run on which machine. Breaking a machine breaks all of its containers together. The
# roles: etcd, pg (PostgreSQL with Patroni), valkey, sentinel, relay, door (the front door), s3 (the bucket),
# probe and kvprobe (where the rehearsal asks from). A role that appears once is named by itself ("door"),
# one on several machines by role and machine ("pg-a"). Relays are placed by RELAYS, in turn on the
# machines listed here with one.
MACHINES = {
    "a": ("etcd", "pg", "valkey", "sentinel", "relay"),
    "b": ("etcd", "pg", "valkey", "sentinel", "relay"),
    "c": ("etcd", "pg", "valkey", "sentinel", "relay"),
    "edge": ("door",),
    "store": ("s3",),
    "probe": ("probe", "kvprobe"),
}
RELAYS = int(os.environ.get("RELAYS", "3"))
TTL = int(os.environ.get("REHEARSE_PATRONI_TTL", "0"))  # 0: Patroni's own (30 s)

PG_SUPER, PG_REPL = "rehearsal-super", "rehearsal-repl"
DB_USER, DB_PASS = "texloom", "tl"
S3_KEY, S3_SECRET = "rehearsal", "rehearsal-secret-123"
SENTINEL_DOWN_MS = 5000  # how long a primary must be silent before a Sentinel thinks it down (the design: aim for 5 s)
SLOW = 0.9  # seconds: a request through the front door that waits longer counts as one that waited
WS_TIMEOUT = os.environ.get("REHEARSE_NGINX_WS_TIMEOUT", "")  # nginx's proxy_read_timeout for WebSockets, if not nginx.conf's
DOOR_WAIT = 15  # seconds a request through the front door may take: more than nginx's 10 s (nginx.conf), so its own timeouts show
HOW = {"kill": "killed", "pause": "frozen", "cut": "cut off"}

ASK_HTTP = ("import sys, urllib.request, urllib.error\n"
            "for u in sys.argv[1:]:\n"
            "    try:\n"
            "        r = urllib.request.urlopen(u, timeout=2); print(r.status, r.read().decode()); break\n"
            "    except urllib.error.HTTPError as e:\n"
            "        print(e.code, ''); break\n"
            "    except Exception:\n"
            "        pass\n")
COUNT_CONNECTIONS = ("import sys\n"
                     "n = 0\n"
                     "for line in open('/proc/net/tcp').readlines()[1:]:\n"
                     "    x = line.split()\n"
                     "    n += x[1].endswith(':2253') and x[2].startswith(sys.argv[1]) and x[3] == '01'\n"
                     "print(n)\n")  # established connections to port 8787 from the address given (in /proc's hex)
ASK_SENTINELS = ('for h in "$@"; do echo "$h $(timeout 3 valkey-cli -t 2 -h $h -p 26379 --raw '
                 'SENTINEL get-master-addr-by-name texloom 2>/dev/null | head -1)"; done')

LABS: set["Lab"] = set()  # labs that are up, removed at the end whatever happens


class Lab:
    """The containers of one rehearsal, under a prefix of their own, all removed afterwards."""

    def __init__(self, tmp: Path, relays: int = RELAYS):
        self.p = "tlrelays" + secrets.token_hex(2)
        self.tmp = tmp
        self.where: dict[str, str] = {}  # container (its short name) -> machine
        self.role: dict[str, str] = {}
        roles = [r for rs in MACHINES.values() for r in rs if r != "relay"]
        for m, rs in MACHINES.items():
            for r in rs:
                if r != "relay":
                    c = r if roles.count(r) == 1 else f"{r}-{m}"
                    self.where[c], self.role[c] = m, r
        hosts = [m for m, rs in MACHINES.items() if "relay" in rs]
        for i in range(relays):
            m = hosts[i % len(hosts)]
            c = f"relay-{m}" + (str(i // len(hosts) + 1) if i >= len(hosts) else "")
            self.where[c], self.role[c] = m, "relay"
        self.broken: dict[str, str] = {}  # container -> how it is broken now
        self.ips: dict[str, str] = {}  # relay -> its fixed address on the lab's network (nginx needs addresses)
        self.door_port = self.s3_port = 0
        self.door_ip = ""
        self.bus = False  # whether the relays take a bus setting

    def n(self, c: str) -> str:
        return f"{self.p}-{c}"

    def of(self, role: str) -> list[str]:
        return [c for c, r in self.role.items() if r == role]

    def live(self, role: str) -> list[str]:
        return [c for c in self.of(role) if c not in self.broken]

    def on(self, target: str) -> list[str]:
        """A machine's containers, or the one container named."""
        if target in MACHINES:
            return [c for c, m in self.where.items() if m == target]
        if target in self.where:
            return [target]
        raise ValueError(f"no machine or container {target!r} (machines: {', '.join(MACHINES)})")

    # --- building it -----------------------------------------------------------------

    def run(self, c: str, image: str, *args: str, env: dict | None = None, ports: tuple = (), mounts: tuple = (),
            entrypoint: str | None = None, restart: str | None = None) -> None:
        cmd = ["run", "-d", "--name", self.n(c), "--hostname", c, "--label", LABEL, "--network", self.n("net"), "--network-alias", c,
               *(["--ip", self.ips[c]] if c in self.ips else [])]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        for p in ports:
            cmd += ["-p", f"127.0.0.1::{p}"]
        for m in mounts:
            cmd += ["-v", m]
        if entrypoint is not None:
            cmd += ["--entrypoint", entrypoint]
        if restart:
            cmd += ["--restart", restart]
        docker(*cmd, image, *args)
        docker("network", "connect", "--alias", c, self.n("m-" + self.where[c]), self.n(c))

    def _wait(self, what: str, cond, secs: float = 90) -> None:
        end = time.monotonic() + secs
        while time.monotonic() < end:
            if ask(cond):
                return
            time.sleep(0.5)
        raise RuntimeError(f"timed out waiting for {what}")

    def up(self) -> None:
        LABS.add(self)
        for _ in range(10):  # a private range of its own (relays need fixed addresses), one that is free
            sub = f"10.{secrets.randbelow(50) + 200}.{secrets.randbelow(256)}"
            if docker("network", "create", "--label", LABEL, "--subnet", f"{sub}.0/24", self.n("net"), check_=False).returncode == 0:
                break
        else:
            raise RuntimeError("couldn't find a free private range for the lab's network")
        self.ips = {r: f"{sub}.{101 + i}" for i, r in enumerate(self.of("relay"))}
        for m in MACHINES:
            docker("network", "create", "--label", LABEL, self.n("m-" + m))

        self.run("s3", S3, env={"RUSTFS_ACCESS_KEY": S3_KEY, "RUSTFS_SECRET_KEY": S3_SECRET}, ports=(9000,))
        etcds = self.of("etcd")
        cluster = ",".join(f"{e}=http://{e}:2380" for e in etcds)
        for e in etcds:
            self.run(e, ETCD, "/usr/local/bin/etcd", "--name", e, "--data-dir", "/var/lib/etcd/data",
                     "--listen-client-urls", "http://0.0.0.0:2379", "--advertise-client-urls", f"http://{e}:2379",
                     "--listen-peer-urls", "http://0.0.0.0:2380", "--initial-advertise-peer-urls", f"http://{e}:2380",
                     "--initial-cluster", cluster, "--initial-cluster-state", "new", "--initial-cluster-token", self.p)
        valkeys = self.of("valkey")
        for i, v in enumerate(valkeys):
            self.run(v, VALKEY, "valkey-server", "--port", "6379", "--protected-mode", "no", "--save", "", "--appendonly", "no",
                     "--replica-announce-ip", v, *(("--replicaof", valkeys[0], "6379") if i else ()))
        self.run("probe", PATRONI_IMAGE, "infinity", entrypoint="sleep")
        self.run("kvprobe", VALKEY, "sleep", "infinity")

        self.s3_port = self.port("s3", 9000)
        self._wait("the bucket", lambda: self.s3("PUT", "/bucket") in (200, 409))
        self._wait("etcd", lambda: self.etcd_healthy() == len(etcds))

        # Patroni: the first node starts the cluster, then the others join it as standbys
        pgs = self.of("pg")
        self.patroni(pgs[0])
        self._wait("Patroni to start the cluster", lambda: (self.leader() or {}).get("name") == pgs[0])
        for c in pgs[1:]:
            self.patroni(c)
        if TTL:
            loop = max(1, TTL // 3)
            docker("exec", self.n(pgs[0]), "patronictl", "-c", "/etc/patroni.yml", "edit-config", "--force",
                   "-s", f"ttl={TTL}", "-s", f"loop_wait={loop}", "-s", f"retry_timeout={loop}")

        # Sentinels, once the Valkeys answer (a Sentinel won't start on a name that doesn't resolve)
        self._wait("Valkey's replicas", lambda: "connected_slaves:" + str(len(valkeys) - 1) in (self.kv(valkeys[0], "INFO", "replication") or ""))
        for s in self.of("sentinel"):
            conf = "\n".join(["port 26379", "protected-mode no", "sentinel resolve-hostnames yes", "sentinel announce-hostnames yes",
                              f"sentinel announce-ip {s}", f"sentinel monitor texloom {valkeys[0]} 6379 {len(self.of('sentinel')) // 2 + 1}",
                              f"sentinel down-after-milliseconds texloom {SENTINEL_DOWN_MS}", "sentinel failover-timeout texloom 60000",
                              "sentinel parallel-syncs texloom 1"])
            self.run(s, VALKEY, "sh", "-c", f"printf '%s\\n' '{conf}' > /tmp/sentinel.conf && exec valkey-sentinel /tmp/sentinel.conf")

        self._wait("Patroni's standbys to follow", lambda: self.followers() == set(pgs[1:]), 180)
        for sql in (f"CREATE ROLE {DB_USER} LOGIN PASSWORD '{DB_PASS}'", f"CREATE DATABASE texloom OWNER {DB_USER}",
                    "CREATE TABLE probe (at timestamptz DEFAULT now())"):
            if self.sql(sql) is None:
                raise RuntimeError(f"couldn't run {sql!r} on the primary")
        self._wait("the Sentinels to agree", lambda: self.sentinels_ready(), 60)

        for r in self.of("relay"):
            self.run(r, RELAY_IMAGE, *self.relay_args(), restart="on-failure")
        self._wait("the relays", lambda: all(self.relay_ok(r) for r in self.of("relay")), 120)

        servers = "\n\t\t".join(f"server {self.ips[r]}:8787 max_fails=1 fail_timeout=5s;  # {r}" for r in self.of("relay"))
        conf = (HERE / "nginx.conf").read_text().replace("\n\t\tRELAYS\n", f"\n\t\t{servers}\n")
        if WS_TIMEOUT:
            conf = conf.replace("proxy_read_timeout 1h;", f"proxy_read_timeout {WS_TIMEOUT};")
        (self.tmp / "nginx.conf").write_text(conf)
        self.run("door", NGINX, mounts=(f"{self.tmp / 'nginx.conf'}:/etc/nginx/nginx.conf:ro",), ports=(8080,))
        self.door_port = self.port("door", 8080)
        seen: set[str] = set()
        self._wait("the front door to reach every relay", lambda: seen.add(self.door()[1]) or seen >= set(self.of("relay")), 60)

    def patroni(self, c: str) -> None:
        self.run(c, PATRONI_IMAGE, env={
            "PATRONI_NAME": c, "PATRONI_RESTAPI_CONNECT_ADDRESS": f"{c}:8008", "PATRONI_POSTGRESQL_CONNECT_ADDRESS": f"{c}:5432",
            "PATRONI_ETCD3_HOSTS": ",".join(f"{e}:2379" for e in self.of("etcd")),
            "PATRONI_SUPERUSER_PASSWORD": PG_SUPER, "PATRONI_REPLICATION_PASSWORD": PG_REPL})

    def pg_url(self, db: str = "postgres", user: str = "postgres", password: str = PG_SUPER, extra: str = "") -> str:
        hosts = ",".join(f"{c}:5432" for c in self.of("pg"))
        return f"postgresql://{user}:{password}@{hosts}/{db}?target_session_attrs=primary{extra}"

    def relay_args(self) -> list[str]:
        args = ["--host", "0.0.0.0", "--port", "8787", "--database", self.pg_url("texloom", DB_USER, DB_PASS),
                "--data", "/data", "--min-free", "1M", "--files", "s3://bucket", "--s3-endpoint", "http://s3:9000",
                "--s3-region", "us-east-1", "--s3-key-id", S3_KEY, "--s3-secret", S3_SECRET]
        if self.bus:  # (step 7: the relays reach each other through Valkey, found by its Sentinels)
            args += ["--bus", "redis+sentinel://" + ",".join(f"{s}:26379" for s in self.of("sentinel")) + "/texloom"]
        return args

    def down(self) -> None:
        names = docker("ps", "-a", "--filter", f"name=^{self.p}-", "--format", "{{.Names}}", check_=False).stdout.split()
        if names:
            docker("rm", "-f", "-v", *names, check_=False, timeout=180)
        for net in ["net", *("m-" + m for m in MACHINES)]:
            docker("network", "rm", self.n(net), check_=False)
        LABS.discard(self)

    # --- breaking it -----------------------------------------------------------------

    def break_(self, target: str, how: str) -> list[str]:
        """Break a machine (all its containers) or one container: kill, pause or cut."""
        cs = [c for c in self.on(target) if c not in self.broken]
        names = [self.n(c) for c in cs]
        if how == "kill":
            for c in names:
                docker("update", "--restart", "no", c, check_=False)  # (a relay would be started again by Docker)
            docker("kill", *names)
        elif how == "pause":
            docker("pause", *names)
        elif how == "cut":
            # a machine is cut off from the others, and keeps its own network; one container is cut off from everything
            nets = ["net"] if target in MACHINES else ["net", "m-" + self.where[target]]
            for c in names:
                for net in nets:
                    docker("network", "disconnect", "-f", self.n(net), c)
            how = "cut" if target in MACHINES else "cut-all"
        else:
            raise ValueError(f"no way to break things called {how!r} (kill, pause or cut)")
        self.broken.update(dict.fromkeys(cs, how))
        return cs

    def mend(self, target: str) -> None:
        for c in self.on(target):
            how = self.broken.pop(c, None)
            if how == "kill":
                docker("start", self.n(c))
                if self.role[c] == "relay":
                    docker("update", "--restart", "on-failure", self.n(c), check_=False)
            elif how == "pause":
                docker("unpause", self.n(c))
            elif how in ("cut", "cut-all"):
                docker("network", "connect", "--alias", c, *(["--ip", self.ips[c]] if c in self.ips else []), self.n("net"), self.n(c))
                if how == "cut-all":
                    docker("network", "connect", "--alias", c, self.n("m-" + self.where[c]), self.n(c))

    # --- looking ---------------------------------------------------------------------

    def port(self, c: str, inner: int) -> int:
        return int(docker("port", self.n(c), f"{inner}/tcp").stdout.split(":")[-1].split()[0])

    def ex(self, c: str, *args: str, timeout: float = 20) -> subprocess.CompletedProcess:
        return docker("exec", self.n(c), *args, check_=False, timeout=timeout)

    def logs(self, c: str, tail: int = 25, since: float | None = None) -> str:
        args = ["logs", *(["--since", str(int(since))] if since else ["--tail", str(tail)]), self.n(c)]
        p = docker(*args, check_=False)
        return (p.stdout + p.stderr).strip()

    def s3(self, method: str, path: str, data: str | None = None) -> int:
        cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--aws-sigv4", "aws:amz:us-east-1:s3", "-u", f"{S3_KEY}:{S3_SECRET}",
               "-X", method, f"http://127.0.0.1:{self.s3_port}{path}"]
        if data is not None:
            cmd += ["--data-binary", data]
        return int(subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False).stdout or 0)

    def s3_get(self, path: str) -> str:
        return subprocess.run(["curl", "-s", "--aws-sigv4", "aws:amz:us-east-1:s3", "-u", f"{S3_KEY}:{S3_SECRET}",
                               f"http://127.0.0.1:{self.s3_port}{path}"], capture_output=True, text=True, timeout=15, check=False).stdout

    def etcd_healthy(self) -> int:
        """How many etcd members answer as healthy, asked from a live one."""
        live = self.live("etcd")
        if not live:
            return 0
        endpoints = ",".join(f"http://{e}:2379" for e in self.of("etcd"))
        p = self.ex(live[0], "etcdctl", "--endpoints", endpoints, "--dial-timeout", "2s", "--command-timeout", "3s", "endpoint", "health")
        return (p.stdout + p.stderr).count("is healthy")

    def http(self, *urls: str) -> tuple[int, str]:
        """(status, body) of the first of these the probe reaches, (0, "") if none."""
        p = self.ex("probe", "python3", "-c", ASK_HTTP, *urls)
        status, _, body = p.stdout.strip().partition(" ")
        return (int(status), body) if status.isdigit() else (0, "")

    def cluster(self) -> list[dict]:
        """Patroni's members, as the first live node that answers tells it."""
        status, body = self.http(*(f"http://{c}:8008/cluster" for c in self.live("pg")))
        return json.loads(body).get("members", []) if status == 200 else []

    def leader(self) -> dict | None:
        return next((m for m in self.cluster() if m.get("role") == "leader" and m.get("state") == "running"), None)

    def followers(self) -> set[str]:
        return {m["name"] for m in self.cluster() if m.get("role") in ("replica", "sync_standby") and m.get("state") == "streaming"}

    def member(self, name: str) -> dict:
        return next((m for m in self.cluster() if m.get("name") == name), {})

    def sql(self, q: str, db: str = "postgres") -> str | None:
        """What a statement returns, run on the primary through the multi-host address (None: it failed)."""
        p = self.ex("probe", "psql", self.pg_url(db, extra="&connect_timeout=2"), "-qtAX", "-v", "ON_ERROR_STOP=1", "-c", q, timeout=30)
        return p.stdout.strip() if p.returncode == 0 else None

    def local_primary(self, c: str) -> bool:
        """Whether this PostgreSQL itself says it is a primary (asked inside its container: no network needed)."""
        p = self.ex(c, "psql", "-h", "/var/run/postgresql", "-U", "postgres", "-tAX", "-c", "SELECT pg_is_in_recovery()", timeout=10)
        return p.returncode == 0 and p.stdout.strip() == "f"

    def kv(self, host: str, *cmd: str, port: int = 6379) -> str | None:
        p = self.ex("kvprobe", "timeout", "3", "valkey-cli", "-t", "2", "-h", host, "-p", str(port), "--raw", *cmd, timeout=10)
        out = p.stdout.strip()
        return out if p.returncode == 0 and out and not out.startswith(("ERR", "READONLY", "MASTERDOWN", "LOADING")) else None

    def sentinels_say(self) -> dict[str, str]:
        """Which Valkey each live Sentinel names as the primary ("" if it doesn't answer)."""
        p = self.ex("kvprobe", "sh", "-c", ASK_SENTINELS, "sh", *self.live("sentinel"))
        return {s: (rest.split() or [""])[0] for s, _, rest in (line.partition(" ") for line in p.stdout.splitlines() if line)}

    def kv_primary(self) -> str:
        """The Valkey that most live Sentinels name ("" if none)."""
        said = [v for v in self.sentinels_say().values() if v]
        return Counter(said).most_common(1)[0][0] if said else ""

    def sentinels_ready(self) -> bool:
        for s in self.of("sentinel"):
            out = (self.kv(s, "SENTINEL", "master", "texloom", port=26379) or "").split()
            info = dict(zip(out[::2], out[1::2]))
            if info.get("num-slaves") != str(len(self.of("valkey")) - 1) or info.get("num-other-sentinels") != str(len(self.of("sentinel")) - 1):
                return False
        return True

    def kv_write(self) -> bool:
        """A write as a client does it: ask the Sentinels for the primary, then write there."""
        primary = self.kv_primary()
        return bool(primary) and self.kv(primary, "SET", "probe", str(time.time())) == "OK"

    def kv_role(self, c: str) -> str:
        """master or slave, as this Valkey itself says (asked inside its container)."""
        p = self.ex(c, "timeout", "3", "valkey-cli", "--raw", "ROLE", timeout=10)
        return p.stdout.split()[0] if p.returncode == 0 and p.stdout.split() else ""

    def relay_ok(self, r: str) -> bool:
        return self.http(f"http://{r}:8787/api/info")[0] == 200

    def door(self) -> tuple[int, str, tuple[str, ...]]:
        """(status, the relay that answered, every relay nginx tried) of a request through the front door."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.door_port}/api/info", timeout=DOOR_WAIT) as r:
                status, tried = r.status, r.headers.get("X-Relay", "")
        except urllib.error.HTTPError as e:
            status, tried = e.code, e.headers.get("X-Relay", "")
        except OSError:
            return 0, "", ()
        relays = self.relays_in(tried)
        return status, (relays[-1] if relays and status == 200 else ""), relays

    def relays_in(self, header: str) -> tuple[str, ...]:
        """The relays nginx names in X-Relay ($upstream_addr: every address it tried, the last one answered)."""
        names = {ip: r for r, ip in self.ips.items()}
        return tuple(names.get(a.strip().rpartition(":")[0], a.strip()) for a in header.replace(" : ", ",").split(",") if a.strip())

    def connections(self, r: str) -> int:
        """How many connections from the front door a relay holds (asked inside its container)."""
        if not self.door_ip:
            self.door_ip = docker("inspect", "--format", f'{{{{(index .NetworkSettings.Networks "{self.n("net")}").IPAddress}}}}',
                                  self.n("door")).stdout.strip()
        hexip = "".join(f"{int(o):02X}" for o in reversed(self.door_ip.split(".")))
        p = self.ex(r, "python3", "-c", COUNT_CONNECTIONS, hexip)
        return int(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip().isdigit() else -1


def ask(cond) -> bool:
    """cond(), with Docker failing to answer counted as no."""
    try:
        return bool(cond())
    except (RuntimeError, subprocess.TimeoutExpired, ValueError, KeyError, OSError):
        return False


# --- timing ------------------------------------------------------------------------------

TIMES: list[tuple[str, str, float | None]] = []  # (scenario, what, seconds), for the table at the end
SCENE = [""]


def took(what: str, secs: float | None, note: str = "") -> None:
    TIMES.append((SCENE[0], what, secs))
    t = f"{secs:.1f} s" if secs is not None else "not within the wait"
    print(f"  · {what}: {t}" + (f" ({note})" if note else ""), flush=True)


def say(what: str) -> None:
    """A measured fact that is neither a time nor a check."""
    print(f"  · {what}", flush=True)


async def timed(cond, t0: float, secs: float, every: float = 0.5) -> float | None:
    """Seconds from t0 until cond() holds (asked in a thread: it asks Docker), or None if not within secs."""
    while time.monotonic() - t0 < secs:
        if await asyncio.to_thread(ask, cond):
            return time.monotonic() - t0
        await asyncio.sleep(every)
    return None


class Track:
    """Asks something over and over in the background, noting when each answer came and whether it was good."""

    def __init__(self, cond, every: float = 0.5):
        self.cond, self.every = cond, every
        self.seen: list[tuple[float, float, bool]] = []  # (asked, answered, good)
        self.task: asyncio.Task | None = None

    def start(self) -> "Track":
        self.task = asyncio.create_task(self._run())
        return self

    async def _run(self) -> None:
        while True:
            t = time.monotonic()
            good = await asyncio.to_thread(ask, self.cond)
            self.seen.append((t, time.monotonic(), good))
            await asyncio.sleep(self.every)

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def outage(self, t0: float) -> tuple[int, float | None]:
        """(answers that went wrong after t0, seconds from t0 until the first good answer after the last of
        them: 0 if none went wrong, None if the last answer is still wrong)."""
        after = [x for x in self.seen if x[1] >= t0]
        bad = [i for i, x in enumerate(after) if not x[2]]
        if not bad:
            return 0, 0.0
        rest = after[bad[-1] + 1:]
        return len(bad), (rest[0][1] - t0 if rest else None)

    async def recovered(self, t0: float, secs: float) -> tuple[int, float | None]:
        """Waits (up to secs from t0) until something went wrong after t0 and is good again."""
        while time.monotonic() - t0 < secs:
            n, back = self.outage(t0)
            if n and back is not None:
                return n, back
            await asyncio.sleep(0.25)
        return self.outage(t0)


class DoorTrack:
    """Requests through the front door, several at a time, from threads: when each went, how long it waited,
    whether it was answered and by which relay."""

    def __init__(self, lab: Lab, workers: int = 4):
        self.lab, self.workers = lab, workers
        self.seen: list[tuple[float, float, int, str, tuple[str, ...]]] = []  # (sent, answered, status, relay, relays tried)
        self.stopping = threading.Event()
        self.threads = [threading.Thread(target=self._work, daemon=True) for _ in range(workers)]

    def start(self) -> "DoorTrack":
        for t in self.threads:
            t.start()
        return self

    def _work(self) -> None:
        while not self.stopping.is_set():
            t = time.monotonic()
            status, relay, tried = self.lab.door()
            self.seen.append((t, time.monotonic(), status, relay, tried))
            time.sleep(0.1)

    async def stop(self) -> None:
        self.stopping.set()
        await asyncio.gather(*(asyncio.to_thread(t.join, DOOR_WAIT + 5) for t in self.threads))

    def bad(self, t0: float, t1: float = float("inf")) -> list[tuple]:
        """Requests answered between t0 and t1 that failed or waited longer than SLOW."""
        return [x for x in self.seen if t0 <= x[1] <= t1 and (x[2] != 200 or x[1] - x[0] > SLOW)]

    def served_by(self, relay: str, t0: float) -> float | None:
        return next((x[1] - t0 for x in self.seen if x[0] >= t0 and x[2] == 200 and x[3] == relay), None)


# --- the scenarios -------------------------------------------------------------------------

async def watch_pg(lab: Lab, how: str, old: str, t0: float, writes: Track) -> None:
    """After the PostgreSQL primary `old` was broken at t0: a standby is promoted and writes work again. A
    primary that was cut off is watched for how long it still takes writes."""
    demoted_job = asyncio.create_task(timed(lambda: not lab.local_primary(old), t0, 150)) if how == "cut" else None
    elected = await timed(lambda: (lab.leader() or {}).get("name") not in (None, old), t0, 150)
    check(f"Patroni promotes a standby when the primary is {HOW[how]}", elected is not None, lab.cluster())
    took("a standby is promoted (Patroni's leader changes)", elected)
    n, back = await writes.recovered(t0, 150)
    took("writes through the multi-host address work again", back, f"{n} tries failed")
    if demoted_job:
        demoted = await demoted_job
        took("the cut-off primary stops taking writes (Patroni demotes it: no etcd majority)", demoted)
        if demoted is not None and elected is not None:
            overlap = max(0.0, demoted - elected)
            took("time two PostgreSQLs said they were the primary at once", overlap,
                 "only its own machine could reach the old one" if overlap else "none: the old one gave up first")


async def new_timeline(lab: Lab, timeline: int) -> None:
    """The new primary is on the next timeline, which the design reads to tell a failover from a blip."""
    t = time.monotonic()
    cp = await timed(lambda: lab.sql("SELECT timeline_id FROM pg_control_checkpoint()") == str(timeline + 1), t, 60)
    check("the new primary's timeline is one higher (pg_control_checkpoint(), as the design reads it)", cp is not None,
          lab.sql("SELECT timeline_id FROM pg_control_checkpoint()"))
    took("the new primary's pg_control_checkpoint() shows the new timeline (from when writes worked again)", cp)
    pt = await timed(lambda: int((lab.leader() or {}).get("timeline") or 0) == timeline + 1, t, 60)
    took("Patroni's own view (/cluster) shows the new timeline (likewise)", pt, "it is updated on Patroni's next round")


async def pg_after_mend(lab: Lab, how: str, old: str, t1: float) -> None:
    """After the old primary was mended at t1: it follows the new one. One that was frozen or cut off wakes
    still thinking it is the primary, until Patroni sees otherwise: for how long?"""
    if how in ("pause", "cut"):
        still = await timed(lambda: not lab.local_primary(old), t1, 60, 0.2)
        took("once mended, the old primary still said it was the primary for", still,
             "it would take writes meanwhile from anyone who reaches it" if still else "")
    back = await timed(lambda: old in lab.followers(), t1, 180)
    check("mended, the old primary follows the new one", back is not None, lab.member(old))
    took("the old primary follows the new one again, after mending", back)


async def scenario_pg(lab: Lab, how: str, target: str) -> None:
    leader = lab.leader()
    assert leader is not None
    old, timeline = leader["name"], int(leader["timeline"])
    writes = Track(lambda: lab.sql("INSERT INTO probe DEFAULT VALUES RETURNING 1") == "1").start()
    try:
        await asyncio.sleep(2)
        check("before: writes through the multi-host address work", writes.seen and all(x[2] for x in writes.seen), writes.seen)
        t0 = time.monotonic()
        lab.break_(old, how)
        print(f"  ({old}, the primary, is {HOW[how]})", flush=True)
        await watch_pg(lab, how, old, t0, writes)
    finally:
        await writes.stop()
    await new_timeline(lab, timeline)
    t1 = time.monotonic()
    lab.mend(old)
    await pg_after_mend(lab, how, old, t1)


async def watch_kv(lab: Lab, how: str, old: str, t0: float, writes: Track) -> None:
    def promoted() -> bool:
        said = set(lab.sentinels_say().values())
        return len(said) == 1 and "" not in said and old not in said and lab.kv_role(said.pop()) == "master"
    done = await timed(promoted, t0, 120)
    check(f"the Sentinels promote a replica when the Valkey primary is {HOW[how]}", done is not None, lab.sentinels_say())
    took("every live Sentinel names a new primary, which says it is one", done, f"Sentinels wait {SENTINEL_DOWN_MS / 1000:g} s first")
    n, back = await writes.recovered(t0, 120)
    took("writes to the primary the Sentinels name work again", back, f"{n} tries failed")


async def kv_after_mend(lab: Lab, how: str, old: str, t1: float) -> None:
    if how in ("pause", "cut"):
        still = await timed(lambda: lab.kv_role(old) != "master", t1, 60, 0.2)
        took("once mended, the old Valkey primary still said it was the primary for", still,
             "what it took meanwhile is thrown away when it follows" if still else "")
    back = await timed(lambda: lab.kv_role(old) == "slave" and "master_link_status:up" in (lab.kv(old, "INFO", "replication") or ""), t1, 120)
    check("mended, the old Valkey primary follows the new one", back is not None, lab.kv(old, "INFO", "replication"))
    took("the old Valkey primary follows the new one again, after mending", back)


async def scenario_valkey(lab: Lab, how: str, target: str) -> None:
    old = lab.kv_primary()
    writes = Track(lab.kv_write).start()
    try:
        await asyncio.sleep(2)
        check("before: writes to the primary the Sentinels name work", writes.seen and all(x[2] for x in writes.seen), writes.seen)
        t0 = time.monotonic()
        lab.break_(old, how)
        print(f"  ({old}, the primary, is {HOW[how]})", flush=True)
        await watch_kv(lab, how, old, t0, writes)
    finally:
        await writes.stop()
    t1 = time.monotonic()
    lab.mend(old)
    await kv_after_mend(lab, how, old, t1)


async def watch_door(lab: Lab, how: str, victims: list[str], t0: float, done: float, door: DoorTrack, secs: float = 40) -> None:
    """After relays were broken at t0 (Docker had done it by `done`): what the front door does. nginx checks
    health passively, so it learns that a relay is broken only from a request it sends there, leaves the
    relay out for fail_timeout (5 s), and then tries it again with one request."""
    await asyncio.sleep(max(0.0, t0 + secs - time.monotonic()))
    after = [x for x in door.seen if x[0] >= done]
    failed = [x for x in door.seen if x[1] >= t0 and x[2] != 200]
    check(f"with relays {HOW[how]} ({', '.join(victims)}), every request through the front door is still answered "
          f"(within {DOOR_WAIT} s)", not failed, failed[:5])
    late = [round(x[0] - t0, 2) for x in after if x[3] in victims]
    check("and only by the other relays, once they were broken", not late, f"sent this long after the break: {late[:5]}")
    sent = [x for x in after if set(x[4]) & set(victims)]
    first_out = next((x[1] - t0 for x in sent if x[3] and x[3] not in victims), None)
    took("nginx first leaves a broken relay out (a request it sent there was passed to another)", first_out)
    if sent:
        gaps = [round(b[0] - a[0], 1) for a, b in zip(sent, sent[1:])]
        say(f"over {secs:g} s nginx sent {len(sent)} of {len(after)} requests to a broken relay "
            f"(at {', '.join(f'{x[0] - t0:.1f}' for x in sent[:12])}{' …' if len(sent) > 12 else ''} s; gaps {gaps[:8]})")
        waits = sorted(x[1] - x[0] for x in sent)
        say(f"each of those waited {waits[0]:.1f} to {waits[-1]:.1f} s (median {waits[len(waits) // 2]:.1f} s) before another relay answered")
    slow = [x for x in after if x[1] - x[0] > SLOW and not set(x[4]) & set(victims)]
    say(f"requests that waited over {SLOW:g} s without going to a broken relay: {len(slow)}")


async def scenario_door(lab: Lab, how: str, target: str) -> None:
    victim = target or lab.of("relay")[0]
    if len(lab.of("relay")) < 2:
        check("the front door needs two relays or more for this (RELAYS)", False, len(lab.of("relay")))
        return
    door = DoorTrack(lab).start()
    try:
        await asyncio.sleep(3)
        check("before: every request through the front door is answered quickly, by every relay",
              not door.bad(0) and {x[3] for x in door.seen} >= set(lab.of("relay")), door.bad(0)[:5])
        t0 = time.monotonic()
        lab.break_(victim, how)
        await watch_door(lab, how, [victim], t0, time.monotonic(), door)
        t1 = time.monotonic()
        lab.mend(victim)
        back = await timed(lambda: door.served_by(victim, t1) is not None, t1, 60)
        check("mended, the front door sends to it again", back is not None)
        took("mended, the front door sends to it again", back, "nginx tries it again 5 s after it last failed")
    finally:
        await door.stop()


async def scenario_machine(lab: Lab, how: str, target: str) -> None:
    m = target or "a"
    cs = lab.on(m)
    leader = lab.leader() or {}
    pg_old = leader.get("name") if leader.get("name") in cs else None
    kv_old = lab.kv_primary()
    kv_old = kv_old if kv_old in cs else None
    relays = [c for c in cs if lab.role[c] == "relay"]
    print(f"  (machine {m}: {', '.join(cs)}; primaries on it: {', '.join(filter(None, (pg_old, kv_old))) or 'none'})", flush=True)
    pg_writes = Track(lambda: lab.sql("INSERT INTO probe DEFAULT VALUES RETURNING 1") == "1").start()
    kv_writes = Track(lab.kv_write).start()
    door = DoorTrack(lab).start()
    try:
        await asyncio.sleep(3)
        check("before: writes to PostgreSQL and Valkey work, and the front door answers quickly",
              all(t.seen and all(x[2] for x in t.seen) for t in (pg_writes, kv_writes)) and door.seen and not door.bad(0),
              (pg_writes.seen[-3:], kv_writes.seen[-3:], door.bad(0)[:3]))
        t0 = time.monotonic()
        lab.break_(m, how)
        done = time.monotonic()
        jobs = []
        if pg_old:
            jobs.append(watch_pg(lab, how, pg_old, t0, pg_writes))
        if kv_old:
            jobs.append(watch_kv(lab, how, kv_old, t0, kv_writes))
        if relays:
            jobs.append(watch_door(lab, how, relays, t0, done, door, 40))
        await asyncio.gather(*jobs)
        if not pg_old:
            n, back = pg_writes.outage(t0)
            check("PostgreSQL's writes go on (its primary is elsewhere)", n == 0 or back is not None, pg_writes.seen[-5:])
        if not kv_old:
            n, back = kv_writes.outage(t0)
            check("Valkey's writes go on (its primary is elsewhere)", n == 0 or back is not None, kv_writes.seen[-5:])
        live = len(lab.live("etcd"))
        check(f"etcd keeps its majority ({live} of {len(lab.of('etcd'))} members answer)", lab.etcd_healthy() == live and live > len(lab.of("etcd")) // 2)
    finally:
        for t in (pg_writes, kv_writes):
            await t.stop()
        await door.stop()
    if pg_old:
        await new_timeline(lab, int(leader.get("timeline") or 0))
    t1 = time.monotonic()
    seen = DoorTrack(lab).start() if relays else None
    lab.mend(m)

    async def etcd_back() -> None:
        back = await timed(lambda: lab.etcd_healthy() == len(lab.of("etcd")), t1, 120)
        check("mended, etcd has every member again", back is not None)
        took("mended, every etcd member is healthy again", back)

    async def follows(c: str) -> None:
        if lab.role[c] == "pg":
            took(f"mended, {c} follows again", await timed(lambda: c in lab.followers(), t1, 180))
        else:
            took(f"mended, {c} follows again", await timed(lambda: "master_link_status:up" in (lab.kv(c, "INFO", "replication") or ""), t1, 120))

    async def door_back() -> None:
        assert seen is not None
        back = await timed(lambda: all(seen.served_by(r, t1) is not None for r in relays), t1, 90)
        check("mended, the front door sends to its relays again", back is not None)
        took("mended, the front door sends to its relays again", back)

    jobs = [pg_after_mend(lab, how, pg_old, t1)] if pg_old else []
    jobs += [kv_after_mend(lab, how, kv_old, t1)] if kv_old else []
    jobs += [etcd_back()] + [follows(c) for c in cs if lab.role[c] in ("pg", "valkey") and c not in (pg_old, kv_old)]
    jobs += [door_back()] if relays else []
    try:
        await asyncio.gather(*jobs)  # (all at once: each is timed from the mend)
    finally:
        if seen:
            await seen.stop()


def log_summary(log: str, t0_wall: float) -> list[str]:
    """A relay's log, shortened: its records, with seconds from t0_wall, each traceback by its last line and
    access-log lines that went well left out; the same record again is counted, not repeated."""
    records: list[list[str]] = []
    for line in log.splitlines():
        if line[:4].isdigit() and line[4:5] == "-":
            records.append([line])
        elif records and line.strip():
            records[-1].append(line)
    out: list[str] = []
    seen: dict[str, int] = {}
    for rec in records:
        head = rec[0]
        if '" 200 ' in head or '" 101 ' in head or '" 204 ' in head:
            continue
        try:
            at = time.mktime(time.strptime(head[:19], "%Y-%m-%d %H:%M:%S")) - time.timezone + int(head[20:23]) / 1000
            when = f"{at - t0_wall:+7.1f} s"
        except ValueError:
            when = "        "
        what = head[24:].strip()
        if len(rec) > 1:
            what += "  <- " + rec[-1].strip()
        what = what[:230]
        if what in seen:
            seen[what] += 1
            continue
        seen[what] = 1
        out.append(f"{when}  {what}")
    return [x + (f"  (x{seen[x[11:]]})" if seen.get(x[11:], 1) > 1 else "") for x in out]


class Heard(logging.Handler):
    """What the apps' sync engines log (connected, disconnected, offline), with when."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.lines: list[tuple[float, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append((time.monotonic(), record.getMessage()))


async def scenario_relay_pg(lab: Lab, how: str, target: str) -> None:
    """One of today's relays, through a multi-host address, when the PostgreSQL primary is broken: measured."""
    relay = lab.of("relay")[0]
    base = f"http://127.0.0.1:{lab.door_port}"
    keys = Keys.generate()
    tmp = lab.tmp
    alice = App(tmp / "alice", keys, base)
    bob = App(tmp / "bob", keys.personal("Bob")[2], base)
    heard = Heard()
    logger = logging.getLogger("texloom")
    level = logger.level
    logger.addHandler(heard)
    logger.setLevel(logging.INFO)
    try:
        ra, rb = await alice.start(), await bob.start()
        for i in range(3):
            await alice.edit(f"alice {i}")
        await bob.edit("bob 0")
        check("before: Alice and Bob, through the front door and one relay, see each other's changes",
              await until(lambda: text(ra) == text(rb)), (text(ra), text(rb)))
        leader = lab.leader()
        assert leader is not None
        old = leader["name"]
        await alice.edit("alice just before")  # stored, and acknowledged, just before the primary goes
        head_before = lab.sql(f"SELECT seq FROM head WHERE project = '{keys.project}'", "texloom")
        say(f"the project's log is at change {head_before} on {old}, the primary")

        relay_up = Track(lambda: lab.relay_ok(relay)).start()
        db_up = Track(lambda: lab.sql("SELECT 1", "texloom") == "1").start()
        since = time.time() - 1
        t0 = time.monotonic()
        lab.break_(old, how)
        print(f"  ({old}, the primary, is {HOW[how]}; Alice makes a change straight away)", flush=True)
        waiting = set(alice.r.outbox)
        add_line(ra, "alice during")
        during = set(alice.r.outbox) - waiting
        elected_job = asyncio.create_task(timed(lambda: (lab.leader() or {}).get("name") not in (None, old), t0, 180))
        bob_job = asyncio.create_task(timed(lambda: "alice during" in text(rb), t0, 180, 0.2))
        stored = await timed(lambda: not during & set(alice.r.outbox), t0, 180, 0.2)
        elected = await elected_job
        check(f"(the lab) Patroni promotes a standby when the primary is {HOW[how]}", elected is not None, lab.cluster())
        took("a standby is promoted", elected)
        n, db_back = await db_up.recovered(t0, 30)
        took("the database answers through the multi-host address again (from the probe)", db_back, f"{n} tries failed")
        took("Alice's change made at the break is acknowledged as stored", stored)
        took("Bob sees Alice's change (passed on live, or after it is stored)", await bob_job)
        await relay_up.stop()
        await db_up.stop()
        rn, rback = relay_up.outage(t0)
        say(f"the relay's /api/info failed {rn} times after the break" + (f", fine again {rback:.1f} s after it" if rn and rback is not None else ""))
        state = json.loads(docker("inspect", "--format", "{{json .State}}", lab.n(relay)).stdout)
        restarts = docker("inspect", "--format", "{{.RestartCount}}", lab.n(relay)).stdout.strip()
        say(f"the relay's process: {state.get('Status')}, restarted {restarts} times by Docker (as systemd would: Restart=on-failure)")
        conn = [(round(t - t0, 1), line) for t, line in heard.lines if t >= t0 and ("connect" in line or "offline" in line)]
        say(f"the apps' connections after the break: {conn or 'none dropped'}")
        said = Counter(line[:160] for t, line in heard.lines if t >= t0 and not ("connect" in line or "offline" in line))
        say("what the apps logged after the break: " + ("; ".join(f"{x} (x{k})" for x, k in said.items()) or "nothing"))
        say(f"still waiting in Alice's outbox: {len(alice.r.outbox)}, in Bob's: {len(bob.r.outbox)}")

        # changes made once the new primary is there
        def change(app: App, line: str) -> set[str]:
            """Make a change; the ids it waits in the outbox under."""
            had = set(app.r.outbox)
            add_line(app.r, line)
            return set(app.r.outbox) - had
        t1 = time.monotonic()
        after = change(alice, "alice after")
        took("a change Alice makes after the failover is stored (acknowledged)", await timed(lambda: not after & set(alice.r.outbox), t1, 120, 0.2))
        bob_after = change(bob, "bob after")
        took("and one of Bob's", await timed(lambda: not bob_after & set(bob.r.outbox), time.monotonic(), 120, 0.2))
        took("Bob sees Alice's change made after the failover", await timed(lambda: "alice after" in text(rb), t1, 30, 0.2),
             "a change that builds on one he lacks waits until he has that one")
        say(f"still waiting in Alice's outbox: {len(alice.r.outbox)}, in Bob's: {len(bob.r.outbox)}")
        head_after = lab.sql(f"SELECT seq FROM head WHERE project = '{keys.project}'", "texloom")
        say(f"the project's log is at change {head_after} on the new primary ({(lab.leader() or {}).get('name')})")

        if alice.r.outbox:  # what mends it: the app connecting again (its outbox is sent on every connect)
            await alice.stop()
            t2 = time.monotonic()
            ra = await alice.start()
            took("once Alice's app connects again, all she has waiting is stored", await timed(lambda: not alice.r.outbox, t2, 60, 0.2))
            took("and Bob then sees her changes", await timed(lambda: "alice during" in text(rb) and "alice after" in text(rb), t2, 60, 0.2))
        newcomer = App(tmp / "newcomer", Keys(keys.project, keys.key, None), base)
        want = ["alice 0", "alice 2", "bob 0", "alice just before", "alice during", "alice after", "bob after"]
        try:
            rc = await asyncio.wait_for(newcomer.start(), 60)
            await until(lambda: all(x in text(rc) for x in want), 30)
            missing = [x for x in want if x not in text(rc)]
            say("a newcomer, catching up from the new primary, gets every change" if not missing else
                f"a newcomer, catching up from the new primary, misses: {missing}")
        except (TimeoutError, AssertionError) as e:
            say(f"a newcomer can't catch up ({type(e).__name__})")
        finally:
            await newcomer.stop()
        same = await until(lambda: text(ra) == text(rb), 20)
        say("Alice and Bob end with the same text" if same else f"Alice and Bob differ: {text(ra)!r} / {text(rb)!r}")
        print("  · the relay's log since the break (without requests that went well; tracebacks by their last line):", flush=True)
        for x in log_summary(lab.logs(relay, since=since), t0_wall=since + 1):
            print("      " + x, flush=True)
        lab.mend(old)
        check("(the lab) mended, the old primary follows the new one", await timed(lambda: old in lab.followers(), time.monotonic(), 180) is not None)
    finally:
        logger.removeHandler(heard)
        logger.setLevel(level)
        await alice.stop()
        await bob.stop()


def ws_relay(lab: Lab, a: App) -> str:
    """Which relay an app's WebSocket reached, as nginx told it (X-Relay, on the answer to its upgrade)."""
    ws = a.r.ws if a.r else None
    resp = getattr(ws, "_response", None) if ws is not None and not ws.closed else None
    if resp is None:
        return "not connected"
    tried = lab.relays_in(resp.headers.get("X-Relay", ""))
    return tried[-1] if tried else "?"


async def scenario_spread(lab: Lab, how: str, target: str) -> None:
    """Apps (each with a project of its own) open WebSockets through the front door: how least_conn spreads
    them over the relays, opened one after another, then all at once. With `how`, relay a (or the target)
    is broken first, and each app's wait to connect is timed: nginx leaves a relay out only after a request
    to it fails, and a WebSocket's request may wait (proxy_read_timeout) for as long as the app does."""
    relays = lab.of("relay")
    n = 4 * len(relays)
    base = f"http://127.0.0.1:{lab.door_port}"
    victim = (target or relays[0]) if how else ""
    if victim:
        lab.break_(victim, how)
        print(f"  ({victim} is {HOW[how]} before the apps connect)", flush=True)
    live = [r for r in relays if r != victim]
    for way in ("one after another", "all at once"):
        apps = [App(lab.tmp / f"spread-{way[:3]}-{i}", Keys.generate(), base) for i in range(n)]
        waits: list[float | None] = []
        began: dict[int, float] = {}

        async def start(a: App) -> None:
            began[id(a)] = t = time.monotonic()
            try:
                await a.start()  # (it waits 30 s at most for the app to connect and catch up)
                waits.append(time.monotonic() - t)
            except TimeoutError:
                waits.append(None)
        try:
            if way == "all at once":
                await asyncio.gather(*(start(a) for a in apps))
            else:
                for a in apps:
                    await start(a)
            await asyncio.sleep(1)
            said = Counter(ws_relay(lab, a) for a in apps)
            held = {r: said.get(r, 0) for r in live}
            say(f"{n} apps' WebSockets, opened {way}, by the relay nginx chose (X-Relay): "
                + ", ".join(f"{k} {v}" for k, v in sorted(said.items())))
            say("connections from nginx each relay holds (a cross-check; nginx may keep an idle one for up to a minute): "
                + ", ".join(f"{r} {lab.connections(r)}" for r in live))
            done = sorted(w for w in waits if w is not None)
            say(f"their wait to connect and catch up: {done[0]:.1f} to {done[-1]:.1f} s, median {done[len(done) // 2]:.1f} s"
                + (f"; {len(waits) - len(done)} not connected within 30 s" if len(done) < len(waits) else "") if done else "none connected")
            if victim:
                stuck = [a for a in apps if a.r is None or not a.r.synced.is_set()]
                say(f"apps not connected within 30 s: {len(stuck)} of {n} opened {way}")
                if not stuck:
                    took(f"the longest wait for an app to connect, opened {way}", done[-1] if done else None)
                elif way == "one after another":
                    # how long they wait in the end: nginx gives the WebSocket's request an hour (proxy_read_timeout),
                    # so it is the app's own limit that ends it (aiohttp's: 5 minutes), and its next try lands elsewhere
                    connected: dict[int, float] = {}
                    end = time.monotonic() + 420
                    while time.monotonic() < end and len(connected) < len(stuck):
                        for a in stuck:
                            if id(a) not in connected and a.r and a.r.synced.is_set():
                                connected[id(a)] = time.monotonic() - began[id(a)]
                        await asyncio.sleep(1)
                    got = sorted(connected.values())
                    say(f"the {len(stuck)} that nginx sent to the {HOW[how]} relay connected after "
                        + (", ".join(f"{x:.0f}" for x in got) + " s" if got else "nothing yet")
                        + (f"; {len(stuck) - len(got)} not within 420 s" if len(got) < len(stuck) else ""))
                    took("the longest an app sent to the broken relay waited to connect", got[-1] if len(got) == len(stuck) else None)
            else:
                even = max(held.values()) - min(held.values()) <= 1
                check(f"least_conn spreads them evenly over every relay ({way})", sum(held.values()) == n and even, held)
        finally:
            await asyncio.gather(*(a.stop() for a in apps))
        await asyncio.sleep(2)
    if victim:
        lab.mend(victim)


def everyone_has(apps: list, lines: list[str]) -> bool:
    return all(a.r is not None and all(x in text(a.r) for x in lines) for a in apps)


async def scenario_collab(lab: Lab, how: str, target: str) -> None:
    """Several relays at once (FUTURE_ARCHITECTURE.md): three apps editing one stored project, each on a relay of its
    own, and two on a sync-only project, through the front door. A part is broken (`target`: a relay, the default
    relay-a; valkey or pg, whichever is the primary; or a whole machine) while every app goes on editing. Timed: how
    long until every app has every change, and every stored change is acknowledged; then the same after mending, and
    a newcomer catching up from the database alone."""
    base = f"http://127.0.0.1:{lab.door_port}"
    victim = {"": "relay-a", "valkey": lab.kv_primary(), "pg": (lab.leader() or {}).get("name", "pg-a")}.get(target, target)
    stored, sync = Keys.generate(), Keys.generate()
    check("(the sync-only project is set so)", lab.sql(f"INSERT INTO mode(project, relay) VALUES ('{sync.project}', true)", "texloom") is not None)
    names = ["alice", "bob", "carol"]
    writers = [App(lab.tmp / n, stored if n == "alice" else stored.personal(n.title())[2], base) for n in names]
    pair = [App(lab.tmp / n, sync if n == "dave" else sync.personal(n.title())[2], base) for n in ("dave", "erin")]
    everyone = writers + pair
    try:
        for a in everyone:  # (one after another: least_conn puts each on the relay with the fewest)
            await a.start()
        on = {n: ws_relay(lab, a) for n, a in zip(names + ["dave", "erin"], everyone)}
        say("which relay each app reached: " + ", ".join(f"{n} {r}" for n, r in on.items()))
        check("the three writers are on three different relays", len({on[n] for n in names}) == 3, on)
        for n, a in zip(names, writers):
            await a.edit(f"{n} before")
        add_line(pair[0].r, "dave before")  # (the file made once, then both write to it)
        await until(lambda: "dave before" in text(pair[1].r), 30)
        add_line(pair[1].r, "erin before")
        before = [f"{n} before" for n in names]
        check("before: every writer has every change", await until(lambda: everyone_has(writers, before), 30))
        check("before: the sync-only pair have each other's", await until(lambda: everyone_has(pair, ["dave before", "erin before"]), 30))

        t0 = time.monotonic()
        lab.break_(victim, how)
        print(f"  ({victim} is {HOW[how]}; every app makes a change at once, and another 5 s later)", flush=True)
        during = []
        for n, a in zip(names + ["dave", "erin"], everyone):
            add_line(a.r, f"{n} during")
        await asyncio.sleep(5)
        for n, a in zip(names + ["dave", "erin"], everyone):
            add_line(a.r, f"{n} later")
        during = [f"{n} {w}" for n in names for w in ("during", "later")]
        sync_lines = [f"{n} {w}" for n in ("dave", "erin") for w in ("during", "later")]
        writers_have = asyncio.create_task(timed(lambda: everyone_has(writers, during), t0, 180, 0.2))
        pair_have = asyncio.create_task(timed(lambda: everyone_has(pair, sync_lines), t0, 180, 0.2))
        stored_all = await timed(lambda: all(a.r is not None and not a.r.outbox for a in writers), t0, 180, 0.2)
        w, s_ = await writers_have, await pair_have
        check(f"with {victim} {HOW[how]}, every writer ends with every change made during the break", w is not None)
        check(f"... and every one of them is stored (acknowledged)", stored_all is not None)
        check(f"... and the sync-only pair end with each other's", s_ is not None)
        took("every writer has every change made during the break", w)
        took("every one of them is stored", stored_all)
        took("the sync-only pair have each other's", s_)
        say("which relay each app is on now: " + ", ".join(f"{n} {ws_relay(lab, a)}" for n, a in zip(names + ["dave", "erin"], everyone)))

        lab.mend(victim)
        t1 = time.monotonic()
        for n, a in zip(names, writers):
            add_line(a.r, f"{n} after")
        after = [f"{n} after" for n in names]
        w2 = await timed(lambda: everyone_has(writers, after), t1, 120, 0.2)
        check(f"mended: changes made after reach every writer", w2 is not None)
        took("after mending, every writer has a change each made", w2, "from the mend")
        newcomer = App(lab.tmp / "newcomer", Keys(stored.project, stored.key, None), base)
        try:
            for a in writers:
                await a.stop()  # (so the newcomer has only the database to catch up from)
            nc = await newcomer.start()
            want = before + during + after
            got = await until(lambda: all(x in text(nc) for x in want), 30)
            check("a newcomer, alone, gets every change from the database", got, [x for x in want if x not in text(nc)])
        finally:
            await newcomer.stop()
        for r in lab.live("relay"):
            said = [x for x in log_summary(lab.logs(r, tail=400), t0_wall=time.time() - (time.monotonic() - t0) - 1)
                    if any(k in x for k in ("failed over", "gone", "bus", "unavailable", "recheck"))]
            if said:
                print(f"  · {r} said: " + "; ".join(said[:6]), flush=True)
    finally:
        await asyncio.gather(*(a.stop() for a in everyone), return_exceptions=True)


async def scenario_up(lab: Lab, how: str, target: str) -> None:
    print(f"""
  The lab is up. Its containers are named {lab.p}-<name>:
    {', '.join(f'{m}: ' + ' '.join(lab.on(m)) for m in MACHINES)}
  The front door: http://127.0.0.1:{lab.door_port}   the bucket: http://127.0.0.1:{lab.s3_port} (key {S3_KEY})
  The database, on its primary:   docker exec -it {lab.n('probe')} psql '{lab.pg_url()}'
  Patroni:                        docker exec {lab.n('pg-a')} patronictl -c /etc/patroni.yml list
  Valkey, through a Sentinel:     docker exec -it {lab.n('kvprobe')} valkey-cli -h sentinel-a -p 26379
  Ctrl-C takes it all down.""", flush=True)
    await asyncio.Event().wait()


SCENARIOS = {"pg": scenario_pg, "valkey": scenario_valkey, "door": scenario_door, "machine": scenario_machine,
             "relay-pg": scenario_relay_pg, "spread": scenario_spread, "collab": scenario_collab, "up": scenario_up}
DEFAULT = ([f"{s}:{h}" for s in ("pg", "valkey", "door", "machine") for h in HOW] + ["spread", "spread:kill", "spread:pause", "relay-pg:kill"]
           + [f"collab:{h}:relay-a" for h in HOW] + ["collab:kill:valkey", "collab:kill:pg", "collab:kill:a", "collab:cut:a"])


def build_images() -> bool:
    """The Patroni image, and the relay's from this checkout; whether the relay takes a bus setting yet."""
    docker("build", "-q", "--label", LABEL, "-t", PATRONI_IMAGE, "-f", str(HERE / "Dockerfile.patroni"), str(HERE), timeout=900)
    with tempfile.TemporaryDirectory(prefix="texloom-rehearsal-relays-") as t:
        ctx = Path(t)
        for f in ("pyproject.toml", "README.md"):
            shutil.copy(ROOT / f, ctx / f)
        for d in ("server", "texloom"):
            shutil.copytree(ROOT / d, ctx / d, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(HERE / "Dockerfile.relay", ctx / "Dockerfile")
        docker("build", "-q", "--label", LABEL, "-t", RELAY_IMAGE, str(ctx), timeout=900)
    return "--bus" in docker("run", "--rm", "--label", LABEL, RELAY_IMAGE, "--help", check_=False).stdout


def remove_images() -> None:
    docker("rmi", "-f", PATRONI_IMAGE, RELAY_IMAGE, check_=False)


def remove_everything() -> None:
    """Everything any rehearsal of relays made, found by its label."""
    ids = docker("ps", "-aq", "--filter", f"label={LABEL}", check_=False).stdout.split()
    if ids:
        docker("rm", "-f", "-v", *ids, check_=False, timeout=180)
    nets = docker("network", "ls", "-q", "--filter", f"label={LABEL}", check_=False).stdout.split()
    if nets:
        docker("network", "rm", *nets, check_=False)
    images = docker("images", "-q", "--filter", f"label={LABEL}", check_=False).stdout.split()
    if images:
        docker("rmi", "-f", *set(images), check_=False)
    print(f"removed {len(ids)} containers, {len(nets)} networks and {len(set(images))} images", flush=True)


def print_times() -> None:
    if not TIMES:
        return
    width = max(len(w) for _, w, _ in TIMES)
    print("\ntimes (seconds from the break, or from the mend for what follows it):")
    last = None
    for scene, what, secs in TIMES:
        if scene != last:
            print(f"  {scene}")
            last = scene
        print(f"    {what:<{width}}  " + (f"{secs:6.1f}" if secs is not None else "     -"))


async def main() -> int:
    if not shutil.which("docker") or docker("version", "--format", "{{.Server.Version}}", check_=False).returncode != 0:
        print("skipped: Docker isn't running")
        return 0
    names = sys.argv[1:] or DEFAULT
    if names == ["down"]:
        remove_everything()
        return 0
    runs = []
    for name in names:
        family, _, rest = name.partition(":")
        how, _, target = rest.partition(":")
        how = how or ("" if family in ("spread", "up") else "kill")
        if family not in SCENARIOS or (how and how not in HOW):
            sys.exit(f"no such scenario: {name} (have {', '.join(SCENARIOS)}, then :kill, :pause or :cut, and for machine :<machine>)")
        runs.append((name, family, how, target))
    print("building the images (Patroni's, and the relay's from this checkout)…", flush=True)
    bus = await asyncio.to_thread(build_images)
    print("the relays " + ("take a bus setting: they are given the Sentinels' address" if bus else "don't take a bus setting yet"), flush=True)
    for name, family, how, target in runs:
        SCENE[0] = name
        print(f"\n== {name}", flush=True)
        with tempfile.TemporaryDirectory(prefix=f"texloom-rehearsal-relays-{family}-") as t:
            lab = Lab(Path(t), relays=1 if family == "relay-pg" else RELAYS)
            lab.bus = bus
            try:
                start = time.monotonic()
                await asyncio.to_thread(lab.up)
                k = {r: len(lab.of(r)) for r in ("etcd", "pg", "valkey", "sentinel", "relay")}
                check(f"the lab is up ({time.monotonic() - start:.0f} s): {k['etcd']} etcd members, Patroni's primary and {k['pg'] - 1} "
                      f"standbys, Valkey's primary, {k['valkey'] - 1} replicas and {k['sentinel']} Sentinels, the bucket, the front door "
                      f"and {k['relay']} relay{'s' * (k['relay'] != 1)}", True)
                check("the bucket keeps a file", lab.s3("PUT", "/bucket/hello", "hello") == 200 and lab.s3_get("/bucket/hello") == "hello")
                await SCENARIOS[family](lab, how, target)
            except Exception as e:
                check(f"{name}: ran to the end", False, f"{type(e).__name__}: {e}")
                for c in [*lab.of("relay"), "door"]:
                    print(f"--- {c}:\n{lab.logs(c, 20)}", flush=True)
            finally:
                if os.environ.get("REHEARSE_LOGS"):
                    for r in lab.of("relay"):
                        print(f"--- {r}:\n{lab.logs(r, 60)}", flush=True)
                await asyncio.to_thread(lab.down)
    print_times()
    return summary("relays rehearsal")


def run() -> int:
    # Ctrl-C, and a plain kill, take the lab down (even when started where Ctrl-C was ignored, as in the background)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, lambda *_: os.kill(os.getpid(), signal.SIGINT))
    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped: taking the lab down", flush=True)
        print_times()
        return 130
    finally:
        for lab in list(LABS):
            lab.down()
        remove_images()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(run())
