# Roadmap

What's planned or considered, roughly in the order it would be done. Each item is a GitHub issue,
which has the details and is where it's discussed; this file is the overview. Known limitations
that are accepted rather than planned are in [DESIGN.md](DESIGN.md#known-limitations--open-questions).

## Next: the server, towards redundancy and scale

How the pieces fit, and why (a bucket for files, one primary database, a lease as the tie-breaker),
is in [SCALING.md](SCALING.md). In this order, each building on the one before:

1. **[A warm standby](https://github.com/jhudsy/texloom/issues/12)**: PostgreSQL streaming to a
   second server (or a hosted PostgreSQL), stored files in a bucket, and a written switch-over; then
   automatic failover using a lease in the bucket. About 2 days for the standby; no change to the
   relay's code. Until then, a cold standby restored nightly from the backups covers
   losing the server (SCALING.md, "Today: a cold standby from the backups").
2. **[The TeX engine from a CDN, PDFs only when someone is looking](https://github.com/jhudsy/texloom/issues/15)**:
   most of the traffic, off the relay. About 2 days.
3. **[A model of how the relay orders each project's changes](https://github.com/jhudsy/texloom/issues/8)**
   (TLA+, with the GitHub lease), before several servers run at once. 2–3 days.
4. **[Several servers at once](https://github.com/jhudsy/texloom/issues/12)** (on PostgreSQL), each
   project's connections all sent to one server (SCALING.md, "Several relays: route by project"):
   totals across projects (an owner's usage, the server's total) into the database, servers telling
   each other when one changes a project another holds, and maintenance run once. About 3–4 days.
   Until then, new code keeps to "Keeping several servers possible" there.

## Later

1. **[Comments in your own editor](https://github.com/jhudsy/texloom/issues/36)**: `texloom lsp` and
   the TeXLoom comments extension for VS Code (on the Marketplace and Open VSX since 2026-11-24) are
   done. Left, if they're used: live updates in VS Code, and a standalone VS Code extension with the
   web app's collaboration.
2. **[A PDF of the changes between two versions](https://github.com/jhudsy/texloom/issues/4)**, like
   `latexdiff`. 2–3 days; builds on the export's merging into one file.

## Testing

The **[TLA+ model](https://github.com/jhudsy/texloom/issues/8)** of the GitHub lease and the server
protocol is step 3 of Next. What's checked already: README → Checks, and DESIGN.md → Guarding text
handling.

## Technical debt

Not broken, but costing more the longer it's left (label `tech-debt`), roughly most useful first:

- **[Browser checks for the PDF view, comments from the PDF, the service worker and the arXiv
  download](https://github.com/jhudsy/texloom/issues/21)**: only the git hosts and the Review tab have kept browser checks so far.
- **[Code kept for older apps and data](https://github.com/jhudsy/texloom/issues/27)**: each place is marked and listed
  (`scripts/check_compat.py`, DESIGN.md). Left: removing the batch the server's minimum version allows, converting
  list entries without their server, renaming the `github` settings, and noting which app versions wrote to each stored project.

## Parked

Decided to wait; each says when to revisit it.

- **[Drop the pycrdt sticky-index workarounds](https://github.com/jhudsy/texloom/issues/1)**: pycrdt
  misplaces anchors in non-ASCII text, so the desktop app avoids them there (`texloom/anchors.py`).
  Fixed upstream in yrs 0.28.1; about half a day once a pycrdt release includes it.
- **[LanguageTool grammar checking](https://github.com/jhudsy/texloom/issues/5)**: sends text to a
  third party, so opt-in only; if people ask.
- **[Mendeley](https://github.com/jhudsy/texloom/issues/6)**: needs a server-side secret; exporting a
  `.bib` into a synced folder works today; if people ask.
- **[Sync-only projects ask one peer for a file](https://github.com/jhudsy/texloom/issues/16)**, not
  every peer; when relay traffic is high with few people connected.
- **[Keep documents from outgrowing snapshots](https://github.com/jhudsy/texloom/issues/18)**:
  compressed snapshots are written since 0.4.36 (read since 0.4.31), about a third the size. Next,
  if needed, a true checkpoint: when real projects' snapshots near 50 MB.
- **[One-click desktop updates](https://github.com/jhudsy/texloom/issues/10)**: once the apps are
  signed.
- **[Direct browser-to-browser sync (WebRTC)](https://github.com/jhudsy/texloom/issues/11)**: small
  benefit for a large cost.
