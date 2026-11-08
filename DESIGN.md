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
 local daemon    ─┘       ┌────────┐        └─ local daemon (collaborator)
 (Python, folder  ───────▶│ server │
  sync + latexmk)         │ relay +│  SQLite: opaque ciphertext log + blobs
                          │ store  │
                          └────────┘
```

1. **Server** (`server/`, Python + aiohttp + SQLite). Relays updates between connected
   peers, appends them to a per-project log, stores encrypted blobs, serves the web
   app. It never sees keys or plaintext. It verifies write signatures so that holders
   of a read-only link cannot write.
2. **Web editor** (`web/`, TypeScript + Vite + CodeMirror 6 + Yjs). Full replica in
   IndexedDB, works offline, live cursors, file tree, PDF view, in-browser compile.
3. **Daemon** (`texloom/`, Python + pycrdt). Mirrors a project to a real directory,
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
| `versions`: Y.Array | saved versions: `{at, by, label?, auto?, snap}` (encoded `Y.snapshot`) |
| `clients`: Y.Map  | Yjs client id → display name (for "who wrote this")             |
| `review`: Y.Map   | id → Y.Map `{kind: comment\|suggestion, file, start, end, quote, text, by, at, resolved, outcome?, replies: Y.Array}`; `start`/`end` are encoded Yjs relative positions, so they follow edits |
| `members`: Y.Map  | personal edit links the owner issued: public key → `{name, at, by, g, revoked?}`, `g` = the owner's grant. Any editor can write this map, so the share dialog only trusts entries whose grant verifies, the owner's own record of issued links (localStorage) and keys seen signing edits; enforcement is by signatures |

Text merges character by character (CRDT). Binary files are last-writer-wins
per path (whole-file replacement), as in Dropbox.

Blobs are **content-addressed**: `id = HMAC-SHA256(key, plaintext)[:16]` (keyed so the
server cannot confirm guesses about the content), encrypted with the project key, and
uploaded once. Compiled PDFs are blobs too, so collaborators can view the latest PDF
without compiling.

## Links, keys and access

- Each project has an **Ed25519 keypair** and a 256-bit **AES-GCM key**.
- The project ID *is* the public key (base64url), so the server can verify writes
  without any registration step.
- **Edit link:** `https://host/#p=<id>&k=<aes key>&w=<signing seed>`
- **Read-only link:** `https://host/#p=<id>&k=<aes key>`
- Keys live after `#`, which browsers never send to the server.
- Every stored update is `sign(seed, "upd:" ‖ ciphertext)`. The server and peers
  reject unsigned writes. Without `w`, the editor is read-only.
- **Personal edit links:** `#p=<id>&k=<aes key>&m=<personal seed>&g=<grant>&n=<name>`, where
  `grant = sign(owner seed, "grant:" ‖ personal public key ‖ ":" ‖ name)`. A personal
  signature is `'M' ‖ personal pub (32) ‖ grant (64) ‖ len(name) ‖ name ‖ sig (64)`, so
  anyone can verify it against the project id alone. A plain 64-byte signature is the
  owner's.
- **Revocation:** the owner sends `sign(owner seed, "revoke:" ‖ personal pub)`. Keys are
  always compared in their canonical base64url spelling (decoders accept variants of the
  same 32 bytes). The relay stores it, refuses that key's updates, snapshots, blobs, mode
  changes and signed peer-to-peer updates, and sends the list in every `log` reply (and
  `rv` frames). Clients keep the list on the device, load it before connecting, and
  ignore peer-to-peer updates until the server's `log` reply is applied; then they refuse
  that key's peer-to-peer updates too. Already-stored updates stay valid (the relay
  accepted them while the key was good), so documents still converge. Updates carry no
  trustworthy time, so revocation relies on the relay: only moving to new keys doesn't.
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
| `{t:"hello", since}` | send me everything after sequence number `since` |
| `{t:"up", d, s}` | encrypted Yjs update `d` with signature `s`; stored and broadcast |
| `{t:"snap", upto, d, s}` | compaction: encrypted full state replacing log entries ≤ `upto`; `s` signs `"snap:<upto>:" ‖ d` |
| `{t:"aw", d}` | ephemeral, encrypted awareness (cursors, names); relayed, never stored |
| `{t:"p2p", d, s?, to?}` | encrypted peer-to-peer payload (below); relayed to one peer or all, never stored |
| `{t:"mode", m, ts, s}` | set storage mode `"server"` or `"relay"`; `s` signs `"mode:<m>:<ts>"`, valid for 10 min; `relay` (which deletes the server copy) only from the owner |
| `{t:"revoke", p, s}` | owner only: revoke personal key `p`; `s` signs `"revoke:" ‖ p` |

