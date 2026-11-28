#!/usr/bin/env python3
"""Set up TeXLoom on several machines from one settings file (FUTURE_ARCHITECTURE.md, "Deploying to machines").

    deploy/cluster.py plan   cluster.toml     # what would change (changes nothing)
    deploy/cluster.py apply  cluster.toml     # make it so; run again whenever the file (or this checkout) changes
    deploy/cluster.py status cluster.toml     # every part's health

deploy/cluster.example.toml says what the file holds. One machine needs none of this: deploy/install.sh sets it up.

Each machine runs Debian 13 and is reached over SSH as root (as install.sh does). The tool puts the machines on a
private WireGuard network of their own, and every part listens only there (or on localhost): PostgreSQL kept
available by Patroni, with etcd; Valkey, with Sentinels to replace its primary; the relays; and nginx on each relay's
machine as the front door, sending to every relay that is well (deploy/door-watch.py). A relay's machine is set up
by install.sh itself, with the settings this writes. The machines' firewalls let TeXLoom's own ports be reached only
through WireGuard ("ports", the default, which leaves the rest alone, so other services on a machine are unaffected);
"strict" lets in nothing but SSH, HTTP(S) and WireGuard.

Secrets (database, Valkey and admin passwords) are made the first time and kept in a folder `secrets/` beside the
settings file, never in it: keep that folder private (it is all that's needed to reach the parts).

What it doesn't do yet: change etcd's members once they have started (adding a database machine later), and TLS with
several doors (each needs the certificate: run with `certbot = false` and see FUTURE_ARCHITECTURE.md).
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import ipaddress
import json
import os
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PARTS = ("relay", "door", "postgresql", "etcd", "valkey", "sentinel")
WG = "texloom"  # the WireGuard interface's name
PG_VERSION = "17"  # Debian 13's
PACKAGES = {  # what each part needs, on Debian 13 (the relay's own are install.sh's)
    "base": ["wireguard-tools", "nftables", "curl", "python3", "rsync", "iputils-ping"],
    "etcd": ["etcd-server", "etcd-client"],
    "postgresql": [f"postgresql-{PG_VERSION}", "patroni", "python3-etcd"],  # (Debian's patroni can use several stores: etcd's client is ours to ask for)
    "valkey": ["valkey-server", "valkey-tools"],
    "sentinel": ["valkey-sentinel", "valkey-tools"],
    "door": [],
    "relay": [],
}
# TeXLoom's own ports, reached only through WireGuard: relay, PostgreSQL, Patroni, etcd (clients, peers), Valkey, Sentinel
PORTS = (8787, 5432, 8008, 2379, 2380, 6379, 26379)
SECRETS = ("pg-superuser", "pg-replicator", "pg-texloom", "valkey-admin", "valkey-relay", "valkey-sentinel",
           "valkey-replica", "admin-token")
# what the relays may send to Valkey (server/bus.py's COMMANDS), and what Sentinels and replicas need
RELAY_COMMANDS = ("hello", "ping", "publish", "subscribe", "unsubscribe", "get", "set", "del", "incrby", "pexpire",
                  "hset", "hget", "hdel", "hgetall", "hlen", "multi", "exec")
SENTINEL_ACL = ("+client +subscribe +publish +ping +info +multi +slaveof +replicaof +config +exec +role "
                "&__sentinel__:hello")
REPLICA_ACL = "+psync +replconf +ping"


class Problem(Exception):
    """Something in the settings, or on a machine, that stops the tool (said plainly)."""


def say(*a: object) -> None:
    print(*a, flush=True)


# --- the settings file -----------------------------------------------------------------------------------------------

@dataclass
class Machine:
    name: str
    address: str  # where the other machines reach it (WireGuard), and SSH too unless `ssh` says otherwise
    ssh: str  # where SSH reaches it: host or host:port (default: address)
    runs: tuple[str, ...]
    relay: dict = field(default_factory=dict)  # its relay's settings, over [relay]
    wg: str = ""  # its WireGuard address (kept from the machine, or given out by the tool)
    pubkey: str = ""
    facts: dict = field(default_factory=dict)

    def has(self, part: str) -> bool:
        return part in self.runs


@dataclass
class Cluster:
    path: Path
    name: str  # the public name (nginx's site; the certificate)
    machines: list[Machine]
    ssh_user: str
    ssh_opts: list[str]
    subnet: ipaddress.IPv4Network
    wg_port: int
    firewall: str
    certbot: bool
    bucket: dict
    relay_settings: dict
    move_in: str = ""  # a machine install.sh set up, whose server, data and codes move into the cluster
    secrets: dict[str, str] = field(default_factory=dict)

    def of(self, part: str) -> list[Machine]:
        return [m for m in self.machines if m.has(part)]

    @property
    def secrets_dir(self) -> Path:
        return self.path.parent / "secrets"


def load(path: Path) -> Cluster:
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise Problem(f"{path}: {e}") from None
    known = {"name", "ssh", "network", "machines", "bucket", "relay", "tls", "move_in"}
    if extra := set(raw) - known:
        raise Problem(f"{path}: unknown setting(s) {sorted(extra)}")
    name = raw.get("name") or ""
    if not name:
        raise Problem(f"{path}: `name` (the public name people reach, e.g. app.example.org) is needed")
    ssh = raw.get("ssh", {})
    opts = list(ssh.get("options", []))
    if key := ssh.get("key"):
        opts += ["-i", str(Path(key).expanduser())]
    net = raw.get("network", {})
    machines = []
    for mname, m in (raw.get("machines") or {}).items():
        runs = tuple(m.get("runs", []))
        if bad := [p for p in runs if p not in PARTS]:
            raise Problem(f"machine {mname}: unknown part(s) {bad}: each is one of {', '.join(PARTS)}")
        if not m.get("address"):
            raise Problem(f"machine {mname}: `address` (where the other machines, and SSH, reach it) is needed")
        if extra := set(m) - {"address", "ssh", "runs", "wg", "relay"}:
            raise Problem(f"machine {mname}: unknown setting(s) {sorted(extra)}")
        machines.append(Machine(mname, m["address"], m.get("ssh", m["address"]), runs, m.get("relay", {}), m.get("wg", "")))
    c = Cluster(path=path, name=name, machines=machines, ssh_user=ssh.get("user", "root"), ssh_opts=opts,
                subnet=ipaddress.ip_network(net.get("subnet", "10.77.0.0/24")), wg_port=int(net.get("port", 51820)),
                firewall=net.get("firewall", "ports"), certbot=bool(raw.get("tls", {}).get("certbot", True)),
                bucket=raw.get("bucket", {}), relay_settings=raw.get("relay", {}), move_in=raw.get("move_in", ""))
    check(c)
    return c


def check(c: Cluster) -> None:
    """The settings make a cluster that can work, or a Problem saying why not."""
    if not c.machines:
        raise Problem("no machines")
    if c.firewall not in ("ports", "strict", "off"):
        raise Problem("network.firewall is ports, strict or off")
    if not c.of("relay"):
        raise Problem("no machine runs a relay")
    if not c.of("door"):
        raise Problem("no machine runs the front door (`door`)")
    for m in c.of("door"):
        if not m.has("relay"):
            raise Problem(f"machine {m.name}: a door runs on a relay's machine (install.sh sets up both)")
    if not c.of("postgresql"):
        raise Problem("no machine runs PostgreSQL")
    if not c.of("etcd"):
        raise Problem("PostgreSQL is kept available by Patroni, which needs etcd on some machines (three, ideally)")
    if len(c.of("etcd")) % 2 == 0:
        raise Problem(f"{len(c.of('etcd'))} etcd members: an odd number (1, 3, 5) keeps a majority as well with one less")
    if len(c.of("relay")) > 1 and not (c.of("valkey") and c.of("sentinel")):
        raise Problem("several relays pass messages through Valkey: some machines must run `valkey` and `sentinel`")
    if c.of("valkey") and not c.of("sentinel"):
        raise Problem("Valkey is found through its Sentinels: some machines must run `sentinel` (three, ideally)")
    if len(c.of("relay")) > 1 and not c.bucket.get("url", "").startswith("s3://"):
        raise Problem("several relays share stored files through a bucket: [bucket] url = \"s3://…\" (and its endpoint, key)")
    names = [m.name for m in c.machines]
    if len(set(names)) != len(names):
        raise Problem("two machines have one name")
    if c.move_in and c.move_in not in [m.name for m in c.of("relay")]:
        raise Problem(f"move_in = {c.move_in!r}: name the machine (one of [machines]) that runs the server moving in, "
                      "with `relay` (and usually `door`) among what it runs")


def warnings(c: Cluster) -> list[str]:
    out = []
    if len(c.of("etcd")) < 3:
        out.append(f"{len(c.of('etcd'))} etcd member(s): with fewer than three, losing one stops the database failing over")
    if c.of("sentinel") and len(c.of("sentinel")) < 3:
        out.append(f"{len(c.of('sentinel'))} Sentinel(s): with fewer than three, losing one stops Valkey failing over")
    if len(c.of("postgresql")) < 2:
        out.append("one PostgreSQL: nothing to fail over to")
    return out


def load_secrets(c: Cluster, make: bool) -> None:
    """The cluster's secrets, from secrets/ beside the settings (made there the first time, if `make`)."""
    d = c.secrets_dir
    for name in SECRETS:
        f = d / name
        if f.exists():
            c.secrets[name] = f.read_text().strip()
        elif make:
            d.mkdir(mode=0o700, exist_ok=True)
            value = secrets.token_urlsafe(24)
            f.write_text(value + "\n")
            f.chmod(0o600)
            c.secrets[name] = value
        else:
            c.secrets[name] = "(made at apply)"
    if sf := c.bucket.get("secret_file"):
        p = (c.path.parent / sf).expanduser()
        try:
            c.secrets["s3"] = p.read_text().strip()
        except OSError as e:
            raise Problem(f"[bucket] secret_file {p}: {e.strerror}") from None


