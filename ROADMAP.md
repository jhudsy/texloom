# Roadmap

What's planned or considered, roughly in the order it would be done. Each item is a GitHub issue,
which has the details and is where it's discussed; this file is the overview. Known limitations
that are accepted rather than planned are in [DESIGN.md](DESIGN.md#known-limitations--open-questions).

## Next: the server, towards redundancy and scale

How the pieces fit, and why (three ways to run it: one server; two with a standby; several relays), is in
[SCALING.md](SCALING.md). In this order, each building on the one before:

1. **[A warm standby](https://github.com/jhudsy/texloom/issues/12)**: built and checked, switched by hand
   (STANDBY.md; SCALING.md, "Way 2"). Automatic failover is item 4's: the lease between two servers that was
   built first is retired. A cold standby restored nightly from the backups still covers losing both
   (SCALING.md, "Way 1: one server").
2. **[The TeX engine from a CDN](https://github.com/jhudsy/texloom/issues/15)**: most of the traffic, off the relay.
   Parked until monthly traffic nears what the server's hosting includes. The server must stay self-contained, so the
   first answer needs no TeXLoom change: the engine files already carry long cache headers, so a pull-through cache (one that
   fetches from the server on demand, nothing uploaded) in front of the whole site serves them, at the cost of that cache
   seeing all traffic's metadata (addresses, project ids). The other way keeps it to the public engine files: a separate
   host for `/core/` that pulls from the server, opt-in, with the server's copy as the fallback and the browser checking
   each file against the server's list of hashes. The other half,
   compiled PDFs fetched only while someone is looking, is done (`scripts/check_web_pdf.py`).
3. **[A model of how the relay orders each project's changes](https://github.com/jhudsy/texloom/issues/8)**
   (TLA+) across several servers, and of the client-side GitHub lease. The relays' model is done
   (`models/relays`).
4. **[Several relays at once](https://github.com/jhudsy/texloom/issues/12)** (SCALING.md, "Way 3: several
   relays"): any relay serves any connection; live messages (cursors, presence, sync-only projects) pass
   between relays through a bus (Redis); the database is kept available by a standard tool,
   Patroni. Built on the branch `stateless-relays` (FUTURE_ARCHITECTURE.md), merged once it is proven: the bus
   (Valkey with Sentinels), quotas read from the database, creation codes shared through it, gaps and failovers
   handled, a TLA+ model (`models/relays`) and a Docker rehearsal (`scripts/rehearse_relays.py`). Left: a load
   test across relays, a deploy tool, and a rehearsal on real machines.

## Later

1. **[Comments in your own editor](https://github.com/jhudsy/texloom/issues/36)**: `texloom lsp` and
   the TeXLoom comments extension for VS Code (on the Marketplace and Open VSX) are done. Left, if they're used: live updates in VS Code, and a standalone VS Code extension with the
   web app's collaboration.
2. **[A PDF of the changes between two versions](https://github.com/jhudsy/texloom/issues/4)**, like
   `latexdiff`. 2–3 days; builds on the export's merging into one file.
3. **Project list passwords that resist offline guessing better** (no issue yet): today a list
   password's secret is scrypt (N=2^17) of the password, salted with the username, so whoever holds
   a server's data can guess passwords offline, slowed only by scrypt and the strength check.
   Either a memory-hard KDF tuned harder (Argon2id, which `@noble/hashes` and `cryptography` both
   have), as a v2 secret alongside v1, or an OPRF with the server (OPAQUE-style), so guesses need the
   server and can be rate-limited. Needs both clients and the server; 2–4 days.

## Technical debt

Not broken, but costing more the longer it's left (label `tech-debt`), roughly most useful first:

- **[Browser checks for the PDF view, comments from the PDF, the service worker and the arXiv
  download](https://github.com/jhudsy/texloom/issues/21)**: only the git hosts and the Review tab have kept browser checks so far.
- **[Code kept for older apps and data](https://github.com/jhudsy/texloom/issues/27)**: each place is marked and listed
  (`scripts/check_compat.py`, DESIGN.md). Everything up to 0.4.33 is gone except the shim that keeps an app too old to say its
  version (before 0.4.30) from reconnecting every second when refused, which goes once the admin page shows none connect.
  Left: renaming the `github` settings, and noting which app versions wrote to each stored project, so that what remains
  can be retired by evidence rather than judgement.

## Parked

Decided to wait; each says when to revisit it.

- **[Drop the pycrdt sticky-index workarounds](https://github.com/jhudsy/texloom/issues/1)**: pycrdt
  misplaces anchors in non-ASCII text, so the desktop app avoids them there (`texloom/anchors.py`).
  Fixed upstream in yrs 0.28.1; about half a day once a pycrdt release includes it.
- **[LanguageTool grammar checking](https://github.com/jhudsy/texloom/issues/5)**: sends text to a
  third party, so opt-in only; if people ask.
- **[Mendeley](https://github.com/jhudsy/texloom/issues/6)**: needs a server-side secret; exporting a
  `.bib` into a synced folder works today; if people ask.
- **[Keep documents from outgrowing snapshots](https://github.com/jhudsy/texloom/issues/18)**:
  compressed snapshots are written today, about a third the size. Next,
  if needed, a true checkpoint: when real projects' snapshots near 50 MB.
- **[One-click desktop updates](https://github.com/jhudsy/texloom/issues/10)**: once the apps are
  signed.
- **[Direct browser-to-browser sync (WebRTC)](https://github.com/jhudsy/texloom/issues/11)**: small
  benefit for a large cost.
