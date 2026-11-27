# Future architecture: stateless relays

A proposal, not built, and not yet tracked in git. It describes where the server should go so that one
design runs on one machine with no high availability and grows, a machine at a time, to as many people as
needed, with high availability once there are enough machines. It replaces the three designs for "Way 3" in
[SCALING.md](SCALING.md), and in the end the lease-based failover of "Way 2" as well. Read SCALING.md first:
the reasons there (everyone keeps a full copy, the server sees only encrypted data, projects are
independent, compiling is not the server's job) are what make this design simple.

## The idea

Take every piece of state out of the relays and put it in a tier built to keep it:

```
  apps ──► front door ──► relays (any number, interchangeable, hold no state of their own)
                             │              │                    │
                 live messages│  exact records│              files│
                 + soft state │  (truth)      │                   │
                             ▼              ▼                    ▼
                        bus (Redis)     database             bucket
```

- **Relays** hold WebSocket connections, check signatures and pass messages on. A relay can stop at any time.
  Its apps reconnect to another one and lose nothing.
- **The database** holds everything that must be exact and lasting: each project's edit log (numbered by the
  database), registrations, storage modes, revocations, usage totals, project lists. It is the source of
  truth for stored projects.
- **The bucket** holds stored files, as now.
- **The bus** carries live messages between relays, one channel per project. It also holds *soft state*:
  state that may be lost or go stale without harm, and that must vanish when the relay that wrote it dies.
  Examples are who is connected to a project, and which relays are alive.

A **room** stays what it is in the code today: a relay's list of its own connections to one project. In
this design it is also the relay's subscription to that project's channel. The relay subscribes when its
first connection to the project arrives and unsubscribes when its last one leaves. No room has an owner,
and nothing routes a project to a particular relay.

### One machine to many

Each tier sits behind a small interface: a written **contract** (what it must guarantee) and a
**conformance check** that any implementation must pass, as `scripts/check_store.py` already holds SQLite
and PostgreSQL to the same answers. Anything that passes can be used. The implementations below are the
ones planned, not the only possible ones:

| Tier | The contract, in short | One machine (the default) | Several machines |
|---|---|---|---|
| Database | transactions; a per-project lock for the length of a transaction; numbering per project; durable once acknowledged | SQLite | PostgreSQL (`server/db.py` already has both); a distributed SQL database is possible if it passes |
| Bus and soft state | publish/subscribe by channel, at most once, in order per publisher, with every loss shown as a broken connection; keys with an expiry; atomic add | In-process: delivery to the relay's own rooms, dictionaries | Redis or Valkey; NATS and others are possible if they pass |
| Files | put, get, delete and exists, by key; a key's content never changes; conditional writes (only for Way 2's lease) | A folder | An S3-compatible bucket (`server/files.py` already has both) |

The relay code is the same either way. It always publishes through the bus interface, keeps soft state
through the soft-state interface and writes exact state in database transactions. On one machine the bus
never drops anything, so the code that fills gaps never runs, but it is there. Growing means changing
settings, not code. The existing `--copy-database-from` moves the database. A matching copy for files
moves a folder into a bucket.

### Configuration

One setting per tier, a URL, and **the URL's scheme chooses the implementation**. This is how `database`
and `files` already work:

```toml
database = "postgresql://db1,db2/texloom?target_session_attrs=primary"   # default: SQLite in the data folder
bus      = "redis+sentinel://s1,s2,s3/texloom"                            # default: in-process
files    = "s3://texloom"                                                 # default: a folder in the data folder
```

- **Nothing set is one machine.** A small server has none of these lines.
- **Every relay has the same settings file.** Nothing identifies a relay: its number comes from the bus at
  start-up. Adding a relay means copying the file to another machine and starting it.
- **Whether PostgreSQL and Redis are on this machine or others is only the host in the URL.** Moving one
  later changes one line.
- **Secrets stay out of the file:** passwords in the URL may come from the environment or a file the
  service reads, as now.
