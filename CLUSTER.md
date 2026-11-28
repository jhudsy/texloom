# Several machines

One machine is the place to start, and for many servers the place to stay: [INSTALL.md](INSTALL.md) sets it up with
`deploy/install.sh`, on a small VPS or a Raspberry Pi. This page is for running TeXLoom on several machines, so that
losing one (or its database, or its message bus) costs people a reconnect rather than an outage, and so that it can
grow.

Three ways to run TeXLoom, each adding to the one before:

- **One server** ([INSTALL.md](INSTALL.md)): one machine, set up by `deploy/install.sh`; backups, and a
  [cold standby](INSTALL.md#a-cold-standby) restored from them, cover losing it.
- **A standby, switched by hand** ([INSTALL.md](INSTALL.md#a-standby-switched-by-hand)): a second machine following
  the first's PostgreSQL, which a person promotes in minutes.
- **Several machines** (this page): relays sharing a database kept available by Patroni, and a bus, taking over
  from each other by themselves. The design is in [DESIGN.md](DESIGN.md#several-relays).

Contents: [what runs where](#what-runs-where) · [what you need](#what-you-need) · [the settings](#the-settings) ·
[setting it up](#setting-it-up) · [DNS](#dns) · [certificates](#certificates) · [what's on each machine](#whats-on-each-machine) ·
[day to day](#day-to-day) · [backups](#backups) · [what a failure costs](#what-a-failure-costs) · [moving a server in](#moving-a-server-in) ·
[monitoring](#monitoring) · [growing](#growing) · [load](#load) · [the front door](#the-front-door) ·
[not done yet](#not-done-yet)

## What runs where

| part | what it does | how many |
|---|---|---|
| **relay** | the TeXLoom server, as install.sh sets it up; any relay serves any project | one per machine people reach, two or more |
| **door** | nginx, on a relay's machine: TLS, the web app, and every request sent to a relay that is well (`deploy/door-watch.py` checks each relay's `/api/health` every 2 s) | on each relay's machine |
| **postgresql** | the database, kept available by Patroni: a primary and standbys; a standby is promoted when the primary is lost | two or more |
| **etcd** | where Patroni keeps track of which PostgreSQL is the primary | an odd number: three keeps working with one lost |
| **valkey** | the relays' message bus (what one relay's people send reaches the others' people) | one primary, a replica or more |
| **sentinel** | finds Valkey's primary for the relays, and replaces it when it's lost | three keeps working with one lost |
| **monitoring** | (optional) Prometheus and Grafana, watching every part; alerts by email | one, if any |

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
- **A name** (`app.example.org`) whose DNS lists the doors' addresses ([DNS](#dns)).
- This checkout, with the web app's dependencies (`cd web && npm ci`): the tool builds the web app once and copies
  it to every relay.

**Why WireGuard.** The parts talk to each other unencrypted (the relays to PostgreSQL and Valkey, Patroni to etcd,
the doors to the relays), each on ports of its own. With firewall rules alone, each port needs its list of machines
allowed, and the traffic crosses the network in the clear: acceptable on a provider's private network in one
datacentre, not between datacentres or providers. WireGuard gives every machine a private, encrypted address that
works the same wherever the machines are; every part listens only on it, so the firewall needs only TeXLoom's own
ports closed to everything but WireGuard (or, with `firewall = "strict"`, three rules: SSH, HTTP and HTTPS from
anywhere, everything that comes through WireGuard, nothing else). TLS on each part instead would mean five
certificate set-ups rather than one tunnel; Tailscale is WireGuard with no configuration, at the price of its
coordination service.

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
- `[tls]`: each door's certificate ([below](#certificates)); `certbot = false` if something in front of the doors
  ends TLS (a provider's load balancer).
- `[monitoring]`, with a machine that `runs` it: [below](#monitoring).
- `[backup]`: [below](#backups).
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
writes, then each door's health watcher and certificate, then (if asked) the monitoring. On new machines it takes about ten minutes; it prints the owner's creation
code the first time (keep it). It's safe to run again whenever: it changes only what differs. `status` exits with an
error when something is unwell (or a machine can't be reached), so a script or a timer can watch it.

`scripts/rehearse_deploy.py` does all of this, and the jobs below, on Docker containers standing in for new
machines (with Pebble, Let's Encrypt's test server, for the certificates).

## DNS

The name people use (`name` in the settings) points at every door: one `A` record per door, its public address (and
an `AAAA` record per door with IPv6). Nothing else is needed: no load balancer, no extra machine. Every relay serves
every project, so it doesn't matter which door someone reaches.

```
app.example.org.  300  IN  A  203.0.113.1
app.example.org.  300  IN  A  203.0.113.2
app.example.org.  300  IN  A  203.0.113.3
```

- **Time to live: 300 s** (five minutes). A change reaches everyone within that; lower it to 60 s a day before a
  planned change (moving a server in, replacing a door) and put it back after.
- **A new door**: add its record once `status` says it is well. It gets its certificate before that (the checks pass
  between doors), so it serves HTTPS from its first request.
- **Taking a door out on purpose**: take its record out first, wait the time to live (and a little: some resolvers keep
  records longer), then take it out of the settings and `apply`.
- **A door lost**: take its record out as soon as you can (by hand, or a script using your DNS provider's API), and
  put it back once it is well again. Until then, see [below](#when-a-door-is-down).
- **Moving a server in**: the name keeps pointing at the old server until the doors are well; then point it at the
  doors instead (all of them), and `apply` again so that each gets its own certificate.
- Some DNS providers check health themselves and leave out an address that doesn't answer ("failover" or "health
  checked" records): with one of those, a lost door is taken out for you. A provider's load balancer in front of the
  doors does the same ([the front door](#the-front-door)).

### When a door is down

A door is nginx on a relay's machine, so a door down is usually its machine down: its relay goes with it (and its
PostgreSQL, Valkey, etcd or Sentinel, if it runs them: each has its own failover, [below](#what-a-failure-costs)).
While its address is still in DNS:

- **People connected through it** lose their connection; their apps connect again, through another door, within
  seconds, and send anything not yet stored. Nothing is lost.
- **The desktop app**, connecting, tries the name's addresses in turn, the next a quarter of a second after one doesn't
  answer: it barely notices.
- **Browsers** also try the next address, but some only after waiting for the dead one (up to its connection timeout:
  tens of seconds for a page load), and the web app's own wait to connect is 15 s before it tries again. People who
  open the web app then may see a slow first load, or a short "offline", until the address is taken out of DNS.
- **Certificates**: a door renewing while Let's Encrypt is sent to the dead address fails that try; certbot tries
  twice a day from 30 days before expiry, so it gets through once the address is out of DNS (or Let's Encrypt is sent
  to another door). `status` and the monitoring warn 14 days before.
- **Nothing else depends on it**: the other doors send only to relays that are well, and the rest of the cluster
  carries on (three machines keep working with any one lost).

## Certificates

Each door gets its own certificate for the name from Let's Encrypt, and certbot (its timer) renews it. Let's Encrypt
checks that a door asking for one is reached through the name, by fetching a file from the name's address; with
several doors that may be another door. So a door that hasn't the file passes the request on to the other doors
over WireGuard, and the one that asked answers it. A door can therefore get and renew its certificate whichever door
the name sends Let's Encrypt to, even before its own address is in DNS. Plain HTTP is sent to HTTPS, except these
checks.

`apply` registers each door with Let's Encrypt (`[tls] email` for its notices), which accepts Let's Encrypt's
subscriber agreement. Let's Encrypt issues five certificates a week for one name, so with more than five doors a new
one may wait a few days for its own (`apply` says so). A door that can't get one yet keeps plain HTTP until the next
`apply`, and `status` says so; a door moved in from a server that had a certificate (install.sh's) keeps it, renewed
through the doors from then on.

## What's on each machine

| part | its settings (written by the tool) | its service | its data |
|---|---|---|---|
| WireGuard | `/etc/wireguard/texloom.conf`; the machine's key `texloom.key` (made there, never leaves it) | `wg-quick@texloom` | |
| firewall | `/etc/texloom/firewall.nft` (its own table, `inet texloom`) | `texloom-firewall` | |
| etcd | `/etc/default/etcd` | `etcd` | `/var/lib/etcd/texloom` |
| PostgreSQL | `/etc/patroni/config.yml` | `texloom-patroni` | `/var/lib/postgresql/texloom` |
| Valkey | `/etc/valkey/texloom-valkey.conf` (written once: Sentinels change it), users in `texloom-users.acl` | `texloom-valkey` | none kept (`save ""`): it holds only what is soon out of date |
| Sentinel | `/etc/valkey/texloom-sentinel.conf` (written once: it keeps what it learns) | `texloom-sentinel` | |
| relay | `/etc/texloom/texloom.toml`, `/etc/texloom/admin-token`; with `[backup]`, `backup.env`, `restic-password` (and `rclone.conf`) there | `texloom`, `texloom-backup.timer` | `/var/lib/texloom` (spooled uploads, the TeX Live cache, the backup's copy) |
| door | nginx's site (install.sh's), which includes `/etc/texloom/door/*.conf` (HTTPS, and the certificate checks: `tls.conf`, `acme.conf`); the relays in `/etc/texloom/relays`, the upstream the watcher writes in `/etc/nginx/conf.d/texloom-relays.conf`; the checks passed between doors in `texloom-acme.conf` there | `nginx`, `texloom-door-watch`, certbot's timer | `/etc/letsencrypt` |
| exporters (with `monitoring`) | `/etc/default/prometheus-node-exporter`, `-postgres-exporter`, `-redis-exporter` | those | |
| monitoring | `/etc/prometheus/prometheus.yml`, `texloom-alerts.yml`, `alertmanager.yml`; Grafana's in `/etc/texloom/grafana.env` and `/etc/grafana/provisioning/` | `prometheus`, `grafana-server`, `prometheus-alertmanager` | `/var/lib/prometheus`, `/var/lib/grafana` |

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
  missing. If it was a door, take its address out of DNS ([when a door is down](#when-a-door-is-down)). If it comes
  back, `apply` takes it in again. If it won't, take it out (above): until then, etcd counts it, so one more loss would
  stop the database failing over. If it may be in someone else's hands (stolen, its provider account misused), change
  the secrets too (next).
- **Changing every secret** (`deploy/cluster.py rotate cluster.toml`): after taking out a machine that is lost or no
  longer trusted, or now and then. It changes every secret the tool made (the database's and Valkey's passwords, the
  admin token, the monitoring's, the backups' password) while everything runs: Valkey's users take the new passwords
  beside the old until every part has them, PostgreSQL's roles change and their users follow at once, and the relays
  restart one at a time (their apps reconnect to the others). Afterwards the old secrets work nowhere; keep a copy of
  the new `secrets/`. If it stops partway, run it again: it carries on with the same new secrets. It can't change a
  provider's keys (the bucket's, the backups' storage, a mail server's): make a new one there, put it in its file,
  `apply`, and delete the old one there. A lost machine's WireGuard key stops working when it leaves the settings (the
  others forget it); a lost door's certificate stays valid until it expires (90 days at most; Let's Encrypt can revoke
  it from its account if you need that sooner).
- **Backups**: [below](#backups).

## Backups

The PostgreSQL standbys aren't backups: a mistake or a deletion reaches them within a second. With `[backup]` in the
settings, `apply` sets up the same hourly backup as on one server ([INSTALL.md](INSTALL.md#backups):
`deploy/texloom-backup`, with restic) on every relay. Each tries every hour; the first to begin makes the hour's
backup (the database says who began one last), and the others leave it, so backups go on whichever machines are
lost.

```toml
[backup]
repository = "b2:my-bucket:texloom"      # restic's: anywhere it can write, at another provider than the machines'
keys_file = "secrets/backup-keys"         # its keys, as lines KEY=value (B2_ACCOUNT_ID=…, B2_ACCOUNT_KEY=…)
healthcheck_url = "https://hc-ping.com/…"  # (optional) pinged after each backup: so you hear when they stop

[backup.bucket_copy]                      # (optional) the stored files copied to a bucket at another provider
url = "s3://texloom-copy"
endpoint = "https://s3.example.net"
key_id = "…"
secret_file = "secrets/copy-secret"
```

Each backup is the database as it was at one moment (as an SQLite file, whatever the database is), the settings,
the certificates, and the stored files: either copied to the second bucket (keep old versions there, with its
lifecycle rules, or a file deleted by mistake is deleted from the copy too), or, without `bucket_copy`, copied into a
folder on the relay making the backup (as big as every stored file, and a new relay starts by copying them all) and
sent with restic. The tool makes the restic password, `secrets/restic-password`, the first time: **keep a copy of
`cluster.toml` and `secrets/` somewhere else** (a password manager): every machine's settings are written from them,
and the backups can't be read without that password.

Nothing else needs backing up: Valkey holds only what is soon out of date (live messages, who is connected where),
etcd only which PostgreSQL is the primary, each relay's own folder only uploads in progress and its TeX Live cache
(fetched again), WireGuard's keys are made again by a new machine, and the dashboards come from this repository
(Prometheus's history would be lost).

**Restoring**: `restic restore latest --target /root/restore` on a relay; stop the relays (`systemctl stop texloom`
on each); empty the database (on the primary: `DROP DATABASE texloom`, `CREATE DATABASE texloom OWNER texloom`) and put
the copy into it (`texloom-server --config /etc/texloom/texloom.toml --copy-database-from
sqlite:/root/restore/var/lib/texloom/backup/texloom.db`; the standbys follow by themselves); put back into the bucket
what it lacks, if it lost files (`--files-restore` the restored folder, or copy them back from the second bucket); and
start the relays. Or build a new cluster and move in a server restored from the backup (a
[cold standby](INSTALL.md#a-cold-standby) is one). Either way people lose nothing made since the backup: each app finds
that the server's log isn't the one it saw, and sends its changes again.

## What a failure costs

Editing never waits: every app holds the whole project and edits locally. What a failure delays is collaborators
seeing each other's changes, and changes reaching the database. It never loses a change: each one is either
acknowledged after it is stored and passed on, or kept by its writer and sent again.

Measured in the Docker lab (`scripts/rehearse_relays.py`, on one computer, with Patroni's and Sentinel's default
timings), in seconds from the break:

| what broke | back after |
|---|---|
| the PostgreSQL primary killed / frozen / cut off | writes through the relays' address work again after 26 / 29 / 25 s. A cut-off primary demoted itself after 14 s, so there were never two. The new timeline showed within 2 s of writes resuming |
| the Valkey primary killed / frozen / cut off | a replica was promoted and took writes after 7 / 7.5 / 7.5 s. The old one, once mended, still took writes for about 10 s and lost them when it rejoined (soft state only, so harmless) |
| a whole machine, holding both primaries | Valkey after 7–8 s, PostgreSQL after 24–27 s; etcd kept its majority |
| a relay, seen through nginx (`least_conn`, `max_fails=1`, `fail_timeout=5s`) | nginx left a killed relay out after 3–8 s, a frozen one after 10 s, a cut-off one after 11–19 s. No request through nginx failed |

And with people editing (`rehearse_relays.py collab`: three apps editing one stored project, each on a relay of its
own, and two on a sync-only project, all through nginx; every app makes a change at the break and another 5 s
later), seconds from the break until every app has every change:

| what broke | stored project | sync-only project |
|---|---|---|
| a relay killed | 5.5 | 5.5 |
| a relay frozen | 24 (its apps notice by their keep-alive in 10 s; nginx then sent one back to the frozen relay first, 10 s more: measured before the doors' health watcher, which now keeps it from doing so) | 5.3 |
| a relay cut off | 23 | 5.5 |
| the Valkey primary killed | 5.9 | 6.5 (29 with 0.4.40 apps, which answer a peer's request for what it lacks at most once every 30 s) |
| the PostgreSQL primary killed | 33 (mostly Patroni's 30 s leader key, which can be tuned) | 5.5 |
| a whole machine, holding both primaries, killed / cut off | 40 / 35 | 12 / 7.7 |

Once mended, every change made reached every app within 0.2 s, and a newcomer catching up from the database alone
got every change.

## Moving a server in

A server install.sh set up (with SQLite or its own PostgreSQL, files in a folder or the bucket, codes in files or its
database) can move into a new cluster, bringing its projects, stored files and codes, in one of two ways:

- **As one of the cluster's machines** (the easy way to grow from one server): list it under `[machines]` with what it
  should run (`relay`, `door`; not `postgresql` if it has PostgreSQL databases of its own), and add
  `move_in = "its name"`. It becomes one of the relays, and keeps its certificate.
- **From outside the cluster** (a machine that runs other things too, or not Debian 13): `[move_in] ssh = "host"`.
  Once its copy is in, its server is stopped and won't start again by itself; its data, nginx and the rest are left as
  they were. Until the name points at the doors, they use its certificate (Let's Encrypt's checks still reach it, not
  them); `apply` again afterwards gets them their own.

Either way, `apply` stops its relay (keeping its settings as `/etc/texloom/texloom.toml.before-cluster`), copies its
database into a file beside it, puts its stored files into the bucket (unless they're there already), then, once the
first relay is installed, puts that copy into the cluster's database and imports its codes files. `plan` says what it
would do, and which of its settings the cluster's relays wouldn't have (add those to `[relay]`). It works only into a
new cluster, whose database is still empty; if a step fails, its server is started again as it was and the cluster's
database emptied, so `apply` can try again. Nothing is deleted from it.

People's apps reconnect by themselves; anything changed during the minutes the server was stopped is sent again
(as after a restore from a backup). Point the name at the doors once `status` is well (lower its DNS TTL a day
before, so the change is quick).

## Monitoring

Optional, and never needed on one machine: `status` and each relay's admin page say what's wrong now. Monitoring
keeps the history (last night's load, a disk filling over a week) and tells you by email when something stays wrong
for a few minutes, because recovery by itself can hide a slow decline: a standby no longer copying, a Sentinel down
(so the next failover would fail), a certificate not renewing.

Add `"monitoring"` to one machine's `runs` (best a small one of its own, so that it still reports when the others are
in trouble; it needs about 0.5 GB of memory, and Grafana alone is a download of a few hundred MB and about a GB of
disk; `[monitoring] grafana = false` leaves it out), and `[monitoring]` for email ([the example](deploy/cluster.example.toml)).
`apply` then installs, from Debian's packages:

- on every machine, **node_exporter** (CPU, memory, disk, network, and whether each of the cluster's services runs), and
  PostgreSQL's and Valkey's exporters where those run; Patroni, etcd and the relays have their own `/metrics`. All
  listen on WireGuard only.
- on the monitoring machine, **Prometheus** (every part every 15 s, kept 30 days), the alert rules in
  [`deploy/monitoring/alerts.yml`](deploy/monitoring/alerts.yml) (a machine, service or relay down; the bus or database
  away; stores slow; no PostgreSQL primary, or a standby not copying; etcd without a leader; a Valkey replica missing;
  a disk over 80% full; memory low; a certificate within 14 days of expiring), **Alertmanager** if alerts are emailed,
  and **Grafana** (from Grafana's own repository; Debian has none) with [the dashboard](deploy/monitoring/dashboard.json):
  TeXLoom's numbers (connections, messages and stores a second, how long a store takes, catch-ups, the bus and the
  database, app versions) beside the machines', PostgreSQL's, etcd's and Valkey's.

Prometheus and Grafana listen on the monitoring machine only. To look:

```bash
ssh -L 3000:127.0.0.1:3000 root@MONITORING-MACHINE    # then http://localhost:3000, user admin, the password in secrets/grafana-admin
```

The relay's numbers ([`server/metrics.py`](server/metrics.py)) say nothing of who works on what: no project ids, codes
or addresses. Keep an uptime check of the site from outside as well (any free one): it notices when the monitoring
itself is down.

## Growing

High availability starts at about three machines, because etcd and the Sentinels decide by majority; relays,
etcd members and Sentinels can share those machines. Past that, each part grows by its own means:

- **Relays** are interchangeable, so they grow by adding relays: a machine in the settings, and `apply` (every
  door is told). Taking one away moves its connections to the others as it stops. Relays first, because they're
  the part that grows with people. (Adding and replacing them by themselves, from load and health through a
  provider's API, isn't built: [ROADMAP.md](ROADMAP.md).)
- **PostgreSQL** grows by more standbys (more copies) and a bigger machine for writes. Past that, in order: a
  connection pooler ([INSTALL.md](INSTALL.md#the-database) says what it must allow), batching edits so a burst of
  typing is one write, read replicas for catch-ups, and then projects split across databases by project id. None
  of these is built; each is changed by hand, rarely.
- **Valkey**: one primary carries a lot ([Load](#load)); its replicas are for failover. If it ever needs more, in
  order: send less through it (the doors keeping a project's people on one relay, below); its I/O threads
  (`io-threads`: the network fills before its CPU); several independent Valkey groups, each relay choosing one by
  a hash of the project id (nearly everything on the bus is per project; the relay-wide parts, the list of relays
  alive, each relay's own channel and its numbering, would stay on one group). Valkey Cluster's sharded pub/sub
  (`SPUBLISH`, with each project's keys and channel on one shard) does the same split, but the relay's own client
  would have to learn the cluster's redirects; plain `PUBLISH` is copied to every node of a cluster, so it doesn't
  grow that way.
- **Kubernetes** isn't needed: the parts are few, and each has its own way to stay available.

## Load

`scripts/loadtest.py` with `--relays 3`: relays on one machine (a laptop: 10 fast and 4 slow cores) sharing a
throwaway PostgreSQL and a Valkey in Docker, simulated apps speaking the real protocol, each project's people put on
different relays (the worst case: everything they send each other crosses the bus). An edit is "too slow" when the
slowest 5% take over 200 ms to arrive.

| | one relay | three relays, people spread | three relays, each project on one |
|---|---|---|---|
| edits a second before too slow (3 people a project, 2 typing) | 800 (1,600 fails: CPU 100%) | 2,600 (busiest relay 90%, p95 10 ms) | 2,600 (busiest 86%, p95 7 ms) |
| CPU at 800 edits a second, all relays | 56% | 120% | 102% |
| Valkey at 2,600 edits a second | | 16% of a core, 5.1 MB/s out | 13%, 1.7 MB/s |
| one project, 100 people typing (200 edits/s, 19,800 deliveries/s) | 78%, p95 17 ms | busiest 40%, p95 8 ms | (one relay) |
| large peer-to-peer pieces (4 MiB) between people on different relays, while others edit | 64 MB/s, edits p95 47 ms | 64 MB/s (Valkey sending 171 MB/s), edits p95 102 ms; at 96 MB/s offered, edits slow to 563 ms | (none cross) |

Every change reached everyone in every row that wasn't too slow. What it says:

- **Several relays carry more, but not in proportion**: three carried about twice what one did. Each edit costs
  more CPU in all: every message is published, and Valkey sends it back to the relay that published it too; and
  here three relays, PostgreSQL, Valkey and the simulated apps shared one laptop, which separate machines don't.
- **The relays fill before Valkey does**: at 2,600 edits a second the busiest relay was at 90% and Valkey at 16%.
  Valkey's cost doesn't grow with the number of relays, since a project's messages go only to the relays with
  someone in it. What fills first is its network, with large peer-to-peer transfers (files in sync-only projects):
  at 64 MB/s of them it sent 171 MB/s, and other people's edits slowed (one connection per relay carries both).
- **Try keeping each project's people on one relay first** if the bus's bandwidth ever matters: nginx's `hash
  $request_uri consistent` for `/ws/`, instead of `least_conn`, cut the bus's traffic to a third and edits' p95
  from 10 to 7 ms, with little CPU saved. It costs balance (a busy project loads one relay) and moves a dead relay's
  projects as a group, so `least_conn` stays the default.

## The front door

The doors (nginx on each relay's machine, sending to every relay that is well) with DNS listing all of them are
the simplest front door, free and with no extra machine: apps and browsers try the next address when one doesn't
answer, so a machine that is off costs little, and a frozen one costs a connect timeout (15 s) before the next
address is tried. DNS isn't health-aware, so a dead machine's address stays until it is taken out (by hand, or a
script using the DNS provider's API, with a lifetime of a minute or so). Good for two or three machines. Other ways,
in front of the same doors:

- **A provider's load balancer**: it checks health itself (`/api/health`), balances by connection count, and is
  kept available by the provider. The best value while every machine is at one provider; it can't span providers.
- **A floating address with keepalived** (VRRP) between two door machines: one holds the address, the other takes
  it when the first stops answering. Within one provider and datacentre only, and more to run.
- **A proxying CDN with load balancing**: it spans providers, checks health and absorbs abuse, but is usually paid
  and sees every request (only who connects to which project id, and when: the content is end-to-end encrypted).

Whatever is in front, nginx needs the doors' health watcher (`deploy/door-watch.py`): open-source nginx learns a
relay is down only when a request to it fails, and would send a reconnecting app straight back to a frozen relay.
(HAProxy in nginx's place would do that one job itself.)

## Not done yet

- **Other systems than Debian 13** for the cluster's machines (a server moving in from outside can run anything
  install.sh does).
- **Adding or replacing machines by themselves**, from load and health, through a provider's API.
