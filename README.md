# TeXLoom

Local-first, collaborative LaTeX. Every collaborator has a full copy of the project,
in the browser or as a plain folder on disk, and can keep working offline. A small
server passes **end-to-end encrypted** changes between people and can be told to keep
no copy at all. No accounts: you share a link. (The command-line tools are called
`texloom`.)

- Live editing with other people's cursors (click someone's avatar to jump to them);
  offline edits merge automatically on reconnect. Two files created under the same name while
  apart are both kept (one as `name (conflict).tex`). Installs as an offline-capable app.
- Compile in the browser (TeX Live 2026 in WebAssembly), or with your own TeX installation.
  Errors are listed with clickable locations; SyncTeX jumps between source and PDF
  (the → / ← arrows on the editor/PDF divider, ⌘⌥→ / ⌘⌥←, or double-click / right-click the PDF).
- History without git: automatic and named versions (name any version later, e.g. the one you
  submitted), compare with now, restore a file or everything, and see who wrote each line.
- Comments and suggested changes (tracked changes) that collaborators accept or reject.
- Edit the project as an ordinary folder, so Claude Code, vim or VS Code can work on it
  directly; changes flow both ways, live. Chrome and Edge can sync a folder with nothing
  installed.
- Agents as collaborators: an MCP server (`texloom mcp`) lets Claude Code or any MCP
  client read, edit, comment, suggest and compile, visible to everyone by name and cursor.
  A built-in assistant does the same with Claude, any OpenAI-compatible API, or a local
  model (LM Studio, Ollama).
- LaTeX-aware editor: completion of environments, commands, files, citation keys and
  labels; a preview of the equation under the cursor; a checker for common mistakes, with
  one-click fixes; spell check that skips LaTeX; search and replace across files; an outline with
  word counts; paste or drop images; add references from a DOI or arXiv id.
- In English, French, German, Spanish, Brazilian Portuguese or Simplified Chinese: the browser's language is used, or choose one in Settings (the
  desktop app follows the system language, or the one chosen in its Settings).
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
- ✕ (or Settings → **Remove…**) asks what to remove: **from this browser only** (the
  server's and collaborators' copies stay; the link brings it back), or, for the owner,
  **from the server too** (the server deletes its encrypted copy and stops storing the
  project; collaborators keep their copies and can still work together while online at the
  same time), or **for everyone**: the server deletes its copy and refuses the project from
  then on, so nobody can sync it or open it from a link again. Collaborators' browsers and apps
  keep a read-only copy, with a notice and a download button. This can't be undone. Files in
  folders on your computer are never deleted.

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

**Send links from Share, not the address bar.** In the web app the address bar shows only the
project's id. The keys stay in the browser, so a copied address opens nothing for anyone else (it
says to ask for a link), and only works in the browser that opened the project. Links from Share
work as usual, and old links and bookmarks that contain keys still open.

**Email…** next to a personal or read-only link opens your own email app with an
invitation and the link filled in. The link travels in that email, never through the
TeXLoom server (which must not see its keys), so anyone who can read the email can open
the project. A personal link is shown, and can be emailed, only when you create it.

### Your project list on other devices

The start page lists the projects opened in that browser, and only there. To take the list with
you, choose **Sync this list…**: the list is kept on the server, encrypted, and merged
between every browser and computer you connect. The server can't read it. Removing a project from
the list on one device removes it on the others.

A list is opened by a **sync link** (`https://server/#sync=…`). Keep that link in a password
manager, because it opens every project on the list. You can also add a **passkey**, which your
phone or password manager carries between devices; passkeys need a browser with the WebAuthn PRF
feature and belong to the server's address. Or set a **username and password** in the web app:
both stay on your device, and only a value worked out from them (with scrypt) reaches the server.
Only use a sync link that's yours, since using one adds your projects to that list.

If a link gets out, **Replace the sync link…** moves the list to a new one. The old link stops
working, and so do passkeys and the password, which you then add again. **Delete this list…**
removes it for every device. Passkeys and the password can each be removed in the same dialog.

