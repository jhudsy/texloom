#!/usr/bin/env python3
"""Set up TeXLoom on several machines from one settings file (CLUSTER.md).

    deploy/cluster.py plan   cluster.toml     # what would change (changes nothing)
    deploy/cluster.py apply  cluster.toml     # make it so; run again whenever the file (or this checkout) changes
    deploy/cluster.py status cluster.toml     # every part's health
    deploy/cluster.py rotate cluster.toml     # every secret it made changed (after taking out a lost machine)

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

Each door gets its own Let's Encrypt certificate for the name (`[tls]`): a check from Let's Encrypt that reaches a door
which didn't ask is passed over WireGuard to the others, so a door can get (and renew) its certificate whichever door
the name's addresses send the check to, including a door not yet in DNS.

A server install.sh set up can move into a new cluster (`move_in`), bringing its projects, stored files and codes:
either as one of its machines, or from outside it (its server is then stopped and left as it is).
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
PARTS = ("relay", "door", "postgresql", "etcd", "valkey", "sentinel", "monitoring")
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
    "monitoring": ["prometheus"],  # (and Grafana, from Grafana's own repository: Debian has none)
}
# with a machine running `monitoring`, every machine's numbers for Prometheus (installed without what they recommend)
EXPORTERS = {"base": "prometheus-node-exporter", "postgresql": "prometheus-postgres-exporter",
             "valkey": "prometheus-redis-exporter"}
EXPORTER_PORTS = {"prometheus-node-exporter": 9100, "prometheus-postgres-exporter": 9187, "prometheus-redis-exporter": 9121}
ACME_PORT = 8788  # where a door answers the other doors' Let's Encrypt checks (not 8790: a sales service may use that)
ACME_DIR = "/var/lib/texloom-acme"  # (nginx reads it: not in the relay's own folder, which it can't enter)
# TeXLoom's own ports, reached only through WireGuard: relay, PostgreSQL, Patroni, etcd (clients, peers), Valkey, Sentinel,
# the doors' certificate checks, the exporters
PORTS = (8787, 5432, 8008, 2379, 2380, 6379, 26379, ACME_PORT, *EXPORTER_PORTS.values())
SECRETS = ("pg-superuser", "pg-replicator", "pg-texloom", "valkey-admin", "valkey-relay", "valkey-sentinel",
           "valkey-replica", "admin-token", "pg-metrics", "valkey-metrics", "grafana-admin")
# what the relays may send to Valkey (server/bus.py's COMMANDS), and what Sentinels and replicas need
RELAY_COMMANDS = ("hello", "ping", "publish", "subscribe", "unsubscribe", "get", "set", "del", "incrby", "pexpire",
                  "hset", "hget", "hdel", "hgetall", "hlen", "multi", "exec")
SENTINEL_ACL = ("+client +subscribe +publish +ping +info +multi +slaveof +replicaof +config +exec +role "
                "&__sentinel__:hello")
REPLICA_ACL = "+psync +replconf +ping"
METRICS_ACL = "+ping +info +config|get +client|list +slowlog|get +slowlog|len +latency|latest +memory|stats +select +cluster|info"


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
    certbot: bool  # each door gets its own Let's Encrypt certificate
    acme: dict  # [tls]: email, server (the ACME directory: Let's Encrypt's unless this says otherwise)
    monitoring: dict  # [monitoring]: grafana, retention, and email for alerts
    backup: dict  # [backup]: restic's repository and keys, and (bucket_copy) where the stored files are copied
    bucket: dict
    relay_settings: dict
    move_in: str = ""  # a machine (of these) install.sh set up, whose server, data and codes move into the cluster
    outside: Machine | None = None  # or a server install.sh set up outside the cluster, moving in from there
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
    known = {"name", "ssh", "network", "machines", "bucket", "relay", "tls", "move_in", "monitoring", "backup"}
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
    tls = raw.get("tls", {})
    if extra := set(tls) - {"certbot", "email", "server"}:
        raise Problem(f"[tls]: unknown setting(s) {sorted(extra)}")
    move = raw.get("move_in", "")
    outside = None
    if isinstance(move, dict):  # [move_in] ssh = "host[:port]": a server outside the cluster
        if set(move) != {"ssh"} or not move["ssh"]:
            raise Problem("[move_in]: `ssh` (where SSH reaches the server moving in from outside, host or host:port) and "
                          "nothing else; or move_in = \"name\" for one of [machines]")
        outside = Machine("the server moving in", move["ssh"].rsplit(":", 1)[0], move["ssh"], ())
        move = ""
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
                firewall=net.get("firewall", "ports"), certbot=bool(tls.get("certbot", True)),
                acme={k: tls[k] for k in ("email", "server") if tls.get(k)}, monitoring=raw.get("monitoring", {}), backup=raw.get("backup", {}),
                bucket=raw.get("bucket", {}),
                relay_settings=raw.get("relay", {}), move_in=move, outside=outside)
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
    if len(c.of("monitoring")) > 1:
        raise Problem("one machine runs `monitoring` (Prometheus, Grafana): it watches every machine")
    if extra := set(c.monitoring) - {"grafana", "retention", "email_to", "email_from", "smtp", "smtp_user", "smtp_password_file"}:
        raise Problem(f"[monitoring]: unknown setting(s) {sorted(extra)}")
    if c.monitoring and not c.of("monitoring"):
        raise Problem("[monitoring] is set, but no machine runs `monitoring`")
    b = c.backup
    if extra := set(b) - {"repository", "password_file", "keys_file", "healthcheck_url", "keep_hourly", "keep_daily",
                          "bucket_copy"}:
        raise Problem(f"[backup]: unknown setting(s) {sorted(extra)}")
    if b and not b.get("repository"):
        raise Problem("[backup] repository = \"…\" (restic's: where backups go, e.g. b2:bucket:texloom) is needed")
    if b.get("bucket_copy") and not str(b["bucket_copy"].get("url", "")).startswith("s3://"):
        raise Problem("[backup.bucket_copy] url = \"s3://…\" (a bucket at another provider), with its endpoint and keys")
    if b.get("bucket_copy") and not c.bucket.get("url", "").startswith("s3://"):
        raise Problem("[backup.bucket_copy]: the stored files are in a folder, not a bucket: they are backed up with restic")
    if c.monitoring.get("email_to") and not c.monitoring.get("smtp"):
        raise Problem("[monitoring] email_to needs smtp = \"host:port\" (the mail server alerts are sent through)")
    names = [m.name for m in c.machines]
    if len(set(names)) != len(names):
        raise Problem("two machines have one name")
    if c.move_in and c.move_in not in [m.name for m in c.of("relay")]:
        raise Problem(f"move_in = {c.move_in!r}: name the machine (one of [machines]) that runs the server moving in, "
                      "with `relay` (and usually `door`) among what it runs")
    if c.outside and c.outside.ssh in [m.ssh for m in c.machines] + [m.address for m in c.machines]:
        raise Problem(f"[move_in] ssh = {c.outside.ssh!r} is one of [machines]: for a server becoming one of the cluster's "
                      "machines, move_in = \"its name\"")


def warnings(c: Cluster) -> list[str]:
    out = []
    if len(c.of("etcd")) < 3:
        out.append(f"{len(c.of('etcd'))} etcd member(s): with fewer than three, losing one stops the database failing over")
    if c.of("sentinel") and len(c.of("sentinel")) < 3:
        out.append(f"{len(c.of('sentinel'))} Sentinel(s): with fewer than three, losing one stops Valkey failing over")
    if len(c.of("postgresql")) < 2:
        out.append("one PostgreSQL: nothing to fail over to")
    if c.certbot and len(c.of("door")) > 5:
        out.append(f"{len(c.of('door'))} doors, each with its own certificate: Let's Encrypt issues five a week for one name, "
                   "so a new door may wait for its own")
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
# the door's certificate: Let's Encrypt's (and how it is renewed), or one copied from a server that moved in
if [ -d "/etc/letsencrypt/live/$SITE_NAME" ]; then
  echo "cert=$(sed -n 's/^authenticator = //p' "/etc/letsencrypt/renewal/$SITE_NAME.conf" 2>/dev/null)"
elif [ -f "/etc/texloom/tls/$SITE_NAME.crt" ]; then echo "cert=copied"; fi
echo "certbot_site=$(grep -qs 'managed by Certbot' "/etc/nginx/sites-available/$SITE_NAME" && echo yes || echo no)"
"""