# --- talking to a machine --------------------------------------------------------------------------------------------

class Remote:
    """A machine, over SSH (one connection, kept open: ControlMaster)."""

    def __init__(self, c: Cluster, m: Machine, control: Path):
        self.m = m
        host, _, port = m.ssh.rpartition(":") if m.ssh.count(":") == 1 else (m.ssh, "", "")
        self.target = f"{c.ssh_user}@{host}"
        self.root = c.ssh_user == "root"
        self.opts = [*c.ssh_opts, *(["-p", port] if port else []), "-o", "BatchMode=yes", "-o", "ControlMaster=auto",
                     "-o", f"ControlPath={control}/%C", "-o", "ControlPersist=120", "-o", "ServerAliveInterval=15"]

    def run(self, script: str, check: bool = True, timeout: float = 1800) -> str:
        """Run a bash script there (as root), returning what it printed."""
        cmd = ["ssh", *self.opts, "-o", "ConnectTimeout=10", self.target, "bash -s" if self.root else "sudo bash -s"]
        p = subprocess.run(cmd, input="set -euo pipefail\nexport DEBIAN_FRONTEND=noninteractive\n" + script,
                           capture_output=True, text=True, timeout=timeout)
        if check and p.returncode != 0:
            raise Problem(f"{self.m.name}: {(p.stderr or p.stdout).strip()[-1500:]}")
        return p.stdout


@dataclass
class File:
    """A file the tool keeps on a machine. `once`: written only if it isn't there (the part changes it itself)."""
    path: str
    content: str
    mode: str = "644"
    owner: str = "root:root"
    once: bool = False

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


def put_script(files: list[File]) -> str:
    """A script writing `files` (those marked once only if they aren't there)."""
    out = []
    for f in files:
        b64 = base64.b64encode(f.content.encode()).decode()
        user, group = f.owner.split(":")
        write = (f"install -d -m 755 {shlex.quote(str(Path(f.path).parent))}\n"
                 f"t=$(mktemp) && echo {b64} | base64 -d > \"$t\" && "
                 f"install -m {f.mode} -o {user} -g {group} \"$t\" {shlex.quote(f.path)} && rm -f \"$t\"\n")
        out.append(f"if [ ! -e {shlex.quote(f.path)} ]; then\n{write}fi\n" if f.once else write)
    return "".join(out)


def shas_script(paths: list[str]) -> str:
    return "".join(f"[ -e {shlex.quote(p)} ] && echo \"{p} $(sha256sum < {shlex.quote(p)} | cut -d' ' -f1)\" || echo \"{p} -\"\n"
                   for p in paths)


