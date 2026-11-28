# Future architecture: stateless relays

A proposal, not built, and not yet tracked in git. It describes where the server should go so that one
design runs on one machine with no high availability and grows, a machine at a time, to as many people as
needed, with high availability once there are enough machines. It replaces the three designs for "Way 3" in
[SCALING.md](SCALING.md), and the lease-based failover of "Way 2" (now retired) as well. Read SCALING.md first:
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
| Files | put, get, delete and exists, by key; a key's content never changes | A folder | An S3-compatible bucket (`server/files.py` already has both) |

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
  relay. **Built for the bus and PostgreSQL:** a relay with no `bus` holds an advisory lock on the database
  (`ALONE_LOCK`) and a second one refuses to start, saying why (`scripts/check_groundwork.py`). The `bus`
  setting exists (`--bus`, `bus = …`) and the relay opens it at start. So a second relay started against one-machine settings stops, rather than splitting the project
  into two rooms that never hear each other. It also checks the bus and database meet their contracts.

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

**Measured in the lab** (`scripts/rehearse_relays.py`, Docker on one computer, Patroni's and Sentinel's default
timings; seconds from the break):

| What broke | Back after |
|---|---|
| PostgreSQL primary killed / frozen / cut off | writes through the multi-host address work again after 26 / 29 / 25 s. A cut-off primary demotes itself after 14 s, so there were never two. The new timeline shows within 2 s of writes resuming. |
| Valkey primary killed / frozen / cut off | a replica is promoted and takes writes after 7 / 7.5 / 7.5 s. The old one, once mended, still takes writes for about 10 s, and loses them when it rejoins (soft state only, so harmless). |
| A whole machine (holding both primaries) | Valkey after 7–8 s, PostgreSQL after 24–27 s; etcd keeps its majority. |
| A relay, seen through nginx (`least_conn`, `max_fails=1`, `fail_timeout=5s`) | nginx leaves a killed relay out after 3–8 s, a frozen one after 10 s, a cut-off one after 11–19 s. No request through nginx failed. |

**Several relays at once, measured in the lab** (`collab`: three apps editing one stored project, each on a relay of
its own, and two on a sync-only project, all through nginx; every app makes a change at the break and another 5 s
later; seconds from the break until every app has every change):

| What broke | Stored project (all also stored) | Sync-only project |
|---|---|---|
| a relay killed | 5.5 | 5.5 |
| a relay frozen | 24 (its apps notice by their keep-alive in 10 s; nginx then sends one back to the frozen relay first, 10 s more, until the front door has active health checks) | 5.3 |
| a relay cut off | 23 | 5.5 |
| the Valkey primary killed | 5.9 | 6.5 (29 with 0.4.40 apps: see below) |
| the PostgreSQL primary killed | 33 (mostly Patroni's 30 s leader key: tunable) | 5.5 |
| a whole machine, holding both primaries, killed / cut off | 40 / 35 | 12 / 7.7 |

After mending, every change made reaches every app within 0.2 s, and a newcomer catching up from the database alone
gets every change. Three fixes came from this: the relay pings every connection every 5 s and nginx gives up on a
WebSocket quiet for 10 s (a frozen relay otherwise kept getting new connections: nginx counted an app giving up
first as the app's failure); database statements give up after 20 s rather than 120 s (a cut-off primary's
connections hang rather than fail: it took two minutes to find the new one). Traced afterwards (`collab` prints a
timeline of each break): sync-only projects took 29 s after a Valkey failover although the bus was back and `resync`
sent at 6.4 s, because each app answered a peer's request for what it lacks at most once per 30 s, and they had last
exchanged moments before. Apps now answer small requests at once (only large ones wait), so 6.5 s; apps older than the
release with this keep the 30 s. And apps on a frozen or cut-off relay noticed only after 20 s: the server's keep-alive
is now 5 s, so 10 s. And a database cut off (its machine cut off: statements hang until COMMAND_TIMEOUT) made every
app with a change waiting take its relay for gone, since a connection's messages are handled in turn and its pings
waited behind the change: they all reconnected at once, nginx timed the reconnections out and took every relay for
down, and the apps backed off to 15 s, long after the database was back (52 s). Now a relay tells an app whose message
waits on the database to `wait` (no app reconnects but those on a broken relay), and apps whose server answered (an HTTP
error, or a 1013 "try again" close) retry at most every 4 s: 35 s, most of it Patroni's own failover.

Found by the lab:
- **nginx's WebSocket read timeout must be short** (60 s, not 1 h). With 1 h, a third of the apps sent to a
  frozen relay never connected (nginx never counted it as failed); with 60 s, one waited a minute and went
  elsewhere. A healthy WebSocket is never idle that long: the server pings every 30 s. The apps also need a
  timeout on connecting.
- **A change sent while the database is unreachable is dropped** by today's relay: the error is logged and
  nothing is answered or passed on, and the app doesn't send it again until it reconnects. Its collaborators
  miss everything it does meanwhile. The design's answer (pass it on live, `q: 0`, as for a refusal, with an
  error the app retries on) is still to be built.
