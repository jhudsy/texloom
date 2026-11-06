# TeXLoom

Local-first, collaborative LaTeX. Every collaborator has a full copy of the project,
in the browser or as a plain folder on disk, and can keep working offline. A small
server passes **end-to-end encrypted** changes between people and can be told to keep
no copy at all. No accounts: you share a link. (The command-line tools are called
`texloom`.)

- Live editing with other people's cursors; offline edits merge automatically on reconnect.
- Compile in the browser (TeX Live 2026 in WebAssembly), or with your own TeX installation.
- Edit the project as an ordinary folder, so Claude Code, vim or VS Code can work on it
  directly. Changes flow both ways, live.
- Folders, images and other files; download the PDF or the whole project as a `.zip`.
- LaTeX-aware editor:
  - environments complete with their bodies;
  - `\includegraphics`, `\cite`, `\ref` and `\input` suggest the project's files, bibliography keys and labels;
  - missing ones are flagged.
- Edit links and read-only links.
- A built-in assistant that reads, edits and compiles the project, using Claude, any
  OpenAI-compatible API, or a local model (LM Studio, Ollama).
- The server is a single Python process with SQLite, small enough for a Raspberry Pi.

See [DESIGN.md](DESIGN.md) for how it works.

## How this was made: an AI note

TeXLoom was **vibe coded**: nearly all of the code, tests, deployment scripts and
documentation were written by an AI assistant (Anthropic's Claude, working in Claude
Code), directed and reviewed by the author through conversation. The author chose
the design and the features; the AI wrote the implementation, ran the tests, and
deployed it.

Its security review was also AI-performed: an independent AI audit, followed by fixes
that were verified by testing. That is not the same as a professional audit.

Treat this as an early prototype: keep your own copies of important work, and read the
security model below before relying on it.

## Using TeXLoom: nothing to install

One person runs a TeXLoom server, for example at `https://texloom.example.org`. Everyone
else just opens a link in a browser. No accounts, no software. The first time someone
opens a link, they're asked:
- their name;
- whether to keep a copy in a folder;
- how to compile: in the browser, with their own TeX, or not at all;
- whether to sync live or manually.

**If you work only in the browser, your copy lives in the browser's storage.** Clearing
the site's data (or history and site data) deletes it, and Safari may delete a site's
data after about 7 days without a visit. A project stored on the server can be reopened
from its link. One that isn't (marked **not on server**, with a warning in the editor)
survives only on devices, so keep a folder copy or download a `.zip`.

The start page lists the projects you've opened **in that browser**:
- each has a short **project code** (the same one `texloom links` prints), so two
  projects with the same name can always be told apart;
- each shows the document's `\title`, who started it and who else you've seen in it, and
  whether you have edit or read-only access;
- ✎ gives a project a private label that only you see;
- ✕ removes this browser's copy only. Collaborators keep theirs, and the server's copy
  stays unless the project is set to "Don't store anything on the server".

The server can't read or recover links, so keep important links somewhere safe; a
password manager works well.

### Editor

