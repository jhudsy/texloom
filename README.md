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
- The server is a single Python process with SQLite, small enough for a Raspberry Pi.

See [DESIGN.md](DESIGN.md) for how it works.

## Quick start

```bash
uv sync                                     # Python: server + folder helper
cd web && npm install && npm run build      # web editor -> web/dist
npm run download-tex                        # optional: in-browser TeX (~685 MB, into ../tex-assets)
cd .. && uv run texloom-server --port 8787
```

Open http://localhost:8787, click **New project**, then **Share** for the links. The first
time someone opens a link, they're asked:
- their name;
- whether to keep a copy in a folder;
- how to compile: in the browser, with their own TeX, or not at all;
- whether to sync live or manually.

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

## Deploying the server

```bash
uv run texloom-server --host 127.0.0.1 --port 8787 --db /var/lib/texloom/texloom.db
```

Put it behind HTTPS (Caddy, nginx, or a Cloudflare / Tailscale tunnel). This is
required: browsers only allow the encryption API (WebCrypto) on `https://` pages or
`localhost`. The proxy must pass WebSocket upgrades for `/ws/`.

**NAT and firewalls:** every participant (browser or folder helper) only makes
*outgoing* HTTPS/WebSocket connections to the server, like any web page, so home
routers, NAT and ordinary firewalls need no configuration. Only the server needs to be
reachable. For a Raspberry Pi at home, either forward port 443 to it, or use a tunnel
(Cloudflare Tunnel, Tailscale Funnel) so you don't need to open any ports at all.

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

## Status

Early prototype. Not yet built:
- in-editor LLM assistant (any provider, including local models)
- history / compare-versions UI
- direct browser-to-browser WebRTC
- syncing a local folder from the browser itself (File System Access API, Chromium only)
- conflict warnings when two people create the same path offline
- packaging the helper for `pipx` / `uvx`

## License

AGPL-3.0-or-later. In-browser TeX uses [texlyre-busytex](https://github.com/TeXlyre/texlyre-busytex) (AGPL-3.0).
