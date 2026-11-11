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
| `versions`: Y.Array | saved versions: `{at, by, label?, auto?, link}` |
| `versionChain`: Y.Array | what versions are built from: `{id, base, snap}`, see History |
| `clients`: Y.Map  | Yjs client id → display name (for "who wrote this")             |
| `review`: Y.Map   | id → Y.Map `{kind: comment\|suggestion, file, start, end, quote, text, by, at, resolved, outcome?, replies: Y.Array, typed?, conflict?}`; `start`/`end` are encoded Yjs relative positions, so they follow edits; `typed` and `conflict` come from suggest mode (see below). Moving a file makes a new `Y.Text`, so `movePath` re-anchors its items at the same places in the copy and updates `file` |
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
| `{t:"delete", ts, s}` | owner only: delete the project for everyone; `s` signs `"delete:<ts>"`, valid for 10 min. The server erases what it stores, keeps a tombstone (the `deleted` table, which purges never touch; its `projects` row goes) and from then on answers the project's connections with `deleted` and its file requests with 410 |

| server → client | meaning |
|---|---|
| `{t:"log", entries:[{q,d,s}], head, floor, mode, you, peers, store, rv}` | catch-up reply to `hello` (a snapshot, if any, comes first); `you` = this connection's peer id; `floor` = entries up to here were evicted; `rv` = revocations |
| `{t:"up", q, d, s}` | someone else's update |
| `{t:"ack", q}` | your update was stored at sequence `q` |
| `{t:"aw", d, from}` | someone's awareness |
| `{t:"left", from}` | that connection closed: stop showing the awareness it sent |
| `{t:"p2p", d, s?, from}` | a peer's payload |
| `{t:"mode", m}` | storage mode changed |
| `{t:"rv", p, s}` | a personal key was revoked |
| `{t:"deleted", at, ts, s}` | the owner deleted the project for everyone (at, ms), with the owner's signature `s` over `"delete:<ts>"`, which clients check, so a server can't fake it; the connection then closes. Clients keep their copy read-only and stop connecting (web: `deleted` in the replica's kv store; helper: `state.json`) |

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

**Private servers** (`--private`). A connection does nothing until its `hello` is accepted (a silent
one is dropped after 15 s). A project is admitted if its owner is a current creation code. One with no
code yet (unregistered, or `default`) gets the first valid code offered; one whose code has
lapsed is refused until it's renewed, and can't be taken over by another code. A refusal is
`{t:"err", code, denied:true}` followed by a close. Open connections are checked again about once a
minute. File uploads and downloads and new project lists follow the same rule.

**Storage allowances.** The server can't inspect content and project ids are free to
mint, so storage is governed by allowances in a codes file: `<code|default> <0|size|unlimited>
[until=YYYY-MM-DD] [label]`. A line without an allowance is skipped, with a warning in the log.
- **Owner:** the first stored write to a project registers it to the creation code sent
  in `hello` or `X-Create-Code`, or to `default`.
- **Allowance:** an owner's allowance is the sum over all its projects.
- **Expiry:** after its `until` day (UTC) a code counts as removed: its projects fall back to
  `default`, and it no longer registers new projects. `--purge-expired-after DAYS` evicts
  (as idle eviction does) the stored projects of codes expired longer than that; re-uploads
  are then refused until the code is renewed. The owner of a project never changes.
- **Refusals:** `needs-code` (default allowance 0), `quota-full`, `code-expired` (the owner's
  code has expired or was removed, and the default allowance doesn't cover the write),
  `project-full` (`--project-max-bytes`, not applied to unlimited owners) and `server-full`
  (`--max-bytes`, `--min-free`).
- **No lost work:** a refused `up` is still broadcast live but not acked; it stays in the
  sender's outbox and is retried. Clients show a "not saved on server" warning.
- **Snapshots** must satisfy `max(last snapshot, floor) < upto ≤ head`, at most 12 MiB
  (bigger documents just aren't compacted), and they are counted against limits by their
  size change.
- **Replays:** an identical `up` (same ciphertext hash) is acked with its existing sequence
  number and not stored again; hashes of updates a snapshot replaced are kept (`gone`),
  so those can't be stored again either. Only the last 10,000 sequence numbers per project
  are remembered, each counting 100 bytes against the allowance, and eviction, purging or
  going relay-only forgets them. An older replay is harmless: updates are idempotent, and
  it counts against the allowance only until the next snapshot.
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
2. **Native:** a daemon with `--compile` (or the desktop app, per folder) runs `latexmk`.
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

Either way the PDF goes into the `out` map, so other peers just view it.

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
  its own window, and closing the window hides it (the Dock icon brings it back). Windows and
  Linux get a Settings button in the toolbar; closing hides the window in the tray, saying so the
  first time, and on a desktop with no tray asks whether to minimise instead of quitting.

## Project lists

An optional, encrypted copy of a person's list of projects (`server/lists.py`, `texloom/listsync.py`,
`web/src/listsync.ts`; the two clients must match). The server stores opaque blobs and can't tie
one to the projects it names.

- A list has a random 32-byte secret K, carried in a sync link (`/#sync=<K>`).
  `seed = HMAC-SHA256(K, "texloom list sign")` gives an Ed25519 key pair whose public key is the
  list's id; `aes = HMAC-SHA256(K, "texloom list encrypt")` is the AES-GCM key. The blob is
  `iv | ciphertext`.
- `GET /api/list/<id>` returns the blob and `X-Seq`. `PUT` carries `X-Seq` (must be the current
  value + 1, otherwise 409 and the client merges and retries) and `X-Sig`, a signature over
  `list:<id>:<seq>:` + blob. `DELETE` is signed over `listdel:<id>`. A new list needs what a new
  project needs (`X-Create-Code`) and belongs to that code, so purging the code removes it.
- Document: `{"v":1,"projects":{"<id>":{"f":fragment,"n":name,"at":ms,"w":bool,"nick"?,"t"?,"by"?,"c"?:ms,"m"?:ms}},"gone":{"<id>":ms}}`.
  `c` is when the project joined the list and `m` when a device last saw its files change (the
  server can't tell, and at most every few minutes, because each write syncs the list).
  Merging keeps the latest `gone` time per project and the most recently opened entry, never
  replaces an edit link with a read-only one, keeps the earliest `c` and latest `m`, and drops a
  project removed after it was last opened.
  Removal records are forgotten after a year.
- Other ways in are entries of the same kind holding `{"v":1,"wrap":<K>}`, opened by their own
  secret. A password's secret is `scrypt(password, "texloom list password v1:" + username, N=2^17,
  r=8, p=1)`, with the password NFKC-normalised and the username also trimmed and lower-cased. A
  passkey's secret comes from WebAuthn PRF with the salt `texloom project list v1`. An entry opened
  by `HMAC-SHA256(K, "texloom list slots")` records each way in with its signing seed (not its
  encryption key), so whoever holds K can remove them but not read them.
- `DELETE` retires an entry: its contents go, but its id answers 410 until it is purged, so a
  device with a replaced (perhaps leaked) link can't start the list again under that link. Replacing
  a link retires every way in, because anyone holding the old link can read what it records. The
  desktop app can't use a passkey, so it opens `/#unlock-for-app`, and the page returns the sync
  link through `texloom://sync?link=…` (at once if the browser already has the list; **Use in the
  TeXLoom app** in the web dialog sends it unasked), which the app confirms before using unless it
  is the list the app already has.
- Settings that follow the list, for now just the creation code, are a further entry opened by
  `HMAC-SHA256(K, "texloom list settings")`: `{"v":1,"s":{"code":{"v":code,"at":ms}}}`. The latest
  `at` wins (a tie goes to the larger canonical form, the same on every device; checked by the shared
  vectors), so a code entered in the web app reaches the desktop app's New project dialog and the
  other way round. The web app records when its code was entered (`texloom.codeAt`); the desktop
  app keeps one code per server in `creation-codes.json` (mode 600) in its config folder. A code
  entered before dates were kept counts as the oldest. Anyone with the sync link can read the code,
  as they can every project on the list. The times are each device's clock, so a device whose clock
  is far ahead could keep an older code winning until a code is entered again after that time.

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

## Copying a project

`web/src/copyproject.ts` makes a new project (new keys, the copier as owner) from the current state
of another: files, explicit folders, and the `main`, `bib`, `engine` and `lang` settings. History,
saved versions, comments, members and compiled output are left behind, and binary files are read
and stored again because each project encrypts with its own key. The copy is filled while
disconnected, so it reaches the server only when opened, with the storage chosen for it. Anyone who
can read a project can copy it. A copy of a project with no server copy has none either. The desktop
app copies a folder's project files (not `.texloom`, tools' folders, LaTeX's working files or
symlinks) into a new folder and shares that as a new project with the same settings.

## Known limitations / open questions

- Deleting a project for everyone stops it on its server only. Anyone with a copy can still read
  it, and could share it as a new project; it can't take back what people already have.
- Renaming a file on disk, in a folder kept in sync, reaches the project as one file deleted and
  another added, so comments and suggestions on it lose their place (renaming in the app keeps them).
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
- Deleting a project's data on the server (delete for everyone, idle eviction, purging an
  unused project) runs in one go on the relay's event loop, so for a very large project
  other connections wait until it finishes. Fix: delete in batches, yielding between them,
  or hand it to a worker thread.
- The desktop app refreshes its project list every second, reading each project's tags
  again and rebuilding every row. Fine for tens of projects; for many more, keep the tags
  until their file changes and update only rows whose state changed.

[texlyre-busytex]: https://github.com/TeXlyre/texlyre-busytex
