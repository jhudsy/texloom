# texloom

Local-first, collaborative LaTeX. Every collaborator has a full copy of the project,
in the browser or as a plain folder on disk, and can keep working offline. A tiny
server passes **end-to-end encrypted** changes between people; it never sees your
files and can be told to keep no copy at all. No accounts: you share a link.

- Live editing with other people's cursors; offline edits merge automatically on reconnect.
- Compile in the browser (TeX Live 2026 in WebAssembly), or with your own TeX installation.
- Edit the project as an ordinary folder, so Claude Code, vim or VS Code can work on it
  directly. Changes flow both ways, live.
- Images and other binary files are shared too, so everyone can compile.
- Edit links and read-only links.
- A built-in assistant that reads, edits and compiles the project, using Claude, any
  OpenAI-compatible API, or a local model (LM Studio, Ollama).
- The server is a single Python process with SQLite, small enough for a Raspberry Pi.

See [DESIGN.md](DESIGN.md) for how it works.

## Using texloom: nothing to install

One person runs a texloom server, for example at `https://texloom.example.org`. Everyone
else just opens a link in a browser. No accounts, no software. The first time someone
opens a link, they're asked:
- their name;
- whether to keep a copy in a folder;
- how to compile: in the browser, with their own TeX, or not at all;
- whether to sync live or manually.

The start page lists the projects you've opened **in that browser**, with ✕ to remove
one. Removing deletes only that browser's copy: collaborators keep theirs, a folder
copy is just a folder, and the server's encrypted copy is deleted only by choosing
"Don't store anything on the server" in the project's settings. The list is kept
per browser and the server can't read or recover links, so keep important links
somewhere safe (a password manager works well). Any saved link restores the project
from the server's copy.

Installing anything is optional, and only for people who want the project as a folder
on disk (for Claude Code, vim, …) or want to compile with their own TeX installation;
see "Working on a project as a folder" below.

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
| **Caddy** ([deploy/Caddyfile](deploy/Caddyfile)) | a domain name pointing at the server, ports 80/443 open | `https://texloom.example.org`; Caddy gets and renews the Let's Encrypt certificate itself |
| **Cloudflare Tunnel** | a free Cloudflare account (and a domain, for a permanent name) | HTTPS with **no open ports**, so it's ideal for a Raspberry Pi at home. Try it instantly with `cloudflared tunnel --url http://localhost:8787`, which prints a temporary `https://….trycloudflare.com` address |
| **Tailscale Funnel** | Tailscale on the server | `tailscale funnel 8787` gives `https://<machine>.<tailnet>.ts.net`, also with no open ports |

To keep it running, there's a systemd unit in [deploy/texloom.service](deploy/texloom.service).

**Ubuntu/Debian with nginx** (e.g. alongside other sites): [deploy/install.sh](deploy/install.sh)
builds locally, copies the code over SSH and sets up a `texloom` user, a sandboxed
systemd service on `127.0.0.1:8787`, an nginx site and a Let's Encrypt certificate.
nginx serves the static files itself (web app and TeX files), so only sync traffic
reaches Python. Re-run it to update:

```bash
deploy/install.sh root@texloom.example.org texloom.example.org
```
Participants only ever make outgoing HTTPS connections to the server, so their
firewalls and NAT need no changes.

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
| `texloom new <dir> --server URL` | share an existing folder as a new project; prints both links |
| `texloom clone <link> <dir>` | check out a project |
| `texloom run <dir> [--compile] [--shell-escape]` | keep the folder in sync, live |
| `texloom sync <dir>` | sync once and exit (manual sync) |
| `texloom storage <dir> server\|relay` | keep an encrypted copy on the server, or nothing |
| `texloom links <dir>` | print the edit / read-only links |

Per-folder options are in `<dir>/.texloom/config.json`. They are never shared:

```json
{ "compile": false, "engine": null, "latexmk_args": [], "shell_escape": false, "label": "" }
```