def facts(c: Cluster, r: Remote) -> dict:
    out = {}
    for line in r.run(f"SITE_NAME={shlex.quote(c.name)}\n" + FACTS).splitlines():
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


def packages(c: Cluster, m: Machine) -> list[str]:
    want = list(PACKAGES["base"])
    for p in m.runs:
        want += [x for x in PACKAGES[p] if x not in want]
    if c.of("monitoring"):
        want += [x for p, x in EXPORTERS.items() if p == "base" or m.has(p)]
        if m.has("monitoring") and c.monitoring.get("email_to"):
            want.append("prometheus-alertmanager")
    if c.backup and m.has("relay"):
        want += ["restic"] + (["rclone"] if c.backup.get("bucket_copy") else [])
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


def valkey_users(c: Cluster, old: dict[str, str] | None = None) -> File:
    """Valkey's users. `old`: their passwords before `rotate`, valid as well as the new ones meanwhile."""
    def pw(user: str) -> str:
        new, was = c.secrets[f"valkey-{user}"], (old or {}).get(f"valkey-{user}")
        return f">{new}" + (f" >{was}" if was and was != new else "")
    relay = " ".join(f"+{x}" for x in RELAY_COMMANDS)
    return File("/etc/valkey/texloom-users.acl", "\n".join([
        "user default off",
        f"user admin on {pw('admin')} ~* &* +@all",
        f"user relay on {pw('relay')} resetkeys ~texloom:* resetchannels &texloom:* -@all {relay}",
        f"user sentinel on {pw('sentinel')} -@all {SENTINEL_ACL}",
        f"user replica on {pw('replica')} -@all {REPLICA_ACL}",
        *([f"user metrics on {pw('metrics')} resetkeys resetchannels -@all {METRICS_ACL}"] if c.of("monitoring") else []),
        ""]), "640", "valkey:valkey")


def vadmin(*passwords: str) -> str:
    """Shell defining `v CMD…`: a command to this machine's Valkey as its admin, with the first of `passwords` it takes
    (valkey-cli says nothing in its exit status of a refused login, or of an error reply: so each reply is checked),
    the password passed in the environment (not on the command line, where `ps` shows it)."""
    tries = " ".join(shlex.quote(p) for p in passwords)
    return f"""
for VPW in {tries}; do [ "$(REDISCLI_AUTH="$VPW" valkey-cli --no-auth-warning --user admin -h 127.0.0.1 ping 2>&1)" = PONG ] && break; VPW=; done
[ -n "$VPW" ] || {{ echo "Valkey refused every admin password it was given" >&2; exit 1; }}
v() {{ out=$(REDISCLI_AUTH="$VPW" valkey-cli --no-auth-warning --user admin -h 127.0.0.1 "$@" 2>&1); [ "$out" = OK ] || {{ echo "valkey $1: $out" >&2; exit 1; }}; }}
"""


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
    if c.outside:
        remotes[c.outside.name] = Remote(c, c.outside, control)
    return remotes


def step_base(c: Cluster, r: Remote) -> None:
    """Packages (with no service started by installing it), and this machine's WireGuard key."""
    m = r.m
    missing = [p for p in packages(c, m) if p not in m.facts["installed"]]
    plain = [p for p in missing if not p.startswith("prometheus")]
    script = ""
    if missing:
        say(f"  {m.name}: installing {' '.join(missing)}")
        script += """
mkdir -p /etc/postgresql-common
grep -qs '^create_main_cluster' /etc/postgresql-common/createcluster.conf || echo 'create_main_cluster = false' >> /etc/postgresql-common/createcluster.conf
printf '#!/bin/sh\\nexit 101\\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d
trap 'rm -f /usr/sbin/policy-rc.d' EXIT
apt-get update -qq
[ -z "%s" ] || apt-get install -y -qq -o Dpkg::Options::=--force-confold %s >/dev/null
[ -z "%s" ] || apt-get install -y -qq --no-install-recommends -o Dpkg::Options::=--force-confold %s >/dev/null
# (the packages' own services: TeXLoom's are its own units)
for s in etcd valkey-server valkey-sentinel patroni; do systemctl disable -q --now $s 2>/dev/null || true; done
""" % (" ".join(plain), " ".join(plain), " ".join(set(missing) - set(plain)), " ".join(set(missing) - set(plain)))
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
        # (etcd refuses a change of members until it has settled after the last one: "unhealthy cluster")
        live.run(f"""for i in $(seq 30); do
  if out=$(etcdctl --endpoints=http://127.0.0.1:2379 member add {m.name} --peer-urls=http://{m.wg}:2380 2>&1); then exit 0; fi
  case "$out" in *"unhealthy cluster"*) sleep 2;; *) echo "$out" >&2; exit 1;; esac
done
echo "$out" >&2; exit 1""")
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
""" + ("" if not c.of("monitoring") else f"""
[ "$(q -c "SELECT 1 FROM pg_roles WHERE rolname='metrics'")" = 1 ] || q -c "CREATE ROLE metrics LOGIN IN ROLE pg_monitor"
q -c "ALTER ROLE metrics PASSWORD '{c.secrets['pg-metrics'].replace("'", "''")}'" >/dev/null
"""))


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
  {vadmin(c.secrets['valkey-admin']) + "v acl load" if before != acl.sha else ":"}
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


def moving(c: Cluster, remotes: dict[str, Remote]) -> tuple[Remote, dict] | None:
    """The server moving in (its machine, and what it has), unless there is none or it has moved in already."""
    name = c.outside.name if c.outside else c.move_in
    if not name:
        return None
    old = old_server(remotes[name])
    return None if old["moved"] else (remotes[name], old)


def carried(c: Cluster, m: Machine, old: dict) -> list[str]:
    """The moving-in server's settings that the cluster's relay settings don't give the same."""
    new = {**example_settings(), **c.relay_settings, **m.relay}
    return [f"{k} = {json.dumps(v)}" for k, v in old["settings"].items() if k not in CLUSTER_OWN and new.get(k) != v]


def database_url(c: Cluster) -> str:
    return (f"postgresql://texloom:{c.secrets['pg-texloom']}@" + ",".join(f"{p.wg}:5432" for p in c.of("postgresql"))
            + "/texloom?target_session_attrs=primary")


SNAPSHOT = "/var/tmp/texloom-move-in"  # the moving server's database (SQLite) and codes files, on their way in
SERVER = "/var/www/texloom/.venv/bin/texloom-server"


