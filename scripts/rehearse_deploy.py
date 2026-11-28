#!/usr/bin/env python3
"""A rehearsal of deploy/install.sh and deploy/cluster.py in Docker: new Debian 13 "machines" set up from nothing, a
server moved in, the cluster grown, broken and mended, and a machine replaced.

Each machine is a container running systemd and SSH and nothing else (deploy/rehearsal/Dockerfile.machine), as a new
VPS would be; a bucket (RustFS) stands in for the provider's, and Pebble (Let's Encrypt's test server, with its DNS
server saying where the name points) for Let's Encrypt. The tools are run as you would run them, from this checkout.
The machines:

  old    one server as install.sh sets it up on its own (SQLite, files in a folder, certbot's nginx plugin), given a
         project, a stored file and a codes file of the kind servers had before the database kept codes; then moved
         into the cluster, as one of its machines (or, with --outside, from outside it: old then stays outside)
  a, b   relay, door, PostgreSQL (Patroni), etcd, Valkey, Sentinel; b also Prometheus and Grafana (`monitoring`)
  c      relay, door, etcd, Sentinel (a smaller third); later replaced by d
  d      the same as c, added in c's place

with the strict firewall. In order:

- install.sh on a new machine (the one-machine path), a project there, and its certificate;
- `plan`, then `apply` with `move_in`: the cluster is set up and old's server moved in (its project, its stored file
  and its codes reach every door), and `status` says every part is well. The name still points at old only, so each
  door's certificate check is passed on to the door that asked (or, with --outside, where old passes nothing on, the
  doors use old's certificate until the name points at a door, and then get their own);
- Prometheus finds every part's numbers, Grafana has the dashboard;
- every relay begins a backup at once: one makes it (restic, to the lab's bucket server), the stored files are copied to
  a second bucket, and the database's copy restores;
- apps through different doors (over HTTPS) see each other's changes; `apply` again changes nothing that matters;
- machine a is lost: apps on the other doors carry on, `status` says what is missing, and Prometheus's alert for it
  starts; a comes back, `apply` takes it in again;
- c is replaced by d (c killed, the settings changed, `apply`): etcd's members and the Sentinels follow, every part
  is well; c is started again and `retire` stops what the cluster had put on it.

Needs Docker (privileged containers: systemd, WireGuard), about 6 GB free, and the web app's dependencies
(web/node_modules: install.sh builds it). Everything it makes is removed afterwards (--keep leaves it running).

    uv run --extra server python scripts/rehearse_deploy.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_restore import App, text  # noqa: E402
from checklib import ROOT, check, summary, until  # noqa: E402
from rehearse_relays import S3, docker  # noqa: E402

from texloom import tls  # noqa: E402
from texloom.crypto import Keys, b64e  # noqa: E402

LABEL = "texloom-rehearsal=deploy"
IMAGE = "texloom-lab-machine:1"
NET, SUBNET = "texloom-deploy", "172.31.77"
S3_KEY, S3_SECRET = "rehearsal", "rehearsal-secret-123"
EVERYTHING = ["relay", "door", "postgresql", "etcd", "valkey", "sentinel"]
THIRD = ["relay", "door", "etcd", "sentinel"]
NUMBER = {"a": 11, "b": 12, "c": 13, "d": 14, "old": 15}
SSH_PORT, HTTP_PORT, HTTPS_PORT = 2200, 8090, 8490  # + the machine's number
OLD_CODE = "AAAA-BBBB-CCCC"  # in old's codes file
NAME = "app.texloom.test"  # the cluster's name: Pebble's DNS server says which machine it points at
PEBBLE, DNS = "ghcr.io/letsencrypt/pebble:2.8.0", "ghcr.io/letsencrypt/pebble-challtestsrv:2.8.0"
ACME = "https://pebble:14000/dir"
TLS = ssl.create_default_context()  # (Pebble's root, once it has made one)

_getaddrinfo = socket.getaddrinfo


def _lab_getaddrinfo(host, *a, **k):  # the name, from here, is the machines' ports on this computer
    return _getaddrinfo("127.0.0.1" if host == NAME else host, *a, **k)


socket.getaddrinfo = _lab_getaddrinfo


def say(what: str) -> None:
    print(f"  {what}", flush=True)


class Lab:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.key = tmp / "id"
        self.settings = tmp / "cluster" / "cluster.toml"
        self.ssh = ["ssh", "-i", str(self.key), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "LogLevel=ERROR", "-o", "BatchMode=yes"]

    def name(self, m: str) -> str:
        return f"texloom-deploy-{m}"

    def start(self) -> None:
        say("building the machines' image")
        docker("build", "-q", "-t", IMAGE, "--label", LABEL, "-f", str(ROOT / "deploy" / "rehearsal" / "Dockerfile.machine"),
               str(ROOT / "deploy" / "rehearsal"), timeout=900)
        self.stop()
        docker("network", "create", "--label", LABEL, "--subnet", f"{SUBNET}.0/24", NET)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)], check=True)
        docker("run", "-d", "--label", LABEL, "--name", self.name("s3"), "--network", NET, "--ip", f"{SUBNET}.9",
               "-e", f"RUSTFS_ACCESS_KEY={S3_KEY}", "-e", f"RUSTFS_SECRET_KEY={S3_SECRET}", S3)
        # Let's Encrypt, as Pebble: checks on port 80, the name looked up in its own DNS server (pointing at old at first)
        docker("run", "-d", "--label", LABEL, "--name", self.name("dns"), "--network", NET, "--ip", f"{SUBNET}.8", DNS,
               "-defaultIPv4", f"{SUBNET}.{NUMBER['old']}", "-defaultIPv6", "")
        config = json.dumps({"pebble": {"listenAddress": "0.0.0.0:14000", "managementListenAddress": "0.0.0.0:15000",
                                        "certificate": "test/certs/localhost/cert.pem", "privateKey": "test/certs/localhost/key.pem",
                                        "httpPort": 80, "tlsPort": 443, "ocspResponderURL": "", "externalAccountBindingRequired": False}})
        (self.tmp / "pebble.json").write_text(config)
        docker("run", "-d", "--label", LABEL, "--name", self.name("pebble"), "--network", NET, "--ip", f"{SUBNET}.7",
               "--network-alias", "pebble", "-e", "PEBBLE_VA_NOSLEEP=1", "-e", "PEBBLE_WFE_NONCEREJECT=0",
               "-v", f"{self.tmp / 'pebble.json'}:/lab.json:ro", PEBBLE, "-config", "/lab.json", "-dnsserver", f"{SUBNET}.8:8053")
        docker("cp", f"{self.name('pebble')}:/test/certs/pebble.minica.pem", str(self.tmp / "minica.pem"))
        for m, n in NUMBER.items():
            docker("run", "-d", "--label", LABEL, "--name", self.name(m), "--hostname", m, "--network", NET, "--ip", f"{SUBNET}.{n}",
                   "--privileged", "--cgroupns=host", "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw", "--tmpfs", "/run", "--tmpfs", "/run/lock",
                   "-p", f"127.0.0.1:{SSH_PORT + n}:22", "-p", f"127.0.0.1:{HTTP_PORT + n}:80", "-p", f"127.0.0.1:{HTTPS_PORT + n}:443",
                   IMAGE)
        for m in NUMBER:
            self.ready(m)
            # (Pebble's own address is certified by its test CA, which the machines' certbot is told to trust)
            docker("cp", str(self.tmp / "minica.pem"), f"{self.name(m)}:/usr/local/share/ca-certificates/pebble-minica.crt")
            docker("exec", self.name(m), "update-ca-certificates")
        root = docker("run", "--rm", "--network", NET, "--label", LABEL, "-v", f"{self.tmp / 'minica.pem'}:/minica.pem:ro",
                      "curlimages/curl:8.17.0", "-sf", "--retry", "10", "--retry-all-errors", "--cacert", "/minica.pem",
                      "https://pebble:15000/roots/0").stdout
        (self.tmp / "pebble-root.pem").write_text(root)
        global TLS
        TLS = ssl.create_default_context(cafile=str(self.tmp / "pebble-root.pem"))
        tls._context = TLS  # (the apps check the doors' certificates against Pebble's root)
        for _ in range(60):
            if docker("exec", self.name("a"), "bash", "-c", f"exec 3<>/dev/tcp/{SUBNET}.9/9000", check_=False).returncode == 0:
                break
            time.sleep(1)
        for bucket in ("bucket", "backups", "bucket-copy"):
            docker("run", "--rm", "--network", NET, "--label", LABEL, "curlimages/curl:8.17.0", "-s", "-X", "PUT", "--aws-sigv4",
                   "aws:amz:us-east-1:s3", "-u", f"{S3_KEY}:{S3_SECRET}", f"http://{SUBNET}.9:9000/{bucket}")
        self.settings.parent.mkdir()
        (self.settings.parent / "secrets").mkdir(mode=0o700)
        (self.settings.parent / "secrets" / "s3-secret").write_text(S3_SECRET + "\n")
        (self.settings.parent / "secrets" / "backup-keys").write_text(
            f"AWS_ACCESS_KEY_ID={S3_KEY}\nAWS_SECRET_ACCESS_KEY={S3_SECRET}\nAWS_DEFAULT_REGION=us-east-1\n")

    def ready(self, m: str) -> None:
        """The machine (started, or started again) answers SSH, with the lab's key."""
        pub = self.key.with_suffix(".pub").read_text().strip()
        for _ in range(60):
            if docker("exec", self.name(m), "systemctl", "is-active", "ssh", check_=False).stdout.strip() == "active":
                break
            time.sleep(1)
        docker("exec", self.name(m), "sh", "-c", f"echo '{pub}' > /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys")

    def point(self, m: str) -> None:
        """The name points at machine m (Pebble's DNS server says so)."""
        docker("run", "--rm", "--network", NET, "--label", LABEL, "curlimages/curl:8.17.0", "-sf", "-X", "POST", "-d",
               f'{{"ip": "{SUBNET}.{NUMBER[m]}"}}', f"http://{SUBNET}.8:8055/set-default-ipv4")

    def write_settings(self, machines: dict[str, list[str]], move_in: str = "") -> None:
        outside = move_in and move_in not in machines
        blocks = "".join(f'\n[machines.{m}]\naddress = "{SUBNET}.{NUMBER[m]}"\nssh = "127.0.0.1:{SSH_PORT + NUMBER[m]}"\n'
                         f'runs = [{", ".join(chr(34) + x + chr(34) for x in runs)}]\n' for m, runs in machines.items())
        self.settings.write_text(f"""name = "{NAME}"
{f'move_in = "{move_in}"' if move_in and not outside else ""}

[ssh]
user = "root"
key = "{self.key}"
options = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]

[network]
firewall = "strict"
{blocks}
[bucket]
url = "s3://bucket"
endpoint = "http://{SUBNET}.9:9000"
region = "us-east-1"
key_id = "{S3_KEY}"
secret_file = "secrets/s3-secret"

[relay]
min_free = "1M"
default_allowance = "unlimited"   # (the apps here have no creation code)

[tls]
certbot = true
server = "{ACME}"

[backup]
repository = "s3:http://{SUBNET}.9:9000/backups"
keys_file = "secrets/backup-keys"

[backup.bucket_copy]
url = "s3://bucket-copy"
endpoint = "http://{SUBNET}.9:9000"
region = "us-east-1"
key_id = "{S3_KEY}"
secret_file = "secrets/s3-secret"
""" + (f'\n[move_in]\nssh = "127.0.0.1:{SSH_PORT + NUMBER[move_in]}"\n' if outside else ""))

    def stop(self) -> None:
        names = docker("ps", "-a", "--filter", f"label={LABEL}", "--format", "{{.Names}}", check_=False).stdout.split()
        if names:
            docker("rm", "-f", "-v", *names, check_=False, timeout=180)
        docker("network", "rm", NET, check_=False)

    def tool(self, command: str, *args: str, timeout: float = 3600) -> tuple[int, str, float]:
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, str(ROOT / "deploy" / "cluster.py"), command, str(self.settings), *args],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr, time.monotonic() - t0

    def prometheus(self, query: str) -> list:
        """What Prometheus (on b) answers to a query: its result's series."""
        out = self.on("b", f"curl -s --max-time 5 http://127.0.0.1:9090/api/v1/query --data-urlencode query={shlex.quote(query)}")
        try:
            return json.loads(out.stdout)["data"]["result"]
        except (ValueError, KeyError):
            return []

    def on(self, m: str, script: str) -> subprocess.CompletedProcess:
        return subprocess.run([*self.ssh, "-p", str(SSH_PORT + NUMBER[m]), "root@127.0.0.1", "bash -s"], input=script,
                              capture_output=True, text=True, timeout=600)

    https = False  # (once old has its certificate: from then on, every door has one)

    def url(self, m: str) -> str:
        return f"https://{NAME}:{HTTPS_PORT + NUMBER[m]}" if self.https else f"http://{NAME}:{HTTP_PORT + NUMBER[m]}"

    def served(self, m: str) -> str:
        """The fingerprint of the certificate machine m's door serves for the name ("" if it serves none)."""
        try:
            with socket.create_connection((NAME, HTTPS_PORT + NUMBER[m]), timeout=10) as sock:
                with TLS.wrap_socket(sock, server_hostname=NAME) as t:
                    return hashlib.sha256(t.getpeercert(binary_form=True)).hexdigest()
        except OSError:
            return ""


