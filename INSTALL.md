# Running a TeXLoom server

A TeXLoom server is one Python process: it serves the web app and the in-browser TeX files, and
relays and stores projects' encrypted changes. It can't read any project. It needs:

- **a database** for the edit logs, project records and project lists: an SQLite file, which
  needs nothing installed (the default), or PostgreSQL (14 or later) for a warm standby
  ([The database](#the-database));
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
- [Backups](#backups) · [Updating](#updating)
- [A standby server](#a-standby-server), in [STANDBY.md](STANDBY.md)

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
| `admin_token` or `admin_token_file` | none | the password for `/admin` (at least 16 characters) |
| `no_project_lists`, `no_arxiv` | `false` | turn off storing project lists, or importing from arXiv |
| `texlive`, `texlive_max`, `texlive_mirror` | none, `4G`, none | TeX Live packages on demand: the cache folder, its size (the packages used least recently make room), mirrors to try first (a list) |
| `failover`, `failover_name`, `failover_ttl` | `false`, the host name, `15` | switch to a standby by itself ([Letting the two servers switch over by themselves](STANDBY.md#letting-the-two-servers-switch-over-by-themselves)) |

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
- **PostgreSQL** (14 or later), for a warm standby ([A standby server](#a-standby-server)):
  - `postgresql:///texloom?host=/var/run/postgresql`: on this machine, over the local socket, as
    the user the server runs as (no password anywhere; the install script's choice with
    `DATABASE=postgresql`);
  - `postgresql://texloom:PASSWORD@db.example.org:5432/texloom?sslmode=require`: elsewhere.

  The server uses up to 10 connections for its queries (PostgreSQL allows 100 by default), plus one that
  listens for what other servers tell it and, for a moment each hour, one for its upkeep.
  [SCALING.md](SCALING.md) says when the number of connections matters.

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

(These read the settings file like the server does: run them as described under Settings.) A renewal with `--months` counts
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
newest version comes from the server), and project lists refuse them too. An app too old to say its
version can't show the request, is counted as "unknown" on the admin page, and is closed at once, so it
tries again every second or two until its user updates it.

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

A second machine holding a nearly current copy of the database, so that losing the server means minutes
rather than a restore from last night's backup, and optionally taking over by itself. It is optional:
a server with a nightly backup and a machine you could restore it on is a perfectly good setup. It needs
PostgreSQL and stored files in a bucket. All of it is in [STANDBY.md](STANDBY.md): your own PostgreSQL or
a hosted one, promoting, rebuilding the old primary, switching over by itself, and moving traffic.

## Updating

Re-run `deploy/install.sh` with the same arguments: it copies the new version, keeps the data and
settings, and restarts the service, which creates any new tables itself. By
hand: `git pull`, `uv sync --extra server`, rebuild the web app, restart.