def bucket_env(c: Cluster) -> str:
    b = c.bucket
    return (f"TEXLOOM_S3_ENDPOINT={shlex.quote(b.get('endpoint', ''))} TEXLOOM_S3_REGION={shlex.quote(b.get('region', 'auto'))} "
            f"TEXLOOM_S3_KEY_ID={shlex.quote(b.get('key_id', ''))} TEXLOOM_S3_SECRET={shlex.quote(c.secrets.get('s3', ''))}")


def take_out(c: Cluster, s: Remote, old: dict) -> None:
    """Moving a server in, first half, on its machine: its relay stopped (its settings kept as
    texloom.toml.before-cluster); its database copied into a SQLite file, with its codes files beside it; its stored files
    put into the bucket (unless they are there already). Nothing of its own is changed or deleted."""
    say(f"== moving in {s.m.name}'s server")
    if missing := carried(c, s.m, old):
        say("  note: settings it had that the cluster's relays don't (add them to [relay] if they should stay): " + "; ".join(missing))
    server = f"runuser -u texloom -- {SERVER} --config /dev/null --data {shlex.quote(old['data'])}"
    snap = f"sqlite:{SNAPSHOT}/texloom.db"
    steps = [f"rm -rf {SNAPSHOT} && install -d -o texloom -g texloom -m 700 {SNAPSHOT}",
             f"{server} --database {snap} --copy-database-from {shlex.quote(old['database'])}"]
    if not old["files"].startswith("s3://"):
        steps.append(f"runuser -u texloom -- env {bucket_env(c)} {SERVER} --config /dev/null --data {shlex.quote(old['data'])} "
                     f"--database {snap} --files {shlex.quote(c.bucket['url'])} --files-restore {shlex.quote(old['files'] or old['folder'])}")
    elif old["files"].rstrip("/") != c.bucket.get("url", "").rstrip("/"):
        raise Problem(f"{s.m.name} keeps its files in {old['files']}, not the cluster's bucket: copy them across first")
    for i, f in enumerate(old["codes"]):
        steps.append(f"[ ! -f {shlex.quote(f)} ] || install -m 600 -o texloom {shlex.quote(f)} {SNAPSHOT}/codes-{i}.txt")
    say(f"  stopping its relay, then copying its database{'' if old['files'].startswith('s3://') else ', putting its files into the bucket'}")
    try:
        out = s.run("cp -np /etc/texloom/texloom.toml /etc/texloom/texloom.toml.before-cluster\nsystemctl stop texloom\n"
                    + "\n".join(steps) + "\n")
    except Problem:
        s.run("systemctl start texloom", check=False)
        raise Problem(f"moving {s.m.name}'s server in failed before anything of the cluster's changed, and it was started "
                      "again as it was. What went wrong: see above") from None
    for line in out.splitlines():
        if "row(s)" in line or "copied" in line or "put back" in line:
            say(f"    {line.strip()}")


def bring_in(c: Cluster, s: Remote, t: Remote, primary: Remote, old: dict) -> None:
    """Moving a server in, second half, on the first relay (t, which may be s itself): the copy taken out of it put into
    the cluster's database (emptied first: it is new, so holds nothing but what its relay made on starting), and its
    codes files imported. On failure, the cluster's database is emptied and the server started again as it was."""
    if s is not t:
        say(f"  copying it to {t.m.name}")
        t.run(f"rm -rf {SNAPSHOT} && install -d -m 700 {SNAPSHOT}")
        send = subprocess.Popen(["ssh", *s.opts, s.target, f"{'' if s.root else 'sudo '}tar -C {SNAPSHOT} -c ."],
                                stdout=subprocess.PIPE)
        got = subprocess.run(["ssh", *t.opts, t.target, f"{'' if t.root else 'sudo '}tar -C {SNAPSHOT} -x"],
                             stdin=send.stdout, capture_output=True, text=True, timeout=3600)
        send.stdout.close()
        if send.wait() != 0 or got.returncode != 0:
            s.run("systemctl start texloom", check=False)
            raise Problem(f"copying {s.m.name}'s database to {t.m.name} failed ({got.stderr.strip()[-500:]}): its server "
                          "was started again as it was")
        t.run(f"chown -R texloom:texloom {SNAPSHOT}")
    psql = "cd /tmp; runuser -u postgres -- psql -h /var/run/postgresql -qtAX"
    server = f"runuser -u texloom -- {SERVER} --config /etc/texloom/texloom.toml"
    say(f"  putting it into the cluster's database (from {t.m.name})")
    try:
        t.run("systemctl stop texloom")
        held = primary.run(f"""{psql} texloom -c "SELECT CASE WHEN to_regclass('log') IS NULL THEN 0 ELSE
  (SELECT COUNT(*) FROM log) + (SELECT COUNT(*) FROM projects) END" """).strip()
        if held != "0":
            raise Problem("the cluster's database already holds projects, so a server can't move into it (moving in is "
                          "for a new cluster: its database is copied whole)")
        primary.run(f"{psql} -c 'DROP DATABASE IF EXISTS texloom' -c 'CREATE DATABASE texloom OWNER texloom'")
        out = t.run(f"{server} --copy-database-from sqlite:{SNAPSHOT}/texloom.db\n"
                    f"for f in {SNAPSHOT}/codes-*.txt; do [ ! -f \"$f\" ] || {server} --import-codes-file \"$f\"; done\n"
                    f"systemctl start texloom\nrm -rf {SNAPSHOT}\n")
        s.run(f"date -u > /etc/texloom/moved-in\nrm -rf {SNAPSHOT}\n"
              + ("" if s is t else "systemctl disable -q texloom\n"))
    except Problem as e:
        t.run("systemctl stop texloom", check=False)
        primary.run(f"{psql} -c 'DROP DATABASE IF EXISTS texloom' -c 'CREATE DATABASE texloom OWNER texloom'", check=False)
        t.run("systemctl start texloom", check=False)
        s.run("cp /etc/texloom/texloom.toml.before-cluster /etc/texloom/texloom.toml && systemctl start texloom"
              if s is t else "systemctl start texloom", check=False)
        raise Problem(f"moving {s.m.name}'s server in failed: it was started again as it was, and the cluster's database "
                      f"emptied, so `apply` can try again. What went wrong: {e}") from None
    for line in out.splitlines():
        if "row(s)" in line or "default allowance" in line or "imported" in line:
            say(f"    {line.strip()}")
    if old["codes"]:
        say(f"  its codes files ({', '.join(old['codes'])}) are in the database now: take them out of what still names them "
            "(a sales service's --codes-file, say)")
    if s is not t:
        say(f"  {s.m.name}'s server is stopped, and won't start again by itself; its data and nginx are left as they "
            f"were. Point {c.name} at the doors.")