def parallel(fn, items) -> list:
    """fn(item) for each item at once; the first Problem raised is raised here (after all have finished)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(items))) as ex:
        futures = [ex.submit(fn, i) for i in items]
        results, problems = [], []
        for f in futures:
            try:
                results.append(f.result())
            except Problem as e:
                problems.append(str(e))
                results.append(None)
    if problems:
        raise Problem("\n".join(problems))
    return results


# --- what each machine should have -----------------------------------------------------------------------------------

FACTS = r"""
. /etc/os-release
echo "os=$ID $VERSION_ID"
echo "hostname=$(hostname)"
if [ -f /etc/wireguard/texloom.key ]; then echo "pubkey=$(wg pubkey < /etc/wireguard/texloom.key)"; fi
if [ -f /etc/wireguard/texloom.conf ]; then echo "wg=$(sed -n 's#^Address = \([0-9.]*\)/.*#\1#p' /etc/wireguard/texloom.conf)"; fi
echo "etcd_data=$([ -d /var/lib/etcd/texloom/member ] && echo yes || echo no)"
echo "pg_data=$([ -f /var/lib/postgresql/texloom/PG_VERSION ] && echo yes || echo no)"
echo "valkey_conf=$([ -f /etc/valkey/texloom-valkey.conf ] && echo yes || echo no)"
echo "sentinel_conf=$([ -f /etc/valkey/texloom-sentinel.conf ] && echo yes || echo no)"
echo "installed=$(dpkg-query -W -f='${Package} ${db:Status-Status}\n' 2>/dev/null | awk '$2=="installed"{print $1}' | tr '\n' ' ')"
echo "ssh_port=$(sshd -T 2>/dev/null | awk '$1=="port"{print $2; exit}')"
if [ -d /var/lib/postgresql ] && ls /etc/postgresql/*/* >/dev/null 2>&1; then echo "pg_clusters=$(ls -d /etc/postgresql/*/* | tr '\n' ' ')"; fi
"""


def facts(c: Cluster, r: Remote) -> dict:
    out = {}
    for line in r.run(FACTS).splitlines():
        k, _, v = line.partition("=")
        out[k] = v.strip()
    out["installed"] = set(out.get("installed", "").split())
    if out.get("os") != "debian 13":
        raise Problem(f"{r.m.name}: runs {out.get('os') or 'an unknown system'}; the tool knows Debian 13")
    if r.m.has("postgresql") and out.get("pg_clusters"):
        raise Problem(f"{r.m.name}: already has PostgreSQL cluster(s) of its own ({out['pg_clusters'].strip()}): "
                      "Patroni runs PostgreSQL here, so use a machine without one (or remove them first)")
    return out


def give_wg_addresses(c: Cluster) -> None:
    """Each machine keeps the WireGuard address it has; one without gets the next free one in the subnet."""
    taken = {m.facts.get("wg") or m.wg for m in c.machines} - {""}
    free = (str(h) for h in c.subnet.hosts() if str(h) not in taken)
    for m in c.machines:
        m.wg = m.facts.get("wg") or m.wg or next(free)
        if ipaddress.ip_address(m.wg) not in c.subnet:
            raise Problem(f"machine {m.name}: its WireGuard address {m.wg} isn't in {c.subnet}")


def packages(m: Machine) -> list[str]:
    want = list(PACKAGES["base"])
    for p in m.runs:
        want += [x for x in PACKAGES[p] if x not in want]
    return want


def wg_conf(c: Cluster, m: Machine) -> File:
    peers = "".join(f"\n[Peer]\n# {o.name}\nPublicKey = {o.pubkey}\nEndpoint = {o.address}:{c.wg_port}\n"
                    f"AllowedIPs = {o.wg}/32\nPersistentKeepalive = 25\n" for o in c.machines if o is not m)
    return File(f"/etc/wireguard/{WG}.conf",
                f"# Written by deploy/cluster.py: the cluster's private network. Changes here are replaced.\n[Interface]\n"
                f"Address = {m.wg}/{c.subnet.prefixlen}\nListenPort = {c.wg_port}\nPostUp = wg set %i private-key /etc/wireguard/{WG}.key\n"
                + peers, "600")


def firewall(c: Cluster, m: Machine) -> File:
    ports = ", ".join(map(str, PORTS))
    if c.firewall == "strict":
        ssh = m.facts.get("ssh_port") or "22"
        chain = (f"        type filter hook input priority 0; policy drop;\n"
                 f"        ct state established,related accept\n        iif lo accept\n        iifname \"{WG}\" accept\n"
                 f"        meta l4proto {{ icmp, ipv6-icmp }} accept\n        tcp dport {{ {ssh}, 80, 443 }} accept\n"
                 f"        udp dport {c.wg_port} accept\n")
    else:  # ports: only TeXLoom's own ports are closed to everything but WireGuard and this machine
        chain = (f"        type filter hook input priority 0; policy accept;\n"
                 f"        iif lo accept\n        iifname \"{WG}\" accept\n        tcp dport {{ {ports} }} drop\n")
    return File("/etc/texloom/firewall.nft",
                "# Written by deploy/cluster.py (network.firewall = \"%s\"). Only this table is TeXLoom's: others are left alone.\n"
                "table inet texloom\ndelete table inet texloom\ntable inet texloom {\n    chain input {\n%s    }\n}\n"
                % (c.firewall, chain))


FIREWALL_UNIT = """[Unit]
Description=TeXLoom's firewall table (deploy/cluster.py)
Before=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/texloom/firewall.nft
ExecStop=/usr/sbin/nft delete table inet texloom

[Install]
WantedBy=multi-user.target
"""


def etcd_conf(c: Cluster, m: Machine, state: str, members: list[Machine] | None = None) -> File:
    """`members`: the cluster as this member joins it (default: every etcd machine). Once started, etcd keeps its
    own list (ETCD_INITIAL_* are read only the first time)."""
    cluster = ",".join(f"{e.name}=http://{e.wg}:2380" for e in (members or c.of("etcd")))
    return File("/etc/default/etcd", "\n".join([
        "# Written by deploy/cluster.py: one of the etcd members Patroni keeps its leader key in. Changes here are replaced.",
        f"ETCD_NAME={m.name}", "ETCD_DATA_DIR=/var/lib/etcd/texloom",
        f"ETCD_LISTEN_PEER_URLS=http://{m.wg}:2380", f"ETCD_LISTEN_CLIENT_URLS=http://{m.wg}:2379,http://127.0.0.1:2379",
        f"ETCD_INITIAL_ADVERTISE_PEER_URLS=http://{m.wg}:2380", f"ETCD_ADVERTISE_CLIENT_URLS=http://{m.wg}:2379",
        f"ETCD_INITIAL_CLUSTER={cluster}", f"ETCD_INITIAL_CLUSTER_STATE={state}", "ETCD_INITIAL_CLUSTER_TOKEN=texloom", ""]))


def patroni_conf(c: Cluster, m: Machine) -> File:
    s = c.secrets
    etcd = json.dumps([f"{e.wg}:2379" for e in c.of("etcd")])
    net = str(c.subnet)
    return File("/etc/patroni/config.yml", f"""# Written by deploy/cluster.py: this machine's part of the PostgreSQL that Patroni keeps available.
# Changes here are replaced. (Settings shared by the cluster, under bootstrap.dcs, are kept in etcd once it started.)
scope: texloom
namespace: /service/
name: {m.name}

restapi:
  listen: {m.wg}:8008
  connect_address: {m.wg}:8008

etcd3:
  hosts: {etcd}

bootstrap:
  dcs:
    ttl: 30
    loop_wait: 10
    retry_timeout: 10
    maximum_lag_on_failover: 1048576
    postgresql:
      use_pg_rewind: true
      parameters:
        wal_level: replica
        hot_standby: "on"
        wal_log_hints: "on"
        max_wal_senders: 10
        max_replication_slots: 10
        max_connections: 200
        password_encryption: scram-sha-256
  initdb:
    - encoding: UTF8
    - data-checksums

postgresql:
  listen: {m.wg},127.0.0.1:5432
  connect_address: {m.wg}:5432
  data_dir: /var/lib/postgresql/texloom
  bin_dir: /usr/lib/postgresql/{PG_VERSION}/bin
  pgpass: /var/lib/postgresql/.pgpass-patroni
  authentication:
    superuser:
      username: postgres
      password: "{s['pg-superuser']}"
    replication:
      username: replicator
      password: "{s['pg-replicator']}"
    rewind:
      username: postgres
      password: "{s['pg-superuser']}"
  parameters:
    unix_socket_directories: /var/run/postgresql
  pg_hba:
    - local all all peer
    - host all all 127.0.0.1/32 scram-sha-256
    - host replication replicator {net} scram-sha-256
    - host all all {net} scram-sha-256
""", "600", "postgres:postgres")


PATRONI_UNIT = """[Unit]
Description=PostgreSQL kept available by Patroni (deploy/cluster.py)
After=network-online.target etcd.service wg-quick@texloom.service
Wants=network-online.target

[Service]
Type=simple
User=postgres
Group=postgres
ExecStart=/usr/bin/patroni /etc/patroni/config.yml
ExecReload=/bin/kill -HUP $MAINPID
KillMode=process
TimeoutSec=30
Restart=on-failure

