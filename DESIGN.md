# TeXLoom — design

A local-first, collaborative LaTeX editor. Every participant holds a full copy of
each project; a tiny server relays and stores **encrypted** changes it cannot read.
No accounts: a project is shared by sending a link.

## Goals

- Live collaborative editing in the browser; keep working offline; merge on reconnect.
- Server is a dumb, low-power relay + encrypted store (Raspberry Pi / cheap VPS).
- Compilation happens on users' machines: in the browser (WebAssembly TeX Live) or
  via a local native TeX install (fallback for shell-escape, huge documents, etc.).
- Agents are first-class: point Claude Code (or anything) at a plain folder, or use an
  in-editor assistant backed by any LLM provider, local or remote.
- Version history without git.

## Components

```
 browser editor  ─┐                         ┌─ browser editor (collaborator)
 (CodeMirror+Yjs) │   encrypted, signed     │
                  ├──── updates over WS ────┤
 local helper    ─┘       ┌────────┐        └─ local helper (collaborator)
 (Python, folder  ───────▶│ server │
  sync + latexmk)         │ relay +│  SQLite or PostgreSQL: opaque ciphertext log; files: a folder or S3 bucket
                          │ store  │
                          └────────┘
```

1. **Server** (`server/`, Python + aiohttp + SQLite or PostgreSQL). Relays updates between connected
   peers, appends them to a per-project log (`server/store.py`), stores encrypted blobs (in a
   folder or an S3-compatible bucket, `server/files.py`; the database only records which exist),
   serves the web app. It never sees keys or plaintext. It verifies write signatures so that holders
   of a read-only link cannot write.
2. **Web editor** (`web/`, TypeScript + Vite + CodeMirror 6 + Yjs). Full replica in
   IndexedDB, works offline, live cursors, file tree, PDF view, in-browser compile.
3. **Helper** (`texloom/`, Python + pycrdt; the class is `Daemon`, the desktop app and `texloom run` host it). Mirrors a project to a real directory,
   bidirectionally, so any tool (Claude Code, vim, VS Code) can edit plain files. Also
   a full replica (an always-on peer if you want one), and the native-compile fallback.

## Data model (one Yjs document per project)

| Y root            | content                                                         |
|-------------------|-----------------------------------------------------------------|
| `files`: Y.Map    | path → Y.Text, for text files (.tex, .bib, .sty, …)             |
| `bin`: Y.Map      | path → `{h, size}`: content id of an encrypted blob (images, PDFs used as figures, fonts…) |
| `dirs`: Y.Map     | path → true for explicit folders (folders are otherwise implied by file paths) |
| `meta`: Y.Map     | `name`, `main` (main .tex file), settings                       |
| `out`: Y.Map      | latest compiled output: `{pdf, log, synctex, by, at}` (blob ids; stored as *output*, see below) |
| `versions`: Y.Array | saved versions: `{at, by, label?, auto?, link}` |
| `versionChain`: Y.Array | what versions are built from: `{id, base, snap}`, see History |
| `clients`: Y.Map  | Yjs client id → display name (for "who wrote this")             |
| `review`: Y.Map   | id → Y.Map `{kind: comment\|suggestion, file, start, end, quote, text, by, at, s?, resolved, outcome?, replies: Y.Array, typed?, conflict?}`; `s` signs `"review:" ‖ id ‖ "\n" ‖ by`, and a reply `{by, at, text, s?}` signs `"reply:" ‖ id ‖ "\n" ‖ at ‖ "\n" ‖ by`: the Review tab marks a name ✓ when a personal link's signature vouches for it (with the name the owner granted), and others as typed by anyone with an edit link. What a comment says isn't signed: changing it is an edit like any other. `start`/`end` are encoded Yjs relative positions, so they follow edits; `typed` and `conflict` come from suggest mode (see below). Moving a file makes a new `Y.Text`, so `movePath` re-anchors its items at the same places in the copy and updates `file` |
| `revoked`: Y.Map  | personal key → the owner's revocation (as below), so everyone who syncs the document learns it, whatever the server sends |
| `members`: Y.Map  | personal edit links the owner issued: public key → `{name, at, by, g, revoked?}`, `g` = the owner's grant. Any editor can write this map, so the share dialog only trusts entries whose grant verifies, the owner's own record of issued links (localStorage) and keys seen signing edits; enforcement is by signatures |

Text merges character by character (CRDT). Binary files are last-writer-wins
per path (whole-file replacement), as in Dropbox.

Blobs are **content-addressed**: `id = HMAC-SHA256(key, plaintext)[:16]` (keyed so the
server cannot confirm guesses about the content), encrypted with the project key, and
uploaded once. (The same 32-byte key is the AES-GCM key and this HMAC key. No attack on that
is known, but keys derived for each use would be cleaner; changing it would need versioned
links, since every stored ciphertext and file id depends on it, so it stays as it is.) Compiled PDFs are blobs too, so collaborators can view the latest PDF
without compiling. Only the small `out` note reaches everyone; a browser fetches the PDF (and its SyncTeX) only
while it shows the PDF tab or a popped-out window, and only the newest, and reads the log always (`createOutput`,
`web/src/output.ts`). The desktop app never fetches them.

## Links, keys and access

- Each project has an **Ed25519 keypair** and a 256-bit **AES-GCM key**.
- The project ID *is* the public key (base64url), so the server can verify writes
  without any registration step.
- **Edit link:** `https://host/#p=<id>&k=<aes key>&w=<signing seed>`
- **Read-only link:** `https://host/#p=<id>&k=<aes key>`
- The web app removes the keys from the address bar when it opens a link, leaving `#p=<id>`. The
  tab keeps its link in sessionStorage, so a reload keeps the same access; another tab uses the
  best link in this browser's project list. If the browser won't store it, the address is left as is.
- Keys live after `#`, which browsers never send to the server.
- Every stored update is `sign(seed, "upd:" ‖ ciphertext)`. The server and peers
  reject unsigned writes. Without `w`, the editor is read-only.
- **Personal edit links:** `#p=<id>&k=<aes key>&m=<personal seed>&g=<grant>&n=<name>`, where
  `grant = sign(owner seed, "grant:" ‖ personal public key ‖ ":" ‖ name)`. A personal
  signature is `'M' ‖ personal pub (32) ‖ grant (64) ‖ len(name) ‖ name ‖ sig (64)`, so
  anyone can verify it against the project id alone. A plain 64-byte signature is the
  owner's. Signatures go to the server with every change, so since 0.4.35 a link's grant is of an
  empty name (`n=` empty, `len(name)` 0): the server can't learn who people are. The name is granted
  separately, in the encrypted `members` entry (`g` signs that name), which is where apps show
  it from. Links issued before keep their name, and keep working.
- **Revocation:** the owner sends `sign(owner seed, "revoke:" ‖ personal pub)`. Keys are
  always compared in their canonical base64url spelling (decoders accept variants of the
  same 32 bytes). The relay stores it, refuses that key's updates, snapshots, blobs, mode
  changes and signed peer-to-peer updates, and sends the list in every `log` reply (and
  `rv` frames). Clients keep the list on the device, load it before connecting, and
  ignore peer-to-peer updates until the server's `log` reply is applied; then they refuse
  that key's peer-to-peer updates too. Already-stored updates stay valid (the relay
  accepted them while the key was good), so documents still converge. The owner's app also
  writes the revocation into the document (`revoked`), so a server that leaves it out of `rv`
  can't keep it from anyone who syncs the document, peer to peer included. Updates carry no
  trustworthy time, so the log itself relies on the relay: a server working with a revoked
  person can still store and deliver their new changes. Only moving to new keys shuts them
  out completely.
- **Owner-signed values** use the prefix `"owner:"`, e.g. `meta.movedAt = {at, s}` with
  `s = sign(owner seed, "owner:moved:<at>")`; an unsigned one is ignored.
- Revoked people keep the AES key and can still read. **Moving to new keys** copies the
  document (with history) into a fresh project, re-encrypts every blob (ids are keyed),
  clears `members`, marks the old document `meta.movedAt` (owner-signed), and switches the old project
  to relay-only so the server deletes its copy.
- Attribution: each user picks a display name, stored in their local settings. It is
  shown in cursors and history, and is self-declared, not authenticated.

## Sync protocol (WebSocket `/ws/<projectId>`, JSON frames)