def step_relay(c: Cluster, r: Remote, first: bool) -> None:
    """The relay (and, on a door, nginx): install.sh itself, given this machine's settings first."""
    files = [relay_settings(c, r.m), File("/etc/texloom/admin-token", c.secrets["admin-token"] + "\n", "640")]
    site = f"/etc/nginx/sites-available/{c.name}"
    tidy = ""
    if c.certbot and r.m.facts.get("certbot_site") == "yes":  # (the doors' certificates are the tool's: see step_door)
        say(f"  {r.m.name}: its nginx site, with certbot's lines, is kept as {site}.before-cluster and written afresh")
        tidy = f"mv {shlex.quote(site)} {shlex.quote(site + '.before-cluster')}\n"
    r.run(put_script(files) + tidy
          + "getent group texloom >/dev/null && chgrp texloom /etc/texloom/texloom.toml /etc/texloom/admin-token || true\n")
    env = {**os.environ, "SSH_OPTS": " ".join(shlex.quote(o) for o in r.opts), "SKIP_CERT": "1",
           "SKIP_BUILD": "" if first else "1"}
    say(f"  {r.m.name}: install.sh")
    p = subprocess.run([str(HERE / "install.sh"), r.target, c.name], env=env, capture_output=True, text=True, timeout=3600)
    if p.returncode != 0:
        raise Problem(f"{r.m.name}: install.sh failed:\n{(p.stdout + p.stderr).strip()[-2500:]}")
    r.m.facts["certbot_site"] = "no"
    r.run(f"""chgrp texloom /etc/texloom/texloom.toml /etc/texloom/admin-token
for i in $(seq 60); do curl -sf --max-time 2 http://{r.m.wg}:8787/api/health >/dev/null && exit 0; sleep 1; done
echo "the relay isn't well: journalctl -u texloom" >&2; exit 1
""")


def owner_code(c: Cluster, r: Remote) -> None:
    """The owner's creation code, once (codes are in the database: the same for every relay)."""
    server = f"runuser -u texloom -- {SERVER} --config /etc/texloom/texloom.toml"
    out = r.run(f"""
if ! {server} --list-codes | grep -qE '^[0-9A-Z]{{4}}-[0-9A-Z]{{4}}-'; then
  echo "owner=$({server} --add-code owner --allowance unlimited)"
fi
""")
    for line in out.splitlines():
        if line.startswith("owner="):
            say(f"  the owner's creation code (unlimited): {line[6:]} (keep it: it lets you store projects)")


def acme_files(c: Cluster, m: Machine) -> list[File]:
    """Let's Encrypt's checks on a door: answered from its own folder, else passed to the other doors (whichever asked
    for a certificate has the answer); and the doors' own server for those, reached only over WireGuard."""
    others = "".join(f"    server {d.wg}:{ACME_PORT};\n" for d in c.of("door") if d is not m) or f"    server 127.0.0.1:{ACME_PORT};\n"
    return [File("/etc/nginx/conf.d/texloom-acme.conf", f"""# Written by deploy/cluster.py: Let's Encrypt's checks, passed between the doors
# plain HTTP goes to HTTPS (once a door has a certificate: /etc/texloom/door/tls.conf), but not a check
map "$scheme:$request_uri" $texloom_to_https {{
    "~^http:/\\.well-known/acme-challenge/" 0;
    "~^http:" 1;
    default 0;
}}
# the other doors, asked for a check this one has no answer to
upstream texloom_acme_doors {{
{others}}}
# where the other doors ask this one (the firewall keeps this port to WireGuard)
server {{
    listen {ACME_PORT};
    root {ACME_DIR};
    location /.well-known/acme-challenge/ {{ try_files $uri =404; }}
    location / {{ return 404; }}
}}
"""), File("/etc/texloom/door/acme.conf", f"""# Written by deploy/cluster.py, included in the site: Let's Encrypt's checks, answered here or by another door
location ^~ /.well-known/acme-challenge/ {{
    root {ACME_DIR};
    try_files $uri @texloom_acme_doors;
}}
location @texloom_acme_doors {{
    proxy_pass http://texloom_acme_doors;
    proxy_next_upstream error timeout http_404;
    proxy_connect_timeout 2s;
}}
"""), File("/etc/letsencrypt/renewal-hooks/deploy/texloom-nginx", "#!/bin/sh\n# Written by deploy/cluster.py: a renewed "
           "certificate is used at once\nsystemctl reload nginx\n", "755")]


def tls_file(c: Cluster, crt: str, key: str) -> File:
    return File("/etc/texloom/door/tls.conf", f"""# Written by deploy/cluster.py, included in the site: HTTPS, with this door's certificate
listen 443 ssl;
listen [::]:443 ssl;
ssl_certificate {crt};
ssl_certificate_key {key};
ssl_protocols TLSv1.2 TLSv1.3;
ssl_session_cache shared:texloom_tls:10m;
ssl_session_timeout 1d;
if ($texloom_to_https) {{
    return 301 https://$host$request_uri;
}}
""")


RELOAD = "if nginx -t -q 2>/dev/null; then systemctl reload nginx; else nginx -t; exit 1; fi\n"


def backup_files(c: Cluster) -> list[File]:
    """A relay's backup settings (deploy/texloom-backup): restic's repository, password and keys, and rclone's for the
    bucket's copy. Every relay has them: each tries every hour, and the first to begin makes the hour's backup."""
    b, d = c.backup, c.path.parent

    def read(setting: str, path: str) -> str:
        try:
            return (d / path).expanduser().read_text().strip()
        except OSError as e:
            raise Problem(f"[backup] {setting} {path}: {e.strerror}") from None
    pw = b.get("password_file", "secrets/restic-password")
    if not (d / pw).expanduser().exists() and pw == "secrets/restic-password":  # (made the first time, as the other secrets)
        f = d / pw
        f.parent.mkdir(mode=0o700, exist_ok=True)
        f.write_text(secrets.token_urlsafe(32) + "\n")
        f.chmod(0o600)
        say(f"  backups: made a password for them in {f}: keep a copy somewhere else (without it they can't be read)")
    env = [f"RESTIC_REPOSITORY={b['repository']}", "RESTIC_PASSWORD_FILE=/etc/texloom/restic-password",
           f"KEEP_HOURLY={int(b.get('keep_hourly', 48))}", f"KEEP_DAILY={int(b.get('keep_daily', 30))}"]
    if b.get("keys_file"):
        env += [line.strip() for line in read("keys_file", b["keys_file"]).splitlines()
                if line.strip() and not line.startswith("#")]
    if b.get("healthcheck_url"):
        env.append(f"HEALTHCHECK_URL={b['healthcheck_url']}")
    files = [File("/etc/texloom/restic-password", read("password_file", pw) + "\n", "600")]
    if copy := b.get("bucket_copy"):
        def remote(name: str, bucket: dict, secret: str) -> str:
            return (f"[{name}]\ntype = s3\nprovider = Other\nendpoint = {bucket.get('endpoint', '')}\n"
                    f"region = {bucket.get('region', 'auto')}\naccess_key_id = {bucket.get('key_id', '')}\n"
                    f"secret_access_key = {secret}\nno_check_bucket = true\n\n")
        files.append(File("/etc/texloom/rclone.conf", "# Written by deploy/cluster.py: the bucket, and where it is copied\n"
                          + remote("source", c.bucket, c.secrets.get("s3", ""))
                          + remote("copy", copy, read("bucket_copy.secret_file", copy.get("secret_file", ""))
                                   if copy.get("secret_file") else ""), "600"))
        env += [f"BUCKET_SOURCE={c.bucket['url'].removeprefix('s3://')}", f"BUCKET_COPY=copy:{copy['url'].removeprefix('s3://')}"]
    files.append(File("/etc/texloom/backup.env", "# Written by deploy/cluster.py ([backup]): deploy/texloom-backup's settings\n"
                      + "".join(f"{shlex.quote(line.split('=', 1)[0])}={shlex.quote(line.split('=', 1)[1])}\n" for line in env),
                      "600"))
    return files