**A lost list can't be recovered.** The server can't read it, so nobody, including whoever runs the
server, can reset access for you. A forgotten password can only be replaced using the sync link or
a passkey. If you lose the link and every passkey, and forget any password, the list is gone for
good. Your projects aren't deleted, but you'll need each project's own link to open it. A password
is also the one way in that can be guessed, by someone with the server's data, so make it long.

In the desktop app, use **File ▸ Project list…** (also in Settings), which also keeps the list up to date while the app
runs; or, in the web app's list dialog, **Use in the TeXLoom app** sends the list to the app on this
computer, which asks before using it. For the helper, see `texloom projects` below (`use --password`
unlocks with a password). Starting a list counts as creating something on the server, so servers
that require a creation code ask for one. Servers can turn lists off with `--no-project-lists`.

Your **creation code** travels with the list too: the latest one entered in any browser or in the
app is filled in when you make a project elsewhere, such as **New project from a folder…** in the
app. Anyone with your sync link can see it, as they can every project on the list.

### Finding and copying projects

The box above your projects searches their names and tags, and the menu beside it sorts them by
when they were last opened or changed, when they were added, or name. Long lists are split into
pages of 25, 50 or 100, or shown on one page, as you choose. Give a project tags with **+ Tag** beneath it or **Tags** in the editor's header, and click
a tag at the top to show only projects that have it (several tags narrow it further). **Manage
tags…** renames, recolours and deletes them, for every project at once. Tick projects to change their tags or
remove them together, or use the box at the top to tick everything found. Tags are yours
alone: they aren't stored in the project, so collaborators never see them. With a synced list they
follow you to your other devices, encrypted; otherwise they stay in that browser.

**Make a copy** (the copy icon next to a project, or in the editor's header) starts a new project of your own
from the current files and settings, which suits templates. The copy has its own links and none of
the original's history, comments or collaborators, and it counts towards your storage.

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
| **⌘⌥M** / **Ctrl+Alt+M** | comment on the selection |
| **⌘⌥E** / **Ctrl+Alt+E** | suggest mode on or off |
| **⌘⌥→** / **Ctrl+Alt+→** | show the current line in the PDF |
| **⌘⌥←** / **Ctrl+Alt+←** | go to the source of the spot last clicked in the PDF (or the middle of the view); double-clicking or right-clicking the PDF does the same |
| paste or drop an image | adds it to `figs/` and inserts `\includegraphics` (a whole `figure` on an empty line) |
| `\cite{` + a DOI or arXiv id | fetches the BibTeX entry and adds it to your `.bib` |

Images, citation keys and labels that don't exist in the project are underlined, as are
lines with compile errors and common mistakes: environments that aren't closed or are closed
by the wrong `\end`, stray or unclosed braces, `\label` before `\caption` in a figure (its
`\ref` gives the section number), straight quotes, `...` for `\dots`, a space instead of `~`
before `\ref`/`\cite`, `e.g.` followed by a sentence space, `$$…$$`, `sin` for `\sin`,
repeated words, `\bf`-style font commands and `\cite{a}\cite{b}`. Most have a fix in the
hint; Settings turns them off. Spelling is checked with the browser's dictionary (language in
Settings), skipping commands, maths, labels and the preamble. With the cursor in maths
(`$…$`, `\[…\]`, `equation`, `align`, …), a preview shows it typeset by KaTeX, using the
project's `\newcommand`, `\def` and `\DeclareMathOperator` macros (Settings turns it off).
- **Left panel:** **Files**, **Search** (match case, whole words, regular expressions with
  `$1`), and **Outline** (sections in document order through `\input`/`\include`, with
  word counts; the total excludes maths and commands).
- **Right panel:** **PDF**, **Assistant**, **Review** (comments and suggestions: reply,
  resolve, accept, reject) and **History** (versions, compare, restore, "who wrote this").
