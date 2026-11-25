# Scaling and availability

How the TeXLoom server is meant to grow, from one small relay to something that stays up when a machine
fails and copes with many people, and why it is built from the parts it is. (The server is called a relay
because it mostly passes encrypted changes between the apps of the people editing a project, and keeps a
copy.) Each part is marked **built** or **planned**; the work is tracked in [ROADMAP.md](ROADMAP.md) and
[issue #12](https://github.com/jhudsy/texloom/issues/12). Nothing here is needed to run a relay: a small one
stays one process on one machine.

## What makes this easier than most systems

- **Everyone keeps a full copy.** The desktop app and the browser hold the whole project, so the server is a
  way to meet and to keep a backup, not the only copy. If it is down, nobody loses anything, and edits made
  meanwhile sync when it returns. Availability is about convenience, not about avoiding data loss.
- **The server only sees encrypted data.** A second, third or someone else's server needs no more trust than
  the first.
- **Projects are independent.** Each project's changes are one ordered stream and nothing crosses between
  projects, so a project can live on any server (see "One person, projects on different
  servers" in [DESIGN.md](DESIGN.md)), and a database can later be split by project.
- **Compiling is not the server's job.** It happens on the user's machine (WebAssembly TeX Live in the
  browser, or TeX on the desktop). The server hands out the engine and TeX Live package files, which is disk
  and bandwidth, not CPU.

## The three kinds of state

Everything the server remembers is one of three kinds, and where each lives decides how the server can grow.

- **Durable records**: each project's edit log (one stream per project, numbered by the database), its
  registration (its owner, which is a creation code, and its storage mode) and the project lists. They live in the **database**.
- **Stored files**: pictures, PDFs and other uploads. They live in a folder or an **S3-compatible bucket**.
- **Live state**: what passes between the people connected to a project right now. The connections to one
  project are its **room**. A room carries cursors and presence, every change of a sync-only project (one the
  server only passes along and never stores), and a stored project's new changes passed on to the others as they arrive. It lives only in the
  relay's memory and is rebuilt when people reconnect: apps send each other what they lack (peer catch-up,
  [DESIGN.md](DESIGN.md)) and catch up from the database.

The relay also keeps counters and caches in memory (an owner's usage, who has what open);
`scripts/check_server_state.py` lists every one and what it would mean for several relays.

Durable records and files can be moved to systems built for availability and growth. Live state cannot be
put in a database, and sharing it between relays is the one new problem several relays bring (way 3).

**Why files go in a bucket.** They are most of the bytes, and an object store suits them: a file's key is
`<project>/<id>` and the id names the content, so a key holds one content for ever and nothing is updated in
place; the store makes the copies, so a second or replacement server attaches to the same bucket and has
every file; only four S3 requests are used (PUT, GET, DELETE, HEAD), so any S3-compatible store works (Garage,
RustFS, Cloudflare R2, Backblaze B2, AWS S3, …) and nobody is tied to a provider. A folder stays the default
for one small relay: one setting (`files`), one small interface (`server/files.py`). The failover lease also
lives in the bucket (way 2).

## Three ways to run it

Each way adds to the one before and is chosen by settings; the code for a way is loaded only when it is asked
for, so a small server carries none of the rest.

1. **One server** (the default, **built**).
2. **Two servers, with failover** (`failover`, **built**): one serves, the other takes over when it stops.
3. **Several relays** (**planned**): any number serve at once, sharing a database, a bucket and a message bus.

## Way 1: one server

A relay uses one of two databases, behind the same few calls (`server/db.py`, [issue
#30](https://github.com/jhudsy/texloom/issues/30)):

- **SQLite**: one file, one process, nothing to install. The default for anyone running their own relay. A
  second relay on the same file is refused.
- **PostgreSQL**: for a standby, replicas and several relays.

Both run the same SQL, and `scripts/check_store.py` holds them to the same answers, so growing means changing a
setting, not rewriting; `texloom-server --copy-database-from` moves a relay from one to the other. Quotas and
eviction are ordinary code on top.

**Losing the server.** Backups (INSTALL.md) cover it. A cold standby needs nothing more: a second machine
restores the latest backup every night (the database, the settings and codes, and the stored files or a copy
of the bucket), checks it, and stays stopped; if the main server is lost, it starts with that copy and the
name moves to it. Changes made since the backup aren't lost: the apps hold them, notice that the server's log
isn't the one they last saw (they send the last change they have in their hello, and the server says when it
doesn't match), and send everything again (`scripts/check_restore.py`). The nightly restore is also the proof
that the backups work.

## Way 2: two servers, with failover

**A warm standby, switched by hand** (**built**). With PostgreSQL, a second machine follows the primary by
streaming replication and can take over in minutes: one you run, or a hosted PostgreSQL that keeps its own
standby ([STANDBY.md](STANDBY.md); the relay is told the same thing either way, its `database`). A
person decides the primary is gone, promotes the standby, starts the relay on it and moves the name. The relay
refuses to start on a database that is still a standby, and says why. The last moments before the loss may be
missing on the standby; the apps send them again, as after a restore (`scripts/check_standby.py`).

### Switching over

**Automatic failover** (**built**, off unless `failover` is set: `server/failover.py`, [STANDBY.md](STANDBY.md#letting-the-two-servers-switch-over-by-themselves)). No third machine is needed, because the bucket is the tie-breaker:

1. The primary renews a small lease object in the bucket every few seconds.
2. The standby promotes itself only once the lease has not been renewed for long enough.
3. A primary that is alive but cut off from the standby can still renew, so the standby stays put.
4. A primary that cannot reach the bucket cannot renew, so it stops itself (it closes its connections and
   exits). Then the standby can promote safely.
5. A primary that comes back reads the lease first and, its database being a former primary's, waits to be
   rebuilt as a standby (`deploy/rebuild-standby.py`).

This needs a bucket that does conditional writes (not all do: STANDBY.md lists what is known, and a server
asked to fail over tests its store at start and refuses one that fails) and clocks that run at roughly the same
speed: nothing compares one machine's clock with another's. It is **modelled** (`models/failover`,
`scripts/check_model.py`, issue [#8](https://github.com/jhudsy/texloom/issues/8)) and run end to end, first
with two real servers (`scripts/check_failover.py`, in CI) and then with real parts in Docker (relays,
PostgreSQL primary and standby, an S3 store, Caddy; `scripts/rehearse_failover.py`, by hand). They settled
these points:

- **The timers.** The standby waits at least *the most one clock may run faster than another* times the
  primary's lease, on its own clock, after it last saw the lease change, and claims it with a conditional
  write on the version it saw, so a renewal in between makes the claim fail. A standby that could not read the
  lease counts its wait afresh once it can; otherwise a bucket that was away from both servers ends in a
  needless switch.
- **A suspended machine can overlap for a moment.** One whose clock stops while suspended can wake and write
  after the standby has taken over; timers cannot prevent that. It is harmless to the data, because an app
  that was told "stored" by it checks, when it connects to the new primary, that the log there has the last
  change it knows of, and sends everything again if not. So **the apps' hello check is what failover depends
  on**, and it must not be weakened (the model finds a way to lose a change if it can be fooled, and one if it
  is removed). A clock that counts suspended time (`CLOCK_BOOTTIME` on Linux) shortens the window.
- **The bucket becomes something everything waits on.** A primary that cannot reach it stops serving even
  though it and its database are fine; when it is back, service resumes by itself.
- **A server that hangs, rather than stops,** is noticed by the apps only when their keep-alive pings go
  unanswered (every 30 seconds), so people connected through it wait up to about a minute longer than for one
  that dies.

**What it leaves to you:** how traffic reaches whichever server is primary (`/api/health` says which; [STANDBY.md](STANDBY.md#moving-traffic-to-the-primary)
lists ways). Rebuilding the old primary as a standby is one command
(`deploy/rebuild-standby.py`). It does not use both servers at once and does not scale.

## Way 3: several relays

Planned. The idea: decouple state from the relays so that **any relay can serve any connection**, and let each
tier be kept available and scaled by the usual means for what it is.

```
  apps ──► DNS ──► relays (any number; each serves any connection)
                     │        │              │
         live messages│        │durable records│files
                     ▼        ▼              ▼
                bus (Redis)  database tier   bucket
```

Each tier grows and is kept available on its own: relays by adding relays (connections and CPU), the bus by
the means Redis offers, the database by a standard database tool (below), the bucket by the store. Changing one
leaves the others alone, apart from three coupling points to watch: the number of database connections
(relays times pool size), the bus traffic (it grows as a room's people are spread over more relays), and what a
database failover does to writes in flight (the apps resend them).

### Live messages: three designs

Live state (rooms, above) is the one thing that cannot be moved into a database. If the people in one project
may be connected to different relays, their live messages must reach each other across relays. Three ways:

- **A. Route by project** (the simplest; a stepping stone, and later an efficiency). A front end that hashes the project id in each address
  (`/ws/{project}`, `/blob/{project}/…`) sends every connection of a project to the same relay, so a room is in
  one relay's memory and nothing crosses between relays. A relay stopping moves its projects to the others and
  people reconnect. Plain DNS cannot do it: it needs a load balancer that hashes (nginx: `hash … consistent`).
  One very crowded project cannot outgrow its relay.
- **B. Any relay, with a bus** (**planned**, the target). Each relay publishes a room's live messages to a
  publish/subscribe bus, such as **Redis** (or a compatible one, such as Valkey), and subscribes to the rooms
  it has connections in. Relays are interchangeable, so DNS can simply list them all. Costs: one more
  component, and a message crosses the bus once for each relay holding part of the room, so a room spread over
  many relays costs more (hashing by project, design A, can be added later as an efficiency, not a
  requirement).
- **C. Gateways in front of room owners** (not planned). Many connection-holding gateways in front of
  room-owning servers placed by consistent hashing, so each room has one owner: what [Slack's real-time
  messaging](https://slack.engineering/real-time-messaging/) (gateway and channel servers) and [Figma's
  multiplayer](https://www.figma.com/blog/making-multiplayer-more-reliable/) (one in-memory process per open
  file) are described as doing. It pays only when connections far outnumber rooms. A TeXLoom relay holds no
  authoritative copy of a document (it cannot read one), so its rooms are light, which is why B is viable here
  and A is a stepping stone.

**Why a bus of this kind is enough.** Live messages may be lost without harm: after a reconnect the apps send
each other what they lack and catch up from the database, so delivery can be at most once. **Redis can be made
available** (Sentinel, or a managed service; a failover takes seconds, subscribers must subscribe again and
messages in flight then are lost). A relay treats any reconnection to the bus as "I may have missed
something, look again", as it already does for its database listener. While the bus is away, stored projects
keep syncing through the database and only live cursors, presence and sync-only projects pause. (How quickly
an app notices a gap in a stored project's changes during that is still to be checked.)

### The database tier

A separate problem with known solutions, which is why it is kept apart.

- **Kept available by a standard tool, not by `failover`.** `failover` fences a *relay* that has lost its
  lease, which is enough when the relay is the only client of its own database. With relays connected to a
  database over a network, a partitioned old primary's database would go on accepting writes from the others,
  so the database itself must be fenced, and that is what Patroni, pg_auto_failover and hosted PostgreSQL
  services do (Patroni arms a watchdog before promoting and demotes the old primary, using `pg_rewind` or a new
  base backup to rejoin it). That is from their documentation; none has been run here yet. The two ways are
  not combined.
- **Relays find the writable database from a list of hosts** (`postgresql://h1,h2/db?target_session_attrs=primary`),
  which the driver handles (tried with asyncpg: it picks the primary whichever order they are listed in, and
  finds the new one after a promotion). It does not protect against an old primary that is still alive: that is
  the tool's fencing.
- **Connections follow the number of relays, not of people.** Each relay uses up to 10 for queries (a maximum,
  mostly idle) plus one that listens, against PostgreSQL's 100 by default. A connection pooler (PgBouncer, or
  a managed one) multiplexes more; the listening and lock connections must bypass it, and the pool needs the
  pooler's prepared-statement settings (not tried here).
- **To grow further, split projects across databases by project id,** each with its own standby; projects never
  talk to each other. One thing does span projects: an **owner** is a creation code, and a server adds up the
  stored bytes of all of an owner's projects to enforce its allowance. With several databases an owner's
  projects may sit on different ones, so its total would be a sum across them or kept in one small shared
  place. That is left undecided.
- **A distributed SQL database** (horizontal writes in one) is not planned: the single primary has plenty of
  headroom (limit 5 below), and CockroachDB's [documentation](https://docs.cockroachlabs.com/docs/stable/postgresql-compatibility)
  says it lacks session advisory locks and LISTEN/NOTIFY, which the server uses (in the PostgreSQL class of
  `server/db.py`, so another class could replace them).

### The front door

With design B any relay serves any connection, so the name in DNS can list every relay's address; apps and
browsers try another address when one does not answer (the desktop app's connector does; not tried against a
dead one). Each relay needs a certificate for the name, and DNS does not itself know which relay is alive: a
dead one stays listed until removed or until a DNS service that health-checks does it. How traffic reaches the
machines (addresses, ports, certificates) is the operator's to arrange.

### What is built, and what is left

Built (`scripts/check_groundwork.py`, two servers on one PostgreSQL; each is safe and useful with one):

- **Storage totals in the database**: the `usage` ledger, kept in step with every change (set from a recount,
  and added to inside the transaction that appends a change), checked against a recount daily. A server's
  owner and server totals are still held in memory, started from the ledger.
- **Telling the other relays**: a purge, eviction, deletion, storage-mode change or move to another code is
  told to the others (LISTEN/NOTIFY, a small `{"p": project}`); a relay that hears it reads that project
  again, ignores what it said itself and what is not one of ours, and looks again at everything when its
  listening connection comes back.
- **Upkeep on one server at a time** (idle eviction, purges, the ledger's recount), under a PostgreSQL
  advisory lock; with one server, or SQLite, it always runs; a server with no database open does nothing.

Left:

- The bus: publishing and subscribing to rooms, the resync after a reconnect, and its availability.
- Reading an owner's total from the database at the moment of a quota check, so that two relays writing at
  once cannot together pass a limit.
- Per-project and per-address limits across relays (each relay keeps its own today, so they add up), and the
  same codes file on every relay.
- Whether a project is open anywhere, not only on this relay (eviction asks today).
- A rehearsal with two relays, as for way 2 (`scripts/rehearse_failover.py`).

### Keeping several servers possible

Until then, new code keeps to these rules, so that none of this gets harder:

- **Anything about a project is reached through its address.** A new address that reads or writes a project's
  data has `{project}` in its path (which also keeps design A open). One that doesn't (project lists,
  `/api/info`) keeps its state in the database, with changes that check what they replace (as project lists do).
- **State in a relay's memory is one of:** a setting; about its own connections; deliberately its own (memory
  budget, caches of public files, rate limits); about one project (a copy another relay could be told to drop);
  a total across projects (to be read from the database); or background work (to run once).
  `scripts/check_server_state.py` lists every piece and fails on a new one until it is put in one of these,
  which makes it a decision rather than an accident.
- **Totals across projects are worked out from what is stored** (the ledger is a kept copy, checked by
  recounting), so where they are kept can change without what they mean changing.
- **Background work is safe to run on one relay only,** and safe to repeat (a purge or eviction that stops
  half-way is finished next time).
- **Nothing a relay must know is only in its own files:** stored files go through the file store and the
  database holds the rest. Its own folder is for spooling uploads and caches.
- **Edits are numbered by the database,** never in a relay's memory.
- **SQL works on both databases** (`scripts/check_store.py`); what only PostgreSQL can do for several relays
  (LISTEN/NOTIFY, advisory locks) stays in its class in `server/db.py`.

### Open questions

- Detecting a gap in a stored project's live changes when the bus drops messages, and how fast apps notice.
- Securing the bus: it carries encrypted changes and unsigned live messages inside the trust boundary, so it
  needs a private network and authentication.
- Where owners' totals live once there are several databases.
- Whether to run the database ourselves or use a managed one: a managed one removes most of the work of
  replication, failover and backups, at a cost (the data is encrypted either way).

## The limits, in the order you would hit them

The order is reasoned, not measured: nobody has taken a deployment to its limits. `scripts/loadtest.py`
measures one relay on the machine it runs on (latency, CPU, memory per scenario); run it on a machine like
yours before relying on a number. What to watch meanwhile: the admin page (stored bytes, who is connected now,
each project's size), the relay process's CPU (`top`) and memory (`systemctl show texloom -p MemoryCurrent`),
the number of database connections (`SELECT count(*) FROM pg_stat_activity`), and the host's outbound traffic.

| # | The limit | What you would notice | What to do |
|---|---|---|---|
| 1 | **Bandwidth for the in-browser TeX engine.** The web editor compiles in the browser, so a first-time visitor downloads the engine from this server (`busytex.wasm` is 32 MB; the full TeX Live bundles are 93, 201 and 342 MB, but with "TeX Live files on demand" a document pulls only the package files it needs) and the browser keeps them afterwards. This is most of the bytes the server sends, and none of it needs the relay's CPU. | the host's outbound traffic or its provider's allowance; a slow first compile for new visitors | serve `/core` and `/texlive` from a CDN or a caching proxy in front (static files that change only with a release), and compile PDFs only when someone is looking ([#15](https://github.com/jhudsy/texloom/issues/15)). Cheaper than any more servers, so first. |
| 2 | **The relay's memory.** The systemd unit caps it (`MemoryMax=1G`), and `--memory` (512 MB by default) is what big work at once may use: catch-ups, large messages, uploads, arXiv imports. | big operations are refused or slow; the process is killed at the cap | raise `--memory` and `MemoryMax` together (the cap stays about 500 MB above it), or a bigger machine |
| 3 | **One relay's CPU.** It is one Python process, so one core. Live messages are passed between connections in its memory, so the work grows with connections times edits per second. | the process near 100% of one core; edits arriving late | a faster core helps more than more cores; then several relays (way 3) |
| 4 | **Database connections.** Each server uses up to 10 for queries plus one that listens, against PostgreSQL's `max_connections` (100 by default): about 8 servers. | `pg_stat_activity` near the limit; connection errors | a connection pooler (see "The database tier") |
| 5 | **Writes to the one primary database.** One small write per batch of edits for each active project (and the ledger's update, in the same transaction), and reads when someone connects and catches up. | the database's CPU or disk, queries waiting | in order: batch edits for a few hundred milliseconds so a burst of typing is one write; read replicas for catch-ups; and only then, projects split across databases by project id |
| 6 | **Storage.** Stored files live in the bucket (a cost, not a limit). The database holds each project's edit log, replaced by a snapshot as it grows (over 12 MB the snapshot is stored as an upload, and above 50 MB snapshots stop and the log grows: [#18](https://github.com/jhudsy/texloom/issues/18)). | the database's disk, the bucket's bill | idle projects are dropped from the server (`--evict-idle-days`; their writers upload them again), a project may use `--project-max-bytes` (200 MB by default), and a creation code has an allowance |
| 7 | **Availability** (not capacity): the relay, the database, the bucket, what sits in front. | people can't connect | way 2 for the relay (and, with `failover` on, everything waits on the bucket); a database tool for the database (way 3); the bucket's own redundancy; how traffic reaches the servers is yours to arrange |

**A project's size** on the admin page is mostly its stored files (figures, PDFs and the like), counted as
they are stored. Its edit log is small by comparison and is compacted, so a project of tens of megabytes is
ordinary, and the Snapshot column (the compressed editing history alone) is what the 12 MB and 50 MB thresholds
are about. A project is a concern when it approaches the per-project limit, or when many together approach what
the bucket and the database are sized for.

## What we are deliberately not doing

Several regions (TeXLoom is local-first, so latency matters little), a distributed database (above), and a
compile service (compiling is the client's job).
