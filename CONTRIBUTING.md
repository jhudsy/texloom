# Contributing

How to work on TeXLoom: running it from a checkout, where the code is, the checks and CI, releasing the
desktop app, and translations. How it works is in [DESIGN.md](DESIGN.md); running a server is in
[INSTALL.md](INSTALL.md). Found a problem, or have an idea? Please [open an
issue](https://github.com/jhudsy/texloom/issues).

## Running it from a checkout

```bash
uv sync --extra server
(cd web && npm ci)                      # once: the web app's packages
uv run texloom-server --data data       # terminal 1: the server, its database in data/texloom.db
cd web && npm run dev                   # terminal 2 -> http://localhost:5173 (proxies to :8787)
```

To work against PostgreSQL instead, `uv run python scripts/pg.py data/pg -- uv run texloom-server
--data data` runs a database of its own from the folder (it needs PostgreSQL's programs: `brew
install postgresql@17`, `apt install postgresql`; nothing is installed as a service).

## Where the code is

| path | what |
|---|---|
| `server/app.py` | the relay's web application: its addresses, settings, database start and background upkeep (eviction, purges, the usage recount) |
| `server/relay.py` | relay (aiohttp): the WebSocket protocol (connections, catch-up, updates, storage modes), memory limits, passing messages to other relays and filling gaps |
| `server/bus.py` | the bus between relays: in-process, or a Redis-compatible server (Valkey) spoken to directly; its contract |
| `server/metrics.py` | the relay's `/metrics`, in Prometheus's format |
| `server/policy.py` | storage rules: creation codes and allowances, limits, what may be stored and by whom |
| `server/endpoints.py` | the relay's HTTP endpoints: stored files, `/api/info`, project lists, code checks; response headers |
| `server/wire.py` | what clients send: ids, base64, signatures, app versions |
| `server/cli.py` | the server's command line (`texloom-server`): options, creation codes, backups |
| `server/store.py` | what the relay stores: edit logs, projects, which files are stored |
| `server/db.py` | the database under the store: SQLite or PostgreSQL, behind the same few calls; copying from one to the other |
| `server/files.py` | where stored files' content goes: a folder or an S3-compatible bucket |
| `server/lists.py`, `server/texlive.py` | encrypted project lists; TeX Live files on demand for the in-browser engine |
| `server/config.py` | the server's settings: the settings file, `TEXLOOM_*` variables, the command line |
| `texloom/crypto.py` | keys, links, AES-GCM, Ed25519 (mirrors `web/src/crypto.ts`) |
| `texloom/replica.py` | folder helper's replica: pycrdt document, outbox, peer handshake, blobs |
| `texloom/folder.py` | two-way folder ⇄ CRDT sync, path safety, locked-down latexmk |
| `texloom/folderrules.py` | the folder sync's rules: which files are the project's, text, merging, line endings, renames (as `web/src/foldersync.ts`) |
| `texloom/gitsync.py` | `--git`: a synced folder's git repository (all of it, or the folder's part of it) kept in step, taking turns with browsers |
| `texloom/cli.py` | command line |
| `texloom/config.py` | a synced folder's own settings and project link, and the folders on this computer's list |
| `texloom/mcp.py` | MCP server (stdio JSON-RPC): tools, presence, comments and suggestions |
| `texloom/daemon.py`, `registry.py`, `autostart.py` | `texloom start`: one process syncing every folder on this computer's list; that list; starting it at login |
| `texloom/listsync.py`, `textpos.py` | the project list on the server (as `web/src/listsync.ts`); positions in a text, the only place a text is edited by position |
| `texloom/confine.py`, `sharedfolder.py` | keeping a collaborator's document from reading this computer's other files; a folder also shared through a file-sync service |
| `texloom/comments.py`, `lsp.py` | the Review tab's comments for people who work in their own editor: the overview file, and a language server |
| `texloom/app.py`, `app_list.py`, `app_detail.py`, `app_updates.py` | the desktop app's window: menus and actions; the list of projects; the chosen project; updates |
| `texloom/dialogs.py`, `dialogs_share.py`, `dialogs_list.py`, `dialogs_tags.py`, `dialogs_tools.py` | its dialogs: settings and copying; sharing and joining; the project list; tags; AI tools and editors |
| `server/arxiv.py`, `server/admin.py` | arXiv source import proxy; read-only admin page |
| `web/src/main.ts` | entry: the start page or a project, as the address asks |
| `web/src/startpage.ts`, `projectview.ts`, `appinfo.ts` | the start page and its list; a project open in the editor; what both share |
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
| `web/src/merges.ts`, `texloom/merges.py` | notes paragraphs two people changed while apart, to check |
| `web/src/latexlog.ts`, `synctex.ts` | log parsing into problems; SyncTeX |
| `web/src/templates.ts`, `references.ts`, `pasteimage.ts` | templates and import; DOI/arXiv references; pasted images |
| `web/src/zotero.ts`, `github.ts`, `forges.ts`, `dropbox.ts` (and `*ui.ts`) | integrations: a `.bib` file from Zotero; keeping a git repository (GitHub, GitLab, Gitea, Forgejo) or a Dropbox folder in step |
| `web/src/sharing.ts`, `crypto.ts` | links, personal links, revocation, moving to new keys |
| `web/src/foldersync.ts` | folder sync from the browser (File System Access API) |
| `web/sw-template.js` | service worker (offline app) |
| `deploy/` | install script (SQLite or PostgreSQL, nginx, certbot, systemd, settings file), the deploy tool for several machines (`cluster.py`, its example settings, and the doors' health watcher `door-watch.py`; [CLUSTER.md](CLUSTER.md)), a Caddy config, `rebuild-standby.py`, the Docker rehearsals' images (`rehearsal/`), a Grafana dashboard and alert rules (`monitoring/`) |
| `models/` | a TLA+ model of several relays (`models/relays`, whose README says what it checks) |

## Checks

Several things are written twice, once in each app, and must agree exactly. Each pair is checked
against the same cases in `scripts/` (`*-vectors.json`, `folder-rules.json`):

```bash
uv run python scripts/check_listsync.py         # the project list (listsync-vectors.json)
uv run python scripts/check_xr.py               # which documents a compile builds first for \externaldocument (xr-vectors.json)
uv run python scripts/check_packing.py          # compressed snapshots: both apps read the same cases (packing-vectors.json)
uv run python scripts/check_replica.py          # what a replica does with the server's messages (replica-vectors.json, from make_replica_vectors.py)
uv run python scripts/check_folder_rules.py     # the folder syncs' rules: which files sync, text or not, merging, settling, line endings, renames (folder-rules.json)
uv run python scripts/make_folder_rules.py --check   # folder-rules.json is what make_folder_rules.py writes
cd web && npm run checks                        # the web app's side of each (and its own checks, below)
```

The web app's own checks, in `web/` (`npm run checks` runs every `scripts/check-*.mjs`, the ones
above too, a few at once, as CI does):

```bash
npm run check-i18n         # every text has its translations, plural forms and placeholders
npm run check-versions     # saved versions: every one opens exactly, and storing them stays small
npm run check-suggest      # suggest mode: random typing and collaborators' edits keep the view and suggestions in step
npm run check-pdfanchor    # comments from the PDF: selected text is found in its LaTeX source
npm run check-submission   # the arXiv / journal .zip: files followed, comments removed, files merged
npm run check-mcpclient    # the Assistant's MCP client, against a stand-in server: JSON and event-stream replies, sessions, tokens, errors, tool names
npm run check-gitshacache  # the git id cache keeps only the project's current files, and survives blocked storage
npm run check-props        # random texts and edits (Windows line endings, emoji): the editor and the shared text stay identical, no emoji is cut in two, merges and line endings behave
npm run check-folder-model # the browser's folder sync through random steps, on a fake clock and folder
npm run check-zotero       # the Zotero integration (the others above, and this, all run in `npm run checks`)
```

The desktop app and the server, from the top folder, by what they protect. Text handling:

```bash
uv run --with hypothesis python scripts/check_props.py          # random texts and edits: the desktop's merge agrees with the web app's (needs npm ci in web/)
uv run --with mypy python scripts/check_textpos.py              # character, byte and UTF-16 positions can't be mixed up (texloom/textpos.py)
uv run --with hypothesis python scripts/check_folder_model.py   # the desktop's folder sync through random steps: saves, refused writes, restarts, stale copies
uv run python scripts/check_folder_regressions.py               # folder-sync mistakes that must not come back: a deleted file reappearing, a half-saved file shared, a renamed folder's files left behind
uv run python scripts/check_gitsync.py                          # the git bridge (--git) against real git: the whole repository or a folder in it; only the project's files committed, a co-author's push merged and shared, nothing unsafe brought in
```

Security:

```bash
uv run python scripts/check_settings.py                         # settings that run programs live in your own settings folder, never in the synced folder
uv run python scripts/check_confine.py                          # a compile can't carry a file from outside the project into the PDF (needs TeX)
uv run python scripts/check_comment_names.py                    # a comment author can't fake the verified ✓
uv run --extra server python scripts/check_server_abuse.py      # what an outsider could do: revocations come out of the row budget, TeX Live paths stay in the cache
```

The server (the ones marked need PostgreSQL's programs; each starts a throwaway server and database):

```bash
uv run --extra server python scripts/check_store.py             # the database: SQLite and PostgreSQL give the same answers, and copy into each other
uv run --extra server python scripts/check_files.py             # the file store: a folder, and Garage (S3) if installed
uv run --extra server python scripts/check_config.py            # texloom.example.toml lists every server setting, and parses
uv run python scripts/check_server_state.py                     # what each server keeps in memory, and its addresses, each placed for several relays (DESIGN.md → Rules for server state)
uv run --extra server python scripts/check_move_code.py         # moving a project to another creation code
uv run --extra server python scripts/check_restore.py           # a server restored from a backup: an app that had seen later changes misses none
uv run --extra server python scripts/check_metrics.py           # the relay's /metrics for Prometheus: its format, what it counts, no project ids in it
uv run --extra server python scripts/check_backup.py            # backups: the database at one moment while written to, one server of several making it, restored
uv run --extra server python scripts/check_standby.py           # a warm standby promoted (needs PostgreSQL): apps miss none of the changes it never got
uv run python scripts/check_rebuild_standby.py                  # deploy/rebuild-standby.py: the old primary rebuilt as a standby; what it refuses (needs PostgreSQL)
uv run --extra server python scripts/check_groundwork.py        # what several servers on one database need: the usage ledger, the lock, notifications, upkeep once (needs PostgreSQL)
uv run --extra server python scripts/check_relays.py           # several relays on one database and a bus, apps on each (needs PostgreSQL)
uv run --extra server python scripts/check_relays_failover.py  # several relays through a real database failover: a lost change sent again (needs PostgreSQL)
uv run --extra server python scripts/check_bus.py --valkey      # the bus's contract, in-process and a real Valkey (needs Docker for --valkey)
uv run --extra server python scripts/check_resync.py           # the apps' resync and recheck answers
uv run --extra server python scripts/check_db_away.py          # the database away, slow or stalled: changes passed on live, apps asked to wait or try again
uv run python scripts/check_model.py                            # the relays' design in TLA+ (needs Java): relays dying, bus breaks, a database failover; some cases must fail
uv run --extra server python scripts/rehearse_relays.py        # by hand, needs Docker: several relays with real parts (Patroni, Valkey and Sentinels, an S3 store, nginx), each broken in turn
uv run --extra server python scripts/rehearse_deploy.py        # by hand, needs Docker: install.sh and deploy/cluster.py on new Debian machines; a server moved in, machines lost, replaced, retired
```

Folders and comments, end to end:

```bash
uv run --extra server python scripts/check_shared_folder.py     # two computers on one folder shared through a stand-in Dropbox: they take turns, nothing arrives twice
uv run --extra server python scripts/check_comments.py          # a folder's comments overview, texloom comments list/reply/resolve, and texloom lsp
```

Housekeeping:

```bash
uv run --with pyyaml python scripts/check_workflows.py          # every GitHub Actions file parses (one that doesn't is not a failing test: its workflow just stops running)
uv run python scripts/check_quiet.py                            # no error swallowed without a trace (DESIGN.md → Errors deliberately ignored)
uv run python scripts/check_compat.py                           # code kept for older apps, servers and data: listed, markers well formed (DESIGN.md)
uv run --locked python scripts/check_version.py                 # the version is the same everywhere, and uv.lock is up to date
uv run python scripts/check_i18n.py                             # every text has its translations in every language
uv run python scripts/check_bundled_mcp.py texloom              # the MCP server answers over stdin/stdout (CI runs it on the built app too)
uv run python scripts/conncheck.py https://app.texloom.org      # connect to a server the way the desktop app does, saying what happened at each step
```

`conncheck.py` is for someone who can't connect; CI runs it on Windows, macOS and Linux on demand
(Actions → Connection check). The checks against a real server share `scripts/checklib.py`: each
starts its own throwaway server and removes its folder afterwards, unless something failed or
`KEEP=1` is set.

The server's checks each start a throwaway database of their own (`scripts/testdb.py`): an SQLite
file, or with `TEXLOOM_CHECK_DATABASE=postgresql` a PostgreSQL (`scripts/pg.py`, which needs
PostgreSQL's programs). `check_store.py` checks both when PostgreSQL's programs are there.

Sync-only projects end to end: `uv run --extra server python scripts/check_synconly.py` starts its own throwaway
server (which keeps nothing without a creation code) and two copies of `texloom run`, as two
computers, then edits through their folders: every file arriving, edits both ways and at once,
a burst of edits, one computer off for a while, pictures, renames and deletions, and that the
server stored nothing. Each computer plays an operating system for line endings (files saved on
"windows" end lines with \r\n), and every pairing is run: unix/unix, windows/windows, and
windows/unix both ways. With Playwright it also runs the web app in headless Chromium, typing for
real against a Unix and a Windows desktop (`unix/browser`, `windows/browser`); see the script's own
description. With `CHECK_REFUSE_WRITES=0.3`, 30% of the desktops' file writes are refused, as on
Windows when another program has a file open, and everything must still end in step (CI runs
this on Linux too). A few minutes; `--keep` leaves the folders and logs:

```bash
uv run --with playwright python -m playwright install chromium   # once
uv run --extra server --with playwright python scripts/check_synconly.py
```

The web app in a real browser (headless Chromium, through its own screens; build it first). Each
starts its own throwaway server; `scripts/webcheck.py` is what they share, for writing more:

```bash
(cd web && npm run build)
uv run --extra server --with playwright python scripts/check_web_git.py     # keeping a project in step with GitHub, GitLab and Gitea (stand-ins: scripts/fakeforge.py)
uv run --extra server --with playwright python scripts/check_web_review.py  # the Review tab: comments in reading order, several drafts at once, acting on several
uv run --extra server --with playwright python scripts/check_web_pdf.py     # a compiled PDF is downloaded only while the PDF is on screen (or popped out), and only the newest
uv run --extra server --with playwright python scripts/check_web_update.py  # a new version of the app is offered (Reload) to a tab that is already open
uv run --extra server --with playwright python scripts/check_keepalive.py   # a server that stops answering without closing anything is noticed (web app and desktop helper), and connected to again
uv run --extra server --with playwright python scripts/check_web_listsync.py  # the project list: a project opened while the list syncs keeps the time it was opened
uv run --extra server --with playwright python scripts/check_web_listways.py  # the project list's passwords, replacing and deleting it, across the desktop app and the web app
uv run --extra server --with playwright python scripts/check_web_dropbox.py # keeping a project in step with a Dropbox folder (stand-in: scripts/fakedropbox.py)
```

The same checks against a real deployment, such as a staging server tried before production:
set `TEXLOOM_CHECK_SERVER` to its address and, if it asks for creation codes,
`TEXLOOM_CHECK_CODE_FILE` to a file holding one. The browser opens it through a pass-through on
127.0.0.1 (the test hooks are only for pages from localhost); past that it's all the deployment's
own: TLS, the web server in front, the server. They store small test projects and a project list
there, so use a server meant for testing:

```bash
TEXLOOM_CHECK_SERVER=https://staging.example.org TEXLOOM_CHECK_CODE_FILE=~/staging-code \
  uv run --extra server --with playwright python scripts/check_web_review.py
```

The desktop app's windows: `uv run --extra app python scripts/check_app.py` opens the main window
and each dialog the way its menus do (offscreen, any system), and checks a dialog asked for from
another opens over it. On a Mac, `--native` also checks the menu-bar icon's right click and the
Dock, which rely on undocumented AppKit and Qt behaviour; CI runs it on macOS when the app changes
and weekly (`desktop-app.yml`), and before each release build.

`scripts/check_quiet.py` refuses errors swallowed without a trace (DESIGN.md → Errors deliberately
ignored).

What CI runs (`.github/workflows/`):

| workflow | when | what |
|---|---|---|
| `checks.yml` | every push to main and every pull request | the quick checks, as four parallel jobs: desktop, server, web, properties |
| `sync-only.yml` | the desktop's sync or the server changes; nightly; on demand | sync-only projects end to end, on Linux, macOS and a real Windows (a few minutes). A server-only pull request runs on Ubuntu alone |
| `web-app.yml` | the web app or the server changes; nightly; on demand | the web app in a real browser |
| `desktop-app.yml` | the desktop app changes; weekly | the app's windows, and on macOS the menu-bar and Dock fixes |
| `model.yml` | the model changes; weekly | the relays' model (TLA+) |
| `workflows.yml` | a workflow file changes | every workflow file parses |
| `app.yml`, `vscode-extension.yml` | a release tag | the downloadable app builds; the VS Code extension's publication |
| `conncheck.yml` | by hand | connect to a server the way the app does, on three systems |

To see what a relay can take, `uv run --extra server python scripts/loadtest.py all` starts its own throwaway server
and reports latency, CPU and memory for each scenario. Results depend on the machine. `loadtest.py spread spread-room
p2p --relays 3` compares one relay with several sharing a database and a Valkey (Docker), each project's people spread
over them (CLUSTER.md, "Load").

## Releasing the desktop app

```bash
uv run python scripts/release.py X.Y.Z --dry-run   # can it be made? (changes nothing)
uv run python scripts/release.py X.Y.Z             # --no-push: commit and tag, but don't push
```

It refuses unless the version is newer, `main` is clean and level with GitHub, the tag is new
and the quick checks pass. Then it sets the version in `pyproject.toml`, `texloom/__init__.py`
and `uv.lock`, commits "Release X.Y.Z", tags `vX.Y.Z` and pushes both; the tag starts the app
builds (`app.yml`), which make the GitHub release. Last, it writes a stub for the release into
the website's changelog (`../texloom-web/changelog.html`, or `--web`): the entry in the list, the
"Latest" badge moved to it, and the desktop app's commits since the last release as a comment.
The wording is yours; commit and publish the website separately. The app's Help menu opens the
website's help page (`../texloom-web/help.html`, [texloom.org/help.html](https://texloom.org/help.html)): a change to
something it describes updates it in the same go, and each release is a moment to check it for anything out of date. `--no-push` stops short of
pushing. `scripts/check_version.py`, in CI, checks the three version strings agree.

## Translations

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
3. Check: `npm run check-i18n` in `web/` and `uv run python scripts/check_i18n.py`. Both list
   missing or unused text, wrong plural forms and `{{placeholder}}` mismatches for every
   language (name languages to check only those; `--allow-missing` for one in progress).
