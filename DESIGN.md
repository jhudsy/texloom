# texloom — design

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
| `meta`: Y.Map     | `name`, `main` (main .tex file), settings                       |
| `out`: Y.Map      | latest compiled output: `{pdf, log, by, at}` (blob ids)         |

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
- Revocation = create a new project (new keys) and re-share. Documented limitation.
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
| `{t:"mode", m, ts, s}` | set storage mode `"server"` or `"relay"`; `s` signs `"mode:<m>:<ts>"`, valid for 10 min |

| server → client | meaning |
|---|---|
| `{t:"log", entries:[{q,d,s}], head, mode, you, peers}` | catch-up reply to `hello` (a snapshot, if any, comes first); `you` = this connection's peer id |
| `{t:"up", q, d, s}` | someone else's update |
| `{t:"ack", q}` | your update was stored at sequence `q` |
| `{t:"aw", d}` | someone's awareness |
| `{t:"p2p", d, s?, from}` | a peer's payload |
| `{t:"mode", m}` | storage mode changed |

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
Blobs move the same way: `{k:"breq", id}` → `{k:"blob", id, b}`. The receiver checks
`HMAC(key, b) == id`, so any peer may serve them.

**Storage modes:**
- `server` (default): the server keeps the encrypted log and blobs, so
  collaborators never need to be online at the same time.
- `relay`: the server deletes the project's data and from then on only forwards;
  `up` is broadcast and acknowledged, but not stored. Peers sync with each other
  using the catch-up handshake, so they must be connected at the same time.

The mode is a shared project setting, `meta.storage`. A replica sends a signed `mode`
command only when its user *changes* the setting, queuing it if offline; it never
"corrects" the server on connect, so a stale replica can't undo someone else's
switch. Switching back to `server` re-uploads a full snapshot and all blobs.

**Storage safeguards.** The server can't inspect content, and project ids are free to
mint, so it limits by size and, optionally, by who may create stored projects.
- **Usage:** it keeps byte counts per project (log + blobs) and overall, and checks free
  disk space before each write.
- **Refusals:** a write that would exceed `--project-max-bytes`, `--max-bytes` or
  `--min-free` is refused with `err {code: "project-full" | "server-full"}`. Under
  `--codes-file`, the first write to an unregistered project needs a valid creation code,
  sent in `hello` or the `X-Create-Code` header; otherwise it gets `needs-code`.
- **No lost work:** a refused `up` is still broadcast live but not acked, so it stays in the
  sender's outbox and is retried. Clients show a "not saved on server" warning.
- **Registration:** registered projects need no code afterwards. Projects stored before
  this feature are grandfathered in.
- **Rate limits:** nginx limits code checks (6/min per IP), uploads and connections.

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
   Worker. The engine is about 32 MB plus the 93 MB basic package set, preloaded. The
   recommended (200 MB) and extra (340 MB) sets are catalogued and fetched only if a
   document needs them. Everything is cached in IndexedDB per site, so it's shared by
   all projects and downloaded once per browser, with a progress bar. They are static
   files under `/core/`, so the server (or a CDN) can host them.
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

## Known limitations / open questions

- Two peers who both *create* the same path while offline: one file wins (LWW on the
  `files` map). Mitigation: show a warning, keep the loser as `name (conflict).tex`.
- Long divergent offline edits of the same paragraph merge character-wise and may
  interleave. Plan: highlight concurrently-edited regions after a sync for review.
- Self-declared identities; no revocation without re-keying.
- Browser storage can be evicted: request persistent storage; the server copy and
  daemon copies are the backup.

[texlyre-busytex]: https://github.com/TeXlyre/texlyre-busytex