[Install]
WantedBy=multi-user.target
"""


def valkey_users(c: Cluster) -> File:
    s = c.secrets
    relay = " ".join(f"+{x}" for x in RELAY_COMMANDS)
    return File("/etc/valkey/texloom-users.acl", "\n".join([
        "user default off",
        f"user admin on >{s['valkey-admin']} ~* &* +@all",
        f"user relay on >{s['valkey-relay']} resetkeys ~texloom:* resetchannels &texloom:* -@all {relay}",
        f"user sentinel on >{s['valkey-sentinel']} -@all {SENTINEL_ACL}",
        f"user replica on >{s['valkey-replica']} -@all {REPLICA_ACL}", ""]), "640", "valkey:valkey")


def valkey_conf(c: Cluster, m: Machine, primary: Machine | None) -> File:
    lines = [
        "# Written once by deploy/cluster.py; Valkey and its Sentinels change it afterwards (replicaof, at a failover).",
        f"bind {m.wg} 127.0.0.1", "port 6379", "protected-mode no", "aclfile /etc/valkey/texloom-users.acl",
        "masteruser replica", f"masterauth {c.secrets['valkey-replica']}", f"replica-announce-ip {m.wg}",
        'save ""', "appendonly no", "dir /var/lib/valkey", "maxmemory 256mb", "maxmemory-policy volatile-ttl"]
    if primary is not None and primary is not m:
        lines.append(f"replicaof {primary.wg} 6379")
    return File("/etc/valkey/texloom-valkey.conf", "\n".join(lines) + "\n", "640", "valkey:valkey", once=True)


def sentinel_conf(c: Cluster, m: Machine, primary: Machine) -> File:
    quorum = len(c.of("sentinel")) // 2 + 1
    return File("/etc/valkey/texloom-sentinel.conf", "\n".join([
        "# Written once by deploy/cluster.py; the Sentinel keeps what it learns here.",
        f"bind {m.wg} 127.0.0.1", "port 26379", "protected-mode no", f"sentinel announce-ip {m.wg}",
        "dir /var/lib/valkey",
        f"sentinel monitor texloom {primary.wg} 6379 {quorum}",
        "sentinel auth-user texloom sentinel", f"sentinel auth-pass texloom {c.secrets['valkey-sentinel']}",
        "sentinel down-after-milliseconds texloom 5000", "sentinel failover-timeout texloom 60000",
        "sentinel parallel-syncs texloom 1", ""]), "640", "valkey:valkey", once=True)


def valkey_unit(kind: str) -> str:
    binary, conf = ("valkey-server", "texloom-valkey.conf") if kind == "valkey" else ("valkey-sentinel", "texloom-sentinel.conf")
    return f"""[Unit]
Description=TeXLoom's {'Valkey' if kind == 'valkey' else 'Valkey Sentinel'} (deploy/cluster.py)
After=network-online.target wg-quick@texloom.service
Wants=network-online.target

[Service]
Type=notify
User=valkey
Group=valkey
ExecStart=/usr/bin/{binary} /etc/valkey/{conf} --supervised systemd --daemonize no
Restart=always
LimitNOFILE=10032
ReadWritePaths=/var/lib/valkey /etc/valkey
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""


def example_settings() -> dict:
    """texloom.example.toml's settings (those it sets, not those it only mentions): every relay's starting point."""
    return {k: v for k, v in tomllib.loads((ROOT / "texloom.example.toml").read_text()).items() if not isinstance(v, dict)}


def relay_settings(c: Cluster, m: Machine) -> File:
    """The relay's settings: texloom.example.toml's (as install.sh starts from), then the cluster's own, then [relay]
    and the machine's [machines.NAME.relay]."""
    s = c.secrets
    ours = {"database": database_url(c), "host": m.wg, "port": 8787, "admin_token_file": "/etc/texloom/admin-token"}
    lines = ["# Written by deploy/cluster.py from the cluster's settings ([relay] there): changes here are replaced."]
    if c.of("valkey"):
        bus = (f"redis+sentinel://relay:{s['valkey-relay']}@" + ",".join(f"{x.wg}:26379" for x in c.of("sentinel"))
               + "/texloom")
        ours["bus"] = bus
    example = example_settings()
    for k, v in {**c.relay_settings, **m.relay}.items():
        if k in (*ours, "bus", "files", "admin_token") or isinstance(v, dict):
            raise Problem(f"[relay] {k}: the tool sets this itself (or, for the bucket, [bucket])")
    for k, v in {**example, **ours, **c.relay_settings, **m.relay}.items():
        lines.append(f"{k} = {json.dumps(v)}")
    b = c.bucket
    if b.get("url"):
        lines.append(f"files = {json.dumps(b['url'])}")
        lines += ["", "[s3]"] + [f"{k} = {json.dumps(b[k])}" for k in ("endpoint", "region", "key_id") if b.get(k)]
        if s.get("s3"):
            lines.append(f"secret = {json.dumps(s['s3'])}")
    return File("/etc/texloom/texloom.toml", "\n".join(lines) + "\n", "640", "root:root")


DOOR_UNIT = """[Unit]
Description=TeXLoom's front door: nginx sends only to relays that are well (deploy/door-watch.py)
After=nginx.service wg-quick@texloom.service
Wants=nginx.service

[Service]
ExecStart=/usr/bin/python3 /usr/local/lib/texloom/door-watch.py
Restart=always

[Install]
WantedBy=multi-user.target
"""


# --- the steps -------------------------------------------------------------------------------------------------------

def connect(c: Cluster, control: Path, tolerate: bool = False) -> dict[str, Remote]:
    """Every machine's facts. `tolerate`: one that can't be reached is noted (facts["unreachable"]), not a Problem."""
    remotes = {m.name: Remote(c, m, control) for m in c.machines}

    def get(m: Machine) -> None:
        try:
            m.facts = facts(c, remotes[m.name])
        except (Problem, subprocess.TimeoutExpired) as e:
            if not tolerate:
                raise
            m.facts = {"unreachable": str(e).splitlines()[-1][:200], "installed": set()}
    parallel(get, c.machines)
    give_wg_addresses(c)
    return remotes


def step_base(c: Cluster, r: Remote) -> None:
    """Packages (with no service started by installing it), and this machine's WireGuard key."""
    m = r.m
    missing = [p for p in packages(m) if p not in m.facts["installed"]]
    script = ""
    if missing:
        say(f"  {m.name}: installing {' '.join(missing)}")
        script += """
mkdir -p /etc/postgresql-common
grep -qs '^create_main_cluster' /etc/postgresql-common/createcluster.conf || echo 'create_main_cluster = false' >> /etc/postgresql-common/createcluster.conf
printf '#!/bin/sh\\nexit 101\\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d
trap 'rm -f /usr/sbin/policy-rc.d' EXIT
apt-get update -qq
apt-get install -y -qq -o Dpkg::Options::=--force-confold %s >/dev/null
# (the packages' own services: TeXLoom's are its own units)
for s in etcd valkey-server valkey-sentinel patroni; do systemctl disable -q --now $s 2>/dev/null || true; done
""" % " ".join(missing)
    script += f"""
install -d -m 700 /etc/wireguard
[ -f /etc/wireguard/{WG}.key ] || (umask 077 && wg genkey > /etc/wireguard/{WG}.key)
echo "pubkey=$(wg pubkey < /etc/wireguard/{WG}.key)"
"""
    for line in r.run(script).splitlines():
        if line.startswith("pubkey="):
            m.pubkey = line.split("=", 1)[1]