- **A relay refuses a combination that can't work, and says why.** A relay with an in-process bus, a SQLite
  database or a folder for files takes an exclusive lock on it at start-up; SQLite already refuses a second
  relay. So a second relay started against one-machine settings stops, rather than splitting the project
  into two rooms that never hear each other. It also checks the bus and database meet their contracts
  (SCALING.md: `failover` already tests a bucket's conditional writes this way).

That leaves the tiers' own settings (Patroni, Sentinel, the bucket) to those tools, and the relay with
three lines at most.

| Stage | What runs | Survives | Grow it when |
|---|---|---|---|
| 0 | One machine: one relay, SQLite, in-process bus, a folder | nothing (systemd restarts the relay) | one core is busy, or memory is short |
| 1 | One machine: one relay, PostgreSQL, Redis, a bucket | nothing; ready to add relays | as above |
| 2 | Two or more relays on their own machines; PostgreSQL and Redis on one machine; a bucket | a relay machine | the relays need more CPU or connections, or the database machine is the risk |
| 3 | As 2, with PostgreSQL kept available by Patroni (three etcd members) or a managed service, Redis by Sentinel (three sentinels) or a managed service, and a front door that checks health | any one machine | a tier reaches its limit (below) |
| 4+ | Each tier grown by its own means | any one machine per tier | — |

High availability starts at stage 3, at about three machines, because Patroni's etcd and Redis Sentinel
both decide by majority. Relays, etcd members and sentinels can share those machines. Managed PostgreSQL
and Redis remove most of that work.

How each tier grows past stage 3:
- **Relays:** add relays.
- **Redis:** one node carries a lot. After that, Redis Cluster with *sharded* pub/sub (`SPUBLISH` /
  `SSUBSCRIBE`, Redis 7), with each project's keys and channel on one shard (`{project}` in the key). Plain
  `PUBLISH` is copied to every node of a cluster, so it does not scale that way.
- **PostgreSQL:** in order, a connection pooler, batching edits, read replicas for catch-ups, and then
  splitting projects across databases by project id. The limits table in SCALING.md still applies.

## Where each piece of state goes

`scripts/check_server_state.py` lists everything a relay keeps in memory. Each piece goes to one of these:

| State | Goes to | How |
|---|---|---|
| "Is this change already stored?" and the append | Database | Both inside the append transaction, after `pg_advisory_xact_lock` on the project. Today the in-process `asyncio.Lock` (`store.lock`) does this. SQLite's single writer already serialises it. |
| An owner's storage total and its limit | Database | **Built.** Added up in the same transaction, holding the owner (`pg_advisory_xact_lock`): its projects' `usage` rows, its project lists, and uploads under way (`reserved`, rows that stop counting after 15 minutes, so a relay that dies mid-upload counts too much for a while, never too little). Only owners with a limit are added up or held; one owner's writes queue on its lock. If owners with very many projects make the sum slow, a row per owner kept in step is the next step. |
| The server's total (`--max-bytes`) | Each relay, read again from the database every minute | **Built.** Several relays together can pass it by what they accept in that minute: it is a guard against filling the machine, and an exact one would put every write in the server on one row. |
| Usage per project, storage mode, revocations, registration | Database (as now) | Relays cache them. Changes are announced on the bus (today: LISTEN/NOTIFY), and a relay that has missed announcements reads again. |
| Connection ids (`you`, `from`, `to`) | Nowhere shared | The relay's number (taken from a counter in the bus at start-up) times 2³², plus a counter of its own. Still an integer below 2⁵³, so JavaScript and old apps take it as they are. |
| Who is connected to a project, and on which relay | Bus, soft | A hash per project of connection id → relay. A relay renews its own entries and removes them when its connections close. |
| Which relays are alive | Bus, soft | A key per relay with an expiry (10 s), renewed every 3 s. When one expires, a surviving relay that shares a project with it sends `left` for its connections and removes them from the hash. |
| Is a project open anywhere (for idle eviction) | Bus, soft | Its connection hash is not empty. |
| Connections to one project (`MAX_PER_PROJECT`, 256 today, counted per relay) | Bus, soft | The size of the connection hash, checked before a connection is accepted. This also bounds a project's pace (below). |
| Rate limits per address | Bus, soft | `INCR` with an expiry per time window. An alternative is each relay enforcing 1/N of the limit, if a shared check per message costs too much. |
| A project's pace (the room's message rate) | Each relay, as now | A project spread over k relays may get up to k times the pace. k is at most the number of relays and at most the project's connections (`MAX_PER_PROJECT`), so this is accepted. Sharing it would mean every relay spending from one pace kept in Redis, a round trip per message, for no real gain. |
| Memory budget, caches of public files | Each relay, as now | They are about the relay itself. |
| Upkeep (eviction, purges, recounts) | One relay at a time | A PostgreSQL advisory lock, as now. |

Everything exact is in the database. Everything in Redis may be lost: a Redis failover can drop recent
writes, so nothing whose loss matters goes there. After a Redis failover, relays rewrite their soft state
from what they hold.

## Messages

### Channels

- `p:{project}`: everything sent to a whole project, such as changes (`up`), presence (`aw`), `left`, and
  broadcast peer-to-peer messages.