`shell_escape` (needed for minted and similar packages) lets the document run programs
on *your* machine. Since collaborators write the document, it is only ever a local opt-in.

## Assistant

The **Assistant** tab next to the PDF is a chat that works on the project like a
collaborator. It can list, read and search files, make targeted edits, create or delete
files, compile, and read the compile log. With **Ask before applying edits** (the
default), each change appears as a diff to apply or reject. Every request's edits can
be undone in one click, and they reach collaborators like anyone else's.

Providers (pick one in the tab; settings via ⚙):
- **Anthropic (Claude):** uses the official SDK from the browser; default model
  `claude-opus-5-5`. Effort is adjustable, thinking summaries are shown, and if a safety
  classifier declines a request it is retried on Anthropic's recommended fallback model.
- **OpenAI, OpenRouter, or any OpenAI-compatible endpoint** (vLLM, llama.cpp, LiteLLM, …).
- **LM Studio / Ollama, running locally.** Enable CORS in LM Studio
  (`lms server start --cors`). Ollama accepts `localhost` pages by default; from another
  site, set `OLLAMA_ORIGINS`. Use a model with tool calling (for example a Qwen 3.x model).

API keys and provider settings are stored only in your browser. They are not part of
the project and never pass through the texloom server: requests go straight from your
browser to the provider.

## Settings

**Project settings** are shared with everyone who has an edit link:
- main file
- engine (pdfLaTeX, XeLaTeX or LuaLaTeX)
- bibliography tool (detect automatically, BibTeX, Biber or none)
- server storage:
  - **Keep an encrypted copy** (default): collaborators receive changes even if you're
    never online at the same time.
  - **Don't store anything**: the server only relays between people who are connected at
    the same moment. Switching to this deletes the server's copy.

**Device settings** apply only in your browser:
- your name
- live or manual sync. Manual keeps changes on your device until you press **Sync now**,
  for example to finish a thought first. Live updates are tiny, so bandwidth is rarely
  a reason.
- where to compile, and whether to recompile automatically after you stop typing.

The in-browser TeX engine is cached once per browser and shared by all projects. Only
the engine plus the basic package set (about 125 MB) is fetched up front; larger package
sets load only if a document needs them. Progress is shown while it downloads.

## Development

```bash
uv run texloom-server --port 8787   # terminal 1
cd web && npm run dev               # terminal 2 -> http://localhost:5173 (proxies to :8787)
```

| path | what |
|---|---|
| `server/relay.py` | relay + encrypted store (aiohttp, SQLite); verifies signatures; storage modes |
| `texloom/crypto.py` | keys, links, AES-GCM, Ed25519 (mirrors `web/src/crypto.ts`) |
| `texloom/replica.py` | Python replica: pycrdt doc, outbox, peer handshake, blobs |
| `texloom/folder.py` | two-way folder ⇄ CRDT sync with fine-grained diffs; latexmk |
| `texloom/cli.py` | command line |
| `web/src/replica.ts` | browser replica: Yjs + IndexedDB, outbox, peer handshake, awareness, blobs |
| `web/src/main.ts` | UI: file tree, CodeMirror 6 collaborative editor, PDF viewer (PDF.js) |
| `web/src/settings.ts` | first-open setup, project + device settings |
| `web/src/compile.ts` | in-browser compile (texlyre-busytex) |
| `web/src/assistant/` | assistant: provider adapters (`llm.ts`), project tools (`tools.ts`), chat panel (`panel.ts`) |
| `deploy/` | Caddy config (automatic HTTPS) and systemd unit |

## Status

Early prototype. Not yet built:
- history / compare-versions UI
- direct browser-to-browser WebRTC
- syncing a local folder from the browser itself (File System Access API, Chromium only)
- conflict warnings when two people create the same path offline
- packaging the helper for `pipx` / `uvx`

## License

AGPL-3.0-or-later. In-browser TeX uses [texlyre-busytex](https://github.com/TeXlyre/texlyre-busytex) (AGPL-3.0).
