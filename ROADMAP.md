# Roadmap

What's planned or considered, roughly in the order it would be done. Each item is a GitHub issue,
which has the details and is where it's discussed; this file is the overview. Known limitations
that are accepted rather than planned are in [DESIGN.md](DESIGN.md#known-limitations--open-questions).

## Next: the server, towards redundancy and scale

How the server runs as one process or as several relays, and why, is in [DESIGN.md](DESIGN.md#several-relays);
running it on several machines, in [CLUSTER.md](CLUSTER.md). A standby switched by hand is built
([INSTALL.md](INSTALL.md#a-standby-switched-by-hand)).

1. **[The TeX engine from a CDN](https://github.com/jhudsy/texloom/issues/15)**: most of the traffic, off the relay.
   Parked until monthly traffic nears what the server's hosting includes. The server must stay self-contained, so the
   first answer needs no TeXLoom change: the engine files already carry long cache headers, so a pull-through cache (one that
   fetches from the server on demand, nothing uploaded) in front of the whole site serves them, at the cost of that cache
   seeing all traffic's metadata (addresses, project ids). The other way keeps it to the public engine files: a separate
   host for `/core/` that pulls from the server, opt-in, with the server's copy as the fallback and the browser checking
   each file against the server's list of hashes. The other half,
   compiled PDFs fetched only while someone is looking, is done (`scripts/check_web_pdf.py`).
2. **[A model of the client-side GitHub lease](https://github.com/jhudsy/texloom/issues/8)** (TLA+). The model of
   how several relays order each project's changes is done (`models/relays`).
3. **[Several relays at once](https://github.com/jhudsy/texloom/issues/12)**: any relay serves any connection; live
   messages (cursors, presence, sync-only projects) pass between relays through a bus (Valkey, with Sentinels); the
   database is kept available by Patroni. Built on the branch `stateless-relays`, merged once it is proven: the bus,
   quotas read from the database, creation codes kept in it, gaps and failovers handled, a TLA+ model
   (`models/relays`), a Docker rehearsal (`scripts/rehearse_relays.py`), a load test across relays
   (`scripts/loadtest.py --relays 3`), the deploy tool (`deploy/cluster.py`, CLUSTER.md, rehearsed by
   `scripts/rehearse_deploy.py`) with a certificate on every door and a server moved in by copying it from a machine
   that stays outside, optional monitoring (the relay's `/metrics`, Prometheus and Grafana), and hourly backups that
   work the same on one server and on several (`texloom-server --backup`, `deploy/texloom-backup`). Left: a command
   that changes every secret (`rotate`, so that a lost machine's passwords stop working), a refactor and
   simplification of the code (one way where one server and several differ needlessly: install.sh's certificate, the
   codes files), a security audit, and a rehearsal on real machines (a few hourly-billed servers in two datacentres,
   for what Docker can't show: real networks and clocks, TLS, systemd, a provider's own failures); then the merge.

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
4. **Relays added and replaced by themselves** (no issue yet): from load (connections per relay, CPU) and health (a
   relay unwell for minutes is replaced), through a provider's API. Relays first, since they're the tier that grows
   with people; today `deploy/cluster.py` adds and retires machines when told.

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