- **Suggest mode:** **✎ Suggest mode** in the Review tab (⌘⌥E / Ctrl+Alt+E, or right-click → *Suggest changes*) makes what
  you type into suggestions instead of changes, like tracked changes. You edit as usual; each changed
  stretch becomes a suggestion in the Review tab, shown word by word (removed words struck through in
  red, new ones green), for someone to accept or reject. Until then the file, the PDF and everyone
  else's copy stay as they were. ⚙ Settings → *Suggested changes in the editor* also shows them in the
  editor, underlined or in full; by default the editor shows just the text. Undo takes a suggestion back. Search and replace, and
  adding a package for a symbol, suggest too while it's on. It's remembered per project in that
  browser, and a bar across the editor says when it's on. Suggestions are blue in the Review tab and
  comments amber. You can go back to suggestions you typed on an earlier visit, in the same browser, and keep
  editing them (one tab at a time per file). If someone changes the very words you're replacing,
  your suggestion stays and accepting it asks first.
- **Problems:** after compiling, errors and warnings are listed under the PDF with their
  file and line; click one to go there. Biber and BibTeX errors (a missing `.bib`, say) are
  listed too. **Raw log** shows the whole log. **Compile from scratch** deletes LaTeX's
  working files (`.aux`, `.bbl`, …) first: in-browser compiles always start from scratch,
  so it matters when a texloom helper compiles with your own TeX (it keeps them in
  `.texloom/build`).
