# A standby server

A second machine holding a nearly current copy of the database, so that if the server ([INSTALL.md](INSTALL.md))
is lost you can be running again in minutes rather than restoring from last night's backup. Nobody loses
work either way (everyone's devices hold full copies and send what the server lacks when they
reconnect: `scripts/check_standby.py` and `scripts/check_restore.py` run both cases), so this buys
less waiting, not safety. It is optional: a server with a nightly backup and a machine you could
restore it on is a perfectly good setup. It needs PostgreSQL, and stored files in a bucket, so that
the standby sees them too ([Stored files](INSTALL.md#stored-files)). With SQLite, [Backups](INSTALL.md#backups) is the
way.

Contents: [your own PostgreSQL, primary and standby](#your-own-postgresql-primary-and-standby)
([when the primary is lost](#when-the-primary-is-lost), [rebuilding the old primary](#afterwards-rebuild-the-old-primary),
[switching on purpose](#switching-on-purpose)),
[switching over by themselves](#letting-the-two-servers-switch-over-by-themselves),
[moving traffic to the primary](#moving-traffic-to-the-primary), and [a hosted PostgreSQL](#a-hosted-postgresql).

There are two ways to get the standby, [your own PostgreSQL](#your-own-postgresql-primary-and-standby) or
[a hosted one](#a-hosted-postgresql); the server is told the same thing either way (`database`), and
everything else in [INSTALL.md](INSTALL.md) stays as it is. Switching over by itself, and how traffic follows, come after.

## Your own PostgreSQL, primary and standby

PostgreSQL (14 or later, the same major version on both machines) copies the primary's changes to
the standby as they happen (streaming replication). The standby is read-only until promoted, and
the server refuses to start on it, saying why.

1. **Allow each machine in on the other.** Either may be the standby, and after a switch the old primary
   is rebuilt from the new one, so both need to let the other replicate. Create the role once, on the
   primary (it is copied to the standby), and on **each** machine let the other's address in over TLS
   (in `pg_hba.conf`, which is not copied; PostgreSQL's `listen_addresses` must include an address the
   other can reach: keep it off the public internet, with a firewall or a private network):

   ```bash
   sudo -u postgres psql -c "CREATE ROLE replicator REPLICATION LOGIN PASSWORD 'a long random password'"
   sudo -u postgres psql -c "SELECT pg_create_physical_replication_slot('standby')"
   # pg_hba.conf, on each machine, with the OTHER machine's address:
   #   hostssl  replication  replicator  OTHER-ADDRESS/32  scram-sha-256
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

4. **Plan how the name will move to the standby** ([Moving traffic to the primary](#moving-traffic-to-the-primary)),
   including a certificate for the name on the standby before it is needed.

### When the primary is lost

On the standby:

1. Make sure the old primary is really down, and will stay down. Two primaries would number
   changes differently, and the apps would have to untangle it. If you can't be sure (it may only be
   cut off), power it off or disconnect it.
2. Promote the standby: `sudo -u postgres psql -c "SELECT pg_promote()"`. It is now a primary and
   takes writes.
3. Start the server there: `systemctl enable --now texloom`.
4. Move the name to the standby's address. Apps reconnect on their own, and send again whatever the
   standby lacked (as in the introduction), so nobody has to do anything.

### Afterwards: rebuild the old primary

You now have one server and no standby. The old primary must not come back as a primary:
rebuild it as a standby of the new one, with the helper, on the old primary's machine (as root):

```bash
sudo env PGPASSWORD='the replicator password' /var/www/texloom/deploy/rebuild-standby.py NEW-PRIMARY-ADDRESS
```

It stops the TeXLoom server and PostgreSQL there, **keeps the old data directory aside** (as
`….old-DATE`; delete it when you are sure: it holds the changes the new primary never got, which the
apps send again), takes a streaming base backup of the new primary, starts PostgreSQL as its standby,
and waits until it is streaming. If `failover` is on it then starts the server again, which waits as
the standby; without failover a server can't run on a standby database, so it leaves the server stopped
and says so. It refuses,
changing nothing, if this machine's server is the primary (it answers 200 on `/api/health`), if the
address is another cluster, or if you don't confirm; if the backup fails it puts the old data back.
`--cluster VERSION/NAME` picks one if there are several, and `--help` lists the rest. Until it has
run, a failure of the new primary has no standby.

### Switching on purpose

For maintenance: stop the server on the primary first, wait until the standby has everything (`SELECT pg_current_wal_lsn()` on the primary, and `pg_last_wal_replay_lsn()` on the standby, are equal), then promote and move the name: nothing is missing, so there is no catch-up.

A standby copies mistakes as well as data (a project deleted, a table dropped), so it doesn't
replace [Backups](INSTALL.md#backups). Try the switch once on your own pair before you need it:
`scripts/check_standby.py` does it on one machine with throwaway databases, which shows the steps
and what the apps do, but not your network or your DNS.

## Letting the two servers switch over by themselves

Optional, and off by default: a server on its own, and a pair you switch by hand, need none of
this, and with it off the server doesn't even load the code for it. When on, the two servers share
a small "lease" in the bucket. The one that holds it is the primary and serves; it renews the lease
every few seconds. The other waits, and when the lease hasn't been renewed for long enough it takes
it, promotes its own PostgreSQL and serves. A primary that can't renew (the bucket is unreachable) stops
itself, closing its connections, before the other can take over, so there are never two serving.
The design and the checks behind it are in `models/failover/README.md`; `scripts/check_failover.py`
runs two real servers through a lost primary, one cut off from the bucket and the bucket going away;
`scripts/rehearse_failover.py` does it again with real parts in Docker (see its first lines).

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

- **Passes the test:** RustFS.
- **Documented as supported** (`If-Match`, `If-None-Match`), so run the test on yours: Cloudflare R2, AWS S3.
- **Fail or unconfirmed:** Garage ignores the conditions (fine for stored files, not for the lease);
  Backblaze B2 is reported by users of other tools to answer `If-None-Match` with 501, so it can't hold a
  lease (not tried here).

If the bucket you keep files in can't, the lease can't live there (it is in the same bucket as the
stored files); failover then isn't available until one that can is used.

Then, once, on the **primary**, let the server's PostgreSQL role promote a standby (the grant is
copied to the standby with everything else):

```bash
sudo -u postgres psql texloom -c "GRANT EXECUTE ON FUNCTION pg_promote(boolean, integer) TO texloom"
```

Optionally, so that the admin page can show the standby's state and how far behind it is (otherwise it
says the details are hidden), let that role read PostgreSQL's replication statistics:

```bash
sudo -u postgres psql -c "GRANT pg_monitor TO texloom"
```

And in `texloom.toml` on **each** server (each needs its own `failover_name`), then restart both,
the primary first:

```toml
failover = true
failover_name = "server-a"   # "server-b" on the other
# failover_ttl = 15          # seconds
```

- **The admin page** (`/admin`, with the admin token) gets a Failover section: this server's role, who
  holds the lease and how recently it was renewed (or, on the standby, how long it has been unchanged
  against the wait), and PostgreSQL's side: whether it is a primary and its standbys' state and delay, or
  a standby and how far its replay is behind. The standby serves the admin page too, with just that
  section, since it has no database open.
- **Time.** A primary stops itself after `failover_ttl` seconds without a successful renewal, and the
  standby waits twice that, plus a margin, before it takes over: with the default 15 seconds, about
  35, plus a few for PostgreSQL to promote, so people see "connecting…" for under a minute. A lower
  value is quicker and more easily upset by a slow bucket. Servers' clocks need not agree, only run
  at about the same speed: nothing compares one machine's clock with the other's.
- **After a switch,** the server that stopped (or was lost) restarts as a standby, but its PostgreSQL is
  still a primary with a history the new one doesn't have, so it **won't take over** (it says so in its
  log) until you [rebuild it](#afterwards-rebuild-the-old-primary). Until then you have one server and no
  standby.
- **Starting a server again** (an update, a restart) while it holds the lease takes it back within a
  couple of renewal intervals (about ten seconds by default). The other server sees renewals missing
  for that long, which is well short of the wait before it takes over.
- It runs under the install script's systemd unit as it is (`Restart=on-failure`: a server that stops
  itself exits with status 75).
- **The bucket becomes something everything waits on.** The lease lives in it, so a primary that can't
  reach it for `failover_ttl` seconds stops serving, even though it and its database are fine. While the
  bucket is away from both servers nothing is served; when it is back the service resumes by itself, in
  about the time a restart takes, and the standby does not take over on the strength of the time it
  couldn't look. (Without failover, editing and syncing do not depend on the bucket.)
- **A server that hangs, rather than stopping, is noticed by the apps when their keep-alive goes unanswered**
  (a ping after 10 seconds of hearing nothing, given up 10 seconds later), so people connected through it
  wait up to about 20 seconds longer than for a server that dies, whose connections are reset. The standby still takes over after the wait.
- **What it can't do** (see `models/failover/README.md`): a machine suspended and woken (a virtual
  machine) after the other took over can accept changes for a moment until its first renewal fails.
  Nobody loses anything, because an app that was told "stored" by it checks, when it reconnects, that
  the new primary has its last change, and sends everything again if not.

## Moving traffic to the primary

Failover makes one server the primary; it does not move traffic. Each server's `/api/health` answers
**200 only on the primary**, and 503 on a standby and on a server that has stopped itself (the body
says which it is and who holds the lease). Whatever people reach the servers through must send them to
the one that answers 200. How the servers are reached at all (their addresses, DNS, ports,
certificates) depends on your network and is yours to arrange; it isn't something TeXLoom does.

Some ways to follow the primary:

- **A proxy on each machine that checks both servers.** `deploy/Caddyfile.failover` is an example for
  two machines that the public can reach: both addresses are in DNS, and each machine's Caddy forwards
  to whichever relay answers 200, so nothing changes at a switch. Its first lines say what the relays
  need (to reach each other over a private network, and never port 8787 open to the internet).
- **A load balancer, or a DNS service, with health checks** on `/api/health`.
- **A floating address** that your own tooling moves to the primary.
- **A command of your own** that looks at `/api/health` and updates whatever you use.

Apps reconnect with backoff, so a switch looks like a short "connecting…": about the time the standby
waits (see above), plus whatever your arrangement adds (a DNS record's lifetime, say).

## A hosted PostgreSQL

A provider's PostgreSQL service keeps its own standby, switches over by itself and takes its own
backups, which is most of the work above. Use its address as `database`:

```toml
database = "postgresql://texloom:PASSWORD@HOST:5432/texloom?sslmode=require"
```

The only differences from a PostgreSQL of your own:

- **The address.** The password goes in the settings file or `TEXLOOM_DATABASE` in the
  environment, as for any secret ([Settings](INSTALL.md#settings)). Install the server as usual and then
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
