# Running a TeXLoom server

A TeXLoom server is one Python process: it serves the web app and the in-browser TeX files, and
relays and stores projects' encrypted changes. It can't read any project. It needs:

- **a database** for the edit logs, project records and project lists: an SQLite file, which
  needs nothing installed (the default), or PostgreSQL (14 or later) for a standby or several
  servers ([The database](#the-database));
- **a place for stored files** (pictures, PDFs, uploads, all encrypted by the apps): a folder on
  the server, or an S3-compatible bucket;
- **HTTPS** in front of it for anyone but yourself, because browsers only allow the encryption the
  editor relies on over secure connections.

Settings live in one file, `/etc/texloom/texloom.toml` ([Settings](#settings)).

- [On an Ubuntu or Debian server](#on-an-ubuntu-or-debian-server) (the install script)
- [Other ways to give it HTTPS](#other-ways-to-give-it-https)
- [Installing by hand](#installing-by-hand), and [on your own computer](#on-your-own-computer)
- [Settings](#settings)
- [The database](#the-database) · [Stored files](#stored-files)
- [Creation codes](#creation-codes) · [Private servers](#private-servers) ·
  [Retiring old app versions](#retiring-old-app-versions) · [Idle projects, admin page, arXiv import](#idle-projects-admin-page-arxiv-import)
- [Backups](#backups) · [A standby server](#a-standby-server), by hand or [by themselves](#letting-the-two-servers-switch-over-by-themselves), and [moving traffic to the primary](#moving-traffic-to-the-primary) · [Updating](#updating)

## On an Ubuntu or Debian server

[`deploy/install.sh`](deploy/install.sh) installs or updates a server from a checkout on your own
computer, over ssh. You need a domain name pointing at the server, and on your computer
[uv](https://docs.astral.sh/uv/) and Node.js 22.

```bash
git clone https://github.com/jhudsy/texloom && cd texloom
(cd web && npm ci)
deploy/install.sh root@server.example.org texloom.example.org
```

It works alongside other sites on the machine and only adds things:

| what | where |
|---|---|
| a `texloom` system user | |
| the app and its Python environment | `/var/www/texloom` (`TEXLOOM_DIR=…` for elsewhere) |
| the database (SQLite), stored files, TeX Live packages fetched on demand | `/var/lib/texloom` |
| settings, creation codes, the admin password | `/etc/texloom/texloom.toml`, `codes.txt`, `admin-token` |
| a sandboxed systemd service on 127.0.0.1:8787, using at most 1 GB of memory (`MemoryMax`) | `texloom.service` |
| an nginx site with rate limits and security headers, and a Let's Encrypt certificate | `/etc/nginx/sites-available/<domain>` |

The first run prints the owner's creation code (unlimited storage) and where the admin page is.
`texloom.toml` is written once, from then on it's yours: later runs don't change it. Options:
`SSH_OPTS="-p 2222"`, `SKIP_CERT=1` (no certificate yet, e.g. before DNS points at the server),
`TEXLIVE_MIRROR=https://…/systems/texlive/tlnet/` (a TeX Live mirror near the server, used first),
`DATABASE=postgresql` (first install only: PostgreSQL instead of SQLite, with a `texloom` database
the `texloom` user reaches over the local socket, no password).

Then [set up backups](#backups): the script keeps none.

## Other ways to give it HTTPS

| option | what you need | result |
|---|---|---|
| **nginx + Let's Encrypt** (the install script) | an Ubuntu/Debian server with a domain name | as above |
| **Caddy** ([deploy/Caddyfile](deploy/Caddyfile)) | a domain name pointing at the server, ports 80/443 open | Caddy gets and renews the certificate itself |
| **Cloudflare Tunnel** | a free Cloudflare account | HTTPS with no open ports, e.g. for a Raspberry Pi at home: `cloudflared tunnel --url http://localhost:8787` |
| **Tailscale Funnel** | Tailscale on the server | `tailscale funnel 8787`, also with no open ports |

People only ever make outgoing HTTPS connections to the server, so their firewalls need no changes.

The server limits what one client may do (connections, arXiv imports, TeX Live downloads) by its
address, counting an IPv6 address's /64 as one. Behind a proxy on the same computer it takes the
address from the `X-Real-IP` header, so the proxy must set it from the real client, replacing any
the client sent (the nginx site and the Caddyfile do). Behind a tunnel that passes on a client's
own `X-Real-IP`, a client could choose the address it counts as. Requests from the same computer
without it aren't limited per address.

## Installing by hand

```bash
git clone https://github.com/jhudsy/texloom && cd texloom
uv sync --extra server                      # Python: the server (and the desktop app's folder sync)
cd web && npm ci && npm run build           # the web app -> web/dist
npm run download-tex                        # in-browser TeX (~685 MB, into ../tex-assets)
cd ..
```

Write a settings file (copy [`texloom.example.toml`](texloom.example.toml) and change what differs;
[Settings](#settings)) and start it. Its database is an SQLite file in `data` unless the settings
name another ([The database](#the-database)).

```bash
uv run texloom-server --config /etc/texloom/texloom.toml
```

For every TeX Live package in in-browser compiles (otherwise only the bundles in `tex-assets`),
build the package index once with `texlive = "/var/lib/texloom/texlive"` in the settings:

```bash
uv run texloom-server --config /etc/texloom/texloom.toml --texlive-init   # ~2 s; re-run to refresh
```

### On your own computer

For trying it out or working on it:

```bash
uv run --extra server texloom-server --data data     # its database: data/texloom.db
```

To try it with PostgreSQL, [`scripts/pg.py`](scripts/pg.py) runs one of its own in a folder (it
needs PostgreSQL's programs: `brew install postgresql@17` or `apt install postgresql`, nothing set
up as a service):

```bash
uv run --extra server python scripts/pg.py data/pg -- uv run --extra server texloom-server --data data
```

Then open http://localhost:8787. Browsers treat `localhost` as secure, so no certificate is needed.

## Settings

Every option of `texloom-server` (`texloom-server --help` lists them) can be set in the settings
file under its name, with `_` for `-`; [`texloom.example.toml`](texloom.example.toml) lists them
all, and is what the install script starts from. The file is `--config`, else `TEXLOOM_CONFIG`,
else `./texloom.toml`, else `/etc/texloom/texloom.toml`, if they exist (a `texloom.toml` in the
checkout is ignored by git). Tables such as `[s3]` go last: in TOML, every line after one belongs
to it. An environment variable `TEXLOOM_<NAME>`
(`TEXLOOM_MIN_VERSION=0.4.30`) overrides the file, and the command line overrides both. A setting
the server doesn't know is an error, so a typo doesn't go unnoticed. After a change:
`systemctl restart texloom`.

```toml
database = "sqlite:///var/lib/texloom/texloom.db"
data = "/var/lib/texloom"
codes_file = ["/etc/texloom/codes.txt"]
admin_token_file = "/etc/texloom/admin-token"
min_free = "2G"
```

| setting | default | what it does |
|---|---|---|
| `database` | `texloom.db` in `data` | an SQLite file or a PostgreSQL database ([The database](#the-database)) |
| `data` | `data` | this server's own folder: uploads are spooled here, `min_free` is kept free here, and stored files go in `data/files` unless `files` says otherwise |
| `files`, `[s3]` | `data/files` | where stored files go ([Stored files](#stored-files)) |
| `host`, `port` | `127.0.0.1`, `8787` | where it listens |
| `static`, `tex_assets` | `web/dist`, `tex-assets` | the built web app, and the in-browser TeX files |
| `codes_file` | none | creation codes and allowances ([Creation codes](#creation-codes)); a list. `TEXLOOM_CODES_FILES` adds files to it |
| `min_free` | `2G` | refuse to store anything when the disk of `data` has less free |
| `project_max_bytes` | `200M` | most one project may store (except with an unlimited allowance) |
| `memory` | `512M` | memory for big work at once (catch-ups, large messages, uploads, arXiv imports); more waits its turn. The process needs about 500M on top, so raise `MemoryMax` in `texloom.service` with it |
| `max_bytes` | unlimited | most the server stores in all |
| `private` | `false` | only projects created with a creation code may use the server ([Private servers](#private-servers)) |
| `min_version` | none | the oldest app allowed ([Retiring old app versions](#retiring-old-app-versions)) |
| `codes_url` | none | a page saying how to get a creation code; the apps link to it |
| `dropbox_app_key` | none | the app key of a Dropbox app for this server (README → Dropbox): lets the web app keep projects in step with Dropbox folders |
| `evict_idle_days` | `0` (never) | drop the stored copy of projects nobody opened for this long |
| `purge_expired_after` | never | drop the stored copies of projects whose code expired this many days ago |
| `admin_token` or `admin_token_file` | none | the password for `/admin` |
| `no_project_lists`, `no_arxiv` | `false` | turn off storing project lists, or importing from arXiv |
| `texlive`, `texlive_max`, `texlive_mirror` | none, `4G`, none | TeX Live packages on demand: the cache folder, its size (the packages used least recently make room), mirrors to try first (a list) |

Secrets (the database password, the S3 secret key, the admin token) belong in the settings file,
readable only by the server's user (the install script makes it `root:texloom`, mode 640), or in
the environment. On the command line they'd show in `ps`.

## The database

`database` is one of:

- **an SQLite file**: `sqlite:///var/lib/texloom/texloom.db` (`sqlite:` and the path; a relative
  path, `sqlite:data/texloom.db`, is from the folder the server starts in). Without the setting,
  `texloom.db` in `data`. Nothing to install or look after, and the install script's choice. One
  server process uses it: a second one started on the same file refuses to run (one-off commands
  such as `--list-codes` can use it while the server runs).
- **PostgreSQL** (14 or later), for a warm standby, replicas, or several servers sharing it:
  - `postgresql:///texloom?host=/var/run/postgresql`: on this machine, over the local socket, as
    the user the server runs as (no password anywhere; the install script's choice with
    `DATABASE=postgresql`);
  - `postgresql://texloom:PASSWORD@db.example.org:5432/texloom?sslmode=require`: elsewhere.

  The server uses up to 10 connections.

The server creates its tables when it starts, either way. To move a server from one to the other
(SQLite up to PostgreSQL, say), stop it, copy everything into the new, empty database, point the
settings at it, and start it again:

```bash
systemctl stop texloom
sudo -u texloom texloom-server --config /etc/texloom/texloom.toml \
  --database "postgresql:///texloom?host=/var/run/postgresql" \
  --copy-database-from sqlite:///var/lib/texloom/texloom.db
# then database = "postgresql:///texloom?host=/var/run/postgresql" in texloom.toml
systemctl start texloom
```

(After the install script, the program is `/var/www/texloom/.venv/bin/texloom-server`.) The old
database is left as it was.

## Stored files

The database records which files are stored. Their content goes in the folder `data/files`, or
wherever `files` says:

- a folder: `files = "/srv/texloom-files"`;
- an S3-compatible bucket: `files = "s3://BUCKET"` or `"s3://BUCKET/PREFIX"`, with

  ```toml
  [s3]
  endpoint = "https://s3.example.org"
  region = "auto"     # what the store calls its region: "garage" for Garage by default, "auto" for Cloudflare R2
  key_id = "…"
  secret = "…"
  ```

Any S3-compatible store works: [Garage](https://garagehq.deuxfleurs.fr/) or RustFS on your own
machines, or Cloudflare R2, Backblaze B2 or AWS S3. It only ever sees encrypted data. The server
checks it can reach the store when it starts, and stops with the reason if not. A bucket adds
redundancy only if the store has it: one Garage on the same machine is no safer than the folder;
Garage on three machines, or a hosted store, is.

To move from the folder to a bucket: stop the server, set `files` and `[s3]`, put the folder's
files into the bucket, and start it:

```bash
sudo -u texloom texloom-server --files-restore /var/lib/texloom/files   # copies only what the bucket lacks
```

`uv run --extra server python scripts/check_files.py s3://BUCKET/PREFIX` (with `TEXLOOM_S3_*` set)
checks a store of your own.

## Creation codes

The server can't see what it stores, and making a project needs no account, so storage is governed
by **allowances** in a small text file (`codes_file`):

```
# <code|default>  <allowance>  [until=YYYY-MM-DD]  [label]
default          0                               # projects created without a code: not stored on the server
A3F7-9KQ2-XM4P   unlimited                       owner
K7P2-QX4M-9H3D   2G         until=2027-11-07     alice
```

- A code's allowance is the total stored across all projects created with it. `default` covers
  people without a code. `0` means "not stored on the server": those people can still create
  projects, but only sync-only ones, whose copies live on their devices. The desktop app makes a
  project sync-only by itself when it's created without a code, or when the server refuses to
  store one; **Keep a copy on the server…** (Project menu) adds a code later.
- Anyone with a project's link can open and edit it; codes only govern storage of new projects. A
  code is entered once per browser (**Create project** asks, or ⚙ Settings → Sync & storage →
  Creation code…). The folder helper takes `texloom new … --code`, or `create_code` in
  `.texloom/config.json`.
- The file is re-read when it changes. Deleting a line revokes a code: its projects stay stored,
  but new changes then count against `default`.
- `until=` (optional) is the last day, in UTC, that a code is valid. After that, new changes to its
  projects aren't stored and it can't store new projects. Renewing (changing the date) restores
  it, with no restart. Projects stay tied to the code they were created with, so renew a code
  rather than issuing a new one.
- `purge_expired_after` drops the stored copies of projects whose code expired more than that many
  days ago (checked hourly; skipped while someone has the project open). The files stay on
  people's devices and the projects carry on sync-only; renewing the code stores them again on
  their next sync. `--purge-code CODE` does it at once for one expired or deleted code.
- Several codes files can be combined (`codes_file` is a list, and `TEXLOOM_CODES_FILES` adds more,
  separated by `:`), for example one kept by hand and one kept by a sales service. The first file
  that sets `default` decides it; `--add-code` writes to the first; `--renew-code` finds the code
  in whichever file lists it.

```bash
texloom-server --add-code alice --allowance 2G --months 12          # prints a new code
texloom-server --renew-code K7P2-QX4M-9H3D --months 12              # or --until 2028-01-31, --allowance 10G
texloom-server --list-codes                                         # usage, expiry, deleted codes
texloom-server --purge-code K7P2-QX4M-9H3D
```

(These read the settings file like the server does; run them as the server's user, e.g.
`sudo -u texloom /var/www/texloom/.venv/bin/texloom-server …`.) A renewal with `--months` counts
from the later of today and the code's current expiry, so renewing early loses nothing.
`--purge-code` refuses a code that is still valid; restart the server afterwards so its storage
totals are current (`purge_expired_after`, inside the server, needs no restart).

Without a codes file, every project may be stored. A change the server refuses to store is never
lost: it still reaches everyone online, the sender keeps it and retries, and the editor says the
project isn't saved on the server.

## Private servers

By default anyone who can reach your server can use it for live projects, though they can't read
anyone else's. `private = true` keeps it for your own group (it needs a codes file):

- Only projects created with a valid creation code may connect, even without server storage. The
  `default` line doesn't count.
- People with such a project's link need nothing else: give a code to whoever creates projects,
  and links to everyone else.
- When a code expires or is removed, its projects stop syncing until it's renewed. Copies on
  people's devices carry on working.
- Projects from before the server went private are registered the first time someone opens one
  with a valid code.

The web app and TeX files can still be downloaded by anyone, and `no_arxiv` turns off importing
through the server. For a server nobody outside can even see, put it behind a VPN such as
Tailscale or WireGuard, or a firewall.

## Retiring old app versions

The apps say which version they are when they connect, and the admin page lists the versions that
connect, with when each was last seen. Once old versions have stopped connecting, set the oldest
allowed with `min_version = "X.Y.Z"`. Older apps, and those too old to say, are then turned away
with a request to update: the desktop app shows "Update needed", the web app offers to reload (its
newest version comes from the server), and project lists refuse them too. An app too old to say
its version (before 0.4.30) can't show the request, and would reconnect every second or two: the
server keeps it connected but hearing nothing for ten minutes at a time instead. It's counted as
"unknown" on the admin page each time it reconnects, until its user updates it.

## Idle projects, admin page, arXiv import

- `evict_idle_days` drops the stored copy of projects nobody has opened for that many days. The
  server copy then behaves like a cache: the next time someone with an edit link opens the project,
  their copy is uploaded again. Until then, someone with only a link and no copy of their own can't
  open it.
- `admin_token_file` (or `admin_token`) turns on a read-only page at `/admin`. Log in with any user
  name and the token as password. It shows storage, disk, limits, usage per creation code, the app
  versions that connect, and per project its size, code, state and last activity. Names and
  contents stay encrypted.
- Importing an arXiv paper's source goes through the server, because arXiv doesn't let browsers
  fetch it. The server makes one request at a time, at least 3 s apart, at most 10 per hour per
  address, with size caps.

## Backups

The install script keeps none. Back up, every night and somewhere else:

- **the database**: with SQLite, `sqlite3 /var/lib/texloom/texloom.db ".backup texloom-backup.db"`
  (`apt install sqlite3`; consistent while the server runs: don't just copy the file, whose last
  changes may still be in `texloom.db-wal`). With PostgreSQL, `sudo -u postgres pg_dump
  --format=custom texloom > texloom.pgdump` (also consistent while it runs);
- **stored files**: `texloom-server --files-for-backup DIR` prints the folder to back up: the files
  folder (without its `.incoming`), or, with a bucket, DIR, which it first makes a copy of the
  bucket (copying only what's new since last time). Files are written once and never changed, so a
  copy taken while the server runs is consistent;
- **settings**: `/etc/texloom`.

To restore: install the server, stop it, put back the database (SQLite: copy the backup over
`texloom.db`, owned by `texloom`, and delete any `texloom.db-wal` and `texloom.db-shm` beside it;
PostgreSQL: `sudo -u postgres pg_restore -d texloom --clean --if-exists texloom.pgdump`), put back the settings and the files (into the files folder; with a bucket,
`texloom-server --files-restore <the restored copy>` puts back whatever the bucket lacks), and
start it. Everyone's devices hold full copies
of their projects, so edits made since the backup come back as people reconnect.

## A standby server

A second machine holding a nearly current copy of the database, so that if the server is lost you
can be running again in minutes rather than restoring from last night's backup. Nobody loses
work either way (everyone's devices hold full copies and send what the server lacks when they
reconnect: `scripts/check_standby.py` and `scripts/check_restore.py` run both cases), so this buys
less waiting, not safety. It is optional: a server with a nightly backup and a machine you could
restore it on is a perfectly good setup. It needs PostgreSQL, and stored files in a bucket, so that
the standby sees them too ([Stored files](#stored-files)). With SQLite, [Backups](#backups) is the
way.

There are two ways to get the standby; the server is told the same thing either way (`database`),
and everything else in this file stays as it is.

### Your own PostgreSQL, primary and standby

PostgreSQL (14 or later, the same major version on both machines) copies the primary's changes to
the standby as they happen (streaming replication). The standby is read-only until promoted, and
the server refuses to start on it, saying why.

1. **On the primary**, allow the standby in. Create a role, and let it in over TLS from the
   standby's address only (in `pg_hba.conf`; PostgreSQL's `listen_addresses` must include an
   address the standby can reach: keep it off the public internet, with a firewall or a private
   network):

   ```bash
   sudo -u postgres psql -c "CREATE ROLE replicator REPLICATION LOGIN PASSWORD 'a long random password'"
   sudo -u postgres psql -c "SELECT pg_create_physical_replication_slot('standby')"
   # pg_hba.conf:  hostssl  replication  replicator  STANDBY-ADDRESS/32  scram-sha-256
   systemctl reload postgresql
   ```

   The slot makes the primary keep the changes the standby hasn't received yet, so a standby that
   is offline for a while still catches up; but one that is gone for good makes the primary's disk
   fill with them, so drop the slot (`SELECT pg_drop_replication_slot('standby')`) if you give the
   standby up.

2. **On the standby**, install the server as for the primary, with the same settings
   (`/etc/texloom`, including the creation codes file and the bucket), and stop it: `systemctl
   disable --now texloom`. Then replace its PostgreSQL's data with a copy of the primary's (the
   paths are Debian's: change `17` to your version):

   ```bash
   systemctl stop postgresql
   sudo -u postgres rm -rf /var/lib/postgresql/17/main
   sudo -u postgres pg_basebackup -h PRIMARY-ADDRESS -U replicator -D /var/lib/postgresql/17/main \
     -R -X stream -S standby --checkpoint=fast       # asks for the role's password
   systemctl start postgresql
   ```

   `-R` writes the settings that make it a standby. Settings the primary keeps in its
   `postgresql.conf` (memory, `listen_addresses`) are not copied on Debian: set them here too.

3. **Check it is following.** On the primary, `sudo -u postgres psql -c "SELECT client_addr,
   state, replay_lag FROM pg_stat_replication"` shows the standby as `streaming`; on the standby,
   `sudo -u postgres psql -c "SELECT pg_is_in_recovery(), now() - pg_last_xact_replay_timestamp()"`
   says `t` and how far behind it is (a quiet server shows the time since its last change).

4. **Make the name movable.** The apps reach the server by its name, so a switch is moving the
   name to the standby: give the DNS record a short lifetime (60 seconds) beforehand, and have a
   certificate for the name ready on the standby (Caddy gets its own; with nginx and certbot, get
   one for the name there with the DNS challenge, which works before the name points to it).

**When the primary is lost**, on the standby:

1. Make sure the old primary is really down, and will stay down. Two primaries would number
   changes differently, and the apps would have to untangle it. If you can't be sure (it may only be
   cut off), power it off or disconnect it.
2. Promote the standby: `sudo -u postgres psql -c "SELECT pg_promote()"`. It is now a primary and
   takes writes.
3. Start the server there: `systemctl enable --now texloom`.
4. Move the name to the standby's address. Apps reconnect on their own. Changes made in the last
   moments before the loss may not have reached the standby: the apps hold them, notice that the
   server's log isn't the one they last saw, and send everything again, so nothing is lost, and
   nobody has to do anything.

**Afterwards** you have one server and no standby. The old primary must not come back as a primary:
make it a standby of the new one, by emptying its PostgreSQL data and repeating step 2 with the
new primary's address (and swap the DNS name's roles if you like, or keep the new arrangement).
Until then, a failure of the new primary has no standby. To switch on purpose (maintenance), stop
the server on the primary first and wait until the standby has everything (`SELECT
pg_current_wal_lsn()` on the primary, and `pg_last_wal_replay_lsn()` on the standby, are equal),
then promote and move the name: nothing is missing, so there is no catch-up.

A standby copies mistakes as well as data (a project deleted, a table dropped), so it doesn't
replace [Backups](#backups). Try the switch once on your own pair before you need it:
`scripts/check_standby.py` does it on one machine with throwaway databases, which shows the steps
and what the apps do, but not your network or your DNS.

### Letting the two servers switch over by themselves

Optional, and off by default: a server on its own, and a pair you switch by hand, need none of
this, and with it off the server doesn't even load the code for it. When on, the two servers share
a small "lease" in the bucket. The one that holds it is the primary and serves; it renews the lease
every few seconds. The other waits, and when the lease hasn't been renewed for long enough it takes
it, promotes its own PostgreSQL and serves. A primary that can't renew (the bucket is unreachable) stops
itself, closing its connections, before the other can take over, so there are never two serving.
The design and the checks behind it are in `models/failover/README.md`; `scripts/check_failover.py`
runs two real servers through a lost primary and one cut off from the bucket.

You need the pair from the section above (a primary and a streaming standby, each with its server
installed) and a bucket for the stored files that does **conditional writes** properly, because the
lease lives in that bucket and the store is what decides who gets it. Not every S3-compatible store
does, so test yours first, from either server (it writes and removes test objects in the bucket,
including eight writers at once, to see that exactly one wins):

```bash
sudo -u texloom /var/www/texloom/.venv/bin/texloom-server --config /etc/texloom/texloom.toml --failover-check
```

A server asked to fail over runs the same test when it starts, and refuses a store that fails it.
What is known (the stores change, so the test is what counts):

| store | conditional writes |
|---|---|
| RustFS | passes the test (tried: the 2026-11 image) |
| Cloudflare R2, AWS S3 | documented as supported (`If-Match`, `If-None-Match`); run the test on yours |
| Backblaze B2 | reported by users of other tools to answer `If-None-Match` with 501, so it can't hold a lease; not tried here |
| Garage | fails the test (v2.6.0 ignores the conditions): fine for stored files, not for the lease |
| MinIO | not tried: its community edition no longer publishes images |

If the bucket you keep files in can't, the lease can't live there (it is in the same bucket as the
stored files); failover then isn't available until one that can is used.

Then, once, on the **primary**, let the server's PostgreSQL role promote a standby (the grant is
copied to the standby with everything else):

```bash
sudo -u postgres psql texloom -c "GRANT EXECUTE ON FUNCTION pg_promote(boolean, integer) TO texloom"
```

And in `texloom.toml` on **each** server (each needs its own `failover_name`), then restart both,
the primary first:

```toml
failover = true
failover_name = "server-a"   # "server-b" on the other
# failover_ttl = 15          # seconds
```

- **Time.** A primary stops itself after `failover_ttl` seconds without a successful renewal, and the
  standby waits twice that, plus a margin, before it takes over: with the default 15 seconds, about
  35, plus a few for PostgreSQL to promote, so people see "connecting…" for under a minute. A lower
  value is quicker and more easily upset by a slow bucket. Servers' clocks need not agree, only run
  at about the same speed: nothing compares one machine's clock with the other's.
- **Who gets the traffic.** The standby answers every address but `/api/health` with 503, and
  `/api/health` is 200 only on the primary. The servers don't move the name themselves: see
  [Moving traffic to the primary](#moving-traffic-to-the-primary).
- **After a switch.** The server that stopped (or was lost) restarts as a standby, but its PostgreSQL
  is still a primary with a history the new one doesn't have, so it **won't take over** (it says so in
  its log) until you rebuild it as a standby of the new primary, as in "Afterwards" above. Until then
  you have one server and no standby, as after a manual switch.
- **Starting a server again** (an update, a restart) while it holds the lease takes it back within a
  couple of renewal intervals (about ten seconds by default). The other server sees renewals missing
  for that long, which is well short of the wait before it takes over.
- It runs under the install script's systemd unit as it is (`Restart=on-failure`: a server that stops
  itself exits with status 75).
- **What it can't do** (see `models/failover/README.md`): a machine suspended and woken (a virtual
  machine) after the other took over can accept changes for a moment until its first renewal fails.
  Nobody loses anything, because an app that was told "stored" by it checks, when it reconnects, that
  the new primary has its last change, and sends everything again if not.

### Moving traffic to the primary

Failover makes one server the primary; people must also reach it. That needs an entry point that
survives the primary's loss, and there are two ways to arrange one. The servers behave the same in
both (`/api/health` is how each tells the primary), and you can move from one to the other later.

| | **A. both machines in DNS** | **B. the standby's address added when it takes over** |
|---|---|---|
| for | two machines the public can reach well (two VPSes) | a standby on a home connection (a Raspberry Pi) |
| DNS | both addresses, always | the primary's address; a script moves it |
| a visitor | reaches either machine, whose Caddy forwards to the primary | reaches the primary; after a switch, the standby |
| a switch takes | the lease time, about a minute | the lease time plus the DNS lifetime (set it to 60 s), about two minutes |
| needs | the relays to reach each other over a private network | the standby reachable from outside (ports 80 and 443 forwarded), and a DNS provider with an API |
| costs | half of everyone's visits come through the other machine's connection | a script, and a key for your DNS provider on each machine |

**B is the one to use with a home standby:** a home line is a poor address to keep in DNS, since half
of everyone would come through its upload, and a home address changes. What follows is B, then A.

#### B. A standby that is added to DNS when it takes over

On **each** of the two servers (the script does nothing on the one that isn't the primary, and on the
VPS it sets the record back to the VPS's address after you have rebuilt it as a standby and a
switch has been made the other way):

1. **A command that points the name at this machine.** `deploy/follow-primary.sh` runs a command
   of yours while this server is the primary: when it becomes the primary, again every ten minutes
   while it stays so, and again every 30 seconds if the command fails. So the command must be safe to
   repeat. It is yours, and stays in your own settings, because it depends on your DNS provider.
   Make it a short script that finds this machine's public address and sets the name's A record to it
   with the provider's API, for example `/etc/texloom/set-dns.sh` (mode 755, with no key in it):

   ```sh
   #!/bin/sh
   set -eu
   ip=$(curl -fsS https://ifconfig.me)   # this machine's public address (any such service will do)
   # the call your DNS provider documents for setting one A record to "$ip", TTL 60,
   # authenticated with "$DNS_API_KEY":
   curl -fsS … 
   ```

2. **Its settings.** The unit reads `/etc/texloom/follow-primary.env`, as root, and runs the script as
   an unprivileged throwaway user, so the key is in the file and not in the script:

   ```bash
   sudo cp /var/www/texloom/deploy/texloom-follow-primary.service /etc/systemd/system/
   sudo install -m 600 /dev/null /etc/texloom/follow-primary.env
   sudoedit /etc/texloom/follow-primary.env      # ON_PRIMARY=/etc/texloom/set-dns.sh  and  DNS_API_KEY=…
   sudo systemctl enable --now texloom-follow-primary
   journalctl -u texloom-follow-primary -f       # "this server is the primary", "ran the command: ok"
   ```

   (`INTERVAL`, `CONFIRM`, `REFRESH` and `RETRY` can go in the same file; the script's first lines
   say what they are.)

3. **A short DNS lifetime, a day ahead.** Lower the name's TTL to 60 seconds well before you rely
   on this: resolvers keep the old answer for the old lifetime. After a switch some visitors still
   reach the old address until theirs expires.
4. **The standby reachable from outside.** Forward ports 80 and 443 on the router to the Pi, and run
   Caddy there with `deploy/Caddyfile` for the same name. The Pi's SSD matters more than its CPU.
5. **A certificate ready on the standby before it is needed.** Caddy gets certificates when DNS
   points at it, which on the standby is only after a switch, so the first visitors would wait, or
   fail, while it asks. Either use Caddy's DNS challenge (a Caddy build with your DNS provider's
   module: Caddy's documentation explains), which needs no traffic, or copy Caddy's certificate
   storage (`/var/lib/caddy/.local/share/caddy`) from the primary's machine to the Pi on a schedule.
   Neither has been tried here: **try a switch** before you rely on it.
6. **Try a switch.** Stop the server on the primary (`systemctl stop texloom`) and watch the standby
   take over (its journal, `curl https://…/api/health` on it), the record move (the second script's
   journal), and the app reconnect. Then rebuild the old primary as a standby, as in "Afterwards".

#### A. Both machines in DNS

For two machines that are both public. Put both addresses in DNS for the name, and on each machine
run Caddy with `deploy/Caddyfile.failover` instead of `deploy/Caddyfile`: it forwards each request to
whichever of the two servers answers 200 on `/api/health`, so a visitor who reaches the standby's
machine is passed on to the primary, and nothing in DNS changes at a switch. The file's first lines say
how: the two relays must reach each other over a private network (WireGuard, Tailscale), and port 8787
must never be open to the internet. No script runs, so skip `texloom-follow-primary`.

#### Changing from one to the other

- **B to A** (the Pi's place is taken by a public machine): put the second machine's address in DNS
  next to the first (and raise the TTL again if you like), install `Caddyfile.failover` on both, set up
  the private network, and `systemctl disable --now texloom-follow-primary` on both.
- **A to B**: take the standby's address out of DNS (so only the primary's is there), lower the TTL to
  60 seconds a day ahead, install `deploy/Caddyfile` on the standby again, and set up the script as
  above.

Neither change touches the servers or their settings.

### A hosted PostgreSQL

A provider's PostgreSQL service keeps its own standby, switches over by itself and takes its own
backups, which is most of the work above. Use its address as `database`:

```toml
database = "postgresql://texloom:PASSWORD@HOST:5432/texloom?sslmode=require"
```

The only differences from a PostgreSQL of your own:

- **The address.** The password goes in the settings file or `TEXLOOM_DATABASE` in the
  environment, as for any secret ([Settings](#settings)). Install the server as usual and then
  set `database`; `deploy/install.sh` leaves a PostgreSQL on another machine alone, and installs none
  here.
- **The connection.** Use the provider's direct address, not a connection pooler's in
  "transaction" mode (PgBouncer's usual one): the server relies on connections that last through a
  transaction and, later, on notifications.
- **What switches over.** The provider moves its database to its standby, and the server keeps
  using the same address. What to do when the server's machine is lost is as for the rest: install
  it on another, with the same settings and bucket, and move the name.
- **Cost and where it is.** Providers differ in price, in which regions they have, and in what
  their standbys and backups are (some give a standby only on dearer plans, some only backups).
  Look for one in your region: the data is encrypted either way, so a provider needs no trust, but
  the server and its database should be close, since the server makes a few queries for every
  change.

## Updating

Re-run `deploy/install.sh` with the same arguments: it copies the new version, keeps the data and
settings, and restarts the service, which creates any new tables itself. By
hand: `git pull`, `uv sync --extra server`, rebuild the web app, restart.