- **Patroni keeps a primary that is cut off from its standbys and clients but still reaches its own machine's
  etcd member** (an early version of the cut): there was no failover. Cutting the whole machine works.

Two smaller points:

- **After a long outage, reconnecting can lag.** **Built:** apps now back off to at most 15 s (it was 30 s),
  and each wait is between half and all of it at random, so that after an outage everyone doesn't come back at
  once.
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
   **Built** (`Relay._up`; checked in `scripts/check_server_abuse.py`, "Resends"). A duplicate is passed on again
   only if it is among the project's last `REPUBLISH_RECENT` (1000) changes, and counts against its sender's
   pace: anyone who hears a project's signed changes can replay them, so old ones would only be a way to make
   the relay repeat them to everyone.
3. **App-level keep-alive** in the web app and the desktop helper. **Built:** `ping`/`pong`/`wait` and `ka`
   in the sync protocol (DESIGN.md), checked by `scripts/check_keepalive.py`.
4. **Connection ids that stay unique** across relays. **Built** (`Relay.join_bus`): a relay's number comes from the
   bus's counter `relays`; after a break in the bus every relay puts the counter back past its own number, since a
   Redis failover can lose its last additions and would otherwise hand a starting relay a number in use (a
   relay starting in the seconds before that can still be given one: rare, and harmless but for that relay's
   connections sharing ids with another's). Checked in `scripts/check_server_abuse.py`, "Ids".
5. **The bus and soft-state interfaces,** with in-process implementations. All passing on of messages goes
   through them, even on one machine. **The contract and both implementations are built** (`server/bus.py`:
   `MemoryBus`, and `RespBus`, the relay's own Redis-protocol client, so that every break is seen), checked by
   `scripts/check_bus.py` against both, the Valkey one in Docker as a user allowed only `bus.COMMANDS`.
   **The relay passes everything on through it** (`Relay.broadcast`, `pass_on`, `_from_bus`): stored changes,
   presence, peer-to-peer messages to everyone or to one connection on another relay (`r:<relay>`), `left`,
   deletions, revocations and moves. Who is connected is on the bus (`c:<project>`), so "peers", the limit per
   project and "is it open anywhere" count every relay; each relay says it is alive every 3 s, and one unheard
   for 10 s is gone: the others tell their own apps its connections have left. Checked with two relays in one
   process (`scripts/check_relays.py`).
6. **Gap tracking in the relay,** filling from the database, the periodic head check, and `resync`/`recheck`
   (with closing for older apps).
   **Gap tracking is built** (`Relay._new`, `_fill`, `_check_heads`): a gap is filled from the database 0.5 s
   after it shows, the heads of every stored project open here are read at once every minute (every second
   while the bus is away), and a break in the bus is followed by a head check and `resync` for sync-only
   projects. A gap that was compacted meanwhile can't be passed on as changes: its apps are asked to `recheck`.
   **What a database failover needs is built** (Decided, below): a relay reads the timeline in every store
   transaction, at every head check (every second after the database was unavailable) and as soon as the store's
   listener is back; a later one means a failover (`Relay._failover`): every connection here is asked to recheck
   at once, tracking starts afresh from the new heads, and a change stored before and not yet acknowledged is
   stored again first. Changes on the bus carry their timeline. Checked with a real standby promoted
   (`scripts/check_relays_failover.py`).
   **The messages are built:** both apps say `rs: 1` in their hello and answer `resync` (their state vector)
   and `recheck` (their hello again); `Relay.ask` sends them, spread over a few seconds, and closes apps that
   don't say `rs` (`scripts/check_resync.py`). What calls `ask`, and gap tracking, come with the bus. The apps'
   side ships in the release that merges this branch (its compat marker names 0.4.67 until then).
7. **The Redis implementations:** relay heartbeats, the connection hashes, the per-relay channels, and
   behaviour while the bus is away (checking heads every second) or the database is away (live-only
   passing on).
8. **A model:** extend `models/` to check that no stored change is lost and that every collaborator ends up
   with every change, across relay deaths, bus breaks and a database failover. The failover model found the
   need for the hello check; this one should look for what the new message order might miss. **Built:**
   `models/relays` (`scripts/check_model.py relays`). It found two holes around a database failover, now
   rules in "Decided" below: store again before acknowledging, and the timeline on the bus.
9. **A rehearsal in Docker:** three relays, PostgreSQL with Patroni and etcd, Redis with Sentinel, an S3
   store, and a front door. Kill each part and measure the times in the table above. Add a load test that spreads one project's people over
   several relays. **Built:** `scripts/loadtest.py spread spread-room p2p --relays 3` (results in "Load", below). It
   found a lone relay pacing a large peer-to-peer message as if it went over the bus too (half speed for a file sent to
   a peer, from this branch), now fixed: a large one goes over the bus only when someone in the project is elsewhere.
10. **Retire Way 2's lease-based failover**, and update SCALING.md, STANDBY.md, INSTALL.md and the help page.
    **Done:** `server/failover.py`, its settings' effect, `--failover-check`, the bucket's conditional writes, the
    admin page's panel, `check_failover.py`, `rehearse_failover.py`, `models/failover` and `Caddyfile.failover` are
    gone. Settings files that still say `failover` are accepted with a warning (a compat marker in `server/cli.py`).

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
- **After a failover, a relay stores again what it stored before it** (found by `models/relays`). A change
  stored but not yet acknowledged when the relay notices a failover is put through the transaction again
  (found, or added) and acknowledged with its new number. Otherwise its acknowledgement can arrive after the
  app's recheck, and the app forgets a change the database lost; by then its number may belong to another
  app's change, so a second recheck doesn't catch it either.
- **Changes on the bus say which timeline numbered them** (found by `models/relays`). A relay doesn't move
  its `upto` for a number from an older timeline (it now belongs to another change), and one that hears of
  a newer timeline this way deals with the failover (recheck, storing again, `upto` from the new head)
  before passing anything on.
- **The timeline is read from the WAL file being written** (`pg_walfile_name(pg_current_wal_lsn())`, its first
  8 hex digits), not `pg_control_checkpoint()`: that one changes only at the next checkpoint, which the lab saw
  lag a promotion by up to 2 s while writes already went to the new primary. Any user may read it.
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

## Rolling it out

What's left between a branch that works and a live service on several machines. The live server's own pieces
(the website, the storage plans service, backups, monitoring) and how to move them are in the private website
repository's runbook, not here.

### Older apps

Every app version a server allows (`--min-version`, 0.4.40 on texloom.org) must keep working when relays become
several. What changes for them, and why it's safe:

- **Connection ids grow** (a relay's number × 2³² + a count), staying JSON integers below 2⁵³: the apps treat
  them as opaque numbers.
- **"resync" and "recheck" are never sent to them** (no `rs` in their hello): their connection is closed instead,
  and they connect again and catch up, as after any drop.
- **"Try again"** (a 1013 close, or 503 before connecting) is a drop to them: they connect again. Before 0.4.67
  they reset their wait on connecting, so while a database is away they retry every second or two rather than
  less and less often: more load, nothing wrong.
- **Before 0.4.67 they have no timeout on connecting**: one sent to a frozen relay waits until nginx gives up
  (60 s), then connects elsewhere.
- **Store, publish, then acknowledge** is invisible to them; so is a change passed on again.

Claimed, not yet shown: **to do**, a lab scenario running the desktop sync engine of the oldest allowed version
(installed from its git tag) next to current apps, on different relays, through the same breaks as `collab`.

### Deploying to machines

`deploy/install.sh` sets up one machine. For several, **to do**: one command that reads a settings file listing the
machines and what each runs, and sets them all up, again whenever the file changes (idempotent):

```toml
# cluster.toml: kept outside the repository (it names machines; secrets come from files beside it)
[ssh]
user = "root"
key = "~/.ssh/texloom"
[machines]
a = { address = "10.0.0.1", runs = ["relay", "postgresql", "etcd", "valkey", "sentinel"] }
b = { address = "10.0.0.2", runs = ["relay", "postgresql", "etcd", "valkey", "sentinel"] }
c = { address = "10.0.0.3", runs = ["relay", "etcd", "sentinel"] }      # a witness, and a relay
door = { address = "10.0.0.9", runs = ["front-door"], name = "app.example.org" }
[bucket]
url = "s3://bucket"
```

It would install and configure each part (Patroni and etcd for PostgreSQL, Valkey and Sentinel, the relays with the
same settings file, nginx with every relay as an upstream: `least_conn`, `proxy_next_upstream … http_503`), the
private network between machines, access lists between the parts, and the firewall.

**Between machines: WireGuard, and a firewall of three rules.** The parts talk unencrypted (the relays to PostgreSQL,
Valkey and each other through nginx; Patroni to etcd), and each has ports of its own (8787, 5432, 8008, 2379/2380, 6379,
26379). With firewall rules alone, each port needs its list of machines allowed, and the traffic crosses the network
in the clear: acceptable on a provider's private network in one datacentre, not between datacentres or providers.
WireGuard gives every machine a private, encrypted address that works the same wherever the machines are; every
part listens only on it, and the firewall is: SSH, HTTP and HTTPS from anywhere; everything that comes through
WireGuard; nothing else. (TLS on each part instead means five certificate set-ups rather than one tunnel; Tailscale is
WireGuard with no configuration, at the price of its coordination service.) The tool makes each machine's key pair
and its list of peers from the settings file.

**One machine stays as simple as it is.** With no `bus` setting a relay uses the in-process bus, and SQLite is still
the default database: nothing of Valkey, Patroni, etcd or a front door is needed, and `install.sh` works as before. The
new code runs there too (heartbeats, pings, `/api/health`, the codes copied into its own database), cheaply, and
most of the checks exercise exactly that path. Before merging: `install.sh` on a fresh machine as well. Commands:
`plan` (what would change), `apply`, `add relay <machine>`, `status` (every part's health). A Python script over SSH,
like `install.sh` and `rebuild-standby.py`, rather than a new tool to learn. It's tested on the Docker lab's
machines first, then on real ones: a few hourly-billed VPSs for an afternoon, in two datacentres, are the
rehearsal that Docker can't be (real networks and clocks, TLS, systemd, the provider's own failures).

### The front door

Whatever decides which relay a new connection reaches. It must not be a single point of failure itself, and it
should send connections only to relays that are well (`/api/health`). In the Docker lab it is one nginx on a machine of
its own: the simplest thing to rehearse with, and not how to deploy (if it fails, nothing is reached). The ways, from
simplest:

1. **nginx on every relay machine, and DNS naming all of them.** Each machine's nginx terminates TLS (the same
   certificate on each, e.g. certbot with a DNS challenge) and sends to **every** relay, not only its own (`least_conn`,
   `proxy_next_upstream error timeout http_503`, and the health watcher below), so DNS only has to reach *a* live nginx
   and nginx does the balancing. Free, and no extra machine. DNS isn't health-aware: a dead machine's address stays until
   it is taken out (by hand, or a script using the DNS provider's API, with a TTL of a minute or so). Apps and browsers
   try the next address when one refuses or is unreachable, so a machine that is off costs little; a frozen one costs a
   connect timeout (15 s) before the next address is tried. Good for two or three machines.
2. **The provider's load balancer** (most offer one, for a few euros a month), in front of the same nginx on each
   machine. It checks health itself (an HTTP check of `/api/health`), balances by connection count, and is kept
   available by the provider: the best value while every machine is at one provider. It can't span providers.
3. **A floating address with keepalived** (VRRP) between two door machines: one holds the address, the other takes it
   when the first stops answering. Within one provider and datacentre only; more to run than (2).
4. **A proxying CDN with load balancing**: spans providers, checks health, and absorbs abuse; usually paid, and it sees
   every request (the content is end-to-end encrypted, so it learns only who connects to which project id, when).

Whatever the outer layer, the nginx in front of the relays needs **active health checks**, which open-source nginx
lacks: it learns a relay is down only by a request failing, and the lab showed it sending an app that was reconnecting
straight back to a frozen relay first (10 s lost). **To do**: a small health watcher beside each nginx (part of the
deploy tool) that polls every relay's `/api/health` every 2 s and marks those that fail twice `down` in nginx's
upstream (a reload: graceful); or HAProxy in nginx's place for this one job (its health checks are built in).

### Health checks and monitoring

Two kinds, and a real deployment needs both. **Routing checks** steer traffic away from a broken part within seconds,
by themselves: the front door's (above); Patroni (through etcd; its REST API's `/primary` and `/health` let anything
find the primary); the Sentinels for Valkey; a relay's own `/api/health` (it refuses new connections while its bus or
database is away). Underneath, systemd restarts any service that stops, and Patroni restarts PostgreSQL. **Monitoring**
tells a person within minutes, because recovery by itself can hide a slow decline: a standby no longer copying, a
Sentinel down (so the next failover would fail), a disk filling, a certificate expiring. What to watch: every relay's
`/api/health`, Patroni's cluster (members, lag), etcd's and the Sentinels' health, disk space and certificates on every
machine, backups (a ping when each finishes), and the site itself from outside; alerting by email when something stays
wrong for a few minutes. **To do**, in the deploy tool: set both up with each machine, and a `status` command showing
every part's health at once.

### Shared state the relays don't have yet

- **Creation codes are files** (`--codes-file`), written by hand or by a sales service, and every relay must see the
  same codes. **Built:** each relay copies its codes files into the database (`codes`: source file, code, size, label,
  until) when they change (looking every 5 s), tells the others (`texloom_codes`), and takes every relay's: its own
  files first, then the others' by name. So a code written on one machine (a sale, `--add-code`) counts on every relay
  within seconds, and the files and the sales service stay as they are. A file with the same path on two machines must
  be the same file. A file no relay uses any more: `texloom-server --forget-codes-file FILE`. Checked in
  `scripts/check_relays.py`.
- **Limits per address** (connections, rates) are counted per relay, so several relays together allow that many
  times more. The front door's limits (nginx's, per address) still hold. Sharing them (counters on the bus) is
  possible if it matters.