- `r:<relay>`: peer-to-peer messages sent to one connection (`to`). The sending relay looks up the target's
  relay in the connection hash and publishes only there. Peer-to-peer pieces are up to 4 MB, and files up
  to 50 MB in sync-only projects, so they should not be copied to every relay holding the project.

A relay delivers a message to its own connections first, then publishes it. It ignores its own messages
when they come back from the bus.

### A change to a stored project

1. Verify the signature (as now).
2. One transaction: lock the project, check for a duplicate, check the owner's limit, append, add to the
   ledger. The commit gives the change number `q`.
3. Deliver to this relay's other connections, and publish `{q, d, s}` on `p:{project}`.
4. **Then** acknowledge to the sender. Today the acknowledgement comes first. Reversing it means a relay
   that dies before publishing has not acknowledged, so the sender sends the change again.
5. A change that is already stored is acknowledged **and published again**. Today it is only acknowledged.
   That way, a resend after a relay died between storing and publishing reaches everyone. Receivers ignore
   copies they already have: changes merge in any order and any number of times.

On the receiving relay, each project with local connections keeps the highest change number below which
it has seen every change, starting from the database's head when the relay joins the project:

- The next number: pass it on.
- A higher one: pass it on too, since apps take changes in any order. If the numbers in between have not
  come within 500 ms, read them from the database and pass them on.
- One already seen: drop it.
- A periodic check, once a minute and batched for all the relay's projects
  (`SELECT project, seq FROM head WHERE project = ANY($1)`), catches the last change of a burst that never
  reached the bus. Nothing after it would show the gap. That case needs a relay to die between storing and
  publishing while the sender also goes away for good.

### A change to a sync-only project

Nothing is stored, so nothing can be read back. The guarantees come from the order of operations instead:

1. Deliver to this relay's other connections, then publish on `p:{project}`.
2. **Then** acknowledge. Today `ack q:0` comes first. An app keeps a change in its outbox until it is
   acknowledged, and sends it again after reconnecting.
3. Any relay whose connection to the bus breaks, for any reason (Redis failover, a slow subscriber
   disconnected by Redis, a network blip), may have missed messages. Once it is back, it tells every
   connection it holds in a sync-only project to catch up from its peers (`{"t": "resync"}`).
   - An app that understands `resync` sends its state vector (`sv1`), as it already does after every
     connect, and its peers send what it lacks.
   - Older apps don't know `resync`, so the relay closes their connections instead. They reconnect and do
     the same.

Redis pub/sub loses messages only around a break in a subscriber's connection, never silently on a live
connection. So the three steps cover every loss: the sender's relay died (no acknowledgement, the sender
resends), a receiver's relay died (its apps reconnect and catch up from peers), or the bus broke (`resync`).
One limit is the same as today: a change made while its writer was the only one connected reaches the
others only when the writer and they are next connected at the same time.

### Presence, joining and leaving

Presence (`aw`) goes on the project channel and may be lost: the next update replaces it. `left` is sent
by the relay that held the connection. When a relay dies, `left` is sent for each of its connections by a
surviving relay that notices the expiry. The `peers` count in the catch-up is read from the connection hash.

### Catch-up

Unchanged. Any relay reads the log from the database. A relay subscribes to the project's channel before
reading, so nothing arriving during the catch-up is missed; anything that arrives twice is ignored by the app.

## When something fails: how long people wait

Editing never waits: every app holds the whole project and edits locally. What a failure delays is
**collaborators seeing each other's changes** and **changes reaching the database**. It never loses a
change, because every one is either acknowledged after it is stored and published, or kept by its writer
and sent again.

The times are what the design aims for, with today's constants where they apply. None is measured. The
rehearsal (below) is there to measure them.

