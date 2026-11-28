# Several machines

One machine is the place to start, and for many servers the place to stay: [INSTALL.md](INSTALL.md) sets it up with
`deploy/install.sh`, on a small VPS or a Raspberry Pi. This page is for running TeXLoom on several machines, so that
losing one (or its database, or its message bus) costs people a reconnect rather than an outage, and so that it can
grow. The design behind it, and what was measured, is in [FUTURE_ARCHITECTURE.md](FUTURE_ARCHITECTURE.md).

Contents: [what runs where](#what-runs-where) · [what you need](#what-you-need) · [the settings](#the-settings) ·
[setting it up](#setting-it-up) · [what's on each machine](#whats-on-each-machine) · [day to day](#day-to-day) ·
[moving a server in](#moving-a-server-in) · [not done yet](#not-done-yet)

## What runs where

| part | what it does | how many |
|---|---|---|
| **relay** | the TeXLoom server, as install.sh sets it up; any relay serves any project | one per machine people reach, two or more |
| **door** | nginx, on a relay's machine: TLS, the web app, and every request sent to a relay that is well (`deploy/door-watch.py` checks each relay's `/api/health` every 2 s) | on each relay's machine |
| **postgresql** | the database, kept available by Patroni: a primary and standbys; a standby is promoted when the primary is lost | two or more |
| **etcd** | where Patroni keeps track of which PostgreSQL is the primary | an odd number: three keeps working with one lost |
| **valkey** | the relays' message bus (what one relay's people send reaches the others' people) | one primary, a replica or more |
| **sentinel** | finds Valkey's primary for the relays, and replaces it when it's lost | three keeps working with one lost |

Stored files go in an S3-compatible bucket every relay uses ([INSTALL.md](INSTALL.md#stored-files)); creation codes are
in the database, so every relay has the same.

Three machines running everything (a smaller third can run just a relay, a door, etcd and a Sentinel) keep working
when any one of them is lost: the apps on it reconnect to another door within seconds, a lost PostgreSQL primary is
replaced in about half a minute (Patroni's own wait), a lost Valkey primary in a few seconds.

## What you need

- **Machines running Debian 13**, reached over SSH as root with a key (as install.sh is), each with its own hostname.
  A cheap VPS each is enough to start; they needn't be alike.
- **UDP port 51820 open between them** (WireGuard: the machines talk to each other only through it), and **ports 80
  and 443** open to everyone on the doors.
- **A bucket** for stored files (with more than one relay, a folder can't be shared).
- **A name** (`app.example.org`) whose DNS lists the doors' addresses. Browsers and apps try the next address when one
  doesn't answer, so a lost machine costs little; take its address out of DNS when you take it out of the cluster.
- This checkout, with the web app's dependencies (`cd web && npm ci`): the tool builds the web app once and copies
  it to every relay.

## The settings

One file, kept somewhere private (it names your machines): start from
[`deploy/cluster.example.toml`](deploy/cluster.example.toml), which explains each setting.

```toml
name = "app.example.org"

[ssh]
key = "~/.ssh/texloom"

[machines.a]
address = "203.0.113.1"
runs = ["relay", "door", "postgresql", "etcd", "valkey", "sentinel"]
[machines.b]
address = "203.0.113.2"
runs = ["relay", "door", "postgresql", "etcd", "valkey", "sentinel"]
[machines.c]
address = "203.0.113.3"
runs = ["relay", "door", "etcd", "sentinel"]

[bucket]
url = "s3://my-bucket"
endpoint = "https://s3.example.org"
key_id = "…"
secret_file = "secrets/s3-secret"

[relay]
default_allowance = "0"
```

- `address` is where the other machines reach a machine (a provider's private network if they share one, else its
  public address); `ssh = "host:port"` if SSH reaches it elsewhere.
- `[relay]` holds settings for every relay, as in [`texloom.example.toml`](texloom.example.toml); `[machines.a.relay]`
  for one. The tool sets the database, the bus, where the relay listens, the bucket and the admin token itself.
- `[network] firewall`: `"ports"` (the default) closes TeXLoom's own ports to everything but WireGuard and leaves the
  rest of the machine alone (other services on it are unaffected); `"strict"` lets in nothing but SSH, HTTP(S) and
  WireGuard; `"off"`.
- **Secrets** (the database's, Valkey's and the admin passwords) are made by the first `apply` in a folder
  `secrets/` beside the settings, never in them. Keep that folder private, and a copy of it safe: it's all that's
  needed to reach the parts.

## Setting it up

```bash
deploy/cluster.py plan   cluster.toml     # what would change (changes nothing)
deploy/cluster.py apply  cluster.toml     # make it so
deploy/cluster.py status cluster.toml     # every part's health, machine by machine
```

`apply` installs each part's packages, puts the machines on their private network, starts etcd, PostgreSQL (and
makes the relays' role and database), Valkey and its Sentinels, then each relay with install.sh and the settings it
writes, then each door's health watcher. On new machines it takes about ten minutes; it prints the owner's creation
code the first time (keep it). It's safe to run again whenever: it changes only what differs. `status` exits with an
error when something is unwell (or a machine can't be reached), so a script or a timer can watch it.

`scripts/rehearse_deploy.py` does all of this, and the jobs below, on Docker containers standing in for new
machines.

## What's on each machine

| part | its settings (written by the tool) | its service | its data |
|---|---|---|---|
| WireGuard | `/etc/wireguard/texloom.conf`; the machine's key `texloom.key` (made there, never leaves it) | `wg-quick@texloom` | |
| firewall | `/etc/texloom/firewall.nft` (its own table, `inet texloom`) | `texloom-firewall` | |
| etcd | `/etc/default/etcd` | `etcd` | `/var/lib/etcd/texloom` |
| PostgreSQL | `/etc/patroni/config.yml` | `texloom-patroni` | `/var/lib/postgresql/texloom` |
| Valkey | `/etc/valkey/texloom-valkey.conf` (written once: Sentinels change it), users in `texloom-users.acl` | `texloom-valkey` | none kept (`save ""`): it holds only what is soon out of date |
| Sentinel | `/etc/valkey/texloom-sentinel.conf` (written once: it keeps what it learns) | `texloom-sentinel` | |
| relay | `/etc/texloom/texloom.toml`, `/etc/texloom/admin-token` | `texloom` | `/var/lib/texloom` (spooled uploads, the TeX Live cache) |
| door | nginx's site (install.sh's); the relays in `/etc/texloom/relays`, the upstream the watcher writes in `/etc/nginx/conf.d/texloom-relays.conf` | `nginx`, `texloom-door-watch` | |

Everything listens on the machine's WireGuard address (or only on the machine itself), except nginx and SSH. Valkey
has a user for the relays, allowed only the commands they send and keys and channels starting `texloom:`. Changes
made by hand to a file the tool writes are replaced at the next `apply`; change the settings instead.

## Day to day

- **Updating TeXLoom**: `apply` from an up-to-date checkout (it runs install.sh on each relay in turn: the first, then
  the rest at once; apps on each reconnect within seconds).
- **Creation codes**: on any relay's machine, as for one server ([INSTALL.md](INSTALL.md#creation-codes)), e.g.
  `sudo -u texloom /var/www/texloom/.venv/bin/texloom-server --config /etc/texloom/texloom.toml --add-code alice
  --allowance 2G`. Every relay has a new code within seconds.
- **Adding a machine**: add it to the settings, `apply`. A new PostgreSQL copies the primary and becomes a standby; a
  new etcd member is added to the cluster before it starts; a new Valkey follows the primary; the Sentinels' quorum
  follows their number; every door starts sending to a new relay. Add its address to DNS once `status` is well.
- **Taking a machine out** (or replacing one: take one out and add another in the same `apply`): take it out of DNS,
  then out of the settings, then `apply`. etcd forgets the member (so the rest keep their majority), the Sentinels
  forget its Valkey and Sentinel, PostgreSQL and the doors stop using it. If the machine is still reachable,
  `deploy/cluster.py retire cluster.toml ADDRESS` stops and disables what the cluster put on it, so that it can't come
  back and rejoin; its data is left in place (wipe it before reusing the machine).
- **A machine lost** (dead, unreachable): nothing needs doing at once; the rest carry on, and `status` says what's
  missing. If it comes back, `apply` takes it in again. If it won't, take it out (above): until then, etcd counts it,
  so one more loss would stop the database failing over.
- **Backups**: as for one server ([INSTALL.md](INSTALL.md#backups)), from any machine: `pg_dump` of the database
  through the relays' address (it reaches the primary), and the bucket. Valkey holds nothing to back up; etcd only
  which PostgreSQL is the primary. Keep a copy of the settings and `secrets/`.

## Moving a server in

A server install.sh set up (with SQLite or its own PostgreSQL, files in a folder or the bucket, codes in files or its
database) can become one of a new cluster's machines, bringing its projects, stored files and codes: list it under
`[machines]` with what it should run (`relay`, `door`; it can't run `postgresql` if it has PostgreSQL databases of its
own), and add `move_in = "its name"`. Before starting the new relays, `apply` stops its relay, keeps its settings as
`/etc/texloom/texloom.toml.before-cluster`, copies its database into the cluster's, puts its stored files into the
bucket (unless they're there already) and imports its codes files, then sets it up as a relay. `plan` says what it
would do, and which of its settings the cluster's relays wouldn't have (add those to `[relay]`). It works only into a
new cluster, whose database is still empty; if a copy fails, its server is started again as it was and the cluster's
database emptied, so `apply` can try again. Nothing is deleted from it.

People's apps reconnect by themselves; anything changed during the minutes the server was stopped is sent again
(as after a restore from a backup). Point the name at the other doors as well once `status` is well.

## Not done yet

- **A certificate on several doors.** The tool runs certbot on the first door only (`[tls] certbot`); the others need
  the same certificate. Until the tool copies it (or uses a DNS challenge), run with one door, or a load balancer in
  front that terminates TLS.
- **Moving in from a machine that stays outside** the cluster (one running other things, or not Debian 13): today the
  machine moved in becomes a member. Meanwhile, copy the database by hand (`texloom-server --copy-database-from`).
- **Other systems than Debian 13**, and adding or replacing machines by themselves from load and health through a
  provider's API.
- **Dashboards** (optional, never needed on one machine): FUTURE_ARCHITECTURE.md, "Health checks and monitoring".