def step_backup(c: Cluster, r: Remote) -> None:
    if not c.backup:
        r.run("rm -f /etc/texloom/backup.env /etc/texloom/restic-password /etc/texloom/rclone.conf\n")
        return
    r.run(put_script(backup_files(c)))


def step_door(c: Cluster, r: Remote) -> None:
    """The health watcher, and (with [tls] certbot) the certificate checks passed between doors."""
    listed = "".join(f"{m.wg}:8787\n" for m in c.of("relay"))
    files = [File("/etc/texloom/relays", "# Written by deploy/cluster.py: every relay, for the front door\n" + listed),
             File("/usr/local/lib/texloom/door-watch.py", (HERE / "door-watch.py").read_text(), "755"),
             File("/etc/systemd/system/texloom-door-watch.service", DOOR_UNIT)]
    if c.certbot:
        files += acme_files(c, r.m)
    tidy = "" if c.certbot else "rm -f /etc/texloom/door/acme.conf /etc/texloom/door/tls.conf /etc/nginx/conf.d/texloom-acme.conf\n"
    r.run(put_script(files) + tidy + f"install -d -m 755 {ACME_DIR}\n" + RELOAD + """
systemctl daemon-reload
systemctl enable -q texloom-door-watch
systemctl restart texloom-door-watch
""")


def door_certificate(c: Cluster, r: Remote, outside: Remote | None) -> None:
    """This door's own certificate from Let's Encrypt (checked through any door), renewed by certbot's timer. One that
    nginx's certbot plugin got (a server moved in) is renewed again through the doors from now on. A door that can't get
    one yet (the name still points at a server moving in from outside) uses that server's, until it can."""
    live = f"/etc/letsencrypt/live/{c.name}"
    args = (f"certonly --webroot -w {ACME_DIR} -d {shlex.quote(c.name)} --cert-name {shlex.quote(c.name)} "
            f"--non-interactive --agree-tos --quiet "
            + (f"--email {shlex.quote(c.acme['email'])} " if c.acme.get("email") else "--register-unsafely-without-email ")
            + (f"--server {shlex.quote(c.acme['server'])} " if c.acme.get("server") else ""))
    have = r.m.facts.get("cert", "")
    if have and have != "copied" and have != "webroot":
        say(f"  {r.m.name}: its certificate is renewed through the doors from now on (until now: {have})")
        out = r.run(f"certbot {args} --force-renewal 2>&1 && echo got || true")
        if "got" not in out.split():
            say(f"  {r.m.name}: !! couldn't renew it through the doors yet, so certbot still renews it its old way, which "
                f"fails with several doors: run `apply` again once {c.name} points at the doors\n{out.strip()[-600:]}")
    elif have != "webroot":
        out = r.run(f"certbot {args} 2>&1 && echo got || true")
        if "got" in out.split():
            say(f"  {r.m.name}: has its certificate from Let's Encrypt")
            have = "webroot"
        elif outside and outside.run(f"[ -f {live}/privkey.pem ] && echo yes || true").strip() == "yes":
            crt, key = (outside.run(f"cat {live}/fullchain.pem"), outside.run(f"cat {live}/privkey.pem"))
            r.run(put_script([File(f"/etc/texloom/tls/{c.name}.crt", crt), File(f"/etc/texloom/tls/{c.name}.key", key, "600")]))
            say(f"  {r.m.name}: uses the certificate of the server moving in until it can get its own (once {c.name} "
                "points at the doors: run `apply` again then)")
            have = "copied"
        elif have != "copied":
            say(f"  {r.m.name}: !! no certificate yet: Let's Encrypt couldn't reach any door through {c.name} (does it "
                f"point at them?); HTTPS waits for the next `apply`\n{out.strip()[-600:]}")
            return
    if have == "copied":
        files, tidy = [tls_file(c, f"/etc/texloom/tls/{c.name}.crt", f"/etc/texloom/tls/{c.name}.key")], ""
    else:
        files, tidy = [tls_file(c, f"{live}/fullchain.pem", f"{live}/privkey.pem")], "rm -rf /etc/texloom/tls\n"
    r.run(put_script(files) + """if ! nginx -t -q 2>/dev/null; then
  nginx -t || true; rm -f /etc/texloom/door/tls.conf; systemctl reload nginx
  echo "nginx refused the door's HTTPS settings (above): left without HTTPS" >&2; exit 1
fi
systemctl reload nginx
""" + tidy)


# --- monitoring (optional: only with a machine running `monitoring`) -------------------------------------------------

UNITS_WATCHED = r"(texloom.*|etcd|nginx|wg-quick@texloom|prometheus.*|grafana-server)\.service"
TEXTFILES = "/var/lib/prometheus/node-exporter"  # numbers node_exporter passes on from files (a door's certificate)
CERT_METRIC = f"""#!/bin/sh
# Written by deploy/cluster.py: this door's certificate's expiry, for Prometheus (node_exporter's textfile collector)
crt=$(sed -n 's/^ssl_certificate \\(.*\\);$/\\1/p' /etc/texloom/door/tls.conf 2>/dev/null)
[ -n "$crt" ] || exit 0
end=$(date -d "$(openssl x509 -enddate -noout -in "$crt" | cut -d= -f2)" +%s)
printf '# HELP texloom_certificate_expiry_seconds when the door'"'"'s certificate expires\\n# TYPE texloom_certificate_expiry_seconds gauge\\ntexloom_certificate_expiry_seconds %s\\n' "$end" > {TEXTFILES}/texloom-cert.prom.new
mv {TEXTFILES}/texloom-cert.prom.new {TEXTFILES}/texloom-cert.prom
"""
CERT_TIMER = """[Unit]
Description=The door's certificate's expiry, for Prometheus (deploy/cluster.py)

[Timer]
OnBootSec=5min
OnUnitActiveSec=6h

[Install]
WantedBy=timers.target
"""
CERT_SERVICE = """[Unit]
Description=The door's certificate's expiry, for Prometheus (deploy/cluster.py)

[Service]
Type=oneshot
ExecStart=/usr/local/lib/texloom/cert-metric
"""
AFTER_WG = """# Written by deploy/cluster.py: it listens on the WireGuard address, so it starts after WireGuard (and tries again)
[Unit]
After=wg-quick@texloom.service
Wants=wg-quick@texloom.service

[Service]
Restart=always
RestartSec=5
"""


