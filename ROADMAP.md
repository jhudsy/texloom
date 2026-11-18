# Roadmap

What's planned or considered, roughly in the order it would be done. Each item is a GitHub issue,
which has the details and is where it's discussed; this file is the overview. Known limitations
that are accepted rather than planned are in [DESIGN.md](DESIGN.md#known-limitations--open-questions).

## Next: the server, towards redundancy and scale

In this order, each building on the one before:

1. **[A warm standby](https://github.com/jhudsy/texloom/issues/12)**: PostgreSQL streaming to a
   second server (or a hosted PostgreSQL), stored files in a bucket, and a written switch-over. About
   2 days; no change to the relay's code.
2. **[The TeX engine from a CDN, PDFs only when someone is looking](https://github.com/jhudsy/texloom/issues/15)**:
   most of the traffic, off the relay. About 2 days.
3. **[A model of how the relay orders each project's changes](https://github.com/jhudsy/texloom/issues/8)**
   (TLA+, with the GitHub lease), before several servers run at once. 2–3 days.
4. **[Several servers at once](https://github.com/jhudsy/texloom/issues/12)**: the database already
   numbers each project's changes correctly with several writers. Left: changes reaching people
   connected to another server (PostgreSQL LISTEN/NOTIFY), sharing what each server keeps in memory
   (usage, deletions, storage modes, revocations), and running maintenance once. About a week.

## Later

1. **[Tell collaborators what's new](https://github.com/jhudsy/texloom/issues/3)**: unread counts,
   changes since your last visit, `@name` mentions. 2–4 days; only while TeXLoom is open, since the
   server can't read projects.
2. **[A PDF of the changes between two versions](https://github.com/jhudsy/texloom/issues/4)**, like
   `latexdiff`. 2–3 days; builds on the export's merging into one file.

## Testing

The **[TLA+ model](https://github.com/jhudsy/texloom/issues/8)** of the GitHub lease and the server
protocol is step 3 of Next. What's checked already: README → Checks, and DESIGN.md → Guarding text
handling.

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