def step_network(c: Cluster, r: Remote) -> None:
    m = r.m
    files = [wg_conf(c, m)]
    if c.firewall != "off":
        files += [firewall(c, m), File("/etc/systemd/system/texloom-firewall.service", FIREWALL_UNIT)]
    r.run(put_script(files) + f"""
systemctl daemon-reload
if systemctl is-active -q wg-quick@{WG}; then
  wg syncconf {WG} <(wg-quick strip {WG} | sed "/^.Interface.$/a PrivateKey = $(cat /etc/wireguard/{WG}.key)")
else systemctl enable -q --now wg-quick@{WG}; fi
""" + ("systemctl enable -q texloom-firewall && systemctl restart texloom-firewall\n" if c.firewall != "off" else
       "systemctl disable -q --now texloom-firewall 2>/dev/null || true\n"))


def wait_network(c: Cluster, r: Remote) -> None:
    """Every other machine answers through WireGuard."""
    others = " ".join(o.wg for o in c.machines if o is not r.m)
    if not others:
        return
    r.run(f"""
for i in $(seq 60); do
  ok=1; for h in {others}; do ping -c1 -W1 $h >/dev/null 2>&1 || ok=0; done
  [ $ok = 1 ] && exit 0; sleep 1
done
echo "can't reach every machine through WireGuard ({others}): check that UDP port {c.wg_port} is open between them" >&2; exit 1
""")


def step_etcd(c: Cluster, r: Remote, state: str, members: list[Machine] | None = None) -> None:
    conf = etcd_conf(c, r.m, state, members)
    if r.run(shas_script([conf.path])).split()[-1] == conf.sha and r.run("systemctl is-active etcd || true").strip() == "active":
        return  # (as it is: a restart would only cost the cluster a member for a moment)
    r.run(put_script([conf]) + """
install -d -o etcd -g etcd -m 700 /var/lib/etcd/texloom
systemctl enable -q etcd
systemctl restart etcd --no-block
""")


def etcd_members(r: Remote) -> list[dict]:
    """etcd's members as one of them sees them: id (hex, as etcdctl takes it), name, peer address."""
    out = json.loads(r.run("etcdctl --endpoints=http://127.0.0.1:2379 member list -w json"))
    return [{"id": format(x["ID"], "x"), "name": x.get("name", ""), "peer": (x.get("peerURLs") or [""])[0]}
            for x in out.get("members", [])]


def grow_etcd(c: Cluster, remotes: dict[str, Remote], started: list[Machine]) -> None:
    """etcd's members made those of the settings: those gone taken out, new ones added one at a time."""
    live = remotes[started[0].name]
    wanted = {m.name: m for m in c.of("etcd")}
    for x in etcd_members(live):
        if x["name"] not in wanted or f"http://{wanted[x['name']].wg}:2380" != x["peer"]:
            say(f"  etcd: taking out {x['name'] or x['peer']} (no longer in the settings)")
            live.run(f"etcdctl --endpoints=http://127.0.0.1:2379 member remove {x['id']}")
    members = list(started)
    for m in c.of("etcd"):
        if m in started:
            continue
        say(f"  etcd: adding {m.name}")
        live.run(f"etcdctl --endpoints=http://127.0.0.1:2379 member add {m.name} --peer-urls=http://{m.wg}:2380")
        members.append(m)
        step_etcd(c, remotes[m.name], "existing", members)
        wait_etcd(c, remotes[m.name])


def wait_etcd(c: Cluster, r: Remote) -> None:
    r.run("""
for i in $(seq 90); do etcdctl --endpoints=http://127.0.0.1:2379 endpoint health >/dev/null 2>&1 && exit 0; sleep 1; done
echo "etcd didn't become healthy: journalctl -u etcd" >&2; exit 1
""")


def step_patroni(c: Cluster, r: Remote) -> None:
    r.run(put_script([patroni_conf(c, r.m), File("/etc/systemd/system/texloom-patroni.service", PATRONI_UNIT)]) + """
install -d -o postgres -g postgres -m 700 /var/lib/postgresql/texloom
systemctl daemon-reload
systemctl enable -q texloom-patroni
if systemctl is-active -q texloom-patroni; then systemctl reload texloom-patroni; else systemctl start texloom-patroni; fi
""")


def primary_pg(c: Cluster, r: Remote, wait: float = 180) -> Machine:
    """The machine whose PostgreSQL is the primary (asked of Patroni)."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait:
        out = r.run(f"curl -s --max-time 3 http://{r.m.wg}:8008/cluster || true")
        try:
            members = json.loads(out).get("members", [])
        except ValueError:
            members = []
        for x in members:
            if x.get("role") == "leader" and x.get("state") == "running":
                return next(m for m in c.of("postgresql") if m.name == x["name"])
        time.sleep(2)
    raise Problem("no PostgreSQL primary after waiting: journalctl -u texloom-patroni on the database machines")


def step_database(c: Cluster, r: Remote) -> None:
    """The relays' role and database, on the primary (idempotent)."""
    pw = c.secrets["pg-texloom"].replace("'", "''")
    r.run(f"""
cd /tmp
q() {{ runuser -u postgres -- psql -h /var/run/postgresql -qtAX -v ON_ERROR_STOP=1 "$@"; }}
for i in $(seq 60); do q -c 'SELECT 1' >/dev/null 2>&1 && break; sleep 1; done
[ "$(q -c "SELECT 1 FROM pg_roles WHERE rolname='texloom'")" = 1 ] || q -c "CREATE ROLE texloom LOGIN"
q -c "ALTER ROLE texloom PASSWORD '{pw}'" >/dev/null
[ "$(q -c "SELECT 1 FROM pg_database WHERE datname='texloom'")" = 1 ] || q -c "CREATE DATABASE texloom OWNER texloom"
""")


def current_valkey_primary(c: Cluster, remotes: dict[str, Remote]) -> Machine:
    """Where Valkey's primary is: what a Sentinel already running says, or else the first Valkey machine."""
    for m in c.of("sentinel"):
        if not m.facts.get("sentinel_conf") == "yes":
            continue
        out = remotes[m.name].run("valkey-cli -h 127.0.0.1 -p 26379 sentinel get-master-addr-by-name texloom 2>/dev/null || true")
        host = out.split()[0] if out.split() else ""
        for v in c.of("valkey"):
            if v.wg == host:
                return v
    return c.of("valkey")[0]


def step_valkey(c: Cluster, r: Remote, primary: Machine) -> None:
    acl = valkey_users(c)
    before = r.run(shas_script([acl.path])).split()[-1]
    r.run(put_script([acl, valkey_conf(c, r.m, primary), File("/etc/systemd/system/texloom-valkey.service", valkey_unit("valkey"))]) + f"""
systemctl daemon-reload
systemctl enable -q texloom-valkey
if systemctl is-active -q texloom-valkey; then
  {"valkey-cli -h 127.0.0.1 --user admin --pass " + shlex.quote(c.secrets['valkey-admin']) + " --no-auth-warning acl load >/dev/null" if before != acl.sha else ":"}
else systemctl start texloom-valkey; fi
""")