def show(out: str, lines: int = 40) -> None:
    for line in out.strip().splitlines()[-lines:]:
        print(f"    | {line}", flush=True)


async def apps_agree(lab: Lab, keys: Keys, doors: list[str], tag: str) -> bool:
    """An app through each door; each makes a change; every one sees every change."""
    apps = [App(lab.tmp / f"{tag}-{d}", keys, lab.url(d)) for d in doors]
    try:
        for a in apps:
            await a.start()
        for d, a in zip(doors, apps):
            await a.edit(f"{tag} through {d}")
        want = {f"{tag} through {d}" for d in doors}
        return await until(lambda: all(want <= set(text(a.r).splitlines()) for a in apps), 30)
    except (TimeoutError, AssertionError):
        return False
    finally:
        for a in apps:
            await a.stop()


def put_file(url: str, keys: Keys, data: bytes) -> str:
    bid = os.urandom(16).hex()
    req = urllib.request.Request(f"{url}/blob/{keys.project}/{bid}", data=data, method="PUT",
                                 headers={"X-Sig": b64e(keys.sign(f"blob:{bid}:".encode() + data))})
    urllib.request.urlopen(req, timeout=30, context=TLS).read()
    return bid


def get_file(url: str, keys: Keys, bid: str) -> bytes | None:
    try:
        return urllib.request.urlopen(f"{url}/blob/{keys.project}/{bid}", timeout=30, context=TLS).read()
    except OSError:
        return None