def step_exporters(c: Cluster, r: Remote) -> None:
    """Each part's numbers for Prometheus, on this machine's WireGuard address: node_exporter (the machine, and whether
    each of the cluster's services is running), PostgreSQL's and Valkey's exporters. Patroni, etcd and the relays
    have their own /metrics."""
    m, s = r.m, c.secrets
    node = f"--web.listen-address={m.wg}:9100 --collector.systemd --collector.systemd.unit-include='{UNITS_WATCHED}' " \
           f"--collector.textfile.directory={TEXTFILES}"
    files = [File("/etc/default/prometheus-node-exporter", f'# Written by deploy/cluster.py\nARGS="{node}"\n')]
    units = ["prometheus-node-exporter"]
    if m.has("postgresql"):
        files.append(File("/etc/default/prometheus-postgres-exporter", "# Written by deploy/cluster.py\n"
                          f"DATA_SOURCE_NAME='postgresql://metrics:{s['pg-metrics']}@127.0.0.1:5432/postgres?sslmode=disable'\n"
                          f'ARGS="--web.listen-address={m.wg}:9187"\n', "640", "root:prometheus"))
        units.append("prometheus-postgres-exporter")
    if m.has("valkey"):
        files.append(File("/etc/default/prometheus-redis-exporter", "# Written by deploy/cluster.py\n"
                          f"REDIS_ADDR=redis://127.0.0.1:6379\nREDIS_USER=metrics\nREDIS_PASSWORD={s['valkey-metrics']}\n"
                          f'ARGS="--web.listen-address={m.wg}:9121"\n', "640", "root:prometheus"))
        units.append("prometheus-redis-exporter")
    files += [File(f"/etc/systemd/system/{u}.service.d/texloom.conf", AFTER_WG) for u in units]
    timer = ""
    if m.has("door") and c.certbot:
        files += [File("/usr/local/lib/texloom/cert-metric", CERT_METRIC, "755"),
                  File("/etc/systemd/system/texloom-cert-metric.service", CERT_SERVICE),
                  File("/etc/systemd/system/texloom-cert-metric.timer", CERT_TIMER)]
        timer = "systemctl enable -q --now texloom-cert-metric.timer\nsystemctl start texloom-cert-metric.service\n"
    r.run(put_script(files) + f"install -d -o prometheus -g prometheus {TEXTFILES}\nsystemctl daemon-reload\n"
          + "".join(f"systemctl enable -q {u}\nsystemctl restart {u}\n" for u in units) + timer)


def prometheus_conf(c: Cluster) -> File:
    def job(name: str, port: int, machines: list[Machine], path: str = "/metrics") -> str:
        targets = "".join(f"      - targets: ['{m.wg}:{port}']\n        labels: {{machine: {m.name}}}\n" for m in machines)
        return f"  - job_name: {name}\n    metrics_path: {path}\n    static_configs:\n{targets}" if machines else ""
    email = bool(c.monitoring.get("email_to"))
    return File("/etc/prometheus/prometheus.yml", "# Written by deploy/cluster.py: what Prometheus watches. Changes here are replaced.\n"
                "global:\n  scrape_interval: 15s\n  evaluation_interval: 15s\n"
                "rule_files:\n  - /etc/prometheus/texloom-alerts.yml\n"
                + ("alerting:\n  alertmanagers:\n    - static_configs:\n        - targets: ['127.0.0.1:9093']\n" if email else "")
                + "scrape_configs:\n"
                + job("node", 9100, c.machines) + job("relay", 8787, c.of("relay")) + job("patroni", 8008, c.of("postgresql"))
                + job("postgresql", 9187, c.of("postgresql")) + job("etcd", 2379, c.of("etcd"))
                + job("valkey", 9121, c.of("valkey")))


def alertmanager_conf(c: Cluster) -> File:
    mo = c.monitoring
    password = ""
    if pf := mo.get("smtp_password_file"):
        try:
            password = (c.path.parent / pf).expanduser().read_text().strip()
        except OSError as e:
            raise Problem(f"[monitoring] smtp_password_file {pf}: {e.strerror}") from None
    smtp = {"smtp_smarthost": mo["smtp"], "smtp_from": mo.get("email_from") or f"texloom-alerts@{c.name}",
            **({"smtp_auth_username": mo["smtp_user"], "smtp_auth_password": password} if mo.get("smtp_user") else {})}
    return File("/etc/prometheus/alertmanager.yml", "# Written by deploy/cluster.py: where alerts go. Changes here are replaced.\n"
                "global:\n" + "".join(f"  {k}: {json.dumps(v)}\n" for k, v in smtp.items())
                + "route:\n  receiver: email\n  group_by: [alertname]\n  group_wait: 1m\n  repeat_interval: 12h\n"
                f"receivers:\n  - name: email\n    email_configs:\n      - to: {json.dumps(mo['email_to'])}\n        send_resolved: true\n",
                "640", "root:prometheus")


GRAFANA_REPO = """
install -d -m 755 /etc/apt/keyrings
if [ ! -f /etc/apt/sources.list.d/grafana.list ]; then
  curl -fsSL https://apt.grafana.com/gpg.key -o /etc/apt/keyrings/grafana.asc
  echo "deb [signed-by=/etc/apt/keyrings/grafana.asc] https://apt.grafana.com stable main" > /etc/apt/sources.list.d/grafana.list
  apt-get update -qq
fi
dpkg -s grafana >/dev/null 2>&1 || apt-get install -y -qq --no-install-recommends grafana >/dev/null
"""


def step_monitoring(c: Cluster, r: Remote) -> None:
    """Prometheus (watching every part), its alert rules, Alertmanager (if alerts are emailed), and Grafana with the
    dashboard: listening only on the machine itself (reach them with an SSH tunnel: CLUSTER.md)."""
    mo = c.monitoring
    keep = mo.get("retention", "30d")
    files = [prometheus_conf(c), File("/etc/prometheus/texloom-alerts.yml", (HERE / "monitoring" / "alerts.yml").read_text()),
             File("/etc/default/prometheus", f'# Written by deploy/cluster.py\nARGS="--web.listen-address=127.0.0.1:9090 '
                  f'--storage.tsdb.retention.time={keep} --storage.tsdb.retention.size=2GB"\n')]
    units = ["prometheus"]
    if mo.get("email_to"):
        files += [alertmanager_conf(c), File("/etc/default/prometheus-alertmanager", '# Written by deploy/cluster.py\n'
                  'ARGS="--web.listen-address=127.0.0.1:9093 --cluster.listen-address="\n')]
        units.append("prometheus-alertmanager")
    script = ""
    if mo.get("grafana", True):
        say(f"  {r.m.name}: Grafana")
        script = GRAFANA_REPO
        files += [File("/etc/systemd/system/grafana-server.service.d/texloom.conf", "# Written by deploy/cluster.py\n"
                       "[Service]\nEnvironmentFile=/etc/texloom/grafana.env\n"),
                  File("/etc/texloom/grafana.env", "# Written by deploy/cluster.py: Grafana, on this machine only\n"
                       "GF_SERVER_HTTP_ADDR=127.0.0.1\nGF_USERS_ALLOW_SIGN_UP=false\nGF_ANALYTICS_REPORTING_ENABLED=false\n"
                       "GF_ANALYTICS_CHECK_FOR_UPDATES=false\nGF_PLUGINS_PLUGIN_ADMIN_ENABLED=false\n"
                       f"GF_SECURITY_ADMIN_PASSWORD={c.secrets['grafana-admin']}\n"
                       "GF_SECURITY_DISABLE_GRAVATAR=true\nGF_NEWS_NEWS_FEED_ENABLED=false\n", "640", "root:grafana"),
                  File("/etc/grafana/provisioning/datasources/texloom.yaml", "# Written by deploy/cluster.py\napiVersion: 1\n"
                       "datasources:\n  - name: Prometheus\n    uid: prometheus\n    type: prometheus\n    access: proxy\n"
                       "    url: http://127.0.0.1:9090\n    isDefault: true\n", "640", "root:grafana"),
                  File("/etc/grafana/provisioning/dashboards/texloom.yaml", "# Written by deploy/cluster.py\napiVersion: 1\n"
                       "providers:\n  - name: texloom\n    type: file\n    options:\n      path: /var/lib/texloom-dashboards\n",
                       "640", "root:grafana"),
                  File("/var/lib/texloom-dashboards/texloom.json", (HERE / "monitoring" / "dashboard.json").read_text())]
        units.append("grafana-server")
    r.run(script + put_script(files) + "systemctl daemon-reload\n"
          + "".join(f"systemctl enable -q {u}\nsystemctl restart {u}\n" for u in units)
          + "for i in $(seq 60); do curl -sf http://127.0.0.1:9090/-/ready >/dev/null && exit 0; sleep 1; done\n"
            "echo \"Prometheus isn't ready: journalctl -u prometheus\" >&2; exit 1\n")


