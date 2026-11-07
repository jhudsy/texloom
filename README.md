# TeXLoom

Local-first, collaborative LaTeX. Every collaborator has a full copy of the project,
in the browser or as a plain folder on disk, and can keep working offline. A small
server passes **end-to-end encrypted** changes between people and can be told to keep
no copy at all. No accounts: you share a link. (The command-line tools are called
`texloom`.)

- Live editing with other people's cursors (click someone's avatar to jump to them);
  offline edits merge automatically on reconnect. Installs as an offline-capable app.
- Compile in the browser (TeX Live 2026 in WebAssembly), or with your own TeX installation.
  Errors are listed with clickable locations; SyncTeX jumps between source and PDF
  (the → / ← arrows on the editor/PDF divider, ⌘⌥→ / ⌘⌥←, or double-click / right-click the PDF).
- History without git: automatic and named versions, compare with now, restore a file or
  everything, and see who wrote each line.
- Comments and suggested changes (tracked changes) that collaborators accept or reject.
- Edit the project as an ordinary folder, so Claude Code, vim or VS Code can work on it
  directly; changes flow both ways, live. Chrome and Edge can sync a folder with nothing
  installed.
- Agents as collaborators: an MCP server (`texloom mcp`) lets Claude Code or any MCP
  client read, edit, comment, suggest and compile, visible to everyone by name and cursor.
  A built-in assistant does the same with Claude, any OpenAI-compatible API, or a local
  model (LM Studio, Ollama).
- LaTeX-aware editor: completion of environments, commands, files, citation keys and
  labels; spell check that skips LaTeX; search and replace across files; an outline with
  word counts; paste or drop images; add references from a DOI or arXiv id.
- Start from templates, or import a `.zip` (e.g. from Overleaf) or an arXiv paper's source.
- Read-only links, and personal edit links that can be revoked one by one.
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

### Sharing and revoking

**Share** shows the links. Whoever created the project holds the **owner link**. From it
they can make a **personal edit link** for each collaborator (by name), and later
**revoke** one: the server and everyone's browsers then refuse that person's changes, and
their app reopens read-only. The list shows the start of each link's key, so two people
with the same name can be told apart; only the owner can switch off server storage. A revoked person can still *read* new changes, because the
link holds the encryption key. To shut them out completely, **Move to new keys**: the
project (with history and comments) is copied into a new project with new keys, files
are re-encrypted, the old server copy is deleted, and you send new links to the people
who should keep access. The plain **read-only link** works as before.

**Email…** next to a personal or read-only link opens your own email app with an
invitation and the link filled in. The link travels in that email, never through the
TeXLoom server (which must not see its keys), so anyone who can read the email can open
the project. A personal link is shown, and can be emailed, only when you create it.

### Editor

