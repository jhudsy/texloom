# Roadmap

What's planned or considered, roughly in the order it would be done. Each item is a GitHub issue,
which has the details and is where it's discussed; this file is the overview. Known limitations
that are accepted rather than planned are in [DESIGN.md](DESIGN.md#known-limitations--open-questions).

## Next

Nothing scheduled: the next item comes from Later.

## Later

1. **[Tell collaborators what's new](https://github.com/jhudsy/texloom/issues/3)**: unread counts,
   changes since your last visit, `@name` mentions. 2–4 days; only while TeXLoom is open, since the
   server can't read projects.
2. **[A PDF of the changes between two versions](https://github.com/jhudsy/texloom/issues/4)**, like
   `latexdiff`. 2–3 days; builds on the export's merging into one file.

## Testing

3. **[TLA+ model of the GitHub lease](https://github.com/jhudsy/texloom/issues/8)**, and later the
   server protocol. 2–3 days each; once several people use GitHub sync.

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
- **[Relay throughput](https://github.com/jhudsy/texloom/issues/14)**: a writer thread for SQLite,
  and stored files outside the database; when the relay nears a full core, typing lags, or the
  database grows to several GB.
- **[Less bandwidth](https://github.com/jhudsy/texloom/issues/15)**: the TeX engine from a CDN, and
  compiled PDFs fetched only when someone is looking; when traffic nears what hosting includes.
- **[Sync-only projects ask one peer for a file](https://github.com/jhudsy/texloom/issues/16)**, not
  every peer; when relay traffic is high with few people connected.
- **[Server files in object storage](https://github.com/jhudsy/texloom/issues/9)** (S3, R2): when the
  server's disk gets tight.
- **[Keep documents from outgrowing snapshots](https://github.com/jhudsy/texloom/issues/18)**:
  compressed snapshots, then if needed a true checkpoint; when real projects near 12 MB.
- **[A fault-tolerant, then load-balanced relay](https://github.com/jhudsy/texloom/issues/12)**: first
  a warm standby (the database streamed with Litestream, a second server ready to take over), later
  several servers at once (each project's changes numbered by one writer, live changes shared between
  servers). When downtime starts to matter, or one server gets busy; object storage comes first.
- **[One-click desktop updates](https://github.com/jhudsy/texloom/issues/10)**: once the apps are
  signed.
- **[Direct browser-to-browser sync (WebRTC)](https://github.com/jhudsy/texloom/issues/11)**: small
  benefit for a large cost.
