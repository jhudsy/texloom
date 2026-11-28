#!/usr/bin/env python3
"""A rehearsal of deploy/install.sh and deploy/cluster.py in Docker: new Debian 13 "machines" set up from nothing, a
server moved in, the cluster grown, broken and mended, and a machine replaced.

Each machine is a container running systemd and SSH and nothing else (deploy/rehearsal/Dockerfile.machine), as a new
VPS would be; a bucket (RustFS) stands in for the provider's. The tools are run as you would run them, from this
checkout. The machines:

  old    one server as install.sh sets it up on its own (SQLite, files in a folder), given a project, a stored file
         and a codes file of the kind servers had before the database kept codes; then moved into the cluster
  a, b   relay, door, PostgreSQL (Patroni), etcd, Valkey, Sentinel
  c      relay, door, etcd, Sentinel (a smaller third); later replaced by d
  d      the same as c, added in c's place

with the strict firewall, and no certificate. In order:

- install.sh on a new machine (the one-machine path), and a project there;
- `plan`, then `apply` with `move_in = "old"`: the cluster is set up and old's server moved in (its project, its stored
  file and its codes reach every door), and `status` says every part is well;
- apps through different doors see each other's changes; `apply` again changes nothing that matters;
- machine a is lost: apps on the other doors carry on, and `status` says what is missing; a comes back, `apply`
  takes it in again;
- c is replaced by d (c killed, the settings changed, `apply`): etcd's members and the Sentinels follow, every part
  is well; c is started again and `retire` stops what the cluster had put on it.

Needs Docker (privileged containers: systemd, WireGuard), about 6 GB free, and the web app's dependencies
(web/node_modules: install.sh builds it). Everything it makes is removed afterwards (--keep leaves it running).

    uv run --extra server python scripts/rehearse_deploy.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
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

from texloom.crypto import Keys, b64e  # noqa: E402

LABEL = "texloom-rehearsal=deploy"
IMAGE = "texloom-lab-machine:1"
NET, SUBNET = "texloom-deploy", "172.31.77"
S3_KEY, S3_SECRET = "rehearsal", "rehearsal-secret-123"
EVERYTHING = ["relay", "door", "postgresql", "etcd", "valkey", "sentinel"]
THIRD = ["relay", "door", "etcd", "sentinel"]
NUMBER = {"a": 11, "b": 12, "c": 13, "d": 14, "old": 15}
SSH_PORT, HTTP_PORT = 2200, 8090  # + the machine's number
OLD_CODE = "AAAA-BBBB-CCCC"  # in old's codes file


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
        for m, n in NUMBER.items():
            docker("run", "-d", "--label", LABEL, "--name", self.name(m), "--hostname", m, "--network", NET, "--ip", f"{SUBNET}.{n}",
                   "--privileged", "--cgroupns=host", "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw", "--tmpfs", "/run", "--tmpfs", "/run/lock",
                   "-p", f"127.0.0.1:{SSH_PORT + n}:22", "-p", f"127.0.0.1:{HTTP_PORT + n}:80", IMAGE)
        for m in NUMBER:
            self.ready(m)
        for _ in range(60):
            if docker("exec", self.name("a"), "bash", "-c", f"exec 3<>/dev/tcp/{SUBNET}.9/9000", check_=False).returncode == 0:
                break
            time.sleep(1)
        docker("run", "--rm", "--network", NET, "--label", LABEL, "curlimages/curl:8.17.0", "-s", "-X", "PUT", "--aws-sigv4",
               "aws:amz:us-east-1:s3", "-u", f"{S3_KEY}:{S3_SECRET}", f"http://{SUBNET}.9:9000/bucket")
        self.settings.parent.mkdir()
        (self.settings.parent / "secrets").mkdir(mode=0o700)
        (self.settings.parent / "secrets" / "s3-secret").write_text(S3_SECRET + "\n")

    def ready(self, m: str) -> None:
        """The machine (started, or started again) answers SSH, with the lab's key."""
        pub = self.key.with_suffix(".pub").read_text().strip()
        for _ in range(60):
            if docker("exec", self.name(m), "systemctl", "is-active", "ssh", check_=False).stdout.strip() == "active":
                break
            time.sleep(1)
        docker("exec", self.name(m), "sh", "-c", f"echo '{pub}' > /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys")

    def write_settings(self, machines: dict[str, list[str]], move_in: str = "") -> None:
        blocks = "".join(f'\n[machines.{m}]\naddress = "{SUBNET}.{NUMBER[m]}"\nssh = "127.0.0.1:{SSH_PORT + NUMBER[m]}"\n'
                         f'runs = [{", ".join(chr(34) + x + chr(34) for x in runs)}]\n' for m, runs in machines.items())
        self.settings.write_text(f"""name = "localhost"
{f'move_in = "{move_in}"' if move_in else ""}

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
certbot = false
""")

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

    def on(self, m: str, script: str) -> subprocess.CompletedProcess:
        return subprocess.run([*self.ssh, "-p", str(SSH_PORT + NUMBER[m]), "root@127.0.0.1", "bash -s"], input=script,
                              capture_output=True, text=True, timeout=600)

    def url(self, m: str) -> str:
        return f"http://localhost:{HTTP_PORT + NUMBER[m]}"


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
    finally:
        for a in apps:
            await a.stop()