| What fails | How it is noticed | Effect for people, and how long | Back in step after |
|---|---|---|---|
| A relay **process** (its machine still up) | Its sockets close at once | Its apps reconnect to another relay: the web app's first retry is after 2 s, the desktop helper's after 1 s, plus the catch-up. Others see nothing. | ≈ 2–5 s. Unacknowledged changes are resent and passed on again; sync-only apps catch up from peers on connecting. |
| A relay **machine** or its network (no sockets closed) | The apps' keep-alive (built): a `ping` after 10 s of hearing nothing, given up when nothing comes back within 10 s more. | Its apps' changes reach nobody until they notice. | ≤ 20 s, then as for a relay process. |
| Redis, **with** Sentinel or a managed service | Relays lose their connection. The new primary takes a few seconds once Sentinel decides (`down-after-milliseconds`; aim for 5 s). | Stored projects: relays check database heads every second for their projects (one batched query each) and pass on what is new, so changes arrive about a second later. Sync-only projects, presence and peer-to-peer messages pause. Soft state is rewritten. | ≈ 5–10 s, then the relays fill gaps from the database and send `resync` to sync-only projects. |
| Redis, **without** high availability (stages 0–2) | As above | As above, for as long as Redis is down | As above, once it returns |
| The PostgreSQL primary, **with** Patroni or a managed service | Queries fail. Patroni promotes a standby after its leader key expires (30 s by default; tunable). Managed services quote tens of seconds to a few minutes. | Changes are not stored, so not acknowledged; writers keep them and retry. Relays still pass them on to collaborators as live-only (`q: 0`), as they already do when a change is refused, so collaboration continues. New connections can't catch up and use their local copy meanwhile. | ≈ 30–60 s (Patroni defaults), then the retries are stored. See the next row for changes the old primary acknowledged but never copied. |
| Changes lost with the old primary (asynchronous replication) | A relay's database connection breaks | Apps think those changes are stored. | A relay whose database connection broke compares PostgreSQL's timeline once it is back (Decided, below); if it went up, a failover happened, and it asks its connections to check that the server's log is still the one they saw (`{"t": "recheck"}`; older apps: their connection is closed), spread over a few seconds. The `last` check in the hello finds the loss and the apps send everything again, as after a restore. Fixed within seconds of the failover. Synchronous replication avoids the loss at the cost of one round trip per write. |
| The database **without** high availability (stages 0–2) | As above | As above, until it is restored | As above |
| The bucket | Uploads and downloads fail | Files wait in the writer's queue. Downloads fall back to peers (issue 16). Editing and compiling go on from local copies. | As long as the outage |
| A relay cut off from Redis or the database, but not from its apps | The relay's own checks fail | It would otherwise sit there passing on nothing. After 5 s it fails its health check, stops accepting connections and closes the ones it has, spread over a few seconds. | As for a relay process, ≈ 5–10 s |
| The front door lists a dead relay | DNS with several addresses: a client may try the dead one first and wait for its connect timeout | A connect to a dead address can take tens of seconds before the next one is tried | Health-checked DNS (a TTL of 30–60 s) or a load balancer removes it. How traffic reaches the machines is the operator's to arrange, as now. |
| A planned restart (deploys) | Not a failure | None noticeable: a relay stops accepting new connections and closes its own connections, spread over a minute, and they move to the others. | — |

Two smaller points:

- **After a long outage, reconnecting can lag by up to 30 s.** Apps back off to one try every 30 s, so
  someone may wait up to 30 s after a single-machine relay comes back. Random variation in the back-off,
  and a shorter cap once the server answers again, would help.
- **Mass reconnects need spreading.** When a relay closes many connections at once, the reconnects and
  catch-ups all land on the database together. Every close the relay starts is spread out, and catch-ups
  already go a page at a time.

## Compatibility

Old apps (0.4.38 is the oldest seen) keep working with no change to the protocol they use:

- Connection ids grow but stay JSON integers below 2⁵³.
- Messages old apps don't know (`resync`, `recheck`) are never sent to them. The relay closes their
  connection instead, which has the same effect. The app's version is in its hello.
- The order change (store, publish, then acknowledge) is invisible to apps.
- The keep-alive (built) is used only with a server that offers it in its catch-up reply, because older
  relays reject unknown message types. An old self-hosted server and a new app therefore still work together.

Mark any shim with `compat(…)` as usual.

## Building it

Each step is useful and safe on one machine, so the work can stop at any point. The early ones are worth
doing whatever happens later.

1. **Exact checks into the transaction:** the duplicate check under a transaction-level lock, and the
   owner's total checked and added to in the database. **Built** (`Store.locked`, `Policy.may_store`), and
   checked with two stores racing on one PostgreSQL in `scripts/check_groundwork.py`.
2. **Store, publish, then acknowledge, and publish duplicates again,** for stored and sync-only changes.
3. **App-level keep-alive** in the web app and the desktop helper. **Built:** `ping`/`pong`/`wait` and `ka`
   in the sync protocol (DESIGN.md), checked by `scripts/check_keepalive.py`.