# --- the commands ----------------------------------------------------------------------------------------------------

def plan(c: Cluster, remotes: dict[str, Remote]) -> int:
    """What apply would change, machine by machine."""
    changes = 0
    for m in c.machines:
        r = remotes[m.name]
        lines = []
        if missing := [p for p in packages(c, m) if p not in m.facts["installed"]]:
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
        if m.has("monitoring"):
            kept.append(prometheus_conf(c))
        if m.has("relay"):
            kept.append(relay_settings(c, m))
            if c.backup and (c.path.parent / c.backup.get("password_file", "secrets/restic-password")).exists():
                kept += backup_files(c)
            lines.append("install the relay from this checkout (install.sh)" + (", with nginx" if m.has("door") else ""))
        if kept and "(made at apply)" not in json.dumps([f.content for f in kept]):
            there = dict(line.split(" ", 1) for line in r.run(shas_script([f.path for f in kept])).splitlines())
            lines += [f"{'write' if there.get(f.path) == '-' else 'change'} {f.path}" for f in kept if there.get(f.path) != f.sha]
        if m.name == c.move_in and (mover := moving(c, remotes)):
            lines[:0] = moving_lines(c, *mover)
        if m.has("door") and c.certbot:
            if m.facts.get("certbot_site") == "yes":
                lines.append(f"keep its nginx site (certbot's) as {c.name}.before-cluster: install.sh writes it afresh")
            if not m.facts.get("cert") or m.facts["cert"] == "copied":
                lines.append(f"get a certificate for {c.name} from Let's Encrypt")
            elif m.facts["cert"] != "webroot":
                lines.append(f"renew its certificate through the doors (until now: {m.facts['cert']})")
        say(f"{m.name} ({m.address}, {m.wg}: {', '.join(m.runs)})")
        for line in lines or ["nothing to change"]:
            say(f"  {line}")
        changes += len(lines)
    if c.outside and (mover := moving(c, remotes)):
        lines = moving_lines(c, *mover)
        say(f"{c.outside.name} ({c.outside.ssh}), outside the cluster")
        for line in lines + ["then leave its server stopped (it won't start again by itself), and the rest as it is"]:
            say(f"  {line}")
        changes += len(lines)
    return changes


def moving_lines(c: Cluster, r: Remote, old: dict) -> list[str]:
    return ([f"move its server in: stop it, copy its database ({old['database'].split('?')[0]})"
             + ("" if old["files"].startswith("s3://") else f", its files ({old['files'] or old['folder']}) into the bucket")
             + (f", the codes of {', '.join(old['codes'])}" if old["codes"] else "")]
            + [f"note: a setting it has that the cluster's relays don't: {x}" for x in carried(c, r.m, old)])


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
    if c.of("valkey"):
        say("== Valkey and its Sentinels")
        vp = current_valkey_primary(c, remotes)
        step_valkey(c, remotes[vp.name], vp)  # (the primary first: the replicas start by copying it)
        parallel(lambda r: step_valkey(c, r, vp), R([m for m in c.of("valkey") if m is not vp]))
        parallel(lambda r: step_sentinel(c, r, vp), R(c.of("sentinel")))
        tidy_sentinels(c, remotes)
    relays = sorted(c.of("relay"), key=lambda m: m.name != c.move_in)  # (a server moving in from among them: first)
    mover = moving(c, remotes)
    if mover:
        take_out(c, *mover)
    say("== relays")
    step_relay(c, remotes[relays[0].name], first=True)
    if mover:
        bring_in(c, mover[0], remotes[relays[0].name], remotes[primary_pg(c, remotes[primary.name]).name], mover[1])
    owner_code(c, remotes[relays[0].name])
    parallel(lambda r: step_relay(c, r, first=False), R(relays[1:]))
    if c.backup:
        backup_files(c)  # (its password, made here the first time, before the relays get it at once)
    parallel(lambda r: step_backup(c, r), R(relays))
    say("== front door")
    parallel(lambda r: step_door(c, r), R(c.of("door")))
    if c.certbot:
        outside = remotes.get(c.outside.name) if c.outside else None
        parallel(lambda r: door_certificate(c, r, outside), R(c.of("door")))
    if c.of("monitoring"):
        say("== monitoring")
        parallel(lambda r: step_exporters(c, r), R(c.machines))
        step_monitoring(c, remotes[c.of("monitoring")[0].name])
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
        if c.of("monitoring"):
            s += 'echo "exporters $(for x in node postgres redis; do systemctl cat -q prometheus-$x-exporter >/dev/null 2>&1 && printf "%s %s " $x $(u prometheus-$x-exporter); done)"\n'
        if m.has("monitoring"):
            s += ('echo "monitoring prometheus $(u prometheus), targets down: $(curl -s --max-time 5 http://127.0.0.1:9090/api/v1/targets | python3 -c '
                  '\'import json,sys; t=json.load(sys.stdin)["data"]["activeTargets"]; d=[x["labels"].get("machine","?")+" "+x["labels"]["job"] for x in t if x["health"]=="down"]; print(len(d), "(DOWN: "+", ".join(d)+")" if d else "")\' 2>/dev/null || echo unreachable)'
                  + (', grafana $(u grafana-server)' if c.monitoring.get("grafana", True) else "")
                  + (', alertmanager $(u prometheus-alertmanager)' if c.monitoring.get("email_to") else "") + '"\n')
        if m.has("relay"):
            s += f'echo "relay $(u texloom) $(curl -s --max-time 2 http://{m.wg}:8787/api/health || echo unreachable)"\n'
        if m.has("door"):
            s += 'echo "door $(u nginx) watcher $(u texloom-door-watch), marked down: $(grep -c " down;" /etc/nginx/conf.d/texloom-relays.conf 2>/dev/null || true)"\n'
            if c.certbot:
                s += r"""crt=$(sed -n 's/^ssl_certificate \(.*\);$/\1/p' /etc/texloom/door/tls.conf 2>/dev/null)
if [ -z "$crt" ]; then echo "certificate NONE (HTTPS is off: see apply's output)"
else days=$(( ($(date -d "$(openssl x509 -enddate -noout -in "$crt" | cut -d= -f2)" +%s) - $(date +%s)) / 86400 ))
  case "$crt" in /etc/texloom/tls/*) whose=", the moved-in server's: apply once the name points at the doors";; *) whose="";; esac
  if [ "$days" -lt 14 ]; then echo "certificate EXPIRING: $days days left$whose"; else echo "certificate $days days left$whose"; fi
fi
"""
        return remotes[m.name].run(s, check=False).splitlines()

    for m, lines in zip(c.machines, parallel(one, c.machines)):
        say(f"{m.name} ({m.wg})")
        for line in lines:
            unwell = (" DOWN" in f" {line}" or "unreachable" in line or "unhealthy" in line or '"ok": false' in line
                      or "EXPIRING" in line or "NONE" in line)
            bad += unwell
            say(f"  {'!! ' if unwell else ''}{line}")
    return bad


ROTATED = SECRETS  # the secrets the tool made (a provider's keys are changed there: the bucket's, the backups', email)


