# Scaling and availability

How the TeXLoom server is meant to grow from one small relay to something that stays up when a
machine fails and copes with many people, and why it is built from the parts it is. What exists
today is marked **today**; the rest is **planned** (see [ROADMAP.md](ROADMAP.md), and
[issue #12](https://github.com/jhudsy/texloom/issues/12) for the work itself). Nothing here is
needed to run a relay: a small one stays one process on one machine.

## What makes this easier than most systems

- **Everyone keeps a full copy.** The desktop app and the browser hold the whole project, so the
  server is a way to meet and to keep a backup, not the only copy. If the relay is down, nobody
  loses anything and edits made meanwhile sync when it returns. Availability is about convenience
  and about the backup, not about avoiding data loss.
- **The server only sees encrypted data.** A second, third or someone else's server needs no more
  trust than the first.
- **Projects are independent.** Each project's changes are one ordered stream and nothing crosses
  between projects. So a project can live on any server, and a database can later be split by
  project without redesigning anything. (A project may already live on any server: see "Projects on
  several servers" in [DESIGN.md](DESIGN.md).)
- **Compiling is not the server's job.** It happens on the user's machine (WebAssembly TeX Live in
  the browser, or TeX on the desktop). The server hands out TeX Live package files on demand, which
  is disk and bandwidth, not CPU.

## What runs today

One relay process (aiohttp behind nginx, a systemd service), a PostgreSQL database for the edit
logs, project records and project lists (a relay can use an SQLite file instead: below), and stored files in a folder or an S3-compatible bucket.
Who is connected to which project, and some counters (usage, owners), are kept in the relay's
memory, which is how a change reaches the others connected to it.

## Why stored files go in a bucket

Stored files are most of the bytes the server keeps, so they are the first thing to move off its
disk. An object store fits them unusually well:

- **They never change.** A file's key is `<project>/<id>` and the id names the content, so a key
  holds one content for ever. Writing it twice is harmless and nothing is updated in place, which
  is all an object store offers and all this needs.
- **The store makes the copies.** Durability, replication and growth are its job, not ours. A
  second server, or a replacement, attaches to the same bucket and has every file at once, with
  nothing to copy.
- **It is cheap and standard.** Only four S3 requests are used (PUT, GET, DELETE, HEAD), signed
  with the ordinary S3 scheme. Any S3-compatible store works: Garage or RustFS on your own
  machines, or a hosted one. Nobody is tied to a provider, and the store sees only encrypted data.
- **A folder stays the default.** For one small relay a folder is simpler and needs nothing. The
  choice is one setting (`files`), behind one small interface in `server/files.py`.

The same reasoning is behind the next piece: a bucket is also something every server can reach, so
it can hold the small amount of shared state that two servers must agree on (below).

## The database

A relay uses one of two, behind the same few calls (`server/db.py`, [issue
#30](https://github.com/jhudsy/texloom/issues/30)):

- **SQLite**: one file, one process, nothing to install. The default, for anyone running their
  own relay. It is the single-relay option: a second relay on the same file is refused.
- **PostgreSQL**: replicas, a standby, several relays and, later, splitting projects across
  databases. For servers that need availability or scale.

Both run the same SQL, and one check (`scripts/check_store.py`) holds them to the same answers,
so growing means changing a setting, not rewriting. A relay moves from one to the other by copying
everything across while it is stopped (`texloom-server --copy-database-from`, INSTALL.md).

Accounting and policy (quotas, eviction) are ordinary code on top, kept in the relay's memory. For
several relays at once two more pieces are needed, with PostgreSQL: a **notifier** ("project X has
a new change", LISTEN/NOTIFY) and a **lock or lease** for the single-writer rules; and what each
relay keeps in memory moves into shared state ([#12](https://github.com/jhudsy/texloom/issues/12)).

## Availability

### Today: a cold standby from the backups

The simplest standby needs nothing beyond the backups INSTALL.md describes: a second machine
restores the latest one every night (the database, the settings and codes, and the stored files
or a copy of the bucket), checks it, and stays stopped. If the main server is lost, it starts
with that copy and the names move to it. Changes made since the backup aren't lost: the apps hold
them, notice that the server's log isn't the one they last saw (they send the last change they
have in their hello, and the server says when it doesn't match), and send everything again. So a
restore costs a catch-up, not data (`scripts/check_restore.py`). The nightly restore is also the
proof that the backups work.

### Today: a warm standby, switched by hand

With PostgreSQL, a second machine can follow the primary by streaming replication and take over
in minutes, either one you run or a hosted PostgreSQL that keeps its own standby (INSTALL.md, "A
standby server"; the relay is told the same thing either way, its `database`). A person decides
that the primary is gone, promotes the standby, starts the relay on it and moves the name. The
relay refuses to start on a database that is still a standby, and says why. The last moments
before the loss may be missing on the standby; the apps send them again, as after a restore.
`scripts/check_standby.py` runs exactly that, with a standby that never got the last changes.
What is left is making the decision and the switch automatic (below).

### Today: automatic failover, off unless asked for

The lease below is built (`server/failover.py`; INSTALL.md, "Letting the two servers switch over by
themselves"), modelled first (`models/failover`) and run end to end with two real servers
(`scripts/check_failover.py`). It is one module that is loaded only when `failover` is set, so a
single server, or a pair switched by hand, carries none of it. Moving the name to whichever server
says it is the primary is left to you, with two documented arrangements (INSTALL.md, "Moving traffic
to the primary"): both machines in DNS with Caddy forwarding to the primary
(`deploy/Caddyfile.failover`), or, for a standby on a home connection, a script that runs your DNS
command while the server is the primary (`deploy/follow-primary.sh`). Rebuilding the old primary as
a standby afterwards is manual too.

### Two machines are enough

Nothing needs a vote, because the database is the one place changes are numbered and the relays
defer to it. With one primary (the main server) and one standby, the standby holds a nearly
current copy, kept up by PostgreSQL's streaming replication, and the files are in the bucket.

### Switching over

Detecting that the primary has failed and promoting the standby can be automatic. The danger with
two machines is that the standby cannot tell a dead primary from a broken link between them. If it
promotes while the primary is alive, two primaries number changes differently ("split brain"). A
client's content would survive, since clients keep everything and merge, but the order of changes
on the server would have to be repaired. So the switch must be safe, not just fast.

The design uses the bucket as the tie-breaker, so no third machine is needed:

1. The primary renews a small "I am primary" lease object in the bucket every few seconds.
2. The standby promotes itself only once the lease has expired.
3. A primary that is alive but cut off from the standby can still renew the lease, so the standby
   stays put.
4. A primary that cannot reach the bucket cannot renew, so it stops itself. Then the standby can
   promote safely.
5. A primary that comes back reads the lease first and rejoins as a standby.

This needs a store with conditional writes (most S3-compatible ones have them; check yours) and
clocks that run at roughly the same speed (nothing compares one machine's clock with another's). The
design is **modelled** (`models/failover`, `scripts/check_model.py`, issue
[#8](https://github.com/jhudsy/texloom/issues/8)), and the model settled these points before any
code:

- The standby waits at least *the most one clock may run faster than another* times the primary's
  lease, on its own clock, after it last saw the lease change; and it claims the lease with a
  conditional write on the version it saw, so a renewal in between makes the claim fail.
- A suspended machine whose clock stops can wake and write for a moment after the standby has taken
  over. Timers can't prevent that. It is harmless to the data, because an app that was told "stored"
  by it checks, when it connects to the new primary, that the log there has the last change it knows
  of, and sends everything again if not. So the apps' hello check is what failover depends on, and it
  must not be weakened (the model finds a way to lose a change if the check can be fooled, and one if
  it is removed). Using a clock that counts suspended time (`CLOCK_BOOTTIME` on Linux) shortens the
  window.
- A store whose conditional write isn't atomic and consistent would break all of it, so the failover
  code has to test its store when it starts and refuse to run on one that fails.

Promoting the database is half of it: traffic must follow, by changing DNS (with a short TTL) or
through a front end that checks health. Clients already reconnect with backoff and resume from
their last acknowledged change, so a switch looks like a short "connecting…".

### A standby that is not public

A standby on a home network (a small computer on a private network) works for replication, which
is asynchronous and tolerant of a slow link: at worst the last few seconds of server-side changes
are missing, and clients resend them. To take over, though, it must be reachable by the public, and
its connection and disk then set everyone's speed. Use an SSD, not an SD card. It adds little as
extra capacity.

## Scaling

### Where the load is

1. **The TeX engine and package files.** Most of the bytes. Serve them from a CDN, and compile
   output (PDFs) only when someone is looking ([issue #15](https://github.com/jhudsy/texloom/issues/15)).
   This is cheaper than adding servers, so it comes first.
2. **Live editing.** Passed between connections in the relay's memory; it does not touch the
   database.
3. **The database.** One small write per batch of edits to each active project, and reads when
   someone connects and catches up.

### Growing, in order

1. **Several identical relays** behind a load balancer, sharing one PostgreSQL primary, with every
   connection to a project sent to the same relay (below). Totals across projects move into the
   database, relays tell each other when one changes a project another holds, and maintenance (the
   reaper, eviction, purges) runs once, not once per relay.
2. **When the database starts to feel it**, in this order: batch edits for a few hundred
   milliseconds so a burst of typing is one write; put read replicas (the standbys) to work on
   catch-ups; add a connection pooler (PgBouncer, transaction pooling) if the number of relays
   multiplied by the pool size (10 each) approaches PostgreSQL's limit, keeping the notifier on
   its own direct connection. None of this is needed early: the relay's pool is small and the admin
   page and PostgreSQL's statistics show trouble long before users do.
3. **Only if one primary is not enough**, split projects across several databases by project id,
   each with its own standby. Projects don't talk to each other, so this needs no redesign.

### Several relays: route by project

A relay passes more than stored edits between a project's connections: cursors and presence, and
every change of a sync-only project, go from one connection to the others and are never stored.
So "project X has a new change, read it from the database" can't carry them, and forwarding them
between relays would mean every message crossing (LISTEN/NOTIFY carries at most 8 KB), plus a lock
per project held in the database across several steps, and the ordering questions of
[#8](https://github.com/jhudsy/texloom/issues/8).

Instead, every connection to a project goes to the same relay: the project is in each of its
addresses (`/ws/{project}`, `/blob/{project}/…`), and the front end picks the relay from it
(nginx: `hash … consistent` on the project part of the address). A project's room, its lock and its counters then stay in that
relay's memory, as now. When a relay stops, its projects move to the others and people reconnect,
as after a restart today. What is left for several relays
([#12](https://github.com/jhudsy/texloom/issues/12), about 3–4 days):

- **Totals across projects into the database**: an owner's usage, the server's total, project
  lists' sizes. Kept as rows updated in the same transaction as each write, worked out once from
  what is stored when moving to them.
- **Telling the other relays**: maintenance, a purge, or another relay's admin command can change
  a project another relay holds (its usage, owner, deletion, storage mode). A small LISTEN/NOTIFY
  message ("drop what you know of project X") makes that relay read it again.
- **Maintenance once**: the reaper, eviction and purges run on whichever relay holds a PostgreSQL
  advisory lock.
- **A check with two relays on one database**: routing, totals across both, a relay stopping, and a
  project changed by the other relay.
- Each relay keeps its own rate limits (they add up across relays), and every relay needs the same
  codes file.

### Keeping several servers possible

Until then, new code keeps to these rules, so that none of this gets harder:

- **Anything about a project is reached through its address.** A new address that reads or writes
  a project's data has `{project}` in its path. One that doesn't (project lists, `/api/info`)
  keeps its state in the database, with changes that check what they replace (as project lists do).
- **State in a relay's memory is one of:** a setting; about its own connections; deliberately its
  own (memory budget, caches of public files, rate limits); about one project (a copy another relay
  could be told to drop); a total across projects (to move into the database); or background work
  (to run once). `scripts/check_server_state.py` lists every piece and fails on a new one until
  it is put in one of these, which makes it a decision rather than an accident.
- **Totals across projects are worked out from what is stored**, so that moving them into the
  database later is a change of where they are kept, not of what they mean.
- **Background work is safe to run on one relay only**, and safe to repeat (a purge or eviction
  that stops half-way is finished next time).
- **Nothing a relay must know is only in its own files**: stored files go through the file store,
  and the database holds the rest. Its own folder is for spooling uploads and caches.
- **Edits are numbered by the database**, never in a relay's memory.
- **SQL works on both databases** (`scripts/check_store.py`); what only PostgreSQL can do for
  several relays (LISTEN/NOTIFY, advisory locks) stays in its class in `server/db.py`.

### What we are deliberately not doing

Several regions (TeXLoom is local-first, so latency matters little), a distributed database (the
single primary has plenty of headroom and is far simpler to reason about), a separate message
system such as Redis or NATS (LISTEN/NOTIFY is enough for a signal that carries no data), and a
compile service (compiling is the client's job).

## Open questions

- The ordering model for several writers (the lease above is modelled; several writers aren't: [#8](https://github.com/jhudsy/texloom/issues/8)).
- Whether to run the database ourselves or use a managed one: it removes most of the work of
  replication, failover and backups, at a cost, and the data is encrypted either way.