4. **Connection ids that stay unique** across relays.
5. **The bus and soft-state interfaces,** with in-process implementations. All passing on of messages goes
   through them, even on one machine. **The contract and both implementations are built** (`server/bus.py`:
   `MemoryBus`, and `RespBus`, the relay's own Redis-protocol client, so that every break is seen), checked by
   `scripts/check_bus.py` against both, the Valkey one in Docker as a user allowed only `bus.COMMANDS`. The
   relay doesn't use it yet.
6. **Gap tracking in the relay,** filling from the database, the periodic head check, and `resync`/`recheck`
   (with closing for older apps).
7. **The Redis implementations:** relay heartbeats, the connection hashes, the per-relay channels, and
   behaviour while the bus is away (checking heads every second) or the database is away (live-only
   passing on).
8. **A model:** extend `models/` to check that no stored change is lost and that every collaborator ends up
   with every change, across relay deaths, bus breaks and a database failover. The failover model found the
   need for the hello check; this one should look for what the new message order might miss.
9. **A rehearsal in Docker:** three relays, PostgreSQL with Patroni and etcd, Redis with Sentinel, an S3
   store, and a front door. Kill each part and measure the times in the table above (as
   `scripts/rehearse_failover.py` does for Way 2). Add a load test that spreads one project's people over
   several relays.
10. **Retire Way 2's lease-based failover**, and update SCALING.md, STANDBY.md, INSTALL.md and the help page.

## Decided

- **One database, kept available, first.** The model is one PostgreSQL primary with standbys (Patroni or a
  managed service), not several databases. Several are only a later step, if one primary's writes become
  the limit (SCALING.md's limit 5), by splitting projects across databases by project id. A distributed SQL
  database (CockroachDB, YugabyteDB and the like) is the other way past that limit, as another
  implementation of the database contract. This design needs less of PostgreSQL than today's server does:
  the bus replaces LISTEN/NOTIFY, leaving transaction-level advisory locks to check.
- **Owner totals, if there are several databases:** a shared table, in its own small database, kept
  available like the others. A change can't be written to two databases in one transaction (that needs a
  two-phase commit), so the shared total is **reserved first**: check and add the change's size to the
  owner's total (atomic), then append to the project's database. If the append fails, give the reservation
  back. A relay that dies in between leaves the total too high, never too low, so a crash can only refuse
  too early, never let an owner past the limit. The daily recount (the `usage` ledger is already checked
  this way) corrects the total from what the project databases hold.
- **A project's pace is not shared** (above).
- **Large peer-to-peer transfers over the bus happen only in sync-only projects.** In a stored project,
  files go to the bucket and peers fetch them from there, over HTTP, not the bus. A sync-only project's
  promise is that the server keeps nothing, so its files go from app to app through the relay, in 4 MB
  pieces, up to 50 MB a file. The same goes for a file another app asks for that the bucket doesn't have
  (issue 16's asking of one peer, then others). These travel on the target relay's channel (`r:<relay>`),
  never the project's, so they cross the bus once. Measure what they cost Redis in the rehearsal. If it
  matters, a relay could pass large pieces directly to the target relay over HTTP instead.
- **Telling a database failover from a blip** uses PostgreSQL's own record of promotions. Health checks
  answer "is this the primary now?": `target_session_attrs=primary` in the URL makes the driver connect only
  to the primary, `SELECT pg_is_in_recovery()` answers it for a connection, and Patroni serves the same as
  HTTP for load balancers (`/primary`, `/replica`, `/health`). Whether the primary has *changed* is the
  **timeline**, which goes up by one at every promotion (`SELECT timeline_id FROM pg_control_checkpoint()`).
  A relay notes it when it connects. After a reconnect, a higher timeline means a failover happened and
  acknowledged changes may be missing, so it sends `recheck`; the same timeline means a blip, and nothing
  more is done. A managed service that hides the timeline gets the safe default: every reconnect is
  treated as a failover.
- **PostgreSQL and Redis on one machine are only URLs** (Configuration, above). The open part was whether
  to keep SQLite and the in-process bus at all, so that a small server needs nothing installed. Keep them:
  they are small, and the conformance checks hold them to the same contract.
- **The bus is on a private network,** with Redis ACLs (a user that may only publish, subscribe and use
  TeXLoom's keys) and TLS.

## Open questions

- **Whether the bus keeps its contract under load:** a subscriber that falls behind is disconnected by
  Redis (`client-output-buffer-limit pubsub`). That is a loss shown as a broken connection, so it is
  handled, but the limit must be well above what a busy relay takes in. Measure in the rehearsal.
- **How many projects one relay can subscribe to** before subscribing and unsubscribing costs more than it
  should. Rooms come and go as people open and close projects.

This document is also where operating details go (settings, health checks, what to watch) until there is
a runbook and the installation guide covers several relays.