def step_sentinel(c: Cluster, r: Remote, primary: Machine) -> None:
    r.run(put_script([sentinel_conf(c, r.m, primary), File("/etc/systemd/system/texloom-sentinel.service", valkey_unit("sentinel"))]) + """
systemctl daemon-reload
systemctl enable -q texloom-sentinel
systemctl is-active -q texloom-sentinel || systemctl start texloom-sentinel
""")


SENTINEL_KNOWS = r"""
q() { valkey-cli -h 127.0.0.1 -p 26379 --json "$@"; }
echo "replicas=$(q sentinel replicas texloom | tr -d '\n')"
echo "sentinels=$(q sentinel sentinels texloom | tr -d '\n')"
echo "primary=$(q sentinel master texloom | tr -d '\n')"
"""


def _fields(x) -> dict:
    """A Sentinel's answer about one server, as a dict (valkey-cli --json gives a map, or a flat list of pairs)."""
    if isinstance(x, dict):
        return x
    return dict(zip(x[::2], x[1::2])) if isinstance(x, list) else {}


def sentinel_knows(r: Remote) -> tuple[set[str], set[str], dict]:
    out = dict(line.split("=", 1) for line in r.run(SENTINEL_KNOWS).splitlines() if "=" in line)
    replicas = {_fields(x).get("ip", "") for x in json.loads(out.get("replicas") or "[]")}
    sentinels = {_fields(x).get("ip", "") for x in json.loads(out.get("sentinels") or "[]")}
    return replicas, sentinels, _fields(json.loads(out.get("primary") or "[]"))


def tidy_sentinels(c: Cluster, remotes: dict[str, Remote]) -> None:
    """Each Sentinel's quorum made right for how many there are, and one that remembers a Valkey or Sentinel no longer
    in the settings told to forget it (SENTINEL RESET: one Sentinel at a time, waiting until it has found the rest
    again, so that they are never all relearning at once)."""
    valkeys, sentinels = {m.wg for m in c.of("valkey")}, {m.wg for m in c.of("sentinel")}
    quorum = len(c.of("sentinel")) // 2 + 1
    for m in c.of("sentinel"):
        r = remotes[m.name]
        replicas, others, primary = sentinel_knows(r)
        if str(primary.get("quorum")) != str(quorum):
            r.run(f"valkey-cli -h 127.0.0.1 -p 26379 sentinel set texloom quorum {quorum} >/dev/null")
        if replicas - valkeys or others - sentinels:
            say(f"  {m.name}: its Sentinel forgets {', '.join(sorted((replicas - valkeys) | (others - sentinels)))} (no longer in the settings)")
            r.run("valkey-cli -h 127.0.0.1 -p 26379 sentinel reset texloom >/dev/null")
            t0 = time.monotonic()
            while time.monotonic() - t0 < 90:
                time.sleep(3)
                replicas, others, _ = sentinel_knows(r)
                if len(replicas) >= len(valkeys) - 1 and len(others) >= len(sentinels) - 1:
                    break
            else:
                raise Problem(f"{m.name}: its Sentinel hasn't found the other Valkeys and Sentinels again after forgetting")


# What a server install.sh set up used: its database, files and codes files (the settings file, /etc/texloom/env,
# and the service's environment, where a drop-in may add TEXLOOM_CODES_FILES), and its settings.
OLD_SERVER = r"""
CONF=/etc/texloom/texloom.toml.before-cluster
[ -f "$CONF" ] || CONF=/etc/texloom/texloom.toml
[ -f "$CONF" ] || { echo "no /etc/texloom/texloom.toml: not a server deploy/install.sh set up" >&2; exit 1; }
ENVS="$(cat /etc/texloom/env 2>/dev/null; systemctl show texloom -p Environment --value 2>/dev/null | tr ' ' '\n')"
CONF="$CONF" ENVS="$ENVS" python3 - <<'PY'
import json, os, tomllib
from pathlib import Path
s = tomllib.load(open(os.environ["CONF"], "rb"))
env = dict(l.split("=", 1) for l in os.environ["ENVS"].splitlines() if l.startswith("TEXLOOM_") and "=" in l)
app = Path("/var/www/texloom")
data = Path(env.get("TEXLOOM_DATA") or s.get("data") or "data")
data = data if data.is_absolute() else app / data
database = env.get("TEXLOOM_DATABASE") or s.get("database") or f"sqlite:{data / 'texloom.db'}"
files = env.get("TEXLOOM_FILES") or s.get("files") or ""
codes = [*s.get("codes_file", []), *[x for x in env.get("TEXLOOM_CODES_FILES", "").split(":") if x]]
codes = [str(c if Path(c).is_absolute() else app / c) for c in codes]
print(json.dumps({"database": database, "data": str(data), "files": files, "folder": str(data / "files"), "codes": codes,
                  "settings": {k: v for k, v in s.items() if not isinstance(v, dict)},
                  "moved": os.path.exists("/etc/texloom/moved-in")}))
PY
"""
# settings every relay of the cluster has its own way (or install.sh's), not to be carried over from a server moving in
CLUSTER_OWN = {"database", "bus", "host", "port", "files", "admin_token", "admin_token_file", "data", "static", "tex_assets",
               "codes_file", "texlive"}


def old_server(r: Remote) -> dict:
    return json.loads(r.run(OLD_SERVER).strip().splitlines()[-1])


def carried(c: Cluster, m: Machine, old: dict) -> list[str]:
    """The moving-in server's settings that the cluster's relay settings don't give the same."""
    new = {**example_settings(), **c.relay_settings, **m.relay}
    return [f"{k} = {json.dumps(v)}" for k, v in old["settings"].items() if k not in CLUSTER_OWN and new.get(k) != v]


def database_url(c: Cluster) -> str:
    return (f"postgresql://texloom:{c.secrets['pg-texloom']}@" + ",".join(f"{p.wg}:5432" for p in c.of("postgresql"))
            + "/texloom?target_session_attrs=primary")


