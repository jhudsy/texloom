# TeXLoom

Local-first, collaborative LaTeX. Every collaborator has a full copy of the project,
in the browser or as a plain folder on disk, and can keep working offline. A small
server passes **end-to-end encrypted** changes between people and can be told to keep
no copy at all. No accounts: you share a link. A desktop app and command-line tools (`texloom`) keep a project
in step with an ordinary folder on your computer.

- Live editing with other people's cursors (click someone's avatar to jump to them). Offline
  edits merge automatically on reconnect: two files created under one name while apart are both
  kept (one as `name (conflict).tex`), and a paragraph two people both changed gets a
  **⚠ Check merge** note in the Review tab, since word-by-word merging can leave it reading oddly.
- Compile in the browser (TeX Live 2026 in WebAssembly), or with your own TeX installation.
  Errors are listed with clickable locations; you can jump between a spot in the source and the
  same spot in the PDF (the → / ← arrows on the editor/PDF divider, ⌘⌥→ / ⌘⌥←, or double-click / right-click the PDF).
- History without git: automatic and named versions (name any version later, e.g. the one you
  submitted), compare with now, restore a file or everything, and see who wrote each line.
- Comments and suggested changes (tracked changes) that collaborators accept or reject.
- Edit the project as an ordinary folder, so Claude Code, vim or VS Code can work on it
  directly; changes flow both ways, live. Chrome and Edge can sync a folder with nothing
  installed.
- Keep a bibliography in step with Zotero, and a project in step with a git repository or a
  Dropbox folder.
- Agents as collaborators: an MCP server (`texloom mcp`; MCP is the standard AI agents use to
  call tools) lets Claude Code or any MCP client read, edit, comment, suggest and compile, visible to everyone by name and cursor.
  A built-in assistant does the same with Claude, any OpenAI-compatible API, or a model
  server of your own (Ollama, LM Studio, vLLM).
- LaTeX-aware editor: completion of environments, commands, files, citation keys and
  labels; a preview of the equation under the cursor; a checker for common mistakes, with
  one-click fixes; spell check that skips LaTeX; search and replace across files; an outline with
  word counts; paste or drop images; add references from a DOI or arXiv id.
- In English, French, German, Spanish, Brazilian Portuguese or Simplified Chinese: the browser's language is used, or choose one in Settings (the
  desktop app follows the system language, or the one chosen in its Settings).
- Start from templates, or import a `.zip` (e.g. from Overleaf) or an arXiv paper's source.
- Read-only links, and personal edit links that can be revoked one by one.
- The server is a single Python process, with an SQLite file or PostgreSQL as its database
  ([INSTALL.md](INSTALL.md)), or several on several machines that take over from each other
  ([CLUSTER.md](CLUSTER.md)).

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
[security model](#security-model) before relying on it.

## Using TeXLoom in the browser

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

A project's **⚙ Settings** has tabs: **Project** (main file, engine, bibliography, spelling
language, and removing it: shared with everyone in it), **Compiling** and **Editor** (this device),
**Sync & storage** (syncing on this device; the server's copy and your **creation code**, a code the server's owner gives you, which some servers ask for before they store a new project; a copy in a folder),
**Integrations** (Zotero, git repositories and Dropbox; your accounts are yours, the links are the project's) and **You** (your name
and the interface language, in every project). It opens on the tab you used last.

The start page lists the projects you've opened **in that browser** (your name and the
language are under ⚙ at the top):
- each shows the document's `\title`, who started it and who else you've seen in it, and
  is marked **read-only** if that's all your link allows;
- each has a short **project code** (the same one `texloom links` prints), shown beside the
  name when two projects share a name, and otherwise when you point at the name;
- the folder icon keeps the project in a folder through the TeXLoom app (highlighted once the
  app does); ✎ gives a project a private label that only you see;
- tick boxes, shown when you point at a project, select several to tag or remove together;
- ✕ (or ⚙ Settings → Project → **Remove…**) asks what to remove:
  - **from this browser only**: the server's and collaborators' copies stay, and the link brings
    it back;
  - **from the server too** (owner only): the server deletes its encrypted copy and stops
    storing the project; collaborators keep their copies and can still work together while
    online at the same time;
  - **for everyone** (owner only): the server deletes its copy and refuses the project from then
    on, so nobody can sync it or open it from a link again. Collaborators' browsers and apps keep
    a read-only copy, with a notice and a download button. This can't be undone.

  Files in folders on your computer are never deleted.

The server can't read or recover links, so keep important links somewhere safe; a
password manager works well.

### Sharing and revoking

**Share** shows the links. Whoever created the project holds the **owner link**.

- From it they can make a **personal edit link** for each collaborator (by name). The list
  shows the start of each link's key, so two people with the same name can be told apart.
- They can **revoke** a personal link: the server and everyone's browsers then refuse that
  person's changes, and their app reopens read-only. A revoked person can still *read* new
  changes, because the link holds the encryption key.
- To shut them out completely, **Move to new keys**: the project (with history and comments)
  is copied into a new project with new keys, files are re-encrypted, the old server copy is
  deleted, and you send new links to the people who should keep access.
- The **read-only link** can be given to anyone. Only the owner can switch off server storage.

**Send links from Share, not the address bar.** In the web app the address bar shows only the
project's id. The keys stay in the browser, so a copied address opens nothing for anyone else (it
says to ask for a link), and only works in the browser that opened the project. Links from Share
open as usual, and old links and bookmarks that contain keys still open.

**Email…** next to a personal or read-only link opens your own email app with an
invitation and the link filled in. The link travels in that email, never through the
TeXLoom server (which must not see its keys), so anyone who can read the email can open
the project. A personal link is shown, and can be emailed, only when you create it.

**Move to another code.** A stored project belongs to the code it was first stored with, whoever edits it later. Its owner can
move it to another code with **Move to another code…** (Settings → Sync & storage in the web app, the
Project menu in the desktop app), for example when that code has lapsed, or when the project should
count against someone else's plan. The server checks the new code is valid and has room for the
project; links, collaborators and history stay as they are.

