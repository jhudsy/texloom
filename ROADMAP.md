# Roadmap

What's planned or considered, roughly in the order it would be done. Each item is a GitHub issue,
which has the details and is where it's discussed; this file is the overview. Known limitations
that are accepted rather than planned are in [DESIGN.md](DESIGN.md#known-limitations--open-questions).

## Next: the server, towards redundancy and scale

In this order, each building on the one before:

1. **[Relay throughput](https://github.com/jhudsy/texloom/issues/14)**: a writer thread for SQLite
   that groups commits, so edits and other writes don't wait on each other. 1–2 days. (Its other
   half, stored files outside the database, is done: see #9.)
2. **[A warm standby](https://github.com/jhudsy/texloom/issues/12)**: the database streamed to the
   bucket with Litestream, and a second server ready to take over. 1–2 days, no change to the
   relay's code. Needs the bucket in production (#9) first.
3. **[The TeX engine from a CDN, PDFs only when someone is looking](https://github.com/jhudsy/texloom/issues/15)**:
   most of the traffic, off the relay. About 2 days.
4. **[A model of how the relay orders each project's changes](https://github.com/jhudsy/texloom/issues/8)**
   (TLA+, with the GitHub lease), before several servers run at once. 2–3 days.
5. **[Several servers at once](https://github.com/jhudsy/texloom/issues/12)**: each project routed
   to one server, or the log in a shared database (Postgres). 1–2 weeks.

Done: **[stored files in a folder or an S3-compatible bucket](https://github.com/jhudsy/texloom/issues/9)**
(Garage, RustFS, R2, B2, S3), instead of inside SQLite. Production still uses a folder until a
bucket is chosen.

## Later

1. **[Tell collaborators what's new](https://github.com/jhudsy/texloom/issues/3)**: unread counts,
   changes since your last visit, `@name` mentions. 2–4 days; only while TeXLoom is open, since the
   server can't read projects.
2. **[A PDF of the changes between two versions](https://github.com/jhudsy/texloom/issues/4)**, like
   `latexdiff`. 2–3 days; builds on the export's merging into one file.

## Testing

- The **[TLA+ model](https://github.com/jhudsy/texloom/issues/8)** of the GitHub lease and the
  server protocol is step 4 of Next.

What's checked already: README → Checks, and DESIGN.md → Guarding text handling (including the
folder sync's model-based checks, [#7](https://github.com/jhudsy/texloom/issues/7), done).

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
  compressed snapshots, then if needed a true checkpoint; when real projects near 12 MB.
- **[One-click desktop updates](https://github.com/jhudsy/texloom/issues/10)**: once the apps are
  signed.
- **[Direct browser-to-browser sync (WebRTC)](https://github.com/jhudsy/texloom/issues/11)**: small
  benefit for a large cost.