def put_file(url: str, keys: Keys, data: bytes) -> str:
    bid = os.urandom(16).hex()
    req = urllib.request.Request(f"{url}/blob/{keys.project}/{bid}", data=data, method="PUT",
                                 headers={"X-Sig": b64e(keys.sign(f"blob:{bid}:".encode() + data))})
    urllib.request.urlopen(req, timeout=30).read()
    return bid


def get_file(url: str, keys: Keys, bid: str) -> bytes | None:
    try:
        return urllib.request.urlopen(f"{url}/blob/{keys.project}/{bid}", timeout=30).read()
    except OSError:
        return None


def code_valid(url: str, code: str) -> bool:
    req = urllib.request.Request(f"{url}/api/code", data=f'{{"code": "{code}"}}'.encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        return b'"ok": true' in urllib.request.urlopen(req, timeout=10).read()
    except OSError:
        return False


def install_old(lab: Lab) -> bool:
    """install.sh on a new machine, as for one server, then a codes file of the older kind."""
    env = {**os.environ, "SKIP_CERT": "1", "SSH_OPTS": " ".join([*lab.ssh[1:], "-p", str(SSH_PORT + NUMBER["old"])])}
    t0 = time.monotonic()
    p = subprocess.run([str(ROOT / "deploy" / "install.sh"), "root@127.0.0.1", "localhost"], env=env, capture_output=True,
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


async def rehearse(lab: Lab) -> None:
    print("one server, set up by install.sh:")
    if not install_old(lab):
        return
    keys = Keys.generate()
    seen = await apps_agree(lab, keys, ["old"], "before")
    data = os.urandom(300_000)
    bid = put_file(lab.url("old"), keys, data)
    check("it keeps a project and a stored file", seen and get_file(lab.url("old"), keys, bid) == data)

    print("a cluster, with that server moved in:")
    lab.write_settings({"a": EVERYTHING, "b": EVERYTHING, "c": THIRD, "old": ["relay", "door"]}, move_in="old")
    code, out, _ = lab.tool("plan")
    check("`plan` says what it would install, and that old's server would move in", code == 0 and "install" in out
          and "move its server in" in out, out[-1500:])
    code, out, secs = lab.tool("apply")
    check(f"`apply` sets up the cluster from nothing and moves old's server in ({secs / 60:.1f} min); every part is well",
          code == 0, "")
    show(out, 60)
    if code != 0:
        return
    check("old's project reaches every door", await apps_agree(lab, keys, ["a", "b", "c", "old"], "moved"))
    check("its stored file too (copied into the bucket)", all(get_file(lab.url(d), keys, bid) == data for d in "abc"))
    check("and its codes file's code is valid through every door (in the database now)",
          all(code_valid(lab.url(d), OLD_CODE) for d in ("a", "b", "c", "old")))

    print("again:")
    code, out, secs = lab.tool("apply")
    check(f"`apply` again ({secs / 60:.1f} min) changes nothing that matters, and every part is well", code == 0, out[-1500:])
    code, out, _ = lab.tool("plan")
    check("`plan` then has nothing to change but the relays' code", code == 0 and "change /" not in out and "write /" not in out
          and "start " not in out and "move its server in" not in out, out[-1500:])

    print("machine a lost:")
    docker("kill", lab.name("a"))
    t0 = time.monotonic()
    ok = await apps_agree(lab, keys, ["b", "c"], "after a")
    check(f"apps through the other doors carry on ({time.monotonic() - t0:.0f} s for two apps to join and agree)", ok)
    code, out, _ = lab.tool("status")
    check("`status` says what is missing", code != 0 and "can't reach it" in out, out[-1500:])

    print("machine a back:")
    docker("start", lab.name("a"))
    lab.ready("a")
    code, out, secs = lab.tool("apply")
    check(f"`apply` takes it in again ({secs / 60:.1f} min), and every part is well", code == 0, out[-2000:])
    check("and apps through every door agree", await apps_agree(lab, keys, ["a", "b", "c"], "back"))

    print("machine c replaced by d:")
    docker("kill", lab.name("c"))
    lab.write_settings({"a": EVERYTHING, "b": EVERYTHING, "d": THIRD, "old": ["relay", "door"]}, move_in="old")
    code, out, secs = lab.tool("apply")
    check(f"`apply` takes c out of etcd and the Sentinels and adds d ({secs / 60:.1f} min); every part is well",
          code == 0 and "taking out c" in out and "adding d" in out, out[-2500:])
    show(out, 30)
    check("apps through a, b and d agree", await apps_agree(lab, keys, ["a", "b", "d"], "replaced"))
    docker("start", lab.name("c"))
    lab.ready("c")
    code, out, _ = lab.tool("retire", f"127.0.0.1:{SSH_PORT + NUMBER['c']}")
    still = lab.on("c", "systemctl is-active etcd texloom texloom-sentinel wg-quick@texloom || true").stdout.split()
    check("c, started again, is retired: nothing of the cluster's runs on it", code == 0 and "active" not in still, out + str(still))
    code, out, _ = lab.tool("status")
    check("and the cluster, without it, is well", code == 0, out[-1500:])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
            asyncio.run(rehearse(lab))
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