### Your project list on other devices

The start page lists the projects opened in that browser, and only there. To take the list with
you, choose **Sync between your devices…**: the list is kept on the server, encrypted, and merged
between every browser and computer you connect. The server can't read it. Removing a project from
the list on one device removes it on the others.

**Copies on several servers.** Under **Copies on other TeXLoom servers** (web app: Manage sync…;
desktop app: Project list…), tick other servers to keep a copy of the list there too, encrypted like
the first. Every device keeps all the copies up to date, a copy that was offline catches up, and the
list lives on if one of its servers closes. A sync link or a password opens the list on any of these
servers (for a link, change the address in it); a passkey works only on the server where you added it.

A list is opened by a **sync link** (`https://server/#sync=…`). Keep that link in a password
manager, because it opens every project on the list. You can also add a **passkey**, which your
phone or password manager carries between devices; passkeys need a browser with the WebAuthn PRF
feature and belong to the server's address. Or set a **username and password** (web app or desktop app):
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

The desktop app keeps the list too (**File ▸ Project list…**: [its help page](https://texloom.org/help.html#list));
**Use in the TeXLoom app**, in the web app's list dialog, sends the list to it, and it asks before using it.
`texloom projects` does the same from a terminal ([below](#working-on-a-project-as-a-folder); `use --password`
unlocks with a password).

Starting a list counts as creating something on the server, so servers that require a creation code
ask for one. Servers can turn lists off with `--no-project-lists`.

**Projects on other servers.** A list can hold projects from any TeXLoom server, not just the
one keeping it. Paste another server's project link under **Paste a project link…**: the project
opens here and syncs with its own server, straight from this page, and the start page marks it with
that server's name. Only encrypted data goes there, as here; the keys stay in your browser. The
desktop app lists its folders with their own servers too, and below them, under **On your list,
not here**, the projects on your list that aren't on this computer, each with its server, to join
(**View** can hide them). When you know more
than one server, **Create project** (and **Make a copy**, and the desktop app's **New project from a
folder…**) asks which to keep the new project on, starting with the one you used last; **Another
server…** takes any address. To move another server's project to new keys, open it on that server.

Your **creation code** travels with the list too: the latest one entered in any browser or in the
app is filled in when you make a project elsewhere, such as **New project from a folder…** in the
app. Anyone with your sync link can see it, as they can every project on the list. The settings on
the Your projects page (⚙) show it, with how much of its allowance is used, and let you enter or
change it.

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
| paste or drop an image | adds it to the project's image folder (`figs/` if there is none) and inserts `\includegraphics` (a whole `figure` on an empty line) |
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
  `$1`), and **Outline** (sections in document order through `\input`/`\include`, with word
  counts: see **Word counts** below).
- **Right panel:** **PDF**, **Assistant**, **Review** (comments and suggestions: reply,
  resolve, accept, reject) and **History** (versions, compare, restore, "who wrote this").
- **Files:** the tree has folders. **New** (file, folder, reference, import) and **Upload** work in the selected folder,
  you can drag to move, and dropping files or whole folders from your computer onto a folder uploads them
  there (you're asked first if files would be replaced, or if it's a very large amount).
- **Panes:** drag the dividers between the file list, the editor and the PDF/assistant
  panel; ☰ and ▥ hide the side panes.
- **Symbols:** the **Ω** tab beside Files, Search and Outline (⌘⌥S / Ctrl+Alt+S from the editor) holds mathematical
  and text symbols. Clicking one types it at the
  cursor, in `$…$` if you aren't already in maths. Hovering shows the command and any package it needs, and
  using it adds that package to your preamble if it's missing.
- **Word counts:** the Outline tab counts the document and each section; selecting text shows its count
  in the corner of the editor. LaTeX commands, citations, maths and comments aren't counted. Chinese and
  Japanese are counted by the character.
  **Detailed count…** there runs [TeXCount](https://ctan.org/pkg/texcount) on your device for a breakdown of
  text, headers, captions and maths (the desktop app's **Word count…** uses your own TeX installation's copy).
- **Offline:** after the first visit the app (and, after one compile, the TeX engine) works
  offline; browsers offer to install it as an app.
  A tab left open picks up a new version of the app in the background; a notice offers **Reload**
  (it never reloads by itself, and changes not yet sent are kept in the browser).
- **Keyboard shortcuts:** press **?** (outside a text field), or **Keyboard shortcuts** in ⚙ Settings, for the list.

**Phones and tablets.** In small windows the editor and the PDF take turns on screen, the file list
opens over them from ☰, and the other commands are under ⋯. On touch screens a bar above the keyboard
has the characters LaTeX needs.

**Right-click.** On selected text: show it in the PDF, comment, suggest changes (suggest mode), search the project
or ask the assistant. On a LaTeX command: go to a `\ref`'s label or a `\cite`'s bibliography entry,
find the uses of a `\label`, open an `\input` or `\includegraphics` file, or show that line in the
PDF. Anywhere else you get the browser's own menu with its spelling suggestions, and Shift +
right-click always does. The file list has its own menu (preview, insert into the document,
download, rename, delete), and so does the PDF (go to source, download, compile).

### Compiling and the PDF

- **Problems:** after compiling, errors and warnings are listed under the PDF with their
  file and line; click one to go there. Biber and BibTeX errors (a missing `.bib`, say) are
  listed too. **Raw log** shows the whole log under the PDF; drag its top edge to resize. **Compile from scratch** deletes LaTeX's
  working files (`.aux`, `.bbl`, …) first: in-browser compiles always start from scratch,
  so it matters when a texloom helper compiles with your own TeX (it keeps them in
  `.texloom/build`).
- **Compiling another file:** **Compile** compiles the open file if it has its own `\documentclass`
  (a figure or a separate document), and otherwise the main file; right-click a file to compile it.
  Only the main file's PDF is shared with collaborators.
- **PDFs are fetched when you look at them:** a collaborator's (or your folder's) new PDF is downloaded only while this
  browser shows the PDF tab (or has it popped out), and only the newest one. Elsewhere it waits until you open the tab. The
  log is always read, so problems still show in the editor. The desktop app never downloads PDFs: it compiles with your own TeX.
- **PDF in its own window:** **Pop out** above the PDF (or right-click the PDF) opens it in a separate window, for example
  on another screen. It updates when you compile, double-clicking it jumps to the source, and it
  stays in step if you reload the editor.
- **Text and links in the PDF:** its text can be selected and copied, and with `hyperref` its links work: citations,
  references and the table of contents scroll to their place (**↩ Back** returns), and web links open in a new tab.
- **PDF zoom and pages:** − and + above the PDF change its size (⌘ or Ctrl + scroll, or pinching on a
  trackpad, does the same), and the percentage goes back to normal size; it also offers **Fit width**,
  **Fit page** (a whole page in view, kept as the window changes size) and set sizes, down to 10%. The zoom
  is remembered. Once the PDF has been clicked, ← and → turn its pages (and ↑ ↓ too in Fit page); Home and
  End go to the first and last.
- **Pages and printing:** the box above the PDF shows the current page; type a number and press
  Enter to go there. The printer button opens your browser's print dialog, with pages drawn at 200 dpi. For
  a print that keeps the PDF's own text, download it and print that.
- **Downloads:** the download button above the PDF, and **Source** (a `.zip`) in the header.

**Preview.** Choosing a Markdown file, image or PDF in the file list shows it in the PDF panel, in
place of the compiled document; **Compiled PDF** switches back, as does choosing a `.tex` file or
compiling. Markdown updates as you type, with no compile, and can show the project's own images and
math in `$…$` or `$$…$$`. Raw HTML appears as plain text, and images from other websites aren't loaded.

### Comments and suggestions

- **Suggest mode:** **✎ Suggest mode** in the Review tab (⌘⌥E / Ctrl+Alt+E, or right-click → *Suggest changes*) makes what
  you type into suggestions instead of changes, like tracked changes. You edit as usual; each changed
  stretch becomes a suggestion in the Review tab, shown word by word (removed words struck through in
  red, new ones green), for someone to accept or reject. Until then the file, the PDF and everyone
  else's copy stay as they were. ⚙ Settings → Editor → *Suggested changes in the editor* also shows them in the
  editor, underlined or in full; by default the editor shows just the text. Undo takes a suggestion back. Search and replace, and
  adding a package for a symbol, suggest too while it's on. It's remembered per project in that
  browser, and a bar across the editor says when it's on. Suggestions are blue in the Review tab and
  comments amber. You can go back to suggestions you typed on an earlier visit, in the same browser, and keep
  editing them (one tab at a time per file). If someone changes the very words you're replacing,
  your suggestion stays and accepting it asks first.
- **Mentions:** typing `@` in a comment or reply offers the project's collaborators (its personal
  links' names and everyone who has commented). When an open comment or reply mentions you, the
  Review tab shows **@** and how many; click it, or tick **Mentions me**, to see just those. The
  server can't read projects, so a mention is seen when the project is opened, not sent by email.
  An agent can be asked for "the comments addressed to you" (`list_comments` with `mentioning`).
- **Several at once:** **Resolve all…** in the Review tab resolves every open comment. To act on some,
  ⌘/Ctrl-click cards to select them (⇧-click for a run, ⌘/Ctrl+A for all), or select text in the
  editor and press **Select in text** for the comments and suggestions on it; a bar then offers
  Resolve, Accept and Reject for the lot. Each is one change for everyone, with **Undo** right after.
  Accepting several skips any suggestion whose text has changed since, to accept on its own.
- **Comments from the PDF:** select text in the PDF (in the app or a popped-out window), then right-click ▸ **Add a comment…**
  or press C. A box opens beside the text, and the comment goes on those words in the source: SyncTeX gives the
  lines, and the selected words are found there. Where they aren't in the source as such (maths, tables, generated text),
  it goes on the line they come from, and the box says so; a bibliography entry is found in the `.bib` file.
- **In your own editor:** a synced folder keeps the open comments in `.texloom/comments.md`, the desktop app and `texloom comments` list and answer them, and a language server (`texloom lsp`, with a VS Code extension) shows them beside the text: see [Comments in your own editor](#comments-in-your-own-editor).

### Starting and exporting projects

- **Create project** starts from a template (article, paper with bibliography, Beamer, thesis
  with chapters, letter) or an import: a `.zip` (Overleaf: Menu → Download → Source; a
  GitHub "Download ZIP"; any zipped folder) or an arXiv paper's LaTeX source. **From a folder
  on this computer…** hands over to the [TeXLoom app](#working-on-a-project-as-a-folder), which asks
  for the folder, uploads it and keeps it in sync, with this server and your creation code
  filled in. **New → Import…** in
  the file list does the same for an existing project. From git: clone it and run
  `texloom new` in the folder.
- **arXiv / journal** (next to **Source**; under ⋯ in small windows) downloads a `.zip` for arXiv or a
  journal: only the files the main document uses (followed through `\input`, `\include`,
  `\includegraphics`, `\usepackage`, … and `\graphicspath`), with the compiled bibliography
  (`.bbl`, from a compile in the browser) instead of the `.bib`, and without build files.
  **Remove comments** (on by default: arXiv publishes the source) keeps what follows `%` in
  verbatim, `\verb` and URLs; **One .tex file** merges the `\input` and `\include` files into the
  main one, and with BibTeX the bibliography too. When everything is inside the main file's folder,
  that folder is the `.zip`'s top level. Web app only.

### References and Zotero

- **New → Reference…** in the file list adds a bibliography entry with a readable key such as
  `vaswani2017attention`, and cites it at the cursor. Type words from the title and authors to search
  (Crossref for published work, DataCite for arXiv preprints) and pick the paper, or paste a DOI, a
  doi.org or arXiv link, or an arXiv id. The entry comes from doi.org.
- **Zotero:** project ⚙ Settings → **Integrations** → Zotero keeps a `.bib` file in step with a Zotero collection (or
  a whole library, yours or a group's). Connect once with a Zotero API key (made at
  zotero.org/settings/keys, read access is enough): it stays in your browser, and with a project
  list it travels with the list, encrypted, like the creation code; it never enters a project.
  **Link a collection…** picks the collection, BibTeX or BibLaTeX and the file, and fetches it;
  **Refresh** there (or right-click the file → **Refresh from Zotero**) fetches it again, changing
  only what changed. The link is part of the project, so collaborators see that the file is
  generated, by whom and when; anyone whose key can read the collection (say, a shared Zotero
  group) can refresh it. Keys are Zotero's own (`smithtitle2020`). Edit entries in Zotero: a refresh
  replaces hand edits to a linked file.
- **Zotero with Better BibTeX, through a folder:** with the [TeXLoom app](#working-on-a-project-as-a-folder)
  keeping the project in a folder, Zotero's [Better BibTeX](https://retorque.re/zotero-better-bibtex/)
  plugin can do this without the web app: right-click a collection in Zotero → **Export
  Collection…** → *Better BibTeX* (or *Better BibLaTeX*), tick **Keep updated**, and save the file
  into the project's folder. Better BibTeX rewrites it whenever the collection changes, and the app
  syncs it to everyone. Better BibTeX's citation keys are stable and configurable, which some
  people prefer to Zotero's own.

## Running a server

A server is one small Python process, with an SQLite file (nothing to install) or PostgreSQL as
its database. [INSTALL.md](INSTALL.md) covers installing one (a script does it on Ubuntu or
Debian), its settings file, moving from one database to the other, stored files in a folder or an
S3-compatible bucket, creation codes and storage allowances, private servers, backups, a standby switched by
hand, and how far one server goes. [CLUSTER.md](CLUSTER.md) runs it on several machines (relays sharing a
database kept available by Patroni and a message bus, set up by one command), which take over from each other by
themselves.

## Working on a project as a folder

Use this for Claude Code or any other tool, and to compile with your own TeX installation.

**In Chrome or Edge, nothing to install:** ⚙ Settings → Sync & storage → *Keep a copy in a folder* →
**Choose a folder…**. The tab keeps the folder in sync both ways while it is open. After a
browser restart, click **📁 Reconnect folder** once. (Safari and Firefox can't open a folder
from a web page, so they don't offer this: use the app, below.)

How a folder copy (browser or app) takes in an edit made on disk:

- Only what changed since the file was last synced is applied, on top of collaborators' edits made
  meanwhile.
- Lines changed on both sides take the folder's version; the other version stays in History.
- An older version reappearing in the file (an editor saving or reloading a stale copy) is not an
  edit: the project's text is written back.
- Sync a folder with one TeXLoom at a time: the browser stops if it finds the app or `texloom run`
  keeping the same folder.

**With the TeXLoom app** (macOS, Windows, Linux): download it from the
[releases page](https://github.com/jhudsy/texloom/releases/latest), or install it with
`uv tool install 'texloom[app] @ git+https://github.com/jhudsy/texloom'` and run `texloom app`. It keeps all
your project folders in sync from the menu bar or tray. It opens your project list if you already use one in a
browser, turns a folder you have into a project (**New project from a folder…**), or joins one from a link
(**Join a project with a link…**; in the web app, **Sync to a folder…** next to a project does the same in one
click). It can compile with your own TeX when files change, for you and for browsers set to **With my own TeX
installation** (⚙ Settings → Compiling), and keep the folder's git repository in step. [The app's help
page](https://texloom.org/help.html), also under its **Help** menu, walks through it, including opening the
downloads the first time (they aren't signed with paid certificates yet). The app is the same helper as below,
with a window: it and `texloom start` share the folder list, and only one of them syncs a given folder at a time.

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
each with its own settings (compile, git, …: `texloom settings <dir>`), log lines labelled
with the folder's name, and folders added or left while it runs picked up within seconds.
`texloom autostart on` starts it at login (a launchd agent on macOS, a systemd user
service on Linux, a hidden script in the Startup folder on Windows) and writes a log
(`texloom autostart status` says where). A folder is only ever synced by one texloom at a
time: a second `texloom run` on it says so and stops.

While `run` is going, edits to the files are merged character by character with what
collaborators type in their browsers. With `--compile`:
- it runs `latexmk` after changes and shares the PDF with everyone;
- browsers set to **Compile with my own TeX** send their compile requests to it.

| command | what it does |
|---|---|
| `texloom start` | sync every folder on this computer's list, in one process |
| `texloom autostart on\|off\|status` | run `texloom start` at login (macOS, Linux, Windows) |
| `texloom list` / `add <dir>` / `leave <dir>` | the folder list (`clone` and `new` add to it; leaving keeps the files) |
| `texloom projects show` / `projects new --server URL` / `projects use '<sync link>'` / `projects off` | your encrypted project list on the server: what's on it (`--links` prints each project's link), start one, use one from elsewhere, stop on this computer |
| `texloom new <dir> --server URL [--code C]` | share an existing folder as a new project; prints both links |
| `texloom clone <link> <dir>` | check out a project |
| `texloom run <dir> [--compile] [--git] [--shell-restricted \| --shell-escape]` | keep the folder in sync, live; `--git` also keeps the folder's git repository in step with the project (below) |
| `texloom sync <dir> [--git]` | sync once and exit (manual sync) |
| `texloom storage <dir> server\|relay` | keep an encrypted copy on the server, or nothing |
| `texloom links <dir>` | print the project code and the edit / read-only links |
| `texloom allow <dir>` | review lines that run programs; allow them for `--shell-escape` compiles |
| `texloom settings <dir> [KEY=VALUE …]` | this computer's settings for the folder (compile, engine, latexmk_args, …) |

Per-folder options are kept in TeXLoom's own settings folder on this computer, never in the
synced folder (someone sharing the folder through Dropbox or the like could write them there),
and never shared. `texloom settings <dir>` shows them; `texloom settings <dir> KEY=VALUE …`
changes them (values as JSON):

```
compile = false   engine = null   latexmk_args = []   shell_escape = false   confine = true   git = false   label = ""
```

They belong to the folder where it is, and its project: a folder that is moved or renamed
starts over with these defaults. (Upgrading from before 0.4.56: the first run brings each listed
folder's old settings over from `<dir>/.texloom/config.json`, but from a folder that may be shared, such as
one in Dropbox, only `compile`, `engine` and `label`; the log says which must be set again.) The project link and `create_code` stay in `<dir>/.texloom/config.json`, which only you can read.

Collaborators write the document, so compiling is locked down by default:
- **No running programs:** TeX may not run programs (`-no-shell-escape`).
- **No writing outside the project:** TeX's "paranoid" file access; on macOS the sandbox
  (below) also lets a compile write only in the project (not its `.texloom` or `.git`),
  its build folder, TeX's caches and the temporary folders.
- **No reading outside the project:** a compile may read only the project, TeX's own files
  and fonts (installed fonts; in home folders, only the font folders such as
  `~/Library/Fonts`), and not the project's `.texloom` folder (which holds its keys). TeX's
  paranoid mode doesn't ensure this (TeX Live 2026 reads `\input{../../notes}`, absolute
  paths and dot files; LuaLaTeX can read anything), so two more layers do:
  - **A sandbox** hides home folders and mounted volumes, except those places: macOS's
    `sandbox-exec`, or on Linux `bwrap` (bubblewrap) when it is installed and allowed to
    run. Windows has none.
  - **A check, everywhere:** each TeX, BibTeX and biber run records what it read, and the
    `.xdv`/`.dvi` that xdvipdfmx (XeLaTeX) or dvips turn into the PDF is read for specials
    and fonts that name files (`pdf:fstream`, `pdf:image`, `PSfile=`, …). If a compile read
    anything else, or a record was rewritten while it ran (a document can `\openout` its own
    `.fls`), its PDF and log aren't shared with collaborators (they could contain what was
    read) and its working files are deleted.

  What the check can't see: Lua code in LuaLaTeX reading through `loadfile`, `dofile` and
  the like, which TeX doesn't record. The sandbox stops those reads; on Windows, and on Linux
  without bubblewrap, nothing does, so compile LuaLaTeX documents there only for projects
  whose editors you trust.

  `"confine": false` turns both off, for projects whose collaborators you trust.
- **No rc files:** latexmk ignores rc files (`-norc`), because a `latexmkrc` is Perl.
- **Unsafe files aren't written:** neither the helper nor the browser's folder sync
  writes files such as `latexmkrc` or `texmf.cnf`, anything in `.git`, `.vscode` or
  `.idea` (however it's capitalised), nor any path outside the folder.

**Running programs.** Some packages need TeX to run programs (shell escape). Choose how
much with the `shell_escape` setting (in the app, or `texloom settings`), or on the command line:
- `false` (default): nothing runs. Browser compiles are always like this.
- `"restricted"` / `--shell-restricted`: only TeX Live's list of safe programs (enough for
  e.g. `epstopdf`, not for minted).
- `true` / `--shell-escape`: the programs that lines you approve run, e.g. minted's. Because
  anyone with an edit link writes the document, lines that run programs must be approved first:
  `texloom allow <dir>` lists them (`\write18`, minted, pythontex, Lua `os.execute`, …) in
  every file, whatever its name, pictures and other binary files included (TeX can read any
  of them), and allows exactly those. When a line is new or changed, that compile runs in restricted
  mode instead, and the log says which line is waiting. Once they are approved, the compile
  runs in TeX's *restricted* shell escape, with the programs those lines name added to TeX
  Live's safe list (`pygmentize` and `latexminted` for minted, the first word of a
  `\write18{…}`, …). The approval check only sees commands written out plainly; TeX can build
  one from macros (`\csname write\endcsname18`) or from a file it writes while compiling, but
  such a command can then only run those same programs. What can't be prevented: an approved
  program that can itself run anything (a shell, Python, a TeX engine given `-shell-escape`,
  `pygmentize -x` with a lexer file in the project) can be made to by a hidden command too;
  Lua's `os.execute` and programs that aren't named plainly (`\write18{\mycmd}`) don't work
  in this mode; and MiKTeX doesn't take the added programs. So use `true` only for projects
  whose editors you trust.
- `"unrestricted"` (only through `texloom settings`): approved lines with full shell escape,
  for packages that need a shell, such as minted before version 3 (TeX Live 2024 and
  earlier). Macros can hide a command from the approval check, and then run anything.

When a collaborator's change writes such a line into your folder, or an `% arara:`
directive or `% !TeX program` comment (which your own LaTeX editor may follow), the helper
and the browser's folder sync point it out once.

### Dropbox from the web app

Project ⚙ Settings → **Integrations** → Dropbox keeps a project in step with a folder in your
Dropbox, or one shared with you there, with nothing to install:
- **Connect Dropbox…** once: it takes you to Dropbox to allow TeXLoom, then back. The connection
  stays in your browser (with a project list it follows the list, encrypted); TeXLoom's server
  never sees it.
- **Link a Dropbox folder…**: a folder (made if there isn't one). Files only in one of the two end
  up in both; where both have a file that differs, you choose whose version stays.
- **Then, while the project is open:** changes made in Dropbox come in within seconds, merged with
  collaborators' edits; the project's changes go up once it has been quiet for 10 seconds, or
  straight away when you leave the tab (only if Dropbox still has the version they were made on:
  otherwise its changes come in first). Closing the tab before then loses nothing: the changes
  are in the project, and go to Dropbox the next time a browser with Dropbox connected has it open. Clashes,
  and one browser at a time, work as for git repositories, below.
- **A shared Dropbox folder** sits at a different place in each person's Dropbox: a collaborator
  approves the link with where it is in theirs, and TeXLoom checks it's the same folder (by its
  id, `.texloom/folder-id`, which it puts there).
- **With the TeXLoom app too:** if someone's app keeps the same folder in step (Dropbox bringing it
  to their computer), browsers leave it to the app, so changes don't come in twice.
- Dropbox's conflicted copies are never brought in.

For a server of your own: make an app in the [Dropbox App Console](https://www.dropbox.com/developers/apps)
(Scoped access, Full Dropbox; permissions `account_info.read`, `files.metadata.read`,
`files.content.read`, `files.content.write`; redirect URI `https://<your server>/dropbox-auth.html`;
*Allow public clients* on), and set its app key as `dropbox_app_key` in the server's settings. The
app secret isn't needed (the browser signs in with PKCE).

### A folder kept by a file-sync service (Dropbox, iCloud Drive, OneDrive, Google Drive, Syncthing)

A synced folder can live in a folder that a file-sync service keeps, and that folder can be shared
with other people and computers there, some of them running TeXLoom on it too:
- **Only one computer keeps it in step at a time.** Otherwise each change would reach the others
  twice, through TeXLoom and through the service, and the second copy would be shared again as
  if it were a new edit. Computers running TeXLoom on the same folder find each other through
  the project (the folder has an id, `.texloom/folder-id`, which the service copies), and take
  turns: the window says **Standing by** on the others. If the one keeping it in step stops,
  another takes over in about a minute.
- **On another computer**, once the service has brought the folder: in the app, **Add folder**
  (or `texloom add <folder>`). It takes the project link from the first computer's records in the
  folder, and waits until it has the project from the server before it does anything.
- **Each computer keeps its own records** in `.texloom/computers/<its id>/` (older folders are moved there the first time). The service copies everyone's, but each computer only
  uses its own. Like the folder's files, they include the project's edit link: anyone the service
  folder is shared with can open the project.
- **Saved on two computers at once:** where the service has kept one version and set the other
  aside as a "conflicted copy", TeXLoom merges from the version each save was made on, so
  neither edit is lost. The conflicted copy itself (Dropbox's `… (… conflicted copy …)`,
  Syncthing's `….sync-conflict-…`) is never shared; the app points it out, to merge by hand.
- **A computer with only the service** (no TeXLoom) is a collaborator through the folder: its
  edits come in through whichever computer keeps the folder in step. It doesn't see live typing,
  and the service's own rules apply when two people save the same file at once.
- A computer whose folder is shared this way syncs it only while online: offline, it can't know
  whether another computer has taken over.

### Using TeXLoom with git

A synced folder can also be a git repository: run `git init` in it (or clone your repo
and run `texloom new` in it). The working files sync with collaborators as usual. With
server storage (the default) they needn't be online at the same time: changes wait on
the server until they next open the project. There are three ways to use it:

- [by hand](#by-hand-in-the-synced-folder), committing and pushing yourself;
- [automatically](#automatically-from-a-computer-that-stays-on---git), with the helper's `--git`, on a computer that stays on;
- [from the browser](#from-the-browser-github-gitlab-gitea-forgejo), with nothing to install.

#### By hand, in the synced folder

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

#### Automatically, from a computer that stays on (`--git`)

Like Overleaf's GitHub sync, but continuous, and with any host (GitHub, GitLab, Bitbucket, your own): run the
helper with `--git` on a folder that is also a git clone, or a folder inside one, on any computer that stays on
(a desktop, a server, a Raspberry Pi).

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
- **A folder inside a repository:** a paper in a repository that holds much else can be a project of
  its own: make the project from that folder (`papers/2026-loom` in your clone). Only its files are
  committed, and nothing else in the clone: what you have changed or staged elsewhere stays as it
  is. Fetching, merging and pushing take the whole branch, as `git pull` and `git push` do. In the
  browser, the same is a link with "Folder in the repository"; the two take turns.
- **Credentials:** git uses your own setup (SSH key or credential helper). The TeXLoom
  server isn't involved, so the project stays end-to-end encrypted.
- **Options:** `texloom settings <dir> 'git={"interval": 60, "quiet": 120, "push": true}'`.
  `texloom sync <dir> --git` does one round and exits (e.g. from cron).

To set it up from an existing repository, in the app: *Add project ▸ New project from a git repository…*
clones it (or, with a folder's path in it such as `papers/2026-loom`, just that folder, as a sparse
checkout) and makes the project with this turned on; in the web app, *Create project ▸ From a git
repository…* links a new project to it instead. By hand: `git clone <repo> ~/papers/demo`, then
`texloom new ~/papers/demo --server https://your.server` and share its links. From an
existing project: `texloom clone '<link>' ~/papers/demo`, then in that folder `git init`,
`git add -A`, `git commit`, `git remote add origin <repo>` and `git push -u origin main`.
(To join an existing project with an existing, different repository, combine them by hand
once.) Lines that ask to run programs, arriving from git, are pointed out as they are for
collaborators' changes.

#### From the browser (GitHub, GitLab, Gitea, Forgejo)

Nothing to install, nothing left running: project ⚙ Settings → **Integrations** → Git repositories. It works
with **GitHub**, **GitLab** (gitlab.com or your own) and **Gitea or Forgejo** (Codeberg, gitea.com or your own).

- **Connect** once per service with a token. It stays in your browser, and with a project list
  it follows the list, encrypted; it never goes into a project, and it's only ever sent to its
  own service.
  - GitHub: a fine-grained token (github.com → Settings → Developer settings → Personal access
    tokens) for just the repositories you'll use, with *Contents: Read and write*.
  - GitLab: a personal access token with the *api* scope (GitLab has no narrower scope that can
    make commits), or a project access token to limit it to one repository.
  - Gitea or Forgejo: an access token (Settings → Applications) with *repository: read and
    write* and *user: read*. Codeberg and gitea.com allow this from the browser; a server of your
    own needs its admin to turn on CORS (`[cors] ENABLED = true` in its `app.ini`).
- **Link a repository…**: the service, the repository (owner/name, a GitLab group/…/name, or
  its address), the branch (a new one starts from the main branch), and optionally a folder in it
  that holds the project. Files only in the repository come into the project and files only in
  the project go to the repository; where both have a file that differs, you choose whose version
  stays. An empty repository gets the project as its first commits.
- **Then, while the project is open:** new commits come in every minute, merged with
  collaborators' edits (each file three ways); the project's changes go up as one commit once
  it has been quiet for 2 minutes. **Sync now** does both at once.
- **Clashes:** if the repository and the project changed the same lines, nothing comes in, and
  Settings says which files. Make one side match the other, or press **Keep the project's
  version**, which brings in the rest of the repository's changes and sends the project's
  version of the clashing lines.
- **One at a time:** everyone in the project sees the link, and it syncs from the browser of
  whoever has connected that service (or the TeXLoom app, below), one copy at a time, so the same
  commit is never brought in twice. Settings says who it is; if they close the project, another
  takes over within two minutes.
- **The same repository in a folder too:** turn on *Keep this folder's git repository in step*
  (`--git`) for that folder in the TeXLoom app. It then takes turns with browsers. Pulling into
  a synced clone of the linked repository by hand, while a browser syncs it, could bring the
  same changes in twice; the app warns about such a folder.
- **Bitbucket** isn't supported from the browser (its API doesn't say which files changed
  without downloading them all); use the `--git` bridge above, which works with any host.

### Comments in your own editor

A synced folder keeps `.texloom/comments.md`, an overview of the open comments with their file and line
(the ones that mention you are marked), for reading in any editor.

- **The desktop app** shows how many are open and how many mention you, with **Review in browser**, and,
  since it runs in the background, gives a system notification when a new comment mentions you.
- **From a terminal:** `texloom comments DIR` lists them (`--mine`, `--all`); `texloom comments reply DIR ID TEXT`
  and `texloom comments resolve DIR ID` answer them. They work while the app is syncing the folder.
- **Beside the text:** `texloom lsp` is a language server (also `TeXLoom lsp`, in the app). It shows each open
  comment as a diagnostic on its text (a warning when it mentions you), the thread on hover, and **Resolve** and
  **Open the Review tab** as code actions. It keeps its own copy of the project, so new comments appear while you
  work, and it follows the quoted text when the buffer has unsaved edits. Set it up in:
  - **VS Code:** the **TeXLoom comments** extension ([Marketplace](https://marketplace.visualstudio.com/items?itemName=texloom.texloom),
    [Open VSX](https://open-vsx.org/extension/texloom/texloom) for VSCodium and similar; source in `editors/vscode`)
    starts it and shows native comment threads with reply and resolve, and starts new comments from the + beside a
    line or **Add Comment** on a selection.
  - **Neovim** (0.11 or later): `vim.lsp.config('texloom', { cmd = { 'texloom', 'lsp' }, filetypes = { 'tex', 'plaintex' },
    root_markers = { '.texloom' } })`, then `vim.lsp.enable('texloom')`.
  - **Emacs** with lsp-mode: `(lsp-register-client (make-lsp-client :new-connection
    (lsp-stdio-connection '("texloom" "lsp")) :major-modes '(latex-mode) :server-id 'texloom :add-on? t))`
    (eglot runs one server per buffer, so use lsp-mode beside texlab).
  - **Helix:** a `[language-server.texloom]` with `command = "texloom", args = ["lsp"]`, added to the `latex`
    language's `language-servers`.
  - **Zed, Sublime Text** (LSP package) **and Kate** have a "custom server" setting that takes the same command.
- **Show comments in your editor…** (the app's File menu, beside Use an AI agent on your computer) shows the right
  command and snippet for each editor. The app's Settings has a switch for each of the two servers (MCP for AI
  tools, and this one); when one is off, the command refuses to start on that computer.
- **Replying and starting a comment** need an editor that can ask for text, as the VS Code extension does (the
  server's `texloom.reply` and `texloom.addComment` commands). Elsewhere, use `texloom comments reply`, or the web app.

### Agents (MCP)

`texloom mcp` is an [MCP](https://modelcontextprotocol.io) server, so Claude Code, Claude
Desktop or any MCP client can work on a project directly, without a folder:

```bash
claude mcp add texloom -- texloom mcp ~/papers/demo        # a folder from texloom clone/new
claude mcp add texloom -- texloom mcp '<edit link>'         # or straight from a link
```

With the downloadable app there's nothing more to install: the binary inside it runs the server
too (`/Applications/TeXLoom.app/Contents/MacOS/TeXLoom mcp …` on a Mac, `TeXLoom.exe mcp …` in the
app's folder on Windows). **Use an AI agent on your computer…** in the app gives the lines with its path.

The agent appears to everyone as **Claude (agent)** (change with `--name`), with its own
cursor and its lines credited in "who wrote this". Its tools: list, read, search, write and
edit files; **suggest** changes and **comment** (shown in the Review tab for people to
accept or reject); list, reply to and resolve comments; and compile (with local `latexmk`)
or read the latest compile log.

Its list of comments marks each author **[verified]** when a personal link's signature vouches for
the name, or **[unverified]** when it's a name anyone with the shared edit link could have typed, so
"handle the comments from Alice" can't be steered by someone typing "Alice". A long list comes in
parts, and can be narrowed to one file. The Assistant tab's list works the same way.

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
  suggestion, to apply or reject, and so does each comment, reply or resolve it would make;
  deleting a file always asks, even with this turned off;
- every request's edits can be undone in one click, and they reach collaborators like
  anyone else's.

### Providers

Set providers up under ⚙ (a key, or a server of your own); the tab's picker then offers the
models of every provider that's set up, grouped by provider, and choosing one switches to it.
The models are the provider's own list, asked for when it's set up and whenever the panel opens;
⟳ asks again.
- **Anthropic (Claude):** uses the official SDK from the browser. Effort is adjustable, thinking summaries are shown, and if a safety
  classifier declines a request it is retried on Anthropic's recommended fallback model.
- **OpenAI, OpenRouter.**
- **Self-hosted:** Ollama, LM Studio, vLLM, llama.cpp, LiteLLM or any server with the OpenAI API, on
  this computer or another. With no address, it finds Ollama or LM Studio on this computer. The
  server must allow this site (CORS): `lms server start --cors` for LM Studio, `OLLAMA_ORIGINS` for
  Ollama. Browsers block plain http to another machine from an https page, so a server elsewhere
  needs an https address, or an SSH tunnel (`ssh -L 11434:localhost:11434 your-server`) with the
  address left empty. Use a model with tool calling (for example a Qwen 3.x model).

### Conversations and your instructions

Conversations are saved in your browser, per project: **Past conversations** lists them to read, continue (on the provider and
model they used) or delete, and they're removed if you remove the project from this browser. To keep none at all (on a shared
computer, say), untick **Keep past conversations in this browser** there; it offers to delete those already kept, for this
project or every project.

**Your instructions**, in ⚙, are standing instructions of your own ("write in British English", "use `\cref`"):
they're sent with the assistant's own on every request, to every model and provider, and are never summarised away.
They're kept only in your browser. **What the assistant is sent…** next to them shows everything sent besides the
conversation itself: the assistant's instructions with yours, the tools it can use, the context block in front of
each message, and roughly how many tokens they and the current conversation take.

### Long conversations

Each request sends the whole conversation, so a long one would eventually outgrow what the model can take in at
once (its *context*). Before that happens, at 70% of the context (or of 200,000 tokens, if that's less), the
assistant asks the model to summarise the earlier messages and carries on from the summary: "Earlier messages
summarised to make room" shows what it kept, and the messages themselves stay on screen. If a request is too long
anyway, it makes room the same way and tries once more. The context size comes from Anthropic, LM Studio (as the
model is loaded), Ollama or OpenRouter; you can also enter it under ⚙ **Context window**. Local models are often
loaded with a small context: 16,000 tokens or more leaves room to work, since the assistant's own instructions take
a few thousand. A model that thinks before answering can take a few minutes over a summary.

### More tools and more context

**Tools for the Assistant**, under ⚙, adds tools from other MCP servers for it to use beside its own: a
literature search, a reference manager, your lab's own tools. Give the server's address (https, or http on this
computer) and a token if it needs one, and **Test** lists its tools. The requests go straight from your browser to
that server, so only servers on the web that allow requests from the page (CORS) work; servers that run as local
programs (stdio) can't be reached from a page. The servers and their tokens are kept only in your browser, never
in the project, so a collaborator's Assistant uses only the servers they added. Their tools act outside the
project, and what they return could contain instructions aimed at the assistant: while **Ask before applying
edits** is on, each call shows the tool and its arguments and waits for **Allow**, unless you mark that server as
**Trusted**. Its tools are named after the server, so they can't stand in for the built-in ones, and
**What the assistant is sent…** lists them with their size.

**Use an AI agent on your computer…** in the tab shows how to connect a tool you already use through
[MCP](#agents-mcp) instead, such as Claude Code. It runs under your own account with that tool, so it
needs no API key here. It's separate from the tab: you talk to the tool in its own window, and its work
shows up in the project under its name.

To point the assistant at more than the open file, **+ Files** adds files (each named, for it to read
once per conversation, with its size in tokens) and **+ Selection** adds the selected passage (quoted once).
It can also find and read any file by itself when a question needs it.

While another tab is open, the Assistant tab shows a pulsing dot as the assistant works, and glows once it
has answered or is waiting for you to apply or reject a change (the browser tab's title gets a dot too).

API keys and provider settings are stored only in your browser. They are not part of
the project and never pass through the TeXLoom server: requests go straight from your
browser to the provider.

## Settings

A project's **⚙ Settings** are described under [Using TeXLoom in the browser](#using-texloom-in-the-browser):
the project's main file, engine and bibliography tool are shared with everyone who has an edit link, and
the rest (compiling, sync, your name and creation code) apply to you. If no engine is chosen, a document
using ctex, xeCJK or fontspec compiles with XeLaTeX in the browser, so Chinese works without changing
anything. The in-browser TeX engine is cached once per browser and shared by all projects (about 125 MB up
front; larger package sets load only if a document needs them), and can be removed under Compiling.

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

Running it from a checkout, a map of the code, the checks and CI, releasing the desktop app and adding a
translation are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Status

Early prototype. What's planned or considered next, with links to the issues, is in [ROADMAP.md](ROADMAP.md).

## License

AGPL-3.0-or-later. In-browser TeX uses [texlyre-busytex](https://github.com/TeXlyre/texlyre-busytex) (AGPL-3.0); the equation preview uses [KaTeX](https://katex.org) (MIT).
Everything the web app bundles, with its licence, is listed at `/licenses.txt` on every server
(made by the build; linked at the foot of the start page and in Settings), and the desktop app's
under **Help ▸ About TeXLoom ▸ Licences…** (`texloom/assets/third-party.txt`, made by
`scripts/licences.py` when the app is built). The TeX engine is patched at deploy time
(`deploy/patch-busytex.py`); those changes are part of this repository, as the AGPL asks.

`web/public/vendor/texcount.pl` is TeXcount 3.1.1 by Einar Andreas Rodland, included unmodified under the
LaTeX Project Public License.