def code_valid(url: str, code: str) -> bool:
    req = urllib.request.Request(f"{url}/api/code", data=f'{{"code": "{code}"}}'.encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        return b'"ok": true' in urllib.request.urlopen(req, timeout=10, context=TLS).read()
    except OSError:
        return False


def install_old(lab: Lab) -> bool:
    """install.sh on a new machine, as for one server, then a codes file of the older kind."""
    env = {**os.environ, "SKIP_CERT": "1", "SSH_OPTS": " ".join([*lab.ssh[1:], "-p", str(SSH_PORT + NUMBER["old"])])}
    t0 = time.monotonic()
    p = subprocess.run([str(ROOT / "deploy" / "install.sh"), "root@127.0.0.1", NAME], env=env, capture_output=True,
                       text=True, timeout=3600)
    ok = p.returncode == 0 and "owner's creation code" in p.stdout
    check(f"install.sh sets up one server on a new Debian 13 machine ({(time.monotonic() - t0) / 60:.1f} min), printing the "
          "owner's code", ok, (p.stdout + p.stderr)[-2500:])
    if not ok:
        return False
    out = lab.on("old", f"""set -e
echo '{OLD_CODE.replace("-", "")} 1G kept in a file' > /etc/texloom/old-codes.txt
chown root:texloom /etc/texloom/old-codes.txt && chmod 640 /etc/texloom/old-codes.txt
sed -i 's#^default_allowance = .*#default_allowance = "unlimited"#' /etc/texloom/texloom.toml
echo 'codes_file = ["/etc/texloom/old-codes.txt"]' >> /etc/texloom/texloom.toml
systemctl restart texloom
for i in $(seq 30); do curl -sf http://127.0.0.1:8787/api/health >/dev/null && exit 0; sleep 1; done; exit 1
""")
    check("and runs with a codes file of the older kind", out.returncode == 0 and code_valid(lab.url("old"), OLD_CODE), out.stderr)
    return out.returncode == 0


def certificate_on_old(lab: Lab) -> bool:
    """old's certificate, as install.sh gets one (certbot's nginx plugin), from Pebble."""
    out = lab.on("old", f"certbot --nginx -d {NAME} --non-interactive --redirect --quiet --agree-tos "
                        f"--register-unsafely-without-email --server {ACME}")
    lab.https = True
    ok = out.returncode == 0 and bool(lab.served("old")) and code_valid(lab.url("old"), OLD_CODE)
    check("it gets its certificate as install.sh does (certbot's nginx plugin; Let's Encrypt being Pebble here)", ok,
          out.stdout + out.stderr)
    return ok


RESTIC = "set -a; . /etc/texloom/backup.env; set +a; "


async def backups(lab: Lab, relays: list[str], keys: Keys, bid: str) -> None:
    """Every relay begins the hour's backup at once (as their timers do); one makes it."""
    started = await asyncio.gather(*(asyncio.to_thread(lab.on, m, "systemctl start texloom-backup; "
                                                       "journalctl -u texloom-backup -n 5 --no-pager") for m in relays))
    snaps = lab.on(relays[0], RESTIC + "restic snapshots --json").stdout
    made = json.loads(snaps or "[]")
    check(f"every relay begins a backup at once: one makes it ({len(made)} snapshot)", len(made) == 1,
          [s.stdout[-300:] + s.stderr[-300:] for s in started])
    copied = lab.on(relays[1], "rclone --config /etc/texloom/rclone.conf lsf -R copy:bucket-copy").stdout
    check("the stored files are copied to the second bucket", bid in copied, copied[-300:])
    count = lab.on(relays[-1], RESTIC + "rm -rf /tmp/r && restic restore latest --target /tmp/r --include "
                   "/var/lib/texloom/backup/texloom.db >/dev/null && python3 -c \"import sqlite3; print(sqlite3.connect("
                   f"'/tmp/r/var/lib/texloom/backup/texloom.db').execute('SELECT COUNT(*) FROM log WHERE project = ?', "
                   f"('{keys.project}',)).fetchone()[0])\"")
    check("and restores, on another machine: the database's copy holds the project", count.stdout.strip() not in ("", "0"),
          count.stdout + count.stderr[-400:])


def own_certificates(lab: Lab, doors: list[str], not_like: str) -> bool:
    """Each door serves a certificate of its own for the name (none the same as another's, or as `not_like`)."""
    seen = {d: lab.served(d) for d in doors}
    return all(seen.values()) and len(set(seen.values())) == len(doors) and not_like not in seen.values()


async def rehearse(lab: Lab, outside: bool) -> None:
    print("one server, set up by install.sh:")
    if not install_old(lab):
        return
    keys = Keys.generate()
    seen = await apps_agree(lab, keys, ["old"], "before")
    data = os.urandom(300_000)
    bid = put_file(lab.url("old"), keys, data)
    check("it keeps a project and a stored file", seen and get_file(lab.url("old"), keys, bid) == data)
    if not certificate_on_old(lab):
        return
    olds = lab.served("old")

    doors = ["a", "b", "c"] if outside else ["a", "b", "c", "old"]
    machines = {"a": EVERYTHING, "b": [*EVERYTHING, "monitoring"], "c": THIRD, **({} if outside else {"old": ["relay", "door"]})}
    print(f"a cluster, with that server moved in {'from outside' if outside else 'as one of its machines'}:")
    lab.write_settings(machines, move_in="old")
    code, out, _ = lab.tool("plan")
    check("`plan` says what it would install, and that old's server would move in", code == 0 and "install" in out
          and "move its server in" in out and "get a certificate" in out, out[-1500:])
    code, out, secs = lab.tool("apply")
    check(f"`apply` sets up the cluster from nothing and moves old's server in ({secs / 60:.1f} min); every part is well",
          code == 0, "")
    show(out, 70)
    if code != 0:
        return
    check("old's project reaches every door (over HTTPS)", await apps_agree(lab, keys, doors, "moved"))
    check("its stored file too (copied into the bucket)", all(get_file(lab.url(d), keys, bid) == data for d in doors))
    check("and its codes file's code is valid through every door (in the database now)",
          all(code_valid(lab.url(d), OLD_CODE) for d in doors))
    if outside:
        check("with the name still pointing at old (which passes no checks on), the doors use old's certificate",
              all(lab.served(d) == olds for d in doors), str({d: lab.served(d)[:12] for d in doors}))
        st = lab.on("old", "systemctl is-active texloom; systemctl is-enabled texloom; ls /var/lib/texloom/texloom.db").stdout
        check("old's server is stopped and won't start again by itself; its data is left", st.split()[:2] == ["inactive", "disabled"]
              and "texloom.db" in st, st)
        lab.point("a")
        print("the name pointed at door a:")
    else:
        check("each door has its own certificate, though the name points only at old: each check is passed to the door "
              "that asked", own_certificates(lab, doors, ""), "")
        renewal = lab.on("old", f"grep '^authenticator' /etc/letsencrypt/renewal/{NAME}.conf").stdout
        check("old's certificate (certbot's nginx plugin's) is renewed through the doors from now on",
              "webroot" in renewal, renewal)
        print("again:")
    redirect = lab.on("b", f"curl -s -o /dev/null -w '%{{http_code}}' -H 'Host: {NAME}' http://127.0.0.1/").stdout
    check("plain HTTP is sent to HTTPS", redirect == "301", redirect)
    want = 2 * len(doors) + 3 * 2 + 3  # node and relay on each machine (all doors); Patroni, PostgreSQL, Valkey on a and b; etcd on 3
    up = []
    for _ in range(40):  # (Prometheus was just restarted: each target shows once it has been scraped)
        up = lab.prometheus("up")
        if len(up) >= want and all(x["value"][1] == "1" for x in up):
            break
        time.sleep(3)
    jobs = {x["metric"]["job"] for x in up}
    check(f"Prometheus finds every part's numbers ({len(up)} targets: {', '.join(sorted(jobs))}), all up",
          up and all(x["value"][1] == "1" for x in up) and jobs >= {"node", "relay", "patroni", "postgresql", "etcd", "valkey"},
          [(x["metric"].get("machine"), x["metric"]["job"], x["value"][1]) for x in up])
    check("with TeXLoom's own: every relay's connections, and the door's certificate's expiry",
          len(lab.prometheus("texloom_connections")) == len(doors)
          and len(lab.prometheus("texloom_certificate_expiry_seconds")) == len(doors)
          and len(lab.prometheus('node_systemd_unit_state{name="texloom.service",state="active"} == 1')) == len(doors))
    password = (lab.settings.parent / "secrets" / "grafana-admin").read_text().strip()
    found = lab.on("b", f"curl -s -u admin:{shlex.quote(password)} 'http://127.0.0.1:3000/api/search?query=TeXLoom'").stdout
    rules = lab.on("b", "curl -s http://127.0.0.1:9090/api/v1/rules").stdout
    check("Grafana has the dashboard, and Prometheus the alert rules", '"uid":"texloom"' in found and "MachineDown" in rules,
          found[:300] + rules[:300])
    await backups(lab, doors, keys, bid)

    code, out, secs = lab.tool("apply")
    check(f"`apply` again ({secs / 60:.1f} min) changes nothing that matters, and every part is well", code == 0, out[-1500:])
    if outside:
        check("and each door now has its own certificate (each check reaching a passes it to the door that asked)",
              own_certificates(lab, doors, olds), out[-1500:])
    code, out, _ = lab.tool("plan")
    check("`plan` then has nothing to change but the relays' code", code == 0 and "change /" not in out and "write /" not in out
          and "start " not in out and "move its server in" not in out and "certificate" not in out, out[-1500:])

    print("machine a lost:")
    docker("kill", lab.name("a"))
    t0 = time.monotonic()
    survivors = [d for d in doors if d != "a"]
    while not (ok := await apps_agree(lab, keys, survivors, f"after a {time.monotonic() - t0:.0f}")) and time.monotonic() - t0 < 150:
        await asyncio.sleep(5)
    check(f"apps through the other doors carry on ({time.monotonic() - t0:.0f} s until new apps join and agree: a was "
          "PostgreSQL's primary, replaced after Patroni's wait)", ok)
    code, out, _ = lab.tool("status")
    check("`status` says what is missing", code != 0 and "can't reach it" in out, out[-1500:])
    for _ in range(30):  # (Prometheus notices at its next scrape and rule evaluation: within 30 s or so)
        alerts = lab.prometheus('ALERTS{alertname="MachineDown"}')
        if any(x["metric"].get("machine") == "a" for x in alerts):
            break
        time.sleep(3)
    check("and Prometheus's alert for it has started (it fires, and is emailed, after two minutes)",
          any(x["metric"].get("machine") == "a" for x in alerts), alerts)

    print("machine a back:")
    docker("start", lab.name("a"))
    lab.ready("a")
    code, out, secs = lab.tool("apply")
    check(f"`apply` takes it in again ({secs / 60:.1f} min), and every part is well", code == 0, out[-2000:])
    check("and apps through every door agree", await apps_agree(lab, keys, doors, "back"))

    print("machine c replaced by d:")
    docker("kill", lab.name("c"))
    machines = {("d" if m == "c" else m): runs for m, runs in machines.items()}
    doors = [("d" if m == "c" else m) for m in doors]
    lab.write_settings(machines, move_in="old")
    code, out, secs = lab.tool("apply")
    check(f"`apply` takes c out of etcd and the Sentinels and adds d ({secs / 60:.1f} min); every part is well",
          code == 0 and "taking out c" in out and "adding d" in out, out[-2500:])
    show(out, 30)
    check("d has its own certificate, though the name doesn't point at it", own_certificates(lab, doors, ""))
    check("apps through every door agree", await apps_agree(lab, keys, doors, "replaced"))
    docker("start", lab.name("c"))
    lab.ready("c")
    code, out, _ = lab.tool("retire", f"127.0.0.1:{SSH_PORT + NUMBER['c']}")
    still = lab.on("c", "systemctl is-active etcd texloom texloom-sentinel wg-quick@texloom || true").stdout.split()
    check("c, started again, is retired: nothing of the cluster's runs on it", code == 0 and "active" not in still, out + str(still))
    code, out, _ = lab.tool("status")
    check("and the cluster, without it, is well", code == 0, out[-1500:])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outside", action="store_true", help="old moves in from outside the cluster (and stays outside)")
    ap.add_argument("--keep", action="store_true", help="leave the machines running afterwards (to look at them)")
    ap.add_argument("--clean", action="store_true", help="remove what --keep left, then exit")
    a = ap.parse_args()
    kept = Path(tempfile.gettempdir()) / "texloom-deploy-kept"
    if a.clean:
        Lab(Path(tempfile.gettempdir())).stop()
        shutil.rmtree(kept, ignore_errors=True)
        return 0
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("skipped: Docker isn't running")
        return 0
    with tempfile.TemporaryDirectory(prefix="texloom-deploy-") as t:
        lab = Lab(Path(t))
        try:
            lab.start()
            asyncio.run(rehearse(lab, a.outside))
        finally:
            if a.keep:
                shutil.rmtree(kept, ignore_errors=True)
                shutil.copytree(lab.tmp, kept)
                settings = kept / "cluster" / "cluster.toml"
                if settings.exists():
                    settings.write_text(settings.read_text().replace(str(lab.key), str(kept / "id")))
                print(f"left running; the settings and key: {kept}/cluster/cluster.toml (remove it all: --clean)")
            else:
                lab.stop()
    return summary("deploy rehearsal")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