def move_in(c: Cluster, r: Remote, primary: Remote) -> None:
    """The server install.sh set up on r.m, moved into the (new, empty) cluster: its relay stopped; its database copied
    into the cluster's; its stored files into the bucket (unless they are there); its codes files' codes into the
    database. Its settings file is kept as texloom.toml.before-cluster; nothing is deleted."""
    old = old_server(r)
    if old["moved"]:
        return
    say(f"== moving in {r.m.name}'s server")
    if missing := carried(c, r.m, old):
        say("  note: settings it had that the cluster's relays don't (add them to [relay] if they should stay): " + "; ".join(missing))
    empty = primary.run("""cd /tmp
q() { runuser -u postgres -- psql -h /var/run/postgresql -qtAX texloom -c "$1"; }
if [ "$(q "SELECT to_regclass('log') IS NULL")" = t ]; then echo 0; else q "SELECT (SELECT COUNT(*) FROM log) + (SELECT COUNT(*) FROM projects)"; fi
""").strip()
    if empty != "0":
        raise Problem(f"move_in = {r.m.name!r}: the cluster's database already holds projects, so a server can't move into it "
                      "(moving in is for a new cluster: its database is copied whole)")
    server = "/var/www/texloom/.venv/bin/texloom-server --config /dev/null"
    b = c.bucket
    s3 = (f"TEXLOOM_S3_ENDPOINT={shlex.quote(b.get('endpoint', ''))} TEXLOOM_S3_REGION={shlex.quote(b.get('region', 'auto'))} "
          f"TEXLOOM_S3_KEY_ID={shlex.quote(b.get('key_id', ''))} TEXLOOM_S3_SECRET={shlex.quote(c.secrets.get('s3', ''))}")
    url = shlex.quote(database_url(c))
    steps = [f"runuser -u texloom -- {server} --data {shlex.quote(old['data'])} --database {url} "
             f"--copy-database-from {shlex.quote(old['database'])}"]
    if old["files"] in ("", "folder") or not old["files"].startswith("s3://"):
        folder = old["files"] if old["files"] and not old["files"].startswith("s3://") else old["folder"]
        steps.append(f"runuser -u texloom -- env {s3} {server} --data {shlex.quote(old['data'])} --database {url} "
                     f"--files {shlex.quote(b['url'])} --files-restore {shlex.quote(folder)}")
    elif old["files"].rstrip("/") != b.get("url", "").rstrip("/"):
        raise Problem(f"{r.m.name} keeps its files in {old['files']}, not the cluster's bucket: copy them across first")
    for f in old["codes"]:
        steps.append(f"[ ! -f {shlex.quote(f)} ] || runuser -u texloom -- {server} --data {shlex.quote(old['data'])} "
                     f"--database {url} --import-codes-file {shlex.quote(f)}")
    say(f"  stopping its relay, then copying its database{', files' if len(steps) > 1 else ''} and codes")
    try:
        out = r.run("cp -np /etc/texloom/texloom.toml /etc/texloom/texloom.toml.before-cluster\nsystemctl stop texloom\n"
                    + "\n".join(steps) + "\ndate -u > /etc/texloom/moved-in\n")
    except Problem:
        r.run("systemctl start texloom", check=False)
        primary.run("cd /tmp; runuser -u postgres -- psql -h /var/run/postgresql -q -c 'DROP DATABASE IF EXISTS texloom' "
                    "-c 'CREATE DATABASE texloom OWNER texloom'", check=False)
        raise Problem(f"moving {r.m.name} in failed: its server was started again as it was, and the cluster's database "
                      "emptied, so `apply` can try again. What went wrong: see above") from None
    for line in out.splitlines():
        if line.strip() and ("row(s)" in line or "copied" in line or "put back" in line or "default allowance" in line):
            say(f"    {line.strip()}")
    if old["codes"]:
        say(f"  its codes files ({', '.join(old['codes'])}) are in the database now: take them out of what still names them "
            "(a drop-in's TEXLOOM_CODES_FILES, a sales service's --codes-file)")


def step_relay(c: Cluster, r: Remote, first: bool) -> None:
    """The relay (and, on a door, nginx): install.sh itself, given this machine's settings first."""
    files = [relay_settings(c, r.m), File("/etc/texloom/admin-token", c.secrets["admin-token"] + "\n", "640")]
    r.run(put_script(files) + "getent group texloom >/dev/null && chgrp texloom /etc/texloom/texloom.toml /etc/texloom/admin-token || true\n")
    env = {**os.environ, "SSH_OPTS": " ".join(shlex.quote(o) for o in r.opts),
           "SKIP_CERT": "" if c.certbot and first else "1", "SKIP_BUILD": "" if first else "1"}
    say(f"  {r.m.name}: install.sh")
    p = subprocess.run([str(HERE / "install.sh"), r.target, c.name], env=env, capture_output=True, text=True, timeout=3600)
    if p.returncode != 0:
        raise Problem(f"{r.m.name}: install.sh failed:\n{(p.stdout + p.stderr).strip()[-2500:]}")
    r.run("chgrp texloom /etc/texloom/texloom.toml /etc/texloom/admin-token\n")
    if first:  # the owner's creation code, once: the codes are in the database, the same for every relay
        server = "runuser -u texloom -- /var/www/texloom/.venv/bin/texloom-server --config /etc/texloom/texloom.toml"
        out = r.run(f"""
if ! {server} --list-codes | grep -qE '^[0-9A-Z]{{4}}-[0-9A-Z]{{4}}-'; then
  echo "owner=$({server} --add-code owner --allowance unlimited)"
fi
""")
        for line in out.splitlines():
            if line.startswith("owner="):
                say(f"  the owner's creation code (unlimited): {line[6:]} (keep it: it lets you store projects)")
    r.run(f"""
for i in $(seq 60); do curl -sf --max-time 2 http://{r.m.wg}:8787/api/health >/dev/null && exit 0; sleep 1; done
echo "the relay isn't well: journalctl -u texloom" >&2; exit 1
""")


def step_door(c: Cluster, r: Remote) -> None:
    listed = "".join(f"{m.wg}:8787\n" for m in c.of("relay"))
    r.run(put_script([File("/etc/texloom/relays", "# Written by deploy/cluster.py: every relay, for the front door\n" + listed),
                      File("/usr/local/lib/texloom/door-watch.py", (HERE / "door-watch.py").read_text(), "755"),
                      File("/etc/systemd/system/texloom-door-watch.service", DOOR_UNIT)]) + """
systemctl daemon-reload
systemctl enable -q texloom-door-watch
systemctl restart texloom-door-watch
""")


# --- the commands ----------------------------------------------------------------------------------------------------

def plan(c: Cluster, remotes: dict[str, Remote]) -> int:
    """What apply would change, machine by machine."""
    changes = 0
    for m in c.machines:
        r = remotes[m.name]
        lines = []
        if missing := [p for p in packages(m) if p not in m.facts["installed"]]:
            lines.append(f"install {' '.join(missing)}")
        if not m.facts.get("pubkey"):
            lines.append(f"join the WireGuard network as {m.wg}")
        kept = [wg_conf(c, m)] if m.pubkey else []
        if c.firewall != "off":
            kept.append(firewall(c, m))
        if m.has("etcd") and m.facts.get("etcd_data") != "yes":
            lines.append("start etcd")
        if m.has("postgresql"):
            kept.append(patroni_conf(c, m))
            if m.facts.get("pg_data") != "yes":
                lines.append("start PostgreSQL (Patroni)")
        if m.has("valkey"):
            kept.append(valkey_users(c))
            if m.facts.get("valkey_conf") != "yes":
                lines.append("start Valkey")
        if m.has("sentinel") and m.facts.get("sentinel_conf") != "yes":
            lines.append("start a Sentinel")
        if m.has("relay"):
            kept.append(relay_settings(c, m))
            lines.append("install the relay from this checkout (install.sh)" + (", with nginx" if m.has("door") else ""))
        if kept and "(made at apply)" not in json.dumps([f.content for f in kept]):
            there = dict(line.split(" ", 1) for line in r.run(shas_script([f.path for f in kept])).splitlines())
            lines += [f"{'write' if there.get(f.path) == '-' else 'change'} {f.path}" for f in kept if there.get(f.path) != f.sha]
        if m.name == c.move_in:
            old = old_server(r)
            if not old["moved"]:
                lines.insert(0, f"move its server in: stop it, copy its database ({old['database'].split('?')[0]})"
                             + ("" if old["files"].startswith("s3://") else f", its files ({old['files'] or old['folder']}) into the bucket")
                             + (f", the codes of {', '.join(old['codes'])}" if old["codes"] else ""))
                lines += [f"note: a setting it has that the cluster's relays don't: {x}" for x in carried(c, m, old)]
        say(f"{m.name} ({m.address}, {m.wg}: {', '.join(m.runs)})")
        for line in lines or ["nothing to change"]:
            say(f"  {line}")
        changes += len(lines)
    return changes