| client → server | meaning |
|---|---|
| `{t:"hello", since, app, v, code?, last?}` | send me everything after sequence number `since` (a connection's first message, within 10 s, or it is closed); a long log comes in pages (below); `code` = a creation code, checked once per connection; `last` = `[q, sha256]`, the newest change (not a snapshot) this copy has from the server, received or acknowledged, with the SHA-256 of its `d` (base64url): the server answers `lost` if its log ends before `q` or holds another change there; `app` (desktop, web) and `v` (its version) are counted for the admin page, and a server with `--min-version` answers an older one (or one without `v`) `{t:"err", denied, code:"update-needed", min}` and closes. Project lists check `X-TeXLoom: app/version` the same way (426). |
| `{t:"up", d, s}` | encrypted Yjs update `d` with signature `s`; stored and broadcast |
| `{t:"snap", upto, d, s}` | compaction: encrypted full state replacing log entries ≤ `upto`; `s` signs `"snap:<upto>:" ‖ d` |
| `{t:"aw", d}` | ephemeral, encrypted awareness (cursors, names); relayed, never stored; past 32 KiB/s a connection's are dropped |
| `{t:"p2p", d, s?, to?}` | encrypted peer-to-peer payload (below); relayed to one peer or all, never stored; `s` must be a valid writer's signature (else dropped), and one without is paced (Per-connection limits, below) |
| `{t:"mode", m, ts, s}` | set storage mode `"server"` or `"relay"`; `s` signs `"mode:<m>:<ts>"`, valid for 10 min; only from the owner |
| `{t:"revoke", p, s}` | owner only: revoke personal key `p`; `s` signs `"revoke:" ‖ p` |
| `{t:"usage"}` | ask for the project's storage figures again (what the reply to `hello` carries), fresh: an app showing how much is used asks when it shows them |
| `{t:"move", c, ts, s}` | owner only: move the stored project to creation code `c`; `s` signs `"move:<c>:<ts>"`, valid for 10 min. The server checks the code is valid and not lapsed, has room for the project and that the project is within the size limit for it, then makes it the project's owner code and recounts both codes. Answered with `moved`, or an `err` whose code is `move-bad-code`, `move-no-room` (with `need`, `free`), `move-too-big` (`need`, `max`), `move-not-stored`, `move-rejected` or `move-busy` (after five wrong codes on one connection). On a private server, someone holding a valid code may connect to a project whose code lapsed, so that it can be moved |
| `{t:"delete", ts, s}` | owner only: delete the project for everyone; `s` signs `"delete:<ts>"`, valid for 10 min. The server erases what it stores, keeps a tombstone (the `deleted` table, which purges never touch; its `projects` row goes) and from then on answers the project's connections with `deleted` and its file requests with 410 |

| server → client | meaning |
|---|---|
| `{t:"log", entries:[{q,snap,d,s}], head, floor, count, mode, you, peers, store, rv, lost?, more?}` | catch-up reply to `hello` (a snapshot, if any, comes first; `snap` marks it, as the signature's prefix differs); `count` = how many entries the server holds; `you` = this connection's peer id; `floor` = entries up to here were evicted; `rv` = revocations; `lost` = this log isn't the one the hello's `last` came from (the app catches up from the start, as after eviction); `more` = the rest follows as `logmore` |
| `{t:"logmore", entries, more?}` | the next page of a catch-up; the last has no `more` |
| `{t:"up", q, d, s}` | someone else's update (`q` 0: not stored, because the project is relay-only or the server refused it; you keep it and retry) |
| `{t:"ack", q, id?}` | your update was stored at sequence `q` (`id` echoes the one sent with it); `q` 0: the project is relay-only, so the update was passed on and nothing is kept |
| `{t:"snapped", upto}` | your `snap` was stored: its sender then deletes the uploads of the entries it replaced (Big changes as uploads, below) |
| `{t:"err", code, m, …}` | a refusal or an error (an update that fails its signature check gets one with just `m`, and no `code`): `update-needed` (with `min`; the connection then closes); `needs-code`, `code-expired` (with `denied`, on a private server); for an `up`, with its `id`: `quota-full`, `project-full`, `server-full`, `code-expired`, `needs-code`; `mode-rejected`, `mode-busy`, `delete-rejected`; and the `move-*` codes above |
| `{t:"aw", d, from}` | someone's awareness |
| `{t:"left", from}` | that connection closed: stop showing the awareness it sent |
| `{t:"p2p", d, s?, from}` | a peer's payload |
| `{t:"mode", m}` | storage mode changed |
| `{t:"moved"}` | your `move` is done |
| `{t:"usage", store, moved: true}` | to everyone connected, after a move: the new figures (`ownerUsed` only to a connection holding the new code); a write refused for the old code (`quota-full`, `code-expired`, `project-full`) is sent again |
| `{t:"rv", p, s}` | a personal key was revoked |
| `{t:"deleted", at, ts, s}` | the owner deleted the project for everyone (at, ms), with the owner's signature `s` over `"delete:<ts>"`, which clients check, so a server can't fake it; the connection then closes. Clients keep their copy read-only and stop connecting (web: `deleted` in the replica's kv store; helper: `state.json`) |

Ciphertext = `iv(12 bytes) ‖ AES-256-GCM(plaintext)`.

**Offline:** each replica keeps an outbox of local updates (merged with
`Y.mergeUpdates`) that the server hasn't acknowledged yet, plus the last sequence
number it has seen. On reconnect it sends `hello`, applies what it missed, then
flushes the outbox. A log over 8 MiB goes a page at a time (about
8 MiB of stored entries each, at least one entry), so a catch-up needs a page's memory on the
server whatever the log's size; the app applies each page as it comes and does what follows a
catch-up (moving `since`, flushing the outbox, compacting) only after the last. Yjs updates are idempotent and commutative, so order and
duplicates don't matter.

**Restarts:** when the server stops it refuses new connections and closes every open one at
once with 1001 ("going away"), waiting up to 5 s for them and then up to 10 s for other requests
(an upload, say), so a restart takes seconds and clients start reconnecting straight away; peers
aren't told each other has `left`, since everyone is going. It listens with a backlog of 2,048, so
the burst of reconnections that follows isn't refused. Clients reconnect with backoff (first after
1–2 s, doubling up to 30 s) and resume from their last sequence number, so nothing is lost.

**Compaction:** when the log grows past N entries, any writer uploads an encrypted
full-state snapshot; the server drops the entries it replaces. (The server can't
compact by itself: it can't read the data.)

**Compressed updates:** an update (a snapshot, a big change, or one passed between peers) may be
*packed*: `\x00TLz` followed by the update compressed with zlib, before it's encrypted
(`texloom/packing.py`, `web/src/packing.ts`; `scripts/packing-vectors.json` checks that both read
the same cases). Data counts as packed only if it also decompresses cleanly, so a plain update
is never taken for one. Every app reads and writes packed updates: snapshots, changes and what a peer
catching up is sent, when 64 KiB or more and packing makes them smaller. Text editing packs to
about a third.

**Peer catch-up (Yjs sync step 1/2, relayed):** after `hello`, every replica broadcasts
`p2p {k:"sv1", sv}`, its state vector. Each writer that receives it replies directly
with `{k:"upd", u}`, the update the newcomer lacks, signed with `"p2p:" ‖ ciphertext`.
Every peer also replies with `{k:"sv2", sv}`, so the newcomer can send back whatever
*they* lack. Read-only peers never send updates, and unsigned updates are ignored.
Anyone with the link can ask, a read-only member too, and a few bytes ask for megabytes, so a
writer answers each connection at most once every 30 s (a request sooner is answered then,
with its latest state vector), and answers one request at a time; the same goes for a file
asked for with `breq`. The desktop answers apart from receiving, so a flood can't stall it.
**Compiled output** (PDF, log, SyncTeX) is uploaded with `X-Kind: output` and replaced by
every compile. `DELETE /blob/<project>/<id>` with `X-Sig = sign("del:<id>")` erases stored
content: any writer may erase *output*; only the owner may erase other content, and
content later uploaded as a project file stops being output. This is storage only:
deleting a file from the project is a document edit any editor makes, and History can
restore it while its content is stored. Clients find replaced outputs in the document's
own history (every past `out.latest`, kept because gc is off), skip anything ever used in
`bin`, and erase the rest locally and (as writers) on the server; the owner's client also
clears outputs stored before they were marked. The helper erases the output it replaces,
asking for output only (`X-Only-Output: 1`).

Blobs move the same way: `{k:"breq", id}` → `{k:"blob", id, b}`. The receiver checks
`HMAC(key, b) == id`, so any peer may serve them, and a file that changes meanwhile is a different
blob with a different id: nothing served is ever half of one thing and half of another (a copy that
is cut off fails the check and counts as not having it; the desktop writes its copies in one step,
`replace_file`). Asked of everyone, every peer that has the file sends it and all but one copy is thrown
away, so a client asks one connection at a time: `{k:"breq", id, one:1}` sent to a peer it has heard
from (`to`), which answers with the file, or `{k:"bno", id}` if it hasn't it. The next peer is asked
at once on a `bno` or a wrong answer, and after 4 s of silence (a peer sending the file in pieces
counts as answering), up to three; then everyone is asked, as before. Apps before 0.4.61 ask everyone
and never say `bno`: asked alone, they answer as always, or stay silent until the timeout (`ASK_ONE`,
`ASK_PEERS`; `web/src/replica.ts`, `texloom/replica.py`; cases in `scripts/replica-vectors.json`).

**Storage modes:**
- `server` (default): the server keeps the encrypted log and blobs, so
  collaborators never need to be online at the same time.
- `relay`: the server deletes the project's data and from then on only forwards;
  `up` is broadcast and acknowledged, but not stored. Peers sync with each other
  using the catch-up handshake, so they must be connected at the same time.

Only the owner chooses the mode, both ways: the server accepts a `mode` command only with
the owner's signature, and the shared project setting `meta.storage` is `{m, at, s}` with
`s` the owner's signature of `"owner:storage:<m>:<at>"`, so an editor can't make everyone
believe the server keeps a copy it doesn't (or stop them uploading). Without a valid one (a
project from before 0.4.35, or one an editor overwrote), apps go by what the server last said it does, kept
for when they're offline; the owner's app signs an old plain value on connecting. Each app keeps the highest `at` it has seen signed (with its mode) and ignores a lower
one, so an editor can't roll the setting back by writing an older value the owner once signed. A replica sends a signed `mode`
command only when its user *changes* the setting, queuing it if offline; it never
"corrects" the server on connect, so a stale replica can't undo someone else's
switch. Switching back to `server` re-uploads a full snapshot and all blobs.

**Private servers** (`--private`). A connection does nothing until its `hello` is accepted (on any
server, one that says nothing is dropped after 10 s). A project is admitted if its owner is a current
creation code. One with no code yet (unregistered, or `default`) is admitted with a valid code, and
bound to it by its first signed write (a stored change or an upload): anyone can make up a project
id, so connecting with a code binds nothing. One whose code has
lapsed is refused until it's renewed, and can't be taken over by another code. A refusal is
`{t:"err", code, denied:true}` followed by a close. Open connections are checked again about once a
minute. File uploads and downloads and new project lists follow the same rule.

**Storage allowances.** The server can't inspect content and project ids are free to
mint, so storage is governed by allowances in a codes file: `<code|default> <0|size|unlimited>
[until=YYYY-MM-DD] [label]`. A line without an allowance is skipped, with a warning in the log.
- **Owner:** the first stored write to a project registers it to the creation code sent
  in `hello` or `X-Create-Code`, or to `default`.
- **Allowance:** an owner's allowance is the sum over all its projects.
- **What's used, shown:** each project's status (`store` in the `log` reply, and on request:
  `{"t": "usage"}` answered with `{"t": "usage", "store": …}`) says what the server keeps of the
  project, its per-project limit and the owner's allowance. `ownerUsed`, all that the owner's code
  stores (its other projects and project lists too), goes only to a connection that sent that
  code: anyone else would learn how much the owner keeps elsewhere. The web app shows the figures
  in Settings ▸ Sync & storage (`usage.ts`), the desktop app under the project's status, each
  asking for fresh ones as it shows them.
- **Expiry:** after its `until` day (UTC) a code counts as removed: its projects fall back to
  `default`, and it no longer registers new projects. `--purge-expired-after DAYS` evicts
  (as idle eviction does) the stored projects of codes expired longer than that; re-uploads
  are then refused until the code is renewed. The owner of a project never changes.
- **Refusals:** `needs-code` (default allowance 0), `quota-full`, `code-expired` (the owner's
  code has expired or was removed, and the default allowance doesn't cover the write),
  `project-full` (`--project-max-bytes`, not applied to unlimited owners) and `server-full`
  (`--max-bytes`, `--min-free`).
- **No lost work:** a refused `up` is still broadcast live but not acked; it stays in the
  sender's outbox and is retried. Clients show a "not saved on server" warning, saying the
  owner can add a code or make the project sync-only. They never switch by themselves: any
  server could send `needs-code`. (A project made without a code on a server that needs one
  is sync-only from the start.)
- **Snapshots** must satisfy `max(last snapshot, floor) < upto ≤ head`. A `snap` frame carries at most
  12 MiB; a bigger snapshot goes up as an upload (Big changes as uploads, below). Snapshots are counted
  against limits by their size change.
- **Replays:** an identical `up` (same ciphertext hash) is acked with its existing sequence
  number and not stored again; hashes of updates a snapshot replaced are kept (`gone`),
  so those can't be stored again either (anyone in a project's room hears its signed updates).
  Only the last 10,000 sequence numbers per project are remembered, each counting 100 bytes
  against the allowance; eviction, purging and going relay-only remember the dropped log's too
  (else a project's whole history could be replayed into it once emptied), and updates of 64 KiB
  or more are remembered for good (`kept`). An older small replay is harmless: updates are
  idempotent, it counts against the allowance only until the next snapshot, and clients compact
  every few hundred entries.
- **Where files are:** the content of stored files is kept outside the database, in a folder
  (`<files>/<project>/<id>`) or an S3-compatible bucket (`server/files.py`); the table `stored`
  records each one's size and kind. An upload is written to the store first and recorded
  after, so a recorded file always has its content; a stop in between leaves at worst an
  unrecorded copy, which the same upload later overwrites. An upload counts against the
  allowances from the moment it's accepted (`Store.pending`), so uploads at once can't
  together pass a limit. Folder reads and writes, and checking an upload's signature, run on
  worker threads, so a large file doesn't hold up live editing.
- **The database:** an SQLite file or PostgreSQL (`server/db.py`), behind the same few calls:
  execute, fetch, and transactions. Both run the same SQL, written for both, and
  `scripts/check_store.py` holds them to the same answers. PostgreSQL goes through asyncpg; SQLite
  runs its calls one at a time on a thread of its own; either way the relay never waits on it.
  SQLite allows one server process per file (a second is refused), PostgreSQL several. A project's
  edits are numbered in the database: `head` is incremented and read back (`UPDATE … RETURNING`)
  in the same transaction as the log row, so numbers stay right however many servers write.
- **Locking:** within one server, `Store.lock(project)` keeps a project's check-then-write steps (is this
  a replay? may it be stored? store it; a snapshot's range check and the snapshot; mode changes,
  deletion, an upload's quota check and reservation) from interleaving with another message for it;
  different projects go on at once.
- **Kept in memory, for speed:** each project's usage (read at the start from the `usage` table, a ledger
  kept in step with every change and recounted daily against the stored data: `Store.reconcile`), owners and
  each owner's total, last-seen times, the deletion and storage mode of the 20,000 projects used most
  recently (the rest are read when needed), and the revocations of the 10,000 most recently used. That's
  right for one server. Servers sharing a PostgreSQL tell each other when a project changes (LISTEN/NOTIFY,
  `Store._told`) and the others read it again, and idle eviction, purges and the recount run on one server at
  a time, under an advisory lock (`upkeep` in `server/app.py`). Several at once would also pass live messages
  between them through a bus and read totals from the database at a quota check (SCALING.md, "Way 3: several
  relays"); new state follows the rules there, and `scripts/check_server_state.py` lists it all.
- **Erasing:** when a project is evicted, purged, made relay-only or deleted for everyone, or a
  file is deleted, its stored files stop counting and can no longer be fetched at once: their
  rows move to the table `trash`, and their content is erased in the background
  (`Store._reap`), carrying on after a restart. One write or erase of a file's content runs at
  a time (`Store._holding`), and an upload of content in the trash takes it out, so an erase
  decided earlier can't remove what that upload wrote. Deleted and relay-only projects' files
  left by a stop are dropped at the next start; an evicted project's leftovers stay and count
  again, as its writers may have uploaded since.
- **Memory:** big work shares one budget, 512 MiB unless set (`memory`, `Budget`): catch-up replies (about 4×
  their stored size, or a page's), large frames being handled (4× their size), uploads being
  checked and stored, and arXiv imports (24 MiB each, two at a time: the download and the zip go
  through temporary files). One address (an IPv6 /64) may hold at most two catch-ups' worth of it,
  and a reader taking a large reply slower than 64 KiB/s is cut off. Work that doesn't fit waits its turn, except a large
  frame, which is answered "server busy" (the client tries again). With frames still arriving
  (128 MiB), replies waiting to go out (128 MiB) and the arXiv cache (32 MiB), this keeps the
  service under its `MemoryMax` (1G in `deploy/install.sh`). WebSocket frames are read whole
  before they can be checked, so they are capped at 16 MiB. Files go over HTTP (≤ 50 MiB, a few uploads read at a time), or peer to peer
  (≤ 50 MiB) in relay-only projects. Clients keep their own frames small: pending updates
  are merged into `up`s of at most 6 MiB, and a `p2p` payload over 4 MiB (a whole project
  for a peer catching up, a file) goes as encrypted `part`s of 4 MiB that the receiver puts
  together again (at most 32 parts, 4 messages at a time), paced to stay under the rate limit.
  Receivers hold at most 128 MiB of unfinished pieces (oldest dropped first), two messages per
  sender, and drop any that has gone quiet for 60 s: pieces come from anyone with a link, even
  a read-only one, so they mustn't be able to exhaust a phone's memory. The server never
  stores or puts together pieces.
- **Big changes as uploads:** a change over 6 MiB (a full copy of a project with a lot of
  text) or a snapshot over 12 MiB goes up as an encrypted upload, like a file (≤ 50 MiB,
  kind `output`), and the log entry is a small reference to it (`\x00texloom-ref\x00` +
  `{"b": id}`, encrypted like any update, so the server can't tell). Readers fetch it over
  HTTP when they apply the entry. If it couldn't be fetched just now (offline, a server error),
  `since` stays below that entry, no snapshot is made, and they reconnect after a while
  (5 s, doubling up to 5 min) to fetch it again. If it's gone (404, or not what the reference
  names: a snapshot replaced it meanwhile), they catch up from the start once, which brings
  the snapshot, and after that skip it with a warning: retrying for ever would let a writer
  who referred to nothing stop everyone's snapshots. The server answers an accepted snapshot
  with `snapped`, and its sender then deletes the uploads of the entries it replaced. In a
  relay-only project a big change goes to the peers online as a `p2p` update instead.
- **Per-connection limits:** frame-size caps per message type, a message/byte rate limit,
  at most 5 `hello`s per 10 s, one creation-code check, a global connection cap, 32
  connections per address (an IPv4 address, or an IPv6 /64, as the reverse proxy's `X-Real-IP`
  says) and 256 per project. Peers that stop reading are disconnected after 20 s rather than
  stalling broadcasts. What's relayed unsigned (`aw`, and `p2p` without `s`) needs only the
  project's id, so it can't get anyone cut off: a recipient already holding 2 MiB unsent, or
  slow to take the last one, is skipped. It is paced as bytes delivered (size × recipients):
  12 MiB/s per connection (32 MiB at once; the apps pace a file's pieces at 96 MiB per 10 s) and
  32 MiB/s per project; past that its sender isn't read from for a while, and small messages are
  dropped rather than waited for. Rows for projects that store nothing (relay mode, revocations)
  come out of a server-wide budget; at most 1000 revocations per project. Ids must be in the one
  base64url spelling clients use (each id has four), so nothing kept by id can be sidestepped.
  Request bodies other than uploads are read whole only up to 260 KiB. nginx adds per-client
  limits (an IPv4 address, or an IPv6 /64) on code checks, uploads and connections.

**Folder hardening.** Every path from the project goes through `safe_rel`
(`texloom/paths.py`) in the helper and `cleanPath` (`web/src/projectfiles.ts`) in the
browser's folder sync and imports; the two apply the same rules. They reject:
- absolute paths, drive letters, backslashes, and `.`, `..` or empty segments;
- control characters and `<>:"|?*`;
- `.texloom`, `.git`, `.vscode`, `.idea` at any depth, compared the way case-insensitive
  file systems see names (NFKC, case-folded: `.GIT` is `.git`);
- Windows aliases: segments ending in a dot or space, and 8.3 short names (`GIT~1`);
- files TeX tools execute or read as configuration (`latexmkrc`, `*.latexmkrc`, `texmf.cnf`).

The resolved path must stay inside the project folder (no writing through symlinks).
- **Blob ids** must be 32 hex characters, and blobs from the server are checked against
  their HMAC id.
- **latexmk** runs with `-norc`, `-no-shell-escape` (unless opted in locally) and
  `openin_any=p`/`openout_any=p`; the main file is passed as `./name`. Paranoid openin
  doesn't keep reads in the project (on TeX Live 2026 it lets `../`, absolute paths and dot
  files through, and Lua's `io.open` reads anything), so `texloom/confine.py` adds a sandbox
  where there is one (`sandbox-exec` on macOS, `bwrap` on Linux; home folders and volumes
  hidden but for the project, TeX's folders, fonts and fontconfig; the project's `.texloom`
  hidden but for the build folder) and, everywhere, a check: latexmk's `$pdflatex`,
  `$bibtex`, … go through a Perl hook that appends each run's `.fls`/`.blg` to
  `<name>.fls-reads`/`.blg-reads` (TeX rewrites its `.fls` every pass, so a document could
  read a file on the first pass only). A run's part of the record must be whole (both PWD
  lines, then only INPUT/OUTPUT, no NULs, no OUTPUT naming a record: a document can
  `\openout` its own `.fls`); xdvipdfmx/dvips record nothing, so the `.xdv`/`.dvi` is parsed
  and every name in its specials and fonts that is a file outside fails the check too. On
  macOS the sandbox also denies writes but for the project, the build folder, TeX's caches
  and the temporary folders. A compile that read outside those places publishes
  only a note, and its build folder is deleted. The local setting `"confine": false` turns both off.
  BibTeX runs from the
  document's folder (`-bibfudge-`, so `\bibliography{../refs}` is found) with `openout_any=a`
  for it alone, since paranoid mode refuses its .bbl/.blg in `.texloom/build` (a dot folder)
  and it writes no other files.
- **No shell for names a document gives:** latexmk runs some programs through the shell with file
  names in double quotes, where `` ` `` and `$( )` still run commands, and the names come from
  collaborators. So the main document, and those it takes labels from, must have names of letters,
  digits, spaces and `. _ - + / ( )` (`tex_names_refused`, or nothing is compiled); makeindex,
  xdvipdfmx, dvips, dvipdf and ps2pdf run as argument lists (`texloom_exec` in the Perl hook); and
  latexmk's `kpsewhich`, which it asks about the names in `\bibliography{…}` and the like through
  the shell, isn't asked about a name containing `` ` `` `$` `"` `\` or a control character.
- **Lines that run programs** (`texloom/scan.py`, `programRequests` in `texscan.ts`):
  `\write18`, `\ShellEscape`, `\input|`, Lua `os.execute`/`io.popen`, packages such as
  minted or pythontex, TikZ externalisation ("shell"), and arara directives or editor
  `% !TeX program` comments ("tools"). "Shell" lines are looked for in every file, as TeX
  reads a file of any name: texts, and binary files' bytes read as Latin-1 ("tools" only in
  TeX's own file types). With full shell escape, a compile uses
  `-shell-escape` only if every "shell" line is approved in `.texloom/allowed.json`
  (keyed by file and line text, set by `texloom allow`), else `-shell-restricted` with a
  note in the published log. Lines a remote change writes into a synced folder are
  reported once (helper log, browser notice). Plainly written forms only: TeX macros can
  build any command, so this is a tripwire, not a guarantee.
- **Malformed frames** are logged and dropped.

**Manual sync (per device):** the replica doesn't keep a connection open. Local edits
build up in the outbox; **Sync now** connects, runs the same catch-up, flushes the
outbox, waits briefly for peers' replies and disconnects. `texloom sync <dir>` is the
folder equivalent.

**NAT / firewalls:** all connections are outbound HTTPS/WebSocket from participants to
the server, so NAT needs no traversal. Only the server must be reachable (a public
host, a port forward, or a tunnel). Browsers also require HTTPS for WebCrypto. If
direct browser-to-browser WebRTC is added later, it will need STUN, and TURN (or this
relay) as the fallback for symmetric NATs.

## Folder sync (the helper)

- `texloom new <dir>`: turn an existing folder into a project and print both links.
- `texloom clone <link> <dir>`: check out a project into a folder.
- `texloom run <dir>`: keep the folder and the project in sync, live.

This is the main Claude Code integration: `cd ~/papers/x && claude "fix the
related-work section"`. Edits appear live in collaborators' browsers.

### How changes move between the folder and the project

- Disk → CRDT: on a file change, diff the old text against the new and apply the
  minimal inserts/deletes. Concurrent edits from browsers therefore merge rather
  than being overwritten.
- CRDT → disk: write the merged file. If the file changed on disk since the last
  sync, ingest that change first. A file that can't be written is tried again after 0.5 s,
  doubling up to 5 s (local edits are taken in first, as before any remote change): on Windows a
  file another program has open (an antivirus scan, an indexer, an editor) can't be replaced.
- Reading a changed file: only once it has stopped changing. A read halfway through a save
  (some editors empty a file, then write it), or through copying a big file in, would share a
  cut-off file, and for text its missing end as deleted. Changed files are looked at, then again
  0.2 s later (1 s for a file of 1 MB or more, whose copying can pause for a moment; 2 s for one
  that became empty or vanished: rarely meant), and any still changing wait for their next
  change, or a look shortly after. Each time a file is found still changing, the wait doubles,
  up to 2 s (`settle_for`), since on a slow or busy disk copying in a big file can pause for longer
  than 0.2 s. Their date and size are noted before
  reading (and after writing, from the file written before it goes into place), so a save that
  lands in between is never taken for what was read and missed. An empty file is never taken
  for an older copy reappearing, whose repair would write over a save in progress. The browser's
  folder sync (foldersync.ts), which polls every 2 s, does the same: a change is read once a later
  look finds the same date and size (the same waits, `settleFor`), a file gone from the
  folder is deleted only after 2 s, a file changing while read (NotReadableError) is looked at
  again rather than counting towards stopping the sync, and after writing a file is read back:
  if an editor saved meanwhile, it is read before anything is written there again.
- Line endings: the project's texts break lines with "\n" only. A file saved on Windows ends
  lines with "\r\n"; it comes in as "\n" and is written back with "\r\n" (`c` in known.json,
  judged by the file's first line).
  The browser's folder sync does the same. The editor counts only "\n" as a line break
  (`EditorState.lineSeparator`; by default CodeMirror also takes "\r\n" as one), so its
  positions match the shared text's. Any "\r" in a project text (pasted, say; a lone one too,
  rather than taken for an old Mac line break) is taken out, each by itself, by the web app as it
  arrives and by the folder sync when it starts (`keepLF`, `strip_cr`).
- Whole characters: a character outside the BMP (an emoji, Unicode maths such as 𝒜) is two
  UTF-16 units in the web app. An edit between them leaves half a character: Yjs turns both
  halves into "\ufffd" for everyone, and the desktop's copy (whole characters only) differs.
  Typing never does this, but position-based edits could (restoring from History, importing over a
  file: 😀 → 😁 or 𝒜 → 𝒞 share their first half). `setText` keeps common ends by whole character,
  and an editor filter (`wholechars.ts`) widens any other change that would cut one.
  `npm run check-props` checks both.
- Renames on disk: a rename reaches the sync as a deletion and an addition, perhaps in
  different batches. A file gone from disk whose last synced content (by hash) is now in a
  file the project doesn't have yet has moved there, and moves as renaming in the web app
  does (`movePath`): the text is copied and comments and suggestions are anchored at the same
  spots in the copy; the main-file setting follows. With several candidates, one with the
  same file name is preferred (a renamed folder). The browser's folder sync does the same.
- Ignores build products (`*.aux`, `*.log`, `*.synctex.gz`, the main PDF, …) and
  `.texloom/`, `.git/`.
- Blobs referenced by remote updates are fetched in the background. In relay mode
  they arrive from a peer over the same connection, so fetching inline would deadlock.

### Compiling from a folder

- `--compile`: run `latexmk` after changes (debounced) and publish PDF + log to `out`.
  It also answers `p2p {k:"creq", n, at}` compile requests from browsers set to "compile
  with my own TeX" (acknowledging with `{k:"cack", by, n}`). Requests are signed like `upd`
  (only a writer can ask) and carry when they were made (`at`, ms) and a random `n`: a helper
  takes one made in the last five minutes (and at most a minute ahead of its clock) whose `n`
  it hasn't seen, so a recorded request can't be replayed later; one without a recent `at`
  is ignored. The answers (`cack`, `cerr`) are signed too and carry the request's `n`:
  the browser takes only a writer's answer to one of its own recent requests, so a read-only
  member can't fake one, nor the server replay one.

### Settings that stay on this computer

- Local options (`compile`, `engine`, `latexmk_args`, `shell_escape` and the lines
  allowed to use it, `confine`, `git`, `label`) live in the user's own settings folder
  (`config.settings_path`: `folder-settings/<hash of the folder's path>.json`, which also
  notes the project id), never in the folder: anyone sharing the folder through Dropbox or
  the like can write any file in it, `.texloom/computers/<id>/config.json` included. A
  moved folder, or a link changed to another project, starts over with the defaults. The first run of
  0.4.56 brought each listed folder's old settings over, only `compile`, `engine` and `label` from a
  folder that may be shared (`config.looks_shared`).
  `.texloom/computers/<id>/config.json` keeps the link, creation code and name.

### A folder also shared through a file-sync service

`texloom/sharedfolder.py`. A synced folder in Dropbox (iCloud Drive, OneDrive, Google Drive,
Syncthing…) that another computer also runs TeXLoom on gets each change twice: through TeXLoom,
and as the service copies the file. Mostly the second copy matches and is dropped, but whatever the
service brings while a computer's TeXLoom isn't running (or is offline) would be read in as that
computer's own edit when it starts: the same text twice (Yjs keeps both insertions). The
service would also copy each computer's records to the others. So:

- **Records per computer:** `paths.state_dir(folder)` = `.texloom/computers/<computer id>/`
  (the id: `computer-id` in the app's settings folder, random). Everything but the folder's
  `folder-id`, the `lock` and `.gitignore` lives there: the replica, `known.json`, the merge bases,
  `config.json` (the link), the git bridge's lease id, build output. A folder from before 0.4.40 has them in `.texloom` itself; `state_dir` moves them
  (config.json last, so an interrupted move finishes next time). FolderSync keeps its files in `replica.dir`.
- **Finding each other:** `.texloom/folder-id` (random, made by the first computer; the service
  copies it). Each computer sends `{"k": "fold", "f": folder id, "m": computer id, "h": holds the
  turn, "by": name}` to the project's peers every ANNOUNCE (20 s): peer-to-peer through the relay,
  encrypted, not stored, so the project's history doesn't grow. Hearing another computer with the
  same folder id marks the folder shared (`shared.json`, forgotten after 30 days of not hearing).
- **Taking turns:** a computer that holds the turn keeps it (two at once: the lower computer id
  keeps it); with none holding, the lowest id heard from in the last EXPIRE (70 s) takes it. A
  shared folder is synced only while online and caught up, after ANNOUNCE + 5 s of hearing the
  others, and never into a copy of the project that has no files yet.
- **Standing by** (`FolderSync.standby`): nothing is read in or written out. Files on disk that
  already hold the project's version (as the service brought them) are noted as synced, with
  their text as the merge base (`_track`). Taking over (`resume`) then reads in only what
  changed on this computer since, as at any start.
- **Saves from another computer's older copy:** a shared folder's text file merges from
  `fork_point`: the recent version (the last six merge bases) closest to the save, by lines. A
  service that kept one of two simultaneous saves (and set the other aside) then loses neither
  edit: merging from the latest base would take the save as undoing what its older copy lacked.
- **Conflicted copies** (`CONFLICT_COPY`: Dropbox's and Nextcloud's `… (… conflicted copy …)`,
  Syncthing's `….sync-conflict-<date>-<time>…`) are not project files, in the app and in the
  browser's folder sync; the app points each out once.
- **Adding** the service's copy on another computer (`cli.adopt`, the app's Add folder): no
  records of its own there yet, so it takes the project link from another computer's
  `config.json` and starts out marked shared, standing by until its copy has caught up.

`scripts/check_shared_folder.py` runs two computers on one folder with a stand-in service between
them (and a third as a plain collaborator); with `TEXLOOM_NO_TURNS=1` it shows the duplication.

## Compilation

1. **In browser:** [texlyre-busytex] (TeX Live 2026 compiled to WebAssembly, AGPL):
   pdfLaTeX, XeLaTeX and LuaLaTeX, plus bibtex8, biber and makeindex. Runs in a Web
   Worker. The engine is about 32 MB plus the 93 MB basic package set, preloaded and cached
   in IndexedDB per site (downloaded once per browser). **Everything else in TeX Live comes
   on demand** (`server/texlive.py`): the engine asks for any file it lacks as
   `GET /texlive/<year>/<kpathsea format>/<name>` (200 = the file, 404 = a miss it
   remembers). The server keeps TeX Live's package index (`texlive.tlpdb`, ~187,000 run
   files) in SQLite, maps the request to a path the way kpathsea would (per-format folders
   and suffixes), and the first time a file is wanted downloads its package from a mirror,
   checks it against the index's SHA-512 as it streams to disk, and unpacks only that
   package's run files under `/var/lib/texloom/texlive`. The cache stays under `--texlive-max`:
   to make room, the packages used least recently go first (last use is recorded hourly). A
   package that couldn't be fetched isn't tried again for 10 minutes; one address (an IPv6 /64)
   may have at most 400 packages or 1 GiB fetched an hour, and everyone together the cache's size
   an hour, so the cache can't be made to thrash; and nginx limits each
   address: past 400 requests in quick succession (a first compile asks for many, one at a
   time) it slows them to 20 a second rather than refusing them, as the engine takes only a
   404 as a miss. Files are served with a one-year immutable cache (the URL names the
   release), and the service worker keeps them for offline use. The year must be the engine's: its formats are built from that release, so
   the index comes from that year's repository (the live tlnet while current, the
   historic `tlnet-final` afterwards). **Mirrors:** one is pinned when the index is built;
   if it fails or holds another version of a package (checksum mismatch), the CTAN
   redirector is tried (three times, a different mirror each), then any `--texlive-mirror`;
   if all differ, the index is refreshed from another mirror (at most every 10 minutes).
   A request waits up to 25 s for a first install (the engine gives up after 30); a
   bigger package answers 503 and is there for the next compile. Servers without
   `--texlive` use the recommended (200 MB) and extra (340 MB) bundles instead, fetched when a
   document needs them.
2. **Native:** a helper with `--compile` (or the desktop app, per folder) runs `latexmk`.
   Full TeX Live: shell-escape, minted, etc. It finds TeX the way a terminal would
   (`use_tex_on_path`: /etc/paths.d on macOS, TeX Live's and MiKTeX's usual folders), since
   programs started from the Finder, Dock, Start menu or at login don't get a terminal's PATH.
   A compile starts once changes stop: 1.5 s after a change in the folder, 4 s after one from
   elsewhere (those arrive keystroke by keystroke), at once for a browser's request (`creq`,
   answered `cack`, or `cerr` with the reason when the compile can't run). A running compile
   is never stopped; one more follows it if anything changed meanwhile. Compiles can't keep
   setting each other off: the PDF written into the folder and the working files in
   `.texloom/build` are never synced, a published PDF isn't a file change, and a file that
   changes in the folder during or just after a compile counts as generated (sources such as
   .tex and .bib never do): a chain of more than three compiles started that way stops, and
   changes from elsewhere to generated files only don't start one.

**Documents that take labels from each other** (`\externaldocument` from xr, xr-hyper or
zref-xr; xcite's `\externalcitedocument`) need the other document's .aux, which only its own
compile writes. Before the main document, a compile builds each document it names
(`web/src/xr.ts`, `texloom/xr.py`, checked against the same cases in
`scripts/xr-vectors.json`), farthest first, and gives each compile the .aux files it reads.
Two documents citing each other (a paper and its supplement) form a cycle, cut where it
closes: the document that would close it isn't compiled again. In the browser its .aux is
simply missing there (labels of the other document are unaffected), and each .aux is kept in
memory until a file other than the main one changes. Natively each one is compiled in
`.texloom/build/xr/<path>/`, and the document closing a cycle gets the main document's .aux
from its last compile, so repeated compiles settle. The MCP `compile` builds them in place.

Either way the PDF goes into the `out` map, so other peers just view it.

**A submission `.zip`** (arXiv, journals; `web/src/submission.ts`, checked by
`web/scripts/check-submission.mjs`) has to build where it's sent, so it errs towards too much:
every file named in the argument of any command in the main file, and in the files it reaches
(.tex, .sty, .cls, …), goes in, with or without its extension, from the main file's folder or a
`\graphicspath` folder. A file named but unused costs a few bytes; one missing fails the build.
The `.bbl` comes from a compile in the browser, which hands it back with the PDF; it replaces the
`.bib`. Removing comments is per line: a comment-only line goes, a comment after code leaves a bare
`%` (so the line end keeps its meaning), and `%` in verbatim environments, `\verb` and URLs isn't a
comment. It's in the web app only: the desktop app syncs folders and isn't an export tool.

### Moving to a new TeX Live release

The in-browser engine's formats are built from one release, so everything follows it. It keeps
working after the next release comes out: the server then uses that year's `tlnet-final` archive.
To move, once `texlyre-busytex` publishes a build of the new release:

1. Update `texlyre-busytex` in `web/package.json`, and add its asset checksum to `deploy/install.sh`.
2. Set `YEAR` in `server/texlive.py`. On the next start, the server drops the old release's cached
   packages and index, and `--texlive-init` builds the new index. Browsers discard the old engine
   when the new service worker activates, because its version changed.
3. Update "TeX Live 2026" in `web/src/settings.ts` (and its translations), `README.md` and this file.

## In-editor assistant

A chat panel (`web/src/assistant/`) that acts on the project like another collaborator.

- **Adapters** (`llm.ts`), both streaming:
  - **Anthropic:** the official `@anthropic-ai/sdk`, used from the browser
    (`dangerouslyAllowBrowser`; the user's own key). Defaults to `claude-opus-5-5` with
    adaptive thinking shown as summaries and an explicit `output_config.effort`. On
    models that support it, `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`)
    reruns a classifier-declined request on the recommended fallback model. Tools use
    `eager_input_streaming`, and top-level `cache_control` caches the growing prefix
    across the tool loop.
  - **OpenAI-compatible:** `/v1/chat/completions` with `tools`, parsing the streamed
    `tool_calls` and `reasoning`/`reasoning_content` deltas. Covers OpenAI, OpenRouter,
    LM Studio, Ollama, vLLM, llama.cpp and LiteLLM.
- **Conversation:** each session keeps the provider-native message list and only appends
  to it, so Claude's thinking blocks (with signatures) are echoed back unchanged. A
  refused or truncated turn is never appended. After a mid-output fallback, blocks
  before the last `fallback` marker that the next model can't continue from are
  omitted. Switching provider or model starts a new conversation.
- **Tools** (`tools.ts`): `list_files`, `read_file` (with line numbers), `search`,
  `edit_file` (exact, unique match → one small Y.Text delete + insert), `write_file`
  (common prefix/suffix trimmed), `delete_file`, `compile`, `get_compile_log` (errors
  and warnings extracted), and for the Review tab `suggest_edit`, `add_comment`, `list_comments`,
  `reply_comment` and `resolve_comment`. Read-only links get only the non-writing tools. MCP servers
  the person adds (⚙; `mcpclient.ts`, Streamable HTTP) are called straight from the browser, never
  through the TeXLoom server, and their results are untrusted input.
  - Every input is validated with zod before running. Streamed and local-model inputs
    can be truncated or malformed; failures go back to the model as error results.
  - `edit_file` finds its target again *after* the user approves, because collaborators
    may have typed in the meantime.
- **Review and undo:** with "Ask before applying edits", each change waits as a diff card
  (Apply / Reject); so do a comment, a reply and resolving one, which everyone sees (the
  request may come from something the assistant read, such as a comment). Assistant transactions use their own Yjs origin, tracked by a
  `Y.UndoManager`; each request is one undo step ("Undo these changes"). Its edits
  sync to collaborators like any local edit.
- **Compaction** (`compactBefore`, `makeRoom` and `summarise` in `panel.ts`; the session methods in `llm.ts`).
  The room is the model's context (⚙, else Anthropic's Models API, LM Studio's loaded context,
  Ollama's `/api/ps` or `num_ctx` (4,096 by default), OpenRouter's model list, else a guess), at most 200,000
  tokens. Use is the provider's own count for the last request (`usage`; OpenAI-compatible servers are asked
  with `stream_options.include_usage`, dropped for a server that refuses it) plus characters / 3 for what's
  been added since, or characters / 3 for everything.
  - Before a message, past 70% of the room: the history is turned into a plain-text transcript (tool results
    cut to 1,500 characters; the middle left out if the transcript itself wouldn't fit), the model is asked
    for a summary without tools, and the history before the last exchange (kept word for word if small) is
    replaced by it, prefixed to the first kept user message as `<summary>`.
  - Between the steps of one request, past 90%: older tool results of this request are cut to 2,000
    characters, then the history before it is summarised. A request rejected as too long (matched on the
    error text) gets the same treatment and one retry.
  - Any edit to the history drops every Claude thinking block, since a block is valid only after the exact
    history it came from (removing all of them is allowed). The summary is shown, and saved, as its own entry
    in the conversation; what's saved for continuing is the compacted history.
- **The person's instructions** (⚙, `texloom.ai.instructions`, up to 4,000 characters) are appended to the system
  prompt in an `<instructions_from_the_person>` block, below the assistant's rules, which win if they conflict.
  The system prompt is fixed for a conversation (saving settings starts a new one), so it still caches. A saved
  conversation records a fingerprint of the prompt it ran with; continuing it under a different one (new
  instructions, or a new version) drops Claude's thinking blocks, which are valid only with the prompt they were
  made under. **What the assistant is sent…** shows the prompt, the tools and the next context block.
- **Context:** the system prompt is static, so it caches. Each user message starts with
  a `<context>` block: project, main file, open file and current selection.
  The person can add files (**+ Files**) and passages (**+ Selection**) for it to consider as well. To spare
  the model's context, a file is only named, with a request to read it the first time a conversation sees it,
  and a passage is quoted once and then named; summarising takes over if the reading fills the room.
- **Keys** live in `localStorage` only; they never enter the Yjs document or pass
  through the texloom server.

## Suggest mode

Typing that becomes suggestions instead of changes (`web/src/suggestmode.ts`, `suggestcore.ts`).
A suggestion changes nothing until accepted, so while suggesting the editor isn't bound to the
shared `Y.Text` at all. It shows this tab's view: the shared text with the tab's own open typed
suggestions applied. Each of those is a hunk, a range `[a, b)` of the shared text and its
replacement, stored as an ordinary `review` entry (`quote` = the shared text there, `text` = the
replacement) with `typed` set to this browser's id for the project (`texloom.suggester.<project>`, different in each project so a browser can't be followed between them), so every other reader (the Review tab,
accept and reject, the assistant, the MCP tools) needs nothing new.

- **Typing:** each change in the editor, in view positions, merges with the hunks it touches into
  one hunk; it's trimmed to what actually differs from the shared text and grown to whole words (an
  insertion takes the word beside it), so a suggestion reads "is → is very". A hunk that matches
  the shared text again goes, which is how undo withdraws a suggestion. Hunks never touch. Changes
  are saved 0.8 s after typing stops, when the tab is hidden, and before anything reads or settles
  suggestions (accepting one, the assistant).
- **Others' edits:** hunks keep Yjs relative positions in memory. After any transaction from
  elsewhere that touches the file or the review map, hunks move with their anchors and the editor
  gets the difference, word by word, so the cursor and undo history stay put. A change by someone
  else to text a hunk replaces is carried into the hunk (`rebase`) when it only touches words the
  hunk keeps; when it touches the words being changed, the hunk keeps its text and is marked
  `conflict`, so accepting it asks first. A hunk someone accepted, rejected or deleted leaves the
  view, except anything typed into it since it was last saved, which becomes a new suggestion.
  In memory, an end of a hunk that is a word also takes in text typed right against it, so
  "is → is very" becomes "isn't → isn't very" when someone types "n't"; text that starts with a
  space or punctuation is put back outside. (Stored anchors don't do this, so others' views keep
  such text beside the suggestion.)
- **In the editor**, suggestions are by default not marked at all (they're in the Review tab);
  Settings can underline them or show them in full, where a word replaced by a similar one shows
  only the letters that differ (`refine` in `worddiff.ts`). A pure insertion anchors both ends to the character
  before it, so text others type at that spot comes after; other hunks hold their first and last
  replaced characters, so text typed at either edge falls outside.
- **Whose:** `typed` is public, so anyone could write a suggestion with someone else's; a browser
  takes back as its own only suggestions it recorded making (`texloom.madehere.<project>`). A tab
  suggesting in a file holds a lock on it in localStorage (renewed every 5 s, stale after 15 s);
  another tab of the same browser then uses its own page's id there, so two tabs never edit the
  same suggestions. Collaborators' cursors are mapped into the view, and this tab's
  is published on the shared text in the usual `cursor` awareness field.
- **Positions** outside the editor (comments on a selection, search results, compile errors, the
  assistant's context) are in the shared file; the editor maps them to and from its view.
- **Display:** every suggestion is drawn as a word diff (`worddiff.ts`: Myers over word tokens,
  with spaces or a single punctuation mark between two changes joined into one), in the editor
  and in the Review card. Accepting applies only the differing words, so text in common keeps its
  authors and others' edits next to it survive.
- `npm run check-suggest` drives the bookkeeping with thousands of random edits from the user
  and from a collaborator, checking that the view always equals the shared text with the hunks
  applied, that saving and loading round-trips, and that accepting everything gives what was typed.

## Paragraphs changed apart

Yjs merges concurrent edits character by character, so two people rewriting the same sentence
while apart can end up with their words interleaved. Nothing is lost, but it can read oddly, so
such paragraphs get a note in Review (`web/src/merges.ts`, `texloom/merges.py`, same rules):

- **Edits made apart.** While a copy's changes can't reach anyone (offline; connected but not
  yet caught up; or sync-only with nobody else online, web only), each place it edits is
  remembered: the web app as Yjs relative positions, the desktop as UTF-8 byte ranges moved
  along with every change (pycrdt's deltas are exact; its sticky indices aren't, see Known
  limitations). They are kept across restarts (localStorage; `.texloom/apart.json`, with the
  text's hash). The desktop counts edits made while it wasn't running as long apart.
- **Merged into.** When changes from others then land in a paragraph (text between blank lines;
  only the lines changed if it is over 3000 characters) holding one of those places, and that
  edit is at least 30 s old (brief reconnections don't count), the paragraph is noted.
- **The note.** After changes have stopped arriving for 1.5 s: a review item of kind `comment`
  with `merge: [names]` (the others whose changes came in, from `clients`), `by` the noticing
  copy's name, and the paragraph as `quote`; one per paragraph, not if one is already open
  there. Shown as **⚠ Check merge**, highlighted in the editor, resolved like a comment.
  Only the copy that was apart notices; the one that stayed online sees the note arrive. The
  desktop's notes have no anchors (pycrdt): the web app anchors any item that has none where
  its quote occurs exactly once.
- **Forgetting.** Once everything has been exchanged (caught up, nothing waiting to be sent)
  and nothing has arrived for 10 s, the remembered places are dropped.

## History

The Yjs document keeps tombstones of deleted text, so a version needs only a
`Y.snapshot(doc)`: a state vector and everything deleted so far. "View at version", "diff
two versions" and "restore file" are computed client-side. Named versions are just
labelled snapshots.

A whole snapshot per version made documents grow with the square of their history, because
the document never forgets anything stored in it, even after a version is removed. So
versions are a chain (`web/src/versions.ts`): each link in `versionChain` holds only the
deletions and clocks new since the link it builds on (`base`), and a version names its link.
Simultaneous saves just fork the chain. Before writing a link, the saver rebuilds it and
compares with the real state; if they differ, or the chain is damaged, the link holds the
whole snapshot (`base: null`). Removing a version only unlists it, since later links build
on it. Every 64th link holds a whole snapshot anyway, so rebuilding a version never walks
more than 64 links, and each tab remembers the snapshot at the tip of the chain, so saving
a version rebuilds nothing it has already rebuilt. `npm run check-versions` in `web/` tests
all this.

Automatic versions (`autoSave`, `web/src/history.ts`) are saved after two minutes without local
edits and when the tab is hidden, but at most one per project every 15 minutes, whoever is
editing: a version anyone saves after your edits covers them. Older ones thin out (`thinned`: all
from the last day, the newest per hour for a week, per day before that, at most 300), and the
History list folds them by day between named versions. Edits themselves are stored as they are
made (IndexedDB, and the server or people online), so versions are only bookmarks.

## Agents (MCP)

`texloom mcp` keeps its own replica (state under `~/.cache/texloom/mcp/<project>`) and
speaks JSON-RPC 2.0 over stdio. It publishes y-protocols awareness frames (encrypted like
everything else) with its name and a cursor built from pycrdt sticky indices (UTF-16
offsets, matching Yjs), so it shows up like a person. Comments and suggestions it makes
use the same `review` entries as the web app; their anchors are encoded relative
positions, which pycrdt produces in the Yjs wire format.

## The desktop app

`texloom app` (PySide6, optional extra `texloom[app]`; PyInstaller builds from
`packaging/texloom-app.spec`, made by `.github/workflows/app.yml`) runs the same `Daemon`
as `texloom start` on a background thread with its own asyncio loop; the Qt window on the
main thread only reads `Daemon.states` and hands work (join, share, restart) to that loop.
- **Folder list:** `texloom/registry.py`, one JSON file in the user config directory,
  shared with the CLI. A per-folder lock (`.texloom/lock`) keeps two processes off one folder.
- **One copy per user:** a local socket; a second launch hands over its arguments and exits.
- **texloom://join?link=<encoded link>** opens the join dialog with the link filled in
  (registered by the macOS bundle, in HKCU on Windows, by a .desktop file on Linux). It
  stays on the machine, and nothing is joined without confirming in the app. A page can't tell
  whether the app is installed, so the web app always says what to do if nothing happens.
- **Following each system:** one window built from `QAction`s shared by the toolbar, the menu
  bar, the folder's **More** button and the right-click menu. Icons are `QIcon.ThemeIcon`s, which
  Qt draws with SF Symbols on macOS, Fluent icons on Windows and the icon theme on Linux (none
  there means text only). On macOS: the toolbar joins the title bar, dialogs that belong to the
  window (`Dialog`) open as sheets, Settings and About move into the app menu through their
  menu roles (every other action is `NoRole`, so Qt doesn't guess from its text), Settings is
  its own window, and closing the window hides it. While no window is open the app switches to
  the accessory activation policy (`mac_dock`), so it leaves the Dock and ⌘-Tab and lives in the
  menu bar; opening any window brings the Dock icon back. Windows and
  Linux get a Settings button in the toolbar; closing hides the window in the tray, saying so the
  first time, and on a desktop with no tray asks whether to minimise instead of quitting.

## Project lists

An optional, encrypted copy of a person's list of projects (`server/lists.py`, `texloom/listsync.py`,
`web/src/listsync.ts`; the two clients must match). The server stores opaque blobs and can't tie
one to the projects it names.

### The list, its API and its document

- A list has a random 32-byte secret K, carried in a sync link (`/#sync=<K>`).
  `seed = HMAC-SHA256(K, "texloom list sign")` gives an Ed25519 key pair whose public key is the
  list's id; `aes = HMAC-SHA256(K, "texloom list encrypt")` is the AES-GCM key. The blob is
  `iv | ciphertext`.
- `GET /api/list/<id>` returns the blob and `X-Seq`. `PUT` carries `X-Seq` (must be the current
  value + 1, otherwise 409 and the client merges and retries) and `X-Sig`, a signature over
  `list:<id>:<seq>:` + blob. `DELETE` is signed over `listdel:<id>`. A new list needs what a new
  project needs (`X-Create-Code`) and belongs to that code, so purging the code removes it. Its
  size counts against that code's allowance and the server's limits like a project's; a write
  that would pass them is refused with 507 `list-full` (at most 256 KiB a list in any case).
- Document: `{"v":1,"projects":{"<id>":{"f":fragment,"n":name,"at":ms,"w":bool,"s"?,"nick"?,"t"?,"by"?,"c"?:ms,"m"?:ms}},"gone":{"<id>":ms}}`.
  `s` is the project's server (an origin) when it isn't the one keeping the list.
  `c` is when the project joined the list and `m` when a device last saw its files change (the
  server can't tell, and at most every few minutes, because each write syncs the list).
  Merging keeps the latest `gone` time per project and the most recently opened entry, never
  replaces an edit link with a read-only one, keeps the earliest `c` and latest `m`, and drops a
  project removed after it was last opened.
  Removal records are forgotten after a year.
### Padding, and other ways in

- Every entry's JSON is padded with spaces (which JSON ignores) to a multiple of 512 bytes before
  it is encrypted, so the small entries below look like a small list, tags or settings.
- Other ways in are entries of the same kind holding `{"v":1,"wrap":<K>}`, opened by their own
  secret. A password's secret is `scrypt(password, "texloom list password v1:" + username, N=2^17,
  r=8, p=1)`, with the password NFKC-normalised and the username lower-cased and NFKC-normalised
  after stripping one fixed set of space characters from its ends (tab, LF, VT, FF, CR, U+001C–1F,
  U+0085, the Unicode spaces and U+FEFF: the same in both apps, as JavaScript's `trim()` and
  Python's `strip()` differ). A new password must be at least 12 characters, with at least 6
  different ones, and score at least 60 bits on a small estimate both apps share
  (`passwordStrength`/`password_strength`, held by the shared vectors): each character counts
  log2 of the size of the character classes used, except repeats (`aaaa`, `abab`) and runs
  (`abcd`, `4321`), and a common password, keyboard run or part of the username (matched after
  undoing `0→o`, `@→a` and the like) counts 8 bits in all. The dialogs offer a generated
  passphrase (four made-up words of three syllables, about 75 bits). Existing passwords still
  open the list. A passkey's secret comes from WebAuthn PRF with the salt `texloom project list
  v1`. An entry opened by `HMAC-SHA256(K, "texloom list slots")` records each way in with its
  signing seed (not its encryption key), so whoever holds K can remove them but not read them, and
  `h`, the SHA-256 of the way in's stored (encrypted) entry: the same encrypted entry is written to
  every copy.
### Copies on several servers

- A list can be kept on several servers (`web/src/listsync.ts`, `texloom/listsync.py`).
  Its settings carry `copy:<origin>` (on or off, with when), each device remembers the set beside
  its key, and every entry is synced on every copy: each is read and merged into what's known so
  far, written back where that adds something, and the copies written before others added more are
  written again. Everything in a list merges in any order (entries, tags, settings, and the record of
  ways in, which keeps removed ones as `gone`), so a copy that was offline just catches up; one that
  can't be reached is skipped, but one is needed. Settings are synced first, since they say where
  the copies are. A copy that appears through the settings, rather than being chosen on this
  device, is marked to ask: it is neither read nor written until it is ticked in the list's
  window (whoever had the sync link could have added one). Password entries are copied to every
  copy, as they are (encrypted, and signed with the seed the record of ways in keeps), only when
  their hash matches the record's `h` (so a copy can't have something else copied in its place),
  and not if any copy says the entry is retired (410: a copy answering 404 just missed the
  removal); passkeys belong to their server's address, so they stay where they were made. A list retired on any copy is retired: replacing or deleting it retires
  it on every copy. Dropping a copy leaves what's there, with its settings marking it dropped. The
  servers allow `X-Seq` across sites, so a page can sync copies on other servers. Every list entry
  names its project's server (`s`), and every creation code its server (`code:<origin>`), because a list can be kept on
  several servers.
### Retiring and replacing a list

- `DELETE` retires an entry: its contents go, but its id answers 410 until it is purged, so a
  device with a replaced (perhaps leaked) link can't start the list again under that link. A retired
  id is kept a year and counted: an owner with 256 (100,000 in all on a server where anyone may make
  lists) can make no new list until old ones go. Replacing
  a link retires every way in, because anyone holding the old link can read what it records, and
  keeps the new list only on the server its link points to and the other copies the person ticks
  then (none by default: whoever had the old link could have added one); the dialog says to
  revoke the integration tokens the list carried. The desktop app can't use a passkey, so it opens
  `/#unlock-for-app=<nonce>` (a fresh random nonce), and the page, which says the list goes to the
  program that opens `texloom://` links, returns the sync link through
  `texloom://sync?link=…&n=<nonce>` (at once if the browser already has the list). The app uses it
  without asking only if it carries the nonce of the request it made in the last ten minutes, once,
  and points to the server it sent the person to; any other (**Use in the TeXLoom app** in the web
  dialog sends it unasked, and so could any web page) is shown with a warning and used only once
  confirmed (No is the default), unless it is the list the app already has.
### A sync link that arrives in the browser

- A sync link that arrives in the web app's address (`/#sync=<K>`, from a link in any page or
  email, or the desktop app's **Add a passkey…**, which opens its saved link) is never used by
  itself: it opens the list dialog with the link filled in and a warning that it came from a link,
  that every project in the browser and the settings below would go on that list for whoever made
  it to read, and to use only your own; Cancel is focused and **Use this list** is the only way on.
### Settings that follow the list

- Settings that follow the list are a further entry opened by `HMAC-SHA256(K, "texloom list
  settings")`: `{"v":1,"s":{<name>:{"v":value,"at":ms}}}`, with the creation codes
  (`code:<origin>`), the servers keeping copies (`copy:<origin>`), the name shown to collaborators
  (`name`), and the web app's integration accounts as JSON, with their tokens: Zotero (`zotero`,
  its API key), GitHub (`github`), GitLab, Gitea and Forgejo (`git`) and Dropbox (`dropbox`, its
  refresh token). The desktop app reads and writes the codes, copies and name, and keeps the others
  as they are. The latest `at` wins (a tie goes to the larger canonical form, the same on every
  device; checked by the shared vectors); an `at` more than ten minutes ahead of the merging
  device's clock counts as now + ten minutes, so a setting dated far ahead can't outlast every
  later change. So a code entered in the web app reaches the desktop app's New project dialog and
  the other way round. The web app records when its code was entered (`texloom.codeAt`); the desktop
  app keeps one code per server in `creation-codes.json` (mode 600) in its config folder. A code
  entered before dates were kept counts as the oldest. Anyone with the sync link can read the codes
  and tokens, as they can every project on the list.

### Tags

- Tags are a person's own and never enter a project. They live in `texloom.tags` in the browser and,
  with a synced list, in a further entry opened by `HMAC-SHA256(K, "texloom list tags")`, separate
  from the list so that clients which don't know about tags (the merge drops unknown top-level
  fields) can't lose them. Document: `{"v":1,"tags":{"<id>":{"n":name,"at":ms,"gone"?:true,"c"?:colour}},"on":{"<project>":{"<id>":ms}}}`;
  a negative time in `on` means the tag was taken off at that moment. `c` is a colour someone chose;
  without it (or if it's unknown here) the colour comes from a hash of the id, so it is the same
  everywhere and costs nothing to store, which is why `TAG_COLOURS` (in `listcore.ts` and `listsync.py`, checked by the shared vectors) must keep its order and length. Each tag, and each tag on each
  project, takes its latest change, so a rename on one device and an assignment on another both
  survive; on a tie, deleting and assigning win. Tags whose names match when folded become the one
  with the smallest id, with every project either had. `tidyTags` drops deletions over a year old,
  marks for deleted tags and projects no longer on the list. Creating the entry needs a creation
  code, like any list entry, so a device with one creates it at once for devices without. The two
  clients (`listcore.ts` and `tags.ts`; `listsync.py` and `tags.py`) are checked against the same
  test data. A folder on a server other than the list's keeps its tags on that computer only.

## References and Zotero

`web/src/references.ts` adds an entry from a DOI (or an arXiv id, as its DataCite DOI
10.48550/arXiv.<id>) through doi.org's BibTeX, with a key like `vaswani2017attention`. Searching by
title and authors asks Crossref (published work) and DataCite limited to arXiv, sorted by
relevance (Crossref doesn't hold arXiv's DOIs), from the browser; both allow it, and only the
words typed are sent.

`web/src/zotero.ts` keeps a `.bib` file in step with a Zotero collection through api.zotero.org,
also from the browser. Like any integration it has two levels. The **account** (an API key, the
user id and name) is the person's: in `localStorage`, and in the project list's settings entry
(`zotero`, beside the `code:<origin>` entries, the latest one set winning) so it follows them; never in a project. The
**link** is the project's: `meta.zotero = {<file>: {lib, libName, coll, collName, format, by, at}}`,
so collaborators see the file is generated and by whom; anyone whose own key reads that library can
refresh it. A refresh exports the collection's top-level items as BibTeX or BibLaTeX (100 per
request, using `Total-Results`) and applies the difference to the file (`applyText`), directly
even in suggest mode, as the file is generated. Keys are Zotero's own.

## Dropbox

`web/src/dropbox.ts` keeps a project in step with a Dropbox folder from the browser, as the git
integration does with a repository, but file by file: Dropbox has no commits.
- **Signing in:** OAuth with PKCE, in the same tab (Dropbox's login can stall in a pop-up; no
  secret anywhere: the server's `dropbox_app_key`, from /api/info, names the app). The verifier
  waits in the tab's sessionStorage; Dropbox sends the tab back to `/dropbox-auth.html`, which
  swaps the code for tokens, keeps the account and returns to the project. The account (`texloom.dropbox`: refresh token, short-lived access token) is
  the person's, and follows the project list as the setting `dropbox`.
- **The link** (`meta.dropbox = {folder, path, name, by, at, blocked, clash}`) names the folder by
  its id, `.texloom/folder-id` in it, made if missing: the same id the app's folder sync uses
  (sharedfolder.py). Each browser approves it with the folder's path in its own Dropbox
  (`texloom.dropboxApproved.<project>`, with the account), checked against that id: a shared
  folder sits at a different path in each member's Dropbox.
- **The base:** the shared map `dropboxBase` (path -> {rev, h, size}): each file's Dropbox
  revision and content hash as last synced. Only changed entries are rewritten, so the project's
  history grows with the changes, not the folder's size.
- **Bringing in** (when Dropbox's long poll, `notify.dropboxapi.com`, says the folder changed, and
  every minute): the listing, in full the first time and after that only what changed since
  (Dropbox's cursor, kept per account and folder); each file whose content hash differs from its
  base is taken (unchanged here), merged three ways (base: that revision, downloaded by `rev:`),
  or clashes, as for git; `blocked` holds the clashing files' revisions. Revisions Dropbox no
  longer keeps (30 days and more) mean no base: a clash.
- **Sending** (after 10 s quiet, on leaving the tab, or Sync now; four files at a time): each file
  whose hash differs from its base is uploaded in "update" mode with its
  base revision (or "add" for a new one), which Dropbox refuses if the file changed since; a
  deletion names its base revision. A refusal means bringing in first. Each success updates its
  base entry at once, so a send cut short is finished by the next.
- **One at a time:** the lease `dropboxLease`, as `githubLease`. And browsers stand by while the
  TeXLoom app on someone's computer keeps the same folder (its `fold` messages, with that folder
  id and "holding", heard in the last 70 s; `Replica.folders`), Dropbox carrying it to that
  computer: syncing from both would bring each change in twice. A browser listens 25 s before
  first syncing a link.
- Dropbox's conflicted copies, `.texloom` and build output are never project files.

The folder is checked to be the linked one every 10 minutes, not every round. With
`texloom.debug` = 1 in localStorage, each sync's time and requests go to the console.

`scripts/check_web_dropbox.py` drives it against `scripts/fakedropbox.py`.

## Git repositories (GitHub, GitLab, Gitea, Forgejo)

`web/src/github.ts` keeps a project in step with a git repository from the browser, through
the host's own API (`web/src/forges.ts`: GitHub, GitLab, and Gitea or Forgejo, such as Codeberg;
`github.ts` and `githubui.ts` are this generic integration despite their names, and `meta.github` and
`githubLease` its stored names). A browser can't speak git's own protocol to
these hosts (they don't allow it from other sites), but their APIs do allow it (CORS; a
self-hosted Gitea or Forgejo only once its admin turns that on). Each host is an adapter with the
same few calls: whose token, a branch's commit, a commit's files with their git blob ids, a blob,
start a branch, and commit on top of a known parent. Bitbucket's API gives no blob ids, so it
isn't one; the app's git bridge works with any host.

As for Zotero, the **account** (a token and its login, one per host) is the person's
(`localStorage`; GitHub's under `texloom.github` and the list's settings entry `github`, others
under `texloom.gitAccounts` and the entry `git`); the **link** is the project's:
`meta.github = {kind?, host?, repo, branch, dir, head, by, at, blocked, clash}` (no `kind`:
GitHub), where `head` is the commit last brought in or sent, the base for the next three-way
merge, whoever syncs. A token is only ever sent to its own host: a link to a host this browser
has no account for isn't synced from here.

Any collaborator can change the link, and syncing uses the browser's own token, so a browser syncs
only a link its user made or approved there (`texloom.githubApproved.<project>` in `localStorage`:
host, repository, branch and folder; one without a host is GitHub). A link someone else set shows
"Approve and sync" instead; until then nothing is fetched from or sent to the host with this
browser's token.

- **Bringing in** (every minute): for each kept file the repository changed between `head` and
  the branch's commit: if the project has its version already, nothing; if the project still
  has `head`'s version, the repository's; if both changed a text file, `merge3Strict` (diff.ts: null
  where both changed the same lines differently); anything else clashes. One clash and nothing
  comes in: `blocked` and `clash` record the commit and files until the branch moves on, or
  someone keeps the project's version (`merge3`, ours where they clash). The decisions are
  made first (fetching what they need), then applied in one change, after checking that
  neither `head` nor the files changed meanwhile.
- **Sending** (after 2 minutes quiet, or Sync now): the files whose git blob id (SHA-1 of
  `blob <size>\0…`) differs from `head`'s tree go up as one commit whose parent is `head`.
  GitHub: blobs, one tree (`base_tree` = `head`'s, deletions as `sha: null`), the commit, and a
  fast-forward ref update, which fails if the branch moved (then bring that in first); an empty
  repository takes no git-API writes, so its first file goes through the contents API. GitLab
  (one commit of file actions) and Gitea/Forgejo (one change-files call, each update or deletion
  naming the blob it replaces) commit on top of the branch as it is: TeXLoom checks the branch is
  still at `head` just before. If someone pushes in that moment, the commit lands on top of theirs
  (its parent isn't `head`): `head` then stays, and the next bringing-in finds the project's files
  already there and brings in the rest. A branch that doesn't exist yet is started from the
  default branch when linking.
- **Kept files:** inside `dir`, names `cleanPath` accepts, not build output, at most 50 MB
  (GitLab doesn't list sizes, so its files are checked once fetched).
  Repository files TeXLoom wouldn't keep are left alone (sends build on `base_tree`).
- **One at a time:** two copies merging the same commit at once would insert its text twice
  (Yjs keeps both insertions). `meta.githubLease = {id, by, n}` names the copy that syncs; it
  rewrites it (n+1) every 30 s, and another copy takes it only after seeing no change for
  100 s by its own clock (so clocks needn't agree), and acts 10 s after claiming, once any
  rival claim has met it (the map keeps one). Ids are per tab (sessionStorage, so a reload
  resumes) and, for the TeXLoom app, per folder (`.texloom/git-device`). A closing tab gives
  the lease up if it can.
- **The app's git bridge** (`texloom/gitsync.py`, `--git`) takes part when its folder's
  upstream is the linked repository and branch (and `dir` is empty; its remote address, https or
  ssh, names the link's host and repository): it brings in, commits and
  pushes only while holding the lease, and records the commit it matches as `head`. While it
  waits, collaborators' changes (including those a browser brought in from the host) reach its
  folder through TeXLoom as usual; when its turn comes, git merges the identical change once.
  A synced folder that is a plain clone of the linked repository, without `--git`, gets a
  warning: pulling there by hand while a browser syncs could bring changes in twice.

## One person, projects on different servers

A project lives on one server; a person's projects needn't. (This is about where a project lives, not
how a server scales: see [SCALING.md](SCALING.md).) The web app belongs to the server it's
loaded from (its "home": the list, settings, creation code and new projects are there), and a
project from elsewhere syncs with its own server straight from the page (`web/src/servers.ts`):

- Inside the app a project from another server is opened as `#s=<origin>&p=…&k=…`; pasting another
  server's link (`https://other/#p=…`) turns into that. Share still gives the project's own links,
  on its own server. The list keeps the server per project (`s`), and so does the desktop app,
  whose folders always had their own servers. A link can't change the server of a project already
  in the list (anyone with its read link could make one naming their own server): it asks, and a
  different server is used for that tab only; nor can it replace the list's keys, except with more
  access to the same project.
- A server must be `https` (`http` only on this computer, for testing, or when the app itself is
  on `http`), and nothing but encrypted data goes to it: the same as for the home server. The relay
  already answers any origin (CORS `*`), and the page's policy allows `https:`/`wss:` connections.
- Creation codes are per server, each sent only to its own server. The list's settings carry a
  `code:<origin>` entry for each server; the desktop app's
  `creation-codes.json` maps onto them, so a code entered anywhere reaches every device.
- A new project (or copy) goes on a server you choose when you know more than one: the home server,
  the servers of projects on your list, those you hold a code for (and in the desktop app, its
  folders' servers), defaulting to the last one used (`texloom.lastServer`; `last-server.json`).
  The web app makes it there directly, with that server's code. Moving to new keys makes a new
  project, so it's done from the project's own server's web app.

## Copying a project

`web/src/copyproject.ts` makes a new project (new keys, the copier as owner) from the current state
of another: files, explicit folders, and the `main`, `bib`, `engine` and `lang` settings. History,
saved versions, comments, members and compiled output are left behind, and binary files are read
and stored again because each project encrypts with its own key. The copy is filled while
disconnected, so it reaches the server only when opened, with the storage chosen for it. Anyone who
can read a project can copy it. A copy of a project with no server copy has none either. The desktop
app copies a folder's project files (not `.texloom`, tools' folders, LaTeX's working files or
symlinks) into a new folder and shares that as a new project with the same settings.

## Two replicas, one behaviour

The core is written twice: `texloom/*.py` for the desktop app and `web/src/*.ts` for the browser.
What each must do the same is held by cases both run, written once:

- **The replica** (`scripts/replica-vectors.json`, from `scripts/make_replica_vectors.py`; run by
  `scripts/check_replica.py` and `web/scripts/check-replica.mjs`): a fresh replica, opened with the
  owner's, a personal or a read-only link, gets the server's messages and must end as the case
  says: catching up (pages, out of order, the server having lost its copy, a relayed project),
  which entries count (signatures and their prefixes, snapshots, compressed, undecryptable,
  malformed, uploads fetched, gone or not there yet), revocations, deletion, refusals and storage
  problems, peers' changes (signed, revoked, in pieces, before catching up) and answers to a peer,
  and snapshots of a long log. Also what goes out: edits made here (sent once caught up, several
  as one, kept until acknowledged, sent again after reconnecting, kept when refused) and files
  (uploaded signed and marked as file or output, kept when refused, none for a relayed project or
  a read-only link; fetched and checked against their id, asked of peers, given to peers). The
  messages are real (encrypted and signed); the expected results are written by hand from the
  protocol, so the two apps are held to it, not to each other. Each replica runs against a
  stand-in connection and server, so nothing touches the network.
- **The folder syncs' rules** (`scripts/folder-rules.json`, from `scripts/make_folder_rules.py`;
  run by `scripts/check_folder_rules.py` and `web/scripts/check-folder-rules.mjs`): which files
  in a folder are the project's (working files, editors' droppings, conflicted copies, tools' and
  dot folders, the compiled PDF, refused paths), which are text (UTF-8, no NUL near the start,
  never a PDF or image), merging a file saved while the project changed, how long a changed file
  settles, Windows line endings, and which new file a gone one was renamed to. Each rule is one
  named function on each side (`synced_file`/`syncedPath`, `pick_moves`/`pickMoves`…), the one
  the sync itself uses.
- **The folder syncs' behaviour**: `scripts/folder-cases.json` (below), run by both model checks;
  a failing random run of either prints its steps in that form, so a failure found in one app is
  replayed in the other.
- **The project list** (`scripts/listsync-vectors.json`), **compression** (`packing-vectors.json`)
  and **cross-references** (`xr-vectors.json`), and text handling through `check_props` on both
  sides (below).

Not covered by shared cases: what only one app does (presence is the web app's; a folder synced
by several computers, and compiling for others, are the desktop's). New logic goes on one
side where it can: the desktop app only syncs, so what is computed (an export, a merge shown to
the user) lives in the web app and reaches the desktop as data.

## Guarding text handling

Text goes wrong at boundaries, where two parts count positions differently. Three kinds of check
guard them.

- **Positions are typed.** In the desktop app a Python string index, a UTF-8 byte offset (pycrdt's
  edits and change events) and a UTF-16 offset (Yjs, the editor, anchors) are distinct types
  (`texloom/textpos.py`: Char, Byte, U16), every edit of a project text by position goes through
  that module, and `scripts/check_textpos.py` checks both (mypy on the modules that handle
  positions; nothing else editing texts by position). In the web app, a position in the shared text
  and one in what the editor shows (they differ while suggesting) are distinct types
  (`web/src/positions.ts`: SharedPos, ViewPos), so the compiler rejects using one for the other.
- **Properties, with random texts.** `web/scripts/check-props.mjs` (fast-check) and
  `scripts/check_props.py` (Hypothesis) try hundreds of random texts and edits, including
  Windows line endings, lone "\r", emoji and other line-like characters, against what must always
  hold: the editor (with exactly its text settings, `editortext.ts`) and the shared text stay
  identical with y-codemirror's copying mirrored both ways; edits never leave half a character;
  applying a diff gives exactly its target; merges follow their laws; line endings round-trip; a
  remembered range follows its text. The desktop's merge uses the web app's line diff (Myers) and
  lines split at "\n" only, and the two are compared on the same random cases.
- **Invariants, in test builds.** With `texloom.testHooks` set on localhost (web) or
  `TEXLOOM_CHECKS=1` (desktop), the apps check themselves as they run: the editor shows exactly
  the shared text once things settle; shared texts have no "\r" and no half characters; a file
  the folder sync takes as in step holds the project's text with its own line endings. A broken
  one is recorded (`window.__texloomBroken`, "INVARIANT BROKEN" in the log), and the end-to-end
  check fails on it, naming the first thing that went wrong.

- **The folder sync, step by step.** `scripts/check_folder_model.py` (Hypothesis) and
  `web/scripts/check-folder-model.mjs` (fast-check) run the real folder sync, on two computers with
  a collaborator, through random sequences of what can happen: an editor saving, typing into the
  project, changes delivered between any two, sync rounds and time passing, writes refused, an
  older copy coming back, the app quitting and the file saved meanwhile. The file is in parts, each
  written by one person, so what each part must hold is known exactly: after every step, each
  editor's part on disk is what it saved and typed parts are kept; at the end everything agrees.
  Timers, and in the browser the clock, folder and storage, belong to the test, so a failure
  replays exactly, shrunk to the shortest sequence. The rule behind most of what they found:
  *base* is what the file holds (set only once a write succeeds, kept across restarts), and a file
  changed since the sync last saw it is read before anything is written over it.

- **A library of the failures found.** `scripts/folder-cases.json` holds every folder-sync failure
  found so far (by the models, the end-to-end check or a report) as its exact steps, with what it
  caught; both models run every case on every check before their random runs, so each stays
  exercised. A new failure gets a case.

## Errors deliberately ignored

Some errors are expected and harmless: browser storage blocked (private browsing, site settings)
or full, a folder or file already gone, a page draw cancelled by a newer one, a peer that has
left. Showing them would only alarm; dropping them silently would hide the day one of them is a
real failure. So none is dropped without a trace:

- **Web app:** `quiet(why, e)` (`web/src/quiet.ts`) instead of an empty `catch`. The last 100 are
  kept, each with its reason; with `texloom.debug` = 1 in localStorage each also goes to the
  console as it happens, and `window.texloom.quiet()` lists them. The commonest reason,
  `STORAGE`, is storage blocked or full: what was being kept is then kept for this visit only.
  The Dropbox and git sync rounds show their own errors as their status; anything that escapes
  one is a bug, and goes to the console as an error.
- **Desktop app and server:** a handler for any error (`except Exception`, `BaseException`, or a
  bare `except`) logs it, raises, or passes the error on (shown, returned). The ones that can be
  ignored log at debug level: in the desktop app's log only with `TEXLOOM_DEBUG=1` set, with the
  command line's `-v`. Handlers for a named error (`except OSError`) are taken as deliberate.

`scripts/check_quiet.py` (in CI) refuses an empty `catch`, a no-op `.catch()`, and a broad
`except` that leaves no trace.

## Code kept for older apps, servers and data

Every place that handles something older carries a marker just above it, saying what must happen
before it can go (issue 27):

```
# compat(refused 0.4.30): apps too old to say their version ("unknown" on the admin page): remove _hold and HOLD_UNVERSIONED
// compat(data 0.4.35): the plain, unsigned storage value; converted on read: its owner signs it
```

- **app X**: apps older than X. Apps reach the server, and each other, only through a server, so
  this goes once every server's `--min-version` is X or later.
- **refused X**: apps older than X that servers turn away, handled so that they don't keep
  retrying. Raising the minimum version doesn't end this: they are already refused. It goes once
  they stop connecting. The admin page shows when each app version last connected.
- **server X**: servers older than X. This goes once every server people use runs X or later.
- **data X**: something written before X that is still stored somewhere. Raising the minimum
  version doesn't make it go away, so it is **converted when it is read**, where that's possible,
  and the marker says how, or why it can't be.

`scripts/check_compat.py` lists them (`--min-version` and `--servers` say which app and server
ones could go), and CI checks every marker is well formed and that each data marker says
whether it is converted. When a server's minimum version moves up, remove a batch.

**Converting data when it's read.** Who can convert something depends on where it is kept:

- *In a project* (the shared document, and the server's log of it). The server can't convert
  it: everything in the log is encrypted, so it can't even see what form a value has. An app
  can. When an app that may write opens the project and finds the old form, it writes the new
  form as an ordinary edit. That edit reaches everyone else and the server's log; the next
  snapshot then replaces the old entries. Some conversions need a particular key: only the owner
  can sign the storage setting, for example. Until such a person opens the project, it keeps the
  old form.
- *On the server, readable by it* (its database, the TeX Live index). The server converts it when
  it starts. Once every server has started on that version, the reading code can go.
- *On a computer or in a browser* (a folder's `.texloom` records, browser storage). The app
  converts it the next time it reads it.
- *In a project list*. The list is encrypted too, so the server can't convert it. An app can,
  the next time it saves the list.
- *In people's hands* (links sent by email). These can't be converted. The owner can make new
  links, or move the project to new keys.

A project nobody opens is never converted, so code that reads data can't go just because time
has passed. Removing it is a judgement: has the conversion been out long enough, and what happens
to a project that is still in the old form? Prefer turning the old reading code into a harmless
fallback (a default value, a field ignored) over removing it, so such a project still opens.
The server could help with this judgement by noting the newest app version that has written to
each stored project. It knows that version from the hello, without seeing anything inside. That
isn't built yet.

## Known limitations / open questions

- Deleting a project for everyone stops it on its server only. Anyone with a copy can still read
  it, and could share it as a new project; it can't take back what people already have.
- Two peers who both *create* (or upload) the same path while apart: Yjs keeps one value
  per map key and deletes the other everywhere. `web/src/clashes.ts` finds the loser (a
  deleted value for the key that no later write saw: no item has it as `origin`) and
  writes it back as `name (conflict).ext`, with a banner. Only a browser that already held
  the losing version restores it, since only it knows exactly what that version said; same
  content on both sides is not a clash. The folder helper (pycrdt) can't see item origins,
  so a version created there and lost while no browser held it is not restored.
- Long divergent offline edits of the same paragraph merge character-wise and may
  interleave. They are flagged for review (see *Paragraphs changed apart*), not prevented.
- pycrdt (0.14) places sticky indices wrongly in non-ASCII text made of several pieces (it
  mixes UTF-8 and UTF-16 offsets), so the Python side reads anchors only in ASCII text and
  makes them only in a freshly created text (`texloom/anchors.py`). Comments and suggestions
  it adds to non-ASCII text carry just their quote, and the web app anchors them there.
- Self-declared display names. Personal links tie edits to a key the owner named, but
  revoking stops writing, not reading, without re-keying.
- Idle eviction (`--evict-idle-days`) makes the server copy a cache: sequence numbers
  keep counting, and a writer whose `since` is at or below the reported `floor`
  re-uploads its whole state and files. Link
  holders with no copy can't open an evicted project until a writer returns.
- A server restored from a backup numbers changes again from where the backup ends, so a
  number an app saw before may now name another change. Each app sends the newest change it
  has (`last`: number and content hash); when the server's log ends before it or holds another
  change there, it says `lost`, and the app catches up from the start and re-uploads, as after
  eviction (`scripts/check_restore.py`). It can't tell once that change has been compacted away
  and forgotten (`GONE_KEEP` changes later), nor for apps from before 0.4.52, which don't send it.
- Browser storage can be evicted: request persistent storage; the server copy and
  the helpers' copies are the backup.

[texlyre-busytex]: https://github.com/TeXlyre/texlyre-busytex