- **The admin page** shows one relay's view: **to do**, a summary across relays (the bus knows who is alive and
  connected).

### Growing and shrinking

- **Relays** are interchangeable, so adding one is: a machine, the settings file, `add relay`, and the front door
  told. Taking one away: its connections move to the others as it stops (spread over a minute). **To do** after
  that: doing it by itself, from load (connections per relay, CPU) and from health (a relay unwell for minutes is
  replaced), through the provider's API. Relays first, because they're the tier that grows with people.
- **PostgreSQL** grows by adding standbys (more copies, and reads), and by a bigger machine for writes; past that,
  the steps in "One machine to many". Changed by hand, rarely.
- **Valkey**: one primary carries a lot (measured in "Load", below); its replicas are for failover. If it ever
  needs more, in order: send less through it (the front door keeping a project's people on one relay, below); its
  I/O threads (`io-threads`: the network, not its CPU, is what fills first); several independent Valkey groups,
  each relay choosing one by a hash of the project id (nearly everything on the bus is per project; the relay-wide
  parts, the heartbeat list, each relay's own channel and its numbering, would live on one group); Valkey Cluster's
  sharded pub/sub does the same split, but the relay's own client would have to learn the cluster's redirects.
- **Kubernetes** isn't needed for this: the tiers are few and each has its own way to stay available.

### Load

`scripts/loadtest.py` with `--relays 3`: relays on one machine (a laptop: 10 fast and 4 slow cores) sharing a
throwaway PostgreSQL and a Valkey in Docker, simulated apps speaking the real protocol, each project's people put on
different relays (the worst case: everything they send each other crosses the bus). An edit is "too slow" when the
slowest 5% take over 200 ms to arrive. All on PostgreSQL.

| | one relay | three relays, people spread | three relays, each project on one |
|---|---|---|---|
| edits a second before too slow (3 people a project, 2 typing) | 800 (1,600 fails: CPU 100%) | 2,600 (busiest relay 90%, p95 10 ms) | 2,600 (busiest 86%, p95 7 ms) |
| CPU at 800 edits a second, all relays | 56% | 120% | 102% |
| Valkey at 2,600 edits a second | | 16% of a core, 5.1 MB/s out | 13%, 1.7 MB/s |
| one project, 100 people typing (200 edits/s, 19,800 deliveries/s) | 78%, p95 17 ms | busiest 40%, p95 8 ms | (one relay) |
| large peer-to-peer pieces (4 MiB) between people on different relays, while others edit | 64 MB/s, edits p95 47 ms | 64 MB/s (Valkey sending 171 MB/s), edits p95 102 ms; at 96 MB/s offered edits slow to 563 ms | (none cross) |

Every change reached everyone in every row that wasn't too slow. What it says:

- **Several relays carry more, but not in proportion**: three carried about twice what one did. Each edit costs
  more CPU in all: one relay alone using the bus (Valkey) took 75% at 800 edits a second against 56% without it
  (every message is published, and Valkey sends it back to the relay that published it too), and the rest is three
  relays, PostgreSQL, Valkey and the simulated apps sharing one laptop. On separate machines the second part goes.
- **The relays fill before Valkey does**: at 2,600 edits a second the busiest relay was at 90% and Valkey at 16%.
  Valkey's cost doesn't grow with the number of relays: a project's messages go only to the relays with someone in
  it. What fills first is its network, with large peer-to-peer transfers (files in sync-only projects): at 64 MB/s
  of them it sent 171 MB/s, and other people's edits slowed (head-of-line: one connection per relay carries both).
- **Keeping each project's people on one relay** (nginx `hash $request_uri consistent` for `/ws/`, instead of
  `least_conn`) cut the bus's traffic to a third and edits' p95 from 10 to 7 ms, with little CPU saved. It costs
  balance (a busy project loads one relay) and moves a dead relay's projects as a group. `least_conn` stays the
  default; hashing is the first thing to try if the bus's bandwidth ever matters.
- **A lone relay on macOS** refused some of 1,200 connections opened at once (the listen queue: macOS allows 128);
  Linux allows thousands, so a server doesn't.

## Open questions

- **Whether the bus keeps its contract under load:** a subscriber that falls behind is disconnected by
  Redis (`client-output-buffer-limit pubsub`). That is a loss shown as a broken connection, so it is
  handled, but the limit must be well above what a busy relay takes in. Measure in the rehearsal.
- **How many projects one relay can subscribe to** before subscribing and unsubscribing costs more than it
  should. Rooms come and go as people open and close projects.

This document is also where operating details go (settings, health checks, what to watch) until there is
a runbook and the installation guide covers several relays.