- **Create project** starts from a template (article, paper with bibliography, Beamer, thesis
  with chapters, letter) or an import: a `.zip` (Overleaf: Menu → Download → Source; a
  GitHub "Download ZIP"; any zipped folder) or an arXiv paper's LaTeX source. **From a folder
  on this computer…** hands over to the [TeXLoom app](#working-on-a-project-as-a-folder), which asks
  for the folder, uploads it and keeps it in sync, with this server and your creation code
  filled in. **New → Import…** in
  the file list does the same for an existing project. From git: clone it and run
  `texloom new` in the folder.
- **New → Reference…** in the file list adds a bibliography entry from a DOI or arXiv id (via doi.org) with a
  readable key such as `vaswani2017attention`, and cites it at the cursor.
- **Offline:** after the first visit the app (and, after one compile, the TeX engine) works
  offline; browsers offer to install it as an app.
- **Files:** the tree has folders. **New** (file, folder, reference, import) and **Upload** work in the selected folder,
  you can drag to move, and dropping files or whole folders from your computer onto a folder uploads them
  there (you're asked first if files would be replaced, or if it's a very large amount).
- **Panes:** drag the dividers between the file list, the editor and the PDF/assistant
  panel; ☰ and ▥ hide the side panes.
- **PDF in its own window:** the window button above the PDF opens it in a separate window, for example
  on another screen. It updates when you compile, double-clicking it jumps to the source, and it
  stays in step if you reload the editor.
- **Word counts:** the Outline tab counts the document and each section; selecting text shows its count
  in the corner of the editor. LaTeX commands, citations, maths and comments aren't counted. Chinese and
  Japanese are counted by the character.
  **Detailed count…** there runs [TeXCount](https://ctan.org/pkg/texcount) on your device for a breakdown of
  text, headers, captions and maths (the desktop app's **Word count…** uses your own TeX installation's copy).
- **Symbols:** the **Ω** tab beside Files, Search and Outline (⌘⌥S / Ctrl+Alt+S from the editor) holds mathematical
  and text symbols. Clicking one types it at the
  cursor, in `$…$` if you aren't already in maths. Hovering shows the command and any package it needs, and
  using it adds that package to your preamble if it's missing.
- **Compiling another file:** **Compile** compiles the open file if it has its own `\documentclass`
  (a figure or a separate document), and otherwise the main file; right-click a file to compile it.
  Only the main file's PDF is shared with collaborators.
- **PDF zoom:** − and + above the PDF change its size, and the percentage goes back to normal
  size; ⌘ or Ctrl + scroll, or pinching on a trackpad, does the same. The zoom is remembered.
- **Pages and printing:** the box above the PDF shows the current page; type a number and press
  Enter to go there. The printer button opens your browser's print dialog, with pages drawn at 200 dpi. For
  a print that keeps the PDF's own text, download it and print that.
- **Compile log:** **Log** shows it under the PDF; drag its top edge to resize.
- **Downloads:** the download button above the PDF, and **Source** (a `.zip`) in the header.

**Phones and tablets.** In small windows the editor and the PDF take turns on screen, the file list
opens over them from ☰, and the other commands are under ⋯. On touch screens a bar above the keyboard
has the characters LaTeX needs.

**Right-click.** On selected text: show it in the PDF, comment, suggest changes (suggest mode), search the project
or ask the assistant. On a LaTeX command: go to a `\ref`'s label or a `\cite`'s bibliography entry,
find the uses of a `\label`, open an `\input` or `\includegraphics` file, or show that line in the
PDF. Anywhere else you get the browser's own menu with its spelling suggestions, and Shift +
right-click always does. The file list has its own menu (preview, insert into the document,
download, rename, delete), and so does the PDF (go to source, download, compile).

**Preview.** Choosing a Markdown file, image or PDF in the file list shows it in the PDF panel, in
place of the compiled document; **Compiled PDF** switches back, as does choosing a `.tex` file or
compiling. Markdown updates as you type, with no compile, and can show the project's own images and
math in `$…$` or `$$…$$`. Raw HTML appears as plain text, and images from other websites aren't loaded.

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

For TeX Live files on demand (every TeX Live package available to in-browser compiles;
otherwise only the bundles in `tex-assets` are), build the package index once and start
the server with it:

```bash
uv run texloom-server --texlive data/texlive --texlive-init   # ~2 s; re-run to refresh
uv run texloom-server --port 8787 --texlive data/texlive       # --texlive-max 3G caps its disk use
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
# <code|default>  <allowance>  [until=YYYY-MM-DD]  [label]
default          0                               # projects created without a code: not stored on the server
A3F7-9KQ2-XM4P   unlimited                       owner
K7P2-QX4M-9H3D   2G         until=2027-11-07     alice
```

- A code's allowance is the total stored across **all** projects created with it.
  `default` covers people without a code. `0` means "not stored on the server": those
  people can still create projects, but only as live, relay-only projects whose copies
  live on their devices.
- Anyone with a project's link can open and edit it; codes only govern **storage of new
  projects**. A code is entered once per browser (**Create project** asks, or ⚙ Settings →
  Creation code…). The folder helper takes `texloom new … --code`, `create_code` in
  `.texloom/config.json`. `TEXLOOM_CODE` is read only when creating something new, so it can't reach
  whichever server a link you open points at.
- The file is re-read when it changes. Deleting a line revokes a code: its projects stay
  stored, but new changes then count against `default`.
- `until=` (optional) is the last day, in UTC, that a code is valid. After that it counts as
  revoked: new changes to its projects aren't stored and it can't store new projects.
  Renewing (changing the date) restores it, with no restart. Projects stay tied to the
  code they were created with, so renew a code rather than issuing a new one.
- `--purge-expired-after DAYS` deletes the stored copies of projects whose code expired
  more than DAYS days ago (checked hourly; skipped while someone has the project open).
  As with idle eviction, the files stay on people's devices and the projects carry on
  relay-only; renewing the code later stores them again on their next sync.
  `--purge-code CODE` does the same at once for one expired or deleted code.
  (`TEXLOOM_PURGE_EXPIRED_AFTER` sets it too, e.g. from a systemd drop-in.)
- Several codes files can be combined: repeat `--codes-file`, or list extra files in
  `TEXLOOM_CODES_FILES` (separated by `:`), for example one kept by hand and one kept by a
  sales service. The first file that sets `default` decides it; `--add-code` writes to the
  first file given; `--renew-code` finds the code in whichever file lists it.

```bash
texloom-server --codes-file /etc/texloom/codes.txt --add-code alice --allowance 2G --months 12   # prints a new code
texloom-server --codes-file /etc/texloom/codes.txt --renew-code K7P2-QX4M-9H3D --months 12          # or --until 2028-01-31, --allowance 10G
texloom-server --codes-file /etc/texloom/codes.txt --list-codes --db /var/lib/texloom/texloom.db   # usage, expiry, deleted codes
texloom-server --codes-file /etc/texloom/codes.txt --purge-code K7P2-QX4M-9H3D --db /var/lib/texloom/texloom.db
```

A renewal with `--months` counts from the later of today and the code's current expiry,
so renewing early loses nothing. `--purge-code` refuses a code that is still valid; run it
with the server stopped, or restart the server afterwards so its storage totals are
current (`--purge-expired-after`, inside the server, needs neither).

The server also refuses to store anything when free disk drops below `--min-free`
(default 2G), which protects other sites on the machine. Space freed inside the
database counts as free, because SQLite reuses it before growing the file. Two more limits are
`--project-max-bytes` (default 200M per project, except for unlimited allowances) and
`--max-bytes` (total). Without `--codes-file`, every project may be stored.

A refused change is never lost:
- it still reaches everyone online at that moment;
- the sender keeps it and retries;
- the editor shows a warning that the project isn't saved on the server.


### Private servers

By default anyone who can reach your server can use it for live projects, though they can't read
anyone else's. To keep it for your own group, add `--private` (or `TEXLOOM_PRIVATE=1`), which
needs a `--codes-file`:

- Only projects created with a valid creation code may connect, even without server storage. The
  `default` line doesn't count.
- People with such a project's link need nothing else. Give a code to whoever creates projects,
  and links to everyone else.
- When a code expires or is removed, its projects stop syncing until it's renewed. Copies on
  people's devices carry on working.
- Projects from before the server went private are re-registered the first time someone opens one
  with a valid code.

The web app and TeX files can still be downloaded by anyone, and `--no-arxiv` turns off importing
through the server. For a server nobody outside can even see, put it behind a VPN such as
Tailscale or WireGuard, or a firewall.

### Idle projects, admin page, arXiv import

- `--evict-idle-days N` (or `TEXLOOM_EVICT_IDLE_DAYS`) drops the stored copy of projects nobody has opened for N days
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
**Choose a folder…**. The tab keeps the folder in sync both ways while it is open. After a
browser restart, click **📁 Reconnect folder** once.

How a folder copy (browser or app) takes in an edit made on disk: only what changed since
the file was last synced is applied, on top of collaborators' edits made meanwhile (lines
changed on both sides take the folder's version; the other stays in History). An older
version reappearing in the file (an editor saving or reloading a stale copy) is not an
edit: the project's text is written back. Sync a folder with one TeXLoom at a time: the
browser stops if it finds the app or `texloom run` keeping the same folder.

**With the TeXLoom app** (macOS, Windows, Linux): download it from the
[releases page](https://github.com/jhudsy/texloom/releases/latest), or install it with
`uv tool install 'texloom[app] @ git+https://github.com/jhudsy/texloom'` and run
`texloom app`. It keeps all your project folders in sync from the menu bar or tray. The first
time, it shows two choices; afterwards they are under **Add project** in the toolbar (and the
**File** menu):
- **New project from a folder…** turns a folder you already have into a project: its files are
  uploaded, encrypted, to `app.texloom.org` (or the server you type) and the folder stays in step.
- **Join a project with a link…** takes a link (from the Share dialog or an email), shows the
  project's name and your access, and puts it in a folder you choose (`~/TeXLoom/<name>` by
  default). In the web app, **Sync to a folder…** next to a project on the start page does the
  same in one click, as does Settings → *Keep a copy in a folder* → **Open in the TeXLoom app**.
  The app asks before joining, and says if the project already has a folder on this computer.
  A project the app opens in the browser (**Open in browser**, or one made from the web app's
  **Create project ▸ From a folder…**) shows **In the TeXLoom app** there instead of **Sync to a
  folder…**, and isn't asked where its files should live.
- **Add a TeXLoom folder from this computer…** brings back one set up earlier (e.g. left, or
  cloned on the command line).

Choosing a folder in the list shows whether it is up to date, offline or sending changes, with
**Open folder**, **Open in browser** and **Share…** (the read-only link, your own edit link, and
for owners **edit links for others**: one per person or group, to copy or email; revoking one
is in the web app's Share dialog). **More** (also the **Project** menu and the right-click menu)
has **Tags…**, **Make a copy…** (a new project, in a new folder, from this one's files),
**Word count…**, **Connect an AI tool via MCP…** (how to connect a tool you already use through
[MCP](#agents-mcp)) and **Leave…**, which stops syncing a folder (its files stay).
- **Compiling:** compile with your own TeX when files change. Once that's on, **Running programs
  when compiling** is *Never* (default), *TeX Live's safe list*, or *Anything the document asks,
  once I allow it*, with **Review lines…** to see and allow them (as `texloom allow`), and
  **Compile from scratch** first deletes LaTeX's working files.
- **Git:** keep the folder's git repository in step (commit, pull, push).
- **Activity** unfolds the folder's recent log.
- The search box finds folders by name or tag; the ⇅ button beside it (or the **View** menu)
  sorts them and filters by tag. Tags show in colour, which **Manage tags…** lets you change.
  Select several (Shift or ⌘/Ctrl-click) to change their **Tags…** or **Leave…** them together.
  Tags are the same ones as in the web app, through your project list.
- **Settings** (TeXLoom ▸ Settings… or ⌘, on a Mac; the toolbar elsewhere): start at login, so
  folders keep syncing; the language, used from the next start (`TEXLOOM_LANG=fr` or `en`
  overrides it); and the project list.
- Closing the window keeps TeXLoom syncing in the menu bar or tray (on a Mac, click its Dock icon
  to bring the window back). On Linux desktops without a tray, closing asks whether to minimise
  instead, since quitting stops syncing.

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
| `texloom projects` / `new --server URL` / `use '<sync link>'` / `off` | your encrypted project list on the server: what's on it (`--links` prints each project's link), start one, use one from elsewhere, stop on this computer |
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
or read the latest compile log.

Asking it to "suggest" rather than "edit" is only a request. To enforce it, add
`--suggest-only`: the agent then has no way to change files directly, and every change is a
suggestion people accept or reject. With a read-only link it can't change the project at all.

```bash
claude mcp add texloom -- texloom mcp --suggest-only ~/papers/demo
```

Each tool is labelled with the standard MCP annotations (read-only, destructive), so clients can
decide when to ask you first. `write_file` and `edit_file` are marked destructive.

**Two things to keep in mind:**

- **`claude mcp add` saves its command in your client's settings, including any link you give it.**
  An edit link there is a full key to the project: anything that can read that file can edit it.
  Prefer a folder (`texloom mcp ~/papers/demo`), or a read-only link if the agent only needs to read.
- **Project files and comments are text other people wrote, and the agent reads them.** Someone
  with access could write instructions aimed at it. Use `--suggest-only` on projects you share with
  people you don't fully trust, and be careful about what else that agent can reach.

## Assistant

The **Assistant** tab next to the PDF is a chat that works on the project like a
collaborator:
- it can list, read and search files, read the comments and suggestions in the Review tab and reply to or resolve them, make targeted edits, create or delete files,
  compile, and read the compile log;
- with **Ask before applying edits** (the default), each change appears word by word, like a
  suggestion, to apply or reject; deleting a file always asks, even with this turned off;
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

Conversations are saved in your browser, per project: **Past conversations** lists them to read, continue (on the provider and
model they used) or delete, and they're removed if you remove the project from this browser.

**Connect an AI tool via MCP…** in the tab shows how to connect a tool you already use through
[MCP](#agents-mcp) instead, such as Claude Code. It runs under your own account with that tool, so it
needs no API key here.

API keys and provider settings are stored only in your browser. They are not part of
the project and never pass through the TeXLoom server: requests go straight from your
browser to the provider.

## Settings

**Project settings** are shared with everyone who has an edit link:
- main file
- engine (pdfLaTeX, XeLaTeX or LuaLaTeX; if none is chosen, documents using ctex, xeCJK or fontspec
  compile with XeLaTeX in the browser, so Chinese works without changing anything)
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
| `web/src/review.ts`, `worddiff.ts` | comments and suggested changes, shown word by word |
| `web/src/suggestmode.ts`, `suggestcore.ts` | suggest mode: typing that becomes suggestions |
| `web/src/navigate.ts`, `spell.ts`, `mathpreview.ts`, `checks.ts` | search and replace, outline, word count; spell check; equation preview; common mistakes |
| `web/src/clashes.ts` | keeps both files when two people create the same name while apart |
| `web/src/latexlog.ts`, `synctex.ts` | log parsing into problems; SyncTeX |
| `web/src/templates.ts`, `references.ts`, `pasteimage.ts` | templates and import; DOI/arXiv references; pasted images |
| `web/src/sharing.ts`, `crypto.ts` | links, personal links, revocation, moving to new keys |
| `web/src/foldersync.ts` | folder sync from the browser (File System Access API) |
| `web/sw-template.js` | service worker (offline app) |
| `deploy/` | install script (nginx, certbot, systemd), Caddy config, systemd unit |


### Checks

The project list has to match exactly between the Python and web clients. Both are checked
against the same data, `scripts/listsync-vectors.json`:

```bash
python scripts/check_listsync.py
cd web && npm run check-listsync
npm run check-versions  # saved versions: every one opens exactly, and storing them stays small
npm run check-suggest   # suggest mode: random typing and collaborators' edits keep the view and suggestions in step
```

To see what a relay can take, `uv run python scripts/loadtest.py all` starts its own throwaway server
and reports latency, CPU and memory for each scenario. Results depend on the machine.

### Translations

Interface text is written in English inside `t('…')` (web) or `_('…')` (desktop app); the
English is the key, and text without a translation shows in English. Each language has
`web/src/locales/<code>/<module>.ts` (one file per source file) and
`texloom/locales/<code>.py`. A count is keyed by its English plural form and holds the
language's forms: `['{{n}} fichier', '{{n}} fichiers']`, or one per plural category for
languages with more (Polish: `{ one, few, many, other }`).

The German, Spanish, Brazilian Portuguese and Simplified Chinese translations were drafted by AI and
haven't yet been read by native speakers; corrections are very welcome.

To add a language (`it`, say):
1. Web: copy `web/src/locales/fr/` to `web/src/locales/it/` and translate the values;
   add `'it'` to `Lang` and `LANGUAGES` in `web/src/i18n.ts` so it can be chosen.
2. Desktop app: copy `texloom/locales/fr.py` to `it.py`, translate it, and add it to
   `CATALOGS` in `texloom/locales/__init__.py`. If its plurals aren't "one for 1, other
   otherwise", add the rule to `PLURAL_RULES` in `texloom/i18n.py`.
3. Check: `npm run check-i18n` in `web/` and `python scripts/check_i18n.py`. Both list
   missing or unused text, wrong plural forms and `{{placeholder}}` mismatches for every
   language (name languages to check only those; `--allow-missing` for one in progress).

## Status

Early prototype. Not yet built:
- direct browser-to-browser WebRTC (changes always pass through a relay)
- a desktop app bundling the helper (for now: `uv tool install`, or folder sync in Chrome/Edge)

## License

AGPL-3.0-or-later. In-browser TeX uses [texlyre-busytex](https://github.com/TeXlyre/texlyre-busytex) (AGPL-3.0); the equation preview uses [KaTeX](https://katex.org) (MIT).

`web/public/vendor/texcount.pl` is TeXcount 3.1.1 by Einar Andreas Rodland, included unmodified under the
LaTeX Project Public License.