| server → client | meaning |
|---|---|
| `{t:"log", entries:[{q,d,s}], head, floor, mode, you, peers, store, rv}` | catch-up reply to `hello` (a snapshot, if any, comes first); `you` = this connection's peer id; `floor` = entries up to here were evicted; `rv` = revocations |
| `{t:"up", q, d, s}` | someone else's update |
| `{t:"ack", q}` | your update was stored at sequence `q` |
| `{t:"aw", d}` | someone's awareness |
| `{t:"p2p", d, s?, from}` | a peer's payload |
| `{t:"mode", m}` | storage mode changed |
| `{t:"rv", p, s}` | a personal key was revoked |

Ciphertext = `iv(12 bytes) ‖ AES-256-GCM(plaintext)`.

**Offline:** each replica keeps an outbox of local updates (merged with
`Y.mergeUpdates`) that the server hasn't acknowledged yet, plus the last sequence
number it has seen. On reconnect it sends `hello`, applies what it missed, then
flushes the outbox. Yjs updates are idempotent and commutative, so order and
duplicates don't matter.

**Compaction:** when the log grows past N entries, any writer uploads an encrypted
full-state snapshot; the server drops the entries it replaces. (The server can't
compact by itself: it can't read the data.)

**Peer catch-up (Yjs sync step 1/2, relayed):** after `hello`, every replica broadcasts
`p2p {k:"sv1", sv}`, its state vector. Each writer that receives it replies directly
with `{k:"upd", u}`, the update the newcomer lacks, signed with `"p2p:" ‖ ciphertext`.
Every peer also replies with `{k:"sv2", sv}`, so the newcomer can send back whatever
*they* lack. Read-only peers never send updates, and unsigned updates are ignored.
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
`HMAC(key, b) == id`, so any peer may serve them.

**Storage modes:**
- `server` (default): the server keeps the encrypted log and blobs, so
  collaborators never need to be online at the same time.
- `relay`: the server deletes the project's data and from then on only forwards;
  `up` is broadcast and acknowledged, but not stored. Peers sync with each other
  using the catch-up handshake, so they must be connected at the same time.

The mode is a shared project setting, `meta.storage`; any editor may switch storage on,
but only the owner may switch it off. A replica sends a signed `mode`
command only when its user *changes* the setting, queuing it if offline; it never
"corrects" the server on connect, so a stale replica can't undo someone else's
switch. Switching back to `server` re-uploads a full snapshot and all blobs.

**Storage allowances.** The server can't inspect content and project ids are free to
mint, so storage is governed by allowances in a codes file: `<code|default> <0|size|unlimited>
[until=YYYY-MM-DD] [label]`.
- **Owner:** the first stored write to a project registers it to the creation code sent
  in `hello` or `X-Create-Code`, or to `default`.
- **Allowance:** an owner's allowance is the sum over all its projects.
- **Expiry:** after its `until` day (UTC) a code counts as removed: its projects fall back to
  `default`, and it no longer registers new projects. `--purge-expired-after DAYS` evicts
  (as idle eviction does) the stored projects of codes expired longer than that; re-uploads
  are then refused until the code is renewed. The owner of a project never changes.
- **Refusals:** `needs-code` (default allowance 0), `quota-full`, `project-full`
  (`--project-max-bytes`, not applied to unlimited owners) and `server-full`
  (`--max-bytes`, `--min-free`).
- **No lost work:** a refused `up` is still broadcast live but not acked; it stays in the
  sender's outbox and is retried. Clients show a "not saved on server" warning.
- **Snapshots** must satisfy `max(last snapshot, floor) < upto ≤ head`, at most 12 MiB
  (bigger documents just aren't compacted), and they are counted against limits by their
  size change.
- **Replays:** an identical `up` (same ciphertext hash) is acked with its existing sequence
  number and not stored again; hashes of updates a snapshot replaced are kept (`gone`),
  so those can't be stored again either.
- **Memory:** WebSocket frames are read whole before they can be checked, so they are
  capped at 16 MiB, and large frames being handled are capped server-wide (192 MiB).
  Files go over HTTP (≤ 50 MiB, a few uploads read at a time), or peer to peer
  (≤ 11 MiB) in relay-only projects.
- **Per-connection limits:** frame-size caps per message type, a message/byte rate limit,
  at most 5 `hello`s per 10 s, one creation-code check, and a global connection cap.
  Peers that stop reading are disconnected after 20 s rather than stalling broadcasts.
  Rows for projects that store nothing (relay mode, revocations) come out of a
  server-wide budget; at most 1000 revocations per project. nginx adds per-IP limits on
  code checks, uploads and connections.

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
  `openin_any=p`/`openout_any=p`; the main file is passed as `./name`.
- **Lines that run programs** (`texloom/scan.py`, `programRequests` in `texscan.ts`):
  `\write18`, `\ShellEscape`, `\input|`, Lua `os.execute`/`io.popen`, packages such as
  minted or pythontex, TikZ externalisation ("shell"), and arara directives or editor
  `% !TeX program` comments ("tools"). With full shell escape, a compile uses
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

## Folder sync (daemon)

- `texloom new <dir>`: turn an existing folder into a project and print both links.
- `texloom clone <link> <dir>`: check out a project into a folder.
- `texloom run <dir>`: keep the folder and the project in sync, live.
- Disk → CRDT: on a file change, diff the old text against the new and apply the
  minimal inserts/deletes. Concurrent edits from browsers therefore merge rather
  than being overwritten.
- CRDT → disk: write the merged file. If the file changed on disk since the last
  sync, ingest that change first.
- Ignores build products (`*.aux`, `*.log`, `*.synctex.gz`, the main PDF, …) and
  `.texloom/`, `.git/`.
- `--compile`: run `latexmk` after changes (debounced) and publish PDF + log to `out`.
  It also answers `p2p {k:"creq"}` compile requests from browsers set to "compile
  with my own TeX" (acknowledging with `{k:"cack", by}`).
- Local options in `.texloom/config.json`: `engine`, `latexmk_args`, `shell_escape`,
  `label`. They are never shared. Shell-escape runs code from collaborators, so it is
  strictly a local opt-in.
- Blobs referenced by remote updates are fetched in the background. In relay mode
  they arrive from a peer over the same connection, so fetching inline would deadlock.

This is the main Claude Code integration: `cd ~/papers/x && claude "fix the
related-work section"`. Edits appear live in collaborators' browsers.

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
   checks it against the index's SHA-512, and unpacks only that package's run files under
   `/var/lib/texloom/texlive` (capped by `--texlive-max`). Files are served with a one-year
   immutable cache (the URL names the release), and the service worker keeps them for
   offline use. The year must be the engine's: its formats are built from that release, so
   the index comes from that year's repository (the live tlnet while current, the
   historic `tlnet-final` afterwards). **Mirrors:** one is pinned when the index is built;
   if it fails or holds another version of a package (checksum mismatch), the CTAN
   redirector is tried (three times, a different mirror each), then any `--texlive-mirror`;
   if all differ, the index is refreshed from another mirror (at most every 10 minutes).
   A request waits up to 25 s for a first install (the engine gives up after 30); a
   bigger package answers 503 and is there for the next compile. Servers without
   `--texlive` keep the old behaviour: the recommended (200 MB) and extra (340 MB) bundles,
   fetched when a document needs them.
2. **Native:** a daemon with `--compile` runs `latexmk`. Full TeX Live: shell-escape,
   minted, etc.

Either way the PDF goes into the `out` map, so other peers just view it.

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
- **History:** each session keeps the provider-native message list and only appends
  to it, so Claude's thinking blocks (with signatures) are echoed back unchanged. A
  refused or truncated turn is never appended. After a mid-output fallback, blocks
  before the last `fallback` marker that the next model can't continue from are
  omitted. Switching provider or model starts a new conversation.
- **Tools** (`tools.ts`): `list_files`, `read_file` (with line numbers), `search`,
  `edit_file` (exact, unique match → one small Y.Text delete + insert), `write_file`
  (common prefix/suffix trimmed), `delete_file`, `compile`, `get_compile_log` (errors
  and warnings extracted). Read-only links get only the non-writing tools.
  - Every input is validated with zod before running. Streamed and local-model inputs
    can be truncated or malformed; failures go back to the model as error results.
  - `edit_file` finds its target again *after* the user approves, because collaborators
    may have typed in the meantime.
- **Review and undo:** with "Ask before applying edits", each change waits as a diff card
  (Apply / Reject). Assistant transactions use their own Yjs origin, tracked by a
  `Y.UndoManager`; each request is one undo step ("Undo these changes"). Its edits
  sync to collaborators like any local edit.
- **Context:** the system prompt is static, so it caches. Each user message starts with
  a `<context>` block: project, main file, open file and current selection.
- **Keys** live in `localStorage` only; they never enter the Yjs document or pass
  through the texloom server.

## History

The Yjs document keeps tombstones of deleted text, so snapshots are cheap:
`Y.snapshot(doc)` stored with a label and time. "View at version", "diff two
versions" and "restore file" are computed client-side. Named versions are just
labelled snapshots.

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
  stays on the machine, and nothing is joined without confirming in the app.

## Known limitations / open questions

- Deleting a project for everyone: today the owner can remove the server's copy (relay mode),
  but collaborators can keep syncing peer to peer, and a writer could switch storage back on.
  Planned: an owner-signed "deleted" marker the server keeps, refusing all further traffic
  for the project; collaborators' apps then show "The owner deleted this project", keep a
  read-only copy and offer a .zip. Their files on disk stay (they are theirs).
- Two peers who both *create* (or upload) the same path while apart: Yjs keeps one value
  per map key and deletes the other everywhere. `web/src/clashes.ts` finds the loser (a
  deleted value for the key that no later write saw: no item has it as `origin`) and
  writes it back as `name (conflict).ext`, with a banner. Only a browser that already held
  the losing version restores it, since only it knows exactly what that version said; same
  content on both sides is not a clash. The folder helper (pycrdt) can't see item origins,
  so a version created there and lost while no browser held it is not restored.
- Long divergent offline edits of the same paragraph merge character-wise and may
  interleave. Plan: highlight concurrently-edited regions after a sync for review.
- Self-declared display names. Personal links tie edits to a key the owner named, but
  revoking stops writing, not reading, without re-keying.
- Idle eviction (`--evict-idle-days`) makes the server copy a cache: sequence numbers
  keep counting, and a writer whose `since` is at or below the reported `floor`
  re-uploads its whole state and files. Link
  holders with no copy can't open an evicted project until a writer returns.
- Browser storage can be evicted: request persistent storage; the server copy and
  daemon copies are the backup.

[texlyre-busytex]: https://github.com/TeXlyre/texlyre-busytex