| | |
|---|---|
| **⌘S** / **Ctrl+S** | compile |
| **⌘B** / **Ctrl+B**, **⌘I** / **Ctrl+I** | wrap the selection in `\textbf{}` / `\textit{}` (or insert an empty one; press again to unwrap) |
| **⌘/** / **Ctrl+/** | comment or uncomment lines |
| `\begin{…}` + **Enter** | adds the matching `\end{…}` (and `\item` for lists) |
| typing `\` | completes commands and environments; `\begin{table}`, `figure`, `itemize`, `align`, … come with their usual contents. **Tab** moves between the fields to fill in |
| inside `\includegraphics{`, `\cite{`, `\ref{`, `\input{`, `\usepackage{` | suggests the project's images, bibliography keys (with titles), labels, files and common packages |

| **⌘⇧F** / **Ctrl+Shift+F** | search (and replace) across all files |
| **⌘⌥M** / **Ctrl+Alt+M**, **⌘⌥E** / **Ctrl+Alt+E** | comment on the selection / suggest a change to it |
| **⌘⌥→** / **Ctrl+Alt+→** | show the current line in the PDF |
| **⌘⌥←** / **Ctrl+Alt+←** | go to the source of the spot last clicked in the PDF (or the middle of the view); double-clicking or right-clicking the PDF does the same |
| paste or drop an image | adds it to `figs/` and inserts `\includegraphics` (a whole `figure` on an empty line) |
| `\cite{` + a DOI or arXiv id | fetches the BibTeX entry and adds it to your `.bib` |

Images, citation keys and labels that don't exist in the project are underlined, as are
lines with compile errors. Spelling is checked with the browser's dictionary (language in
Settings), skipping commands, maths, labels and the preamble.
- **Left panel:** **Files**, **Search** (match case, whole words, regular expressions with
  `$1`), and **Outline** (sections in document order through `\input`/`\include`, with
  word counts; the total excludes maths and commands).
- **Right panel:** **PDF**, **Assistant**, **Review** (comments and suggestions: reply,
  resolve, accept, reject) and **History** (versions, compare, restore, "who wrote this").
- **Problems:** after compiling, errors and warnings are listed under the PDF with their
  file and line; click one to go there. **Raw log** shows the whole log.
- **New projects** start from a template (article, paper with bibliography, Beamer, thesis
  with chapters, letter) or an import: a `.zip` (Overleaf: Menu → Download → Source; a
  GitHub "Download ZIP"; any zipped folder) or an arXiv paper's LaTeX source. **Import** in
  the file list does the same for an existing project. From git: clone it and run
  `texloom new` in the folder.
- **+ Reference** adds a bibliography entry from a DOI or arXiv id (via doi.org) with a
  readable key such as `vaswani2017attention`, and cites it at the cursor.
- **Offline:** after the first visit the app (and, after one compile, the TeX engine) works
  offline; browsers offer to install it as an app.
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

### Idle projects, admin page, arXiv import

- `--evict-idle-days N` drops the stored copy of projects nobody has opened for N days
  (default: never). The server copy then behaves like a cache: the next time someone with
  an edit link opens the project (browser or helper), their copy is uploaded again. Until
  then, someone with only a link and no copy of their own can't open it.
- `--admin-token-file FILE` (or `TEXLOOM_ADMIN_TOKEN`) turns on a read-only page at
  `/admin`. Log in with any user name and the token as password. It shows storage, disk,
  limits, usage per creation code, and per-project size, owner code, state and last
  activity. Names and contents stay encrypted. The install script creates the token in
  `/etc/texloom/admin-token`.
- Importing an arXiv paper's source goes through the server, because arXiv doesn't allow
  browsers to fetch it. The server makes one request at a time, at least 3 s apart, at most
  10 per hour per address, with size caps. `--no-arxiv` turns it off.

## Working on a project as a folder

Use this for Claude Code or any other tool, and to compile with your own TeX installation.

**In Chrome or Edge, nothing to install:** Settings → *Keep a copy in a folder* →
**Choose a folder…**. The tab keeps the folder in sync both ways while it is open (files
changed on both sides take the folder's version; the other stays in History). After a
browser restart, click **📁 Reconnect folder** once.

**With the TeXLoom app** (macOS, Windows, Linux): download it from the
[releases page](https://github.com/jhudsy/texloom/releases/latest), or install it with
`uv tool install 'texloom[app] @ git+https://github.com/jhudsy/texloom'` and run
`texloom app`. It keeps all your project folders in sync from the menu bar or tray:
- **Join project…** takes a link (from the Share dialog or an email), shows the project's
  name and your access, and puts it in a folder you choose (`~/TeXLoom/<name>` by default).
  In the web app, Settings → *Keep a copy in a folder* → **Open in the TeXLoom app** does
  the same in one click; the app asks before joining.
- **Share a folder…** turns an existing folder into a new project.
- Each folder shows whether it is up to date, offline or sending changes, with its recent
  activity, and switches for compiling with your own TeX and for keeping its git repository
  in step. **Leave…** stops syncing a folder (its files stay).
- **Start at login** keeps it running in the menu bar or tray.

The downloads aren't signed with paid certificates yet. On macOS, the first time,
right-click the app → **Open** (or System Settings → Privacy & Security → **Open Anyway**);
on Windows, SmartScreen → **More info** → **Run anyway**. The app is the same helper as
below, with a window: it and `texloom start` share the folder list, and only one of them
syncs a given folder at a time.

**With the `texloom` helper** (any OS, works without a browser, can compile): install it
once with [uv](https://docs.astral.sh/uv/) (or pipx):

```bash
uv tool install git+https://github.com/jhudsy/texloom
```

```bash
texloom clone '<link>' ~/papers/demo      # quote the link: it contains '&'
texloom run ~/papers/demo --compile       # live two-way sync + latexmk
```

Or share an existing folder: `texloom new ~/papers/existing --server https://your.server`.
On Windows, quote paths with spaces: `texloom run "C:\Users\Bob Smith\Desktop\paper"`.
`texloom run` prints the full path of the folder it syncs. (From a source checkout, prefix
the commands with `uv run`.)

**Several folders, and starting at login.** `clone` and `new` add the folder to this
computer's folder list. One `texloom start` then keeps every folder on the list in sync:
each with its own settings (`.texloom/config.json`: compile, git, …), log lines labelled
with the folder's name, and folders added or left while it runs picked up within seconds.
`texloom autostart on` starts it at login (a launchd agent on macOS, a systemd user
service on Linux, a hidden script in the Startup folder on Windows) and writes a log
(`texloom autostart status` says where). A folder is only ever synced by one texloom at a
time: a second `texloom run` on it says so and stops.

While `run` is going, edits to the files are merged character by character with what
collaborators type in their browsers. With `--compile`:
- it runs `latexmk` after changes and shares the PDF with everyone;
- browsers set to **Compile with my own TeX** send their compile requests to it.

| command | |
|---|---|
| `texloom start` | sync every folder on this computer's list, in one process |
| `texloom autostart on\|off\|status` | run `texloom start` at login (macOS, Linux, Windows) |
| `texloom list` / `add <dir>` / `leave <dir>` | the folder list (`clone` and `new` add to it; leaving keeps the files) |
| `texloom new <dir> --server URL [--code C]` | share an existing folder as a new project; prints both links |
| `texloom clone <link> <dir>` | check out a project |
| `texloom run <dir> [--compile] [--shell-restricted \| --shell-escape]` | keep the folder in sync, live |
| `texloom run <dir> --git` | also keep the folder's git repository in step with the project (below) |
| `texloom sync <dir> [--git]` | sync once and exit (manual sync) |
| `texloom storage <dir> server\|relay` | keep an encrypted copy on the server, or nothing |
| `texloom links <dir>` | print the project code and the edit / read-only links |
| `texloom allow <dir>` | review lines that run programs; allow them for `--shell-escape` compiles |

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
- **Unsafe files aren't written:** neither the helper nor the browser's folder sync
  writes files such as `latexmkrc` or `texmf.cnf`, anything in `.git`, `.vscode` or
  `.idea` (however it's capitalised), nor any path outside the folder.

**Running programs.** Some packages need TeX to run programs (shell escape). Choose how
much with `"shell_escape"` in `.texloom/config.json`, or on the command line:
- `false` (default): nothing runs. Browser compiles are always like this.
- `"restricted"` / `--shell-restricted`: only TeX Live's list of safe programs (enough for
  e.g. `epstopdf`, not for minted).
- `true` / `--shell-escape`: anything the document asks for, e.g. minted. Because anyone
  with an edit link writes the document, lines that run programs must be approved first:
  `texloom allow <dir>` lists them (`\write18`, minted, pythontex, Lua `os.execute`, …) and
  allows exactly those. When a line is new or changed, that compile runs in restricted
  mode instead, and the log says which line is waiting. The check sees commands written
  out plainly; macros can hide one, so use `true` only for projects whose editors you trust.

When a collaborator's change writes such a line into your folder, or an `% arara:`
directive or `% !TeX program` comment (which your own LaTeX editor may follow), the helper
and the browser's folder sync point it out once.

### Using TeXLoom with git

A synced folder can also be a git repository: run `git init` in it (or clone your repo
and run `texloom new` in it). The working files sync with collaborators as usual. With
server storage (the default) they needn't be online at the same time: changes wait on
the server until they next open the project.

**What stays out of the project:**
- `.git/` is never synced, and collaborators can't write into it.
- `.texloom/` holds the project link, keys included, and has its own `.gitignore`, so git
  won't commit it and the edit link can't end up on GitHub by accident.
- Build files (`.aux`, `.log`, the compiled PDF, …) aren't synced; add them to your
  `.gitignore` if you don't want them in git either. The `.gitignore` itself is a project
  file and is shared.

**Safe:** `git add`, `commit`, `diff`, `log`, `push`. They only read the working files.

**Changes the project for everyone:** any git command that rewrites the working files:
`git checkout <branch>`, `pull`, `merge`, `rebase`, `stash`, `reset --hard`. The helper
sees the rewritten files as your edits and shares them. (History can undo it.) Stopping
`texloom run` first doesn't help: it picks up the changed files when it starts again.

**Recommended:**
- Treat the synced folder as one branch: commit snapshots there and push them.
- For other branches or experiments, use a separate checkout that isn't synced, e.g.
  `git worktree add ../paper-experiment`, and merge back when you mean to share.
- Pull others' commits into the synced folder only when you want them shared, ideally
  when nobody is typing in the same paragraph.
- Do rebases and conflict resolution in the separate checkout: collaborators' edits can
  arrive in the synced folder mid-operation and look like unexpected changes to git.

**Keeping a GitHub repository in step automatically** (like Overleaf's GitHub sync, but
continuous): run the helper with `--git` on a folder that is also a git clone, on any
computer that stays on (a desktop, a server, a Raspberry Pi).

```bash
texloom run ~/papers/demo --git
```

- **Project → git:** once the project's files have been quiet for 2 minutes, they are
  committed (only project files: no build output, no `.texloom`) and pushed to the
  branch's upstream.
- **git → project:** every minute it fetches. New commits are merged into the folder and
  shared with collaborators.
- **Clashes:** if the same lines changed on both sides, nothing is merged and the project
  stays as it is; the helper prints the commands to combine them in a separate worktree.
  A pushed "resolution" that still contains conflict markers is refused too, so they
  never reach collaborators.
- **Credentials:** git uses your own setup (SSH key or credential helper). The TeXLoom
  server isn't involved, so the project stays end-to-end encrypted.
- **Options** in `.texloom/config.json`: `"git": {"interval": 60, "quiet": 120, "push": true}`.
  `texloom sync <dir> --git` does one round and exits (e.g. from cron).

To set it up from an existing repository: `git clone <repo> ~/papers/demo`, then
`texloom new ~/papers/demo --server https://your.server` and share its links. From an
existing project: `texloom clone '<link>' ~/papers/demo`, then in that folder `git init`,
`git add -A`, `git commit`, `git remote add origin <repo>` and `git push -u origin main`.
(To join an existing project with an existing, different repository, combine them by hand
once.) Lines that ask to run programs, arriving from git, are pointed out as they are for
collaborators' changes.

### Agents (MCP)

`texloom mcp` is an [MCP](https://modelcontextprotocol.io) server, so Claude Code, Claude
Desktop or any MCP client can work on a project directly, without a folder:

```bash
claude mcp add texloom -- texloom mcp ~/papers/demo        # a folder from texloom clone/new
claude mcp add texloom -- texloom mcp '<edit link>'         # or straight from a link
```

The agent appears to everyone as **Claude (agent)** (change with `--name`), with its own
cursor and its lines credited in "who wrote this". Its tools: list, read, search, write and
edit files; **suggest** changes and **comment** (shown in the Review tab for people to
accept or reject); list, reply to and resolve comments; and compile (with local `latexmk`)
or read the latest compile log. Ask it to "suggest" rather than "edit" when you want to
review its changes first.

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
- **Edits are signed.** Writes are signed with the owner's key or a personal key the owner
  granted (the grant travels with each signature), and the server and every peer reject
  unsigned, wrongly signed or revoked changes, so read-only links can't edit.
- **The server can't tamper with content.** Files fetched from it are checked against
  their content hash.

**What isn't**
- **The web app trusts the server that serves it.** A malicious or compromised operator
  could serve modified JavaScript that reads the keys from the link. Use a server you
  trust (or run your own); the folder helper doesn't have this exposure. The server
  sends a strict Content-Security-Policy, and the install script pins the in-browser
  TeX download by checksum.
- **Links can't be un-shared.** Personal edit links can be revoked, but revoking stops
  editing, not reading: use **Move to new keys** to stop reading too. Owner and read-only
  links can only be retired by moving to new keys. Read-only holders see everything,
  including the names and presence of other collaborators.
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
| `texloom/mcp.py` | MCP server (stdio JSON-RPC): tools, presence, comments and suggestions |
| `server/arxiv.py`, `server/admin.py` | arXiv source import proxy; read-only admin page |
| `web/src/main.ts` | start page and project view wiring |
| `web/src/replica.ts` | browser replica: Yjs + IndexedDB, outbox, peer handshake, awareness, blobs |
| `web/src/filetree.ts`, `projectfiles.ts` | folders and files, moves, zip export |
| `web/src/editor.ts`, `latex.ts` | CodeMirror editor; LaTeX completion, shortcuts, reference checks |
| `web/src/output.ts`, `compile.ts`, `pdfview.ts` | compiling (in browser or via the helper), PDF view, log |
| `web/src/settings.ts`, `storagenotice.ts`, `projects.ts`, `layout.ts` | setup and settings, storage warnings, start-page list, panes |
| `web/src/assistant/` | assistant: provider adapters (`llm.ts`), tools (`tools.ts`), chat panel (`panel.ts`) |
| `web/src/history.ts`, `diff.ts` | versions, compare, restore, line authors |
| `web/src/review.ts` | comments and suggested changes |
| `web/src/navigate.ts`, `spell.ts` | search and replace, outline, word count; spell check |
| `web/src/latexlog.ts`, `synctex.ts` | log parsing into problems; SyncTeX |
| `web/src/templates.ts`, `references.ts`, `pasteimage.ts` | templates and import; DOI/arXiv references; pasted images |
| `web/src/sharing.ts`, `crypto.ts` | links, personal links, revocation, moving to new keys |
| `web/src/foldersync.ts` | folder sync from the browser (File System Access API) |
| `web/sw-template.js` | service worker (offline app) |
| `deploy/` | install script (nginx, certbot, systemd), Caddy config, systemd unit |

## Status

Early prototype. Not yet built:
- direct browser-to-browser WebRTC (changes always pass through a relay)
- a desktop app bundling the helper (for now: `uv tool install`, or folder sync in Chrome/Edge)
- tracked changes made by typing in "suggest mode" (suggestions are made from a selection)

## License

AGPL-3.0-or-later. In-browser TeX uses [texlyre-busytex](https://github.com/TeXlyre/texlyre-busytex) (AGPL-3.0).
