# Running a TeXLoom server

A TeXLoom server is one Python process: it serves the web app and the in-browser TeX files, and
relays and stores projects' encrypted changes. It can't read any project. It needs:

- **a database** for the edit logs, project records and project lists: an SQLite file, which
  needs nothing installed (the default), or PostgreSQL (14 or later) for a standby or several
  relays ([The database](#the-database));
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
- [Backups](#backups) · [A standby, switched by hand](#a-standby-switched-by-hand) ·
  [How far one server goes](#how-far-one-server-goes) · [Updating](#updating)
- Several machines, taking over from each other by themselves: [CLUSTER.md](CLUSTER.md)

## On an Ubuntu or Debian server

[`deploy/install.sh`](deploy/install.sh) installs or updates a server from a checkout on your own
computer, over ssh. You need a domain name pointing at the server, and on your computer
[uv](https://docs.astral.sh/uv/) and Node.js 22.

```bash
git clone https://github.com/jhudsy/texloom && cd texloom
(cd web && npm ci)
deploy/install.sh root@server.example.org texloom.example.org
```

It is tested on Ubuntu 24.04 and Debian 13. It works alongside other sites on the machine and only adds
things (and installs nginx, certbot, `python3-venv`, `rsync` and `curl` if they're missing, and PostgreSQL
with `DATABASE=postgresql`; it also downloads about 700 MB of in-browser TeX files, checked against a
checksum, so allow a few GB of disk):

| what | where |
|---|---|
| a `texloom` system user | |
| the app and its Python environment | `/var/www/texloom` (`TEXLOOM_DIR=…` for elsewhere) |
| the database (SQLite), stored files, TeX Live packages fetched on demand | `/var/lib/texloom` |
| settings, the admin password | `/etc/texloom/texloom.toml`, `admin-token` (creation codes are in the database) |
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
uv sync --extra server                      # Python: the server and its PostgreSQL driver
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

To keep it running, create a `texloom` user and use [`deploy/texloom.service`](deploy/texloom.service) as a
systemd unit (edit its paths); it reads `/etc/texloom/env` for `TEXLOOM_<NAME>` settings.

To let in-browser compiles fetch any TeX Live package on demand (without this only the bundled packages in
`tex-assets` are available), set `texlive = "/var/lib/texloom/texlive"` in the settings and build the
package index once:

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

**Running `texloom-server` yourself.** After the install script the program is
`/var/www/texloom/.venv/bin/texloom-server`, which is not on `PATH`, and the settings file is only
readable by the server's user. So a command in this guide that says `texloom-server …` means
`sudo -u texloom /var/www/texloom/.venv/bin/texloom-server --config /etc/texloom/texloom.toml …` (or
`uv run texloom-server …` if you installed by hand).

Every option of `texloom-server` (`texloom-server --help` lists them) can be set in the settings
file under its name, with `_` for `-`; [`texloom.example.toml`](texloom.example.toml) lists them
all, and is what the install script starts from. The file is `--config`, else `TEXLOOM_CONFIG`,
else `./texloom.toml`, else `/etc/texloom/texloom.toml`, if they exist. Tables such as `[s3]` go last: in TOML, every line after one belongs
to it. An environment variable `TEXLOOM_<NAME>`
(`TEXLOOM_MIN_VERSION=0.4.30`) overrides the file, and the command line overrides both. A setting
the server doesn't know is an error, so a typo doesn't go unnoticed. After a change:
`systemctl restart texloom`.

```toml
database = "sqlite:///var/lib/texloom/texloom.db"
data = "/var/lib/texloom"
default_allowance = "0"
admin_token_file = "/etc/texloom/admin-token"
min_free = "2G"
```

| setting | default | what it does |
|---|---|---|
| `database` | `texloom.db` in `data` | an SQLite file or a PostgreSQL database ([The database](#the-database)) |
| `data` | `data` | this server's own folder: uploads are spooled here, `min_free` is kept free here, and stored files go in `data/files` unless `files` says otherwise |
| `files`, `[s3]` | `data/files` | where stored files go ([Stored files](#stored-files)) |
| `bus` | none (in-process) | for several relays sharing a database: the Redis-compatible server (Valkey) through which they pass messages to each other ([CLUSTER.md](CLUSTER.md) sets it). Leave it unset for one server: one with no bus refuses to start on a PostgreSQL database another such server uses |
| `host`, `port` | `127.0.0.1`, `8787` | where it listens |
| `static`, `tex_assets` | `web/dist`, `tex-assets` | the built web app, and the in-browser TeX files |
| `default_allowance` | unlimited (`0` in the install script's settings) | what a project created without a creation code may store ([Creation codes](#creation-codes)) |
| `codes_file` | none | (servers before the database kept codes) codes from files, this server's only; a list. `TEXLOOM_CODES_FILES` adds files to it |
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
| `admin_token` or `admin_token_file` | none | the password for `/admin` (at least 16 characters) |
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
- **PostgreSQL** (14 or later), for a standby ([A standby, switched by hand](#a-standby-switched-by-hand)) or
  several relays ([CLUSTER.md](CLUSTER.md)):
  - `postgresql:///texloom?host=/var/run/postgresql`: on this machine, over the local socket, as
    the user the server runs as (no password anywhere; the install script's choice with
    `DATABASE=postgresql`);
  - `postgresql://texloom:PASSWORD@db.example.org:5432/texloom?sslmode=require`: elsewhere.

  The server uses up to 10 connections for its queries (PostgreSQL allows 100 by default), plus one that
  listens for what other servers tell it, one holding a lock while it runs with no `bus` (so that no second
  server uses the database meanwhile) and, for a moment each hour, one for its upkeep. So connections
  follow the number of servers, not of people: about eight servers fill PostgreSQL's default. A connection
  pooler (PgBouncer, or a hosted one) lets more share them, but the listening connection and the advisory
  locks must bypass it, and the pool needs the pooler's settings for prepared statements (not tried here).

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

The old database is left as it was.

**A hosted PostgreSQL.** A provider's PostgreSQL service keeps its own standby, switches over by itself and takes
its own backups. Use its address as `database`:

```toml
database = "postgresql://texloom:PASSWORD@HOST:5432/texloom?sslmode=require"
```

The password goes in the settings file or `TEXLOOM_DATABASE` in the environment, as for any secret
([Settings](#settings)); `deploy/install.sh` leaves a PostgreSQL on another machine alone, and installs none
here. Use the provider's direct address, not a connection pooler's in "transaction" mode (PgBouncer's usual
one): the server relies on connections that last through a transaction, and on notifications. The provider
moves its database to its standby and the server keeps using the same address; if the server's machine is
lost, install it on another with the same settings and bucket, and move the name. Providers differ in price,
regions, and what their standbys and backups are (some give a standby only on dearer plans). The data is
encrypted either way, so a provider needs no trust, but choose one near the server, which makes a few queries
for every change.

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
by **allowances**, given by **creation codes**. They are kept in the server's database, so every server
sharing it has the same codes, and a change counts everywhere within seconds. Change them with the server's
command line, on any machine that has the settings file:

```bash
texloom-server --add-code alice --allowance 2G --months 12          # prints a new code
texloom-server --renew-code K7P2-QX4M-9H3D --months 12              # or --until 2028-01-31, --allowance 10G
texloom-server --remove-code K7P2-QX4M-9H3D                          # its projects fall back to the default
texloom-server --list-codes                                         # usage, expiry, removed codes
texloom-server --purge-code K7P2-QX4M-9H3D
texloom-server --export-codes codes.txt                             # every code, to a file (- for the screen)
texloom-server --import-codes-file codes.txt                        # a file's codes into the database (new ones only)
```

(These read the settings file like the server does: run them as described under Settings, as the server's user,
`sudo -u texloom`, since the database is its.) The install script makes the owner's code (unlimited) on a first
install and prints it.

- A code's allowance (`0`, a size such as `500M` or `5G`, or `unlimited`) is the total stored across all projects
  created with it. **`default_allowance`** covers people without a code (`0` in the settings the install script
  writes; unset, unlimited). `0` means "not stored on the server": those people can still create projects, but only
  sync-only ones, whose copies live on their devices. The desktop app makes a project sync-only by itself when it's
  created without a code, or when the server refuses to store one; **Keep a copy on the server…** (Project menu) adds
  a code later.
- Anyone with a project's link can open and edit it; codes only govern storage of new projects. A
  code is entered once per browser (**Create project** asks, or ⚙ Settings → Sync & storage →
  Creation code…). The folder helper takes `texloom new … --code`, or `create_code` in
  `.texloom/config.json`.
- Removing a code revokes it: its projects stay stored, but new changes then count against the default.
- `--months` or `--until` (optional) sets the last day, in UTC, that a code is valid. After that, new changes to its
  projects aren't stored and it can't store new projects. Renewing it restores it, with no restart. A renewal with
  `--months` counts from the later of today and the code's current expiry, so renewing early loses nothing. Projects
  stay tied to the code they were created with, so renew a code rather than issuing a new one.
- `purge_expired_after` drops the stored copies of projects whose code expired more than that many
  days ago (checked hourly; skipped while someone has the project open). The files stay on
  people's devices and the projects carry on sync-only; renewing the code stores them again on
  their next sync. `--purge-code CODE` does it at once for one expired or removed code; it refuses a code that is
  still valid. Restart the server afterwards so its storage totals are current (`purge_expired_after`, inside the
  server, needs no restart).

A change the server refuses to store is never lost: it still reaches everyone online, the sender keeps it and
retries, and the editor says the project isn't saved on the server.

**Codes files** (servers set up before the database kept codes). Such a server's settings name a text file
(`codes_file`), one code a line, which it reads again whenever it changes:

```
# <code|default>  <allowance>  [until=YYYY-MM-DD]  [label]
default          0
K7P2-QX4M-9H3D   2G         until=2027-11-07     alice
```

It keeps working, but a file's codes count only on the server that reads it. To move them into the database:

```bash
sudo -u texloom texloom-server --config /etc/texloom/texloom.toml --import-codes-file /etc/texloom/codes.txt
```

then take the file out of `codes_file` (and set `default_allowance` to its `default` line, which the import prints)
and restart the server. Until then, the commands above change the database's codes, and a code in the file is
renewed in the file. A program that runs `texloom-server --codes-file FILE --add-code …` with no database (a sales
service, say) still writes that file.

## Private servers

By default anyone who can reach your server can use it for live projects, though they can't read
anyone else's. `private = true` keeps it for your own group (with [creation codes](#creation-codes)):

- Only projects created with a valid creation code may connect, even without server storage. The
  default allowance doesn't count.
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
newest version comes from the server), and project lists refuse them too. An app too old to say its
version can't show the request, and is counted as "unknown" on the admin page.

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

The install script sets up a backup every hour, sent off-site with [restic](https://restic.net), which stays off until
you say where to (`deploy/texloom-backup`, run by `texloom-backup.timer`):

```bash
apt install restic
head -c 32 /dev/urandom | base64 > /etc/texloom/restic-password && chmod 600 /etc/texloom/restic-password   # keep a copy elsewhere too
cat > /etc/texloom/backup.env <<'EOF'
RESTIC_REPOSITORY=b2:my-bucket:texloom
RESTIC_PASSWORD_FILE=/etc/texloom/restic-password
B2_ACCOUNT_ID=…
B2_ACCOUNT_KEY=…
EOF
chmod 600 /etc/texloom/backup.env
systemctl start texloom-backup && journalctl -u texloom-backup -n 5    # the first one now: it makes the repository
```

Each backup holds:

- **the database**, as it was at one moment while the server ran, as an SQLite file whatever the database is
  (`texloom-server --backup`);
- **the stored files**: the files folder as it is (files are written once and never changed), or, with a bucket, a
  copy of it kept in `/var/lib/texloom/backup/files` (only what's new is copied; it needs as much disk as the files).
  Or copy the bucket to a bucket at another provider instead (`BUCKET_COPY`, with rclone: see the top of
  `deploy/texloom-backup`), keeping old versions there;
- **the settings**, `/etc/texloom` (creation codes are in the database, so its backup has them), and the certificates.

It keeps every backup of the last 48 hours and one a day for 30 days (`KEEP_HOURLY`, `KEEP_DAILY`). Set
`HEALTHCHECK_URL` to a [healthchecks.io](https://healthchecks.io) check (or any URL pinged after each backup, and at
`…/fail` when one fails) so that you hear when backups stop. **Keep the restic password somewhere else as well**: the
backups can't be read without it, and it isn't inside them. Try a restore now and then (below): it's the only proof
that the backups work.

To restore: `restic restore latest --target /root/restore`, then install the server and stop it, and put back

- the database: with SQLite, copy `/root/restore/var/lib/texloom/backup/texloom.db` over `texloom.db` (owned by
  `texloom`; delete any `texloom.db-wal` and `texloom.db-shm` beside it); with PostgreSQL, into an empty database:
  `texloom-server --config /etc/texloom/texloom.toml --copy-database-from sqlite:/root/restore/var/lib/texloom/backup/texloom.db`;
- the stored files: into the files folder; with a bucket, `texloom-server --config /etc/texloom/texloom.toml
  --files-restore /root/restore/var/lib/texloom/backup/files` puts back whatever the bucket lacks;
- the settings, from `/root/restore/etc/texloom`,

and start it. Everyone's devices hold full copies of their projects, so edits made since the backup come back as
people reconnect.

### A cold standby

Restoring takes as long as it takes you to find a machine and install it. A cold standby has it ready: a second
machine restores the latest backup every night (the database, the settings, and the stored files or a copy of
the bucket), checks it, and stays stopped. If the server is lost, start it there and move the name to it. Edits
made since the backup aren't lost: each app sends the newest change it has from the server in its hello, finds
that the server's log isn't the one it saw, and sends everything again (`scripts/check_restore.py`). The nightly
restore is also the proof that the backups work.

## A standby, switched by hand

A second machine holding a nearly current copy of the database, so that losing the server means minutes rather
than a restore from last night's backup. Nobody loses work either way: everyone's devices hold full copies and
send what the server lacks when they reconnect (`scripts/check_standby.py` and `scripts/check_restore.py` run
both cases), so a standby buys less waiting, not safety. It is optional: a server with a nightly backup and a
machine you could restore it on is a perfectly good setup. It needs PostgreSQL (your own, below, or a
[hosted one](#the-database), which keeps its own standby), and stored files in a bucket, so that the standby
sees them too ([Stored files](#stored-files)). For machines that take over from each other without you, see
[CLUSTER.md](CLUSTER.md).

PostgreSQL (14 or later, the same major version on both machines) copies the primary's changes to the standby as
they happen (streaming replication). The standby is read-only until promoted, and the server refuses to start on
it, saying why.

1. **Allow each machine in on the other.** Either may be the standby, and after a switch the old primary is
   rebuilt from the new one, so both need to let the other replicate. Create the role once, on the primary (it
   is copied to the standby), and on **each** machine let the other's address in over TLS (in `pg_hba.conf`,
   which is not copied; PostgreSQL's `listen_addresses` must include an address the other can reach: keep it
   off the public internet, with a firewall or a private network):

   ```bash
   sudo -u postgres psql -c "CREATE ROLE replicator REPLICATION LOGIN PASSWORD 'a long random password'"
   sudo -u postgres psql -c "SELECT pg_create_physical_replication_slot('standby')"
   # pg_hba.conf, on each machine, with the OTHER machine's address:
   #   hostssl  replication  replicator  OTHER-ADDRESS/32  scram-sha-256
   systemctl reload postgresql
   ```

   The slot makes the primary keep the changes the standby hasn't received yet, so a standby that is offline
   for a while still catches up; but one that is gone for good makes the primary's disk fill with them, so drop
   the slot (`SELECT pg_drop_replication_slot('standby')`) if you give the standby up.

2. **On the standby**, install the server as for the primary, with the same settings (`/etc/texloom`, and the
   bucket; the creation codes are in the database, so the standby has them), and stop it: `systemctl disable
   --now texloom`. Then replace its PostgreSQL's data with a copy of the primary's (the paths are Debian's:
   change `17` to your version):

   ```bash
   systemctl stop postgresql
   sudo -u postgres rm -rf /var/lib/postgresql/17/main
   sudo -u postgres pg_basebackup -h PRIMARY-ADDRESS -U replicator -D /var/lib/postgresql/17/main \
     -R -X stream -S standby --checkpoint=fast       # asks for the role's password
   systemctl start postgresql
   ```

   `-R` writes the settings that make it a standby. Settings the primary keeps in its `postgresql.conf`
   (memory, `listen_addresses`) are not copied on Debian: set them here too.

3. **Check it is following.** On the primary, `sudo -u postgres psql -c "SELECT client_addr, state, replay_lag
   FROM pg_stat_replication"` shows the standby as `streaming`; on the standby, `sudo -u postgres psql -c
   "SELECT pg_is_in_recovery(), now() - pg_last_xact_replay_timestamp()"` says `t` and how far behind it is (a
   quiet server shows the time since its last change).

4. **Plan how the name will move to the standby**, including a certificate for the name on the standby before
   it is needed. Only a server whose database is the primary runs at all, so its `/api/health` answering 200 is
   the sign. Change the DNS record (with its lifetime lowered to a minute or so beforehand), move a floating
   address, or put a proxy with a health check on `/api/health` in front of both. Apps reconnect by themselves.

**When the primary is lost**, on the standby:

1. Make sure the old primary is really down, and will stay down. Two primaries would number changes
   differently, and the apps would have to untangle it. If you can't be sure (it may only be cut off), power it
   off or disconnect it.
2. Promote the standby: `sudo -u postgres psql -c "SELECT pg_promote()"`. It is now a primary and takes writes.
3. Start the server there: `systemctl enable --now texloom`.
4. Move the name to the standby's address. Apps reconnect on their own, and send again whatever the standby
   lacked, so nobody has to do anything.

**Afterwards, rebuild the old primary.** You now have one server and no standby, and the old primary must not
come back as a primary. Rebuild it as a standby of the new one, on the old primary's machine (as root):

```bash
sudo env PGPASSWORD='the replicator password' /var/www/texloom/deploy/rebuild-standby.py NEW-PRIMARY-ADDRESS
```

It stops the TeXLoom server and PostgreSQL there, **keeps the old data directory aside** (as `….old-DATE`;
delete it when you are sure: it holds the changes the new primary never got, which the apps send again), takes a
streaming base backup of the new primary, starts PostgreSQL as its standby, and waits until it is streaming. A
server can't run on a standby database, so it leaves the server stopped and says so. It refuses, changing
nothing, if this machine's server is the primary (it answers 200 on `/api/health`), if the address is another
cluster, or if you don't confirm; if the backup fails it puts the old data back. `--cluster VERSION/NAME` picks
one if there are several, and `--help` lists the rest. Until it has run, a failure of the new primary has no
standby.

**Switching on purpose**, for maintenance: stop the server on the primary first, wait until the standby has
everything (`SELECT pg_current_wal_lsn()` on the primary, and `pg_last_wal_replay_lsn()` on the standby, are
equal), then promote and move the name: nothing is missing, so there is no catch-up.

A standby copies mistakes as well as data (a project deleted, a table dropped), so it doesn't replace
[Backups](#backups). Try the switch once on your own pair before you need it: `scripts/check_standby.py` does it
on one machine with throwaway databases, which shows the steps and what the apps do, but not your network or
your DNS.

## How far one server goes

Nobody has taken a deployment to its limits, so the order below is reasoned, not measured.
`scripts/loadtest.py` measures one relay on the machine it runs on (latency, CPU, memory per scenario): run it on
a machine like yours before relying on a number. On a laptop, one relay carried about 800 edits a second (three
people a project, two of them typing) before the slowest 5% took over 200 ms to arrive.

| # | The limit | What you would notice | What to do |
|---|---|---|---|
| 1 | **Bandwidth for the in-browser TeX engine.** The web editor compiles in the browser, so a first-time visitor downloads the engine from the server (`busytex.wasm` is 32 MB; the full TeX Live bundles are 93, 201 and 342 MB, but with TeX Live files on demand a document pulls only the package files it needs) and the browser keeps them afterwards. This is most of the bytes the server sends, and none of it needs the relay's CPU. | the host's outbound traffic, or its provider's allowance; a slow first compile for new visitors | serve `/core` and `/texlive` from a CDN or a caching proxy in front (static files that change only with a release). Cheaper than more servers, so first |
| 2 | **The relay's memory.** The systemd unit caps it (`MemoryMax=1G`), and `memory` (512M by default) is what big work at once may use: catch-ups, large messages, uploads, arXiv imports. | big operations are refused or slow; the process is killed at the cap | raise `memory` and `MemoryMax` together (the cap stays about 500 MB above it), or a bigger machine |
| 3 | **One relay's CPU, and its connections.** It is one Python process, so one core, and the work grows with connections times edits a second. It takes at most 2,000 connections at once (`MAX_CONNECTIONS` in `server/relay.py`). | the process near 100% of one core; edits arriving late | a faster core helps more than more cores; then several relays ([CLUSTER.md](CLUSTER.md)) |
| 4 | **Database connections**, with PostgreSQL ([The database](#the-database)) | `SELECT count(*) FROM pg_stat_activity` near `max_connections`; connection errors | a connection pooler |
| 5 | **Writes to the one database.** One small write per batch of edits for each active project, and reads when someone connects and catches up. | the database's CPU or disk; queries waiting | a faster disk, or PostgreSQL on a machine of its own (batching edits, and catch-ups read from a replica, would be next: neither is built) |
| 6 | **Storage.** The database holds each project's edit log, replaced by a snapshot as it grows (over 12 MB the snapshot is stored as an upload; above 50 MB snapshots stop and the log grows: [#18](https://github.com/jhudsy/texloom/issues/18)). Stored files are most of it. | the disk, or the bucket's bill | `evict_idle_days` (writers upload an evicted project again), `project_max_bytes` (200M by default), and creation codes' allowances |
| 7 | **Availability**, not capacity: the relay, the database, the disk, the network in front. | people can't connect | backups and a [cold standby](#a-cold-standby); a [standby switched by hand](#a-standby-switched-by-hand); several relays ([CLUSTER.md](CLUSTER.md)) |

A project's size on the admin page is mostly its stored files, counted as they are stored. Its edit log is small
by comparison and is compacted, so a project of tens of megabytes is ordinary; the Snapshot column (the
compressed editing history alone) is what the 12 MB and 50 MB thresholds are about.

**What to watch**: the admin page (stored bytes, who is connected now, each project's size), the relay's CPU
(`top`) and memory (`systemctl show texloom -p MemoryCurrent`), database connections, the disk and the host's
outbound traffic. For history and graphs, the relay also serves `/metrics` in Prometheus's format (connections,
messages, stores and how long they take, catch-up bytes, app versions; no project ids or codes). nginx doesn't
pass it on: it is reached on the relay's own address (`http://127.0.0.1:8787/metrics` on one machine).
Prometheus and its `node_exporter` (the machine's CPU, memory, disk and network) can run beside the server from
Debian's packages (`apt install prometheus prometheus-node-exporter`), scraping both. It is optional, and none
of it is installed by default. [`deploy/monitoring/`](deploy/monitoring/) holds a Grafana dashboard and alert rules
([CLUSTER.md](CLUSTER.md)).

## Updating

Re-run `deploy/install.sh` with the same arguments: it copies the new version, keeps the data and
settings, and restarts the service, which creates any new tables itself. By
hand: `git pull`, `uv sync --extra server`, rebuild the web app, restart.

A settings file that still says `failover` (two servers sharing a lease in the bucket, retired in favour of
[several relays](CLUSTER.md)) gets a warning in the log and is otherwise unaffected.
