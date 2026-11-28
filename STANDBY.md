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
[moving traffic to the primary](#moving-traffic-to-the-primary), [a hosted PostgreSQL](#a-hosted-postgresql), and
[switching over without you](#switching-over-without-you).

There are two ways to get the standby, [your own PostgreSQL](#your-own-postgresql-primary-and-standby) or
[a hosted one](#a-hosted-postgresql); the server is told the same thing either way (`database`), and
everything else in [INSTALL.md](INSTALL.md) stays as it is. How traffic follows comes after, and then what to
use if the switch should happen without you.

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
   (`/etc/texloom`, and the bucket; the creation codes are in the database, so the standby has them), and stop it: `systemctl
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
and waits until it is streaming. A server can't run on a standby database, so it leaves the server
stopped and says so. It refuses, changing nothing, if this machine's server is the primary (it answers 200 on `/api/health`), if the
address is another cluster, or if you don't confirm; if the backup fails it puts the old data back.
`--cluster VERSION/NAME` picks one if there are several, and `--help` lists the rest. Until it has
run, a failure of the new primary has no standby.

### Switching on purpose

For maintenance: stop the server on the primary first, wait until the standby has everything (`SELECT pg_current_wal_lsn()` on the primary, and `pg_last_wal_replay_lsn()` on the standby, are equal), then promote and move the name: nothing is missing, so there is no catch-up.

A standby copies mistakes as well as data (a project deleted, a table dropped), so it doesn't
replace [Backups](INSTALL.md#backups). Try the switch once on your own pair before you need it:
`scripts/check_standby.py` does it on one machine with throwaway databases, which shows the steps
and what the apps do, but not your network or your DNS.

## Moving traffic to the primary

After a switch the name people use has to reach the new primary's server. Only a server whose database is
the primary runs at all (one pointed at a standby says so and stops), so its `/api/health` answering 200 is
the sign. How the servers are reached (their addresses, DNS, ports, certificates) depends on your network
and is yours to arrange; it isn't something TeXLoom does. Some ways:

- **DNS:** change the record to the standby's address. Lower its lifetime (TTL) to a minute or so
  beforehand, so the change is seen quickly.
- **A floating address** that you, or your provider's tools, move to the new primary.
- **A proxy or load balancer in front of both**, with a health check on `/api/health`.

Apps reconnect with backoff, so a switch looks like a short "connecting…": the time you take, plus whatever
your arrangement adds (a DNS record's lifetime, say).

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

## Switching over without you

For servers that take over from each other by themselves, run several relays: two or more machines, each
with a relay, sharing one database whose standby is promoted by Patroni when the primary is lost, and a bus
(Valkey, with Sentinels to replace its primary), behind a front door that sends people to the relays that
are up. Every relay serves at once, so losing one costs its apps a reconnect, and losing the database's
primary about half a minute of waiting (most of it Patroni's own wait before it promotes, which can be tuned).
[CLUSTER.md](CLUSTER.md) sets it up (one command, from a settings file listing the machines), and
[FUTURE_ARCHITECTURE.md](FUTURE_ARCHITECTURE.md) describes the design and how it was tested. (An earlier way, two servers sharing a lease in the bucket, is retired: a settings file that
still says `failover` gets a warning in the log and is otherwise unaffected.)