| | |
|---|---|
| **⌘S** / **Ctrl+S** | compile |
| **⌘B** / **Ctrl+B**, **⌘I** / **Ctrl+I** | wrap the selection in `\textbf{}` / `\textit{}` (or insert an empty one; press again to unwrap) |
| **⌘/** / **Ctrl+/** | comment or uncomment lines |
| `\begin{…}` + **Enter** | adds the matching `\end{…}` (and `\item` for lists) |
| typing `\` | completes commands and environments; `\begin{table}`, `figure`, `itemize`, `align`, … come with their usual contents. **Tab** moves between the fields to fill in |
| inside `\includegraphics{`, `\cite{`, `\ref{`, `\input{`, `\usepackage{` | suggests the project's images, bibliography keys (with titles), labels, files and common packages |

Images, citation keys and labels that don't exist in the project are underlined.
- **Files:** the tree has folders. New files and folders go into the selected folder,
  you can drag to move, and dropping files from your computer onto a folder uploads them
  there.
- **Panes:** drag the dividers between the file list, the editor and the PDF/assistant
  panel; ☰ and ▥ hide the side panes.
- **Compile log:** **Log** shows it under the PDF; drag its top edge to resize.
- **Downloads:** **⤓ PDF** and **⤓ Source** (a `.zip`).

## Running a server

The server is the web app, the encrypted relay and the in-browser TeX files, all in one
small Python process.

```bash
git clone https://github.com/jhudsy/texloom && cd texloom
uv sync                                     # Python: server + folder helper
cd web && npm install && npm run build      # web editor -> web/dist
npm run download-tex                        # in-browser TeX (~685 MB, into ../tex-assets)
cd .. && uv run texloom-server --port 8787
```

On your own machine, that's enough: open http://localhost:8787. Browsers treat
`localhost` as secure. To let other people in, the server needs **HTTPS**, because
browsers only allow the encryption the editor relies on over secure connections. Pick one:

| option | what you need | result |
|---|---|---|
| **nginx + Let's Encrypt** ([deploy/install.sh](deploy/install.sh)) | an Ubuntu/Debian server with a domain name | a sandboxed service plus an nginx site with a certificate, rate limits and security headers. It works alongside other sites. Run `deploy/install.sh root@host texloom.example.org`; re-run it to update |
| **Caddy** ([deploy/Caddyfile](deploy/Caddyfile)) | a domain name pointing at the server, ports 80/443 open | Caddy gets and renews the certificate itself |
| **Cloudflare Tunnel** | a free Cloudflare account | HTTPS with **no open ports**, ideal for a Raspberry Pi at home. Try `cloudflared tunnel --url http://localhost:8787` |
| **Tailscale Funnel** | Tailscale on the server | `tailscale funnel 8787`, also with no open ports |

Participants only ever make outgoing HTTPS connections to the server, so their
firewalls and NAT need no changes.

### Storage allowances (creation codes)

The server can't see what it stores, and making a project needs no account, so storage
is governed by **allowances** in a small text file (`--codes-file`):

```
# <code|default>  <allowance>  [label]
default          0             # projects created without a code: not stored on the server
A3F7-9KQ2-XM4P   unlimited     owner
K7P2-QX4M-9H3D   2G            alice
```

- A code's allowance is the total stored across **all** projects created with it.
  `default` covers people without a code. `0` means "not stored on the server": those
  people can still create projects, but only as live, relay-only projects whose copies
  live on their devices.
- Anyone with a project's link can open and edit it; codes only govern **storage of new
  projects**. A code is entered once per browser (**New project** asks, or ⚙ Settings →
  Creation code…). The folder helper takes `texloom new … --code`, `create_code` in
  `.texloom/config.json`, or the `TEXLOOM_CODE` environment variable.
- The file is re-read when it changes. Deleting a line revokes a code: its projects stay
  stored, but new changes then count against `default`.
- Projects stored before allowances existed are grandfathered as unlimited.

```bash
texloom-server --codes-file /etc/texloom/codes.txt --add-code alice --allowance 2G   # prints a new code
texloom-server --codes-file /etc/texloom/codes.txt --list-codes --db /var/lib/texloom/texloom.db
```

The server also refuses to store anything when free disk drops below `--min-free`
(default 2G), which protects other sites on the machine. Two more limits are
`--project-max-bytes` (default 200M per project, except for unlimited allowances) and
`--max-bytes` (total). Without `--codes-file`, every project may be stored.

A refused change is never lost:
- it still reaches everyone online at that moment;
- the sender keeps it and retries;
- the editor shows a warning that the project isn't saved on the server.

## Working on a project as a folder

Use this for Claude Code or any other tool, and to compile with your own TeX installation:

```bash
uv run texloom clone '<link>' ~/papers/demo      # quote the link: it contains '&'
uv run texloom run ~/papers/demo --compile       # live two-way sync + latexmk
```

Or share an existing folder: `uv run texloom new ~/papers/existing --server https://your.server`.

While `run` is going, edits to the files are merged character by character with what
collaborators type in their browsers. With `--compile`:
- it runs `latexmk` after changes and shares the PDF with everyone;
- browsers set to **Compile with my own TeX** send their compile requests to it.

| command | |
|---|---|
| `texloom new <dir> --server URL [--code C]` | share an existing folder as a new project; prints both links |
| `texloom clone <link> <dir>` | check out a project |
| `texloom run <dir> [--compile] [--shell-escape]` | keep the folder in sync, live |
| `texloom sync <dir>` | sync once and exit (manual sync) |
| `texloom storage <dir> server\|relay` | keep an encrypted copy on the server, or nothing |
| `texloom links <dir>` | print the project code and the edit / read-only links |

Per-folder options are in `<dir>/.texloom/config.json`, which only you can read. They are
never shared:

```json
{ "compile": false, "engine": null, "latexmk_args": [], "shell_escape": false, "latexmk_rc": null, "label": "", "create_code": "" }
```

Collaborators write the document, so compiling is locked down by default:
- **No running programs:** TeX may not run programs (`-no-shell-escape`).
- **No access outside the project:** TeX may not read or write files there (TeX's
  "paranoid" file access).
- **No rc files:** latexmk ignores rc files (`-norc`), because a `latexmkrc` is Perl.
  Name one you trust with `latexmk_rc`.
- **Unsafe files aren't written:** the helper never writes files such as `latexmkrc` or
  `texmf.cnf` from the project, nor any path outside the folder.

`shell_escape` (needed for minted and similar) lets the document run programs on *your*
machine: turn it on only for projects whose editors you trust.

## Assistant

The **Assistant** tab next to the PDF is a chat that works on the project like a
collaborator:
- it can list, read and search files, make targeted edits, create or delete files,
  compile, and read the compile log;
- with **Ask before applying edits** (the default), each change appears as a diff to
  apply or reject;
- every request's edits can be undone in one click, and they reach collaborators like
  anyone else's.

Providers (pick one in the tab; settings via ⚙):
- **Anthropic (Claude):** uses the official SDK from the browser; the default model is
  `claude-opus-5-5`. Effort is adjustable, thinking summaries are shown, and if a safety
  classifier declines a request it is retried on Anthropic's recommended fallback model.
- **OpenAI, OpenRouter, or any OpenAI-compatible endpoint** (vLLM, llama.cpp, LiteLLM, …).
- **LM Studio / Ollama, running locally.** Enable CORS in LM Studio
  (`lms server start --cors`). Ollama accepts `localhost` pages by default; from another
  site, set `OLLAMA_ORIGINS`. Use a model with tool calling (for example a Qwen 3.x model).

API keys and provider settings are stored only in your browser. They are not part of
the project and never pass through the TeXLoom server: requests go straight from your
browser to the provider.

## Settings

**Project settings** are shared with everyone who has an edit link:
- main file
- engine (pdfLaTeX, XeLaTeX or LuaLaTeX)
- bibliography tool (detect automatically, BibTeX, Biber or none)
- server storage:
  - **Keep an encrypted copy** (default)
  - **Don't store anything:** this deletes the server's copy

**Device settings** apply only in your browser:
- your name
- creation code
- live or manual sync. Manual keeps changes on your device until you press **Sync now**.
- where to compile, and whether to recompile automatically after you stop typing.

The in-browser TeX engine is cached once per browser and shared by all projects (about
125 MB up front; larger package sets load only if a document needs them).

## Security model

**What's protected**
- **Server storage is encrypted.** Project content, file names and compiled PDFs are
  encrypted in the browser or the folder helper with a key that is only in the link
  (after `#`, which browsers never send to servers). The server stores and relays
  ciphertext.
- **Edits are signed.** Writes are signed with the edit key, and the server and every
  peer reject unsigned or wrongly signed changes, so read-only links can't edit.
- **The server can't tamper with content.** Files fetched from it are checked against
  their content hash.

**What isn't**
- **The web app trusts the server that serves it.** A malicious or compromised operator
  could serve modified JavaScript that reads the keys from the link. Use a server you
  trust (or run your own); the folder helper doesn't have this exposure. The server
  sends a strict Content-Security-Policy, and the install script pins the in-browser
  TeX download by checksum.
- **Anyone with a link has that access, permanently.** Links can't be revoked: to cut
  someone off, copy the project to a new one. Read-only holders see everything, including
  the names and presence of other collaborators.
- **Editors are trusted with the project.** A malicious editor can delete or vandalise
  content, or tell the server to drop its copy. Keep your own copies (folder copy, zip,
  or your own git) of work that matters.
- **The project ID is not secret.** It appears in server logs. It lets someone connect
  and see encrypted traffic sizes and presence, but not content.
- **Compiling with your own TeX runs TeX on documents others wrote.** The defaults above
  close the known ways to read your files or run commands. For untrusted collaborators,
  run the helper in a container or VM.

Found a problem? Please open an issue.

## Development

```bash
uv run texloom-server --port 8787   # terminal 1
cd web && npm run dev               # terminal 2 -> http://localhost:5173 (proxies to :8787)
```

| path | what |
|---|---|
| `server/relay.py` | relay + encrypted store (aiohttp, SQLite): signatures, storage modes, allowances, limits |
| `texloom/crypto.py` | keys, links, AES-GCM, Ed25519 (mirrors `web/src/crypto.ts`) |
| `texloom/replica.py` | folder helper's replica: pycrdt document, outbox, peer handshake, blobs |
| `texloom/folder.py` | two-way folder ⇄ CRDT sync, path safety, locked-down latexmk |
| `texloom/cli.py` | command line |
| `web/src/main.ts` | start page and project view wiring |
| `web/src/replica.ts` | browser replica: Yjs + IndexedDB, outbox, peer handshake, awareness, blobs |
| `web/src/filetree.ts`, `projectfiles.ts` | folders and files, moves, zip export |
| `web/src/editor.ts`, `latex.ts` | CodeMirror editor; LaTeX completion, shortcuts, reference checks |
| `web/src/output.ts`, `compile.ts`, `pdfview.ts` | compiling (in browser or via the helper), PDF view, log |
| `web/src/settings.ts`, `storagenotice.ts`, `projects.ts`, `layout.ts` | setup and settings, storage warnings, start-page list, panes |
| `web/src/assistant/` | assistant: provider adapters (`llm.ts`), tools (`tools.ts`), chat panel (`panel.ts`) |
| `deploy/` | install script (nginx, certbot, systemd), Caddy config, systemd unit |

## Status

Early prototype. Not yet built:
- history / compare-versions UI
- comments and tracked changes
- SyncTeX (click between source and PDF)
- direct browser-to-browser WebRTC
- syncing a local folder from the browser itself (File System Access API, Chromium only)
- packaging the helper for `pipx` / `uvx`

## License

AGPL-3.0-or-later. In-browser TeX uses [texlyre-busytex](https://github.com/TeXlyre/texlyre-busytex) (AGPL-3.0).