def rotate(c: Cluster, remotes: dict[str, Remote]) -> None:
    """Every secret the tool made changed, so that one a lost (or once-trusted) machine had no longer works: take the
    machine out first (`apply`), then this. Nothing stops meanwhile: Valkey's users take the new passwords beside the
    old until every part has them; PostgreSQL's roles change and their users follow at once (the relays one at a time,
    each back before the next). If it stops partway, run it again: it carries on with the same new secrets (the old
    are kept as secrets/NAME.old until it ends)."""
    if gone := [m.name for m in c.machines if m.facts.get("unreachable")]:
        raise Problem(f"can't reach {', '.join(gone)}: take a lost machine out of the settings (and `apply`) before rotating")
    d = c.secrets_dir
    old: dict[str, str] = {}
    for name in ROTATED:
        f, was = d / name, d / f"{name}.old"
        if not was.exists():  # (it exists when a rotation is being carried on)
            was.write_text(f.read_text())
            was.chmod(0o600)
            f.write_text(secrets.token_urlsafe(24) + "\n")
        old[name] = was.read_text().strip()
        c.secrets[name] = f.read_text().strip()
    restic = c.backup and "password_file" not in c.backup  # (the tool's own restic password: changed too)
    if restic:
        f, was = d / "restic-password", d / "restic-password.old"
        if not was.exists():
            was.write_text(f.read_text())
            was.chmod(0o600)
            f.write_text(secrets.token_urlsafe(32) + "\n")
    q = shlex.quote
    R = lambda ms: [remotes[m.name] for m in ms]  # noqa: E731

    if c.of("valkey"):
        say("== Valkey: the new passwords beside the old, the replicas and Sentinels given the new")
        def valkey_both(r: Remote) -> None:
            r.run(put_script([valkey_users(c, old)]) + vadmin(c.secrets["valkey-admin"], old["valkey-admin"]) + f"""
v acl load
v config set masterauth {q(c.secrets['valkey-replica'])}
v config rewrite
""")
        parallel(valkey_both, R(c.of("valkey")))
        parallel(lambda r: r.run(f"""out=$(valkey-cli -h 127.0.0.1 -p 26379 sentinel set texloom auth-pass {q(c.secrets['valkey-sentinel'])} 2>&1)
[ "$out" = OK ] || {{ echo "sentinel set: $out" >&2; exit 1; }}
"""), R(c.of("sentinel")))
    say("== PostgreSQL: its roles' passwords, then Patroni's settings")
    primary = primary_pg(c, remotes[c.of("postgresql")[0].name])
    roles = {"postgres": "pg-superuser", "replicator": "pg-replicator", "texloom": "pg-texloom",
             **({"metrics": "pg-metrics"} if c.of("monitoring") else {})}
    remotes[primary.name].run("cd /tmp\n" + "".join(
        f"runuser -u postgres -- psql -h /var/run/postgresql -qtAX -v ON_ERROR_STOP=1 -f - <<'SQL' >/dev/null\n"
        f"ALTER ROLE {role} PASSWORD '{c.secrets[name].replace(chr(39), chr(39) * 2)}';\nSQL\n" for role, name in roles.items()))
    parallel(lambda r: r.run(put_script([patroni_conf(c, r.m)]) + "systemctl reload texloom-patroni\n"), R(c.of("postgresql")))
    say("== relays, one at a time (each well again before the next)")
    for r in R(c.of("relay")):
        files = [relay_settings(c, r.m), File("/etc/texloom/admin-token", c.secrets["admin-token"] + "\n", "640")]
        r.run(put_script(files) + f"""chgrp texloom /etc/texloom/texloom.toml /etc/texloom/admin-token
systemctl restart texloom
for i in $(seq 60); do curl -sf --max-time 2 http://{r.m.wg}:8787/api/health >/dev/null && exit 0; sleep 1; done
echo "the relay isn't well after its restart: journalctl -u texloom" >&2; exit 1
""")
        say(f"  {r.m.name}: well")
    if c.of("monitoring"):
        say("== monitoring")
        parallel(lambda r: step_exporters(c, r), R(c.machines))
        m = remotes[c.of("monitoring")[0].name]
        if c.monitoring.get("grafana", True):
            m.run(f"grafana cli --homepath /usr/share/grafana admin reset-admin-password {q(c.secrets['grafana-admin'])} "
                  ">/dev/null 2>&1 || grafana-cli --homepath /usr/share/grafana admin reset-admin-password "
                  f"{q(c.secrets['grafana-admin'])} >/dev/null")
        step_monitoring(c, m)
    if restic:
        say("== backups: restic's repository opened by the new password, not the old")
        new_pw, old_pw = (d / "restic-password").read_text().strip(), (d / "restic-password.old").read_text().strip()
        r = R(c.of("relay"))[0]
        r.run(f"""set -a; . /etc/texloom/backup.env; set +a
umask 077; new=$(mktemp); was=$(mktemp); trap 'rm -f "$new" "$was"' EXIT
printf '%s\\n' {q(new_pw)} > "$new"; printf '%s\\n' {q(old_pw)} > "$was"
if ! RESTIC_PASSWORD_FILE="$new" restic cat config >/dev/null 2>&1; then
  RESTIC_PASSWORD_FILE="$was" restic key add --new-password-file "$new" >/dev/null
fi
id=$(RESTIC_PASSWORD_FILE="$was" restic key list --json 2>/dev/null | python3 -c 'import json,sys; print(next(k["id"] for k in json.load(sys.stdin) if k["current"]))' 2>/dev/null || true)
[ -z "$id" ] || RESTIC_PASSWORD_FILE="$new" restic key remove "$id" >/dev/null
""")
        parallel(lambda r: step_backup(c, r), R(c.of("relay")))
    if c.of("valkey"):
        say("== Valkey: the old passwords taken away")
        parallel(lambda r: r.run(put_script([valkey_users(c)]) + vadmin(c.secrets["valkey-admin"]) + "v acl load\n"), R(c.of("valkey")))
    for name in [*ROTATED, *(["restic-password"] if restic else [])]:
        (d / f"{name}.old").unlink(missing_ok=True)
    say("== done: the old secrets work nowhere now. Keep a copy of the new secrets/ (a password manager).")
    say("   Not changed here (a provider's keys: make a new one there, put it in its file, `apply`, then delete the old "
        "one there): the bucket's" + (", the backups' storage" if c.backup else "")
        + (", the copy's bucket" if c.backup.get("bucket_copy") else "")
        + (", the mail server's" if c.monitoring.get("smtp_password_file") else "") + ".")


RETIRE = """
for u in texloom-door-watch texloom-cert-metric.timer texloom texloom-sentinel texloom-valkey texloom-patroni etcd prometheus grafana-server \
         prometheus-alertmanager prometheus-node-exporter prometheus-postgres-exporter prometheus-redis-exporter wg-quick@texloom texloom-firewall; do
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
    ap.add_argument("command", choices=("plan", "apply", "status", "retire", "rotate"))
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
            remotes = connect(c, Path(control), tolerate=a.command in ("status", "rotate"))
            if a.command == "plan":
                plan(c, remotes)
                return 0
            if a.command == "apply":
                apply(c, remotes)
                return 1 if status(c, remotes) else 0
            if a.command == "rotate":
                rotate(c, remotes)
                return 1 if status(c, remotes) else 0
            return 1 if status(c, remotes) else 0
    except Problem as e:
        print(f"cluster: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