def apply(c: Cluster, remotes: dict[str, Remote]) -> None:
    R = lambda ms: [remotes[m.name] for m in ms]  # noqa: E731
    say("== packages and keys")
    parallel(lambda r: step_base(c, r), R(c.machines))
    say("== the private network (WireGuard) and firewall")
    parallel(lambda r: step_network(c, r), R(c.machines))
    parallel(lambda r: wait_network(c, r), R(c.machines))
    say("== etcd")
    started = [m for m in c.of("etcd") if m.facts.get("etcd_data") == "yes"]
    if not started:  # a new cluster: every member at once
        parallel(lambda r: step_etcd(c, r, "new"), R(c.of("etcd")))
        parallel(lambda r: wait_etcd(c, r), R(c.of("etcd")))
    else:
        parallel(lambda r: step_etcd(c, r, "existing"), R(started))
        parallel(lambda r: wait_etcd(c, r), R(started))
        grow_etcd(c, remotes, started)
    say("== PostgreSQL (Patroni)")
    parallel(lambda r: step_patroni(c, r), R(c.of("postgresql")))
    primary = primary_pg(c, remotes[c.of("postgresql")[0].name])
    say(f"  primary: {primary.name}")
    step_database(c, remotes[primary.name])
    if c.move_in:
        move_in(c, remotes[c.move_in], remotes[primary.name])
    if c.of("valkey"):
        say("== Valkey and its Sentinels")
        vp = current_valkey_primary(c, remotes)
        step_valkey(c, remotes[vp.name], vp)  # (the primary first: the replicas start by copying it)
        parallel(lambda r: step_valkey(c, r, vp), R([m for m in c.of("valkey") if m is not vp]))
        parallel(lambda r: step_sentinel(c, r, vp), R(c.of("sentinel")))
        tidy_sentinels(c, remotes)
    say("== relays")
    relays = c.of("relay")
    step_relay(c, remotes[relays[0].name], first=True)
    parallel(lambda r: step_relay(c, r, first=False), R(relays[1:]))
    say("== front door")
    parallel(lambda r: step_door(c, r), R(c.of("door")))
    say("== done")


STATUS = r"""
u() { systemctl is-active -q "$1" && echo up || echo DOWN; }
echo "wireguard $(u wg-quick@texloom) $(wg show texloom latest-handshakes 2>/dev/null | awk -v now=$(date +%s) '$2>0{n++; if (now-$2>180) old++} END{printf "%d peers, %d stale", n, old}')"
"""


def status(c: Cluster, remotes: dict[str, Remote]) -> int:
    """Each part's health, from each machine; returns how many are unwell."""
    bad = 0
    adm = shlex.quote(c.secrets.get("valkey-admin", ""))

    def one(m: Machine) -> list[str]:
        if m.facts.get("unreachable"):
            return [f"machine DOWN: can't reach it ({m.facts['unreachable']})"]
        s = STATUS
        if m.has("etcd"):
            s += 'echo "etcd $(u etcd) $(etcdctl --endpoints=http://127.0.0.1:2379 endpoint health 2>&1 | grep -o \'is healthy\\|is unhealthy\' || echo unreachable)"\n'
        if m.has("postgresql"):
            s += (f'echo "postgresql $(u texloom-patroni) $(curl -s --max-time 2 http://{m.wg}:8008/patroni | python3 -c '
                  '\'import json,sys; d=json.load(sys.stdin); print(d.get("role"), d.get("state"), "lag", d.get("replication_state") or "-")\' 2>/dev/null || echo unreachable)"\n')
        if m.has("valkey"):
            s += (f'echo "valkey $(u texloom-valkey) $(valkey-cli -h 127.0.0.1 --user admin --pass {adm} --no-auth-warning '
                  'info replication 2>/dev/null | grep -E "^role:|^master_link_status:" | tr -d "\\r" | tr "\\n" " " || echo unreachable)"\n')
        if m.has("sentinel"):
            s += ('echo "sentinel $(u texloom-sentinel) primary $(valkey-cli -h 127.0.0.1 -p 26379 sentinel get-master-addr-by-name texloom '
                  '2>/dev/null | head -1 || echo unknown)"\n')
        if m.has("relay"):
            s += f'echo "relay $(u texloom) $(curl -s --max-time 2 http://{m.wg}:8787/api/health || echo unreachable)"\n'
        if m.has("door"):
            s += 'echo "door $(u nginx) watcher $(u texloom-door-watch), marked down: $(grep -c " down;" /etc/nginx/conf.d/texloom-relays.conf 2>/dev/null || true)"\n'
        return remotes[m.name].run(s, check=False).splitlines()

    for m, lines in zip(c.machines, parallel(one, c.machines)):
        say(f"{m.name} ({m.wg})")
        for line in lines:
            unwell = " DOWN" in f" {line}" or "unreachable" in line or "unhealthy" in line or '"ok": false' in line
            bad += unwell
            say(f"  {'!! ' if unwell else ''}{line}")
    return bad


RETIRE = """
for u in texloom-door-watch texloom texloom-sentinel texloom-valkey texloom-patroni etcd wg-quick@texloom texloom-firewall; do
  if systemctl is-enabled -q $u 2>/dev/null || systemctl is-active -q $u 2>/dev/null; then systemctl disable -q --now $u && echo "stopped $u"; fi
done
"""


def retire(c: Cluster, target: str) -> None:
    """Stop and disable every part the cluster put on a machine no longer in it (its data is left where it is), so that
    it can't come back and rejoin as if it were still a member."""
    if target in [m.name for m in c.machines] or target in [m.address for m in c.machines] or target in [m.ssh for m in c.machines]:
        raise Problem(f"{target} is in the settings: take it out of them (and `apply`) first")
    m = Machine("retired", target, target, ())
    with tempfile.TemporaryDirectory(prefix="tl-", dir="/tmp" if Path("/tmp").is_dir() else None) as control:
        out = Remote(c, m, Path(control)).run(RETIRE)
    say(out.strip() or "nothing of the cluster's was running there")
    say("its data is still there (/var/lib/etcd/texloom, /var/lib/postgresql/texloom, /var/www/texloom): wipe or destroy the "
        "machine before using it again")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("plan", "apply", "status", "retire"))
    ap.add_argument("settings", type=Path, help="the cluster's settings file (see deploy/cluster.example.toml)")
    ap.add_argument("machine", nargs="?", help="retire: the machine taken out of the settings (its address, or host:port for SSH)")
    a = ap.parse_args()
    try:
        c = load(a.settings)
        for w in warnings(c):
            say(f"note: {w}")
        load_secrets(c, make=a.command == "apply")
        if a.command == "retire":
            if not a.machine:
                raise Problem("retire: name the machine (its address)")
            retire(c, a.machine)
            return 0
        # (SSH's control sockets: a short path, since macOS allows 104 bytes for one)
        with tempfile.TemporaryDirectory(prefix="tl-", dir="/tmp" if Path("/tmp").is_dir() else None) as control:
            remotes = connect(c, Path(control), tolerate=a.command == "status")
            if a.command == "plan":
                plan(c, remotes)
                return 0
            if a.command == "apply":
                apply(c, remotes)
                return 1 if status(c, remotes) else 0
            return 1 if status(c, remotes) else 0
    except Problem as e:
        print(f"cluster: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
